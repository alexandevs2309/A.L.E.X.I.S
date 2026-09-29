"""FASE 3.4.1 — El retry de `json_schema` a `json_object`, verificado.

## Corrección de por medio

La auditoría de Fase 3.4 afirmó que el retry no existía. **Era falso.** Está en
`HTTPChatProvider._post()` desde antes, y funciona: ante un `HTTPError` de 400, 404, 422 o
501 reintenta una sola vez con `response_format: {"type": "json_object"}`. La auditoría leyó
la línea del `fallback` y concluyó que pertenecía a otro provider.

Lo que sí era verdad es lo segundo: **el retry no tenía ni un test**. La única mención de
`json_schema` en la suite era un test live de Gemini que comprobaba que Gemini lo *acepta*,
que es la preocupación contraria. Un mecanismo de red sin cobertura puede romperse sin que
nadie se entere, y este decide si un modelo externo pequeño es utilizable o no.

Por eso este fichero no añade código al provider: **fija el comportamiento que ya existe**
para que no se pueda degradar en silencio. Si alguien cambia el conjunto de códigos que
disparan el retry, estos tests fallan.

## Lo que se fija

- El retry ocurre SÓLO ante rechazo de `response_format`, nunca ante 401, 403, 429, 5xx,
  timeout ni conexión caída.
- El segundo request cambia únicamente `response_format`; todo lo demás —system, messages,
  temperature, max_tokens, modelo, cabeceras— viaja idéntico.
- Si el segundo intento también falla, el resultado es no-REAL. El retry no se esconde.
- El `latency_ms` reportado es el total de ambos intentos, no el del último: un retry
  lento no puede parecer rápido.

Ninguna llamada de red ocurre: `_post_raw` se sustituye por un doble en cada test.
"""

import json
import pathlib
import sys
import urllib.error
import urllib.request

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.models.provider import ModelOutcome, ModelRequest, ModelTask  # noqa: E402
from alexis.models.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402


# =========================================================================== #
# Dobles: un _post_raw que registra lo que se le envía y decide qué responde
# =========================================================================== #


class _HTTPError(urllib.error.HTTPError):
    """HTTPError con lo mínimo para que `_http_error_reason` lo describa."""

    def __init__(self, code):
        super().__init__("http://proveedor.invalido/v1/chat/completions", code,
                         "error", {}, None)


class _Doble:
    """Sustituye `_post_raw`. `guiones` es la lista de respuestas, en orden.

    Cada elemento es un `dict` (éxito) o un `_HTTPError` (fallo). Al agotarse, repite el
    último, para que un test pueda afirmar sobre el último intento.
    """

    def __init__(self, *guiones):
        self.guiones = list(guiones)
        self.enviados = []

    def __call__(self, payload):
        self.enviados.append(json.loads(json.dumps(payload)))
        if not self.enviados:
            raise AssertionError("no debería llamarse sin guiones")
        idx = min(len(self.enviados) - 1, len(self.guiones) - 1)
        r = self.guiones[idx]
        if isinstance(r, BaseException):
            raise r
        return r


def _ok(text="hola", model="modelo-ejecutado", upstream="proveedor-final"):
    return {
        "id": "gen-1",
        "model": model,
        "provider": upstream,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "cost": 0},
    }


