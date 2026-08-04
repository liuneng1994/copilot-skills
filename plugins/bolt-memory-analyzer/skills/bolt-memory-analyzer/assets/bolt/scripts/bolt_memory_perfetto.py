#!/usr/bin/env python3
#
# Copyright (c) ByteDance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Convert Bolt memory traces to Perfetto and run Trace Processor workflows."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

import bolt_memory_trace_core as trace_core


BINARY_MAGIC = b"BLTMEM2\0"
BINARY_HEADER = struct.Struct("<8sHHIIIQQIIQ")
RECORD_HEADER = struct.Struct("<HHI")
EVENT_RECORD = struct.Struct("<QQQQQQQQIIIBBH")

RECORD_POOL_DEFINITION = 1
RECORD_STACK_DEFINITION = 2
RECORD_EVENT = 3
RECORD_MAPPING_DEFINITION = 7

OPERATION_NAMES = {1: "alloc", 2: "free", 3: "grow"}

TRACK_EVENT_INSTANT = 3
TRACK_EVENT_COUNTER = 4
CLOCK_REALTIME = 1
CLOCK_MONOTONIC = 3
CLOCK_MONOTONIC_COARSE = 4
COUNTER_UNIT_COUNT = 2
COUNTER_UNIT_BYTES = 3
SEQUENCE_ID = 0xB017
TRACE_PROCESSOR_WRAPPER_URL = "https://get.perfetto.dev/trace_processor"


@dataclass
class TraceHeader:
    major: int
    minor: int
    realtime_start_ns: int
    monotonic_start_ns: int
    pid: int
    session_id: int


@dataclass
class Mapping:
    mapping_id: int
    start: int
    limit: int
    file_offset: int
    path: str


@dataclass
class Definitions:
    header: TraceHeader
    pools: dict[int, str]
    mappings: list[Mapping]
    raw_stacks: dict[int, list[int]]
    symbols: dict[int, str]
    peak_sequence: int


@dataclass
class StackStats:
    allocated_bytes: int = 0
    freed_bytes: int = 0
    allocation_count: int = 0
    free_count: int = 0


@dataclass
class ActiveAllocation:
    stack_id: int
    size: int


@dataclass
class ProfileSnapshot:
    timestamp_ns: int
    start_timestamp_ns: int
    stats: dict[int, StackStats]


def encode_varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    output = bytearray()
    while value >= 0x80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def proto_key(field: int, wire_type: int) -> bytes:
    return encode_varint((field << 3) | wire_type)


def proto_uint(field: int, value: int) -> bytes:
    return proto_key(field, 0) + encode_varint(value)


def proto_int(field: int, value: int) -> bytes:
    return proto_key(field, 0) + encode_varint(value)


def proto_string(field: int, value: str) -> bytes:
    return proto_bytes(field, value.encode())


def proto_bytes(field: int, value: bytes) -> bytes:
    return proto_key(field, 2) + encode_varint(len(value)) + value


def read_header(file: BinaryIO, path: Path) -> TraceHeader:
    data = file.read(BINARY_HEADER.size)
    if len(data) != BINARY_HEADER.size:
        raise ValueError(f"{path}: truncated Bolt memory trace header")
    (
        magic,
        major,
        minor,
        header_size,
        endian_marker,
        _flags,
        realtime_start_ns,
        monotonic_start_ns,
        pid,
        _reserved,
        session_id,
    ) = BINARY_HEADER.unpack(data)
    if magic != BINARY_MAGIC or major != 2:
        raise ValueError(f"{path}: expected Bolt memory trace binary v2")
    if endian_marker != 0x01020304:
        raise ValueError(f"{path}: unsupported byte order")
    if header_size < BINARY_HEADER.size:
        raise ValueError(f"{path}: invalid header size {header_size}")
    if header_size > BINARY_HEADER.size:
        extension = file.read(header_size - BINARY_HEADER.size)
        if len(extension) != header_size - BINARY_HEADER.size:
            raise ValueError(f"{path}: truncated extended header")
    return TraceHeader(
        major=major,
        minor=minor,
        realtime_start_ns=realtime_start_ns,
        monotonic_start_ns=monotonic_start_ns,
        pid=pid,
        session_id=session_id,
    )


def records(file: BinaryIO, path: Path) -> Iterator[tuple[int, bytes]]:
    while True:
        header = file.read(RECORD_HEADER.size)
        if not header:
            return
        if len(header) != RECORD_HEADER.size:
            raise ValueError(f"{path}: truncated record header")
        record_type, _flags, payload_size = RECORD_HEADER.unpack(header)
        payload = file.read(payload_size)
        if len(payload) != payload_size:
            raise ValueError(f"{path}: truncated record payload")
        yield record_type, payload


