"""Listener de doble palmada: percepción → Event Bus.

Este servicio es EXCLUSIVAMENTE un sensor: detecta la doble palmada y publica
`ClapDetected` en el Event Bus (topic `perception.clap_detected`). NO ejecuta
acciones: quién consume ese evento es el Agent/Orchestrator de ALEXIS
(`alexis.core.runtime.AlexisRuntime`) a través de una misión normal.

El micrófono se abre con `sounddevice` (extra `[desktop]`); sin él, `start()`
devuelve `(False, razón)` y el detector sigue siendo utilizable mediante
`ingest_block()`/`ingest_rms()` (tests, o cualquier fuente de audio propia).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
import time

from alexis.perception.clap_detector import ClapDetected, ClapDetector
from alexis.perception.microphone import MicrophoneSelector, env_input_device

DEFAULT_TOPIC = "perception.clap_detected"


class ClapListener:
    def __init__(
        self,
        detector: ClapDetector | None = None,
        selector: MicrophoneSelector | None = None,
        sample_rate: int = 44100,
        block_ms: int = 40,
        channels: int = 1,
    ):
        self.detector = detector or ClapDetector(sample_rate=sample_rate, block_ms=block_ms)
        self.selector = selector or MicrophoneSelector(sample_rate=sample_rate, block_ms=block_ms, channels=channels)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bus = None
        self._topic = DEFAULT_TOPIC
        self._last_rms = 0.0
        self._last_debug_print = 0.0

    # --- API de percepción pura (tests y otras fuentes de audio) -------------

    def ingest_block(self, block, now: float | None = None):
        """Procesa un bloque mono y devuelve `ClapDetected` o None."""
        return self.detector.feed_block(block, now=now)

    def ingest_rms(self, level: float, now: float | None = None):
        return self.detector.feed_rms(level, now=now)

    # --- Integración con el Event Bus (opcional) -----------------------------

    def available(self) -> bool:
        return self.selector.available()

    def usable(self) -> bool:
        """Hay alguna vía para capturar el mic: PortAudio (sounddevice) o ALSA (`arecord`)."""
        return self.selector.available() or shutil.which("arecord") is not None

    def start(self, bus, topic: str = DEFAULT_TOPIC, loop: asyncio.AbstractEventLoop | None = None, run_async: bool = True) -> tuple[bool, str]:
        """Arranca el micrófono (PortAudio si está, si no ALSA/arecord) y publica
        cada ClapDetected en el bus.

        `run_async=True`: el detector pide el bloque de audio en un hilo y
        publica con `asyncio.run_coroutine_threadsafe` usando `loop` (el de la
        app). Una vez detectada la doble palmada, REARMA solo (puede repetirse).
        """
        if self._thread is not None:
            return False, "listener ya activo"
        self._bus = bus
        self._topic = topic
        self._loop = loop or (asyncio.get_event_loop() if asyncio.get_event_loop().is_running() else None)
        self._stop = threading.Event()

        if self.selector.available():
            ok, note = self._start_sounddevice()
            if ok:
                return ok, note
            print(f"[clap] PortAudio no usable por {note!r}; probando arecord…")

        ok, note = self._start_alsa()
        if ok:
            return ok, note
        return False, f"sin micrófono: ni sounddevice/PortAudio ni arecord ({self.selector.choose().note})"

    def _start_sounddevice(self) -> tuple[bool, str]:
        probe = self.selector.choose()
        if probe.device is None:
            return False, probe.note

        import sounddevice as sd

        device = probe.device
        sample_rate = self.selector.sample_rate
        channels = self.selector.channels
        blocksize = max(int(sample_rate * self.selector.block_ms / 1000), 1)

        def _listen():
            self.detector.reset()
            try:
                with sd.InputStream(device=device, samplerate=sample_rate, channels=channels, dtype="float32", blocksize=blocksize) as stream:
                    while not self._stop.is_set():
                        data, overflowed = stream.read(blocksize)
                        if overflowed:
                            continue
                        if os.environ.get("ALEXIS_CLAP_DEBUG") == "1":
                            self._last_rms = float(np.sqrt(np.mean(data**2)))
                        event = self.detector.feed_block(data)
                        if event is not None:
                            self._log_detection(event)
                            self._publish(event)
            except Exception as exc:  # noqa: BLE001 — el sensor no debe tumbar la app
                self._publish(ClapDetected(timestamp=time.time(), confidence=0.0, source="microphone.error"))
                print(f"[clap] micrófono detenido: {exc}")

        self._thread = threading.Thread(target=_listen, name="alexis-clap", daemon=True)
        self._thread.start()
        return True, f"escuchando en dispositivo [{device}]: {probe.note}"

    def _alsa_plug(self, override: str | None) -> str:
        """Resuelve el ALSA plug a usar: override por substring, si no "default".

        Honesto: no inventa dispositivos; si el override no coincide con ninguno,
        cae a "default" y el probe real lo dirá en el log.
        """
        if override:
            try:
                out = subprocess.run(["arecord", "-L"], capture_output=True, text=True, timeout=5).stdout
            except Exception:  # noqa: BLE001
                out = ""
            for name in out.splitlines():
                name = name.strip()
                if not name or name.startswith(("/", "#")) or name in ("null", "discard", "empty"):
                    continue
                if name.endswith(":") or "#" in name:
                    continue
                if override.lower() in name.lower():
                    return name
        return "default"

    def _feed_stream(self, proc) -> None:
        """Loop común: lee S16LE mono del proceso de captura y alimenta el detector."""
        import numpy as np  # import diferido: el listener debe importar sin audio

        sample_rate = self.selector.sample_rate
        samples_per_block = max(int(sample_rate * self.selector.block_ms / 1000), 1)
        chunk_bytes = samples_per_block * 2  # S16_LE mono
        self.detector.reset()
        while not self._stop.is_set() and proc.poll() is None:
            raw = proc.stdout.read(chunk_bytes)
            if not raw or len(raw) < chunk_bytes:
                break
            block = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            if os.environ.get("ALEXIS_CLAP_DEBUG") == "1":
                self._last_rms = float(np.sqrt(np.mean(block**2)))
            event = self.detector.feed_block(block)
            if event is not None:
                self._log_detection(event)
                self._publish(event)
            self._log_floor()

    def _start_alsa(self) -> tuple[bool, str]:
        """Captura del mic: prefiere PipeWire (`parec`) y cae a ALSA (`arecord`).
        Ambos emiten S16_LE mono; el detector es agnóstico del transportista."""
        sample_rate = self.selector.sample_rate
        channels = self.selector.channels

        def parec_cmd() -> list[str]:
            return [
                "parec", "-n", "ALEXIS-clap",
                "--format=s16le",
                "--rate", str(sample_rate),
                "--channels", str(channels),
                "--file-format=raw",
            ]

        def arecord_cmd() -> list[str]:
            device = self._alsa_plug(env_input_device())
            return [
                "arecord", "-q", "-D", device, "-t", "raw",
                "-f", "S16_LE", "-r", str(sample_rate), "-c", str(channels),
            ]

        candidates: list[tuple[list[str], str]] = []
        if shutil.which("parec"):
            candidates.append((parec_cmd(), f"por PipeWire (parec, {sample_rate} Hz)"))
        if shutil.which("arecord"):
            candidates.append((arecord_cmd(), f"por arecord (plug '{self._alsa_plug(env_input_device())}')"))
        if not candidates:
            return False, "ni parec (PipeWire) ni arecord en el PATH"

        def _listen(cmd: list[str], note: str):
            proc = None
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self._feed_stream(proc)
            except Exception as exc:  # noqa: BLE001 — el sensor no debe tumbar la app
                self._publish(ClapDetected(timestamp=time.time(), confidence=0.0, source="microphone.error"))
                print(f"[clap] captura detenida ({note}): {exc}")
            finally:
                if proc is not None:
                    try:
                        proc.terminate()
                    except Exception:  # noqa: BLE001
                        pass

        cmd, note = candidates[0]
        self._thread = threading.Thread(target=_listen, args=(cmd, note), name="alexis-clap", daemon=True)
        self._thread.start()
        return True, f"escuchando {note}"

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _log_detection(self, event: ClapDetected) -> None:
        """Reacción visible SIEMPRE: una palmada detectada se ve en el log del demo."""
        gap = f"gap={event.gap_s:.3f}s" if event.gap_s else "gap=n/a"
        lvl = f" level={event.level:.4f}" if event.level is not None else ""
        print(f"[clap] ¡PALMADA! conf={event.confidence:.3f} {gap}{lvl} → {self._topic}")

    def _log_floor(self) -> None:
        """Debug opt-in (ALEXIS_CLAP_DEBUG=1): muestra el RMS de cada ~0.5 s para
        calibrar el umbral sin tocar el código."""
        if os.environ.get("ALEXIS_CLAP_DEBUG") != "1":
            return
        now = time.monotonic()
        if (now - self._last_debug_print) < 0.5:
            return
        self._last_debug_print = now
        print(
            f"[clap] rms_ultimo={self._last_rms:.4f} "
            f"ruido={self.detector.noise_floor:.4f} "
            f"umbral={self.detector._threshold():.4f}"  # noqa: SLF001 — depuración
        )

    def _publish(self, event: ClapDetected) -> None:
        if self._bus is None:
            return
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._bus.publish(self._topic, event), self._loop)
        else:
            try:
                self._loop = asyncio.new_event_loop() if self._loop is None else self._loop
                self._loop.run_until_complete(self._bus.publish(self._topic, event))
            except RuntimeError:
                print("[clap] sin loop asyncio para publicar el evento")