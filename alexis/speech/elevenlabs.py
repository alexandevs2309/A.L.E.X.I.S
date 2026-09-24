"""Proveedor ElevenLabs para ALEXIS, portado de `jarvis-main`.

Conserva de JARVIS: API key / voice / model configurables, caché de audio
generado y reutilización sin llamada a la API. El `elevenlabs` SDK se importa
de forma perezosa (extra `[desktop]`), por lo que este módulo importa sin él y
reporta un error honesto si no está instalado o la API falla.

Namespace de config: `ALEXIS_ELEVENLABS_*` con fallback temporal `ELEVENLABS_*`
(compatibilidad con el `.env` de JARVIS).
Caché: `ALEXIS_TTS_CACHE_DIR` (fallback JARVIS_WELCOME_CACHE_DIR, fallback
`alexis/.cache/tts`).
"""

from __future__ import annotations

import hashlib
import os
import time
import wave
from pathlib import Path

from alexis.speech.tts import TTSProvider, TTSResult

DEFAULT_MODEL = "eleven_multilingual_v2"
DEFAULT_OUTPUT_FORMAT = "pcm_24000"
DEFAULT_PCM_SAMPLE_RATE = 24000


def _env(*keys: str) -> str:
    for key in keys:
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def elevenlabs_env_config(prefix: str = "ALEXIS_ELEVENLABS_") -> dict:
    return {
        "api_key": _env(f"{prefix}API_KEY", "ELEVENLABS_API_KEY") or None,
        "voice_id": _env(f"{prefix}VOICE_ID", "ELEVENLABS_VOICE_ID") or None,
        "model_id": _env(f"{prefix}MODEL_ID", "ELEVENLABS_MODEL_ID") or DEFAULT_MODEL,
        "output_format": _env(f"{prefix}OUTPUT_FORMAT", "ELEVENLABS_OUTPUT_FORMAT") or DEFAULT_OUTPUT_FORMAT,
        "output_path": None,
    }


def pcm_sample_rate(output_format: str) -> int:
    override = _env("ALEXIS_TTS_SAMPLE_RATE", "ELEVENLABS_PCM_SAMPLE_RATE")
    if override.isdigit():
        return int(override)
    if output_format.startswith("pcm_"):
        try:
            return int(output_format.split("_", maxsplit=1)[1])
        except (ValueError, IndexError):
            return DEFAULT_PCM_SAMPLE_RATE
    return DEFAULT_PCM_SAMPLE_RATE


def tts_cache_dir() -> Path:
    override = _env("ALEXIS_TTS_CACHE_DIR", "JARVIS_WELCOME_CACHE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent.parent.parent / ".cache" / "tts"


def tts_cache_key(text: str, voice_id: str, model_id: str, output_format: str) -> str:
    return hashlib.sha256(f"{text}|{voice_id}|{model_id}|{output_format}".encode()).hexdigest()[:24]


def tts_cache_path(
    text: str,
    voice_id: str,
    model_id: str,
    output_format: str,
    cache_dir: Path | None = None,
) -> Path:
    return (cache_dir or tts_cache_dir()) / f"{tts_cache_key(text, voice_id, model_id, output_format)}.wav"


def load_cached_wav(path: Path):
    """Devuelve el PCM mono de 16 bits como bytes o None si no es un WAV válido."""
    try:
        with wave.open(str(path), "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                return None
            return wf.readframes(wf.getnframes())
    except (OSError, wave.Error):
        return None


def store_cached_wav(path: Path, pcm_bytes: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with wave.open(str(tmp), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    tmp.replace(path)


def _play_pcm(bytes_pcm: bytes, sample_rate: int) -> bool:
    """Playback opcional en el host (requiere sounddevice). Devuelve False si no hay."""
    try:
        import sounddevice as sd  # noqa: F401
    except Exception:
        return False
    try:
        import numpy as np  # import diferido: elevenlabs.py debe importar sin numpy
    except Exception:
        return False
    try:
        pcm_f = np.frombuffer(bytes_pcm, dtype=np.int16).astype(np.float32) / 32768.0
        sd.play(pcm_f, sample_rate)
        sd.wait()
        return True
    except Exception:
        return False


class ElevenLabsProvider(TTSProvider):
    name = "elevenlabs"

    def __init__(self, config: dict | None = None):
        self.config = config or elevenlabs_env_config()

    def available(self) -> bool:
        return bool(self.config.get("api_key") and self.config.get("voice_id"))

    async def synthesize(
        self,
        text: str,
        voice_id: str | None = None,
        model_id: str | None = None,
        play: bool = False,
    ) -> TTSResult:
        started = time.monotonic()
        text = (text or "").strip()
        if not text:
            return TTSResult(False, self.name, message="texto vacío", error="texto vacío")
        if not self.available():
            return TTSResult(
                False,
                self.name,
                message="ElevenLabs requiere API key y voice id (ALEXIS_ELEVENLABS_API_KEY / _VOICE_ID)",
                error="credenciales ElevenLabs incompletas",
            )

        vid = voice_id or self.config["voice_id"]
        mid = model_id or self.config["model_id"]
        fmt = self.config["output_format"]
        rate = pcm_sample_rate(fmt)
        cache = tts_cache_path(text, vid, mid, fmt)

        cached = load_cached_wav(cache)
        if cached is not None:
            if play:
                _play_pcm(cached, rate)
            duration = int((time.monotonic() - started) * 1000)
            return TTSResult(True, self.name, message="TTS desde caché", duration_ms=duration, path=str(cache), format=fmt)

        try:
            from elevenlabs.client import ElevenLabs
        except ImportError:
            return TTSResult(False, self.name, message="falta elevenlabs (pip install alexis[desktop])", error="SDK no instalado")
        try:
            client = ElevenLabs(api_key=self.config["api_key"])
            chunks = client.text_to_speech.convert(voice_id=vid, text=text, model_id=mid, output_format=fmt)
            raw = b"".join(chunks)
        except Exception as exc:  # noqa: BLE001 — se reporta, no se oculta
            return TTSResult(False, self.name, message=f"ElevenLabs falló: {exc}", error=str(exc))
        if not raw:
            return TTSResult(False, self.name, message="ElevenLabs devolvió audio vacío", error="audio vacío")
        try:
            store_cached_wav(cache, raw, rate)
        except OSError as exc:
            return TTSResult(False, self.name, message=f"no pude guardar el caché: {exc}", error=str(exc))
        if play:
            _play_pcm(raw, rate)
        duration = int((time.monotonic() - started) * 1000)
        return TTSResult(True, self.name, message="TTS ElevenLabs", duration_ms=duration, path=str(cache), format=fmt)