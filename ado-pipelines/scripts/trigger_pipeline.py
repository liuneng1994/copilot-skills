#!/usr/bin/env python3
"""
Trigger an Azure DevOps pipeline run on a specified branch.

Usage:
    # List pipelines matching a keyword
    python trigger_pipeline.py list [--keyword KEYWORD]

    # Show pipeline parameters (from YAML definition)
    python trigger_pipeline.py params --pipeline <name_or_id>

    # Trigger a pipeline run
    python trigger_pipeline.py run --pipeline <name_or_id> --branch <branch> [--param key=value ...]

Arguments:
    list                      List available pipelines. Optional --keyword to filter.
    params                    Show runtime parameters for a pipeline.
    run                       Queue a pipeline run.
    --pipeline NAME_OR_ID     Pipeline name (substring match) or numeric ID.
    --branch BRANCH           Branch to run on (e.g. main, users/foo/feature).
                              Automatically prefixed with refs/heads/ if needed.
    --param KEY=VALUE         Runtime parameter overrides (repeatable).
    --org ORG_URL             ADO org URL (default: https://msdata.visualstudio.com).
    --project NAME            ADO project (default: A365).

Output:
    JSON to stdout.

Exit codes:
    0  success
    1  usage / argument error
    2  API / network error
    3  ambiguous pipeline match (multiple results)
"""

import argparse
import json
import logging
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


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
        raise RuntimeError(f"HTTP {exc.code} from {url}: {exc.read().decode()[:500]}") from exc


