"""Configuración de la capa de modelos (F2.1).

El provider se elige por entorno, sin tocar el Core (P2). Si no hay provider real
configurado, el router queda con contingencia determinista marcada como `DEGRADED`; si
la contingencia también está desactivada, el router devuelve `UNAVAILABLE`.
"""

import os
from dataclasses import dataclass, field

from alexis.models.degraded import EchoModel
from alexis.models.provider import ModelProvider

#: Nombres aceptados en `ALEXIS_MODEL_PROVIDER`.
KNOWN_PROVIDERS = ("local_http", "openai_compatible", "omniroute", "none")


def _bool_env(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _float_env(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass
class ModelConfig:
    """Configuración de la capa de modelos, leída del entorno."""

    provider: str = "none"
    base_url: str = ""
    model_name: str = ""
    dialect: str = "ollama"
    fallback: str = "degraded"  # degraded | none
    deadline_ms: int = 30000
    timeout_s: float = 60.0
    budget_usd: float = 0.0
    cost_per_1k_tokens: float = 0.0
    priority: int = 50
    extra_providers: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "ModelConfig":
        source = env if env is not None else os.environ
        get = source.get
        provider = (get("ALEXIS_MODEL_PROVIDER") or "none").strip()
        if provider not in KNOWN_PROVIDERS:
            provider = "none"
        fallback = (get("ALEXIS_MODEL_FALLBACK") or "degraded").strip().lower()
        if fallback not in ("degraded", "none"):
            fallback = "degraded"
        return cls(
            provider=provider,
            base_url=(get("ALEXIS_MODEL_BASE_URL") or "").strip(),
            model_name=(get("ALEXIS_MODEL_NAME") or "").strip(),
            dialect=(get("ALEXIS_MODEL_DIALECT") or "ollama").strip().lower(),
            fallback=fallback,
            deadline_ms=_int_env("ALEXIS_MODEL_DEADLINE_MS", 30000),
            timeout_s=_float_env("ALEXIS_MODEL_TIMEOUT_S", 60.0),
            budget_usd=_float_env("ALEXIS_MODEL_BUDGET_USD", 0.0),
            cost_per_1k_tokens=_float_env("ALEXIS_MODEL_COST_PER_1K", 0.0),
            priority=_int_env("ALEXIS_MODEL_PRIORITY", 50),
            extra_providers=[
                p.strip() for p in (get("ALEXIS_MODEL_EXTRA_PROVIDERS") or "").split(",") if p.strip()
            ],
        )

    def allow_degraded(self) -> bool:
        return self.fallback == "degraded"

    def build_providers(self) -> list[ModelProvider]:
        """Providers declarados por configuración (sin incluir la contingencia)."""
        providers: list[ModelProvider] = []
        names = [self.provider] + [p for p in self.extra_providers if p != self.provider]
        for name in names:
            if name == "local_http":
                from alexis.models.providers.local_http import LocalHTTPProvider

                provider = LocalHTTPProvider(
                    base_url=self.base_url, model=self.model_name or None, timeout=self.timeout_s
                )
            elif name == "openai_compatible":
                from alexis.models.providers.openai_compatible import OpenAICompatibleProvider

                provider = OpenAICompatibleProvider(
                    base_url=self.base_url,
                    model=self.model_name or None,
                    cost_per_1k_tokens=self.cost_per_1k_tokens,
                    timeout=self.timeout_s,
                )
            elif name == "omniroute":
                from alexis.models.providers.omniroute import OmniRouteProvider

                provider = OmniRouteProvider(base_url=self.base_url, model=self.model_name or None)
            else:
                continue
            provider.priority = self.priority if name == self.provider else provider.priority + 10
            if self.dialect in ("ollama", "openai") and name != "omniroute":
                provider.dialect = self.dialect
            providers.append(provider)
        return providers

    def build_degraded(self) -> EchoModel | None:
        if not self.allow_degraded():
            return None
        return EchoModel()
