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

"""Install the Bolt area-flamegraph plugin into a Perfetto checkout."""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path


PLUGIN_ID = "dev.bolt.Memory"
SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_SOURCE = SCRIPT_DIR / "perfetto" / "ui_plugin" / PLUGIN_ID


def validate_checkout(repo: Path) -> tuple[Path, Path]:
    plugin_root = repo / "ui" / "src" / "plugins"
    defaults = repo / "ui" / "src" / "core" / "embedder" / "default_plugins.ts"
    if not plugin_root.is_dir() or not defaults.is_file():
        raise ValueError(
            f"{repo} is not a compatible Perfetto UI checkout"
        )
    return plugin_root, defaults


def planned_files(source: Path, destination: Path) -> list[tuple[Path, Path]]:
    copies = []
    for file in sorted(path for path in source.rglob("*") if path.is_file()):
        target = destination / file.relative_to(source)
        if target.exists() and filecmp.cmp(file, target, shallow=False):
            continue
        copies.append((file, target))
    return copies


def update_default_plugins(content: str) -> str:
    marker = "export const defaultPlugins = [\n"
    entry = f"  '{PLUGIN_ID}',\n"
    if entry in content:
        return content
    if marker not in content:
        raise ValueError("default_plugins.ts has an unsupported structure")
    return content.replace(marker, marker + entry, 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--perfetto", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    repo = args.perfetto.resolve()
    try:
        plugin_root, defaults = validate_checkout(repo)
        destination = plugin_root / PLUGIN_ID
        copies = planned_files(PLUGIN_SOURCE, destination)
        conflicts = [
            str(target)
            for source, target in copies
            if target.exists()
            and not filecmp.cmp(source, target, shallow=False)
        ]
        if conflicts and not args.force:
            raise ValueError(
                "refusing to overwrite modified plugin files without --force: "
                + ", ".join(conflicts)
            )
        original_defaults = defaults.read_text()
        updated_defaults = update_default_plugins(original_defaults)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print(f"Perfetto checkout: {repo}")
    for source, target in copies:
        print(f"copy: {source.name} -> {target}")
    if updated_defaults != original_defaults:
        print(f"register: {PLUGIN_ID} in {defaults}")
    if args.dry_run:
        return 0

    destination.mkdir(parents=True, exist_ok=True)
    for source, target in copies:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    if updated_defaults != original_defaults:
        defaults.write_text(updated_defaults)
    print("installed Bolt Perfetto UI plugin")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
