import importlib.util
import os
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

spec = importlib.util.spec_from_file_location(
    "bolt_memory_analyze", SCRIPTS_DIR / "bolt_memory_analyze.py"
)
analyzer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = analyzer
spec.loader.exec_module(analyzer)

perfetto_spec = importlib.util.spec_from_file_location(
    "bolt_memory_perfetto", SCRIPTS_DIR / "bolt_memory_perfetto.py"
)
perfetto_analyzer = importlib.util.module_from_spec(perfetto_spec)
sys.modules[perfetto_spec.name] = perfetto_analyzer
perfetto_spec.loader.exec_module(perfetto_analyzer)


HEADER = struct.Struct("<8sHHIIIQQIIQ")
RECORD_HEADER = struct.Struct("<HHI")
EVENT = struct.Struct("<QQQQQQQQIIIBBH")


def record(record_type: int, payload: bytes) -> bytes:
    return RECORD_HEADER.pack(record_type, 0, len(payload)) + payload


def event(
    sequence: int,
    time_ns: int,
    allocation_id: int,
    address: int,
    size: int,
    pool_id: int,
    stack_id: int,
    operation: int,
) -> bytes:
    return record(
        3,
        EVENT.pack(
            sequence,
            time_ns,
            allocation_id,
            0,
            address,
            0,
            size,
            0,
            pool_id,
            stack_id,
            7,
            operation,
            0,
            0,
        ),
    )


def synthetic_trace(path: Path) -> None:
    records = []
    records.append(record(8, struct.pack("<QQQII", 1, 4096, 16, 75, 0)))
    for pool_id, name in ((1, b"short-lived"), (2, b"retained")):
        records.append(record(1, struct.pack("<II", pool_id, len(name)) + name))
    stack_data = struct.pack("<Q", 0x1234)
    records.append(record(2, struct.pack("<III", 1, 2, len(stack_data)) + stack_data))

    sequence = 1
    base = 1_000_000_000
    records.append(event(sequence, base, 1, 0x1000, 8192, 2, 1, 1))
    sequence += 1
    records.append(event(sequence, base + 100_000_000, 2, 0x2000, 65536, 2, 1, 1))
    sequence += 1

    for index in range(20):
        allocation_id = 100 + index
        address = 0x3000 + index * 0x100
        start = base + 200_000_000 + index * 20_000_000
        records.append(
            event(
                sequence,
                start,
                allocation_id,
                address,
                4096,
                1,
                1,
                1,
            )
        )
        sequence += 1
        records.append(
            event(
                sequence,
                start + 5_000_000,
                allocation_id,
                address,
                4096,
                1,
                0,
                2,
            )
        )
        sequence += 1

    records.append(
        event(sequence, base + 1_700_000_000, 2, 0x2000, 65536, 2, 0, 2)
    )
    event_count = sequence
    sequence += 1
    records.append(
        event(sequence, base + 2_000_000_000, 200, 0x9000, 1024, 1, 1, 1)
    )
    event_count += 1
    records.append(
        event(sequence + 1, base + 2_001_000_000, 200, 0x9000, 1024, 1, 0, 2)
    )
    event_count += 1

    records_before_stats = len(records)
    records.append(
        record(
            5,
            struct.pack(
                "<QQQQQQQQQQ",
                event_count,
                records_before_stats,
                156672,
                23,
                0,
                0,
                0,
                0,
                1,
                8192,
            ),
        )
    )
    final_record_count = len(records) + 1
    records.append(
        record(
            6,
            struct.pack(
                "<QQQQQQQQQ",
                base + 2_100_000_000,
                1_700_000_000_000_000_000,
                final_record_count,
                event_count,
                sequence + 1,
                0,
                0,
                1,
                8192,
            ),
        )
    )

    header = HEADER.pack(
        b"BLTMEM2\0",
        2,
        0,
        HEADER.size,
        0x01020304,
        1,
        1_700_000_000_000_000_000,
        base,
        42,
        0,
        12345,
    )
    path.write_bytes(header + b"".join(records))


