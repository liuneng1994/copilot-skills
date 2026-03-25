#!/usr/bin/env python3
"""
Fetch Azure DevOps pipeline build details, timeline, and failed task logs.

Usage:
    python fetch_build_info.py <build_id_or_url> [--logs] [--log-lines N]

Arguments:
    build_id_or_url   Numeric build ID (e.g. 212185976) or a full ADO build URL.
    --logs            Also fetch logs for failed tasks (default: off).
    --log-lines N     Number of trailing log lines to show per failed task (default: 80).
    --org ORG_URL     ADO organization URL (default: https://msdata.visualstudio.com).
    --project NAME    ADO project name (default: A365).

Output:
    Prints a JSON document to stdout with keys:
      build     – summary fields (status, result, pipeline, branch, etc.)
      timeline  – lists of stages and jobs with state/result
      failures  – for each failed task: name, log_id, issues, and (if --logs) tail of the log

Exit codes:
    0  success
    1  usage / argument error
    2  network or API error

Examples:
    python fetch_build_info.py 212185976
    python fetch_build_info.py 'https://msdata.visualstudio.com/A365/_build/results?buildId=212185976&view=results' --logs
"""

import argparse
import json
import logging
import re
import subprocess
import sys
import urllib.request
import urllib.error

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def parse_build_url(url_or_id: str) -> str:
    """Extract the numeric build ID from a URL or plain ID string."""
    if url_or_id.isdigit():
        return url_or_id
    match = re.search(r"buildId=(\d+)", url_or_id)
    if match:
        return match.group(1)
    raise ValueError(
        f"Cannot extract buildId from: {url_or_id!r}. "
        "Pass a numeric build ID or a URL containing buildId=<number>."
    )


def get_access_token() -> str:
    """Obtain a Bearer token for the Azure DevOps REST API via `az` CLI."""
    result = subprocess.run(
        [
            "az", "account", "get-access-token",
            "--resource", "499b84ac-1321-427f-aa17-267ca6975798",
            "--query", "accessToken", "-o", "tsv",
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"az account get-access-token failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def api_get(token: str, url: str) -> dict:
    """GET a JSON response from the Azure DevOps REST API."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from {url}: {exc.read().decode()[:300]}") from exc


def fetch_build_summary(token: str, org: str, project: str, build_id: str) -> dict:
    """Return a condensed summary of the build."""
    url = f"{org}/{project}/_apis/build/builds/{build_id}?api-version=7.0"
    raw = api_get(token, url)
    return {
        "id": raw.get("id"),
        "buildNumber": raw.get("buildNumber"),
        "status": raw.get("status"),
        "result": raw.get("result"),
        "pipeline": raw.get("definition", {}).get("name"),
        "sourceBranch": raw.get("sourceBranch"),
        "reason": raw.get("reason"),
        "requestedFor": raw.get("requestedFor", {}).get("displayName"),
        "startTime": raw.get("startTime"),
        "finishTime": raw.get("finishTime"),
        "url": f"{org}/{project}/_build/results?buildId={build_id}&view=results",
    }


def fetch_timeline(token: str, org: str, project: str, build_id: str) -> dict:
    """Return stages, jobs, and failed tasks from the build timeline."""
    url = f"{org}/{project}/_apis/build/builds/{build_id}/timeline?api-version=7.0"
    raw = api_get(token, url)
    records = raw.get("records", [])

    def pick(r):
        return {
            "name": r.get("name"),
            "state": r.get("state"),
            "result": r.get("result"),
            "order": r.get("order", 0),
        }

    stages = sorted(
        [pick(r) for r in records if r.get("type") == "Stage"],
        key=lambda x: x["order"],
    )
    jobs = sorted(
        [pick(r) for r in records if r.get("type") == "Job"],
        key=lambda x: x["order"],
    )

    failed_tasks = []
    for r in records:
        if r.get("type") == "Task" and r.get("result") == "failed":
            log_info = r.get("log") or {}
            failed_tasks.append({
                "name": r.get("name"),
                "log_id": log_info.get("id"),
                "log_url": log_info.get("url"),
                "issues": [
                    {"type": i.get("type"), "message": i.get("message")}
                    for i in (r.get("issues") or [])
                ],
            })

    return {"stages": stages, "jobs": jobs, "failed_tasks": failed_tasks}


def fetch_task_log(token: str, log_url: str, tail_lines: int) -> str:
    """Fetch the raw log text and return the last *tail_lines* lines."""
    req = urllib.request.Request(log_url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return f"[Error fetching log: HTTP {exc.code}]"
    lines = text.splitlines()
    return "\n".join(lines[-tail_lines:])


def main():
    parser = argparse.ArgumentParser(description="Fetch ADO pipeline build info.")
    parser.add_argument("build", help="Build ID or full ADO build URL")
    parser.add_argument("--logs", action="store_true", help="Fetch logs for failed tasks")
    parser.add_argument("--log-lines", type=int, default=80, help="Tail lines per log (default 80)")
    parser.add_argument("--org", default="https://msdata.visualstudio.com", help="ADO org URL")
    parser.add_argument("--project", default="A365", help="ADO project name")
    args = parser.parse_args()

    try:
        build_id = parse_build_url(args.build)
    except ValueError as exc:
        logger.error(str(exc))
        sys.exit(1)

    try:
        token = get_access_token()
    except RuntimeError as exc:
        logger.error(str(exc))
        sys.exit(2)

    logger.info("Fetching build %s ...", build_id)
    build_summary = fetch_build_summary(token, args.org, args.project, build_id)
    timeline = fetch_timeline(token, args.org, args.project, build_id)

    failures = []
    if args.logs:
        for task in timeline["failed_tasks"]:
            entry = dict(task)
            if task.get("log_url"):
                logger.info("Fetching log for failed task: %s (log_id=%s)", task["name"], task["log_id"])
                entry["log_tail"] = fetch_task_log(token, task["log_url"], args.log_lines)
            failures.append(entry)
    else:
        failures = timeline["failed_tasks"]

    output = {
        "build": build_summary,
        "timeline": {
            "stages": timeline["stages"],
            "jobs": timeline["jobs"],
        },
        "failures": failures,
    }

    json.dump(output, sys.stdout, indent=2, ensure_ascii=False)
    print()


if __name__ == "__main__":
    main()
