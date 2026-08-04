#!/usr/bin/env python3
#
# Copyright (c) ByteDance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Shared binary parsing, symbolization, and lifecycle reconstruction."""

from __future__ import annotations

import bisect
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BINARY_MAGIC = b"BLTMEM2\0"
BINARY_HEADER = struct.Struct("<8sHHIIIQQIIQ")
RECORD_HEADER = struct.Struct("<HHI")
EVENT_RECORD = struct.Struct("<QQQQQQQQIIIBBH")

RECORD_POOL_DEFINITION = 1
RECORD_STACK_DEFINITION = 2
RECORD_EVENT = 3
RECORD_CHECKPOINT = 4
RECORD_STATS = 5
RECORD_TRAILER = 6
RECORD_MAPPING_DEFINITION = 7
RECORD_CONFIGURATION = 8

OPERATION_NAMES = {
    1: "alloc",
    2: "free",
    3: "grow",
}


@dataclass
class Allocation:
    pool: str
    addr: str
    size: int
    stack_id: int | None
    start_seq: int
    start_us: int
    end_seq: int | None = None
    end_us: int | None = None
    free_stack_id: int | None = None
    allocation_id: int | None = None


def stack_frames(stack: str) -> list[str]:
    frames: list[str] = []
    for line in stack.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            parts = line.split(None, 2)
            frame = parts[-1] if len(parts) >= 3 else line
        else:
            frame = line
        frames.append(frame)
    return frames or ["(stack capture disabled or unavailable)"]


def flame_frames(stack: str) -> list[str]:
    frames = []
    for frame in stack_frames(stack):
        if frame.startswith("0x"):
            continue
        if "MemoryPoolImpl::allocate" in frame or "MemoryPoolImpl::grow" in frame:
            continue
        if frame in {"main", "__libc_start_main", "_start"}:
            continue
        if frame.startswith("testing::") or "HandleExceptionsInMethodIfSupported" in frame:
            continue
        if frame.startswith("folly::ThreadPoolExecutor") or frame.startswith("folly::CPUThreadPoolExecutor"):
            continue
        if frame.startswith("void folly::detail::function::FunctionTraits"):
            continue
        frames.append(frame)
    frames.reverse()
    return frames or ["(stack capture disabled or unavailable)"]


def read_exact(file, size: int, context: str) -> bytes:
    data = file.read(size)
    if len(data) != size:
        raise ValueError(
            f"{context}: truncated record, expected {size} bytes, got {len(data)}"
        )
    return data


