#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from memory_lib import (
    build_fts_query,
    connect,
    ensure_schema,
    recency_score,
    row_to_dict,
    scope_bias,
    utc_now,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read and rank Copilot memories.")
    parser.add_argument("--db", help="SQLite database path")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--repo")
    parser.add_argument("--thread-id", dest="thread_id")
    parser.add_argument("--user-id", dest="user_id")
    parser.add_argument("--scope", action="append", choices=["thread", "repo", "user", "reflection"])
    parser.add_argument(
        "--kind",
        action="append",
        choices=["fact", "preference", "decision", "failure", "workflow", "summary", "constraint"],
    )
    parser.add_argument("--query")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--max-content-chars", dest="max_content_chars", type=int, default=320)
    return parser.parse_args()


def score_record(item: dict[str, Any], lexical_score: float) -> float:
    return (
        lexical_score * 0.45
        + item["confidence"] * 0.20
        + item["value_score"] * 0.20
        + recency_score(item["updated_at"]) * 0.10
        + scope_bias(item["scope"]) * 0.05
    )


def make_prompt_block(items: list[dict[str, Any]]) -> str:
    if not items:
        return "No relevant memory found."
    lines = ["Relevant memory:"]
    for item in items:
        lines.append(
            f"- [{item['scope']}|{item['kind']}|conf={item['confidence']:.2f}|updated={item['updated_at']}] {item['summary']}"
        )
        if item.get("subject"):
            lines.append(f"  subject: {item['subject']}")
        lines.append(f"  detail: {item['content']}")
        if item["tags"]:
            lines.append(f"  tags: {', '.join(item['tags'])}")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    fts_query = build_fts_query(args.query)
    items: list[dict[str, Any]] = []

    with connect(args.db) as conn:
        ensure_schema(conn)
        filters = ["m.namespace = ?", "m.status = 'active'"]
        params: list[Any] = [args.namespace]

        if args.repo:
            filters.append("(m.repo = ? OR m.repo IS NULL)")
            params.append(args.repo)
        if args.thread_id:
            filters.append("(m.thread_id = ? OR m.thread_id IS NULL)")
            params.append(args.thread_id)
        if args.user_id:
            filters.append("(m.user_id = ? OR m.user_id IS NULL)")
            params.append(args.user_id)
        if args.scope:
            filters.append(f"m.scope IN ({','.join('?' for _ in args.scope)})")
            params.extend(args.scope)
        if args.kind:
            filters.append(f"m.kind IN ({','.join('?' for _ in args.kind)})")
            params.extend(args.kind)

        if fts_query:
            sql = f"""
                SELECT m.*, bm25(memory_fts) AS bm25_score
                FROM memory_fts
                JOIN memories m ON m.rowid = memory_fts.rowid
                WHERE memory_fts MATCH ? AND {' AND '.join(filters)}
                LIMIT ?
            """
            rows = conn.execute(sql, [fts_query, *params, max(args.limit * 4, 16)]).fetchall()
            for row in rows:
                item = row_to_dict(row)
                bm25_score = float(row["bm25_score"])
                lexical_score = 1.0 / (1.0 + max(bm25_score, 0.0))
                item["score"] = score_record(item, lexical_score)
                items.append(item)

        if not items:
            sql = f"""
                SELECT m.*, 0.0 AS bm25_score
                FROM memories m
                WHERE {' AND '.join(filters)}
                ORDER BY m.updated_at DESC
                LIMIT ?
            """
            rows = conn.execute(sql, [*params, max(args.limit * 4, 16)]).fetchall()
            for row in rows:
                item = row_to_dict(row)
                item["score"] = score_record(item, 0.25)
                items.append(item)

        items.sort(key=lambda item: item["score"], reverse=True)
        top_items = items[: args.limit]

        if top_items:
            now = utc_now()
            conn.executemany(
                """
                UPDATE memories
                SET access_count = access_count + 1,
                    last_accessed_at = ?
                WHERE id = ?
                """,
                [(now, item["id"]) for item in top_items],
            )
            conn.commit()

    for item in top_items:
        if len(item["content"]) > args.max_content_chars:
            item["content"] = item["content"][: args.max_content_chars - 3] + "..."

    payload = {
        "ok": True,
        "query": args.query,
        "namespace": args.namespace,
        "count": len(top_items),
        "memories": top_items,
        "prompt_block": make_prompt_block(top_items),
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
