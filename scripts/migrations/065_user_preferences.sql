-- User Model (módulo MISSING → COMPLETE).
--
-- Una SOLA tabla para las cuatro categorías. No hay `user_projects` ni
-- `user_decisions`: un proyecto activo es una preferencia de categoría `project` con
-- `value->>'active' = 'true'`, y una decisión es una preferencia de categoría `context`
-- con `value = {context, outcome}`. Cuatro tablas para un mismo par clave/valor obligarían a
-- un CRUD duplicado y a decidir en cada escritura si va a una u otra; una sola deja la
-- decisión en el VALOR, que es donde pertenece.
--
-- Idempotente: se aplica sobre `schema.SQL`, que ya la declara para bases nuevas.

CREATE TABLE IF NOT EXISTS user_preferences (
    id          TEXT PRIMARY KEY,
    category    TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       JSONB NOT NULL,
    confidence  FLOAT NOT NULL DEFAULT 1.0,
    source      TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(category, key)
);

-- Las categorías son un conjunto CERRADO y el comentario no lo puede hacer cumplir: sin
-- CHECK, un `categoria = 'proyects'` (con 's' de más) se escribe sin error y no lo lee
-- nadie. Se convierte el comentario en una restricción.
ALTER TABLE user_preferences DROP CONSTRAINT IF EXISTS user_preferences_category_check;
ALTER TABLE user_preferences
    ADD CONSTRAINT user_preferences_category_check
    CHECK (category IN ('project', 'style', 'approval', 'context'));

-- Igual con `source`: distingue lo que el USUITO dijo de lo que ALEXIS dedujo, y esa
-- diferencia es la que permite no presentar como hecho una inferencia.
ALTER TABLE user_preferences DROP CONSTRAINT IF EXISTS user_preferences_source_check;
ALTER TABLE user_preferences
    ADD CONSTRAINT user_preferences_source_check
    CHECK (source IS NULL OR source IN ('explicit', 'inferred', 'default'));

-- `confidence` fuera de [0, 1] no es "mucha confianza": es un dato corrupto que ordena
-- mal y que un `MAX()` elige como la más fiable.
ALTER TABLE user_preferences DROP CONSTRAINT IF EXISTS user_preferences_confidence_check;
ALTER TABLE user_preferences
    ADD CONSTRAINT user_preferences_confidence_check
    CHECK (confidence >= 0.0 AND confidence <= 1.0);

CREATE INDEX IF NOT EXISTS idx_user_prefs_category ON user_preferences(category);