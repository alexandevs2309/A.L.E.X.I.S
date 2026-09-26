"""Ruta de conversación (chat): preguntas e información → respuesta de voz por
el model router, en vez de mandarlas a fs.read y acabar en BLOCKED."""

import pytest

from alexis.autonomy.mission import MissionEngine
from alexis.cognition.planner import Planner
from alexis.contracts import (
    AutonomyLevel,
    MissionEnvelope,
    MissionState,
    PlanStep,
    RiskLevel,
)
from alexis.execution import SandboxExecutor
from alexis.security.sandbox import SandboxRunner
from alexis.tools.registry import ToolRegistry
from alexis.verification import FilesystemVerifier


async def _mission(objective):
    envelope = MissionEnvelope(
        objective=objective,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=["understand", "analyze", "execute", "respond"],
    )
    return MissionEngine().create(objective, envelope)


class _FakeLLMRouter:
    """Router falso con un provider REAL que devuelve un texto dado."""

    def __init__(self, text: str):
        self._text = text

    def providers(self):
        return [object()]

    async def complete(self, request):
        from alexis.models import ModelResponse

        return ModelResponse(text=self._text)


class _FakeTTS:
    async def synthesize(self, text, voice_id=None, model_id=None):
        from alexis.speech.tts import TTSResult

        return TTSResult(True, "fake", message="ok", path="/tmp/fake.wav")


@pytest.fixture
def fake_tts(monkeypatch):
    monkeypatch.setattr("alexis.execution.get_tts_provider", lambda: _FakeTTS())


class TestPlannerChat:
    async def test_question_makes_chat_plan(self):
        plan = await Planner().create_plan(await _mission("¿qué puedes hacer por mí?"))
        assert [s.action for s in plan.steps] == ["analyze", "respond"]
        assert plan.steps[1].action == "respond"

    async def test_file_read_is_not_chat(self):
        plan = await Planner().create_plan(await _mission("lee README.txt"))
        assert [s.action for s in plan.steps] == ["analyze", "research", "execute", "verify"]

    async def test_write_is_not_chat(self):
        plan = await Planner().create_plan(await _mission("crea un archivo notas.txt"))
        assert [s.action for s in plan.steps] == ["analyze", "research", "execute", "verify"]


class TestExecutorChat:
    async def test_chat_respond_uses_model(self, tmp_path, fake_tts):
        executor = SandboxExecutor(
            tools=ToolRegistry(),
            sandbox=SandboxRunner(workspace=tmp_path),
            model_router=_FakeLLMRouter(
                "Puedo leer y crear archivos en tu workspace, abrir sitios o aplicaciones "
                "y responder por voz a tus preguntas."
            ),
        )
        mission = await _mission("¿qué puedes hacer por mí?")
        step = PlanStep("respond", "responde", "respond", RiskLevel.LOW)
        result = await executor.execute(mission, step)
        assert "workspace" in result.output["message"]
        assert result.output["tts"]["ok"] is True

    async def test_chat_fallback_is_honest_when_no_model(self, tmp_path):
        class _Empty(_FakeLLMRouter):
            def providers(self):
                return []

        executor = SandboxExecutor(
            tools=ToolRegistry(),
            sandbox=SandboxRunner(workspace=tmp_path),
            model_router=_Empty("ignorar"),
        )
        mission = await _mission("¿en qué puedes ayudarme?")
        step = PlanStep("respond", "responde", "respond", RiskLevel.LOW)
        result = await executor.execute(mission, step)
        assert "No pude responder" in result.output["message"]


class TestVerifierChat:
    async def test_chat_mission_passes_with_respond(self, tmp_path):
        verifier = FilesystemVerifier(workspace=tmp_path)
        mission = await _mission("¿qué puedes hacer por mí?")
        # P0 §5.5: `COMPLETED` ya no se puede escribir sin verificación del objetivo.
        # Al verificador del plan le da igual el estado; lo que se prueba aquí es su
        # veredicto sobre los resultados, no el estado final de la misión.
        mission.state = MissionState.NEEDS_VERIFICATION
        mission.results.append(
            {
                "step": "respond",
                "success": True,
                "output": {"action": "respond", "message": "Respuesta de ALEXIS."},
                "error": None,
            }
        )
        verification = await verifier.verify(mission, await Planner().create_plan(mission))
        assert verification.passed is True
        assert verification.confidence >= 0.8

    async def test_chat_mission_fails_without_respond(self, tmp_path):
        verifier = FilesystemVerifier(workspace=tmp_path)
        mission = await _mission("¿qué puedes hacer por mí?")
        mission.state = MissionState.NEEDS_VERIFICATION
        verification = await verifier.verify(mission, await Planner().create_plan(mission))
        assert verification.passed is False


