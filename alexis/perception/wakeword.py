"""Wake word de ALEXIS: reconoce el nombre del asistente en texto transcrito.

El slot de STT (`alexis.perception.interfaces.AudioPerception.transcribe`) aún
no está implementado; este módulo consume su SALIDA (texto) y es 100% puro.
Funciona igual si el texto llega de un STT futuro, del teclado o del chat.

**ALCANCE REAL, para que nadie lo lea como «wake word funcionando»** (2026-10-13):
este módulo está implementado y probado (`tests/test_perception.py::TestWakeword`),
pero **no es alcanzable desde la voz**. No hay STT, así que nadie le entrega texto
transcrito: hoy sólo lo alcanzarían quien lo llame directamente con texto. La
auditoría de §40 detectó esto porque la implementación es real y la fila del contrato
mencionaba el absentismo de STT sin explicar qué queda con utilizable.
Consecuencia práctica: ALEXIS puede **oir** una palmada (`clap_listener`), **hablar**
(TTS + `voice_bridge`) y **reaccionar a un texto** que le llegue por teclado o chat, pero
**no puede escuchar lo que se le dice**. Cerrar ese hueco exige STT, que `VISION.md` §40
enlista como *potential feature* y no como requisito.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_WAKE_WORDS = ("alexis", "alex", "hey alexis", "ok alexis", "oye alexis")


@dataclass
class WakewordMatch:
    word: str
    index: int
    confidence: float
    rest: str


def normalize(text: str) -> str:
    """Minúsculas y sin acentos para el match del wake word."""
    table = str.maketrans(
        "áéíóúÁÉÍÓÚüÜñÑ",
        "aeiouAEIOUuUnN",
    )
    return text.translate(table).strip().lower()


def find_wakeword(
    text: str,
    words: tuple[str, ...] = DEFAULT_WAKE_WORDS,
) -> WakewordMatch | None:
    """Busca el wake word al principio o dentro del texto transcrito."""
    if not text or not text.strip():
        return None
    normalized = normalize(text)
    for word in sorted(words, key=len, reverse=True):
        key_index = normalize(word)
        if not key_index:
            continue
        idx = normalized.find(key_index)
        if idx < 0:
            continue
        rest = normalized[idx + len(key_index):].lstrip(":,. ").strip()
        confidence = 1.0 if idx <= 1 else 0.85
        return WakewordMatch(word=word, index=idx, confidence=confidence, rest=rest)
    return None


def is_activation(text: str, words: tuple[str, ...] = DEFAULT_WAKE_WORDS) -> bool:
    return find_wakeword(text, words) is not None