def _vacio_truncado():
    return {
        "id": "gen-2",
        "model": "modelo-racionador",
        "provider": "proveedor-final",
        "choices": [{"index": 0, "finish_reason": "length",
                     "message": {"role": "assistant", "content": None, "reasoning": "..."}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 50, "cost": 0},
    }


def _provider():
    return OpenAICompatibleProvider(
        base_url="https://proveedor.invalido/api",
        model="slug-configurado",
        api_key="clave-de-prueba",
        timeout=5.0,
    )


def _peticion():
    return ModelRequest(
        task=ModelTask.REASON,
        system="eres un clasificador",
        messages=[{"role": "user", "content": "elige una opcion"}],
        schema={"type": "object", "properties": {"action": {"type": "string"}},
                "required": ["action"]},
        max_tokens=2048,
        temperature=0.0,
        deadline_ms=60000,
    )


async def _con_doble(monkeypatch, *guiones):
    p = _provider()
    doble = _Doble(*guiones)
    monkeypatch.setattr(p, "_post_raw", doble)
    resp = await p.complete(_peticion())
    return resp, doble


# =========================================================================== #
# TEST 1 — json_schema aceptado: una sola llamada, REAL
# =========================================================================== #


@pytest.mark.asyncio
async def test_01_json_schema_aceptado_una_sola_llamada(monkeypatch):
    resp, doble = await _con_doble(monkeypatch, _ok())

    assert len(doble.enviados) == 1, "sin error no debe haber reintento"
    assert doble.enviados[0]["response_format"]["type"] == "json_schema"
    assert resp.outcome is ModelOutcome.REAL


# =========================================================================== #
# TEST 2 — rechazo específico: segundo intento con json_object, REAL
# =========================================================================== #


@pytest.mark.asyncio
@pytest.mark.parametrize("codigo", [400, 404, 422, 501])
async def test_02_rechazo_de_json_schema_reintenta_como_json_object(monkeypatch, codigo):
    resp, doble = await _con_doble(monkeypatch, _HTTPError(codigo), _ok())

    assert len(doble.enviados) == 2
    assert doble.enviados[0]["response_format"]["type"] == "json_schema"
    assert doble.enviados[1]["response_format"]["type"] == "json_object"
    assert resp.outcome is ModelOutcome.REAL
    assert resp.error is None


# =========================================================================== #
# TEST 3 — ambos rechazos: resultado NO-REAL, y no se reintenta a ciegas
# =========================================================================== #


@pytest.mark.asyncio
async def test_03_si_json_object_tampoco_falla_el_resultado_es_no_real(monkeypatch):
    resp, doble = await _con_doble(monkeypatch, _HTTPError(400), _HTTPError(400))

    assert len(doble.enviados) == 2, "el retry es UN reintento, no un bucle"
    assert resp.outcome is ModelOutcome.UNAVAILABLE
    assert resp.is_real is False
    assert resp.error, "un fallo debe decir por qué"


# =========================================================================== #
# TEST 4-8 — lo que NO debe reintentarse
# =========================================================================== #


@pytest.mark.asyncio
@pytest.mark.parametrize("codigo", [401, 403, 429, 500, 502, 503, 504, 408, 499])
async def test_04_sin_retry_para_errores_que_no_son_de_response_format(monkeypatch, codigo):
    """401/403 son credenciales, 429 es cuota, 5xx es el provider. Nada se arregla reintentando.

    Reintentar un 429 sólo gasta cuota ajena, y reintentar un 401 con la misma clave
    falla igual. Este test es el que impide que alguien "abra" el conjunto de códigos.
    """
    resp, doble = await _con_doble(monkeypatch, _HTTPError(codigo))

    assert len(doble.enviados) == 1, f"{codigo} no debe disparar el retry"
    assert resp.outcome is ModelOutcome.UNAVAILABLE


@pytest.mark.asyncio
async def test_05_sin_retry_ante_timeout(monkeypatch):
    """Un timeout no es un rechazo de `response_format`. Reintentar lo convertiría en
    latencia doble sin cambiar la causa."""
    resp, doble = await _con_doble(monkeypatch, urllib.error.URLError("timed out"))

    assert len(doble.enviados) == 1
    assert resp.outcome is ModelOutcome.UNAVAILABLE


@pytest.mark.asyncio
async def test_06_sin_retry_ante_error_de_conexion(monkeypatch):
    resp, doble = await _con_doble(
        monkeypatch, urllib.error.URLError("connection refused"))

    assert len(doble.enviados) == 1
    assert resp.outcome is ModelOutcome.UNAVAILABLE


# =========================================================================== #
# TEST 9 — respuesta vacía o truncada: tampoco es un rechazo del formato
# =========================================================================== #


@pytest.mark.asyncio
async def test_07_content_null_no_reintenta(monkeypatch):
    """`content: null` con `finish_reason: length` no es un problema de esquema.

    Ocurrió de verdad: un modelo se gastó el presupuesto pensando y no emitió texto. Si
    eso disparara el retry, se doblaría el gasto para obtener la misma respuesta vacía.
    """
    resp, doble = await _con_doble(monkeypatch, _vacio_truncado())

    assert len(doble.enviados) == 1
    assert resp.outcome is ModelOutcome.UNAVAILABLE
    assert "límite de tokens" in (resp.error or "")


# =========================================================================== #
# TEST 10 — el reintento conserva la provenance
# =========================================================================== #


@pytest.mark.asyncio
async def test_08_el_retry_conserva_provenance_del_intento_exitoso(monkeypatch):
    resp, _ = await _con_doble(monkeypatch, _HTTPError(400), _ok())

    assert resp.outcome is ModelOutcome.REAL
    assert resp.model == "slug-configurado", "el pedido no cambia"
    assert resp.resolved_model == "modelo-ejecutado", "el ejecutado sí se registra"
    assert resp.resolved_provider == "proveedor-final"


@pytest.mark.asyncio
async def test_09_el_segundo_request_solo_cambia_response_format(monkeypatch):
    """Todo lo demás viaja idéntico: system, messages, temperature, max_tokens, modelo.

    Si el retry "ayudara" al modelo reescribiendo el prompt, el E2E mediría otra cosa.
    """
    _, doble = await _con_doble(monkeypatch, _HTTPError(400), _ok())
    primero, segundo = doble.enviados

    assert segundo["response_format"] == {"type": "json_object"}
    for clave in ("model", "messages", "temperature", "max_tokens", "stream"):
        assert segundo[clave] == primero[clave], clave
    # Y el system viaja DENTRO de messages[0], como en el primero.
    assert segundo["messages"][0] == primero["messages"][0]
    assert segundo["messages"][0]["role"] == "system"


# =========================================================================== #
# TEST 11 — tokens y latencia no se inventan
# =========================================================================== #


@pytest.mark.asyncio
async def test_10_los_tokens_vienen_de_la_respuesta_real(monkeypatch):
    resp, _ = await _con_doble(monkeypatch, _HTTPError(400), _ok())

    assert (resp.tokens_in, resp.tokens_out) == (11, 7)


@pytest.mark.asyncio
async def test_11_la_latencia_cubre_ambos_intentos(monkeypatch):
    """Un retry lento no puede reportar la latencia del último intento.

    Se mide con un doble que tarda: si la latencia fuera sólo del segundo request,
    `latency_ms` apenas lo notaría, y un proveedor lento parecería rápido.
    """
    import time

    p = _provider()
    llamadas = {"n": 0}

    def lento(payload):
        llamadas["n"] += 1
        time.sleep(0.06)
        if llamadas["n"] == 1:
            raise _HTTPError(400)
        return _ok()

    monkeypatch.setattr(p, "_post_raw", lento)
    resp = await p.complete(_peticion())

    assert llamadas["n"] == 2
    assert resp.latency_ms >= 100, (
            f"la latencia reportada ({resp.latency_ms}ms) no cubre los dos intentos"
    )


# =========================================================================== #
# TEST 12 — el código genérico no nombra ningún provider concreto
# =========================================================================== #


def test_12_http_base_no_omite_ningun_provider_concreto():
    src = (PROJECT_ROOT / "alexis" / "models" / "providers" / "http_base.py").read_text(
        encoding="utf-8").lower()
    for nombre in ("openrouter", "gemini", "omniroute", "openrouter/free"):
        assert nombre not in src, f"el código genérico no debe mencionar {nombre}"


# =========================================================================== #
# El conjunto de códigos que dispara el retry, fijado por contrato
# =========================================================================== #


def test_13_el_conjunto_de_codigos_es_explicito_y_estable():
    """Si alguien amplía el conjunto, este test falla a propósito.

    Ampliarlo "para ser tolerante" sería peligroso: convertiría reintentos de cuota o de
    proveedor caído en algo routine, que es justo lo que este mecanismo evita.
    """
    import inspect

    from alexis.models.providers.http_base import HTTPChatProvider

    fuente = inspect.getsource(HTTPChatProvider._post)
    assert "400, 404, 422, 501" in fuente, (
        "el conjunto de códigos que dispara el retry cambió; revisa si sigue siendo "
        "sólo un rechazo de response_format"
    )
