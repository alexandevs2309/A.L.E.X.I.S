"""H1 — el read-only NO es un bypass de Policy.

Antes de H1, `AutonomyGate.decide` devolvía `allowed=True` para toda acción read-only
**antes** de consultar `PolicyEngine` (`gates.py:53-58`). Eso contradecía el contrato del
propio gate ("fuera del envelope la puerta se cierra") y dejaba un agujero: una
capability de lectura fuera del envelope atravesaba la frontera.

Estos tests fijan la frontera única: Policy decide, el Gate la aplica.
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
    Observation,
    Plan,
    PlanStep,
    RiskLevel,
    Verification,
)
from alexis.core.runtime import AlexisRuntime  # noqa: E402
from alexis.events.bus import EventBus  # noqa: E402
from alexis.learning.system import ExperienceLearner  # noqa: E402
from alexis.memory.store import InMemoryMemory  # noqa: E402
from alexis.security.policy import PolicyEngine  # noqa: E402

ALL_ACTIONS = ["understand", "analyze", "research", "execute", "verify", "modify", "test", "respond"]


def _mission(objective="analiza notas.txt", **over):
    data = dict(
        objective=objective,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=list(ALL_ACTIONS),
    )
    data.update(over)
    return MissionEngine().create(objective, MissionEnvelope(**data))


def _step(step_id="investigar", action="research", capability="fs.read", risk=RiskLevel.LOW, approval=False):
    return PlanStep(step_id, f"paso {step_id}", action, risk, "executor", capability=capability, requires_approval=approval)


class _Executor:
    def __init__(self):
        self.calls = []

    async def execute(self, mission, step, *, tool_name=None):
        self.calls.append(step.id)
        return ExecutionResult(
            success=True,
            output={"ok": True, "step": step.id},
            observations=[Observation(f"tool.{step.id}", {"ok": True}, trusted=True)],
        )

    async def __call__(self, mission, step, tool_name=None):
        return await self.execute(mission, step, tool_name=tool_name)


class _PassingVerifier:
    async def verify(self, mission, plan):
        return Verification(passed=True, evidence=["ok"], confidence=0.9, notes="verificado")


def _runtime(executor):
    from alexis.cognition.planner import Planner

    return AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        gate=AutonomyGate(),
        executor=executor,
        verifier=_PassingVerifier(),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
    )


# ----------------------------------------------------------------------
# Los dos casos obligatorios
# ----------------------------------------------------------------------


def test_read_only_capability_outside_the_envelope_is_denied():
    gate = AutonomyGate()
    mission = _mission(capabilities=["fs.write", "tts.speak"])
    step = _step(capability="fs.read")

    decision = gate.decide(mission, step, PolicyEngine())

    assert decision.allowed is False
    assert decision.matched_rule == "capability.outside_envelope"
    assert "fs.read" in decision.reason


def test_read_only_capability_inside_the_envelope_is_allowed():
    gate = AutonomyGate()
    mission = _mission(capabilities=["fs.read", "research.filesystem"])
    step = _step(capability="fs.read")

    decision = gate.decide(mission, step, PolicyEngine())

    assert decision.allowed is True
    assert decision.requires_approval is False
    assert decision.matched_rule == "allow.within_envelope"


def test_read_only_action_outside_allowed_actions_is_denied():
    gate = AutonomyGate()
    mission = _mission(allowed_actions=["respond"])
    step = _step(action="research", capability="fs.read")

    decision = gate.decide(mission, step, PolicyEngine())

    assert decision.allowed is False
    assert decision.matched_rule == "action.outside_envelope"


def test_read_only_forbidden_action_is_denied():
    gate = AutonomyGate()
    mission = _mission(forbidden_actions=["research"])
    step = _step(action="research", capability="fs.read")

    decision = gate.decide(mission, step, PolicyEngine())

    assert decision.allowed is False
    assert decision.matched_rule == "envelope.forbidden"


@pytest.mark.asyncio
async def test_read_only_outside_envelope_never_reaches_the_executor():
    """El caso obligatorio completo: DENY y executor.calls == []."""
    executor = _Executor()
    runtime = _runtime(executor)
    mission = _mission(capabilities=["tts.speak"])
    mission.plan = Plan(mission.id, [_step(capability="fs.read")])

    result = await runtime.run_mission(mission)

    assert result.state is MissionState.BLOCKED
    assert executor.calls == []
    assert result.context["blocked_reason"]
    assert "fs.read" in result.context["blocked_reason"]


# ----------------------------------------------------------------------
# Lo que NO cambia: la lectura no compra saltarse una aprobación
# ----------------------------------------------------------------------


def test_read_only_low_risk_does_not_ask_for_approval():
    gate = AutonomyGate()
    mission = _mission(capabilities=["fs.read"])
    decision = gate.decide(mission, _step(), PolicyEngine())
    assert decision.allowed is True
    assert decision.requires_approval is False


def test_read_only_step_that_explicitly_requires_approval_pauses():
    gate = AutonomyGate()
    mission = _mission(capabilities=["fs.read"])
    step = _step(action="research", capability="fs.read", approval=True)

    decision = gate.decide(mission, step, PolicyEngine())

    assert decision.allowed is True
    assert decision.requires_approval is True


def test_read_only_high_risk_pauses_instead_of_being_auto_allowed():
    gate = AutonomyGate()
    mission = _mission(capabilities=["fs.read"])
    step = _step(action="research", capability="fs.read", risk=RiskLevel.HIGH)

    decision = gate.decide(mission, step, PolicyEngine())

    assert decision.allowed is True
    assert decision.requires_approval is True
    assert decision.matched_rule == "risk.requires_approval"


# ----------------------------------------------------------------------
# Regresión: el camino normal sigue funcionando
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_read_write_mission_still_completes():
    executor = _Executor()
    runtime = _runtime(executor)
    from alexis.capabilities import build_catalog

    enabled = [s.id for s in build_catalog().enabled()]
    mission = _mission(capabilities=enabled)
    from alexis.cognition.planner import Planner

    mission.plan = await Planner().create_plan(mission)

    result = await runtime.run_mission(mission)

    assert result.state is MissionState.COMPLETED
    assert executor.calls == [s.id for s in mission.plan.steps]


@pytest.mark.asyncio
async def test_assist_still_lets_reads_through_and_blocks_writes():
    gate = AutonomyGate()
    mission = _mission(autonomy=AutonomyLevel.ASSIST, capabilities=["fs.read", "fs.write"])

    read = gate.decide(mission, _step(action="research", capability="fs.read"), PolicyEngine())
    write = gate.decide(
        mission,
        _step(step_id="escribir", action="execute", capability="fs.write", risk=RiskLevel.MEDIUM),
        PolicyEngine(),
    )

    assert read.allowed is True and read.requires_approval is False
    assert write.allowed is True and write.requires_approval is True


def test_gate_reason_mentions_the_policy_verdict():
    gate = AutonomyGate()
    mission = _mission(capabilities=["fs.read"])
    decision = gate.decide(mission, _step(), PolicyEngine())
    assert "permitido dentro del envelope" in decision.reason
