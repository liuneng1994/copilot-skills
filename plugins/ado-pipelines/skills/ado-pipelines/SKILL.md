---
name: ado-pipelines
description: >
  Query Azure DevOps pipeline build status, diagnose failures, and trigger new pipeline runs.
  Use this skill whenever the user shares an ADO build URL (containing buildId=), asks about a
  pipeline run, wants to know why a build failed, asks to check CI/CD status, or wants to trigger /
  queue / run a pipeline. Also trigger when the user mentions "pipeline", "build failure",
  "build status", "CI", "ADO build", "run pipeline", "trigger build", or "queue build".
---

# Pipeline Build Analysis & Trigger

Analyze Azure DevOps (ADO) pipeline builds for the Gluten project — query status, fetch logs for
failed tasks, identify root causes, and trigger new pipeline runs on any branch.

## Prerequisites

- `az` CLI must be authenticated (`az login` already done).
- The default ADO org/project are `https://msdata.visualstudio.com` / `A365`.

## Scripts

Two bundled scripts live in the `scripts/` directory next to this SKILL.md:

| Script | Purpose |
|--------|---------|
| `fetch_build_info.py` | Query build status, timeline, and failed-task logs |
| `trigger_pipeline.py` | List pipelines, discover parameters, and trigger runs |

## Quick Reference — Query Build Status

```bash
# Status overview only (fast)
python <skill-dir>/scripts/fetch_build_info.py <build_id_or_url>

# Status + failed-task logs (slower, but needed for root-cause analysis)
python <skill-dir>/scripts/fetch_build_info.py <build_id_or_url> --logs --log-lines 120
```

The script accepts either a numeric build ID or a full ADO URL
(e.g. `https://msdata.visualstudio.com/A365/_build/results?buildId=212185976&view=results`).

Output is JSON with three top-level keys: `build`, `timeline`, `failures`.

## Quick Reference — Trigger Pipeline

```bash
# List all pipelines (or filter by keyword)
python <skill-dir>/scripts/trigger_pipeline.py list
python <skill-dir>/scripts/trigger_pipeline.py list --keyword Gluten

# Show a pipeline's runtime parameters and defaults
python <skill-dir>/scripts/trigger_pipeline.py params --pipeline <name_or_id>

# Trigger a pipeline run with parameter overrides
python <skill-dir>/scripts/trigger_pipeline.py run \
    --pipeline <name_or_id> \
    --branch <branch_name> \
    --param sparkVersion=3.5 \
    --param enableGlutenUT=true
```

`<skill-dir>` is the directory containing this SKILL.md file.

## Workflow

### Step 1 — Fetch build info

Run the script **without** `--logs` first to get a fast overview.
Present a summary table to the user:

| Field | Value |
|-------|-------|
| Pipeline | `build.pipeline` |
| Build # | `build.buildNumber` |
| Status | `build.status` / `build.result` |
| Branch | `build.sourceBranch` |
| Triggered by | `build.requestedFor` |
| Reason | `build.reason` (manual / pullRequest / schedule) |
| Start / Finish | `build.startTime` — `build.finishTime` |

Then list stages and jobs with status icons:
- ✅ succeeded
- ⚠️ succeededWithIssues
- ❌ failed
- 🔄 inProgress
- ⏭️ skipped / canceled

If the build is still **inProgress**, report what is done and what is running. No further
analysis is needed unless the user asks.

### Step 2 — Diagnose failures

If the build **failed**, re-run the script with `--logs --log-lines 120` to fetch the tail of
each failed task's log.

For each failed task, examine `failures[].log_tail` and `failures[].issues` to determine the
root cause. Apply the classification rules below.

### Step 3 — Classify the failure

Assign each failed task to **one** of the categories below based on the log content, exit code,
and error messages. A single build may have failures in multiple categories.

