/*
 * Copyright (c) ByteDance Ltd. and/or its affiliates
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include "bolt/common/memory/MemoryTraceRecorder.h"
#include "bolt/common/process/StackTrace.h"

#include <folly/Likely.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <limits>
#include <mutex>
#include <optional>
#include <regex>
#include <sstream>
#include <string>
#include <type_traits>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include <sys/syscall.h>
#include <unistd.h>

namespace bytedance::bolt::memory {
namespace {

constexpr char kBinaryMagic[8] = {'B', 'L', 'T', 'M', 'E', 'M', '2', '\0'};
constexpr uint16_t kBinaryMajorVersion = 2;
constexpr uint16_t kBinaryMinorVersion = 0;
constexpr uint32_t kBinaryHeaderSize = 56;
constexpr uint32_t kEndianMarker = 0x01020304;
constexpr uint64_t kDefaultBufferBytes = 1 << 20;
constexpr uint64_t kDefaultCheckpointEvents = 1 << 16;

enum class RecordType : uint16_t {
  kPoolDefinition = 1,
  kStackDefinition = 2,
  kEvent = 3,
  kCheckpoint = 4,
  kStats = 5,
  kTrailer = 6,
  kMappingDefinition = 7,
  kConfiguration = 8,
};

enum class EventOperation : uint8_t {
  kAlloc = 1,
  kFree = 2,
  kGrow = 3,
};

uint64_t parsePositiveEnv(const char* name, uint64_t defaultValue) {
  const char* value = std::getenv(name);
  if (value == nullptr || *value == '\0') {
    return defaultValue;
  }
  char* end = nullptr;
  const auto parsed = std::strtoull(value, &end, 10);
  return end != value && *end == '\0' && parsed > 0 ? parsed : defaultValue;
}

template <typename T>
void appendLittleEndian(std::vector<char>& output, T value) {
  static_assert(std::is_integral_v<T>);
  using Unsigned = std::make_unsigned_t<T>;
  auto unsignedValue = static_cast<Unsigned>(value);
  for (size_t i = 0; i < sizeof(T); ++i) {
    output.push_back(static_cast<char>((unsignedValue >> (i * 8)) & 0xff));
  }
}

void appendBytes(
    std::vector<char>& output,
    const void* data,
    size_t dataSize) {
  const auto* begin = static_cast<const char*>(data);
  output.insert(output.end(), begin, begin + dataSize);
}

struct RawStackHash {
  size_t operator()(const std::vector<uint64_t>& stack) const {
    size_t hash = stack.size();
    for (const auto address : stack) {
      hash ^= std::hash<uint64_t>{}(address) + 0x9e3779b9 + (hash << 6) +
          (hash >> 2);
    }
    return hash;
  }
};

class RecorderState {
 public:
  RecorderState() {
    const char* traceFile = std::getenv("BOLT_MEMORY_TRACE_FILE");
    if (traceFile == nullptr || std::string(traceFile).empty()) {
      return;
    }

    out_.open(
        traceFile,
        std::ios::out | std::ios::trunc | std::ios::binary);
    if (!out_.is_open()) {
      return;
    }

    const char* stackEnv = std::getenv("BOLT_MEMORY_TRACE_STACKS");
    captureStacks_ = stackEnv == nullptr || std::string(stackEnv) != "0";
    stackMinBytes_ = parsePositiveEnv("BOLT_MEMORY_TRACE_STACK_MIN_BYTES", 1);
    flushBytes_ = parsePositiveEnv(
        "BOLT_MEMORY_TRACE_BUFFER_BYTES", kDefaultBufferBytes);
    checkpointEvents_ = parsePositiveEnv(
        "BOLT_MEMORY_TRACE_CHECKPOINT_EVENTS", kDefaultCheckpointEvents);

    const char* poolRegex = std::getenv("BOLT_MEMORY_TRACE_POOL_REGEX");
    if (poolRegex != nullptr && !std::string(poolRegex).empty()) {
      poolRegexText_ = poolRegex;
      poolRegex_.emplace(poolRegexText_);
    }

    realtimeStartNs_ = nowRealtimeNs();
    monotonicStartNs_ = nowMonotonicNs();
    pid_ = static_cast<uint32_t>(::getpid());
    binaryBuffer_.reserve(flushBytes_ + 4096);
    writeHeader();
    writeConfiguration();
    writeMappings();
    enabled_ = out_.good();
  }

  ~RecorderState() {
    if (!enabled_) {
      return;
    }
    std::lock_guard<std::mutex> lock(mutex_);
    writeMappings();
    writeStatsRecord();
    writeTrailerRecord();
    flushBinaryBuffer();
  }

  bool enabled() const {
    return enabled_;
  }

  void
  recordAlloc(const std::string& poolName, const void* addr, int64_t size) {
    recordEvent(
        EventOperation::kAlloc, poolName, addr, size, nullptr, 0);
  }

  void recordFree(const std::string& poolName, const void* addr, int64_t size) {
    recordEvent(
        EventOperation::kFree, poolName, addr, size, nullptr, 0);
  }

  void recordGrow(
      const std::string& poolName,
      const void* oldAddr,
      const void* addr,
      int64_t oldSize,
      int64_t size) {
    recordEvent(
        EventOperation::kGrow,
        poolName,
        addr,
        size,
        oldAddr,
        oldSize);
  }

 private:
  void writeHeader() {
    std::vector<char> header;
    header.reserve(kBinaryHeaderSize);
    appendBytes(header, kBinaryMagic, sizeof(kBinaryMagic));
    appendLittleEndian(header, kBinaryMajorVersion);
    appendLittleEndian(header, kBinaryMinorVersion);
    appendLittleEndian(header, kBinaryHeaderSize);
    appendLittleEndian(header, kEndianMarker);
    uint32_t flags = captureStacks_ ? 1 : 0;
    flags |= poolRegexText_.empty() ? 0 : 2;
    appendLittleEndian(header, flags);
    appendLittleEndian(header, realtimeStartNs_);
    appendLittleEndian(header, monotonicStartNs_);
    appendLittleEndian(header, pid_);
    appendLittleEndian(header, uint32_t{0});
    appendLittleEndian(
        header, realtimeStartNs_ ^ monotonicStartNs_ ^ uint64_t{pid_});
    out_.write(header.data(), header.size());
    out_.flush();
  }

  void writeMappings() {
    std::ifstream maps("/proc/self/maps");
    std::string line;
    while (std::getline(maps, line)) {
      std::istringstream input(line);
      std::string range;
      std::string permissions;
      std::string offsetText;
      std::string device;
      uint64_t inode = 0;
      if (!(input >> range >> permissions >> offsetText >> device >> inode) ||
          permissions.find('x') == std::string::npos) {
        continue;
      }
      std::string path;
      std::getline(input >> std::ws, path);
      if (path.empty() || path.front() == '[') {
        continue;
      }
      const auto separator = range.find('-');
      if (separator == std::string::npos) {
        continue;
      }
      char* end = nullptr;
      const uint64_t start =
          std::strtoull(range.substr(0, separator).c_str(), &end, 16);
      if (end == nullptr || *end != '\0') {
        continue;
      }
      const uint64_t limit =
          std::strtoull(range.substr(separator + 1).c_str(), &end, 16);
      if (end == nullptr || *end != '\0') {
        continue;
      }
      const uint64_t fileOffset = std::strtoull(offsetText.c_str(), &end, 16);
      if (end == nullptr || *end != '\0') {
        continue;
      }
      constexpr const char* kDeletedSuffix = " (deleted)";
      constexpr size_t kDeletedSuffixSize = 10;
      if (path.size() >= kDeletedSuffixSize &&
          path.compare(
              path.size() - kDeletedSuffixSize,
              kDeletedSuffixSize,
              kDeletedSuffix) == 0) {
        path.resize(path.size() - kDeletedSuffixSize);
      }
      const auto mappingKey = std::to_string(start) + ":" +
          std::to_string(limit) + ":" + std::to_string(fileOffset) + ":" +
          path;
      if (!mappingKeys_.insert(mappingKey).second) {
        continue;
      }
      const auto pathSize = static_cast<uint32_t>(std::min<size_t>(
          path.size(), std::numeric_limits<uint32_t>::max()));
      appendRecordHeader(
          RecordType::kMappingDefinition,
          sizeof(uint32_t) * 2 + sizeof(uint64_t) * 3 + pathSize);
      appendLittleEndian(binaryBuffer_, nextMappingId_++);
      appendLittleEndian(binaryBuffer_, pathSize);
      appendLittleEndian(binaryBuffer_, start);
      appendLittleEndian(binaryBuffer_, limit);
      appendLittleEndian(binaryBuffer_, fileOffset);
      appendBytes(binaryBuffer_, path.data(), pathSize);
    }
  }

  void writeConfiguration() {
    const auto regexSize = static_cast<uint32_t>(std::min<size_t>(
        poolRegexText_.size(), std::numeric_limits<uint32_t>::max()));
    appendRecordHeader(
        RecordType::kConfiguration,
        sizeof(uint64_t) * 3 + sizeof(uint32_t) * 2 + regexSize);
    appendLittleEndian(binaryBuffer_, stackMinBytes_);
    appendLittleEndian(binaryBuffer_, flushBytes_);
    appendLittleEndian(binaryBuffer_, checkpointEvents_);
    appendLittleEndian(binaryBuffer_, uint32_t{75});
    appendLittleEndian(binaryBuffer_, regexSize);
    appendBytes(binaryBuffer_, poolRegexText_.data(), regexSize);
  }

  static uint64_t nowMonotonicNs() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
  }

  static uint64_t nowRealtimeNs() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
  }

  static uint32_t currentThreadId() {
    return static_cast<uint32_t>(::syscall(SYS_gettid));
  }

  bool poolMatches(const std::string& poolName) const {
    if (!poolRegex_.has_value()) {
      return true;
    }
    thread_local std::unordered_map<std::string, bool> matchCache;
    const auto cached = matchCache.find(poolName);
    if (cached != matchCache.end()) {
      return cached->second;
    }
    return matchCache.emplace(poolName, std::regex_match(poolName, *poolRegex_))
        .first->second;
  }

  static uint64_t pointerValue(const void* ptr) {
    return reinterpret_cast<uint64_t>(ptr);
  }

  std::vector<uint64_t> captureCurrentStack(
      EventOperation operation,
      int64_t size) {
    if (!captureStacks_ || operation == EventOperation::kFree ||
        static_cast<uint64_t>(size) < stackMinBytes_) {
      return {};
    }
    try {
      const process::StackTrace capturedStack(3);
      const auto& frames = capturedStack.getStack();
      std::vector<uint64_t> stack;
      stack.reserve(frames.size());
      for (const auto* frame : frames) {
        stack.push_back(reinterpret_cast<uint64_t>(frame));
      }
      return stack;
    } catch (...) {
      stackCaptureErrors_.fetch_add(1, std::memory_order_relaxed);
      return {};
    }
  }

  uint32_t internPool(const std::string& poolName) {
    const auto it = poolToId_.find(poolName);
    if (it != poolToId_.end()) {
      return it->second;
    }
    const uint32_t poolId = nextPoolId_++;
    poolToId_[poolName] = poolId;
    const auto nameSize = static_cast<uint32_t>(std::min<size_t>(
        poolName.size(), std::numeric_limits<uint32_t>::max()));
    appendRecordHeader(
        RecordType::kPoolDefinition,
        sizeof(uint32_t) * 2 + nameSize);
    appendLittleEndian(binaryBuffer_, poolId);
    appendLittleEndian(binaryBuffer_, nameSize);
    appendBytes(binaryBuffer_, poolName.data(), nameSize);
    return poolId;
  }

  uint32_t internStack(const std::vector<uint64_t>& stack) {
    if (stack.empty()) {
      return 0;
    }

    const auto it = stackToId_.find(stack);
    if (it != stackToId_.end()) {
      return it->second;
    }

    const uint32_t stackId = nextStackId_++;
    stackToId_[stack] = stackId;

    const auto frameCount = static_cast<uint32_t>(std::min<size_t>(
        stack.size(), std::numeric_limits<uint32_t>::max() / sizeof(uint64_t)));
    const uint32_t stackSize = frameCount * sizeof(uint64_t);
    appendRecordHeader(
        RecordType::kStackDefinition,
        sizeof(uint32_t) * 3 + stackSize);
    appendLittleEndian(binaryBuffer_, stackId);
    appendLittleEndian(binaryBuffer_, uint32_t{2});
    appendLittleEndian(binaryBuffer_, stackSize);
    for (uint32_t i = 0; i < frameCount; ++i) {
      appendLittleEndian(binaryBuffer_, stack[i]);
    }
    return stackId;
  }

  void appendRecordHeader(RecordType type, uint32_t payloadSize) {
    appendLittleEndian(binaryBuffer_, static_cast<uint16_t>(type));
    appendLittleEndian(binaryBuffer_, uint16_t{0});
    appendLittleEndian(binaryBuffer_, payloadSize);
    ++totalRecords_;
  }

  void appendEventRecord(
      EventOperation operation,
      uint64_t seq,
      uint64_t timestampNs,
      uint64_t allocationId,
      uint32_t poolId,
      uint32_t stackId,
      uint32_t tid,
      const void* addr,
      int64_t size,
      const void* oldAddr,
      int64_t oldSize) {
    constexpr uint32_t kEventPayloadSize = 80;
    appendRecordHeader(RecordType::kEvent, kEventPayloadSize);
    appendLittleEndian(binaryBuffer_, seq);
    appendLittleEndian(binaryBuffer_, timestampNs);
    appendLittleEndian(binaryBuffer_, allocationId);
    appendLittleEndian(binaryBuffer_, uint64_t{0});
    appendLittleEndian(binaryBuffer_, pointerValue(addr));
    appendLittleEndian(binaryBuffer_, pointerValue(oldAddr));
    appendLittleEndian(binaryBuffer_, static_cast<uint64_t>(size));
    appendLittleEndian(binaryBuffer_, static_cast<uint64_t>(oldSize));
    appendLittleEndian(binaryBuffer_, poolId);
    appendLittleEndian(binaryBuffer_, stackId);
    appendLittleEndian(binaryBuffer_, tid);
    appendLittleEndian(binaryBuffer_, static_cast<uint8_t>(operation));
    appendLittleEndian(binaryBuffer_, uint8_t{0});
    appendLittleEndian(binaryBuffer_, uint16_t{0});
  }

  void writeCheckpointRecord(uint64_t seq, uint64_t timestampNs) {
    constexpr uint32_t kCheckpointPayloadSize = sizeof(uint64_t) * 7;
    appendRecordHeader(RecordType::kCheckpoint, kCheckpointPayloadSize);
    appendLittleEndian(binaryBuffer_, seq);
    appendLittleEndian(binaryBuffer_, timestampNs);
    appendLittleEndian(
        binaryBuffer_, static_cast<uint64_t>(activeAllocations_.size()));
    appendLittleEndian(binaryBuffer_, activeBytes_);
    appendLittleEndian(binaryBuffer_, totalAllocations_);
    appendLittleEndian(binaryBuffer_, totalAllocatedBytes_);
    appendLittleEndian(binaryBuffer_, unmatchedFrees_);
  }

  void writeStatsRecord() {
    constexpr uint32_t kStatsPayloadSize = sizeof(uint64_t) * 10;
    appendRecordHeader(RecordType::kStats, kStatsPayloadSize);
    appendLittleEndian(binaryBuffer_, totalEvents_);
    appendLittleEndian(binaryBuffer_, totalRecords_);
    appendLittleEndian(binaryBuffer_, totalAllocatedBytes_);
    appendLittleEndian(binaryBuffer_, totalAllocations_);
    appendLittleEndian(binaryBuffer_, unmatchedFrees_);
    appendLittleEndian(binaryBuffer_, addressReuse_);
    appendLittleEndian(
        binaryBuffer_,
        stackCaptureErrors_.load(std::memory_order_relaxed));
    appendLittleEndian(binaryBuffer_, flushCount_);
    appendLittleEndian(
        binaryBuffer_, static_cast<uint64_t>(activeAllocations_.size()));
    appendLittleEndian(binaryBuffer_, activeBytes_);
  }

  void writeTrailerRecord() {
    constexpr uint32_t kTrailerPayloadSize = sizeof(uint64_t) * 9;
    appendRecordHeader(RecordType::kTrailer, kTrailerPayloadSize);
    appendLittleEndian(binaryBuffer_, nowMonotonicNs());
    appendLittleEndian(binaryBuffer_, nowRealtimeNs());
    appendLittleEndian(binaryBuffer_, totalRecords_);
    appendLittleEndian(binaryBuffer_, totalEvents_);
    appendLittleEndian(binaryBuffer_, nextSeq_ - 1);
    appendLittleEndian(binaryBuffer_, unmatchedFrees_);
    appendLittleEndian(
        binaryBuffer_,
        stackCaptureErrors_.load(std::memory_order_relaxed));
    appendLittleEndian(
        binaryBuffer_, static_cast<uint64_t>(activeAllocations_.size()));
    appendLittleEndian(binaryBuffer_, activeBytes_);
  }

  void flushBinaryBuffer() {
    if (binaryBuffer_.empty()) {
      return;
    }
    out_.write(binaryBuffer_.data(), binaryBuffer_.size());
    binaryBuffer_.clear();
    ++flushCount_;
  }

  void recordEvent(
      EventOperation operation,
      const std::string& poolName,
      const void* addr,
      int64_t size,
      const void* oldAddr,
      int64_t oldSize) {
    if (FOLLY_LIKELY(!enabled_) || size <= 0 || !poolMatches(poolName)) {
      return;
    }

    const auto stack = captureCurrentStack(operation, size);
    const uint32_t tid = currentThreadId();
    std::lock_guard<std::mutex> lock(mutex_);
    if (!enabled_) {
      return;
    }

    const uint64_t seq = nextSeq_++;
    const uint64_t timestampNs = nowMonotonicNs();
    const uint32_t poolId = internPool(poolName);
    const uint32_t stackId = internStack(stack);
    uint64_t allocationId = 0;
    const uint64_t address = pointerValue(addr);

    if (operation == EventOperation::kAlloc) {
      const auto existing = activeAllocations_.find(address);
      if (existing != activeAllocations_.end()) {
        ++addressReuse_;
        activeBytes_ -= std::min(activeBytes_, existing->second.size);
      }
      allocationId = nextAllocationId_++;
      activeAllocations_[address] = {allocationId, static_cast<uint64_t>(size)};
      activeBytes_ += size;
      ++totalAllocations_;
      totalAllocatedBytes_ += size;
    } else if (operation == EventOperation::kFree) {
      const auto existing = activeAllocations_.find(address);
      if (existing == activeAllocations_.end()) {
        ++unmatchedFrees_;
      } else {
        allocationId = existing->second.id;
        activeBytes_ -= std::min(activeBytes_, existing->second.size);
        activeAllocations_.erase(existing);
      }
    } else {
      const uint64_t oldAddress = pointerValue(oldAddr);
      const auto existing = activeAllocations_.find(oldAddress);
      if (existing == activeAllocations_.end()) {
        ++unmatchedFrees_;
        allocationId = nextAllocationId_++;
        ++totalAllocations_;
        totalAllocatedBytes_ += size;
      } else {
        allocationId = existing->second.id;
        activeBytes_ -= std::min(activeBytes_, existing->second.size);
        activeAllocations_.erase(existing);
        if (size > oldSize) {
          totalAllocatedBytes_ += size - oldSize;
        }
      }
      activeAllocations_[address] = {
          allocationId, static_cast<uint64_t>(size)};
      activeBytes_ += size;
    }

    appendEventRecord(
        operation,
        seq,
        timestampNs,
        allocationId,
        poolId,
        stackId,
        tid,
        addr,
        size,
        oldAddr,
        oldSize);

    ++totalEvents_;
    if (checkpointEvents_ > 0 && totalEvents_ % checkpointEvents_ == 0) {
      writeCheckpointRecord(seq, timestampNs);
    }
    if (binaryBuffer_.size() >= flushBytes_) {
      flushBinaryBuffer();
      if (!out_.good()) {
        enabled_ = false;
      }
    }
  }

  struct ActiveAllocation {
    uint64_t id;
    uint64_t size;
  };

  std::atomic<bool> enabled_{false};
  bool captureStacks_{true};
  std::ofstream out_;
  std::string poolRegexText_;
  std::optional<std::regex> poolRegex_;
  std::mutex mutex_;
  uint64_t nextSeq_{1};
  uint64_t nextAllocationId_{1};
  uint32_t nextMappingId_{1};
  uint32_t nextPoolId_{1};
  uint32_t nextStackId_{1};
  uint64_t realtimeStartNs_{0};
  uint64_t monotonicStartNs_{0};
  uint32_t pid_{0};
  uint64_t stackMinBytes_{1};
  uint64_t flushBytes_{kDefaultBufferBytes};
  uint64_t checkpointEvents_{kDefaultCheckpointEvents};
  uint64_t totalRecords_{0};
  uint64_t totalEvents_{0};
  uint64_t totalAllocations_{0};
  uint64_t totalAllocatedBytes_{0};
  uint64_t unmatchedFrees_{0};
  uint64_t addressReuse_{0};
  uint64_t flushCount_{0};
  uint64_t activeBytes_{0};
  std::atomic<uint64_t> stackCaptureErrors_{0};
  std::vector<char> binaryBuffer_;
  std::unordered_map<std::string, uint32_t> poolToId_;
  std::unordered_map<std::vector<uint64_t>, uint32_t, RawStackHash> stackToId_;
  std::unordered_map<uint64_t, ActiveAllocation> activeAllocations_;
  std::unordered_set<std::string> mappingKeys_;
};

RecorderState& recorderState() {
  static RecorderState state;
  return state;
}

} // namespace

bool MemoryTraceRecorder::enabled() {
  return recorderState().enabled();
}

void MemoryTraceRecorder::recordAlloc(
    const std::string& poolName,
    const void* addr,
    int64_t size) {
  recorderState().recordAlloc(poolName, addr, size);
}

void MemoryTraceRecorder::recordFree(
    const std::string& poolName,
    const void* addr,
    int64_t size) {
  recorderState().recordFree(poolName, addr, size);
}

void MemoryTraceRecorder::recordGrow(
    const std::string& poolName,
    const void* oldAddr,
    const void* addr,
    int64_t oldSize,
    int64_t size) {
  recorderState().recordGrow(poolName, oldAddr, addr, oldSize, size);
}

} // namespace bytedance::bolt::memory
