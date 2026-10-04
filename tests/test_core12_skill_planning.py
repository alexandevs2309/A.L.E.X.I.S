"""CORE-12 — Planning dirigido por skills: la reutilización gobierna el plan.

CORE-11 dejó la skill construida, validada y guardada, y el E2E la "reutilizó"
llamando a `registry.match` a mano y ejecutando su procedure fuera del runtime.
Eso no es reutilización: es un test que maneja la skill.

Aquí la skill entra en la planificación de verdad: cuando el objetivo de una misión
coincide con el `applicability` de una skill validada (y la misión puede ofrecer sus
capabilities), `_plan_with_rules` genera el plan DESDE su procedure, en vez de la
plantilla fija `understand→research→execute→verify`. No es una excepción: el plan de
la skill se valida igual que cualquier otro, y si no valida, cae a la plantilla.

Regla heredada del registro: sólo `MATCH` reutiliza. `UNCERTAIN` disfraza de
estrategia lo que es conjetura.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.gates import AutonomyGate  # noqa: E402
from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.capabilities import build_catalog  # noqa: E402
from alexis.cognition.goal_verification import GoalVerifier  # noqa: E402
from alexis.cognition.planner_model import PlanValidator  # noqa: E402
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    MissionEnvelope,
    PlanStep,
    RiskLevel,
)
from alexis.core.runtime import AlexisRuntime  # noqa: E402
from alexis.events.bus import EventBus  # noqa: E402
from alexis.execution import SandboxExecutor  # noqa: E402
from alexis.learning.lesson import (  # noqa: E402
    Lesson,
    Outcome,
    build_lesson_from_outcome,
)
from alexis.learning.skill import (  # noqa: E402
    SkillMatch,
    SkillRegistry,
    SkillValidator,
    skill_from_lesson,
)
from alexis.learning.system import ExperienceLearner  # noqa: E402
from alexis.memory.store import InMemoryMemory  # noqa: E402
from alexis.security.policy import PolicyEngine  # noqa: E402
from alexis.security.sandbox import SandboxRunner  # noqa: E402
from alexis.tools.filesystem import build_filesystem_tools  # noqa: E402
from alexis.tools.registry import ToolRegistry  # noqa: E402
from alexis.verification import FilesystemVerifier  # noqa: E402
from alexis.world.model import WorldModel  # noqa: E402

ACTIONS = ["understand", "analyze", "research", "execute", "verify", "modify", "test", "respond"]
CAPS = build_catalog()
FULL_CAPS = [spec.id for spec in CAPS.specs() if spec.status == "available"]


def _mission(objective="crea el archivo informe.txt", criteria=None, caps=None):
    return MissionEngine().create(
        objective,
        MissionEnvelope(objective=objective, autonomy=AutonomyLevel.SUPERVISED,
                        allowed_actions=list(ACTIONS),
                        capabilities=list(caps if caps is not None else FULL_CAPS)),
        success_criteria=list(criteria or ["file_exists:informe.txt"]),
    )


def _step(step_id, capability, action="execute", risk=RiskLevel.LOW, **args):
    return PlanStep(step_id, f"paso {step_id}", action, risk, "executor",
                    capability=capability, args=dict(args))


def _runtime(workspace):
    sandbox = SandboxRunner(workspace=workspace)
    tools = ToolRegistry()
    for tool in build_filesystem_tools(workspace):
        tools.register(tool)
    world = WorldModel()
    rt = AlexisRuntime(
        planner=None,
        policy=PolicyEngine(),
        executor=SandboxExecutor(tools=tools, sandbox=sandbox),
        verifier=FilesystemVerifier(workspace=workspace),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        world=world,
    )
    from alexis.cognition.loop import CognitiveRuntime

    rt.cognitive = CognitiveRuntime(
        policy=rt.policy, gate=rt.gate, executor=rt.executor, verifier=rt.verifier,
        model_router=None, catalog=CAPS, world=world, goal_verifier=GoalVerifier(world=world),
    )
    from alexis.cognition.planner import Planner

    rt.planner = Planner()  # como apps/demo/app.py: el suelo por reglas
    return rt


def _seed_skill(
    rt,
    objective: str = "crea el archivo informe.txt",
    *,
    procedure_caps=("fs.write", "fs.stat"),
    required=("fs.write", "fs.stat"),
):
    """Siembra una skill validada por el pipeline REAL lesson → candidata → versión.

    Sin ejecución: lo que importa aquí es que la versión validada exista y el planner
    la pueda usar. La creación por ejecución se prueba en el E2E.
    """
    lesson = build_lesson_from_outcome(
        experience_id="e-seed",
        statement="crear un archivo de texto con fs.write y comprobar con fs.stat",
        outcome=Outcome.SUCCESS,
        evidence=["claim-seed"],
        scope="fs/derived",
        applicability=objective,
        contraindications=["no aplicar fuera del envelope"],
        prerequisites=["fs.write", "fs.stat"],
    )
    candidate = skill_from_lesson(lesson)
    candidate.required_capabilities = list(required)
    candidate.procedure = [
        {"id": "escribir", "action": "execute", "capability": procedure_caps[0], "risk": "medium",
         "args": {"path": "informe.txt", "content": "hola"}, "requires_approval": False, "side_effects": True},
        {"id": "comprobar", "action": "research", "capability": procedure_caps[1], "risk": "low",
         "args": {"path": "informe.txt"}, "requires_approval": False, "side_effects": False},
    ]
    registry = rt.skill_registry()
    validator = SkillValidator(catalog=CAPS, policy=rt.policy)
    envelope = MissionEnvelope(objective=objective, autonomy=AutonomyLevel.SUPERVISED,
                               allowed_actions=list(ACTIONS),
                               capabilities=list(required) + ["fs.read"])
    version = validator.promote(candidate, envelope=envelope, existing=registry.all())
    assert version is not None, candidate.provenance.get("rejection_reasons")
    registry.add(version)
    return version


# ======================================================================
# Sin skills, el plan sigue siendo el suelo por reglas
# ======================================================================


@pytest.mark.asyncio
async def test_01_sin_skills_usa_el_plan_por_reglas(tmp_path):
    """Sin ninguna skill el comportamiento de siempre: el suelo por reglas (Req 6:
    la forma del plan depende del objetivo; el objetivo de escritura genera
    `execute` → `verify`)."""
    rt = _runtime(tmp_path)
    mission = _mission()
    await rt._ensure_plan(mission)

    provenance = mission.context["plan_provenance"]
    assert provenance["proposed_by"] == "rule_based"
    assert provenance.get("strategy") == "objective_driven"
    assert [s.id for s in mission.plan.steps] == ["execute", "verify"]


# ======================================================================
# Con una skill validada, el plan es su procedure
# ======================================================================


@pytest.mark.asyncio
async def test_02_skill_validada_genera_el_plan_desde_su_procedure(tmp_path):
    """La reutilización sin mano: la skill ES el plan, no una descripción suya."""
    rt = _runtime(tmp_path)
    version = _seed_skill(rt)
    mission = _mission()

    await rt._ensure_plan(mission)

    steps = mission.plan.steps
    assert [s.id for s in steps] == ["escribir", "comprobar"]
    assert [s.capability for s in steps] == ["fs.write", "fs.stat"]
    assert all(s.proposed_by == "skill" for s in steps)
    assert steps[0].args == {"path": "informe.txt", "content": "hola"}

    provenance = mission.context["plan_provenance"]
    assert provenance["proposed_by"] == "skill"
    assert provenance["skill_id"] == version.skill_id
    assert provenance["skill_version"] == 1
    assert "MATCH" in " ".join(provenance["reasons"]) or "coincide" in " ".join(provenance["reasons"])


@pytest.mark.asyncio
async def test_03_match_con_fuera_del_objetivo_no_reutiliza(tmp_path):
    """Un objetivo sin relación → NO_MATCH → la plantilla. La skill no decide sola."""
    rt = _runtime(tmp_path)
    _seed_skill(rt, "crea el archivo informe.txt")
    mission = _mission(objective="responde quién es el inventor de la imprenta")

    await rt._ensure_plan(mission)

    assert mission.context["plan_provenance"]["proposed_by"] == "rule_based"
    # Objetivo informacional: la plantilla es entender+responder; lo que importa es que
    # ningún paso venga de la skill.
    assert all(s.proposed_by != "skill" for s in mission.plan.steps)
    assert not any(s.id in ("escribir", "comprobar") for s in mission.plan.steps)


@pytest.mark.asyncio
async def test_04_skill_que_necesita_capability_ausente_no_aplica(tmp_path):
    """La misión no puede ofrecer lo que la skill necesita → no reutiliza.

    La skill pide `fs.write` y la misión no lo ofrece: el registro la filtra en el
    momento de buscar (requisito 2 de `match`). El pipeline CORE-11 ni siquiera permite
    crear una skill que pida una capability inexistente: esto es el lado realista.
    Nota: sin `fs.write` tampoco la plantilla puede ejecutar, así que aquí lo honesto es
    que la skill NUNCA se use — el plan queda sin pasos de skill o sin plan, nunca con
    una estrategia que la misión no puede ejecutar.
    """
    rt = _runtime(tmp_path)
    _seed_skill(rt)  # requiere fs.write + fs.stat
    mission = _mission(objective="crea el archivo informe.txt",
                       caps=["fs.read", "fs.stat"])

    await rt._ensure_plan(mission)

    provenance = mission.context.get("plan_provenance", {})
    assert provenance.get("proposed_by") != "skill", provenance
    if mission.plan is not None:
        assert all(s.proposed_by != "skill" for s in mission.plan.steps)
        assert not any(s.id in ("escribir", "comprobar") for s in mission.plan.steps)


# ======================================================================
# La skill no es una excepción a la validación
# ======================================================================


@pytest.mark.asyncio
async def test_05_plan_de_skill_que_no_valida_cae_al_planner_por_reglas(tmp_path):
    """Una skill con una capability fuera del catálogo no cuela su plan: se valida
    igual que cualquier plan, y al fallar, cae al suelo por reglas (objetivo)."""
    rt = _runtime(tmp_path)
    rt.plan_validator = PlanValidator(catalog=CAPS)
    # `fs.hack` no existe en el catálogo real: el plan propuesto por la skill no pasa.
    _seed_skill(rt, procedure_caps=("fs.hack", "fs.stat"))
    mission = _mission()

    await rt._ensure_plan(mission)

    provenance = mission.context["plan_provenance"]
    assert provenance["proposed_by"] == "rule_based", provenance
    assert [s.id for s in mission.plan.steps] == ["execute", "verify"]


# ======================================================================
# E2E: la segunda misión se planea sola con la skill
# ======================================================================


@pytest.mark.asyncio
async def test_06_e2e_la_segunda_mision_se_planea_sola_con_la_skill(tmp_path):
    """Cinco hechos en una pieza:

    1. La misión 1 ejecuta `fs.write`+`fs.stat` de verdad y verifica el objetivo.
    2. Del cierre sale una skill validada (pipeline CORE-11 real).
    3. La misión 2, SIN que nadie toque el registro a mano, planifica sola y su plan
       ES la procedure de la skill (proposed_by=skill).
    4. El plan se ejecuta de verdad con herramientas reales y el fichero reaparece.
    5. La verificación de la misión 2 lo confirma.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    rt = _runtime(workspace)

    # -- Misión 1: aprende la estrategia (CORE-11, sin atajos) ----------------- #
    from alexis.contracts import Plan
    from alexis.learning.lesson import classify_outcome

    mission1 = _mission()
    mission1.plan = Plan(mission1.id, [
        _step("escribir", "fs.write", risk=RiskLevel.MEDIUM, path="informe.txt", content="hola"),
        _step("comprobar", "fs.stat", action="research", path="informe.txt"),
    ])
    knowledge = rt.cognitive.knowledge_for(mission1)
    for step in mission1.plan.steps:
        result = await rt.executor.execute(mission1, step, tool_name=step.capability)
        assert result.success, result.error
        mission1.results.append({"step": step.id, "success": True, "output": result.output})
        for claim in rt.cognitive.evidence.from_execution_result(result):
            knowledge.add_claim(claim)
        rt.cognitive.observe_world(mission1, step, result)
        rt._record_decision(mission1, step, rt.gate.decide(mission1, step, rt.policy))
    rt.cognitive.store_knowledge(mission1, knowledge)

    verification1 = GoalVerifier(world=rt.world).verify(mission1)
    assert verification1.verified is True, verification1.reason
    await rt.learning.record_experience(mission1, verification1)
    await rt._advance_learning(mission1)

    registry = rt.skill_registry()
    assert len(registry.all()) == 1
    assert (workspace / "informe.txt").is_file()

    # La prueba tiene que ser que la misión 2 CREA el fichero, no que sobra uno viejo.
    (workspace / "informe.txt").unlink()

    # -- Misión 2: se planifica sola desde la skill, sin que nadie la toque ----- #
    mission2 = _mission()
    await rt._ensure_plan(mission2)

    provenance = mission2.context["plan_provenance"]
    assert provenance["proposed_by"] == "skill", provenance
    assert [s.id for s in mission2.plan.steps] == ["escribir", "comprobar"]
    assert all(s.proposed_by == "skill" for s in mission2.plan.steps)

    # Se ejecuta el plan real: cada paso pasa por Gate como cualquier otro.
    for step in mission2.plan.steps:
        decision = rt.gate.decide(mission2, step, rt.policy)
        assert decision.allowed is True, "una skill no puede saltarse la autoridad"
        result = await rt.executor.execute(mission2, step, tool_name=step.capability)
        assert result.success, f"la skill falló en {step['id'] if isinstance(step, dict) else step.id}: {result.error}"
        rt.cognitive.observe_world(mission2, step, result)
        mission2.results.append({"step": step.id, "success": True, "output": result.output})

    assert (workspace / "informe.txt").is_file(), "la skill debía recrear el fichero"

    verification2 = GoalVerifier(world=rt.world).verify(mission2)
    assert verification2.verified is True, verification2.reason