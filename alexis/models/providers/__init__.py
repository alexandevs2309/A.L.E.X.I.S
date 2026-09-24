"""Providers de modelo (F2.1). Todos son adapters: el Core no depende de ninguno."""

from alexis.models.providers.local_http import LocalHTTPProvider
from alexis.models.providers.omniroute import OmniRouteProvider
from alexis.models.providers.openai_compatible import OpenAICompatibleProvider

__all__ = ["LocalHTTPProvider", "OmniRouteProvider", "OpenAICompatibleProvider"]
