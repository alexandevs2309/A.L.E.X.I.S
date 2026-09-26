"""Fase 2, incremento 2.4: World Model operativo y consultable.

El World Model no es un almacén: existe para que el runtime **use conocimiento del
mundo al decidir**. El caso central: si una herramienta ya observó que la ruta del
objetivo no existe, repetir una lectura sobre ella es un fallo previsible; ALEXIS debe
preguntar en vez de insistir.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.cognition.loop import CognitiveRuntime  # noqa: E402
from alexis.cognition.state import KnowledgeState  # noqa: E402
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
from alexis.models.provider import ModelOutcome, ModelRequest, ModelResponse  # noqa: E402
from alexis.world.model import FILE, TOOL, WorldEntity, WorldModel  # noqa: E402

ACTIONS = ["understand", "analyze", "research", "execute", "verify", "respond"]


def _mission(objective, **over):
    data = dict(objective=objective, autonomy=AutonomyLevel.SUPERVISED, allowed_actions=list(ACTIONS))
    data.update(over)
    return MissionEngine().create(objective, MissionEnvelope(**data))


def _plan(*steps):
    return Plan("m", list(steps))


def _step(step_id, action="execute", capability="fs.read", depends_on=()):
    return PlanStep(
        step_id,
        f"paso {step_id}",
        action,
        RiskLevel.MEDIUM,
        "executor",
        list(depends_on),
        capability=capability,
    )


def _result(success, output, error=None):
    return ExecutionResult(
        success=success,
        output=output,
        error=error,
        observations=[Observation("tool.test", output, trusted=True)] if output else [],
    )


class _NoopPolicy:
    def authorize(self, mission, step):
        class _D:
            allowed = True
            requires_approval = False
            reason = "test"

        return _D()

    def evaluate(self, mission, step):
        return self.authorize(mission, step)


class _PassingVerifier:
    async def verify(self, mission, plan):
        return Verification(passed=True, evidence=["ok"], confidence=0.9, notes="verificado")


class _ScriptedExecutor:
    """Devuelve lo que el mundo debería registrar, paso a paso."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def __call__(self, mission, step, tool_name=None):
        self.calls.append(step.id)
        outcome = self.script.pop(0) if self.script else (True, {"ok": True, "path": "notas.txt"})
        success, output = outcome
        error = None if success else "no existe"
        return _result(success, output, error)

    async def execute(self, mission, step, *, tool_name=None):
        return await self.__call__(mission, step, tool_name)


class _Router:
    def __init__(self):
        self.requests = []

    async def complete(self, request: ModelRequest):
        self.requests.append(request)
        return ModelResponse(
            text="",
            data={"action": "research", "step_id": "investigar", "rationale": "sigo"},
            provider="stub",
            model="stub",
            outcome=ModelOutcome.REAL,
        )


def _runtime(executor, world, model_router=None):
    return CognitiveRuntime(
        policy=_NoopPolicy(),
        executor=executor,
        verifier=_PassingVerifier(),
        world=world,
        model_router=model_router,
    )


async def _drain(cognitive, mission, plan, limit=10):
    knowledge = cognitive.knowledge_for(mission)
    actions = []
    for _ in range(limit):
        pending = cognitive.pending_steps(mission, plan, knowledge)
        outcome = await cognitive.step(mission, knowledge, pending_steps=pending, plan=plan)
        knowledge = outcome.knowledge
        actions.append(outcome.action.value)
        if outcome.done:
            return actions, knowledge, outcome
    raise AssertionError("el bucle no terminó")


# ----------------------------------------------------------------------
# El World Model registra lo que las herramientas observaron de verdad
# ----------------------------------------------------------------------


def test_observe_execution_records_a_real_file_observation():
    world = WorldModel()
    step = _step("investigar", action="research", capability="fs.stat")

    world.observe_execution(step, _result(True, {"ok": True, "path": "notas.txt", "exists": True, "size": 12}))

    entity = world.known_path("notas.txt")
    assert entity is not None
    assert entity.kind == FILE
    assert entity.attributes["exists"] is True
    assert entity.attributes["size"] == 12
    assert entity.source == "tool:fs.stat"
    assert entity.confidence < 1.0
    assert entity.observations == 1


