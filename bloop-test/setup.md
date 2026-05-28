# Bloop Setup (One-Time)

## Prerequisite

Bloop only does **incremental Scala compilation** — it does **not** run Maven plugins
(protobuf, shade, antlr, etc.) and it does not build the native C++ side. Before bloop
can work, the project must have already been built once via Maven so that:

- `target/generated-sources/protobuf/java/` exists for every module that has `.proto` files
- The native `cpp/build/releases/lib{gluten,velox}.so` libraries exist
- The `~/.m2/repository/org/apache/gluten/...` artifacts are installed

The canonical entry point is the unified build script (handles M2 protection, CI mode,
PGO, ASan, Conda, jemalloc, etc.):

```bash
cd /root/gluten
./dev/build-nee.sh --all          # First-time: build Velox + C++ + Java
./dev/build-nee.sh --gluten-java  # Incremental Java rebuild only
./dev/build-nee.sh --gluten       # Incremental C++ rebuild only
```

Once `./dev/build-nee.sh --all` has succeeded at least once, proceed with the bloop
setup below.

## Run setup

Run this complete block to install bloop, generate config, and patch for Gluten:

```bash
# === 1. Environment (MUST use JDK 17, not 21) ===
export JAVA_HOME=/usr/lib/jvm/msopenjdk-17
export PATH="$HOME/.local/share/coursier/bin:$JAVA_HOME/bin:$PATH"

# === 2. Install Bloop (skip if already installed) ===
if ! command -v bloop &>/dev/null; then
  cs install bloop
fi

# === 3. Restart Bloop server with JDK 17 ===
# CRITICAL: Bloop server MUST run on JDK 17. JDK 21 causes
# "sun.nio.ch.DirectBuffer not found" errors due to module restrictions.
bloop exit 2>/dev/null; sleep 2
bloop about

# === 4. Maven auth for ADO feeds ===
M2_SETTINGS="/root/.m2/settings.xml"
PAT=$(xmlstarlet sel -N x="http://maven.apache.org/SETTINGS/1.0.0" \
  -t -v "//x:server[x:id='SynapseMaven']/x:password" "$M2_SETTINGS" \
  | tr -d '\n' | tr -d '\r')
export MSDATA_USER="msdata"
export MSDATA_KEY="$PAT"

# === 5. Generate protobuf sources FIRST (bloop can't run Maven plugins) ===
#
# Maven profile notes:
#   -Pspark-4.1 -Pscala-2.13 -Pbackends-velox -Pdelta -- baseline (matches dev/lib/build-gluten-java.sh)
#   -Pspark-ut                                        -- required for gluten-ut test compilation
#   -Pjava-17                                         -- OPTIONAL; auto-activated when JAVA_HOME points to JDK 17
#   -Prsm                                             -- add for remote-shuffle (RSM) tests, matches build-nee.sh --ci
#
# Settings file notes:
#   ~/.m2/settings.xml             -- default for local dev (matches build-nee.sh local mode)
#   .pipelines/conf/settings.xml   -- CI settings (matches build-nee.sh --ci)
cd /root/gluten
mvn -s ~/.m2/settings.xml generate-sources \
  -Pspark-4.1,scala-2.13,backends-velox,delta,spark-ut \
  -DskipTests -Dspotless.check.skip=true -Dscalastyle.skip=true

# === 6. Generate Bloop config from Maven POM ===
mvn -s ~/.m2/settings.xml \
  ch.epfl.scala:bloop-maven-plugin:2.0.3:bloopInstall \
  -Pspark-4.1,scala-2.13,backends-velox,delta,spark-ut \
  -DskipTests -Dspotless.check.skip=true -Dscalastyle.skip=true

# === 7. Patch Bloop configs ===
python3 -c "
import json, glob, os

for f in glob.glob('.bloop/*.json'):
    with open(f) as fh:
        d = json.load(fh)
    modified = False

    # 7a. Add protobuf generated-sources to bloop source paths
    srcs = d['project']['sources']
    for src in list(srcs):
        base = os.path.dirname(src)
        module_dir = os.path.dirname(os.path.dirname(base))
        gen_dir = os.path.join(module_dir, 'target/generated-sources/protobuf/java')
        if os.path.isdir(gen_dir) and gen_dir not in srcs:
            srcs.append(gen_dir)
            modified = True
            break

    # 7b. Remove -release scalac option (incompatible with bloop Zinc)
    scala = d['project'].get('scala', {})
    opts = scala.get('options', [])
    new_opts = [o for o in opts if o not in ('-release:17', '-release')]
    if len(new_opts) != len(opts):
        scala['options'] = new_opts
        modified = True

    if modified:
        with open(f, 'w') as fh:
            json.dump(d, fh, indent=4)

# 7c. Add JVM test options to all *-test.json configs
#     (matches Maven's extraJavaTestArgs + scalatest systemProperties)
jvm_opts = [
    '-XX:+IgnoreUnrecognizedVMOptions',
    '--add-opens=java.base/java.lang=ALL-UNNAMED',
    '--add-opens=java.base/java.lang.invoke=ALL-UNNAMED',
    '--add-opens=java.base/java.lang.reflect=ALL-UNNAMED',
    '--add-opens=java.base/java.io=ALL-UNNAMED',
    '--add-opens=java.base/java.net=ALL-UNNAMED',
    '--add-opens=java.base/java.nio=ALL-UNNAMED',
    '--add-opens=java.base/java.time=ALL-UNNAMED',
    '--add-opens=java.base/java.util=ALL-UNNAMED',
    '--add-opens=java.base/java.util.concurrent=ALL-UNNAMED',
    '--add-opens=java.base/java.util.concurrent.atomic=ALL-UNNAMED',
    '--add-opens=java.base/jdk.internal.ref=ALL-UNNAMED',
    '--add-opens=java.base/sun.nio.ch=ALL-UNNAMED',
    '--add-opens=java.base/sun.nio.cs=ALL-UNNAMED',
    '--add-opens=java.base/sun.security.action=ALL-UNNAMED',
    '--add-opens=java.base/sun.util.calendar=ALL-UNNAMED',
    '-Djdk.reflect.useDirectMethodHandle=false',
    '-Dio.netty.tryReflectionSetAccessible=true',
    '-Dlog4j.configurationFile=file:src/test/resources/log4j2.properties',
    '-Dspark.testing=true',
    '-Xmx4g',
]
for f in glob.glob('.bloop/*-test.json'):
    with open(f) as fh:
        d = json.load(fh)
    platform = d['project'].setdefault('platform', {})
    platform['name'] = 'jvm'
    config = platform.setdefault('config', {})
    config['options'] = jvm_opts
    with open(f, 'w') as fh:
        json.dump(d, fh, indent=4)

# 7d. Add velox UDF lib path for backends-velox-test
f = '.bloop/backends-velox-test.json'
with open(f) as fh:
    d = json.load(fh)
opts = d['project']['platform']['config']['options']
udf_opt = '-Dvelox.udf.lib.path=../cpp/build//velox/udf/examples/libmyudf.so,../cpp/build//velox/udf/examples/libmyudaf.so'
if udf_opt not in opts:
    opts.append(udf_opt)
with open(f, 'w') as fh:
    json.dump(d, fh, indent=4)
"

# === 8. Verify ===
bloop projects | head -5
bloop compile gluten-ras-common  # Quick smoke test
echo "=== Bloop setup complete ==="
```

## When to Re-run Setup

- Switching Spark versions (e.g., 3.5 → 4.1)
- Adding/removing Maven modules or dependencies
- Proto files changed (re-run steps 5-7)
- After `git checkout` to a different branch with different POM
- After running `./dev/build-nee.sh --gluten-java --clean` (POM regeneration may change classpath)
