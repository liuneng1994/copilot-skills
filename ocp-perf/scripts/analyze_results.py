"""Analyze and compare OCP perf-results across one or more runs.

Consumes a directory produced by download_results.py (per-build subdirs plus an
optional manifest.json) or explicit name=path pairs, then prints comparison
reports.

Dimensions
----------
Always:
  - response time   (executionTimeMs per query; ratios vs a reference run)
  - task / resource (shuffle, spill, coreSeconds, executorComputingTime, ...)
  - operator stats  (operatorStats.csv diff) + native fallback detection (query-plans)
  - anomalies       (ANOMALIES section: 0-byte / invalid metrics / failed validation)
  - config/version  (JAR VERSIONS + key Spark config diff across runs)
  - query results   (compares query-output rows across runs when present)
Opt-in (flags):
  --system-metrics  summarize CPU/mem/disk/net per run
  --profiling       locate CPU stack-trace (flame graph) files per run
  --all             enable all opt-in dimensions

Run comparison is generic: it does NOT assume a native-vs-baseline setup. The
response-time ratio uses a reference run per suite (a JVM-UDF baseline if that
signal is present, else the first run); use --baseline <label> to force one.

Examples
--------
    python analyze_results.py --in /tmp/ocp
    python analyze_results.py --run a=/tmp/ocp/220685051 --run b=/tmp/ocp/220685054
    python analyze_results.py --in /tmp/ocp --baseline build220685054
    python analyze_results.py --in /tmp/ocp --all
"""

import argparse
import csv
import glob
import json
import os
import re
import sys


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_run_root(build_dir):
    """Return the sf_<SF> directory inside a downloaded build dir, or None."""
    hits = glob.glob(os.path.join(build_dir, "*", "sf_*"))
    hits = [h for h in hits if os.path.isdir(h)]
    if not hits:
        # Some layouts may not nest under tag; try one level.
        hits = glob.glob(os.path.join(build_dir, "sf_*"))
    return hits[0] if hits else None


def short_label(tag, suite, native_flag, build_id=None):
    """Derive a compact run label.

    Uses suite (scala/java) and the JVM-UDF native flag when present, but does
    NOT assume a native/baseline setup: when neither signal is available it
    falls back to the build id, then a tag token, then 'run'.
    """
    parts = []
    if suite and "SCALA" in suite:
        parts.append("scala")
    elif suite and "JAVA" in suite:
        parts.append("java")
    if native_flag == "true":
        parts.append("native")
    elif native_flag == "false":
        parts.append("base")
    if parts:
        return "_".join(parts)
    if build_id:
        return "build{}".format(build_id)
    if tag:
        token = re.split(r"[-_/]+", tag.strip("-_/"))[-1]
        if token:
            return token
    return "run"


# ---------------------------------------------------------------------------
# summary.txt parsing
# ---------------------------------------------------------------------------

SECTION_RE = re.compile(r"^SECTION:\s*(.+)$")
SUBSECTION_RE = re.compile(r"^SUB-SECTION:\s*(.+)$")


def split_sections(lines):
    """Split summary lines into {section_name: [lines]} (top-level SECTIONs)."""
    sections = {}
    cur = None
    buf = []
    for l in lines:
        m = SECTION_RE.match(l)
        if m:
            if cur is not None:
                sections[cur] = buf
            cur = m.group(1).strip()
            buf = []
            continue
        buf.append(l)
    if cur is not None:
        sections[cur] = buf
    return sections


def parse_kv_block(lines):
    """Parse 'key : value' or 'key | value' lines into a dict."""
    out = {}
    for l in lines:
        if "|" in l:
            parts = [p.strip() for p in l.split("|")]
            if len(parts) == 2 and parts[0] and not parts[0].startswith("-"):
                out[parts[0]] = parts[1]
        elif ":" in l:
            k, _, v = l.partition(":")
            k = k.strip()
            v = v.strip()
            if k and v and " " not in k.split()[0][:0] or k:
                out.setdefault(k, v)
    return out


