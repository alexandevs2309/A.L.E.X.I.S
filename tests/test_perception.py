"""Tests de percepción de audio: selección de micrófono y detector de palmadas."""

import math

import numpy as np
import pytest

from alexis.perception.clap_detector import ClapDetector
from alexis.perception.microphone import (
    choose_device_no_probe,
    default_device_index,
    resolve_device_index,
)
from alexis.perception.wakeword import find_wakeword, is_activation, normalize

DEVICES = [
    {"name": "Realtek Audio (In)", "max_input_channels": 2},
    {"name": "USB Mic 4K", "max_input_channels": 1},
    {"name": "Speakers (Out)", "max_input_channels": 0},
]


def _impulse(clap_rms: float, rest_rms: float, blocks: int, at: int) -> np.ndarray:
    level = np.full(blocks, math.sqrt(rest_rms**2), dtype=np.float32)
    level[at] = clap_rms
    return level


def _block(rms: float, n: int = 256) -> np.ndarray:
    base = np.zeros(n, dtype=np.float32)
    base[0] = rms * math.sqrt(n)
    return base


class TestMicrophoneSelection:
    def test_devices_are_pure(self):
        choices = [(i, d) for i, d in enumerate(DEVICES) if d["max_input_channels"] >= 1]
        assert [i for i, _ in choices] == [0, 1]

    def test_resolve_by_index(self):
        assert resolve_device_index("1", DEVICES) == 1

    def test_resolve_by_name_substring(self):
        assert resolve_device_index("USB", DEVICES) == 1

    def test_resolve_no_match(self):
        assert resolve_device_index("nope", DEVICES) is None

    def test_override_wins(self):
        idx, note = choose_device_no_probe(DEVICES, 0, override="USB")
        assert idx == 1
        assert "override" in note

    def test_default_picked(self):
        idx, _ = choose_device_no_probe(DEVICES, 0)
        assert idx == 0

    def test_first_input_when_no_default(self):
        idx, _ = choose_device_no_probe(DEVICES, None)
        assert idx == 0

    def test_output_only_device_rejected(self):
        idx, _ = choose_device_no_probe([DEVICES[2]], 0)
        assert idx is None

    def test_default_device_index_filters(self):
        assert default_device_index(DEVICES, 2) is None


class TestClapDetector:
    def _clap_pair(self, det, t0, first=0.5, second=0.9):
        """Doble palma realista: rearmado inicial, primer pico, silencio, segundo pico.

        Línea de tiempo: quiet@t0, pico@+0.05, quiet@+0.10, pico@+0.20
        → gap = 0.15 s (dentro de la ventana por defecto).
        """
        det.feed_rms(0.001, now=t0)
        det.feed_rms(first, now=t0 + 0.05)
        det.feed_rms(0.001, now=t0 + 0.10)
        return det.feed_rms(second, now=t0 + 0.20)

    def test_silence_no_detection(self):
        det = ClapDetector()
        t = 10.0
        for _ in range(20):
            assert det.feed_rms(0.001, now=t) is None
            t += 0.04

    def test_single_clap_no_double(self):
        det = ClapDetector()
        assert det.feed_rms(0.5, now=0.0) is None
        assert det.feed_rms(0.9, now=0.1) is None

    def test_double_clap_detected(self):
        det = ClapDetector()
        event = self._clap_pair(det, 0.0)
        assert event is not None
        assert event.source == "microphone"
        assert event.gap_s == pytest.approx(0.15, abs=0.01)
        assert 0.0 <= event.confidence <= 1.0

    def test_no_double_without_quietsing_between(self):
        det = ClapDetector()
        now = 0.0
        assert det.feed_rms(0.5, now=now) is None
        now += 0.15
        assert det.feed_rms(0.9, now=now) is None  # sin rearmado -> no cuenta

    def test_gap_too_large_not_a_double(self):
        det = ClapDetector()
        det.feed_rms(0.5, now=0.0)
        det.feed_rms(0.001, now=0.05)
        assert det.feed_rms(0.9, now=1.0) is None  # fuera de max_double_gap_s

    def test_gap_too_small_not_a_double(self):
        det = ClapDetector(min_double_gap_s=0.05)
        det.feed_rms(0.5, now=0.0)
        det.feed_rms(0.001, now=0.02)
        assert det.feed_rms(0.9, now=0.04) is None  # demasiado pronto

    def test_cooldown_prevents_rapid_retrigger(self):
        det = ClapDetector(cooldown_s=0.45)
        assert self._clap_pair(det, 0.0) is not None  # evento en t=0.20 (last=0.20)
        # par completo INMEDIATO al evento: todos los picos quedan bajo cooldown
        det.feed_rms(0.001, now=0.21)
        det.feed_rms(0.5, now=0.26)  # 0.26 - 0.20 = 0.06 < 0.45 --> bloqueado
        det.feed_rms(0.001, now=0.31)
        assert det.feed_rms(0.9, now=0.41) is None  # 0.41 - 0.20 < 0.45 --> bloqueado
        # pasada la ventana de cooldown vuelve a detectar
        det.feed_rms(0.001, now=0.67)
        det.feed_rms(0.5, now=0.72)  # 0.52 >= 0.45 --> primer pico
        det.feed_rms(0.001, now=0.77)
        assert det.feed_rms(0.9, now=0.87) is not None  # gap 0.15 --> evento

    def test_retrigger_after_falling_below_threshold(self):
        det = ClapDetector(spike_ratio=7.0, min_rms=0.012)
        assert self._clap_pair(det, 0.0) is not None
        # con cooldown superado y tras rearmarse, la doble palma vuelve a detectarse
        assert self._clap_pair(det, 0.7) is not None

    def test_feed_block_uses_numpy(self):
        det = ClapDetector()
        det.feed_block(_block(0.001), now=0.0)
        det.feed_block(_block(0.5), now=0.05)
        det.feed_block(_block(0.001), now=0.10)
        event = det.feed_block(_block(0.9), now=0.20)
        assert event is not None

    def test_rms_mono_of_known_signal(self):
        from alexis.perception.clap_detector import rms_mono

        signal = np.full(256, 0.5, dtype=np.float32)
        assert rms_mono(signal) == pytest.approx(0.5, abs=1e-4)


