CREATE TABLE IF NOT EXISTS conversation_history_head (
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    stream TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    chain_hash TEXT NOT NULL,
    PRIMARY KEY (tenant_id, task_id, stream),
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS conversation_history_event (
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    stream TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    chain_hash TEXT NOT NULL,
    message TEXT NOT NULL,
    PRIMARY KEY (tenant_id, task_id, stream, sequence),
    UNIQUE (tenant_id, task_id, stream, event_id),
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS conversation_history_migration (
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    stream TEXT NOT NULL,
    boundary_key TEXT NOT NULL,
    proof TEXT NOT NULL,
    PRIMARY KEY (tenant_id, task_id, stream, boundary_key),
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);
