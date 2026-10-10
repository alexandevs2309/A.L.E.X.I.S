import os
from contextlib import asynccontextmanager
from pathlib import Path

from psycopg.rows import dict_row

from alexis.storage import schema

DEFAULT_DSN = os.environ.get(
    "ALEXIS_DATABASE_URL",
    "postgresql://alexis:change-me@localhost:5433/alexis",
)

#: `<repo>/scripts/migrations`. Se resuelve desde este fichero y no desde el CWD: el
#: arranque ocurre desde el servidor HTTP, desde los tests y desde CLI, y el directorio
#: de trabajo no es el mismo en los tres.
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "migrations"

#: Migraciones que `migrate()` NO aplica automáticamente.
#:
#: No es una lista de "pendientes": es una lista de **destructivas**. Una migración que
#: hace `DROP TABLE` sobre tablas que el código consulta no es un cambio de esquema, es una
#: eliminación de datos en producción, y un arranque no debe decidir eso solo.
#:
#: 063 consolida el Skill System: crea `skills` y borra `skill_candidates`,
#: `skill_versions` y `skill_performance`. Las tres las lee
#: `alexis/storage/repositories.py`, así que aplicarla automáticamente dejaría el Skill
#: System con "relation does not exist". La consolidación del lado del CÓDIGO nunca se
#: terminó —los repositorios siguen leyendo las tablas viejas—, así que la migración no
#: puede precederla.
#:
#: Para desbloquearla hay que migrar el código a `skills` primero, y entonces quitarla de
#: aquí en el mismo commit.
QUARANTINED_MIGRATIONS = {
    "063_skills_consolidation.sql": (
        "hace DROP TABLE sobre skill_candidates/skill_versions/skill_performance, "
        "que alexis/storage/repositories.py sigue consultando"
    ),
}


class Database:
    def __init__(self, dsn: str = DEFAULT_DSN):
        self.dsn = dsn
        self._pool = None

    async def open(self):
        from psycopg_pool import AsyncConnectionPool

        if self._pool is None:
            self._pool = AsyncConnectionPool(self.dsn, min_size=1, max_size=5, open=False)
            await self._pool.open()
        elif self._pool.closed:
            await self._pool.open()
        await self.execute("SELECT 1")

    async def close(self):
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def execute(self, query, params=None):
        async with self._pool.connection() as conn:
            await conn.execute(query, params)

    async def fetch(self, query, params=None):
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(query, params)
                return await cur.fetchall()

    @asynccontextmanager
    async def transaction(self):
        """Un ÚNICO contexto transaccional, tomado del driver.

        `execute()` abre y cierra una conexión del pool por sentencia, así que varias
        llamadas NO son atómicas. Para lo que debe serlo —guardar las entidades y sus
        relaciones del World Model como una unidad— hace falta una conexión fija y la
        transacción que el propio driver ofrece. No es un sistema de transacciones
        paralelo: es el de psycopg, expuesto una vez.

        Uso:
            async with db.transaction() as conn:
                await conn.execute(sql, params)
        """
        async with self._pool.connection() as conn:
            async with conn.transaction():
                yield conn

    async def migrate(self) -> dict:
        """Esquema base y después las migraciones pendientes, en orden y una sola vez.

        `schema.SQL` sigue siendo la fuente de verdad del ESQUEMA y es idempotente; los
        ficheros de `scripts/migrations/` son el historial. Se aplican después y sólo una
        vez, registrados en `migrations_applied`.

        Devuelve un resumen para que quien llama (y los tests) puedan comprobar qué pasó en
        vez de asumir que todo fue bien:

            {"applied": [...], "skipped": [...], "quarantined": {nombre: motivo},
             "failed": (nombre, error) | None}
        """
        await self.open()
        await self.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await self.execute(schema.SQL)

        applied: list[str] = []
        skipped: list[str] = []
        failed: tuple[str, str] | None = None

        for name in self.migration_files():
            if name in QUARANTINED_MIGRATIONS:
                continue
            if await self._migration_applied(name):
                skipped.append(name)
                continue
            try:
                await self._apply_migration(name)
            except Exception as exc:  # noqa: BLE001 — se reporta el nombre y el motivo
                # Se detiene aquí. Seguir aplicaría las siguientes sobre un estado a
                # medias, que es peor que no haber aplicado ninguna.
                failed = (name, f"{type(exc).__name__}: {exc}")
                break
            applied.append(name)

        return {
            "applied": applied,
            "skipped": skipped,
            "quarantined": dict(QUARANTINED_MIGRATIONS),
            "failed": failed,
        }

    @staticmethod
    def migration_files() -> list[str]:
        """Nombres de migración ordenados. El prefijo numérico define el orden."""
        if not MIGRATIONS_DIR.is_dir():
            return []
        return sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))

    async def _migration_applied(self, name: str) -> bool:
        filas = await self.fetch(
            "SELECT 1 FROM migrations_applied WHERE migration_name = %(name)s", {"name": name}
        )
        return bool(filas)

    async def _apply_migration(self, name: str) -> None:
        """Ejecuta un fichero y lo registra, en la MISMA transacción.

        Juntas a propósito: si el `INSERT` de registro fallara después de ejecutar el
        SQL, la migración se reaplicaría en el siguiente arranque. Con un `DROP TABLE`
        de por medio, eso ya no sería un reintento idempotente.
        """
        sql = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
        async with self.transaction() as conn:
            await conn.execute(sql)
            await conn.execute(
                "INSERT INTO migrations_applied (migration_name) VALUES (%(name)s) "
                "ON CONFLICT (migration_name) DO NOTHING",
                {"name": name},
            )