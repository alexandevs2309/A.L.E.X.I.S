"""H2 — `PlanStep.args` llega al Executor y es la fuente de verdad.

Antes de H2, `SandboxExecutor` ignoraba `step.args` y re-derivaba el path desde el
texto del objetivo (`extract_workspace_path`), con dos fuentes de verdad para la misma
acción. Ahora: lo que el validador autorizó es exactamente lo que la tool recibe.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.gates import AutonomyGate  # noqa: E402
from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.cognition.planner_model import PlanValidator  # noqa: E402
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
from alexis.execution import SandboxExecutor  # noqa: E402
from alexis.learning.system import ExperienceLearner  # noqa: E402
from alexis.memory.store import InMemoryMemory  # noqa: E402
from alexis.security.policy import PolicyEngine  # noqa: E402
from alexis.security.sandbox import SandboxRunner  # noqa: E402
from alexis.tools.filesystem import build_filesystem_tools  # noqa: E402
from alexis.tools.registry import Tool, ToolRegistry  # noqa: E402
from alexis.verification import FilesystemVerifier  # noqa: E402

ALL_ACTIONS = ["understand", "analyze", "research", "execute", "verify", "modify", "test", "respond"]


def _mission(objective, **over):
    data = dict(objective=objective, autonomy=AutonomyLevel.SUPERVISED, allowed_actions=list(ALL_ACTIONS))
    data.update(over)
    return MissionEngine().create(objective, MissionEnvelope(**data))


class _Spy:
    """Tool que registra exactamente los argumentos que recibe."""

    def __init__(self, name, ok=True):
        self.name = name
        self.ok = ok
        self.received = []

    def tool(self):
        async def handler(args):
            self.received.append(dict(args))
            return {"ok": self.ok, "path": args.get("path"), "spy": self.name}

        return Tool(
            name=self.name,
            description="spy",
            schema={"type": "object", "properties": {}},
            permissions=[],
            handler=handler,
        )


def _executor(tmp_path, spy):
    registry = ToolRegistry()
    registry.register(spy.tool())
    return SandboxExecutor(tools=registry, sandbox=SandboxRunner(workspace=tmp_path))


def _step(step_id="leer", action="execute", capability="fs.read", args=None, risk=RiskLevel.MEDIUM):
    return PlanStep(
        step_id,
        f"paso {step_id}",
        action,
        risk,
        "executor",
        capability=capability,
        args=dict(args or {}),
    )


# ----------------------------------------------------------------------
# H2.1 — los args llegan al executor
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_args_reach_the_tool_exactly(tmp_path):
    spy = _Spy("fs.read")
    executor = _executor(tmp_path, spy)
    mission = _mission("lee lo que sea")
    step = _step(args={"path": "notas.txt", "max_bytes": 2048})

    result = await executor.execute(mission, step, tool_name="fs.read")

    assert result.success is True
    assert spy.received == [{"path": "notas.txt", "max_bytes": 2048}]


@pytest.mark.asyncio
async def test_plan_args_are_not_merged_with_derived_ones(tmp_path):
    """No hay fusión: lo que se entrega al tool es exactamente `PlanStep.args`."""
    spy = _Spy("fs.read")
    executor = _executor(tmp_path, spy)
    mission = _mission("lee notas.txt")
    step = _step(args={"path": "otro.txt"})

    await executor.execute(mission, step, tool_name="fs.read")

    assert spy.received[0]["path"] == "otro.txt"
    assert "notas.txt" not in str(spy.received[0])


# ----------------------------------------------------------------------
# H2.2 — no se reconstruyen desde el objetivo
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_args_win_over_the_path_mentioned_in_the_objective(tmp_path):
    """El objetivo sugiere `b.txt`; el paso dice `a.txt`. Gana el paso."""
    spy = _Spy("fs.read")
    executor = _executor(tmp_path, spy)
    mission = _mission("lee el archivo b.txt del workspace")
    step = _step(args={"path": "a.txt"})

    await executor.execute(mission, step, tool_name="fs.read")

    assert spy.received[0]["path"] == "a.txt"
    assert spy.received[0]["path"] != "b.txt"


@pytest.mark.asyncio
async def test_write_uses_the_planned_path_and_content(tmp_path):
    spy = _Spy("fs.write")
    executor = _executor(tmp_path, spy)
    mission = _mission("crea el archivo b.txt")
    step = _step(step_id="crear", capability="fs.write", args={"path": "a.txt", "content": "contenido real"})

    result = await executor.execute(mission, step, tool_name="fs.write")

    assert result.success is True
    assert spy.received[0]["path"] == "a.txt"
    assert spy.received[0]["content"] == "contenido real"


@pytest.mark.asyncio
async def test_write_fills_only_missing_keys_never_overriding_planned_ones(tmp_path):
    spy = _Spy("fs.write")
    executor = _executor(tmp_path, spy)
    mission = _mission("crea el archivo a.txt")
    step = _step(step_id="crear", capability="fs.write", args={"path": "a.txt", "content": "mío"})

    await executor.execute(mission, step, tool_name="fs.write")

    assert spy.received[0]["content"] == "mío"
    assert spy.received[0]["overwrite"] is True


# ----------------------------------------------------------------------
# H2.3 — perímetro: dentro ejecuta, fuera no
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_argument_inside_the_perimeter_executes(tmp_path):
    (tmp_path / "notas.txt").write_text("hola", encoding="utf-8")
    registry = ToolRegistry()
    registry.register_all(build_filesystem_tools(tmp_path))
    executor = SandboxExecutor(tools=registry, sandbox=SandboxRunner(workspace=tmp_path))
    mission = _mission("lee notas.txt")
    step = _step(action="research", capability="fs.read", args={"path": "notas.txt"})

    result = await executor.execute(mission, step, tool_name="fs.read")

    assert result.success is True
    assert result.output["path"] == "notas.txt"


@pytest.mark.asyncio
async def test_argument_outside_the_perimeter_is_rejected_before_the_tool_runs(tmp_path):
    spy = _Spy("fs.read")
    executor = _executor(tmp_path, spy)
    mission = _mission("lee notas.txt")
    step = _step(args={"path": "../../etc/passwd"})

    result = await executor.execute(mission, step, tool_name="fs.read")

    assert result.success is False
    assert "perímetro" in result.error
    assert spy.received == []


@pytest.mark.asyncio
async def test_tampering_after_validation_cannot_execute(tmp_path):
    """Args validados = notas.txt; alguien los cambia antes de ejecutar."""
    spy = _Spy("fs.read")
    executor = _executor(tmp_path, spy)
    mission = _mission("lee notas.txt")
    step = _step(args={"path": "notas.txt"})
    validator = PlanValidator()
    assert validator.validate_args(step) == []

    step.args = {"path": "../../etc/passwd"}
    result = await executor.execute(mission, step, tool_name="fs.read")

    assert result.success is False
    assert "perímetro" in result.error
    assert spy.received == []


# ----------------------------------------------------------------------
# H2.4 — capability y tool coherentes
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_declared_capability_decides_the_tool(tmp_path):
    """`capability=fs.stat` con `action=execute` debe ejecutar fs.stat, no fs.read."""
    registry = ToolRegistry()
    stat = _Spy("fs.stat")
    read = _Spy("fs.read")
    registry.register(stat.tool())
    registry.register(read.tool())
    executor = SandboxExecutor(tools=registry, sandbox=SandboxRunner(workspace=tmp_path))
    mission = _mission("analiza notas.txt")
    step = _step(action="execute", capability="fs.stat", args={"path": "notas.txt"})

    result = await executor.execute(mission, step)

    assert result.success is True
    assert stat.received and not read.received


# ----------------------------------------------------------------------
# H2.5 — Policy/Gate siguen siendo la autoridad
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_args_do_not_grant_authorization(tmp_path):
    """Args válidos ≠ permiso. Si la policy deniega, el executor no se llama."""
    calls = []

    async def never(mission, step, *, tool_name=None):
        calls.append(step.id)
        return ExecutionResult(success=True, output={})

    runtime = AlexisRuntime(
        planner=None,
        policy=PolicyEngine(),
        executor=never,
        verifier=None,
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
    )
    mission = _mission("lee notas.txt", capabilities=["tts.speak"])
    mission.plan = Plan(mission.id, [_step(args={"path": "notas.txt"})])

    result = await runtime.run_mission(mission)

    assert result.state is MissionState.BLOCKED
    assert calls == []


# ----------------------------------------------------------------------
# H2.6 — compatibilidad legacy
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_step_without_args_still_derives_from_the_objective(tmp_path):
    spy = _Spy("fs.read")
    executor = _executor(tmp_path, spy)
    mission = _mission("lee notas.txt del workspace")
    step = _step(args={})

    await executor.execute(mission, step, tool_name="fs.read")

    assert spy.received[0]["path"] == "notas.txt"


@pytest.mark.asyncio
async def test_legacy_remove_without_args_keeps_its_honest_error(tmp_path):
    registry = ToolRegistry()
    registry.register_all(build_filesystem_tools(tmp_path))
    executor = SandboxExecutor(tools=registry, sandbox=SandboxRunner(workspace=tmp_path))
    mission = _mission("borra algo sin decir qué")
    step = _step(step_id="borrar", capability="fs.remove", args={})

    result = await executor.execute(mission, step, tool_name="fs.remove")

    assert result.success is False
    assert "No nombraste el archivo" in result.error


# ----------------------------------------------------------------------
# Integración: la cadena completa con el runtime
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_chain_plan_args_are_what_executes(tmp_path):
    (tmp_path / "notas.txt").write_text("contenido real", encoding="utf-8")
    from alexis.cognition.planner import Planner

    mission = _mission(
        "lee notas.txt",
        capabilities=["cognition.understand", "research.filesystem", "fs.read", "fs.stat", "tts.speak"],
    )
    plan = await Planner().create_plan(mission)
    plan.steps[1].args = {"path": "notas.txt"}
    plan.steps[1].proposed_by = "model"
    mission.plan = plan

    registry = ToolRegistry()
    registry.register_all(build_filesystem_tools(tmp_path))
    executor = SandboxExecutor(tools=registry, sandbox=SandboxRunner(workspace=tmp_path))
    runtime = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=executor,
        verifier=FilesystemVerifier(workspace=tmp_path),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
    )

    result = await runtime.run_mission(mission)

    research = [r for r in result.results if r.get("step") == "research"]
    assert research and research[0]["success"] is True
    assert research[0]["output"]["path"] == "notas.txt"


@pytest.mark.asyncio
async def test_cognitive_replan_copy_preserves_the_args(tmp_path):
    """El segundo punto de pérdida: la copia del paso en un replan."""
    from alexis.cognition.loop import CognitiveRuntime
    from alexis.cognition.state import Decision, NextAction

    executor = SandboxExecutor(tools=ToolRegistry(), sandbox=SandboxRunner(workspace=tmp_path))
    cognitive = CognitiveRuntime(
        policy=PolicyEngine(),
        gate=AutonomyGate(),
        executor=executor,
        verifier=FilesystemVerifier(workspace=tmp_path),
    )
    original = _step(args={"path": "notas.txt"})
    decision = Decision(
        action=NextAction.EXECUTE_TOOL,
        step_id=original.id,
        tool="fs.stat",
        capability=original.capability,
        rationale="intento con otra tool",
    )

    copy = cognitive._plan_step_for(decision, [original])

    assert copy.args == {"path": "notas.txt"}
    assert copy.id == original.id
    assert copy.depends_on == original.depends_on


def test_args_survive_plan_serialization():
    from alexis.cognition.planner import plan_from_dict, plan_to_dict

    plan = Plan("m", [_step(args={"path": "notas.txt", "max_bytes": 10})])
    back = plan_from_dict(plan_to_dict(plan), "m")
    assert back.steps[0].args == {"path": "notas.txt", "max_bytes": 10}
