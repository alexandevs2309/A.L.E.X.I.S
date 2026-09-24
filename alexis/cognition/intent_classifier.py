"""Primer punto de uso real del ModelRouter: clasificación de intención (F2.3 slice).

Qué decide este módulo y qué NO:

- **Sí**: qué tipo de turno es (`IntentKind`), cuál es el objetivo, y qué capabilities
  **sugiere** el modelo (`CapabilityProposal`).
- **No**: qué capabilities se usan. Eso es `Selection` (F2.5), y sólo la puede hacer el
  Core recortando contra catálogo + envelope + policy (P3.4/P3.5).

P1 (procedencia): cada `Intent` lleva en `model_meta` de dónde salió la decisión. Si el
modelo no está disponible, falla o su respuesta no valida, se usa el clasificador
determinista y la decisión queda marcada como contingencia: nunca se presenta como
razonamiento real.

P2 (separación): este módulo sólo usa el contrato `ModelRouter`. No sabe qué proveedor
hay registrado, ni menciona OmniRoute ni ningún proveedor concreto.

El fallback determinista (`RuleBasedIntentClassifier`) es el clasificador histórico de
v0.3 (`is_activation_objective` + palabras clave) y se conserva como camino válido.
"""

import json
import logging
import re

from alexis.cognition.contracts import (
    CapabilityProposal,
    Intent,
    IntentKind,
    SelfBrief,
)
from alexis.models.provider import ModelOutcome, ModelRequest, ModelTask

LOGGER = logging.getLogger("alexis.cognition.intent")

_VALID_KINDS = {k.value for k in IntentKind}

_SYSTEM_PROMPT = (
    "Eres el clasificador de intencion de ALEXIS. Responde solo JSON con el esquema dado. "
    "kind=greeting: saludos. kind=capability_query: que puede hacer. kind=self_query: que hizo o "
    "hace. kind=meta_query: como funciona o quien es. kind=task: pide una accion o resultado, y "
    "entonces objective es obligatorio. requested_capabilities: solo ids de la lista dada, es una "
    "sugerencia y NO un permiso; nunca inventes ids. Si no hay lista, deja el array vacio."
)

_FEWSHOT = [
    ("Hola ALEXIS.", '{"kind":"greeting","objective":null,"target":null,"success_criteria":[],"requested_capabilities":[],"side_effects_intent":"unknown","ambiguity":null,"needs_clarification":false,"confidence":0.95}'),
    ("¿Qué puedes hacer?", '{"kind":"capability_query","objective":null,"target":null,"success_criteria":[],"requested_capabilities":[],"side_effects_intent":"unknown","ambiguity":null,"needs_clarification":false,"confidence":0.9}'),
    ("Revisa este proyecto y dime qué problemas importantes encuentras.", '{"kind":"task","objective":"Revisar el proyecto e informar de los problemas importantes","target":null,"success_criteria":["Identificar problemas importantes con evidencia"],"requested_capabilities":["fs.read","fs.stat"],"side_effects_intent":"read","ambiguity":null,"needs_clarification":false,"confidence":0.85}'),
]

#: Palabras que delatan una petición de acción (fallback determinista).
_TASK_VERBS = (
    "revisa", "revisar", "revísame", "crea", "crear", "escribe", "escribir", "borra",
    "borrar", "elimina", "eliminar", "investiga", "investigar", "busca", "buscar",
    "lee", "leer", "analiza", "analizar", "corrige", "corregir", "arréglalo", "haz",
    "dime", "dime qué", "genera", "generar", "modifica", "abrir", "abre", "pon",
    "resume", "resumir", "compara", "cuéntame", "explica", "explícame", "muéstrame",
    "review", "read", "write", "create", "delete", "remove", "analyze", "fix", "show",
)

_CAPABILITY_WORDS = (
    "qué puedes hacer", "que puedes hacer", "qué sabes hacer", "que sabes hacer",
    "qué puedes", "que puedes", "qué herramientas", "que herramientas", "qué puedes usar",
    "qué eres capaz", "que eres capaz", "qué tienes", "capacidades", "herramientas",
)

_SELF_WORDS = (
    "qué hiciste", "que hiciste", "qué estás haciendo", "que estas haciendo",
    "qué has hecho", "qué hicisteis", "cómo vas", "como vas", "qué tal",
    "qué recuerdas", "qué sabes de mí", "qué has aprendido", "qué decidiste",
    "por qué tomaste", "por que tomaste", "qué te pasa",
)

