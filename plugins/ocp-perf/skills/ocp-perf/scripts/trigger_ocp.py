"""Trigger Spark Fabric OneClick Performance (OCP) runs.

Two modes:
  single   - trigger one run, optionally cloning a reference build's parameters.
  group    - trigger several runs cloned from reference builds, applying a
             shared additionalConfig override and a shared tag suffix. This is
             the common "native vs baseline x scala vs java" matrix pattern.

The script only prepares and submits parameters. The agent MUST confirm the
final parameters with the user (ask_user) before invoking trigger.

Examples
--------
Show what a single run cloned from a reference build would submit (dry-run):

    python trigger_ocp.py single \
        --branch nengliu/gluten-java-udf-ocp \
        --ref-build 220685051 \
        --set additionalConfig='{"spark.gluten.sql.broadcastNestedLoopJoinTransformerEnabled":"false"}' \
        --reuse-jar 220680140 \
        --tag-suffix bnlj-off --dry-run

Trigger a 4-run group cloned from 4 reference builds, merging one config key
into each run's existing additionalConfig and appending a tag suffix:

    python trigger_ocp.py group \
        --branch nengliu/gluten-java-udf-ocp \
        --ref scala_native=220685051 \
        --ref scala_baseline=220685054 \
        --ref java_native=220685060 \
        --ref java_baseline=220685061 \
        --merge-config spark.gluten.sql.broadcastNestedLoopJoinTransformerEnabled=false \
        --reuse-jar 220680140 \
        --tag-suffix bnlj-off
"""

import argparse
import json
import sys

import common


def _apply_reuse_jar(params, reuse_jar):
    if reuse_jar:
        params["buildRepo"] = "false"
        params["buildNumber"] = str(reuse_jar)
    return params


def _merge_config(params, merge_pairs):
    """Merge key=value pairs into the JSON additionalConfig param."""
    if not merge_pairs:
        return params
    raw = params.get("additionalConfig") or "{}"
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError:
        cfg = {}
    for pair in merge_pairs:
        if "=" not in pair:
            raise ValueError("--merge-config expects key=value, got: " + pair)
        k, v = pair.split("=", 1)
        cfg[k] = v
    params["additionalConfig"] = json.dumps(cfg, separators=(",", ":"))
    return params


def _apply_sets(params, set_pairs):
    """Overwrite top-level template parameters with key=value pairs."""
    for pair in set_pairs or []:
        if "=" not in pair:
            raise ValueError("--set expects key=value, got: " + pair)
        k, v = pair.split("=", 1)
        params[k] = v
    return params


def _apply_tag_suffix(params, suffix):
    if suffix:
        base = params.get("tagName") or "ocp-run"
        params["tagName"] = base + "-" + suffix
    return params


def build_params_from_ref(token, ref_build, set_pairs, merge_pairs,
                          reuse_jar, tag_suffix):
    params = common.get_template_parameters(token, ref_build)
    if not params:
        raise RuntimeError(
            "Reference build {} has no templateParameters".format(ref_build)
        )
    params = _merge_config(params, merge_pairs)
    params = _apply_sets(params, set_pairs)
    params = _apply_reuse_jar(params, reuse_jar)
    params = _apply_tag_suffix(params, tag_suffix)
    return params


def cmd_single(args, token):
    if args.ref_build:
        params = build_params_from_ref(
            token, args.ref_build, args.set, args.merge_config,
            args.reuse_jar, args.tag_suffix,
        )
    else:
        params = {}
        params = _merge_config(params, args.merge_config)
        params = _apply_sets(params, args.set)
        params = _apply_reuse_jar(params, args.reuse_jar)
        params = _apply_tag_suffix(params, args.tag_suffix)

    plan = {"branch": args.branch, "templateParameters": params}
    if args.dry_run:
        print(json.dumps({"dryRun": True, "plan": plan}, indent=2, ensure_ascii=False))
        return
    result = common.trigger_run(token, args.branch, params)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_group(args, token):
    refs = {}
    for pair in args.ref:
        if "=" not in pair:
            raise ValueError("--ref expects name=buildId, got: " + pair)
        name, build_id = pair.split("=", 1)
        refs[name] = build_id

    prepared = []
    for name, build_id in refs.items():
        params = build_params_from_ref(
            token, build_id, args.set, args.merge_config,
            args.reuse_jar, args.tag_suffix,
        )
        prepared.append({"case": name, "refBuild": build_id, "params": params})

    if args.dry_run:
        print(json.dumps({"dryRun": True, "branch": args.branch,
                          "runs": prepared}, indent=2, ensure_ascii=False))
        return

    triggered = []
    for item in prepared:
        res = common.trigger_run(token, args.branch, item["params"])
        res["case"] = item["case"]
        res["refBuild"] = item["refBuild"]
        triggered.append(res)
    print(json.dumps(triggered, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Trigger OCP perf runs.")
    sub = parser.add_subparsers(dest="command", required=True)

    common_args = argparse.ArgumentParser(add_help=False)
    common_args.add_argument("--branch", "-b", required=True,
                             help="Branch to run on (refs/heads/ auto-prefixed).")
    common_args.add_argument("--set", action="append", default=[],
                             help="Overwrite a top-level template param: key=value (repeatable).")
    common_args.add_argument("--merge-config", action="append", default=[],
                             help="Merge a key=value into additionalConfig JSON (repeatable).")
    common_args.add_argument("--reuse-jar", default=None,
                             help="Reuse a prior jar: sets buildRepo=false, buildNumber=<id>.")
    common_args.add_argument("--tag-suffix", default=None,
                             help="Append '-<suffix>' to the cloned tagName.")
    common_args.add_argument("--dry-run", action="store_true",
                             help="Print the parameters that would be submitted; do not trigger.")

    p_single = sub.add_parser("single", parents=[common_args],
                              help="Trigger one run.")
    p_single.add_argument("--ref-build", default=None,
                          help="Clone templateParameters from this build id.")

    p_group = sub.add_parser("group", parents=[common_args],
                             help="Trigger several runs cloned from reference builds.")
    p_group.add_argument("--ref", action="append", required=True,
                         help="Reference build as name=buildId (repeatable).")

    args = parser.parse_args()

    try:
        token = common.get_access_token()
    except RuntimeError as exc:
        common.logger.error(str(exc))
        sys.exit(2)

    try:
        if args.command == "single":
            cmd_single(args, token)
        elif args.command == "group":
            cmd_group(args, token)
    except (ValueError, RuntimeError) as exc:
        common.logger.error(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
