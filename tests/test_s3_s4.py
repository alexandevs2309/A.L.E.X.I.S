import asyncio
import pathlib
import sys
import tempfile
import time

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

pytestmark = pytest.mark.skipif(
    not __import__("os").getenv("ALEXIS_DATABASE_URL"),
    reason="Requiere ALEXIS_DATABASE_URL para integración",
)

from alexis.autonomy.mission import MissionEngine
from alexis.autonomy.scheduler import Scheduler
from alexis.autonomy.task_runner import TaskRunner
from alexis.cognition.planner import Planner
from alexis.contracts import (
    AutonomyLevel,
    MissionEnvelope,
    MissionState,
    Task,
    TaskState,
)
from alexis.core.runtime import AlexisRuntime
from alexis.events.bus import EventBus
from alexis.execution import SandboxExecutor
from alexis.learning.system import ExperienceLearner
from alexis.memory.store import InMemoryMemory
from alexis.security.policy import PolicyEngine
from alexis.security.sandbox import SandboxRunner
from alexis.storage.repositories import (
    AuditRepository,
    CheckpointRepository,
    EventRepository,
    ExecutionRepository,
    MissionRepository,
    TaskRepository,
    VerificationRepository,
)
from alexis.tools.filesystem import build_filesystem_tools
from alexis.tools.registry import ToolRegistry
from alexis.cognition.goal_verification import GoalVerifier
from alexis.verification import FilesystemVerifier
from alexis.world.model import WorldModel


def _build(db):
    ws = pathlib.Path(tempfile.mkdtemp())
    (ws / "reporte.txt").write_text("informe de prueba\nlinea 2\n", encoding="utf-8")
    tools = ToolRegistry()
    tools.register_all(build_filesystem_tools(ws))
    sandbox = SandboxRunner(ws)
    world = WorldModel()
    runtime = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=SandboxExecutor(tools, sandbox),
        verifier=FilesystemVerifier(ws),
        world=world,
        goal_verifier=GoalVerifier(world=world),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        mission_repo=MissionRepository(db),
        event_repo=EventRepository(db),
        audit_repo=AuditRepository(db),
        verification_repo=VerificationRepository(db),
        observation_repo=None,
    )
    runner = TaskRunner(
        task_repo=TaskRepository(db),
        execution_repo=ExecutionRepository(db),
        checkpoint_repo=CheckpointRepository(db),
        event_bus=runtime.events,
    )
    runtime.task_runner = runner
    return runtime, runner, ws


@pytest.mark.asyncio
async def test_read_mission_real_tasks_and_completion(db):
    runtime, runner, ws = _build(db)
    await db.open()
    await db.migrate()

    mission = MissionEngine().create(
        "leeme el archivo reporte.txt",
        MissionEnvelope(
            "leeme el archivo reporte.txt",
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=["read", "research", "execute", "verify"],
        ),
        # P0 §5.5: el objetivo se demuestra con el hecho observado, no con los pasos ok.
        success_criteria=["El archivo file_exists:reporte.txt está escrito"],
    )
    await runtime.run_mission(mission)
    assert mission.state is MissionState.COMPLETED
    assert all(r["success"] for r in mission.results)

    tasks = await runner.task_repo.list(mission_id=mission.id)
    assert len(tasks) == 4
    assert all(t.status is TaskState.COMPLETED for t in tasks)

    executions = await runner.execution_repo.list(tasks[0].id)
    assert executions and executions[0]["ok"] is True

    checkpoint = await runner.checkpoint_repo.latest(mission.id)
    assert checkpoint is not None and checkpoint["step_index"] == 3

    verification = await VerificationRepository(db).list(mission.id)
    assert verification and verification[0]["passed"] is True

    await db.close()


@pytest.mark.asyncio
async def test_write_intent_runs_auto_and_creates(db):
    runtime, runner, ws = _build(db)
    await db.open()
    await db.migrate()

    mission = MissionEngine().create(
        "crea un archivo nuevo.txt",
        MissionEnvelope(
            "crea un archivo nuevo.txt",
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=["read", "research", "execute", "verify"],
        ),
        # P0 §5.5: el objetivo se demuestra con el hecho observado, no con los pasos ok.
        success_criteria=["El archivo file_exists:nuevo.txt está escrito"],
    )
    # Crear/editar NO es delicado: corre automáticamente, sin aprobación.
    await runtime.run_mission(mission)
    assert mission.state is MissionState.COMPLETED
    assert "pending_approval" not in mission.context
    assert (ws / "nuevo.txt").exists()
    assert "ALEXIS" in (ws / "nuevo.txt").read_text(encoding="utf-8")

    tasks = await runner.task_repo.list(mission_id=mission.id)
    steps = [t.args.get("step") for t in tasks]
    assert steps == ["understand", "research", "execute", "verify"]
    assert len(mission.results) == 4

    verification = await VerificationRepository(db).list(mission.id)
    assert verification and verification[-1]["passed"] is True

    await db.close()


