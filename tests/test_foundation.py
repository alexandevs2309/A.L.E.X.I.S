import pytest
from alexis.contracts import MissionEnvelope, AutonomyLevel
from alexis.autonomy.mission import MissionEngine
from alexis.security.policy import PolicyEngine


def test_mission_creation():
    m = MissionEngine().create(
        "Analyze project",
        MissionEnvelope("Analyze project", AutonomyLevel.SUPERVISED)
    )
    assert m.goal.objective == "Analyze project"


def test_policy_allows_analyze():
    m = MissionEngine().create(
        "Analyze",
        MissionEnvelope("Analyze", allowed_actions=["read"])
    )
    step = type("Step", (), {"action": "analyze", "risk": type("Risk", (), {})()})()
    step.risk = __import__("alexis.contracts", fromlist=["RiskLevel"]).RiskLevel.LOW
    assert PolicyEngine().authorize(m, step).allowed
