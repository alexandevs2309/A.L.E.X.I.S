"""Cierre CORE-11/12 (BLOCKER-1) — lo aprendido sobrevive al reinicio.

El agujero real era éste: el ciclo CORE-11 aprendía y guardaba, pero un runtime nuevo nacía
con el `SkillRegistry` VACÍO. Una skill validada en la misión de ayer no servía hoy, porque
nadie la cargaba de vuelta del `learning_repo` que la guardó. Un registro cuyo contenido se
pierde al apagar no es un registro: es memoria RAM con nombre de base de datos.

Aquí se cierra con `AlexisRuntime.hydrate_learning()`: versiones y rendimiento validados se
cargan UNA vez por instancia desde el MISMO repositorio que los guarda, y la carga ocurre
antes de planificar, de aprender y de registrar rendimiento. Y el E2E lo demuestra con un
reinicio de verdad: runtime 1 aprende y persiste, runtime 2 (mismo repositorio, SIN copiar
nada en memoria) planifica reutilizando la skill que sólo estaba en la base.

Regla de fondo que se mantiene: recordar mal no puede tumbar ni planificar ni aprender.
La hidratación es una caché optimista; un fallo se registra y se sigue con lo que haya.
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
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    MissionEnvelope,
    Plan,
    PlanStep,
    RiskLevel,
)
from alexis.core.runtime import AlexisRuntime  # noqa: E402
from alexis.events.bus import EventBus  # noqa: E402
from alexis.execution import SandboxExecutor  # noqa: E402
from alexis.learning.skill import (  # noqa: E402
    SkillCandidate,
    SkillPerformance,
    SkillStatus,
    SkillValidator,
    SkillVersion,
)
from alexis.learning.system import ExperienceLearner  # noqa: E402
from alexis.memory.store import InMemoryMemory  # noqa: E402
from alexis.security.policy import PolicyEngine  # noqa: E402
from alexis.security.sandbox import SandboxRunner  # noqa: E402
from alexis.storage.repositories import LearningRepository  # noqa: E402
from alexis.tools.filesystem import build_filesystem_tools  # noqa: E402
from alexis.tools.registry import ToolRegistry  # noqa: E402
from alexis.verification import FilesystemVerifier  # noqa: E402
from alexis.world.model import WorldModel  # noqa: E402

ACTIONS = ["understand", "analyze", "research", "execute", "verify", "modify", "test", "respond"]
CAPS = build_catalog()


def _mission(objective="crea el archivo informe.txt", criteria=None, caps=None):
    return MissionEngine().create(
        objective,
        MissionEnvelope(objective=objective, autonomy=AutonomyLevel.SUPERVISED,
                        allowed_actions=list(ACTIONS),
                        capabilities=list(caps if caps is not None else ["fs.write", "fs.stat", "fs.read"])),
        success_criteria=list(criteria or ["file_exists:informe.txt"]),
    )


def _step(step_id, capability, action="execute", risk=RiskLevel.LOW, **args):
    return PlanStep(step_id, f"paso {step_id}", action, risk, "executor",
                    capability=capability, requires_approval=False, args=dict(args))


def _runtime(workspace, repo):
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
        learning_repo=repo,
    )
    from alexis.cognition.loop import CognitiveRuntime

    rt.cognitive = CognitiveRuntime(
        policy=rt.policy, gate=rt.gate, executor=rt.executor, verifier=rt.verifier,
        model_router=None, catalog=CAPS, world=world, goal_verifier=GoalVerifier(world=world),
    )
    return rt


def _candidate(**over):
    base = dict(
        name="fs_derived",
        purpose="crear un archivo y confirmar que existe sin duplicar la escritura",
        required_capabilities=["fs.write", "fs.stat"],
        procedure=[
            {"id": "escribir", "capability": "fs.write", "action": "execute",
             "args": {"path": "informe.txt"}, "requires_approval": False},
            {"id": "comprobar", "capability": "fs.stat", "action": "research",
             "args": {"path": "informe.txt"}, "requires_approval": False},
        ],
        success_criteria=["el archivo existe"],
        verification=["fs.stat sobre el path"],
        risk="medium",
        applicability="crea el archivo informe.txt :: fs.write, fs.stat",
        contraindications=["no aplica a borrados"],
    )
    base.update(over)
    return SkillCandidate(**base)


def _validated_v1(**over):
    return SkillValidator(catalog=CAPS).promote(_candidate(**over))


# ======================================================================
# Hidratación
# ======================================================================


@pytest.mark.asyncio
async def test_01_sin_repositorio_no_hidrata_y_no_reintenta(tmp_path):
    """Sin `learning_repo` no hay nada que recordar, pero la instancia no vuelve a
    preguntar misión a misión: una guarda por instancia y se acabó."""
    rt = _runtime(tmp_path / "ws", repo=None)

    await rt.hydrate_learning()

    assert rt.skill_registry().all() == []
    assert getattr(rt, "_learning_hydrated", False) is True
    await rt.hydrate_learning()  # una segunda llamada no rompe ni duplica
    assert rt.skill_registry().all() == []


@pytest.mark.asyncio
async def test_02_hidrata_versiones_y_rendimiento_desde_la_bd(db, tmp_path):
    """Una version validada y una ejecución guardadas vuelven al registro del runtime.

    La rehidratación carga exactamente lo que el ciclo guardó: la version por su payload y el
    rendimiento por el suyo, y el registro queda como si nunca se hubiera apagado."""
    repo = LearningRepository(db)
    version = _validated_v1()
    await repo.save_skill_version(version)
    await repo.save_performance(SkillPerformance(
        skill_id=version.skill_id, version=1, mission_id="m1",
        outcome="completed", verified=True,
    ))

    rt = _runtime(tmp_path / "ws", repo)
    await rt.hydrate_learning()

    assert getattr(rt, "_learning_hydrated", False) is True
    loaded = rt.skill_registry().all()
    assert len(loaded) == 1
    assert loaded[0].skill_id == version.skill_id
    assert loaded[0].version == 1
    assert loaded[0].name == "fs_derived"
    assert loaded[0].status == SkillStatus.VALIDATED.value
    records = rt.skill_registry().performance_for(version.skill_id)
    assert len(records) == 1
    assert records[0].verified is True


@pytest.mark.asyncio
async def test_03_una_version_no_utilizable_no_se_hidrata(db, tmp_path):
    """Sólo se hidratan versiones utilizables (`validated`/`superseded`).

    Una version rechazada no se carga: el registro no guarda estrategias que la validación
    ya descartó, y `registry.match` las saltaría igualmente."""
    repo = LearningRepository(db)
    version = _validated_v1()
    rejected = SkillVersion.from_dict(version.to_dict())
    rejected.status = SkillStatus.REJECTED.value
    await repo.save_skill_version(version)
    await repo.save_skill_version(rejected)

    rt = _runtime(tmp_path / "ws", repo)
    await rt.hydrate_learning()

    loaded = rt.skill_registry().all()
    assert len(loaded) == 1
    assert loaded[0].status == SkillStatus.VALIDATED.value


@pytest.mark.asyncio
async def test_05_la_hidratacion_es_idempotente(db, tmp_path):
    """Dos llamadas no duplican: la hidratación es una guarda por instancia, y aunque se
    llamara de nuevo, la deduplicación por (skill_id, version) la hace segura."""
    repo = LearningRepository(db)
    await repo.save_skill_version(_validated_v1())

    rt = _runtime(tmp_path / "ws", repo)
    await rt.hydrate_learning()
    await rt.hydrate_learning()

    assert len(rt.skill_registry().all()) == 1


@pytest.mark.asyncio
async def test_06_no_duplica_lo_que_el_ciclo_activo_ya_publico(db, tmp_path):
    """Si una misión publicó v1 en memoria ANTES de hidratar, la carga no la repite."""
    repo = LearningRepository(db)
    version = _validated_v1()
    await repo.save_skill_version(version)

    rt = _runtime(tmp_path / "ws", repo)
    rt.skill_registry().add(SkillVersion.from_dict(version.to_dict()))
    await rt.hydrate_learning()

    assert len(rt.skill_registry().all()) == 1


@pytest.mark.asyncio
async def test_07_tras_reinicio_la_mejora_crea_v2_sobre_lo_hidratado(db, tmp_path):
    """El versionado inmutable no se rompe por el reinicio: `promote` calcula la versión
    siguiente sobre lo hidratado, y v1 pasa a `superseded` en memoria.

    Antes de este cierre el registro nacía vacío y `promote` volvía a proponer v1, con lo
    que la mejora chocaba contra el `ON CONFLICT DO NOTHING` y se perdía en silencio."""
    repo = LearningRepository(db)
    await repo.save_skill_version(_validated_v1())

    rt = _runtime(tmp_path / "ws", repo)
    await rt.hydrate_learning()

    v2 = SkillValidator(catalog=CAPS).promote(_candidate(), existing=rt.skill_registry().all())
    assert v2 is not None
    assert v2.version == 2
    assert v2.status == SkillStatus.VALIDATED.value
    assert rt.skill_registry().all()[0].status == SkillStatus.SUPERSEDED.value


# ======================================================================
# E2E: el reinicio de verdad reutiliza lo aprendido
# ======================================================================


@pytest.mark.asyncio
async def test_04_e2e_reinicio_en_medio_reutiliza_la_skill_persistida(db, tmp_path):
    """El ciclo COMPLETO a través de un reinicio, sin copiar nada en memoria.

    Runtime 1: ejecución REAL (SandboxExecutor + GoalVerifier), epílogo REAL
    (`record_experience` → `_advance_learning`) y todo queda persistido en `learning_repo`.

    Runtime 2: instancia NUEVA con el mismo repositorio y el registro vacío — es el reinicio.
    La primera planificación hidrata, encuentra la skill SÓLO porque la base la guardó, y la
    usa como estrategia (`proposed_by == "skill"`). Cualquier sistema que aprenda y no haga
    esto, aprende para nada.
    """
    ws1 = tmp_path / "ws1"
    ws1.mkdir()
    repo = LearningRepository(db)
    rt = _runtime(ws1, repo)

    # ---------- Misión 1: verificada de verdad, persistida ------------- #
    mission = _mission()
    plan = Plan(mission.id, [
        _step("escribir", "fs.write", risk=RiskLevel.MEDIUM, path="informe.txt", content="hola"),
        _step("comprobar", "fs.stat", action="research", path="informe.txt"),
    ])
    mission.plan = plan

    knowledge = rt.cognitive.knowledge_for(mission)
    for step in plan.steps:
        result = await rt.executor.execute(mission, step, tool_name=step.capability)
        assert result.success, f"{step.id} falló: {result.error}"
        mission.results.append({"step": step.id, "success": True, "output": result.output})
        for claim in rt.cognitive.evidence.from_execution_result(result):
            knowledge.add_claim(claim)
        rt.cognitive.observe_world(mission, step, result)
        rt._record_decision(mission, step, rt.gate.decide(mission, step, rt.policy))

    verification = GoalVerifier(world=rt.world).verify(mission)
    assert verification.verified is True, verification.reason
    rt.cognitive.store_knowledge(mission, knowledge)
    await rt.learning.record_experience(mission, verification)
    await rt._advance_learning(mission)

    version = mission.context.get("skill_version")
    assert version is not None, "no se produjo skill versionada"
    assert version["version"] == 1
    assert len(rt.skill_registry().all()) == 1

    # Persistida de verdad: un repositorio NUEVO sobre la misma base la ve.
    assert len(await LearningRepository(db).skill_versions(limit=10)) == 1

    # ---------- Reinicio: runtime nuevo, mismo repositorio ------------- #
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    rt2 = _runtime(ws2, repo)  # NADA se copia en memoria: `_skill_registry` no se toca.
    assert rt2.skill_registry().all() == []

    mission2 = _mission()
    await rt2._ensure_plan(mission2)

    assert mission2.plan is not None, "el reinicio no produjo plan"
    provenance = mission2.context.get("plan_provenance") or {}
    assert provenance.get("proposed_by") == "skill", (
        "la misión 2 debió usar la skill persistida, no la plantilla"
    )
    assert provenance.get("skill_id") == version["skill_id"]
    assert provenance.get("skill_version") == 1
    assert len(rt2.skill_registry().all()) == 1