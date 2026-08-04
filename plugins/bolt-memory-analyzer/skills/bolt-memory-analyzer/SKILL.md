---
name: bolt-memory-analyzer
description: Instrument a Bolt checkout with a configurable high-performance MemoryPool recorder, capture binary v2 memory traces, generate an offline interactive timeline/flame-graph report, and emit bounded JSON findings for memory peaks, long allocation lifetimes, leaks, unmatched lifecycle events, allocation churn, and trace-integrity problems. Use when asked to install or update Bolt memory tracing, run a memory capture, explain a memory peak, inspect retention or leaks, analyze a .bin Bolt memory trace, generate the HTML report, or provide model-oriented memory diagnosis.
---

# Bolt Memory Analyzer

Provide the full Bolt MemoryPool tracing and analysis workflow. Keep tracing
disabled unless `BOLT_MEMORY_TRACE_FILE` is explicitly set.

## Locate Resources

Resolve `<skill>` as this `SKILL.md` directory.

- Installer: `<skill>/scripts/install_into_bolt.py`
- Installation verifier: `<skill>/scripts/verify_installation.py`
- Integration patch: `<skill>/references/bolt-integration.patch`
- Complete bundled code: `<skill>/assets/bolt/`
- Protocol reference:
  `<skill>/assets/bolt/scripts/bolt_memory_trace_format.md`

## Install Into Bolt

1. Confirm the target is a Bolt checkout.
2. Inspect `git status`; never overwrite unrelated user changes.
3. Dry-run the installer:

```bash
python3 <skill>/scripts/install_into_bolt.py \
  --repo <bolt-checkout> \
  --dry-run
```

4. If the patch applies and bundled destinations are absent or identical,
   install:

```bash
python3 <skill>/scripts/install_into_bolt.py --repo <bolt-checkout>
python3 <skill>/scripts/verify_installation.py --repo <bolt-checkout>
```

Do not use `--force` unless the user explicitly authorizes replacing differing
tool files. If the integration patch does not apply, read
`references/bolt-integration.patch` and adapt only the MemoryPool hook and CMake
source registration to the checkout's current structure.

## Build

Use the checkout's existing build system. A focused validation is:

```bash
cmake --build <bolt-checkout>/_build/Release --target bolt_memory -j2
```

Rebuild the exact binary that will be profiled so the recorder object is linked.
Confirm with:

```bash
strings <binary> | rg 'BOLT_MEMORY_TRACE_FILE|BLTMEM2'
```

## Capture

Run any Bolt binary with:

```bash
env \
  BOLT_MEMORY_TRACE_FILE=/tmp/bolt-memory.bin \
  BOLT_MEMORY_TRACE_STACK_MIN_BYTES=4096 \
  BOLT_MEMORY_TRACE_BUFFER_BYTES=1048576 \
  BOLT_MEMORY_TRACE_CHECKPOINT_EVENTS=65536 \
  <binary> <arguments>
```

Optional controls:

- `BOLT_MEMORY_TRACE_POOL_REGEX`: record matching MemoryPool names only.
- `BOLT_MEMORY_TRACE_STACKS=0`: disable stack collection.
- `BOLT_MEMORY_TRACE_STACK_MIN_BYTES`: capture stacks only for allocations at
  or above this size.

The recorder writes binary v2, interns pools and raw stacks, records executable
mappings, and symbolizes offline. Preserve the profiled binaries until analysis
finishes; otherwise frames fall back to `module+offset`.

## Analyze For A Model

Prefer compact JSON:

```bash
python3 <bolt-checkout>/scripts/bolt_memory_analyze.py \
  /tmp/bolt-memory.bin \
  --format json \
  --top 5 \
  --max-frames 8
```

Use these sections in order:

1. `assessment`: severity, confidence, and finding count.
2. `findings`: actionable conclusions and evidence references.
3. `peak`: exact peak-live attribution, peak-growth sources, release latency.
4. `lifetimes`: percentiles and suspicious retained allocations.
5. `churn`: temporary-allocation pressure and dominant stacks.
6. `trace.integrity_errors`: qualify all conclusions when non-empty.

Never call pool concentration a leak by itself. Treat `live-at-end`,
`long-lifetime`, `unmatched-free`, and slow post-peak release as retention
signals. Adjust thresholds with CLI flags when workload duration or expected
buffer ownership is known. Use `--fail-on warning` or `--fail-on critical` only
for CI policy.

## Generate Interactive Report

```bash
python3 <bolt-checkout>/scripts/bolt_memory_trace_viewer.py \
  /tmp/bolt-memory.bin \
  -o ./bolt-memory-report.html
```

Open the self-contained HTML. Click the timeline for allocations live at one
point; drag a range to update KPIs, tables, and flame graph for allocations
overlapping that interval. Verify the report with a headless browser when one is
available.

## Validate The Bundled Tool

Run:

```bash
python3 -m unittest -v \
  <bolt-checkout>/scripts/tests/test_bolt_memory_analyze.py
```

The tests cover leaks, long lifetimes, allocation churn, truncated traces, and
grow-aware peak replay.