def test_observe_execution_records_a_failed_lookup_as_nonexistent():
    world = WorldModel()
    step = _step("investigar", action="research", capability="fs.stat")

    world.observe_execution(step, _result(True, {"ok": True, "path": "noexiste.txt", "exists": False}))

    entity = world.known_path("noexiste.txt")
    assert entity.attributes["exists"] is False
    assert world.missing_paths(["noexiste.txt"]) == ["noexiste.txt"]
    assert world.missing_paths(["otro.txt"]) == []


def test_repeated_observations_increment_the_counter():
    world = WorldModel()
    step = _step("investigar", action="research", capability="fs.stat")
    for _ in range(3):
        world.observe_execution(step, _result(True, {"ok": True, "path": "notas.txt", "exists": True}))
    assert world.known_path("notas.txt").observations == 3


def test_observe_execution_ignores_outputs_without_a_path():
    world = WorldModel()
    step = _step("entender", action="analyze", capability="cognition.understand")
    world.observe_execution(step, _result(True, {"analysis": "sin ruta"}))
    assert world.snapshot() == []


# ----------------------------------------------------------------------
# Consulta
# ----------------------------------------------------------------------


def test_query_by_kind_and_text():
    world = WorldModel()
    world.upsert(WorldEntity("file:notas.txt", FILE, "notas.txt", {"exists": True}))
    world.upsert(WorldEntity("file:otro.txt", FILE, "otro.txt", {"exists": True}))
    world.declare_tool("fs.read", {"sandbox": "sandbox-project"})

    assert len(world.query(kind=FILE)) == 2
    assert len(world.query(kind=TOOL)) == 1
    relevant = world.for_objective("lee notas.txt del proyecto")
    assert relevant
    assert relevant[0].name == "notas.txt"


def test_dependencies_are_queryable():
    world = WorldModel()
    world.upsert(WorldEntity("project:hospitality", "project", "Hospitality"))
    world.upsert(WorldEntity("service:api", "service", "API"))
    world.upsert(WorldEntity("database:postgres", "database", "PostgreSQL"))
    world.relate("project:hospitality", "service:api", "depends_on")
    world.relate("service:api", "database:postgres", "depends_on")

    assert [e.name for e in world.dependencies("project:hospitality")] == ["API"]
    assert {e.name for e in world.neighbors("service:api")} == {"Hospitality", "PostgreSQL"}


def test_prompt_lines_carry_the_source():
    world = WorldModel()
    world.observe_execution(
        _step("investigar", action="research", capability="fs.stat"),
        _result(True, {"ok": True, "path": "notas.txt", "exists": True}),
    )
    lines = world.to_prompt_lines()
    assert lines and "notas.txt" in lines[0]
    assert "evidencia de tool:fs.stat" in lines[0]


# ----------------------------------------------------------------------
# Comportamiento real: el mundo cambia la decisión
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_world_knowledge_prevents_a_predictable_second_failure():
    """El caso central: `fs.stat` ya observó que el archivo no existe.

    Repetir la lectura sería terquedad, no razonamiento: ALEXIS pregunta.
    """
    world = WorldModel()
    mission = _mission("lee el archivo fantasma.txt")
    plan = _plan(
        _step("investigar", action="research", capability="fs.stat"),
        _step("leer", action="research", capability="fs.read", depends_on=("investigar",)),
    )
    executor = _ScriptedExecutor(
        [
            (True, {"ok": True, "path": "fantasma.txt", "exists": False}),
        ]
    )
    cognitive = _runtime(executor, world)

    actions, knowledge, outcome = await _drain(cognitive, mission, plan)

    assert world.known_path("fantasma.txt").attributes["exists"] is False
    assert executor.calls == ["investigar"]
    assert actions[-1] == "ask_user"
    assert outcome.mission_state is MissionState.WAITING_CLARIFICATION
    assert "fantasma.txt" in outcome.question
    assert any("no existe" in k for k in knowledge.known)


