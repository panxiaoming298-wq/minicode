CREATE TABLE IF NOT EXISTS memory_schema_version (
    version INTEGER PRIMARY KEY,
    applied_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_task (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    workspace_id TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    current_run_id TEXT NOT NULL DEFAULT '',
    current_step_id TEXT NOT NULL DEFAULT '',
    manual_memory_mode TEXT,
    cross_session_required BOOLEAN NOT NULL DEFAULT FALSE,
    restart_recovery_required BOOLEAN NOT NULL DEFAULT FALSE,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS agent_run (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    current_memory_mode TEXT NOT NULL,
    model_name TEXT NOT NULL DEFAULT '',
    model_context_limit BIGINT NOT NULL DEFAULT 0,
    started_at DOUBLE PRECISION NOT NULL,
    completed_at DOUBLE PRECISION,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_step (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    parent_step_id TEXT NOT NULL DEFAULT '',
    sequence_no BIGINT NOT NULL,
    step_type TEXT NOT NULL DEFAULT 'AGENT',
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'RUNNING',
    acceptance_criteria JSONB NOT NULL DEFAULT '{}'::jsonb,
    input_event_id TEXT NOT NULL DEFAULT '',
    output_event_id TEXT NOT NULL DEFAULT '',
    started_at DOUBLE PRECISION NOT NULL,
    completed_at DOUBLE PRECISION,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, run_id) REFERENCES agent_run(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_event (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    step_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    content_text TEXT NOT NULL DEFAULT '',
    content_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    artifact_uri TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    token_count BIGINT NOT NULL DEFAULT 0,
    caused_by_event_id TEXT NOT NULL DEFAULT '',
    correlation_id TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL DEFAULT '',
    schema_version INTEGER NOT NULL DEFAULT 1,
    created_at DOUBLE PRECISION NOT NULL,
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);
DROP INDEX IF EXISTS agent_event_idempotency;
CREATE UNIQUE INDEX agent_event_idempotency
    ON agent_event(tenant_id, task_id, idempotency_key) WHERE idempotency_key <> '';
CREATE INDEX IF NOT EXISTS agent_event_task_time
    ON agent_event(tenant_id, task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS agent_event_step
    ON agent_event(tenant_id, task_id, step_id);

CREATE TABLE IF NOT EXISTS agent_task_state (
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    version BIGINT NOT NULL DEFAULT 0,
    phase TEXT NOT NULL DEFAULT 'INITIAL',
    current_step_id TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL DEFAULT '',
    active_constraints JSONB NOT NULL DEFAULT '[]'::jsonb,
    active_decisions JSONB NOT NULL DEFAULT '{}'::jsonb,
    open_errors JSONB NOT NULL DEFAULT '[]'::jsonb,
    completed_steps JSONB NOT NULL DEFAULT '[]'::jsonb,
    pending_steps JSONB NOT NULL DEFAULT '[]'::jsonb,
    active_artifacts JSONB NOT NULL DEFAULT '[]'::jsonb,
    blocked_by JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (tenant_id, task_id),
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_decision (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    decision_key TEXT NOT NULL,
    decision_text TEXT NOT NULL,
    structured_value JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','SUPERSEDED','REJECTED','TOMBSTONED')),
    source_event_id TEXT NOT NULL DEFAULT '',
    supersedes_id TEXT NOT NULL DEFAULT '',
    superseded_by_id TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS agent_decision_active
    ON agent_decision(tenant_id, task_id, decision_key, status);

CREATE TABLE IF NOT EXISTS agent_dependency (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    dependency_type TEXT NOT NULL,
    dependency_id TEXT NOT NULL,
    required BOOLEAN NOT NULL DEFAULT TRUE,
    dependency_depth INTEGER NOT NULL DEFAULT 0,
    created_at DOUBLE PRECISION NOT NULL,
    UNIQUE (tenant_id, task_id, step_id, dependency_type, dependency_id),
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS agent_dependency_step
    ON agent_dependency(tenant_id, task_id, step_id, required);

CREATE TABLE IF NOT EXISTS agent_artifact (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    uri TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_event_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CONSTRAINT agent_artifact_status_check
        CHECK (status IN ('PENDING_UPLOAD','ACTIVE','MISSING','CORRUPT','TOMBSTONED')),
    schema_version INTEGER NOT NULL DEFAULT 1,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS agent_artifact_task
    ON agent_artifact(tenant_id, task_id, status, created_at DESC);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'agent_artifact'::regclass
          AND conname = 'agent_artifact_status_check'
          AND pg_get_constraintdef(oid) LIKE '%CORRUPT%'
    ) THEN
        ALTER TABLE agent_artifact
            DROP CONSTRAINT IF EXISTS agent_artifact_status_check;
        ALTER TABLE agent_artifact
            ADD CONSTRAINT agent_artifact_status_check
            CHECK (status IN ('PENDING_UPLOAD','ACTIVE','MISSING','CORRUPT','TOMBSTONED'));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS agent_checkpoint (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    state_version BIGINT NOT NULL,
    current_step_id TEXT NOT NULL DEFAULT '',
    memory_mode TEXT NOT NULL,
    snapshot JSONB NOT NULL,
    content_hash TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    created_at DOUBLE PRECISION NOT NULL,
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS agent_checkpoint_latest
    ON agent_checkpoint(tenant_id, task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_memory (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    step_id TEXT NOT NULL DEFAULT '',
    memory_type TEXT NOT NULL,
    title TEXT NOT NULL,
    preview_text TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','OPEN','RESOLVED','SUPERSEDED','REJECTED','TOMBSTONED')),
    importance DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    source_event_id TEXT NOT NULL DEFAULT '',
    artifact_id TEXT NOT NULL DEFAULT '',
    source_start BIGINT NOT NULL DEFAULT 0,
    source_end BIGINT NOT NULL DEFAULT 0,
    source_unit TEXT NOT NULL DEFAULT 'CHAR',
    content_hash TEXT NOT NULL,
    token_count BIGINT NOT NULL DEFAULT 0,
    phase TEXT NOT NULL DEFAULT '',
    facets JSONB NOT NULL DEFAULT '[]'::jsonb,
    search_document TEXT NOT NULL DEFAULT '',
    milvus_collection TEXT NOT NULL DEFAULT '',
    milvus_primary_key TEXT NOT NULL DEFAULT '',
    indexed_at DOUBLE PRECISION,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS agent_memory_search
    ON agent_memory(tenant_id, task_id, status, memory_type, phase);
ALTER TABLE agent_memory
    ADD COLUMN IF NOT EXISTS search_document TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS agent_memory_search_document_fts
    ON agent_memory USING GIN (to_tsvector('simple', search_document));
ALTER TABLE agent_memory
    DROP CONSTRAINT IF EXISTS agent_memory_tenant_id_task_id_memory_type_content_hash_key;
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_indexes
        WHERE schemaname = current_schema()
          AND indexname = 'agent_memory_source_identity'
          AND indexdef NOT LIKE '%source_start%'
    ) THEN
        DROP INDEX agent_memory_source_identity;
    END IF;
END
$$;
CREATE UNIQUE INDEX IF NOT EXISTS agent_memory_source_identity
    ON agent_memory(
        tenant_id, task_id, memory_type, content_hash, source_event_id, artifact_id,
        source_start, source_end
    );

CREATE TABLE IF NOT EXISTS agent_outbox (
    id TEXT PRIMARY KEY,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    claim_token TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL,
    processed_at DOUBLE PRECISION
);
ALTER TABLE agent_outbox
    ADD COLUMN IF NOT EXISTS claim_token TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS agent_outbox_pending
    ON agent_outbox(status, next_retry_at, created_at);

CREATE TABLE IF NOT EXISTS memory_working_set (
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 1,
    state_version BIGINT NOT NULL DEFAULT 0,
    expires_at DOUBLE PRECISION NOT NULL,
    last_used_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (tenant_id, task_id, phase, memory_id),
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS memory_deletion_audit (
    request_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_id_hash TEXT NOT NULL,
    scope TEXT NOT NULL,
    actor_hash TEXT NOT NULL DEFAULT '',
    reason_hash TEXT NOT NULL DEFAULT '',
    execution_status TEXT NOT NULL DEFAULT 'COMPLETED',
    deleted_counts JSONB NOT NULL,
    requested_at DOUBLE PRECISION NOT NULL,
    completed_at DOUBLE PRECISION NOT NULL
);
ALTER TABLE memory_deletion_audit ADD COLUMN IF NOT EXISTS actor_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE memory_deletion_audit ADD COLUMN IF NOT EXISTS reason_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE memory_deletion_audit ADD COLUMN IF NOT EXISTS execution_status TEXT NOT NULL DEFAULT 'COMPLETED';

CREATE TABLE IF NOT EXISTS memory_retention_policy (
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    retention_until DOUBLE PRECISION,
    legal_hold BOOLEAN NOT NULL DEFAULT FALSE,
    deletion_mode TEXT NOT NULL DEFAULT 'HARD_DELETE',
    set_by TEXT NOT NULL DEFAULT 'system',
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (tenant_id, task_id),
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS memory_archive_manifest (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    uri TEXT NOT NULL,
    archive_type TEXT NOT NULL,
    codec TEXT NOT NULL,
    media_type TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    object_hash TEXT NOT NULL,
    event_count BIGINT NOT NULL,
    uncompressed_size_bytes BIGINT NOT NULL,
    compressed_size_bytes BIGINT NOT NULL,
    first_event_id TEXT NOT NULL DEFAULT '',
    last_event_id TEXT NOT NULL DEFAULT '',
    first_created_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_created_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    rule_name TEXT NOT NULL DEFAULT '',
    backup_class TEXT NOT NULL DEFAULT 'STANDARD',
    status TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE','VERIFIED','RESTORED','TOMBSTONED')),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_verified_at DOUBLE PRECISION,
    restored_at DOUBLE PRECISION,
    schema_version INTEGER NOT NULL DEFAULT 1,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    UNIQUE (tenant_id, task_id, artifact_id),
    FOREIGN KEY (tenant_id, task_id) REFERENCES agent_task(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (artifact_id) REFERENCES agent_artifact(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS memory_archive_manifest_task
    ON memory_archive_manifest(tenant_id, task_id, status, last_created_at);

CREATE OR REPLACE FUNCTION minicode_require_active_task() RETURNS trigger AS $$
DECLARE
    current_status TEXT;
BEGIN
    SELECT status INTO current_status
    FROM agent_task
    WHERE tenant_id=NEW.tenant_id AND id=NEW.task_id
    FOR UPDATE;
    IF current_status IS DISTINCT FROM 'ACTIVE' THEN
        RAISE EXCEPTION 'task is not ACTIVE';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS fence_agent_run ON agent_run;
CREATE TRIGGER fence_agent_run BEFORE INSERT OR UPDATE ON agent_run
    FOR EACH ROW EXECUTE FUNCTION minicode_require_active_task();
DROP TRIGGER IF EXISTS fence_agent_step ON agent_step;
CREATE TRIGGER fence_agent_step BEFORE INSERT OR UPDATE ON agent_step
    FOR EACH ROW EXECUTE FUNCTION minicode_require_active_task();
DROP TRIGGER IF EXISTS fence_agent_event ON agent_event;
CREATE TRIGGER fence_agent_event BEFORE INSERT ON agent_event
    FOR EACH ROW EXECUTE FUNCTION minicode_require_active_task();
DROP TRIGGER IF EXISTS fence_agent_task_state ON agent_task_state;
CREATE TRIGGER fence_agent_task_state BEFORE INSERT OR UPDATE ON agent_task_state
    FOR EACH ROW EXECUTE FUNCTION minicode_require_active_task();
DROP TRIGGER IF EXISTS fence_agent_decision ON agent_decision;
CREATE TRIGGER fence_agent_decision BEFORE INSERT OR UPDATE ON agent_decision
    FOR EACH ROW EXECUTE FUNCTION minicode_require_active_task();
DROP TRIGGER IF EXISTS fence_agent_dependency ON agent_dependency;
CREATE TRIGGER fence_agent_dependency BEFORE INSERT OR UPDATE ON agent_dependency
    FOR EACH ROW EXECUTE FUNCTION minicode_require_active_task();
DROP TRIGGER IF EXISTS fence_agent_artifact ON agent_artifact;
CREATE TRIGGER fence_agent_artifact BEFORE INSERT OR UPDATE ON agent_artifact
    FOR EACH ROW EXECUTE FUNCTION minicode_require_active_task();
DROP TRIGGER IF EXISTS fence_agent_checkpoint ON agent_checkpoint;
CREATE TRIGGER fence_agent_checkpoint BEFORE INSERT OR UPDATE ON agent_checkpoint
    FOR EACH ROW EXECUTE FUNCTION minicode_require_active_task();
DROP TRIGGER IF EXISTS fence_agent_memory ON agent_memory;
CREATE TRIGGER fence_agent_memory BEFORE INSERT OR UPDATE ON agent_memory
    FOR EACH ROW EXECUTE FUNCTION minicode_require_active_task();
DROP TRIGGER IF EXISTS fence_memory_working_set ON memory_working_set;
CREATE TRIGGER fence_memory_working_set BEFORE INSERT OR UPDATE ON memory_working_set
    FOR EACH ROW EXECUTE FUNCTION minicode_require_active_task();
DROP TRIGGER IF EXISTS fence_memory_retention_policy ON memory_retention_policy;
CREATE TRIGGER fence_memory_retention_policy BEFORE INSERT OR UPDATE ON memory_retention_policy
    FOR EACH ROW EXECUTE FUNCTION minicode_require_active_task();
DROP TRIGGER IF EXISTS fence_memory_archive_manifest ON memory_archive_manifest;
CREATE TRIGGER fence_memory_archive_manifest BEFORE INSERT OR UPDATE ON memory_archive_manifest
    FOR EACH ROW EXECUTE FUNCTION minicode_require_active_task();

CREATE OR REPLACE FUNCTION minicode_protect_agent_event() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'agent_event is append-only';
    END IF;
    IF TG_OP = 'DELETE' AND current_setting('minicode.allow_hard_delete', true) <> 'on' THEN
        RAISE EXCEPTION 'agent_event deletion requires an authorized hard-delete transaction';
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS protect_agent_event ON agent_event;
CREATE TRIGGER protect_agent_event
    BEFORE UPDATE OR DELETE ON agent_event
    FOR EACH ROW EXECUTE FUNCTION minicode_protect_agent_event();

INSERT INTO memory_schema_version(version, applied_at)
VALUES (9, EXTRACT(EPOCH FROM clock_timestamp()))
ON CONFLICT(version) DO NOTHING;
