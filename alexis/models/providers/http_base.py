"""Base HTTP compartida para providers de modelo (F2.1).

Sin dependencias externas: usa `urllib` de la stdlib en un hilo, para no bloquear el
event loop. La lógica de payload/parseo es **pura y testeable** (`build_payload`,
`parse_response`); la red sólo ocurre en `complete`.
"""

import asyncio
import json
import time
import urllib.error
import urllib.request

from alexis.models.provider import (
    ModelOutcome,
    ModelProvider,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
)


class HTTPChatProvider(ModelProvider):
    """Provider HTTP de chat con dialectos `ollama` y `openai`."""

    supports = set()  # lo declara cada subclase
    priority = 50
    cost_per_1k_tokens = 0.0
    latency_p50_ms = 1500
    privacy_max = "normal"
    available = True
    degraded = False
    dialect = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout: float = 60.0,
        endpoint: str | None = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self._endpoint = endpoint

    # ------------------------------------------------------------------ #
    # Payloads (puros)
    # ------------------------------------------------------------------ #

    @property
    def endpoint(self) -> str:
        if self._endpoint:
            return f"{self.base_url}{self._endpoint}"
        if self.dialect == "openai":
            return f"{self.base_url}/v1/chat/completions"
        return f"{self.base_url}/api/chat"

    def build_payload(self, request: ModelRequest) -> dict:
        messages = [{"role": "user", "content": request.messages[-1]["content"]}] if request.messages else []
        if request.system:
            messages.insert(0, {"role": "system", "content": request.system})
        if self.dialect == "openai":
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
                "stream": False,
            }
            if request.schema:
                # Formato OpenAI `json_schema`: el endpoint puede forzar el esquema
                # (gramática en llama.cpp/vLLM, structured outputs en OpenAI), lo que hace
                # fiable incluso con modelos pequeños. Si el servidor no lo soporta, se
                # degrada a `json_object` en el reintento.
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "alexis_response",
                        "schema": request.schema,
                        "strict": False,
                    },
                }
            return payload
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": request.temperature, "num_predict": request.max_tokens},
        }
        if request.schema:
            payload["format"] = "json"
        return payload

    def _post(self, payload: dict) -> dict:
        """POST con degradación: si el servidor rechaza `json_schema`, reintenta una vez
        con `json_object` (compatibilidad con endpoints parcialmente compatibles)."""
        fallback = None
        if isinstance(payload.get("response_format"), dict):
            rf = payload["response_format"]
            if rf.get("type") == "json_schema":
                fallback = {"type": "json_object"}
        try:
            return self._post_raw(payload)
        except urllib.error.HTTPError as exc:
            if fallback is None or exc.code not in (400, 404, 422, 501):
                raise
            relaxed = dict(payload)
            relaxed["response_format"] = fallback
            return self._post_raw(relaxed)

    def _post_raw(self, payload: dict) -> dict:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.endpoint, data=body, headers=self.build_headers(), method="POST"
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode() or "{}")

    def build_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def parse_response(self, raw: dict) -> tuple[str, dict | None, int, int]:
        """Devuelve `(text, data, tokens_in, tokens_out)` según el dialecto."""
        if self.dialect == "openai":
            choices = raw.get("choices") or []
            text = ""
            if choices:
                text = (choices[0].get("message") or {}).get("content") or ""
            usage = raw.get("usage") or {}
            data = _maybe_json(text)
            return text, data, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        message = raw.get("message") or {}
        text = message.get("content") or raw.get("response") or ""
        data = _maybe_json(text)
        if raw.get("response") and not message:
            data = raw.get("response")
        return text, data, int(raw.get("prompt_eval_count") or 0), int(raw.get("eval_count") or 0)

    # ------------------------------------------------------------------ #
    # Red
    # ------------------------------------------------------------------ #

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self.base_url:
            return ModelResponse(
                text="",
                provider=self.id,
                model=self.model,
                outcome=ModelOutcome.UNAVAILABLE,
                error="provider HTTP sin base_url",
            )
        started = time.monotonic()
        payload = self.build_payload(request)
        try:
            raw = await asyncio.to_thread(self._post, payload)
        except urllib.error.HTTPError as exc:
            return ModelResponse(
                text="",
                provider=self.id,
                model=self.model,
                outcome=ModelOutcome.UNAVAILABLE,
                error=f"HTTP {exc.code}",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 — fallo de red no tumba el Core
            return ModelResponse(
                text="",
                provider=self.id,
                model=self.model,
                outcome=ModelOutcome.UNAVAILABLE,
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        text, data, tokens_in, tokens_out = self.parse_response(raw)
        cost = self.cost_per_1k_tokens / 1000.0 * (tokens_in + tokens_out)
        return ModelResponse(
            text=text,
            data=data,
            provider=self.id,
            model=self.model,
            outcome=ModelOutcome.REAL,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


def _maybe_json(text: str) -> dict | None:
    """Parseo tolerante: si el texto es JSON objeto, se usa como `data`."""
    if not text:
        return None
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


__all__ = ["HTTPChatProvider", "ModelProviderError"]