@pytest.mark.asyncio
async def test_world_knowledge_favours_the_viable_step_over_the_predictable_failure():
    """Cadena completa de conocimiento del mundo guiando decisiones.

    1. El mundo observó que `fantasma.txt` NO existe → leerlo es fallo previsible → se
       salta `leer` y ejecuta `crear`.
    2. `crear` devuelve que el archivo existe → el mundo se actualiza.
    3. Ahora `leer` sí es viable → se ejecuta.

    La segunda lectura no era un paso del plan: la habilitó una observación.
    """
    world = WorldModel()
    world.observe_execution(
        _step("investigar", action="research", capability="fs.stat"),
        _result(True, {"ok": True, "path": "fantasma.txt", "exists": False}),
    )
    mission = _mission("lee el archivo fantasma.txt")
    plan = _plan(
        _step("leer", action="research", capability="fs.read"),
        _step("crear", action="execute", capability="fs.write"),
    )
    executor = _ScriptedExecutor(
        [
            (True, {"ok": True, "path": "fantasma.txt", "exists": True}),
            (True, {"ok": True, "path": "fantasma.txt", "exists": True}),
        ]
    )
    cognitive = _runtime(executor, world)

    actions, knowledge, _ = await _drain(cognitive, mission, plan)

    assert executor.calls == ["crear", "leer"]
    assert any("no es viable" in h for h in knowledge.hypotheses)
    assert world.known_path("fantasma.txt").attributes["exists"] is True
    assert "ask_user" not in actions


@pytest.mark.asyncio
async def test_world_is_empty_does_not_change_behavior():
    world = WorldModel()
    mission = _mission("lee notas.txt")
    plan = _plan(_step("investigar", action="research", capability="fs.read"))
    executor = _ScriptedExecutor([(True, {"ok": True, "path": "notas.txt", "exists": True})])
    cognitive = _runtime(executor, world)

    actions, _, outcome = await _drain(cognitive, mission, plan)

    assert executor.calls == ["investigar"]
    assert "ask_user" not in actions
    assert outcome.mission_state is MissionState.NEEDS_VERIFICATION


@pytest.mark.asyncio
async def test_without_world_model_behavior_is_unchanged():
    mission = _mission("lee el archivo fantasma.txt")
    plan = _plan(
        _step("investigar", action="research", capability="fs.stat"),
        _step("leer", action="research", capability="fs.read", depends_on=("investigar",)),
    )
    executor = _ScriptedExecutor(
        [
            (True, {"ok": True, "path": "fantasma.txt", "exists": False}),
            (True, {"ok": True, "path": "fantasma.txt"}),
        ]
    )
    cognitive = _runtime(executor, None)

    actions, _, _ = await _drain(cognitive, mission, plan)

    assert executor.calls == ["investigar", "leer"]
    assert "ask_user" not in actions


@pytest.mark.asyncio
async def test_world_context_reaches_the_decision_prompt():
    world = WorldModel()
    world.upsert(WorldEntity("file:notas.txt", FILE, "notas.txt", {"exists": True, "size": 30}))
    mission = _mission("revisa notas.txt")
    plan = _plan(
        _step("investigar", action="research", capability="fs.read"),
        _step("responder", action="respond", capability="tts.speak"),
    )
    router = _Router()
    executor = _ScriptedExecutor([(True, {"ok": True, "path": "notas.txt", "exists": True})])
    cognitive = _runtime(executor, world, model_router=router)
    knowledge = KnowledgeState(objective=mission.goal.objective, iterations=1)

    await cognitive.decide_next_action(
        mission, knowledge, pending_steps=cognitive.pending_steps(mission, plan, knowledge)
    )

    prompt = router.requests[-1].messages[-1]["content"]
    assert "world (lo que he observado del entorno)" in prompt
    assert "notas.txt" in prompt


@pytest.mark.asyncio
async def test_broken_world_model_does_not_break_the_runtime():
    class _Broken:
        def known_path(self, path):
            raise RuntimeError("world caído")

        def for_objective(self, objective, limit=6):
            raise RuntimeError("world caído")

        def fails_on_missing(self, capability):
            return False

    mission = _mission("lee notas.txt")
    plan = _plan(_step("investigar", action="research", capability="fs.read"))
    executor = _ScriptedExecutor([(True, {"ok": True, "path": "notas.txt", "exists": True})])
    cognitive = _runtime(executor, _Broken())

    actions, _, _ = await _drain(cognitive, mission, plan)

    assert executor.calls == ["investigar"]
    assert "ask_user" not in actions


def test_knowledge_world_is_serialized():
    knowledge = KnowledgeState(objective="o")
    knowledge.world = ["file:notas.txt (exists=True)"]
    restored = KnowledgeState.from_dict(knowledge.to_dict())
    assert restored.world == ["file:notas.txt (exists=True)"]
