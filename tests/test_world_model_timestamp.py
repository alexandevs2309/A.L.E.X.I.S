"""P0 §4.2 — TIMESTAMP PERSISTENCE del World Model.

`WorldEntity` ya tenía `last_seen`, pero `to_dict()` no lo escribía. El ciclo real era:

    observe at T1 -> to_dict() -> (mission.context) -> restore -> last_seen ~= NOW

La edad real de la observación se perdía en cada guardado, y una entidad restaurada
parecía recién vista. Eso es exactamente el riesgo "conocimiento obsoleto tratado como
actual" que §4.5 (staleness) tiene que poder detectar: si al restaurar la edad es cero,
no hay forma de distinguir "lo acabo de ver" de "no sé cuándo lo vi".

Este incremento sólo preserva el tiempo. No decide nada sobre él.
"""

import json
import pathlib
import sys
import time

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.cognition.loop import CognitiveRuntime  # noqa: E402
from alexis.cognition.state import KnowledgeState  # noqa: E402
from alexis.security.policy import PolicyEngine  # noqa: E402
from alexis.world.model import (  # noqa: E402
    LAST_SEEN_UNKNOWN,
    Scope,
    WorldEntity,
    WorldModel,
)


def _entity(name="notas.txt", last_seen=None, scope="", exists=True):
    return WorldEntity(
        f"file:{name}", "file", name, {"exists": exists}, source="tool:fs.stat",
        confidence=0.7, last_seen=last_seen if last_seen is not None else time.time(),
        scope=scope,
    )


# --------------------------------------------------------------------------- #
# 1-4. to_dict / from_dict: ida y vuelta
# --------------------------------------------------------------------------- #


def test_01_to_dict_incluye_last_seen():
    marca = 1790416504.5493128
    fila = _entity(last_seen=marca).to_dict()
    assert "last_seen" in fila
    assert fila["last_seen"] == marca


def test_02_from_dict_conserva_last_seen_exactamente():
    marca = 1790416504.5493128
    original = _entity(last_seen=marca)
    restaurada = WorldEntity.from_dict(original.to_dict())
    assert restaurada.last_seen == marca
    assert restaurada.last_seen == original.last_seen, "igualdad estricta, no aproximada"


def test_03_conserva_precision_de_float():
    """Un float64 tiene ~15-17 dígitos significativos; deben sobrevivir todos."""
    for marca in (0.1, 1 / 3, 1790416504.5493128, 1.7976931348623157e308, 5e-324):
        original = _entity(last_seen=marca)
        vuelta = WorldEntity.from_dict(original.to_dict()).last_seen
        assert vuelta == marca, f"{marca!r} no sobrevivio: {vuelta!r}"
    # Y el viaje por JSON, que es el que hace `mission.context`.
    marca = 1790416504.5493128
    via_json = json.loads(json.dumps({"last_seen": marca}))["last_seen"]
    assert via_json == marca
    ida_y_vuelta = WorldEntity.from_dict(json.loads(json.dumps(_entity(last_seen=marca).to_dict())))
    assert ida_y_vuelta.last_seen == marca


def test_04_conserva_el_scope_de_4_1():
    scope = Scope.from_workspace("/tmp/proyecto")
    original = _entity(last_seen=1000.0, scope=scope.id)
    fila = original.to_dict()
    assert fila["scope"] == scope.id
    assert WorldEntity.from_dict(fila).scope == scope.id


def test_05_todos_los_campos_sobreviven():
    original = WorldEntity("file:x.txt", "file", "x.txt", {"exists": False, "size": 3},
                           source="tool:fs.stat", confidence=0.7, mission_id="m1",
                           observations=4, last_seen=1234.5, scope="abc")
    fila = original.to_dict()
    for campo in ("id", "kind", "name", "attributes", "source", "confidence",
                  "mission_id", "observations", "scope", "last_seen"):
        assert campo in fila, f"falta {campo}"
    vuelta = WorldEntity.from_dict(fila)
    for campo in ("id", "kind", "name", "attributes", "source", "confidence",
                  "mission_id", "observations", "scope", "last_seen"):
        assert getattr(vuelta, campo) == getattr(original, campo), campo


