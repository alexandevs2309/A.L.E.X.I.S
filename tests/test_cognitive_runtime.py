import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.gates import AutonomyGate  # noqa: E402
from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.cognition.contracts import Claim, ClaimKind  # noqa: E402
from alexis.cognition.evidence import ClaimGuard, EvidenceStore  # noqa: E402
from alexis.cognition.loop import CognitiveRuntime, diagnose_failure  # noqa: E402
from alexis.cognition.planner import Planner  # noqa: E402
from alexis.cognition.goal_verification import GoalVerifier  # noqa: E402
from alexis.cognition.state import Decision, KnowledgeState, NextAction  # noqa: E402
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    ExecutionResult,
    MissionEnvelope,
    MissionState,
    Observation,
    Plan,
    PlanStep,
    RiskLevel,
    Verification,
    VerificationCheck,
)
from alexis.core.runtime import AlexisRuntime  # noqa: E402
from alexis.events.bus import EventBus  # noqa: E402
from alexis.learning.system import ExperienceLearner  # noqa: E402
from alexis.memory.store import InMemoryMemory  # noqa: E402
from alexis.models.provider import ModelOutcome, ModelRequest, ModelResponse  # noqa: E402
from alexis.security.policy import PolicyEngine  # noqa: E402
from alexis.verification import FilesystemVerifier  # noqa: E402
from alexis.world.model import WorldModel  # noqa: E402

ACTIONS = ["understand", "analyze", "research", "execute", "verify", "modify", "respond"]


def _envelope(objective, **over):
    data = dict(
        objective=objective,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=list(ACTIONS),
        capabilities=[],
    )
    data.update(over)
    return MissionEnvelope(**data)


def _mission(objective, success_criteria=None, **over):
    return MissionEngine().create(
        objective, _envelope(objective, **over), success_criteria=success_criteria
    )


def _plan(*steps):
    return Plan("m", list(steps))


def _step(step_id, action="execute", capability="fs.read", approval=False):
    return PlanStep(
        step_id,
        f"paso {step_id}",
        action,
        RiskLevel.MEDIUM,
        "executor",
        capability=capability,
        requires_approval=approval,
    )


class _ScriptedExecutor:
    """Ejecutor controlado: cada tool falla o tiene éxito según el guion."""

    def __init__(self, script, errors=None):
        self.script = dict(script)
        self.errors = dict(errors or {})
        self.calls = []

    async def execute(self, mission, step, *, tool_name=None):
        self.calls.append({"step": step.id, "action": step.action, "tool": tool_name})
        ok = self.script.get(step.id, True)
        if ok:
            return ExecutionResult(
                success=True,
                output={"ok": True, "step": step.id},
                observations=[Observation(f"tool.{step.id}", {"ok": True}, trusted=True)],
            )
        return ExecutionResult(
            success=False,
            output={"ok": False},
            error=self.errors.get(step.id, "no such file or directory: notas.txt"),
            observations=[Observation(f"tool.{step.id}", {"ok": False}, trusted=True)],
        )

    async def __call__(self, mission, step, tool_name=None):
        return await self.execute(mission, step, tool_name=tool_name)


class _PassingVerifier:
    def __init__(self, passed=True, checks=True):
        self.passed = passed
        self.checks = checks
        self.calls = 0

    async def verify(self, mission, plan):
        self.calls += 1
        evidence = ["archivo_existe=True", "size=42"]
        checks = []
        if self.checks:
            checks = [VerificationCheck("fs.stat", "deterministic", self.passed, "verificado", ["e1"], "fs", True)]
        return Verification(
            passed=self.passed,
            evidence=evidence,
            confidence=0.9,
            notes="verificación determinista del workspace",
            checks=checks,
            independent=True,
        )


class _NoopPolicy:
    def authorize(self, mission, step):
        class _D:
            allowed = True
            requires_approval = False
            reason = "test"

        return _D()

    def evaluate(self, mission, step):
        return self.authorize(mission, step)


def _runtime(executor, verifier, **over):
    return CognitiveRuntime(
        policy=_NoopPolicy(),
        executor=executor,
        verifier=verifier,
        **over,
    )


