import asyncio
import os
import pathlib
import sys
import tempfile

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.gates import AutonomyGate  # noqa: E402
from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.autonomy.queue import MissionWorker  # noqa: E402
from alexis.cognition.planner import Planner  # noqa: E402
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    MissionEnvelope,
    MissionState,
    PlanStep,
    RiskLevel,
)
from alexis.security.policy import PolicyEngine  # noqa: E402

NEED_DB = pytest.mark.skipif(
    not os.getenv("ALEXIS_DATABASE_URL"),
    reason="Requiere ALEXIS_DATABASE_URL para integración",
)


def _envelope(objective, autonomy=AutonomyLevel.SUPERVISED, **over):
    data = dict(
        objective=objective,
        autonomy=autonomy,
        allowed_actions=["read", "research", "execute", "verify"],
    )
    data.update(over)
    return MissionEnvelope(**data)


def _mission(objective, autonomy=AutonomyLevel.SUPERVISED, **over):
    return MissionEngine().create(objective, _envelope(objective, autonomy, **over))


class _FakeRepo:
    def __init__(self, missions):
        self.missions = list(missions)

    async def next_pending(self):
        return self.missions.pop(0) if self.missions else None


# ----------------------------------------------------------------------
# Puertas de autonomía (unidad, sin DB ni runtime)
# ----------------------------------------------------------------------


def test_gate_supervised_delicate_asks_approval():
    gate = AutonomyGate()
    mission = _mission("borra el archivo x.txt")
    step = PlanStep("execute", "Borrar", "execute", RiskLevel.MEDIUM, "executor", requires_approval=True)
    decision = gate.decide(mission, step, PolicyEngine())
    assert decision.allowed is True
    assert decision.requires_approval is True
    assert "aprobación" in decision.reason


def test_gate_supervised_permits_allowed_effects():
    gate = AutonomyGate()
    mission = _mission("crea el archivo prueba.txt")
    step = PlanStep("execute", "Crear", "execute", RiskLevel.MEDIUM, "executor", requires_approval=False)
    decision = gate.decide(mission, step, PolicyEngine())
    assert decision.allowed is True
    assert decision.requires_approval is False


def test_gate_autonomous_destroys_alone_within_envelope():
    gate = AutonomyGate()
    mission = _mission(
        "borra el archivo x.txt",
        autonomy=AutonomyLevel.AUTONOMOUS,
        approval_required=[],
    )
    step = PlanStep("execute", "Borrar", "execute", RiskLevel.MEDIUM, "executor", requires_approval=True)
    decision = gate.decide(mission, step, PolicyEngine())
    assert decision.allowed is True
    assert decision.requires_approval is False
    assert "autonomous" in decision.reason


def test_gate_autonomous_respects_approval_required():
    gate = AutonomyGate()
    mission = _mission(
        "borra el archivo x.txt",
        autonomy=AutonomyLevel.AUTONOMOUS,
        approval_required=["destructive"],
    )
    step = PlanStep("execute", "Borrar", "execute", RiskLevel.MEDIUM, "executor", requires_approval=True)
    decision = gate.decide(mission, step, PolicyEngine())
    assert decision.allowed is True
    assert decision.requires_approval is True
    assert "approval_required" in decision.reason


def test_gate_blocks_outside_envelope():
    gate = AutonomyGate()
    mission = _mission("borra el archivo x.txt", forbidden_actions=["execute"])
    step = PlanStep("execute", "Borrar", "execute", RiskLevel.MEDIUM, "executor", requires_approval=False)
    decision = gate.decide(mission, step, PolicyEngine())
    assert decision.allowed is False
    assert "no ejecuto" in decision.reason


def test_gate_assist_proposes_effects():
    gate = AutonomyGate()
    mission = _mission("borra el archivo x.txt", autonomy=AutonomyLevel.ASSIST)
    step = PlanStep("execute", "Borrar", "execute", RiskLevel.MEDIUM, "executor", requires_approval=False)
    decision = gate.decide(mission, step, PolicyEngine())
    assert decision.allowed is True
    assert decision.requires_approval is True
    assert "assist" in decision.reason
    assert "propongo" in decision.reason


def test_gate_read_only_never_asks():
    gate = AutonomyGate()
    mission = _mission("leeme el archivo x.txt")
    step = PlanStep("understand", "Entender", "analyze", RiskLevel.LOW, "reasoner")
    decision = gate.decide(mission, step, PolicyEngine())
    assert decision.allowed is True
    assert decision.requires_approval is False


# ----------------------------------------------------------------------
# Cola de misiones (worker FIFO serial)
# ----------------------------------------------------------------------


