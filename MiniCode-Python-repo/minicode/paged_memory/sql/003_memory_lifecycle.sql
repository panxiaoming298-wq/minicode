-- Lifecycle clocks are separate from immutable source/content timestamps.
CREATE TABLE IF NOT EXISTS agent_memory_lifecycle (
    memory_id TEXT PRIMARY KEY REFERENCES agent_memory(id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    last_accessed_at REAL NOT NULL DEFAULT 0,
    last_decay_at REAL NOT NULL DEFAULT 0,
    archived_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS agent_memory_lifecycle_scope
    ON agent_memory_lifecycle(tenant_id, task_id);
