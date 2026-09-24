"""R3 — "datos ≠ instrucciones": filtro de contenido no confiable.

Una `Observation` que viene de una tool, de un archivo, de una web o de un repo es
**dato**, nunca instrucción. Este módulo es el único punto por el que ese contenido
puede entrar a un contexto o a un prompt, y lo envuelve de forma que el consumidor no
pueda confundirlo con una orden del Core.

Dos defensas, ambas deterministas y sin modelo:

1. **Marcado explícito**: el contenido se envuelve en un bloque
   `[[UNTRUSTED_DATA source=...]] … [[/UNTRUSTED_DATA]]` que lo declara dato.
2. **Neutralización**: las frases de metainstrucción ("ignore previous instructions",
   "you are now", "ignora las instrucciones anteriores"…) y los marcadores de rol
   (`system:`, `assistant:`) se sustituyen por un marcador inerte, de modo que el texto
   no puede *actuar* como instrucción aunque el modelo lo lea.

Además se rompen los propios delimitadores del bloque si el contenido los contiene, de
modo que el dato no puede "escapar" del marcado ni falsificar su cierre.

Decisión de diseño: **no se borra el contenido**. Se conserva (es evidencia y puede ser
legítimo) pero neutralizado y marcado. Un filtro que destruye evidencia rompería
`docs/AUDIT-v0.3.md` y la trazabilidad; lo que se rompe es su capacidad de actuar como
orden.
"""

import re
from dataclasses import dataclass, field
from typing import Any

OPEN_MARKER = "[[UNTRUSTED_DATA"
CLOSE_MARKER = "[[/UNTRUSTED_DATA]]"

#: Instruction-like phrases (meta-instructions, EN + ES). Deliberadamente NO incluye
#: órdenes de trabajo del usuario ("borra el archivo x.txt"): ésas son datos legítimos
#: que el Core ya evalúa con Policy/Envelope, no intentos de tomar el control del prompt.
INJECTION_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"ignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+"
               r"(?:instructions?|prompts?|rules?|directions?)", re.I),
    re.compile(r"disregard\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above|earlier|your)\s+"
               r"(?:instructions?|prompts?|rules?)", re.I),
    re.compile(r"forget\s+(?:everything|all\s+(?:previous|prior)\s+"
               r"(?:instructions?|rules?))", re.I),
    re.compile(r"you\s+are\s+now\b", re.I),
    re.compile(r"from\s+now\s+on\s+you\b", re.I),
    re.compile(r"new\s+instructions?\s*:", re.I),
    re.compile(r"(?:reveal|show|print|repeat|output)\s+(?:me\s+)?(?:your|the)\s+"
               r"(?:system\s+)?(?:prompt|instructions?)", re.I),
    re.compile(r"override\s+(?:the\s+)?(?:policy|permissions?|envelope|rules?)", re.I),
    re.compile(r"ignora\s+(?:todas?\s+)?(?:las?\s+)?(?:instrucciones|reglas)\s+"
               r"(?:anteriores|previas)", re.I),
    re.compile(r"olvida\s+(?:todo|todas\s+las\s+instrucciones|las\s+instrucciones)", re.I),
    re.compile(r"ahora\s+eres\b", re.I),
    re.compile(r"a\s+partir\s+de\s+ahora\s+eres\b", re.I),
    re.compile(r"nuevas?\s+instrucciones\s*:", re.I),
    re.compile(r"(?:revela|muestra|repite|imprime)\s+(?:tu\s+)?(?:prompt|instrucciones)",
               re.I),
    re.compile(r"(?:anula|ignora)\s+(?:la\s+)?pol[ií]tica", re.I),
)

#: Marcadores de rol que un modelo interpretaría como un turno de sistema/asistente.
#: Se neutralizan al inicio de línea y también en medio de un párrafo, tras
#: puntuación de cierre de frase (`… . system: …`), que es donde un atacante los
#: encaja para que el modelo los lea como un turno nuevo. `user:`/`tool:` sólo se
#: neutralizan al inicio de línea (son señales de rol más débiles y frecuentes en datos
#: legítimos).
ROLE_PREFIX_PATTERN = re.compile(
    r"(?im)(^|(?<=[\n\r.;!?])\s+)(system|assistant|developer)\s*:", re.I
)
ROLE_LINE_START_PATTERN = re.compile(r"(?im)^(\s*)(user|tool)\s*:", re.I)

NEUTRALIZED = "[neutralized-instruction]"


