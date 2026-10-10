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

-- CORE-07 — Estado APRENDIDO del Self Model (lo único que no se puede recalcular).
-- Se guarda una fila por tipo de registro con su contenido y su provenance. NO se
-- guardan `current_goal`, `decisions`, `task_results`, `confidence`, `permissions` ni
-- `current_policy`: eso es una proyección del objeto misión en vivo, que ya vive en
-- `missions`. Copiarlo aquí sería una segunda memoria que puede contradecir a la real.
--
-- `mission_id` es provenance OPCIONAL y NULLABLE: una lección pre-misión (o de origen
-- desconocido) es legítima sin misión. Y NO es identidad: las lecciones son conocimiento
-- global del Self Model, no están particionadas por misión.
CREATE TABLE IF NOT EXISTS self_learnings (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('lesson', 'observation', 'reflection')),
    text TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    mission_id TEXT REFERENCES missions(id) ON DELETE SET NULL,
    at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_self_learnings_kind ON self_learnings(kind, at DESC);
CREATE INDEX IF NOT EXISTS ix_self_learnings_mission ON self_learnings(mission_id, at DESC);

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

-- Control de migraciones. `Database.migrate()` ejecuta `schema.SQL` y después aplica,
-- en orden y una sola vez, los ficheros de `scripts/migrations/`. Sin esta tabla no hay
-- forma de saber cuáles se aplicaron: `schema.SQL` es idempotente y una migración que se
-- reaplica sobre datos reales puede no serlo.
CREATE TABLE IF NOT EXISTS migrations_applied (
    migration_name TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS observations (
    id BIGSERIAL PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    content JSONB NOT NULL DEFAULT '{}'::jsonb,
    trusted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_observations_mission ON observations(mission_id, created_at);

-- Búsqueda semántica de memoria. Se declara AQUÍ y TAMBIÉN en la migración 064, y es
-- deliberado: este `SQL` describe el estado completo de una base nueva y es idempotente;
-- la migración es el historial para bases ya desplegadas, donde un `CREATE TABLE IF NOT
-- EXISTS` no añadiría la columna. Los dos caminos convergent en el mismo sitio.
--
-- NULLABLE: las observaciones ya escritas quedan sin vector y el provider cae a
-- coincidencia de términos, que es exactamente el comportamiento anterior.
--
-- Lo que NO está aquí y sí en `scripts/migrations/`: `goals`, `schedule_rules` y `skills`.
-- Pertenecen a las migraciones que las crean; meterlas en el esquema base volvería a
-- tener dos fuentes de verdad para lo mismo.
ALTER TABLE observations
    ADD COLUMN IF NOT EXISTS embedding vector(384);

CREATE INDEX IF NOT EXISTS ix_observations_embedding
    ON observations USING hnsw (embedding vector_cosine_ops);

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

-- CORE-11: el aprendizaje tiene que sobrevivir a un reinicio. Sin esto, una lección es texto
-- en RAM y una skill es un fichero que nadie ejecuta. Las cuatro tablas están separadas por
-- su ciclo de vida, no por conveniencia: una lección nace de una experiencia, una candidata de
-- una lección, una versión de una candidata validada, y el rendimiento de un uso. Borrar una
-- candidata no debe borrar la lección que la originó.
CREATE TABLE IF NOT EXISTS lessons (
    id BIGSERIAL PRIMARY KEY,
    lesson_id TEXT NOT NULL UNIQUE,
    statement TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '',
    source_experience TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT 'unknown',
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
  CREATE INDEX IF NOT EXISTS ix_lessons_scope ON lessons(scope, created_at DESC);

CREATE TABLE IF NOT EXISTS skill_candidates (
    id BIGSERIAL PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
  CREATE INDEX IF NOT EXISTS ix_skill_candidates_name ON skill_candidates(name, status);

-- Una versión publicada es INMUTABLE: la app no expone un UPDATE, y por eso una ejecución
-- antigua siempre se puede reconstruir contra la skill que realmente se usó.
CREATE TABLE IF NOT EXISTS skill_versions (
    id BIGSERIAL PRIMARY KEY,
    skill_id TEXT NOT NULL,
    version INT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'validated',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (skill_id, version)
);
  CREATE INDEX IF NOT EXISTS ix_skill_versions_status ON skill_versions(status, name);

CREATE TABLE IF NOT EXISTS skill_performance (
    id BIGSERIAL PRIMARY KEY,
    skill_id TEXT NOT NULL,
    version INT NOT NULL,
    mission_id TEXT NOT NULL,
    verified BOOLEAN NOT NULL DEFAULT FALSE,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
  CREATE INDEX IF NOT EXISTS ix_skill_performance_skill ON skill_performance(skill_id, version);

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

  -- P0 §4.5.2/.3/.4 — evolution de confianza, conflictos y procedencia.
  --
  -- Son ALTER y no CREATE porque `world_entities` YA existe desde §4.3: hace falta
  -- añadir columnas sin perder las filas. `IF NOT EXISTS` los hace idempotentes, así que
  -- `migrate()` puede ejecutarse las veces que haga falta (y en una base recién creada,
  -- donde las columnas nacen vacías con sus defaults).
  --
  -- `base_confidence` se persiste aparte de `confidence` porque la confianza es un valor
  -- DERIVADO de (base, support, contradictions). Guardar sólo el resultado impediría
  -- recalcularla sin perder el historial, que es lo que hace §4.5.5 al rehidratar.
  ALTER TABLE world_entities
      ADD COLUMN IF NOT EXISTS base_confidence REAL NOT NULL DEFAULT 0,
      ADD COLUMN IF NOT EXISTS support INT NOT NULL DEFAULT 1,
      ADD COLUMN IF NOT EXISTS contradictions INT NOT NULL DEFAULT 0,
      ADD COLUMN IF NOT EXISTS evidence_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
      ADD COLUMN IF NOT EXISTS conflicts JSONB NOT NULL DEFAULT '[]'::jsonb,
      -- El recuento de valores por atributo. Sin esto, al rehidratar una entidad se
      -- perdería el historial de contradicciones y la confianza volvería a la de una
      -- única observación.
      ADD COLUMN IF NOT EXISTS value_counts JSONB NOT NULL DEFAULT '{}'::jsonb;

  -- `confidence` y `base_confidence` pasan a DOUBLE PRECISION, por el mismo motivo que
  -- `last_seen` arriba. Con §4.5.2 la confianza dejó de ser un 0/1/0.7 aproximado para
  -- depender de un conteo de fuentes, y `REAL` (float4) se la comía: al rehidratar,
  -- 0.814106108683 volvía como 0.8141061, así que el estado recuperado NO era el que se
  -- observó y el determinismo entre procesos se rompía sin que nada fallara.
  -- Reejecutable sin efecto: si la columna ya es DOUBLE PRECISION, la conversión es no-op.
  -- Las filas ya truncadas por REAL conservan su valor (esa precisión no se recupera);
  -- a partir de aquí las escrituras son exactas.
  ALTER TABLE world_entities
      ALTER COLUMN confidence TYPE DOUBLE PRECISION,
      ALTER COLUMN base_confidence TYPE DOUBLE PRECISION;
  """