def collect_definitions(path: Path, symbolize: bool) -> Definitions:
    pools: dict[int, str] = {}
    mappings: list[Mapping] = []
    raw_stacks: dict[int, list[int]] = {}
    active_sizes: dict[int, int] = {}
    active_bytes = 0
    peak_bytes = -1
    peak_sequence = 0
    with path.open("rb") as file:
        header = read_header(file, path)
        for record_type, payload in records(file, path):
            if record_type == RECORD_POOL_DEFINITION:
                pool_id, name_size = struct.unpack_from("<II", payload)
                pools[pool_id] = payload[8 : 8 + name_size].decode(
                    errors="replace"
                )
            elif record_type == RECORD_MAPPING_DEFINITION:
                mapping_id, path_size, start, limit, file_offset = (
                    struct.unpack_from("<IIQQQ", payload)
                )
                mappings.append(
                    Mapping(
                        mapping_id=mapping_id,
                        start=start,
                        limit=limit,
                        file_offset=file_offset,
                        path=payload[32 : 32 + path_size].decode(
                            errors="replace"
                        ),
                    )
                )
            elif record_type == RECORD_STACK_DEFINITION:
                stack_id, encoding, data_size = struct.unpack_from(
                    "<III", payload
                )
                if encoding != 2 or data_size % 8:
                    continue
                raw_stacks[stack_id] = [
                    address[0]
                    for address in struct.iter_unpack("<Q", payload[12:])
                ]
            elif record_type == RECORD_EVENT:
                (
                    sequence,
                    _timestamp_ns,
                    allocation_id,
                    _related_allocation_id,
                    _address,
                    _old_address,
                    size,
                    old_size,
                    _pool_id,
                    _stack_id,
                    _tid,
                    operation,
                    _event_flags,
                    _reserved,
                ) = EVENT_RECORD.unpack_from(payload)
                if operation == 1:
                    active_sizes[allocation_id] = size
                    active_bytes += size
                elif operation == 2:
                    active_bytes -= active_sizes.pop(allocation_id, 0)
                elif operation == 3:
                    previous = active_sizes.get(allocation_id, old_size)
                    active_sizes[allocation_id] = size
                    active_bytes = max(0, active_bytes - previous + size)
                if active_bytes > peak_bytes:
                    peak_bytes = active_bytes
                    peak_sequence = sequence
    symbol_rows = [
        {
            "mapping_id": mapping.mapping_id,
            "start": mapping.start,
            "limit": mapping.limit,
            "file_offset": mapping.file_offset,
            "path": mapping.path,
        }
        for mapping in mappings
    ]
    symbols = (
        trace_core.symbolize_stacks(raw_stacks, symbol_rows)
        if symbolize
        else {}
    )
    return Definitions(
        header=header,
        pools=pools,
        mappings=mappings,
        raw_stacks=raw_stacks,
        symbols=symbols,
        peak_sequence=peak_sequence,
    )


def trace_packet(**fields: bytes) -> bytes:
    packet = b"".join(fields.values())
    return proto_bytes(1, packet)


def packet_fields(
    *,
    timestamp_ns: int | None = None,
    data_field: int,
    data: bytes,
    sequence_flags: int | None = None,
) -> bytes:
    packet = bytearray()
    if timestamp_ns is not None:
        packet += proto_uint(8, timestamp_ns)
        packet += proto_uint(58, CLOCK_MONOTONIC)
    packet += proto_bytes(data_field, data)
    packet += proto_uint(10, SEQUENCE_ID)
    if sequence_flags is not None:
        packet += proto_uint(13, sequence_flags)
    return bytes(packet)


def debug_uint(name: str, value: int, *, pointer: bool = False) -> bytes:
    annotation = proto_string(10, name)
    annotation += proto_uint(7 if pointer else 3, value)
    return proto_bytes(4, annotation)


def debug_string(name: str, value: str) -> bytes:
    annotation = proto_string(10, name) + proto_string(6, value)
    return proto_bytes(4, annotation)


def process_uuid(header: TraceHeader) -> int:
    return 0xB017000000000000 ^ header.session_id


def event_track_uuid(header: TraceHeader) -> int:
    return process_uuid(header) ^ 0x100


def metadata_track_uuid(header: TraceHeader) -> int:
    return process_uuid(header) ^ 0x101


def bytes_counter_uuid(header: TraceHeader) -> int:
    return process_uuid(header) ^ 0x200


def allocations_counter_uuid(header: TraceHeader) -> int:
    return process_uuid(header) ^ 0x201


def process_descriptor(header: TraceHeader) -> bytes:
    process = proto_int(1, header.pid) + proto_string(
        6, "Bolt memory trace"
    )
    descriptor = proto_uint(1, process_uuid(header))
    descriptor += proto_string(2, "Bolt memory")
    descriptor += proto_bytes(3, process)
    return descriptor


def track_descriptor(
    uuid: int,
    name: str,
    parent_uuid: int,
    *,
    counter_unit: int | None = None,
) -> bytes:
    descriptor = proto_uint(1, uuid)
    descriptor += proto_uint(5, parent_uuid)
    descriptor += proto_string(2, name)
    if counter_unit is not None:
        descriptor += proto_bytes(8, proto_uint(3, counter_unit))
    return descriptor


def clock_snapshot(header: TraceHeader) -> bytes:
    realtime = proto_uint(1, CLOCK_REALTIME) + proto_uint(
        2, header.realtime_start_ns
    )
    monotonic = proto_uint(1, CLOCK_MONOTONIC) + proto_uint(
        2, header.monotonic_start_ns
    )
    monotonic_coarse = proto_uint(1, CLOCK_MONOTONIC_COARSE) + proto_uint(
        2, header.monotonic_start_ns
    )
    return (
        proto_bytes(1, realtime)
        + proto_bytes(1, monotonic)
        + proto_bytes(1, monotonic_coarse)
        + proto_uint(2, CLOCK_MONOTONIC)
    )


def mapping_for_address(
    mappings: list[Mapping], address: int
) -> Mapping | None:
    for mapping in mappings:
        if mapping.start <= address < mapping.limit:
            return mapping
    return None


def symbol_frames(symbols: dict[int, str], stack_id: int) -> list[str]:
    stack = symbols.get(stack_id, "")
    frames = []
    for line in stack.splitlines():
        parts = line.split(None, 2)
        frames.append(parts[-1] if len(parts) >= 3 else line)
    return frames