def detect_injection(text: str) -> list[str]:
    """Nombres de los patrones de metainstrucción detectados (no devuelve el texto)."""
    if not text:
        return []
    found: list[str] = []
    for pattern in INJECTION_PATTERNS:
        if pattern.search(text):
            found.append(pattern.pattern[:48])
    if ROLE_PREFIX_PATTERN.search(text) or ROLE_LINE_START_PATTERN.search(text):
        found.append("role-marker")
    return found


def is_suspicious(text: str) -> bool:
    return bool(detect_injection(text))


def neutralize(text: str) -> tuple[str, list[str]]:
    """Sustituye metainstrucciones y marcadores de rol por texto inerte.

    Devuelve `(texto_neutralizado, patrones_detectados)`.
    """
    if not text:
        return "", []
    found = detect_injection(text)
    clean = text
    for pattern in INJECTION_PATTERNS:
        clean = pattern.sub(NEUTRALIZED, clean)
    # Un marcador de rol puede abrir un turno nuevo: se neutraliza (en línea o embebido).
    clean = ROLE_PREFIX_PATTERN.sub(NEUTRALIZED, clean)
    clean = ROLE_LINE_START_PATTERN.sub(lambda m: f"{m.group(1)}{NEUTRALIZED}", clean)
    return clean, found


def _escape_markers(text: str) -> str:
    """Rompe los delimitadores del bloque para que el dato no pueda cerrar/abrir marcado."""
    return text.replace(OPEN_MARKER, "[[ UNTRUSTED_DATA").replace(CLOSE_MARKER, "[[/ UNTRUSTED_DATA]]")


def sanitize_untrusted(text: str, *, source: str | None = None, max_len: int | None = None) -> str:
    """Envuelve contenido no confiable como dato explícito y neutralizado.

    El resultado siempre es un bloque `[[UNTRUSTED_DATA …]]` auto-contenido: aunque se
    incruste en un prompt mayor, queda delimitado y declarado como dato.
    """
    raw = "" if text is None else str(text)
    if max_len is not None and len(raw) > max_len:
        raw = raw[:max_len]
    clean, _ = neutralize(raw)
    clean = _escape_markers(clean)
    label = source or "unknown"
    return f"{OPEN_MARKER} source={label}]]\n{clean}\n{CLOSE_MARKER}"


@dataclass
class UntrustedData:
    """Contenido no confiable listo para contexto/prompt."""

    text: str
    source: str = "unknown"
    findings: list[str] = field(default_factory=list)
    neutralized: bool = False

    @property
    def is_neutralized(self) -> bool:
        return self.neutralized

    def render(self) -> str:
        return self.text


def wrap_untrusted(content: Any, *, source: str | None = None, max_len: int | None = None) -> UntrustedData:
    """Punto de entrada para contenido externo (tool, archivo, web, repo, memoria)."""
    if content is None:
        text = ""
    elif isinstance(content, (dict, list)):
        # Un payload estructurado también es dato: se serializa, no se interpreta.
        import json

        text = json.dumps(content, ensure_ascii=False, default=str)
    else:
        text = str(content)
    findings = detect_injection(text)
    rendered = sanitize_untrusted(text, source=source, max_len=max_len)
    return UntrustedData(
        text=rendered,
        source=source or "unknown",
        findings=findings,
        neutralized=bool(findings),
    )


def is_trusted(observation: Any) -> bool:
    """`Observation.trusted` decide; por defecto (False) es no confiable."""
    if observation is None:
        return False
    if isinstance(observation, dict):
        return bool(observation.get("trusted", False))
    return bool(getattr(observation, "trusted", False))


def observation_context_line(observation: Any, *, max_len: int = 200) -> str:
    """Línea de contexto para una observación, aplicando el filtro SI no es confiable.

    Es la función que deben usar los consumidores de observaciones (Self Model,
    MemoryProvider, prompts). Una observación `trusted=True` se pasa tal cual: el
    runtime ya la ha marcado como propia.
    """
    if observation is None:
        return ""
    if is_trusted(observation):
        source = observation.get("source") if isinstance(observation, dict) else getattr(observation, "source", "trusted")
        content = observation.get("content") if isinstance(observation, dict) else getattr(observation, "content", observation)
        if isinstance(content, dict):
            content = content.get("text") or content
        return f"confiable[{source}]: {str(content)[:max_len]}" if content else ""
    source = observation.get("source") if isinstance(observation, dict) else getattr(observation, "source", "unknown")
    content = observation.get("content") if isinstance(observation, dict) else getattr(observation, "content", observation)
    if isinstance(content, dict):
        content = content.get("text") or content
    if not content:
        return ""
    return sanitize_untrusted(str(content), source=source, max_len=max_len)