| # | Category | Signals |
|---|----------|---------|
| 1 | **C++ Compilation Error** | Compiler error messages (`error:`, `fatal error:`), `ninja: build stopped`, `make: *** [...] Error`. Read the actual compiler message to identify the file and line. |
| 2 | **C++ Linker Error** | `undefined reference to`, `ld returned`, `multiple definition of`. Often caused by missing library or symbol version mismatch. |
| 3 | **OOM / Resource Exhaustion** | Exit code 137 (SIGKILL/OOM-killer), `Cannot allocate memory`, `out of memory`, `mmap failed`, `insufficient memory`. Common on build agents with limited RAM. |
| 4 | **Process Crash (SIGABRT/SIGSEGV)** | Exit code 134 (SIGABRT) or 139 (SIGSEGV), `core dumped`, `Aborted`, `Assertion failed`, `unreachable code was reached`. Check for assertion messages in the log. |
| 5 | **Scala / Maven Build Failure** | `[ERROR]` with `mvn` context, `BUILD FAILURE`, `compilation error`, `not found: value`, `type mismatch`. Read the Maven error summary. |
| 6 | **Unit Test Failure** | `Tests run:.*Failures:`, `test.*FAILED`, `org.scalatest`, `AssertionError`, `not equal`. Identify which test class and method failed. |
| 7 | **Scalastyle / Lint Failure** | `scalastyle`, `checkstyle`, `Scalastyle.*error`, `File.*does not pass`. Show the specific style violations. |
| 8 | **Infrastructure / Agent Issue** | Node.js assertion failures in ADO agent tasks, `UniversalPackages` download crashes, `Secure Supply Chain` failures, network errors (`Connection refused`, `timeout`), vcpkg bootstrap failures. These are **not code issues** — they are CI environment problems. |
| 9 | **Artifact / Publish Failure** | `Path does not exist`, `Publish Build Artifacts` failure. Usually a **downstream consequence** of an earlier build failure — flag it as secondary. |
| 10 | **Other** | Anything that doesn't fit the above. Quote the key error lines and make a best-effort diagnosis. |

### Step 4 — Present the diagnosis

For each failure, provide:

1. **Category** and short summary
2. **Root cause** — the specific error message or condition
3. **File and line** (if available from compiler output or stack traces)
4. **Is this a code issue or infra issue?** — clearly distinguish between problems the developer
   can fix and problems that need a pipeline re-run or agent fix.
5. **Suggested action** — what to do next (see table below)

### Suggested Actions by Category

| Category | Suggested Action |
|----------|-----------------|
| C++ Compilation Error | Fix the source file. Check if a dependency header changed. Verify the Velox branch is compatible. |
| C++ Linker Error | Check CMakeLists.txt for missing libraries. Verify vcpkg dependencies. |
| OOM / Resource Exhaustion | Re-run the build (transient) or reduce parallelism (`-j` flag). Check if the agent pool has enough RAM. |
| Process Crash | Check the assertion message. If vcpkg/toolchain crash, it's infra — re-run. If it's in project code, investigate the assertion. |
| Scala / Maven Build Failure | Read the compiler error. Check for API changes in dependencies. |
| Unit Test Failure | Run the failing test locally. Check recent code changes to the test or tested code. |
| Scalastyle / Lint | Run `mvn scalastyle:check` locally and fix violations. |
| Infrastructure / Agent | **Re-run the build.** If persistent, report to the pipeline/infra team. |
| Artifact / Publish | Fix the upstream build failure first; this will resolve automatically. |

### Step 5 — Cross-reference (optional, if user asks)

When deeper investigation is requested:
- Search the repository for the failing source file or test class.
- Check `cpp/compile.sh` and `CMakeLists.txt` for build configuration.
- For Velox compilation errors, check if the Velox submodule branch matches expectations.
- Look at the PR diff (if `build.reason == pullRequest`) to see what changed.

## Multiple Builds

If the user provides multiple build URLs, process them in parallel using sub-agents (one per
build) and present a consolidated view.

---

# Part 2 — Trigger Pipeline Runs

## Workflow for Triggering a Pipeline

### Step 1 — Identify the pipeline

