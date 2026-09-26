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

    @pytest.mark.asyncio
    async def test_speak_works_inside_running_event_loop(self):
        # Regresión: dentro del loop de ALEXIS, `asyncio.run` crasheaba con
        # "cannot be called from a running event loop" y la misión fallaba.
        result = tts_speak("hola", provider=self._FakeProvider())
        assert result["ok"] is True
        assert result["path"] == "/caché/fake.wav"


class TestDesktopRoutePrecision:
    def test_plain_dime_is_not_tts_speak(self):
        # Regresión: "revisa los archivos del workspace y dime cuántos hay"
        # terminaba en tts.speak (crash) en vez de la ruta de filesystem.
        assert desktop_intent("revisa los archivos del workspace y dime cuántos hay") is None
        assert desktop_intent("dime cuál es la capital de Francia") is None
        assert desktop_intent("dime cómo calculas los precios") is None

    def test_explicit_speech_markers_still_trigger_tts(self):
        assert desktop_intent("dime hola en voz alta") == ("tts.speak", {"text": "hola"})
        assert desktop_intent("pronuncia el número cinco por voz") == ("tts.speak", {"text": "el número cinco"})
        assert desktop_intent("repite diez en voz alta") == ("tts.speak", {"text": "diez"})


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
        # P0 §5.5: sin verificación del objetivo no se escribe COMPLETED.
        mission.state = MissionState.NEEDS_VERIFICATION
        # `webbrowser_open` inyectado: el delegado "host" llama a `open_claude()`, y sin
        # este doble el suite abría https://claude.ai/new en el navegador de quien lo
        # ejecutara. Un test no puede tener efectos sobre el escritorio.
        executor = SandboxExecutor(tools=ToolRegistry(), sandbox=SandboxRunner(workspace=tmp_path),
                                   desktop_delegate="host", webbrowser_open=lambda u: None)
        step = PlanStep("execute", "run", "execute", RiskLevel.MEDIUM)
        result = await executor.execute(mission, step)
        mission.results.append({"step": "execute", "success": True, "output": result.output, "error": None})
        verification = await verifier.verify(mission, await Planner().create_plan(mission))
        assert verification.passed is True
        assert verification.confidence >= 0.8

    async def test_desktop_mission_fails_verify_without_dispatch(self, tmp_path):
        verifier = FilesystemVerifier(workspace=tmp_path)
        mission = await _desktop_mission("abre claude code")
        # P0 §5.5: sin verificación del objetivo no se escribe COMPLETED.
        mission.state = MissionState.NEEDS_VERIFICATION
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

def _with_composed_response(mission, *, verdict="success", goal_verified=True):
    """Pega en la misión la respuesta que §5.6.9 compuso (P0 GAP 1).

    Los canales ya no inventan su propia respuesta: leen esta. El test sigue ejerciendo
    el canal (voz, TTS, modelo), pero con la fuente semántica real.
    """
    from alexis.cognition.goal_verification import GoalVerification
    from alexis.cognition.response import ResponseComposer
    from alexis.cognition.state import KnowledgeState

    knowledge = KnowledgeState(objective=mission.goal.objective)
    knowledge.last_verdict = verdict
    knowledge.mark_completed("respond")
    verification = GoalVerification(
        objective=mission.goal.objective, evaluations=[], verified=goal_verified, reason=""
    )
    mission.context["response"] = ResponseComposer().compose(
        mission, knowledge, verification, model_outcome="real"
    ).to_dict()
    return mission


class _FakeLLMRouter:
    def __init__(self, text: str):
        self._text = text

    def providers(self):
        return [object()]

    async def complete(self, request):
        from alexis.models import ModelResponse

        return ModelResponse(text=self._text)


