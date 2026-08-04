---
name: bolt-memory-analyzer
description: Instrument a Bolt checkout with a configurable high-performance MemoryPool recorder, convert large binary v2 captures to standard Perfetto TrackEvent and native heap traces, query them with Trace Processor SQL, serve them to Perfetto UI, and emit bounded JSON findings for memory peaks, long allocation lifetimes, leaks, unmatched lifecycle events, and churn. Use when asked to install or update Bolt memory tracing, migrate memory analysis to Perfetto, process a large trace with native indexing, run a memory capture, explain a memory peak, inspect retention or leaks, analyze a .bin or .perfetto-trace file, or provide model-oriented memory diagnosis.
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

## Prepare Large Traces With Perfetto

Use Perfetto for large traces. Native Trace Processor parses and indexes them,
and Perfetto UI queries visible ranges.

```bash
python3 <bolt-checkout>/scripts/bolt_memory_perfetto.py prepare \
  /tmp/bolt-memory.bin \
  -o /tmp/bolt-memory.perfetto-trace
```

This downloads the official Trace Processor into a local cache, converts the
trace, runs native SQL analysis, and prints the next `serve` command.

For an existing Perfetto trace:

```bash
python3 <bolt-checkout>/scripts/bolt_memory_perfetto.py analyze \
  /tmp/bolt-memory.perfetto-trace \
  --trace-processor <trace_processor_shell>
```

Use these JSON sections in order:

1. `assessment`: severity, confidence, and finding count.
2. `findings`: actionable conclusions and evidence references.
3. `peak`: exact peak-live attribution, peak-growth sources, release latency.
4. `lifetimes`: percentiles and suspicious retained allocations.
5. `churn`: temporary-allocation pressure and dominant stacks.
6. `trace.integrity_errors`: qualify all conclusions when non-empty.

Never call pool concentration a leak by itself. Treat `live-at-end`,
`long-lifetime`, `unmatched-free`, and slow post-peak release as retention
signals. Adjust thresholds with CLI flags when workload duration or expected
buffer ownership is known.

## Open Perfetto UI

```bash
python3 <bolt-checkout>/scripts/bolt_memory_perfetto.py serve \
  /tmp/bolt-memory.perfetto-trace \
  --trace-processor <trace_processor_shell>
```

Open `https://ui.perfetto.dev` and accept the native acceleration prompt. The
local Trace Processor serves the already-loaded trace at
`http://127.0.0.1:9001`.

The Perfetto trace contains exact alloc/free/grow TrackEvents, active-memory
counter tracks, interned mappings/callstacks, and periodic/peak/final native
heap snapshots.

## Install Arbitrary-Range Flamegraphs

The stock heap-profile view only opens recorded snapshots. Install the bundled
`dev.bolt.Memory` UI plugin to compute flamegraphs for any area selection.
Check the Trace Processor version first and use the matching Perfetto `vX.Y`
tag; a mismatched UI may redirect to an official build without the Bolt plugin.

```bash
<trace_processor_shell> --version
git clone https://github.com/google/perfetto.git /tmp/perfetto
git -C /tmp/perfetto checkout vX.Y
python3 <bolt-checkout>/scripts/bolt_memory_perfetto.py install-ui-plugin \
  --perfetto /tmp/perfetto
/tmp/perfetto/tools/install-build-deps --ui
python3 <bolt-checkout>/scripts/bolt_memory_perfetto.py build-ui \
  --perfetto /tmp/perfetto
python3 <bolt-checkout>/scripts/bolt_memory_perfetto.py serve-ui \
  --perfetto /tmp/perfetto
```

Open `http://localhost:10000`, load the trace, and drag any timeline area.
Select the `Bolt Memory Flamegraph` tab. It offers live bytes at the range end,
allocated bytes, allocation count, and byte-seconds overlapping the range.

## Validate The Bundled Tool

Run:

```bash
TRACE_PROCESSOR=<trace_processor_shell> \
  python3 <bolt-checkout>/scripts/tests/test_bolt_memory_analyze.py
```

Set `TRACE_PROCESSOR=<trace_processor_shell>` to include Perfetto conversion and
native SQL integration tests. Tests cover leaks, long lifetimes, allocation
churn, truncated traces, grow-aware peak replay, peak conservation, and
Perfetto findings.
