"""FASE 4/5: tools de escritorio en el ToolRegistry (correctas y honestas).

La palmada NO ejecuta estas tools: están a disposición del orchestrador vía
intención de misión. Aquí solo se verifica registro, schema, riesgo y el
comportamiento honesto en Linux (sin navegador real, sin proceso real).
"""

import asyncio

import pytest

from alexis.autonomy.mission import MissionEngine
from alexis.cognition.planner import Planner
from alexis.contracts import AutonomyLevel, MissionEnvelope, MissionState, PlanStep, RiskLevel
from alexis.execution import SandboxExecutor
from alexis.security.sandbox import SandboxRunner
from alexis.tools.desktop import (
    build_desktop_tools,
    chrome_open,
    cursor_open,
    desktop_intent,
    desktop_reply,
    open_binance,
    open_claude,
    spotify_play,
    tts_speak,
)
from alexis.tools.registry import ToolRegistry
from alexis.verification import FilesystemVerifier

NAMES = {"spotify.play", "chrome.open_url", "claude.open", "binance.open", "cursor.open", "tts.speak"}
RISKS = {"spotify.play": "low", "chrome.open_url": "medium", "claude.open": "medium",
         "binance.open": "medium", "cursor.open": "medium", "tts.speak": "low"}


class TestRegistry:
    def test_all_desktop_tools_registered(self):
        registry = ToolRegistry()
        registry.register_all(build_desktop_tools())
        assert {t.name for t in registry.list()} == NAMES

    def test_risk_levels(self):
        registry = ToolRegistry()
        registry.register_all(build_desktop_tools())
        assert {t.name: t.risk for t in registry.list()} == RISKS

    def test_each_tool_has_schema_and_timeout(self):
        registry = ToolRegistry()
        registry.register_all(build_desktop_tools())
        for t in registry.list():
            assert t.schema is not None
            assert t.timeout


class TestChrome:
    def test_system_browser_when_no_chrome(self):
        opened = []
        result = chrome_open("https://claude.ai/new", webbrowser_open=lambda u: opened.append(u), chrome=None)
        assert result == {"ok": True, "engine": "system-browser", "url": "https://claude.ai/new"}
        assert opened == ["https://claude.ai/new"]

    def test_empty_url_honest(self):
        assert chrome_open("  ", webbrowser_open=lambda u: [])["ok"] is False

    def test_open_claude_uses_configured_url(self):
        opened = []
        result = open_claude(webbrowser_open=lambda u: opened.append(u), chrome=None)
        assert result["ok"] is True
        assert opened[0].startswith("https://")


class TestSpotify:
    def test_plays_uri_via_opener(self):
        opened = []
        result = spotify_play("https://open.spotify.com/track/x", webbrowser_open=lambda u: opened.append(u))
        assert result == {"ok": True, "uri": "https://open.spotify.com/track/x"}
        assert opened == ["https://open.spotify.com/track/x"]

    def test_missing_uri_honest(self):
        assert spotify_play("", webbrowser_open=lambda u: [])["ok"] is False


class TestCursor:
    def test_honest_failure_when_not_installed(self):
        result = cursor_open(executable=None)
        assert result["ok"] is False
        assert "no encontrado" in result["reason"]


class TestTtsSpeak:
    class _FakeProvider:
        name = "fake"

        async def synthesize(self, text, voice_id=None, model_id=None):
            from alexis.speech.tts import TTSResult

            return TTSResult(True, "fake", message="ok", path="/caché/fake.wav")

    def test_speak_returns_result_with_path(self):
        result = tts_speak("hola", provider=self._FakeProvider())
        assert result["ok"] is True
        assert result["provider"] == "fake"
        assert result["path"] == "/caché/fake.wav"

    def test_speak_empty_text_honest(self):
        result = tts_speak("", provider=self._FakeProvider())
        assert result["ok"] is False


class TestHandlersAsync:
    async def test_handlers_are_callable_and_honest(self):
        registry = ToolRegistry()
        registry.register_all(build_desktop_tools(webbrowser_open=lambda u: None))
        claude = registry.get("claude.open")
        out = await claude.handler()
        assert out["ok"] is True
        spotify = registry.get("spotify.play")
        out = await spotify.handler(uri="https://open.spotify.com/track/x")
        assert out["ok"] is True
        empty = await spotify.handler(uri="")
        assert empty["ok"] is False


class TestDesktopIntent:
    def test_open_claude(self):
        assert desktop_intent("abre claude code") == ("claude.open", {})
        assert desktop_intent("Ábreme Claude") == ("claude.open", {})

    def test_open_cursor(self):
        assert desktop_intent("abre cursor") == ("cursor.open", {})

    def test_open_binance(self):
        assert desktop_intent("abre binance") == ("binance.open", {})
        assert desktop_intent("abre el gráfico de bitcoin") == ("binance.open", {})

    def test_open_url(self):
        assert desktop_intent("abre github.com/proj") == ("chrome.open_url", {"url": "github.com/proj"})
        assert desktop_intent("abre https://claude.ai/new") == ("chrome.open_url", {"url": "https://claude.ai/new"})

    def test_spotify(self):
        assert desktop_intent("pon la canción en spotify") == ("spotify.play", {})
        assert desktop_intent("ponme música") == ("spotify.play", {})

    def test_speak(self):
        assert desktop_intent("dime hola en voz alta") == ("tts.speak", {"text": "hola"})
        assert desktop_intent("pronuncia el número cinco por voz") == ("tts.speak", {"text": "el número cinco"})

    def test_asking_info_does_not_trigger(self):
        assert desktop_intent("dame información sobre bitcoin") is None
        assert desktop_intent("qué es claude") is None
        assert desktop_intent("") is None


