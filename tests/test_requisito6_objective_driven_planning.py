"""P0 Requisito 6 — Planificación OBJETIVO-DIRIGIDA.

El suelo por reglas ya no es la secuencia universal `understand → research → execute →
verify`: es un DAG que depende del objetivo concreto (y del catálogo real). La plantilla
universal queda únicamente como FALLBACK declarado para intenciones no soportadas.

Cobertura requerida (24 categorías) + 1 E2E real a través del runtime oficial:
 1. sólo lectura           2. escritura              3. investigación/análisis
 4. razonamiento puro      5. DAG multi-paso         6. orden del DAG
 7. ciclos                 8. dependencia inexistente 9. plan mínimo
10. regresión secuencia fija 11. fallback explícito   12. precedencia de skill
13. contrato por paso      14. policy DENY           15. aprobación del gate
16. perímetro del envelope 17. verificación real     18. paso ok ≠ objetivo ok
19. replaneo determinista  20. recuperación/reanudación 21. reinicio con checkpoint
22. seguridad de modelo    23. seguridad de skill    24. sin efectos colaterales
25. E2E por el runtime oficial.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.gates import AutonomyGate
from alexis.autonomy.mission import MissionEngine
from alexis.capabilities.catalog import build_catalog
from alexis.cognition.goal_verification import GoalVerifier
from alexis.cognition.loop import CognitiveRuntime
from alexis.cognition.planner import PLANNED_FALLBACK_TEMPLATE, Planner
from alexis.cognition.planner_model import PlanValidator
from alexis.contracts import (
    AutonomyLevel,
    MissionEnvelope,
    MissionState,
    Plan,
    PlanStep,
    RiskLevel,
)
from alexis.core.runtime import AlexisRuntime
from alexis.events.bus import EventBus
from alexis.execution import SandboxExecutor
from alexis.learning.system import ExperienceLearner
from alexis.memory.store import InMemoryMemory
from alexis.security.policy import PolicyEngine
from alexis.security.sandbox import SandboxRunner
from alexis.tools.filesystem import build_filesystem_tools
from alexis.tools.registry import ToolRegistry
from alexis.verification import FilesystemVerifier
from alexis.world.model import WorldModel

ACTIONS = ["understand", "analyze", "research", "execute", "verify", "modify", "test", "respond"]
CATALOG = build_catalog()
UNIVERSAL = ["understand", "research", "execute", "verify"]


def _mission(objective, criteria=None, allowed=None, caps=None):
    return MissionEngine().create(
        objective,
        MissionEnvelope(
            objective=objective,
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=list(allowed if allowed is not None else ACTIONS),
            capabilities=list(caps) if caps is not None
            else [s.id for s in CATALOG.specs() if s.status == "available"],
        ),
        success_criteria=list(criteria or []),
    )


async def _plan(objective, **kw):
    return await Planner().create_plan(_mission(objective, **kw))


def _ids(plan):
    return [s.id for s in plan.steps]


def _actions(plan):
    return [s.action for s in plan.steps]


# ======================================================================
# 1-4. Forma del plan según la familia del objetivo
# ======================================================================


@pytest.mark.asyncio
async def test_01_lectura_es_solo_observacion():
    plan = await _plan("lee el archivo reporte.txt")
    assert _ids(plan) == ["research"]
    assert plan.steps[0].capability == "fs.read"
    assert plan.steps[0].requires_approval is False


@pytest.mark.asyncio
async def test_02_escritura_es_efecto_y_verificacion_sin_lectura():
    plan = await _plan("crea el archivo informe.txt")
    assert _ids(plan) == ["execute", "verify"]
    assert plan.steps[0].capability == "fs.write"
    assert plan.steps[0].risk is RiskLevel.MEDIUM
    assert plan.steps[0].requires_approval is False
    assert plan.steps[1].capability == "verification.filesystem"
    assert "fs.read" not in [s.capability for s in plan.steps]


@pytest.mark.asyncio
async def test_03_investigacion_reune_evidencia_y_sintetiza():
    for objective in ("analiza notas.txt", "revisa el proyecto", "investiga el estado actual"):
        plan = await _plan(objective)
        assert _ids(plan) == ["research", "analyze"]
        assert plan.steps[0].capability == "fs.read"
        assert plan.steps[1].capability == "cognition.analyze"
        assert plan.steps[1].depends_on == ["research"]


@pytest.mark.asyncio
async def test_04_razonamiento_puro_no_ejecuta_herramientas():
    plan = await _plan("¿cuál es la capital de Francia?")
    assert _ids(plan) == ["understand", "respond"]
    assert "research" not in _ids(plan)
    assert "execute" not in _ids(plan)
    assert "verify" not in _ids(plan)


# ======================================================================
# 5-9. DAG: orden, ciclos, dependencias, mínimo
# ======================================================================


@pytest.mark.asyncio
async def test_05_dag_multipaso_respeta_orden_topologico():
    plan = await _plan("borra el archivo basura.txt")
    assert _ids(plan) == ["execute", "verify"]
    assert plan.steps[1].depends_on == ["execute"]
    order = {step.id: i for i, step in enumerate(plan.steps)}
    for dep in plan.steps[1].depends_on:
        assert order[dep] < order["verify"]


@pytest.mark.asyncio
async def test_06_orden_dag_para_todas_las_familias():
    for objective in ("crea un archivo a.txt", "borra el archivo b.txt", "¿existe el archivo c.txt?"):
        plan = await _plan(objective)
        order = {step.id: i for i, step in enumerate(plan.steps)}
        for step in plan.steps:
            for dep in step.depends_on:
                assert order[dep] < order[step.id]


@pytest.mark.asyncio
async def test_07_el_validador_detecta_ciclos():
    mission = _mission("crea el archivo x.txt")
    plan = Plan(
        mission_id=mission.id,
        steps=[
            PlanStep("a", "a", "research", RiskLevel.LOW, "researcher", depends_on=["b"]),
            PlanStep("b", "b", "research", RiskLevel.LOW, "researcher", depends_on=["a"]),
        ],
    )
    reasons = PlanValidator(catalog=build_catalog()).validate(mission, plan)
    assert any("ciclo" in r for r in reasons)


@pytest.mark.asyncio
async def test_08_dependencia_a_paso_inexistente():
    mission = _mission("crea el archivo y.txt")
    plan = Plan(
        mission_id=mission.id,
        steps=[
            PlanStep("a", "a", "research", RiskLevel.LOW, "researcher"),
            PlanStep("b", "b", "research", RiskLevel.LOW, "researcher", depends_on=["no_existe"]),
        ],
    )
    reasons = PlanValidator(catalog=build_catalog()).validate(mission, plan)
    assert reasons and any("no_existe" in r or "dependencia" in r for r in reasons)


@pytest.mark.asyncio
async def test_09_plan_minimo_para_el_objetivo():
    plan = await _plan("crea el archivo z.txt")
    assert len(plan.steps) == 2
    assert _ids(plan) == ["execute", "verify"]
    assert "understand" not in _ids(plan) and "research" not in _ids(plan)


# ======================================================================
# 10-11. Regresión de secuencia fija + fallback declarado
# ======================================================================


@pytest.mark.asyncio
async def test_10_ningun_objetivo_devuelve_la_secuencia_universal():
    for objective in (
        "lee el archivo a.txt",
        "crea el archivo b.txt",
        "borra el archivo c.txt",
        "analiza el proyecto",
        "¿existe el archivo d.txt?",
    ):
        plan = await _plan(objective)
        assert _ids(plan) != UNIVERSAL
        assert _actions(plan) != ["analyze", "research", "execute", "verify"]


@pytest.mark.asyncio
async def test_11_fallback_explicito_solo_para_intencion_no_soportada(tmp_path):
    plan = await _plan("renombra el archivo reporte.txt a nuevo.txt")
    assert _ids(plan) == UNIVERSAL
    assert all(s.proposed_by == PLANNED_FALLBACK_TEMPLATE for s in plan.steps)
    execute = [s for s in plan.steps if s.id == "execute"][0]
    assert execute.capability == "execution.sandbox"

    # La TRAZA del runtime lo declara explícitamente como fallback, no como objetivo.
    rt = _runtime_with_cognitive(tmp_path)
    mission = _mission("renombra el archivo reporte.txt a nuevo.txt")
    await rt._ensure_plan(mission)
    provenance = mission.context.get("plan_provenance") or {}
    assert provenance.get("proposed_by") == "rule_based"
    assert provenance.get("strategy") == "fallback_template"


# ======================================================================
# 12-13. Precedencia de skill y contrato por paso
# ======================================================================


def _runtime_with_cognitive(workspace):
    tools = ToolRegistry()
    for tool in build_filesystem_tools(workspace):
        tools.register(tool)
    world = WorldModel()
    rt = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=SandboxExecutor(tools=tools, sandbox=SandboxRunner(workspace=workspace)),
        verifier=FilesystemVerifier(workspace=workspace),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        world=world,
    )
    rt.cognitive = CognitiveRuntime(
        policy=rt.policy,
        gate=rt.gate,
        executor=rt.executor,
        verifier=rt.verifier,
        model_router=None,
        catalog=CATALOG,
        world=world,
        goal_verifier=GoalVerifier(world=world),
    )
    return rt


def _seed_valid_skill(rt, objective="crea el archivo informe.txt"):
    from alexis.learning.lesson import Lesson, Outcome, build_lesson_from_outcome
    from alexis.learning.skill import SkillValidator, skill_from_lesson

    lesson = build_lesson_from_outcome(
        experience_id="e-req6",
        statement="crear un archivo de texto con fs.write y comprobar con fs.stat",
        outcome=Outcome.SUCCESS,
        evidence=["claim-req6"],
        scope="fs/derived",
        applicability=objective,
        contraindications=["no aplicar fuera del envelope"],
        prerequisites=["fs.write", "fs.stat"],
    )
    candidate = skill_from_lesson(lesson)
    candidate.required_capabilities = ["fs.write", "fs.stat"]
    candidate.procedure = [
        {"id": "escribir", "action": "execute", "capability": "fs.write", "risk": "medium",
         "args": {"path": "informe.txt", "content": "hola"}, "requires_approval": False, "side_effects": True},
        {"id": "comprobar", "action": "research", "capability": "fs.stat", "risk": "low",
         "args": {"path": "informe.txt"}, "requires_approval": False, "side_effects": False},
    ]
    registry = rt.skill_registry()
    envelope = MissionEnvelope(objective=objective, autonomy=AutonomyLevel.SUPERVISED,
                               allowed_actions=list(ACTIONS), capabilities=["fs.write", "fs.stat", "fs.read"])
    version = SkillValidator(catalog=CATALOG, policy=rt.policy).promote(candidate, envelope=envelope, existing=registry.all())
    assert version is not None
    registry.add(version)
    return version


@pytest.mark.asyncio
async def test_12_la_skill_validada_tiene_precedencia(tmp_path):
    rt = _runtime_with_cognitive(tmp_path)
    _seed_valid_skill(rt)
    mission = _mission("crea el archivo informe.txt")
    await rt._ensure_plan(mission)
    assert mission.plan.steps[0].id == "escribir"
    assert mission.plan.steps[1].id == "comprobar"


@pytest.mark.asyncio
async def test_13_cada_paso_lleva_objetivo_y_criterios():
    plan = await _plan("crea el archivo informe.txt")
    assert [s.id for s in plan.steps] == ["execute", "verify"]
    for step in plan.steps:
        assert step.objective, f"{step.id} sin objective"
        assert step.success_criteria, f"{step.id} sin success_criteria"
    verify = plan.steps[1]
    assert "verificar con observaciones independientes y registrar el resultado" in verify.success_criteria
    # P0 §11 / Req #7: el paso de escritura no describe su éxito, lo DECLARA con un
    # predicado que el GoalVerifier sabe evaluar. Antes era prosa ("deja ... en el estado
    # previsto") y nadie la evaluaba: el paso se completaba por `tool.success`.
    assert plan.steps[0].success_criteria == ["file_exists:informe.txt"]


# ======================================================================
# 14-16. Policy, gate y envelope
# ======================================================================


@pytest.mark.asyncio
async def test_14_policy_deny_bloquea_la_mision(tmp_path):
    tools = ToolRegistry()
    for tool in build_filesystem_tools(tmp_path):
        tools.register(tool)
    world = WorldModel()
    rt = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=SandboxExecutor(tools=tools, sandbox=SandboxRunner(workspace=tmp_path)),
        verifier=FilesystemVerifier(workspace=tmp_path),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        world=world,
        goal_verifier=GoalVerifier(world=world),
    )
    # La policy impide `execute` para esta misión → el plan de escritura queda BLOCKED.
    mission = _mission("crea el archivo vetado.txt", allowed=["research"])
    await rt.run_mission(mission)
    assert mission.state is MissionState.BLOCKED
    assert mission.context.get("blocked_reason")


@pytest.mark.asyncio
async def test_15_el_borrado_pide_aprobacion_del_gate(tmp_path):
    tools = ToolRegistry()
    for tool in build_filesystem_tools(tmp_path):
        tools.register(tool)
    (tmp_path / "para-borrar.txt").write_text("x", encoding="utf-8")
    world = WorldModel()
    rt = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=SandboxExecutor(tools=tools, sandbox=SandboxRunner(workspace=tmp_path)),
        verifier=FilesystemVerifier(workspace=tmp_path),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        world=world,
        goal_verifier=GoalVerifier(world=world),
    )
    mission = _mission("borra el archivo para-borrar.txt")
    await rt.run_mission(mission)
    assert mission.state is MissionState.WAITING_APPROVAL
    pending = mission.context.get("pending_approval") or {}
    assert pending.get("step") == "execute"
    assert (tmp_path / "para-borrar.txt").exists()


@pytest.mark.asyncio
async def test_16_el_perimetro_del_envelope_acota_el_plan():
    mission = _mission(
        "crea el archivo informe.txt",
        allowed=["research"],
        caps=["fs.write"],
        criteria=["file_exists:informe.txt"],
    )
    plan = await Planner().create_plan(mission)
    reasons = PlanValidator(catalog=build_catalog()).validate(mission, plan)
    assert reasons, "el validador debe rechazar un plan fuera del perímetro del envelope"
    assert any("envelope" in r for r in reasons)


# ======================================================================
# 17-18. Verificación real
# ======================================================================


@pytest.mark.asyncio
async def test_17_objetivo_verificado_con_observacion_real(tmp_path):
    tools = ToolRegistry()
    for tool in build_filesystem_tools(tmp_path):
        tools.register(tool)
    world = WorldModel()
    rt = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=SandboxExecutor(tools=tools, sandbox=SandboxRunner(workspace=tmp_path)),
        verifier=FilesystemVerifier(workspace=tmp_path),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        world=world,
        goal_verifier=GoalVerifier(world=world),
    )
    mission = _mission(
        "crea el archivo informe.txt",
        criteria=["El archivo file_exists:informe.txt está escrito"],
    )
    await rt.run_mission(mission)
    assert mission.state is MissionState.COMPLETED
    assert (tmp_path / "informe.txt").exists()
    assert all(r["success"] for r in mission.results)


@pytest.mark.asyncio
async def test_18_paso_ok_no_es_objetivo_ok(tmp_path):
    tools = ToolRegistry()
    for tool in build_filesystem_tools(tmp_path):
        tools.register(tool)
    world = WorldModel()
    rt = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=SandboxExecutor(tools=tools, sandbox=SandboxRunner(workspace=tmp_path)),
        verifier=FilesystemVerifier(workspace=tmp_path),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        world=world,
        goal_verifier=GoalVerifier(world=world),
    )
    # El plan EScribe informe.txt bien, pero el objetivo pide OTRO archivo: los pasos
    # pueden salir bien y el objetivo NO cumplirse.
    mission = _mission(
        "crea el archivo informe.txt",
        criteria=["El archivo file_exists:otro.txt está escrito"],
    )
    await rt.run_mission(mission)
    assert (tmp_path / "informe.txt").exists()
    assert mission.state is not MissionState.COMPLETED


# ======================================================================
# 19-21. Replaneo, recuperación y reinicio
# ======================================================================


@pytest.mark.asyncio
async def test_19_el_replaneo_es_determinista_y_objetivo_dirigido():
    first = await _plan("crea el archivo informe.txt")
    second = await _plan("crea el archivo informe.txt")
    assert _ids(first) == _ids(second) == ["execute", "verify"]
    assert first.steps[0].capability == second.steps[0].capability == "fs.write"


@pytest.mark.asyncio
async def test_20_la_aprobacion_no_aleja_el_plan_de_su_objetivo(tmp_path):
    tools = ToolRegistry()
    for tool in build_filesystem_tools(tmp_path):
        tools.register(tool)
    (tmp_path / "temporal.txt").write_text("x", encoding="utf-8")
    world = WorldModel()
    rt = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=SandboxExecutor(tools=tools, sandbox=SandboxRunner(workspace=tmp_path)),
        verifier=FilesystemVerifier(workspace=tmp_path),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        world=world,
        goal_verifier=GoalVerifier(world=world),
    )
    mission = _mission(
        "borra el archivo temporal.txt",
        criteria=["El archivo file_missing:temporal.txt ya no está"],
    )
    await rt.run_mission(mission)
    assert mission.state is MissionState.WAITING_APPROVAL
    mission.context["approved_step_ids"] = ["execute"]
    mission.context.pop("pending_approval", None)
    mission.results.clear()
    await rt.run_mission(mission)
    assert mission.state is MissionState.COMPLETED
    assert not (tmp_path / "temporal.txt").exists()
    assert _ids(mission.plan) == ["execute", "verify"]


@pytest.mark.asyncio
async def test_21_reinicio_retoma_desde_el_checkpoint(tmp_path):
    from alexis.autonomy.task_runner import TaskRunner

    class _StubTaskRepo:
        def __init__(self):
            self.tasks = {}

        async def upsert(self, task):
            self.tasks[task.id] = task

    class _StubExecutionRepo:
        def __init__(self):
            self.rows = []

        async def insert(self, execution):
            self.rows.append(execution)

    class _StubCheckpointRepo:
        def __init__(self):
            self.checkpoints = []

        async def save(self, cp):
            self.checkpoints.append(cp)

        async def latest(self, mission_id):
            matches = [c for c in self.checkpoints if c.mission_id == mission_id]
            if not matches:
                return None
            last = matches[-1]
            return {"step_index": last.step_index, "payload": last.payload}

        async def close_open_tasks(self, mission_id, status="cancelled"):
            return

    tools = ToolRegistry()
    for tool in build_filesystem_tools(tmp_path):
        tools.register(tool)
    world = WorldModel()
    rt = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=SandboxExecutor(tools=tools, sandbox=SandboxRunner(workspace=tmp_path)),
        verifier=FilesystemVerifier(workspace=tmp_path),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        world=world,
        goal_verifier=GoalVerifier(world=world),
    )
    runner = TaskRunner(
        task_repo=_StubTaskRepo(),
        execution_repo=_StubExecutionRepo(),
        checkpoint_repo=_StubCheckpointRepo(),
        event_bus=rt.events,
    )
    rt.task_runner = runner
    mission = _mission(
        "crea el archivo informe.txt",
        criteria=["El archivo file_exists:informe.txt está escrito"],
    )
    (tmp_path / "informe.txt").write_text("checkpoint", encoding="utf-8")
    mission.results = [{"step": "execute", "success": True, "task": "execute"}]
    # Req 6: escribir genera [execute, verify]; el execute ya se hizo y está con checkpoint.
    await runner.save_checkpoint(mission, 0)
    await rt.run_mission(mission)
    assert mission.state is MissionState.COMPLETED
    assert mission.context.get("resumed_at_step") == 1
    # La reanudación no reinicia: el execute no volvió a ejecutarse.
    executed = [r for r in mission.results if r.get("step") == "execute"]
    assert len(executed) == 1


# ======================================================================
# 22-24. Seguridad de modelo/skill y efectos colaterales
# ======================================================================


@pytest.mark.asyncio
async def test_22_el_plan_del_modelo_no_inseguro_no_rompe_el_suelo(tmp_path):
    rt = _runtime_with_cognitive(tmp_path)
    mission = _mission("crea el archivo informe.txt")
    await rt._ensure_plan(mission)
    # Sin skill y sin modelo: el suelo es el plan objetivo-dirigido.
    flows = [(s.id, s.action) for s in mission.plan.steps] if mission.plan else []
    assert flows == [("execute", "execute"), ("verify", "verify")]


@pytest.mark.asyncio
async def test_23_skill_con_capability_fuera_del_catalogo_cae_al_suelo(tmp_path):
    from alexis.learning.lesson import Outcome, build_lesson_from_outcome
    from alexis.learning.skill import SkillValidator, skill_from_lesson

    rt = _runtime_with_cognitive(tmp_path)
    lesson = build_lesson_from_outcome(
        experience_id="e-sospechosa",
        statement="hackear con fs.hack",
        outcome=Outcome.SUCCESS,
        evidence=["claim-sospechosa"],
        scope="fs/derived",
        applicability="crea el archivo informe.txt",
        contraindications=["no aplicar fuera del envelope"],
        prerequisites=["fs.hack"],
    )
    candidate = skill_from_lesson(lesson)
    candidate.required_capabilities = ["fs.hack"]
    candidate.procedure = [
        {"id": "hack", "action": "execute", "capability": "fs.hack", "risk": "low",
         "args": {}, "requires_approval": False, "side_effects": True},
    ]
    validator = SkillValidator(catalog=CATALOG, policy=rt.policy)
    envelope = MissionEnvelope(objective="crea el archivo informe.txt",
                               autonomy=AutonomyLevel.SUPERVISED,
                               allowed_actions=list(ACTIONS), capabilities=["fs.hack"])
    version = validator.promote(candidate, envelope=envelope, existing=[])
    assert version is None, "una capability inexistente no puede validarse"
    mission = _mission("crea el archivo informe.txt")
    await rt._ensure_plan(mission)
    assert [s.id for s in mission.plan.steps] == ["execute", "verify"]


@pytest.mark.asyncio
async def test_24_ninguna_familia_anade_efectos_colaterales_innecesarios():
    side_effects = set()
    for objective in (
        "lee el archivo a.txt",
        "analiza notas.txt",
        "¿existe el archivo b.txt?",
        "¿cuál es la capital de Francia?",
    ):
        plan = await _plan(objective)
        for step in plan.steps:
            spec = CATALOG.get(step.capability) if step.capability else None
            if spec is not None and getattr(spec, "side_effects", False):
                side_effects.add(f"{objective}|{step.id}")
    # Ninguna familia read-only incurre en ejecución con efectos colaterales.
    assert not side_effects
    # La escritura tiene EXACTAMENTE un paso con efectos (el `execute`).
    write = await _plan("crea el archivo c.txt")
    effects = [
        step.id for step in write.steps
        if CATALOG.get(step.capability) is not None
        and getattr(CATALOG.get(step.capability), "side_effects", False)
    ]
    assert effects == ["execute"]


# ======================================================================
# 25. E2E real por el runtime oficial
# ======================================================================


@pytest.mark.asyncio
async def test_25_e2e_runtime_oficial_plan_objetivo_dirigido(tmp_path):
    """Misión real de escritura por `AlexisRuntime.run_mission` (piezas oficiales, sin
    fakes en la ruta de ejecución): el plan generado es [execute(fs.write), verify] y NO
    la secuencia universal."""
    tools = ToolRegistry()
    for tool in build_filesystem_tools(tmp_path):
        tools.register(tool)
    world = WorldModel()
    rt = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=SandboxExecutor(tools=tools, sandbox=SandboxRunner(workspace=tmp_path)),
        verifier=FilesystemVerifier(workspace=tmp_path),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        world=world,
        goal_verifier=GoalVerifier(world=world),
    )
    mission = _mission(
        "crea el archivo lanzamiento.txt",
        criteria=["El archivo file_exists:lanzamiento.txt está escrito"],
    )

    await rt.run_mission(mission)

    assert mission.state is MissionState.COMPLETED
    assert (tmp_path / "lanzamiento.txt").exists()
    assert all(r["success"] for r in mission.results)
    ids = _ids(mission.plan)
    assert ids != UNIVERSAL, "la secuencia universal ya no es el plan de un objetivo"
    assert ids == ["execute", "verify"]
    assert mission.plan.steps[0].capability == "fs.write"
    assert mission.plan.steps[1].capability == "verification.filesystem"
    assert mission.plan.steps[1].depends_on == ["execute"]