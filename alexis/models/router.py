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

from typing import TYPE_CHECKING

from alexis.models.provider import (
    ModelOutcome,
    ModelProvider,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelTask,
)

if TYPE_CHECKING:  # pragma: no cover — sólo para el anotador de complete()
    from alexis.models.correlation import RoutingCorrelation

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
        # CORE-06: gasto acumulado POR CORRELACIÓN (cada misión lleva la suya). El
        # router es un singleton de proceso compartido por el clasificador de
        # intención, el runtime cognitivo y el planner, así que un contador único
        # convertiría el presupuesto de cada misión en un presupuesto global de
        # ALEXIS. La clave es la de `RoutingCorrelation` (misión o pre-misión), nunca
        # un contador compartido. `spent_usd` sigue siendo el total del proceso: no
        # cambia de semántica.
        self._spent_by_correlation: dict[str, float] = {}
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
        """Reinicia el presupuesto GLOBAL del proceso.

        No debe usarse para el presupuesto de una misión: eso convertiría el límite de
        una misión en el de todo ALEXIS y borraría el gasto de las demás. Para el
        presupuesto por misión, cada petición lleva su `max_cost_usd` y su gasto se
        contabiliza por correlación.
        """
        self.budget_usd = budget_usd
        self.spent_usd = 0.0
        self._spent_by_correlation.clear()

    def spent_usd_for(self, correlation_key: str | None) -> float:
        """Gasto acumulado de UNA correlación (misión). 0.0 si no ha gastado.

        Las llamadas sin correlación (pre-misión) cuelgan de su propia clave, así que no
        se imputan a ninguna misión posterior.
        """
        return self._spent_by_correlation.get(correlation_key or "", 0.0)

    @staticmethod
    def _budget_key(correlation) -> str:
        """Clave de accumulación de una petición.

        La correlación manda: es lo que aísla el presupuesto de cada misión. Sin ella
        (llamadas que no la aportan) se usa una clave propia, para que ese gasto no
        termine descontándose de la primera misión que pase.
        """
        if correlation is None:
            return ""
        return str(correlation.correlation_id or "")

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

    def _within_budget(
        self,
        provider: ModelProvider,
        request: ModelRequest,
        *,
        spent: float = 0.0,
    ) -> bool:
        """¿Cabe este provider en lo que QUEDA de presupuesto?

        `spent` es lo ya gastado por la MISMA correlación (la misión), no el total del
        proceso. Es lo que hace que el límite sea por misión: con el total global, una
        misión que ya gastó dejaría sin presupuesto a la siguiente aunque ésta tuviese
        el suyo entero.

        `max_cost_usd == 0` sigue significando "sin límite", tanto en la petición como
        en el global del operador; si ambos son 0, no hay límite.
        """
        if self.budget_usd <= 0 and request.max_cost_usd <= 0:
            return True
        limit = min(x for x in (self.budget_usd, request.max_cost_usd) if x > 0)
        return spent + _estimate_cost(provider, request) <= limit

    def candidates(
        self,
        request: ModelRequest,
        *,
        correlation: "RoutingCorrelation | None" = None,
    ) -> list[ModelProvider]:
        """Providers reales usables para la petición, en orden determinista.

        Los providers de contingencia (`degraded=True`) quedan **fuera** de la cadena:
        sólo se usan después, vía `_degraded_provider()`, para que `DEGRADED` sea
        siempre un desenlace explícito y no un candidato más (P1).

        El presupuesto se evalúa contra lo gastado por **esta** correlación. Si ningún
        candidato cabe, se cae a los providers sin coste por token —que no cuestan nada
        y por tanto nunca exceden el límite— en vez de a los caros: el presupuesto
        acotado no puede saltarse por un `or`.
        """
        spent = self.spent_usd_for(self._budget_key(correlation))
        eligible = [
            p
            for p in self._providers.values()
            if not p.degraded and self._eligible(p, request)
        ]
        affordable = [p for p in eligible if self._within_budget(p, request, spent=spent)]
        pool = affordable or [p for p in eligible if not p.cost_per_1k_tokens]
        # "Caben" = su deadline efectivo (propio o global) cubre su latencia típica.
        in_deadline = [p for p in pool if p.latency_p50_ms <= p.effective_deadline_ms(request)]
        late = [p for p in pool if p.latency_p50_ms > p.effective_deadline_ms(request)]
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

    async def complete(
        self,
        request: ModelRequest,
        *,
        correlation: "RoutingCorrelation | None" = None,
    ) -> ModelResponse:
        """Ejecuta la tarea cognitiva. Nunca lanza por falta de provider (P1).

        `correlation` es opcional y keyword-only a propósito: los llamadores que no
        saben nada de misiones siguen funcionando igual (`complete(request)`), y los
        que sí pasan un contexto genérico de correlación. El Router no lo interpreta:
        lo adjunta al evento `model.routed`. Para que eso sea seguro, la correlación
        se aplica DENTRO de `_audit()`, antes de publicar; mutar el payload después
        sería una carrera, porque la publicación es fire-and-forget.
        """
        started = time.monotonic()
        chain: list[str] = []
        last_error: str | None = None

        for provider in self.candidates(request, correlation=correlation):
            chain.append(provider.id)
            try:
                response = await asyncio.wait_for(
                    provider.complete(request),
                    timeout=provider.effective_deadline_ms(request) / 1000.0,
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
                self._account(response, correlation=correlation)
                self._audit(request, response, correlation=correlation)
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
                # La contingencia es instantánea, pero la CADENA no lo fue: reportar 0 ms
                # escondería que se esperó el deadline de cada provider anterior. La
                # latencia que se audita es la de la decisión completa.
                response.latency_ms = int((time.monotonic() - started) * 1000)
                self._account(response, correlation=correlation)
                self._audit(request, response, correlation=correlation)
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
        self._audit(request, response, correlation=correlation)
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

    def _account(self, response: ModelResponse, *, correlation=None) -> None:
        """Contabilidad doble: el total del proceso y el de esta correlación.

        El coste real (`ModelResponse.cost_usd`), nunca una estimación: el
        presupuesto debe medir lo que se pagó, no lo que se previó.
        """
        coste = max(0.0, response.cost_usd)
        self.spent_usd += coste
        self._spent_by_correlation[self._budget_key(correlation)] = (
            self._spent_by_correlation.get(self._budget_key(correlation), 0.0) + coste
        )

    def _audit(
        self,
        request: ModelRequest,
        response: ModelResponse,
        *,
        correlation: "RoutingCorrelation | None" = None,
    ) -> None:
        payload = response.audit_event(request.task)
        # CORE-05: la correlación se adjunta AQUÍ, dentro de la unidad que va a publicar.
        # Hacerlo después sería una carrera: `_publish()` corre en su propia task y puede
        # haber entregado el payload a las colas antes de que nadie lo toque. Además es
        # el último punto donde el Router ve el evento, así que no necesita saber qué
        # significa `mission_id`: sólo lo copia.
        if correlation is not None:
            payload["correlation"] = correlation.to_dict()
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
    # La contingencia también se registra aquí. Antes sólo lo hacía `apps/demo/server.py`,
    # a mano: quien usara este constructor se quedaba sin fallback determinista y acababa
    # en UNAVAILABLE aunque `allow_degraded` estuviera activo. Dos caminos de construcción
    # tienen que dar el mismo router.
    degraded = config.build_degraded()
    if degraded is not None:
        router.register(degraded)
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
