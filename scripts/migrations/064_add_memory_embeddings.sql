-- Búsqueda semántica (vector) en la memoria episódica. MINDMAP §4, tipo Episodic.
--
-- `Database.migrate()` ejecuta este fichero DESPUÉS de `schema.SQL`, así que la columna
-- queda creada tanto en una base nueva (por `schema.SQL`, que la declara también) como en
-- una ya desplegada (por esta migración, que es la que puede añadir columnas a una tabla
-- existente: `CREATE TABLE IF NOT EXISTS` no las añade).
--
-- La duplicación con `schema.py` es deliberada y no una forgotura: `schema.SQL` es
-- idempotente y describe el estado completo de una base nueva; este fichero es el
-- historial para las que ya existían.
--
-- NULLABLE a propósito, y no es un detalle: la columna empieza a `NULL` para todas las
-- observaciones existentes. `PostgresMemoryProvider` ordena por distancia de coseno
-- cuando hay consulta vector y cae a coincidencia de términos cuando no la hay, así que
-- una base con observaciones sin vector sigue funcionando exactamente igual que antes.
-- Backfill de embeddings es un trabajo aparte y opcional.
--
-- Dimensión 384: la de all-MiniLM-L6-v2, el modelo local ligero de referencia. El
-- provider de embeddings es inyectable (`alexis/memory/embeddings.py`), de modo que
-- cambiar de modelo exige cambiar la dimensión AQUÍ también. Si se cambia sin migrar,
-- la consulta vector falla ruidosamente en vez de devolver basura silenciosa.

ALTER TABLE observations
    ADD COLUMN IF NOT EXISTS embedding vector(384);

-- HNSW con coseno: la métrica que usa `<=>`. Se declara AFTER INSERT, no CONCURRENTLY,
-- porque `migrate()` corre al arrancar y un CONCURRENTY exige transacción y ejecutarse fuera de
-- ella; para una tabla de observaciones esto no compensa.
CREATE INDEX IF NOT EXISTS ix_observations_embedding
    ON observations USING hnsw (embedding vector_cosine_ops);
