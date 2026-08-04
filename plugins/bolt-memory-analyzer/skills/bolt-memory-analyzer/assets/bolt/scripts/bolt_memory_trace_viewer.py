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

"""Build an offline interactive report from a Bolt memory trace binary."""

from __future__ import annotations

import argparse
import bisect
import html
import json
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


def fmt_bytes(value: int | float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    number = float(value)
    for unit in units:
        if abs(number) < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(number)}B"
            return f"{number:.2f}{unit}"
        number /= 1024
    return f"{number:.2f}TB"


def metric_label(metric: str) -> str:
    if metric == "allocations":
        return "allocations"
    if metric == "live_bytes":
        return "live bytes"
    return "allocated bytes"


def fmt_metric(node: dict[str, Any], metric: str) -> str:
    if metric == "allocations":
        return str(int(node.get("allocations", 0)))
    return fmt_bytes(int(node.get(metric, 0)))


def child_value(node: dict[str, Any], metric: str) -> float:
    return float(node.get(metric, 0) or 0)


def flame_color(depth: int, node: dict[str, Any]) -> str:
    live_bytes = float(node.get("live_bytes", 0) or 0)
    if live_bytes > 0:
        total_bytes = max(1.0, float(node.get("bytes", 1) or 1))
        live_ratio = min(1.0, live_bytes / total_bytes)
        light = round(86 - live_ratio * 18)
        return f"hsl(2 78% {light}%)"
    hue = (depth * 29 + 204) % 360
    return f"hsl({hue} 70% 78%)"


