"""P0 §4.5 (parcial) — CADUCIDAD DE LAS AUSENCIAS.

Reproduce, con la misma secuencia que se observó sirviendo el servidor:

1. Una misión lee un archivo que NO existe → el mundo registra `exists: False`.
2. El usuario crea el archivo.
3. El proceso se reinicia y el World Model se rehidrata desde PostgreSQL (§4.4).
4. Una misión nueva sobre ese archivo encuentralo en disco, pero ALEXIS respondía:

       state: waiting_clarification
       "observé con tool:verification.filesystem que «informe-trimestral.txt» no
        existe (evidencia, no suposición)"

Es decir: un hecho CADUCO convertible en un bloqueo, y con §4.4 durable entre reinicios.

La corrección: una observación de ausencia caduca. Pasado el plazo, el mundo deja de
sostener la ausencia y **se difiere a la herramienta**, que es quien puede comprobarlo.
Ante la duda, difiere: preguntar al usuario interrumpe su autonomía; un `fs.stat` devuelve
la verdad por un coste mínimo.
"""

import pathlib
import sys
import time

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.world.model import (  # noqa: E402
    ABSENCE_TTL_S,
    LAST_SEEN_UNKNOWN,
    Scope,
    WorldEntity,
    WorldModel,
)


def _ausente(path="fantasma.txt", edad=0.0, scope=""):
    """Una entidad de ausencia observada hace `edad` segundos."""
    return WorldEntity(
        f"file:{path}", "file", path, {"exists": False}, source="tool:fs.stat",
        confidence=0.7, last_seen=time.time() - edad, scope=scope,
    )


def _ausente_sin_fecha(path="fantasma.txt", scope=""):
    """Ausencia observada pero SIN fecha: una fila legacy (`LAST_SEEN_UNKNOWN`)."""
    return WorldEntity(
        f"file:{path}", "file", path, {"exists": False}, source="recovered",
        confidence=0.0, last_seen=LAST_SEEN_UNKNOWN, scope=scope,
    )


def _presente(path="notas.txt", edad=0.0, scope=""):
    return WorldEntity(
        f"file:{path}", "file", path, {"exists": True}, source="tool:fs.stat",
        confidence=0.7, last_seen=time.time() - edad, scope=scope,
    )


# --------------------------------------------------------------------------- #
# 1. Una ausencia fresca bloquea (el comportamiento legítimo se conserva)
# --------------------------------------------------------------------------- #


def test_01_ausencia_fresca_sigue_bloqueando():
    w = WorldModel()
    w.upsert(_ausente())
    assert w.observed_absent("fantasma.txt") is not None
    assert w.missing_paths(["fantasma.txt"]) == ["fantasma.txt"]


def test_02_la_ausencia_fresca_llega_hasta_el_bloqueo_del_core():
    """El punto de consumo real, no sólo el modelo."""
    from alexis.cognition.loop import CognitiveRuntime
    from alexis.security.policy import PolicyEngine
    from tests.test_world_model import _mission  # la misma misión que usa el test base

    w = WorldModel()
    w.upsert(_ausente())
    cognitive = CognitiveRuntime(policy=PolicyEngine(), verifier=None, world=w)
    assert cognitive.world_missing_path(_mission("lee el archivo fantasma.txt")) is not None


# --------------------------------------------------------------------------- #
# 2. Una ausencia vencida NO bloquea
# --------------------------------------------------------------------------- #


def test_03_ausencia_vencida_no_bloquea():
    w = WorldModel()
    w.upsert(_ausente(edad=ABSENCE_TTL_S + 1))
    assert w.observed_absent("fantasma.txt") is None
    assert w.missing_paths(["fantasma.txt"]) == []


def test_04_el_limite_es_inclusivo():
    """ justo en el plazo sigue bloqueando; un segundo más, no."""
    w = WorldModel()
    w.upsert(_ausente(edad=ABSENCE_TTL_S - 5))
    assert w.observed_absent("fantasma.txt") is not None


def test_05_ausencia_muy_antigua_no_bloquea():
    w = WorldModel()
    w.upsert(_ausente(edad=86_400))
    assert w.missing_paths(["fantasma.txt"]) == []


