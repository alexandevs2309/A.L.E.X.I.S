"""Model Provider: el contrato intercambiable de cognición real (F2.1).

Este módulo define QUÉ necesita el Core (una `ModelTask`) y CÓMO se declara un
proveedor. No decide qué hacer (eso es el Cognitive Core) ni qué puede hacer (eso es
Policy/Envelope): solo hace posible la llamada a un modelo.

Invariante P1 (docs/COGNITIVE-CORE-F2.md §1.3): la procedencia del razonamiento es un
tri-estado explícito — `REAL` (modelo real), `DEGRADED` (contingencia determinista) o
`UNAVAILABLE` (no hay provider utilizable). Nunca se degrada en silencio.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ModelTask(str, Enum):
    UNDERSTAND = "understand"  # comprensión de intención
    ANALYZE = "analyze"  # análisis de contexto/evidencia
    REASON = "reason"  # razonar sobre opciones
    SELECT = "select"  # proponer capabilities
    PLAN = "plan"  # planificación dinámica
    SYNTHESIZE = "synthesize"  # respuesta natural
    CRITIQUE = "critique"  # verificar conclusiones
    REFLECT = "reflect"  # lección/reflexión


class ModelOutcome(str, Enum):
    REAL = "real"  # model output real de un provider real
    DEGRADED = "degraded"  # contingencia determinista (DegradedModel)
    UNAVAILABLE = "unavailable"  # no hay provider utilizable


@dataclass
class ModelRequest:
    task: ModelTask
    system: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    schema: dict[str, Any] | None = None
    max_tokens: int = 1500
    temperature: float = 0.2
    privacy: str = "normal"  # normal | sensitive | secret
    max_cost_usd: float = 0.0
    deadline_ms: int = 30000

    @classmethod
    def simple(cls, task: ModelTask, prompt: str, **kwargs: Any) -> "ModelRequest":
        """Atajo para una petición de un solo prompt de usuario."""
        return cls(task=task, messages=[{"role": "user", "content": prompt}], **kwargs)


@dataclass
class ModelResponse:
    text: str
    data: dict[str, Any] | None = None
    provider: str = "unknown"
    model: str = "unknown"
    outcome: ModelOutcome = ModelOutcome.REAL
    fallback_used: bool = False
    fallback_from: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    error: str | None = None
    #: Motivo por el que se recurrrió al fallback (P1: la auditoría debe explicar *por
    #: qué* una respuesta vino de otro provider, no sólo *que* vino de otro).
    fallback_error: str | None = None
    chain: list[str] = field(default_factory=list)

    @property
    def is_real(self) -> bool:
        return self.outcome is ModelOutcome.REAL

    @property
    def is_degraded(self) -> bool:
        return self.outcome is ModelOutcome.DEGRADED

    @property
    def is_unavailable(self) -> bool:
        return self.outcome is ModelOutcome.UNAVAILABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "provider": self.provider,
            "model": self.model,
            "outcome": self.outcome.value,
            "fallback_used": self.fallback_used,
            "fallback_from": self.fallback_from,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "chain": list(self.chain),
        }

    def audit_event(self, task: ModelTask) -> dict[str, Any]:
        """Payload de `model.routed` (P1: outcome siempre explícito)."""
        return {
            "task": task.value,
            "provider": self.provider,
            "model": self.model,
            "outcome": self.outcome.value,
            "fallback_used": self.fallback_used,
            "fallback_from": self.fallback_from,
            "fallback_error": self.fallback_error,
            "chain": list(self.chain),
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
            "error": self.error,
        }


class ModelProviderError(RuntimeError):
    """Fallo de un provider concreto; el Router lo usa para encadenar fallback."""


class ModelProviderUnavailable(ModelProviderError):
    """El provider no está disponible (no registrado, desactivado o sin endpoint)."""


class ModelProvider(ABC):
    """Proveedor de modelo intercambiable.

    `available=False` declara honestamente que el provider no puede usarse ahora; el
    Router lo ignora en lugar de fingir (misma regla que las capabilities `missing`).
    `degraded=True` marca los providers de contingencia determinista, cuyos
    resultados NUNCA se presentan como razonamiento real (P1).
    """

    id: str = "provider"
    supports: set[ModelTask] = set()
    priority: int = 100  # menor = preferido
    cost_per_1k_tokens: float = 0.0
    latency_p50_ms: int = 1000
    privacy_max: str = "normal"  # normal < sensitive < secret
    available: bool = True
    degraded: bool = False

    @abstractmethod
    async def complete(self, request: ModelRequest) -> ModelResponse: ...

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "available": self.available,
            "degraded": self.degraded,
            "priority": self.priority,
            "supports": sorted(t.value for t in self.supports),
            "cost_per_1k_tokens": self.cost_per_1k_tokens,
            "latency_p50_ms": self.latency_p50_ms,
            "privacy_max": self.privacy_max,
        }