class PerfettoInterning:
    def __init__(self, definitions: Definitions):
        self.definitions = definitions
        self.next_string_id = 1
        self.next_frame_id = 1
        self.string_ids: dict[str, int] = {}
        self.frame_ids: dict[tuple[int, int, str], int] = {}

    def interned_string(self, field: int, value: str) -> tuple[int, bytes]:
        existing = self.string_ids.get(value)
        if existing is not None:
            return existing, b""
        string_id = self.next_string_id
        self.next_string_id += 1
        self.string_ids[value] = string_id
        message = proto_uint(1, string_id) + proto_bytes(2, value.encode())
        return string_id, proto_bytes(field, message)

    def mapping_messages(self) -> bytes:
        output = bytearray()
        for mapping in self.definitions.mappings:
            path_id, path_message = self.interned_string(17, mapping.path)
            output += path_message
            message = proto_uint(1, mapping.mapping_id)
            message += proto_uint(8, mapping.file_offset)
            message += proto_uint(3, 0)
            message += proto_uint(4, mapping.start)
            message += proto_uint(5, mapping.limit)
            message += proto_uint(6, 0)
            message += proto_uint(7, path_id)
            output += proto_bytes(19, message)
        return bytes(output)

    def stack_message(self, stack_id: int) -> bytes:
        addresses = self.definitions.raw_stacks.get(stack_id, [])
        symbols = symbol_frames(self.definitions.symbols, stack_id)
        output = bytearray()
        callstack_frame_ids = []
        for index, address in enumerate(addresses):
            mapping = mapping_for_address(self.definitions.mappings, address)
            mapping_id = mapping.mapping_id if mapping else 0
            relative_pc = (
                address - mapping.start + mapping.file_offset
                if mapping
                else address
            )
            function = (
                symbols[index]
                if index < len(symbols)
                else (
                    f"{Path(mapping.path).name}+0x{relative_pc:x}"
                    if mapping
                    else f"0x{address:x}"
                )
            )
            frame_key = (mapping_id, relative_pc, function)
            frame_id = self.frame_ids.get(frame_key)
            if frame_id is None:
                frame_id = self.next_frame_id
                self.next_frame_id += 1
                self.frame_ids[frame_key] = frame_id
                function_id, function_message = self.interned_string(
                    5, function
                )
                output += function_message
                frame = proto_uint(1, frame_id)
                frame += proto_uint(2, function_id)
                if mapping_id:
                    frame += proto_uint(3, mapping_id)
                    frame += proto_uint(4, relative_pc)
                frame += proto_uint(7, 1)
                output += proto_bytes(6, frame)
            callstack_frame_ids.append(frame_id)
        callstack = proto_uint(1, stack_id)
        for frame_id in reversed(callstack_frame_ids):
            callstack += proto_uint(2, frame_id)
        output += proto_bytes(7, callstack)
        return bytes(output)


def write_packet(output: BinaryIO, packet: bytes) -> None:
    output.write(proto_bytes(1, packet))


def write_initial_packets(
    output: BinaryIO,
    definitions: Definitions,
    interning: PerfettoInterning,
) -> None:
    write_packet(
        output,
        packet_fields(
            data_field=6,
            data=clock_snapshot(definitions.header),
            sequence_flags=1,
        ),
    )
    write_packet(
        output,
        packet_fields(
            data_field=60,
            data=process_descriptor(definitions.header),
        ),
    )
    parent = process_uuid(definitions.header)
    for descriptor in (
        track_descriptor(
            event_track_uuid(definitions.header),
            "Bolt MemoryPool events",
            parent,
        ),
        track_descriptor(
            metadata_track_uuid(definitions.header),
            "Bolt MemoryPool metadata",
            parent,
        ),
        track_descriptor(
            bytes_counter_uuid(definitions.header),
            "Bolt active bytes",
            parent,
            counter_unit=COUNTER_UNIT_BYTES,
        ),
        track_descriptor(
            allocations_counter_uuid(definitions.header),
            "Bolt active allocations",
            parent,
            counter_unit=COUNTER_UNIT_COUNT,
        ),
    ):
        write_packet(output, packet_fields(data_field=60, data=descriptor))
    mapping_data = interning.mapping_messages()
    if mapping_data:
        write_packet(output, packet_fields(data_field=12, data=mapping_data))
    for pool_id, pool_name in sorted(definitions.pools.items()):
        write_packet(
            output,
            packet_fields(
                timestamp_ns=definitions.header.monotonic_start_ns,
                data_field=11,
                data=pool_definition_event(
                    definitions,
                    pool_id,
                    pool_name,
                ),
                sequence_flags=2,
            ),
        )


def counter_event(track_uuid: int, value: int) -> bytes:
    event = proto_uint(9, TRACK_EVENT_COUNTER)
    event += proto_uint(11, track_uuid)
    event += proto_int(30, value)
    return event


def memory_event(
    definitions: Definitions,
    operation: int,
    sequence: int,
    allocation_id: int,
    related_allocation_id: int,
    address: int,
    old_address: int,
    size: int,
    old_size: int,
    pool_id: int,
    stack_id: int,
    tid: int,
    active_bytes: int,
    active_allocations: int,
) -> bytes:
    name = OPERATION_NAMES[operation]
    event = proto_string(22, "bolt.memory")
    event += proto_string(23, name)
    event += proto_uint(9, TRACK_EVENT_INSTANT)
    event += proto_uint(11, event_track_uuid(definitions.header))
    event += proto_uint(52, allocation_id)
    event += proto_uint(31, bytes_counter_uuid(definitions.header))
    event += proto_int(12, active_bytes)
    event += proto_uint(31, allocations_counter_uuid(definitions.header))
    event += proto_int(12, active_allocations)
    event += debug_uint("seq", sequence)
    event += debug_uint("allocation_id", allocation_id)
    if related_allocation_id:
        event += debug_uint("related_allocation_id", related_allocation_id)
    event += debug_uint("address", address, pointer=True)
    if old_address:
        event += debug_uint("old_address", old_address, pointer=True)
    event += debug_uint("size", size)
    if old_size:
        event += debug_uint("old_size", old_size)
    event += debug_uint("pool_id", pool_id)
    if stack_id:
        event += debug_uint("stack_id", stack_id)
    event += debug_uint("tid", tid)
    return event


