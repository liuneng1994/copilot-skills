---
name: ocp-perf
description: Trigger Spark Fabric OneClick Performance (OCP) perf runs, download their perf-results artifacts, and analyze/compare results across a group of runs (response time, query result comparison, task/resource metrics, operator stats + native fallback detection, anomalies/validation, config & version diff, system metrics, profiling). Use when asked to run/trigger an OCP perf test, reuse a prior jar, download perf-results, or compare native vs baseline / scala vs java OCP runs.
---

# OCP Perf (Spark Fabric OneClick Performance)

End-to-end helper for the **Spark Fabric OneClick Performance** pipeline
(ADO id `44400`, org `https://msdata.visualstudio.com`, project `A365`):
trigger runs, download the `perf-results` artifact, and compare results across
a group of runs.

## Prerequisites
- `az` CLI logged in (`az login` done). Token is obtained via
  `az account get-access-token`.
- Python 3 (stdlib only). No extra packages required.

## Scripts
All scripts live in `scripts/` next to this file. `<dir>` below is that path.

| Script | Purpose |
|--------|---------|
| `common.py` | Shared ADO REST + artifact helpers (imported by the others). |
| `trigger_ocp.py` | Trigger one run or a group of runs; clone params from a reference build. |
| `download_results.py` | Download `perf-results` for explicit build ids or a tag prefix. |
| `analyze_results.py` | Parse + compare runs and print reports. |

## Core concepts
- A **run** is one OCP build. Its key knobs live in `templateParameters`:
  `perfSuiteType`, `tagName`, `scaleFactor`, `lakeHouseName`, `additionalConfig`
  (JSON of Spark confs), `buildRepo`/`buildNumber` (reuse-jar), `useMasterVHD`.
- **native vs baseline** is controlled by
  `spark.gluten.sql.columnar.backend.velox.udf.jvm.enabled` inside
  `additionalConfig` (`true` = native, `false` = fallback baseline).
- A **group** is a set of runs that share an experiment, usually the matrix
  `{scala,java} x {native,baseline}`, distinguished by `tagName`.
- **Reuse jar**: to skip the Maven build and reuse a prior build's jar set
  `buildRepo=false` and `buildNumber=<jar build id>`.

---

## Part 1 - Trigger

The fastest, safest way to trigger is to **clone a known-good reference build's
parameters** and only override what changes (a config key, the tag, the jar).
This avoids re-specifying ~40 parameters and guarantees apples-to-apples runs.

### Step 1 - find a reference build
Pick a prior successful run of the same experiment (same suite/SF/lakehouse).
Inspect its parameters:

```bash
az pipelines build show --id <refBuildId> \
  --org https://msdata.visualstudio.com --project A365 \
  --query templateParameters -o json
```

### Step 2 - dry-run the trigger
Always dry-run first to inspect the exact parameters that will be submitted:

```bash
python <dir>/scripts/trigger_ocp.py group \
    --branch <branch> \
    --ref scala_native=<refScalaNative> \
    --ref scala_baseline=<refScalaBaseline> \
    --ref java_native=<refJavaNative> \
    --ref java_baseline=<refJavaBaseline> \
    --merge-config <sparkConfKey>=<value> \
    --reuse-jar <jarBuildId> \
    --tag-suffix <suffix> \
    --dry-run
```

- `--merge-config k=v` merges a key into each run's existing `additionalConfig`
  JSON (repeatable). Use this to flip one Spark conf for the whole group.
- `--set k=v` overwrites a top-level template parameter (repeatable).
- `--reuse-jar <id>` sets `buildRepo=false`, `buildNumber=<id>`.
- `--tag-suffix s` appends `-s` to each cloned `tagName` (keeps groups distinct).

`single` mode is the same minus `--ref ...`; pass `--ref-build <id>` to clone, or
omit it to build params purely from `--set`/`--merge-config`.

### Step 3 - CONFIRM with the user, then trigger
**CRITICAL: never trigger without explicit user confirmation.** Use `ask_user`
to show the resolved branch + per-run tag + `additionalConfig` + reuse-jar and
let the user accept/override. After confirmation, drop `--dry-run` to submit.

### Step 4 - report
Print each triggered build id, tag, and the `_build/results?buildId=...` URL.

---

## Part 2 - Download results

Runs publish a single artifact named **`perf-results`** (only on runs that
reached query execution; infra-failed runs have none).

By explicit ids:
```bash
python <dir>/scripts/download_results.py --out <outDir> \
    --build <id1> --build <id2> --build <id3> --build <id4>
```

By tag prefix (auto-discovers the group on the pipeline):
```bash
python <dir>/scripts/download_results.py --out <outDir> \
    --tag-prefix <commonTagPrefix> --branch <branch> --only-succeeded
```

