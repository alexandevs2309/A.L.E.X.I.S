SQL = """
CREATE TABLE IF NOT EXISTS missions (
    id TEXT PRIMARY KEY,
    goal JSONB NOT NULL,
    envelope JSONB NOT NULL,
    state TEXT NOT NULL,
    autonomy TEXT NOT NULL,
    context JSONB NOT NULL DEFAULT '{}'::jsonb,
    results JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mission_events (
    id BIGSERIAL PRIMARY KEY,
    mission_id TEXT REFERENCES missions(id) ON DELETE CASCADE,
    topic TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_mission_events_mission ON mission_events(mission_id, created_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    event TEXT NOT NULL,
    actor TEXT NOT NULL,
    mission_id TEXT REFERENCES missions(id) ON DELETE SET NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_audit_mission ON audit_log(mission_id, created_at);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'task',
    agent TEXT NOT NULL DEFAULT 'general',
    tool TEXT,
    args JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 3,
    error TEXT,
    result JSONB,
    lease_until TIMESTAMPTZ,
    deadline TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_tasks_mission ON tasks(mission_id);
CREATE INDEX IF NOT EXISTS ix_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS ix_tasks_lease ON tasks(status, lease_until);

CREATE TABLE IF NOT EXISTS executions (
    id BIGSERIAL PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    tool TEXT NOT NULL,
    args_hash TEXT NOT NULL DEFAULT '',
    ok BOOLEAN,
    output JSONB,
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_executions_task ON executions(task_id);

CREATE TABLE IF NOT EXISTS observations (
    id BIGSERIAL PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    content JSONB NOT NULL DEFAULT '{}'::jsonb,
    trusted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_observations_mission ON observations(mission_id, created_at);

CREATE TABLE IF NOT EXISTS verifications (
    id BIGSERIAL PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    passed BOOLEAN NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    notes TEXT,
    verifier TEXT NOT NULL DEFAULT 'unknown',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_verifications_mission ON verifications(mission_id, created_at);

CREATE TABLE IF NOT EXISTS checkpoints (
    id BIGSERIAL PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    step_index INT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_checkpoints_mission ON checkpoints(mission_id, step_index);
"""