"""P0 §4.1 - SCOPE ISOLATION del World Model.

El World Model se queda sin identidad de proyecto. Las tools de filesystem devuelven
rutas **relativas al workspace** (`p.relative_to(root)`), asi que `notas.txt` en el
proyecto A y en el proyecto B son la misma cadena. Con la clave anterior -solo `id`- un
World Model compartido, que es lo que hara #4.3, afirmaria como hechos del proyecto A
cosas observadas en el B.

INVARIANTE: `file:notas.txt` en el ambito A != `file:notas.txt` en el ambito B.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.world.model import (  # noqa: E402
    IMPLICIT_SCOPE,
    WorldEntity,
    WorldModel,
    Scope,
)


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #


def _entity(path="notas.txt", exists=True, kind="file", scope=""):
    eid = f"file:{path}" if kind == "file" else f"{kind}:{path}"
    return WorldEntity(eid, kind, path, {"exists": exists}, source="tool:fs.stat",
                       confidence=0.7, scope=scope)


def _world_with(path, exists=True, scope=None):
    w = WorldModel(scope=scope)
    w.upsert(_entity(path, exists=exists, scope=(scope.id if isinstance(scope, Scope) else "")))
    return w


# --------------------------------------------------------------------------- #
# 1. Mismo path + scope A != mismo path + scope B
# --------------------------------------------------------------------------- #


def test_01_mismo_path_en_dos_scopes_son_entidades_distintas():
    a = WorldModel(scope=Scope.from_workspace("/tmp/proyecto-a"))
    b = WorldModel(scope=Scope.from_workspace("/tmp/proyecto-b"))
    a.upsert(_entity("notas.txt", scope=a.scope.id))
    b.upsert(_entity("notas.txt", scope=b.scope.id))

    ea = a.get("file:notas.txt")
    eb = b.get("file:notas.txt")
    assert ea is not None and eb is not None
    assert ea is not eb
    assert ea.scope != eb.scope, "el ámbito debe distinguir las dos entidades"


def test_02_un_solo_modelo_con_dos_scopes_no_confunde():
    """Un WorldModel compartido (el caso de #4.3) separa por clave, no por instancia."""
    w = WorldModel()
    a, b = Scope.from_workspace("/tmp/a"), Scope.from_workspace("/tmp/b")
    w.upsert(_entity("notas.txt", scope=a.id))
    w.upsert(_entity("notas.txt", scope=b.id))
    assert len(w.entities) == 2, "el ámbito forma parte de la clave"
    assert w.get("file:notas.txt", scope=a).scope == a.id
    assert w.get("file:notas.txt", scope=b).scope == b.id
    assert w.scopes() == sorted([a.id, b.id])


def test_03_el_scope_es_determinista():
    assert Scope.from_workspace("/tmp/x").id == Scope.from_workspace("/tmp/x").id
    assert Scope.from_workspace("/tmp/x").id != Scope.from_workspace("/tmp/y").id


def test_04_la_ruta_se_resuelve_antes_de_identificar(tmp_path):
    """`/tmp/a` y `/tmp/a/` son el mismo ámbito, y también un symlink a él."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "enlace"
    link.symlink_to(real)
    assert Scope.from_workspace(str(real)).id == Scope.from_workspace(str(real) + "/").id
    assert Scope.from_workspace(str(link)).id == Scope.from_workspace(str(real)).id


# --------------------------------------------------------------------------- #
# 2. query(scope=A) nunca devuelve entidades de scope B
# --------------------------------------------------------------------------- #


def test_05_query_no_mezcla_scopes():
    w = WorldModel()
    a, b = Scope.from_workspace("/tmp/a"), Scope.from_workspace("/tmp/b")
    w.upsert(_entity("notas.txt", scope=a.id))
    w.upsert(_entity("notas.txt", scope=b.id))
    w.upsert(_entity("otro.txt", scope=b.id))

    res_a = w.query(scope=a)
    assert [e.name for e in res_a] == ["notas.txt"]
    assert all(e.scope == a.id for e in res_a)
    res_b = w.query(scope=b)
    assert sorted(e.name for e in res_b) == ["notas.txt", "otro.txt"]


def test_06_query_por_texto_no_mezcla_scopes():
    w = WorldModel()
    a, b = Scope.from_workspace("/tmp/a"), Scope.from_workspace("/tmp/b")
    w.upsert(_entity("notas.txt", scope=a.id))
    w.upsert(_entity("notas.txt", scope=b.id))
    # `terms()` no parte por el punto: el token del archivo es "notas.txt", no "notas".
    assert len(w.query(text="lee notas.txt", scope=a)) == 1
    assert len(w.query(text="lee notas.txt", scope=b)) == 1


def test_07_for_objective_no_mezcla_scopes():
    w = WorldModel()
    a, b = Scope.from_workspace("/tmp/a"), Scope.from_workspace("/tmp/b")
    w.upsert(_entity("notas.txt", scope=a.id))
    w.upsert(_entity("secretos.txt", scope=b.id))
    assert [e.name for e in w.for_objective("lee notas.txt", scope=a)] == ["notas.txt"]
    assert [e.name for e in w.for_objective("lee notas.txt", scope=b)] == []


# --------------------------------------------------------------------------- #
# 3. known_path(scope=A) nunca usa conocimiento de scope B
# --------------------------------------------------------------------------- #


def test_08_known_path_aisla():
    w = WorldModel()
    a, b = Scope.from_workspace("/tmp/a"), Scope.from_workspace("/tmp/b")
    w.upsert(_entity("notas.txt", exists=True, scope=a.id))
    w.upsert(_entity("notas.txt", exists=False, scope=b.id))
    assert w.known_path("notas.txt", scope=a).attributes["exists"] is True
    assert w.known_path("notas.txt", scope=b).attributes["exists"] is False


def test_09_known_path_no_ve_lo_que_solo_existe_en_otro_scope():
    w = WorldModel()
    a, b = Scope.from_workspace("/tmp/a"), Scope.from_workspace("/tmp/b")
    w.upsert(_entity("notas.txt", scope=b.id))
    assert w.known_path("notas.txt", scope=a) is None


# --------------------------------------------------------------------------- #
# 4. missing_paths(scope=A) nunca usa conocimiento de scope B
# --------------------------------------------------------------------------- #


def test_10_missing_paths_aisla():
    w = WorldModel()
    a, b = Scope.from_workspace("/tmp/a"), Scope.from_workspace("/tmp/b")
    w.upsert(_entity("notas.txt", exists=False, scope=a.id))
    w.upsert(_entity("notas.txt", exists=True, scope=b.id))
    assert w.missing_paths(["notas.txt"], scope=a) == ["notas.txt"]
    assert w.missing_paths(["notas.txt"], scope=b) == [], "en B el archivo SÍ existe"


# --------------------------------------------------------------------------- #
# 5. relate/neighbors no cruzan scopes accidentalmente
# --------------------------------------------------------------------------- #


def test_11_relate_no_une_scopes():
    w = WorldModel()
    a, b = Scope.from_workspace("/tmp/a"), Scope.from_workspace("/tmp/b")
    for scope in (a, b):
        w.upsert(WorldEntity("project:app", "project", "app", scope=scope.id))
    w.upsert(WorldEntity("file:mod.py", "file", "mod.py", scope=a.id))
    w.upsert(WorldEntity("file:otro.py", "file", "otro.py", scope=b.id))
    w.relate("project:app", "file:mod.py", scope=a)
    w.relate("project:app", "file:otro.py", scope=b)
    assert [e.name for e in w.dependencies("project:app", scope=a)] == ["mod.py"]
    assert [e.name for e in w.dependencies("project:app", scope=b)] == ["otro.py"]


def test_12_neighbors_no_ve_scopes_ajenos():
    w = WorldModel()
    a, b = Scope.from_workspace("/tmp/a"), Scope.from_workspace("/tmp/b")
    w.upsert(_entity("notas.txt", scope=a.id))
    w.upsert(_entity("notas.txt", scope=b.id))
    w.relate("file:notas.txt", "file:notas.txt", scope=a)
    # el grafo de A no debe traducirse en "el nodo de B es vecino de sí mismo"
    assert [e.scope for e in w.neighbors("file:notas.txt", scope=a)] == [a.id]
    assert w.neighbors("file:notas.txt", scope=b) == []


# --------------------------------------------------------------------------- #
# 6. observe_execution registra en el scope correcto
# --------------------------------------------------------------------------- #


class _Result:
    def __init__(self, output, success=True):
        self.output = output
        self.success = success


class _Step:
    def __init__(self, capability="fs.stat"):
        self.capability = capability


def test_13_observe_execution_usa_el_scope_del_modelo():
    a = Scope.from_workspace("/tmp/a")
    w = WorldModel(scope=a)
    w.observe_execution(_Step(), _Result({"path": "notas.txt", "exists": True, "size": 10}))
    e = w.known_path("notas.txt")
    assert e is not None and e.scope == a.id


def test_14_observe_execution_acepta_scope_explicito():
    a, b = Scope.from_workspace("/tmp/a"), Scope.from_workspace("/tmp/b")
    w = WorldModel(scope=a)
    w.observe_execution(_Step(), _Result({"path": "notas.txt", "exists": True}), scope=b)
    assert w.known_path("notas.txt", scope=b) is not None
    assert w.known_path("notas.txt", scope=a) is None, "no debe caer en el ámbito por defecto"


def test_15_observe_execution_deja_aumentar_observations_por_separado():
    a, b = Scope.from_workspace("/tmp/a"), Scope.from_workspace("/tmp/b")
    w = WorldModel(scope=a)
    w.observe_execution(_Step(), _Result({"path": "notas.txt", "exists": True}), scope=a)
    w.observe_execution(_Step(), _Result({"path": "notas.txt", "exists": True}), scope=a)
    w.observe_execution(_Step(), _Result({"path": "notas.txt", "exists": True}), scope=b)
    assert w.known_path("notas.txt", scope=a).observations == 2
    assert w.known_path("notas.txt", scope=b).observations == 1


# --------------------------------------------------------------------------- #
# 7. to_prompt_lines no mezcla scopes
# --------------------------------------------------------------------------- #


def test_16_to_prompt_lines_no_mezcla_scopes():
    w = WorldModel()
    a, b = Scope.from_workspace("/tmp/a"), Scope.from_workspace("/tmp/b")
    w.upsert(_entity("notas.txt", scope=a.id))
    w.upsert(_entity("secreto.txt", scope=b.id))
    lineas = w.to_prompt_lines(scope=a)
    assert lineas and all("secreto.txt" not in ln for ln in lineas)
    assert any("notas.txt" in ln for ln in lineas)


def test_17_to_prompt_lines_solo_muestra_el_ambito_del_modelo():
    w = WorldModel(scope=Scope.from_workspace("/tmp/a"))
    w.upsert(_entity("notas.txt", scope=w.scope.id))
    w.upsert(_entity("otro.txt", scope=Scope.from_workspace("/tmp/b").id))
    assert all("otro.txt" not in ln for ln in w.to_prompt_lines())


# --------------------------------------------------------------------------- #
# 8. Compatibilidad: scope implícito = comportamiento actual
# --------------------------------------------------------------------------- #


def test_18_sin_scope_se_reproduce_el_comportamiento_anterior():
    w = WorldModel()
    assert w.scope.id == IMPLICIT_SCOPE
    w.upsert(_entity("notas.txt"))
    assert w.known_path("notas.txt") is not None
    assert w.get("file:notas.txt") is not None
    assert [e.name for e in w.snapshot()] == ["notas.txt"]
    assert w.upsert(_entity("notas.txt")).observations == 2
    w.relate("file:notas.txt", "file:otro.txt")
    w.upsert(_entity("otro.txt"))
    assert [e.name for e in w.neighbors("file:notas.txt")] == ["otro.txt"]


def test_19_put_sustituye_sin_fusionar_ni_contar():
    """La restauración debe reponer lo que había, no re-observarlo."""
    w = WorldModel(scope=Scope.from_workspace("/tmp/a"))
    w.upsert(_entity("notas.txt"))
    w.put(_entity("notas.txt"))  # como restore_world
    assert w.known_path("notas.txt").observations == 1


def test_20_un_scope_serializado_conserva_el_id():
    """Al rehidratar desde un id guardado NO se re-hashea: se conserva tal cual.

    `label` y `root` son informativos y no viajan; lo que define la identidad es `id`.
    """
    s = Scope.from_workspace("/tmp/a")
    recuperado = Scope.coerce(s.id)
    assert recuperado.id == s.id
    assert Scope.coerce(s.to_dict()).id == s.id


def test_21_to_dict_lleva_el_scope():
    w = WorldModel(scope=Scope.from_workspace("/tmp/a"))
    e = w.upsert(_entity("notas.txt"))
    assert e.to_dict()["scope"] == w.scope.id


# --------------------------------------------------------------------------- #
# 9. E2E: dos workspaces con idéntico relative path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_22_e2e_dos_workspaces_aislados(tmp_path):
    """workspace-A/notas.txt y workspace-B/notas.txt, observados con las tools REALES.

    No hay dobles: `SandboxExecutor` con el registro real de filesystem sobre cada
    workspace. Cada resultado se feedea a un `WorldModel` con el ambito derivado de SU
    workspace, que es la cadena por la que el runtime los alimentaria.
    """
    from alexis.autonomy.mission import MissionEngine
    from alexis.contracts import AutonomyLevel, MissionEnvelope, PlanStep, RiskLevel, PlanStep, RiskLevel
    from alexis.execution import SandboxExecutor
    from alexis.security.sandbox import SandboxRunner
    from alexis.tools.filesystem import build_filesystem_tools
    from alexis.tools.registry import ToolRegistry

    ws_a, ws_b = tmp_path / "workspace-A", tmp_path / "workspace-B"
    for ws in (ws_a, ws_b):
        ws.mkdir()
    (ws_a / "notas.txt").write_text("contenido propio de A", encoding="utf-8")
    (ws_b / "notas.txt").write_text("B", encoding="utf-8")   # distinto tamaño, a propósito

    mission = MissionEngine().create(
        "comprueba notas.txt",
        MissionEnvelope(objective="comprueba notas.txt", autonomy=AutonomyLevel.SUPERVISED,
                        allowed_actions=["understand", "research", "execute", "verify"]),
    )
    paso = PlanStep("s1", "comprueba notas.txt", "execute", RiskLevel.LOW, "executor",
                    capability="fs.stat", args={"path": "notas.txt"})

    world_a = WorldModel(scope=Scope.from_workspace(ws_a))
    world_b = WorldModel(scope=Scope.from_workspace(ws_b))
    assert world_a.scope.id != world_b.scope.id, "los ambitos deben diferir"

    # Observa cada workspace con SU executor, y comprueba que la tool devuelve la ruta
    # RELATIVA: es la causa raiz de la colision que este incremento evita.
    for ws, world in ((ws_a, world_a), (ws_b, world_b)):
        registry = ToolRegistry()
        registry.register_all(build_filesystem_tools(ws))
        executor = SandboxExecutor(tools=registry, sandbox=SandboxRunner(ws))
        result = await executor.execute(mission, paso, tool_name="fs.stat")
        assert result.output["path"] == "notas.txt", "la tool devuelve ruta relativa"
        world.observe_execution(paso, result, mission)

    # 1) son entidades diferentes, con el tamaño real de cada una
    ea, eb = world_a.known_path("notas.txt"), world_b.known_path("notas.txt")
    assert ea is not None and eb is not None
    assert (ea.scope, eb.scope) == (world_a.scope.id, world_b.scope.id)
    assert ea.attributes["size"] != eb.attributes["size"], "cada ambito tiene el suyo"
    assert ea.observations == 1 and eb.observations == 1

    # 2) query A devuelve A, query B devuelve B
    assert [e.scope for e in world_a.query(text="notas.txt")] == [world_a.scope.id]
    assert [e.scope for e in world_b.query(text="notas.txt")] == [world_b.scope.id]

    # 3) known_path A no ve B
    assert world_a.get("file:notas.txt") is not world_b.get("file:notas.txt")

    # 4) missing_paths A no ve B
    assert world_a.missing_paths(["notas.txt"]) == []
    assert world_b.missing_paths(["notas.txt"]) == []


def test_23_e2e_un_modelo_compartido_dos_workspaces(tmp_path):
    """Un mismo WorldModel con los dos ambitos: la forma en que #4.3 los tendra.

    En A el archivo NO existe; en B SI. Una lectura sobre el modelo compartido lo sabe, y
    no confunde un proyecto con el otro.
    """
    ws_a, ws_b = tmp_path / "A", tmp_path / "B"
    for ws in (ws_a, ws_b):
        ws.mkdir()
    w = WorldModel()
    a, b = Scope.from_workspace(ws_a), Scope.from_workspace(ws_b)
    w.observe_execution(_Step(), _Result({"path": "notas.txt", "exists": False}), scope=a)
    w.observe_execution(_Step(), _Result({"path": "notas.txt", "exists": True}), scope=b)
    assert w.missing_paths(["notas.txt"], scope=a) == ["notas.txt"]
    assert w.missing_paths(["notas.txt"], scope=b) == []


# --------------------------------------------------------------------------- #
# 10. El scope no toca Policy/Gate/envelope
# --------------------------------------------------------------------------- #


def test_24_el_scope_no_toca_la_autoridad():
    from alexis.autonomy.gates import AutonomyGate
    from alexis.autonomy.mission import MissionEngine
    from alexis.cognition.loop import CognitiveRuntime
    from alexis.contracts import AutonomyLevel, MissionEnvelope, PlanStep, RiskLevel
    from alexis.security.policy import PolicyEngine

    mission = MissionEngine().create(
        "lee notas.txt",
        MissionEnvelope(objective="lee notas.txt", autonomy=AutonomyLevel.SUPERVISED,
                        allowed_actions=["understand", "research", "execute", "verify"]),
    )
    envelope_antes = list(mission.envelope.allowed_actions)
    policy = PolicyEngine()
    gate = AutonomyGate()
    w = WorldModel(scope=Scope.from_workspace("/tmp/a"))
    w.upsert(_entity("notas.txt", exists=False))
    cognitive = CognitiveRuntime(policy=policy, gate=gate, verifier=None, world=w)

    # El scope no crea ni quita capacidades ni acciones permitidas.
    assert mission.envelope.allowed_actions == envelope_antes
    assert cognitive.gate is gate, "el gate no se sustituye"
    assert cognitive.policy is policy, "la policy no se sustituye"
    assert not hasattr(mission.context, "approved_step_ids")
    # Y el mundo sigue sin autoridad para conceder nada: con la evidencia de ausencia,
    # `world_block` propone una PREGUNTA, no deniega ni modifica el envelope.
    from alexis.cognition.state import KnowledgeState

    knowledge = KnowledgeState(objective="lee notas.txt")
    pendientes = [PlanStep("s1", "lee", "execute", RiskLevel.LOW, "e", capability="fs.read",
                           args={"path": "notas.txt"})]
    question = cognitive.world_block(mission, knowledge, pendientes)
    assert question and "?" in question, "pregunta al usuario"
    assert mission.envelope.allowed_actions == envelope_antes
    assert mission.state is not None
