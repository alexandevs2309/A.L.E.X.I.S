"""Proveedor edge-tts: voces neurales latinas gratis (sin clave ni dispositivos).

edge-tts usa el servicio online de Microsoft Edge TTS (sin API key ni coste).
Genera MP3 (no WAV), que se guarda en la carpeta de caché de ALEXIS; el
`voice_bridge` del host detecta el archivo nuevo y lo reproduce por `gst-play`.

Voces (ALEXIS_EDGE_VOICE), por ejemplo:
  es-MX-DaliaNeural / es-MX-JorgeNeural  (mexicana)
  es-US-AlonsoNeural / es-US-PalomaNeural (US-Latino)
  es-CO-GonzaloNeural / es-PE-CamilaNeural / es-CL-CatalinaNeural
  es-AR-ElenaNeural / es-ES-ElviraNeural / es-ES-TristanNeural
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from alexis.speech.tts import TTSProvider, TTSResult

DEFAULT_VOICE = "es-MX-DaliaNeural"

SPANISH_VOICES = {
    "es-MX-DaliaNeural": "España mexicana · femenina",
    "es-MX-JorgeNeural": "España mexicana · masculina",
    "es-US-AlonsoNeural": "España US-Latino · masculina",
    "es-US-PalomaNeural": "España US-Latino · femenina",
    "es-CO-GonzaloNeural": "España colombiana · masculina",
    "es-PE-CamilaNeural": "España peruana · femenina",
    "es-CL-CatalinaNeural": "España chilena · femenina",
    "es-AR-ElenaNeural": "España argentina · femenina",
    "es-ES-ElviraNeural": "España de España · femenina",
    "es-ES-TristanNeural": "España de España · masculina",
}


def _cache_dir() -> Path:
    override = os.environ.get("ALEXIS_TTS_CACHE_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent.parent.parent / ".cache" / "tts"


def edge_voice(from_config: str | None = None) -> str:
    voice = (from_config or os.environ.get("ALEXIS_EDGE_VOICE", "") or DEFAULT_VOICE).strip()
    return voice if voice in SPANISH_VOICES or voice.endswith("Neural") else DEFAULT_VOICE


class EdgeTTSProvider(TTSProvider):
    name = "edge"

    def __init__(self, voice: str | None = None):
        self.voice = edge_voice(voice)

    def available(self) -> bool:
        try:
            import edge_tts  # noqa: F401

            return True
        except Exception:
            return False

    async def synthesize(self, text, voice_id=None, model_id=None) -> TTSResult:
        started = time.monotonic()
        text = (text or "").strip()
        if not text:
            return TTSResult(False, self.name, message="texto vacío", error="texto vacío")
        try:
            import edge_tts
        except ImportError as exc:
            return TTSResult(False, self.name, message="falta edge-tts (pip install edge-tts)", error=str(exc))
        voice = edge_voice(voice_id) if voice_id else self.voice
        key = hashlib.sha256(f"edge|{voice}|{text}".encode()).hexdigest()[:24]
        out = _cache_dir() / f"{key}.mp3"
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            duration = int((time.monotonic() - started) * 1000)
            return TTSResult(True, self.name, message="TTS edge desde caché", duration_ms=duration, path=str(out), format="mp3")
        try:
            await edge_tts.Communicate(text, voice).save(str(out))
        except Exception as exc:  # noqa: BLE001 — reporta honesto
            return TTSResult(False, self.name, message=f"edge-tts falló: {exc}", error=str(exc))
        if not out.exists():
            return TTSResult(False, self.name, message="edge-tts no guardó audio", error="sin archivo")
        duration = int((time.monotonic() - started) * 1000)
        return TTSResult(True, self.name, message="TTS edge", duration_ms=duration, path=str(out), format="mp3")


def _main(argv: list[str] | None = None) -> int:
    """CLI: sintetiza una frase con la voz elegida (prueba rápida de voces)."""
    import argparse
    import asyncio

    ap = argparse.ArgumentParser(prog="alexis.speech.edge", description="Prueba una voz latina de edge-tts")
    ap.add_argument("--text", default="Hola, soy ALEXIS. ¿En qué te ayudo?")
    ap.add_argument("--voice", default=None, help="voz edge-tts (ej. es-CO-GonzaloNeural); por defecto ALEXIS_EDGE_VOICE")
    args = ap.parse_args(argv)
    provider = EdgeTTSProvider(voice=args.voice)
    result = asyncio.run(provider.synthesize(args.text))
    print("ok" if result.ok else "error")
    print(result.message)
    if result.path:
        print(result.path)
    return 0 if result.ok else 1


if __name__ == "__main__":
    import sys

    sys.exit(_main())