def parse_metrics_table(lines):
    """Parse a pipe-delimited table with a header row into {queryId: {col: val}}."""
    header = None
    rows = {}
    for l in lines:
        if "|" not in l:
            continue
        cells = [c.strip() for c in l.split("|")]
        if header is None:
            if "queryId" in cells and "executionTimeMs" in cells:
                header = cells
            continue
        if len(cells) != len(header):
            continue
        d = dict(zip(header, cells))
        qid = d.get("queryId", "").strip()
        if qid and qid != "queryId":
            rows[qid] = d
    return rows


def parse_summary(summary_path):
    with open(summary_path, errors="replace") as f:
        lines = f.read().splitlines()
    sections = split_sections(lines)
    data = {
        "jarVersions": parse_kv_block(sections.get("JAR VERSIONS", [])),
        "versionInfo": parse_kv_block(sections.get("VERSION INFORMATION", [])),
        "testConfig": parse_kv_block(sections.get("TEST CONFIGURATION", [])),
        "sparkConfig": parse_kv_block(sections.get("SPARK CONFIGURATION", [])),
        "anomalies": [l for l in sections.get("ANOMALIES", []) if l.strip()],
        "metrics": parse_metrics_table(sections.get("VALID MIN RUNTIME METRICS", [])),
    }
    return data


# ---------------------------------------------------------------------------
# operatorStats + plans + query-output
# ---------------------------------------------------------------------------

