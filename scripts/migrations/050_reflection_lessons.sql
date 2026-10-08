-- CORE-13: lecciones estructuradas de ReflectionEngine.
ALTER TABLE lessons
    ADD COLUMN IF NOT EXISTS mission_id TEXT,
    ADD COLUMN IF NOT EXISTS trigger TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS insight TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS evidence_ids JSONB NOT NULL DEFAULT '[]'::jsonb;
