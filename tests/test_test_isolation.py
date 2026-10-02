"""Fase 2, incremento 2.0: pruebas del aislamiento de la base de datos de tests.

No usan el fixture `db` a propósito: necesitan abrir DOS conexiones simultáneas, la de
test y la de desarrollo, para comprobar que son bases distintas.
"""

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from db_isolation import (  # noqa: E402
    DEFAULT_TEST_DB_NAME,
    DatabaseIsolationError,
    assert_isolated,
    database_name,
    resolve_test_dsn,
    truncate_all,
    with_database,
)

DEV_DSN = os.environ.get("ALEXIS_DEV_DATABASE_URL", "")
TEST_DSN = os.environ.get("ALEXIS_TEST_DATABASE_URL", "")

requires_db = pytest.mark.skipif(
    not DEV_DSN or not TEST_DSN,
    reason="Requiere ALEXIS_DATABASE_URL para comprobar el aislamiento",
)


# ----------------------------------------------------------------------
# Mecanismo: derivación y guardas (puro, sin BD)
# ----------------------------------------------------------------------


def test_derivation_keeps_credentials_host_and_port():
    dev = "postgresql://alexis:change-me@127.0.0.1:5433/alexis"
    derived = with_database(dev, DEFAULT_TEST_DB_NAME)

    assert derived == "postgresql://alexis:change-me@127.0.0.1:5433/alexis_test"
    assert database_name(derived) == "alexis_test"
    # El DSN derivado conserva usuario y contraseña, no solo host y puerto. La contraseña
    # del fixture es `change-me` a propósito: el detector de secretos la reconoce como
    # placeholder, así que un DSN de test no puede enmascarar una credencial real.
    assert "alexis:change-me@" in derived


def test_explicit_test_dsn_wins():
    explicit = "postgresql://u:change-me@host:5433/otro_test"
    assert resolve_test_dsn("postgresql://a:change-me@h:5433/alexis", explicit=explicit) == explicit


def test_test_database_is_not_the_development_database():
    dev = "postgresql://alexis:change-me@127.0.0.1:5433/alexis"
    assert database_name(resolve_test_dsn(dev)) != database_name(dev)


def test_guard_blocks_the_development_database():
    dev = "postgresql://alexis:change-me@127.0.0.1:5433/alexis"
    with pytest.raises(DatabaseIsolationError) as exc:
        assert_isolated(dev, dev)
    assert "desarrollo" in str(exc.value)


def test_guard_allows_a_different_database():
    dev = "postgresql://alexis:change-me@127.0.0.1:5433/alexis"
    assert_isolated(with_database(dev, DEFAULT_TEST_DB_NAME), dev)


def test_unsafe_database_name_is_rejected():
    with pytest.raises(DatabaseIsolationError):
        with_database("postgresql://u:change-me@h:5433/alexis", "alexis; DROP DATABASE postgres")
    with pytest.raises(DatabaseIsolationError):
        with_database("postgresql://u:change-me@h:5433/alexis", "Mayusculas")


# ----------------------------------------------------------------------
# Comportamiento real: dos bases, cero contaminación
# ----------------------------------------------------------------------


@requires_db
@pytest.mark.asyncio
async def test_test_data_is_invisible_to_the_development_database(db, dev_dsn):
    """Un test escribe en `alexis_test`; el runtime de desarrollo no lo ve."""
    import psycopg

    from alexis.autonomy.mission import MissionEngine
    from alexis.contracts import AutonomyLevel, MissionEnvelope
    from alexis.storage.repositories import EventRepository, MissionRepository

    mission = MissionEngine().create(
        "mision que solo existe en la base de test",
        MissionEnvelope(
            "mision que solo existe en la base de test",
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=["read"],
        ),
    )
    await MissionRepository(db).upsert(mission)
    await EventRepository(db).append("test.only", {"ok": True}, mission.id)

    assert (await MissionRepository(db).get(mission.id)) is not None

    with psycopg.connect(dev_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM missions WHERE id = %s", (mission.id,))
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM mission_events WHERE mission_id = %s", (mission.id,))
            assert cur.fetchone()[0] == 0


@requires_db
@pytest.mark.asyncio
async def test_test_suite_cannot_pollute_the_development_queue(db, dev_dsn):
    """La cola del runtime de desarrollo es la de `alexis`; los tests no la alimentan."""
    import psycopg

    from alexis.autonomy.mission import MissionEngine
    from alexis.contracts import AutonomyLevel, MissionEnvelope, MissionState
    from alexis.storage.repositories import MissionRepository

    def dev_counts():
        with psycopg.connect(dev_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM missions")
                total = cur.fetchone()[0]
                cur.execute(
                    "SELECT count(*) FROM missions WHERE state = %s", (MissionState.PENDING.value,)
                )
                pending = cur.fetchone()[0]
        return total, pending

    before = dev_counts()

    for index in range(5):
        mission = MissionEngine().create(
            f"carga de test {index}",
            MissionEnvelope(
                f"carga de test {index}",
                autonomy=AutonomyLevel.SUPERVISED,
                allowed_actions=["read"],
            ),
        )
        await MissionRepository(db).upsert(mission)

    after = dev_counts()

    assert after == before
    assert await db.fetch("SELECT count(*) AS n FROM missions") != [{"n": 0}]


@requires_db
@pytest.mark.asyncio
async def test_repeated_runs_do_not_accumulate_data(db):
    """Repetibilidad: el fixture limpia, así que la suite no acumula filas."""
    first = await db.fetch("SELECT count(*) AS n FROM missions")
    await db.execute(
        "INSERT INTO missions (id, goal, envelope, state, autonomy) "
        "VALUES ('x', '{}'::jsonb, '{}'::jsonb, 'pending', 'supervised')"
    )
    polluted = await db.fetch("SELECT count(*) AS n FROM missions")
    assert polluted[0]["n"] == (first[0]["n"] + 1)

    await truncate_all(db)
    cleaned = await db.fetch("SELECT count(*) AS n FROM missions")
    assert cleaned[0]["n"] == 0


@requires_db
@pytest.mark.asyncio
async def test_test_database_is_separate_from_the_development_one(db, dev_dsn):
    """Prueba de humo del aislamiento a nivel de conexión."""
    assert db.dsn == TEST_DSN
    assert db.dsn != dev_dsn
    assert database_name(db.dsn) != database_name(dev_dsn)
    current = await db.fetch("SELECT current_database() AS name")
    assert current[0]["name"] == database_name(TEST_DSN)
