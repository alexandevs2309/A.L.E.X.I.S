"""Provider HTTP local (Ollama / vLLM / llama.cpp server) — F2.1.

Es un *adapter*: el Core no lo conoce. Cambiar de modelo local a cloud, o añadir otro
adapter, es configuración (P2).
"""

import os

from alexis.models.provider import ModelTask
from alexis.models.providers.http_base import HTTPChatProvider


class LocalHTTPProvider(HTTPChatProvider):
    """Modelo local servido por HTTP. Privacidad `secret`: no sale de la máquina."""

    id = "local_http"
    supports = set(ModelTask)
    priority = 20  # preferido: local antes que cloud
    privacy_max = "secret"
    dialect = "ollama"

    def __init__(self, base_url: str | None = None, model: str | None = None, **kwargs):
        super().__init__(
            base_url=base_url or os.environ.get("ALEXIS_MODEL_BASE_URL", ""),
            model=model or os.environ.get("ALEXIS_MODEL_NAME", "qwen2.5-coder"),
            **kwargs,
        )
        self.available = bool(self.base_url)