async def _drain(cognitive, mission, plan, limit=12):
    knowledge = cognitive.knowledge_for(mission)
    actions = []
    for _ in range(limit):
        pending = cognitive.pending_steps(mission, plan, knowledge)
        outcome = await cognitive.step(mission, knowledge, pending_steps=pending, plan=plan)
        knowledge = outcome.knowledge
        actions.append(outcome.action.value)
        if outcome.done:
            return actions, knowledge, outcome
    raise AssertionError("el bucle no terminó")


# ----------------------------------------------------------------------
# Test obligatorio 1: fallo → diagnóstico → REPLAN → éxito → VERIFY → FINISH
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_is_observed_diagnosed_replanned_then_verified_and_finished():
    mission = _mission("revisa el proyecto y dime qué falla")
    plan = _plan(_step("investigar", action="research"), _step("responder", action="respond", capability="tts.speak"))
    executor = _ScriptedExecutor({"investigar": False})
    verifier = _PassingVerifier(passed=True)
    cognitive = _runtime(executor, verifier)

    actions, knowledge, outcome = await _drain(cognitive, mission, plan)

    assert actions == [
        "research",
        "replan",
        "execute_tool",
        "verify",
        "finish",
    ], actions
    assert executor.calls[0]["step"] == "investigar"
    assert executor.calls[-1]["step"] == "responder"
    assert "investigar" in knowledge.failed_steps
    assert "responder" in knowledge.completed_steps
    assert knowledge.replans == 1
    assert knowledge.diagnosis.startswith("not_found")
    assert knowledge.last_failure_kind == "not_found"
    assert knowledge.verified is True
    assert knowledge.verification_passed is True
    # P0 §5.5: el plan verificó bien, el objetivo no está demostrado. La misión no se
    # completa: queda en NEEDS_VERIFICATION, que es "todavía no", no "falló".
    assert outcome.mission_state is MissionState.NEEDS_VERIFICATION
    assert "investigar" in " ".join(knowledge.unknown)


@pytest.mark.asyncio
async def test_replan_does_not_retry_the_failed_step():
    mission = _mission("lee notas.txt")
    plan = _plan(_step("investigar", action="research"), _step("responder", action="respond", capability="tts.speak"))
    executor = _ScriptedExecutor({"investigar": False})
    cognitive = _runtime(executor, _PassingVerifier())

    actions, knowledge, _ = await _drain(cognitive, mission, plan)

    assert [c["step"] for c in executor.calls] == ["investigar", "responder"]
    assert actions.count("replan") == 1
    assert knowledge.needs_replan is False


@pytest.mark.asyncio
async def test_replans_are_capped_and_then_it_asks_the_user():
    mission = _mission("revisa tres cosas")
    plan = _plan(
        _step("a", action="research"),
        _step("b", action="research"),
        _step("c", action="research"),
    )
    executor = _ScriptedExecutor({"a": False, "b": False, "c": False})
    verifier = _PassingVerifier(passed=False)
    cognitive = _runtime(executor, verifier, max_replans=2)

    actions, knowledge, outcome = await _drain(cognitive, mission, plan, limit=20)

    assert knowledge.replans == 2
    assert actions[-1] == "ask_user"
    assert outcome.mission_state is MissionState.WAITING_CLARIFICATION
    assert knowledge.verification_passed is False
    assert set(knowledge.failed_steps) == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_unblockable_failure_aborts_instead_of_claiming_success():
    mission = _mission("revisa tres cosas")
    plan = _plan(_step("a", action="research"), _step("b", action="research"))
    executor = _ScriptedExecutor(
        {"a": False, "b": False},
        errors={"a": "boom", "b": "boom"},
    )
    cognitive = _runtime(executor, _PassingVerifier(passed=False), max_replans=2)

    actions, knowledge, outcome = await _drain(cognitive, mission, plan, limit=20)

    assert knowledge.replans == 2
    assert actions[-1] == "abort"
    assert outcome.mission_state is MissionState.FAILED
    assert knowledge.verification_passed is False
    assert knowledge.facts() or all(
        claim.kind is not ClaimKind.FACT for claim in knowledge.claims
    )


@pytest.mark.asyncio
async def test_absent_progress_is_detected_and_aborts():
    mission = _mission("revisa el proyecto")
    plan = _plan(_step("investigar", action="research"))
    executor = _ScriptedExecutor({"investigar": True})
    verifier = _PassingVerifier(passed=False)
    cognitive = _runtime(executor, verifier, max_replans=2, max_stalls=1)

    actions, knowledge, outcome = await _drain(cognitive, mission, plan, limit=20)

    assert knowledge.verified is True
    assert knowledge.verification_passed is False
    assert actions[-1] == "abort"
    assert knowledge.iterations <= cognitive.max_iterations


