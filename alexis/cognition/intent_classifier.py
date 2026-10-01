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
from alexis.cognition.criteria import normalize_criteria
from alexis.models.correlation import pre_mission
from alexis.models.provider import ModelOutcome, ModelRequest, ModelTask

LOGGER = logging.getLogger("alexis.cognition.intent")

_VALID_KINDS = {k.value for k in IntentKind}

_SYSTEM_PROMPT = (
    "Eres el clasificador de intencion de ALEXIS. Responde solo JSON con el esquema dado. "
    "Hay nueve kinds y cada uno tiene su significado; no los confundas. "
    "kind=greeting: saludos. "
    "kind=small_talk: cortesia que no pide nada (gracias, vale, perfecto). "
    "kind=capability_query: que puede hacer. "
    "kind=self_query: que hizo o hace. "
    "kind=meta_query: como funciona o quien es. "
    "kind=question: pregunta general sin objetivo concreto. "
    "kind=clarification: la respuesta a una pregunta que ALEXIS ya hizo. "
    "kind=command: control conversacional sobre ALEXIS (para, detente, cancela eso, "
    "repite, status, reanuda). NO es un imperativo cualquiera: no pide trabajo sobre el "
    "mundo, solo controla su comportamiento. "
    "kind=task: pide una accion o un resultado sobre el mundo, y entonces objective es "
    "obligatorio. 'cancela eso' es command; 'cancela el informe del trimestre' es task. "
    "kind=unknown: el turno no se puede clasificar; entonces needs_clarification es true. "
    "Solo task crea mission. "
    "success_criteria: solo para kind=task, y en el vocabulario que ALEXIS sabe comprobar, "
    "con el formato predicado:argumento y el predicado pegado a la ruta, sin texto libre: "
    "file_exists:RUTA (el archivo existe), file_missing:RUTA (el archivo no debe existir), "
    "file_size_at_least:RUTA:BYTES (pesa al menos N bytes), tests_passing:suite (la suite pasa), "
    "tests_failing:suite (la suite falla). Ejemplo correcto: \"file_exists:notas.txt\". "
    "Si la tarea no admite ninguno de esos predicados, deja el array vacio: es honesto, "
    "mientras que un criterio de texto libre no se puede comprobar y la mission no podra "
    "darse por cumplida. "
    "requested_capabilities: solo ids de la lista dada, es una sugerencia y NO un "
    "permiso; nunca inventes ids. Si no hay lista, deja el array vacio."
)