class TestPlannerDesktop:
    async def test_desktop_plan_produces_execute_and_respond(self):
        envelope = MissionEnvelope(
            objective="abre claude code",
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=["understand", "analyze", "execute", "respond"],
        )
        mission = MissionEngine().create("abre claude code", envelope)
        plan = await Planner().create_plan(mission)
        assert [s.action for s in plan.steps] == ["analyze", "execute", "respond"]
        assert all(s.requires_approval is False for s in plan.steps)


class TestExecutorDesktopDispatch:
    async def _mission(self, objective, tmp_path):
        envelope = MissionEnvelope(
            objective=objective,
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=["understand", "analyze", "execute", "respond"],
        )
        return MissionEngine().create(objective, envelope)

    async def test_executes_registered_desktop_tool(self, tmp_path):
        registry = ToolRegistry()
        registry.register_all(build_desktop_tools(webbrowser_open=lambda u: None))
        executor = SandboxExecutor(tools=registry, sandbox=SandboxRunner(workspace=tmp_path))
        mission = await self._mission("abre https://example.com/", tmp_path)
        step = PlanStep("execute", "run", "execute", RiskLevel.MEDIUM)
        result = await executor.execute(mission, step)
        assert result.success is True
        assert result.output["ok"] is True
        assert result.output["url"] == "https://example.com/"

    async def test_executor_returns_honest_error_when_tool_missing(self, tmp_path):
        executor = SandboxExecutor(tools=ToolRegistry(), sandbox=SandboxRunner(workspace=tmp_path))
        mission = await self._mission("abre claude code", tmp_path)
        step = PlanStep("execute", "run", "execute", RiskLevel.MEDIUM)
        result = await executor.execute(mission, step)
        assert result.success is False
        assert "claude.open" in result.error

    async def test_respond_confirms_desktop_action(self, tmp_path, monkeypatch):
        class _FakeProvider:
            name = "fake"

            async def synthesize(self, text, voice_id=None, model_id=None):
                from alexis.speech.tts import TTSResult

                return TTSResult(True, "fake", message="ok", path="/tmp/fake.wav")

        monkeypatch.setattr("alexis.execution.get_tts_provider", lambda: _FakeProvider())
        executor = SandboxExecutor(tools=ToolRegistry(), sandbox=SandboxRunner(workspace=tmp_path))
        mission = await self._mission("abre claude code", tmp_path)
        step = PlanStep("respond", "confirma", "respond", RiskLevel.LOW)
        result = await executor.execute(mission, step)
        assert result.success is True
        assert result.output["message"] == desktop_reply("claude.open")

    async def test_respond_chrome_includes_url(self, tmp_path, monkeypatch):
        class _FakeProvider:
            name = "fake"

            async def synthesize(self, text, voice_id=None, model_id=None):
                from alexis.speech.tts import TTSResult

                return TTSResult(True, "fake", message="ok", path="/tmp/fake.wav")

        monkeypatch.setattr("alexis.execution.get_tts_provider", lambda: _FakeProvider())
        executor = SandboxExecutor(tools=ToolRegistry(), sandbox=SandboxRunner(workspace=tmp_path))
        mission = await self._mission("abre https://ejemplo.dev/x", tmp_path)
        step = PlanStep("respond", "confirma", "respond", RiskLevel.LOW)
        result = await executor.execute(mission, step)
        message = result.output["message"]
        assert "ejemplo.dev/x" in message
        assert "abrí https://ejemplo.dev/x en el navegador" not in message


class TestVerifierDesktop:
    async def test_desktop_mission_verifies_on_dispatch(self, tmp_path):
        verifier = FilesystemVerifier(workspace=tmp_path)
        mission = await _desktop_mission("abre claude code")
        mission.state = MissionState.COMPLETED
        executor = SandboxExecutor(tools=ToolRegistry(), sandbox=SandboxRunner(workspace=tmp_path), desktop_delegate="host")
        step = PlanStep("execute", "run", "execute", RiskLevel.MEDIUM)
        result = await executor.execute(mission, step)
        mission.results.append({"step": "execute", "success": True, "output": result.output, "error": None})
        verification = await verifier.verify(mission, await Planner().create_plan(mission))
        assert verification.passed is True
        assert verification.confidence >= 0.8

    async def test_desktop_mission_fails_verify_without_dispatch(self, tmp_path):
        verifier = FilesystemVerifier(workspace=tmp_path)
        mission = await _desktop_mission("abre claude code")
        mission.state = MissionState.COMPLETED
        verification = await verifier.verify(mission, await Planner().create_plan(mission))
        assert verification.passed is False
        assert "no fue despachada" in verification.notes


async def _desktop_mission(objective):
    envelope = MissionEnvelope(
        objective=objective,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=["understand", "analyze", "execute", "respond"],
    )
    return MissionEngine().create(objective, envelope)