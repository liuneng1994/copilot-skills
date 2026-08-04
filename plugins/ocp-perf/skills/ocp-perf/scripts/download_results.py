"""Download perf-results artifacts for OCP runs.

Select runs either by explicit build ids or by tag prefix (auto-discovers the
matching group on the OCP pipeline). Each build's perf-results artifact is
downloaded to <out>/<buildId>/.

Examples
--------
By explicit ids:

    python download_results.py --out /tmp/ocp \
        --build 220685051 --build 220685054 --build 220685060 --build 220685061

By tag prefix (a "group"):

    python download_results.py --out /tmp/ocp \
        --tag-prefix jvmudf-complex-sf100-20260528 \
        --branch nengliu/gluten-java-udf-ocp

The script prints a JSON manifest mapping build id -> {tag, suite, path,
status} which analyze_results.py consumes.
"""

import argparse
import json
import os
import sys

import common


def resolve_builds(token, build_ids, tag_prefix, branch, only_succeeded):
    """Return a list of build briefs to download."""
    briefs = []
    if build_ids:
        for bid in build_ids:
            briefs.append(common.build_brief(common.get_build(token, bid)))
    if tag_prefix:
        briefs.extend(common.find_builds_by_tag_prefix(
            token, tag_prefix, branch=branch))

    # De-duplicate by id, keep first occurrence.
    seen = {}
    for b in briefs:
        if b["id"] not in seen:
            seen[b["id"]] = b
    result = list(seen.values())

    if only_succeeded:
        result = [b for b in result if b.get("result") == "succeeded"]
    return result


def main():
    parser = argparse.ArgumentParser(description="Download OCP perf-results artifacts.")
    parser.add_argument("--out", "-o", required=True,
                        help="Output directory; per-build subdirs are created.")
    parser.add_argument("--build", action="append", default=[],
                        help="Explicit build id (repeatable).")
    parser.add_argument("--tag-prefix", default=None,
                        help="Discover builds whose tagName starts with this prefix.")
    parser.add_argument("--branch", default=None,
                        help="Restrict tag-prefix discovery to this branch.")
    parser.add_argument("--only-succeeded", action="store_true",
                        help="Skip builds whose result is not 'succeeded'.")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Do not re-download a build whose dir already exists.")
    parser.add_argument("--include-eventlog", action="store_true",
                        help="Keep raw event-log dirs ({}); pruned by default "
                             "since they are large and unused by the analyzer."
                             .format(", ".join(common.EVENTLOG_DIRS)))
    args = parser.parse_args()

    if not args.build and not args.tag_prefix:
        parser.error("provide --build and/or --tag-prefix")

    try:
        token = common.get_access_token()
    except RuntimeError as exc:
        common.logger.error(str(exc))
        sys.exit(2)

    briefs = resolve_builds(token, args.build, args.tag_prefix,
                            args.branch, args.only_succeeded)
    if not briefs:
        common.logger.error("No builds matched the selection.")
        sys.exit(1)

    manifest = []
    for b in briefs:
        bid = b["id"]
        dest = os.path.join(args.out, str(bid))
        entry = {
            "id": bid,
            "tagName": b.get("tagName"),
            "perfSuiteType": b.get("perfSuiteType"),
            "scaleFactor": b.get("scaleFactor"),
            "result": b.get("result"),
            "additionalConfig": b.get("additionalConfig"),
            "path": dest,
        }
        if args.skip_existing and os.path.isdir(dest) and os.listdir(dest):
            entry["download"] = "skipped-existing"
            manifest.append(entry)
            common.logger.info("skip existing %s (%s)", bid, b.get("tagName"))
            continue
        try:
            common.download_artifact(bid, dest)
            entry["download"] = "ok"
            if not args.include_eventlog:
                removed, freed = common.prune_eventlog(dest)
                if removed:
                    entry["eventlogPruned"] = removed
                    common.logger.info(
                        "pruned %d event-log dir(s) (~%d MiB) from %s",
                        removed, freed // (1024 * 1024), bid)
            common.logger.info("downloaded %s (%s)", bid, b.get("tagName"))
        except RuntimeError as exc:
            entry["download"] = "failed"
            entry["error"] = str(exc)
            common.logger.warning("download failed for %s: %s", bid, exc)
        manifest.append(entry)

    out_manifest = os.path.join(args.out, "manifest.json")
    os.makedirs(args.out, exist_ok=True)
    with open(out_manifest, "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    common.logger.info("Wrote manifest: %s", out_manifest)


if __name__ == "__main__":
    main()