# ----------------------------------------------------------------------
# Test obligatorio 2: objetivo ambiguo → ASK_USER antes de ejecutar nada
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ambiguous_objective_asks_user_before_executing_anything():
    mission = _mission("haz que el proyecto sea más rápido")
    mission.context["intent"] = {
        "kind": "task",
        "objective": "haz que el proyecto sea más rápido",
        "ambiguity": "¿Quieres optimizar el backend, el frontend o las consultas a la base de datos?",
        "needs_clarification": True,
        "confidence": 0.4,
    }
    plan = _plan(_step("investigar", action="research"))
    executor = _ScriptedExecutor({})
    cognitive = _runtime(executor, _PassingVerifier())

    actions, knowledge, outcome = await _drain(cognitive, mission, plan)

    assert actions == ["ask_user"]
    assert executor.calls == []
    assert outcome.mission_state is MissionState.WAITING_CLARIFICATION
    assert outcome.question == mission.context["intent"]["ambiguity"]
    assert knowledge.unknown


@pytest.mark.asyncio
async def test_ask_user_keeps_knowledge_for_a_later_resume():
    mission = _mission("mejora el proyecto")
    mission.context["intent"] = {
        "kind": "task",
        "ambiguity": "¿Qué parte quieres que mejore?",
        "needs_clarification": True,
    }
    cognitive = _runtime(_ScriptedExecutor({}), _PassingVerifier())
    knowledge = cognitive.knowledge_for(mission)
    outcome = await cognitive.step(mission, knowledge, pending_steps=[], plan=None)

    cognitive.store_knowledge(mission, outcome.knowledge)
    restored = cognitive.knowledge_for(mission)

    assert restored.unknown == outcome.knowledge.unknown
    assert restored.clarification == "¿Qué parte quieres que mejore?"


# ----------------------------------------------------------------------
# Test obligatorio 3: un claim del modelo sin evidencia NO puede ser FACT
# ----------------------------------------------------------------------


def test_model_fact_without_evidence_is_degraded_to_inference():
    guard = ClaimGuard()
    claim = Claim(
        id="c1",
        kind=ClaimKind.FACT,
        text="el backend tiene un cuello de botella en la consulta X",
        source="model",
        confidence=0.95,
        verified=False,
        evidence_ids=[],
    )
    guarded = guard.guard(claim)
    assert guarded.kind is ClaimKind.INFERENCE
    assert guarded.verified is False
    assert guard.degradations[0]["from"] == "fact"


def test_model_fact_claiming_evidence_is_degraded_to_inference():
    guard = ClaimGuard()
    guarded = guard.guard(
        Claim(
            id="c2",
            kind=ClaimKind.FACT,
            text="la latencia es 900 ms",
            source="model",
            evidence_ids=["e1"],
            confidence=0.9,
            verified=False,
        )
    )
    assert guarded.kind is ClaimKind.INFERENCE


def test_fact_without_evidence_becomes_uncertainty_when_there_is_none():
    guarded = ClaimGuard().guard(
        Claim(id="c3", kind=ClaimKind.FACT, text="x", source="runtime", evidence_ids=[], verified=False)
    )
    assert guarded.kind is ClaimKind.UNCERTAINTY


def test_sound_fact_from_deterministic_verification_survives():
    guarded = ClaimGuard().guard(
        Claim(id="c4", kind=ClaimKind.FACT, text="existe", source="verifier", evidence_ids=["e1"], verified=True)
    )
    assert guarded.kind is ClaimKind.FACT


def test_evidence_store_never_promotes_model_output_to_fact():
    store = EvidenceStore()
    claims = store.from_model_claims(
        [
            {"text": "la causa es la base de datos", "kind": "fact"},
            {"text": "asumo que el proyecto es Go", "kind": "assumption"},
            {"text": "no sé si es el frontend", "kind": "uncertainty"},
        ]
    )
    kinds = [c.kind for c in claims]
    assert ClaimKind.FACT not in kinds
    assert kinds == [ClaimKind.INFERENCE, ClaimKind.ASSUMPTION, ClaimKind.UNCERTAINTY]
    assert store.facts() == []


