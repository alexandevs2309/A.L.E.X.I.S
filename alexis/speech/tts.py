"""Interfaz de TTS y fábrica por configuración.

La voz del navegador (Web Speech API en el face app) NO se reemplaza; este
TTS es una capacidad **adicional del servidor** para responder por voz cuando
ALEXIS lo decida (ej.: clap detectado, resumen de misión).

Configuración:
  ALEXIS_TTS_PROVIDER = none | elevenlabs   (default: elevenlabs si hay key,
                                             none si no hay credenciales)
"""

from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class TTSResult:
    ok: bool
    provider: str
    message: str = ""
    duration_ms: int = 0
    path: str | None = None
    format: str = "pcm"
    error: str | None = None


class TTSProvider(ABC):
    name = "abstract"

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        voice_id: str | None = None,
        model_id: str | None = None,
    ) -> TTSResult:
        """Devuelve el audio (o ruta a archivo) y su formato. No lanza; reporta error."""


class NoopTTSProvider(TTSProvider):
    """Proveedor honesto sin credenciales: NO simula ser una voz."""

    name = "none"

    async def synthesize(self, text, voice_id=None, model_id=None) -> TTSResult:
        return TTSResult(
            ok=False,
            provider=self.name,
            message=f"TTS no configurado ({text[:40]!r})",
            error="sin proveedor TTS configurado",
        )


def provider_from_env() -> str:
    configured = (os.environ.get("ALEXIS_TTS_PROVIDER") or "").strip().lower()
    if configured:
        return configured
    for key in ("ALEXIS_ELEVENLABS_API_KEY", "ELEVENLABS_API_KEY"):
        if (os.environ.get(key) or "").strip():
            return "elevenlabs"
    try:
        import edge_tts  # noqa: F401
    except Exception:
        pass
    else:
        return "edge"
    for name in ("espeak-ng", "espeak", "flite"):
        if shutil.which(name):
            return "local"
    return "none"


def get_tts_provider() -> TTSProvider:
    mode = provider_from_env()
    if mode == "elevenlabs":
        from alexis.speech.elevenlabs import ElevenLabsProvider  # import diferido (evita ciclo)

        return ElevenLabsProvider()
    if mode == "edge":
        from alexis.speech.edge import EdgeTTSProvider  # import diferido (evita ciclo)

        return EdgeTTSProvider()
    if mode == "local":
        from alexis.speech.local import LocalTTSProvider  # import diferido (evita ciclo)

        return LocalTTSProvider()
    if mode == "auto":
        from alexis.speech.elevenlabs import ElevenLabsProvider
        from alexis.speech.edge import EdgeTTSProvider
        from alexis.speech.local import LocalTTSProvider

        eleven = ElevenLabsProvider()
        if eleven.available():
            return eleven
        edge = EdgeTTSProvider()
        if edge.available():
            return edge
        local = LocalTTSProvider()
        if local.available():
            return local
    return NoopTTSProvider()


async def synthesize_with_fallback(text: str, provider: TTSProvider | None = None) -> TTSResult:
    """Sintetiza con el proveedor configurado; si no puede (SDK ausente, red,
    credenciales), cae a edge-tts (gratis) y, si tampoco, devuelve el error
    honesto del proveedor principal para que no haya silencios fingidos."""
    main = provider if provider is not None else get_tts_provider()
    result = await main.synthesize(text)
    if result.ok:
        return result
    try:
        from alexis.speech.edge import EdgeTTSProvider  # import diferido (evita ciclo)

        if EdgeTTSProvider().available():
            fallback = await EdgeTTSProvider().synthesize(text)
            if fallback.ok:
                return fallback
    except Exception:  # noqa: BLE001 — el fallback nunca debe romper la misión
        pass
    return result