# --------------------------------------------------------------------------- #
# 3. Edad desconocida: no se puede afirmar que sea fresco
# --------------------------------------------------------------------------- #


def test_06_edad_desconocida_no_bloquea():
    w = WorldModel()
    w.upsert(_ausente_sin_fecha())
    assert w.known_path("fantasma.txt").attributes["exists"] is False, "la observación sigue ahí"
    assert w.observed_absent("fantasma.txt") is None, "pero no sostiene la ausencia"
    assert w.missing_paths(["fantasma.txt"]) == []


def test_07_legacy_restaurado_no_bloquea():
    """Una fila legacy (`LAST_SEEN_UNKNOWN`) no puede convertirse en un bloqueo."""
    w = WorldModel()
    w.upsert(_ausente_sin_fecha())
    entidad = WorldEntity.from_dict(w.known_path("fantasma.txt").to_dict())
    assert entidad.last_seen == LAST_SEEN_UNKNOWN
    assert w.is_absence_fresh(entidad) is False


# --------------------------------------------------------------------------- #
# 4. Una presencia vieja NO caduca
# --------------------------------------------------------------------------- #


def test_08_presencia_no_caduca():
    """El coste es asimétrico: una presencia vieja falla de forma honesta y barata."""
    w = WorldModel()
    w.upsert(_presente(edad=86_400))
    assert w.known_path("notas.txt") is not None
    assert w.is_absence_fresh(w.known_path("notas.txt")) is False
    # No es una ausencia, así que nunca bloquea ni por error ni por caducidad.
    assert w.observed_absent("notas.txt") is None
    assert w.missing_paths(["notas.txt"]) == []


# --------------------------------------------------------------------------- #
# 5. known_path sigue siendo la lectura CRUDA
# --------------------------------------------------------------------------- #


def test_09_known_path_no_cambia_de_semantica():
    """`GoalVerifier` la usa como evidencia sin mirar `exists`: no debe filtrarse."""
    w = WorldModel()
    w.upsert(_ausente(edad=86_400))
    assert w.known_path("fantasma.txt") is not None, "la observación sigue consultable"
    assert w.known_path("fantasma.txt").source == "tool:fs.stat"


# --------------------------------------------------------------------------- #
# 6. Aislamiento por scope intacto
# --------------------------------------------------------------------------- #


def test_10_la_caducidad_no_rompe_el_aislamiento():
    a, b = Scope.from_workspace("/tmp/a"), Scope.from_workspace("/tmp/b")
    w = WorldModel()
    w.upsert(_ausente(edad=10, scope=a.id))
    w.upsert(_ausente(edad=10, scope=b.id))
    assert w.missing_paths(["fantasma.txt"], scope=a) == ["fantasma.txt"]
    assert w.missing_paths(["fantasma.txt"], scope=b) == ["fantasma.txt"]


def test_11_ausencia_vencida_en_A_no_afecta_a_B():
    a, b = Scope.from_workspace("/tmp/a"), Scope.from_workspace("/tmp/b")
    w = WorldModel()
    w.upsert(_ausente(edad=10_000, scope=a.id))
    w.upsert(_ausente(edad=10, scope=b.id))
    assert w.missing_paths(["fantasma.txt"], scope=a) == []
    assert w.missing_paths(["fantasma.txt"], scope=b) == ["fantasma.txt"]


# --------------------------------------------------------------------------- #
# 7. La reproducción exacta: crear el archivo después
# --------------------------------------------------------------------------- #


def test_12_la_reproduccion_del_servidor():
    """Paso a paso, como se observó en el servidor real."""
    w = WorldModel()

    # 1) observación de ausencia
    w.upsert(_ausente("informe-trimestral.txt", edad=0.0))
    assert w.missing_paths(["informe-trimestral.txt"]) == ["informe-trimestral.txt"], \
        "inmediatamente después, el mundo tiene razón en bloquear"

    # 2) el usuario crea el archivo. El mundo no puede enterarse solo.
    # 3) pasa el tiempo
    w.upsert(w.known_path("informe-trimestral.txt"))     # misma entidad, ahora vieja
    entidad = w.known_path("informe-trimestral.txt")
    entidad.last_seen = time.time() - (ABSENCE_TTL_S + 30)
    w.put(entidad)

    # 4) ahora sí: el archivo existe y el mundo NO debe afirmar lo contrario
    assert w.missing_paths(["informe-trimestral.txt"]) == []
    assert w.observed_absent("informe-trimestral.txt") is None
    # La observación sigue disponible como dato, sólo ha dejado de mandar.
    assert w.known_path("informe-trimestral.txt").attributes["exists"] is False