def test_verification_without_deterministic_check_cannot_produce_fact():
    store = EvidenceStore()
    claims = store.from_verification(
        Verification(
            passed=True,
            evidence=["parece que funciona"],
            confidence=0.99,
            notes="solo lo dijo el modelo",
            checks=[VerificationCheck("critic", "model_critic", True, "ok", [], "model", False)],
        )
    )
    assert claims[0].kind is ClaimKind.INFERENCE
    assert store.facts() == []


def test_deterministic_verification_produces_fact_with_evidence():
    store = EvidenceStore()
    claims = store.from_verification(
        Verification(
            passed=True,
            evidence=["archivo_existe=True"],
            confidence=0.9,
            notes="verificado en el workspace",
            checks=[VerificationCheck("fs.stat", "deterministic", True, "ok", ["e1"], "fs", True)],
        )
    )
    assert claims[0].kind is ClaimKind.FACT
    assert claims[0].verified is True


# ----------------------------------------------------------------------
# El modelo propone; no puede salirse de las opciones
# ----------------------------------------------------------------------


class _StubRouter:
    def __init__(self, response):
        self.response = response
        self.requests = []

    async def complete(self, request: ModelRequest):
        self.requests.append(request)
        return self.response


def _response(data, outcome=ModelOutcome.REAL, text=""):
    return ModelResponse(
        text=text or str(data),
        data=data,
        provider="stub",
        model="stub-model",
        outcome=outcome,
    )


@pytest.mark.asyncio
async def test_model_chooses_among_real_options_and_its_claims_stay_inferences():
    mission = _mission("analiza el proyecto")
    plan = _plan(_step("investigar", action="research"), _step("responder", action="respond", capability="tts.speak"))
    router = _StubRouter(
        _response(
            {
                "action": "execute_tool",
                "step_id": "responder",
                "rationale": "primero investigo, luego respondo",
                "claims": [{"text": "el proyecto usa Go", "kind": "fact"}],
            }
        )
    )
    store = EvidenceStore()
    cognitive = _runtime(_ScriptedExecutor({}), _PassingVerifier(), model_router=router, evidence=store)

    knowledge = KnowledgeState(objective=mission.goal.objective, iterations=1)
    decision = await cognitive.decide_next_action(
        mission, knowledge, pending_steps=cognitive.pending_steps(mission, plan, knowledge)
    )

    assert decision.action is NextAction.EXECUTE_TOOL
    assert decision.step_id == "responder"
    assert decision.proposed_by == "model"
    assert decision.cognition_outcome == "real"
    assert all(c.kind is not ClaimKind.FACT for c in decision.claims)


@pytest.mark.asyncio
async def test_model_proposal_outside_the_options_is_rejected():
    mission = _mission("analiza el proyecto")
    plan = _plan(_step("investigar", action="research"), _step("responder", action="respond", capability="tts.speak"))
    router = _StubRouter(
        _response({"action": "finish", "rationale": "ya está", "step_id": None, "capability": None})
    )
    cognitive = _runtime(_ScriptedExecutor({}), _PassingVerifier(), model_router=router)

    knowledge = KnowledgeState(objective=mission.goal.objective, iterations=1)
    decision = await cognitive.decide_next_action(
        mission, knowledge, pending_steps=cognitive.pending_steps(mission, plan, knowledge)
    )

    assert decision.action is NextAction.RESEARCH
    assert decision.proposed_by == "deterministic"
    assert decision.rejected
    assert "fuera de opciones" in decision.model_meta["fallback_reason"]


@pytest.mark.asyncio
async def test_finish_without_verification_is_impossible_even_if_the_model_asks():
    mission = _mission("analiza el proyecto")
    router = _StubRouter(_response({"action": "finish", "rationale": "sin verificar"}))
    cognitive = _runtime(_ScriptedExecutor({}), _PassingVerifier(), model_router=router)

    knowledge = KnowledgeState(objective=mission.goal.objective, iterations=1)
    decision = await cognitive.decide_next_action(mission, knowledge, pending_steps=[])

    assert decision.action is not NextAction.FINISH
    assert decision.action is NextAction.VERIFY


