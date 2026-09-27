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
KNOWN_PROVIDERS = (
    "local_http",
    "openai_compatible",
    "gemini",
    "openrouter",
    "omniroute",
    "none",
)

#: Providers declarables en la cadena `ALEXIS_MODEL_PROVIDERS`, con la clase que los
#: implementa. Declarar la cadena completa es preferible a configurar uno a uno: el orden
#: ES la prioridad, y un orden explícito en una variable se lee mejor que una suma de
#: prioridades sueltas.
#: `nombre -> (módulo, clase, variable de entorno de la clave)`.
#:
#: El nombre de la clave se declara AQUÍ y no en cada provider, para que el config y el
#: provider no puedan acabar usando nombres distintos. `ALEXIS_GEMINI_API_KEY` es el que
#: usan tanto esta tabla como `gemini.py`.
CHAIN_PROVIDERS = {
    "gemini": ("alexis.models.providers.gemini", "GeminiProvider", "ALEXIS_GEMINI_API_KEY"),
    "openrouter": (
        "alexis.models.providers.openrouter",
        "OpenRouterProvider",
        "ALEXIS_OPENROUTER_API_KEY",
    ),
    "local_http": (
        "alexis.models.providers.local_http",
        "LocalHTTPProvider",
        "ALEXIS_MODEL_API_KEY",
    ),
    "openai_compatible": (
        "alexis.models.providers.openai_compatible",
        "OpenAICompatibleProvider",
        "ALEXIS_MODEL_API_KEY",
    ),
    "omniroute": (
        "alexis.models.providers.omniroute",
        "OmniRouteProvider",
        "ALEXIS_MODEL_API_KEY",
    ),
}


