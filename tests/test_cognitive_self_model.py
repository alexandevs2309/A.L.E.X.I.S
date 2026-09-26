"""Fase 2, incremento 2.3: el Self Model como entrada de decisión.

Objetivo: que ALEXIS descubra que NO puede hacer algo **antes** de intentar, y pregunte,
en vez de ejecutar una tool que no tiene y terminar en BLOCKED.
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
from alexis.self.model import SelfModel  # noqa: E402
from alexis.self.presence import derive_presence  # noqa: E402
from alexis.self.sync import SelfModelSync  # noqa: E402

ACTIONS = ["understand", "analyze", "research", "execute", "verify", "respond"]


def _mission(objective, **over):
    data = dict(objective=objective, autonomy=AutonomyLevel.SUPERVISED, allowed_actions=list(ACTIONS))
    data.update(over)
    return MissionEngine().create(objective, MissionEnvelope(**data))


def _plan(*steps):
    return Plan("m", list(steps))


def _step(step_id, action="execute", capability="fs.read"):
    return PlanStep(step_id, f"paso {step_id}", action, RiskLevel.MEDIUM, "executor", capability=capability)


def _self_model(mission, *, capabilities, available, tools=()):
    model = SelfModel(capabilities=list(capabilities))
    model.update(mission, tools=list(tools), memory_items=[], verification=None, available=list(available))
    return model


class _NoopPolicy:
    def authorize(self, mission, step):
        class _D:
            allowed = True
            requires_approval = False
            reason = "test"

        return _D()

    def evaluate(self, mission, step):
        return self.authorize(mission, step)


class _DenyOutsideEnvelope(_NoopPolicy):
    def authorize(self, mission, step):
        declared = set(getattr(mission.envelope, "capabilities", []) or [])
        if declared and step.capability and step.capability not in declared:
            class _D:
                allowed = False
                requires_approval = False
                reason = f"la capacidad '{step.capability}' no está en el envelope de la misión"

            return _D()
        return super().authorize(mission, step)


class _PassingVerifier:
    def __init__(self, passed=True):
        self.passed = passed

    async def verify(self, mission, plan):
        return Verification(passed=self.passed, evidence=["ok"], confidence=0.9, notes="verificado")


class _Executor:
    def __init__(self):
        self.calls = []

    async def execute(self, mission, step, *, tool_name=None):
        self.calls.append(step.id)
        return ExecutionResult(
            success=True,
            output={"ok": True, "step": step.id},
            observations=[Observation(f"tool.{step.id}", {"ok": True}, trusted=True)],
        )

    async def __call__(self, mission, step, tool_name=None):
        return await self.execute(mission, step, tool_name=tool_name)


class _Router:
    def __init__(self):
        self.requests = []

    async def complete(self, request: ModelRequest):
        self.requests.append(request)
        return ModelResponse(
            text="",
            data={"action": "execute_tool", "step_id": "investigar", "rationale": "sigo"},
            provider="stub",
            model="stub",
            outcome=ModelOutcome.REAL,
        )


def _runtime(executor, self_model, policy=None, model_router=None):
    return CognitiveRuntime(
        policy=policy or _NoopPolicy(),
        executor=executor,
        verifier=_PassingVerifier(),
        self_model=self_model,
        model_router=model_router,
    )


async def _drain(cognitive, mission, plan, limit=8):
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
# Test obligatorio: capability ausente -> ASK_USER antes de cualquier tool
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_capability_asks_user_before_executing_any_tool():
    mission = _mission("lee el repositorio git del proyecto")
    plan = _plan(_step("leer_git", capability="git.read"))
    executor = _Executor()
    self_model = _self_model(
        mission,
        capabilities=["fs.read", "fs.stat", "tts.speak"],
        available=["fs.read", "fs.stat", "tts.speak"],
    )
    cognitive = _runtime(executor, self_model)

    actions, knowledge, outcome = await _drain(cognitive, mission, plan)

    assert actions == ["ask_user"]
    assert executor.calls == []
    assert outcome.mission_state is MissionState.WAITING_CLARIFICATION
    assert "git.read" in outcome.question
    assert "no las tengo" in outcome.question
    assert any("no tengo la capability 'git.read'" in u for u in knowledge.unknown)


@pytest.mark.asyncio
async def test_available_capabilities_still_execute_normally():
    mission = _mission("lee notas.txt")
    plan = _plan(_step("investigar", action="research", capability="research.filesystem"))
    executor = _Executor()
    self_model = _self_model(
        mission,
        capabilities=["research.filesystem", "fs.read", "tts.speak"],
        available=["research.filesystem", "fs.read", "tts.speak"],
    )
    cognitive = _runtime(executor, self_model)

    actions, _, outcome = await _drain(cognitive, mission, plan)

    assert executor.calls == ["investigar"]
    assert "ask_user" not in actions
    assert outcome.mission_state is MissionState.NEEDS_VERIFICATION


@pytest.mark.asyncio
async def test_partial_gap_executes_what_is_available_and_records_the_gap():
    mission = _mission("revisa el proyecto y luego lee el repo git")
    plan = _plan(
        _step("investigar", action="research", capability="research.filesystem"),
        _step("leer_git", capability="git.read"),
    )
    executor = _Executor()
    self_model = _self_model(
        mission,
        capabilities=["research.filesystem", "git.read"],
        available=["research.filesystem"],
    )
    cognitive = _runtime(executor, self_model)

    actions, knowledge, outcome = await _drain(cognitive, mission, plan)

    assert actions == ["research", "ask_user"]
    assert executor.calls == ["investigar"]
    assert "leer_git" not in executor.calls
    assert any("no tengo la capability 'git.read'" in u for u in knowledge.unknown)
    assert outcome.mission_state is MissionState.WAITING_CLARIFICATION
    assert "git.read" in outcome.question


@pytest.mark.asyncio
async def test_a_dependent_step_is_not_executable_if_its_dependency_cannot_run():
    """`responder` depende de `leer_git`: sin `git.read`, responder tampoco tiene sentido."""
    mission = _mission("analiza el historial del repositorio git")
    plan = _plan(
        _step("leer_git", action="research", capability="git.read"),
        _step(
            "responder",
            action="respond",
            capability="tts.speak",
        ),
    )
    plan.steps[1].depends_on = ["leer_git"]
    executor = _Executor()
    self_model = _self_model(
        mission,
        capabilities=["fs.read", "tts.speak"],
        available=["fs.read", "tts.speak"],
    )
    cognitive = _runtime(executor, self_model)

    actions, _, outcome = await _drain(cognitive, mission, plan)

    assert actions == ["ask_user"]
    assert executor.calls == []
    assert "git.read" in outcome.question


@pytest.mark.asyncio
async def test_empty_self_brief_does_not_block_the_mission():
    """Si el brief no dice qué hay disponible, no se inventa una carencia."""
    mission = _mission("lee notas.txt")
    plan = _plan(_step("investigar", action="research"))
    executor = _Executor()
    self_model = _self_model(mission, capabilities=[], available=[])
    cognitive = _runtime(executor, self_model)

    actions, _, outcome = await _drain(cognitive, mission, plan)

    assert executor.calls == ["investigar"]
    assert outcome.mission_state is MissionState.NEEDS_VERIFICATION
    assert "ask_user" not in actions


@pytest.mark.asyncio
async def test_without_self_model_behavior_is_unchanged():
    mission = _mission("lee el repositorio git")
    plan = _plan(_step("leer_git", capability="git.read"))
    executor = _Executor()
    cognitive = _runtime(executor, None)

    actions, _, outcome = await _drain(cognitive, mission, plan)

    assert executor.calls == ["leer_git"]
    assert "ask_user" not in actions
    assert outcome.mission_state is MissionState.NEEDS_VERIFICATION


@pytest.mark.asyncio
async def test_broken_self_model_does_not_break_the_runtime():
    class _Broken:
        def snapshot(self):
            raise RuntimeError("self model caído")

    mission = _mission("lee notas.txt")
    plan = _plan(_step("investigar", action="research"))
    executor = _Executor()
    cognitive = _runtime(executor, _Broken())

    actions, _, _ = await _drain(cognitive, mission, plan)

    assert executor.calls == ["investigar"]
    assert "ask_user" not in actions


# ----------------------------------------------------------------------
# Frontera con Policy: lo que tengo pero no me autorizan -> POLICY
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capability_outside_envelope_is_decided_by_policy_not_by_ask_user():
    mission = _mission("borra notas.txt", capabilities=["fs.read"])
    plan = _plan(_step("borrar", capability="fs.remove"))
    executor = _Executor()
    self_model = _self_model(
        mission,
        capabilities=["fs.read", "fs.remove"],
        available=["fs.read", "fs.remove"],
    )
    cognitive = _runtime(executor, self_model, policy=_DenyOutsideEnvelope())

    actions, knowledge, outcome = await _drain(cognitive, mission, plan)

    assert actions == ["execute_tool"]
    assert outcome.mission_state is MissionState.BLOCKED
    assert executor.calls == []
    assert any("no está en el envelope" in u for u in knowledge.unknown)


# ----------------------------------------------------------------------
# El Self Model llega a la decisión del modelo
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_brief_reaches_the_decision_prompt():
    mission = _mission("revisa el proyecto")
    plan = _plan(
        _step("investigar", action="research", capability="research.filesystem"),
        _step("responder", action="respond", capability="tts.speak"),
    )
    router = _Router()
    self_model = _self_model(
        mission,
        capabilities=["research.filesystem", "tts.speak"],
        available=["research.filesystem", "tts.speak"],
    )
    cognitive = _runtime(_Executor(), self_model, model_router=router)
    knowledge = KnowledgeState(objective=mission.goal.objective, iterations=1)

    await cognitive.decide_next_action(
        mission, knowledge, pending_steps=cognitive.pending_steps(mission, plan, knowledge)
    )

    prompt = router.requests[-1].messages[-1]["content"]
    assert "self model:" in prompt
    assert "puedo: research.filesystem, tts.speak" in prompt
    assert "me falta:" in prompt


@pytest.mark.asyncio
async def test_self_model_is_consulted_on_every_decision():
    class _Spy(SelfModel):
        calls = 0

        def snapshot(self):
            type(self).calls += 1
            return super().snapshot()

    mission = _mission("lee notas.txt")
    plan = _plan(_step("investigar", action="research"), _step("responder", action="respond", capability="tts.speak"))
    model = _Spy(capabilities=["research.filesystem", "tts.speak"])
    model.update(mission, tools=[], memory_items=[], verification=None, available=["research.filesystem", "tts.speak"])
    cognitive = _runtime(_Executor(), model)

    await _drain(cognitive, mission, plan)

    assert _Spy.calls >= 2


# ----------------------------------------------------------------------
# El Self Model también refleja lo que hizo el bucle (salida)
# ----------------------------------------------------------------------


def _sync(mission, available=("fs.read",)):
    model = SelfModel(capabilities=list(available) + ["git.read"])
    model.update(mission, tools=[], memory_items=[], verification=None, available=list(available))
    sync = SelfModelSync(model, lambda: mission, aux=lambda: {"tools": [], "lessons": [], "memory_items": []})
    return model, sync


def test_self_model_records_a_replan_and_shows_replanning_presence():
    mission = _mission("revisa el proyecto")
    mission.state = MissionState.RUNNING
    model, sync = _sync(mission)

    sync.apply_event(
        "cognition.step",
        {
            "action": "replan",
            "diagnosis": "not_found: el archivo no existe",
            "decision": {"step_id": "investigar", "rationale": "cambio de estrategia"},
        },
    )

    assert model.current_state == "replanning"
    assert any("cambié de estrategia" in o["text"] for o in model.observations_about_self)
    assert any("not_found" in o["text"] for o in model.observations_about_self)


def test_self_model_records_the_ask_user_question():
    mission = _mission("lee el repo git")
    mission.state = MissionState.WAITING_CLARIFICATION
    model, sync = _sync(mission)

    sync.apply_event(
        "cognition.step",
        {"action": "ask_user", "question": "no tengo git.read", "decision": {"rationale": "me falta"}},
    )

    assert any("pregunté al usuario" in o["text"] for o in model.observations_about_self)
    assert model.current_state == "waiting_for_approval"


def test_self_model_is_honest_about_degraded_cognition():
    mission = _mission("lee notas.txt")
    mission.state = MissionState.RUNNING
    model, sync = _sync(mission)

    sync.apply_event(
        "cognition.step",
        {
            "action": "execute_tool",
            "success": True,
            "decision": {"step_id": "investigar", "cognition_outcome": "degraded"},
        },
    )

    assert any("sin razonamiento real" in o["text"] for o in model.observations_about_self)


def test_presence_reflects_waiting_clarification():
    mission = _mission("lee notas.txt")
    mission.state = MissionState.WAITING_CLARIFICATION
    assert derive_presence(mission) == "waiting_for_approval"


# ----------------------------------------------------------------------
# El Self Model no puede afirmar capacidades que no tiene
# ----------------------------------------------------------------------


def test_self_model_does_not_claim_missing_capabilities():
    """Regresión: `available` por defecto era el catálogo completo (30), incluidas las `missing`.

    Con eso el runtime creía tener `git.read` y nunca preguntaba.
    """
    from alexis.capabilities import build_catalog

    catalog = build_catalog()
    all_ids = [s.id for s in catalog.specs()]
    enabled_ids = [s.id for s in catalog.enabled()]

    honest = SelfModel(capabilities=all_ids, available=enabled_ids)
    legacy = SelfModel(capabilities=all_ids)

    assert "git.read" not in honest.snapshot()["available_capabilities"]
    assert "git.read" in honest.snapshot()["capabilities"]
    assert "git.read" in legacy.snapshot()["available_capabilities"]
