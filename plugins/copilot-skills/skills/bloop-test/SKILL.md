---
name: bloop-test
description: Run Scala tests using Bloop build server for fast incremental compilation. Use when user asks to run Scala/Spark tests, compile Scala code, or wants faster test iteration. Handles bloop installation, configuration generation, and test execution in the Gluten project.
---

# Bloop Test Runner Skill

Use this skill when the user wants to:
- Run Scala test suites in the Gluten project
- Compile Scala code incrementally (faster than Maven)
- Set up Bloop for the first time

## Quick Reference

**Check if bloop is ready:**
```bash
export JAVA_HOME=/usr/lib/jvm/msopenjdk-17
export PATH="$HOME/.local/share/coursier/bin:$JAVA_HOME/bin:$PATH"
bloop projects 2>/dev/null | head -3
```
- If projects listed → ready, see `run-tests.md`
- If error or empty → see `setup.md`

## Files in this skill

| File | When to read |
|---|---|
| `setup.md` | First-time setup or re-generating config |
| `run-tests.md` | Running tests and compiling (daily use) |
| `troubleshooting.md` | When something goes wrong |
