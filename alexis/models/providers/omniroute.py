"""Adapter preparado para OmniRoute — F2.1. **No implementado, y honesto sobre ello.**

Igual que la capability `omniroute.run` (estado `missing` en
`alexis/capabilities/catalog.py`), este adapter declara `available=False` hasta que
exista un endpoint real. El Router lo ignora; si alguien lo invoca directamente, falla
con un error explícito en vez de fingir.

Regla P2: OmniRoute es un **provider más**, no el cerebro de ALEXIS. El Cognitive Core
no lo referencia y no depende de él.
"""

from alexis.models.provider import (
    ModelOutcome,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelTask,
)
from alexis.models.providers.openai_compatible import OpenAICompatibleProvider


class OmniRouteProvider(OpenAICompatibleProvider):
    """Adapter OmniRoute. Requiere endpoint real; hasta entonces `available=False`."""

    id = "omniroute"
    supports = set(ModelTask)
    priority = 40
    privacy_max = "sensitive"
    dialect = "openai"

    def __init__(self, base_url: str | None = None, model: str | None = None, **kwargs):
        super().__init__(base_url=base_url, model=model or "omniroute/default", **kwargs)
        # Honesto: sin endpoint no está disponible (no se "degrada" en silencio).
        self.available = bool(self.base_url)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self.available:
            return ModelResponse(
                text="",
                provider=self.id,
                model=self.model,
                outcome=ModelOutcome.UNAVAILABLE,
                error="OmniRoute adapter no configurado (sin endpoint); capability omniroute.run sigue missing",
            )
        return await super().complete(request)


__all__ = ["OmniRouteProvider", "ModelProviderError"]