def trim_middle(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 8:
        return text[: max(0, max_chars - 3)] + "..."
    head = int((max_chars - 3) * 0.58 + 0.999)
    tail = int((max_chars - 3) * 0.42)
    return f"{text[:head]}...{text[len(text) - tail:]}"


def flatten_flame(
    node: dict[str, Any],
    metric: str,
    x: float,
    y: float,
    width: float,
    depth: int,
    rows: list[dict[str, Any]],
    min_width: float,
) -> None:
    value = max(0.0, child_value(node, metric))
    if width < min_width or value <= 0:
        return
    rows.append({"node": node, "x": x, "y": y, "width": width, "depth": depth})
    cursor = x
    children = [child for child in node.get("children", []) if child_value(child, metric) > 0]
    total = sum(child_value(child, metric) for child in children)
    for child in children:
        child_width = width * child_value(child, metric) / total if total > 0 else 0
        flatten_flame(child, metric, cursor, y + 24, child_width, depth + 1, rows, min_width)
        cursor += child_width


def render_static_flamegraph(
    root: dict[str, Any],
    metric: str = "bytes",
) -> tuple[str, str, str]:
    root_value = child_value(root, metric)
    if root_value <= 0:
        view_box = "0 0 1100 70"
        body = (
            '<text x="16" y="36" class="flame-root-label">'
            f"No flame graph data for {html.escape(metric_label(metric))}.</text>"
        )
        return view_box, body, f"No {metric_label(metric)} to display."

    width = 1100
    pad_x = 8
    top = 26
    frame_h = 20
    min_width = 2.5
    rows: list[dict[str, Any]] = []
    flatten_flame(root, metric, pad_x, top, width - pad_x * 2, 0, rows, min_width)
    max_depth = max((int(row["depth"]) for row in rows), default=0)
    height = top + (max_depth + 1) * 24 + 28
    parts = [
        (
            f'<text x="{pad_x}" y="16" class="flame-root-label">'
            f"{html.escape(str(root.get('name', 'all allocations')))} - "
            f"{html.escape(fmt_metric(root, metric))} {html.escape(metric_label(metric))}"
            "</text>"
        )
    ]
    for row in rows:
        node = row["node"]
        node_value = child_value(node, metric)
        pct = node_value / root_value * 100
        text_chars = int((float(row["width"]) - 10) / 6.4)
        label = trim_middle(str(node.get("name", "")), text_chars) if text_chars >= 5 else ""
        title = (
            f"{node.get('name', '')}\n"
            f"{fmt_metric(node, metric)} {metric_label(metric)} ({pct:.1f}%)\n"
            f"bytes {fmt_bytes(int(node.get('bytes', 0) or 0))}, "
            f"live {fmt_bytes(int(node.get('live_bytes', 0) or 0))}, "
            f"allocations {int(node.get('allocations', 0) or 0)}"
        )
        parts.append(
            f'<g class="flame-frame" tabindex="0">'
            f"<title>{html.escape(title)}</title>"
            f'<rect x="{float(row["x"]):.2f}" y="{float(row["y"]):.0f}" '
            f'width="{max(0.0, float(row["width"])):.2f}" height="{frame_h}" '
            f'rx="2" fill="{flame_color(int(row["depth"]), node)}"></rect>'
            + (
                f'<text x="{float(row["x"]) + 5:.2f}" y="{float(row["y"]) + 14:.0f}">'
                f"{html.escape(label)}</text>"
                if label
                else ""
            )
            + "</g>"
        )
    top_children = len([child for child in root.get("children", []) if child_value(child, metric) > 0])
    meta = (
        f"{top_children} top-level branch{'es' if top_children != 1 else ''} - "
        f"{len(rows)} visible frame{'s' if len(rows) != 1 else ''} - "
        f"root {fmt_metric(root, metric)} {metric_label(metric)}"
    )
    return f"0 0 {width} {height}", "".join(parts), meta


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


def render_html(model: dict[str, Any], *, trace_path: Path, output: Path) -> None:
    payload = json.dumps(model, separators=(",", ":"))
    vendor_dir = Path(__file__).resolve().parent / "vendor"
    d3_js = (vendor_dir / "d3.min.js").read_text()
    flamegraph_js = (vendor_dir / "d3-flamegraph.min.js").read_text()
    summary = model["summary"]
    trace_metadata = model.get("trace_metadata", {})
    trace_complete = bool(trace_metadata.get("clean_shutdown"))
    anomaly_count = (
        summary["live_allocations"] + summary["unmatched_frees"]
        + (0 if trace_complete else 1)
    )
    verdict_class = "bad" if anomaly_count else "ok"
    verdict_text = (
        f"{anomaly_count} anomaly signal{'s' if anomaly_count != 1 else ''} "
        f"need{'s' if anomaly_count == 1 else ''} review"
        if anomaly_count
        else "Trace completed cleanly with no live allocations or unmatched frees"
    )
    flame_view_box, flame_svg, flame_meta = render_static_flamegraph(model["flamegraph"])

    def cell(value: Any) -> str:
        return html.escape(str(value))

    def fmt_ms(us: int | float) -> str:
        return f"{float(us) / 1000:.2f}ms"

    def icon(name: str, extra_class: str = "") -> str:
        paths = {
            "activity": '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline>',
            "alert": '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"></path><line x1="12" x2="12" y1="9" y2="13"></line><line x1="12" x2="12.01" y1="17" y2="17"></line>',
            "check": '<path d="M20 6 9 17l-5-5"></path>',
            "clock": '<circle cx="12" cy="12" r="9"></circle><polyline points="12 7 12 12 15 14"></polyline>',
            "database": '<ellipse cx="12" cy="5" rx="8" ry="3"></ellipse><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"></path><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"></path>',
            "flame": '<path d="M8.5 14.5A4 4 0 0 0 12 21a4 4 0 0 0 3.5-6.5c-.8-1.1-1.5-1.8-1.5-3.5 0-1.4.6-2.6 1.5-3.5C12 8 9 10.5 8.5 14.5Z"></path><path d="M12 21c-1.4-1-2-2.1-2-3.3 0-1 .7-2 2-3.2 1.3 1.2 2 2.2 2 3.2 0 1.2-.6 2.3-2 3.3Z"></path>',
            "layers": '<path d="m12 2 9 5-9 5-9-5 9-5Z"></path><path d="m3 12 9 5 9-5"></path><path d="m3 17 9 5 9-5"></path>',
            "list": '<line x1="8" x2="21" y1="6" y2="6"></line><line x1="8" x2="21" y1="12" y2="12"></line><line x1="8" x2="21" y1="18" y2="18"></line><line x1="3" x2="3.01" y1="6" y2="6"></line><line x1="3" x2="3.01" y1="12" y2="12"></line><line x1="3" x2="3.01" y1="18" y2="18"></line>',
            "memory": '<rect x="5" y="5" width="14" height="14" rx="2"></rect><path d="M9 9h6v6H9z"></path><path d="M9 1v4"></path><path d="M15 1v4"></path><path d="M9 19v4"></path><path d="M15 19v4"></path><path d="M1 9h4"></path><path d="M1 15h4"></path><path d="M19 9h4"></path><path d="M19 15h4"></path>',
            "route": '<circle cx="6" cy="19" r="3"></circle><circle cx="18" cy="5" r="3"></circle><path d="M9 19h3a6 6 0 0 0 6-6V8"></path>',
            "table": '<path d="M3 5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5Z"></path><path d="M3 10h18"></path><path d="M10 3v18"></path>',
            "timer": '<path d="M10 2h4"></path><path d="M12 14l3-3"></path><circle cx="12" cy="14" r="8"></circle>',
            "zap": '<path d="m13 2-9 12h8l-1 8 9-12h-8l1-8Z"></path>',
        }
        return (
            f'<svg class="icon {extra_class}" aria-hidden="true" viewBox="0 0 24 24">'
            f"{paths[name]}</svg>"
        )

    anomaly_rows: list[str] = []
    for error in trace_metadata.get("integrity_errors", []):
        anomaly_rows.append(
            "<tr>"
            '<td><span class="bad">trace integrity</span></td>'
            f'<td colspan="5">{cell(error)}</td>'
            "</tr>"
        )
    for row in model["unmatched_frees"]:
        anomaly_rows.append(
            "<tr>"
            f"<td><span class=\"bad\">unmatched {cell(row.get('op', ''))}</span></td>"
            f"<td>{cell(row.get('pool', ''))}</td>"
            f"<td class=\"num\">{cell(row.get('addr', ''))}</td>"
            f"<td class=\"num\">{fmt_bytes(int(row.get('size', 0)))}</td>"
            f"<td class=\"num\">{cell(row.get('seq', ''))}</td>"
            f"<td class=\"num\">{cell(row.get('stack_id', ''))}</td>"
            "</tr>"
        )
    for row in model["anomalies"]["live_allocations"]:
        anomaly_rows.append(
            "<tr>"
            "<td><span class=\"bad\">live allocation</span></td>"
            f"<td>{cell(row['pool'])}</td>"
            f"<td class=\"num\">{cell(row['addr'])}</td>"
            f"<td class=\"num\">{fmt_bytes(row['size'])}</td>"
            f"<td class=\"num\">{cell(row['start_seq'])}</td>"
            f"<td class=\"num\">{cell(row['stack_id'])}</td>"
            "</tr>"
        )
    if not anomaly_rows:
        anomaly_rows.append('<tr><td colspan="6" class="empty">No anomalies detected.</td></tr>')

    longest_rows = [
        "<tr>"
        f"<td>{cell(row['pool'])}</td>"
        f"<td class=\"num\">{cell(row['addr'])}</td>"
        f"<td class=\"num\">{fmt_bytes(row['size'])}</td>"
        f"<td class=\"num\">{cell(row['stack_id'])}</td>"
        f"<td class=\"num\">{cell(row['start_seq'])}</td>"
        f"<td class=\"num\">{cell(row['end_seq'] if row['end_seq'] is not None else 'live')}</td>"
        f"<td class=\"num\">{fmt_ms(row['lifetime_us'])}</td>"
        "</tr>"
        for row in model["anomalies"]["longest_lived_allocations"]
    ]

    pool_rows = [
        "<tr>"
        f"<td>{cell(row['pool'])}</td>"
        f"<td class=\"num\">{cell(row['allocations'])}</td>"
        f"<td class=\"num\">{fmt_bytes(row['bytes'])}</td>"
        f"<td class=\"num\">{cell(row['live'])}</td>"
        f"<td class=\"num\">{fmt_bytes(row['live_bytes'])}</td>"
        "</tr>"
        for row in model["pools"]
    ]

    stack_rows = [
        "<tr>"
        f"<td class=\"num\">{cell(row['stack_id'])}</td>"
        f"<td class=\"num\">{cell(row['allocations'])}</td>"
        f"<td class=\"num\">{fmt_bytes(row['bytes'])}</td>"
        f"<td class=\"num\">{cell(row['live'])}</td>"
        f"<td class=\"num\">{fmt_bytes(row['live_bytes'])}</td>"
        f"<td><pre class=\"stack\">{cell(row['stack'])}</pre></td>"
        "</tr>"
        for row in model["stacks"]
    ]

    lifetime_rows = [
        "<tr>"
        f"<td>{cell(row['pool'])}</td>"
        f"<td class=\"num\">{cell(row['addr'])}</td>"
        f"<td class=\"num\">{fmt_bytes(row['size'])}</td>"
        f"<td class=\"num\">{cell(row['stack_id'])}</td>"
        f"<td class=\"num\">{cell(row['start_seq'])}</td>"
        f"<td class=\"num\">{cell(row['end_seq'] if row['end_seq'] is not None else 'live')}</td>"
        f"<td class=\"num\">{fmt_ms(row['lifetime_us'])}</td>"
        "</tr>"
        for row in model["lifetimes"]
    ]

    css = """
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #172033; background: #f8fafc; }
.wrap { max-width: 1180px; margin: 0 auto; }
h1 { margin: 0 0 6px; font-size: 24px; }
h2 { margin: 26px 0 10px; font-size: 16px; }
.meta { color: #64748b; font-size: 13px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 18px 0; }
.card { background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px; display: grid; grid-template-columns: 34px 1fr; gap: 10px; align-items: center; }
.card.bad { border-color: #fecaca; background: #fff7f7; }
.card.warn { border-color: #fde68a; background: #fffbeb; }
.card.good { border-color: #bbf7d0; background: #f0fdf4; }
.card-icon { width: 34px; height: 34px; border-radius: 8px; display: grid; place-items: center; background: #eff6ff; color: #2563eb; }
.card.bad .card-icon { background: #fee2e2; color: #dc2626; }
.card.warn .card-icon { background: #fef3c7; color: #b45309; }
.card.good .card-icon { background: #dcfce7; color: #166534; }
.metric { font-size: 24px; font-weight: 700; font-variant-numeric: tabular-nums; }
.label { color: #64748b; font-size: 12px; margin-top: 4px; }
.subtext { color: #64748b; font-size: 12px; margin-top: 2px; min-height: 16px; }
.toolbar { display: flex; flex-wrap: wrap; gap: 10px; margin: 16px 0; align-items: center; }
input, select { border: 1px solid #cbd5e1; border-radius: 6px; padding: 7px 9px; background: #fff; min-height: 34px; }
button { border: 1px solid #2563eb; background: #2563eb; color: white; border-radius: 6px; padding: 7px 10px; cursor: pointer; }
button.secondary { border-color: #cbd5e1; background: #fff; color: #334155; }
.panel { background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px; overflow-x: auto; }
.section-title { display: flex; align-items: center; gap: 8px; }
.icon { width: 18px; height: 18px; stroke: currentColor; fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; flex: 0 0 auto; }
.icon-fill { fill: currentColor; stroke: none; }
.icon-sm { width: 14px; height: 14px; }
.icon-inline { display: inline-flex; align-items: center; gap: 6px; }
.context-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; color: #334155; font-size: 12px; margin: 10px 0 0; }
.context-pill { display: inline-flex; align-items: center; gap: 6px; padding: 4px 8px; border: 1px solid #cbd5e1; border-radius: 999px; background: #f8fafc; font-variant-numeric: tabular-nums; }
.verdict { margin: 18px 0; padding: 14px 16px; border-radius: 8px; border: 1px solid; font-weight: 700; }
.verdict.ok { background: #f0fdf4; border-color: #bbf7d0; color: #166534; }
.verdict.bad { background: #fff7f7; border-color: #fecaca; color: #b91c1c; }
.empty { color: #64748b; padding: 10px 0; }
details { margin: 18px 0; }
summary { cursor: pointer; font-weight: 700; color: #334155; margin-bottom: 10px; }
summary .icon { vertical-align: -3px; margin-right: 6px; }
svg { display: block; }
#timeline { width: 100%; min-width: 720px; height: 260px; }
.timeline-hint { color: #64748b; font-size: 12px; margin: 8px 0 0; }
.timeline-status { color: #334155; font-size: 12px; margin: 8px 0 0; font-variant-numeric: tabular-nums; }
.timeline-cursor { stroke: #dc2626; stroke-width: 2; pointer-events: none; }
.timeline-brush { fill: #2563eb; fill-opacity: 0.13; stroke: #2563eb; stroke-width: 1; pointer-events: none; }
.timeline-hit { fill: transparent; cursor: crosshair; }
#flamegraphFallbackSvg { width: 100%; min-width: 960px; height: auto; }
#flamegraphInteractive { min-width: 960px; }
.flame-panel { overflow-x: auto; }
.flame-toolbar { justify-content: space-between; }
.flame-controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.flame-meta { color: #64748b; font-size: 12px; }
.flame-details { min-height: 20px; color: #334155; font-size: 12px; margin: 0 0 10px; font-variant-numeric: tabular-nums; }
.flame-crumbs { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 10px; }
.crumb { border-color: #cbd5e1; background: #fff; color: #334155; }
.crumb.active { border-color: #2563eb; color: #1d4ed8; background: #eff6ff; }
.is-hidden { display: none !important; }
.d3-flame-graph rect { stroke: #eeeeee; fill-opacity: 0.86; }
.d3-flame-graph rect:hover { stroke: #0f172a; stroke-width: 0.7; cursor: pointer; }
.d3-flame-graph-label { pointer-events: none; white-space: nowrap; text-overflow: ellipsis; overflow: hidden; font-size: 12px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin-left: 4px; margin-right: 4px; line-height: 1.5; padding: 0; font-weight: 400; color: #0f172a; text-align: left; }
.d3-flame-graph .fade { opacity: 0.45 !important; }
.d3-flame-graph .title { font-size: 14px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
.flame-frame rect { stroke: rgba(255,255,255,0.92); stroke-width: 1; }
.flame-frame text { pointer-events: none; font-size: 11px; fill: #0f172a; }
.flame-frame:hover rect { stroke: #0f172a; stroke-width: 1.2; }
.flame-match rect { stroke: #facc15; stroke-width: 2; }
.flame-faded { opacity: 0.35; }
.flame-root-label { font-size: 12px; fill: #475569; }
table { width: 100%; border-collapse: collapse; background: #fff; font-size: 13px; }
th, td { border-bottom: 1px solid #edf2f7; padding: 8px 9px; text-align: left; vertical-align: top; }
th { position: sticky; top: 0; background: #f1f5f9; color: #334155; z-index: 1; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tr:hover { background: #f8fafc; }
pre { white-space: pre-wrap; margin: 0; font-size: 12px; color: #334155; }
.stack { max-width: 560px; max-height: 180px; overflow: auto; }
.warn { color: #b45309; }
.bad { color: #dc2626; }
"""
    js = """
const DATA = __DATA__;
let flameFocusPath = [];
let flameSelectedMetric = 'bytes';
let flameChart = null;
let flameContext = {mode: 'full', startUs: null, endUs: null};
let timelineState = null;
const fmtBytes = (v) => {
  const units = ['B','KB','MB','GB','TB'];
  let n = Number(v || 0), i = 0;
  while (Math.abs(n) >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return i === 0 ? `${Math.trunc(n)}B` : `${n.toFixed(2)}${units[i]}`;
};
const fmtMs = (us) => `${(Number(us || 0) / 1000).toFixed(2)}ms`;
const metricLabel = (metric) => metric === 'allocations' ? 'allocations' : metric === 'live_bytes' ? 'live bytes' : 'allocated bytes';
const fmtMetric = (node, metric) => metric === 'allocations' ? String(node.allocations || 0) : fmtBytes(node[metric] || 0);
const runStartUs = () => DATA.timeline.length ? DATA.timeline[0].time_us : 0;
const runEndUs = () => DATA.timeline.length ? DATA.timeline[DATA.timeline.length - 1].time_us : 0;
const relMs = (us) => `${((Number(us || 0) - runStartUs()) / 1000).toFixed(2)}ms`;
const absMs = (us) => `${(Number(us || 0) / 1000).toFixed(2)}ms`;
function toD3FlameNode(node, metric) {
  const value = childValue(node, metric);
  return {
    name: node.name,
    value,
    bytes: node.bytes || 0,
    live_bytes: node.live_bytes || 0,
    allocations: node.allocations || 0,
    children: (node.children || [])
      .filter(child => childValue(child, metric) > 0)
      .map(child => toD3FlameNode(child, metric)),
  };
}
function flameDetails(node, metric) {
  const source = node && node.data ? node.data : node;
  if (!source) return '';
  return `${source.name} - ${fmtMetric(source, metric)} ${metricLabel(metric)} - bytes ${fmtBytes(source.bytes || 0)}, live ${fmtBytes(source.live_bytes || 0)}, allocations ${source.allocations || 0}`;
}
function flameTextMatches(node, query) {
  const raw = String(query || '').trim().toLowerCase();
  if (!raw) return false;
  const source = node && node.data ? node.data : node;
  return String((source && source.name) || '').toLowerCase().includes(raw);
}
function emptyFlameNode(name) {
  return {name, bytes: 0, live_bytes: 0, allocations: 0, children: []};
}
function frameListForStack(stackId) {
  return DATA.stack_frames[String(stackId)] || ['(stack capture disabled or unavailable)'];
}
function addAllocationToFlame(root, allocation, liveAtCursor) {
  const frames = frameListForStack(allocation.stack_id);
  let node = root;
  node.bytes += allocation.size;
  node.allocations += 1;
  if (liveAtCursor) node.live_bytes += allocation.size;
  frames.forEach(frame => {
    let child = node._children.get(frame);
    if (!child) {
      child = {name: frame, bytes: 0, live_bytes: 0, allocations: 0, _children: new Map()};
      node._children.set(frame, child);
    }
    child.bytes += allocation.size;
    child.allocations += 1;
    if (liveAtCursor) child.live_bytes += allocation.size;
    node = child;
  });
}
function freezeDynamicFlameNode(node) {
  const children = [...node._children.values()]
    .sort((a, b) => (b.bytes - a.bytes) || (b.allocations - a.allocations))
    .map(freezeDynamicFlameNode);
  return {
    name: node.name,
    bytes: node.bytes,
    live_bytes: node.live_bytes,
    allocations: node.allocations,
    children,
  };
}
function buildFlamegraphFromAllocations(allocations, name, liveAtCursor = false) {
  if (!allocations.length) {
    return emptyFlameNode(name);
  }
  const root = {name, bytes: 0, live_bytes: 0, allocations: 0, _children: new Map()};
  allocations.forEach(allocation => addAllocationToFlame(root, allocation, liveAtCursor));
  return freezeDynamicFlameNode(root);
}
function allocationsLiveAt(us) {
  return DATA.allocations.filter(a => a.start_us <= us && (a.end_us == null || us < a.end_us));
}
function allocationsOverlappingRange(startUs, endUs) {
  const lo = Math.min(startUs, endUs);
  const hi = Math.max(startUs, endUs);
  return DATA.allocations.filter(a => a.start_us <= hi && (a.end_us == null || a.end_us >= lo));
}
function lifetimesForContext() {
  if (flameContext.mode === 'live') {
    const at = flameContext.startUs ?? runEndUs();
    return DATA.lifetimes.filter(a => a.start_us <= at && (a.end_us == null || at < a.end_us));
  }
  if (flameContext.mode === 'range') {
    const start = flameContext.startUs ?? runStartUs();
    const end = flameContext.endUs ?? runEndUs();
    const lo = Math.min(start, end);
    const hi = Math.max(start, end);
    return DATA.lifetimes.filter(a => a.start_us <= hi && (a.end_us == null || a.end_us >= lo));
  }
  return DATA.lifetimes;
}
function allocationsForContext() {
  if (flameContext.mode === 'live') {
    return allocationsLiveAt(flameContext.startUs ?? runEndUs());
  }
  if (flameContext.mode === 'range') {
    return allocationsOverlappingRange(flameContext.startUs ?? runStartUs(), flameContext.endUs ?? runEndUs());
  }
  return DATA.allocations;
}
function contextTitle() {
  if (flameContext.mode === 'live') return `Live at ${relMs(flameContext.startUs)}`;
  if (flameContext.mode === 'range') return `Range ${relMs(flameContext.startUs)} to ${relMs(flameContext.endUs)}`;
  return 'Full run';
}
function contextWindowUs() {
  if (flameContext.mode === 'live') return [flameContext.startUs ?? runEndUs(), flameContext.startUs ?? runEndUs()];
  if (flameContext.mode === 'range') {
    const start = flameContext.startUs ?? runStartUs();
    const end = flameContext.endUs ?? runEndUs();
    return [Math.min(start, end), Math.max(start, end)];
  }
  return [runStartUs(), runEndUs()];
}
function contextSummary() {
  const allocations = allocationsForContext();
  const lifetimes = lifetimesForContext();
  const totalBytes = allocations.reduce((sum, a) => sum + Number(a.size || 0), 0);
  const liveBytes = flameContext.mode === 'live'
    ? totalBytes
    : allocations.filter(a => a.end_us == null).reduce((sum, a) => sum + Number(a.size || 0), 0);
  const [start, end] = contextWindowUs();
  return {
    allocations: allocations.length,
    bytes: totalBytes,
    liveAllocations: flameContext.mode === 'live' ? allocations.length : allocations.filter(a => a.end_us == null).length,
    liveBytes,
    lifetimes: lifetimes.length,
    start,
    end,
  };
}
function updateMetricCard(id, value, subtext) {
  const valueEl = document.getElementById(`${id}Value`);
  const subEl = document.getElementById(`${id}Sub`);
  if (valueEl) valueEl.textContent = value;
  if (subEl) subEl.textContent = subtext || '';
}
function updateContextMetrics() {
  const summary = contextSummary();
  const label = document.getElementById('contextLabel');
  if (label) label.textContent = contextTitle();
  const range = document.getElementById('contextRange');
  if (range) {
    range.textContent = flameContext.mode === 'full'
      ? `${relMs(summary.start)} to ${relMs(summary.end)}`
      : flameContext.mode === 'live'
        ? relMs(summary.start)
        : `${relMs(summary.start)} to ${relMs(summary.end)}`;
  }
  updateMetricCard('contextAllocations', String(summary.allocations), flameContext.mode === 'live' ? 'live at cursor' : 'in selected context');
  updateMetricCard('contextBytes', fmtBytes(summary.bytes), flameContext.mode === 'live' ? 'live bytes at cursor' : 'allocated bytes in context');
  updateMetricCard('contextLive', String(summary.liveAllocations), `${fmtBytes(summary.liveBytes)} live bytes`);
  updateMetricCard('contextLifetimes', String(summary.lifetimes), 'matching allocation rows');
}
function aggregatePools(rows) {
  const byPool = new Map();
  rows.forEach(r => {
    const row = byPool.get(r.pool) || {pool: r.pool, allocations: 0, bytes: 0, live: 0, live_bytes: 0};
    row.allocations += 1;
    row.bytes += Number(r.size || 0);
    if (flameContext.mode === 'live' || r.end_us == null) {
      row.live += 1;
      row.live_bytes += Number(r.size || 0);
    }
    byPool.set(r.pool, row);
  });
  return [...byPool.values()].sort((a, b) => (b.bytes - a.bytes) || (b.allocations - a.allocations));
}
function aggregateStacks(rows) {
  const byStack = new Map();
  rows.forEach(r => {
    const sid = r.stack_id ?? 0;
    const original = DATA.stacks.find(s => Number(s.stack_id) === Number(sid));
    const row = byStack.get(sid) || {stack_id: sid, allocations: 0, bytes: 0, live: 0, live_bytes: 0, stack: original ? original.stack : '(stack capture disabled or unavailable)'};
    row.allocations += 1;
    row.bytes += Number(r.size || 0);
    if (flameContext.mode === 'live' || r.end_us == null) {
      row.live += 1;
      row.live_bytes += Number(r.size || 0);
    }
    byStack.set(sid, row);
  });
  return [...byStack.values()].sort((a, b) => (b.bytes - a.bytes) || (b.allocations - a.allocations));
}
function currentRows(kind) {
  if (kind === 'pools') return aggregatePools(allocationsForContext());
  if (kind === 'stacks') return aggregateStacks(allocationsForContext());
  return lifetimesForContext().sort((a, b) => (b.size - a.size) || (b.lifetime_us - a.lifetime_us));
}
function currentFlameRoot() {
  if (flameContext.mode === 'live') {
    const at = flameContext.startUs ?? runEndUs();
    return buildFlamegraphFromAllocations(allocationsLiveAt(at), `live at ${relMs(at)}`, true);
  }
  if (flameContext.mode === 'range') {
    const start = flameContext.startUs ?? runStartUs();
    const end = flameContext.endUs ?? runEndUs();
    return buildFlamegraphFromAllocations(
      allocationsOverlappingRange(start, end),
      `allocated ${relMs(start)} to ${relMs(end)}`,
      false,
    );
  }
  return DATA.flamegraph;
}
function contextLabel(root) {
  if (flameContext.mode === 'live') {
    return `Live at ${relMs(flameContext.startUs)} - ${root.allocations} allocations - ${fmtBytes(root.live_bytes)} live`;
  }
  if (flameContext.mode === 'range') {
    return `Allocated from ${relMs(flameContext.startUs)} to ${relMs(flameContext.endUs)} - ${root.allocations} allocations - ${fmtBytes(root.bytes)}`;
  }
  return `Full run - ${root.allocations} allocations - ${fmtBytes(root.bytes)}`;
}
function renderAnomalies() {
  const body = document.getElementById('anomaliesBody');
  const rows = [];
  (DATA.trace_metadata?.integrity_errors || []).forEach(error => {
    rows.push(`<tr><td><span class="bad">trace integrity</span></td><td colspan="5">${escapeHtml(error)}</td></tr>`);
  });
  const [start, end] = contextWindowUs();
  DATA.unmatched_frees
    .filter(r => flameContext.mode === 'full' || (r.time_us >= start && r.time_us <= end))
    .forEach(r => {
    rows.push(`<tr><td><span class="bad">unmatched ${escapeHtml(r.op)}</span></td><td>${escapeHtml(r.pool || '')}</td><td class="num">${escapeHtml(r.addr || '')}</td><td class="num">${fmtBytes(r.size || 0)}</td><td class="num">${r.seq ?? ''}</td><td class="num">${r.stack_id ?? ''}</td></tr>`);
  });
  lifetimesForContext().filter(r => r.live).slice(0, 100).forEach(r => {
    rows.push(`<tr><td><span class="bad">live allocation</span></td><td>${escapeHtml(r.pool)}</td><td class="num">${escapeHtml(r.addr)}</td><td class="num">${fmtBytes(r.size)}</td><td class="num">${r.start_seq}</td><td class="num">${r.stack_id}</td></tr>`);
  });
  body.innerHTML = rows.length ? rows.join('') : '<tr><td colspan="6" class="empty">No anomalies detected.</td></tr>';
}
function drawTimeline() {
  const svg = document.getElementById('timeline');
  const data = DATA.timeline;
  svg.innerHTML = '';
  if (!data.length) return;
  const w = 1100, h = 240, padL = 54, padR = 18, padT = 18, padB = 34;
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  const minT = data[0].time_us, maxT = data[data.length - 1].time_us || minT + 1;
  const maxB = Math.max(...data.map(d => d.bytes), 1);
  const x = (t) => padL + (t - minT) / Math.max(1, maxT - minT) * (w - padL - padR);
  const tFromX = (px) => minT + (Math.max(padL, Math.min(w - padR, px)) - padL) / (w - padL - padR) * Math.max(1, maxT - minT);
  const y = (b) => h - padB - b / maxB * (h - padT - padB);
  timelineState = {w, h, padL, padR, padT, padB, minT, maxT, maxB, x, tFromX};
  let d = '';
  data.forEach((p, i) => { d += `${i ? 'L' : 'M'}${x(p.time_us).toFixed(1)},${y(p.bytes).toFixed(1)} `; });
  svg.insertAdjacentHTML('beforeend', `<path d="${d}" fill="none" stroke="#2563eb" stroke-width="2"/>`);
  svg.insertAdjacentHTML('beforeend', `<line x1="${padL}" y1="${h-padB}" x2="${w-padR}" y2="${h-padB}" stroke="#94a3b8"/>`);
  svg.insertAdjacentHTML('beforeend', `<line x1="${padL}" y1="${padT}" x2="${padL}" y2="${h-padB}" stroke="#94a3b8"/>`);
  svg.insertAdjacentHTML('beforeend', `<text x="${padL}" y="13" font-size="12" fill="#475569">peak ${fmtBytes(maxB)}</text>`);
  svg.insertAdjacentHTML('beforeend', `<text x="${w-padR}" y="${h-8}" text-anchor="end" font-size="12" fill="#475569">${fmtMs(maxT-minT)}</text>`);
  svg.insertAdjacentHTML('beforeend', `<g id="timelineSelection"></g>`);
  svg.insertAdjacentHTML('beforeend', `<rect class="timeline-hit" x="${padL}" y="${padT}" width="${w-padL-padR}" height="${h-padT-padB}"></rect>`);
  attachTimelineHandlers(svg);
  updateTimelineSelection();
}
function timelinePoint(event, svg) {
  const pt = svg.createSVGPoint();
  pt.x = event.clientX;
  pt.y = event.clientY;
  return pt.matrixTransform(svg.getScreenCTM().inverse());
}
function attachTimelineHandlers(svg) {
  const hit = svg.querySelector('.timeline-hit');
  if (!hit || !timelineState) return;
  let dragStartUs = null;
  let dragging = false;
  hit.addEventListener('pointerdown', event => {
    const p = timelinePoint(event, svg);
    dragStartUs = timelineState.tFromX(p.x);
    dragging = true;
    hit.setPointerCapture(event.pointerId);
  });
  hit.addEventListener('pointermove', event => {
    if (!dragging || dragStartUs == null) return;
    const p = timelinePoint(event, svg);
    const endUs = timelineState.tFromX(p.x);
    flameContext = {mode: 'range', startUs: Math.min(dragStartUs, endUs), endUs: Math.max(dragStartUs, endUs)};
    updateTimelineSelection();
  });
  hit.addEventListener('pointerup', event => {
    if (!dragging || dragStartUs == null) return;
    const p = timelinePoint(event, svg);
    const endUs = timelineState.tFromX(p.x);
    if (Math.abs(timelineState.x(endUs) - timelineState.x(dragStartUs)) < 4) {
      flameContext = {mode: 'live', startUs: endUs, endUs: null};
    } else {
      flameContext = {mode: 'range', startUs: Math.min(dragStartUs, endUs), endUs: Math.max(dragStartUs, endUs)};
    }
    dragging = false;
    dragStartUs = null;
    updateTimelineSelection();
	    renderContext();
  });
}
function updateTimelineSelection() {
  const svg = document.getElementById('timeline');
  const group = document.getElementById('timelineSelection');
  const status = document.getElementById('timelineSelectionStatus');
  if (!svg || !group || !timelineState) return;
  group.innerHTML = '';
  if (flameContext.mode === 'live' && flameContext.startUs != null) {
    const x = timelineState.x(flameContext.startUs);
    group.insertAdjacentHTML('beforeend', `<line class="timeline-cursor" x1="${x}" y1="${timelineState.padT}" x2="${x}" y2="${timelineState.h - timelineState.padB}"></line>`);
    if (status) status.textContent = `Report context: live allocations at ${relMs(flameContext.startUs)}. Metrics, tables, and flame graph are filtered to this point.`;
  } else if (flameContext.mode === 'range' && flameContext.startUs != null && flameContext.endUs != null) {
    const x1 = timelineState.x(flameContext.startUs);
    const x2 = timelineState.x(flameContext.endUs);
    group.insertAdjacentHTML('beforeend', `<rect class="timeline-brush" x="${Math.min(x1, x2)}" y="${timelineState.padT}" width="${Math.abs(x2 - x1)}" height="${timelineState.h - timelineState.padT - timelineState.padB}"></rect>`);
    if (status) status.textContent = `Report context: allocations overlapping ${relMs(flameContext.startUs)} to ${relMs(flameContext.endUs)}. Metrics, tables, and flame graph are filtered to this range.`;
  } else if (status) {
    status.textContent = 'Report context: full run. Click the timeline for live allocations at a time, or drag to select a range.';
  }
}
function flameColor(depth, node) {
  if ((node.live_bytes || 0) > 0) {
    const liveRatio = Math.min(1, (node.live_bytes || 0) / Math.max(1, node.bytes || 1));
    const light = Math.round(86 - liveRatio * 18);
    return `hsl(2 78% ${light}%)`;
  }
  const hue = (depth * 29 + 204) % 360;
  return `hsl(${hue} 70% 78%)`;
}
function getFlameFocus() {
  let node = DATA.flamegraph;
  flameFocusPath.forEach(index => {
    node = node.children[index];
  });
  return node || DATA.flamegraph;
}
function trimMiddle(text, maxChars) {
  if (text.length <= maxChars) return text;
  if (maxChars <= 8) return text.slice(0, Math.max(0, maxChars - 3)) + '...';
  const head = Math.ceil((maxChars - 3) * 0.58);
  const tail = Math.floor((maxChars - 3) * 0.42);
  return `${text.slice(0, head)}...${text.slice(text.length - tail)}`;
}
function childValue(node, metric) {
  return Number(node[metric] || 0);
}
function flattenFlame(node, metric, x, y, width, depth, rows, minWidth) {
  const value = Math.max(0, childValue(node, metric));
  if (width < minWidth || value <= 0) return;
  rows.push({node, x, y, width, depth});
  let cursor = x;
  const children = (node.children || []).filter(child => childValue(child, metric) > 0);
  const total = children.reduce((sum, child) => sum + childValue(child, metric), 0);
  children.forEach(child => {
    const childWidth = total > 0 ? width * childValue(child, metric) / total : 0;
    flattenFlame(child, metric, cursor, y + 24, childWidth, depth + 1, rows, minWidth);
    cursor += childWidth;
  });
}
function renderFlameCrumbs() {
  const holder = document.getElementById('flameCrumbs');
  let node = DATA.flamegraph;
  const crumbs = [`<button class="crumb ${flameFocusPath.length ? '' : 'active'}" onclick="setFlameFocus([])">all allocations</button>`];
  const path = [];
  flameFocusPath.forEach(index => {
    path.push(index);
    node = node.children[index];
    crumbs.push(`<button class="crumb active" onclick="setFlameFocus([${path.join(',')}])">${escapeHtml(trimMiddle(node.name, 46))}</button>`);
  });
  holder.innerHTML = crumbs.join('');
}
function setFlameFocus(path) {
  flameFocusPath = path;
  renderFlamegraph();
}
function renderFlamegraph() {
  const metric = document.getElementById('flameMetric')?.value || flameSelectedMetric;
  flameSelectedMetric = metric;
  const query = (document.getElementById('flameSearch')?.value || '').trim();
  const interactive = document.getElementById('flamegraphInteractive');
  const fallback = document.getElementById('flamegraphFallback');
  const meta = document.getElementById('flameMeta');
  const details = document.getElementById('flameDetails');
  const root = currentFlameRoot();
  const rootValue = childValue(root, metric);
  if (!rootValue) {
    if (interactive) {
      interactive.innerHTML = '';
      interactive.classList.add('is-hidden');
    }
    fallback?.classList.remove('is-hidden');
    renderEmptyFlamegraph(root, metric);
    return;
  }
  if (window.flamegraph && window.d3 && interactive) {
    fallback?.classList.add('is-hidden');
    interactive.classList.remove('is-hidden');
    interactive.innerHTML = '';
    flameFocusPath = [];
    renderFlameCrumbs();
    const data = toD3FlameNode(root, metric);
    flameChart = flamegraph()
      .width(1100)
      .cellHeight(20)
      .transitionDuration(180)
      .minFrameSize(1)
      .sort(true)
      .title('')
      .setDetailsElement(details)
      .setDetailsHandler(text => {
        if (details) details.textContent = text || flameDetails(root, metric);
      })
      .label(node => trimMiddle(node.data.name, Math.max(0, Math.floor((node.x1 - node.x0) * 135))))
      .setLabelHandler(node => `${node.data.name} (${fmtMetric(node.data, metric)})`)
      .onClick(node => {
        const input = document.getElementById('flameSearch');
        if (input) input.value = '';
        if (flameChart) flameChart.clear();
        if (meta) meta.textContent = `zoomed - ${fmtMetric(node.data, metric)} ${metricLabel(metric)} - ${node.data.name}`;
        if (details) details.textContent = flameDetails(node, metric);
      });
    flameChart.getValue(node => node.value || 0);
    flameChart.getChildren(node => node.children || []);
    flameChart.setSearchMatch(flameTextMatches);
    flameChart.setSearchHandler((matches, matchedValue, totalValue) => {
      const pct = totalValue ? (matchedValue / totalValue * 100).toFixed(2) : '0.00';
      if (meta) {
        meta.textContent = `${matches.length} highlighted frame${matches.length === 1 ? '' : 's'} - ${fmtMetric({[metric]: matchedValue, allocations: matchedValue}, metric)} matched - ${pct}% of root`;
      }
    });
    flameChart.setDetailsHandler(text => {
      if (details) details.textContent = text || flameDetails(root, metric);
    });
    flameChart(d3.select('#flamegraphInteractive').datum(data));
    if (details) details.textContent = contextLabel(root);
    if (query) {
      flameChart.search(query);
    } else if (meta) {
      meta.textContent = `${(root.children || []).length} top-level branch${(root.children || []).length === 1 ? '' : 'es'} - interactive - ${contextLabel(root)}`;
    }
    return;
  }

  const focus = root;
  const svg = document.getElementById('flamegraphFallbackSvg');
  renderFlameCrumbs();
  if (!svg) return;
  svg.innerHTML = '';
  const w = 1100, padX = 8, top = 26, frameH = 20, minWidth = 2.5;
  const rows = [];
  flattenFlame(focus, metric, padX, top, w - padX * 2, 0, rows, minWidth);
  const maxDepth = rows.reduce((max, row) => Math.max(max, row.depth), 0);
  const h = top + (maxDepth + 1) * 24 + 28;
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  const matches = [];
  svg.insertAdjacentHTML('beforeend', `<text x="${padX}" y="16" class="flame-root-label">${escapeHtml(focus.name)} - ${fmtMetric(focus, metric)} ${escapeHtml(metricLabel(metric))}</text>`);
  rows.forEach(row => {
    const node = row.node;
    const isMatch = query && node.name.toLowerCase().includes(query);
    if (isMatch) matches.push(node);
    const textChars = Math.floor((row.width - 10) / 6.4);
    const label = textChars >= 5 ? escapeHtml(trimMiddle(node.name, textChars)) : '';
    const pct = (childValue(node, metric) / rootValue * 100).toFixed(1);
    const className = `flame-frame${isMatch ? ' flame-match' : ''}${query && !isMatch ? ' flame-faded' : ''}`;
    const title = `${node.name}\\n${fmtMetric(node, metric)} ${metricLabel(metric)} (${pct}%)\\nbytes ${fmtBytes(node.bytes || 0)}, live ${fmtBytes(node.live_bytes || 0)}, allocations ${node.allocations || 0}`;
    svg.insertAdjacentHTML('beforeend', `<g class="${className}" tabindex="0"><title>${escapeHtml(title)}</title><rect x="${row.x.toFixed(2)}" y="${row.y}" width="${Math.max(0, row.width).toFixed(2)}" height="${frameH}" rx="2" fill="${flameColor(row.depth, node)}"></rect>${label ? `<text x="${(row.x + 5).toFixed(2)}" y="${row.y + 14}">${label}</text>` : ''}</g>`);
  });
  [...svg.querySelectorAll('.flame-frame')].forEach((group, i) => {
    group.addEventListener('click', () => {
      const clicked = rows[i];
      if (clicked.depth === 0) return;
      const path = locateFlamePath(focus, clicked.node);
      if (path) setFlameFocus(flameFocusPath.concat(path));
    });
    group.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        group.dispatchEvent(new Event('click'));
      }
    });
  });
  const topChildren = (focus.children || []).filter(child => childValue(child, metric) > 0).length;
  const matchText = query ? ` - ${matches.length} highlighted frame${matches.length === 1 ? '' : 's'}` : '';
  meta.textContent = `${topChildren} top-level branch${topChildren === 1 ? '' : 'es'} - ${rows.length} visible frame${rows.length === 1 ? '' : 's'} - root ${fmtMetric(focus, metric)} ${metricLabel(metric)}${matchText}`;
}
function renderEmptyFlamegraph(root, metric) {
  const svg = document.getElementById('flamegraphFallbackSvg');
  const meta = document.getElementById('flameMeta');
  const details = document.getElementById('flameDetails');
  renderFlameCrumbs();
  if (!svg) return;
  svg.innerHTML = '';
  svg.setAttribute('viewBox', '0 0 1100 78');
  if (meta) meta.textContent = `No ${metricLabel(metric)} to display - ${contextLabel(root)}`;
  if (details) details.textContent = contextLabel(root);
  svg.insertAdjacentHTML('beforeend', `<text x="16" y="34" class="flame-root-label">No ${escapeHtml(metricLabel(metric))} in the selected time context.</text>`);
  svg.insertAdjacentHTML('beforeend', '<text x="16" y="58" class="flame-root-label">Click another time, drag a wider range, or return to the full run.</text>');
}
function resetFlameContext() {
  flameContext = {mode: 'full', startUs: null, endUs: null};
  updateTimelineSelection();
  renderContext();
}
function resetFlameZoom() {
  if (flameChart) {
    flameChart.resetZoom();
  }
}
function searchFlamegraph() {
  const query = (document.getElementById('flameSearch')?.value || '').trim();
  if (!flameChart) {
    renderFlamegraph();
    return;
  }
  if (query) {
    flameChart.search(query);
  } else {
    flameChart.clear();
    const metric = document.getElementById('flameMetric')?.value || flameSelectedMetric;
    const root = currentFlameRoot();
    const branches = (root.children || []).length;
    const meta = document.getElementById('flameMeta');
    if (meta) {
      meta.textContent = `${branches} top-level branch${branches === 1 ? '' : 'es'} - interactive - ${contextLabel(root)}`;
    }
  }
}
function clearFlameSearch() {
  const input = document.getElementById('flameSearch');
  if (input) input.value = '';
  if (flameChart) {
    flameChart.clear();
  }
  renderFlamegraph();
}
function locateFlamePath(root, target) {
  const stack = [[root, []]];
  while (stack.length) {
    const [node, path] = stack.pop();
    if (node === target) return path;
    (node.children || []).forEach((child, index) => stack.push([child, path.concat(index)]));
  }
  return null;
}
function renderRows(kind) {
  const q = document.getElementById(`${kind}Filter`).value.toLowerCase();
  const limit = Number(document.getElementById(`${kind}Limit`).value || 100);
  const body = document.getElementById(`${kind}Body`);
  const rows = currentRows(kind).filter(r => JSON.stringify(r).toLowerCase().includes(q)).slice(0, limit);
  const count = document.getElementById(`${kind}Count`);
  if (count) count.textContent = String(currentRows(kind).length);
  body.innerHTML = rows.length ? rows.map(r => {
    if (kind === 'pools') {
      return `<tr><td>${escapeHtml(r.pool)}</td><td class="num">${r.allocations}</td><td class="num">${fmtBytes(r.bytes)}</td><td class="num">${r.live}</td><td class="num">${fmtBytes(r.live_bytes)}</td></tr>`;
    }
    if (kind === 'stacks') {
      return `<tr><td class="num">${r.stack_id}</td><td class="num">${r.allocations}</td><td class="num">${fmtBytes(r.bytes)}</td><td class="num">${r.live}</td><td class="num">${fmtBytes(r.live_bytes)}</td><td><pre class="stack">${escapeHtml(r.stack)}</pre></td></tr>`;
    }
    return `<tr><td>${escapeHtml(r.pool)}</td><td class="num">${escapeHtml(r.addr)}</td><td class="num">${fmtBytes(r.size)}</td><td class="num">${r.stack_id}</td><td class="num">${r.start_seq}</td><td class="num">${r.end_seq ?? 'live'}</td><td class="num">${fmtMs(r.lifetime_us)}</td></tr>`;
	  }).join('') : '<tr><td colspan="7" class="empty">No rows in the selected time context.</td></tr>';
}
function renderLongest() {
  const body = document.getElementById('longestBody');
  const rows = lifetimesForContext().sort((a, b) => (b.lifetime_us - a.lifetime_us) || (b.size - a.size)).slice(0, 25);
  const count = document.getElementById('longestCount');
  if (count) count.textContent = String(rows.length);
  body.innerHTML = rows.length
    ? rows.map(r => `<tr><td>${escapeHtml(r.pool)}</td><td class="num">${escapeHtml(r.addr)}</td><td class="num">${fmtBytes(r.size)}</td><td class="num">${r.stack_id}</td><td class="num">${r.start_seq}</td><td class="num">${r.end_seq ?? 'live'}</td><td class="num">${fmtMs(r.lifetime_us)}</td></tr>`).join('')
    : '<tr><td colspan="7" class="empty">No allocations in the selected time context.</td></tr>';
}
function renderContext() {
  updateContextMetrics();
  renderAnomalies();
  renderLongest();
  renderFlamegraph();
  ['pools','stacks','lifetimes'].forEach(renderRows);
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
window.addEventListener('DOMContentLoaded', () => {
  drawTimeline();
  renderContext();
});
"""
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bolt memory trace</title>
  <style>{css}</style>
</head>
<body>
  <div class="wrap">
    <h1>Bolt memory trace</h1>
	    <div class="meta">Trace: <code>{html.escape(str(trace_path))}</code> · binary v{html.escape(str(trace_metadata.get('version', 'unknown')))} · PID {html.escape(str(trace_metadata.get('pid', 'unknown')))}</div>
	    <div class="verdict {verdict_class} icon-inline">{icon('check' if not anomaly_count else 'alert')}<span>{html.escape(verdict_text)}</span></div>
	    <div class="context-bar">
	      <span class="context-pill">{icon('clock', 'icon-sm')}<span id="contextLabel">Full run</span></span>
	      <span class="context-pill">{icon('route', 'icon-sm')}<span id="contextRange">0.00ms to {fmt_ms(summary['duration_us'])}</span></span>
	    </div>
	    <div class="grid">
	      <div class="card"><div class="card-icon">{icon('activity')}</div><div><div class="metric">{summary['events']}</div><div class="label">trace events</div><div class="subtext">recorded MemoryPool operations</div></div></div>
	      <div class="card"><div class="card-icon">{icon('memory')}</div><div><div class="metric">{fmt_bytes(summary['peak_bytes'])}</div><div class="label">peak usage</div><div class="subtext">at {fmt_ms(summary['peak_time_us'] - (model['timeline'][0]['time_us'] if model['timeline'] else 0))}</div></div></div>
	      <div class="card"><div class="card-icon">{icon('database')}</div><div><div id="contextBytesValue" class="metric">{fmt_bytes(summary['total_alloc_bytes'])}</div><div class="label">allocated bytes</div><div id="contextBytesSub" class="subtext">full run</div></div></div>
	      <div class="card"><div class="card-icon">{icon('layers')}</div><div><div id="contextAllocationsValue" class="metric">{summary['allocations']}</div><div class="label">allocations</div><div id="contextAllocationsSub" class="subtext">full run</div></div></div>
	      <div class="card {'bad' if summary['live_allocations'] else 'good'}"><div class="card-icon">{icon('alert' if summary['live_allocations'] else 'check')}</div><div><div id="contextLiveValue" class="metric">{summary['live_allocations']}</div><div class="label">live allocations</div><div id="contextLiveSub" class="subtext">{fmt_bytes(model['flamegraph']['live_bytes'])} live bytes</div></div></div>
	      <div class="card {'bad' if summary['unmatched_frees'] else 'good'}"><div class="card-icon">{icon('alert' if summary['unmatched_frees'] else 'check')}</div><div><div class="metric">{summary['unmatched_frees']}</div><div class="label">unmatched frees/grows</div><div class="subtext">trace consistency checks</div></div></div>
	      <div class="card"><div class="card-icon">{icon('timer')}</div><div><div id="contextLifetimesValue" class="metric">{len(lifetime_rows)}</div><div class="label">lifetime rows</div><div id="contextLifetimesSub" class="subtext">matching allocation rows</div></div></div>
	    </div>
	    <h2 class="section-title">{icon('alert')}<span>Anomaly review</span></h2>
    <div class="panel"><table><thead><tr><th>signal</th><th>pool</th><th class="num">addr</th><th class="num">size</th><th class="num">seq</th><th class="num">stack</th></tr></thead><tbody id="anomaliesBody">{''.join(anomaly_rows)}</tbody></table></div>
	    <h2 class="section-title">{icon('activity')}<span>Memory usage timeline</span></h2>
    <div class="panel">
      <svg id="timeline" role="img" aria-label="Memory usage over time"></svg>
	      <div id="timelineSelectionStatus" class="timeline-status">Report context: full run. Click the timeline for live allocations at a time, or drag to select a range.</div>
    </div>
	    <h2 class="section-title">{icon('flame')}<span>Allocation flame graph</span></h2>
    <div class="toolbar flame-toolbar">
      <div class="flame-controls">
        <select id="flameMetric" onchange="renderFlamegraph()" aria-label="flame graph metric">
          <option value="bytes">allocated bytes</option>
          <option value="live_bytes">live bytes</option>
          <option value="allocations">allocations</option>
        </select>
        <input id="flameSearch" placeholder="highlight frames" oninput="searchFlamegraph()">
        <button type="button" class="icon-inline" onclick="resetFlameContext()">{icon('clock', 'icon-sm')}<span>Full run</span></button>
        <button type="button" class="icon-inline" onclick="resetFlameZoom()">{icon('route', 'icon-sm')}<span>Reset zoom</span></button>
        <button type="button" class="icon-inline" onclick="clearFlameSearch()">{icon('check', 'icon-sm')}<span>Clear</span></button>
      </div>
      <div id="flameMeta" class="flame-meta">{html.escape(flame_meta)}</div>
    </div>
    <div id="flameDetails" class="flame-details">all allocations - {fmt_bytes(model['flamegraph']['bytes'])} allocated bytes</div>
    <div id="flameCrumbs" class="flame-crumbs"><button class="crumb active">all allocations</button></div>
    <div class="panel flame-panel">
      <div id="flamegraphInteractive" class="is-hidden"></div>
      <div id="flamegraphFallback"><svg id="flamegraphFallbackSvg" role="img" aria-label="Allocation flame graph fallback" viewBox="{html.escape(flame_view_box)}">{flame_svg}</svg></div>
    </div>
	    <details open>
	      <summary>{icon('timer')}Longest-lived allocations (<span id="longestCount">{len(longest_rows)}</span>)</summary>
      <div class="panel"><table><thead><tr><th>pool</th><th class="num">addr</th><th class="num">size</th><th class="num">stack</th><th class="num">start</th><th class="num">end</th><th class="num">lifetime</th></tr></thead><tbody id="longestBody">{''.join(longest_rows)}</tbody></table></div>
    </details>
    <details>
	      <summary>{icon('database')}Pool distribution (<span id="poolsCount">{len(pool_rows)}</span>)</summary>
      <div class="toolbar"><input id="poolsFilter" placeholder="filter pools" oninput="renderRows('pools')"><select id="poolsLimit" onchange="renderRows('pools')"><option>50</option><option>100</option><option>500</option></select></div>
      <div class="panel"><table><thead><tr><th>pool</th><th class="num">allocations</th><th class="num">bytes</th><th class="num">live</th><th class="num">live bytes</th></tr></thead><tbody id="poolsBody">{''.join(pool_rows)}</tbody></table></div>
    </details>
    <details>
	      <summary>{icon('layers')}Allocation call sites (<span id="stacksCount">{len(stack_rows)}</span>)</summary>
      <div class="toolbar"><input id="stacksFilter" placeholder="filter stacks" oninput="renderRows('stacks')"><select id="stacksLimit" onchange="renderRows('stacks')"><option>25</option><option>100</option><option>500</option></select></div>
      <div class="panel"><table><thead><tr><th class="num">stack</th><th class="num">allocations</th><th class="num">bytes</th><th class="num">live</th><th class="num">live bytes</th><th>frames</th></tr></thead><tbody id="stacksBody">{''.join(stack_rows)}</tbody></table></div>
    </details>
    <details>
	      <summary>{icon('table')}All allocation lifetimes (<span id="lifetimesCount">{len(lifetime_rows)}</span>)</summary>
      <div class="toolbar"><input id="lifetimesFilter" placeholder="filter lifetimes" oninput="renderRows('lifetimes')"><select id="lifetimesLimit" onchange="renderRows('lifetimes')"><option>50</option><option>100</option><option>500</option></select></div>
      <div class="panel"><table><thead><tr><th>pool</th><th class="num">addr</th><th class="num">size</th><th class="num">stack</th><th class="num">start</th><th class="num">end</th><th class="num">lifetime</th></tr></thead><tbody id="lifetimesBody">{''.join(lifetime_rows)}</tbody></table></div>
    </details>
	    <details>
	      <summary>{icon('memory')}Capture metadata</summary>
	      <div class="panel"><table><tbody>
	        <tr><th>Protocol</th><td>binary v{html.escape(str(trace_metadata.get('version', 'unknown')))}</td><th>Clean shutdown</th><td>{'yes' if trace_complete else 'no'}</td></tr>
	        <tr><th>Records</th><td>{html.escape(str(trace_metadata.get('records_read', 0)))}</td><th>Mappings</th><td>{html.escape(str(trace_metadata.get('mappings', 0)))}</td></tr>
	        <tr><th>Stack threshold</th><td>{fmt_bytes(int(trace_metadata.get('configuration', {}).get('stack_min_bytes', 0)))}</td><th>Max frames</th><td>{html.escape(str(trace_metadata.get('configuration', {}).get('max_stack_frames', 0)))}</td></tr>
	        <tr><th>Write buffer</th><td>{fmt_bytes(int(trace_metadata.get('configuration', {}).get('buffer_bytes', 0)))}</td><th>Checkpoint interval</th><td>{html.escape(str(trace_metadata.get('configuration', {}).get('checkpoint_events', 0)))} events</td></tr>
	        <tr><th>Pool filter</th><td colspan="3"><code>{html.escape(str(trace_metadata.get('configuration', {}).get('pool_regex', '') or '(none)'))}</code></td></tr>
	      </tbody></table></div>
	    </details>
  </div>
  <script>{d3_js}</script>
  <script>{flamegraph_js}</script>
  <script>{js.replace('__DATA__', payload)}</script>
</body>
</html>
"""
    output.write_text(body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "trace",
        type=Path,
        help="Bolt memory trace binary v2 file from BOLT_MEMORY_TRACE_FILE",
    )
    parser.add_argument("-o", "--output", type=Path, required=True, help="Output self-contained HTML report")
    args = parser.parse_args()
    events, stacks, metadata = read_trace(args.trace)
    model = build_model(events, stacks)
    model["trace_metadata"] = metadata[0]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    render_html(model, trace_path=args.trace, output=args.output)
    print(f"wrote {args.output}")
    print(
        f"events={model['summary']['events']} peak={fmt_bytes(model['summary']['peak_bytes'])} "
        f"live={model['summary']['live_allocations']} unmatched={model['summary']['unmatched_frees']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
