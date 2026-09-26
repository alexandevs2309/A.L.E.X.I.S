"""P0 §5.5 — `COMPLETED` exige objetivo verificado, por todas las vías.

La invariante no está en el sitio donde se marca el estado: está en
`Mission.__setattr__`. Asignar `MissionState.COMPLETED` sin un `GoalVerification`
confirmado lanza `UnverifiedGoalError`, sea cual sea el camino que lo intente. Aquí se
comprueba que:

1. Ninguna forma de "terminar bien" produce `COMPLETED` sin evidencia de objetivo.
2. `COMPLETED` sigue siendo alcanzable cuando el objetivo **sí** está verificado.
3. Todos los caminos —cognitivo, legacy, constructor, persistencia, API, scripts— respetan
   la misma autoridad.

Cada `COMPLETED` de este archivo se apoya en una observación real de una tool de
filesystem sobre el `tmp_path` del test. Ninguno fabrica evidencia.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from alexis.autonomy.gates import AutonomyGate  # noqa: E402
from alexis.autonomy.goal_state import goal_is_confirmed, settle  # noqa: E402
from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.cognition.goal_verification import (  # noqa: E402
    CriterionStatus,
    GoalVerification,
    GoalVerifier,
)
from alexis.cognition.loop import CognitiveRuntime  # noqa: E402
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    Mission,
    MissionEnvelope,
    MissionState,
    Plan,
    PlanStep,
    RiskLevel,
    UnverifiedGoalError,
)
from alexis.core.runtime import AlexisRuntime  # noqa: E402
from alexis.events.bus import EventBus  # noqa: E402
from alexis.execution import SandboxExecutor  # noqa: E402
from alexis.learning.system import ExperienceLearner  # noqa: E402
from alexis.memory.store import InMemoryMemory  # noqa: E402
from alexis.security.policy import PolicyEngine  # noqa: E402
from alexis.security.sandbox import SandboxRunner  # noqa: E402
from alexis.storage.serialization import mission_from_row, mission_to_row  # noqa: E402
from alexis.tools.filesystem import build_filesystem_tools  # noqa: E402
from alexis.tools.registry import ToolRegistry  # noqa: E402
from alexis.verification import FilesystemVerifier  # noqa: E402
from alexis.world.model import WorldModel  # noqa: E402

ACTIONS = ["understand", "analyze", "research", "execute", "test", "verify", "modify", "respond"]


def _mission(objective="cumple el objetivo", criteria=None, **over):
    data = dict(
        objective=objective,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=list(ACTIONS),
        capabilities=_enabled_capabilities(),
    )
    data.update(over)
    return MissionEngine().create(objective, MissionEnvelope(**data), success_criteria=criteria)


def _real_executor(tmp_path):
    registry = ToolRegistry()
    registry.register_all(build_filesystem_tools(tmp_path))
    return SandboxExecutor(tools=registry, sandbox=SandboxRunner(tmp_path))


async def _observe(tmp_path, world, mission, name):
    """Observa un archivo real con la tool real y deja el hecho en el WorldModel."""
    step = PlanStep(
        "observar",
        f"observar {name}",
        "research",
        RiskLevel.LOW,
        "executor",
        capability="fs.stat",
        args={"path": name},
    )
    result = await _real_executor(tmp_path).execute(mission, step, tool_name="fs.stat")
    world.observe_execution(step, result, mission)
    return result


def _cognitive(world, goal_verifier, verifier=None, **over):
    return CognitiveRuntime(
        policy=PolicyEngine(),
        gate=AutonomyGate(),
        executor=over.pop("executor", None),
        verifier=verifier or FilesystemVerifier(workspace=over.pop("workspace", ".")),
        world=world,
        goal_verifier=goal_verifier,
        **over,
    )


def _enabled_capabilities():
    from alexis.capabilities import build_catalog

    return [s.id for s in build_catalog().enabled()]


def _cognitive_with_tools(tmp_path, world, **over):
    return _cognitive(
        world,
        GoalVerifier(world=world),
        executor=_real_executor(tmp_path),
        workspace=tmp_path,
        **over,
    )


async def _run_to_end(cognitive, mission, plan, limit=12):
    knowledge = cognitive.knowledge_for(mission)
    outcome = None
    for _ in range(limit):
        pending = cognitive.pending_steps(mission, plan, knowledge)
        outcome = await cognitive.step(mission, knowledge, pending_steps=pending, plan=plan)
        knowledge = outcome.knowledge
        if outcome.done:
            break
    return outcome


def _read_step(name="notas.txt"):
    return PlanStep(
        "leer",
        f"leer {name}",
        "research",
        RiskLevel.LOW,
        "executor",
        capability="fs.read",
        args={"path": name},
    )


# ----------------------------------------------------------------------
# La invariante: asignar COMPLETED sin verificación es imposible
# ----------------------------------------------------------------------


def test_asignar_completed_sin_verificacion_es_imposible():
    mission = _mission("obj", ["El archivo file_exists:notas.txt está escrito"])

    with pytest.raises(UnverifiedGoalError) as boom:
        mission.state = MissionState.COMPLETED

    assert "ACTION SUCCESS no es OBJECTIVE SUCCESS" in str(boom.value)
    assert mission.state is not MissionState.COMPLETED


def test_el_constructor_tampoco_puede_crear_una_mision_completada():
    envelope = MissionEnvelope(
        objective="obj", autonomy=AutonomyLevel.SUPERVISED, allowed_actions=list(ACTIONS)
    )

    with pytest.raises(UnverifiedGoalError):
        Mission(id="x", goal=None, envelope=envelope, state=MissionState.COMPLETED)


def test_una_verificacion_fabricada_no_autoriza_completed():
    """`verified=True` a mano, sin criterios, no abre la puerta: se revalida."""
    fabricated = GoalVerification(objective="obj", evaluations=[], verified=True, reason="a mí me parece")
    assert goal_is_confirmed(fabricated) is False

    mission = _mission("obj")
    mission.goal_verification = fabricated
    with pytest.raises(UnverifiedGoalError):
        mission.state = MissionState.COMPLETED

    assert mission.state is not MissionState.COMPLETED


def test_una_verificacion_serializada_con_verified_true_no_alcanza():
    raw = {
        "objective": "obj",
        "verified": True,
        "reason": "",
        "criteria": [{"criterion": "los tests pasan", "status": "satisfied", "reason": "", "evidence": []}],
    }
    reloaded = GoalVerification.from_dict(raw)

    assert reloaded.verified is True
    assert goal_is_confirmed(reloaded) is False, "sin evidencia fiable no hay completado"


def test_settle_rechaza_un_objeto_que_no_es_goal_verification():
    mission = _mission("obj")

    with pytest.raises(TypeError):
        settle(mission, {"verified": True, "criteria": []})


# ----------------------------------------------------------------------
# Casos 1-5: la acción termina bien y el objetivo no está demostrado
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accion_exitosa_sin_criterios_no_completa(tmp_path):
    """Caso 1: la acción va bien, el objetivo no tiene ni un criterio que comprobar."""
    (tmp_path / "notas.txt").write_text("contenido", encoding="utf-8")
    mission = _mission("lee notas.txt", criteria=None)
    world = WorldModel()
    plan = Plan("m", [_read_step()])

    outcome = await _run_to_end(
        _cognitive_with_tools(tmp_path, world), mission, plan
    )

    assert outcome.mission_state is MissionState.NEEDS_VERIFICATION
    assert "sin criterios" in (mission.context.get("goal_verification_reason") or "")


@pytest.mark.asyncio
async def test_accion_exitosa_con_evidencia_insuficiente_no_completa(tmp_path):
    """Caso 2: hay acción y hay criterio, pero no hay evidencia que lo respalde."""
    (tmp_path / "notas.txt").write_text("contenido", encoding="utf-8")
    mission = _mission("lee notas.txt", ["Los tests del proyecto pasan"])
    world = WorldModel()
    plan = Plan("m", [_read_step()])

    outcome = await _run_to_end(
        _cognitive_with_tools(tmp_path, world), mission, plan
    )

    assert outcome.mission_state is MissionState.NEEDS_VERIFICATION
    verification = mission.goal_verification
    assert verification.verified is False
    assert verification.evaluations[0].status is CriterionStatus.INSUFFICIENT_EVIDENCE


@pytest.mark.asyncio
async def test_accion_exitosa_con_criterio_no_satisfecho_no_completa(tmp_path):
    """Caso 3: se observó lo contrario de lo que el objetivo pedía."""
    mission = _mission(
        "lee el archivo informe.md",
        ["El archivo file_exists:informe.md está escrito"],
    )
    world = WorldModel()
    plan = Plan("m", [_read_step("informe.md")])

    outcome = await _run_to_end(
        _cognitive_with_tools(tmp_path, world), mission, plan
    )

    # El bucle pidió clarifying porque el archivo no existía. Lo que importa:
    assert outcome.mission_state is not MissionState.COMPLETED
    assert mission.state is not MissionState.COMPLETED

    # Y el verificador marca el criterio como NO CUMPLIDO, no como "sin datos": se
    # observa con la tool real que el archivo no existe, y el criterio dice que sí.
    assert not (tmp_path / "informe.md").exists()
    await _observe(tmp_path, world, mission, "informe.md")
    verification = GoalVerifier(world=world).verify(mission)
    estado = settle(mission, verification)

    assert verification.evaluations[0].status is CriterionStatus.NOT_SATISFIED
    assert verification.verified is False
    assert estado is MissionState.BLOCKED


@pytest.mark.asyncio
async def test_accion_fallida_no_completa(tmp_path):
    """Caso 4: la acción falla; no hay nada que verificar ni que completar."""
    mission = _mission("lee notas.txt", ["El archivo file_exists:notas.txt está escrito"])
    world = WorldModel()
    plan = Plan("m", [_read_step()])

    outcome = await _run_to_end(
        _cognitive_with_tools(tmp_path, world), mission, plan
    )

    assert outcome.mission_state is not MissionState.COMPLETED
    assert mission.state is not MissionState.COMPLETED


def test_verified_false_no_completa(tmp_path):
    """Caso 5: el GoalVerifier dice que no."""
    world = WorldModel()
    mission = _mission("obj", ["El archivo file_exists:informe.md existe"])
    verification = GoalVerifier(world=world).verify(mission)

    estado = settle(mission, verification)

    assert verification.verified is False
    assert estado is MissionState.NEEDS_VERIFICATION


# ----------------------------------------------------------------------
# Casos 6-8: verificado sí, completado sí
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verified_true_producece_completed(tmp_path):
    """Caso 6: el objetivo está demostrado con evidencia de una tool real."""
    (tmp_path / "informe.md").write_text("contenido", encoding="utf-8")
    mission = _mission(
        "redacta el archivo informe.md",
        ["El archivo file_exists:informe.md está escrito"],
    )
    world = WorldModel()
    plan = Plan("m", [_read_step("informe.md")])

    outcome = await _run_to_end(
        _cognitive_with_tools(tmp_path, world), mission, plan
    )

    assert mission.goal_verification is not None
    assert mission.goal_verification.verified is True
    assert mission.state is MissionState.COMPLETED
    assert outcome.mission_state is MissionState.COMPLETED


@pytest.mark.asyncio
async def test_multiples_criterios_uno_falla_no_completa(tmp_path):
    """Caso 7: siete de ocho no bastan."""
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "b.md").write_text("x", encoding="utf-8")
    (tmp_path / "z.md").write_text("x", encoding="utf-8")
    mission = _mission(
        "deja en orden los archivos a.md y b.md",
        [
            "El archivo file_exists:a.md existe",
            "El archivo file_exists:b.md existe",
            "El archivo file_missing:z.md no está",
        ],
    )
    world = WorldModel()
    plan = Plan("m", [_read_step("a.md")])

    await _observe(tmp_path, world, mission, "b.md")
    await _observe(tmp_path, world, mission, "z.md")
    await _run_to_end(
        _cognitive_with_tools(tmp_path, world), mission, plan
    )

    assert mission.goal_verification.verified is False
    assert mission.state is not MissionState.COMPLETED
    assert mission.state is MissionState.BLOCKED


@pytest.mark.asyncio
async def test_multiples_criterios_todos_satisfechos_completa(tmp_path):
    """Caso 8: todos los criterios con evidencia válida."""
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "b.md").write_text("x", encoding="utf-8")
    mission = _mission(
        "deja en orden los archivos a.md y b.md",
        [
            "El archivo file_exists:a.md existe",
            "El archivo file_exists:b.md existe",
        ],
    )
    world = WorldModel()
    plan = Plan("m", [_read_step("a.md")])

    await _observe(tmp_path, world, mission, "a.md")
    await _observe(tmp_path, world, mission, "b.md")
    await _run_to_end(
        _cognitive_with_tools(tmp_path, world), mission, plan
    )

    assert mission.goal_verification.verified is True
    assert mission.state is MissionState.COMPLETED


# ----------------------------------------------------------------------
# La regresión anti-false-success que el plan exige
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regresion_accion_exitosa_nunca_por_si_sola_completa(tmp_path):
    """Todo va bien y el objetivo NO queda demostrado. El sistema lo impide.

    Si alguien reintrodujera `ACTION SUCCESS -> COMPLETED` (por ejemplo, volviendo a poner
    `mission.state = COMPLETED` en la rama de FINISH o en `_finalize`), este test falla.
    """
    (tmp_path / "notas.txt").write_text("contenido", encoding="utf-8")
    mission = _mission(
        "lee el archivo notas.txt y arregla el bug de normalización",
        ["Los tests del proyecto pasan", "El bug está corregido"],
    )
    world = WorldModel()
    plan = Plan("m", [_read_step(), _step_respond()])
    runtime = _cognitive_with_tools(tmp_path, world)

    outcome = await _run_to_end(runtime, mission, plan)

    # Las acciones fueron bien: ese es el "action success" que ya no basta. En el camino
    # cognitivo el rastro de una acción correcta está en el conocimiento, no en results.
    assert "leer" in outcome.knowledge.completed_steps, "el paso de lectura tuvo que funcionar"
    assert not outcome.knowledge.failed_steps, "ningún paso debió fallar"
    assert any("'leer' completado" in k for k in outcome.knowledge.known)

    # Y aun así: no completado, porque el objetivo no está demostrado.
    assert mission.state is not MissionState.COMPLETED
    assert mission.state in (MissionState.NEEDS_VERIFICATION, MissionState.BLOCKED)
    assert mission.goal_verification is not None
    assert mission.goal_verification.verified is False
    assert outcome.mission_state is not MissionState.COMPLETED


def _step_respond():
    return PlanStep(
        "responder",
        "responder",
        "respond",
        RiskLevel.LOW,
        "executor",
        capability="tts.speak",
    )


def test_la_asignacion_directa_sigue_siendo_un_error_aunque_pase_todo():
    """El regression test anterior pasa por el runtime; este cierra la puerta por dentro."""
    world = WorldModel()
    mission = _mission("obj", ["El archivo file_exists:informe.md existe"])
    mission.goal_verification = GoalVerifier(world=world).verify(mission)

    with pytest.raises(UnverifiedGoalError):
        mission.state = MissionState.COMPLETED


# ----------------------------------------------------------------------
# Casos 9 y 10: recuperación y persistencia
# ----------------------------------------------------------------------


def test_mision_verificada_se_recupera_como_completada(tmp_path):
    """Caso 9 (primera mitad): lo completed de verdad se puede recuperar."""
    (tmp_path / "informe.md").write_text("x", encoding="utf-8")
    mission = _mission("obj", ["El archivo file_exists:informe.md existe"])
    mission.goal.success_criteria = ["El archivo file_exists:informe.md existe"]

    from goal_completion import world_with_file

    settle(mission, GoalVerifier(world=world_with_file("informe.md")).verify(mission))
    assert mission.state is MissionState.COMPLETED

    row = mission_to_row(mission)
    row["state"] = "completed"
    row["results"] = "[]"
    reloaded = mission_from_row(row)

    assert reloaded.state is MissionState.COMPLETED
    assert reloaded.goal_verification is not None
    assert reloaded.goal_verification.verified is True


def test_una_mision_que_dice_completed_sin_verificacion_no_se_recupera_como_completada():
    """Caso 9 (segunda mitad): una fila `completed` sin verificación no se carga como tal."""
    mission = _mission("obj", ["El archivo file_exists:informe.md existe"])
    row = mission_to_row(mission)
    row["state"] = "completed"
    row["results"] = "[]"

    reloaded = mission_from_row(row)

    assert reloaded.state is MissionState.NEEDS_VERIFICATION
    assert reloaded.goal_verification is None
    assert "no se confía" in reloaded.context["goal_verification_reason"]


def test_el_estado_persistido_no_puede_ser_completed_sin_verificacion():
    """Caso 10: lo que sale a la base de datos respeta la regla en ambos sentidos."""
    world = WorldModel()
    sin_criterios = _mission("obj", None)
    settle(sin_criterios, GoalVerifier(world=world).verify(sin_criterios))
    row = mission_to_row(sin_criterios)
    row["results"] = "[]"

    recargada = mission_from_row(row)

    assert recargada.state is not MissionState.COMPLETED
    assert recargada.state is MissionState.NEEDS_VERIFICATION


# ----------------------------------------------------------------------
# Casos 11 y 12: API, scripts y camino legacy
# ----------------------------------------------------------------------


def test_ningun_modulo_de_produccion_asigna_completed_fuera_de_la_autoridad():
    """Caso 11: la regla es estructural, no una convención.

    `settle()` es el único que puede escribir COMPLETED, más la restauración de una fila
    ya verificada. Si alguien reintroduce la asignación en el runtime, en la API o en un
    script, este test falla.
    """
    permitidos = {
        "alexis/autonomy/goal_state.py",       # la autoridad
        "alexis/contracts.py",                 # la invariante (solo compara)
        "alexis/storage/serialization.py",     # restaura una verificación válida
    }
    patrones = ("state = MissionState.COMPLETED", "state=MissionState.COMPLETED")
    culpables = []
    for ruta in sorted((PROJECT_ROOT / "alexis").rglob("*.py")) + sorted(
        (PROJECT_ROOT / "apps").rglob("*.py")
    ):
        if "__pycache__" in str(ruta):
            continue
        relativo = str(ruta.relative_to(PROJECT_ROOT))
        if relativo in permitidos:
            continue
        texto = ruta.read_text(encoding="utf-8")
        for patron in patrones:
            if patron in texto:
                culpables.append(f"{relativo}: {patron}")
    assert not culpables, (
        "asignaciones directas a COMPLETED fuera de la autoridad: " + "; ".join(culpables)
    )


def test_la_api_no_puede_crear_una_mision_completada():
    """Caso 11: lo que expone la API no incluye una vía para declarar COMPLETED."""
    fuente = (PROJECT_ROOT / "apps" / "api" / "main.py").read_text(encoding="utf-8")
    assert "MissionState.COMPLETED" not in fuente
    assert "state = MissionState.COMPLETED" not in fuente

    # Y la ruta de creación de la API deja la misión en un estado que no es COMPLETED.
    mission = MissionEngine().create(
    "crea una nota",
    MissionEnvelope(
        objective="crea una nota",
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=list(ACTIONS),
    ),
    )
    assert mission.state is MissionState.PENDING


def test_el_demo_no_tiene_una_via_para_forzar_completed():
    fuente = (PROJECT_ROOT / "apps" / "demo" / "server.py").read_text(encoding="utf-8")
    assert "state = MissionState.COMPLETED" not in fuente


@pytest.mark.asyncio
async def test_el_camino_legacy_no_completa_sin_verificacion(tmp_path):
    """Caso 12: el legacy respeta la regla; sin GoalVerifier no completa."""
    from alexis.cognition.planner import Planner

    (tmp_path / "notas.txt").write_text("contenido", encoding="utf-8")
    mission = _mission("lee notas.txt", ["Los tests del proyecto pasan"])
    mission.plan = await Planner().create_plan(mission)
    runtime = AlexisRuntime(
    planner=Planner(),
    policy=PolicyEngine(),
    executor=_real_executor(tmp_path),
    verifier=FilesystemVerifier(tmp_path),
    memory=InMemoryMemory(),
    learning=ExperienceLearner(),
    event_bus=EventBus(),
    gate=AutonomyGate(),
    )

    result = await runtime.run_mission(mission)

    assert result.state is MissionState.NEEDS_VERIFICATION
    assert "no hay GoalVerifier" in (result.context.get("goal_verification_reason") or "")


@pytest.mark.asyncio
async def test_el_camino_legacy_completa_con_la_misma_autoridad(tmp_path):
    """Caso 12 (segunda mitad): con evidencia real, el legacy también completa."""
    from alexis.cognition.planner import Planner

    (tmp_path / "notas.txt").write_text("contenido", encoding="utf-8")
    mission = _mission(
    "lee notas.txt",
    ["El archivo file_exists:notas.txt está escrito"],
    )
    mission.plan = await Planner().create_plan(mission)
    world = WorldModel()
    runtime = AlexisRuntime(
    planner=Planner(),
    policy=PolicyEngine(),
    executor=_real_executor(tmp_path),
    verifier=FilesystemVerifier(tmp_path),
    memory=InMemoryMemory(),
    learning=ExperienceLearner(),
    event_bus=EventBus(),
    gate=AutonomyGate(),
    world=world,
    goal_verifier=GoalVerifier(world=world),
    )

    result = await runtime.run_mission(mission)

    assert result.goal_verification is not None
    assert result.goal_verification.verified is True
    assert result.state is MissionState.COMPLETED


@pytest.mark.asyncio
async def test_no_verificado_no_es_failure(tmp_path):
    """Un objetivo no demostrado deja la misión viva, no failed.

    `NEEDS_VERIFICATION` significa "todavía no". Confundirlo con `FAILED` sería la otra
    cara del mismo error: mentir por exceso.
    """
    mission = _mission("obj", ["El archivo file_exists:informe.md existe"])
    world = WorldModel()

    estado = settle(mission, GoalVerifier(world=world).verify(mission))

    assert estado is MissionState.NEEDS_VERIFICATION
    assert estado is not MissionState.FAILED
    assert estado is not MissionState.BLOCKED