async def test_worker_runs_fifo_serially():
    m1, m2, m3 = _mission("a"), _mission("b"), _mission("c")
    worker = MissionWorker(runner=lambda m: asyncio.sleep(0), mission_repo=_FakeRepo([m1, m2, m3]), poll_seconds=0.01)
    ran = []
    current = 0

    async def runner(m):
        nonlocal current
        current += 1
        assert current == 1, "el worker debe correr una misión a la vez"
        ran.append(m.id)
        await asyncio.sleep(0.02)
        current -= 1

    worker.runner = runner
    task = asyncio.create_task(worker.loop())
    for _ in range(300):
        if len(ran) == 3:
            break
        await asyncio.sleep(0.01)
    worker.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert ran == [m1.id, m2.id, m3.id]


# ----------------------------------------------------------------------
# Integración (PostgreSQL) — nivel AUTONOMOUS dentro del envelope
# ----------------------------------------------------------------------


def _build_with_gate(gate, db):
    from alexis.autonomy.task_runner import TaskRunner
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
    from alexis.verification import FilesystemVerifier

    ws = pathlib.Path(tempfile.mkdtemp())
    tools = ToolRegistry()
    tools.register_all(build_filesystem_tools(ws))
    sandbox = SandboxRunner(ws)
    runtime = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=SandboxExecutor(tools, sandbox),
        verifier=FilesystemVerifier(ws),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        mission_repo=MissionRepository(db),
        event_repo=EventRepository(db),
        audit_repo=AuditRepository(db),
        verification_repo=VerificationRepository(db),
        observation_repo=None,
        gate=gate,
    )
    runner = TaskRunner(
        task_repo=TaskRepository(db),
        execution_repo=ExecutionRepository(db),
        checkpoint_repo=CheckpointRepository(db),
        event_bus=runtime.events,
    )
    runtime.task_runner = runner
    return runtime, runner, ws


@NEED_DB
async def test_autonomous_destructive_runs_within_envelope_alone(db):
    runtime, runner, ws = _build_with_gate(AutonomyGate(), db)
    await db.open()
    await db.migrate()

    target = ws / "secret.txt"
    target.write_text("borrame", encoding="utf-8")

    mission = _mission(
        "borra el archivo secret.txt",
        autonomy=AutonomyLevel.AUTONOMOUS,
        approval_required=[],
    )
    await runtime.run_mission(mission)

    assert mission.state is MissionState.COMPLETED
    assert not target.exists()
    assert mission.context.get("decisions", {}).get("execute")
    assert "evaluations" in mission.context

    await db.close()


@NEED_DB
async def test_autonomous_asks_human_when_destructive_in_approval_required(db):
    runtime, runner, ws = _build_with_gate(AutonomyGate(), db)
    await db.open()
    await db.migrate()

    target = ws / "secret.txt"
    target.write_text("borrame", encoding="utf-8")

    mission = _mission(
        "borra el archivo secret.txt",
        autonomy=AutonomyLevel.AUTONOMOUS,
        approval_required=["destructive", "production", "external_communication"],
    )
    await runtime.run_mission(mission)

    assert mission.state is MissionState.WAITING_APPROVAL
    pending = mission.context.get("pending_approval") or {}
    assert pending.get("step") == "execute"
    assert target.exists()

    mission.context["approved_step_ids"] = ["execute"]
    mission.context.pop("pending_approval", None)
    mission.results.clear()
    await runtime.mission_repo.upsert(mission)
    await runtime.run_mission(mission)

    assert mission.state is MissionState.COMPLETED
    assert not target.exists()
    assert mission.context.get("resumed_at_step") == 2

    await db.close()


@NEED_DB
async def test_plan_persisted_so_it_is_not_replanned_on_restart(db):
    runtime, runner, ws = _build_with_gate(None, db)
    await db.open()
    await db.migrate()
    (ws / "reporte.txt").write_text("informe\n", encoding="utf-8")

    mission = _mission("leeme el archivo reporte.txt")
    await runtime.run_mission(mission)
    assert mission.state is MissionState.COMPLETED
    assert mission.context.get("plan_steps")

    from alexis.cognition.planner import Planner

    class SpyPlanner(Planner):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def create_plan(self, mission):
            self.calls += 1
            return await super().create_plan(mission)

    spy = SpyPlanner()
    reloaded = await runtime.mission_repo.get(mission.id)
    assert reloaded is not None
    reloaded.context.pop("resumed_at_step", None)
    runtime.planner = spy
    await runtime.run_mission(reloaded)

    assert spy.calls == 0, "el plan debe restaurarse desde context['plan_steps'] sin replanificar"
    assert reloaded.state is MissionState.COMPLETED

    await db.close()