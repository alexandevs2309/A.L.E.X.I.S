"""Selección de micrófono para la percepción de audio de ALEXIS.

Port de la lógica de `jarvis-main` (selección automática de dispositivo, probe
de silencio, override por variable de entorno) manteniendo el comportamiento
original, pero sin acoplarse a `sounddevice` en el import: el binding se importa
de forma perezosa y la selección determinista es pura (testeable sin audio).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

INPUT_PROBE_S = 0.5
INPUT_SILENT_RMS = 0.001
DEFAULT_SAMPLE_RATE = 44100
DEFAULT_BLOCK_MS = 40
DEFAULT_CHANNELS = 1


def env_input_device() -> str | None:
    """Override por entorno: `ALEXIS_INPUT_DEVICE` y compat temporal `JARVIS_INPUT_DEVICE`."""
    for key in ("ALEXIS_INPUT_DEVICE", "JARVIS_INPUT_DEVICE"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return None


def input_devices(devices: list[dict]) -> list[tuple[int, dict]]:
    """Filtra los dispositivos con al menos un canal de entrada."""
    return [
        (idx, dev)
        for idx, dev in enumerate(devices)
        if dev.get("max_input_channels", 0) >= 1
    ]


def resolve_device_index(spec: str, devices: list[dict]) -> int | None:
    """Resuelve un override: índice entero o substring del nombre del dispositivo."""
    spec = spec.strip()
    if spec.isdigit():
        return int(spec)
    needle = spec.lower()
    for idx, dev in input_devices(devices):
        if needle in dev["name"].lower():
            return idx
    return None


def default_device_index(devices: list[dict], current_default: int | None) -> int | None:
    """Devuelve el índice del dispositivo por defecto si tiene canales de entrada."""
    if current_default is None or current_default < 0:
        return None
    for idx, dev in input_devices(devices):
        if idx == current_default:
            return idx
    return None


def choose_device_no_probe(
    devices: list[dict],
    current_default: int | None,
    override: str | None = None,
) -> tuple[int | None, str]:
    """Selección determinista SIN audio (para tests y para runtimes sin mic).

    Orden: override explícito → default → primer input detectado.
    Retorna (índice, nota) o (None, "no hay dispositivos de entrada").
    """
    if override:
        idx = resolve_device_index(override, devices)
        if idx is None:
            return None, f"no hay dispositivo de entrada que coincida con {override!r}"
        return idx, f"override {override!r} -> dispositivo [{idx}]"
    idx = default_device_index(devices, current_default)
    if idx is not None:
        return idx, f"dispositivo por defecto [{idx}]"
    inputs = input_devices(devices)
    if not inputs:
        return None, "no hay dispositivos de entrada"
    idx, _ = inputs[0]
    return idx, f"primer dispositivo de entrada [{idx}]"


@dataclass
class ProbeResult:
    device: int | None
    peak_rms: float | None
    note: str


class MicrophoneSelector:
    """Selector real con probe de silencio y auto-selección del mic más fuerte.

    Requiere `sounddevice` instalado (extra `[desktop]`); si no está disponible,
    `available()` devuelve False y `choose()` cae a la selección determinista.
    """

    def __init__(
        self,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        block_ms: int = DEFAULT_BLOCK_MS,
        channels: int = DEFAULT_CHANNELS,
        probe_s: float = INPUT_PROBE_S,
        silent_rms: float = INPUT_SILENT_RMS,
    ):
        self.sample_rate = sample_rate
        self.block_ms = block_ms
        self.channels = channels
        self.probe_s = probe_s
        self.silent_rms = silent_rms

    def _blocksize(self) -> int:
        return max(int(self.sample_rate * self.block_ms / 1000), 1)

    def available(self) -> bool:
        try:
            import sounddevice  # noqa: F401

            return True
        except Exception:
            return False

    def _probe(self, sd, device: int, blocksize: int) -> float | None:
        try:
            with sd.InputStream(
                device=device,
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="float32",
                blocksize=blocksize,
            ) as stream:
                peak = 0.0
                deadline = time.monotonic() + self.probe_s
                while time.monotonic() < deadline:
                    data, _ = stream.read(blocksize)
                    peak = max(peak, _rms_mono(data))
                return float(peak)
        except Exception:
            return None

    def choose(self) -> ProbeResult:
        """Elija un micrófono real probando ruido; degrada a selección pura si falta audio."""
        override = env_input_device()
        blocked = self._blocksize()
        if not self.available():
            try:
                import sounddevice as sd

                devices = list(sd.query_devices())
                default = sd.default.device[0] if sd.default.device else None
            except Exception:
                devices, default = [], None
            idx, note = choose_device_no_probe(devices, default, override)
            return ProbeResult(idx, None, f"{note} (sin sounddevice/PortAudio)")
        import sounddevice as sd

        devices = list(sd.query_devices())
        default = sd.default.device[0] if sd.default.device else None
        if override:
            idx = resolve_device_index(override, devices)
            if idx is None:
                return ProbeResult(None, None, f"override {override!r} sin coincidencia")
            peak = self._probe(sd, idx, blocked)
            note = f"override [{idx}]: {devices[idx]['name']}"
            if peak is None:
                note += " (no abrible; se probará igual)"
            elif peak < self.silent_rms:
                note += f" (probe rms={peak:.5f}: silencioso)"
            else:
                note += f" (probe OK rms={peak:.5f})"
            return ProbeResult(idx, peak, note)
        if default is not None and default >= 0:
            peak = self._probe(sd, default, blocked)
            if peak is not None and peak >= self.silent_rms:
                return ProbeResult(default, peak, f"default [{default}]: probe OK rms={peak:.5f}")
        best_idx, best_peak = None, -1.0
        for idx, dev in input_devices(devices):
            if default is not None and idx == default:
                continue
            peak = self._probe(sd, idx, blocked)
            if peak is not None and peak > best_peak:
                best_idx, best_peak = idx, peak
        if best_idx is not None and best_peak >= self.silent_rms:
            return ProbeResult(best_idx, best_peak, f"auto-selección [{best_idx}]: probe rms={best_peak:.5f}")
        idx, note = choose_device_no_probe(devices, default, override)
        return ProbeResult(idx, None, f"{note} (sin activo, fallback)")


def _rms_mono(block) -> float:
    import numpy as np

    arr = np.asarray(block, dtype=np.float64)
    if arr.ndim > 1:
        arr = arr.mean(axis=1)
    if arr.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(arr**2)))