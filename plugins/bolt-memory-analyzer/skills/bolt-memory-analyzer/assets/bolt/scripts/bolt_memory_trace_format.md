# Bolt Memory Trace Binary Format

Bolt memory trace version 2 is a little-endian, append-only record stream. The
producer is disabled unless `BOLT_MEMORY_TRACE_FILE` is set. Convert captures
with `bolt_memory_perfetto.py` and inspect them through Perfetto Trace Processor.

## Header

Every file starts with a fixed 56-byte header:

| Field | Type | Description |
| --- | --- | --- |
| magic | `char[8]` | `BLTMEM2\0` |
| major version | `u16` | `2` |
| minor version | `u16` | Backward-compatible additions |
| header size | `u32` | Bytes including extensions |
| endian marker | `u32` | `0x01020304` |
| flags | `u32` | Bit 0: stacks, bit 1: pool filter |
| realtime start | `u64` | Unix epoch nanoseconds |
| monotonic start | `u64` | Monotonic nanoseconds |
| pid | `u32` | Producer process ID |
| reserved | `u32` | Must be zero |
| session ID | `u64` | Per-process trace identifier |

Readers must reject unsupported major versions. They may skip header bytes
beyond the size defined by the version they understand.

## Record Framing

Each record starts with:

| Field | Type | Description |
| --- | --- | --- |
| type | `u16` | Record type |
| flags | `u16` | Type-specific flags |
| payload size | `u32` | Number of bytes after the record header |

Readers must skip unknown record types using `payload size`. New record types
may be introduced in a minor version without breaking existing readers.

## Record Types

### Pool definition (`type=1`)

`pool_id:u32`, `name_size:u32`, followed by UTF-8 pool name bytes.

### Stack definition (`type=2`)

`stack_id:u32`, `encoding:u32`, `data_size:u32`, followed by stack data.
Encoding `2` is an array of little-endian `u64` program counters ordered from
leaf to root. Stacks are interned and written once.

### Event (`type=3`)

The payload is currently 80 bytes:

| Field | Type |
| --- | --- |
| sequence | `u64` |
| monotonic timestamp | `u64` nanoseconds |
| allocation ID | `u64` |
| related allocation ID | `u64` |
| address | `u64` |
| old address | `u64` |
| size | `u64` |
| old size | `u64` |
| pool ID | `u32` |
| stack ID | `u32` |
| thread ID | `u32` |
| operation | `u8` |
| flags | `u8` |
| reserved | `u16` |

Operations are `1=alloc`, `2=free`, and `3=grow`. An allocation ID identifies
one logical lifetime and remains stable across grow operations. Addresses are
diagnostic and must not be used as the only lifecycle key.

### Checkpoint (`type=4`)

Contains sequence, monotonic timestamp, active allocation count, active bytes,
total allocation count, total allocated bytes, and unmatched free count as
seven `u64` values.

Checkpoints support indexed replay without changing event semantics.

### Statistics (`type=5`)

Contains ten `u64` values: events, records, allocated bytes, allocations,
unmatched frees, address reuse count, stack capture errors, write flushes,
active allocations, and active bytes.

### Trailer (`type=6`)

Contains monotonic end, realtime end, records, events, last sequence, unmatched
frees, stack capture errors, active allocations, and active bytes as nine
`u64` values.

The presence of a valid trailer marks a clean shutdown. A missing trailer means
the trace may be truncated, but complete framed records before the truncation
remain readable.

### Mapping definition (`type=7`)

Contains `mapping_id:u32`, `path_size:u32`, `start:u64`, `limit:u64`,
`file_offset:u64`, followed by UTF-8 path bytes. Executable mappings allow the
offline converter to symbolize raw program counters after capture.

### Configuration (`type=8`)

Contains stack minimum bytes, buffer bytes, checkpoint event interval as three
`u64` values, maximum stack frames and pool-regex size as two `u32` values,
followed by UTF-8 pool-regex bytes.

## Time Semantics

Events use a monotonic clock so wall-clock adjustments cannot reorder memory
operations. The header stores realtime and monotonic anchors for correlation
with external logs. Sequence numbers define total recorder order when multiple
threads have equal or near-equal timestamps.

## Performance Properties

- No work is performed unless `BOLT_MEMORY_TRACE_FILE` is set.
- Pool names and raw stacks are interned.
- Allocation-thread stack capture records program counters only.
- Symbolization happens offline in the Perfetto converter and analyzer.
- Free events reuse the allocation stack and do not capture another stack.
- Writes are buffered and checkpoints are periodic.
