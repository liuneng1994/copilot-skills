#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = "~/.copilot/memory/copilot_memory.db"
SCHEMA_PATH = SCRIPT_DIR / "schema.sql"
SECRET_PATTERNS = {
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "password_assignment": re.compile(
        r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{10,}"
    ),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def expand_db_path(raw: str | None) -> Path:
    value = raw or os.environ.get("COPILOT_MEMORY_DB") or DEFAULT_DB_PATH
    return Path(os.path.expanduser(value)).resolve()


def connect(db_path: str | None) -> sqlite3.Connection:
    path = expand_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def safe_json_loads(raw: str, default: Any) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def load_json_file(path: Path, default: Any) -> Any:
    try:
        return safe_json_loads(path.read_text(encoding="utf-8"), default)
    except OSError:
        return default


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def fingerprint(namespace: str, scope: str, kind: str, subject: str | None, summary: str) -> str:
    payload = "\n".join(
        [
            normalize_text(namespace).lower(),
            normalize_text(scope).lower(),
            normalize_text(kind).lower(),
            normalize_text(subject).lower(),
            normalize_text(summary).lower(),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    if raw.lstrip().startswith("["):
        value = json.loads(raw)
        if not isinstance(value, list):
            raise ValueError("tags JSON must be a list")
        return [normalize_text(str(item)) for item in value if normalize_text(str(item))]
    return [normalize_text(item) for item in raw.split(",") if normalize_text(item)]


def parse_json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("metadata JSON must be an object")
    return value


def union_tags(old_tags: str, new_tags: list[str]) -> list[str]:
    merged = parse_tags(old_tags) + new_tags
    ordered: list[str] = []
    seen: set[str] = set()
    for tag in merged:
        tag_key = tag.lower()
        if tag and tag_key not in seen:
            ordered.append(tag)
            seen.add(tag_key)
    return ordered


def looks_secret(*values: str | None) -> str | None:
    text = "\n".join(value for value in values if value)
    for name, pattern in SECRET_PATTERNS.items():
        if pattern.search(text):
            return name
    return None


def float_in_range(value: float, field_name: str) -> float:
    if value < 0 or value > 1:
        raise ValueError(f"{field_name} must be between 0 and 1")
    return value


def build_fts_query(raw: str | None) -> str | None:
    if not raw:
        return None
    tokens = re.findall(r"[A-Za-z0-9_/-]+", raw.lower())
    if not tokens:
        return None
    filtered = [token for token in tokens if len(token) > 1]
    deduped = list(dict.fromkeys(filtered))
    if not deduped:
        return None
    return " OR ".join(f'"{token}"' for token in deduped[:12])


def parse_yaml_scalar(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return ""
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip("'\"") for item in inner.split(",") if item.strip()]
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value.strip("'\"")


def load_policy(path: Path) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip(" "))
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()

        container = stack[-1][1]
        key, sep, remainder = stripped.partition(":")
        if not sep:
            continue

        key = key.strip()
        value = remainder.strip()
        if not value:
            child: dict[str, Any] = {}
            container[key] = child
            stack.append((indent, child))
            continue

        container[key] = parse_yaml_scalar(value)

    return root


def policy_value(policy: dict[str, Any], *keys: str, default: Any = None) -> Any:
    current: Any = policy
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["tags"] = safe_json_loads(item["tags"], [])
    item["metadata"] = safe_json_loads(item["metadata"], {})
    return item


def scope_bias(scope: str) -> float:
    return {
        "thread": 1.0,
        "repo": 0.9,
        "user": 0.75,
        "reflection": 0.85,
    }.get(scope, 0.7)


def recency_score(updated_at: str) -> float:
    try:
        updated = datetime.fromisoformat(updated_at)
    except ValueError:
        return 0.0
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    age_days = max((datetime.now(timezone.utc) - updated).total_seconds() / 86400.0, 0.0)
    return max(0.0, 1.0 - min(age_days, 60.0) / 60.0)
