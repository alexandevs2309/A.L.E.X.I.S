import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("ALEXIS_DATABASE_URL"),
    reason="Requiere ALEXIS_DATABASE_URL para integración",
)

from alexis.autonomy.mission import MissionEngine
from alexis.cognition.planner import Planner
from alexis.contracts import AutonomyLevel, MissionEnvelope, MissionState
from alexis.core.runtime import AlexisRuntime
from alexis.events.bus import EventBus
from alexis.execution import LocalExecutor
from alexis.learning.system import ExperienceLearner
from alexis.memory.store import InMemoryMemory
from alexis.security.policy import PolicyEngine
from alexis.storage.db import Database
from alexis.storage.repositories import (
    AuditRepository,
    EventRepository,
    MissionRepository,
    VerificationRepository,
)
from alexis.verification import BasicVerifier


def _runtime(db):
    return db, AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=LocalExecutor(),
        verifier=BasicVerifier(),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        mission_repo=MissionRepository(db),
        event_repo=EventRepository(db),
        audit_repo=AuditRepository(db),
        verification_repo=VerificationRepository(db),
    )


@pytest.mark.asyncio
async def test_runtime_completes_and_persists_across_restart(db, test_dsn):
    runtime = _runtime(db)[1]

    mission = MissionEngine().create(
        "Persistir y recuperar",
        MissionEnvelope(
            "Persistir y recuperar",
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=["read", "research", "modify", "test", "commit", "execute"],
        ),
    )
    await runtime.run_mission(mission)
    assert mission.state is MissionState.COMPLETED
    assert runtime.latest_verification["confidence"] == pytest.approx(0.7)

    await db.close()

    db2 = Database(test_dsn)
    await db2.open()
    recovered = await MissionRepository(db2).get(mission.id)
    assert recovered is not None
    assert recovered.state is MissionState.COMPLETED
    assert any(r.get("step") == "verify" and r.get("success") for r in recovered.results)

    verifications = await VerificationRepository(db2).list(mission.id)
    events = await EventRepository(db2).list(mission.id)
    audit = await AuditRepository(db2).list()
    await db2.close()

    assert verifications and verifications[0]["passed"] is True
    assert verifications[0]["confidence"] == pytest.approx(0.7)
    assert any(e["topic"] == "mission.completed" for e in events)
    assert any(a["event"] == "mission.completed" for a in audit)


@pytest.mark.asyncio
async def test_runtime_persists_waiting_approval(db, test_dsn):
    runtime = _runtime(db)[1]

    mission = MissionEngine().create(
        "Requerirá aprobación",
        MissionEnvelope(
            "Requerirá aprobación",
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=["read", "research"],
        ),
    )
    await runtime.run_mission(mission)
    assert mission.state is MissionState.WAITING_APPROVAL

    await db.close()

    db2 = Database(test_dsn)
    await db2.open()
    recovered = await MissionRepository(db2).get(mission.id)
    await db2.close()

    assert recovered is not None
    assert recovered.state is MissionState.WAITING_APPROVAL
    assert recovered.context["pending_approval"]["action"] == "execute"
    assert recovered.context["pending_approval"]["risk"] == "medium"