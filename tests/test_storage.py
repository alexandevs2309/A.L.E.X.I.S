import os

import pytest

from alexis.autonomy.mission import MissionEngine
from alexis.contracts import (
    AutonomyLevel,
    MissionEnvelope,
    MissionState,
    PlanStep,
    RiskLevel,
    Task,
    TaskState,
)
from alexis.security.policy import PolicyEngine
from alexis.security.policy_rules import evaluate_rules
from alexis.storage.serialization import mission_from_row, mission_to_row, task_from_row, task_to_row


def _mission():
    m = MissionEngine().create(
        "Probar storage",
        MissionEnvelope(
            "Probar storage",
            autonomy=AutonomyLevel.AUTONOMOUS,
            allowed_actions=["read", "execute"],
        ),
    )
    m.state = MissionState.RUNNING
    m.context["pending_approval"] = {"step": "x", "action": "execute"}
    m.results.append({"step": "understand", "success": True})
    return m


def test_mission_row_roundtrip():
    m = _mission()
    row = mission_to_row(m)
    assert row["state"] == "running"
    back = mission_from_row(row)
    assert back.id == m.id
    assert back.goal.objective == m.goal.objective
    assert back.envelope.autonomy is AutonomyLevel.AUTONOMOUS
    assert back.state is MissionState.RUNNING
    assert back.context == m.context
    assert back.results == m.results


def test_envelope_authority_fields_survive_the_roundtrip():
    """`capabilities`, `perimeters` y `auto_approve` son autoridad de policy.

    Si se pierden al releer, la regla `capability.outside_envelope` deja de aplicarse
    tras un reinicio: el mismo paso que estaba bloqueado volvería a permitirse.
    """
    m = MissionEngine().create(
        "Probar storage",
        MissionEnvelope(
            "Probar storage",
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=["research", "execute", "verify"],
            capabilities=["fs.read", "research.filesystem"],
            perimeters=[{"path": "workspace", "mode": "rw"}],
            auto_approve=["fs.stat"],
        ),
    )
    back = mission_from_row(mission_to_row(m))

    assert back.envelope.capabilities == ["fs.read", "research.filesystem"]
    assert back.envelope.perimeters == [{"path": "workspace", "mode": "rw"}]
    assert back.envelope.auto_approve == ["fs.stat"]


def test_capability_outside_envelope_is_still_denied_after_reload():
    m = MissionEngine().create(
        "borrar cosas",
        MissionEnvelope(
            "borrar cosas",
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=["execute"],
            capabilities=["fs.read"],
        ),
    )
    back = mission_from_row(mission_to_row(m))
    step = PlanStep("borrar", "Borrar", "execute", RiskLevel.CRITICAL, "executor", capability="fs.remove")

    verdict, reason, rule = evaluate_rules(back, step)

    assert verdict == "deny"
    assert rule == "capability.outside_envelope"
    assert "fs.remove" in reason
    assert PolicyEngine().evaluate(back, step).allowed is False


def test_self_model_sees_the_reloaded_envelope():
    from alexis.self.model import SelfModel

    m = MissionEngine().create(
        "revisar",
        MissionEnvelope(
            "revisar",
            allowed_actions=["research"],
            capabilities=["fs.read"],
            perimeters=[{"path": "workspace"}],
        ),
    )
    back = mission_from_row(mission_to_row(m))
    self_model = SelfModel(capabilities=["fs.read", "fs.remove"])
    self_model.update(back, tools=[], memory_items=[], verification=None, available=["fs.read"])

    assert self_model.active_envelope["capabilities"] == ["fs.read"]
    assert self_model.active_envelope["perimeters"] == [{"path": "workspace"}]
    assert self_model.permissions["allowed_actions"] == ["research"]


def test_task_row_roundtrip():
    t = Task(
        id="t-1",
        mission_id="m-1",
        tool="fs.read",
        args={"path": "README.md"},
        status=TaskState.RETRYING,
        attempts=2,
        max_attempts=5,
        result={"ok": True, "lines": 10},
    )
    row = task_to_row(t)
    assert row["status"] == "retrying"
    back = task_from_row(row)
    assert back.id == t.id
    assert back.mission_id == t.mission_id
    assert back.status is TaskState.RETRYING
    assert back.attempts == 2
    assert back.max_attempts == 5
    assert back.args == {"path": "README.md"}
    assert back.result == {"ok": True, "lines": 10}


pytestmark = pytest.mark.skipif(
    not os.getenv("ALEXIS_DATABASE_URL"),
    reason="Requiere ALEXIS_DATABASE_URL para integración",
)


@pytest.mark.asyncio
async def test_persist_and_recover(db, test_dsn):
    from alexis.storage.db import Database
    from alexis.storage.repositories import EventRepository, MissionRepository

    mission = _mission()
    repo = MissionRepository(db)
    await repo.upsert(mission)
    await EventRepository(db).append("test.event", {"ok": True}, mission.id)

    await db.close()

    db2 = Database(test_dsn)
    await db2.open()
    recovered = await MissionRepository(db2).get(mission.id)
    events = await EventRepository(db2).list(mission.id)
    await db2.close()

    assert recovered is not None
    assert recovered.state is MissionState.RUNNING
    assert recovered.context == mission.context
    assert len(events) == 1
    assert events[0]["topic"] == "test.event"


@pytest.mark.asyncio
async def test_reloaded_mission_keeps_its_authority_after_a_real_restart(db, test_dsn):
    """Reinicio real: la autoridad del envelope sobrevive al viaje por PostgreSQL.

    Sin preservar `capabilities`, la policy reconstruiría un envelope sin capabilities
    y dejaría pasar una capability que el usuario nunca autorizó.
    """
    from alexis.storage.db import Database
    from alexis.storage.repositories import MissionRepository

    mission = MissionEngine().create(
        "borrar notas.txt",
        MissionEnvelope(
            "borrar notas.txt",
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=["execute", "verify"],
            capabilities=["fs.read", "verification.filesystem"],
            auto_approve=[],
        ),
    )
    await MissionRepository(db).upsert(mission)
    await db.close()

    db2 = Database(test_dsn)
    await db2.open()
    recovered = await MissionRepository(db2).get(mission.id)
    await db2.close()

    assert recovered is not None
    assert recovered.envelope.capabilities == ["fs.read", "verification.filesystem"]

    step = PlanStep("borrar", "Borrar", "execute", RiskLevel.CRITICAL, "executor", capability="fs.remove")
    decision = PolicyEngine().evaluate(recovered, step)
    assert decision.allowed is False
    assert decision.matched_rule == "capability.outside_envelope"