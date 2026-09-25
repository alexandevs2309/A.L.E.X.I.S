"""H2.1 — Plan Reuse Validation: una sola frontera, venga el plan de donde venga.

Antes de H2.1, `AlexisRuntime._ensure_plan` reutilizaba `mission.plan` y
`context["plan_steps"]` con un `return` temprano: **sin volver a validar**. Solo la
ruta de creación (`_plan_with_model`) pasaba por `PlanValidator`. Tras un restart, el
plan volvía por `context["plan_steps"]` sin control.

Ahora toda entrada de plan —memoria, deserialización, modelo o reglas— pasa por el mismo
`PlanValidator`, y `reutilización ≠ confianza`: no hay flag que se saltarla.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.gates import AutonomyGate  # noqa: E402
from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.capabilities import build_catalog  # noqa: E402
from alexis.cognition.planner import Planner, plan_from_dict, plan_to_dict  # noqa: E402
from alexis.cognition.planner_model import PlanValidator  # noqa: E402
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    ExecutionResult,
    MissionEnvelope,
    MissionState,
    Plan,
    PlanStep,
    RiskLevel,
)
from alexis.core.runtime import AlexisRuntime  # noqa: E402
from alexis.events.bus import EventBus  # noqa: E402
from alexis.learning.system import ExperienceLearner  # noqa: E402
from alexis.memory.store import InMemoryMemory  # noqa: E402
from alexis.security.policy import PolicyEngine  # noqa: E402
from alexis.verification import FilesystemVerifier  # noqa: E402

ALL_ACTIONS = ["understand", "analyze", "research", "execute", "verify", "modify", "test", "respond"]
ENABLED = [s.id for s in build_catalog().enabled()]


def _mission(objective="analiza notas.txt", **over):
    data = dict(
        objective=objective,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=list(ALL_ACTIONS),
        capabilities=list(ENABLED),
    )
    data.update(over)
    return MissionEngine().create(objective, MissionEnvelope(**data))


def _step(step_id="investigar", action="research", capability="fs.read", args=None, risk=RiskLevel.LOW):
    return PlanStep(
        step_id,
        f"paso {step_id}",
        action,
        risk,
        "researcher",
        capability=capability,
        args=dict(args or {}),
    )


def _valid_plan(mission):
    return Plan(
        mission.id,
        [
            _step("investigar", action="research", capability="research.filesystem", args={"path": "notas.txt"}),
            _step("verificar", action="verify", capability="verification.filesystem", args={"path": "notas.txt"}),
        ],
    )


class _Executor:
    def __init__(self):
        self.calls = []
        self.executed = []

    async def execute(self, mission, step, *, tool_name=None):
        self.calls.append(step.id)
        self.executed.append({"id": step.id, "capability": step.capability})
        return ExecutionResult(success=True, output={"ok": True, "step": step.id})

    async def __call__(self, mission, step, tool_name=None):
        return await self.execute(mission, step, tool_name=tool_name)


def _runtime(executor, workspace, *, with_validator=True):
    kwargs = {}
    if with_validator:
        kwargs["plan_validator"] = PlanValidator(catalog=build_catalog(), policy=PolicyEngine())
    return AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=executor,
        verifier=FilesystemVerifier(workspace=workspace),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        **kwargs,
    )


# ----------------------------------------------------------------------
# H2.1.1 — mission.plan válido → se revalida y se ejecuta
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_in_memory_plan_is_revalidated_and_runs(tmp_path):
    (tmp_path / 'notas.txt').write_text('contenido', encoding='utf-8')
    executor = _Executor()
    runtime = _runtime(executor, tmp_path)
    mission = _mission()
    mission.plan = _valid_plan(mission)

    result = await runtime.run_mission(mission)

    assert result.state is MissionState.COMPLETED
    assert executor.calls == ["investigar", "verificar"]


# ----------------------------------------------------------------------
# H2.1.2 — mission.plan inválido → NO ejecución
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_memory_plan_with_nonexistent_capability_never_executes(tmp_path):
    (tmp_path / 'notas.txt').write_text('contenido', encoding='utf-8')
    executor = _Executor()
    runtime = _runtime(executor, tmp_path)
    mission = _mission()
    mission.plan = Plan(mission.id, [_step("magia", capability="does.not.exist")])

    result = await runtime.run_mission(mission)

    assert "magia" not in executor.calls
    assert all(c["capability"] != "does.not.exist" for c in executor.executed)
    rejected = [r for r in result.context["plan_rejected"] if r["source"] == "in_memory"]
    assert rejected and any("does.not.exist" in r for r in rejected[0]["reasons"])


# ----------------------------------------------------------------------
# H2.1.3 — context["plan_steps"] inválido (restart) → NO ejecución
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persisted_plan_with_nonexistent_capability_never_executes(tmp_path):
    (tmp_path / 'notas.txt').write_text('contenido', encoding='utf-8')
    executor = _Executor()
    runtime = _runtime(executor, tmp_path)
    mission = _mission()
    mission.context["plan_steps"] = plan_to_dict(Plan(mission.id, [_step("magia", capability="does.not.exist")]))

    result = await runtime.run_mission(mission)

    assert "magia" not in executor.calls
    assert all(c["capability"] != "does.not.exist" for c in executor.executed)
    rejected = [r for r in result.context["plan_rejected"] if r["source"] == "context"]
    assert rejected and any("does.not.exist" in r for r in rejected[0]["reasons"])


@pytest.mark.asyncio
async def test_persisted_invalid_plan_falls_back_to_the_rule_based_planner(tmp_path):
    (tmp_path / 'notas.txt').write_text('contenido', encoding='utf-8')
    executor = _Executor()
    runtime = _runtime(executor, tmp_path)
    mission = _mission()
    mission.context["plan_steps"] = plan_to_dict(Plan(mission.id, [_step("magia", capability="does.not.exist")]))

    result = await runtime.run_mission(mission)

    assert result.state is MissionState.COMPLETED
    assert "magia" not in executor.calls
    fallback_plan = await Planner().create_plan(_mission())
    assert [s.id for s in result.plan.steps] == [s.id for s in fallback_plan.steps]


# ----------------------------------------------------------------------
# H2.1.4 — fuera del envelope / perímetro
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persisted_plan_outside_the_envelope_never_executes(tmp_path):
    (tmp_path / 'notas.txt').write_text('contenido', encoding='utf-8')
    executor = _Executor()
    runtime = _runtime(executor, tmp_path)
    mission = _mission(capabilities=["tts.speak"])
    mission.plan = _valid_plan(mission)

    result = await runtime.run_mission(mission)

    assert "investigar" not in executor.calls
    assert all(c["capability"] not in {"research.filesystem", "verification.filesystem"} for c in executor.executed)
    rejected = [r for r in result.context["plan_rejected"] if r["source"] == "in_memory"]
    reasons = " ".join(rejected[0]["reasons"])
    assert "fuera del envelope" in reasons or "policy deniega" in reasons


@pytest.mark.asyncio
async def test_persisted_plan_with_path_outside_perimeter_never_executes(tmp_path):
    (tmp_path / 'notas.txt').write_text('contenido', encoding='utf-8')
    executor = _Executor()
    runtime = _runtime(executor, tmp_path)
    mission = _mission()
    mission.plan = Plan(
        mission.id,
        [_step("leer", action="research", capability="research.filesystem", args={"path": "/etc/passwd"})],
    )

    result = await runtime.run_mission(mission)

    assert "leer" not in executor.calls
    assert all(c["id"] != "leer" for c in executor.executed)
    rejected = [r for r in result.context["plan_rejected"] if r["source"] == "in_memory"]
    assert rejected and any("perímetro" in r for r in rejected[0]["reasons"])


# ----------------------------------------------------------------------
# H2.1.5 — args inválidos: mismas reglas que un plan nuevo
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persisted_plan_with_invalid_args_never_executes(tmp_path):
    (tmp_path / 'notas.txt').write_text('contenido', encoding='utf-8')
    executor = _Executor()
    runtime = _runtime(executor, tmp_path)
    mission = _mission()
    mission.plan = Plan(
        mission.id,
        [_step("leer", action="research", capability="research.filesystem", args={"path": "../../etc/passwd"})],
    )

    result = await runtime.run_mission(mission)

    assert "leer" not in executor.calls
    assert all(c["id"] != "leer" for c in executor.executed)
    rejected = [r for r in result.context["plan_rejected"] if r["source"] in {"in_memory", "context"}]
    assert rejected and any("perímetro" in r for r in rejected[0]["reasons"])


def test_reused_and_new_plans_use_the_same_argument_rules():
    validator = PlanValidator(catalog=build_catalog(), policy=PolicyEngine())
    mission = _mission()
    step = _step("leer", action="research", capability="research.filesystem", args={"path": "../../x"})

    as_new = validator.validate_args(step)
    plan = Plan(mission.id, [step])
    as_reused = validator.validate(mission, plan)

    assert any("perímetro" in r for r in as_new)
    assert any("perímetro" in r for r in as_reused)


# ----------------------------------------------------------------------
# H2.1.6 — replan: el paso derivado también pasa la frontera
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replan_derived_step_is_validated_before_execution():
    from alexis.cognition.loop import CognitiveRuntime
    from alexis.cognition.state import Decision, NextAction

    executor = _Executor()
    validator = PlanValidator(catalog=build_catalog(), policy=PolicyEngine())
    mission = _mission()
    bad = _step("investigar", action="research", capability="research.filesystem",
                args={"path": "../../etc/passwd"})
    cognitive = CognitiveRuntime(
        policy=PolicyEngine(),
        gate=AutonomyGate(),
        executor=executor,
        verifier=FilesystemVerifier(workspace="/tmp"),
        plan_validator=validator,
    )
    decision = Decision(
        action=NextAction.RESEARCH, step_id="investigar", tool="fs.stat", capability=bad.capability
    )
    derived = cognitive._plan_step_for(decision, [bad])

    reasons = cognitive.validate_step(mission, derived)

    assert any("perímetro" in r for r in reasons)
    assert executor.calls == []


@pytest.mark.asyncio
async def test_valid_replan_derived_step_is_accepted():
    from alexis.cognition.loop import CognitiveRuntime
    from alexis.cognition.state import Decision, NextAction

    executor = _Executor()
    validator = PlanValidator(catalog=build_catalog(), policy=PolicyEngine())
    mission = _mission()
    good = _step("investigar", action="research", capability="research.filesystem", args={"path": "notas.txt"})
    cognitive = CognitiveRuntime(
        policy=PolicyEngine(),
        gate=AutonomyGate(),
        executor=executor,
        verifier=FilesystemVerifier(workspace="/tmp"),
        plan_validator=validator,
    )
    decision = Decision(
        action=NextAction.RESEARCH, step_id="investigar", tool="fs.stat", capability=good.capability
    )
    derived = cognitive._plan_step_for(decision, [good])

    assert cognitive.validate_step(mission, derived) == []


# ----------------------------------------------------------------------
# H2.1.7 — restart / deserialización
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_survives_serialization_and_still_passes_validation():
    mission = _mission()
    plan = _valid_plan(mission)
    restored = plan_from_dict(plan_to_dict(plan), mission.id)
    validator = PlanValidator(catalog=build_catalog(), policy=PolicyEngine())

    assert validator.validate(mission, restored) == []
    assert restored.steps[0].args == {"path": "notas.txt"}


# ----------------------------------------------------------------------
# H2.1.8 — validación ≠ autorización
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_plan_still_blocked_by_policy(tmp_path):
    (tmp_path / 'notas.txt').write_text('contenido', encoding='utf-8')
    executor = _Executor()
    runtime = _runtime(executor, tmp_path)
    mission = _mission(capabilities=["tts.speak"])
    mission.plan = _valid_plan(mission)

    result = await runtime.run_mission(mission)

    assert result.state is MissionState.FAILED
    assert executor.calls == []
    assert result.context["plan_provenance"]["accepted"] is False


# ----------------------------------------------------------------------
# Compatibilidad: sin validador no cambia nada
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_runtime_without_validator_behaves_exactly_as_before(tmp_path):
    """Sin validador no aparece `plan_provenance`: nada cambia de forma observable."""
    (tmp_path / "notas.txt").write_text("contenido", encoding="utf-8")
    executor = _Executor()
    runtime = _runtime(executor, tmp_path, with_validator=False)
    mission = _mission()
    mission.plan = _valid_plan(mission)

    result = await runtime.run_mission(mission)

    assert result.state is MissionState.COMPLETED
    assert executor.calls == ["investigar", "verificar"]
    assert "plan_provenance" not in result.context
    assert "plan_rejected" not in result.context


@pytest.mark.asyncio
async def test_without_validator_the_gate_still_blocks_by_policy(tmp_path):
    """Sin PlanValidator, quien bloquea es la Policy (H1), no la validación de plan."""
    (tmp_path / "notas.txt").write_text("contenido", encoding="utf-8")
    executor = _Executor()
    runtime = _runtime(executor, tmp_path, with_validator=False)
    mission = _mission(capabilities=["tts.speak"])
    mission.plan = _valid_plan(mission)

    result = await runtime.run_mission(mission)

    assert executor.calls == []
    assert result.state is MissionState.BLOCKED


@pytest.mark.asyncio
async def test_rule_based_plan_passes_the_validator_with_a_complete_envelope(tmp_path):
    (tmp_path / 'notas.txt').write_text('contenido', encoding='utf-8')
    runtime = _runtime(_Executor(), tmp_path)
    mission = _mission()

    result = await runtime.run_mission(mission)

    assert result.state is MissionState.COMPLETED
    assert result.context["plan_provenance"]["accepted"] is True
