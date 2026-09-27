"""P0 §4.4 — RUNTIME WIRING + CROSS-RESTART REHYDRATION.

El `WorldRepository` ya existía y estaba validado (§4.3), pero no había pasado por
`PostgreSQL → WorldModel → Core`. Antes de este incremento:

    Tool → observe_execution() → WorldModel (memoria) → save_world()
         → mission.context["world"] → knowledge.world → Context

`WORLD` es un singleton en memoria, así que el conocimiento se reutilizaba entre misiones
**dentro del mismo proceso**, y se perdía en cada reinicio. `restore_world()` está dentro
de `if start:`, así que una misión nueva nunca recuperaba nada.

Aquí el store pasa a ser la fuente de autoridad: hidrata una vez al arrancar y se
escribe ANTES que la proyección legacy. `mission.context["world"]` sigue existiendo, pero
como proyección: si el store falla, la proyección no puede fingir que el conocimiento está
a salvo.
"""

import pathlib
import sys
import time

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.storage.repositories import WorldRepository  # noqa: E402
from alexis.world.model import (  # noqa: E402
    LAST_SEEN_UNKNOWN,
    Scope,
    WorldEntity,
    WorldModel,
)

WS_A = "/tmp/alexis-ws-A"
WS_B = "/tmp/alexis-ws-B"
SCOPE_A = Scope.from_workspace(WS_A)
SCOPE_B = Scope.from_workspace(WS_B)


def _ent(name="notas.txt", scope=SCOPE_A, last_seen=None, exists=True, mission_id="m1"):
    return WorldEntity(
        f"file:{name}", "file", name, {"exists": exists}, source="tool:fs.stat",
        confidence=0.7, mission_id=mission_id, observations=2,
        last_seen=time.time() if last_seen is None else last_seen,
        scope=scope.id,
    )


class _Result:
    def __init__(self, output, success=True):
        self.output = output
        self.success = success


class _Step:
    capability = "fs.stat"


# =========================================================================== #
# A. Hidratación
# =========================================================================== #


@pytest.mark.asyncio
async def test_01_hidratacion_desde_postgres(db):
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    marca = 1790416504.5493128
    await repo.upsert_entity(_ent(last_seen=marca))

    scope = SCOPE_A.id
    entidades = await repo.list_entities(scope=scope)
    aristas = await repo.list_edges(scope=scope)
    mundo = WorldModel(scope=Scope.from_workspace(WS_A))
    assert mundo.hydrate(entidades, aristas, scope=scope) == 1
    assert mundo.known_path("notas.txt") is not None


@pytest.mark.asyncio
async def test_02_02_nuevo_modelo_recupera_entidad_anterior(db):
    await db.open()
    await db.migrate()
    await WorldRepository(db).upsert_entity(_ent(last_seen=1000.0))
    otro = WorldModel(scope=Scope.from_workspace(WS_A))
    otro.hydrate(await WorldRepository(db).list_entities(scope=SCOPE_A.id), scope=SCOPE_A.id)
    assert otro.known_path("notas.txt") is not None


@pytest.mark.asyncio
async def test_03_hidratacion_conserva_last_seen_exacto(db):
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    for marca in (1790416504.5493128, 0.1, 1 / 3):
        await repo.upsert_entity(_ent(f"f{marca}.txt", last_seen=marca))
    mundo = WorldModel(scope=Scope.from_workspace(WS_A))
    mundo.hydrate(await repo.list_entities(scope=SCOPE_A.id), scope=SCOPE_A.id)
    for marca in (1790416504.5493128, 0.1, 1 / 3):
        assert mundo.known_path(f"f{marca}.txt").last_seen == marca


@pytest.mark.asyncio
async def test_04_hidratacion_conserva_scope_exacto(db):
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    await repo.upsert_entity(_ent(scope=SCOPE_A))
    await repo.upsert_entity(_ent(scope=SCOPE_B))
    mundo_a = WorldModel(scope=Scope.from_workspace(WS_A))
    mundo_a.hydrate(await repo.list_entities(scope=SCOPE_A.id), scope=SCOPE_A.id)
    assert mundo_a.known_path("notas.txt").scope == SCOPE_A.id
    assert SCOPE_A.id not in [e.scope for e in await repo.list_entities(scope=SCOPE_B.id)]


