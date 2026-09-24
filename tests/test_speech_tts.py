"""Tests de TTS del servidor: fábrica, caché y compatibilidad de config (sin red)."""

import sys

import pytest

import alexis.speech.elevenlabs as el
from alexis.speech.edge import SPANISH_VOICES, EdgeTTSProvider, edge_voice
from alexis.speech.elevenlabs import (
    ElevenLabsProvider,
    elevenlabs_env_config,
    load_cached_wav,
    pcm_sample_rate,
    store_cached_wav,
    tts_cache_dir,
    tts_cache_key,
    tts_cache_path,
)
from alexis.speech.tts import NoopTTSProvider, get_tts_provider, provider_from_env


class TestTTSConfig:
    def test_elevenlabs_namespace_with_javis_fallback(self, monkeypatch):
        monkeypatch.setenv("ELEVENLABS_API_KEY", "jarvis-key")
        monkeypatch.setenv("ALEXIS_ELEVENLABS_VOICE_ID", "vox")
        cfg = elevenlabs_env_config()
        assert cfg["api_key"] == "jarvis-key"
        assert cfg["voice_id"] == "vox"

    def test_alexis_prefix_takes_priority(self, monkeypatch):
        monkeypatch.setenv("ELEVENLABS_API_KEY", "old")
        monkeypatch.setenv("ALEXIS_ELEVENLABS_API_KEY", "new")
        assert elevenlabs_env_config()["api_key"] == "new"

    def test_model_defaults(self, monkeypatch):
        monkeypatch.delenv("ELEVENLABS_MODEL_ID", raising=False)
        monkeypatch.delenv("ALEXIS_ELEVENLABS_MODEL_ID", raising=False)
        assert elevenlabs_env_config()["model_id"] == "eleven_multilingual_v2"

    def test_pcm_rate_from_format(self):
        assert pcm_sample_rate("pcm_24000") == 24000
        assert pcm_sample_rate("pcm_16000") == 16000

    def test_non_pcm_format_falls_back_to_default(self):
        assert pcm_sample_rate("mp3_44100") == 24000

    def test_pcm_override_env(self, monkeypatch):
        monkeypatch.setenv("ELEVENLABS_PCM_SAMPLE_RATE", "16000")
        assert pcm_sample_rate("pcm_24000") == 16000


class TestTTSCache:
    def test_cache_key_deterministic(self):
        assert tts_cache_key("hola", "v", "m", "pcm_24000") == tts_cache_key("hola", "v", "m", "pcm_24000")
        assert tts_cache_key("hola", "v", "m", "pcm_24000") != tts_cache_key("hola", "v", "others", "pcm_24000")

    def test_cache_path_uses_key(self):
        path = tts_cache_path("texto", "v", "m", "pcm_24000")
        assert path.name == f"{tts_cache_key('texto', 'v', 'm', 'pcm_24000')}.wav"

    def test_store_and_load_roundtrip(self, tmp_path):
        target = tmp_path / "c.wav"
        store_cached_wav(target, b"\x00\x00\xff\x7f\x00\x00\x01\x80", sample_rate=24000)
        assert load_cached_wav(target) == b"\x00\x00\xff\x7f\x00\x00\x01\x80"

    def test_load_missing_returns_none(self, tmp_path):
        assert load_cached_wav(tmp_path / "missing.wav") is None

    def test_skip_unstable_cache_dir(self, monkeypatch):
        monkeypatch.setenv("ALEXIS_TTS_CACHE_DIR", str(tts_cache_dir()))
        assert tts_cache_dir().name == "tts"


class TestTTSProviders:
    async def test_noop_is_honest(self):
        result = await NoopTTSProvider().synthesize("hola")
        assert result.ok is False
        assert "proveedor" in result.error

    async def test_elevenlabs_without_creds_is_honest(self):
        provider = ElevenLabsProvider({"api_key": None, "voice_id": None})
        result = await provider.synthesize("hola")
        assert result.ok is False
        assert "API key" in result.message

    def test_factory_elevenlabs_when_key_present(self, monkeypatch):
        monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
        monkeypatch.setenv("ELEVENLABS_VOICE_ID", "y")
        assert get_tts_provider().name == "elevenlabs"

    def test_factory_none_without_keys(self, monkeypatch):
        monkeypatch.delenv("ALEXIS_TTS_PROVIDER", raising=False)
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        monkeypatch.delenv("ALEXIS_ELEVENLABS_API_KEY", raising=False)
        monkeypatch.setitem(sys.modules, "edge_tts", None)  # simula edge-tts no instalado
        assert get_tts_provider().name == "none"

    def test_factory_edge_when_available(self, monkeypatch):
        import types

        monkeypatch.setitem(sys.modules, "edge_tts", types.ModuleType("edge_tts"))
        monkeypatch.delenv("ALEXIS_TTS_PROVIDER", raising=False)
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        assert provider_from_env() == "edge"
        assert get_tts_provider().name == "edge"


class TestEdgeTTS:
    def test_edge_voice_default(self):
        assert edge_voice() == "es-MX-DaliaNeural"

    def test_edge_voice_respects_env(self, monkeypatch):
        monkeypatch.setenv("ALEXIS_EDGE_VOICE", "es-CO-GonzaloNeural")
        assert edge_voice() == "es-CO-GonzaloNeural"

    def test_edge_voice_unknown_falls_back(self, monkeypatch):
        monkeypatch.setenv("ALEXIS_EDGE_VOICE", "zz-ZZ-Pepita")
        assert edge_voice() == "es-MX-DaliaNeural"

    async def test_edge_honest_without_engine(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "edge_tts", None)
        result = await EdgeTTSProvider().synthesize("hola")
        assert result.ok is False
        assert "edge-tts" in result.message

    def test_edge_voice_list_is_latin_neural(self):
        for voice, label in SPANISH_VOICES.items():
            assert voice.startswith("es-")
            assert voice.endswith("Neural")
            assert label.strip()