def pool_definition_event(
    definitions: Definitions,
    pool_id: int,
    pool_name: str,
) -> bytes:
    event = proto_string(22, "bolt.memory.metadata")
    event += proto_string(23, "pool_definition")
    event += proto_uint(9, TRACK_EVENT_INSTANT)
    event += proto_uint(11, metadata_track_uuid(definitions.header))
    event += debug_uint("pool_id", pool_id)
    event += debug_string("pool", pool_name)
    return event


def stack_definition_event(
    definitions: Definitions,
    stack_id: int,
    leaf_node_id: int,
) -> bytes:
    event = proto_string(22, "bolt.memory.metadata")
    event += proto_string(23, "stack_definition")
    event += proto_uint(9, TRACK_EVENT_INSTANT)
    event += proto_uint(11, metadata_track_uuid(definitions.header))
    event += debug_uint("stack_id", stack_id)
    event += debug_uint("leaf_node_id", leaf_node_id)
    stack = definitions.symbols.get(stack_id)
    if not stack:
        addresses = definitions.raw_stacks.get(stack_id, [])
        stack = "\n".join(f"0x{address:x}" for address in addresses)
    event += debug_string("stack", stack)
    return event


def stack_node_definition_event(
    definitions: Definitions,
    node_id: int,
    parent_id: int,
    name: str,
) -> bytes:
    event = proto_string(22, "bolt.memory.metadata")
    event += proto_string(23, "stack_node_definition")
    event += proto_uint(9, TRACK_EVENT_INSTANT)
    event += proto_uint(11, metadata_track_uuid(definitions.header))
    event += debug_uint("node_id", node_id)
    event += debug_uint("parent_id", parent_id)
    event += debug_string("name", name)
    return event


def stack_node_definitions(
    definitions: Definitions,
) -> tuple[list[tuple[int, int, str]], dict[int, int]]:
    node_ids: dict[tuple[int, str], int] = {}
    stack_leaf_nodes: dict[int, int] = {0: 1}
    rows: list[tuple[int, int, str]] = [(1, 0, "(stack unavailable)")]
    next_node_id = 2
    for stack_id in sorted(definitions.raw_stacks):
        frames = symbol_frames(definitions.symbols, stack_id)
        if not frames:
            frames = [
                f"0x{address:x}"
                for address in reversed(definitions.raw_stacks[stack_id])
            ]
        parent_id = 0
        for frame in reversed(frames):
            key = (parent_id, frame)
            node_id = node_ids.get(key)
            if node_id is None:
                node_id = next_node_id
                next_node_id += 1
                node_ids[key] = node_id
                rows.append((node_id, parent_id, frame))
            parent_id = node_id
        stack_leaf_nodes[stack_id] = parent_id
    return rows, stack_leaf_nodes


def profile_packet(
    definitions: Definitions,
    snapshot: ProfileSnapshot,
    packet_index: int,
) -> bytes:
    process_dump = proto_uint(1, definitions.header.pid)
    process_dump += proto_uint(3, 1)
    process_dump += proto_string(11, "Bolt MemoryPool")
    process_dump += proto_uint(12, 1)
    process_dump += proto_uint(13, 1)
    process_dump += proto_uint(9, snapshot.timestamp_ns)
    process_dump += proto_uint(15, snapshot.start_timestamp_ns)
    for stack_id, stats in sorted(snapshot.stats.items()):
        if not stats.allocated_bytes and not stats.freed_bytes:
            continue
        sample = proto_uint(1, stack_id)
        sample += proto_uint(2, stats.allocated_bytes)
        sample += proto_uint(3, stats.freed_bytes)
        sample += proto_uint(4, snapshot.timestamp_ns)
        sample += proto_uint(5, stats.allocation_count)
        sample += proto_uint(6, stats.free_count)
        process_dump += proto_bytes(2, sample)
    packet = proto_bytes(5, process_dump)
    packet += proto_uint(6, 0)
    packet += proto_uint(7, packet_index)
    return packet


def update_profile_state(
    operation: int,
    allocation_id: int,
    size: int,
    old_size: int,
    stack_id: int,
    active: dict[int, ActiveAllocation],
    stats: dict[int, StackStats],
) -> None:
    effective_stack = stack_id or 0
    if operation == 1:
        active[allocation_id] = ActiveAllocation(effective_stack, size)
        row = stats.setdefault(effective_stack, StackStats())
        row.allocated_bytes += size
        row.allocation_count += 1
        return
    if operation == 2:
        previous = active.pop(allocation_id, None)
        if previous is None:
            return
        row = stats.setdefault(previous.stack_id, StackStats())
        row.freed_bytes += previous.size
        row.free_count += 1
        return
    previous = active.pop(allocation_id, None)
    if previous is not None:
        old_row = stats.setdefault(previous.stack_id, StackStats())
        old_row.freed_bytes += previous.size
        old_row.free_count += 1
    elif old_size:
        old_row = stats.setdefault(effective_stack, StackStats())
        old_row.freed_bytes += old_size
        old_row.free_count += 1
    active[allocation_id] = ActiveAllocation(effective_stack, size)
    new_row = stats.setdefault(effective_stack, StackStats())
    new_row.allocated_bytes += size
    new_row.allocation_count += 1


