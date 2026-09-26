"""Presupuesto de tokens de las llamadas cognitivas (brecha #2).

Un modelo de razonamiento consume tokens en pensar ANTES de emitir el JSON. Con un tope
estrecho la respuesta llega truncada (`finish_reason: length`) y el runtime la descarta
como "no JSON utilizable", aunque la decisión fuese correcta: se pierde la decisión del
LLM y cae al fallback determinista.

Estos tests fijan el contrato: la decisión y el plan llevan presupuesto suficiente y
configurable, y el valor no puede volver aMagicamente a 320/400 sin que falle la suite.
"""

import pytest

from alexis.cognition.loop import CognitiveRuntime
from alexis.cognition.planner_model import ModelPlanner
from alexis.cognition.state import Decision, KnowledgeState, NextAction
from alexis.contracts import AutonomyLevel, Goal, Mission, MissionEnvelope
from alexis.models.config import ModelConfig
from alexis.models.provider import ModelRequest, ModelTask, ModelResponse, ModelOutcome


def _runtime(**kw) -> CognitiveRuntime:
    class _P:
        def evaluate(self, mission, step):  # pragma: no cover - no se usa aquí
            raise AssertionError("no debe evaluar policy en este test")

    return CognitiveRuntime(policy=_P(), verifier=lambda *a, **k: None, **kw)


class _RecordingRouter:
    """Router que guarda la petición y devuelve una decisión válida."""

    def __init__(self, data: dict):
        self.data = data
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest):
        self.requests.append(request)
        return ModelResponse(
            text="{}",
            data=self.data,
            provider="fake",
            model="fake",
            outcome=ModelOutcome.REAL,
        )


async def test_decision_request_carries_configured_token_budget():
    router = _RecordingRouter({"action": "verify", "rationale": "comprobar"})
    runtime = _runtime(model_router=router, decision_max_tokens=2048, decision_deadline_ms=60000)
    mission = Mission(
        id="m",
        goal=Goal("revisar"),
        envelope=MissionEnvelope(objective="revisar", autonomy=AutonomyLevel.SUPERVISED),
    )
    knowledge = KnowledgeState(objective="revisar")
    options = [
        Decision(action=NextAction.EXECUTE_TOOL, step_id="research", capability="fs.read", rationale="avanzar"),
        Decision(action=NextAction.VERIFY, rationale="comprobar"),
    ]
    await runtime._ask_model(mission, knowledge, options)
    assert len(router.requests) == 1
    request = router.requests[0]
    assert request.task is ModelTask.REASON
    assert request.max_tokens == 2048
    assert request.deadline_ms == 60000
    assert request.schema is not None  # el esquema se mantiene


async def test_default_decision_budget_is_wide_enough_for_reasoning_models():
    """El default debe exceder con holgura el gasto medido (≈360 thinking + ≈115 JSON)."""
    runtime = _runtime()
    assert runtime.decision_max_tokens >= 1024
    assert runtime.decision_deadline_ms >= 30000


def test_planner_budget_is_wide_enough_by_default():
    planner = ModelPlanner(_RecordingRouter({"steps": []}))
    assert planner.max_tokens >= 1024


def test_config_defaults_are_reasoning_model_safe():
    cfg = ModelConfig.from_env({})
    assert cfg.max_tokens >= 1024
    assert cfg.deadline_ms >= 30000


def test_config_budget_is_overridable_per_deployment():
    cfg = ModelConfig.from_env({"ALEXIS_MODEL_MAX_TOKENS": "512", "ALEXIS_MODEL_DEADLINE_MS": "15000"})
    assert cfg.max_tokens == 512
    assert cfg.deadline_ms == 15000
