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

"""Analyze a Bolt memory trace and emit bounded, model-friendly JSON."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import bolt_memory_trace_viewer as trace_viewer


SCHEMA_VERSION = "1.0"
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile_value
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def ratio(numerator: int | float, denominator: int | float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def relative_us(timestamp_us: int, start_us: int) -> int:
    return max(0, timestamp_us - start_us)


def stack_evidence(
    stack_id: int,
    stack_frames: dict[str, list[str]],
    max_frames: int,
) -> dict[str, Any]:
    frames = stack_frames.get(
        str(stack_id), ["(stack capture disabled or unavailable)"]
    )
    selected_frames = frames[-max_frames:]
    return {
        "stack_id": stack_id,
        "frames": selected_frames,
        "frame_order": "root_to_leaf",
        "frames_truncated": len(frames) > max_frames,
    }


def aggregate_allocations(
    allocations: Iterable[dict[str, Any]],
    key_name: str,
) -> list[dict[str, Any]]:
    groups: dict[Any, dict[str, Any]] = {}
    for allocation in allocations:
        key = allocation[key_name]
        row = groups.setdefault(
            key,
            {
                key_name: key,
                "bytes": 0,
                "allocations": 0,
                "byte_lifetime_us": 0,
            },
        )
        row["bytes"] += int(allocation["size"])
        row["allocations"] += 1
        row["byte_lifetime_us"] += int(allocation["size"]) * int(
            allocation["lifetime_us"]
        )
    return sorted(
        groups.values(),
        key=lambda row: (
            row["bytes"],
            row["byte_lifetime_us"],
            row["allocations"],
        ),
        reverse=True,
    )


def top_contributors(
    allocations: list[dict[str, Any]],
    total_bytes: int,
    stack_frames: dict[str, list[str]],
    top: int,
    max_frames: int,
) -> dict[str, list[dict[str, Any]]]:
    pools = aggregate_allocations(allocations, "pool")[:top]
    stacks = aggregate_allocations(allocations, "stack_id")[:top]
    for row in pools:
        row["percent"] = round(ratio(row["bytes"], total_bytes) * 100, 2)
    for row in stacks:
        row["percent"] = round(ratio(row["bytes"], total_bytes) * 100, 2)
        row.update(stack_evidence(int(row["stack_id"]), stack_frames, max_frames))
    return {"pools": pools, "stacks": stacks}


def lifetime_rows(
    allocations: list[dict[str, Any]],
    trace_end_us: int,
) -> list[dict[str, Any]]:
    rows = []
    for allocation in allocations:
        end_us = (
            int(allocation["end_us"])
            if allocation["end_us"] is not None
            else trace_end_us
        )
        row = dict(allocation)
        row["lifetime_us"] = max(0, end_us - int(allocation["start_us"]))
        row["live"] = allocation["end_us"] is None
        rows.append(row)
    return rows


def replay_peak(
    events: list[dict[str, Any]],
    peak_sequence: int,
    window_start_us: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    active: dict[str, dict[str, Any]] = {}
    growth: list[dict[str, Any]] = []

    def key(event: dict[str, Any]) -> str:
        allocation_id = int(event.get("allocation_id", 0))
        return (
            f"id:{allocation_id}"
            if allocation_id
            else f"addr:{event.get('addr', '')}"
        )

    for event in events:
        event_sequence = int(event["seq"])
        if event_sequence > peak_sequence:
            break
        operation = event.get("op")
        event_key = key(event)
        size = int(event.get("size", 0))
        timestamp_us = int(event.get("time_us", 0))
        if operation == "alloc":
            allocation = {
                "allocation_id": event.get("allocation_id"),
                "pool": event.get("pool", ""),
                "addr": event.get("addr", ""),
                "size": size,
                "stack_id": int(event.get("stack_id", 0)),
                "start_us": timestamp_us,
                "end_us": None,
                "lifetime_us": 0,
                "live": True,
            }
            active[event_key] = allocation
            if timestamp_us >= window_start_us:
                growth.append(dict(allocation))
        elif operation == "free":
            active.pop(event_key, None)
        elif operation == "grow":
            previous = active.get(event_key)
            previous_size = (
                int(previous["size"])
                if previous is not None
                else int(event.get("old_size", 0))
            )
            allocation = {
                "allocation_id": event.get("allocation_id"),
                "pool": event.get("pool", previous["pool"] if previous else ""),
                "addr": event.get("addr", ""),
                "size": size,
                "stack_id": int(
                    event.get(
                        "stack_id",
                        previous["stack_id"] if previous else 0,
                    )
                ),
                "start_us": (
                    int(previous["start_us"])
                    if previous is not None
                    else timestamp_us
                ),
                "end_us": None,
                "lifetime_us": 0,
                "live": True,
            }
            active[event_key] = allocation
            growth_delta = max(0, size - previous_size)
            if growth_delta and timestamp_us >= window_start_us:
                growth_row = dict(allocation)
                growth_row["size"] = growth_delta
                growth_row["start_us"] = timestamp_us
                growth.append(growth_row)
    return list(active.values()), growth


def first_time_at_or_below(
    timeline: list[dict[str, Any]],
    peak_index: int,
    threshold_bytes: float,
) -> int | None:
    for point in timeline[peak_index:]:
        if int(point["bytes"]) <= threshold_bytes:
            return int(point["time_us"])
    return None


def finding(
    finding_id: str,
    severity: str,
    category: str,
    title: str,
    summary: str,
    evidence: dict[str, Any],
    recommendation: str,
) -> dict[str, Any]:
    return {
        "id": finding_id,
        "severity": severity,
        "category": category,
        "title": title,
        "summary": summary,
        "evidence": evidence,
        "recommendation": recommendation,
    }


def analyze(
    trace_path: Path,
    *,
    top: int,
    max_frames: int,
    peak_window_ms: float | None,
    long_lived_ms: float,
    long_lived_ratio: float,
    long_lived_min_bytes: int,
    short_lived_ms: float,
    short_lived_byte_ratio: float,
    short_lived_min_allocations: int,
    churn_ratio: float,
    peak_concentration: float,
    max_findings: int,
) -> dict[str, Any]:
    events, stacks, metadata_rows = trace_viewer.read_trace(trace_path)
    metadata = metadata_rows[0]
    model = trace_viewer.build_model(events, stacks)
    timeline = model["timeline"]
    allocations = lifetime_rows(
        model["allocations"],
        int(timeline[-1]["time_us"]) if timeline else 0,
    )
    stack_frames = model["stack_frames"]
    summary = model["summary"]

    if timeline:
        trace_start_us = int(timeline[0]["time_us"])
        trace_end_us = int(timeline[-1]["time_us"])
        peak_index = max(
            range(len(timeline)), key=lambda index: int(timeline[index]["bytes"])
        )
        peak_time_us = int(timeline[peak_index]["time_us"])
        peak_bytes = int(timeline[peak_index]["bytes"])
    else:
        trace_start_us = trace_end_us = peak_time_us = peak_bytes = peak_index = 0
    duration_us = max(0, trace_end_us - trace_start_us)

    default_peak_window_us = min(
        1_000_000,
        max(1_000, int(duration_us * 0.10)),
    )
    peak_window_us = (
        max(1, int(peak_window_ms * 1000))
        if peak_window_ms is not None
        else default_peak_window_us
    )
    peak_window_start_us = max(trace_start_us, peak_time_us - peak_window_us)
    peak_sequence = int(timeline[peak_index]["seq"]) if timeline else 0
    active_at_peak, growth_allocations = replay_peak(
        events,
        peak_sequence,
        peak_window_start_us,
    )
    growth_bytes = sum(int(row["size"]) for row in growth_allocations)

    peak_contributors = top_contributors(
        active_at_peak,
        peak_bytes,
        stack_frames,
        top,
        max_frames,
    )
    growth_contributors = top_contributors(
        growth_allocations,
        growth_bytes,
        stack_frames,
        top,
        max_frames,
    )

    drop_50_us = first_time_at_or_below(
        timeline, peak_index, peak_bytes * 0.50
    )
    drop_10_us = first_time_at_or_below(
        timeline, peak_index, peak_bytes * 0.10
    )
    post_peak = {
        "bytes_at_end": int(timeline[-1]["bytes"]) if timeline else 0,
        "percent_retained_at_end": round(
            ratio(int(timeline[-1]["bytes"]) if timeline else 0, peak_bytes) * 100,
            2,
        ),
        "time_to_50_percent_us": (
            drop_50_us - peak_time_us if drop_50_us is not None else None
        ),
        "time_to_10_percent_us": (
            drop_10_us - peak_time_us if drop_10_us is not None else None
        ),
    }

    lifetimes = [int(row["lifetime_us"]) for row in allocations]
    byte_lifetimes = [
        int(row["size"]) * int(row["lifetime_us"]) for row in allocations
    ]
    q1 = percentile(lifetimes, 0.25)
    q3 = percentile(lifetimes, 0.75)
    statistical_outlier_us = q3 + 3 * max(0.0, q3 - q1)
    configured_long_us = min(
        int(long_lived_ms * 1000),
        max(1, int(duration_us * long_lived_ratio)),
    )
    effective_long_us = max(configured_long_us, int(statistical_outlier_us))
    suspicious_lifetimes = [
        row
        for row in allocations
        if int(row["size"]) >= long_lived_min_bytes
        and (
            row["live"]
            or int(row["lifetime_us"]) >= effective_long_us
        )
    ]
    suspicious_lifetimes.sort(
        key=lambda row: (
            row["live"],
            int(row["size"]) * int(row["lifetime_us"]),
            int(row["size"]),
        ),
        reverse=True,
    )
    long_lived_evidence = []
    for row in suspicious_lifetimes[:top]:
        evidence = {
            "allocation_id": row.get("allocation_id"),
            "pool": row["pool"],
            "address": row["addr"],
            "size_bytes": int(row["size"]),
            "lifetime_us": int(row["lifetime_us"]),
            "lifetime_percent_of_trace": round(
                ratio(int(row["lifetime_us"]), duration_us) * 100, 2
            ),
            "live_at_end": bool(row["live"]),
            "start_offset_us": relative_us(
                int(row["start_us"]), trace_start_us
            ),
            "end_offset_us": (
                relative_us(int(row["end_us"]), trace_start_us)
                if row["end_us"] is not None
                else None
            ),
        }
        evidence.update(
            stack_evidence(int(row["stack_id"]), stack_frames, max_frames)
        )
        long_lived_evidence.append(evidence)

    short_lived_us = max(1, int(short_lived_ms * 1000))
    short_lived = [
        row
        for row in allocations
        if not row["live"] and int(row["lifetime_us"]) <= short_lived_us
    ]
    short_lived_bytes = sum(int(row["size"]) for row in short_lived)
    total_allocated_bytes = int(summary["total_alloc_bytes"])
    allocation_rate = ratio(len(allocations), duration_us / 1_000_000)
    byte_rate = ratio(total_allocated_bytes, duration_us / 1_000_000)
    churn = {
        "total_to_peak_ratio": round(
            ratio(total_allocated_bytes, peak_bytes), 3
        ),
        "allocations_per_second": round(allocation_rate, 2),
        "bytes_per_second": round(byte_rate, 2),
        "short_lived_threshold_us": short_lived_us,
        "short_lived_allocations": len(short_lived),
        "short_lived_allocation_percent": round(
            ratio(len(short_lived), len(allocations)) * 100, 2
        ),
        "short_lived_bytes": short_lived_bytes,
        "short_lived_byte_percent": round(
            ratio(short_lived_bytes, total_allocated_bytes) * 100, 2
        ),
        "contributors": top_contributors(
            short_lived,
            short_lived_bytes,
            stack_frames,
            top,
            max_frames,
        ),
    }

    lifetime_summary = {
        "count": len(lifetimes),
        "min_us": int(min(lifetimes, default=0)),
        "p50_us": int(percentile(lifetimes, 0.50)),
        "p90_us": int(percentile(lifetimes, 0.90)),
        "p95_us": int(percentile(lifetimes, 0.95)),
        "p99_us": int(percentile(lifetimes, 0.99)),
        "max_us": int(max(lifetimes, default=0)),
        "mean_us": round(ratio(sum(lifetimes), len(lifetimes)), 2),
        "byte_weighted_mean_us": round(
            ratio(sum(byte_lifetimes), total_allocated_bytes), 2
        ),
        "configured_threshold_us": configured_long_us,
        "statistical_outlier_threshold_us": int(statistical_outlier_us),
        "effective_threshold_us": effective_long_us,
        "suspicious_count": len(suspicious_lifetimes),
        "top_suspicious": long_lived_evidence,
    }

    findings: list[dict[str, Any]] = []
    integrity_errors = metadata.get("integrity_errors", [])
    if integrity_errors:
        findings.append(
            finding(
                "trace-integrity",
                "critical",
                "integrity",
                "Trace is incomplete or inconsistent",
                "Some conclusions may be unreliable because capture integrity checks failed.",
                {"errors": integrity_errors},
                "Repeat the capture and ensure the process writes a clean-shutdown trailer.",
            )
        )
    if int(summary["unmatched_frees"]) > 0:
        findings.append(
            finding(
                "unmatched-free",
                "critical",
                "lifecycle",
                "Free or grow events lack matching allocations",
                "The allocation lifecycle cannot be reconstructed losslessly.",
                {
                    "count": int(summary["unmatched_frees"]),
                    "examples": model["unmatched_frees"][:top],
                },
                "Check filtering scope, capture start time, grow semantics, and dropped events.",
            )
        )
    if int(summary["live_allocations"]) > 0:
        live_rows = [row for row in long_lived_evidence if row["live_at_end"]]
        findings.append(
            finding(
                "live-at-end",
                "critical",
                "retention",
                "Allocations remain live at trace end",
                "These allocations may be leaks or intentionally retained state.",
                {
                    "allocations": int(summary["live_allocations"]),
                    "bytes": int(post_peak["bytes_at_end"]),
                    "top_allocation_ids": [
                        row["allocation_id"] for row in live_rows[:top]
                    ],
                    "evidence_refs": ["lifetimes.top_suspicious"],
                },
                "Confirm ownership at shutdown and add explicit release points for unintended retention.",
            )
        )
    if suspicious_lifetimes:
        findings.append(
            finding(
                "long-lifetime",
                "warning",
                "retention",
                "Large allocations have unusually long lifetimes",
                "Their lifetime exceeds both the configured relative/absolute threshold and the distribution outlier threshold.",
                {
                    "count": len(suspicious_lifetimes),
                    "effective_threshold_us": effective_long_us,
                    "evidence_refs": ["lifetimes.top_suspicious"],
                },
                "Inspect the retaining owner and narrow allocation scope or release buffers earlier.",
            )
        )
    top_peak_pool = (
        peak_contributors["pools"][0] if peak_contributors["pools"] else None
    )
    if top_peak_pool and float(top_peak_pool["percent"]) >= peak_concentration * 100:
        findings.append(
            finding(
                "peak-concentration",
                "info",
                "peak",
                "Memory peak is concentrated in one pool",
                "A single pool accounts for most memory live at the peak.",
                {
                    "threshold_percent": peak_concentration * 100,
                    "top_pool": top_peak_pool["pool"],
                    "top_pool_bytes": top_peak_pool["bytes"],
                    "top_pool_percent": top_peak_pool["percent"],
                    "top_stack_id": (
                        peak_contributors["stacks"][0]["stack_id"]
                        if peak_contributors["stacks"]
                        else None
                    ),
                    "evidence_refs": ["peak.live_contributors"],
                },
                "Inspect the dominant pool and stack first; consider streaming, spilling, or smaller batches.",
            )
        )
    if peak_bytes and post_peak["time_to_50_percent_us"] is None:
        findings.append(
            finding(
                "slow-post-peak-release",
                "warning",
                "retention",
                "Memory does not fall below half of peak before trace end",
                "The peak is followed by sustained retention rather than prompt release.",
                {
                    "peak_bytes": peak_bytes,
                    "bytes_at_end": post_peak["bytes_at_end"],
                    "percent_retained_at_end": post_peak[
                        "percent_retained_at_end"
                    ],
                },
                "Inspect owners of peak-live allocations and verify buffers are released after their last use.",
            )
        )
    high_short_lived_share = (
        len(short_lived) >= short_lived_min_allocations
        and ratio(short_lived_bytes, total_allocated_bytes)
        >= short_lived_byte_ratio
    )
    if churn["total_to_peak_ratio"] >= churn_ratio or high_short_lived_share:
        findings.append(
            finding(
                "allocation-churn",
                "warning",
                "churn",
                "Allocation volume is high relative to peak memory",
                "The process allocates and frees substantially more memory than it holds at once.",
                {
                    "threshold_ratio": churn_ratio,
                    "short_lived_byte_ratio_threshold": short_lived_byte_ratio,
                    "short_lived_min_allocations": short_lived_min_allocations,
                    "total_to_peak_ratio": churn["total_to_peak_ratio"],
                    "short_lived_allocations": churn[
                        "short_lived_allocations"
                    ],
                    "short_lived_allocation_percent": churn[
                        "short_lived_allocation_percent"
                    ],
                    "short_lived_bytes": churn["short_lived_bytes"],
                    "short_lived_byte_percent": churn[
                        "short_lived_byte_percent"
                    ],
                    "evidence_refs": ["churn.contributors"],
                },
                "Reuse buffers, reduce temporary vectors, and inspect the top short-lived allocation stacks.",
            )
        )

    findings.sort(
        key=lambda row: (
            SEVERITY_RANK[row["severity"]],
            row["id"],
        ),
        reverse=True,
    )
    findings = findings[:max_findings]
    overall_severity = max(
        (row["severity"] for row in findings),
        key=lambda severity: SEVERITY_RANK[severity],
        default="info",
    )

    assessment_summary = (
        "Critical memory issues were detected."
        if overall_severity == "critical"
        else "Potential memory inefficiencies were detected."
        if overall_severity == "warning"
        else "No configured memory issue threshold was exceeded."
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "bolt-memory-analyze",
        "trace": {
            "path": str(trace_path),
            "protocol_version": metadata.get("version"),
            "pid": metadata.get("pid"),
            "clean_shutdown": metadata.get("clean_shutdown"),
            "integrity_errors": metadata.get("integrity_errors", []),
            "events": len(events),
            "records": metadata.get("records_read"),
            "duration_us": duration_us,
            "configuration": metadata.get("configuration", {}),
        },
        "assessment": {
            "severity": overall_severity,
            "summary": assessment_summary,
            "finding_count": len(findings),
            "confidence": (
                "high" if metadata.get("clean_shutdown") else "low"
            ),
        },
        "thresholds": {
            "peak_window_us": peak_window_us,
            "long_lived_absolute_us": int(long_lived_ms * 1000),
            "long_lived_trace_ratio": long_lived_ratio,
            "long_lived_min_bytes": long_lived_min_bytes,
            "short_lived_us": short_lived_us,
            "short_lived_byte_ratio": short_lived_byte_ratio,
            "short_lived_min_allocations": short_lived_min_allocations,
            "churn_total_to_peak_ratio": churn_ratio,
            "peak_concentration_percent": peak_concentration * 100,
        },
        "metrics": {
            "total_allocations": len(allocations),
            "total_allocated_bytes": total_allocated_bytes,
            "peak_bytes": peak_bytes,
            "live_allocations_at_end": int(summary["live_allocations"]),
            "bytes_at_end": post_peak["bytes_at_end"],
            "unmatched_frees": int(summary["unmatched_frees"]),
        },
        "peak": {
            "offset_us": relative_us(peak_time_us, trace_start_us),
            "absolute_monotonic_us": peak_time_us,
            "bytes": peak_bytes,
            "active_allocations": len(active_at_peak),
            "growth_window": {
                "start_offset_us": relative_us(
                    peak_window_start_us, trace_start_us
                ),
                "end_offset_us": relative_us(
                    peak_time_us, trace_start_us
                ),
                "new_live_bytes": growth_bytes,
                "percent_of_peak": round(
                    ratio(growth_bytes, peak_bytes) * 100, 2
                ),
                "contributors": growth_contributors,
            },
            "live_contributors": peak_contributors,
            "post_peak_release": post_peak,
        },
        "lifetimes": lifetime_summary,
        "churn": churn,
        "findings": findings,
        "next_actions": [
            row["recommendation"]
            for row in findings
            if row["severity"] in {"critical", "warning"}
        ][:top],
    }


def render_text(report: dict[str, Any]) -> str:
    assessment = report["assessment"]
    peak = report["peak"]
    lines = [
        f"Assessment: {assessment['severity'].upper()} - {assessment['summary']}",
        (
            f"Peak: {peak['bytes']} bytes at +{peak['offset_us']}us, "
            f"{peak['active_allocations']} live allocations"
        ),
        (
            f"Lifetime p95/p99/max: {report['lifetimes']['p95_us']}/"
            f"{report['lifetimes']['p99_us']}/"
            f"{report['lifetimes']['max_us']}us"
        ),
        (
            f"Churn: total/peak={report['churn']['total_to_peak_ratio']}, "
            f"short-lived={report['churn']['short_lived_allocation_percent']}%"
        ),
        "",
        "Findings:",
    ]
    if not report["findings"]:
        lines.append("- none")
    for row in report["findings"]:
        lines.append(
            f"- [{row['severity']}] {row['id']}: {row['summary']}"
        )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path, help="Bolt memory trace binary v2")
    parser.add_argument(
        "--format",
        choices=("json", "pretty-json", "text"),
        default="json",
        help="Output format; compact JSON is the stable model interface",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write output to a file instead of stdout",
    )
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--max-frames", type=int, default=8)
    parser.add_argument("--max-findings", type=int, default=20)
    parser.add_argument(
        "--peak-window-ms",
        type=float,
        help="Peak growth attribution window; default is 10%% of trace, capped at 1s",
    )
    parser.add_argument("--long-lived-ms", type=float, default=1000.0)
    parser.add_argument("--long-lived-ratio", type=float, default=0.50)
    parser.add_argument("--long-lived-min-bytes", type=int, default=4096)
    parser.add_argument("--short-lived-ms", type=float, default=1.0)
    parser.add_argument("--short-lived-byte-ratio", type=float, default=0.80)
    parser.add_argument("--short-lived-min-allocations", type=int, default=100)
    parser.add_argument("--churn-ratio", type=float, default=5.0)
    parser.add_argument("--peak-concentration", type=float, default=0.50)
    parser.add_argument(
        "--fail-on",
        choices=("never", "warning", "critical"),
        default="never",
        help="Optional CI exit policy; analysis and JSON output still complete",
    )
    args = parser.parse_args()
    for name in ("top", "max_frames", "max_findings"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    for name in (
        "long_lived_ratio",
        "peak_concentration",
        "short_lived_byte_ratio",
    ):
        value = getattr(args, name)
        if not 0 <= value <= 1:
            parser.error(f"--{name.replace('_', '-')} must be between 0 and 1")
    return args


def main() -> int:
    args = parse_args()
    try:
        report = analyze(
            args.trace,
            top=args.top,
            max_frames=args.max_frames,
            peak_window_ms=args.peak_window_ms,
            long_lived_ms=args.long_lived_ms,
            long_lived_ratio=args.long_lived_ratio,
            long_lived_min_bytes=args.long_lived_min_bytes,
            short_lived_ms=args.short_lived_ms,
            short_lived_byte_ratio=args.short_lived_byte_ratio,
            short_lived_min_allocations=args.short_lived_min_allocations,
            churn_ratio=args.churn_ratio,
            peak_concentration=args.peak_concentration,
            max_findings=args.max_findings,
        )
    except (OSError, ValueError) as error:
        error_report = {
            "schema_version": SCHEMA_VERSION,
            "tool": "bolt-memory-analyze",
            "error": {
                "type": type(error).__name__,
                "message": str(error),
            },
        }
        print(json.dumps(error_report, separators=(",", ":"), sort_keys=True))
        return 2

    if args.format == "text":
        output = render_text(report)
    else:
        output = json.dumps(
            report,
            indent=2 if args.format == "pretty-json" else None,
            separators=None if args.format == "pretty-json" else (",", ":"),
            sort_keys=True,
        )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n")
    else:
        print(output)

    severity = report["assessment"]["severity"]
    if args.fail_on == "critical" and severity == "critical":
        return 1
    if (
        args.fail_on == "warning"
        and SEVERITY_RANK[severity] >= SEVERITY_RANK["warning"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
