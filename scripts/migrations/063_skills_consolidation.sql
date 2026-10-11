-- Consolidación del Skill System: una sola fuente de verdad (VISION §20/§21).
--
-- Antes coexistían dos sistemas con el mismo propósito y modelos distintos:
--   * CORE-11/12 → `skill_candidates` + `skill_versions` + `skill_performance`
--     (`alexis/learning/skill.py`, registro en memoria rehidratado por `hydrate_learning`).
--   * Pipeline actual → tabla `skills` (`alexis/learning/skills/`).
-- Éste es el sistema oficial: las tres tablas viejas se eliminan y su equivalente vive
-- ahora en `skills`. `lessons` NO se toca (la siguen usando la reflexión y la verificación).
--
-- Lo que se conserva (y por qué):
--   * `procedure` — sin ella no hay "la skill ES el plan": `Planner.plan_from_skill()`
--     construye el plan desde los pasos de la skill. Es la función que hace reutilizable
--     una estrategia, y lived en CORE-12.
--   * El rendimiento ya vive agregado en `skills.performance_stats` (antes era una tabla
--     append-only que, además, nunca se escribía en el camino real de producción).
--
-- Nota sobre el orden: esta migración debe ejecutarse DESPUÉS de la 062 (que crea
-- `skills`). `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` y `DROP TABLE IF EXISTS` la hacen
-- idempotente.

-- 1) Forma final de la tabla: columnas que la rehidratación necesita, CHECK de status
--    ampliado y PK compuesta por (id, version).
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
    ADD COLUMN IF NOT EXISTS risk TEXT NOT NULL DEFAULT 'critical',
    ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE skills DROP CONSTRAINT IF EXISTS skills_status_check;
ALTER TABLE skills ADD CONSTRAINT skills_status_check CHECK (status IN (
    'candidate', 'proposed', 'under_validation', 'validated', 'rejected',
    'superseded', 'active', 'deprecated'));

ALTER TABLE skills DROP CONSTRAINT IF EXISTS skills_pkey;
ALTER TABLE skills DROP CONSTRAINT IF EXISTS skills_name_version_key;
ALTER TABLE skills ADD PRIMARY KEY (id, version);

-- 2) DATOS. El orden importa: primero se copia, después se borra. Este bloque mueve datos
--    REALES de una base desplegada; en una base nueva las tablas viejas no existen y cada
--    INSERT hace cero filas.
--
--    `risk` sale del `payload` (la única copia que había) y si no está o no es del
--    vocabulario va a 'critical'. NUNCA a 'low': `SkillVersion.from_dict` convierte un risk
--    ausente en "low", y una skill de riesgo alto pasada por "low" se saltaría el
--    comprobación de `skill.py:310-318`.
-- Las tres fuentes de datos pueden no existir: una base donde alguien aplicó la
-- consolidación a mano, o una base donde 063 ya corrió y las tablas se borraron. Sin este
-- guardia, re-ejecutar la migración falla con "relation does not exist" en lugar de ser
-- un no-op. Es idempotencia de verdad, no sólo de las columnas.
DO $mig$
BEGIN
    IF to_regclass('skill_versions') IS NOT NULL THEN
        INSERT INTO skills (
            id, version, name, status, risk, validation, provenance, applicability,
            payload, created_at
        )
        SELECT
            sv.skill_id,
            sv.version,
            sv.name,
            sv.status,
            COALESCE(NULLIF(sv.payload->>'risk', ''), 'critical'),
            COALESCE(sv.payload->'validation', '{}'::jsonb),
            COALESCE(sv.payload->'provenance', '{}'::jsonb),
            COALESCE(sv.payload->>'applicability', ''),
            sv.payload,
            sv.created_at
        FROM skill_versions sv
        WHERE sv.skill_id IS NOT NULL
        ON CONFLICT (id, version) DO NOTHING;
    END IF;

    IF to_regclass('skill_candidates') IS NOT NULL THEN
        INSERT INTO skills (
            id, version, name, status, purpose, source_lessons, source_experiences,
            required_capabilities, expected_effects, failure_modes, applicability,
            proposed_version, provenance, risk, payload, created_at
        )
        SELECT
            sc.candidate_id,
            1,
            sc.name,
            'candidate',
            COALESCE(sc.payload->>'purpose', ''),
            COALESCE(sc.payload->'source_lessons', '[]'::jsonb),
            COALESCE(sc.payload->'source_experiences', '[]'::jsonb),
            COALESCE(sc.payload->'required_capabilities', '[]'::jsonb),
            COALESCE(sc.payload->'expected_effects', '[]'::jsonb),
            COALESCE(sc.payload->'failure_modes', '[]'::jsonb),
            COALESCE(sc.payload->>'applicability', ''),
            COALESCE((sc.payload->>'proposed_version')::int, 1),
            COALESCE(sc.payload->'provenance', '{}'::jsonb),
            COALESCE(NULLIF(sc.payload->>'risk', ''), 'critical'),
            sc.payload,
            sc.created_at
        FROM skill_candidates sc
        WHERE sc.candidate_id IS NOT NULL
        ON CONFLICT (id, version) DO NOTHING;
    END IF;

    IF to_regclass('skill_performance') IS NOT NULL THEN
        UPDATE skills s
        SET performance_stats = COALESCE((
                SELECT jsonb_agg(sp.payload ORDER BY sp.created_at)
                FROM skill_performance sp
                WHERE sp.skill_id = s.id AND sp.version = s.version
            ), '{}'::jsonb)
        WHERE EXISTS (SELECT 1 FROM skill_performance sp
                      WHERE sp.skill_id = s.id AND sp.version = s.version);
    END IF;
END
$mig$;

-- 3) Sólo ahora se borra. Con el orden correcto, un fallo en cualquier paso anterior
--    deja las tres tablas intactas y la migración sin registrar: se reintenta entera.
DROP TABLE IF EXISTS skill_performance;
DROP TABLE IF EXISTS skill_versions;
DROP TABLE IF EXISTS skill_candidates;