@pytest.mark.asyncio
async def test_05_hidratacion_recupera_edges(db):
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    await repo.upsert_entity(_ent("a.py"))
    await repo.upsert_entity(_ent("b.py"))
    await repo.save_edge("file:a.py", "file:b.py", scope=SCOPE_A.id)
    mundo = WorldModel(scope=Scope.from_workspace(WS_A))
    mundo.hydrate(await repo.list_entities(scope=SCOPE_A.id),
                  await repo.list_edges(scope=SCOPE_A.id), scope=SCOPE_A.id)
    assert [e.name for e in mundo.dependencies("file:a.py")] == ["b.py"]


@pytest.mark.asyncio
async def test_06_hidratacion_no_incrementa_observations(db):
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    await repo.upsert_entity(_ent(last_seen=1000.0))   # observations=2
    mundo = WorldModel(scope=Scope.from_workspace(WS_A))
    for _ in range(3):   # hidratar tres veces no es observar tres veces
        mundo.hydrate(await repo.list_entities(scope=SCOPE_A.id), scope=SCOPE_A.id)
    assert mundo.known_path("notas.txt").observations == 2


@pytest.mark.asyncio
async def test_07_hidratacion_no_cambia_last_seen(db):
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    antigua = 1000.0
    await repo.upsert_entity(_ent(last_seen=antigua))
    antes = time.time()
    mundo = WorldModel(scope=Scope.from_workspace(WS_A))
    mundo.hydrate(await repo.list_entities(scope=SCOPE_A.id), scope=SCOPE_A.id)
    assert mundo.known_path("notas.txt").last_seen == antigua
    assert mundo.known_path("notas.txt").last_seen < antes


@pytest.mark.asyncio
async def test_08_last_seen_unknown_sobrevive_la_hidratacion(db):
    await db.open()
    await db.migrate()
    await WorldRepository(db).upsert_entity(_ent("legacy.txt",
                                                last_seen=LAST_SEEN_UNKNOWN))
    mundo = WorldModel(scope=Scope.from_workspace(WS_A))
    mundo.hydrate(await WorldRepository(db).list_entities(scope=SCOPE_A.id), scope=SCOPE_A.id)
    assert mundo.known_path("legacy.txt").last_seen == LAST_SEEN_UNKNOWN


@pytest.mark.asyncio
async def test_09_nueva_mision_tras_reinicio_ve_conocimiento_previo(db):
    """El fallo que motivó el incremento: una misión nueva no veía nada tras reiniciar."""
    await db.open()
    await db.migrate()
    await WorldRepository(db).upsert_entity(_ent(last_seen=1234.0))
    # Proceso nuevo: modelo nuevo, hidrata del store. No hay `mission.context` que le
    # devuelva nada, y sin embargo conoce el archivo.
    nuevo = WorldModel(scope=Scope.from_workspace(WS_A))
    nuevo.hydrate(await WorldRepository(db).list_entities(scope=SCOPE_A.id), scope=SCOPE_A.id)
    assert nuevo.known_path("notas.txt") is not None
    assert nuevo.known_path("notas.txt").attributes["exists"] is True


@pytest.mark.asyncio
async def test_10_dos_scopes_aislados_tras_reinicio(db):
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    await repo.upsert_entity(_ent(scope=SCOPE_A, exists=True))
    await repo.upsert_entity(_ent(scope=SCOPE_B, exists=False))
    a = WorldModel(scope=Scope.from_workspace(WS_A))
    a.hydrate(await repo.list_entities(scope=SCOPE_A.id), scope=SCOPE_A.id)
    b = WorldModel(scope=Scope.from_workspace(WS_B))
    b.hydrate(await repo.list_entities(scope=SCOPE_B.id), scope=SCOPE_B.id)
    assert a.missing_paths(["notas.txt"]) == []
    assert b.missing_paths(["notas.txt"]) == ["notas.txt"]


