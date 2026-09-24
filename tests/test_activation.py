"""FASE 3: la palmada es un EVENTO, no el cerebro.

Verifica que ClapDetected → Event Bus → misión de activación atraviesen el
mismo pipeline que el chat (planner → executor → verifier), sin que el clap
ejecute ninguna acción por su cuenta.
"""

import asyncio

import pytest

from alexis.contracts import Mission
from alexis.cognition.planner import Planner
from alexis.events.bus import EventBus
from alexis.execution import SandboxExecutor
from alexis.perception.activation import (
    ACTIVATION_MARKER,
    ACTIVATION_OBJECTIVE,
    WELCOME_REPLY,
    is_activation_objective,
)
from alexis.perception.clap_detector import ClapDetector, ClapDetected
from alexis.verification import FilesystemVerifier
from alexis.security.sandbox import SandboxRunner
from alexis.tools.registry import ToolRegistry


def _activation_mission() -> Mission:
    from alexis.autonomy.mission import MissionEngine
    from alexis.contracts import AutonomyLevel, MissionEnvelope

    envelope = MissionEnvelope(
        objective=ACTIVATION_OBJECTIVE,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=["understand", "analyze", "research", "execute", "verify", "respond"],
    )
    return MissionEngine().create(ACTIVATION_OBJECTIVE, envelope)


class TestActivationSemantics:
    def test_marker_not_triggered_by_user_text(self):
        assert not is_activation_objective("explica qué es la activación de un dispositivo")
        assert is_activation_objective(ACTIVATION_OBJECTIVE)

    def test_welcome_reply_is_explicit(self):
        assert "¿En qué te ayudo?" in WELCOME_REPLY


class TestPlannerWakePlan:
    async def test_activation_plan_has_respond_step(self):
        plan = await Planner().create_plan(_activation_mission())
        actions = [s.action for s in plan.steps]
        assert actions == ["analyze", "respond"]
        assert all(s.requires_approval is False for s in plan.steps)
        assert all(s.risk.value == "low" for s in plan.steps)

    async def test_normal_mission_unaffected(self):
        from alexis.autonomy.mission import MissionEngine
        from alexis.contracts import AutonomyLevel, MissionEnvelope

        envelope = MissionEnvelope(objective="leeme el archivo README.txt", autonomy=AutonomyLevel.SUPERVISED)
        mission = MissionEngine().create("leeme el archivo README.txt", envelope)
        plan = await Planner().create_plan(mission)
        assert [s.action for s in plan.steps] == ["analyze", "research", "execute", "verify"]


class TestExecutorRespond:
    async def test_respond_returns_assistant_message(self, tmp_path):
        runner = SandboxRunner(workspace=tmp_path)
        tools = ToolRegistry()
        executor = SandboxExecutor(tools=tools, sandbox=runner)
        mission = _activation_mission()
        from alexis.contracts import PlanStep, RiskLevel

        step = PlanStep("respond", "greet", "respond", RiskLevel.LOW)
        result = await executor.execute(mission, step)
        assert result.success is True
        assert result.output["message"] == WELCOME_REPLY


class TestClapIsAnEventNotActions:
    async def test_clap_event_published_on_bus(self):
        bus = EventBus()
        sub = bus.subscribe_async()
        detector = ClapDetector()
        detector.feed_rms(0.5, now=0.0)
        detector.feed_rms(0.001, now=0.05)
        event = detector.feed_rms(0.9, now=0.20)
        assert event is not None
        assert isinstance(event, ClapDetected)

        await bus.publish("perception.clap_detected", event)
        item = await asyncio.wait_for(sub.get(), 1.0)
        assert item["topic"] == "perception.clap_detected"
        assert item["payload"].source == "microphone"
        assert item["payload"].confidence > 0.0

    def test_clap_listener_only_produces_events(self):
        from alexis.perception.clap_listener import ClapListener

        listener = ClapListener()
        now = 0.0
        assert listener.ingest_rms(0.001, now=now) is None
        assert listener.ingest_rms(0.5, now=now + 0.05) is None
        assert listener.ingest_rms(0.001, now=now + 0.10) is None
        event = listener.ingest_rms(0.9, now=now + 0.20)
        assert event is not None
        assert event.source == "microphone"


class TestVerifierActivation:
    async def test_activation_mission_verifies_without_workspace(self, tmp_path):
        verifier = FilesystemVerifier(workspace=tmp_path)
        mission = _activation_mission()
        plan = await Planner().create_plan(mission)
        verification = await verifier.verify(mission, plan)
        assert verification.passed is True
        assert verification.confidence == pytest.approx(0.9)
        assert "no escribió nada" in verification.notes