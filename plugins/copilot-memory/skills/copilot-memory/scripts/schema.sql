PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK(scope IN ('thread', 'repo', 'user', 'reflection')),
    namespace TEXT NOT NULL,
    repo TEXT,
    thread_id TEXT,
    user_id TEXT,
    kind TEXT NOT NULL CHECK(kind IN ('fact', 'preference', 'decision', 'failure', 'workflow', 'summary', 'constraint')),
    subject TEXT,
    summary TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.5 CHECK(confidence >= 0 AND confidence <= 1),
    value_score REAL NOT NULL DEFAULT 0.5 CHECK(value_score >= 0 AND value_score <= 1),
    source TEXT NOT NULL DEFAULT 'manual',
    source_ref TEXT,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived', 'superseded', 'deleted')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_accessed_at TEXT,
    access_count INTEGER NOT NULL DEFAULT 0,
    superseded_by TEXT REFERENCES memories(id),
    expires_at TEXT,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_namespace_fingerprint_active
ON memories(namespace, fingerprint)
WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_memories_scope_lookup
ON memories(namespace, scope, status, kind, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_memories_repo_lookup
ON memories(repo, scope, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_memories_thread_lookup
ON memories(thread_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_memories_user_lookup
ON memories(user_id, status, updated_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    summary,
    content,
    subject,
    tags,
    content='memories',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS memories_ai
AFTER INSERT ON memories
BEGIN
    INSERT INTO memory_fts(rowid, summary, content, subject, tags)
    VALUES (new.rowid, new.summary, new.content, COALESCE(new.subject, ''), new.tags);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad
AFTER DELETE ON memories
BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, summary, content, subject, tags)
    VALUES ('delete', old.rowid, old.summary, old.content, COALESCE(old.subject, ''), old.tags);
END;

CREATE TRIGGER IF NOT EXISTS memories_au
AFTER UPDATE ON memories
BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, summary, content, subject, tags)
    VALUES ('delete', old.rowid, old.summary, old.content, COALESCE(old.subject, ''), old.tags);
    INSERT INTO memory_fts(rowid, summary, content, subject, tags)
    VALUES (new.rowid, new.summary, new.content, COALESCE(new.subject, ''), new.tags);
END;