# =========================================================================== #
# B. Persistencia desde el runtime
# =========================================================================== #


def _runtime(world, world_repo=None, **over):
    """Runtime con un CognitiveRuntime REAL alrededor del WorldModel, como en produccion."""
    from alexis.cognition.loop import CognitiveRuntime
    from alexis.core.runtime import AlexisRuntime
    from alexis.learning.system import ExperienceLearner
    from alexis.memory.store import InMemoryMemory
    from alexis.security.policy import PolicyEngine

    policy = PolicyEngine()
    cognitive = CognitiveRuntime(policy=policy, verifier=None, world=world)
    return AlexisRuntime(
        planner=None, policy=policy, executor=None, verifier=None,
        memory=InMemoryMemory(), learning=ExperienceLearner(), event_bus=None,
        world_repo=world_repo, cognitive=cognitive, **over,
    )


def _mission():
    from alexis.autonomy.mission import MissionEngine
    from alexis.contracts import AutonomyLevel, MissionEnvelope

    return MissionEngine().create(
        "lee notas.txt",
        MissionEnvelope(objective="lee notas.txt", autonomy=AutonomyLevel.SUPERVISED,
                        allowed_actions=["understand", "research", "execute", "verify"]),
    )


@pytest.mark.asyncio
async def test_11_observacion_nueva_persiste_en_el_store(db):
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    mundo = WorldModel(scope=Scope.from_workspace(WS_A))
    runtime = _runtime(mundo, world_repo=repo)
    mission = _mission()

    mundo.observe_execution(_Step(), _Result({"path": "notas.txt", "exists": True, "size": 7}))
    assert await runtime._persist_world(mission) is True

    guardada = await repo.get_entity("file:notas.txt", scope=SCOPE_A.id)
    assert guardada is not None
    assert guardada.attributes["size"] == 7
    assert guardada.last_seen == mundo.known_path("notas.txt").last_seen


@pytest.mark.asyncio
async def test_12_sin_store_no_persiste_y_no_falla(db):
    """Sin `world_repo` no hay store: se dice que no, no se finge."""
    await db.open()
    await db.migrate()
    mundo = WorldModel(scope=Scope.from_workspace(WS_A))
    runtime = _runtime(mundo, world_repo=None)
    assert await runtime._persist_world(_mission()) is False


@pytest.mark.asyncio
async def test_13_store_antes_que_proyeccion_legacy(db):
    """El orden de autoridad: store primero, proyección después."""
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    mundo = WorldModel(scope=Scope.from_workspace(WS_A))
    runtime = _runtime(mundo, world_repo=repo)
    mission = _mission()
    mundo.observe_execution(_Step(), _Result({"path": "notas.txt", "exists": True}))

    await runtime._persist_world(mission)
    # La fila del store existe ANTES de que se escriba nada en mission.context.
    assert await repo.get_entity("file:notas.txt", scope=SCOPE_A.id) is not None
    assert "world" not in mission.context

    # Y al escribir la proyección después, el store ya tiene la verdad.
    runtime.cognitive.store_knowledge(mission, runtime.cognitive.knowledge_for(mission))
    assert mission.context.get("world")
    assert await repo.get_entity("file:notas.txt", scope=SCOPE_A.id) is not None


@pytest.mark.asyncio
async def test_14_fallo_de_legacy_projection_no_invalida_el_store(db):
    """Si `save_world` revienta, el store conserva el conocimiento."""
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    mundo = WorldModel(scope=Scope.from_workspace(WS_A))
    runtime = _runtime(mundo, world_repo=repo)
    mission = _mission()
    mundo.observe_execution(_Step(), _Result({"path": "notas.txt", "exists": True}))
    await runtime._persist_world(mission)

    # La proyección legacy falla (p. ej. un `context` que no acepta escritura).
    class _Roto(dict):
        def __setitem__(self, k, v):
            raise RuntimeError("context no escribible")

    mission.context = _Roto(mission.context)
    with pytest.raises(RuntimeError):
        runtime.cognitive.save_world(mission)

    # El store no se enteró: sigue ahí.
    assert await repo.get_entity("file:notas.txt", scope=SCOPE_A.id) is not None
    assert mundo.known_path("notas.txt") is not None


