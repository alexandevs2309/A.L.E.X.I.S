"""Puente micrófono del HOST → ALEXIS (diseñado para ejecutarse fuera del contenedor).

El contenedor de ALEXIS no tiene acceso al audio del host, así que este proceso
hace de sensor: lee el mic del host, detecta la doble palmada con el MISMO
`ClapDetector` de ALEXIS y publica el evento haciendo `POST /clap` al demo.
ALEXIS la trata como una misión de activación normal (el orchestrador decide qué
hacer; la palmada no ejecuta nada por sí misma).

Motores de audio (según disponibilidad del host):
- `sounddevice` (si está instalado): usa `ClapListener` completo (probe/auto-selección).
- `arecord` (ALSA/PipeWire, sin dependencias extra): filtro raw S16_LE → `feed_rms` puro.

Uso (en el host, no en el contenedor):
    export PYTHONPATH=/ruta/ALEXIS
    python -m alexis.perception.bridge_host [--url http://127.0.0.1:8100]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import struct
import subprocess
import sys
import time
import urllib.request

from alexis.perception.clap_detector import ClapDetected, ClapDetector

RATE = 44100
BLOCK_SAMPLES = int(RATE * 40 / 1000)  # 40 ms


class HttpEventBridge:
    """Publica ClapDetected → POST /clap del demo de ALEXIS."""

    def __init__(self, base_url: str, timeout_s: float = 3.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def post(self, event: ClapDetected) -> int | None:
        body = json.dumps(
            {
                "source": event.source,
                "confidence": event.confidence,
                "gap_s": event.gap_s,
                "level": event.level,
                "timestamp": event.timestamp,
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.base_url}/clap",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                return r.status
        except Exception as exc:  # noqa: BLE001
            print(f"[clap] POST /clap falló: {exc}")
            return None


def _post_sync(bridge: HttpEventBridge, event: ClapDetected) -> None:
    status = bridge.post(event)
    print(
        f"[clap] doble palmada gap={event.gap_s:.3f}s conf={event.confidence} "
        f"-> ALEXIS HTTP {status}"
    )


def _engine_arecord(bridge: HttpEventBridge, detector: ClapDetector, device: str = "default") -> int:
    """Escucha con `arecord` y alimenta `ClapDetector.feed_rms` (agua pura, sin deps)."""
    cmd = [
        "arecord", "-q", "-D", device, "-c", "1", "-r", str(RATE),
        "-f", "S16_LE", "-t", "raw", "--buffer-time=40000", "-",
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    t0 = time.monotonic()
    print(f"[clap] arecord: escuchando mic '{device}' (RAW 44.1kHz/16bit). Ctrl+C para salir.")
    try:
        while True:
            raw = proc.stdout.read(BLOCK_SAMPLES * 2)
            if not raw:
                err = proc.stderr.read().decode(errors="replace").strip()
                print(f"[clap] arecord terminó: {err or 'fin de stream'}")
                return 1
            if len(raw) < BLOCK_SAMPLES * 2:
                continue
            vals = struct.unpack(f"<{BLOCK_SAMPLES}h", raw)
            ms = math.sqrt(sum(v * v for v in vals) / BLOCK_SAMPLES) / 32768.0
            event = detector.feed_rms(ms, now=time.monotonic() - t0)
            if event is not None:
                _post_sync(bridge, event)
    except KeyboardInterrupt:
        print("\n[clap] detenido por el usuario.")
    finally:
        proc.terminate()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Puente micrófono del host → ALEXIS /clap")
    ap.add_argument("--url", default="http://127.0.0.1:8100", help="URL del demo de ALEXIS")
    ap.add_argument("--device", default="default", help="dispositivo ALSA (arecord -D)")
    args = ap.parse_args(argv)

    bridge = HttpEventBridge(args.url)
    detector = ClapDetector(sample_rate=RATE, block_ms=40)

    try:
        import sounddevice  # noqa: F401

        return _engine_sounddevice(bridge, detector, args.url)
    except ImportError:
        return _engine_arecord(bridge, detector, args.device)


def _engine_sounddevice(bridge, detector, url) -> int:
    import asyncio

    from alexis.perception.clap_listener import ClapListener

    listener = ClapListener(detector=detector)

    class _BridgeAdapter:
        def __init__(self, b, d):  # noqa: ANN001
            self._bridge = b
            self._detector = d
            self._loop = None

        async def publish(self, topic, event):
            await asyncio.to_thread(_post_sync, self._bridge, event)

    adapter = _BridgeAdapter(bridge, detector)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ok, note = listener.start(adapter, "perception.clap_detected", loop, run_async=True)
    print(f"[clap] listener sounddevice: {note}")
    if not ok:
        return 1
    print("[clap] Escuchando desde el host. Haz una DOBLE PALMADA cerca del micrófono. Ctrl+C para salir.")
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())