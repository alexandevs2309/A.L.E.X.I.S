"""H3 — la exigencia de aprobación de Policy es vinculante.

El bug: `AutonomyGate` solo miraba `step.requires_approval`. Si la Policy devolvía
`requires_approval=True` (por ejemplo la regla `risk.requires_approval` para riesgo
high/critical) y el paso no lo declaraba, el gate devolvía una decisión sin aprobación y
el paso se ejecutaba. La Policy podía exigir una aprobación que nadie atendía.

Ahora: `effective_requires_approval = step.requires_approval OR policy.requires_approval`.
Un `False` en el paso no puede anular un `True` de la Policy.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.gates import AutonomyGate  # noqa: E402
from alexis.autonomy.mission import MissionEngine  # noqa: E402
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


def _mission(objective="borra notas.txt", **over):
    data = dict(
        objective=objective,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=list(ALL_ACTIONS),
        capabilities=["fs.read", "fs.write", "fs.remove"],
    )
    data.update(over)
    return MissionEngine().create(objective, MissionEnvelope(**data))


def _write_step(step_approval: bool, risk=RiskLevel.MEDIUM):
    return PlanStep(
        "escribir",
        "escribir en el workspace",
        "execute",
        risk,
        "executor",
        capability="fs.write",
        requires_approval=step_approval,
    )


class _PolicyStub:
    """Policy con veredicto controlado, para aislar la combinación de las dos fuentes."""

    def __init__(self, allowed=True, requires_approval=False, rule="test.rule"):
        self.allowed = allowed
        self.requires_approval = requires_approval
        self.rule = rule

    def evaluate(self, mission, step):
        class _D:
            pass

        decision = _D()
        decision.allowed = self.allowed
        decision.requires_approval = self.requires_approval
        decision.reason = "veredicto de prueba"
        decision.matched_rule = self.rule
        return decision

    def authorize(self, mission, step):
        return self.evaluate(mission, step)


# ----------------------------------------------------------------------
# Los cinco casos obligatorios
# ----------------------------------------------------------------------


def test_case_a_no_approval_anywhere_executes():
    """Caso A: step=False, policy=False → ALLOW."""
    gate = AutonomyGate()
    decision = gate.decide(_mission(), _write_step(False), _PolicyStub(requires_approval=False))

    assert decision.allowed is True
    assert decision.requires_approval is False


def test_case_b_step_requires_approval_pauses():
    """Caso B: step=True, policy=False → WAITING_APPROVAL."""
    gate = AutonomyGate()
    decision = gate.decide(_mission(), _write_step(True), _PolicyStub(requires_approval=False))

    assert decision.allowed is True
    assert decision.requires_approval is True


def test_case_c_policy_required_approval_cannot_be_ignored_by_gate():
    """Caso C (el bug): step=False, policy=True → WAITING_APPROVAL, no ejecuta."""
    gate = AutonomyGate()
    decision = gate.decide(_mission(), _write_step(False), _PolicyStub(requires_approval=True))

    assert decision.allowed is True
    assert decision.requires_approval is True
    assert "policy" in decision.reason


def test_case_d_both_require_approval_pauses():
    """Caso D: step=True, policy=True → WAITING_APPROVAL."""
    gate = AutonomyGate()
    decision = gate.decide(_mission(), _write_step(True), _PolicyStub(requires_approval=True))

    assert decision.allowed is True
    assert decision.requires_approval is True


def test_case_e_policy_deny_stays_deny_not_approval():
    """Caso E: policy deny NO se convierte en solicitud de aprobación."""
    gate = AutonomyGate()
    decision = gate.decide(
        _mission(),
        _write_step(False),
        _PolicyStub(allowed=False, requires_approval=True, rule="envelope.forbidden"),
    )

    assert decision.allowed is False
    assert decision.requires_approval is False
    assert decision.matched_rule == "envelope.forbidden"


# ----------------------------------------------------------------------
# Con la Policy real: riesgo alto con el paso sin declarar aprobación
# ----------------------------------------------------------------------


def test_high_risk_step_without_declaration_pauses_with_the_real_policy():
    gate = AutonomyGate()
    step = PlanStep(
        "borrar", "borrar", "execute", RiskLevel.CRITICAL, "executor", capability="fs.remove"
    )
    decision = gate.decide(_mission(), step, PolicyEngine())

    assert decision.allowed is True
    assert decision.requires_approval is True
    assert decision.matched_rule == "risk.requires_approval"


def test_high_risk_read_only_step_also_pauses():
    gate = AutonomyGate()
    step = PlanStep(
        "investigar", "investigar", "research", RiskLevel.HIGH, "researcher", capability="fs.read"
    )
    decision = gate.decide(_mission(), step, PolicyEngine())

    assert decision.requires_approval is True


@pytest.mark.parametrize("level", [AutonomyLevel.ASSIST, AutonomyLevel.SUPERVISED, AutonomyLevel.AUTONOMOUS])
def test_policy_requirement_is_binding_in_every_autonomy_level(level):
    gate = AutonomyGate()
    mission = _mission(autonomy=level)
    step = _write_step(False, risk=RiskLevel.CRITICAL)

    decision = gate.decide(mission, step, PolicyEngine())

    assert decision.requires_approval is True


# ----------------------------------------------------------------------
# E2E: WAITING_APPROVAL y executor vacío
# ----------------------------------------------------------------------


class _Executor:
    def __init__(self):
        self.calls = []

    async def execute(self, mission, step, *, tool_name=None):
        self.calls.append(step.id)
        return ExecutionResult(success=True, output={"ok": True})

    async def __call__(self, mission, step, tool_name=None):
        return await self.execute(mission, step, tool_name=tool_name)


@pytest.mark.asyncio
async def test_policy_required_approval_pauses_the_mission_with_no_executor_call(tmp_path):
    executor = _Executor()
    runtime = AlexisRuntime(
        planner=None,
        policy=PolicyEngine(),
        executor=executor,
        verifier=FilesystemVerifier(workspace=tmp_path),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
    )
    mission = _mission()
    mission.plan = Plan(
        mission.id,
        [
            PlanStep(
                "borrar", "borrar", "execute", RiskLevel.CRITICAL, "executor", capability="fs.remove"
            )
        ],
    )

    result = await runtime.run_mission(mission)

    assert result.state is MissionState.WAITING_APPROVAL
    assert executor.calls == []
    assert result.context["pending_approval"]["step"] == "borrar"


@pytest.mark.asyncio
async def test_approved_step_then_executes(tmp_path):
    """Tras la aprobación humana, el mismo paso sí se ejecuta."""
    (tmp_path / "notas.txt").write_text("x", encoding="utf-8")
    executor = _Executor()
    runtime = AlexisRuntime(
        planner=None,
        policy=PolicyEngine(),
        executor=executor,
        verifier=FilesystemVerifier(workspace=tmp_path),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
    )
    mission = _mission()
    mission.plan = Plan(
        mission.id,
        [
            PlanStep(
                "borrar", "borrar", "execute", RiskLevel.CRITICAL, "executor", capability="fs.remove"
            )
        ],
    )
    mission.context["approved_step_ids"] = ["borrar"]

    result = await runtime.run_mission(mission)

    assert executor.calls == ["borrar"]
    assert result.state is not MissionState.WAITING_APPROVAL


# ----------------------------------------------------------------------
# Regresión: el legacy sigue igual
# ----------------------------------------------------------------------


def test_medium_risk_write_without_declaration_still_executes():
    """El cambio solo afecta a donde Policy exige aprobación (riesgo high/critical)."""
    gate = AutonomyGate()
    decision = gate.decide(_mission(), _write_step(False, risk=RiskLevel.MEDIUM), PolicyEngine())

    assert decision.allowed is True
    assert decision.requires_approval is False


def test_low_risk_read_only_still_executes_without_approval():
    gate = AutonomyGate()
    step = PlanStep("leer", "leer", "research", RiskLevel.LOW, "researcher", capability="fs.read")
    decision = gate.decide(_mission("lee notas.txt"), step, PolicyEngine())

    assert decision.allowed is True
    assert decision.requires_approval is False