_FEWSHOT = [
    ("Hola ALEXIS.", '{"kind":"greeting","objective":null,"target":null,"success_criteria":[],"requested_capabilities":[],"side_effects_intent":"unknown","ambiguity":null,"needs_clarification":false,"confidence":0.95}'),
    ("¿Qué puedes hacer?", '{"kind":"capability_query","objective":null,"target":null,"success_criteria":[],"requested_capabilities":[],"side_effects_intent":"unknown","ambiguity":null,"needs_clarification":false,"confidence":0.9}'),
    # La distinción que más se confunde: control conversacional frente a trabajo real.
    ("cancela eso", '{"kind":"command","objective":null,"target":null,"success_criteria":[],"requested_capabilities":[],"side_effects_intent":"unknown","ambiguity":null,"needs_clarification":false,"confidence":0.9}'),
    ("borra el informe del trimestre", '{"kind":"task","objective":"Borrar el informe del trimestre","target":"informe del trimestre","success_criteria":["file_missing:informe.txt"],"requested_capabilities":["fs.remove"],"side_effects_intent":"delete","ambiguity":null,"needs_clarification":false,"confidence":0.85}'),
    ("Revisa este proyecto y dime qué problemas importantes encuentras.", '{"kind":"task","objective":"Revisar el proyecto e informar de los problemas importantes","target":null,"success_criteria":["tests_passing:suite"],"requested_capabilities":["fs.read","fs.stat"],"side_effects_intent":"read","ambiguity":null,"needs_clarification":false,"confidence":0.85}'),
    # B5: leer un archivo del workspace. El criterio es la existencia observada por una
    # herramienta, no "he entendido el contenido": el vocabulario no llega más lejos.
    ("Analiza el archivo notas.txt y dime qué contiene", '{"kind":"task","objective":"Analizar el archivo notas.txt y explicar su contenido","target":"notas.txt","success_criteria":["file_exists:notas.txt"],"requested_capabilities":["fs.read"],"side_effects_intent":"read","ambiguity":null,"needs_clarification":false,"confidence":0.85}'),
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

#: Cortesía: turnos que se entienden y no piden nada. Coincidencia exacta.
_SMALL_TALK_EXACT = (
    "ok", "okay", "vale", "bien", "perfecto", "perfecta", "genial", "claro",
    "estupendo", "entendido", "entendida", "recibido", "recibida", "gracias",
    "muchas gracias", "mil gracias", "de nada", "nada", "ya", "sip", "nop",
    "adiós", "adios", "hasta luego", "chao", "chau", "nos vemos",
)

#: Las mismas, pero como fragmento: "gracias por eso" también es cortesía.
_SMALL_TALK_WORDS = (
    "gracias", "de nada", "hasta luego", "nos vemos", "perfecto", "perfecta",
    "estupendo", "estupenda", "muchas gracias",
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

    def classify(self, utterance: str, brief: SelfBrief | None = None,
                 *, pending_clarification: bool = False) -> Intent:
        """`pending_clarification=True` significa que hay una pregunta abierta.

        En ese caso el turno es una RESPUESTA a esa pregunta, no una tarea nueva: sin
        esto, un "src/main.py" caería en UNKNOWN y se perdería. No se crea una segunda
        misión (P0 §13.7).
        """
        from alexis.perception.activation import is_activation_objective
        from alexis.tools.filesystem import classify_objective_intent

        text = (utterance or "").strip()
        if pending_clarification and text:
            return Intent(
                kind=IntentKind.CLARIFICATION,
                utterance=text,
                confidence=0.8,
                model_meta={"source": self.source, "classifier": "pending_clarification"},
            )
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

        # P0 requisito 1: la cortesía NO es una petición. "Gracias" caía en `UNKNOWN` y
        # recibía "no he entendido la petición", que es absurdo: el turno se entiende
        # perfectamente, sólo no pide nada. `SMALL_TALK` existía en el enum y era
        # inalcanzable; aquí por fin tiene origen.
        #
        # Coincidencia EXACTA para las cortas, y por substring para las largas. "ok" suelta
        # es cortesía; "ok, borra el directorio" es una orden y tiene que llegar a las
        # reglas de tarea, así que no se busca como substring.
        if low in _SMALL_TALK_EXACT or any(word in low for word in _SMALL_TALK_WORDS):
            return Intent(kind=IntentKind.SMALL_TALK, utterance=text, confidence=0.85,
                          model_meta=meta)


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

        # P0 requisito 1: una pregunta interrogariva es `QUESTION`, no `UNKNOWN`.
        # No abre misión (ya lo garantiza `is_task`), pero sí tiene clase propia: la
        # conversación puede responderla en lugar de pedir aclaración.
        if _looks_like_question(text):
            return Intent(
                kind=IntentKind.QUESTION,
                utterance=text,
                confidence=0.55,
                model_meta=meta,
            )

        # P0 requisito 1: `command` = control conversacional sobre ALEXIS. Va DESPUÉS de
        # los verbos de tarea a propósito: "abre la terminal" contiene "abre" y debe
        # seguir siendo `TASK`, porque es trabajo que necesita plan, policy y
        # verificación. Aquí ya no queda ningún verbo operativo, así que lo que queda es
        # una orden de control, que no abre misión (P3.3) y se responde en el acto.
        if _looks_like_command(text):
            return Intent(
                kind=IntentKind.COMMAND,
                utterance=text,
                confidence=0.85,
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


#: P0 requisito 1 — `command` es CONTROL CONVERSACIONAL, no "cualquier imperativo".
#:
#: El vocabulario es cerrado y exige coincidencia EXACTA de la frase entera. No se busca
#: por substring porque el riesgo real aquí es el contrario al habitual: "para" y "estado"
#: son palabras muy frecuentes del español, y una búsqueda laxa las habría convertido en
#: órdenes. Con la frase completa, "para mañana" o "estado del proyecto" NO son comandos.
_COMMAND_VERBS = (
    "para", "para ya", "detente", "detén", "detener", "cancela", "cancelar", "cancélalo",
    "repite", "repetir", "repites", "repítelo", "olvida", "olvidar", "olvídalo",
    "reanuda", "reanudar", "reanúdalo", "status", "estado", "estado actual",
)
#: Objeto admisible del comando. Deliberadamente corto: "cancela eso" es control sobre la
#: operación en curso; "cancela el proyecto entero" es trabajo sobre el mundo y debe caer
#: en las reglas de tarea, no en un comando de conversación.
_COMMAND_TAIL = r"(?:\s+(?:eso|esto|la\s+operaci[oó]n|la\s+tarea|el\s+plan|ahora))?"
_COMMAND_RE = re.compile(
    rf"^(?:{'|'.join(re.escape(v) for v in _COMMAND_VERBS)}){_COMMAND_TAIL}[.!?]*$"
)


def _looks_like_command(text: str) -> bool:
    """¿El turno es una orden de control sobre el propio ALEXIS?"""
    return bool(_COMMAND_RE.match(_normalize(text or "").strip("?!. ")))


#: Aperturas interrogativas en español e inglés. Suficiente para distinguir una pregunta
#: de un comando o de una tarea; no pretende ser un analizador sintáctico.
_QUESTION_OPENERS = (
    "que", "qué", "quien", "quién", "donde", "dónde", "cuando", "cuándo", "por que",
    "por qué", "como", "cómo", "cuanto", "cuánto", "cual", "cuál", "cuales", "cuáles",
    "para que", "para qué", "con que", "con qué", "de que", "de qué",
    "what", "who", "where", "when", "why", "how", "which", "whose",
)


def _looks_like_question(text: str) -> bool:
    """¿El turno es una pregunta? Marca de cierre o apertura interrogativa."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped.endswith("?"):
        return True
    low = _normalize(stripped)
    return any(low.startswith(opener) for opener in _QUESTION_OPENERS)


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

    CORE-02: este punto de salida también es el que aplica el contrato de
    `success_criteria` (`criteria.normalize_criteria`). El clasificador decide QUÉ pide
    el usuario; el contrato decide cómo se escribe eso de forma que el `GoalVerifier`
    pueda comprobarlo. Sin él, una TASK podía llegar a la misión con criterios vacíos o
    en lenguaje libre y el objetivo no se cerraba nunca.
    """

    def __init__(self, router=None, *, rule_based: RuleBasedIntentClassifier | None = None,
                 model: ModelIntentClassifier | None = None):
        self.router = router
        self.rule_based = rule_based or RuleBasedIntentClassifier()
        self.model = model or (ModelIntentClassifier(router) if router is not None else None)
        self.last_proposal: CapabilityProposal | None = None

    async def classify(self, utterance: str, brief: SelfBrief | None = None,
                       *, pending_clarification: bool = False) -> Intent:
        """Punto único de salida de una intención: aquí se cierra el contrato CORE-02.

        La clasificación (modelo primero, reglas como contingencia) ocurre en
        `_classify`. Este envoltorio es el único sitio donde se normalizan los
        criterios de éxito, y por eso los cubre TODOS los caminos: el del modelo, el
        fallback determinista y las correcciones de reglas. Así ninguna TASK llega a
        la misión con criterios que el `GoalVerifier` no sepa leer, sin tocar el
        verificador.
        """
        intent = await self._classify(utterance, brief, pending_clarification=pending_clarification)
        return self._with_canonical_criteria(intent)

    def _with_canonical_criteria(self, intent: Intent) -> Intent:
        """CORE-02: los `success_criteria` de una TASK salen canónicos y trazados.

        Sólo las TASK llevan criterios: una pregunta o un comando de control no abre
        misión, así que no hay nada que verificar y no hay contrato que cumplir. Los
        criterios que no se pueden canonizar NO se descartan: se conservan y quedan
        contados como `unverifiable` en `model_meta["criteria_status"]`, para que la
        traza diga por qué el objetivo no cerró en vez de perder el rastro.
        """
        if not intent.is_task:
            return intent
        criteria, status = normalize_criteria(
            intent.utterance or "",
            intent.objective or intent.utterance or "",
            intent.success_criteria,
        )
        intent.success_criteria = criteria
        intent.model_meta = {**(intent.model_meta or {}), "criteria_status": status}
        return intent

    async def _classify(self, utterance: str, brief: SelfBrief | None = None,
                        *, pending_clarification: bool = False) -> Intent:
        text = (utterance or "").strip()
        if pending_clarification and text:
            # Hay una pregunta abierta: este turno la responde. Ni el modelo ni las reglas
            # deciden eso, y sobre todo NO se crea una segunda misión (P0 §13.7).
            return self.rule_based.classify(text, brief, pending_clarification=True)
        if self.model is None or self.router is None:
            return self._fallback(text, brief, reason="sin ModelRouter configurado", outcome=None)

        request = self.model.build_request(text, brief)
        try:
            # CORE-05: la clasificación ocurre ANTES de que exista misión, así que la
            # correlación es explícitamente pre-misión. Se dice, en vez de omitir el
            # argumento: omitirlo sería indistinguible de "no me importó".
            response = await self.router.complete(
                request, correlation=pre_mission(site="intent_classifier._classify")
            )
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

        # Un modelo pequeño puede "desclasificar" frases con verbo de tarea claro
        # (greeting/capability_query). Si las reglas ven TASK literal, manda la regla:
        # la precisión de los verbos pesa más que la salida débil del modelo 1B.
        if intent.kind is not IntentKind.TASK:
            rule = self.rule_based.classify(text, brief)
            if rule.kind is IntentKind.TASK:
                rule.model_meta = {
                    **(meta or {}),
                    "source": "deterministic",
                    "fallback_reason": "el modelo desclasificó un verbo de tarea claro; reglas deterministas pesan más",
                    "model_outcome": response.outcome.value,
                    "cognition_outcome": ModelOutcome.DEGRADED.value,
                }
                self.last_proposal = None
                return rule

        # Simétrico, y por el mismo motivo que el guard anterior pero en la otra
        # dirección: un comando de control NUNCA debe abrir misión. El vocabulario de
        # control es cerrado y exacto —no depende de razonamiento del modelo—, así que si
        # las reglas reconocen un comando y el modelo dijo `task`, manda la regla. Sin
        # esto, un "para" clasificado como TASK por un modelo pequeño abriría una misión
        # con el único propósito de dejar de hacer nada.
        if intent.kind is IntentKind.TASK:
            rule = self.rule_based.classify(text, brief)
            if rule.kind is IntentKind.COMMAND:
                rule.model_meta = {
                    **(meta or {}),
                    "source": "deterministic",
                    "fallback_reason": "el modelo quiso abrir una misión para un comando de control; las reglas lo corrigen",
                    "model_outcome": response.outcome.value,
                    "cognition_outcome": ModelOutcome.DEGRADED.value,
                }
                self.last_proposal = None
                return rule

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
