import asyncio
import sys

from alexis.contracts import (
    AutonomyLevel,
    Checkpoint,
    Execution,
    MissionEnvelope,
    MissionState,
    Task,
    TaskState,
)
from alexis.autonomy.mission import MissionEngine
from alexis.storage.db import Database
from alexis.storage.repositories import (
    AuditRepository,
    CheckpointRepository,
    EventRepository,
    ExecutionRepository,
    MissionRepository,
    ObservationRepository,
    TaskRepository,
    VerificationRepository,
)


async def migrate():
    db = Database()
    await db.open()
    await db.migrate()
    await db.close()
    print("Esquema aplicado (missions, mission_events, audit_log, tasks, executions, observations, verifications, checkpoints, extension vector).")


async def verify():
    db = Database()
    await db.open()
    await db.migrate()

    mission = MissionEngine().create(
        "Verificación: crear, persistir, recuperar",
        MissionEnvelope(
            "Verificación: crear, persistir, recuperar",
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=["read", "research", "execute"],
        ),
    )
    mission.state = MissionState.RUNNING

    missions = MissionRepository(db)
    events = EventRepository(db)
    audit = AuditRepository(db)
    tasks = TaskRepository(db)
    executions = ExecutionRepository(db)
    observations = ObservationRepository(db)
    verifications = VerificationRepository(db)
    checkpoints = CheckpointRepository(db)

    await missions.upsert(mission)
    await events.append("mission.created", {"mission_id": mission.id}, mission.id)

    task = Task(
        id="t-1",
        mission_id=mission.id,
        kind="task",
        agent="coder",
        tool="fs.read",
        args={"path": "README.md"},
        status=TaskState.QUEUED,
    )
    await tasks.upsert(task)
    await tasks.upsert(
        Task(
            id="t-1",
            mission_id=mission.id,
            tool="fs.read",
            args={"path": "README.md"},
            status=TaskState.COMPLETED,
            result={"ok": True, "lines": 10},
        )
    )
    await executions.insert(
        Execution(
            task_id="t-1",
            tool="fs.read",
            args_hash="h1",
            ok=True,
            output={"ok": True, "lines": 10},
        )
    )
    await observations.insert(mission.id, "tool:fs.read", {"ok": True, "lines": 10}, trusted=False)
    await verifications.insert(mission.id, passed=True, confidence=0.9, verifier="fs_verifier", evidence=["path ok"])
    await checkpoints.save(Checkpoint(mission_id=mission.id, step_index=1, payload={"phase": "execute"}))

    mission.state = MissionState.COMPLETED
    mission.results.append({"task": task.id, "success": True})
    await missions.upsert(mission)
    await audit.record("mission.completed", "verify_cli", mission.id, mission=mission.id)

    await db.close()

    recovered_db = Database()
    await recovered_db.open()
    recovered = await MissionRepository(recovered_db).get(mission.id)
    stored_events = await EventRepository(recovered_db).list(mission.id)
    stored_audit = await AuditRepository(recovered_db).list()
    task_recovered = await TaskRepository(recovered_db).get(task.id)
    claimable = await TaskRepository(recovered_db).next_claimable(mission.id)
    exec_rows = await ExecutionRepository(recovered_db).list(task.id)
    obs_rows = await ObservationRepository(recovered_db).list(mission.id)
    ver_rows = await VerificationRepository(recovered_db).list(mission.id)
    checkpoint = await CheckpointRepository(recovered_db).latest(mission.id)

    assert recovered is not None, "la misión no se recuperó"
    assert recovered.id == mission.id
    assert recovered.state is MissionState.COMPLETED
    assert recovered.results == mission.results
    assert task_recovered is not None and task_recovered.status is TaskState.COMPLETED
    assert task_recovered.result == {"ok": True, "lines": 10}
    assert claimable is None, "no debería haber tareas reclamables tras completar"
    assert len(exec_rows) == 1 and exec_rows[0]["ok"] is True
    assert len(obs_rows) == 1
    assert len(ver_rows) == 1 and ver_rows[0]["passed"] is True and ver_rows[0]["verifier"] == "fs_verifier"
    assert checkpoint is not None and checkpoint["step_index"] == 1
    assert len(stored_events) >= 1
    assert stored_audit and stored_audit[0]["event"] == "mission.completed"

    await recovered_db.close()

    print(f"OK: misión recuperada tras reinicio -> {recovered.id}")
    print(f"OK: task {task_recovered.id} completa con result persistido; sin tareas reclamables.")
    print(f"OK: 1 execution, {len(obs_rows)} observación(es), {len(ver_rows)} verificación(es), checkpoint en paso {checkpoint['step_index']}.")
    print(f"OK: {len(stored_events)} evento(s) y {len(stored_audit)} registro(s) de auditoría persistidos.")


async def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "migrate"
    if command == "migrate":
        await migrate()
    elif command == "verify":
        await verify()
    else:
        print(f"Comando desconocido: {command}")
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())