@pytest.mark.asyncio
async def test_degraded_model_falls_back_to_the_deterministic_option():
    mission = _mission("analiza el proyecto")
    plan = _plan(_step("investigar", action="research"), _step("responder", action="respond", capability="tts.speak"))
    router = _StubRouter(_response(None, outcome=ModelOutcome.DEGRADED))
    cognitive = _runtime(_ScriptedExecutor({}), _PassingVerifier(), model_router=router)

    knowledge = KnowledgeState(objective=mission.goal.objective, iterations=1)
    decision = await cognitive.decide_next_action(
        mission, knowledge, pending_steps=cognitive.pending_steps(mission, plan, knowledge)
    )

    assert decision.action is NextAction.RESEARCH
    assert decision.cognition_outcome == ModelOutcome.DEGRADED.value
    assert decision.proposed_by == "deterministic"


# ----------------------------------------------------------------------
# Policy y executor existentes: el runtime no ejecuta por su cuenta
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_policy_block_stops_the_mission_without_executing():
    class _Deny:
        def authorize(self, mission, step):
            class _D:
                allowed = False
                requires_approval = False
                reason = "'execute' está fuera del envelope de la misión"

            return _D()

        def evaluate(self, mission, step):
            return self.authorize(mission, step)

    mission = _mission("borra todo", allowed_actions=["read"])
    plan = _plan(_step("borrar", action="execute"))
    executor = _ScriptedExecutor({})
    cognitive = CognitiveRuntime(policy=_Deny(), executor=executor, verifier=_PassingVerifier())

    actions, _, outcome = await _drain(cognitive, mission, plan)

    assert actions == ["execute_tool"]
    assert outcome.mission_state is MissionState.BLOCKED
    assert executor.calls == []


@pytest.mark.asyncio
async def test_approval_required_pauses_the_mission():
    class _Approval:
        def authorize(self, mission, step):
            class _D:
                allowed = True
                requires_approval = True
                reason = "efecto de escritura: requiere tu aprobación"

            return _D()

        def evaluate(self, mission, step):
            return self.authorize(mission, step)

    mission = _mission("borra notas.txt")
    plan = _plan(_step("borrar", action="execute", approval=True))
    executor = _ScriptedExecutor({})
    cognitive = CognitiveRuntime(policy=_Approval(), executor=executor, verifier=_PassingVerifier())

    actions, _, outcome = await _drain(cognitive, mission, plan)

    assert actions == ["execute_tool"]
    assert outcome.requires_approval is True
    assert outcome.mission_state is MissionState.WAITING_APPROVAL
    assert executor.calls == []


@pytest.mark.asyncio
async def test_already_approved_step_is_executed_without_asking_again():
    class _Approval:
        def authorize(self, mission, step):
            class _D:
                allowed = True
                requires_approval = True
                reason = "requiere aprobación"

            return _D()

        def evaluate(self, mission, step):
            return self.authorize(mission, step)

    mission = _mission("borra notas.txt")
    mission.context["approved_step_ids"] = ["borrar"]
    plan = _plan(_step("borrar", action="execute", approval=True))
    executor = _ScriptedExecutor({})
    cognitive = CognitiveRuntime(policy=_Approval(), executor=executor, verifier=_PassingVerifier())

    actions, knowledge, outcome = await _drain(cognitive, mission, plan)

    assert [c["step"] for c in executor.calls] == ["borrar"]
    # El paso se ejecutó con aprobación, pero el objetivo sigue sin verificar (§5.5).
    assert outcome.mission_state is MissionState.NEEDS_VERIFICATION
    assert any("aprobado por el usuario" in k for k in knowledge.known)


# ----------------------------------------------------------------------
# Integración: el runtime completo con el bucle cognitivo
# ----------------------------------------------------------------------


async def _observe_real_file(tmp_path, world, name):
    """Observa un archivo real con la tool real, para que el objetivo tenga evidencia real."""
    from alexis.execution import SandboxExecutor
    from alexis.security.sandbox import SandboxRunner
    from alexis.tools.filesystem import build_filesystem_tools
    from alexis.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register_all(build_filesystem_tools(tmp_path))
    executor = SandboxExecutor(tools=registry, sandbox=SandboxRunner(tmp_path))
    step = PlanStep(
        "comprobar",
        f"comprobar {name}",
        "research",
        RiskLevel.LOW,
        "executor",
        capability="fs.stat",
        args={"path": name},
    )
    result = await executor.execute(_mission("comprobar"), step, tool_name="fs.stat")
    world.observe_execution(step, result)
    return result


def _full_runtime(cognitive):
    return AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=cognitive.executor,
        verifier=cognitive.verifier,
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        cognitive=cognitive,
    )


