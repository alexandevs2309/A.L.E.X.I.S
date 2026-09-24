"""Voz de salida en el HOST: reproduce el audio que el servidor genera.

El servidor (contenedor) sintetiza TTS (edge-tts / ElevenLabs / local) y guarda
el archivo en `alexis/.cache/tts/` (carpeta compartida por bind-mount). Este
proceso, en el HOST, vigila esa carpeta y reproduce cada archivo nuevo por los
altavoces: `.wav` con aplay (o paplay), `.mp3` con gst-play.

Cierre del bucle de percepción (sin mic:: señal por server):
    alexis-demo (contenedor)  activación → respond → TTS → NUEVO archivo en caché
    voice_bridge (host)        detecta el archivo → aplay/gst → altavoces

No resintetiza: la voz la elige el servidor (edge-tts, latina, gratis y natural).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
AUDIO_DIR = PROJECT / ".cache" / "tts"  # la misma carpeta donde sintetiza el servidor
MEMO = PROJECT / "alexis" / ".voice_bridge_played.json"  # ruta del host (el .cache lo crea el contenedor como root)
AUDIO_EXTS = (".wav", ".mp3")


def _play(path: Path) -> bool:
    name = path.name.lower()
    player = "aplay" if name.endswith(".wav") else None
    for candidate in (player, "paplay" if name.endswith(".wav") else None):
        if candidate and shutil.which(candidate):
            try:
                subprocess.run([candidate, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
                return True
            except Exception:  # noqa: BLE001
                pass
    gst = shutil.which("gst-launch-1.0")
    if gst:
        try:
            subprocess.run(f'{gst} playbin uri="file://{path.resolve()}" >/dev/null 2>&1', shell=True, timeout=60)
            return True
        except Exception:  # noqa: BLE001
            pass
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Reproduce en el host el audio TTS que genera ALEXIS")
    ap.add_argument("--poll", type=float, default=0.5, help="segundos entre comprobaciones")
    ap.add_argument("--replay", action="store_true", help="reproduce también el audio previo (no solo el nuevo)")
    args = ap.parse_args(argv)

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    played: set[str] = set()
    if MEMO.exists():
        try:
            played = set(json.loads(MEMO.read_text()))
        except json.JSONDecodeError:
            played = set()
    # Lo previo no se repite (salvo --replay): sólo se reproduce lo que aparezca desde ahora.
    if not args.replay:
        for f in AUDIO_DIR.iterdir():
            if f.is_file() and f.name.lower().endswith(AUDIO_EXTS):
                played.add(f.name)
    print(f"[voz] vigilando {AUDIO_DIR} (reproduce archivos nuevos; {len(played)} ya vistos). Ctrl+C para salir.")
    try:
        while True:
            for f in sorted(AUDIO_DIR.glob("*.mp3")) + sorted(AUDIO_DIR.glob("*.wav")):
                name = f.name
                if name in played:
                    continue
                if ".pending." in name or ".tmp" in name:
                    continue
                time.sleep(0.3)  # deja que termine de escribirse
                size_before = f.stat().st_size
                time.sleep(0.3)
                if f.stat().st_size != size_before:
                    continue  # aún escribiéndose
                ok = _play(f)
                played.add(name)
                print(f"[voz] {'reproduje' if ok else 'no pude reproducir'} {name}")
                MEMO.write_text(json.dumps(sorted(played), ensure_ascii=False))
            time.sleep(args.poll)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())