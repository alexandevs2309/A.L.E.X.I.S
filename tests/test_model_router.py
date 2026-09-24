"""Tests F2.1 — Model Router: routing, fallback, presupuesto y tri-estado P1.

El objetivo es comprobar que el Core puede cambiar de provider/config sin editar nada
(P2) y que nunca se presenta contingencia determinista como razonamiento real (P1).
"""

import asyncio

import pytest

from alexis.events.bus import EventBus
from alexis.models.config import ModelConfig
from alexis.models.degraded import EchoModel
from alexis.models.provider import (
    ModelOutcome,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelTask,
)
from alexis.models.providers.local_http import LocalHTTPProvider
from alexis.models.providers.omniroute import OmniRouteProvider
from alexis.models.providers.openai_compatible import OpenAICompatibleProvider
from alexis.models.router import ModelRouter


class FakeProvider(ModelProvider):
    """Provider de test con comportamiento programable."""

    def __init__(self, id, *, supports=None, available=True, priority=10,
                 cost=0.0, latency=10, privacy="normal", degraded=False, fail=False,
                 outcome=ModelOutcome.REAL, delay=0.0, error=None):
        self.id = id
        self.supports = supports or set(ModelTask)
        self.available = available
        self.priority = priority
        self.cost_per_1k_tokens = cost
        self.latency_p50_ms = latency
        self.privacy_max = privacy
        self.degraded = degraded
        self._fail = fail
        self._outcome = outcome
        self._delay = delay
        self._error = error
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail:
            return ModelResponse(text="", provider=self.id, model=self.id,
                                 outcome=ModelOutcome.UNAVAILABLE, error=self._error or "boom")
        return ModelResponse(text=f"respuesta de {self.id}", provider=self.id, model=self.id,
                             outcome=self._outcome)


def _req(task=ModelTask.UNDERSTAND, **kw):
    return ModelRequest.simple(task, "hola", **kw)


# ----------------------------------------------------------------------
# Outcome REAL
# ----------------------------------------------------------------------


async def test_returns_real_outcome():
    router = ModelRouter([FakeProvider("a")])
    resp = await router.complete(_req())
    assert resp.outcome is ModelOutcome.REAL
    assert resp.is_real
    assert resp.text == "respuesta de a"
    assert resp.fallback_used is False


async def test_no_degraded_when_allow_degraded_false():
    router = ModelRouter(allow_degraded=False)
    router.register(EchoModel())
    resp = await router.complete(_req())
    # no hay provider real, no hay contingencia permitida → unavailable honesto
    assert resp.outcome is ModelOutcome.UNAVAILABLE
    assert resp.error


# ----------------------------------------------------------------------
# P1: DEGRADED nunca se presenta como real
# ----------------------------------------------------------------------


async def test_degraded_is_marked_not_real():
    router = ModelRouter([FakeProvider("a", fail=True), EchoModel()])
    resp = await router.complete(_req())
    assert resp.outcome is ModelOutcome.DEGRADED
    assert resp.is_degraded
    assert not resp.is_real
    assert resp.fallback_used is True
    assert resp.fallback_from == "a"
    assert resp.text.startswith("[degraded]")


async def test_unavailable_when_no_provider_and_no_degraded():
    router = ModelRouter([FakeProvider("a", fail=True)], allow_degraded=False)
    resp = await router.complete(_req())
    assert resp.outcome is ModelOutcome.UNAVAILABLE
    assert not resp.is_degraded  # no se confunde unavailable con degraded


async def test_unavailable_when_nothing_registered():
    router = ModelRouter()
    resp = await router.complete(_req())
    assert resp.outcome is ModelOutcome.UNAVAILABLE


# ----------------------------------------------------------------------
# Routing por tarea / disponibilidad / privacidad
# ----------------------------------------------------------------------


async def test_routes_by_supported_task():
    only_plan = FakeProvider("planner", supports={ModelTask.PLAN})
    only_understand = FakeProvider("understander", supports={ModelTask.UNDERSTAND})
    router = ModelRouter([only_plan, only_understand])
    resp = await router.complete(_req(ModelTask.UNDERSTAND))
    assert resp.provider == "understander"
    assert only_plan.calls == 0


