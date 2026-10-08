-- Goal Management — capa superior a MissionEngine.
--
-- Un "goal" es un objetivo de negocio que se descompone en sub-objetivos y/o misiones.
-- El progreso NO es un campo de confianza: se recalcula siempre desde las misiones que
-- llevan `context->>'goal_id'` (ver alexis/goals/repository.py::mission_counts), de modo
-- que sobrevive a reinicios sin segunda memoria.
--
-- Invariantes por diseño (VISION §11):
--   * `success_criteria` es INMUTABLE tras la creación: la API PATCH no puede tocarlo.
--   * Ningún goal llega a `completed` solo porque sus misiones terminaron: exige que el
--     progreso sea 1.0 Y que cada criterio esté cubierto por una misión COMPLETADA.
--   * `resource_budget` es obligatorio; excederlo deja el goal en `paused`.
--
-- `priority` usa 1 = más alta, 5 = más baja. `status` replica los estados del spec:
-- active | paused | completed | abandoned. `progress` es 0..1 calculado, nunca escrito
-- a mano por agentes.

CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES goals(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    priority INT NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'completed', 'abandoned')),
    success_criteria JSONB NOT NULL DEFAULT '[]'::jsonb,
    progress DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 1),
    blockers JSONB NOT NULL DEFAULT '[]'::jsonb,
    risks JSONB NOT NULL DEFAULT '[]'::jsonb,
    resource_budget JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_goals_parent ON goals(parent_id);
CREATE INDEX IF NOT EXISTS ix_goals_status_priority ON goals(status, priority);
CREATE INDEX IF NOT EXISTS ix_goals_objective ON goals(objective);
CREATE INDEX IF NOT EXISTS ix_goals_created ON goals(created_at DESC);