def _bool_env(name: str, default: bool, source: dict | None = None) -> bool:
    get = (source if source is not None else os.environ).get
    raw = (get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _float_env(name: str, default: float, source: dict | None = None) -> float:
    get = (source if source is not None else os.environ).get
    raw = (get(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _int_env(name: str, default: int, source: dict | None = None) -> int:
    get = (source if source is not None else os.environ).get
    raw = (get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _provider_deadlines(get) -> dict:
    """Deadlines por provider desde `ALEXIS_MODEL_DEADLINE_<PROVIDER>_MS`.

    Un patrón, no una variable por proveedor: añadir un provider nuevo no requiere
    inventar otra constante, y las que no se declaren usan el deadline global. Los
    providers que se prueban de verdad son los que necesitan uno propio (una nube que
    responde en 1s no debe heredar el presupuesto generoso de un Ollama de 40s).
    """
    out: dict[str, int] = {}
    for name in CHAIN_PROVIDERS:
        raw = (get(f"ALEXIS_MODEL_DEADLINE_{name.upper()}_MS") or "").strip()
        if raw:
            try:
                out[name] = int(raw)
            except ValueError:
                continue
    return out


@dataclass
class ModelConfig:
    """Configuración de la capa de modelos, leída del entorno."""

    provider: str = "none"
    base_url: str = ""
    model_name: str = ""
    dialect: str = "ollama"
    fallback: str = "degraded"  # degraded | none
    deadline_ms: int = 60000
    timeout_s: float = 60.0
    #: Tope de tokens por respuesta. Los modelos "de razonamiento" (p. ej. Gemini 3.x)
    #: consumen parte del presupuesto en pensar antes de emitir texto: con topes bajos la
    #: respuesta llega vacía o truncada (`finish_reason: length`) y el Core la descarta
    #: como no utilizable, aunque la decisión fuese correcta. Medido: ~360 tokens de
    #: thinking + ~115 de JSON. 2048 deja margen sin inventar gasto ilimitado.
    max_tokens: int = 2048
    budget_usd: float = 0.0
    cost_per_1k_tokens: float = 0.0
    priority: int = 50
    extra_providers: list[str] = field(default_factory=list)
    #: Ruta de chat personalizada del provider principal (p. ej. la API
    #: OpenAI-compat de Gemini usa `/v1beta/openai/chat/completions`).
    endpoint: str = ""
    #: Endpoint/modelo independientes para el provider extra (p. ej. Ollama local),
    #: para poder tener Gemini (cloud) primario y Ollama local de respaldo.
    extra_base_url: str = ""
    extra_model: str = ""
    #: Cadena ordenada de providers a intentar, de más a menos preferido. El orden ES la
    #: prioridad. Vacía = usar el provider principal de siempre.
    chain: list[str] = field(default_factory=list)
    #: Entorno del que se leyó este config. No es parte de la semántica: existe para que
    #: `build_providers()` use la MISMA fuente que `from_env()` y no vuelva a leer
    #: `os.environ`. Sin esto, `ModelConfig.from_env(d)` + `build_providers()`Construían
    #: providers con claves de un sitio y el resto del config con otro.
    _env_source: dict | None = None
    #: Deadline por provider, en ms, indexado por nombre. Se combina con el global
    #: tomando el mínimo (ver `ModelProvider.effective_deadline_ms`).
    provider_deadlines: dict = field(default_factory=dict)

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
            deadline_ms=_int_env("ALEXIS_MODEL_DEADLINE_MS", 60000, source),
            timeout_s=_float_env("ALEXIS_MODEL_TIMEOUT_S", 60.0, source),
            max_tokens=_int_env("ALEXIS_MODEL_MAX_TOKENS", 2048, source),
            budget_usd=_float_env("ALEXIS_MODEL_BUDGET_USD", 0.0, source),
            cost_per_1k_tokens=_float_env("ALEXIS_MODEL_COST_PER_1K", 0.0, source),
            priority=_int_env("ALEXIS_MODEL_PRIORITY", 50, source),
            extra_providers=[
                p.strip() for p in (get("ALEXIS_MODEL_EXTRA_PROVIDERS") or "").split(",") if p.strip()
            ],
            endpoint=(get("ALEXIS_MODEL_ENDPOINT") or "").strip(),
            extra_base_url=(get("ALEXIS_MODEL_EXTRA_BASE_URL") or "").strip(),
            extra_model=(get("ALEXIS_MODEL_EXTRA_MODEL") or "").strip(),
            chain=[
                p.strip() for p in (get("ALEXIS_MODEL_PROVIDERS") or "").split(",") if p.strip()
            ],
            provider_deadlines=_provider_deadlines(get),
            _env_source=dict(source),
        )

    def allow_degraded(self) -> bool:
        return self.fallback == "degraded"

    def _build_one(self, name: str, is_primary: bool, source: dict | None = None):
        """Construye UN provider por nombre. `None` si el nombre no existe.

        Cada provider recibe SU clave y SU URL. Antes sólo `local_http` tenía
        `extra_base_url`, así que un segundo cloud en la cadena no tenía forma de
        declararse: quedaba apuntando al `base_url` del primero.
        """
        entry = CHAIN_PROVIDERS.get(name)
        if entry is None:
            return None
        module_path, class_name, key_env = entry
        module = __import__(module_path, fromlist=[class_name])
        cls = getattr(module, class_name)

        env = source if source is not None else (self._env_source or os.environ)
        key_prefix = name.upper()
        # Orden de la URL: la específica del provider, luego la del provider extra (que
        # es como se declaraba Ollama local), y por último la global. Si ninguna existe,
        # la clase usa su propio default (Gemini y OpenRouter lo traen).
        base_url = (env.get(f"ALEXIS_MODEL_{key_prefix}_BASE_URL") or "").strip()
        if not base_url and name == "local_http":
            # `extra_*` es el canal legacy que se usaba para Ollama local. Aplicarlo a
            # cualquier provider hacía que un cloud acabara apuntando a `127.0.0.1:11434`.
            base_url = (env.get("ALEXIS_MODEL_EXTRA_BASE_URL") or "").strip()
        if not base_url:
            base_url = (self.base_url or "").strip()
        # La clave del provider tiene prioridad; la genérica sirve para no romper configs
        # que ya funcionaban (p. ej. secrets/gemini.env con ALEXIS_MODEL_API_KEY).
        api_key = (env.get(key_env) or "").strip() or None
        if not api_key and key_env != "ALEXIS_MODEL_API_KEY":
            # Compatibilidad: una config que ya funcionaba con la clave genérica sigue
            # valiendo, para no obligar a migrar `secrets/gemini.env`.
            api_key = (env.get("ALEXIS_MODEL_API_KEY") or "").strip() or None
        model = (env.get(f"ALEXIS_MODEL_{key_prefix}_NAME") or "").strip()
        if not model:
            model = (self.extra_model if name == "local_http" else self.model_name) or None
        kwargs = {"base_url": base_url, "api_key": api_key, "model": model}
        if name == "local_http":
            kwargs.pop("api_key", None)
            kwargs["timeout"] = self.timeout_s
        if name == "openai_compatible":
            kwargs["cost_per_1k_tokens"] = self.cost_per_1k_tokens
            kwargs["timeout"] = self.timeout_s
            if self.endpoint:
                kwargs["endpoint"] = self.endpoint
        return cls(**kwargs)

    def build_providers(self, env: dict | None = None) -> list[ModelProvider]:
        """Providers declarados por configuración (sin incluir la contingencia).

        Con `ALEXIS_MODEL_PROVIDERS` la cadena es explícita y su orden es la prioridad.
        Sin ella, se comporta como hasta ahora: el provider principal y sus extras.
        """
        providers: list[ModelProvider] = []
        if self.chain:
            names = list(dict.fromkeys(self.chain))
        else:
            names = [self.provider] + [p for p in self.extra_providers if p != self.provider]
        for index, name in enumerate(names):
            if name in (None, "", "none"):
                continue
            provider = self._build_one(name, is_primary=(index == 0), source=env)
            if provider is None:
                continue
            provider.priority = index * 10
            deadline = self.provider_deadlines.get(name)
            if deadline is not None:
                provider.deadline_ms = deadline
            if self.dialect in ("ollama", "openai") and name not in ("gemini", "openrouter"):
                provider.dialect = self.dialect
            providers.append(provider)
        return providers

    def build_degraded(self) -> EchoModel | None:
        if not self.allow_degraded():
            return None
        return EchoModel()