def convert(
    input_path: Path,
    output_path: Path,
    symbolize: bool,
    snapshot_events: int,
) -> dict[str, int]:
    definitions = collect_definitions(input_path, symbolize)
    interning = PerfettoInterning(definitions)
    stack_nodes, stack_leaf_nodes = stack_node_definitions(definitions)
    emitted_stacks: set[int] = set()
    active: dict[int, int] = {}
    active_bytes = 0
    event_count = 0
    profile_active: dict[int, ActiveAllocation] = {}
    profile_stats: dict[int, StackStats] = {}
    previous_snapshot_ns = definitions.header.monotonic_start_ns
    last_timestamp_ns = definitions.header.monotonic_start_ns
    emitted_snapshot_timestamps: set[int] = set()
    snapshot_count = 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as output:
        write_initial_packets(output, definitions, interning)
        for node_id, parent_id, name in stack_nodes:
            write_packet(
                output,
                packet_fields(
                    timestamp_ns=definitions.header.monotonic_start_ns,
                    data_field=11,
                    data=stack_node_definition_event(
                        definitions,
                        node_id,
                        parent_id,
                        name,
                    ),
                    sequence_flags=2,
                ),
            )
        with input_path.open("rb") as source:
            read_header(source, input_path)
            for record_type, payload in records(source, input_path):
                if record_type != RECORD_EVENT:
                    continue
                (
                    sequence,
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
                    _event_flags,
                    _reserved,
                ) = EVENT_RECORD.unpack_from(payload)
                if operation not in OPERATION_NAMES:
                    continue
                last_timestamp_ns = timestamp_ns
                if stack_id and stack_id not in emitted_stacks:
                    write_packet(
                        output,
                        packet_fields(
                            data_field=12,
                            data=interning.stack_message(stack_id),
                        ),
                    )
                    write_packet(
                        output,
                        packet_fields(
                            timestamp_ns=timestamp_ns,
                            data_field=11,
                            data=stack_definition_event(
                                definitions,
                                stack_id,
                                stack_leaf_nodes.get(stack_id, 0),
                            ),
                            sequence_flags=2,
                        ),
                    )
                    emitted_stacks.add(stack_id)
                if operation == 1:
                    active[allocation_id] = size
                    active_bytes += size
                elif operation == 2:
                    previous = active.pop(allocation_id, 0)
                    active_bytes = max(0, active_bytes - previous)
                else:
                    previous = active.get(allocation_id, old_size)
                    active[allocation_id] = size
                    active_bytes = max(0, active_bytes - previous + size)
                update_profile_state(
                    operation,
                    allocation_id,
                    size,
                    old_size,
                    stack_id,
                    profile_active,
                    profile_stats,
                )

                event = memory_event(
                    definitions,
                    operation,
                    sequence,
                    allocation_id,
                    related_allocation_id,
                    address,
                    old_address,
                    size,
                    old_size,
                    pool_id,
                    stack_id,
                    tid,
                    active_bytes,
                    len(active),
                )
                write_packet(
                    output,
                    packet_fields(
                        timestamp_ns=timestamp_ns,
                        data_field=11,
                        data=event,
                        sequence_flags=2,
                    ),
                )
                event_count += 1
                emit_periodic = (
                    snapshot_events
                    and event_count % snapshot_events == 0
                )
                emit_peak = sequence == definitions.peak_sequence
                if emit_periodic or emit_peak:
                    snapshot = ProfileSnapshot(
                        timestamp_ns=timestamp_ns,
                        start_timestamp_ns=previous_snapshot_ns,
                        stats=profile_stats,
                    )
                    write_packet(
                        output,
                        packet_fields(
                            timestamp_ns=timestamp_ns,
                            data_field=37,
                            data=profile_packet(
                                definitions,
                                snapshot,
                                snapshot_count,
                            ),
                            sequence_flags=2,
                        ),
                    )
                    emitted_snapshot_timestamps.add(timestamp_ns)
                    snapshot_count += 1
                    previous_snapshot_ns = timestamp_ns
        if last_timestamp_ns not in emitted_snapshot_timestamps:
            final_snapshot = ProfileSnapshot(
                timestamp_ns=last_timestamp_ns,
                start_timestamp_ns=previous_snapshot_ns,
                stats=profile_stats,
            )
            write_packet(
                output,
                packet_fields(
                    timestamp_ns=final_snapshot.timestamp_ns,
                    data_field=37,
                    data=profile_packet(
                        definitions,
                        final_snapshot,
                        snapshot_count,
                    ),
                    sequence_flags=2,
                ),
            )
            snapshot_count += 1
    return {
        "events": event_count,
        "stacks": len(emitted_stacks),
        "mappings": len(definitions.mappings),
        "snapshots": snapshot_count,
    }


def find_trace_processor(explicit: Path | None) -> Path:
    if explicit is not None:
        path = explicit.expanduser().resolve()
        if path.is_file():
            return path
        raise ValueError(f"Trace Processor not found: {path}")
    for name in ("trace_processor_shell", "trace_processor"):
        found = shutil.which(name)
        if found:
            return Path(found)
    raise ValueError(
        "Trace Processor not found. Download the official wrapper from "
        "https://get.perfetto.dev/trace_processor or pass --trace-processor."
    )