class TestExecutorRespondWithModel:
    async def test_respond_uses_model_when_router_injected(self, tmp_path):
        executor = SandboxExecutor(
            tools=ToolRegistry(),
            sandbox=SandboxRunner(workspace=tmp_path),
            model_router=_FakeLLMRouter("Claro: el gráfico de bitcoin ya está en tu pantalla."),
        )
        mission = _with_composed_response(await _desktop_mission("abre binance"))
        step = PlanStep("respond", "confirma", "respond", RiskLevel.LOW)
        result = await executor.execute(mission, step)
        assert result.output["message"] == "Claro: el gráfico de bitcoin ya está en tu pantalla."

    async def test_respond_falls_back_when_router_has_no_providers(self, tmp_path):
        class _EmptyRouter(_FakeLLMRouter):
            def providers(self):
                return []

        executor = SandboxExecutor(
            tools=ToolRegistry(),
            sandbox=SandboxRunner(workspace=tmp_path),
            model_router=_EmptyRouter("no debo usarse"),
        )
        mission = await _desktop_mission("abre claude code")
        step = PlanStep("respond", "confirma", "respond", RiskLevel.LOW)
        result = await executor.execute(mission, step)
        assert result.output["message"] == desktop_reply("claude.open")

    async def test_respond_keeps_text_but_skips_tts_when_voice_mode_off(self, tmp_path):
        executor = SandboxExecutor(
            tools=ToolRegistry(),
            sandbox=SandboxRunner(workspace=tmp_path),
            model_router=_FakeLLMRouter("Claro, abrí la página."),
            voice_mode_provider=lambda: False,
        )
        mission = _with_composed_response(await _desktop_mission("abre claude code"))
        step = PlanStep("respond", "confirma", "respond", RiskLevel.LOW)
        result = await executor.execute(mission, step)
        assert result.output["message"] == "Claro, abrí la página."
        assert result.output["tts"]["ok"] is False
        assert result.output["tts"]["provider"] == "voice-mode-off"

    async def test_activation_greeting_stays_canonical(self, tmp_path):
        from alexis.perception.activation import ACTIVATION_MARKER

        executor = SandboxExecutor(
            tools=ToolRegistry(),
            sandbox=SandboxRunner(workspace=tmp_path),
            model_router=_FakeLLMRouter("no debo usarse para el saludo"),
        )
        from alexis.autonomy.mission import MissionEngine
        from alexis.contracts import AutonomyLevel, MissionEnvelope

        envelope = MissionEnvelope(
            objective="palmada",
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=["understand", "analyze", "execute", "respond"],
        )
        mission = MissionEngine().create(f"{ACTIVATION_MARKER} activación", envelope)
        step = PlanStep("respond", "saluda", "respond", RiskLevel.LOW)
        result = await executor.execute(mission, step)
        assert "¿En qué te ayudo" in result.output["message"]


class _StubProvider:
    def __init__(self, available=True, delay=0.0, text=None):
        self.available = available
        self.delay = delay
        self.text = text

    async def complete(self, request):
        from alexis.models import ModelResponse

        if self.delay:
            await asyncio.sleep(self.delay)
        if self.text is None:
            return ModelResponse(text="   ")
        return ModelResponse(text=self.text)


class TestRaceProviders:
    async def _executor(self, tmp_path):
        return SandboxExecutor(tools=ToolRegistry(), sandbox=SandboxRunner(workspace=tmp_path))

    async def _request(self):
        from alexis.models import ModelRequest, ModelTask

        return ModelRequest(
            task=ModelTask.SYNTHESIZE,
            system="sistema",
            messages=[{"role": "user", "content": "pregunta"}],
            max_tokens=50,
            temperature=0.7,
            deadline_ms=3000,
        )

    async def test_first_valid_wins_even_if_not_first_to_finish(self, tmp_path):
        executor = await self._executor(tmp_path)
        text = await executor._race_providers(
            [_StubProvider(delay=0.01, text=None), _StubProvider(delay=0.05, text="ganó"), _StubProvider(delay=0.09, text="nunca")],
            await self._request(),
        )
        assert text == "ganó"

    async def test_ignores_empty_responses_and_keeps_waiting(self, tmp_path):
        executor = await self._executor(tmp_path)
        text = await executor._race_providers(
            [_StubProvider(delay=0.01, text=None), _StubProvider(delay=0.06, text="respuesta fiable")],
            await self._request(),
        )
        assert text == "respuesta fiable"

    async def test_unavailable_providers_are_skipped(self, tmp_path):
        executor = await self._executor(tmp_path)
        text = await executor._race_providers(
            [_StubProvider(available=False, text="no"), _StubProvider(delay=0.02, text="local ok")],
            await self._request(),
        )
        assert text == "local ok"

    async def test_all_empty_yields_blank(self, tmp_path):
        executor = await self._executor(tmp_path)
        text = await executor._race_providers(
            [_StubProvider(delay=0.01, text=None), _StubProvider(delay=0.02, text=None)],
            await self._request(),
        )
        assert text == ""