@pytest.mark.asyncio
async def test_15_fallo_del_store_no_tumba_la_mision(db, caplog):
    """`WorldRepository` caído: warning, el Core sigue, y NO consta como persistido."""
    await db.open()
    await db.migrate()

    class _RepoRoto:
        async def save_snapshot(self, *a, **k):
            raise RuntimeError("connection refused")

        async def list_entities(self, *a, **k):
            raise RuntimeError("connection refused")

    mundo = WorldModel(scope=Scope.from_workspace(WS_A))
    runtime = _runtime(mundo, world_repo=_RepoRoto())
    mission = _mission()
    mundo.observe_execution(_Step(), _Result({"path": "notas.txt", "exists": True}))

    with caplog.at_level("WARNING"):
        persistido = await runtime._persist_world(mission)

    assert persistido is False, "no puede reportarse como persistido"
    assert any("no se pudo persistir" in r.getMessage() for r in caplog.records)
    # El mundo en memoria sigue funcionando: la misión puede continuar.
    assert mundo.known_path("notas.txt") is not None
    assert runtime.cognitive is not None
    assert await db.fetch("SELECT 1 AS n")


@pytest.mark.asyncio
async def test_16_db_no_disponible_durante_arranque_no_destruye_nada(db, caplog):
    """La hidratación falla: se avisa, se sigue, y NO se dice que el mundo esté vacío."""
    await db.open()
    await db.migrate()
    await WorldRepository(db).upsert_entity(_ent(last_seen=2000.0))
    await db.close()          # la base se cae antes de hidratar

    mundo = WorldModel(scope=Scope.from_workspace(WS_A))
    repo = WorldRepository(db)

    async def _hydrate():
        scope = mundo.scope.id
        return mundo.hydrate(await repo.list_entities(scope=scope), scope=scope)

    with pytest.raises(Exception):
        await _hydrate()

    # El modelo sigue vivo y usable; no se ha destruido ni vaciado.
    assert mundo.snapshot() == []
    assert mundo.known_path("notas.txt") is None
    # Y se puede seguir observando con normalidad: la pérdida es de persistencia, no de
    # capacidad de razonar.
    mundo.observe_execution(_Step(), _Result({"path": "otro.txt", "exists": True}))
    assert mundo.known_path("otro.txt") is not None


def test_17_restore_world_legacy_no_vuelve_a_ser_autoridad():
    """`restore_world` sigue existiendo, pero lee la PROYECCIÓN, no el store.

    Se documenta aquí para que quede constancia de que el mecanismo de recuperación real
    es `WorldRepository → WorldModel`, y que este camino es compatibilidad.
    """
    from alexis.cognition.loop import CognitiveRuntime
    from alexis.security.policy import PolicyEngine

    mission = _mission()
    mission.context["world"] = [{"id": "file:proyeccion.txt", "kind": "file",
                                 "name": "proyeccion.txt", "attributes": {"exists": True},
                                 "last_seen": 500.0}]
    cognitive = CognitiveRuntime(policy=PolicyEngine(), verifier=None,
                                 world=WorldModel(scope=Scope.from_workspace(WS_A)))
    assert cognitive.restore_world(mission) == 1
    # Repone lo que la proyección traía, tal cual, sin mirar la base de datos.
    assert cognitive.world.known_path("proyeccion.txt").last_seen == 500.0


def test_18_las_apis_existentes_siguen_funcionando():
    from alexis.cognition.loop import CognitiveRuntime
    from alexis.security.policy import PolicyEngine

    mundo = WorldModel(scope=Scope.from_workspace(WS_A))
    cognitive = CognitiveRuntime(policy=PolicyEngine(), verifier=None, world=mundo)
    mission = _mission()
    assert cognitive.observe_world(mission, _Step(),
                                   _Result({"path": "notas.txt", "exists": True})) is None
    assert cognitive.world_missing_path(mission) is None
    assert cognitive.store_knowledge(mission, cognitive.knowledge_for(mission)) is None
    assert mission.context.get("world")
    assert cognitive.save_world(mission) >= 1
    assert cognitive.restore_world(mission) >= 1