class TestSyntheticClapWithoutMic:
    """Sin micrófono: un doble clap generado como WAV debe detectarse igual.

    Reproduce el proof-of-concept del host: audio sintético real → bloques de
    40 ms → `feed_block` (misma ruta que el listener) → EventBus."""

    RATE = 44100
    BLOCK = int(RATE * 40 / 1000)
    GAP = 0.12

    @staticmethod
    def _make_double_clap() -> np.ndarray:
        n = int(1.2 * TestSyntheticClapWithoutMic.RATE)
        x = np.random.default_rng(7).normal(0.0, 0.001, n).astype(np.float32)
        for t in (0.30, 0.30 + TestSyntheticClapWithoutMic.GAP):
            start = int(t * TestSyntheticClapWithoutMic.RATE)
            for i in range(int(0.025 * TestSyntheticClapWithoutMic.RATE)):
                idx = start + i
                if idx < n:
                    x[idx] += 0.6 * math.exp(-i / (0.008 * TestSyntheticClapWithoutMic.RATE))
        return x

    def test_waveform_fed_like_the_listener_is_detected(self):
        det = ClapDetector(sample_rate=self.RATE, block_ms=40)
        clap = self._make_double_clap()
        events = []
        for start in range(0, len(clap), self.BLOCK):
            event = det.feed_block(clap[start : start + self.BLOCK], now=start / self.RATE)
            if event is not None:
                events.append(event)
        assert len(events) == 1
        assert events[0].gap_s == pytest.approx(self.GAP, abs=0.02)
        assert events[0].confidence >= 0.9
        assert events[0].source == "microphone"

    async def test_waveform_event_reaches_the_event_bus(self):
        import asyncio

        from alexis.events.bus import EventBus

        det = ClapDetector(sample_rate=self.RATE, block_ms=40)
        bus = EventBus()
        sub = bus.subscribe_async()
        clap = self._make_double_clap()
        found = False
        for start in range(0, len(clap), self.BLOCK):
            event = det.feed_block(clap[start : start + self.BLOCK], now=start / self.RATE)
            if event is None:
                continue
            await bus.publish("perception.clap_detected", event)
            item = await asyncio.wait_for(sub.get(), 1.0)
            assert item["topic"] == "perception.clap_detected"
            assert item["payload"].gap_s == pytest.approx(self.GAP, abs=0.02)
            found = True
        assert found, "el clap sintético no llegó al EventBus"


class TestWakeword:
    def test_normalize_removes_accents(self):
        assert normalize("¡ALEXIS!") == "¡alexis!"

    def test_find_alexis(self):
        m = find_wakeword("alexis abre cursor")
        assert m is not None
        assert m.rest == "abre cursor"

    def test_find_within_sentence(self):
        assert is_activation("Hola alexis ¿qué hacés?")
        assert is_activation("Oye alexis, abre Chrome")

    def test_ignores_unrelated(self):
        assert find_wakeword("abre el archivo") is None
        assert not is_activation("")