def parse_operator_stats(run_root):
    path = os.path.join(run_root, "operatorStats.csv")
    out = {}
    if not os.path.isfile(path):
        return out
    with open(path, errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            op = row.get("Operator", "").strip()
            if not op:
                continue
            try:
                freq = int(row.get("frequency", "0") or 0)
            except ValueError:
                freq = 0
            try:
                rows = int(row.get("numInputRows", "0") or 0)
            except ValueError:
                rows = 0
            out[op] = {"frequency": freq, "numInputRows": rows}
    return out


# Operators that indicate a Gluten native columnar path.
NATIVE_HINT = re.compile(r"Transformer|Velox|Columnar|Gluten", re.IGNORECASE)
# Plain Spark physical operators that suggest a fallback to row-based execution.
FALLBACK_HINT = re.compile(
    r"^\s*(\+?-?\s*)?(\**)(HashAggregate|SortMergeJoin|BroadcastHashJoin|"
    r"BroadcastNestedLoopJoin|Sort|Project|Filter|Exchange|Window|Expand|"
    r"Generate|ShuffledHashJoin|CartesianProduct)\b"
)


def detect_fallbacks(run_root):
    """Scan query-plans/*.txt for plain-Spark operators (potential fallbacks)."""
    out = {}
    plan_dir = os.path.join(run_root, "query-plans")
    for path in glob.glob(os.path.join(plan_dir, "queryId=*.txt")):
        qid = os.path.basename(path).replace("queryId=", "").replace(".txt", "")
        native = 0
        fallback_ops = {}
        with open(path, errors="replace") as f:
            for l in f:
                if NATIVE_HINT.search(l):
                    native += 1
                    continue
                m = FALLBACK_HINT.match(l)
                if m:
                    op = m.group(3)
                    fallback_ops[op] = fallback_ops.get(op, 0) + 1
        out[qid] = {"nativeOps": native, "fallbackOps": fallback_ops}
    return out


def collect_query_output(run_root):
    """Return {queryId: {'files': [...], 'rows': int, 'bytes': int}} for result rows.

    Only counts non-.sql files (the .sql is the query text, not the result).
    """
    out = {}
    base = os.path.join(run_root, "query-output")
    for d in glob.glob(os.path.join(base, "queryId=*")):
        qid = os.path.basename(d).replace("queryId=", "")
        files = [f for f in glob.glob(os.path.join(d, "**", "*"), recursive=True)
                 if os.path.isfile(f) and not f.endswith(".sql")]
        nbytes = sum(os.path.getsize(f) for f in files)
        nrows = 0
        for f in files:
            try:
                with open(f, errors="replace") as fh:
                    nrows += sum(1 for _ in fh)
            except OSError:
                pass
        out[qid] = {"files": files, "rows": nrows, "bytes": nbytes}
    return out


def summarize_system_metrics(run_root):
    base = os.path.join(run_root, "system-metrics")
    if not os.path.isdir(base):
        return None
    hosts = set()
    components = set()
    nfiles = 0
    for path in glob.glob(os.path.join(base, "**", "*"), recursive=True):
        if not os.path.isfile(path):
            continue
        nfiles += 1
        m = re.search(r"host=([^/]+)", path)
        if m:
            hosts.add(m.group(1))
        m = re.search(r"metricComponent=([^/]+)", path)
        if m:
            components.add(m.group(1))
    return {"hosts": sorted(hosts), "componentCount": len(components),
            "components": sorted(components), "files": nfiles}


def summarize_profiling(run_root):
    base = os.path.join(run_root, "profiling")
    if not os.path.isdir(base):
        return None
    files = [f for f in glob.glob(os.path.join(base, "**", "*"), recursive=True)
             if os.path.isfile(f)]
    return {"fileCount": len(files), "files": files[:10]}


# ---------------------------------------------------------------------------
# Loading runs
# ---------------------------------------------------------------------------

def load_run(label, build_dir, manifest_entry=None, build_id=None):
    run_root = find_run_root(build_dir)
    if not run_root:
        return None
    summaries = glob.glob(os.path.join(run_root, "summary", "summary.txt"))
    summary = parse_summary(summaries[0]) if summaries else {
        "jarVersions": {}, "versionInfo": {}, "testConfig": {},
        "sparkConfig": {}, "anomalies": [], "metrics": {},
    }
    suite = summary["testConfig"].get("Performance Suite Type") \
        or (manifest_entry or {}).get("perfSuiteType")
    native_flag = summary["sparkConfig"].get(
        "spark.gluten.sql.columnar.backend.velox.udf.jvm.enabled")
    if native_flag is None and manifest_entry:
        cfg = manifest_entry.get("additionalConfig") or "{}"
        try:
            native_flag = json.loads(cfg).get(
                "spark.gluten.sql.columnar.backend.velox.udf.jvm.enabled")
        except json.JSONDecodeError:
            native_flag = None
    tag = summary["testConfig"].get("Tag") or (manifest_entry or {}).get("tagName")
    derived = short_label(tag, suite, native_flag, build_id)
    return {
        "label": label or derived,
        "buildDir": build_dir,
        "runRoot": run_root,
        "tag": tag,
        "suite": suite,
        "nativeFlag": native_flag,
        "summary": summary,
        "operatorStats": parse_operator_stats(run_root),
        "fallbacks": detect_fallbacks(run_root),
        "queryOutput": collect_query_output(run_root),
    }


def discover_runs(in_dir):
    manifest = {}
    mpath = os.path.join(in_dir, "manifest.json")
    if os.path.isfile(mpath):
        with open(mpath) as f:
            for e in json.load(f):
                manifest[str(e["id"])] = e
    runs = []
    for d in sorted(glob.glob(os.path.join(in_dir, "*"))):
        if not os.path.isdir(d):
            continue
        bid = os.path.basename(d)
        entry = manifest.get(bid)
        run = load_run(None, d, entry, build_id=bid)
        if run:
            run["buildId"] = bid
            # disambiguate identical labels
            runs.append(run)
    # ensure unique labels
    seen = {}
    for r in runs:
        base = r["label"]
        if base in seen:
            seen[base] += 1
            r["label"] = "{}#{}".format(base, seen[base])
        else:
            seen[base] = 0
    return runs


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def all_queries(runs):
    qs = set()
    for r in runs:
        qs.update(r["summary"]["metrics"].keys())
    def key(q):
        m = re.search(r"(\d+)", q)
        return (int(m.group(1)) if m else 0, q)
    return sorted(qs, key=key)


def group_by_suite(runs):
    groups = {}
    for r in runs:
        groups.setdefault(r["suite"] or "?", []).append(r)
    return groups


def pick_reference(rs):
    """Choose a reference run within a comparable group.

    Prefers an explicit baseline (JVM-UDF native disabled) when that signal is
    present; otherwise falls back to the first run. This keeps the
    native/baseline workflow working without assuming it exists.
    """
    for r in rs:
        if r.get("nativeFlag") == "false":
            return r
    return rs[0]


def report_response_time(runs, baseline_label=None):
    print("\n==================== RESPONSE TIME (executionTimeMs) ====================")
    labels = [r["label"] for r in runs]
    qs = all_queries(runs)
    w = 16
    header = "{:<20}".format("query") + "".join("{:>{w}}".format(l, w=w) for l in labels)
    print(header)
    print("-" * len(header))
    totals = {r["label"]: 0.0 for r in runs}
    for q in qs:
        line = "{:<20}".format(q)
        for r in runs:
            row = r["summary"]["metrics"].get(q)
            v = _num(row.get("executionTimeMs")) if row else None
            if v is not None:
                totals[r["label"]] += v
                line += "{:>{w},.0f}".format(v, w=w)
            else:
                line += "{:>{w}}".format("-", w=w)
        print(line)
    print("-" * len(header))
    tl = "{:<20}".format("TOTAL")
    for r in runs:
        tl += "{:>{w},.0f}".format(totals[r["label"]], w=w)
    print(tl)

    if len(runs) < 2:
        return

    # Generic ratio: each run vs a reference run. No native/baseline assumed.
    print("\n-- response-time ratio (run / reference; >1 = slower than reference) --")
    if baseline_label:
        ref = next((r for r in runs if r["label"] == baseline_label), None)
        if ref is None:
            print("  (baseline label '{}' not found among runs)".format(baseline_label))
            return
        groups = [("all runs", ref, [r for r in runs if r is not ref])]
    else:
        groups = []
        for suite, rs in group_by_suite(runs).items():
            if len(rs) < 2:
                continue
            ref = pick_reference(rs)
            groups.append(("suite {}".format(suite), ref,
                           [r for r in rs if r is not ref]))
        if not groups:
            print("  (no comparable group with >=2 runs sharing a suite; "
                  "pass --baseline <label> to force a reference run)")
            return

    for title, ref, others in groups:
        print("  {}: reference = {}".format(title, ref["label"]))
        for q in qs:
            vref = _num((ref["summary"]["metrics"].get(q) or {}).get("executionTimeMs"))
            if not vref:
                continue
            cells = []
            for o in others:
                vo = _num((o["summary"]["metrics"].get(q) or {}).get("executionTimeMs"))
                if vo:
                    cells.append("{}={:.2f}".format(o["label"], vo / vref))
            if cells:
                print("    {:<18} {}".format(q, "  ".join(cells)))


TASK_COLS = [
    "shuffleReadMB", "shuffleWrittenMB", "sortSpillSizeBytes",
    "aggregateSpillSizeBytes", "totalCoreSeconds", "executorComputingTimeS",
    "schedulerDelayS", "totalTaskCount", "failedTaskCount",
]


def report_task_metrics(runs):
    print("\n==================== TASK / RESOURCE METRICS ====================")
    qs = all_queries(runs)
    for col in TASK_COLS:
        # only print column if any run has it
        present = any(col in (next(iter(r["summary"]["metrics"].values()), {}))
                      for r in runs if r["summary"]["metrics"])
        if not present:
            continue
        print("\n-- {} --".format(col))
        labels = [r["label"] for r in runs]
        w = 16
        header = "{:<20}".format("query") + "".join("{:>{w}}".format(l, w=w) for l in labels)
        print(header)
        for q in qs:
            line = "{:<20}".format(q)
            for r in runs:
                row = r["summary"]["metrics"].get(q)
                v = _num(row.get(col)) if row else None
                line += ("{:>{w},.2f}".format(v, w=w) if v is not None
                         else "{:>{w}}".format("-", w=w))
            print(line)


def report_operator_stats(runs):
    print("\n==================== OPERATOR STATS (frequency) ====================")
    ops = set()
    for r in runs:
        ops.update(r["operatorStats"].keys())
    if not ops:
        print("  (no operatorStats.csv found)")
        return
    labels = [r["label"] for r in runs]
    w = 14
    header = "{:<70}".format("operator") + "".join("{:>{w}}".format(l, w=w) for l in labels)
    print(header)
    for op in sorted(ops):
        vals = [r["operatorStats"].get(op, {}).get("frequency") for r in runs]
        # only show ops where runs differ or any present
        line = "{:<70}".format(op[:70])
        for v in vals:
            line += "{:>{w}}".format(v if v is not None else "-", w=w)
        # mark differing rows
        present = [v for v in vals if v is not None]
        if len(set(present)) > 1:
            line += "  <-- differs"
        print(line)


def report_fallbacks(runs):
    print("\n==================== NATIVE FALLBACK DETECTION (query-plans) ====================")
    any_plan = any(r["fallbacks"] for r in runs)
    if not any_plan:
        print("  (no query-plans found)")
        return
    for r in runs:
        hdr = r["label"]
        if r["nativeFlag"] is not None:
            hdr += " (nativeFlag={})".format(r["nativeFlag"])
        print("\n-- {} --".format(hdr))
        if not r["fallbacks"]:
            print("    (no plans)")
            continue
        for q in sorted(r["fallbacks"]):
            fb = r["fallbacks"][q]
            ops = fb["fallbackOps"]
            top = ", ".join("{}x{}".format(v, k) for k, v in
                            sorted(ops.items(), key=lambda kv: -kv[1])[:6])
            print("    {:<18} nativeOps={:<5} plainSparkOps: {}".format(
                q, fb["nativeOps"], top or "(none)"))


def report_anomalies(runs):
    print("\n==================== ANOMALIES / VALIDATION ====================")
    for r in runs:
        print("\n-- {} --".format(r["label"]))
        anomalies = r["summary"]["anomalies"]
        if anomalies:
            for l in anomalies:
                print("    " + l)
        else:
            print("    (no ANOMALIES section)")
        # validation flags from metrics table
        fails = [q for q, row in r["summary"]["metrics"].items()
                 if row.get("queryResultValidationSuccess") == "false"]
        if fails:
            print("    validation=false: " + ", ".join(sorted(fails)))


def report_config_version_diff(runs):
    print("\n==================== CONFIG & VERSION DIFF ====================")
    # JAR versions
    print("\n-- JAR VERSIONS --")
    keys = set()
    for r in runs:
        keys.update(r["summary"]["jarVersions"].keys())
    for k in sorted(keys):
        vals = [r["summary"]["jarVersions"].get(k, "-") for r in runs]
        marker = "  <-- differs" if len(set(vals)) > 1 else ""
        print("  {:<28} {}{}".format(k, " | ".join(vals), marker))

    # Spark config diff: only keys that differ across runs
    print("\n-- SPARK CONFIG (only keys differing across runs) --")
    keys = set()
    for r in runs:
        keys.update(r["summary"]["sparkConfig"].keys())
    diff_found = False
    for k in sorted(keys):
        vals = [r["summary"]["sparkConfig"].get(k, "<absent>") for r in runs]
        if len(set(vals)) > 1:
            diff_found = True
            print("  {}".format(k))
            for r, v in zip(runs, vals):
                print("      {:<14} {}".format(r["label"], v))
    if not diff_found:
        print("  (no differing spark config keys)")


def report_query_results(runs):
    print("\n==================== QUERY RESULT COMPARISON ====================")
    if len(runs) < 2:
        print("  (need >=2 runs to compare; only {} present)".format(len(runs)))
        return
    qs = set()
    for r in runs:
        qs.update(r["queryOutput"].keys())
    if not qs:
        print("  (no query-output result rows present in any run; "
              "queryOutputEnabled=false or 0-byte results)")
        return
    labels = [r["label"] for r in runs]
    w = 18
    header = "{:<20}".format("query") + "".join("{:>{w}}".format(l + " rows", w=w) for l in labels)
    print(header)
    for q in sorted(qs):
        line = "{:<20}".format(q)
        rowcounts = []
        for r in runs:
            qo = r["queryOutput"].get(q)
            rc = qo["rows"] if qo else None
            rowcounts.append(rc)
            line += "{:>{w}}".format(rc if rc is not None else "-", w=w)
        present = [c for c in rowcounts if c is not None]
        if len(set(present)) > 1:
            line += "  <-- row count differs"
        print(line)
    print("\n  Note: row-count match does not guarantee identical content; "
          "use the OCP built-in validation (queryResultValidationSuccess) for correctness.")


def report_overview(runs):
    print("==================== RUN OVERVIEW ====================")
    for r in runs:
        nf = r["nativeFlag"] if r["nativeFlag"] is not None else "-"
        print("  {:<16} build={:<11} suite={:<26} nativeFlag={:<6} tag={}".format(
            r["label"], r.get("buildId", "?"), str(r["suite"]),
            str(nf), r["tag"]))


def report_system_metrics(runs):
    print("\n==================== SYSTEM METRICS (summary) ====================")
    for r in runs:
        sm = summarize_system_metrics(r["runRoot"])
        if not sm:
            print("  {}: (none)".format(r["label"]))
            continue
        print("  {}: hosts={} components={} files={}".format(
            r["label"], len(sm["hosts"]), sm["componentCount"], sm["files"]))
        print("      components: {}".format(", ".join(sm["components"][:20])))


def report_profiling(runs):
    print("\n==================== PROFILING (flame graph stack traces) ====================")
    for r in runs:
        pf = summarize_profiling(r["runRoot"])
        if not pf:
            print("  {}: (none)".format(r["label"]))
            continue
        print("  {}: {} files".format(r["label"], pf["fileCount"]))
        for f in pf["files"]:
            print("      " + f)


def main():
    parser = argparse.ArgumentParser(description="Analyze/compare OCP perf-results.")
    parser.add_argument("--in", dest="in_dir", default=None,
                        help="Directory containing per-build subdirs (+ manifest.json).")
    parser.add_argument("--run", action="append", default=[],
                        help="Explicit label=path of a downloaded build dir (repeatable).")
    parser.add_argument("--system-metrics", action="store_true",
                        help="Include system metrics summary.")
    parser.add_argument("--profiling", action="store_true",
                        help="Include profiling/flame-graph file listing.")
    parser.add_argument("--all", action="store_true",
                        help="Enable all opt-in dimensions.")
    parser.add_argument("--baseline", default=None,
                        help="Label of the run to use as the response-time "
                             "reference for ratios (default: auto per suite).")
    args = parser.parse_args()

    runs = []
    if args.in_dir:
        runs = discover_runs(args.in_dir)
    for pair in args.run:
        if "=" not in pair:
            parser.error("--run expects label=path, got: " + pair)
        label, path = pair.split("=", 1)
        run = load_run(label, path, build_id=os.path.basename(path.rstrip("/")))
        if run:
            run["buildId"] = os.path.basename(path.rstrip("/"))
            runs.append(run)

    if not runs:
        print("No runs found. Provide --in <dir> or --run label=path.", file=sys.stderr)
        sys.exit(1)

    report_overview(runs)
    report_response_time(runs, baseline_label=args.baseline)
    report_task_metrics(runs)
    report_operator_stats(runs)
    report_fallbacks(runs)
    report_anomalies(runs)
    report_config_version_diff(runs)
    report_query_results(runs)
    if args.all or args.system_metrics:
        report_system_metrics(runs)
    if args.all or args.profiling:
        report_profiling(runs)


if __name__ == "__main__":
    main()