def symbolize_stacks(
    raw_stacks: dict[int, list[int]],
    mappings: list[dict[str, Any]],
) -> dict[int, str]:
    if not raw_stacks:
        return {}

    executable_mappings = sorted(mappings, key=lambda row: row["start"])
    mapping_starts = [row["start"] for row in executable_mappings]
    requests_by_path: dict[str, dict[int, set[int]]] = {}
    resolved: dict[int, str] = {}
    elf_types: dict[str, int | None] = {}

    def find_mapping(address: int) -> dict[str, Any] | None:
        index = bisect.bisect_right(mapping_starts, address) - 1
        if index < 0:
            return None
        mapping = executable_mappings[index]
        return mapping if address < mapping["limit"] else None

    def elf_type(path: str) -> int | None:
        if path in elf_types:
            return elf_types[path]
        try:
            with open(path, "rb") as binary:
                header = binary.read(20)
            if len(header) < 18 or header[:4] != b"\x7fELF":
                result = None
            elif header[5] == 1:
                result = int.from_bytes(header[16:18], "little")
            elif header[5] == 2:
                result = int.from_bytes(header[16:18], "big")
            else:
                result = None
        except OSError:
            result = None
        elf_types[path] = result
        return result

    def symbol_address(mapping: dict[str, Any], address: int) -> int:
        # ET_EXEC uses linked virtual addresses; ET_DYN uses load-relative ones.
        if elf_type(mapping["path"]) == 2:
            return address
        return address - mapping["start"] + mapping["file_offset"]

    for stack_id, addresses in raw_stacks.items():
        labels: list[str] = []
        for address in addresses:
            mapping = find_mapping(address)
            if mapping is None:
                labels.append(f"0x{address:x}")
                continue
            relative_address = symbol_address(mapping, address)
            requests_by_path.setdefault(mapping["path"], {}).setdefault(
                relative_address, set()
            ).add(stack_id)
            labels.append(
                f"{Path(mapping['path']).name}+0x{relative_address:x}"
            )
        resolved[stack_id] = "\n".join(
            f"# {index:<2d} {label}" for index, label in enumerate(labels)
        )

    addr2line = shutil.which("addr2line")
    if addr2line is None:
        return resolved

    symbol_by_location: dict[tuple[str, int], str] = {}
    for path, offsets in requests_by_path.items():
        if not Path(path).is_file():
            continue
        ordered_offsets = sorted(offsets)
        command = [
            addr2line,
            "-f",
            "-C",
            "-e",
            path,
        ]
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                input="".join(f"0x{offset:x}\n" for offset in ordered_offsets),
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        output_lines = result.stdout.splitlines()
        if len(output_lines) < len(ordered_offsets) * 2:
            continue
        for index, offset in enumerate(ordered_offsets):
            function = output_lines[index * 2].strip()
            location = output_lines[index * 2 + 1].strip()
            if function in {"", "??"}:
                continue
            label = function
            if location not in {"", "??:0", "??:?"}:
                label += f" at {location}"
            symbol_by_location[(path, offset)] = label

    for stack_id, addresses in raw_stacks.items():
        labels = []
        for address in addresses:
            mapping = find_mapping(address)
            if mapping is None:
                labels.append(f"0x{address:x}")
                continue
            relative_address = symbol_address(mapping, address)
            labels.append(
                symbol_by_location.get(
                    (mapping["path"], relative_address),
                    f"{Path(mapping['path']).name}+0x{relative_address:x}",
                )
            )
        resolved[stack_id] = "\n".join(
            f"# {index:<2d} {label}" for index, label in enumerate(labels)
        )
    return resolved


