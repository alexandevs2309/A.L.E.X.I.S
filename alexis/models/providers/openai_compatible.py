"""Provider compatible con la API de OpenAI (cloud o local) — F2.1.

La clave se lee del entorno y **nunca** se registra ni se incluye en payloads de
auditoría (`ModelResponse.audit_event` sólo expone provider/model/outcome/coste).
"""

import os

from alexis.models.provider import ModelTask
from alexis.models.providers.http_base import HTTPChatProvider


class OpenAICompatibleProvider(HTTPChatProvider):
    """Cualquier endpoint con `/v1/chat/completions` (OpenAI, Groq, Together, LM Studio…)."""

    id = "openai_compatible"
    supports = set(ModelTask)
    priority = 60  # cloud: después del local, y con coste declarado
    cost_per_1k_tokens = 0.0  # el operador lo declara si aplica
    privacy_max = "sensitive"  # el contenido sale de la máquina
    dialect = "openai"

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        cost_per_1k_tokens: float = 0.0,
        **kwargs,
    ):
        super().__init__(
            base_url=base_url or os.environ.get("ALEXIS_MODEL_BASE_URL", ""),
            model=model or os.environ.get("ALEXIS_MODEL_NAME", "gpt-4o-mini"),
            api_key=api_key or os.environ.get("ALEXIS_MODEL_API_KEY") or None,
            **kwargs,
        )
        self.cost_per_1k_tokens = cost_per_1k_tokens
        self.available = bool(self.base_url)