class MemoryAnalyzeTest(unittest.TestCase):
    def analyze(self, path: Path):
        return analyzer.analyze(
            path,
            top=5,
            max_frames=5,
            peak_window_ms=None,
            long_lived_ms=500,
            long_lived_ratio=0.5,
            long_lived_min_bytes=4096,
            short_lived_ms=10,
            short_lived_byte_ratio=0.5,
            short_lived_min_allocations=10,
            churn_ratio=2.0,
            peak_concentration=0.5,
            max_findings=20,
        )

    def test_reports_peak_lifetime_churn_and_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.bin"
            synthetic_trace(trace)
            report = self.analyze(trace)

        finding_ids = {row["id"] for row in report["findings"]}
        self.assertEqual(report["assessment"]["severity"], "critical")
        self.assertEqual(report["assessment"]["confidence"], "high")
        self.assertIn("live-at-end", finding_ids)
        self.assertIn("long-lifetime", finding_ids)
        self.assertIn("allocation-churn", finding_ids)
        self.assertIn("peak-concentration", finding_ids)
        self.assertEqual(report["metrics"]["live_allocations_at_end"], 1)
        self.assertEqual(report["peak"]["live_contributors"]["pools"][0]["pool"], "retained")
        self.assertGreater(report["lifetimes"]["suspicious_count"], 0)

    def test_truncated_trace_is_low_confidence(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.bin"
            synthetic_trace(trace)
            data = trace.read_bytes()
            trace.write_bytes(data[:-16])
            report = self.analyze(trace)

        finding_ids = {row["id"] for row in report["findings"]}
        self.assertEqual(report["assessment"]["confidence"], "low")
        self.assertIn("trace-integrity", finding_ids)
        self.assertFalse(report["trace"]["clean_shutdown"])

    def test_peak_replay_uses_size_at_peak_for_grow(self):
        events = [
            {
                "seq": 1,
                "time_us": 10,
                "allocation_id": 1,
                "op": "alloc",
                "pool": "pool",
                "addr": "0x1000",
                "size": 100,
                "stack_id": 1,
            },
            {
                "seq": 2,
                "time_us": 20,
                "allocation_id": 1,
                "op": "grow",
                "pool": "pool",
                "addr": "0x2000",
                "old_addr": "0x1000",
                "old_size": 100,
                "size": 300,
                "stack_id": 2,
            },
            {
                "seq": 3,
                "time_us": 30,
                "allocation_id": 1,
                "op": "grow",
                "pool": "pool",
                "addr": "0x3000",
                "old_addr": "0x2000",
                "old_size": 300,
                "size": 600,
                "stack_id": 3,
            },
        ]

        active, growth = analyzer.replay_peak(events, 2, 15)

        self.assertEqual(active[0]["size"], 300)
        self.assertEqual(active[0]["stack_id"], 2)
        self.assertEqual(sum(row["size"] for row in growth), 200)

    def test_perfetto_conversion_and_trace_processor_analysis(self):
        trace_processor = os.environ.get("TRACE_PROCESSOR")
        if trace_processor is None:
            trace_processor = shutil.which("trace_processor_shell")
        if trace_processor is None:
            self.skipTest("TRACE_PROCESSOR is not available")

        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.bin"
            perfetto_trace = Path(directory) / "trace.perfetto-trace"
            synthetic_trace(trace)
            conversion = perfetto_analyzer.convert(
                trace,
                perfetto_trace,
                symbolize=False,
                snapshot_events=10,
            )
            report = perfetto_analyzer.analyze_trace(
                perfetto_trace,
                Path(trace_processor),
                top=5,
                long_lived_ms=500,
                short_lived_ms=10,
            )

        finding_ids = {row["id"] for row in report["findings"]}
        self.assertEqual(conversion["events"], 45)
        self.assertGreaterEqual(conversion["snapshots"], 2)
        self.assertEqual(report["assessment"]["severity"], "critical")
        self.assertIn("live-at-end", finding_ids)
        self.assertIn("long-lifetime", finding_ids)
        self.assertEqual(
            report["metrics"]["peak_bytes"],
            report["peak"]["pool_coverage"]["bytes"],
        )


if __name__ == "__main__":
    unittest.main()