Each build lands in `<outDir>/<buildId>/...` and a `<outDir>/manifest.json`
records `{id, tagName, perfSuiteType, scaleFactor, result, additionalConfig}`
per build (used by the analyzer for labels and native/baseline detection).
Add `--skip-existing` to avoid re-downloading.

By default the large raw event-log dirs (`spark-events/`, `event-log-dir/`)
are pruned after download to save disk; the analyzer does not use them. Pass
`--include-eventlog` to keep them.

---

## Part 3 - Analyze & compare

```bash
python <dir>/scripts/analyze_results.py --in <outDir>
# or name runs explicitly:
python <dir>/scripts/analyze_results.py \
    --run a=<outDir>/<idA> --run b=<outDir>/<idB>
# force the reference run used for response-time ratios:
python <dir>/scripts/analyze_results.py --in <outDir> --baseline <label>
# include the heavier opt-in sections:
python <dir>/scripts/analyze_results.py --in <outDir> --all
```

The analyzer makes **no assumption that runs are native vs baseline** - it
compares any set of runs. Labels are derived from suite + JVM-UDF native flag
when present, otherwise from the build id (`build<id>`); override with
`--run <label>=<path>`.

Reports produced:

1. **Run overview** - label, build id, suite, nativeFlag (`-` when N/A), tag.
2. **Response time** - `executionTimeMs` per query + totals, plus a ratio of
   each run vs a reference (ratio > 1 = slower than reference). The reference
   is chosen per suite (a JVM-UDF baseline if that flag is present, else the
   first run); use `--baseline <label>` to force a single global reference.
3. **Task / resource metrics** - shuffle R/W MB, sort/agg spill, totalCoreSeconds,
   executorComputingTimeS, schedulerDelayS, task counts, per query.
4. **Operator stats** - `operatorStats.csv` frequency diff across runs
   (rows that differ are flagged).
5. **Native fallback detection** - scans `query-plans/*.txt`; reports native op
   count vs plain-Spark operators per query. Differences between native and
   baseline plans (e.g. `VeloxBroadcastNestedLoopJoinExecTransformer` vs plain
   `BroadcastNestedLoopJoin`) explain perf deltas.
6. **Anomalies / validation** - the `ANOMALIES` section (0-byte, invalid metrics,
   failed validation) and the per-query `queryResultValidationSuccess` flag.
7. **Config & version diff** - JAR versions and only the Spark config keys that
   differ across runs (catches unintended drift; confirms the intended toggle).
8. **Query result comparison** (>=2 runs) - per-query result row counts from
   `query-output/`; flags row-count mismatches. Present only when
   `queryOutputEnabled=true` and results are non-empty.

Opt-in (`--system-metrics`, `--profiling`, or `--all`):
- **System metrics** - per-run hosts/components/file counts from `system-metrics/`.
- **Profiling** - locates CPU flame-graph stack-trace files under `profiling/`.

### Interpreting results
- If a query is `validation=false` / `0 byte`, its timings are indicative only,
  not a clean baseline; fix correctness before trusting the numbers.
- For "native slower than baseline", cross-check the fallback section and the
  task-metrics section: more plain-Spark ops, extra `RowToVeloxColumnar` /
  `VeloxColumnarToRow` transitions, or higher executorComputingTime on the
  native side usually explain it.

---

## perf-results artifact layout (reference)
```
<buildId>/<tag>/sf_<SF>/
  summary/summary.txt          # sections below
  operatorStats.csv            # Operator,frequency,numInputRows
  query-plans/queryId=*.txt    # physical plans (fallback detection)
  query-output/queryId=*/      # result rows (when queryOutputEnabled)
  query-metrics/queryId=*/     # raw per-query metrics json
  system-metrics/queryId=*/host=*/metricComponent=*/<metric>
  profiling/queryId=all/stackTraceType=thread/container=*/rawStackTracesThread.txt
  spark-events/ , cluster-info/ , executionid-map.csv
  # NOTE: spark-events/ and <tag>/event-log-dir/ are raw event logs, pruned
  #       on download by default (--include-eventlog keeps them).
```
`summary.txt` SECTIONs: JAR VERSIONS, VERSION INFORMATION, TEST CONFIGURATION,
SPARK CONFIGURATION, AGGREGATED MIN RUNTIME METRICS, ANOMALIES,
VALID MIN RUNTIME METRICS (the per-query metric table; `executionTimeMs` is the
response time), QUERY BREAKDOWN (per-query sub-sections).

## Notes
- Override org/project/pipeline via env `OCP_ORG`, `OCP_PROJECT`,
  `OCP_PIPELINE_ID` if ever needed.
- Branch refs are auto-prefixed with `refs/heads/`.
- The analyzer parses tolerantly; missing sections or `Error: ...` lines in a
  summary are handled gracefully.