def test_13_el_core_deja_de_bloquear_tras_vencer():
    """El efecto observable: la misión deja de acabar en `waiting_clarification`."""
    from alexis.cognition.loop import CognitiveRuntime
    from alexis.security.policy import PolicyEngine
    from tests.test_world_model import _mission

    w = WorldModel()
    w.upsert(_ausente(edad=ABSENCE_TTL_S + 60))
    cognitive = CognitiveRuntime(policy=PolicyEngine(), verifier=None, world=w)
    assert cognitive.world_missing_path(_mission("lee el archivo fantasma.txt")) is None
    assert cognitive.world_blocked_ids(
        _mission("lee el archivo fantasma.txt"),
        [type("S", (), {"id": "s1", "capability": "fs.read", "depends_on": []})()],
    ) == set()


# --------------------------------------------------------------------------- #
# 8. Sobrevive el ciclo de §4.4 (store → reinicio)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_14_la_ausencia_vencida_no_sobrevive_al_reinicio(db):
    """El problema era que la caducidad SÍ tiene que sobrevivir, y el store la respeta."""
    from alexis.storage.repositories import WorldRepository

    await db.open()
    await db.migrate()
    scope = Scope.from_workspace("/tmp/staleness-ws")
    repo = WorldRepository(db)

    # Observada ausente "hace un rato" y guardada.
    w = WorldModel(scope=scope)
    w.upsert(_ausente(edad=ABSENCE_TTL_S + 60, scope=scope.id))
    salida = w.export(scope=scope)
    await repo.save_snapshot(salida["entities"], salida["edges"], scope=scope.id)

    # Proceso nuevo: rehidrata y, aun así, no bloquea.
    rehidratado = WorldModel(scope=Scope.from_workspace("/tmp/staleness-ws"))
    entidades = await WorldRepository(db).list_entities(scope=scope.id)
    rehidratado.hydrate(entidades, scope=scope.id)

    assert rehidratado.known_path("fantasma.txt").attributes["exists"] is False
    assert rehidratado.missing_paths(["fantasma.txt"]) == [], \
        "el store preserva la observación, pero no su autoridad para bloquear"
    await db.close()


@pytest.mark.asyncio
async def test_15_ausencia_fresca_persistida_sigue_bloqueando(db):
    """El otro lado: lo fresco también tiene que funcionar tras reiniciar."""
    from alexis.storage.repositories import WorldRepository

    await db.open()
    await db.migrate()
    scope = Scope.from_workspace("/tmp/fresh-ws")
    w = WorldModel(scope=scope)
    w.upsert(_ausente(edad=0.0, scope=scope.id))
    salida = w.export(scope=scope)
    await WorldRepository(db).save_snapshot(salida["entities"], salida["edges"],
                                            scope=scope.id)
    rehidratado = WorldModel(scope=Scope.from_workspace("/tmp/fresh-ws"))
    rehidratado.hydrate(await WorldRepository(db).list_entities(scope=scope.id), scope=scope.id)
    assert rehidratado.missing_paths(["fantasma.txt"]) == ["fantasma.txt"]
    await db.close()


# --------------------------------------------------------------------------- #
# 9. Inyectabilidad del reloj
# --------------------------------------------------------------------------- #


def test_16_el_reloj_es_inyectable():
    """Sin esto, probar la caducidad exigiría dormir, y un test que duerme miente."""
    w = WorldModel()
    w.upsert(_ausente(edad=0.0))
    dentro = w.observed_absent("fantasma.txt", now=time.time() + ABSENCE_TTL_S - 1)
    fuera = w.observed_absent("fantasma.txt", now=time.time() + ABSENCE_TTL_S + 1)
    assert dentro is not None
    assert fuera is None
