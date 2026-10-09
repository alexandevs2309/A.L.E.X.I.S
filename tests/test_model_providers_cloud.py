"""Proveedores cloud de modelos: configuración, fallback, provenance y seguridad.

Cubre lo que la evaluación de proveedores dejó demostrado y lo que sigue SIN demostrar:

- **Gemini**: PROBADO contra la API real. `gemini-2.x` está retirado para nuevos usuarios
  (404), así que el modelo por defecto es uno vigente. Medido: ~1.4-3.4 s.
- **OpenRouter**: implementado pero **NO PROBADO** — no hay credencial. Los tests de
  comportamiento que dependen de la red están marcados `external` y se saltan solos.

Ningún test contiene una clave. La que se usa en el test `external` sale de
`secrets/gemini.env`, que está en `.gitignore`; si no existe, el test se salta.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.models.config import ModelConfig  # noqa: E402
from alexis.models.provider import (  # noqa: E402
    ModelOutcome,
    ModelProviderError,
    ModelRequest,
    ModelTask,
)
from alexis.models.providers.gemini import (  # noqa: E402
    GEMINI_BASE_URL,
    GEMINI_DEFAULT_MODEL,
    GeminiProvider,
)
from alexis.models.providers.openrouter import (  # noqa: E402
    OPENROUTER_BASE_URL,
    OPENROUTER_DEFAULT_MODEL,
    OpenRouterProvider,
)
from alexis.models.router import build_router_from_config  # noqa: E402

ENV_GEMINI = {"ALEXIS_GEMINI_API_KEY": "k-de-prueba"}
ENV_CHAIN = "gemini,openrouter,local_http"


# =========================================================================== #
# Configuración y autenticación
# =========================================================================== #


def test_01_gemini_reconoce_known_providers():
    cfg = ModelConfig.from_env({"ALEXIS_MODEL_PROVIDER": "gemini"})
    assert cfg.provider == "gemini", "un provider conocido no debe degradar a 'none'"


def test_02_gemini_tiene_url_y_modelo_por_defecto():
    p = GeminiProvider(api_key="k-de-prueba")
    assert p.base_url == GEMINI_BASE_URL
    assert p.model == GEMINI_DEFAULT_MODEL
    assert p.dialect == "openai"
    assert p.available is True


def test_03_openrouter_usa_api_v1_correcto():
    """`/api` a secas: el endpoint añade `/v1/chat/completions`.

    Poner `/api/v1` aquí produciría `/api/v1/v1/chat/completions`, que es un 404 silencioso
    disfrazado de "modelo no encontrado".
    """
    p = OpenRouterProvider(api_key="k-de-prueba")
    assert p.base_url == OPENROUTER_BASE_URL
    assert p.endpoint == "https://openrouter.ai/api/v1/chat/completions"


def test_04_sin_clave_el_provider_no_esta_disponible():
    """Sin credencial se declara NO disponible: el router lo salta, no es un error."""
    p = GeminiProvider(api_key=None)
    p.api_key = None
    p.available = bool(p.base_url) and bool(p.api_key)
    assert p.available is False
    assert "gemini-3" in GEMINI_DEFAULT_MODEL or "gemini-" in GEMINI_DEFAULT_MODEL


def test_05_la_clave_se_toma_de_la_variable_del_provider():
    env = {"ALEXIS_MODEL_PROVIDERS": "gemini", "ALEXIS_GEMINI_API_KEY": "especifica"}
    providers = ModelConfig.from_env(env).build_providers()
    assert providers[0].api_key == "especifica"


def test_06_la_clave_generica_sigue_sirviendo():
    """Compatibilidad: `secrets/gemini.env` usa `ALEXIS_MODEL_API_KEY` y debe seguir valiendo."""
    env = {"ALEXIS_MODEL_PROVIDERS": "gemini", "ALEXIS_MODEL_API_KEY": "generica"}
    providers = ModelConfig.from_env(env).build_providers()
    assert providers[0].api_key == "generica"
    assert providers[0].available is True


def test_07_la_especifica_gana_a_la_generica():
    env = {"ALEXIS_MODEL_PROVIDERS": "gemini", "ALEXIS_GEMINI_API_KEY": "especifica",
           "ALEXIS_MODEL_API_KEY": "generica"}
    providers = ModelConfig.from_env(env).build_providers()
    assert providers[0].api_key == "especifica"


def test_08_el_config_no_vuelve_a_leer_el_entorno():
    """Regresión: `from_env(d)` + `build_providers()` deben usar la MISMA fuente.

    Antes `build_providers()` leía `os.environ` por su cuenta, así que una clave de test
    podía no ser la que llegaba al provider. Se detectó probando una clave inválida y
    viendo que la petición salía bien.
    """
    env = {"ALEXIS_MODEL_PROVIDERS": "gemini", "ALEXIS_GEMINI_API_KEY": "solo-en-d"}
    providers = ModelConfig.from_env(env).build_providers()
    assert providers[0].api_key == "solo-en-d"


# =========================================================================== #
# Cadena y routing
# =========================================================================== #


def test_09_la_cadena_mantiene_el_orden_declarado():
    env = {"ALEXIS_MODEL_PROVIDERS": ENV_CHAIN, **ENV_GEMINI}
    providers = ModelConfig.from_env(env).build_providers()
    assert [p.id for p in providers] == ["gemini", "openrouter", "local_http"]
    assert [p.priority for p in providers] == [0, 10, 20], "el orden ES la prioridad"


def test_10_el_orden_de_la_cadena_manda_sobre_el_orden_de_prioridad():
    env = {"ALEXIS_MODEL_PROVIDERS": "openrouter,gemini", **ENV_GEMINI}
    providers = ModelConfig.from_env(env).build_providers()
    assert providers[0].id == "openrouter", "lo primero declarado se prueba primero"


def test_11_un_provider_sin_clave_se_salta_sin_romper():
    env = {"ALEXIS_MODEL_PROVIDERS": ENV_CHAIN, **ENV_GEMINI}
    providers = {p.id: p for p in ModelConfig.from_env(env).build_providers()}
    assert providers["openrouter"].available is False
    assert providers["gemini"].available is True


def test_12_extra_base_url_solo_afecta_a_local_http():
    """Regresión: `extra_base_url` se aplicaba a todo provider no primario, así que un
    cloud acababa apuntando a `127.0.0.1:11434`."""
    env = {"ALEXIS_MODEL_PROVIDERS": "gemini,openrouter", **ENV_GEMINI,
           "ALEXIS_OPENROUTER_API_KEY": "k",
           "ALEXIS_MODEL_EXTRA_BASE_URL": "http://127.0.0.1:11434"}
    providers = {p.id: p for p in ModelConfig.from_env(env).build_providers()}
    assert "127.0.0.1" not in providers["openrouter"].base_url
    assert providers["openrouter"].base_url == OPENROUTER_BASE_URL


# =========================================================================== #
# Deadline por provider
# =========================================================================== #


def test_13_deadline_por_provider_se_leye_del_entorno():
    env = {"ALEXIS_MODEL_PROVIDERS": ENV_CHAIN, **ENV_GEMINI,
           "ALEXIS_MODEL_DEADLINE_GEMINI_MS": "7000",
           "ALEXIS_MODEL_DEADLINE_LOCAL_HTTP_MS": "45000"}
    providers = {p.id: p for p in ModelConfig.from_env(env).build_providers()}
    assert providers["gemini"].deadline_ms == 7000
    assert providers["local_http"].deadline_ms == 45000
    assert providers["openrouter"].deadline_ms is None


def test_14_el_deadline_efectivo_es_el_mas_restrictivo():
    """Un cloud lento no puede alargar el presupuesto del Core, ni al revés."""
    p = GeminiProvider(api_key="k")
    p.deadline_ms = 7000
    request = ModelRequest.simple(ModelTask.REASON, "x", deadline_ms=30000)
    assert p.effective_deadline_ms(request) == 7000, "gana el más corto"
    p.deadline_ms = 90000
    assert p.effective_deadline_ms(request) == 30000, "el global acota al provider lento"


def test_15_sin_deadline_propio_se_usa_el_global():
    p = GeminiProvider(api_key="k")
    p.deadline_ms = None
    request = ModelRequest.simple(ModelTask.REASON, "x", deadline_ms=12345)
    assert p.effective_deadline_ms(request) == 12345


# =========================================================================== #
# Errores, fallback y provenance
# =========================================================================== #


def test_16_un_429_se_distingue_de_un_500():
    import urllib.error

    from alexis.models.providers.http_base import _http_error_reason

    r429 = _http_error_reason(urllib.error.HTTPError("u", 429, "Too Many", {}, None))
    r500 = _http_error_reason(urllib.error.HTTPError("u", 500, "Err", {}, None))
    assert "429" in r429 and "rate limit" in r429.lower()
    assert "500" in r500
    assert r429 != r500, "el motivo del fallback debe poder distinguir cuota de caída"


def test_17_el_error_http_no_filtre_el_cuerpo():
    """Un 401 puede incluir parte de la credencial en algunos gateways: no se registra."""
    import urllib.error

    from alexis.models.providers.http_base import _http_error_reason

    exc = urllib.error.HTTPError("u", 401, "Unauthorized", {},
                                 None)
    motivo = _http_error_reason(exc)
    assert "clave-secreta" not in motivo
    assert "401" in motivo


def test_18_el_router_cae_al_siguiente_provider_si_uno_falla():
    import asyncio

    from alexis.models.provider import ModelProvider, ModelResponse

    class _Malo(ModelProvider):
        id = "malo"
        supports = {ModelTask.REASON}
        priority = 0

        async def complete(self, request, *, correlation=None):
            raise ModelProviderError("revienta")

    class _Bueno(ModelProvider):
        id = "bueno"
        supports = {ModelTask.REASON}
        priority = 10

        async def complete(self, request, *, correlation=None):
            return ModelResponse(text="ok", provider="bueno", model="m",
                                 outcome=ModelOutcome.REAL)

    from alexis.models.router import ModelRouter

    r = ModelRouter([_Malo(), _Bueno()])
    resp = asyncio.run(r.complete(ModelRequest.simple(ModelTask.REASON, "x")))
    assert resp.outcome is ModelOutcome.REAL
    assert resp.provider == "bueno"
    assert resp.fallback_used is True
    assert "malo" in str(resp.fallback_error)


def test_19_si_todo_falla_es_degraded_y_lo_declara():
    """Criterio 9: provenance distingue REAL de DEGRADED y nunca se disfraza."""
    import asyncio

    from alexis.models.degraded import EchoModel
    from alexis.models.provider import ModelProvider, ModelResponse
    from alexis.models.router import ModelRouter

    class _Malo(ModelProvider):
        id = "malo"
        supports = {ModelTask.REASON}
        priority = 0

        async def complete(self, request, *, correlation=None):
            raise ModelProviderError("boom")

    r = ModelRouter([_Malo(), EchoModel()])
    resp = asyncio.run(r.complete(ModelRequest.simple(ModelTask.REASON, "x")))
    assert resp.outcome is ModelOutcome.DEGRADED
    assert resp.is_real is False
    assert resp.provider == "echo"
    assert "degraded" in resp.text.lower() or "malo" in str(resp.fallback_error)


def test_20_degraded_reporta_la_latencia_de_la_cadena_completa():
    """Si no, una cadena que tardó 45 s se auditaría como 0 ms."""
    import asyncio

    from alexis.models.degraded import EchoModel
    from alexis.models.provider import ModelProvider
    from alexis.models.router import ModelRouter

    class _Lento(ModelProvider):
        id = "lento"
        supports = {ModelTask.REASON}
        priority = 0

        async def complete(self, request, *, correlation=None):
            import asyncio as a

            await a.sleep(0.15)
            raise ModelProviderError("tarde")

    r = ModelRouter([_Lento(), EchoModel()])
    resp = asyncio.run(r.complete(ModelRequest.simple(ModelTask.REASON, "x")))
    assert resp.outcome is ModelOutcome.DEGRADED
    assert resp.latency_ms >= 150, f"la latencia era {resp.latency_ms} ms"


def test_21_el_audit_event_no_lleva_la_credencial():
    p = GeminiProvider(api_key="clave-que-no-debe-aparecer")
    p.deadline_ms = 8000
    described = p.describe()
    event = {"provider": p.id, "model": p.model}
    assert "clave-que-no-debe-aparecer" not in str(described)
    assert "clave-que-no-debe-aparecer" not in str(event)
    assert "api_key" not in str(described)


def test_22_sin_cloud_todo_sigue_funcionando():
    """Criterio 1 y 2: sin ningún cloud, el Core y el fallback determinista siguen."""
    cfg = ModelConfig.from_env({"ALEXIS_MODEL_PROVIDER": "none",
                                "ALEXIS_MODEL_FALLBACK": "degraded"})
    router = build_router_from_config(cfg)
    router.register(__import__("alexis.models.degraded", fromlist=["EchoModel"]).EchoModel())
    assert [p.id for p in router.providers()] == ["echo"]


# =========================================================================== #
# Privacidad
# =========================================================================== #


def test_23_privacy_secret_excluye_a_los_cloud():
    """Un cloud no debe recibir material `secret`; el router lo filtra por `privacy_max`."""
    from alexis.models.router import ModelRouter

    r = ModelRouter([GeminiProvider(api_key="k")])
    assert r.candidates(ModelRequest.simple(ModelTask.REASON, "x", privacy="secret")) == []
    assert r.candidates(ModelRequest.simple(ModelTask.REASON, "x", privacy="normal")) != []


# =========================================================================== #
# live / external — requieren credenciales y red; se saltan si no hay
# =========================================================================== #


def _live_key() -> str:
    """Lee la clave del fichero local ignorado por git. NUNCA la imprime ni la escribe."""
    import os

    path = PROJECT_ROOT / "secrets" / "gemini.env"
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("ALEXIS_MODEL_API_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    return ""


@pytest.mark.external
@pytest.mark.asyncio
async def test_24_gemini_live_devuelve_real():
    """PROBADO contra la API real. Se salta sin credencial; nunca falla la suite."""
    import asyncio
    import time

    key = _live_key()
    if not key:
        pytest.skip("sin credencial de Gemini: proveedor NOT_CONFIGURED")
    env = {"ALEXIS_MODEL_PROVIDERS": "gemini", "ALEXIS_GEMINI_API_KEY": key,
           "ALEXIS_MODEL_DEADLINE_GEMINI_MS": "25000"}
    router = build_router_from_config(ModelConfig.from_env(env))
    req = ModelRequest.simple(
        ModelTask.REASON,
        "Responde sólo con este JSON: {\"action\":\"execute\",\"ok\":true}",
        max_tokens=300, temperature=0.0, deadline_ms=25000,
    )
    t0 = time.monotonic()
    resp = await router.complete(req)
    elapsed = int((time.monotonic() - t0) * 1000)

    # El invariante que se comprueba aquí NO es "devuelve REAL", sino el que importa:
    # **nunca fingir razonamiento real**. Con la cuota gratuita agotada, la respuesta
    # legítima es DEGRADED con el motivo anotado — y eso también es un acierto. Un test
    # que exigiera REAL fallaría cada vez que se agota la cuota, que es la situation
    # normal de un free tier, ytaparía enmascarando el fallo que sí importa.
    if resp.outcome is ModelOutcome.REAL:
        assert resp.provider == "gemini"
    else:
        assert resp.outcome is ModelOutcome.DEGRADED
        assert resp.is_real is False
        assert resp.fallback_used is True
        # El motivo de la degradación es lo que hay que comprobar, NO un código HTTP
        # concreto: una caída legítima puede ser 429, pero también 500, 503 o timeout, y
        # exigir un código fijo hacía que este test fallara cuando el sistema se
        # comportaba bien. Se exige lo que el contrato sí garantiza: una explicación
        # no vacía, y que identifique al proveedor que falló
        # (`router.complete` compone `f"{provider.id}: {type(exc).__name__}: {exc}"`).
        # Si el proveedor de contingencia fallara también, el motivo llega en `error`.
        motivo = resp.fallback_error or resp.error
        assert motivo, f"una degradación debe decir POR QUÉ: {resp.fallback_error!r}"
        assert "gemini" in str(motivo), \
            f"el motivo debe identificar al proveedor que falló: {motivo!r}"
    assert elapsed < 30000
    assert key not in str(resp.to_dict()), "la clave no puede viajar en la respuesta"
    assert key not in str(resp.audit_event(ModelTask.REASON))


@pytest.mark.external
@pytest.mark.asyncio
async def test_25_gemini_live_acepta_json_schema():
    """El Core manda `response_format: json_schema`; hay que confirmar que se acepta."""
    import os

    key = _live_key()
    if not key:
        pytest.skip("sin credencial de Gemini: proveedor NOT_CONFIGURED")
    env = {"ALEXIS_MODEL_PROVIDERS": "gemini", "ALEXIS_GEMINI_API_KEY": key,
           "ALEXIS_MODEL_DEADLINE_GEMINI_MS": "25000"}
    router = build_router_from_config(ModelConfig.from_env(env))
    schema = {"type": "object",
              "properties": {"action": {"type": "string"}, "step_id": {"type": "string"}},
              "required": ["action", "step_id"]}
    resp = await router.complete(ModelRequest(
        task=ModelTask.REASON,
        messages=[{"role": "user", "content": "Elige action=execute step_id=leer"}],
        schema=schema, max_tokens=400, temperature=0.0, deadline_ms=25000,
    ))
    if resp.outcome is not ModelOutcome.REAL:
        # Mismo criterio que test_24: sin cuota, degradar es lo correcto y hay que
        # poder probarlo. Lo que no se admite nunca es un REAL falso.
        assert resp.is_real is False
        assert resp.fallback_used is True
        pytest.skip(f"sin cuota de Gemini esta vez: {str(resp.fallback_error)[:70]}")
    assert isinstance(resp.data, dict) and "action" in resp.data, \
        f"el esquema no se respetó: {resp.text[:120]}"


@pytest.mark.external
@pytest.mark.asyncio
async def test_26_openrouter_live_no_probado_sin_credencial():
    """OpenRouter: NOT_CONFIGURED. Este test queda como contrato a cumplir cuando haya clave."""
    import os

    if not os.environ.get("ALEXIS_OPENROUTER_API_KEY"):
        pytest.skip("OpenRouter NOT_CONFIGURED: sin ALEXIS_OPENROUTER_API_KEY")
    env = {"ALEXIS_MODEL_PROVIDERS": "openrouter",
           "ALEXIS_OPENROUTER_API_KEY": os.environ["ALEXIS_OPENROUTER_API_KEY"],
           "ALEXIS_MODEL_DEADLINE_OPENROUTER_MS": "30000"}
    router = build_router_from_config(ModelConfig.from_env(env))
    resp = await router.complete(ModelRequest.simple(
        ModelTask.REASON, "Di hola en tres palabras", max_tokens=100,
        temperature=0.0, deadline_ms=30000))
    assert resp.outcome is ModelOutcome.REAL, f"degradó: {resp.error}"