def download_trace_processor(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    wrapper = cache_dir / "trace_processor"
    if not wrapper.exists():
        subprocess.run(
            [
                "curl",
                "-fL",
                "-o",
                str(wrapper),
                TRACE_PROCESSOR_WRAPPER_URL,
            ],
            check=True,
        )
        wrapper.chmod(0o755)
    cache_home = cache_dir / "home"
    environment = dict(os.environ)
    environment["HOME"] = str(cache_home)
    subprocess.run(
        [str(wrapper), "--version"],
        check=True,
        stdout=subprocess.DEVNULL,
        env=environment,
    )
    prebuilts = (
        cache_home / ".local" / "share" / "perfetto" / "prebuilts"
    )
    candidates = sorted(prebuilts.glob("trace_processor_shell-*"))
    if not candidates:
        raise ValueError(
            f"Trace Processor bootstrap did not create a binary under {prebuilts}"
        )
    return candidates[-1]


def sql_views_path() -> Path:
    return Path(__file__).resolve().parent / "perfetto" / "bolt_memory.sql"


def query_rows(
    trace: Path,
    trace_processor: Path | None,
    sql: str,
) -> list[dict[str, str]]:
    executable = find_trace_processor(trace_processor)
    views = sql_views_path().read_text()
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".sql",
        delete=False,
    ) as query_file:
        query_file.write(views)
        query_file.write("\n")
        query_file.write(sql)
        query_path = Path(query_file.name)
    try:
        result = subprocess.run(
            [
                str(executable),
                "query",
                "-f",
                str(query_path),
                str(trace),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        query_path.unlink(missing_ok=True)
    return list(csv.DictReader(io.StringIO(result.stdout.lstrip())))


def analysis_sql(
    top: int,
    long_lived_ns: int,
    short_lived_ns: int,
) -> str:
    return f"""
WITH
peak AS (
  SELECT ts, CAST(value AS INT) AS bytes
  FROM bolt_memory_counter
  WHERE name = 'Bolt active bytes'
  ORDER BY value DESC, ts
  LIMIT 1
),
duration AS (
  SELECT trace_end() - trace_start() AS ns
),
ordered_lifetime AS (
  SELECT
    lifetime_ns,
    ROW_NUMBER() OVER (ORDER BY lifetime_ns) AS row_number,
    COUNT(*) OVER () AS row_count
  FROM bolt_memory_lifetime
),
lifetime_summary AS (
  SELECT
    COUNT(*) AS allocation_count,
    MIN(lifetime_ns) AS min_ns,
    MIN(CASE
      WHEN row_number >= (row_count * 50 + 99) / 100 THEN lifetime_ns
    END) AS p50_ns,
    MIN(CASE
      WHEN row_number >= (row_count * 95 + 99) / 100 THEN lifetime_ns
    END) AS p95_ns,
    MIN(CASE
      WHEN row_number >= (row_count * 99 + 99) / 100 THEN lifetime_ns
    END) AS p99_ns,
    MAX(lifetime_ns) AS max_ns
  FROM ordered_lifetime
),
peak_pool AS (
  SELECT
    state.pool AS key,
    SUM(state.size) AS value,
    COUNT(*) AS count
  FROM bolt_memory_state_interval state, peak
  WHERE state.start_ts <= peak.ts
    AND (state.end_ts IS NULL OR state.end_ts > peak.ts)
  GROUP BY state.pool
  ORDER BY value DESC
  LIMIT {top}
),
peak_stack AS (
  SELECT
    CAST(state.stack_id AS TEXT) AS key,
    SUM(state.size) AS value,
    COUNT(*) AS count,
    stack.stack
  FROM bolt_memory_state_interval state
  LEFT JOIN bolt_memory_stack stack USING (stack_id),
       peak
  WHERE state.start_ts <= peak.ts
    AND (state.end_ts IS NULL OR state.end_ts > peak.ts)
  GROUP BY state.stack_id
  ORDER BY value DESC
  LIMIT {top}
),
long_lived AS (
  SELECT
    CAST(lifetime.allocation_id AS TEXT) AS key,
    lifetime.lifetime_ns AS value,
    lifetime.size AS count,
    lifetime.pool || char(10) || COALESCE(stack.stack, '') AS detail
  FROM bolt_memory_lifetime lifetime
  LEFT JOIN bolt_memory_stack stack USING (stack_id)
  WHERE lifetime.live_at_end OR lifetime.lifetime_ns >= {long_lived_ns}
  ORDER BY lifetime.live_at_end DESC,
           lifetime.size * lifetime.lifetime_ns DESC
  LIMIT {top}
),
short_lived AS (
  SELECT
    COUNT(*) AS allocation_count,
    COALESCE(SUM(size), 0) AS bytes
  FROM bolt_memory_lifetime
  WHERE NOT live_at_end AND lifetime_ns <= {short_lived_ns}
),
totals AS (
  SELECT
    COUNT(*) AS allocation_count,
    COALESCE(SUM(size), 0) AS bytes,
    COALESCE(SUM(live_at_end), 0) AS live_at_end
  FROM bolt_memory_lifetime
)
SELECT 'metric' AS kind, 'peak_bytes' AS key,
       CAST(peak.bytes AS TEXT) AS value, '' AS detail
FROM peak
UNION ALL
SELECT 'metric', 'peak_ts', CAST(peak.ts AS TEXT), '' FROM peak
UNION ALL
SELECT 'metric', 'duration_ns', CAST(duration.ns AS TEXT), '' FROM duration
UNION ALL
SELECT 'metric', 'allocations', CAST(totals.allocation_count AS TEXT), ''
FROM totals
UNION ALL
SELECT 'metric', 'allocated_bytes', CAST(totals.bytes AS TEXT), ''
FROM totals
UNION ALL
SELECT 'metric', 'live_at_end', CAST(totals.live_at_end AS TEXT), ''
FROM totals
UNION ALL
SELECT 'metric', 'lifetime_p50_ns', CAST(p50_ns AS TEXT), ''
FROM lifetime_summary
UNION ALL
SELECT 'metric', 'lifetime_p95_ns', CAST(p95_ns AS TEXT), ''
FROM lifetime_summary
UNION ALL
SELECT 'metric', 'lifetime_p99_ns', CAST(p99_ns AS TEXT), ''
FROM lifetime_summary
UNION ALL
SELECT 'metric', 'lifetime_max_ns', CAST(max_ns AS TEXT), ''
FROM lifetime_summary
UNION ALL
SELECT 'metric', 'short_lived_allocations',
       CAST(short_lived.allocation_count AS TEXT), ''
FROM short_lived
UNION ALL
SELECT 'metric', 'short_lived_bytes', CAST(short_lived.bytes AS TEXT), ''
FROM short_lived
UNION ALL
SELECT 'peak_pool', key, CAST(value AS TEXT), CAST(count AS TEXT)
FROM peak_pool
UNION ALL
SELECT 'peak_stack', key, CAST(value AS TEXT),
       CAST(count AS TEXT) || char(10) || COALESCE(stack, '')
FROM peak_stack
UNION ALL
SELECT 'long_lived', key, CAST(value AS TEXT),
       CAST(count AS TEXT) || char(10) || detail
FROM long_lived;
"""


def analyze_trace(
    trace: Path,
    trace_processor: Path | None,
    *,
    top: int,
    long_lived_ms: float,
    short_lived_ms: float,
) -> dict[str, object]:
    rows = query_rows(
        trace,
        trace_processor,
        analysis_sql(
            top,
            max(1, int(long_lived_ms * 1_000_000)),
            max(1, int(short_lived_ms * 1_000_000)),
        ),
    )
    metrics: dict[str, int] = {}
    peak_pools = []
    peak_stacks = []
    long_lived = []
    for row in rows:
        kind = row["kind"]
        if kind == "metric":
            metrics[row["key"]] = int(row["value"] or 0)
        elif kind == "peak_pool":
            peak_pools.append(
                {
                    "pool": row["key"],
                    "bytes": int(row["value"]),
                    "allocations": int(row["detail"]),
                }
            )
        elif kind == "peak_stack":
            count, _, stack = row["detail"].partition("\n")
            peak_stacks.append(
                {
                    "stack_id": int(row["key"]),
                    "bytes": int(row["value"]),
                    "allocations": int(count),
                    "stack": stack,
                }
            )
        elif kind == "long_lived":
            size, _, detail = row["detail"].partition("\n")
            pool, _, stack = detail.partition("\n")
            long_lived.append(
                {
                    "allocation_id": int(row["key"]),
                    "lifetime_ns": int(row["value"]),
                    "size": int(size),
                    "pool": pool,
                    "stack": stack,
                }
            )
    peak_bytes = metrics.get("peak_bytes", 0)
    allocated_bytes = metrics.get("allocated_bytes", 0)
    allocations = metrics.get("allocations", 0)
    short_lived_allocations = metrics.get("short_lived_allocations", 0)
    findings = []
    if metrics.get("live_at_end", 0):
        findings.append(
            {
                "severity": "critical",
                "id": "live-at-end",
                "summary": "Allocations remain live at trace end.",
            }
        )
    if long_lived:
        findings.append(
            {
                "severity": "warning",
                "id": "long-lifetime",
                "summary": "Allocations exceed the configured lifetime threshold.",
            }
        )
    churn_ratio = allocated_bytes / peak_bytes if peak_bytes else 0.0
    short_ratio = (
        short_lived_allocations / allocations if allocations else 0.0
    )
    if churn_ratio >= 5 or (allocations >= 100 and short_ratio >= 0.8):
        findings.append(
            {
                "severity": "warning",
                "id": "allocation-churn",
                "summary": "Allocation volume is high relative to retained memory.",
            }
        )
    severity = (
        "critical"
        if any(row["severity"] == "critical" for row in findings)
        else "warning"
        if findings
        else "info"
    )
    pool_covered_bytes = sum(row["bytes"] for row in peak_pools)
    stack_covered_bytes = sum(row["bytes"] for row in peak_stacks)
    return {
        "schema_version": "2.0",
        "engine": "perfetto-trace-processor",
        "assessment": {
            "severity": severity,
            "finding_count": len(findings),
        },
        "metrics": metrics,
        "peak": {
            "pools": peak_pools,
            "stacks": peak_stacks,
            "pool_coverage": {
                "bytes": pool_covered_bytes,
                "percent": round(
                    pool_covered_bytes / peak_bytes * 100, 2
                )
                if peak_bytes
                else 0.0,
            },
            "stack_coverage": {
                "bytes": stack_covered_bytes,
                "percent": round(
                    stack_covered_bytes / peak_bytes * 100, 2
                )
                if peak_bytes
                else 0.0,
            },
        },
        "long_lived": long_lived,
        "churn": {
            "total_to_peak_ratio": round(churn_ratio, 3),
            "short_lived_allocation_ratio": round(short_ratio, 3),
        },
        "findings": findings,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    convert_parser = subparsers.add_parser("convert")
    convert_parser.add_argument("input", type=Path)
    convert_parser.add_argument("-o", "--output", required=True, type=Path)
    convert_parser.add_argument(
        "--no-symbolize",
        action="store_true",
        help="Keep module+offset names instead of invoking addr2line",
    )

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("input", type=Path)
    prepare_parser.add_argument("-o", "--output", required=True, type=Path)
    prepare_parser.add_argument("--trace-processor", type=Path)
    prepare_parser.add_argument(
        "--trace-processor-cache",
        type=Path,
        default=Path.home() / ".cache" / "bolt-memory-perfetto",
    )
    prepare_parser.add_argument("--snapshot-events", type=int, default=100_000)
    prepare_parser.add_argument("--no-symbolize", action="store_true")
    convert_parser.add_argument(
        "--snapshot-events",
        type=int,
        default=100_000,
        help="Emit one native heap snapshot per N events, plus peak and final",
    )

    query_parser = subparsers.add_parser("query")
    query_parser.add_argument("trace", type=Path)
    query_parser.add_argument("sql")
    query_parser.add_argument("--trace-processor", type=Path)

    analyze_parser = subparsers.add_parser("analyze")
    analyze_parser.add_argument("trace", type=Path)
    analyze_parser.add_argument("--trace-processor", type=Path)
    analyze_parser.add_argument("--top", type=int, default=5)
    analyze_parser.add_argument("--long-lived-ms", type=float, default=1000)
    analyze_parser.add_argument("--short-lived-ms", type=float, default=1)
    analyze_parser.add_argument(
        "--format",
        choices=("json", "pretty-json"),
        default="json",
    )

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("trace", type=Path)
    serve_parser.add_argument("--trace-processor", type=Path)
    serve_parser.add_argument("--port", type=int, default=9001)
    serve_parser.add_argument("--ip-address", default="127.0.0.1")
    serve_parser.add_argument(
        "--cors-origin",
        action="append",
        default=[
            "http://localhost:10000",
            "http://127.0.0.1:10000",
        ],
        help="Additional browser origin allowed by Trace Processor",
    )

    install_parser = subparsers.add_parser("install-trace-processor")
    install_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path.home() / ".cache" / "bolt-memory-perfetto",
    )

    ui_install_parser = subparsers.add_parser("install-ui-plugin")
    ui_install_parser.add_argument("--perfetto", required=True, type=Path)
    ui_install_parser.add_argument("--force", action="store_true")

    ui_build_parser = subparsers.add_parser("build-ui")
    ui_build_parser.add_argument("--perfetto", required=True, type=Path)

    ui_serve_parser = subparsers.add_parser("serve-ui")
    ui_serve_parser.add_argument("--perfetto", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "convert":
            result = convert(
                args.input,
                args.output,
                symbolize=not args.no_symbolize,
                snapshot_events=max(0, args.snapshot_events),
            )
            print(
                f"wrote {args.output} "
                f"events={result['events']} stacks={result['stacks']} "
                f"mappings={result['mappings']} "
                f"snapshots={result['snapshots']}"
            )
            return 0
        if args.command == "prepare":
            trace_processor = args.trace_processor
            if trace_processor is None:
                trace_processor = download_trace_processor(
                    args.trace_processor_cache
                )
            result = convert(
                args.input,
                args.output,
                symbolize=not args.no_symbolize,
                snapshot_events=max(0, args.snapshot_events),
            )
            report = analyze_trace(
                args.output,
                trace_processor,
                top=5,
                long_lived_ms=1000,
                short_lived_ms=1,
            )
            print(
                json.dumps(
                    {
                        "trace": str(args.output),
                        "trace_processor": str(trace_processor),
                        "conversion": result,
                        "assessment": report["assessment"],
                        "next": (
                            f"python3 {Path(__file__).name} serve "
                            f"{args.output} --trace-processor "
                            f"{trace_processor}"
                        ),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "query":
            executable = find_trace_processor(args.trace_processor)
            return subprocess.run(
                [
                    str(executable),
                    "query",
                    str(args.trace),
                    args.sql,
                ]
            ).returncode
        if args.command == "analyze":
            report = analyze_trace(
                args.trace,
                args.trace_processor,
                top=max(1, args.top),
                long_lived_ms=max(0, args.long_lived_ms),
                short_lived_ms=max(0, args.short_lived_ms),
            )
            print(
                json.dumps(
                    report,
                    indent=2 if args.format == "pretty-json" else None,
                    separators=None
                    if args.format == "pretty-json"
                    else (",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "serve":
            executable = find_trace_processor(args.trace_processor)
            print(
                "Trace Processor is serving the loaded trace. Open the local "
                "Perfetto UI or https://ui.perfetto.dev and connect "
                f"at http://{args.ip_address}:{args.port}."
            )
            command = [
                str(executable),
                "server",
                "http",
                "--port",
                str(args.port),
                "--ip-address",
                args.ip_address,
            ]
            if args.cors_origin:
                command.extend(
                    [
                        "--additional-cors-origins",
                        ",".join(args.cors_origin),
                    ]
                )
            command.append(str(args.trace))
            return subprocess.run(command).returncode
        if args.command == "install-trace-processor":
            wrapper = download_trace_processor(args.cache_dir)
            print(wrapper)
            return 0
        if args.command == "install-ui-plugin":
            installer = (
                Path(__file__).resolve().parent
                / "install_bolt_perfetto_ui.py"
            )
            argv = [
                sys.executable,
                str(installer),
                "--perfetto",
                str(args.perfetto),
            ]
            if args.force:
                argv.append("--force")
            return subprocess.run(argv).returncode
        if args.command == "build-ui":
            repo = args.perfetto.resolve()
            return subprocess.run(
                [str(repo / "ui" / "build")],
                cwd=repo,
            ).returncode
        if args.command == "serve-ui":
            repo = args.perfetto.resolve()
            print("Open http://localhost:10000 and load the Perfetto trace.")
            return subprocess.run(
                [str(repo / "ui" / "run-dev-server")],
                cwd=repo,
            ).returncode
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
