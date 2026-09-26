"""Model Router: resuelve QUÉ PROVEEDOR puede satisfacer una tarea ya decidida (F2.1).

Separación estricta (P2, docs/COGNITIVE-CORE-F2.md §1.3):

- El **Cognitive Core** decide *qué* hay que hacer (intención, plan, capabilities,
  claims).
- El **Model Router** decide *qué proveedor/modelo* puede satisfacer esa necesidad.

El Router no conoce el catálogo de capabilities ni el envelope: no autoriza nada.
OmniRoute, cuando exista, será un provider más registrado aquí; el Core no depende de
él ni de ningún provider concreto.

Desenlaces (P1): devuelve siempre una `ModelResponse` con `outcome` explícito —
`REAL`, `DEGRADED` (contingencia determinista permitida) o `UNAVAILABLE` (no hay
provider utilizable). No lanza excepciones por falta de provider: informa.
"""

import asyncio
import time

from alexis.models.provider import (
    ModelOutcome,
    ModelProvider,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelTask,
)

#: Niveles de privacidad en orden creciente.
_PRIVACY_RANK = {"normal": 0, "sensitive": 1, "secret": 2}


def _privacy_allows(provider_max: str, requested: str) -> bool:
    return _PRIVACY_RANK.get(provider_max, 0) >= _PRIVACY_RANK.get(requested, 0)


def _estimate_cost(provider: ModelProvider, request: ModelRequest) -> float:
    """Coste estimado (USD) de una petición: tokens entrada+salida aproximados.

    Es una estimación honesta y conservadora: sin datos de tokenizer, se asume que la
    entrada+y la salida suman ~2x `max_tokens`."""
    if provider.cost_per_1k_tokens <= 0:
        return 0.0
    return (provider.cost_per_1k_tokens / 1000.0) * (request.max_tokens * 2)