_META_WORDS = (
    "cómo funcionas", "como funcionas", "qué eres", "que eres", "quién eres",
    "quien eres", "qué es alexis", "que es alexis", "cuál es tu propósito",
    "cual es tu proposito", "explica tu arquitectura", "cómo estás hecho",
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _extract_json(text: str) -> dict | None:
    """Extrae el primer objeto JSON balanceado de la respuesta del modelo."""
    if not text:
        return None
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", stripped, re.S)
    if fence:
        stripped = fence.group(1).strip()
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError):
        pass
    start = stripped.find("{")
    while start != -1:
        depth = 0
        for idx in range(start, len(stripped)):
            char = stripped[idx]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(stripped[start : idx + 1])
                    except (ValueError, TypeError):
                        break
                    return parsed if isinstance(parsed, dict) else None
        start = stripped.find("{", start + 1)
    return None


class RuleBasedIntentClassifier:
    """Clasificador determinista (v0.3): Activación + palabras clave.

    Es el fallback *válido*: si no hay modelo, ALEXIS sigue entendiendo lo básico y lo
    dice (`source=deterministic`), sin fingir que hubo razonamiento.
    """

    source = "deterministic"

    def classify(self, utterance: str, brief: SelfBrief | None = None) -> Intent:
        from alexis.perception.activation import is_activation_objective
        from alexis.tools.filesystem import classify_objective_intent

        text = (utterance or "").strip()
        low = _normalize(text)
        meta = {"source": self.source, "classifier": "keywords"}

        if not text:
            return Intent(
                kind=IntentKind.UNKNOWN,
                utterance=text,
                needs_clarification=True,
                ambiguity="turno vacío",
                confidence=0.0,
                model_meta=meta,
            )

        if is_activation_objective(text) or low in ("hola", "hola alexis", "hey", "hey alexis",
                                                    "buenas", "buenos dias", "buenas tardes"):
            return Intent(kind=IntentKind.GREETING, utterance=text, confidence=0.9, model_meta=meta)

        if any(word in low for word in _CAPABILITY_WORDS):
            return Intent(
                kind=IntentKind.CAPABILITY_QUERY, utterance=text, confidence=0.85, model_meta=meta
            )

        if any(word in low for word in _SELF_WORDS):
            return Intent(
                kind=IntentKind.SELF_QUERY, utterance=text, confidence=0.8, model_meta=meta
            )

        if any(word in low for word in _META_WORDS):
            return Intent(kind=IntentKind.META_QUERY, utterance=text, confidence=0.75, model_meta=meta)

        if any(word in low for word in _TASK_VERBS):
            intent_kind = classify_objective_intent(text)
            return Intent(
                kind=IntentKind.TASK,
                utterance=text,
                objective=text,
                side_effects_intent={
                    "write": "modify",
                    "destructive": "delete",
                    "unsupported": "modify",
                }.get(intent_kind, "read"),
                confidence=0.6,
                model_meta=meta,
            )

        return Intent(
            kind=IntentKind.UNKNOWN,
            utterance=text,
            needs_clarification=True,
            ambiguity="no se pudo clasificar el turno",
            confidence=0.2,
            model_meta=meta,
        )


class ModelIntentClassifier:
    """Clasificador respaldado por un modelo real (vía ModelRouter)."""

    source = "model"

    def __init__(self, router, *, max_tokens: int = 220, deadline_ms: int = 60000,
                 temperature: float = 0.0, privacy: str = "normal"):
        self.router = router
        self.max_tokens = max_tokens
        self.deadline_ms = deadline_ms
        self.temperature = temperature
        self.privacy = privacy

    def build_request(self, utterance: str, brief: SelfBrief | None) -> ModelRequest:
        available = list((brief.available_capabilities if brief else []) or [])
        content = (
            f"capacidades: {','.join(available) if available else '(ninguna)'}\n"
            f"turno: {utterance}"
        )
        return ModelRequest(
            task=ModelTask.UNDERSTAND,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
            schema=_intent_schema(),
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            privacy=self.privacy,
            deadline_ms=self.deadline_ms,
        )

    def parse(self, utterance: str, brief: SelfBrief | None, data: dict) -> Intent:
        """Valida la salida del modelo. Devuelve `None` si no es utilizable."""
        kind_value = str(data.get("kind") or "").strip().lower()
        if kind_value not in _VALID_KINDS:
            return None
        kind = IntentKind(kind_value)
        objective = data.get("objective")
        if kind is IntentKind.TASK and not (isinstance(objective, str) and objective.strip()):
            return None
        if kind is not IntentKind.TASK:
            objective = None
        available = set(brief.available_capabilities if brief else [])
        requested_raw = data.get("requested_capabilities") or []
        if not isinstance(requested_raw, list):
            requested_raw = []
        requested = [c for c in (str(x) for x in requested_raw) if not available or c in available]
        criteria = data.get("success_criteria") or []
        if not isinstance(criteria, list):
            criteria = []
        try:
            confidence = float(data.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return Intent(
            kind=kind,
            utterance=utterance,
            objective=objective,
            target=data.get("target") if isinstance(data.get("target"), str) else None,
            success_criteria=[str(c) for c in criteria][:5],
            requested_capabilities=requested,
            side_effects_intent=str(data.get("side_effects_intent") or "unknown"),
            ambiguity=data.get("ambiguity") if isinstance(data.get("ambiguity"), str) else None,
            needs_clarification=bool(data.get("needs_clarification")),
            confidence=max(0.0, min(1.0, confidence)),
        )


def _intent_schema() -> dict:
    """Esquema JSON de la clasificación.

    Se envía al endpoint como `response_format.json_schema`: cuando el servidor puede
    forzarlo (llama.cpp, vLLM, OpenAI), el modelo queda *obligado* a emitir JSON válido,
    lo que hace fiable la clasificación incluso con modelos pequeños.
    """
    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": sorted(_VALID_KINDS)},
            "objective": {"type": ["string", "null"]},
            "target": {"type": ["string", "null"]},
            "success_criteria": {"type": "array", "items": {"type": "string"}},
            "requested_capabilities": {"type": "array", "items": {"type": "string"}},
            "side_effects_intent": {
                "type": "string",
                "enum": ["read", "modify", "delete", "external", "unknown"],
            },
            "ambiguity": {"type": ["string", "null"]},
            "needs_clarification": {"type": "boolean"},
            "confidence": {"type": "number"},
        },
        "required": ["kind"],
    }