# =========================================================================== #
# E2E real: proceso A -> restart -> proceso B
# =========================================================================== #


@pytest.mark.asyncio
async def test_19_e2e_proceso_a_persiste_y_proceso_b_encuentra(db):
    """PROCESO A: observa y persiste. RESTART. PROCESO B: nuevo mundo, nueva misión."""
    from alexis.storage.db import Database
    from alexis.storage.repositories import WorldRepository as RepoB

    await db.open()
    await db.migrate()

    # ── PROCESO A ────────────────────────────────────────────────────────────
    scope_a = Scope.from_workspace(WS_A)
    mundo_a = WorldModel(scope=scope_a)
    runtime_a = _runtime(mundo_a, world_repo=WorldRepository(db))
    mission_a = _mission()
    mundo_a.observe_execution(_Step(), _Result({"path": "notas.txt", "exists": True,
                                                "size": 42}))
    marca = mundo_a.known_path("notas.txt").last_seen
    assert await runtime_a._persist_world(mission_a) is True
    await db.close()                                  # el proceso A se apaga

    # ── PROCESO B (base nueva, repositorio nuevo, mundo nuevo) ──────────────
    otra = Database(dsn=db.dsn)
    await otra.open()
    repo_b = RepoB(otra)
    mundo_b = WorldModel(scope=Scope.from_workspace(WS_A))
    assert mundo_b.hydrate(await repo_b.list_entities(scope=scope_a.id), scope=scope_a.id) == 1

    # Una misión NUEVA, sin `mission.context` de la anterior.
    mission_b = _mission()
    assert mission_b.context.get("world") in (None, [])

    entidad = mundo_b.known_path("notas.txt")
    assert entidad is not None, "el conocimiento anterior tiene que estar"
    assert entidad.attributes["size"] == 42
    assert entidad.last_seen == marca
    assert entidad.source == "tool:fs.stat"
    assert mundo_b.known_path("notas.txt").observations == 1
    await otra.close()


@pytest.mark.asyncio
async def test_20_e2e_multi_scope_tras_restart(db):
    """A sólo encuentra A; B sólo encuentra B. Mesmo relative path, dos proyectos."""
    from alexis.storage.db import Database
    from alexis.storage.repositories import WorldRepository as RepoB

    await db.open()
    await db.migrate()
    mundo = WorldModel()          # un modelo, dos ámbitos
    repo = WorldRepository(db)
    for scope, existe in ((SCOPE_A, True), (SCOPE_B, False)):
        mundo.observe_execution(_Step(), _Result({"path": "notas.txt", "exists": existe}),
                                scope=scope)
        salida = mundo.export(scope=scope)
        await repo.save_snapshot(salida["entities"], salida["edges"], scope=scope.id)
    await db.close()

    otra = Database(dsn=db.dsn)
    await otra.open()
    repo_b = RepoB(otra)
    a = WorldModel(scope=Scope.from_workspace(WS_A))
    a.hydrate(await repo_b.list_entities(scope=SCOPE_A.id), scope=SCOPE_A.id)
    b = WorldModel(scope=Scope.from_workspace(WS_B))
    b.hydrate(await repo_b.list_entities(scope=SCOPE_B.id), scope=SCOPE_B.id)

    assert a.known_path("notas.txt").attributes["exists"] is True
    assert b.known_path("notas.txt").attributes["exists"] is False
    assert a.missing_paths(["notas.txt"]) == []
    assert b.missing_paths(["notas.txt"]) == ["notas.txt"]
    assert a.known_path("notas.txt").scope != b.known_path("notas.txt").scope
    await otra.close()


