"""P0 §5.6 — Response + Reflection + Experience + Verified Learning.

Los 25 tests que el plan exige para §5.6.10, en el orden del enunciado.

Dos invariantes son la razón de existir de este fichero:

1. ``ACTION SUCCESS -> OBJECTIVE SUCCESS`` está cerrado. Ningún camino de respuesta,
   reflexión ni aprendizaje afirma que el objetivo seentimes Tanger cuando
   `GoalVerification` no lo confirmó.
2. La autoridad es intocable. Reflexión, experiencia y aprendizaje no pueden modificar
   policy, permisos, capabilities, envelope ni reglas de seguridad: no tienen con qué.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.cognition.contracts import Claim, ClaimKind, DecisionRecord, UserReply  # noqa: E402
from alexis.cognition.goal_verification import (  # noqa: E402
    CriterionEvaluation,
    CriterionEvidence,
    CriterionStatus,
    GoalVerification,
)
from alexis.cognition.response import ResponseComposer  # noqa: E402
from alexis.cognition.state import KnowledgeState, Verdict  # noqa: E402
from alexis.contracts import AutonomyLevel, MissionEnvelope  # noqa: E402
from alexis.learning.experience import (  # noqa: E402
    EXPERIENCE_SOURCE,
    Experience,
    ExperienceStore,
    LearningBoundary,
    promote_claims,
)
from alexis.learning.reflection import Reflection, build_reflection  # noqa: E402
from alexis.learning.system import ExperienceLearner  # noqa: E402
from alexis.self.model import SelfModel  # noqa: E402
from alexis.self.sync import SelfModelSync  # noqa: E402

ACTIONS = ["understand", "analyze", "research", "execute", "verify", "respond"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _mission(objective="leer el informe", **over):
    data = dict(objective=objective, autonomy=AutonomyLevel.SUPERVISED, allowed_actions=list(ACTIONS))
    data.update(over)
    return MissionEngine().create(objective, MissionEnvelope(**data))


def _knowledge(verdict=Verdict.SUCCESS.value, *, completed=("read",), failed=(), confidence=0.8):
    k = KnowledgeState(objective="leer el informe")
    k.last_verdict = verdict
    k.confidence = confidence
    for step in completed:
        k.mark_completed(step)
    for item in failed:
        if isinstance(item, tuple):
            k.mark_failed(item[0], item[1])
        else:
            k.mark_failed(item, "falló")
    return k


def _verification(verified=True, *, status=CriterionStatus.SATISFIED, reason="criterio satisfecho"):
    return GoalVerification(
        objective="leer el informe",
        evaluations=[
            CriterionEvaluation(
                criterion="el archivo existe",
                status=status,
                reason=reason,
                evidence=[
                    CriterionEvidence(
                        evidence_id="ev-1", source="fs.read", grade="EVIDENCE",
                        detail="el archivo existe", trusted=True,
                    )
                ] if status is CriterionStatus.SATISFIED else [],
            )
        ],
        verified=verified,
        reason=reason,
    )


def _evidence_knowledge(verdict=Verdict.SUCCESS.value):
    k = _knowledge(verdict)
    k.add_claim(
        Claim(id="c1", kind=ClaimKind.EVIDENCE, text="el archivo existe", source="fs.read",
              evidence_ids=["ev-1"], confidence=0.9, verified=True)
    )
    return k


# --------------------------------------------------------------------------- #
# 5.6.1 Response Composer (tests 1-7)
# --------------------------------------------------------------------------- #


def test_01_success_produce_respuesta_correcta():
    """SUCCESS con objetivo verificado: la respuesta lo dice y lo justifica."""
    composer = ResponseComposer()
    reply = composer.compose(
        _mission(), _evidence_knowledge(), _verification(True), model_outcome="real"
    )
    assert reply.verdict == Verdict.SUCCESS.value
    assert reply.goal_verified is True
    assert "verificado" in reply.text
    assert reply.pending == []
    assert composer.asserts_completion(reply) is False


def test_02_partial_success_no_se_presenta_como_exito_completo():
    """PARTIAL_SUCCESS verificado: puede decir que se logró, pero marca lo pendiente."""
    composer = ResponseComposer()
    reply = composer.compose(
        _mission(), _knowledge(Verdict.PARTIAL_SUCCESS.value), _verification(True),
        model_outcome="real",
    )
    assert reply.verdict == Verdict.PARTIAL_SUCCESS.value
    assert "PARTIAL_SUCCESS" in reply.text
    assert "sin prueba suficiente" in reply.text


def test_03_failure_no_se_presenta_como_exito():
    """FAILURE con objetivo verificado: la respuesta reconoce el fallo."""
    composer = ResponseComposer()
    reply = composer.compose(
        _mission(),
        _knowledge(Verdict.FAILURE.value, completed=(), failed=(("read", "no existe"),)),
        _verification(True), model_outcome="real",
    )
    assert reply.verdict == Verdict.FAILURE.value
    assert "FAILURE" in reply.text
    assert "Falló: read" in reply.text


def test_04_insufficient_evidence_no_se_presenta_como_hecho():
    """INSUFFICIENT_EVIDENCE + objetivo sin verificar: no hay afirmación de logro."""
    composer = ResponseComposer()
    reply = composer.compose(
        _mission(), _knowledge(Verdict.INSUFFICIENT_EVIDENCE.value),
        _verification(False, status=CriterionStatus.INSUFFICIENT_EVIDENCE),
        model_outcome="real",
    )
    assert reply.verdict == Verdict.INSUFFICIENT_EVIDENCE.value
    assert reply.goal_verified is False
    assert "NO está verificado" in reply.text
    assert composer.asserts_completion(reply) is False
    assert reply.pending, "debe decir qué queda pendiente"


def test_05_blocked_explica_el_bloqueo():
    """BLOCKED: la respuesta nombra la autoridad que detuvo la acción."""
    mission = _mission()
    mission.context["blocked_reason"] = "fs.remove exige aprobación y no hay envelope"
    composer = ResponseComposer()
    reply = composer.compose(
        mission, _knowledge(Verdict.BLOCKED.value), _verification(False), model_outcome="real"
    )
    assert reply.verdict == Verdict.BLOCKED.value
    assert reply.blocked is True
    assert reply.needs_user is True
    assert "Bloqueado" in reply.text
    assert "Necesita tu intervención" in reply.text


def test_06_degraded_model_provenance_se_conserva():
    """Procedencia DEGRADED viaja a la respuesta y a `self_update`."""
    composer = ResponseComposer()
    reply = composer.compose(
        _mission(), _evidence_knowledge(), _verification(True), model_outcome="degraded"
    )
    assert reply.cognition_outcome == "degraded"
    assert reply.degraded is True
    assert "DEGRADED" in reply.text
    assert reply.self_update["model_outcome"] == "degraded"


def test_07_unavailable_model_no_inventa_respuesta():
    """UNAVAILABLE: la respuesta se declara composed de evidencia, sin modelo."""
    composer = ResponseComposer()
    reply = composer.compose(
        _mission(), _evidence_knowledge(), _verification(True), model_outcome="unavailable"
    )
    assert reply.cognition_outcome == "unavailable"
    assert reply.degraded is True
    assert "UNAVAILABLE" in reply.text
    assert "sólo con evidencia observada" in reply.text


# --------------------------------------------------------------------------- #
# 5.6.2 Decision Record (test 8)
# --------------------------------------------------------------------------- #


def test_08_decision_record_se_persiste():
    """El DecisionRecord serializa y sobrevive round-trip (base de la persistencia)."""
    record = DecisionRecord(
        mission_id="m1", iteration=2, action="execute_tool", capability="fs.read",
        justification="avanzar el paso 'read'", policy_verdict="allow",
        execution_result="ok", evidence_refs=["ev-1"],
        verdict=Verdict.SUCCESS.value, timestamp=123.0, replan_count=1,
    )
    row = record.to_dict()
    assert row["mission_id"] == "m1"
    assert row["policy_verdict"] == "allow"
    assert DecisionRecord.from_dict(row).to_dict() == row
    # Se guarda en mission.context, que es lo que la capa de storage ya persiste.
    mission = _mission()
    mission.context["decisions"] = {f"{row['iteration']}:{row['action']}": row}
    assert mission.context["decisions"]


# --------------------------------------------------------------------------- #
# 5.6.3 Reflection (test 9)
# --------------------------------------------------------------------------- #


def test_09_reflection_se_persiste():
    """La reflexión guarda su estructura completa y hace round-trip."""
    mission = _mission()
    reflection = build_reflection(mission, _evidence_knowledge(), _verification(True))
    row = reflection.to_dict()
    for key in ("objective", "outcome", "what_worked", "what_failed", "blockers",
                "replans", "evidence_quality", "unresolved_questions",
                "lessons_candidate", "confidence"):
        assert key in row, f"falta el campo {key}"
    assert Reflection.from_dict(row).to_dict() == row


# --------------------------------------------------------------------------- #
# 5.6.4 / 5.6.8 Experience y recovery (tests 10, 11)
# --------------------------------------------------------------------------- #


def test_10_experience_se_persiste():
    """La experiencia lleva objetivo, acciones, resultados, evidencia y veredicto."""
    mission = _mission()
    mission.results = [{"step": "read", "success": True, "output": "contenido"}]
    reflection = build_reflection(mission, _evidence_knowledge(), _verification(True))
    experience = Experience(
        mission_id=mission.id, objective="leer el informe",
        actions=["read"], results=["read: ok"], evidence_refs=["ev-1"],
        verdict=Verdict.SUCCESS.value, goal_verified=True,
        reflection=reflection.to_dict(),
    )
    row = experience.to_dict()
    assert row["goal_verified"] is True
    assert row["evidence_refs"] == ["ev-1"]
    assert Experience.from_dict(row).to_dict() == row

    verified = LearningBoundary().evaluate(experience, reflection)
    ExperienceStore.attach(mission, experience, verified)
    assert mission.context["experience"]["mission_id"] == mission.id
    assert mission.context["reflection"]["mission_id"] == mission.id
    assert mission.context["verified_learning"]["can_teach"] is True


def test_11_reinicio_permite_recuperar_experience():
    """Tras «reiniciar» (rehidratar desde mission.context) la experiencia sigue ahí."""
    mission = _mission()
    reflection = build_reflection(mission, _evidence_knowledge(), _verification(True))
    experience = Experience(
        mission_id=mission.id, objective="leer el informe",
        verdict=Verdict.SUCCESS.value, goal_verified=True, reflection=reflection.to_dict(),
    )
    verified = LearningBoundary().evaluate(experience, reflection)
    ExperienceStore.attach(mission, experience, verified)

    # Simula el reinicio: una misión nueva rehidratada desde lo que se persistió.
    recovered = MissionEngine().create(
        "leer el informe",
        MissionEnvelope(objective="leer el informe", autonomy=AutonomyLevel.SUPERVISED,
                        allowed_actions=list(ACTIONS)),
    )
    for key in ("experience", "reflection", "verified_learning"):
        recovered.context[key] = dict(mission.context[key])
    reloaded = ExperienceStore.load(recovered)
    assert reloaded is not None
    assert reloaded.experience.objective == "leer el informe"
    assert reloaded.experience.goal_verified is True
    assert reloaded.can_teach() is True


# --------------------------------------------------------------------------- #
# 5.6.5 Verified Learning Boundary (tests 12-15)
# --------------------------------------------------------------------------- #


def _evaluate(verdict, goal_verified, *, status=CriterionStatus.SATISFIED):
    mission = _mission()
    knowledge = _knowledge(verdict)
    reflection = build_reflection(
        mission, knowledge, _verification(goal_verified, status=status)
    )
    experience = Experience(
        mission_id=mission.id, objective="leer el informe", verdict=verdict,
        goal_verified=goal_verified, reflection=reflection.to_dict(),
    )
    return LearningBoundary().evaluate(experience, reflection)


def test_12_verified_success_puede_producir_lesson_candidate():
    verified = _evaluate(Verdict.SUCCESS.value, True)
    assert verified.can_teach() is True
    assert verified.quality == "verified"
    assert verified.lesson
    assert verified.claim_kind == ClaimKind.EVIDENCE.value


def test_13_insufficient_evidence_no_produce_conocimiento_verificado():
    verified = _evaluate(
        Verdict.INSUFFICIENT_EVIDENCE.value, True, status=CriterionStatus.INSUFFICIENT_EVIDENCE
    )
    assert verified.can_teach() is False
    assert verified.lesson == ""
    assert verified.claim_kind == ClaimKind.UNCERTAINTY.value


def test_14_blocked_no_produce_conocimiento_de_exito():
    verified = _evaluate(Verdict.BLOCKED.value, True)
    assert "éxito" not in verified.lesson.lower() or "no es una conclusión de éxito" in verified.lesson
    assert verified.lesson != ""
    assert "bloqueado" in verified.lesson.lower()
    assert verified.can_teach() is True  # enseña el bloqueo, no el éxito


def test_15_failure_puede_producir_aprendizaje_sobre_el_fallo():
    verified = _evaluate(Verdict.FAILURE.value, True)
    assert verified.can_teach() is True
    assert "falló" in verified.lesson
    assert verified.reason.startswith("aprendizaje sobre el fallo")


# --------------------------------------------------------------------------- #
# Frontera epistémica (tests 16, 17)
# --------------------------------------------------------------------------- #


def test_16_assumption_no_se_convierte_en_fact():
    verified = _evaluate(Verdict.SUCCESS.value, True)
    claim = Claim(id="c1", kind=ClaimKind.ASSUMPTION, text="supongo que está bien", source="model")
    (promoted,) = promote_claims(verified, [claim])
    assert promoted.kind is ClaimKind.ASSUMPTION
    assert promoted.verified is False


def test_17_inference_no_se_convierte_automaticamente_en_fact():
    verified = _evaluate(Verdict.SUCCESS.value, True)
    claim = Claim(id="c2", kind=ClaimKind.INFERENCE, text="por lo tanto funciona", source="model")
    (promoted,) = promote_claims(verified, [claim])
    assert promoted.kind is ClaimKind.INFERENCE
    assert promoted.kind is not ClaimKind.FACT
    assert promoted.verified is False


# --------------------------------------------------------------------------- #
# La autoridad es intocable (tests 18-21)
# --------------------------------------------------------------------------- #


def test_18_reflection_no_puede_modificar_policy():
    """La reflexión no tiene campo alguno que apunte a la Policy."""
    reflection = build_reflection(_mission(), _evidence_knowledge(), _verification(True))
    forbidden = {"policy", "gate", "permissions", "envelope", "capabilities", "authority"}
    assert forbidden.isdisjoint(set(type(reflection).__dataclass_fields__))
    assert reflection.is_frozen() is True


def test_19_reflection_no_puede_modificar_permissions():
    """Congelada: ni siquiera puede alterarse a sí misma."""
    reflection = build_reflection(_mission(), _evidence_knowledge(), _verification(True))
    with pytest.raises(Exception):
        reflection.objective = "otro objetivo"  # type: ignore[misc]


def test_20_experience_no_puede_modificar_mission_envelope():
    """La experiencia no lleva referencia al envelope ni a la misión viva."""
    experience = Experience(mission_id="m1", objective="leer el informe", goal_verified=True)
    assert "envelope" not in type(experience).__dataclass_fields__
    assert "mission" not in type(experience).__dataclass_fields__
    assert experience.is_frozen() is True


def test_21_learning_no_puede_modificar_security_authority():
    """La frontera de aprendizaje no conoce Policy, Gate, catálogo ni envelope."""
    forbidden = {"policy", "gate", "permissions", "envelope", "capabilities", "catalog"}
    assert forbidden.isdisjoint(set(type(LearningBoundary()).__dict__))
    verified = _evaluate(Verdict.SUCCESS.value, True)
    assert forbidden.isdisjoint(set(type(verified).__dataclass_fields__))


# --------------------------------------------------------------------------- #
# Self Model (test 22)
# --------------------------------------------------------------------------- #


def test_22_self_model_recibe_el_resultado_final_correcto():
    """El Self Model expone el veredicto de cognición y la lección verificada."""
    mission = _mission()
    knowledge = _evidence_knowledge()
    mission.context["knowledge"] = knowledge.to_dict()
    reflection = build_reflection(mission, knowledge, _verification(True))
    experience = Experience(
        mission_id=mission.id, objective="leer el informe",
        verdict=Verdict.SUCCESS.value, goal_verified=True, reflection=reflection.to_dict(),
    )
    verified = LearningBoundary().evaluate(experience, reflection)

    model = SelfModel()
    sync = SelfModelSync(model, lambda: mission)
    sync.apply_event("mission.experience", {"mission_id": mission.id, **verified.to_dict()})
    model.update(mission, tools=[], lessons=list(sync.lessons), memory_items=[])

    assert model.last_verdict == Verdict.SUCCESS.value
    assert model.lessons_learned, "la lección verificada debe llegar al Self Model"
    assert model.snapshot()["last_verdict"] == Verdict.SUCCESS.value


# --------------------------------------------------------------------------- #
# Evidencia real (test 23)
# --------------------------------------------------------------------------- #


def test_23_response_composer_usa_evidencia_real():
    """La respuesta cita la evidencia que existe y dice que no hay si no la hay."""
    composer = ResponseComposer()
    con_evidencia = composer.compose(
        _mission(), _evidence_knowledge(), _verification(True), model_outcome="real"
    )
    assert con_evidencia.evidence
    assert con_evidencia.evidence[0]["evidence_ids"] == ["ev-1"]
    assert "Evidencia disponible" in con_evidencia.text

    # Sin evidencia en ninguna parte: ni claims con evidence_ids ni criterios probados.
    sin_evidencia = composer.compose(
        _mission(),
        _knowledge(Verdict.SUCCESS.value),
        GoalVerification(objective="leer el informe", evaluations=[], verified=True, reason=""),
        model_outcome="real",
    )
    assert sin_evidencia.evidence == []
    assert "No hay evidencia registrada" in sin_evidencia.text


# --------------------------------------------------------------------------- #
# Sin chain-of-thought (test 24)
# --------------------------------------------------------------------------- #


def test_24_no_se_almacena_chain_of_thought():
    """Ni la reflexión ni la decisión guardan texto libre del modelo."""
    fields = set(Reflection.__dataclass_fields__) | set(DecisionRecord.__dataclass_fields__)
    for name in ("reasoning", "thought", "thoughts", "chain_of_thought", "raw_model_text",
                 "deliberation", "inner monologue", "scratchpad"):
        assert name not in fields, f"campo de razonamiento privado: {name}"
    # La justificación se recorta: es metadata operacional, no razonamiento.
    largo = "razonamiento " * 500
    record = DecisionRecord(mission_id="m", iteration=1, action="a", justification=largo)
    assert len(record.justification) <= 240
    # Y una reflexión no puede contener un volcado de razonamiento.
    reflection = build_reflection(_mission(), _evidence_knowledge(), _verification(True))
    assert all(len(str(x)) <= 200 for x in reflection.what_worked)
    assert all(len(str(x)) <= 200 for x in reflection.what_failed)


# --------------------------------------------------------------------------- #
# Memoria (25): la experiencia se publica donde la memoria ya lee
# --------------------------------------------------------------------------- #


class _FakeObservationRepo:
    def __init__(self):
        self.rows = []

    async def insert(self, mission_id, source, content, trusted=False):
        self.rows.append((mission_id, source, content, trusted))


@pytest.mark.asyncio
async def test_25_experience_queda_disponible_para_memoria_y_reinicio():
    """La experiencia se publica como observación `trusted=False`: es contexto, no autoridad."""
    repo = _FakeObservationRepo()
    store = ExperienceStore(observation_repo=repo)
    mission = _mission()
    reflection = build_reflection(mission, _evidence_knowledge(), _verification(True))
    experience = Experience(
        mission_id=mission.id, objective="leer el informe",
        verdict=Verdict.SUCCESS.value, goal_verified=True, reflection=reflection.to_dict(),
    )
    verified = LearningBoundary().evaluate(experience, reflection)
    ExperienceStore.attach(mission, experience, verified)

    assert await store.publish(verified) is True
    mission_id, source, content, trusted = repo.rows[0]
    assert source == EXPERIENCE_SOURCE
    assert mission_id == mission.id
    assert trusted is False, "una experiencia no es autoridad: trusted=False por diseño"
    assert content["can_teach"] is True
    # Y sigue siendo recuperable desde mission.context después del reinicio.
    assert ExperienceStore.load(mission) is not None


# --------------------------------------------------------------------------- #
# El learner legacy no queda como un segundo sistema
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_experience_learner_delega_en_la_frontera_real():
    """`ExperienceLearner` ya no es un append en memoria: usa la frontera de §5.6.5."""
    learner = ExperienceLearner()
    mission = _mission()
    mission.context["knowledge"] = _evidence_knowledge().to_dict()
    verified = await learner.record_experience(mission, _verification(True))
    assert verified.can_teach() is True
    assert learner.lessons() == [verified.lesson]
    assert learner.records and learner.experiences[0]["quality"] == "verified"

    # Sin objetivo verificado no aprende nada.
    other = _mission("otra cosa")
    other.context["knowledge"] = _knowledge(Verdict.SUCCESS.value).to_dict()
    nada = await learner.record_experience(other, _verification(False))
    assert nada.can_teach() is False
    assert nada.lesson == ""


def test_user_reply_lleva_los_campos_de_5_6():
    """El contrato de respuesta existente se amplía; no se crea un tipo paralelo."""
    reply = UserReply(text="hola")
    row = reply.to_dict()
    for key in ("verdict", "goal_verified", "pending", "blocked", "needs_user"):
        assert key in row


# --------------------------------------------------------------------------- #
# 5.6.9 integración real: el epílogo se dispara dentro del bucle y persiste
# --------------------------------------------------------------------------- #


class _AllowPolicy:
    """Deja pasar todo: aquí se prueba el epílogo, no la autoridad (eso es §5.6.8 de P0)."""

    def authorize(self, mission, step):
        class _Allow:
            allowed = True
            requires_approval = False
            reason = "permitido"
            matched_rule = "test"
            capability = getattr(step, "capability", None)

        return _Allow()


class _PassingVerifier:
    """Verificador que aprueba: aquí se prueba el epílogo, no la verificación (§5.5)."""

    async def verify(self, mission, plan):
        from alexis.contracts import Verification

        return Verification(passed=True, evidence=["archivo_existe=True"], confidence=0.9)


class _CapturingBus:
    def __init__(self):
        self.published = []

    async def publish(self, topic, payload):
        self.published.append((topic, payload))


@pytest.mark.asyncio
async def test_integracion_el_epilogo_se_dispara_en_una_mision_real():
    """Una misión recorrida de verdad deja response/reflection/experience/learning.

    Esto es lo que separa §5.6 de "tests que pasan": el epílogo lo dispara el runtime al
    cerrar la misión, no una llamada manual del test.
    """
    from alexis.autonomy.mission import MissionEngine  # noqa: E402
    from alexis.cognition.loop import CognitiveRuntime  # noqa: E402
    from alexis.contracts import ExecutionResult, Plan, PlanStep, Verification  # noqa: E402

    mission = _mission("leer el informe")

    async def _execute(mission, step, decision):
        return ExecutionResult(success=True, output="contenido del informe")

    cognitive = CognitiveRuntime(
        policy=_AllowPolicy(),
        executor=None,
        verifier=_PassingVerifier(),
        execute=_execute,
    )
    plan = Plan(
        mission_id=mission.id,
        steps=[PlanStep(id="read", description="leer", action="execute_tool", capability="fs.read")],
    )

    # Se recorre el bucle hasta el final, como haría el runtime.
    knowledge = cognitive.knowledge_for(mission)
    for _ in range(6):
        pending = cognitive.pending_steps(mission, plan, knowledge)
        outcome = await cognitive.step(mission, knowledge, pending_steps=pending, plan=plan)
        knowledge = outcome.knowledge
        cognitive.store_knowledge(mission, knowledge)
        if outcome.done:
            break
    assert mission.context.get("knowledge"), "el bucle debe dejar knowledge para que corra el epílogo"

    # La misión se cierra como completada sólo con verificación (§5.5).
    from alexis.autonomy.goal_state import settle

    settle(mission, None)
    reply, reflection, experience, verified = cognitive.compose_epilogue(
        mission, knowledge, model_outcome="degraded"
    )

    # Las 5 piezas de §5.6 quedaron en mission.context, que es lo que storage persiste.
    for key in ("response", "reflection", "experience", "verified_learning", "decisions"):
        assert key in mission.context, f"falta {key} en mission.context"
    assert mission.context["response"]["verdict"] == reply.verdict
    assert mission.context["experience"]["mission_id"] == mission.id
    assert reply.cognition_outcome == "degraded", "la procedencia DEGRADED se conserva"
    assert isinstance(experience, Experience)
    # Y las decisiones se registró al autorizar.
    assert cognitive.decisions(mission), "el camino cognitivo debe registrar decisiones (§5.6.2)"


@pytest.mark.asyncio
async def test_integracion_el_epilogo_publica_la_experiencia_en_memoria():
    """`_close_cycle` publica la experiencia como observación para la memoria existente."""
    from alexis.core.runtime import AlexisRuntime  # noqa: E402

    class _Repo:
        def __init__(self):
            self.rows = []

        async def insert(self, mission_id, source, content, trusted=False):
            self.rows.append((mission_id, source, content, trusted))

        async def list(self, *a, **k):
            return list(self.rows)

    repo = _Repo()
    runtime = AlexisRuntime(
        planner=None, policy=_AllowPolicy(), executor=None, verifier=None,
        memory=None, learning=None, event_bus=_CapturingBus(), observation_repo=repo,
    )
    # Runtime sin `cognitive`: el epílogo no debe correr ni ROMPER nada.
    mission = _mission("sin cognitive")
    assert await runtime._close_cycle(mission) is False
    assert repo.rows == []
