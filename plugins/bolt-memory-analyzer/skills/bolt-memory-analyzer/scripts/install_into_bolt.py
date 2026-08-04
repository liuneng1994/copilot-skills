#!/usr/bin/env python3
"""Install the bundled Bolt memory analyzer into a Bolt checkout."""

from __future__ import annotations

import argparse
import filecmp
import shutil
import subprocess
import sys
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SKILL_DIR / "assets" / "bolt"
PATCH_FILE = SKILL_DIR / "references" / "bolt-integration.patch"


def bundled_files() -> list[Path]:
    return sorted(
        path
        for path in SOURCE_ROOT.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    )


def validate_checkout(repo: Path) -> None:
    required = [
        repo / "bolt/common/memory/MemoryPool.cpp",
        repo / "bolt/common/memory/CMakeLists.txt",
        repo / "scripts",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise ValueError(
            "not a compatible Bolt checkout; missing: " + ", ".join(missing)
        )


def patch_state(repo: Path) -> str:
    check = subprocess.run(
        ["git", "apply", "--check", str(PATCH_FILE)],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if check.returncode == 0:
        return "applicable"
    reverse = subprocess.run(
        ["git", "apply", "--reverse", "--check", str(PATCH_FILE)],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if reverse.returncode == 0:
        return "applied"
    details = check.stderr.strip() or reverse.stderr.strip()
    raise ValueError(
        "integration patch does not match this checkout; adapt "
        f"{PATCH_FILE} manually. Details: {details}"
    )


def planned_copies(repo: Path, force: bool) -> list[tuple[Path, Path]]:
    copies = []
    conflicts = []
    for source in bundled_files():
        relative = source.relative_to(SOURCE_ROOT)
        destination = repo / relative
        if destination.exists() and filecmp.cmp(source, destination, shallow=False):
            continue
        if destination.exists() and not force:
            conflicts.append(str(relative))
            continue
        copies.append((source, destination))
    if conflicts:
        raise ValueError(
            "refusing to overwrite different files without --force: "
            + ", ".join(conflicts)
        )
    return copies


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo = args.repo.resolve()
    try:
        validate_checkout(repo)
        state = patch_state(repo)
        copies = planned_copies(repo, args.force)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print(f"repo: {repo}")
    print(f"integration patch: {state}")
    for source, destination in copies:
        print(f"copy: {source.relative_to(SOURCE_ROOT)} -> {destination}")
    if args.dry_run:
        return 0

    if state == "applicable":
        subprocess.run(
            ["git", "apply", str(PATCH_FILE)],
            cwd=repo,
            check=True,
        )
    for source, destination in copies:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    for relative in (
        Path("scripts/bolt_memory_analyze.py"),
        Path("scripts/bolt_memory_trace_viewer.py"),
    ):
        path = repo / relative
        path.chmod(path.stat().st_mode | 0o111)

    print("installed Bolt memory analyzer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