def api_post(token: str, url: str, body: dict) -> dict:
    """POST JSON to the Azure DevOps REST API and return the response."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from {url}: {exc.read().decode()[:500]}") from exc


def normalize_branch(branch: str) -> str:
    """Ensure branch has refs/heads/ prefix."""
    if branch.startswith("refs/"):
        return branch
    return f"refs/heads/{branch}"


def list_pipelines(token: str, org: str, project: str, keyword: str = None) -> list:
    """List pipeline definitions, optionally filtered by keyword."""
    url = f"{org}/{project}/_apis/build/definitions?api-version=7.0&queryOrder=definitionNameAscending&$top=200"
    if keyword:
        url += f"&name={urllib.parse.quote(keyword)}"
    raw = api_get(token, url)
    results = []
    for d in raw.get("value", []):
        name = d.get("name", "")
        # If keyword provided but API name filter is exact, do client-side substring match too
        if keyword and keyword.lower() not in name.lower():
            continue
        results.append({
            "id": d.get("id"),
            "name": name,
            "path": d.get("path"),
            "defaultBranch": d.get("repository", {}).get("defaultBranch"),
            "yamlFile": d.get("process", {}).get("yamlFilename"),
            "queueStatus": d.get("queueStatus"),
        })
    return results


def resolve_pipeline(token: str, org: str, project: str, name_or_id: str) -> dict:
    """Resolve a pipeline by ID or name. Raises if ambiguous."""
    if name_or_id.isdigit():
        url = f"{org}/{project}/_apis/build/definitions/{name_or_id}?api-version=7.0"
        d = api_get(token, url)
        return {
            "id": d["id"],
            "name": d["name"],
            "path": d.get("path"),
            "defaultBranch": d.get("repository", {}).get("defaultBranch"),
            "yamlFile": d.get("process", {}).get("yamlFilename"),
        }
    # Search by name
    candidates = list_pipelines(token, org, project, keyword=name_or_id)
    # Try exact match first
    exact = [c for c in candidates if c["name"].lower() == name_or_id.lower()]
    if len(exact) == 1:
        return exact[0]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) == 0:
        raise ValueError(f"No pipeline found matching '{name_or_id}'.")
    if len(candidates) > 1:
        names = [f"  {c['id']}: {c['name']} ({c['path']})" for c in candidates]
        raise ValueError(
            f"Multiple pipelines match '{name_or_id}':\n" + "\n".join(names) +
            "\nPlease use the numeric ID or a more specific name."
        )


def parse_yaml_parameters(yaml_content: str) -> list:
    """Parse runtime parameters from a pipeline YAML file (lightweight, no pyyaml needed)."""
    params = []
    lines = yaml_content.split("\n")
    in_params = False
    current = None
    collecting_values = False
    collecting_default_list = False

    for line in lines:
        stripped = line.strip()

        # Detect start of parameters block
        if re.match(r"^parameters:\s*$", stripped):
            in_params = True
            continue

        if not in_params:
            continue

        # End of parameters block: a top-level key that is not indented
        if not line.startswith(" ") and not line.startswith("\t") and stripped and not stripped.startswith("#"):
            break

        # New parameter entry
        m = re.match(r"\s*- name:\s*(\S+)", stripped)
        if m:
            if current:
                params.append(current)
            current = {"name": m.group(1), "type": "string", "default": None, "values": [], "displayName": None}
            collecting_values = False
            collecting_default_list = False
            continue

        if current is None:
            continue

        # displayName
        m = re.match(r"displayName:\s*(.*)", stripped)
        if m:
            current["displayName"] = m.group(1).strip().strip("'\"")
            continue

        # type
        m = re.match(r"type:\s*(\S+)", stripped)
        if m:
            current["type"] = m.group(1)
            continue

        # default (scalar)
        m = re.match(r"default:\s*(.+)", stripped)
        if m:
            val = m.group(1).strip()
            if val.startswith("[") and val.endswith("]"):
                # Inline list like [0, 1, 2, ...]
                current["default"] = val
                collecting_default_list = False
            elif val in ("true", "false"):
                current["default"] = val
            else:
                current["default"] = val.strip("'\"")
            continue

        # default (block, just "default:")
        m = re.match(r"default:\s*$", stripped)
        if m:
            collecting_default_list = True
            current["default"] = "[]"
            continue

        # values list items
        if stripped == "values:":
            collecting_values = True
            continue

        if collecting_values:
            m = re.match(r"-\s*['\"]?([^'\"]+)['\"]?", stripped)
            if m:
                current["values"].append(m.group(1).strip())
                continue
            else:
                collecting_values = False

    if current:
        params.append(current)

    return params


def get_pipeline_parameters(token: str, org: str, project: str, pipeline: dict) -> list:
    """Fetch the pipeline YAML and extract runtime parameters."""
    yaml_file = pipeline.get("yamlFile")
    if not yaml_file:
        return []

    # Get the YAML content from the repository
    repo_id = None
    # Fetch full definition to get repo ID
    url = f"{org}/{project}/_apis/build/definitions/{pipeline['id']}?api-version=7.0"
    defn = api_get(token, url)
    repo_id = defn.get("repository", {}).get("id")
    default_branch = defn.get("repository", {}).get("defaultBranch", "refs/heads/main")

    if not repo_id:
        return []

    # Fetch YAML file content via Git Items API
    yaml_path = yaml_file.lstrip("/")
    items_url = (
        f"{org}/{project}/_apis/git/repositories/{repo_id}/items"
        f"?path={urllib.parse.quote(yaml_path)}"
        f"&versionDescriptor.version={default_branch.replace('refs/heads/', '')}"
        f"&includeContent=true&api-version=7.0"
    )
    try:
        raw = api_get(token, items_url)
        content = raw.get("content", "")
    except RuntimeError:
        # Fallback: try reading from local filesystem
        import os
        local_path = os.path.join("/root/gluten", yaml_path)
        if os.path.exists(local_path):
            with open(local_path) as f:
                content = f.read()
        else:
            logger.warning("Could not fetch pipeline YAML from API or local filesystem.")
            return []

    return parse_yaml_parameters(content)


def trigger_run(token: str, org: str, project: str, pipeline_id: int,
                branch: str, parameters: dict = None) -> dict:
    """Queue a pipeline run and return the run info."""
    url = f"{org}/{project}/_apis/build/builds?api-version=7.0"
    body = {
        "definition": {"id": pipeline_id},
        "sourceBranch": normalize_branch(branch),
    }
    if parameters:
        # ADO expects templateParameters for YAML pipeline parameters
        body["templateParameters"] = parameters

    raw = api_post(token, url, body)
    return {
        "id": raw.get("id"),
        "buildNumber": raw.get("buildNumber"),
        "status": raw.get("status"),
        "pipeline": raw.get("definition", {}).get("name"),
        "sourceBranch": raw.get("sourceBranch"),
        "requestedFor": raw.get("requestedFor", {}).get("displayName"),
        "url": f"{org}/{project}/_build/results?buildId={raw.get('id')}&view=results",
    }


def main():
    parser = argparse.ArgumentParser(description="Trigger ADO pipeline builds.")
    sub = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = sub.add_parser("list", help="List pipelines")
    p_list.add_argument("--keyword", "-k", help="Filter by keyword (substring match)")

    # params
    p_params = sub.add_parser("params", help="Show pipeline runtime parameters")
    p_params.add_argument("--pipeline", "-p", required=True, help="Pipeline name or ID")

    # run
    p_run = sub.add_parser("run", help="Trigger a pipeline run")
    p_run.add_argument("--pipeline", "-p", required=True, help="Pipeline name or ID")
    p_run.add_argument("--branch", "-b", required=True, help="Branch to run on")
    p_run.add_argument("--param", action="append", default=[],
                       help="Parameter override as key=value (repeatable)")

    # Common args
    for p in [p_list, p_params, p_run]:
        p.add_argument("--org", default="https://msdata.visualstudio.com")
        p.add_argument("--project", default="A365")

    args = parser.parse_args()

    try:
        token = get_access_token()
    except RuntimeError as exc:
        logger.error(str(exc))
        sys.exit(2)

    if args.command == "list":
        pipelines = list_pipelines(token, args.org, args.project, args.keyword)
        json.dump(pipelines, sys.stdout, indent=2, ensure_ascii=False)
        print()

    elif args.command == "params":
        try:
            pipeline = resolve_pipeline(token, args.org, args.project, args.pipeline)
        except ValueError as exc:
            logger.error(str(exc))
            sys.exit(3)
        logger.info("Resolved pipeline: %s (id=%s)", pipeline["name"], pipeline["id"])

        params = get_pipeline_parameters(token, args.org, args.project, pipeline)
        output = {
            "pipeline": pipeline,
            "parameters": params,
        }
        json.dump(output, sys.stdout, indent=2, ensure_ascii=False)
        print()

    elif args.command == "run":
        try:
            pipeline = resolve_pipeline(token, args.org, args.project, args.pipeline)
        except ValueError as exc:
            logger.error(str(exc))
            sys.exit(3)

        # Parse parameter overrides
        param_overrides = {}
        for kv in args.param:
            if "=" not in kv:
                logger.error("Invalid --param format: %r (expected key=value)", kv)
                sys.exit(1)
            k, v = kv.split("=", 1)
            param_overrides[k] = v

        logger.info("Triggering pipeline: %s (id=%s) on branch: %s", pipeline["name"], pipeline["id"], args.branch)
        if param_overrides:
            logger.info("Parameter overrides: %s", param_overrides)

        result = trigger_run(token, args.org, args.project, pipeline["id"], args.branch, param_overrides)
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        print()


if __name__ == "__main__":
    main()
