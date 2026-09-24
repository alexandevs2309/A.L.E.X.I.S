"""Detector de doble palmada de ALEXIS, portado de `jarvis-main`.

Máquina de estados pura sobre RMS: el detector recibe bloques de audio mono
(`feed_block`) o valores RMS ya calculados (`feed_rms`) y emite un evento
`ClapDetected` cuando dos picos transitorios ocurren en el ventana permitida.

Es determinista respecto del reloj inyectado (`now`), por lo que es testeable
sin dispositivo de audio. El evento es el mismo que consumirá el Event Bus
(`alexis.events.bus`), desacoplado de la ejecución de acciones.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

DEFAULT_SAMPLE_RATE = 44100
DEFAULT_BLOCK_MS = 40
DEFAULT_SPIKE_RATIO = 7.0
DEFAULT_COOLDOWN_S = 0.45
DEFAULT_MIN_DOUBLE_GAP_S = 0.05
DEFAULT_MAX_DOUBLE_GAP_S = 0.35
DEFAULT_RETRIGGER_RATIO = 0.55
DEFAULT_NOISE_FLOOR_ALPHA = 0.992
DEFAULT_MIN_RMS = 0.012
DEFAULT_QUIET_GATE_MULT = 2.2


@dataclass
class ClapDetected:
    """Evento de activación por doble palmada (percepción → Event Bus)."""

    timestamp: float
    confidence: float
    source: str = "microphone"
    gap_s: float | None = None
    level: float | None = None


def rms_mono(block) -> float:
    import numpy as np

    arr = np.asarray(block, dtype=np.float64)
    if arr.ndim > 1:
        arr = arr.mean(axis=1)
    if arr.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(arr**2)))


class ClapDetector:
    """Detecta una doble palmada dentro de un rango de separación.

    El estado interno replica la lógica del script original (noise floor
    adaptativo, umbral relativo, retrigger, cooldown). Una detección completa
    devuelve exactamente un `ClapDetected` desde un único `feed_*`.
    """

    def __init__(
        self,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        block_ms: int = DEFAULT_BLOCK_MS,
        spike_ratio: float = DEFAULT_SPIKE_RATIO,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
        min_double_gap_s: float = DEFAULT_MIN_DOUBLE_GAP_S,
        max_double_gap_s: float = DEFAULT_MAX_DOUBLE_GAP_S,
        retrigger_ratio: float = DEFAULT_RETRIGGER_RATIO,
        noise_floor_alpha: float = DEFAULT_NOISE_FLOOR_ALPHA,
        min_rms: float = DEFAULT_MIN_RMS,
        quiet_gate_mult: float = DEFAULT_QUIET_GATE_MULT,
    ):
        self.sample_rate = sample_rate
        self.block_ms = block_ms
        self.spike_ratio = spike_ratio
        self.cooldown_s = cooldown_s
        self.min_double_gap_s = min_double_gap_s
        self.max_double_gap_s = max_double_gap_s
        self.retrigger_ratio = retrigger_ratio
        self.noise_floor_alpha = noise_floor_alpha
        self.min_rms = min_rms
        self.quiet_gate_mult = quiet_gate_mult

        self._noise_floor = 1e-4
        self._spike_armed = True
        self._first_clap_time: float | None = None
        self._last_logged_double = -1.0

    def _threshold(self) -> float:
        return max(self._noise_floor * self.spike_ratio, self.min_rms)

    @property
    def noise_floor(self) -> float:
        return self._noise_floor

    def reset(self) -> None:
        self._noise_floor = 1e-4
        self._spike_armed = True
        self._first_clap_time = None
        self._last_logged_double = -1.0

    def feed_block(self, block, now: float | None = None):
        """Procesa un bloque de audio mono y devuelve ClapDetected o None."""
        return self.feed_rms(rms_mono(block), now=now)

    def feed_rms(self, level: float, now: float | None = None):
        """Procesa un nivel RMS; devuelve `ClapDetected` si completó una doble palmada."""
        now = now if now is not None else time.monotonic()

        quiet_gate = self._noise_floor * self.quiet_gate_mult
        if level < quiet_gate:
            self._noise_floor = self.noise_floor_alpha * self._noise_floor + (
                1.0 - self.noise_floor_alpha
            ) * level
            self._noise_floor = max(self._noise_floor, 1e-7)

        threshold = self._threshold()
        retrigger_level = threshold * self.retrigger_ratio

        if level < retrigger_level:
            self._spike_armed = True

        if not (self._spike_armed and level >= threshold):
            return None
        if (now - self._last_logged_double) < self.cooldown_s:
            return None

        self._spike_armed = False
        if self._first_clap_time is None:
            self._first_clap_time = now
            return None

        gap = now - self._first_clap_time
        if gap < self.min_double_gap_s:
            return None
        if gap <= self.max_double_gap_s:
            self._first_clap_time = None
            self._last_logged_double = now
            confidence = self._confidence(gap, level, threshold)
            return ClapDetected(
                timestamp=now,
                confidence=confidence,
                source="microphone",
                gap_s=gap,
                level=level,
            )
        self._first_clap_time = now
        return None

    def _confidence(self, gap_s: float, level: float, threshold: float) -> float:
        """Heurística 0..1: golpes cerca de ~0.15 s y muy sobre el umbral pesan más."""
        gap_factor = 1.0 - abs(gap_s - 0.15) / 0.4
        level_factor = level / max(threshold, 1e-9)
        raw = 0.35 + 0.45 * max(0.0, min(1.0, gap_factor)) + 0.20 * max(0.0, min(1.0, level_factor))
        return round(max(0.0, min(1.0, raw)), 3)