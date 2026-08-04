#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid

from memory_lib import (
    connect,
    ensure_schema,
    fingerprint,
    float_in_range,
    looks_secret,
    parse_json_object,
    parse_tags,
    union_tags,
    utc_now,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate and write a Copilot memory entry.")
    parser.add_argument("--db", help="SQLite database path")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--scope", required=True, choices=["thread", "repo", "user", "reflection"])
    parser.add_argument(
        "--kind",
        required=True,
        choices=["fact", "preference", "decision", "failure", "workflow", "summary", "constraint"],
    )
    parser.add_argument("--subject")
    parser.add_argument("--summary", required=True)
    parser.add_argument("--content")
    parser.add_argument("--tags")
    parser.add_argument("--confidence", type=float, default=0.7)
    parser.add_argument("--value-score", dest="value_score", type=float, default=0.7)
    parser.add_argument("--repo")
    parser.add_argument("--thread-id", dest="thread_id")
    parser.add_argument("--user-id", dest="user_id")
    parser.add_argument("--source", default="copilot")
    parser.add_argument("--source-ref", dest="source_ref")
    parser.add_argument("--metadata")
    parser.add_argument("--expires-at", dest="expires_at")
    parser.add_argument("--supersedes-id", dest="supersedes_id")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    content = args.content or args.summary

    try:
        float_in_range(args.confidence, "confidence")
        float_in_range(args.value_score, "value_score")
        tags = parse_tags(args.tags)
        metadata = parse_json_object(args.metadata)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2

    secret_name = looks_secret(args.summary, content)
    if secret_name:
        print(json.dumps({"ok": False, "error": f"refused to store suspected secret pattern: {secret_name}"}))
        return 3

    fp = fingerprint(args.namespace, args.scope, args.kind, args.subject, args.summary)
    now = utc_now()

    with connect(args.db) as conn:
        ensure_schema(conn)
        record_id = str(uuid.uuid4())
        insert_result = conn.execute(
            """
            INSERT OR IGNORE INTO memories (
                id, scope, namespace, repo, thread_id, user_id, kind, subject,
                summary, content, tags, confidence, value_score, source, source_ref,
                fingerprint, status, created_at, updated_at, expires_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (
                record_id,
                args.scope,
                args.namespace,
                args.repo,
                args.thread_id,
                args.user_id,
                args.kind,
                args.subject,
                args.summary,
                content,
                json.dumps(tags),
                args.confidence,
                args.value_score,
                args.source,
                args.source_ref,
                fp,
                now,
                now,
                args.expires_at,
                json.dumps(metadata, sort_keys=True),
            ),
        )

        if insert_result.rowcount == 1:
            action = "inserted"
        else:
            existing = conn.execute(
                """
                SELECT *
                FROM memories
                WHERE namespace = ? AND fingerprint = ? AND status = 'active'
                LIMIT 1
                """,
                (args.namespace, fp),
            ).fetchone()

            if existing is None:
                raise sqlite3.IntegrityError("memory insert was ignored but no active record could be found")

            merged_tags = union_tags(existing["tags"], tags)
            merged_metadata = json.loads(existing["metadata"])
            merged_metadata.update(metadata)
            merged_content = content if len(content) >= len(existing["content"]) else existing["content"]
            conn.execute(
                """
                UPDATE memories
                SET summary = ?,
                    content = ?,
                    tags = ?,
                    confidence = MAX(confidence, ?),
                    value_score = MAX(value_score, ?),
                    repo = COALESCE(?, repo),
                    thread_id = COALESCE(?, thread_id),
                    user_id = COALESCE(?, user_id),
                    source = COALESCE(?, source),
                    source_ref = COALESCE(?, source_ref),
                    updated_at = ?,
                    expires_at = COALESCE(?, expires_at),
                    metadata = ?
                WHERE id = ?
                """,
                (
                    args.summary,
                    merged_content,
                    json.dumps(merged_tags),
                    args.confidence,
                    args.value_score,
                    args.repo,
                    args.thread_id,
                    args.user_id,
                    args.source,
                    args.source_ref,
                    now,
                    args.expires_at,
                    json.dumps(merged_metadata, sort_keys=True),
                    existing["id"],
                ),
            )
            record_id = existing["id"]
            action = "updated"

        if args.supersedes_id:
            conn.execute(
                """
                UPDATE memories
                SET status = 'superseded', superseded_by = ?, updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (record_id, now, args.supersedes_id),
            )

        conn.commit()

    print(
        json.dumps(
            {
                "ok": True,
                "action": action,
                "id": record_id,
                "namespace": args.namespace,
                "scope": args.scope,
                "kind": args.kind,
                "fingerprint": fp,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