# --------------------------------------------------------------------------- #
# 5-7. restore_world y el reinicio
# --------------------------------------------------------------------------- #


def _cognitive(world):
    return CognitiveRuntime(policy=PolicyEngine(), verifier=None, world=world)


def _mission():
    from alexis.autonomy.mission import MissionEngine
    from alexis.contracts import AutonomyLevel, MissionEnvelope

    return MissionEngine().create(
        "restaurar el mundo",
        MissionEnvelope(objective="restaurar el mundo", autonomy=AutonomyLevel.SUPERVISED,
                        allowed_actions=["understand", "research", "execute", "verify"]),
    )


def test_06_restore_world_conserva_last_seen():
    world = WorldModel(scope=Scope.from_workspace("/tmp/a"))
    marca = 1790416504.5493128
    world.upsert(_entity(last_seen=marca, scope=world.scope.id))
    mission = _mission()
    _cognitive(world).save_world(mission)   # serializa como lo hace el runtime
    assert mission.context["world"][0]["last_seen"] == marca

    otro = WorldModel(scope=Scope.from_workspace("/tmp/a"))   # "otro proceso"
    cognitive = _cognitive(otro)
    assert cognitive.restore_world(mission) == 1
    assert otro.known_path("notas.txt").last_seen == marca


def test_07_un_restart_no_convierte_last_seen_en_now():
    """El contrato central del incremento: la edad no se reinicia."""
    world = WorldModel(scope=Scope.from_workspace("/tmp/a"))
    antigua = time.time() - 86_400          # ayer
    world.upsert(_entity(last_seen=antigua, scope=world.scope.id))
    mission = _mission()
    _cognitive(world).save_world(mission)

    otro = WorldModel(scope=Scope.from_workspace("/tmp/a"))
    _cognitive(otro).restore_world(mission)
    recuperado = otro.known_path("notas.txt").last_seen
    assert recuperado == antigua
    assert time.time() - recuperado >= 86_000, "debe seguir siendo de ayer"


def test_08_dos_entidades_conservansus_timestamps_independientes():
    world = WorldModel(scope=Scope.from_workspace("/tmp/a"))
    t_a, t_b = 1000.0, 2000.0
    world.upsert(_entity("a.txt", last_seen=t_a, scope=world.scope.id))
    world.upsert(_entity("b.txt", last_seen=t_b, scope=world.scope.id))
    assert world.known_path("a.txt").last_seen == t_a
    assert world.known_path("b.txt").last_seen == t_b


def test_09_upsert_no_aplana_el_timestamp_al_reexecutar():
    """Reobservar actualiza `last_seen`; la entidad anterior queda con SU fecha."""
    world = WorldModel(scope=Scope.from_workspace("/tmp/a"))
    viejo = time.time() - 1000
    world.upsert(_entity(last_seen=viejo, scope=world.scope.id))
    fila_antes = dict(world.known_path("notas.txt").to_dict())
    world.upsert(_entity(last_seen=time.time(), scope=world.scope.id))
    assert world.known_path("notas.txt").last_seen > viejo
    assert fila_antes["last_seen"] == viejo, "el snapshot anterior sigue con su fecha"


# --------------------------------------------------------------------------- #
# 8. Compatibilidad legacy: comportamiento EXPLÍCITO
# --------------------------------------------------------------------------- #


def test_10_legacy_sin_last_seen_usa_el_centinela_explicito():
    fila = {"id": "file:notas.txt", "kind": "file", "name": "notas.txt",
            "attributes": {"exists": True}, "source": "recovered"}
    entidad = WorldEntity.from_dict(fila)
    assert entidad.last_seen == LAST_SEEN_UNKNOWN
    assert entidad.last_seen != time.time(), "no puede parecer recién observada"


def test_11_legacy_no_inventa_el_ahora():
    antes = time.time()
    entidad = WorldEntity.from_dict({"id": "file:x", "name": "x"})
    assert entidad.last_seen == LAST_SEEN_UNKNOWN
    assert entidad.last_seen < antes


