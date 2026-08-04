# Running Tests with Bloop

## Environment (run once per shell session)

```bash
# JDK 17 -- bloop server must run on JDK 17 (see setup.md)
# Value comes from .pipelines/Templates/Spark41Variables.yml:22 (Spark41JavaHome)
export JAVA_HOME=/usr/lib/jvm/msopenjdk-17
export PATH="$HOME/.local/share/coursier/bin:$JAVA_HOME/bin:$PATH"

# Project root -- adjust if your checkout is elsewhere
export GLUTEN_HOME="${GLUTEN_HOME:-$HOME/gluten}"
cd "$GLUTEN_HOME"
```

The full env (CONDA_*, MAVEN_OPTS, VELOX_HOME, CCACHE_DIR, etc.) is set by
`init_env` in `dev/lib/build-common.sh` whenever you invoke `./dev/build-nee.sh`.
Bloop testing only needs the three vars above plus, for Python UDF suites,
`SPARK_HOME` and `CONDA_PYTHON_ENV_PATH` (see Python UDF section below).
**Do not `source build-common.sh` directly** — `init_env` calls `exit 1` on
config failure, which would terminate your interactive shell.

## Pre-flight: Sync native libs to classpath

Bloop loads `libgluten.so`, `libvelox.so` and other native libs from the classpath at runtime.
After any C++ rebuild, sync them before running tests.

```bash
cd "$GLUTEN_HOME"

# Rebuild C++ if sources changed. The unified build script handles vcpkg, ccache,
# jemalloc, PGO, ASan, generator selection (Make/Ninja), and per-module timing --
# always prefer it over invoking make/cmake directly.
GLUTEN_LIB=$(find cpp/build/releases -name "libgluten.so" 2>/dev/null | head -1)
VELOX_LIB=$(find cpp/build/releases -name "libvelox.so" 2>/dev/null | head -1)
if [ -z "$GLUTEN_LIB" ] || [ -z "$VELOX_LIB" ]; then
  echo "C++ libs missing -- run: ./dev/build-nee.sh --all   (see setup.md prerequisite)"
elif [ "$(find cpp/velox -name '*.cc' -o -name '*.h' -newer "$GLUTEN_LIB" 2>/dev/null | head -1)" ]; then
  echo "C++ sources changed, rebuilding via build-nee.sh (incremental)..."
  ./dev/build-nee.sh --gluten
fi

# Sync ALL native libs to both classpath directories
# (bloop uses bloop-bsp-clients-classes, maven uses target/scala-2.13/classes)
for dir in \
  backends-velox/target/scala-2.13/classes/linux/amd64 \
  backends-velox/target/bloop-bsp-clients-classes/classes-bloop-cli/linux/amd64; do
  mkdir -p "$dir"
  cp cpp/build/releases/libgluten.so "$dir/"
  cp cpp/build/releases/libvelox.so "$dir/"
  # Also sync any existing MSFT native libs (e.g. libolccppextensions.so)
  if [ -d backends-velox/target/scala-2.13/classes/linux/amd64 ] && [ "$dir" != "backends-velox/target/scala-2.13/classes/linux/amd64" ]; then
    cp backends-velox/target/scala-2.13/classes/linux/amd64/*.so "$dir/" 2>/dev/null
  fi
done
echo "Native libs synced to classpath"
```

## Compile

```bash
bloop compile backends-velox          # Compile a module
bloop compile backends-velox -w       # Watch mode (auto-recompile on save)
bloop compile gluten-core             # Compile core
```

## Run Tests

```bash
# All tests in a module
bloop test backends-velox-test

# Specific test suite (fully qualified class name)
bloop test backends-velox-test \
  -o org.apache.spark.sql.execution.python.VeloxNativePythonUDFSuite

# Specific test method
bloop test backends-velox-test \
  -o org.apache.spark.sql.execution.python.VeloxNativePythonUDFSuite \
  -- -t "deterministic python udf"

# Pattern matching
bloop test backends-velox-test -o '*PythonUDF*'

# Multiple suites
bloop test backends-velox-test \
  -o org.apache.spark.sql.execution.python.VeloxNativePythonUDFSuite \
  -o org.apache.spark.sql.execution.python.VeloxNativePythonUDFEvalSuite
```

