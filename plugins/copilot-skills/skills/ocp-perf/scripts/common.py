"""Shared helpers for the ocp-perf skill (Spark Fabric OneClick Performance).

Standalone: depends only on the Python stdlib and the `az` CLI being logged in.
Provides ADO REST access, build/parameter lookup, run triggering, build
discovery by tag prefix, and perf-results artifact download.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("ocp-perf")

# Spark Fabric OneClick Performance pipeline.
ORG = os.environ.get("OCP_ORG", "https://msdata.visualstudio.com")
PROJECT = os.environ.get("OCP_PROJECT", "A365")
PIPELINE_ID = int(os.environ.get("OCP_PIPELINE_ID", "44400"))
ARTIFACT_NAME = "perf-results"

# Raw Spark event-log directories. They are large (hundreds of MB) and are not
# consumed by analyze_results.py, so download_results.py prunes them by default.
EVENTLOG_DIRS = ("spark-events", "event-log-dir")

# Azure DevOps resource id used to scope an AAD access token.
ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"

API = "api-version=7.0"


def get_access_token():
    """Obtain a Bearer token for the Azure DevOps REST API via `az` CLI."""
    result = subprocess.run(
        [
            "az", "account", "get-access-token",
            "--resource", ADO_RESOURCE,
            "--query", "accessToken", "-o", "tsv",
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "az account get-access-token failed "
            "(rc={}): {}".format(result.returncode, result.stderr.strip())
        )
    return result.stdout.strip()


def api_get(token, url):
    """GET a JSON response from the Azure DevOps REST API."""
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            "HTTP {} from {}: {}".format(exc.code, url, exc.read().decode()[:500])
        ) from exc


def api_post(token, url, body):
    """POST JSON to the Azure DevOps REST API and return the response."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            "HTTP {} from {}: {}".format(exc.code, url, exc.read().decode()[:500])
        ) from exc


def normalize_branch(branch):
    """Ensure a branch ref has the refs/heads/ prefix."""
    if branch.startswith("refs/"):
        return branch
    return "refs/heads/" + branch


def get_build(token, build_id):
    """Return the full build object for a build id."""
    url = "{}/{}/_apis/build/builds/{}?{}".format(ORG, PROJECT, build_id, API)
    return api_get(token, url)


def get_template_parameters(token, build_id):
    """Return the templateParameters dict a build was queued with."""
    build = get_build(token, build_id)
    params = build.get("templateParameters") or {}
    return dict(params)


def build_brief(build):
    """Reduce a build object to the fields we report on."""
    return {
        "id": build.get("id"),
        "buildNumber": build.get("buildNumber"),
        "status": build.get("status"),
        "result": build.get("result"),
        "sourceBranch": build.get("sourceBranch"),
        "tagName": (build.get("templateParameters") or {}).get("tagName"),
        "perfSuiteType": (build.get("templateParameters") or {}).get("perfSuiteType"),
        "scaleFactor": (build.get("templateParameters") or {}).get("scaleFactor"),
        "additionalConfig": (build.get("templateParameters") or {}).get("additionalConfig"),
        "url": "{}/{}/_build/results?buildId={}&view=results".format(
            ORG, PROJECT, build.get("id")
        ),
    }


def list_recent_builds(token, top=200, branch=None):
    """List recent builds for the OCP pipeline (newest first)."""
    url = "{}/{}/_apis/build/builds?definitions={}&{}&$top={}&queryOrder=finishTimeDescending".format(
        ORG, PROJECT, PIPELINE_ID, API, top
    )
    if branch:
        url += "&branchName=" + urllib.parse.quote(normalize_branch(branch))
    raw = api_get(token, url)
    return raw.get("value", [])


def find_builds_by_tag_prefix(token, tag_prefix, top=300, branch=None):
    """Return briefs of OCP builds whose tagName starts with tag_prefix.

    Matches the templateParameters.tagName, newest first.
    """
    builds = list_recent_builds(token, top=top, branch=branch)
    matched = []
    for b in builds:
        tag = (b.get("templateParameters") or {}).get("tagName") or ""
        if tag.startswith(tag_prefix):
            matched.append(build_brief(b))
    return matched


def trigger_run(token, branch, parameters):
    """Queue an OCP pipeline run with the given templateParameters."""
    url = "{}/{}/_apis/build/builds?{}".format(ORG, PROJECT, API)
    body = {
        "definition": {"id": PIPELINE_ID},
        "sourceBranch": normalize_branch(branch),
    }
    if parameters:
        body["templateParameters"] = parameters
    raw = api_post(token, url, body)
    return {
        "id": raw.get("id"),
        "buildNumber": raw.get("buildNumber"),
        "status": raw.get("status"),
        "sourceBranch": raw.get("sourceBranch"),
        "tagName": (raw.get("templateParameters") or parameters or {}).get("tagName"),
        "url": "{}/{}/_build/results?buildId={}&view=results".format(
            ORG, PROJECT, raw.get("id")
        ),
    }


def download_artifact(build_id, dest_dir, artifact_name=ARTIFACT_NAME):
    """Download a build artifact via the az CLI into dest_dir.

    Returns the dest_dir on success, raises RuntimeError on failure.
    """
    os.makedirs(dest_dir, exist_ok=True)
    result = subprocess.run(
        [
            "az", "pipelines", "runs", "artifact", "download",
            "--run-id", str(build_id),
            "--artifact-name", artifact_name,
            "--path", dest_dir,
            "--org", ORG,
            "--project", PROJECT,
        ],
        capture_output=True, text=True, timeout=900,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "artifact download failed for build {} (rc={}): {}".format(
                build_id, result.returncode, result.stderr.strip()[:500]
            )
        )
    return dest_dir


def prune_eventlog(dest_dir, names=EVENTLOG_DIRS):
    """Delete raw event-log directories anywhere under dest_dir.

    Returns (removed_count, freed_bytes). Matches directory basenames exactly
    against `names` (default EVENTLOG_DIRS). Safe to call when none exist.
    """
    removed, freed = 0, 0
    for root, dirs, _files in os.walk(dest_dir, topdown=True):
        for d in list(dirs):
            if d in names:
                victim = os.path.join(root, d)
                freed += _dir_size(victim)
                shutil.rmtree(victim, ignore_errors=True)
                dirs.remove(d)
                removed += 1
    return removed, freed


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def list_artifacts(token, build_id):
    """List artifact names available for a build."""
    url = "{}/{}/_apis/build/builds/{}/artifacts?{}".format(
        ORG, PROJECT, build_id, API
    )
    raw = api_get(token, url)
    return [a.get("name") for a in raw.get("value", [])]


def eprint(*args):
    print(*args, file=sys.stderr)