@pytest.mark.parametrize("malo", [None, "", "no-es-un-número", {}, [], True])
def test_12_last_seen_invalido_tambien_es_desconocido(malo):
    entidad = WorldEntity.from_dict({"id": "file:x", "name": "x", "last_seen": malo})
    assert entidad.last_seen == LAST_SEEN_UNKNOWN


def test_13_restore_world_con_filas_legacy():
    """Las filas viejas de `mission.context["world"]` se restauran sin inventar fecha."""
    world = WorldModel(scope=Scope.from_workspace("/tmp/a"))
    mission = _mission()
    # Fila tal como la escribía el código anterior a §4.2: sin last_seen, sin scope.
    mission.context["world"] = [
        {"id": "file:notas.txt", "kind": "file", "name": "notas.txt",
         "attributes": {"exists": False}, "source": "recovered",
         "confidence": 0.7, "mission_id": "m1", "observations": 2},
    ]
    cognitive = _cognitive(world)
    assert cognitive.restore_world(mission) == 1
    e = world.known_path("notas.txt")
    assert e is not None
    assert e.last_seen == LAST_SEEN_UNKNOWN
    assert e.scope == world.scope.id, "sin scope en la fila, entra en el de la instancia"
    assert e.observations == 2, "las filas legacy se reponen tal cual"


def test_14_legacy_y_moderna_conviven():
    world = WorldModel(scope=Scope.from_workspace("/tmp/a"))
    moderna = 1790416504.5
    w = WorldModel(scope=world.scope)
    w.put(_entity("vieja.txt", last_seen=LAST_SEEN_UNKNOWN, scope=w.scope.id))
    w.put(_entity("nueva.txt", last_seen=moderna, scope=w.scope.id))
    assert w.known_path("vieja.txt").last_seen == LAST_SEEN_UNKNOWN
    assert w.known_path("nueva.txt").last_seen == moderna


# --------------------------------------------------------------------------- #
# 9. Las APIs existentes siguen funcionando
# --------------------------------------------------------------------------- #


def test_15_las_apis_de_scope_de_4_1_siguen_iguales():
    a, b = Scope.from_workspace("/tmp/a"), Scope.from_workspace("/tmp/b")
    w = WorldModel()
    w.upsert(_entity("notas.txt", last_seen=1.0, scope=a.id))
    w.upsert(_entity("notas.txt", last_seen=2.0, scope=b.id))
    assert w.known_path("notas.txt", scope=a).last_seen == 1.0
    assert w.known_path("notas.txt", scope=b).last_seen == 2.0
    assert w.missing_paths(["notas.txt"], scope=b) == []
    assert len(w.query(scope=a)) == 1
    assert w.to_prompt_lines(scope=a) and "notas.txt" in w.to_prompt_lines(scope=a)[0]


def test_16_save_world_lleva_el_timestamp_en_la_fila():
    world = WorldModel(scope=Scope.from_workspace("/tmp/a"))
    marca = 1790416504.5493128
    world.upsert(_entity(last_seen=marca, scope=world.scope.id))
    mission = _mission()
    _cognitive(world).save_world(mission)
    assert [f["last_seen"] for f in mission.context["world"]] == [marca]


def test_17_el_mundo_sigue_siendo_una_fuente_de_conocimiento():
    """`last_seen` es informativo mientras la observación sea fresca.

    §4.2 lo dejó así a propósito, con la nota de que §4.5 haría que la edad influyera.
    §4.5 (caducidad de ausencias) ya existe: una ausencia VENCIDA deja de bloquear, y
    una fresca sigue bloqueando. Las dos mitades se comprueban aquí.
    """
    from alexis.world.model import ABSENCE_TTL_S

    # Fresca: el mundo tiene razón en sostener la ausencia.
    fresco = WorldModel(scope=Scope.from_workspace("/tmp/a"))
    fresco.upsert(_entity("notas.txt", exists=False, last_seen=time.time(),
                          scope=fresco.scope.id))
    assert fresco.missing_paths(["notas.txt"]) == ["notas.txt"]

    # Vencida: ya no la sostiene, y se difiere a la herramienta. Va en un mundo APARTE,
    # y no "envejeciendo" el anterior, porque desde §4.5.5 `last_seen` es el MÁXIMO de los
    # instantes observados: una observación posterior no puede rebobinar el reloj de la
    # entidad. Envejecer la que ya era fresca la dejaría, con razón, tan fresca como antes.
    # La caducidad se comprueba sobre una ausencia que ya arrastra el TTL de atraso.
    world = WorldModel(scope=Scope.from_workspace("/tmp/a"))
    world.upsert(_entity("notas.txt", exists=False,
                         last_seen=time.time() - ABSENCE_TTL_S - 10,
                         scope=world.scope.id))
    assert world.missing_paths(["notas.txt"]) == []
    # Y la observación sigue consultable: caducar no es borrar.
    assert world.known_path("notas.txt").attributes["exists"] is False


