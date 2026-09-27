"""Google Gemini vía su API compatible con OpenAI (F2.1).

## Por qué una subclase y no un adapter propio

Gemini expone `POST /v1beta/openai/chat/completions` con el MISMO formato de request y
response que OpenAI. `OpenAICompatibleProvider` + `HTTPChatProvider` ya saben construir el
payload, poner el `Authorization: Bearer`, parsear `choices[0].message.content` y leer
`usage`. Escribir un cliente nativo aquí sería duplicar código que ya funciona y que
tiene sus tests. Lo único que este fichero declara son los **defaults propios del
proveedor**: URL, modelo y nombre de variable de entorno para la clave.

## Datos medidos (no supuestos)

Los modelos `gemini-2.x` devolvieron **404 "no longer available to new users"** durante la
evaluación, así que el modelo por defecto NO es uno de ellos. Se verificó la lista real
con `GET /v1beta/models` y se eligió uno vigente.

Medido contra `gemini-3.5-flash`, misma ruta que usa este provider:
- texto simple: **HTTP 200, ~1.4 s**
- con `response_format: json_schema` (que es lo que manda el Core): **HTTP 200, ~2.0 s**

Para comparar: el mismo Core con `qwen2.5:0.5b` local tardó 36-43 s por decisión en la
máquina de desarrollo (Celeron N4000, sin GPU).

## Privacidad — leer antes de enviar nada sensible

La documentación oficial de precios de Gemini marca la columna **"Used to improve our
products"** como **Yes en Free Tier** y **No en Paid Tier**. El prompt que el Core manda
contiene el Context de diez fuentes: Self Model, World Model, Memory, Mission, envelope y
Policy. Con Free Tier, ese contenido puede usarse para mejorar productos de Google.

`privacy_max` se queda en `sensitive`, que es lo que permite enviarlo, pero la decisión de
usar el Free Tier es del operador. Con `ModelRequest(privacy="secret")` este provider queda
excluido por el filtro de privacidad del router, igual que cualquier cloud.
"""

import os

from alexis.models.provider import ModelTask
from alexis.models.providers.openai_compatible import OpenAICompatibleProvider

#: Base de la API OpenAI-compatible de Gemini. El `endpoint` se completa con
#: `/chat/completions`, así que la URL final es la que se midió.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

#: Modelo por defecto. Los `gemini-2.x` se retiraron para nuevos usuarios durante esta
#: evaluación (404); `gemini-3.5-flash` estaba disponible y respondió con latencia baja.
GEMINI_DEFAULT_MODEL = "gemini-3.5-flash"

#: Variable de entorno con la clave. Se acepta también la genérica `ALEXIS_MODEL_API_KEY`
#: para no obligar a tocar la configuración que ya funcionaba con Gemini.
GEMINI_KEY_ENV = "ALEXIS_GEMINI_API_KEY"


class GeminiProvider(OpenAICompatibleProvider):
    """Gemini por su endpoint OpenAI-compatible. Hereda payload, headers y parseo."""

    id = "gemini"
    supports = set(ModelTask)
    #: Prioridad por defecto: por delante del local, porque un cloud disponible decide
    #: mucho mejor. El orden real lo fija `ALEXIS_MODEL_PROVIDERS` o `priority`.
    priority = 20
    #: El operador declara el coste si lo hay; el Free Tier es 0.
    cost_per_1k_tokens = 0.0
    latency_p50_ms = 2000
    #: El contenido sale de la máquina. `secret` queda excluido por el router.
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
            base_url=base_url or GEMINI_BASE_URL,
            model=model or GEMINI_DEFAULT_MODEL,
            api_key=(
                api_key
                or os.environ.get(GEMINI_KEY_ENV)
                or os.environ.get("ALEXIS_MODEL_API_KEY")
                or None
            ),
            **kwargs,
        )
        #: Sin clave el provider se declara NO disponible, y el router lo salta. No es un
        #: error: es "aquí no hay con quién hablar", que el router ya sabe resolver.
        self.available = bool(self.base_url) and bool(self.api_key)


__all__ = ["GeminiProvider", "GEMINI_BASE_URL", "GEMINI_DEFAULT_MODEL", "GEMINI_KEY_ENV"]
