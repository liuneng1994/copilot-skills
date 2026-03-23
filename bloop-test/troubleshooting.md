# Bloop Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `sun.nio.ch.DirectBuffer not found` | Bloop server running on JDK 21 | `bloop exit`, set `JAVA_HOME=/usr/lib/jvm/msopenjdk-17`, run `bloop about` |
| `MemoryUsageStats not found` | Protobuf generated Java missing from bloop sources | Re-run `mvn generate-sources` + patch script (steps 5-7 in setup.md) |
| `putAllColumnDefaultValues not found` | Same — proto Java code is stale | Same fix as above |
| `No bloop server running` | Server not started | `bloop about` (auto-starts) |
| `Project not found` | Config not generated or stale | `bloop projects` to check, re-run setup if needed |
| Stale compilation / wrong results | Zinc cache stale | `bloop clean <project>` then `bloop compile <project>` |
| ADO feed 401 during config generation | Maven auth not set | Export `MSDATA_USER` and `MSDATA_KEY` (see setup.md step 4) |
| `-release does not accept multiple arguments` | `-release:17` in scalac options | Re-run patch script (step 7 in setup.md) |
| Tests hang / no output | Bloop server OOM or stuck | `bloop exit`, increase heap in `~/.bloop/bloop.json` (`-Xmx`), restart |

## Verify Environment

```bash
# Check JDK version (must be 17)
$JAVA_HOME/bin/java -version

# Check bloop server JDK
ps aux | grep bloop | grep -o "msopenjdk-[0-9]*"

# Check bloop is responsive
bloop about

# Check projects are loaded
bloop projects | wc -l
```

## Reset Everything

```bash
bloop exit 2>/dev/null
rm -rf /root/gluten/.bloop
# Then re-run full setup from setup.md
```
