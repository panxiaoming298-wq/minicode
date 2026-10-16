CREATE TABLE IF NOT EXISTS memory_rerank_control (
    key TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    blocked_until REAL NOT NULL DEFAULT 0,
    lease_token TEXT NOT NULL DEFAULT '',
    lease_until REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS memory_rerank_cache (
    key TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    grades TEXT NOT NULL,
    expires_at REAL NOT NULL,
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rerank_cache_expiry ON memory_rerank_cache(expires_at);