class IntentClassifier:
    """Fachada: modelo primero, determinista como contingencia (P1).

    `classify()` nunca lanza. El resultado siempre dice de dónde salió la decisión:

    - `model_meta.source == "model"` y `cognition_outcome == "real"` → el modelo real
      clasificó.
    - `source == "deterministic"` y `cognition_outcome == "degraded"` → no hubo
      razonamiento real (o su salida no validó) y se usó el clasificador de palabras.
    """

    def __init__(self, router=None, *, rule_based: RuleBasedIntentClassifier | None = None,
                 model: ModelIntentClassifier | None = None):
        self.router = router
        self.rule_based = rule_based or RuleBasedIntentClassifier()
        self.model = model or (ModelIntentClassifier(router) if router is not None else None)
        self.last_proposal: CapabilityProposal | None = None

    async def classify(self, utterance: str, brief: SelfBrief | None = None) -> Intent:
        text = (utterance or "").strip()
        if self.model is None or self.router is None:
            return self._fallback(text, brief, reason="sin ModelRouter configurado", outcome=None)

        request = self.model.build_request(text, brief)
        try:
            response = await self.router.complete(request)
        except Exception as exc:  # noqa: BLE001 — el clasificador nunca tumba el Core
            LOGGER.warning("intent: router/complete falló: %s", exc)
            return self._fallback(text, brief, reason=f"router error: {exc}", outcome=None)

        meta = response.audit_event(ModelTask.UNDERSTAND)
        if response.outcome is not ModelOutcome.REAL:
            reason = {
                ModelOutcome.DEGRADED: "respuesta DEGRADED (contingencia determinista del router)",
                ModelOutcome.UNAVAILABLE: "sin provider utilizable (UNAVAILABLE)",
            }.get(response.outcome, "respuesta no real")
            return self._fallback(text, brief, reason=reason, outcome=response.outcome.value, meta=meta)

        data = response.data or _extract_json(response.text)
        if not data:
            return self._fallback(
                text, brief, reason="el modelo respondió sin JSON utilizable",
                outcome=response.outcome.value, meta=meta,
            )

        intent = self.model.parse(text, brief, data)
        if intent is None:
            return self._fallback(
                text, brief, reason="la salida del modelo no pasó la validación",
                outcome=response.outcome.value, meta=meta,
            )

        intent.model_meta = {**meta, "source": "model", "validated": True}
        self.last_proposal = CapabilityProposal(
            capabilities=list(intent.requested_capabilities),
            rationale="sugerencia del modelo; no es permiso (P3.5)",
            model_meta=dict(meta),
        )
        return intent

    def _fallback(self, utterance: str, brief: SelfBrief | None, *, reason: str,
                  outcome: str | None, meta: dict | None = None) -> Intent:
        intent = self.rule_based.classify(utterance, brief)
        intent.model_meta = {
            **(meta or {}),
            "source": "deterministic",
            "fallback_reason": reason,
            "model_outcome": outcome or "unavailable",
            "cognition_outcome": ModelOutcome.DEGRADED.value,
        }
        self.last_proposal = None
        LOGGER.info(
            "intent: usando clasificador determinista (%s); outcome del modelo=%s",
            reason, outcome or "unavailable",
        )
        return intent


__all__ = [
    "IntentClassifier",
    "ModelIntentClassifier",
    "RuleBasedIntentClassifier",
]
