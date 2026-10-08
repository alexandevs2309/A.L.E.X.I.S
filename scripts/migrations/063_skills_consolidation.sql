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

ALTER TABLE skills
    ADD COLUMN IF NOT EXISTS procedure JSONB NOT NULL DEFAULT '[]'::jsonb;

-- `contraindications` también venía de CORE-11 (`skill.py`), y su regla —una skill debe
-- declarar cuándo NO aplicar— es la que impide que una estrategia "siempre aplicable" se
-- disfrazara de aprendizaje.
ALTER TABLE skills
    ADD COLUMN IF NOT EXISTS contraindications JSONB NOT NULL DEFAULT '[]'::jsonb;

-- Sin dependencias entre las familias: `skill_versions.skill_id` y `skill_performance.skill_id`
-- eran texto libre sin FK, así que el borrado es limpio.
DROP TABLE IF EXISTS skill_performance;
DROP TABLE IF EXISTS skill_versions;
DROP TABLE IF EXISTS skill_candidates;