-- Skill System (VISION §20/§21) — habilidades reutilizables versionadas.
--
-- Feedback: experiencia → reflexión → lesson → skill_candidate → VALIDATION →
-- → skill_version → performance → REUSE. Esta tabla es el registro gestionado que cierra
-- el pipeline: un candidato (viene de ReflectionEngine, VISION §20) se valida aquí
-- (`candidate` → `validated`), gana `active` con 3+ éxitos sin fallos (promoter) y pasa a
-- `deprecated` con 3+ fallos consecutivos. Nunca se borra: el histórico de versiones es
-- evidencia (rule 17) y la reautorización en caliente está prohibida (rule 19).
--
-- Invariantes por diseño (VISION §69):
--   * Un skill NUNCA puede auto-modificar su `success_criteria` una vez validado: el
--     `SkillValidator` es determinista y sólo la validación manual (POST /skills/{id}/validate)
--     puede cambiar el estado; los criterios son candidatos verificables o no lo son (regla 11).
--   * Un skill NUNCA puede incluir capacidades que requieran aprobación (side_effects o
--     default_risk >= high) sin declarar `requires_approval` en `verification_method`
--     (fail-closed: si no lo declara, se queda en `candidate`).
--   * La reutilización (SkillReuser) es SUGERENCIA, no obligación: la decisión de usar la
--     skill la toma el planner, y la misión lo registra con `mission.context["skill_used"]`.
--   * `performance_stats` se actualiza tras cada misión que usó la skill (success_count,
--     failure_count, consecutive_failures, avg_duration).
--
-- `id` es TEXT con UUID en la app, igual que missions/goals/schedule_rules en esta base;
-- `created_from_lesson_id` enlaza con `lessons(lesson_id)` (TEXT UNIQUE). La spec pedía
-- UUIDs, pero esta base usa TEXT en todas sus claves foráneas: mezclar tipos no aporta.

CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version INT NOT NULL DEFAULT 1,
    purpose TEXT NOT NULL,
    prerequisites JSONB NOT NULL DEFAULT '[]'::jsonb,
    capabilities_used JSONB NOT NULL DEFAULT '[]'::jsonb,
    success_criteria JSONB NOT NULL DEFAULT '[]'::jsonb,
    verification_method JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'validated'
        CHECK (status IN ('candidate', 'validated', 'active', 'deprecated')),
    performance_stats JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_from_lesson_id TEXT REFERENCES lessons(lesson_id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name, version)
);

CREATE INDEX IF NOT EXISTS ix_skills_status ON skills(status);
CREATE INDEX IF NOT EXISTS ix_skills_name ON skills(name, version);
CREATE INDEX IF NOT EXISTS ix_skills_lesson ON skills(created_from_lesson_id);