## Python UDF Tests

These tests need a Python interpreter and PySpark wired up. Prefer the env vars
defined in `.pipelines/Templates/Spark41Variables.yml` so paths track project
config; only hardcode when the unified build hasn't run on this machine yet.

```bash
# Canonical values (from Spark41Variables.yml lines 22, 52-56):
#   CONDA_PYTHON_VERSION  = 3.13
#   CONDA_PYTHON_ENV_PATH = $(GLUTEN_HOME)/ep/_ep/py313
#   SPARK_HOME            = (empty in CI; locally point at your wildfire-spark checkout)
export SPARK_HOME="${SPARK_HOME:-$HOME/wildfire-spark}"
export CONDA_PYTHON_ENV_PATH="${CONDA_PYTHON_ENV_PATH:-$GLUTEN_HOME/ep/_ep/py313}"
export PYSPARK_PYTHON="${CONDA_PYTHON_ENV_PATH}/bin/python3.13"
export PYSPARK_DRIVER_PYTHON="$PYSPARK_PYTHON"
```

To check the canonical values in use:

```bash
grep -E "Spark41JavaHome|CONDA_|SPARK_HOME" .pipelines/Templates/Spark41Variables.yml
```

## Module Name Mapping

| Maven `-pl` path | Bloop compile | Bloop test |
|---|---|---|
| `backends-velox` | `backends-velox` | `backends-velox-test` |
| `gluten-core` | `gluten-core` | `gluten-core-test` |
| `gluten-substrait` | `gluten-substrait` | `gluten-substrait-test` |
| `gluten-ras/common` | `gluten-ras-common` | `gluten-ras-common-test` |
| `gluten-ras/planner` | `gluten-ras-planner` | `gluten-ras-planner-test` |
| `gluten-delta` | `gluten-delta` | `gluten-delta-test` |
| `gluten-arrow` | `gluten-arrow` | `gluten-arrow-test` |

**Note:** The bloop project names use the Maven artifactId, which may differ
from the directory name. Run `bloop projects | grep <hint>` to find exact names
after setup. Modules requiring the `-Pspark-ut` profile (gluten-ut*) are not
supported by this skill — use Maven for those.

## Remote Shuffle Manager (RSM) Tests — NOT supported by bloop

RSM tests live in `azure-shuffle-blob/` and `ColumnarShuffleManager.stop()` calls
into RSM unconditionally. `bloopInstall` does not propagate the transitive
azure-storage-blob / Fabric `InstrumentedExternalCatalog` jars across module
boundaries, so even non-RSM suites in `backends-velox-test` will abort in suite
teardown with `NoClassDefFoundError: com/azure/storage/blob/BlobServiceClientBuilder`
once `-Prsm` is in the build.

Run RSM (and any suite that transitively touches `RemoteShuffleManagerGluten`) via
Maven:

```bash
cd $GLUTEN_HOME
./dev/build-nee.sh --ci   # Builds with -Prsm
mvn -s ~/.m2/settings.xml test \
  -pl backends-velox \
  -Pspark-4.1,scala-2.13,backends-velox,delta,rsm \
  -Dtest='*RemoteShuffle*' -DfailIfNoTests=false
```

## Tips

- `bloop compile <project> -w` for continuous compilation during development
- `bloop clean <project>` to force full recompile
- `bloop projects` to list all available projects
- Bloop caches compilation state — incremental builds are near-instant
- After `./dev/build-nee.sh --gluten-java --clean` POM/classpath may have changed;
  re-run bloop setup to regenerate `.bloop/*.json`
- When `mvn` returns 401 from ADO feeds, re-inject the bearer token:
  `python3 /root/scripts/m2-azure-bearer.py inject`
