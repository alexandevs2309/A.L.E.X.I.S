"""OpenRouter: un único endpoint con muchos modelos (F2.1).

## Estado: NO PROBADO — sin credenciales

Este provider se evaluó y se dejó implementado, pero **no se ha probado contra la API de
inferencia**: no hay clave de OpenRouter en el entorno ni en `secrets/`. Lo que sí se
verificó es que el endpoint público de modelos responde (HTTP 200, 458 modelos) y que la
API es OpenAI-compatible. Cualquier afirmación sobre su comportamiento en inferencia sería
suponer, así que `docs/MODEL-PROVIDERS.md` lo marca NOT_CONFIGURED, no AVAILABLE.

## Lo que dice la documentación oficial

- Base: `https://openrouter.ai/api/v1`, chat en `POST /v1/chat/completions`.
- Autenticación: `Authorization: Bearer <key>` — la misma que ya usa `build_headers()`.
- Variante gratuita: sufijo `:free` en el slug (p. ej. `qwen/qwen3-8b:free`).
- **`openrouter/free`**: un router que elige entre los modelos gratuitos disponibles.
- Límites del Free Tier (documentados): **20 peticiones/min** y **50 peticiones/día**; la
  cuota diaria sube a 1.000/día al comprar al menos 10 créditos. La compra de créditos
  NO es necesaria para usar los modelos `:free`.
- Rate limits por clave consultables en `GET /api/v1/key`; las respuestas de error
  incluyen cabeceras `X-RateLimit-*`.

Con 50 peticiones/día, este provider es complementar de Gemini, no su sustituto: encaja como
respaldo cuando la cuota de Gemini se agota, no como vía principal.

## Implementación

Subclase de `OpenAICompatibleProvider`: el formato es idéntico al de OpenAI, así que no
hay lógica nueva. Sólo los defaults propios: URL, modelo y variable de entorno.

Lo único que se añade son las cabeceras de atribución opcionales que OpenRouter sugiere
(`HTTP-Referer`, `X-Title`). Si no se definen, el provider sigue funcionando.
"""

import os

from alexis.models.provider import ModelTask
from alexis.models.providers.openai_compatible import OpenAICompatibleProvider

OPENROUTER_BASE_URL = "https://openrouter.ai/api"

#: `openrouter/free` enruta a los modelos gratuitos disponibles en el momento. Es la
#: opción de coste cero y la que no exige saber qué slug está libre hoy, que es justo el
#: problema de los free tiers: la lista rota.
OPENROUTER_DEFAULT_MODEL = "openrouter/free"

OPENROUTER_KEY_ENV = "ALEXIS_OPENROUTER_API_KEY"


class OpenRouterProvider(OpenAICompatibleProvider):
    id = "openrouter"
    supports = set(ModelTask)
    priority = 30  # segundo en la cadena por defecto: después de Gemini, antes del local
    cost_per_1k_tokens = 0.0
    latency_p50_ms = 3000
    privacy_max = "sensitive"
    dialect = "openai"

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        **kwargs,
    ):
        super().__init__(
            #: `/api` a secas: `HTTPChatProvider.endpoint` añade `/v1/chat/completions` en
            #: dialecto openai, que es justo la ruta de OpenRouter. Poner `/api/v1`
            #: aquí produciría `/api/v1/v1/...`.
            base_url=base_url or OPENROUTER_BASE_URL,
            model=model or OPENROUTER_DEFAULT_MODEL,
            api_key=(
                api_key
                or os.environ.get(OPENROUTER_KEY_ENV)
                or os.environ.get("ALEXIS_MODEL_API_KEY")
                or None
            ),
            **kwargs,
        )
        self.available = bool(self.base_url) and bool(self.api_key)
        #: Atribución opcional. OpenRouter la recomienda para aparecer en sus leaderboards
        #: y avisa de "your app" si no se manda nada. Nunca es obligatoria.
        self.referer = os.environ.get("ALEXIS_OPENROUTER_REFERER", "")
        self.title = os.environ.get("ALEXIS_OPENROUTER_TITLE", "ALEXIS")

    def build_headers(self) -> dict:
        headers = super().build_headers()
        if self.referer:
            headers["HTTP-Referer"] = self.referer
        if self.title:
            headers["X-Title"] = self.title
        return headers


__all__ = [
    "OpenRouterProvider",
    "OPENROUTER_BASE_URL",
    "OPENROUTER_DEFAULT_MODEL",
    "OPENROUTER_KEY_ENV",
]
