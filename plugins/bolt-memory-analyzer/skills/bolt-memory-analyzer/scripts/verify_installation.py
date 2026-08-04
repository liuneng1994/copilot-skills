#!/usr/bin/env python3
"""Verify a Bolt memory analyzer installation without building Bolt."""

from __future__ import annotations

import argparse
import filecmp
import py_compile
import sys
import tempfile
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SKILL_DIR / "assets" / "bolt"
EXPECTED_FILES = [
    "bolt/common/memory/MemoryTraceRecorder.cpp",
    "bolt/common/memory/MemoryTraceRecorder.h",
    "scripts/bolt_memory_analyze.py",
    "scripts/bolt_memory_perfetto.py",
    "scripts/bolt_memory_trace_core.py",
    "scripts/bolt_memory_trace_format.md",
    "scripts/perfetto/bolt_memory.sql",
    "scripts/tests/test_bolt_memory_analyze.py",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()

    missing = [relative for relative in EXPECTED_FILES if not (repo / relative).is_file()]
    if missing:
        print("missing files: " + ", ".join(missing), file=sys.stderr)
        return 1

    different = [
        relative
        for relative in EXPECTED_FILES
        if not filecmp.cmp(
            SOURCE_ROOT / relative,
            repo / relative,
            shallow=False,
        )
    ]
    if different:
        print(
            "installed files differ from the bundled version: "
            + ", ".join(different),
            file=sys.stderr,
        )
        return 1

    non_executable = [
        relative
        for relative in (
            "scripts/bolt_memory_analyze.py",
            "scripts/bolt_memory_perfetto.py",
        )
        if not ((repo / relative).stat().st_mode & 0o111)
    ]
    if non_executable:
        print(
            "warning: entry points are not executable; invoke them with python3: "
            + ", ".join(non_executable),
            file=sys.stderr,
        )

    memory_pool = (repo / "bolt/common/memory/MemoryPool.cpp").read_text()
    cmake = (repo / "bolt/common/memory/CMakeLists.txt").read_text()
    checks = {
        "recorder include": "MemoryTraceRecorder.h" in memory_pool,
        "allocation hook": "TRACE_RECORD_ALLOC" in memory_pool,
        "free hook": "TRACE_RECORD_FREE" in memory_pool,
        "grow hook": "TRACE_RECORD_GROW" in memory_pool,
        "cmake source": "MemoryTraceRecorder.cpp" in cmake,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        print("failed checks: " + ", ".join(failed), file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="bolt-memory-verify-") as directory:
        for script in (
            repo / "scripts/bolt_memory_analyze.py",
            repo / "scripts/bolt_memory_perfetto.py",
            repo / "scripts/bolt_memory_trace_core.py",
        ):
            py_compile.compile(
                str(script),
                cfile=str(Path(directory) / f"{script.stem}.pyc"),
                doraise=True,
            )
    print("Bolt memory analyzer installation verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
