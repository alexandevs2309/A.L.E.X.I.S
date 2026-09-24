"""Tests F2.0 — contratos del Cognitive Core.

Comprueban sobre todo los invariantes P3 (docs/COGNITIVE-CORE-F2.md §1.3):
greeting/capability-query no crean misión, sólo TASK la crea, un FACT sin evidencia no
es un hecho, y la verificación sólo la aprueba un check determinista independiente.
"""

import pytest

from alexis.contracts import PlanStep, RiskLevel, Verification, VerificationCheck, verification_can_approve
from alexis.cognition.contracts import (
    DIRECT_KINDS,
    MISSION_KINDS,
    CapabilityProposal,
    Claim,
    ClaimKind,
    Intent,
    IntentKind,
    ReplanDecision,
    Selection,
    SelfBrief,
    UserReply,
)
from alexis.memory.contracts import MemoryContext, MemoryItem, MemoryQuery


# ----------------------------------------------------------------------
# Intent: sólo TASK crea misión
# ----------------------------------------------------------------------


def test_only_task_creates_mission():
    assert MISSION_KINDS == frozenset({IntentKind.TASK})
    assert Intent(kind=IntentKind.TASK, utterance="x").is_task
    for kind in (IntentKind.GREETING, IntentKind.SMALL_TALK, IntentKind.SELF_QUERY,
                 IntentKind.CAPABILITY_QUERY, IntentKind.META_QUERY, IntentKind.UNKNOWN):
        assert not Intent(kind=kind, utterance="x").is_task
        assert Intent(kind=kind, utterance="x").is_direct_answer or kind is IntentKind.UNKNOWN


def test_greeting_and_capability_query_are_direct_answers():
    assert IntentKind.GREETING in DIRECT_KINDS
    assert IntentKind.CAPABILITY_QUERY in DIRECT_KINDS
    assert Intent(kind=IntentKind.GREETING, utterance="Hola ALEXIS.").is_direct_answer
    assert Intent(kind=IntentKind.CAPABILITY_QUERY, utterance="¿Qué puedes hacer?").is_direct_answer


def test_requested_capabilities_are_only_suggestions():
    intent = Intent(
        kind=IntentKind.TASK,
        utterance="borra todo",
        objective="borra todo",
        requested_capabilities=["fs.remove"],
    )
    assert intent.requested_capabilities == ["fs.remove"]
    # la propuesta del modelo es un objeto distinto de la decisión del Core
    proposal = CapabilityProposal(capabilities=["fs.remove"], rationale="lo pide el usuario")
    selection = Selection(selected=[], rejected=[{"capability": "fs.remove", "rule": "policy", "reason": "deny"}])
    assert proposal.capabilities not in selection.selected


def test_intent_to_dict_roundtrip_shape():
    intent = Intent(kind=IntentKind.TASK, utterance="revisa", objective="revisa", confidence=0.8)
    data = intent.to_dict()
    assert data["kind"] == "task"
    assert data["objective"] == "revisa"
    assert data["confidence"] == 0.8


# ----------------------------------------------------------------------
# SelfBrief: se deriva del snapshot del SelfModel (sin inventar)
# ----------------------------------------------------------------------


def test_self_brief_from_snapshot_marks_missing_capabilities():
    snapshot = {
        "identity": {"name": "ALEXIS"},
        "current_state": "working",
        "current_goal": "revisar proyecto",
        "current_mission": {"id": "m1", "state": "running", "autonomy": "supervised"},
        "available_capabilities": ["fs.read", "fs.write"],
        "required_capabilities": ["fs.read", "git.commit"],
        "permissions": {"autonomy": "supervised"},
        "active_envelope": {"autonomy": "supervised"},
        "active_context": ["contexto previo"],
        "uncertainties": ["no sé si hay tests"],
        "confidence": 0.6,
        "pending_approvals": [],
        "recent_actions": [{"step": "research", "success": True}],
        "current_dependencies": ["fs.read"],
        "current_limits": ["sin red"],
    }
    brief = SelfBrief.from_snapshot(snapshot)
    assert brief.state == "working"
    assert brief.mission_id == "m1"
    assert brief.missing_capabilities == ["git.commit"]  # required - available
    assert brief.uncertainties == ["no sé si hay tests"]
    assert brief.to_dict()["available_capabilities"] == ["fs.read", "fs.write"]


