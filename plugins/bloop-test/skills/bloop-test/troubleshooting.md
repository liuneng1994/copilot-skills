# Bloop Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `sun.nio.ch.DirectBuffer not found` | Bloop server running on JDK 21 | `bloop exit`, set `JAVA_HOME=/usr/lib/jvm/msopenjdk-17`, run `bloop about` |
| `MemoryUsageStats not found` | Protobuf generated Java missing from bloop sources | Re-run `mvn generate-sources` + patch script (steps 5-7 in setup.md) |
| `putAllColumnDefaultValues not found` | Same — proto Java code is stale | Same fix as above |
| `Failed to initialize MemoryUtil` / Arrow `--add-opens` | Test JVM missing `--add-opens` flags | Re-run step 7c in setup.md to patch test configs |
| `UnsatisfiedLinkError: undefined symbol` | Native libs out of sync with Java code | Rebuild C++ and sync libs (see run-tests.md pre-flight) |
| `No bloop server running` | Server not started | `bloop about` (auto-starts) |
| `Project not found` | Config not generated or stale | `bloop projects` to check, re-run setup if needed |
| Stale compilation / wrong results | Zinc cache stale | `bloop clean <project>` then `bloop compile <project>` |
| ADO feed 401 during config generation | Maven auth not set or token expired | `python3 /root/scripts/m2-azure-bearer.py inject` to inject a fresh ADO bearer token into `~/.m2/settings.xml` |
| `-release does not accept multiple arguments` | `-release:17` in scalac options | Re-run patch script (step 7b in setup.md) |
| Tests hang / no output | Bloop server OOM or stuck | `bloop exit`, increase heap in `~/.bloop/bloop.json` (`-Xmx`), restart |
| Bloop config out of sync with POM | Module/dep added, scope changed, or `./dev/build-nee.sh --gluten-java --clean` run | Run `./dev/build-nee.sh --gluten-java` to regenerate Maven state, then re-run setup.md steps 5-7 |
| `NoClassDefFoundError: com/azure/storage/blob/BlobServiceClientBuilder` (or `azure-shuffle-blob` / Fabric `InstrumentedExternalCatalog`) on suite teardown | `ColumnarShuffleManager.stop()` calls into RSM unconditionally; bloop does not propagate the `-Prsm` transitive jars | RSM tests are not supported via bloop — run them via Maven (`./dev/build-nee.sh --ci` + `mvn test -Prsm`). See run-tests.md "RSM Tests" |
| `NoClassDefFoundError` on shaded/relocated classes (e.g. relocated Guava, Netty) | Maven shade plugin doesn't run during bloop install | Fall back to `mvn test -pl <module>` for suites that hit shaded classes |
| ADO 401 / `Could not transfer artifact ... from BBC-VHD_PublicPackages` mid-build | Bearer token expired (validity ~1h locally) | Re-inject: `python3 /root/scripts/m2-azure-bearer.py inject` then retry the failing `mvn` |
| `Could not find or load main class ch.epfl.scala.bloop` during install | Wrong settings file or auth not set | Use `mvn -s ~/.m2/settings.xml ...`, ensure `python3 /root/scripts/m2-azure-bearer.py inject` has populated `<servers>` |

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
rm -rf "$GLUTEN_HOME/.bloop"
# Then re-run full setup from setup.md
```

## Reference: where canonical build values live

| Setting | Authoritative source |
|---|---|
| `JAVA_HOME` for tests | `.pipelines/Templates/Spark41Variables.yml` (`Spark41JavaHome`) |
| Maven profiles for Spark 4.1 | `dev/lib/build-gluten-java.sh` (`_setup_java_profiles`) |
| Maven settings file (local) | `~/.m2/settings.xml` (created via `setup.md` step 4) |
| Maven settings file (CI) | `$GLUTEN_HOME/.pipelines/conf/settings.xml` |
| Conda env path | `Spark41Variables.yml` (`CONDA_PYTHON_ENV_PATH`) → `$GLUTEN_HOME/ep/_ep/py313` |
| Native lib output dir | `$GLUTEN_HOME/cpp/build/releases/` |

If the skill instructions ever drift from these, **trust the sources above**
and update the skill via `~/.copilot/skills-repo/bloop-test/`.
