"""Fase 2, incremento 2.0: aislamiento de la base de datos de tests.

El problema: `alexis.storage.db.DEFAULT_DSN` se evalúa **al importar el módulo**, así que
cualquier test que hiciera `Database()` sin argumentos heredaba la DSN de desarrollo y
escribía missions/observations/events en la base del runtime. Consecuencia observada:
la cola FIFO del runtime de desarrollo procesaba misiones de test y una misión real
quedaba `pending` minutos.

Mecanismo: **base de datos de test separada** (`alexis_test`) en el mismo servidor
PostgreSQL. No es una capa de abstracción nueva: es DSN + fixtures. Este módulo tiene
solo la lógica de aislamiento; `conftest.py` la conecta a las fixtures de pytest.

Protecciones:

1. **Guarda previa**: si la DSN de test resuelta apunta a la base de desarrollo, la
   sesión se detiene con `DatabaseIsolationError`.
2. **Red estructural**: `conftest` redirige `ALEXIS_DATABASE_URL` a la DSN de test antes
   de que se importe `alexis.storage.db`. Así, incluso un `Database()` desnudo dentro de
   un test —presente o futuro— no puede tocar la base de desarrollo. La DSN de desarrollo
   queda en `ALEXIS_DEV_DATABASE_URL` solo para comprobar el aislamiento.
3. **Limpieza**: el fixture `db` migra y trunca el esquema público antes de cada test.

El runtime de desarrollo no se toca: sigue leyendo su propio `ALEXIS_DATABASE_URL`.
"""

import re
from urllib.parse import urlsplit, urlunsplit

DEFAULT_TEST_DB_NAME = "alexis_test"
_SAFE_DB_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_EXCLUDED_TABLES = frozenset({"spatial_ref_sys"})


class DatabaseIsolationError(RuntimeError):
    """El entorno de test apunta a la base de datos de desarrollo."""


def database_name(dsn: str) -> str:
    if not dsn:
        return ""
    return (urlsplit(dsn).path or "").lstrip("/")


def with_database(dsn: str, name: str) -> str:
    """Misma URL, credenciales y puerto; otra base."""
    if not _SAFE_DB_NAME.match(name or ""):
        raise DatabaseIsolationError(f"nombre de base de datos no seguro: {name!r}")
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


def resolve_test_dsn(dev_dsn: str = "", explicit: str = "", name: str = "") -> str:
    """DSN de test: la explícita si viene dada, o la derivada de la de desarrollo."""
    explicit = explicit or ""
    if explicit:
        return explicit
    if not dev_dsn:
        return ""
    return with_database(dev_dsn, name or DEFAULT_TEST_DB_NAME)


def assert_isolated(test_dsn: str, dev_dsn: str) -> None:
    """Protección 1: la base de test nunca puede ser la de desarrollo."""
    if not test_dsn or not dev_dsn:
        return
    test_db = database_name(test_dsn)
    dev_db = database_name(dev_dsn)
    if test_db and test_db == dev_db:
        raise DatabaseIsolationError(
            f"ALEXIS_TEST_DATABASE_URL apunta a la base de desarrollo ({test_db!r}). "
            "Se aborta para no contaminarla. Usa una base distinta, p. ej. "
            f"{DEFAULT_TEST_DB_NAME!r}."
        )


def ensure_database_exists(dev_dsn: str, name: str) -> None:
    """Crea la base de test si falta. Idempotente."""
    if not _SAFE_DB_NAME.match(name or ""):
        raise DatabaseIsolationError(f"nombre de base de datos no seguro: {name!r}")
    import psycopg

    with psycopg.connect(dev_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            if cur.fetchone() is not None:
                return
            cur.execute(f'CREATE DATABASE "{name}"')
            print(f"[tests] base de datos de test creada: {name}")


async def truncate_all(db) -> None:
    """Limpieza total del esquema público de la base de test."""
    rows = await db.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    tables = [row["tablename"] for row in rows if row["tablename"] not in _EXCLUDED_TABLES]
    if not tables:
        return
    quoted = ", ".join(f'"{name}"' for name in tables)
    await db.execute(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE")
