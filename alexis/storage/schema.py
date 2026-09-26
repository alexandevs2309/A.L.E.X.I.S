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

  -- P0 §4.3 — World Model persistente. Sustituye a `mission.context["world"]` como almacén
  -- de autoridad; esa proyección sigue existiendo, pero ya no es la fuente.
  --
  -- NO es la tabla `observations`: allí se guarda evidencia cruda con su procedencia; aquí
  -- vive el ESTADO ESTRUCTURADO del mundo, ya interpretado y deduplicado por entidad.
  -- Duplicar una en otra sería guardar dos veces lo mismo con dos ciclos de vida distintos.
  --
  -- La clave primaria es (scope_id, entity_id) porque el path relativo no identifica: dos
  -- proyectos pueden tener `file:notas.txt` (§4.1).
  CREATE TABLE IF NOT EXISTS world_entities (
      scope_id TEXT NOT NULL,
      entity_id TEXT NOT NULL,
      kind TEXT NOT NULL,
      name TEXT NOT NULL,
      attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
      source TEXT NOT NULL DEFAULT 'declared',
      confidence REAL NOT NULL DEFAULT 0,
      -- Sin FK a `missions` a propósito: el conocimiento de un proyecto sobrevive a la
      -- misión que lo observó. Con CASCADE, borrar una misión borraría el mapa del
      -- proyecto; con SET NULL, quedaría un hueco. Aquí `mission_id` es procedencia, no
      -- dependencia. (La evidencia sí vive ligada a su misión, en `observations`.)
      mission_id TEXT,
      observations INT NOT NULL DEFAULT 0,
      -- DOUBLE PRECISION, no REAL: `REAL` es float4 y perdería precisión. `last_seen` tiene
      -- que volver EXACTO tras el round-trip (§4.2). 0 significa "edad desconocida"
      -- (LAST_SEEN_UNKNOWN); NUNCA se rellena con now(), porque inventaría que se acaba de
      -- observar. Un default de now() aquí haría justo eso en cada fila nueva.
      last_seen DOUBLE PRECISION NOT NULL DEFAULT 0,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      PRIMARY KEY (scope_id, entity_id)
  );
  CREATE INDEX IF NOT EXISTS ix_world_entities_scope ON world_entities(scope_id);
  CREATE INDEX IF NOT EXISTS ix_world_entities_last_seen ON world_entities(scope_id, last_seen DESC);
  CREATE INDEX IF NOT EXISTS ix_world_entities_kind ON world_entities(scope_id, kind);

  -- Grafo de relaciones, con la misma clave de ámbito. Sin FK a `world_entities` porque un
  -- grafo puede referenciar algo que aún no se observó; se resuelve al existir la entidad.
  CREATE TABLE IF NOT EXISTS world_edges (
      scope_id TEXT NOT NULL,
      parent_id TEXT NOT NULL,
      child_id TEXT NOT NULL,
      relation TEXT NOT NULL DEFAULT 'depends_on',
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      PRIMARY KEY (scope_id, parent_id, child_id, relation)
  );
  CREATE INDEX IF NOT EXISTS ix_world_edges_scope ON world_edges(scope_id, parent_id);
  CREATE INDEX IF NOT EXISTS ix_world_edges_child ON world_edges(scope_id, child_id);
  """