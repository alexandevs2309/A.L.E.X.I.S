import os

import pytest

from alexis.autonomy.mission import MissionEngine
from alexis.contracts import AutonomyLevel, MissionEnvelope, MissionState, Task, TaskState
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
async def test_persist_and_recover():
    from alexis.storage.db import Database
    from alexis.storage.repositories import EventRepository, MissionRepository

    db = Database()
    await db.open()
    await db.migrate()

    mission = _mission()
    repo = MissionRepository(db)
    await repo.upsert(mission)
    await EventRepository(db).append("test.event", {"ok": True}, mission.id)

    await db.close()

    db2 = Database()
    await db2.open()
    recovered = await MissionRepository(db2).get(mission.id)
    events = await EventRepository(db2).list(mission.id)
    await db2.close()

    assert recovered is not None
    assert recovered.state is MissionState.RUNNING
    assert recovered.context == mission.context
    assert len(events) == 1
    assert events[0]["topic"] == "test.event"