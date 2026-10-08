-- Scheduler (VISION §27) — reglas que generan misiones automáticamente.
--
-- Una regla describe QUÉ misión crear (objective_template + envelope_template) y
-- CUÁNDO (schedule_type + schedule_value). El `Scheduler` la lee desde PostgreSQL cada
-- ~30s y encola las misiones por la MISMA vía que una misión manual: nunca bypasea el
-- envelope ni la política.
--
-- Invariantes por diseño (VISION §49, §69):
--   * El `envelope_template` NUNCA puede ampliar las capacidades permitidas a nivel
--     global: el scheduler rechaza (fail-closed) cualquier capacidad desconocida o que
--     no esté habilitada en el catálogo.
--   * Capacidades destructivas (side_effects=true) exigen el flag explícito
--     `auto_approve_destructive: true` en el propio envelope_template.
--   * Regla 11: toda misión generada lleva `success_criteria` verificables; una regla
--     sin criterios no produce misiones y acaba auto-deshabilitándose.
--   * `consecutive_failures >= max_consecutive_failures (default 3)` → la regla se
--     deshabilita sola; `reset` la reactiva sin borrarla (soft-delete).
--
-- `id` es TEXT con UUID v4 en la app, igual que missions/goals/tasks en esta base; así
-- el FK `goal_id` a `goals(id)` (TEXT) y los adaptadores psycopg no mezclan tipos.

CREATE TABLE IF NOT EXISTS schedule_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    schedule_type TEXT NOT NULL
        CHECK (schedule_type IN ('one_time', 'interval', 'cron')),
    schedule_value JSONB NOT NULL DEFAULT '{}'::jsonb,
    goal_id TEXT REFERENCES goals(id) ON DELETE SET NULL,
    objective_template TEXT NOT NULL,
    envelope_template JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT true,
    last_run_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ,
    last_failure TEXT,
    consecutive_failures INT NOT NULL DEFAULT 0,
    max_consecutive_failures INT NOT NULL DEFAULT 3,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_schedule_rules_due ON schedule_rules(enabled, next_run_at);
CREATE INDEX IF NOT EXISTS ix_schedule_rules_goal ON schedule_rules(goal_id);