@pytest.mark.asyncio
async def test_destructive_intent_requires_approval_then_removes(db):
    runtime, runner, ws = _build(db)
    await db.open()
    await db.migrate()

    target = ws / "para-borrar.txt"
    target.write_text("contenido que se borrará", encoding="utf-8")

    mission = MissionEngine().create(
        "borra el archivo para-borrar.txt",
        MissionEnvelope(
            "borra el archivo para-borrar.txt",
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=["read", "research", "execute", "verify"],
        ),
        # El objetivo de un borrado se demuestra viendo que el archivo ya no está.
        success_criteria=["El archivo file_missing:para-borrar.txt ya no está"],
    )
    # Borrar es DELICADO → pide aprobación humana.
    await runtime.run_mission(mission)
    assert mission.state is MissionState.WAITING_APPROVAL
    pending = mission.context.get("pending_approval") or {}
    assert pending.get("step") == "execute"
    assert target.exists()

    # El humano aprueba y la misión reanuda desde el checkpoint.
    mission.context["approved_step_ids"] = ["execute"]
    mission.context.pop("pending_approval", None)
    mission.results.clear()
    await runtime.mission_repo.upsert(mission)
    await runtime.run_mission(mission)

    assert mission.state is MissionState.COMPLETED
    assert not target.exists()
    assert mission.context.get("resumed_at_step") == 2

    verification = await VerificationRepository(db).list(mission.id)
    assert verification and verification[-1]["passed"] is True
    assert "ya no existe" in (verification[-1]["notes"] or "")

    await db.close()


@pytest.mark.asyncio
async def test_unsupported_intent_fails_honest(db):
    runtime, runner, ws = _build(db)
    await db.open()
    await db.migrate()

    mission = MissionEngine().create(
        "renombra el archivo reporte.txt a nuevo.txt",
        MissionEnvelope(
            "renombra el archivo reporte.txt a nuevo.txt",
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=["read", "research", "execute", "verify"],
        ),
    )
    await runtime.run_mission(mission)
    assert mission.state is MissionState.FAILED
    assert mission.results[-1]["step"] == "execute" and mission.results[-1]["success"] is False

    tasks = await runner.task_repo.list(mission_id=mission.id)
    execute_task = [t for t in tasks if t.args.get("step") == "execute"]
    assert execute_task and execute_task[0].status is TaskState.FAILED
    assert "no está soportado" in (execute_task[0].error or "")

    await db.close()


@pytest.mark.asyncio
async def test_read_intent_runs_without_approval(db):
    runtime, runner, ws = _build(db)
    await db.open()
    await db.migrate()

    mission = MissionEngine().create(
        "leeme el archivo reporte.txt",
        MissionEnvelope(
            "leeme el archivo reporte.txt",
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=["read", "research", "execute", "verify"],
        ),
        # P0 §5.5: el objetivo se demuestra con el hecho observado, no con los pasos ok.
        success_criteria=["El archivo file_exists:reporte.txt está escrito"],
    )
    await runtime.run_mission(mission)
    # Solo lectura → corre automáticamente, sin pausa de aprobación.
    assert mission.state is MissionState.COMPLETED
    assert "pending_approval" not in mission.context

    await db.close()


@pytest.mark.asyncio
async def test_checkpoint_resumes_not_restarts(db):
    runtime, runner, ws = _build(db)
    await db.open()
    await db.migrate()

    mission = MissionEngine().create(
        "leeme el archivo reporte.txt",
        MissionEnvelope(
            "leeme el archivo reporte.txt",
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=["read", "research", "execute", "verify"],
        ),
        # P0 §5.5: el objetivo se demuestra con el hecho observado, no con los pasos ok.
        success_criteria=["El archivo file_exists:reporte.txt está escrito"],
    )
    # simular un checkpoint previo en el paso 2 (solo understand+research hechos)
    mission.results = [{"step": "understand", "success": True, "task": "understand"}, {"step": "research", "success": True, "task": "research"}]
    await runtime.mission_repo.upsert(mission)
    await runner.save_checkpoint(mission, 1)

    await runtime.run_mission(mission)
    assert mission.state is MissionState.COMPLETED
    assert mission.context.get("resumed_at_step") == 2
    # solo los pasos >= 2 crearon tareas nuevas
    tasks = await runner.task_repo.list(mission_id=mission.id)
    created_ids = {t.args.get("step") for t in tasks}
    assert "execute" in created_ids and "verify" in created_ids
    assert len(mission.results) == 4

    await db.close()


@pytest.mark.asyncio
async def test_scheduler_recovers_stale_leases(db):
    runtime, runner, ws = _build(db)
    await db.open()
    await db.migrate()

    mission = MissionEngine().create(
        "leeme el archivo reporte.txt",
        MissionEnvelope(
            "leeme el archivo reporte.txt",
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=["read"],
        ),
    )
    await runtime.mission_repo.upsert(mission)

    stale = Task(id="t-stale", mission_id=mission.id, status=TaskState.RUNNING, lease_until=time.time() - 10)
    expired = Task(id="t-expired", mission_id=mission.id, status=TaskState.QUEUED, deadline=time.time() - 5)
    await runner.task_repo.upsert(stale)
    await runner.task_repo.upsert(expired)

    scheduler = Scheduler(runner.task_repo)
    reclaimed = await scheduler.recover_stale(lease_seconds=60)
    assert reclaimed >= 2

    after_stale = await runner.task_repo.get("t-stale")
    after_expired = await runner.task_repo.get("t-expired")
    assert after_stale.status is TaskState.QUEUED
    assert after_expired.status is TaskState.FAILED
    assert "deadline" in (after_expired.error or "")

    await db.close()