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
    id TEXT NOT NULL,
    version INT NOT NULL DEFAULT 1,
    name TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    procedure JSONB NOT NULL DEFAULT '[]'::jsonb,
    prerequisites JSONB NOT NULL DEFAULT '[]'::jsonb,
    required_capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
    success_criteria JSONB NOT NULL DEFAULT '[]'::jsonb,
    verification_method JSONB NOT NULL DEFAULT '[]'::jsonb,
    applicability TEXT NOT NULL DEFAULT '',
    contraindications JSONB NOT NULL DEFAULT '[]'::jsonb,
    expected_effects JSONB NOT NULL DEFAULT '[]'::jsonb,
    failure_modes JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_lessons JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_experiences JSONB NOT NULL DEFAULT '[]'::jsonb,
    proposed_version INT NOT NULL DEFAULT 1,
    validation JSONB NOT NULL DEFAULT '{}'::jsonb,
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    risk TEXT NOT NULL DEFAULT 'critical'
        CHECK (risk IN ('low', 'medium', 'high', 'critical')),
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'proposed', 'under_validation', 'validated',
                          'rejected', 'superseded', 'active', 'deprecated')),
    performance_stats JSONB NOT NULL DEFAULT '[]'::jsonb,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_from_lesson_id TEXT REFERENCES lessons(lesson_id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, version)
);
CREATE INDEX IF NOT EXISTS ix_skills_status ON skills(status, name);
CREATE INDEX IF NOT EXISTS ix_skills_name_version ON skills(name, version);

-- Corrección de la tabla ya desplegada con la forma original (PK sólo `id` y sin las
-- columnas que la rehidratación necesita).
ALTER TABLE skills
    ADD COLUMN IF NOT EXISTS applicability TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS required_capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS expected_effects JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS failure_modes JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS source_lessons JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS source_experiences JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS proposed_version INT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS validation JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- `critical` y no `low`: si la columna se pierde, el default debe CERRAR, no abrir.
    -- `skill.py:204` convierte un `risk` ausente en "low", que es la declaración mínima.
    ADD COLUMN IF NOT EXISTS risk TEXT NOT NULL DEFAULT 'critical',
    ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb;

-- `superseded` no estaba en el CHECK y `runtime` lo rehidrata: sin esto, guardar la v1 de
-- una skill mejorada violaría la restricción.
ALTER TABLE skills DROP CONSTRAINT IF EXISTS skills_status_check;
ALTER TABLE skills ADD CONSTRAINT skills_status_check CHECK (status IN (
    'candidate', 'proposed', 'under_validation', 'validated', 'rejected',
    'superseded', 'active', 'deprecated'));

-- Una skill tiene varias versiones con el mismo `skill_id`; con `PRIMARY KEY (id)` la v2
-- no cabe y la v1 se pisaría, destruyendo la evidencia que `test_16` exige conservar.
ALTER TABLE skills DROP CONSTRAINT IF EXISTS skills_pkey;
ALTER TABLE skills DROP CONSTRAINT IF EXISTS skills_name_version_key;
ALTER TABLE skills ADD PRIMARY KEY (id, version);

CREATE INDEX IF NOT EXISTS ix_skills_status ON skills(status);
CREATE INDEX IF NOT EXISTS ix_skills_name ON skills(name, version);
CREATE INDEX IF NOT EXISTS ix_skills_lesson ON skills(created_from_lesson_id);