@pytest.mark.asyncio
async def test_full_runtime_completes_through_the_cognitive_loop(tmp_path):
    (tmp_path / "notas.txt").write_text("contenido", encoding="utf-8")
    from alexis.capabilities import build_catalog

    world = WorldModel()
    await _observe_real_file(tmp_path, world, "notas.txt")
    mission = _mission(
        "lee notas.txt",
        capabilities=[s.id for s in build_catalog().enabled()],
        success_criteria=["El archivo file_exists:notas.txt está escrito"],
    )
    mission.plan = await Planner().create_plan(mission)
    cognitive = CognitiveRuntime(
        policy=PolicyEngine(),
        gate=AutonomyGate(),
        executor=None,
        verifier=FilesystemVerifier(workspace=tmp_path),
        execute=_ScriptedExecutor({}),
        world=world,
        goal_verifier=GoalVerifier(world=world),
    )
    runtime = _full_runtime(cognitive)

    result = await runtime.run_mission(mission)

    # §5.5: se completa porque el GoalVerifier lo autorizó, no porque los pasos ok.
    assert result.goal_verification is not None
    assert result.goal_verification.verified is True
    assert result.state is MissionState.COMPLETED
    assert result.context["knowledge"]["verified"] is True
    assert result.context["knowledge"]["claims"]
    assert result.context["claims"]


@pytest.mark.asyncio
async def test_legacy_runtime_is_untouched_when_no_cognitive_runtime(tmp_path):
    (tmp_path / "notas.txt").write_text("contenido", encoding="utf-8")
    from alexis.capabilities import build_catalog

    world = WorldModel()
    await _observe_real_file(tmp_path, world, "notas.txt")
    mission = _mission(
        "lee notas.txt",
        capabilities=[s.id for s in build_catalog().enabled()],
        success_criteria=["El archivo file_exists:notas.txt está escrito"],
    )
    mission.plan = await Planner().create_plan(mission)
    runtime = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=None,
        verifier=FilesystemVerifier(workspace=tmp_path),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        goal_verifier=GoalVerifier(world=world),
    )
    runtime.executor = _ScriptedExecutor({})

    result = await runtime.run_mission(mission)

    # El camino legacy respeta la MISMA autoridad que el cognitivo (§5.5, caso 12).
    assert result.goal_verification is not None
    assert result.state is MissionState.COMPLETED
    assert "knowledge" not in result.context
    assert "claims" not in result.context
    assert [r["step"] for r in result.results] == [s.id for s in mission.plan.steps]


# ----------------------------------------------------------------------
# Diagnóstico y estado
# ----------------------------------------------------------------------


def test_diagnose_failure_is_honest_about_unknown_errors():
    kind, explanation, hypotheses = diagnose_failure(ExecutionResult(success=False, error="boom"))
    assert kind == "unknown"
    assert "boom" in explanation
    assert hypotheses


def test_diagnose_failure_detects_a_missing_tool():
    kind, _, hypotheses = diagnose_failure(
        ExecutionResult(success=False, error="no hay tool registrada 'git.read'")
    )
    assert kind == "tool_missing"
    assert hypotheses


def test_knowledge_state_survives_serialization():
    knowledge = KnowledgeState(objective="o")
    knowledge.add_known("k")
    knowledge.add_unknown("u")
    knowledge.add_hypothesis("h")
    knowledge.add_claim(Claim(id="c", kind=ClaimKind.ASSUMPTION, text="a", source="runtime"))
    knowledge.note_verification(True, 0.8, "ok")

    restored = KnowledgeState.from_dict(knowledge.to_dict())

    assert restored.known == ["k"]
    assert restored.unknown == ["u"]
    assert restored.hypotheses == ["h"]
    assert restored.verified is True
    assert [c.text for c in restored.claims] == ["a"]


def test_progress_fingerprint_changes_only_with_real_progress():
    knowledge = KnowledgeState(objective="o")
    before = knowledge.progress_fingerprint()
    knowledge.iterations += 1
    assert knowledge.progress_fingerprint() == before
    knowledge.mark_completed("investigar")
    assert knowledge.progress_fingerprint() != before


def test_decision_serializes_for_audit():
    decision = Decision(action=NextAction.REPLAN, rationale="cambio", step_id="a")
    payload = decision.to_dict()
    assert payload["action"] == "replan"
    assert payload["rationale"] == "cambio"