class ModelRouter:
    """Enruta peticiones cognitivas al provider más adecuado, con fallback y presupuesto."""

    def __init__(
        self,
        providers: list[ModelProvider] | None = None,
        *,
        allow_degraded: bool = True,
        budget_usd: float = 0.0,
        event_bus=None,
    ):
        self._providers: dict[str, ModelProvider] = {}
        self.allow_degraded = allow_degraded
        self.budget_usd = budget_usd  # 0 = sin límite
        self.spent_usd = 0.0
        self.event_bus = event_bus
        #: Últimas rutas (introspección/tests sin event bus).
        self.routings: list[dict] = []
        for provider in providers or []:
            self.register(provider)

    # ------------------------------------------------------------------ #
    # Registro
    # ------------------------------------------------------------------ #

    def register(self, provider: ModelProvider) -> None:
        self._providers[provider.id] = provider

    def unregister(self, provider_id: str) -> None:
        self._providers.pop(provider_id, None)

    def providers(self) -> list[ModelProvider]:
        return list(self._providers.values())

    def describe(self) -> list[dict]:
        return [p.describe() for p in self._providers.values()]

    def reset_budget(self, budget_usd: float) -> None:
        """Reinicia el presupuesto (por turno/misión: p.ej. `envelope.max_cost_usd`)."""
        self.budget_usd = budget_usd
        self.spent_usd = 0.0

    # ------------------------------------------------------------------ #
    # Selección de candidatos
    # ------------------------------------------------------------------ #

    def _eligible(self, provider: ModelProvider, request: ModelRequest) -> bool:
        if not provider.available:
            return False
        if request.task not in provider.supports:
            return False
        if not _privacy_allows(provider.privacy_max, request.privacy):
            return False
        return True

    def _within_budget(self, provider: ModelProvider, request: ModelRequest) -> bool:
        if self.budget_usd <= 0 and request.max_cost_usd <= 0:
            return True
        limit = min(x for x in (self.budget_usd, request.max_cost_usd) if x > 0)
        return self.spent_usd + _estimate_cost(provider, request) <= limit

    def candidates(self, request: ModelRequest) -> list[ModelProvider]:
        """Providers reales usables para la petición, en orden determinista.

        Los providers de contingencia (`degraded=True`) quedan **fuera** de la cadena:
        sólo se usan después, vía `_degraded_provider()`, para que `DEGRADED` sea
        siempre un desenlace explícito y no un candidato más (P1).

        Orden: providers que caben en el deadline primero; luego `priority`, coste y
        latencia. Un provider que no cabe en el deadline no se descarta (se intenta al
        final) pero nunca se promete que lo cumpla.
        """
        eligible = [
            p
            for p in self._providers.values()
            if not p.degraded and self._eligible(p, request)
        ]
        affordable = [p for p in eligible if self._within_budget(p, request)]
        pool = affordable or [p for p in eligible if not p.cost_per_1k_tokens]
        in_deadline = [p for p in pool if p.latency_p50_ms <= request.deadline_ms]
        late = [p for p in pool if p.latency_p50_ms > request.deadline_ms]
        key = lambda p: (p.priority, p.cost_per_1k_tokens, p.latency_p50_ms, p.id)  # noqa: E731
        return sorted(in_deadline, key=key) + sorted(late, key=key)

    def _degraded_provider(self) -> ModelProvider | None:
        if not self.allow_degraded:
            return None
        for provider in self._providers.values():
            if provider.degraded and provider.available:
                return provider
        return None

    # ------------------------------------------------------------------ #
    # Ejecución
    # ------------------------------------------------------------------ #

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Ejecuta la tarea cognitiva. Nunca lanza por falta de provider (P1)."""
        started = time.monotonic()
        chain: list[str] = []
        last_error: str | None = None

        for provider in self.candidates(request):
            chain.append(provider.id)
            try:
                response = await asyncio.wait_for(
                    provider.complete(request), timeout=max(request.deadline_ms, 1) / 1000.0
                )
                if response is None:
                    raise ModelProviderError(f"provider '{provider.id}' devolvió None")
                if response.error:
                    raise ModelProviderError(response.error)
                if not response.latency_ms:
                    response.latency_ms = int((time.monotonic() - started) * 1000)
                response.chain = list(chain)
                self._mark_fallback(response, chain)
                if response.fallback_used:
                    response.fallback_error = last_error
                self._account(response)
                self._audit(request, response)
                return response
            except (asyncio.TimeoutError, ModelProviderError) as exc:
                last_error = f"{provider.id}: {type(exc).__name__}: {exc}"
                continue
            except Exception as exc:  # noqa: BLE001 — un provider roto no tumba el Core
                last_error = f"{provider.id}: {type(exc).__name__}: {exc}"
                continue

        degraded = self._degraded_provider()
        if degraded is not None and request.task in degraded.supports:
            try:
                response = await degraded.complete(request)
                response.outcome = ModelOutcome.DEGRADED
                response.fallback_used = True
                response.fallback_from = chain[0] if chain else None
                response.fallback_error = last_error
                response.chain = list(chain) + [degraded.id]
                self._account(response)
                self._audit(request, response)
                return response
            except Exception as exc:  # noqa: BLE001
                last_error = f"{degraded.id}: {type(exc).__name__}: {exc}"

        response = ModelResponse(
            text="",
            data=None,
            provider="none",
            model="none",
            outcome=ModelOutcome.UNAVAILABLE,
            fallback_used=True,
            fallback_from=chain[0] if chain else None,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=last_error or f"sin provider utilizable para task={request.task.value}",
            chain=list(chain),
        )
        self._audit(request, response)
        return response

    def _mark_fallback(self, response: ModelResponse, chain: list[str]) -> None:
        """`fallback_used` es True si se intentó algún provider antes del que respondió
        (P1: la procedencia de la respuesta queda siempre explícita)."""
        if len(chain) > 1 or response.is_degraded:
            response.fallback_used = True
            response.fallback_from = chain[0] if len(chain) > 1 else None
    # ------------------------------------------------------------------ #
    # Contabilidad y auditoría
    # ------------------------------------------------------------------ #

    def _account(self, response: ModelResponse) -> None:
        self.spent_usd += max(0.0, response.cost_usd)

    def _audit(self, request: ModelRequest, response: ModelResponse) -> None:
        payload = response.audit_event(request.task)
        self.routings.append(payload)
        bus = self.event_bus
        if bus is None:
            return

        async def _publish():
            try:
                await bus.publish("model.routed", payload)
            except Exception:  # noqa: BLE001 — auditar no puede romper la cognición
                return

        task = asyncio.get_running_loop().create_task(_publish())
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


def build_router_from_config(config, event_bus=None) -> ModelRouter:
    """Construye un router a partir de `alexis.models.config.ModelConfig` (F2.1)."""
    router = ModelRouter(
        allow_degraded=config.allow_degraded,
        budget_usd=config.budget_usd,
        event_bus=event_bus,
    )
    for provider in config.build_providers():
        router.register(provider)
    return router


__all__ = [
    "ModelRouter",
    "ModelOutcome",
    "ModelProvider",
    "ModelProviderError",
    "ModelRequest",
    "ModelResponse",
    "ModelTask",
    "build_router_from_config",
]