async def test_skips_unavailable_provider():
    off = FakeProvider("off", available=False)
    on = FakeProvider("on")
    router = ModelRouter([off, on])
    resp = await router.complete(_req())
    assert resp.provider == "on"
    assert off.calls == 0


async def test_privacy_filtering():
    cloud = FakeProvider("cloud", privacy="sensitive")
    local = FakeProvider("local", privacy="secret")
    router = ModelRouter([cloud, local])
    resp = await router.complete(_req(privacy="secret"))
    assert resp.provider == "local"  # secret no puede ir a cloud
    assert cloud.calls == 0


async def test_priority_beats_cost():
    cheap_low_priority = FakeProvider("cheap", priority=90, cost=0.0)
    pricey_high_priority = FakeProvider("best", priority=10, cost=5.0)
    router = ModelRouter([cheap_low_priority, pricey_high_priority])
    resp = await router.complete(_req())
    assert resp.provider == "best"


# ----------------------------------------------------------------------
# Fallback encadenado
# ----------------------------------------------------------------------


async def test_fallback_chain_tries_next_on_failure():
    a = FakeProvider("a", fail=True)
    b = FakeProvider("b")
    router = ModelRouter([a, b])
    resp = await router.complete(_req())
    assert resp.provider == "b"
    assert a.calls == 1 and b.calls == 1
    assert resp.fallback_used is True
    assert "a" in resp.chain


async def test_deadline_timeout_falls_back():
    slow = FakeProvider("slow", delay=0.2)
    fast = FakeProvider("fast")
    router = ModelRouter([slow, fast])
    resp = await router.complete(_req(deadline_ms=50))
    assert resp.provider == "fast"


# ----------------------------------------------------------------------
# Presupuesto (P3 / envelope max_cost_usd)
# ----------------------------------------------------------------------


async def test_budget_blocks_paid_provider_when_exhausted():
    paid = FakeProvider("paid", cost=100.0)
    free = FakeProvider("free", cost=0.0)
    router = ModelRouter([paid, free], budget_usd=0.001)
    resp = await router.complete(_req(max_tokens=1000))
    assert resp.provider == "free"
    assert paid.calls == 0


async def test_reset_budget_clears_spend():
    router = ModelRouter(budget_usd=1.0)
    router.spent_usd = 0.9
    router.reset_budget(1.0)
    assert router.spent_usd == 0.0


# ----------------------------------------------------------------------
# Auditoría: model.routed siempre con outcome (P1)
# ----------------------------------------------------------------------


async def test_model_routed_event_published_with_outcome():
    bus = EventBus()
    router = ModelRouter([FakeProvider("a")], event_bus=bus)
    await router.complete(_req())
    await asyncio.sleep(0)
    topics = [e["topic"] for e in bus.events]
    assert "model.routed" in topics
    payload = [e["payload"] for e in bus.events if e["topic"] == "model.routed"][0]
    assert payload["outcome"] == "real"
    assert payload["provider"] == "a"


async def test_degraded_routed_event_marks_degraded():
    bus = EventBus()
    router = ModelRouter([FakeProvider("a", fail=True), EchoModel()], event_bus=bus)
    await router.complete(_req())
    await asyncio.sleep(0)
    payload = [e["payload"] for e in bus.events if e["topic"] == "model.routed"][0]
    assert payload["outcome"] == "degraded"
    assert payload["fallback_used"] is True


# ----------------------------------------------------------------------
# P2: cambiar de provider sin tocar el Core
# ----------------------------------------------------------------------


async def test_swap_provider_by_registration_only():
    router = ModelRouter()
    router.register(FakeProvider("v1"))
    r1 = await router.complete(_req())
    router.register(FakeProvider("v2", priority=1))  # v2 pasa a preferirse
    r2 = await router.complete(_req())
    assert r1.provider == "v1"
    assert r2.provider == "v2"


# ----------------------------------------------------------------------
# Providers HTTP: payload/parseo puros + honestidad de disponibilidad
# -------------------------------------------------------------------------


def test_local_http_not_available_without_base_url():
    p = LocalHTTPProvider(base_url="", model="m")
    assert p.available is False
    assert p.dialect == "ollama"
    assert p.endpoint.endswith("/api/chat")


