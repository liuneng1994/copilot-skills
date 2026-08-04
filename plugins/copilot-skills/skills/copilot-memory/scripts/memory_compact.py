#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys

from memory_lib import connect, ensure_schema, utc_now


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive stale Copilot memories.")
    parser.add_argument("--db", help="SQLite database path")
    parser.add_argument("--namespace")
    parser.add_argument("--repo")
    parser.add_argument("--archive-thread-days", dest="archive_thread_days", type=int, default=7)
    parser.add_argument("--archive-general-days", dest="archive_general_days", type=int, default=60)
    parser.add_argument("--archive-superseded-days", dest="archive_superseded_days", type=int, default=14)
    parser.add_argument("--low-value-threshold", dest="low_value_threshold", type=float, default=0.35)
    parser.add_argument("--unused-access-threshold", dest="unused_access_threshold", type=int, default=0)
    return parser.parse_args()


def scoped_clause(args: argparse.Namespace) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if args.namespace:
        clauses.append("namespace = ?")
        params.append(args.namespace)
    if args.repo:
        clauses.append("repo = ?")
        params.append(args.repo)
    return (" AND " + " AND ".join(clauses), params) if clauses else ("", [])


def main() -> int:
    args = parse_args()
    now = utc_now()
    clause, params = scoped_clause(args)

    with connect(args.db) as conn:
        ensure_schema(conn)

        expired = conn.execute(
            f"""
            UPDATE memories
            SET status = 'archived', updated_at = ?
            WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at < ? {clause}
            """,
            [now, now, *params],
        ).rowcount

        stale_thread = conn.execute(
            f"""
            UPDATE memories
            SET status = 'archived', updated_at = ?
            WHERE status = 'active'
              AND scope = 'thread'
              AND julianday('now') - julianday(updated_at) > ?
              {clause}
            """,
            [now, args.archive_thread_days, *params],
        ).rowcount

        stale_low_value = conn.execute(
            f"""
            UPDATE memories
            SET status = 'archived', updated_at = ?
            WHERE status = 'active'
              AND scope != 'thread'
              AND value_score < ?
              AND access_count <= ?
              AND julianday('now') - julianday(updated_at) > ?
              {clause}
            """,
            [now, args.low_value_threshold, args.unused_access_threshold, args.archive_general_days, *params],
        ).rowcount

        superseded = conn.execute(
            f"""
            UPDATE memories
            SET status = 'archived', updated_at = ?
            WHERE status = 'superseded'
              AND julianday('now') - julianday(updated_at) > ?
              {clause}
            """,
            [now, args.archive_superseded_days, *params],
        ).rowcount

        remaining = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM memories
            WHERE status = 'active' {clause}
            """,
            params,
        ).fetchone()["count"]

        conn.commit()

    print(
        json.dumps(
            {
                "ok": True,
                "archived_expired": expired,
                "archived_stale_thread": stale_thread,
                "archived_low_value": stale_low_value,
                "archived_old_superseded": superseded,
                "active_remaining": remaining,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