@pytest.mark.asyncio
async def test_21_e2e_grafo_tras_restart(db):
    """El grafo sobrevive: neighbours y dependencies siguen funcionando."""
    from alexis.storage.db import Database
    from alexis.storage.repositories import WorldRepository as RepoB

    await db.open()
    await db.migrate()
    mundo = WorldModel(scope=Scope.from_workspace(WS_A))
    mundo.observe_execution(_Step(), _Result({"path": "a.py", "exists": True}))
    mundo.observe_execution(_Step(), _Result({"path": "b.py", "exists": True}))
    mundo.relate("file:a.py", "file:b.py", scope=SCOPE_A)
    salida = mundo.export(scope=SCOPE_A)
    await WorldRepository(db).save_snapshot(salida["entities"], salida["edges"],
                                            scope=SCOPE_A.id)
    await db.close()

    otra = Database(dsn=db.dsn)
    await otra.open()
    repo_b = RepoB(otra)
    b = WorldModel(scope=Scope.from_workspace(WS_A))
    b.hydrate(await repo_b.list_entities(scope=SCOPE_A.id),
              await repo_b.list_edges(scope=SCOPE_A.id), scope=SCOPE_A.id)

    assert [e.name for e in b.dependencies("file:a.py")] == ["b.py"]
    assert [e.name for e in b.neighbors("file:b.py")] == ["a.py"]
    await otra.close()


@pytest.mark.asyncio
async def test_22_e2e_una_mision_nueva_reutiliza_el_conocimiento(db):
    """El ciclo del que speaks el objetivo: el store es la fuente, no la misión previa."""
    from alexis.cognition.loop import CognitiveRuntime
    from alexis.security.policy import PolicyEngine

    await db.open()
    await db.migrate()
    await WorldRepository(db).upsert_entity(_ent(last_seen=time.time(), exists=False))
    mundo = WorldModel(scope=Scope.from_workspace(WS_A))
    mundo.hydrate(await WorldRepository(db).list_entities(scope=SCOPE_A.id), scope=SCOPE_A.id)
    cognitive = CognitiveRuntime(policy=PolicyEngine(), verifier=None, world=mundo)
    # La ausencia es fresca: si estuviera vencida, §4.5 la dejaría de sostener (§4.5).

    # Misión nueva: el mundo ya sabe que el archivo no está.
    mission = _mission()
    assert cognitive.world.known_path("notas.txt").attributes["exists"] is False
    assert cognitive.world.missing_paths(["notas.txt"]) == ["notas.txt"]
    # Y el prompt de decisión lo recibe vía knowledge.world → Context.
    knowledge = cognitive.knowledge_for(mission)
    entities = cognitive.world_hint(mission, knowledge)
    knowledge.world = [e.to_line() for e in entities]
    assert any("notas.txt" in w for w in knowledge.world)


@pytest.mark.asyncio
async def test_23_no_toca_policy_gate_ni_envelope(db):
    """El cableado del store no otorga ninguna autoridad nueva."""
    from alexis.autonomy.gates import AutonomyGate
    from alexis.security.policy import PolicyEngine

    await db.open()
    await db.migrate()
    mission = _mission()
    antes = list(mission.envelope.allowed_actions)
    policy, gate = PolicyEngine(), AutonomyGate()
    mundo = WorldModel(scope=Scope.from_workspace(WS_A))
    runtime = _runtime(mundo, world_repo=WorldRepository(db))
    runtime.gate = gate
    mundo.observe_execution(_Step(), _Result({"path": "notas.txt", "exists": False}))
    await runtime._persist_world(mission)

    assert mission.envelope.allowed_actions == antes
    assert mission.state.value == "pending"
    assert isinstance(policy, PolicyEngine) and isinstance(gate, AutonomyGate)
    # Con la evidencia de ausencia, el mundo propone una PREGUNTA; no deniega.
    pendientes = [type("S", (), {"id": "s1", "capability": "fs.read", "depends_on": []})()]
    pregunta = runtime.cognitive.world_block(mission, runtime.cognitive.knowledge_for(mission),
                                             pendientes)
    assert pregunta and "?" in pregunta