def test_local_http_builds_ollama_payload():
    p = LocalHTTPProvider(base_url="http://x:11434", model="m")
    payload = p.build_payload(ModelRequest.simple(ModelTask.ANALYZE, "hola", max_tokens=10))
    assert payload["model"] == "m"
    assert payload["stream"] is False
    assert payload["messages"][-1]["content"] == "hola"


def test_openai_payload_and_headers():
    p = OpenAICompatibleProvider(base_url="http://api", model="m", api_key="k")
    payload = p.build_payload(ModelRequest.simple(ModelTask.PLAN, "hola"))
    assert payload["model"] == "m"
    assert "choices" not in payload
    assert p.build_headers()["Authorization"] == "Bearer k"
    assert p.endpoint.endswith("/v1/chat/completions")


def test_openai_parses_choices_and_usage():
    p = OpenAICompatibleProvider(base_url="http://api", model="m")
    text, data, tin, tout = p.parse_response(
        {"choices": [{"message": {"content": "hola"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 4}}
    )
    assert text == "hola"
    assert (tin, tout) == (3, 4)
    assert data is None


def test_openai_parses_json_content_into_data():
    p = OpenAICompatibleProvider(base_url="http://api", model="m")
    text, data, _, _ = p.parse_response(
        {"choices": [{"message": {"content": '{"steps": [{"id": "x"}]}'}}]}
    )
    assert data == {"steps": [{"id": "x"}]}


def test_omniroute_is_honestly_unavailable_without_endpoint():
    p = OmniRouteProvider(base_url="")
    assert p.available is False


async def test_omniroute_returns_unavailable_when_called_directly():
    p = OmniRouteProvider(base_url="")
    resp = await p.complete(ModelRequest.simple(ModelTask.PLAN, "x"))
    assert resp.outcome is ModelOutcome.UNAVAILABLE
    assert "missing" in (resp.error or "")


async def test_router_ignores_omniroute_when_unconfigured():
    router = ModelRouter([OmniRouteProvider(base_url="")], allow_degraded=False)
    resp = await router.complete(_req())
    assert resp.outcome is ModelOutcome.UNAVAILABLE


# ----------------------------------------------------------------------
# Config por entorno (P2: provider por config, no por código)
# -------------------------------------------------------------------------


def test_config_defaults_to_no_provider():
    cfg = ModelConfig.from_env({})
    assert cfg.provider == "none"
    assert cfg.build_providers() == []
    assert cfg.allow_degraded() is True


def test_config_builds_local_provider_from_env():
    cfg = ModelConfig.from_env(
        {"ALEXIS_MODEL_PROVIDER": "local_http", "ALEXIS_MODEL_BASE_URL": "http://local:11434",
         "ALEXIS_MODEL_NAME": "qwen"}
    )
    providers = cfg.build_providers()
    assert len(providers) == 1
    assert providers[0].id == "local_http"
    assert providers[0].available is True
    assert cfg.build_degraded() is not None


def test_config_fallback_none_disables_degraded():
    cfg = ModelConfig.from_env({"ALEXIS_MODEL_FALLBACK": "none"})
    assert cfg.allow_degraded() is False
    assert cfg.build_degraded() is None


def test_config_ignores_unknown_provider():
    cfg = ModelConfig.from_env({"ALEXIS_MODEL_PROVIDER": "inventado"})
    assert cfg.provider == "none"
    assert cfg.build_providers() == []


async def test_router_from_config_end_to_end():
    cfg = ModelConfig.from_env({"ALEXIS_MODEL_PROVIDER": "none"})
    router = ModelRouter(allow_degraded=cfg.allow_degraded())
    router.register(EchoModel())
    resp = await router.complete(_req())
    assert resp.outcome is ModelOutcome.DEGRADED


# ----------------------------------------------------------------------
# describe() para introspection/health
# ----------------------------------------------------------------------


def test_describe_exposes_outcome_metadata():
    router = ModelRouter([FakeProvider("a"), EchoModel()])
    described = {d["id"]: d for d in router.describe()}
    assert described["a"]["degraded"] is False
    assert described["echo"]["degraded"] is True
    assert "understand" in described["a"]["supports"]


@pytest.mark.parametrize("task", list(ModelTask))
async def test_every_task_is_routable(task):
    router = ModelRouter([FakeProvider("all")])
    resp = await router.complete(_req(task))
    assert resp.outcome is ModelOutcome.REAL
