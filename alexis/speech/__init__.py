"""Síntesis de voz de ALEXIS (lado servidor).

Se importa ligero a propósito: ElevenLabs (que arrastra numpy) se expone vía
`alexis.speech.elevenlabs` y la fábrica `get_tts_provider` resuelve los
proveedores de manera diferida, para no romper el boot del demo si numpy o el
SDK no están instalados.
"""

from alexis.speech.elevenlabs import ElevenLabsProvider  # noqa: F401 (import diferido por el paquete)
from alexis.speech.tts import NoopTTSProvider, TTSProvider, TTSResult, get_tts_provider

__all__ = [
    "ElevenLabsProvider",
    "NoopTTSProvider",
    "TTSProvider",
    "TTSResult",
    "get_tts_provider",
]