"""P0 §4.3 — WORLD STORE PERSISTENTE (PostgreSQL).

`mission.context["world"]` era el almacén del mundo: 50 filas, en el contexto de la
misión, y se perdía con ella. Este incremento mete el World Model en PostgreSQL, con su
propio store, respetando los dos incrementos previos como contrato:

- **Identidad** `(scope_id, entity_id)`, que es la clave primaria de la tabla.
- **Temporalidad** `last_seen` en DOUBLE PRECISION, que vuelve exacto; `0.0` sigue
  significando "edad desconocida" y NUNCA se convierte en `now()`.

La tabla `observations` no se toca: allí vive la evidencia cruda con su procedencia; aquí
el estado ya interpretado. Son cosas distintas.
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

SCOPE_A = Scope.from_workspace("/tmp/proyecto-a")
SCOPE_B = Scope.from_workspace("/tmp/proyecto-b")


def _ent(entity_id="file:notas.txt", name="notas.txt", scope=SCOPE_A, last_seen=None,
         kind="file", **attrs):
    attributes = {"exists": True}
    attributes.update(attrs)
    return WorldEntity(
        entity_id, kind, name, attributes, source="tool:fs.stat", confidence=0.7,
        mission_id="m1", observations=2,
        last_seen=time.time() if last_seen is None else last_seen,
        scope=scope.id if isinstance(scope, Scope) else scope,
    )


# --------------------------------------------------------------------------- #
# 1-2. Schema
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_01_schema_crea_world_entities(db):
    await db.open()
    await db.migrate()
    rows = await db.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'world_entities' ORDER BY column_name"
    )
    columnas = {r["column_name"]: r["data_type"] for r in rows}
    for c in ("scope_id", "entity_id", "kind", "name", "attributes", "source",
              "confidence", "mission_id", "observations", "last_seen"):
        assert c in columnas, f"falta {c}"
    # float8, no float4: con REAL se perdería precisión (§4.2)
    assert columnas["last_seen"] == "double precision"
    await db.close()


@pytest.mark.asyncio
async def test_02_schema_crea_world_edges(db):
    await db.open()
    await db.migrate()
    rows = await db.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'world_edges' ORDER BY column_name"
    )
    assert {r["column_name"] for r in rows} >= {"scope_id", "parent_id", "child_id", "relation"}
    await db.close()


@pytest.mark.asyncio
async def test_03_la_clave_primaria_es_compuesta(db):
    """`file:notas.txt` en dos ámbitos son dos filas, no un conflicto."""
    await db.open()
    await db.migrate()
    rows = await db.fetch(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = 'world_entities'::regclass AND i.indisprimary ORDER BY a.attnum"
    )
    assert [r["attname"] for r in rows] == ["scope_id", "entity_id"]
    await db.close()


# --------------------------------------------------------------------------- #
# 3-5. save/load, scope, last_seen
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_04_save_load_entity(db):
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    e = _ent()
    await repo.upsert_entity(e)
    vuelta = await repo.get_entity("file:notas.txt", scope=SCOPE_A.id)
    assert vuelta is not None
    assert vuelta.id == e.id and vuelta.kind == e.kind and vuelta.name == e.name
    assert vuelta.attributes == e.attributes
    assert vuelta.source == e.source and vuelta.confidence == e.confidence
    assert vuelta.mission_id == e.mission_id and vuelta.observations == e.observations
    await db.close()


@pytest.mark.asyncio
async def test_05_save_load_conserva_scope(db):
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    await repo.upsert_entity(_ent(scope=SCOPE_A))
    assert (await repo.get_entity("file:notas.txt", scope=SCOPE_A.id)).scope == SCOPE_A.id
    await db.close()


@pytest.mark.asyncio
async def test_06_save_load_conserva_last_seen_exacto(db):
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    for marca in (1790416504.5493128, 0.1, 1 / 3, 1.7976931348623157e308):
        await repo.upsert_entity(_ent(f"file:f{marca}.txt", name=f"f{marca}", last_seen=marca))
        vuelta = await repo.get_entity(f"file:f{marca}.txt", scope=SCOPE_A.id)
        assert vuelta.last_seen == marca, f"{marca!r} -> {vuelta.last_seen!r}"
    await db.close()


@pytest.mark.asyncio
async def test_07_last_seen_unknown_no_se_convierte_en_now(db):
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    antes = time.time()
    await repo.upsert_entity(_ent("file:legacy.txt", name="legacy", last_seen=LAST_SEEN_UNKNOWN))
    vuelta = await repo.get_entity("file:legacy.txt", scope=SCOPE_A.id)
    assert vuelta.last_seen == LAST_SEEN_UNKNOWN
    assert vuelta.last_seen < antes, "no puede parecer recién observada"
    await db.close()


@pytest.mark.asyncio
async def test_08_el_default_de_la_columna_no_es_now(db):
    """Una fila insertada sin `last_seen` debe quedar en 0, no en now()."""
    await db.open()
    await db.migrate()
    antes = time.time()
    await db.execute(
        "INSERT INTO world_entities (scope_id, entity_id, kind, name) "
        "VALUES (%(s)s, %(e)s, 'file', 'x')",
        {"s": "probe-scope", "e": "file:sin-fecha"},
    )
    rows = await db.fetch(
        "SELECT last_seen FROM world_entities WHERE scope_id = %(s)s",
        {"s": "probe-scope"},
    )
    assert float(rows[0]["last_seen"]) == 0.0
    assert float(rows[0]["last_seen"]) < antes
    await db.execute("DELETE FROM world_entities WHERE scope_id = %(s)s", {"s": "probe-scope"})
    await db.close()


# --------------------------------------------------------------------------- #
# 6-7. Aislamiento por scope
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_09_aislamiento_por_scope_en_query(db):
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    await repo.upsert_entity(_ent(scope=SCOPE_A))
    await repo.upsert_entity(_ent(scope=SCOPE_B, name="notas-b.txt"))
    a = await repo.list_entities(scope=SCOPE_A.id)
    b = await repo.list_entities(scope=SCOPE_B.id)
    assert [e.name for e in a] == ["notas.txt"]
    assert [e.name for e in b] == ["notas-b.txt"]
    assert all(e.scope == SCOPE_A.id for e in a)
    await db.close()


@pytest.mark.asyncio
async def test_10_mismo_entity_id_en_distintos_scopes_coexiste(db):
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    await repo.upsert_entity(_ent(scope=SCOPE_A, exists=True))
    await repo.upsert_entity(_ent(scope=SCOPE_B, exists=False))
    assert await repo.count_entities(scope=SCOPE_A.id) == 1
    assert await repo.count_entities(scope=SCOPE_B.id) == 1
    assert (await repo.get_entity("file:notas.txt", scope=SCOPE_A.id)).attributes["exists"] is True
    assert (await repo.get_entity("file:notas.txt", scope=SCOPE_B.id)).attributes["exists"] is False
    await db.close()


# --------------------------------------------------------------------------- #
# 8-9. Edges
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_11_save_load_edges(db):
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    await repo.save_edge("project:app", "file:mod.py", scope=SCOPE_A.id)
    await repo.save_edge("project:app", "file:otro.py", "imports", scope=SCOPE_A.id)
    assert await repo.list_edges(scope=SCOPE_A.id) == [
        ("project:app", "file:mod.py", "depends_on"),
        ("project:app", "file:otro.py", "imports"),
    ]
    await db.close()


@pytest.mark.asyncio
async def test_12_las_relaciones_no_cruzan_scopes(db):
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    await repo.save_edge("project:app", "file:mod.py", scope=SCOPE_A.id)
    await repo.save_edge("project:app", "file:secreto.py", scope=SCOPE_B.id)
    assert await repo.dependencies("project:app", scope=SCOPE_A.id) == ["file:mod.py"]
    assert await repo.dependencies("project:app", scope=SCOPE_B.id) == ["file:secreto.py"]
    assert await repo.list_edges(scope=SCOPE_A.id) == [("project:app", "file:mod.py", "depends_on")]
    await db.close()


@pytest.mark.asyncio
async def test_13_neighbors_y_dependencies_por_scope(db):
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    await repo.save_edge("file:a.py", "file:b.py", scope=SCOPE_A.id)
    await repo.save_edge("file:b.py", "file:c.py", scope=SCOPE_A.id)
    assert await repo.dependencies("file:a.py", scope=SCOPE_A.id) == ["file:b.py"]
    assert await repo.neighbors("file:b.py", scope=SCOPE_A.id) == ["file:a.py", "file:c.py"]
    await db.close()


# --------------------------------------------------------------------------- #
# 10. Más de 50 entidades
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_14_mas_de_50_entidades_sobreviven(db):
    """El store NO hereda el tope de 50 de la proyección legacy."""
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    for i in range(137):
        await repo.upsert_entity(_ent(f"file:f{i}.txt", name=f"f{i}.txt",
                                      last_seen=1000.0 + i))
    assert await repo.count_entities(scope=SCOPE_A.id) == 137
    assert len(await repo.list_entities(scope=SCOPE_A.id)) == 137
    # y con tope explícito sigue funcionando
    assert len(await repo.list_entities(scope=SCOPE_A.id, limit=50)) == 50
    await db.close()


@pytest.mark.asyncio
async def test_15_save_snapshot_guarda_mas_de_50(db):
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    w = WorldModel(scope=SCOPE_A)
    for i in range(120):
        w.upsert(_ent(f"file:g{i}.txt", name=f"g{i}.txt", last_seen=2000.0 + i, scope=SCOPE_A))
    salida = w.export()
    assert len(salida["entities"]) == 120
    await repo.save_snapshot(salida["entities"], salida["edges"], scope=SCOPE_A.id)
    assert await repo.count_entities(scope=SCOPE_A.id) == 120
    await db.close()


# --------------------------------------------------------------------------- #
# 11-12. Recovery entre procesos
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_16_dos_conexiones_escriben_y_leen(db):
    """Conexión A escribe, conexión B lee: el store es compartido, no local."""
    from alexis.storage.db import Database

    await db.open()
    await db.migrate()
    conexion_a = WorldRepository(db)
    otra = Database(dsn=db.dsn)
    await otra.open()
    conexion_b = WorldRepository(otra)

    marca = 1790416504.5493128
    await conexion_a.upsert_entity(_ent(last_seen=marca))
    leida = await conexion_b.get_entity("file:notas.txt", scope=SCOPE_A.id)
    assert leida is not None and leida.last_seen == marca
    await otra.close()
    await db.close()


@pytest.mark.asyncio
async def test_17_proceso_nuevo_recupera_el_mundo(db):
    """El ciclo que pedía elIncremento: A guarda, B (proceso nuevo) reconstruye."""
    from alexis.storage.db import Database
    from alexis.storage.repositories import WorldRepository as RepoB

    await db.open()
    await db.migrate()
    # Proceso A
    mundo_a = WorldModel(scope=SCOPE_A)
    marca = 1790416504.5493128
    mundo_a.upsert(_ent(last_seen=marca, scope=SCOPE_A))
    mundo_a.upsert(_ent("file:otro.txt", name="otro.txt", last_seen=marca - 500,
                        scope=SCOPE_A, kind="file"))
    mundo_a.relate("file:notas.txt", "file:otro.txt", scope=SCOPE_A)
    salida = mundo_a.export()
    await RepoB(db).save_snapshot(salida["entities"], salida["edges"], scope=SCOPE_A.id)

    # Proceso B: base nueva, repositorio nuevo, WorldModel nuevo
    otra = Database(dsn=db.dsn)
    await otra.open()
    repo_b = RepoB(otra)
    mundo_b = WorldModel(scope=SCOPE_A)
    entidades = await repo_b.list_entities(scope=SCOPE_A.id)
    aristas = await repo_b.list_edges(scope=SCOPE_A.id)
    assert mundo_b.hydrate(entidades, aristas) == 2

    assert mundo_b.known_path("notas.txt").last_seen == marca
    assert mundo_b.known_path("otro.txt").last_seen == marca - 500
    assert [e.name for e in mundo_b.neighbors("file:notas.txt")] == ["otro.txt"]
    assert [e.name for e in mundo_b.dependencies("file:notas.txt")] == ["otro.txt"]
    assert mundo_b.known_path("notas.txt").observations == 2, "no se re-observó al rehidratar"
    await otra.close()
    await db.close()


@pytest.mark.asyncio
async def test_18_hydrate_no_mueve_last_seen(db):
    """Rehidratar no es observar: si lo fuera, cada reinicio refrescaría la edad."""
    from alexis.storage.db import Database

    await db.open()
    await db.migrate()
    antigua = 1000.0
    await WorldRepository(db).upsert_entity(_ent(last_seen=antigua))
    otra = Database(dsn=db.dsn)
    await otra.open()
    entidades = await WorldRepository(otra).list_entities(scope=SCOPE_A.id)
    mundo = WorldModel(scope=SCOPE_A)
    mundo.hydrate(entidades)
    assert mundo.known_path("notas.txt").last_seen == antigua
    assert mundo.known_path("notas.txt").observations == 2
    await otra.close()
    await db.close()


# --------------------------------------------------------------------------- #
# 13-14. No toca otras cosas
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_19_world_repository_no_escribe_en_observations(db):
    await db.open()
    await db.migrate()
    antes = await db.fetch("SELECT count(*) AS n FROM observations")
    repo = WorldRepository(db)
    w = WorldModel(scope=SCOPE_A)
    w.upsert(_ent(scope=SCOPE_A))
    w.relate("file:notas.txt", "file:x.txt", scope=SCOPE_A)
    salida = w.export()
    await repo.save_snapshot(salida["entities"], salida["edges"], scope=SCOPE_A.id)
    await repo.upsert_entity(_ent("file:y.txt", name="y.txt", scope=SCOPE_A))
    despues = await db.fetch("SELECT count(*) AS n FROM observations")
    assert antes[0]["n"] == despues[0]["n"], "el World Model no genera evidencia"
    await db.close()


@pytest.mark.asyncio
async def test_20_no_cambia_policy_gate_ni_envelope(db):
    from alexis.autonomy.gates import AutonomyGate
    from alexis.autonomy.mission import MissionEngine
    from alexis.contracts import AutonomyLevel, MissionEnvelope
    from alexis.security.policy import PolicyEngine

    await db.open()
    await db.migrate()
    mission = MissionEngine().create(
        "comprueba el mundo",
        MissionEnvelope(objective="comprueba el mundo", autonomy=AutonomyLevel.SUPERVISED,
                        allowed_actions=["understand", "research", "execute", "verify"]),
    )
    envelope_antes = list(mission.envelope.allowed_actions)
    policy, gate = PolicyEngine(), AutonomyGate()
    repo = WorldRepository(db)
    await repo.upsert_entity(_ent(last_seen=time.time()))
    assert mission.envelope.allowed_actions == envelope_antes
    assert mission.state.value == "pending"
    assert isinstance(policy, PolicyEngine) and isinstance(gate, AutonomyGate)
    await db.close()


# --------------------------------------------------------------------------- #
# 16-17. Legacy y fallos
# --------------------------------------------------------------------------- #


def test_21_los_datos_legacy_de_mission_context_no_rompen_el_runtime():
    """`mission.context["world"]` sigue siendo lo que el runtime lee, y sigue siendo
    legible: filas sin `last_seen` y sin `scope` entran con el centinela y el ámbito de la
    instancia. El store nuevo no cambia ese camino."""
    from alexis.cognition.loop import CognitiveRuntime
    from alexis.security.policy import PolicyEngine

    mission = _legacy_mission()
    cognitive = CognitiveRuntime(policy=PolicyEngine(), verifier=None,
                                 world=WorldModel(scope=SCOPE_A))
    assert cognitive.restore_world(mission) == 1
    e = cognitive.world.known_path("notas.txt")
    assert e is not None and e.last_seen == LAST_SEEN_UNKNOWN
    assert e.scope == SCOPE_A.id


def _legacy_mission():
    from alexis.autonomy.mission import MissionEngine
    from alexis.contracts import AutonomyLevel, MissionEnvelope

    m = MissionEngine().create(
        "misión vieja",
        MissionEnvelope(objective="misión vieja", autonomy=AutonomyLevel.SUPERVISED,
                        allowed_actions=["understand", "research", "execute", "verify"]),
    )
    m.context["world"] = [{"id": "file:notas.txt", "kind": "file", "name": "notas.txt",
                           "attributes": {"exists": False}, "source": "recovered",
                           "confidence": 0.7, "observations": 1}]
    return m


@pytest.mark.asyncio
async def test_22_un_fallo_de_db_no_rompe_nada(db):
    """Si la base falla, el WorldModel en memoria sigue siendo utilizable.

    La base es una fuente de conocimiento, no una autoridad: perderla no puede cambiar
    permisos ni tirar abajo la percepcion del Core.
    """
    from alexis.cognition.loop import CognitiveRuntime
    from alexis.security.policy import PolicyEngine

    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    w = WorldModel(scope=SCOPE_A)
    w.upsert(_ent(last_seen=1000.0, scope=SCOPE_A))
    await db.close()          # se cae la base underneath

    # La entidad en memoria sigue ahí y se puede consultar.
    assert w.known_path("notas.txt").last_seen == 1000.0
    # Y un intento de persistir falla sin reventar ni cambiar el estado del mundo.
    with pytest.raises(Exception):
        await repo.list_entities(scope=SCOPE_A.id)
    assert w.known_path("notas.txt") is not None
    assert w.missing_paths(["notas.txt"]) == []
    cognitive = CognitiveRuntime(policy=PolicyEngine(), verifier=None, world=w)
    assert cognitive.world is w


# --------------------------------------------------------------------------- #
# 20. E2E: dos workspaces, mismo path relativo, dos procesos
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_23_e2e_dos_workspaces_aislados_tras_reinicio(tmp_path, db):
    """workspace-A/notas.txt y workspace-B/notas.txt, persistidos y recuperados aislados.

    Es el invariante de §4.1 carried through PostgreSQL: mismo path relativo, distinta
    entidad, distinto ámbito, y ambos sobreviven a la recreation del repositorio.
    """
    from alexis.storage.db import Database

    await db.open()
    await db.migrate()

    ws_a, ws_b = tmp_path / "workspace-A", tmp_path / "workspace-B"
    for ws in (ws_a, ws_b):
        ws.mkdir()
    (ws_a / "notas.txt").write_text("contenido de A", encoding="utf-8")
    (ws_b / "notas.txt").write_text("B", encoding="utf-8")

    scope_a = Scope.from_workspace(ws_a)
    scope_b = Scope.from_workspace(ws_b)
    assert scope_a.id != scope_b.id

    marca_a, marca_b = 1790416504.5493128, 1790416505.5
    mundo = WorldModel()          # un solo modelo, dos ámbitos: la forma de §4.3
    mundo.observe_execution(_step(), _result("notas.txt", exists=True, size=15),
                            scope=scope_a)
    for e in mundo.export(scope=scope_a)["entities"]:
        e.last_seen = marca_a
    mundo.observe_execution(_step(), _result("notas.txt", exists=False),
                            scope=scope_b)
    for e in mundo.export(scope=scope_b)["entities"]:
        e.last_seen = marca_b

    repo = WorldRepository(db)
    for scope in (scope_a, scope_b):
        salida = mundo.export(scope=scope)
        await repo.save_snapshot(salida["entities"], salida["edges"], scope=scope.id)

    # Recrear base, repositorio y WorldModel.
    otra = Database(dsn=db.dsn)
    await otra.open()
    repo2 = WorldRepository(otra)
    assert await repo2.count_entities(scope=scope_a.id) == 1
    assert await repo2.count_entities(scope=scope_b.id) == 1

    fa = await repo2.get_entity("file:notas.txt", scope=scope_a.id)
    fb = await repo2.get_entity("file:notas.txt", scope=scope_b.id)
    assert fa.last_seen == marca_a and fb.last_seen == marca_b
    assert fa.attributes["exists"] is True and fb.attributes["exists"] is False
    assert fa.scope != fb.scope

    # Y un WorldModel rehidratado mantiene el aislamiento (§4.1) tras el reinicio.
    rehidratado = WorldModel(scope=scope_a)
    rehidratado.hydrate([fa], scope=scope_a)
    assert rehidratado.known_path("notas.txt").attributes["exists"] is True
    assert rehidratado.missing_paths(["notas.txt"]) == []
    mundo_b = WorldModel(scope=scope_b)
    assert mundo_b.hydrate([fb], scope=scope_b) == 1
    assert mundo_b.known_path("notas.txt") is not None
    assert mundo_b.missing_paths(["notas.txt"]) == ["notas.txt"], "en B el archivo no existe"
    await otra.close()
    await db.close()


class _Step:
    capability = "fs.stat"


class _Result:
    def __init__(self, output, success=True):
        self.output = output
        self.success = success


def _step():
    return _Step()


def _result(path, exists=True, size=None):
    out = {"path": path, "exists": exists}
    if size is not None:
        out["size"] = size
    return _Result(out, success=exists)