def test_self_brief_handles_empty_snapshot():
    brief = SelfBrief.from_snapshot({})
    assert brief.state == "idle"
    assert brief.available_capabilities == []
    assert brief.missing_capabilities == []


# ----------------------------------------------------------------------
# Evidence: un FACT exige evidencia verificada
# ----------------------------------------------------------------------


def test_fact_without_evidence_is_not_sound():
    fact = Claim(id="c1", kind=ClaimKind.FACT, text="el archivo existe", source="model")
    assert not fact.is_sound()
    unsound = Claim(id="c2", kind=ClaimKind.FACT, text="x", source="tool", evidence_ids=["e1"], verified=False)
    assert not unsound.is_sound()
    sound = Claim(id="c3", kind=ClaimKind.FACT, text="x", source="verifier", evidence_ids=["e1"], verified=True)
    assert sound.is_sound()


def test_non_fact_claims_are_sound_by_themselves():
    for kind in (ClaimKind.EVIDENCE, ClaimKind.INFERENCE, ClaimKind.UNCERTAINTY, ClaimKind.ASSUMPTION):
        assert Claim(id="c", kind=kind, text="t", source="s").is_sound()


# ----------------------------------------------------------------------
# Replan acotado
# ----------------------------------------------------------------------


def test_replan_decision_actions_are_bounded():
    valid = ReplanDecision(action="retry_alternative", alternative_capability="fs.stat", attempt=1)
    assert valid.is_valid()
    assert not ReplanDecision(action="loop_forever").is_valid()


# ----------------------------------------------------------------------
# Verificación: sólo un check determinista independiente aprueba
# ----------------------------------------------------------------------


def test_model_critic_cannot_approve_alone():
    v = Verification(
        passed=False,
        checks=[VerificationCheck(id="c", kind="model_critic", passed=True, verifier="ModelCritic")],
    )
    assert not verification_can_approve(v)  # P3.6


def test_deterministic_independent_check_can_approve():
    v = Verification(
        passed=True,
        checks=[
            VerificationCheck(id="d", kind="deterministic", passed=True, verifier="FilesystemVerifier", can_approve=True)
        ],
        independent=True,
    )
    assert verification_can_approve(v)


def test_verification_legacy_shape_still_works():
    v = Verification(passed=True, evidence=["legacy"], confidence=0.9, notes="ok")
    assert v.checks == []
    assert v.claim_ids == []
    assert v.independent is False


def test_plan_step_positional_backward_compatible():
    step = PlanStep("execute", "d", "execute", RiskLevel.MEDIUM, "executor", ["research"], True, "fs.write")
    assert step.capability == "fs.write"
    assert step.requires_input == {}
    assert step.proposed_by is None


# ----------------------------------------------------------------------
# UserReply / Memory contracts
# ----------------------------------------------------------------------


def test_user_reply_defaults_no_degraded():
    reply = UserReply(text="hola")
    assert reply.degraded is False
    assert reply.cognition_outcome == "none"
    assert reply.to_dict()["kind"] == "answer"


def test_user_reply_carries_degraded_flag():
    reply = UserReply(text="sin modelo real", cognition_outcome="degraded", degraded=True)
    assert reply.to_dict()["cognition_outcome"] == "degraded"
    assert reply.to_dict()["degraded"] is True


def test_memory_context_prompt_lines_marked_as_data():
    ctx = MemoryContext(
        items=[MemoryItem(id="m1", kind="observations", content="contenido", source="obs")],
        sources=["obs"],
        provider="inprocess",
        token_estimate=3,
    )
    line = ctx.as_prompt_lines()[0]
    # R3: el contenido no confiable se entrega como dato marcado, no como instrucción.
    assert line.startswith("[[UNTRUSTED_DATA source=memory:obs]]")
    assert line.endswith("[[/UNTRUSTED_DATA]]")
    assert ctx.to_dict()["provider"] == "inprocess"


def test_memory_query_defaults():
    q = MemoryQuery(text="hola")
    assert q.kinds == ["observations", "episodic", "semantic"]
    assert q.limit == 10


@pytest.mark.parametrize("kind", list(IntentKind))
def test_every_intent_kind_has_a_name(kind):
    assert kind.value