def test_18_una_observacion_tardia_no_refresca_lo_que_ya_se_vio():
    """La regla que sostiene lo anterior, comprobada en la dirección que importa.

    Un resultado encolado, un reintento o un evento que llega tarde trae una marca de
    tiempo antigua. No debe rejuvenecer la entidad: si lo hiciera, una ausencia vencida
    volvería a bloquearse y el TTL dejaría de significar nada.
    """
    from alexis.world.model import ABSENCE_TTL_S

    world = WorldModel(scope=Scope.from_workspace("/tmp/a"))
    world.upsert(_entity("notas.txt", exists=False, last_seen=time.time(),
                         scope=world.scope.id))
    antes = world.known_path("notas.txt").last_seen

    world.upsert(_entity("notas.txt", exists=False,
                         last_seen=time.time() - ABSENCE_TTL_S - 10,
                         scope=world.scope.id))
    assert world.known_path("notas.txt").last_seen == antes
    assert world.missing_paths(["notas.txt"]) == ["notas.txt"], (
        "la observación tardía no debe resucitar la ausencia"
    )


# --------------------------------------------------------------------------- #
# 10. No toca la autoridad
# --------------------------------------------------------------------------- #


def test_18_no_cambia_policy_gate_ni_envelope():
    from alexis.autonomy.gates import AutonomyGate
    from alexis.contracts import MissionState

    mission = _mission()
    envelope_antes = list(mission.envelope.allowed_actions)
    policy, gate = PolicyEngine(), AutonomyGate()
    cognitive = CognitiveRuntime(policy=policy, gate=gate, verifier=None,
                                 world=WorldModel(scope=Scope.from_workspace("/tmp/a")))
    assert cognitive.policy is policy and cognitive.gate is gate
    assert mission.envelope.allowed_actions == envelope_antes
    assert mission.state is MissionState.PENDING


# --------------------------------------------------------------------------- #
# El viaje real: por JSONB de PostgreSQL
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_19_last_seen_sobrevive_a_postgresreal(db):
    """El ciclo completo con la BD: guardar la misión y releerla en otra conexión.

    Es el caso que importa: si el float perdiera un bit al pasar por JSONB, la edad
    quedaría corrupta justo en el reinicio, que es cuando se necesita.
    """
    from alexis.storage.repositories import MissionRepository

    await db.open()
    await db.migrate()
    repo = MissionRepository(db)
    mission = _mission()
    mundo = WorldModel(scope=Scope.from_workspace("/tmp/a"))
    marcas = [1790416504.5493128, 0.1, 1 / 3, 1234567890.123456]
    for i, marca in enumerate(marcas):
        mundo.upsert(_entity(f"f{i}.txt", last_seen=marca, scope=mundo.scope.id))
    _cognitive(mundo).save_world(mission)
    await repo.upsert(mission)

    # "Reinicio": otra conexión, otro WorldModel, otra instancia del Core.
    from alexis.storage.db import Database

    otra = Database(dsn=db.dsn)
    await otra.open()
    releida = await MissionRepository(otra).get(mission.id)
    assert releida is not None

    restaurado = WorldModel(scope=Scope.from_workspace("/tmp/a"))
    assert _cognitive(restaurado).restore_world(releida) == len(marcas)
    for i, marca in enumerate(marcas):
        assert restaurado.known_path(f"f{i}.txt").last_seen == marca, f"f{i}.txt"
    await otra.close()
    await db.close()
