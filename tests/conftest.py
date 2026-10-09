"""Fase 2, incremento 2.0: fixtures que aíslan la base de datos de tests.

La lógica vive en `tests/db_isolation.py`; aquí solo se conecta a pytest y se redirige
el entorno. Este módulo se ejecuta UNA vez por sesión: la red estructural (redirigir
`ALEXIS_DATABASE_URL` antes de que se importe `alexis.storage.db`) depende de eso.

Protección 2 (red estructural): aunque un test haga `Database()` sin argumentos, la
DSN por defecto del proceso ya es la de test. La DSN de desarrollo queda disponible en
`ALEXIS_DEV_DATABASE_URL` únicamente para comprobar que el aislamiento funciona.
"""

import asyncio
import os
import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if str(pathlib.Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from db_isolation import (  # noqa: E402
    DEFAULT_TEST_DB_NAME,
    assert_isolated,
    database_name,
    ensure_database_exists,
    resolve_test_dsn,
    truncate_all,
)

DEV_DSN = os.environ.get("ALEXIS_DEV_DATABASE_URL") or os.environ.get("ALEXIS_DATABASE_URL", "")
TEST_DSN = ""

if DEV_DSN:
    TEST_DSN = resolve_test_dsn(
        DEV_DSN,
        explicit=os.environ.get("ALEXIS_TEST_DATABASE_URL", ""),
        name=os.environ.get("ALEXIS_TEST_DB_NAME", DEFAULT_TEST_DB_NAME),
    )
    assert_isolated(TEST_DSN, DEV_DSN)
    ensure_database_exists(DEV_DSN, database_name(TEST_DSN))
    os.environ["ALEXIS_DEV_DATABASE_URL"] = DEV_DSN
    os.environ["ALEXIS_TEST_DATABASE_URL"] = TEST_DSN
    os.environ["ALEXIS_DATABASE_URL"] = TEST_DSN


@pytest.fixture(scope="session")
def dev_dsn() -> str:
    if not DEV_DSN:
        pytest.skip("requiere ALEXIS_DATABASE_URL configurada")
    return DEV_DSN


@pytest.fixture(scope="session")
def test_dsn(dev_dsn: str) -> str:
    return TEST_DSN


@pytest.fixture
async def db(test_dsn: str):
    """Base de datos de test migrada y vacía. El runtime de desarrollo no la ve."""
    from alexis.storage.db import Database

    database = Database(test_dsn)
    await database.open()
    await database.migrate()
    await truncate_all(database)
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture(autouse=True)
async def _drain_pending_asyncio_tasks():
    """Limpieza cross-test: entierra las tareas que un test dejó en background.

    Sin esto, una tarea en background creada por un test (consumidores del EventBus,
    pools de DB, workers) se destruye durante el test SIGUIENTE, y con `-W error` el
    fallo se atribuye al test inocente en vez de al que fugó la tarea. No cambia la lógica
    de ningún test: sólo garantiza que cada test entierre a sus muertos.

    Se cancela lo que quede vivo en el loop del test. Los loops en BACKGROUND (los que
   某些 módulos abren con `new_event_loop()` + hilo) no son alcanzables desde aquí: los
    detiene el teardown de esos módulos con `stop_services()`.
    """
    import asyncio

    yield
    try:
        current = asyncio.current_task()
    except RuntimeError:
        return
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    for task in pending:
        task.cancel()
    if pending:
        try:
            await asyncio.gather(*pending, return_exceptions=True)
        except asyncio.CancelledError:
            pass
    # Cede un ciclo para que los callbacks de cierre se procesen aquí,
    # no en el test siguiente.
    await asyncio.sleep(0)
