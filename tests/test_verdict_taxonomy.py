"""P0 §5.2 — taxonomía de veredictos: SUCCESS, PARTIAL_SUCCESS, FAILURE,
INSUFFICIENT_EVIDENCE y BLOCKED.

El requisito 10 del plan exige que después de CADA acción se determine el estado de la
acción. Este archivo fija dos cosas:

1. El contrato de cada veredicto (qué significa y qué no significa).
2. Que el bucle EMITE el veredicto en todos sus caminos de retorno, no que existe una
   enumeración sin usar.

La línea que este incremento cruzaría si se pasara de la raya: usar SUCCESS para decir
que el objetivo se consiguió. No hay verificación a nivel de objetivo hasta §5.3/§5.5,
así que ningún camino puede afirmar eso. Hay un test explícito para eso.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.cognition.loop import CognitiveRuntime, verdict_for_execution  # noqa: E402
from alexis.cognition.state import KnowledgeState, Verdict  # noqa: E402
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

ACTIONS = ["understand", "analyze", "research", "execute", "verify", "modify", "respond"]


def _mission(objective, **over):
    data = dict(
        objective=objective,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=list(ACTIONS),
        capabilities=[],
    )
    data.update(over)
    return MissionEngine().create(objective, MissionEnvelope(**data))


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
    """Ejecutor con guion: salida observable, salida vacía o error."""

    def __init__(self, script, errors=None):
        self.script = dict(script)
        self.errors = dict(errors or {})
        self.calls = []

    async def execute(self, mission, step, *, tool_name=None):
        self.calls.append(step.id)
        mode = self.script.get(step.id, "ok")
        if mode == "fail":
            return ExecutionResult(
                success=False,
                output={"ok": False},
                error=self.errors.get(step.id, "no such file or directory: notas.txt"),
                observations=[Observation("tool.fs", {"ok": False}, trusted=True)],
            )
        if mode == "silent":
            return ExecutionResult(success=True, output=None)
        return ExecutionResult(
            success=True,
            output={"ok": True, "step": step.id},
            observations=[Observation("tool.fs", {"ok": True}, trusted=True)],
        )


class _PassingVerifier:
    def __init__(self, passed=True):
        self.passed = passed

    async def verify(self, mission, plan):
        return Verification(
            passed=self.passed,
            evidence=["archivo_existe=True"],
            confidence=0.9,
            notes="verificación del workspace",
            checks=[
                VerificationCheck("fs.stat", "deterministic", self.passed, "v", ["e1"], "fs", True)
            ],
            independent=True,
        )


class _NoopPolicy:
    def __init__(self, allowed=True, requires_approval=False):
        self.allowed = allowed
        self.requires_approval = requires_approval

    def authorize(self, mission, step):
        class _D:
            pass

        d = _D()
        d.allowed = self.allowed
        d.requires_approval = self.requires_approval
        d.reason = "política de test"
        return d

    def evaluate(self, mission, step):
        return self.authorize(mission, step)


def _runtime(executor, verifier, policy=None, **over):
    return CognitiveRuntime(
        policy=policy or _NoopPolicy(),
        executor=executor,
        verifier=verifier,
        **over,
    )


async def _drain(cognitive, mission, plan, limit=12):
    """Recorre el bucle y devuelve el conocimiento final y TODOS los outcomes.

    Se guardan todos porque el veredicto se evalúa por iteración: el último outcome de
    un ciclo sano es FINISH, y el veredicto interesting es el de la iteración anterior.
    """
    knowledge = cognitive.knowledge_for(mission)
    outcomes = []
    for _ in range(limit):
        pending = cognitive.pending_steps(mission, plan, knowledge)
        outcome = await cognitive.step(mission, knowledge, pending_steps=pending, plan=plan)
        knowledge = outcome.knowledge
        outcomes.append(outcome)
        if outcome.done:
            return knowledge, outcomes
    raise AssertionError("el bucle no terminó")


def _executed(outcomes):
    """El outcome de la primera iteración que llegó a ejecutar una tool."""
    for outcome in outcomes:
        if outcome.result is not None:
            return outcome
    raise AssertionError("ninguna iteración ejecutó una tool")


def _with_action(outcomes, action):
    for outcome in outcomes:
        if outcome.action.value == action:
            return outcome
    raise AssertionError(
        f"ninguna iteración took {action!r}: {[o.action.value for o in outcomes]}"
    )


# ----------------------------------------------------------------------
# Contrato de la taxonomía
# ----------------------------------------------------------------------


def test_la_taxonomia_tiene_exactamente_los_cinco_verdicts_del_plan():
    assert [v.value for v in Verdict] == [
        "success",
        "partial_success",
        "failure",
        "insufficient_evidence",
        "blocked",
    ]


def test_los_verdicts_valores_que_concluyen_y_los_que_no():
    assert Verdict.SUCCESS.is_conclusive is True
    assert Verdict.PARTIAL_SUCCESS.is_conclusive is True
    assert Verdict.FAILURE.is_conclusive is True
    assert Verdict.INSUFFICIENT_EVIDENCE.is_conclusive is False
    assert Verdict.BLOCKED.is_conclusive is False


@pytest.mark.parametrize(
    "result, expected",
    [
        (None, Verdict.INSUFFICIENT_EVIDENCE),
        (ExecutionResult(success=False, error="no such file"), Verdict.FAILURE),
        (ExecutionResult(success=True, output=None), Verdict.PARTIAL_SUCCESS),
        (ExecutionResult(success=True, output=""), Verdict.PARTIAL_SUCCESS),
        (ExecutionResult(success=True, output={}), Verdict.PARTIAL_SUCCESS),
        (ExecutionResult(success=True, output=[]), Verdict.PARTIAL_SUCCESS),
        (ExecutionResult(success=True, output={"ok": True}), Verdict.SUCCESS),
        (ExecutionResult(success=True, output="contenido"), Verdict.SUCCESS),
        (ExecutionResult(success=True, output=0), Verdict.SUCCESS),
    ],
)
def test_verdict_for_execution_contrato(result, expected):
    assert verdict_for_execution(result) is expected


def test_una_tool_que_termina_bien_en_silencio_no_es_success():
    """Ejecutar sin error no es conseguir: sin observación no hay evidencia."""
    assert verdict_for_execution(ExecutionResult(success=True, output=None)) is not Verdict.SUCCESS


# ----------------------------------------------------------------------
# Uso real: cada camino del bucle emite su veredicto
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ejecucion_con_observacion_es_success():
    mission = _mission("lee el informe")
    plan = Plan("m", [_step("leer")])

    _, outcomes = await _drain(_runtime(_ScriptedExecutor({}), _PassingVerifier()), mission, plan)

    outcome = _executed(outcomes)
    assert outcome.verdict is Verdict.SUCCESS
    assert "observación" in outcome.verdict_reason


@pytest.mark.asyncio
async def test_ejecucion_sin_observacion_es_partial_success():
    mission = _mission("toca el archivo")
    plan = Plan("m", [_step("tocar")])

    _, outcomes = await _drain(
        _runtime(_ScriptedExecutor({"tocar": "silent"}), _PassingVerifier()), mission, plan
    )

    outcome = _executed(outcomes)
    assert outcome.verdict is Verdict.PARTIAL_SUCCESS
    assert "sin error" in outcome.verdict_reason


@pytest.mark.asyncio
async def test_ejecucion_fallida_es_failure():
    mission = _mission("lee notas.txt")
    plan = Plan("m", [_step("leer")])

    _, outcomes = await _drain(
        _runtime(_ScriptedExecutor({"leer": "fail"}), _PassingVerifier()), mission, plan
    )

    outcome = _executed(outcomes)
    assert outcome.verdict is Verdict.FAILURE
    assert outcome.result.success is False


@pytest.mark.asyncio
async def test_verificacion_pasada_es_success_y_fracasada_es_failure():
    mission = _mission("revisa el proyecto")
    plan = Plan("m", [_step("mirar")])

    _, ok_outcomes = await _drain(
        _runtime(_ScriptedExecutor({}), _PassingVerifier(passed=True), max_replans=0), mission, plan
    )
    ok = _with_action(ok_outcomes, "verify")
    assert ok.verdict is Verdict.SUCCESS
    assert "verificación del plan pasó" in ok.verdict_reason

    mission2 = _mission("revisa el proyecto")
    _, ko_outcomes = await _drain(
        _runtime(
            _ScriptedExecutor({}),
            _PassingVerifier(passed=False),
            max_replans=0,
        ),
        mission2,
        plan,
    )
    ko = _with_action(ko_outcomes, "verify")
    assert ko.verdict is Verdict.FAILURE
    assert "falló" in ko.verdict_reason


@pytest.mark.asyncio
async def test_plan_invalido_es_blocked():
    """El validador rechaza el paso: la acción no llega a ejecutarse."""
    mission = _mission("borra el proyecto entero")
    plan = Plan("m", [_step("borrar", capability="fs.remove", approval=True)])

    class _Rejects:
        def validate_step(self, mission, step):
            return ["fuera del envelope"]

    cognitive = _runtime(
        _ScriptedExecutor({}),
        _PassingVerifier(),
        plan_validator=_Rejects(),
    )
    _, outcomes = await _drain(cognitive, mission, plan)
    outcome = outcomes[-1]

    assert outcome.verdict is Verdict.BLOCKED
    assert outcome.mission_state is MissionState.BLOCKED
    assert "fuera del envelope" in outcome.verdict_reason


@pytest.mark.asyncio
async def test_politica_que_lo_nega_es_blocked():
    mission = _mission("borra el proyecto")
    plan = Plan("m", [_step("borrar", approval=True)])

    cognitive = _runtime(
        _ScriptedExecutor({}),
        _PassingVerifier(),
        policy=_NoopPolicy(allowed=False),
    )
    _, outcomes = await _drain(cognitive, mission, plan)
    outcome = outcomes[-1]

    assert outcome.verdict is Verdict.BLOCKED
    assert "autoridad" in outcome.verdict_reason


@pytest.mark.asyncio
async def test_esperando_aprobacion_es_blocked():
    mission = _mission("publica el informe")
    plan = Plan("m", [_step("publicar", approval=True)])

    cognitive = _runtime(
        _ScriptedExecutor({}),
        _PassingVerifier(),
        policy=_NoopPolicy(requires_approval=True),
    )
    _, outcomes = await _drain(cognitive, mission, plan)
    outcome = outcomes[-1]

    assert outcome.verdict is Verdict.BLOCKED
    assert outcome.mission_state is MissionState.WAITING_APPROVAL
    assert "aprobación" in outcome.verdict_reason


@pytest.mark.asyncio
async def test_sin_avance_es_failure_no_un_success_fantasma():
    """ABORT por no progreso: FAILURE, nunca un SUCCESS de consolación."""
    mission = _mission("revisa tres cosas")
    plan = Plan("m", [_step("a", action="research"), _step("b", action="research")])
    executor = _ScriptedExecutor({"a": "fail", "b": "fail"}, errors={"a": "boom", "b": "boom"})

    cognitive = _runtime(executor, _PassingVerifier(passed=False), max_replans=2)
    _, outcomes = await _drain(cognitive, mission, plan, limit=20)

    outcome = _with_action(outcomes, "abort")
    assert outcome.mission_state is MissionState.FAILED
    assert outcome.verdict is Verdict.FAILURE
    assert outcome.verdict is not Verdict.SUCCESS
    assert all(o.verdict is not Verdict.SUCCESS for o in outcomes[-1:])


@pytest.mark.asyncio
async def test_replan_tras_un_fallo_es_failure():
    """Un replan viene siempre de un fallo previo: su veredicto es FAILURE."""
    mission = _mission("lee notas.txt")
    plan = Plan("m", [_step("a", action="research"), _step("b", action="research")])
    executor = _ScriptedExecutor({"a": "fail", "b": "fail"})

    cognitive = _runtime(executor, _PassingVerifier(), max_replans=2)
    _, outcomes = await _drain(cognitive, mission, plan, limit=20)

    replan = _with_action(outcomes, "replan")
    assert replan.verdict is Verdict.FAILURE
    assert "fallo" in replan.verdict_reason
    assert replan.verdict is not Verdict.SUCCESS


@pytest.mark.asyncio
async def test_replan_sin_fallo_previo_no_afirma_fallo():
    """Si se replanifica sin un fallo registrado, no se inventa un FAILURE."""
    mission = _mission("revisa el proyecto")
    plan = Plan("m", [_step("a", action="research")])
    executor = _ScriptedExecutor({})

    cognitive = _runtime(executor, _PassingVerifier(), max_replans=2)
    knowledge = cognitive.knowledge_for(mission)
    knowledge.needs_replan = True
    pending = cognitive.pending_steps(mission, plan, knowledge)
    outcome = await cognitive.step(mission, knowledge, pending_steps=pending, plan=plan)

    assert outcome.action.value == "replan"
    assert outcome.verdict is Verdict.INSUFFICIENT_EVIDENCE
    assert outcome.verdict is not Verdict.FAILURE


@pytest.mark.asyncio
async def test_preguntar_al_usuario_es_insufficient_evidence():
    mission = _mission("haz que el proyecto sea más rápido")
    mission.context["intent"] = {
        "kind": "task",
        "objective": mission.goal.objective,
        "ambiguity": "¿Backend, frontend o consultas?",
        "needs_clarification": True,
        "confidence": 0.4,
    }
    plan = Plan("m", [_step("investigar", action="research")])
    executor = _ScriptedExecutor({})

    _, outcomes = await _drain(_runtime(executor, _PassingVerifier()), mission, plan)
    outcome = _with_action(outcomes, "ask_user")

    assert outcome.action.value == "ask_user"
    assert outcome.verdict is Verdict.INSUFFICIENT_EVIDENCE
    assert outcome.verdict is not Verdict.SUCCESS
    assert executor.calls == []


# ----------------------------------------------------------------------
# El contrato que más importa: 5.2 NO verifica objetivos
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalizar_no_declara_el_objetivo_verificado():
    """El ciclo puede terminar en COMPLETED y aun así no afirmar nada sobre el objetivo.

    Es la deuda que §5.3/§5.5 deben cerrar. Este test la fija para que nadie la tape con
    un SUCCESS: mientras no exista `GoalVerifier`, terminar no es prueba de nada.
    """
    mission = _mission("lee el informe y dime si está bien")
    plan = Plan("m", [_step("leer")])

    _, outcomes = await _drain(_runtime(_ScriptedExecutor({}), _PassingVerifier()), mission, plan)
    outcome = _with_action(outcomes, "finish")

    # P0 §5.5 cerró la deuda que este test documentaba: la misión ya no puede quedar
    # COMPLETED. Termina en NEEDS_VERIFICATION ("todavía no"), que no es FAILED.
    assert outcome.mission_state is MissionState.NEEDS_VERIFICATION
    assert outcome.verdict is Verdict.INSUFFICIENT_EVIDENCE
    assert outcome.verdict is not Verdict.SUCCESS
    assert "§5.5" in outcome.verdict_reason


@pytest.mark.asyncio
async def test_ningun_camino_terminado_emite_success():
    """Barrido: por muchos caminos que se recorran, el último veredicto no es SUCCESS."""
    mission = _mission("lee el informe y dime si está bien")
    plan = Plan("m", [_step("leer")])
    cognitive = _runtime(_ScriptedExecutor({}), _PassingVerifier())

    knowledge = cognitive.knowledge_for(mission)
    seen = []
    for _ in range(6):
        pending = cognitive.pending_steps(mission, plan, knowledge)
        outcome = await cognitive.step(mission, knowledge, pending_steps=pending, plan=plan)
        knowledge = outcome.knowledge
        seen.append(outcome.verdict)
        if outcome.done:
            break

    assert seen, "el bucle no llegó a producir ningún veredicto"
    assert seen[-1] is not Verdict.SUCCESS
    assert seen[-1] is Verdict.INSUFFICIENT_EVIDENCE, "sin objetivo verificado no hay SUCCESS"
    assert Verdict.SUCCESS in seen, "el camino de ejecución real debería haberse visto"


# ----------------------------------------------------------------------
# Registro y persistencia del veredicto
# ----------------------------------------------------------------------


def test_el_verdicto_por_defecto_no_puede_fingir_exito():
    """Un Outcome construido sin veredicto explícito no puede pasar por SUCCESS."""
    from alexis.cognition.loop import StepOutcome
    from alexis.cognition.state import Decision, NextAction

    outcome = StepOutcome(
        action=NextAction.EXECUTE_TOOL,
        knowledge=KnowledgeState(),
        decision=Decision(action=NextAction.EXECUTE_TOOL, rationale="x"),
    )

    assert outcome.verdict is Verdict.INSUFFICIENT_EVIDENCE
    assert outcome.verdict is not Verdict.SUCCESS


@pytest.mark.asyncio
async def test_el_verdicto_queda_registrado_en_el_conocimiento():
    mission = _mission("lee el informe")
    plan = Plan("m", [_step("leer")])

    knowledge, outcomes = await _drain(_runtime(_ScriptedExecutor({}), _PassingVerifier()), mission, plan)
    outcome = outcomes[-1]

    assert knowledge.last_verdict == outcome.verdict.value
    assert knowledge.verdict_reason == outcome.verdict_reason


def test_el_verdicto_sobrevive_a_un_reinicio():
    state = KnowledgeState(
        objective="obj",
        last_verdict=Verdict.INSUFFICIENT_EVIDENCE.value,
        verdict_reason="no se ejecutó nada todavía",
    )

    reloaded = KnowledgeState.from_dict(state.to_dict())

    assert reloaded.last_verdict == "insufficient_evidence"
    assert reloaded.verdict_reason == "no se ejecutó nada todavía"


@pytest.mark.asyncio
async def test_el_verdicto_se_publica_en_el_outcome_serializado():
    mission = _mission("lee el informe")
    plan = Plan("m", [_step("leer")])

    _, outcomes = await _drain(_runtime(_ScriptedExecutor({}), _PassingVerifier()), mission, plan)
    outcome = outcomes[-1]
    payload = outcome.to_dict()

    assert payload["verdict"] == outcome.verdict.value
    assert payload["verdict_reason"] == outcome.verdict_reason
    assert "success" in payload, "el veredicto no puede sustituir al resultado de la tool"