If the user specifies a pipeline by name or ID, resolve it directly. If unclear, list available
pipelines and let the user choose:

```bash
python <skill-dir>/scripts/trigger_pipeline.py list --keyword <keyword>
```

Present the results as a selection table:

| ID | Name | Path | Default Branch |
|----|------|------|----------------|

If there are multiple matches, use `ask_user` to let the user pick one.

### Step 2 — Discover runtime parameters

Fetch the pipeline's runtime parameters:

```bash
python <skill-dir>/scripts/trigger_pipeline.py params --pipeline <id>
```

This reads the pipeline's YAML definition and extracts the `parameters:` block, returning each
parameter's name, displayName, type, default value, and allowed values.

### Step 3 — Ask the user to confirm parameters

**CRITICAL: Always ask the user before triggering a pipeline.** Use the `ask_user` tool to present
a form with the discovered parameters pre-filled with their defaults. The user must be able to:
- See all parameters and their current defaults
- Override any parameter values
- Confirm the branch to build on
- Confirm or cancel the run

Build the `ask_user` form dynamically from the discovered parameters:
- For `string` params with `values` list → use `enum` field type
- For `boolean` params → use `boolean` field type
- For `string` params without `values` → use free-text `string` field type
- For `object` / list params → use `string` field type (user enters JSON)
- Always include a `branch` field (required)
- Set `default` on each field from the pipeline's declared defaults

Example form structure:
```
branch:        string  (required, no default — user must specify)
sparkVersion:  enum    [3.5, 4.0, 4.1] default=4.1
veloxBranch:   string  default=oss_sync_v1.6.0_baremin
enableGlutenUT: boolean default=true
...
```

### Step 4 — Trigger the run

After the user confirms, trigger the pipeline:

```bash
python <skill-dir>/scripts/trigger_pipeline.py run \
    --pipeline <id> \
    --branch <branch> \
    --param key1=value1 \
    --param key2=value2
```

Only pass parameters that the user explicitly changed from defaults, or pass all parameters — both
approaches work. The API accepts `templateParameters` for YAML pipeline parameters.

### Step 5 — Report the result

Show the triggered build info:

| Field | Value |
|-------|-------|
| Build ID | `result.id` |
| Build # | `result.buildNumber` |
| Status | `result.status` |
| Pipeline | `result.pipeline` |
| Branch | `result.sourceBranch` |
| URL | `result.url` (clickable link) |

Offer to monitor the build status by re-running `fetch_build_info.py` periodically.

## Important Notes

- **Never trigger a pipeline without explicit user confirmation.** Always use `ask_user` first.
- Branch names are auto-prefixed with `refs/heads/` if not already prefixed.
- Pipeline names support substring matching — `Buddy-Mariner` will match `Gluten-Buddy-Mariner`.
- If the pipeline name is ambiguous (matches multiple), show all matches and ask the user to pick.
- Parameters of type `object` (e.g. `testShards: [0,1,2,...]`) should be passed as JSON strings.

---

# Common Gluten Pipelines

| Pipeline | ID | Purpose |
|----------|----|---------|
| Gluten-Buddy-Mariner | 27862 | Full build + CPP UT + Scala tests on Mariner OS |
| Gluten-Nightly-Build | 25570 | Nightly integration tests |
| Gluten-PGO-Build | 27886 | PGO-optimized build |
| Gluten-Buddy-PGO-Build | 27888 | Buddy build with PGO |
| Gluten-Nightly-PGO-Build | — | Nightly PGO build |
| Gluten-E2E-Validation | 28814 | End-to-end validation |
| Gluten-Perf-Validation | 27995 | Performance validation |

Build stages typically follow this order:
1. `sdl_sources` — security / compliance scans
2. `scalastyle check` — code style validation
3. `Build Gluten Bundle` — C++ native build + Maven JVM build
4. `Run Gluten CPP UT` — C++ unit tests
5. `Spark X.Y Build and Test` — full Spark integration tests
