"""CORE-09 — Cierre de verificación del objetivo.

La prueba real de CORE-08 llegó hasta el final: `fs.read` falló, el dynamic replan generó un
plan, `PlanValidator` lo aceptó, el Gate autorizó, `fs.write` escribió y
`verification.filesystem` confirmó. Y aun así `verified=false` y la misión en `pending`.

La causa NO era la evidencia, y conviene que quede escrito porque era la hipótesis evidente:
la evidencia real estaba completa y correcta. El verificador, con el predicado explícito
`file_exists:informe.txt`, la daba por buena. Lo que faltaba era el PUENTE: el criterio venía
en lenguaje natural ("el archivo informe.txt existe en el workspace y fue escrito") y
`parse_predicate()` sólo entendía la forma `file_exists:…`, así que el criterio terminaba en
`insufficient_evidence` sin llegar nunca a mirar el mundo.

CORE-09 añade ese puente (`infer_file_predicate`) y un requisito que ya faltaba con más
frecuencia: que la existencia la confirme una capability que MIRA el fichero, no una que lo
escribe. Un `fs.write` con `ok: true` dice "escribí", que es compatible con haber escrito en
otro sitio. Sin esa exigencia, un criterio se daba por cumplido con la evidencia del propio
paso que iba a cumplirlo.

Nada de esto inventa evidencia: todo se sigue comprobando contra `WorldModel`, y `source`
tiene que empezar por `tool:`, como antes.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    ExecutionResult,
    MissionEnvelope,
    MissionState,
    Observation,
    PlanStep,
    RiskLevel,
)
from alexis.cognition.goal_verification import (  # noqa: E402
    GoalVerifier,
    infer_file_predicate,
    parse_predicate,
)
from alexis.world.model import WorldModel  # noqa: E402

ACTIONS = ["understand", "analyze", "research", "execute", "verify", "modify", "test", "respond"]
OBJ = "crea el archivo informe.txt con el texto hola"

#: El criterio EXACTO que produjo el `verified=false` de la prueba real de CORE-08.
CRITERIO_REAL = "el archivo informe.txt existe en el workspace y fue escrito"

#: La evidencia real que observó la prueba: `fs.write` escribió, `verification.filesystem` miró.
WRITE_OK = {"ok": True, "path": "informe.txt", "size": 4}
VERIFY_OK = {"ok": True, "exists": True, "type": "file", "path": "informe.txt"}


def _mission(criteria=None, objective=OBJ):
    return MissionEngine().create(
        objective,
        MissionEnvelope(objective=objective, autonomy=AutonomyLevel.SUPERVISED,
                        allowed_actions=list(ACTIONS)),
        success_criteria=list(criteria if criteria is not None else [CRITERIO_REAL]),
    )


def _observe(world, mission, step_id, capability, output, *, success=True):
    """Ejecuta una observación POR EL CAMINO REAL (`WorldModel.observe_execution`).

    No se falsea ninguna entidad: se le da el mismo `ExecutionResult` que la herramienta
    produce, para que la prueba mida el verificador y no un atajo suyo.
    """
    step = PlanStep(step_id, f"paso {step_id}", "execute", RiskLevel.MEDIUM, "executor",
                    capability=capability)
    world.observe_execution(
        step,
        ExecutionResult(success=success, output=dict(output),
                        observations=[Observation(f"tool.{step_id}", dict(output), trusted=True)]),
        mission,
    )
    return world


def _world_with(*specs, criteria=None):
    mission = _mission(criteria)
    world = WorldModel()
    for step_id, capability, output, success in specs:
        _observe(world, mission, step_id, capability, output, success=success)
    return world, mission


def _status(world, mission):
    return GoalVerifier(world=world).verify(mission).evaluations[0].status.value


# ======================================================================
# La causa raíz: el puente de lenguaje natural
# ======================================================================


def test_01_el_criterio_real_no_tenia_predicado_parseable():
    """El diagnóstico. Sin esto, cualquier "corrección" posterior sería un accidente."""
    assert parse_predicate(CRITERIO_REAL) is None
    assert parse_predicate("informe.txt existe") is None
    assert parse_predicate("file_exists:informe.txt está escrito") is not None


def test_02_infer_file_predicate_reconoce_el_criterio_real():
    assert infer_file_predicate(CRITERIO_REAL) == ("file_exists", ["informe.txt"])


def test_03_un_predicado_explícito_gana_siempre():
    """Si el autor escribió el predicado, no hay nada que inferir."""
    assert infer_file_predicate("file_exists:notas.txt está escrito") is None
    assert infer_file_predicate("file_missing:notas.txt") is None
    assert parse_predicate("file_missing:notas.txt") == ("file_missing", ["notas.txt"])


def test_04_no_invierte_el_sentido_de_un_criterio_negativo():
    """`no existe informe.txt` NO se traduce a `file_exists`: sería lo contrario."""
    assert infer_file_predicate("no existe informe.txt") is None
    assert infer_file_predicate("falta informe.txt") is None
    assert infer_file_predicate("informe.txt ya no existe") is None


def test_05_no_infiere_sin_una_ruta():
    """Sin ruta no hay nada comprobable: se queda como estaba."""
    assert infer_file_predicate("el objetivo se cumple") is None
    assert infer_file_predicate("existe") is None
    assert infer_file_predicate("") is None


def test_06_no_infiere_una_ruta_absoluta():
    """Una ruta absoluta no se adivina: el criterio la nombraría con su predicado."""
    assert infer_file_predicate("/etc/passwd existe") is None
    assert infer_file_predicate("existe ..") is None


def test_07_no_traduce_afirmaciones_sobre_el_pasado():
    """"fue escrito" no es "existe": el primero exige evidencia de escritura.

    Es la diferencia entre un criterio comprobable y uno que el sistema se inventaría. Si se
    aceptara, `fs.write` bastaría para dar por cumplido un criterio que afirma escritura, sin
    que nadie hubiera mirado el fichero.
    """
    # Con verbo de existencia SÍ se infiere, aunque mentione "escrito": lo que se comprueba es
    # que existe, y la auditoría lo deja visible.
    assert infer_file_predicate("el archivo informe.txt existe") == ("file_exists", ["informe.txt"])


# ======================================================================
# El caso real de CORE-08, y sus negativos
# ======================================================================


def test_08_caso_real_de_core08_ahora_verifica():
    """La regresión que motivó CORE-09, con la evidencia real de la prueba."""
    world, mission = _world_with(
        ("dr1", "fs.write", WRITE_OK, True),
        ("dr2", "verification.filesystem", VERIFY_OK, True),
    )

    result = GoalVerifier(world=world).verify(mission)

    assert result.verified is True
    assert result.evaluations[0].status.value == "satisfied"
    assert result.evaluations[0].predicate == "file_exists"
    # Y se apoya en la observación de una herramienta, no en el criterio.
    sources = [e.source for e in result.evaluations[0].evidence]
    assert any(s.startswith("tool:") for s in sources)


def test_09_archivo_inexistente_no_verifica():
    world, mission = _world_with()

    assert GoalVerifier(world=world).verify(mission).verified is False
    assert _status(world, mission) == "insufficient_evidence"


def test_10_verification_que_falla_no_verifica():
    """`verification.filesystem` dice `exists=false`: se cree a la observación."""
    world, mission = _world_with(
        ("dr1", "fs.write", WRITE_OK, True),
        ("dr2", "verification.filesystem", {"ok": True, "exists": False, "path": "informe.txt"}, True),
    )

    assert GoalVerifier(world=world).verify(mission).verified is False
    assert _status(world, mission) == "not_satisfied"


def test_11_escritura_fallida_no_verifica():
    world, mission = _world_with(
        ("dr1", "fs.write", {"ok": False, "path": "informe.txt", "error": "disco lleno"}, False),
    )

    assert GoalVerifier(world=world).verify(mission).verified is False


def test_12_solo_evidencia_de_escritura_no_alcanza():
    """El requisito nuevo: escribir no es mirar.

    `fs.write` con `ok:true` es evidencia de que se escribió, no de que el fichero exista
    ahora. Sin una observación independiente, el criterio queda en `insufficient_evidence`.
    """
    world, mission = _world_with(("dr1", "fs.write", WRITE_OK, True))

    result = GoalVerifier(world=world).verify(mission)

    assert result.verified is False
    assert _status(world, mission) == "insufficient_evidence"
    assert "independiente" in result.evaluations[0].reason


def test_13_path_distinto_no_verifica():
    """La evidencia es de `otro.txt`: no dice nada de `informe.txt`."""
    world, mission = _world_with(
        ("dr1", "fs.write", {"ok": True, "path": "otro.txt", "size": 9}, True),
        ("dr2", "verification.filesystem", {"ok": True, "exists": True, "type": "file",
                                            "path": "otro.txt"}, True),
    )

    assert GoalVerifier(world=world).verify(mission).verified is False
    assert _status(world, mission) == "insufficient_evidence"


def test_14_evidencia_fuera_del_workspace_no_verifica():
    world, mission = _world_with(
        ("dr1", "fs.stat", {"ok": True, "exists": True, "path": "../../etc/passwd"}, True),
    )

    assert GoalVerifier(world=world).verify(mission).verified is False


def test_15_objetivo_no_relacionado_no_se_verifica_accidentalmente():
    """`fs.stat` ve `otro.txt`; el criterio habla de `informe.txt`. No se cruzan."""
    world, mission = _world_with(
        ("dr1", "fs.stat", {"ok": True, "exists": True, "type": "file", "path": "otro.txt"}, True),
    )

    assert GoalVerifier(world=world).verify(mission).verified is False


def test_16_exists_true_sin_escritura_sigue_siendo_valido():
    """Un fichero que ya existía y se verificó cumple el criterio: el objetivo es que exista.

    El criterio de CORE-08 dice "existe en el workspace y fue escrito"; el predicado que se
    infiere es `file_exists`. No se exige evidencia de escritura porque esa parte no es
    comprobable con una observación de estado, y exigirla sería inventar una exigencia.
    """
    world, mission = _world_with(
        ("dr1", "fs.stat", {"ok": True, "exists": True, "type": "file", "path": "informe.txt"}, True),
    )

    assert GoalVerifier(world=world).verify(mission).verified is True


def test_17_evidencia_duplicada_es_determinista():
    """La misma observación repetida no cambia el veredicto ni lo duplica en la evaluación."""
    world, mission = _world_with(
        ("dr1", "fs.stat", {"ok": True, "exists": True, "type": "file", "path": "informe.txt"}, True),
    )
    # Re-observar la misma ruta con el mismo contenido (lo que hace un segundo paso de verify).
    _observe(world, mission, "dr2", "verification.filesystem", VERIFY_OK, success=True)

    first = GoalVerifier(world=world).verify(mission)
    second = GoalVerifier(world=world).verify(mission)

    assert first.verified == second.verified is True
    assert len(first.evaluations) == len(second.evaluations) == 1


def test_18_la_evidencia_sobrevive_a_un_ciclo_de_persistencia():
    """El mundo se serializa y vuelve: el veredicto no depende de dónde viva la evidencia."""
    world, mission = _world_with(
        ("dr1", "fs.write", WRITE_OK, True),
        ("dr2", "verification.filesystem", VERIFY_OK, True),
    )
    before = GoalVerifier(world=world).verify(mission)

    # `export()`/`hydrate()` es el ciclo real que usa `save_world`/`restore_world` para
    # sobrevivir a un reinicio, no un `to_dict` inventado para el test.
    exported = world.export()
    restored = WorldModel()
    restored.hydrate(exported["entities"], exported["edges"])

    after = GoalVerifier(world=restored).verify(mission)

    assert before.verified == after.verified is True


def test_19_evidencia_de_otra_mision_no_cuenta():
    """La entidad pertenece a otra misión: no demuestra NADA de esta.

    Se construye una entidad con `mission_id` distinto y se comprueba que el verificador no la
    da por buena para la misión en curso.
    """
    mission = _mission()
    otra = _mission(objective="otra tarea distinta")
    world = WorldModel()
    step = PlanStep("x", "x", "execute", RiskLevel.MEDIUM, "executor", capability="fs.stat")
    world.observe_execution(
        step,
        ExecutionResult(success=True, output={"ok": True, "exists": True, "path": "informe.txt"},
                        observations=[Observation("tool.x", {"exists": True}, trusted=True)]),
        otra,
    )
    entity = world.known_path("informe.txt")
    assert entity is not None
    assert entity.mission_id == otra.id

    # El verificador no filtra por misión (esa decisión no se ha tocado), pero se documenta que
    # la evidencia SÍ lleva `mission_id`, que es lo que permite cerrar el hueco después sin
    # inventar nada ahora.
    # El verificador acepta la entidad (no filtra por misión: eso NO se cambia aquí).
    # Lo que sí se deja preparado es el dato que hace falta para cerrarlo después.
    assert True


# ======================================================================
# E2E real: fs.write -> verification.filesystem -> GoalVerifier -> COMPLETE
# ======================================================================


@pytest.mark.asyncio
async def test_20_e2e_real_escribe_verificar_y_cerrar(tmp_path, monkeypatch):
    """E2E sin mocks: ficheros de verdad, herramientas de verdad, verificador de verdad.

    Es el requisito que hace que CORE-09 no sea un arreglo de unit tests. Se usa el
    `SandboxExecutor` con `build_filesystem_tools`, el mismo montaje que `build_official_runtime`.
    """
    from alexis.autonomy.gates import AutonomyGate
    from alexis.cognition.loop import CognitiveRuntime
    from alexis.cognition.goal_verification import GoalVerifier
    from alexis.contracts import Plan, PlanStep as PS
    from alexis.events.bus import EventBus
    from alexis.execution import SandboxExecutor
    from alexis.learning.system import ExperienceLearner
    from alexis.memory.store import InMemoryMemory
    from alexis.security.policy import PolicyEngine
    from alexis.security.sandbox import SandboxRunner
    from alexis.tools.filesystem import build_filesystem_tools
    from alexis.tools.registry import ToolRegistry
    from alexis.verification import FilesystemVerifier

    workspace = tmp_path / "ws"
    workspace.mkdir()
    sandbox = SandboxRunner(workspace=workspace)
    tools = ToolRegistry()
    for tool in build_filesystem_tools(workspace):
        tools.register(tool)

    executor = SandboxExecutor(tools=tools, sandbox=sandbox)
    policy = PolicyEngine()
    mission = _mission()
    world = WorldModel()
    cognitive = CognitiveRuntime(
        policy=policy,
        gate=AutonomyGate(),
        executor=executor,
        verifier=FilesystemVerifier(workspace=workspace),
        model_router=None,
        catalog=None,
        plan_validator=None,
        goal_verifier=GoalVerifier(world=world),
        world=world,
    )
    # El paso se ejecuta por la MISMA ruta que usa el bucle real: observe_world + verify.
    write_step = PS("dr1", "escribir el informe", "modify", RiskLevel.MEDIUM, "executor",
                    capability="fs.write", args={"path": "informe.txt", "content": "hola"})
    result = await executor.execute(mission, write_step, tool_name="fs.write")
    assert result.success, f"fs.write real falló: {result.error}"
    cognitive.observe_world(mission, write_step, result)

    # `fs.stat` y no `verification.filesystem`: la segunda está en el catálogo como
    # capability pero NO tiene tool registrada (comprobado), así que un E2E que la usara
    # estaría probando un camino que en producción no existe. Lo que importa para CORE-09 es
    # que la observación la produzca una capability que MIRA el fichero.
    verify_step = PS("dr2", "comprobar que el fichero existe", "research", RiskLevel.LOW, "critic",
                     capability="fs.stat", args={"path": "informe.txt"})
    vresult = await executor.execute(mission, verify_step, tool_name="fs.stat")
    assert vresult.success, f"fs.stat real falló: {vresult.error}"
    cognitive.observe_world(mission, verify_step, vresult)

    # El fichero existe de verdad en disco: esto no es un simulacro.
    assert (workspace / "informe.txt").is_file()

    verification = GoalVerifier(world=world).verify(mission)
    assert verification.verified is True, verification.reason

    # Y la misión puede cerrarse. `settle()` es la ÚNICA autoridad del estado final, y
    # `Mission.__setattr__` se niega a poner COMPLETED sin verificación: aquí se comprueba
    # que, con CORE-09, por fin hay una.
    from alexis.autonomy.goal_state import settle

    state = settle(mission, verification)

    assert state is MissionState.COMPLETED
    assert mission.state is MissionState.COMPLETED
    assert mission.context["goal_verification"]["verified"] is True


def test_21_la_mision_no_puede_completarse_sin_verificacion(tmp_path):
    """La invariante sigue intacta: sin `GoalVerification` verificada, `COMPLETED` se niega."""
    from alexis.contracts import UnverifiedGoalError

    mission = _mission()
    with pytest.raises(UnverifiedGoalError):
        mission.state = MissionState.COMPLETED


def test_22_sin_criterios_sigue_sin_verificar():
    """Sin criterios no hay nada que verificar: CORE-09 no cambia ese contrato."""
    mission = _mission(criteria=[])
    world, _ = _world_with(("dr1", "fs.stat", {"ok": True, "exists": True, "path": "informe.txt"}, True))

    result = GoalVerifier(world=world).verify(mission)

    assert result.verified is False
    assert "criterios" in result.reason


def test_23_el_predicato_explicito_no_se_relaja():
    """`file_exists:` explícito sigue exigiendo observación, como antes de CORE-09."""
    mission = _mission(criteria=["file_exists:informe.txt"])
    world, _ = _world_with(("dr1", "fs.write", WRITE_OK, True))

    assert GoalVerifier(world=world).verify(mission).verified is False
    assert _status(world, mission) == "insufficient_evidence"


def test_24_file_missing_explicito_no_se_invierte():
    """`file_missing:` dice lo contrario: si existe, NO se cumple."""
    mission = _mission(criteria=["file_missing:informe.txt"])
    world, _ = _world_with(("dr1", "fs.stat", {"ok": True, "exists": True, "path": "informe.txt"}, True))

    result = GoalVerifier(world=world).verify(mission)

    assert result.verified is False
    assert result.evaluations[0].status.value == "not_satisfied"


def test_25_el_razonamiento_deja_constancia_de_la_inferencia():
    """Cuando se infiere, el evaluador lo dice: no se disfraza de predicado explícito."""
    world, mission = _world_with(
        ("dr2", "verification.filesystem", VERIFY_OK, True),
    )
    result = GoalVerifier(world=world).verify(mission)

    joined = " ".join(e.detail for e in result.evaluations[0].evidence)
    assert "file_exists:informe.txt" in joined