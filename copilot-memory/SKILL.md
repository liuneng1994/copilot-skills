---
name: copilot-memory
description: Read and maintain layered memory for multi-session coding tasks. Use this skill before complex work to recall repo facts, user preferences, and past reflections, then write back verified facts and lessons after meaningful task milestones.
---

# Copilot Memory Skill

Use this skill when:

- the task spans multiple sessions
- repository conventions matter
- the user has stable preferences worth remembering
- previous failures or findings should not be repeated

This skill implements a layered memory model:

- `thread`: short-lived task state for the current conversation or issue
- `repo`: repository facts, commands, architecture notes, and known pitfalls
- `user`: stable user preferences and workflow habits
- `reflection`: validated lessons, failed paths, and follow-up guidance

## Safety rules

Never store:

- secrets, tokens, passwords, private keys, or customer data
- raw copyrighted source dumps
- speculative claims that were not verified
- noisy logs unless distilled into a short verified conclusion

Treat `repo` memory as repo-scoped by default. Do not reuse it across repositories unless the user explicitly asks for shared memory.

## Memory lifecycle

### 1. Read memory before planning

Before long-running or multi-step work:

1. Identify the memory namespace.
   - Repo work: use a namespace derived from the repo name or repo root.
   - User preferences: use a stable user namespace.
   - Thread work: use the active issue, PR, or session identifier.
2. Read relevant memories from the most local scope outward:
   - `thread`
   - `repo`
   - `user`
   - `reflection`
3. Inject only the compact memory block into reasoning. Do not paste the whole database output into the prompt.

Example:

```bash
python ./scripts/memory_read.py \
  --namespace "repo:owner/repo" \
  --repo "owner/repo" \
  --thread-id "issue-123" \
  --user-id "default" \
  --scope thread \
  --scope repo \
  --scope user \
  --scope reflection \
  --query "fix flaky integration tests for parquet writer" \
  --limit 8
```

### 2. Write memory only when it is durable

Write memory only for high-value items such as:

- verified build or test commands
- stable repo conventions
- durable user preferences
- architecture facts confirmed from code or docs
- failed approaches that should not be retried
- decisions with clear rationale

Example:

```bash
python ./scripts/memory_write.py \
  --namespace "repo:owner/repo" \
  --repo "owner/repo" \
  --thread-id "issue-123" \
  --scope repo \
  --kind workflow \
  --subject "integration-tests" \
  --summary "Run parquet integration tests from the repo root with the custom profile enabled." \
  --content "Verified on 2026-03-12: use ./gradlew :parquet:integrationTest -PenableCustomProfile from the repo root." \
  --tags "tests,gradle,parquet" \
  --confidence 0.92 \
  --value-score 0.88 \
  --source "copilot"
```

Write reflections after meaningful failures:

```bash
python ./scripts/memory_write.py \
  --namespace "repo:owner/repo" \
  --repo "owner/repo" \
  --thread-id "issue-123" \
  --scope reflection \
  --kind failure \
  --subject "parquet-schema-mismatch" \
  --summary "Do not patch the writer before regenerating the schema fixture." \
  --content "Attempting to patch the writer first caused repeated fixture mismatches. Regenerate the schema fixture before re-running the writer tests." \
  --tags "failure,tests,fixtures" \
  --confidence 0.84 \
  --value-score 0.86 \
  --source "copilot"
```

### 3. Compact memory periodically

Run compaction after large tasks or periodically to archive stale low-value memory:

```bash
python ./scripts/memory_compact.py \
  --namespace "repo:owner/repo" \
  --repo "owner/repo"
```

## Retrieval policy

Use these rules when memories conflict:

1. Prefer `active` over archived or superseded memory.
2. Prefer higher confidence.
3. Prefer fresher memory.
4. Prefer more local scope (`thread` > `repo` > `user` > `reflection`) when relevance is similar.
5. If conflict remains, surface the conflict briefly instead of silently picking one.

## Recommended invocation pattern

1. Read memory.
2. Plan and execute the task.
3. Write back at most a handful of durable memories.
4. Compact only when enough new memory was created or a task closed out.

## Files in this skill

- `scripts/schema.sql`: SQLite schema with FTS5 support
- `scripts/memory_lib.py`: shared helpers
- `scripts/memory_read.py`: query and rank memories
- `scripts/memory_write.py`: validate and upsert memories
- `scripts/memory_compact.py`: archive stale memory
- `memory_policy.yaml`: policy and retention defaults
- `templates/memory_prompt.md`: compact prompt block format
