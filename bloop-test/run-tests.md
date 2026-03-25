# Running Tests with Bloop

## Environment (run once per shell session)

```bash
export JAVA_HOME=/usr/lib/jvm/msopenjdk-17
export PATH="$HOME/.local/share/coursier/bin:$JAVA_HOME/bin:$PATH"
```

## Pre-flight: Sync native libs to classpath

Bloop loads `libgluten.so`, `libvelox.so` and other native libs from the classpath at runtime.
After any C++ rebuild, sync them before running tests:

```bash
cd /root/gluten

# Rebuild C++ if sources changed
GLUTEN_LIB=$(find cpp/build/releases -name "libgluten.so" 2>/dev/null | head -1)
VELOX_LIB=$(find cpp/build/releases -name "libvelox.so" 2>/dev/null | head -1)
if [ -z "$GLUTEN_LIB" ] || [ -z "$VELOX_LIB" ]; then
  echo "C++ libs missing, need full build (see setup.md)"
elif [ "$(find cpp/velox -name '*.cc' -o -name '*.h' -newer "$GLUTEN_LIB" 2>/dev/null | head -1)" ]; then
  echo "C++ sources changed, rebuilding..."
  source dev/vcpkg/env.sh 2>/dev/null
  make -C cpp/build -j$(nproc)
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

Additional env vars required:

```bash
export SPARK_HOME=/root/wildfire-spark
export PYSPARK_PYTHON=/root/miniconda3/envs/py313/bin/python3.13
export PYSPARK_DRIVER_PYTHON=/root/miniconda3/envs/py313/bin/python3.13
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
| `gluten-ut/spark41` | `gluten-ut-velox-spark41` | `gluten-ut-velox-spark41-test` |
| `gluten-ut/common` | `gluten-ut-velox-common` | `gluten-ut-velox-common-test` |

**Note:** `gluten-ut` modules require `-Pspark-ut` profile during setup (steps 5-6).
The bloop project names use the Maven artifactId, which may differ from the directory name.
Run `bloop projects | grep ut` to find exact names after setup.

## Gluten-UT (Spark Unit Tests)

Gluten-UT wraps upstream Spark test suites with Gluten-specific overrides.
These require the `spark-ut` profile during bloop setup.

```bash
# Example: run GlutenPythonUDFSuite for Spark 4.1
bloop test gluten-ut-velox-spark41-test \
  --only "org.apache.spark.sql.execution.python.GlutenPythonUDFSuite" -- \
  -Djava.io.tmpdir=/tmp \
  -Djava.library.path=/root/gluten/cpp/build/releases \
  -Dspark.test.home=$SPARK_HOME
```

## Tips

- `bloop compile <project> -w` for continuous compilation during development
- `bloop clean <project>` to force full recompile
- `bloop projects` to list all available projects
- Bloop caches compilation state — incremental builds are near-instant