@pytest.mark.asyncio
async def test_chat_route_end_to_end_planner_executor_verifier(tmp_path, fake_tts):
    planner = Planner()
    mission = await _mission("¿cuál es tu mejor cualidad?")
    plan = await planner.create_plan(mission)
    executor = SandboxExecutor(
        tools=ToolRegistry(),
        sandbox=SandboxRunner(workspace=tmp_path),
        model_router=_FakeLLMRouter("Mi mejor cualidad es la honestidad al confirmar lo que hago."),
    )
    mission.results.append(
        {
            "step": "understand",
            "success": True,
            "output": {"analysis": "ok"},
            "error": None,
        }
    )
    step = plan.steps[1]
    result = await executor.execute(mission, step)
    mission.results.append({"step": "respond", "success": True, "output": result.output, "error": None})
    verification = await FilesystemVerifier(workspace=tmp_path).verify(mission, plan)
    assert result.output["message"] == "Mi mejor cualidad es la honestidad al confirmar lo que hago."
    assert verification.passed is True


@pytest.mark.asyncio
async def test_model_cannot_downgrade_a_clear_task_verb():
    """Un modelo pequeño que desclasifica un verbo de tarea claro pierde ante las reglas."""
    from alexis.cognition.intent_classifier import IntentClassifier
    from alexis.models import ModelResponse

    class _WeakModel:
        def providers(self):
            return [object()]

        async def complete(self, request):
            return ModelResponse(text='{"kind":"greeting","objective":null,"confidence":0.95}')

    clf = IntentClassifier(_WeakModel())
    intent = await clf.classify("crea un archivo llamado prueba.txt en el workspace con contenido hola mundo")
    assert intent.is_task is True
    assert intent.model_meta.get("source") == "deterministic"
    assert intent.model_meta.get("cognition_outcome") == "degraded"
    assert "verb" in intent.model_meta.get("fallback_reason", "")


@pytest.mark.asyncio
async def test_task_turn_is_enqueued_after_creation():
    """P3.1: un turno TASK crea la misión Y la encola para que el worker la corra."""
    from alexis.cognition.contracts import Intent, IntentKind
    from alexis.cognition.conversation import ConversationSession

    enqueued = []

    class _TaskClassifier:
        async def classify(self, utterance, brief):
            return Intent(kind=IntentKind.TASK, utterance=utterance, objective=utterance, confidence=0.7)

    def create_mission(intent):
        mission = MissionEngine().create(
            intent.objective,
            MissionEnvelope(
                objective=intent.objective,
                autonomy=AutonomyLevel.SUPERVISED,
                allowed_actions=["understand", "respond"],
            ),
        )
        return mission

    async def enqueue_fn(mission):
        enqueued.append(mission)

    session = ConversationSession(classifier=_TaskClassifier(), self_model=None, create_mission=create_mission, enqueue=enqueue_fn)
    reply = await session.handle_turn("crea un archivo prueba.txt")
    assert reply.mission_id is not None
    assert len(enqueued) == 1
    assert enqueued[0].id == reply.mission_id


@pytest.mark.asyncio
async def test_non_task_turn_is_not_enqueued():
    from alexis.cognition.contracts import Intent, IntentKind
    from alexis.cognition.conversation import ConversationSession

    enqueued = []

    class _GreetingClassifier:
        async def classify(self, utterance, brief):
            return Intent(kind=IntentKind.GREETING, utterance=utterance, confidence=0.9)

    async def enqueue_fn(mission):
        enqueued.append(mission)

    session = ConversationSession(classifier=_GreetingClassifier(), self_model=None, enqueue=enqueue_fn)
    reply = await session.handle_turn("hola alexis")
    assert reply.mission_id is None
    assert enqueued == []