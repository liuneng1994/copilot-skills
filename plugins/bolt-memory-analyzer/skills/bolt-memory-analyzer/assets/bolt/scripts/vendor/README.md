Vendored browser assets used by `scripts/bolt_memory_trace_viewer.py`.

These files are embedded into the generated memory trace HTML report so the
interactive flame graph works offline and does not depend on CDN access.

| File | Upstream package | Version | Purpose |
|---|---|---:|---|
| `d3.min.js` | `d3` | 7.9.0 | Browser-side D3 runtime required by the flame graph component. |
| `d3-flamegraph.min.js` | `d3-flame-graph` | 4.1.3 | Interactive flame graph component with hover details, search, and zoom. |

To refresh these assets, download the corresponding `dist/` files from npm or
jsDelivr, then regenerate and browser-test a report with
`scripts/bolt_memory_trace_viewer.py`.
