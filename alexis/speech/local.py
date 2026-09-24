"""Proveedor de TTS local vía `espeak-ng`/`espeak`/`flite` (sin API key).

Útil cuando no hay credenciales de ElevenLabs: si el binario está instalado en
el host se sintetiza un WAV 16-bit mono en la misma carpeta de caché que
ElevenLabs (`alexis/.cache/tts`), que el voice_bridge del host reproduce por
`aplay`. Si no hay binario, `available()` devuelve False y `synthesize()` reporta
una razón honesta (nunca finge que habló).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import wave
from pathlib import Path

from alexis.speech.tts import TTSProvider, TTSResult

DEFAULT_RATE = 16000


def _local_tts_binary() -> str | None:
    for name in ("espeak-ng", "espeak", "flite"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _cache_dir() -> Path:
    override = os.environ.get("ALEXIS_TTS_CACHE_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent.parent.parent / ".cache" / "tts"


def _synth(binary: str, text: str, out: Path) -> tuple[bool, str | None]:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".pending.wav")  # espeak-ng elige formato por EXTENSIÓN
    base = [binary]
    rate = int(os.environ.get("ALEXIS_TTS_SAMPLE_RATE", "").strip() or DEFAULT_RATE)
    if binary.endswith("espeak-ng") or binary.endswith("espeak"):
        base += ["-v", os.environ.get("ALEXIS_TTS_VOICE", "").strip() or "es", "-s", str(rate), "-w", str(tmp)]
    else:  # flite
        base += ["-o", str(tmp), "-t"]
    base.append(text)
    with open(os.devnull, "wb") as null:
        try:
            subprocess.run(base, stdin=null, stdout=null, stderr=null, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
    if not tmp.exists():
        return False, f"{binary} no produjo audio"
    try:
        with wave.open(str(tmp), "rb") as wf:
            if wf.getnchannels() == 1 and wf.getsampwidth() == 2:
                tmp.replace(out)
                return True, None
    except wave.Error as exc:
        return False, str(exc)
    return False, "WAV local no es mono/16bit"


class LocalTTSProvider(TTSProvider):
    name = "local"

    def available(self) -> bool:
        return _local_tts_binary() is not None

    async def synthesize(self, text, voice_id=None, model_id=None) -> TTSResult:
        started = time.monotonic()
        text = (text or "").strip()
        if not text:
            return TTSResult(False, self.name, message="texto vacío", error="texto vacío")
        binary = _local_tts_binary()
        if binary is None:
            return TTSResult(
                False,
                self.name,
                message="no hay motor de voz local (instala: sudo apt install espeak-ng)",
                error="sin binario TTS local",
            )
        import hashlib

        key = hashlib.sha256(f"local|{text}".encode()).hexdigest()[:24]
        out = _cache_dir() / f"{key}.wav"
        ok, err = _synth(binary, text, out)
        if not ok:
            return TTSResult(False, self.name, message=f"TTS local falló: {err}", error=err)
        duration = int((time.monotonic() - started) * 1000)
        return TTSResult(True, self.name, message="TTS local", duration_ms=duration, path=str(out), format="pcm")