def read_trace(path: Path) -> tuple[list[dict[str, Any]], dict[int, str], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    stacks: dict[int, str] = {}
    raw_stacks: dict[int, list[int]] = {}
    pools: dict[int, str] = {}
    mappings: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    trailer: dict[str, Any] | None = None
    record_count = 0
    truncated = False

    with path.open("rb") as file:
        header_data = read_exact(file, BINARY_HEADER.size, f"{path}: header")
        (
            magic,
            major_version,
            minor_version,
            header_size,
            endian_marker,
            flags,
            realtime_start_ns,
            monotonic_start_ns,
            pid,
            _reserved,
            session_id,
        ) = BINARY_HEADER.unpack(header_data)
        if magic != BINARY_MAGIC:
            raise ValueError(
                f"{path}: unsupported trace format; expected Bolt memory trace binary v2"
            )
        if major_version != 2:
            raise ValueError(
                f"{path}: unsupported major version {major_version}; expected 2"
            )
        if endian_marker != 0x01020304:
            raise ValueError(f"{path}: unsupported byte order marker 0x{endian_marker:x}")
        if header_size < BINARY_HEADER.size:
            raise ValueError(
                f"{path}: invalid header size {header_size}, minimum is {BINARY_HEADER.size}"
            )
        if header_size > BINARY_HEADER.size:
            read_exact(
                file,
                header_size - BINARY_HEADER.size,
                f"{path}: extended header",
            )

        metadata = {
            "type": "metadata",
            "version": f"{major_version}.{minor_version}",
            "format": "binary",
            "capture_stacks": bool(flags & 1),
            "pool_filter_enabled": bool(flags & 2),
            "realtime_start_ns": realtime_start_ns,
            "monotonic_start_ns": monotonic_start_ns,
            "pid": pid,
            "session_id": session_id,
        }

        while True:
            record_offset = file.tell()
            record_header = file.read(RECORD_HEADER.size)
            if not record_header:
                break
            if len(record_header) != RECORD_HEADER.size:
                truncated = True
                break
            record_type, record_flags, payload_size = RECORD_HEADER.unpack(
                record_header
            )
            payload = file.read(payload_size)
            if len(payload) != payload_size:
                truncated = True
                break
            record_count += 1

            if record_type == RECORD_POOL_DEFINITION:
                if payload_size < 8:
                    raise ValueError(f"{path}: invalid pool definition record")
                pool_id, name_size = struct.unpack_from("<II", payload)
                if name_size != payload_size - 8:
                    raise ValueError(f"{path}: invalid pool name length")
                pools[pool_id] = payload[8:].decode("utf-8", errors="replace")
            elif record_type == RECORD_STACK_DEFINITION:
                if payload_size < 12:
                    raise ValueError(f"{path}: invalid stack definition record")
                stack_id, encoding, stack_size = struct.unpack_from("<III", payload)
                if stack_size != payload_size - 12:
                    raise ValueError(f"{path}: invalid stack data length")
                if encoding != 2:
                    raise ValueError(
                        f"{path}: unsupported stack encoding {encoding}"
                    )
                if stack_size % 8 != 0:
                    raise ValueError(f"{path}: invalid raw stack data length")
                raw_stacks[stack_id] = [
                    value[0] for value in struct.iter_unpack("<Q", payload[12:])
                ]
            elif record_type == RECORD_EVENT:
                if payload_size < EVENT_RECORD.size:
                    raise ValueError(
                        f"{path}: invalid event record size {payload_size}"
                    )
                (
                    seq,
                    timestamp_ns,
                    allocation_id,
                    related_allocation_id,
                    address,
                    old_address,
                    size,
                    old_size,
                    pool_id,
                    stack_id,
                    tid,
                    operation,
                    event_flags,
                    _event_reserved,
                ) = EVENT_RECORD.unpack_from(payload)
                operation_name = OPERATION_NAMES.get(operation)
                if operation_name is None:
                    continue
                event: dict[str, Any] = {
                    "type": "event",
                    "seq": seq,
                    "time_ns": timestamp_ns,
                    "time_us": timestamp_ns // 1000,
                    "pid": pid,
                    "tid": tid,
                    "allocation_id": allocation_id,
                    "op": operation_name,
                    "pool": pools.get(pool_id, f"(pool {pool_id})"),
                    "addr": f"0x{address:x}",
                    "size": size,
                }
                if stack_id:
                    event["stack_id"] = stack_id
                if related_allocation_id:
                    event["related_allocation_id"] = related_allocation_id
                if old_address:
                    event["old_addr"] = f"0x{old_address:x}"
                    event["old_size"] = old_size
                if event_flags:
                    event["flags"] = event_flags
                events.append(event)
            elif record_type == RECORD_CHECKPOINT:
                if payload_size >= 56:
                    (
                        seq,
                        timestamp_ns,
                        active_allocations,
                        active_bytes,
                        total_allocations,
                        total_allocated_bytes,
                        unmatched_frees,
                    ) = struct.unpack_from("<QQQQQQQ", payload)
                    metadata["last_checkpoint"] = {
                        "seq": seq,
                        "time_ns": timestamp_ns,
                        "active_allocations": active_allocations,
                        "active_bytes": active_bytes,
                        "total_allocations": total_allocations,
                        "total_allocated_bytes": total_allocated_bytes,
                        "unmatched_frees": unmatched_frees,
                    }
            elif record_type == RECORD_STATS:
                if payload_size >= 80:
                    values = struct.unpack_from("<QQQQQQQQQQ", payload)
                    metadata["stats"] = {
                        "events": values[0],
                        "records": values[1],
                        "allocated_bytes": values[2],
                        "allocations": values[3],
                        "unmatched_frees": values[4],
                        "address_reuse": values[5],
                        "stack_capture_errors": values[6],
                        "flushes": values[7],
                        "active_allocations": values[8],
                        "active_bytes": values[9],
                    }
            elif record_type == RECORD_TRAILER:
                if payload_size >= 72:
                    values = struct.unpack_from("<QQQQQQQQQ", payload)
                    trailer = {
                        "monotonic_end_ns": values[0],
                        "realtime_end_ns": values[1],
                        "records": values[2],
                        "events": values[3],
                        "last_seq": values[4],
                        "unmatched_frees": values[5],
                        "stack_capture_errors": values[6],
                        "active_allocations": values[7],
                        "active_bytes": values[8],
                    }
            elif record_type == RECORD_MAPPING_DEFINITION:
                if payload_size < 32:
                    raise ValueError(f"{path}: invalid mapping definition record")
                (
                    mapping_id,
                    path_size,
                    start,
                    limit,
                    file_offset,
                ) = struct.unpack_from("<IIQQQ", payload)
                if path_size != payload_size - 32:
                    raise ValueError(f"{path}: invalid mapping path length")
                mappings.append(
                    {
                        "mapping_id": mapping_id,
                        "start": start,
                        "limit": limit,
                        "file_offset": file_offset,
                        "path": payload[32:].decode(
                            "utf-8", errors="replace"
                        ),
                    }
                )
            elif record_type == RECORD_CONFIGURATION:
                if payload_size < 32:
                    raise ValueError(f"{path}: invalid configuration record")
                (
                    stack_min_bytes,
                    buffer_bytes,
                    checkpoint_events,
                    max_stack_frames,
                    regex_size,
                ) = struct.unpack_from("<QQQII", payload)
                if regex_size != payload_size - 32:
                    raise ValueError(f"{path}: invalid pool regex length")
                metadata["configuration"] = {
                    "stack_min_bytes": stack_min_bytes,
                    "buffer_bytes": buffer_bytes,
                    "checkpoint_events": checkpoint_events,
                    "max_stack_frames": max_stack_frames,
                    "pool_regex": payload[32:].decode(
                        "utf-8", errors="replace"
                    ),
                }

    stacks.update(symbolize_stacks(raw_stacks, mappings))
    integrity_errors: list[str] = []
    if truncated:
        integrity_errors.append("truncated final record")
    if trailer is None:
        integrity_errors.append("missing clean-shutdown trailer")
    else:
        if trailer["events"] != len(events):
            integrity_errors.append(
                f"trailer event count {trailer['events']} != decoded {len(events)}"
            )
        if events and trailer["last_seq"] != events[-1]["seq"]:
            integrity_errors.append(
                f"trailer last sequence {trailer['last_seq']} != decoded {events[-1]['seq']}"
            )
        if trailer["records"] != record_count:
            integrity_errors.append(
                f"trailer record count {trailer['records']} != decoded {record_count}"
            )
    metadata["records_read"] = record_count
    metadata["mappings"] = len(mappings)
    metadata["raw_stacks"] = len(raw_stacks)
    metadata["truncated"] = truncated
    metadata["integrity_errors"] = integrity_errors
    metadata["clean_shutdown"] = not integrity_errors
    if trailer is not None:
        metadata["trailer"] = trailer
    events.sort(key=lambda x: (int(x.get("seq", 0)), int(x.get("time_us", 0))))
    return events, stacks, [metadata]


def build_model(events: list[dict[str, Any]], stacks: dict[int, str]) -> dict[str, Any]:
    active: dict[str, Allocation] = {}
    completed: list[Allocation] = []
    unmatched_frees: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []
    current_by_pool: dict[str, int] = {}
    current_by_stack: dict[int, int] = {}
    peak_bytes = 0
    peak_seq = 0
    peak_us = 0
    total_alloc_bytes = 0

    def add_usage(pool: str, stack_id: int | None, delta: int) -> None:
        current_by_pool[pool] = current_by_pool.get(pool, 0) + delta
        if stack_id is not None:
            current_by_stack[stack_id] = current_by_stack.get(stack_id, 0) + delta

    def allocation_key(event: dict[str, Any], address: str) -> str:
        allocation_id = int(event.get("allocation_id", 0))
        return f"id:{allocation_id}" if allocation_id else f"addr:{address}"

    for event in events:
        op = event.get("op")
        seq = int(event.get("seq", 0))
        time_us = int(event.get("time_us", 0))
        pool = str(event.get("pool", ""))
        addr = str(event.get("addr", "0x0"))
        size = int(event.get("size", 0))
        stack_id = int(event["stack_id"]) if "stack_id" in event else None
        allocation_id = int(event.get("allocation_id", 0)) or None
        key = allocation_key(event, addr)

        if op == "alloc":
            allocation = Allocation(
                pool,
                addr,
                size,
                stack_id,
                seq,
                time_us,
                allocation_id=allocation_id,
            )
            active[key] = allocation
            add_usage(pool, stack_id, size)
            total_alloc_bytes += size
        elif op == "free":
            allocation = active.pop(key, None)
            if allocation is None:
                unmatched_frees.append(event)
            else:
                allocation.end_seq = seq
                allocation.end_us = time_us
                allocation.free_stack_id = stack_id
                completed.append(allocation)
                add_usage(allocation.pool, allocation.stack_id, -allocation.size)
        elif op == "grow":
            old_addr = str(event.get("old_addr", addr))
            old_size = int(event.get("old_size", 0))
            allocation = active.pop(key, None)
            if allocation is None:
                unmatched_frees.append(event)
                allocation = Allocation(
                    pool,
                    addr,
                    size,
                    stack_id,
                    seq,
                    time_us,
                    allocation_id=allocation_id,
                )
                active[key] = allocation
                add_usage(pool, stack_id, size)
                total_alloc_bytes += size
            else:
                add_usage(allocation.pool, allocation.stack_id, -allocation.size)
                allocation.addr = addr
                allocation.size = size
                active[key] = allocation
                add_usage(allocation.pool, allocation.stack_id, size)
                total_alloc_bytes += max(0, size - old_size)

        current_total = sum(v for v in current_by_pool.values() if v > 0)
        if current_total > peak_bytes:
            peak_bytes = current_total
            peak_seq = seq
            peak_us = time_us
        timeline.append(
            {
                "seq": seq,
                "time_us": time_us,
                "bytes": current_total,
                "op": op,
                "pool": pool,
                "size": size,
            }
        )

    leaked = list(active.values())
    all_allocations = completed + leaked

    pools: dict[str, dict[str, Any]] = {}
    for allocation in all_allocations:
        row = pools.setdefault(
            allocation.pool,
            {"pool": allocation.pool, "allocations": 0, "bytes": 0, "live": 0, "live_bytes": 0},
        )
        row["allocations"] += 1
        row["bytes"] += allocation.size
        if allocation.end_seq is None:
            row["live"] += 1
            row["live_bytes"] += allocation.size

    stack_rows: dict[int, dict[str, Any]] = {}
    for allocation in all_allocations:
        sid = allocation.stack_id or 0
        row = stack_rows.setdefault(
            sid,
            {
                "stack_id": sid,
                "allocations": 0,
                "bytes": 0,
                "live": 0,
                "live_bytes": 0,
                "stack": stacks.get(sid, "(stack capture disabled or unavailable)"),
            },
        )
        row["allocations"] += 1
        row["bytes"] += allocation.size
        if allocation.end_seq is None:
            row["live"] += 1
            row["live_bytes"] += allocation.size

    lifetimes = []
    allocation_rows = []
    for allocation in all_allocations:
        end_us = allocation.end_us if allocation.end_us is not None else timeline[-1]["time_us"] if timeline else allocation.start_us
        lifetimes.append(
            {
                "pool": allocation.pool,
                "allocation_id": allocation.allocation_id,
                "addr": allocation.addr,
                "size": allocation.size,
                "stack_id": allocation.stack_id or 0,
                "start_seq": allocation.start_seq,
                "end_seq": allocation.end_seq,
                "start_us": allocation.start_us,
                "end_us": allocation.end_us,
                "lifetime_us": max(0, end_us - allocation.start_us),
                "live": allocation.end_seq is None,
            }
        )
        allocation_rows.append(
            {
                "pool": allocation.pool,
                "allocation_id": allocation.allocation_id,
                "addr": allocation.addr,
                "size": allocation.size,
                "stack_id": allocation.stack_id or 0,
                "start_seq": allocation.start_seq,
                "end_seq": allocation.end_seq,
                "start_us": allocation.start_us,
                "end_us": allocation.end_us,
            }
        )

    leaked_lifetimes = [row for row in lifetimes if row["live"]]
    leaked_lifetimes.sort(key=lambda x: (x["size"], x["lifetime_us"]), reverse=True)
    long_lived = sorted(lifetimes, key=lambda x: x["lifetime_us"], reverse=True)
    anomalous_pools = [
        row
        for row in sorted(
            pools.values(),
            key=lambda x: (x["live_bytes"], x["live"], x["bytes"]),
            reverse=True,
        )
        if row["live"] > 0
    ]

    flame_children: list[dict[str, Any]] = []
    root_node: dict[str, Any] = {
        "name": "all allocations",
        "bytes": 0,
        "live_bytes": 0,
        "allocations": 0,
        "children": {},
    }
    for allocation in all_allocations:
        sid = allocation.stack_id or 0
        frames = flame_frames(stacks.get(sid, "(stack capture disabled or unavailable)"))
        node = root_node
        node["bytes"] += allocation.size
        node["allocations"] += 1
        if allocation.end_seq is None:
            node["live_bytes"] += allocation.size
        for frame in frames:
            children = node["children"]
            child = children.setdefault(
                frame,
                {
                    "name": frame,
                    "bytes": 0,
                    "live_bytes": 0,
                    "allocations": 0,
                    "children": {},
                },
            )
            child["bytes"] += allocation.size
            child["allocations"] += 1
            if allocation.end_seq is None:
                child["live_bytes"] += allocation.size
            node = child

    def freeze_flame_node(node: dict[str, Any]) -> dict[str, Any]:
        children = [
            freeze_flame_node(child)
            for child in sorted(
                node["children"].values(),
                key=lambda x: (x["bytes"], x["allocations"]),
                reverse=True,
            )
        ]
        return {
            "name": node["name"],
            "bytes": node["bytes"],
            "live_bytes": node["live_bytes"],
            "allocations": node["allocations"],
            "children": children,
        }

    flame_children = [freeze_flame_node(child) for child in root_node["children"].values()]
    flame_children.sort(key=lambda x: (x["bytes"], x["allocations"]), reverse=True)

    return {
        "summary": {
            "events": len(events),
            "allocations": len(all_allocations),
            "completed_allocations": len(completed),
            "live_allocations": len(leaked),
            "unmatched_frees": len(unmatched_frees),
            "total_alloc_bytes": total_alloc_bytes,
            "peak_bytes": peak_bytes,
            "peak_seq": peak_seq,
            "peak_time_us": peak_us,
            "duration_us": (timeline[-1]["time_us"] - timeline[0]["time_us"]) if len(timeline) >= 2 else 0,
        },
        "timeline": timeline,
        "allocations": allocation_rows,
        "stack_frames": {
            str(sid): flame_frames(stack)
            for sid, stack in stacks.items()
        },
        "pools": sorted(pools.values(), key=lambda x: x["bytes"], reverse=True),
        "stacks": sorted(stack_rows.values(), key=lambda x: x["bytes"], reverse=True),
        "lifetimes": sorted(lifetimes, key=lambda x: x["size"], reverse=True)[:1000],
        "anomalies": {
            "pools_with_live_allocations": anomalous_pools[:100],
            "live_allocations": leaked_lifetimes[:100],
            "longest_lived_allocations": long_lived[:100],
        },
        "flamegraph": {
            "name": root_node["name"],
            "bytes": root_node["bytes"],
            "live_bytes": root_node["live_bytes"],
            "allocations": root_node["allocations"],
            "children": flame_children,
        },
        "unmatched_frees": unmatched_frees[:1000],
    }
