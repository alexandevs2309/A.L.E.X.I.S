"""FASE 2 — Provenance y semántica de respuesta vacía, sobre respuestas REAL.

La evidencia que motiva este fichero viene de una llamada real a OpenRouter, no de una
suposición. Lo que llegó fue:

    HTTP 200
    model    : poolside/laguna-xs-2.1:free      (OpenRouter enrutó openrouter/free aquí)
    provider : Poolside
    usage    : prompt_tokens=52  completion_tokens=50  cost=0
    choices[0].finish_reason : length
    choices[0].message.content : null
    choices[0].message.reasoning : presente

Y ALEXIS registró `tokens_in=0`, `tokens_out=0`, `model="echo"`, y un motivo que decía
"respuesta vacía del proveedor (finish sin contenido)". Todo eso estaba en el cuerpo de la
respuesta. Estos tests fijan ese hallazgo con la forma exacta que se observó.

Dos cosas que este fichero NO hace, a propósito:

- No convierte un truncamiento en `REAL`. Una respuesta sin texto no es una respuesta.
- No inventa provenance. Si la API no dice qué modelo ejecutó ni cuántos tokens gastó, el
  campo queda en `None`/`0`, que es un hecho distinto de "no lo sé todavía".
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.models.provider import ModelOutcome  # noqa: E402
from alexis.models.providers.http_base import (  # noqa: E402
    _empty_reason,
    _resolved_provenance,
)
from alexis.models.providers.openrouter import OpenRouterProvider  # noqa: E402
from alexis.models.providers.local_http import LocalHTTPProvider  # noqa: E402


# =========================================================================== #
# Fixtures: las tres formas reales que puede tomar la respuesta
# =========================================================================== #

#: Copia literal de lo que devolvió OpenRouter en la llamada real que motivó esta fase.
RAW_REAL_TRUNCADA = {
    "id": "gen-1790665651-1yEoPkcgZ69dpLaMyaCw",
    "object": "chat.completion",
    "created": 1790665651,
    "model": "poolside/laguna-xs-2.1:free",
    "provider": "Poolside",
    "choices": [
        {
            "index": 0,
            "logprobs": None,
            "finish_reason": "length",
            "native_finish_reason": "length",
            "message": {
                "role": "assistant",
                "content": None,
                "refusal": None,
                "reasoning": "Okay, the user asked for \"hola\" in three words...",
            },
        }
    ],
    "usage": {
        "prompt_tokens": 52,
        "completion_tokens": 50,
        "total_tokens": 102,
        "cost": 0,
    },
}

#: Lo mismo pero llegando a escribir. Es un caso que aún no se ha observado, pero su
#: forma la declara la propia API.
RAW_REAL_CON_TEXTO = {
    "model": "poolside/laguna-xs-2.1:free",
    "provider": "Poolside",
    "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Hola"}}
    ],
    "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15, "cost": 0},
}


def _provider():
    return OpenRouterProvider(api_key="clave-de-prueba")


# =========================================================================== #
# Objetivo 1 — provenance: lo que la API dice, se conserva
# =========================================================================== #


def test_01_el_modelo_real_se_conserva_y_no_se_confunde_con_el_pedido():
    resolved, _ = _resolved_provenance(RAW_REAL_TRUNCADA)
    assert resolved == "poolside/laguna-xs-2.1:free"
    # Y el slug pedido sigue siendo lo que era. No se sobrescribe la semántica de `model`.
    assert _provider().model == "openrouter/free"


def test_02_el_proveedor_final_se_conserva():
    _, upstream = _resolved_provenance(RAW_REAL_TRUNCADA)
    assert upstream == "Poolside"


def test_03_sin_model_en_la_respuesta_no_se_inventa():
    resolved, upstream = _resolved_provenance({"choices": [], "usage": {}})
    assert resolved is None
    assert upstream is None


def test_04_un_cuerpo_no_dict_no_revienta():
    assert _resolved_provenance(None) == (None, None)
    assert _resolved_provenance("texto") == (None, None)


# =========================================================================== #
# Objetivo 2 — finish_reason: truncado ≠ vacío
# =========================================================================== #


def test_05_truncado_por_tokens_no_se_reporta_como_vacio():
    motivo = _empty_reason(RAW_REAL_TRUNCADA)
    assert "length" in motivo
    assert "límite de tokens" in motivo
    assert "vacío del proveedor" not in motivo, "un truncamiento no es un proveedor vacío"


def test_06_sin_contenido_con_otro_finish_se_distingue_del_truncamiento():
    raw = {"choices": [{"finish_reason": "content_filter", "message": {"content": None}}]}
    motivo = _empty_reason(raw)
    assert "content_filter" in motivo
    assert "límite de tokens" not in motivo


def test_07_sin_finish_reason_el_motivo_lo_dice():
    assert "sin finish_reason" in _empty_reason({"choices": []})


# =========================================================================== #
# Objetivo 3 — contrato: requested vs resolved, explícito
# =========================================================================== #


async def test_08_caso_1_respuesta_sana_es_real_con_todo_registrado():
    """Caso 1: content válido + usage + model → REAL con el modelo y los tokens reales."""
    from alexis.models.providers.http_base import HTTPChatProvider

    p = _provider()
    text, data, tokens_in, tokens_out = p.parse_response(RAW_REAL_CON_TEXTO)
    assert text == "Hola"
    assert tokens_in == 12
    assert tokens_out == 3
    resolved, _ = _resolved_provenance(RAW_REAL_CON_TEXTO)
    assert resolved == "poolside/laguna-xs-2.1:free"
    assert HTTPChatProvider is not None


async def test_09_caso_2_truncada_no_es_real_pero_conserva_los_tokens():
    """Caso 2: content=null + length + usage → NO real, y los tokens NO se pierden."""
    from alexis.models.provider import ModelResponse

    p = _provider()
    text, _, tokens_in, tokens_out = p.parse_response(RAW_REAL_TRUNCADA)
    resolved, upstream = _resolved_provenance(RAW_REAL_TRUNCADA)
    resp = ModelResponse(
        text=text,
        provider=p.id,
        model=p.model,
        resolved_model=resolved,
        resolved_provider=upstream,
        outcome=ModelOutcome.UNAVAILABLE,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        error=_empty_reason(RAW_REAL_TRUNCADA),
    )
    assert resp.outcome is not ModelOutcome.REAL, "sin texto no hay respuesta real"
    assert resp.is_real is False
    # El gap original: estos tokens se perdían. Ahora están.
    assert (resp.tokens_in, resp.tokens_out) == (52, 50)
    assert resp.resolved_model == "poolside/laguna-xs-2.1:free"
    assert resp.resolved_provider == "Poolside"
    assert "límite de tokens" in resp.error


def test_10_caso_4_sin_usage_no_falla_ni_inventa_tokens():
    raw = {"model": "m", "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}
    _, _, tokens_in, tokens_out = _provider().parse_response(raw)
    assert (tokens_in, tokens_out) == (0, 0)


def test_11_caso_5_sin_model_no_inventa_resolved_model():
    raw = {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
           "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    resolved, _ = _resolved_provenance(raw)
    assert resolved is None
    assert _provider().model == "openrouter/free", "el pedido sigue siendo el slug configurado"


def test_12_los_campos_nuevos_no_rompen_el_contrato_existente():
    """Backwards compatible: construir a pelo y por keyword sigue funcionando."""
    from alexis.models.provider import ModelResponse

    viejo = ModelResponse(text="x", provider="p", model="m")
    assert viejo.resolved_model is None
    assert viejo.resolved_provider is None
    d = viejo.to_dict()
    assert d["model"] == "m" and d["resolved_model"] is None
    ev = viejo.audit_event(__import__(
        "alexis.models.provider", fromlist=["ModelTask"]).ModelTask.REASON)
    assert ev["resolved_model"] is None


# =========================================================================== #
# Objetivo 4 — no-regresión de otros providers
# =========================================================================== #


def test_13_el_dialecto_ollama_no_cambia():
    raw = {"model": "qwen2.5-coder", "message": {"content": "hola"},
           "prompt_eval_count": 7, "eval_count": 2}
    text, _, tokens_in, tokens_out = LocalHTTPProvider().parse_response(raw)
    assert text == "hola"
    assert (tokens_in, tokens_out) == (7, 2)


def test_14_gemini_no_cambia_de_forma():
    """Gemini habla el endpoint OpenAI-compatible, así que parsea como openai.

    Comprobado contra el código: `GEMINI_BASE_URL` termina en `/v1beta/openai` y su
    `dialect` es `"openai"`. Por eso la respuesta llega con `usage.prompt_tokens` y no con
    `usageMetadata`: la forma nativa de la API de Google nunca pasa por aquí.
    """
    from alexis.models.providers.gemini import GEMINI_BASE_URL, GeminiProvider

    assert GEMINI_BASE_URL.endswith("/openai")
    p = GeminiProvider(api_key="k")
    assert p.dialect == "openai"

    raw = {"model": "gemini-x",
           "choices": [{"finish_reason": "stop", "message": {"content": "hola"}}],
           "usage": {"prompt_tokens": 9, "completion_tokens": 1}}
    text, _, tokens_in, tokens_out = p.parse_response(raw)
    assert text == "hola"
    assert (tokens_in, tokens_out) == (9, 1)
    # Y como es openai-compatible, su `model` de raíz también se registra como resuelto.
    assert _resolved_provenance(raw) == ("gemini-x", None)


def test_15_openrouter_sigue_siendo_un_provider_mas_y_nada_más():
    """La regla arquitectónica: fuera de alexis/models/ nadie lo nombra."""
    import pathlib as _p

    raiz = PROJECT_ROOT / "alexis"
    culpables = []
    for f in raiz.rglob("*.py"):
        rel = f.relative_to(PROJECT_ROOT)
        if str(rel).startswith("alexis/models/"):
            continue
        if "openrouter" in f.read_text(encoding="utf-8").lower():
            culpables.append(str(rel))
    assert culpables == [], f"OpenRouter filtrado fuera de models/: {culpables}"
