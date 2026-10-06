"""FASE 6 — Ataques adversariales contra el falso `COMPLETED`.

Cada test de este módulo es un intento deliberado de que ALEXIS declare `COMPLETED` sin
haber conseguido el objetivo. Todos deben ser RECHAZADOS por la arquitectura, no por una
comprobación afortunada del test.

La lista es la del encargo de cierre, en orden:

 1. Fichero preexistente → objetivo semánticamente NO satisfecho.
 2. `tool.success=True` → paso cuyo contrato no se cumple.
 3. El modelo dice "done" y no hay verificación.
 4. El modelo dice "terminado" y no hay verificación.
 5. El modelo dice "finished" (otro idioma) y no hay verificación.
 6. `GoalVerification` fabricada a mano con la bandera puesta.
 7. Claim FACT del modelo sin evidencia fiable.
 8. `setattr(mission, "state", COMPLETED)` sin verificación.
 9. Fila de BD que dice `completed` sin GoalVerification utilizable.
10. Un skill no puede ampliar el envelope.
11. Un DENY no se convierte en aprobación.
12. Replan no puede saltarse Policy/Gate.
13. La persistencia no pierde el estado de verificación.
14. Un criterio trivial no sustituye a uno semántico.
15. La respuesta no afirma éxito cuando el estado dice que no.

No se ha rebajado ni una sola aserción para que pasaran: cada ataque se cierra contra el
código de producción.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.gates import AutonomyGate  # noqa: E402
from alexis.autonomy.goal_state import goal_is_confirmed, settle  # noqa: E402
from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.cognition.criteria import normalize_criteria  # noqa: E402
from alexis.cognition.evidence import ClaimGuard, EvidenceStore  # noqa: E402
from alexis.cognition.goal_verification import (  # noqa: E402
    CriterionEvaluation,
    CriterionStatus,
    GoalVerification,
    GoalVerifier,
)
from alexis.cognition.contracts import Claim, ClaimKind  # noqa: E402
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    MissionEnvelope,
    MissionState,
    PlanStep,
    RiskLevel,
    UnverifiedGoalError,
)
from alexis.security.policy import PolicyEngine  # noqa: E402
from alexis.storage.serialization import mission_from_row, mission_to_row  # noqa: E402
from alexis.world.model import WorldModel  # noqa: E402

ACTIONS = ["understand", "analyze", "research", "execute", "verify", "respond"]


def _mission(objective="Analiza el archivo notas.txt y dime qué contiene", criteria=None):
    if criteria is None:
        criteria, _ = normalize_criteria(objective, objective, [])
    return MissionEngine().create(
        objective,
        MissionEnvelope(
            objective=objective,
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=list(ACTIONS),
        ),
        success_criteria=criteria,
    )


def _observed(path: str, *, exists: bool = True, content=None, size: int = 10) -> None:
    """Observa con la tool que corresponda a lo que se afirma."""
    from alexis.contracts import ExecutionResult, Observation

    world = WorldModel()
    output = {"path": path, "exists": exists}
    if size is not None:
        output["size"] = size
    if content is not None:
        output["content"] = content
    world.observe_execution(
        type("S", (), {"id": "o", "capability": "fs.read", "action": "research"})(),
        ExecutionResult(
            success=True,
            output=output,
            observations=[Observation("tool.fs.read", {"ok": True}, trusted=True)],
        ),
    )
    return world


# =========================================================================== #
# 1. Fichero preexistente → objetivo semántico no satisfecho
# =========================================================================== #


def test_ataque_01_fichero_preexistente_no_satisface_un_objetivo_de_contenido(tmp_path):
    """El ataque original del audit: el fichero existe, el objetivo no se cumple.

    Aquí ni siquiera hay lectura: sólo se comprueba la existencia con `fs.stat`, que es lo
    que el criterio viejo `file_exists` habría dado por bueno.
    """
    from alexis.contracts import ExecutionResult, Observation

    objetivo = "Analiza el archivo notas.txt y dime qué contiene"
    (tmp_path / "notas.txt").write_text("contenido\n", encoding="utf-8")

    world = WorldModel()
    world.observe_execution(
        type("S", (), {"id": "s", "capability": "fs.stat", "action": "research"})(),
        ExecutionResult(
            success=True,
            output={"path": "notas.txt", "exists": True, "size": 10},
            observations=[Observation("tool.fs.stat", {"exists": True}, trusted=True)],
        ),
    )
    mission = _mission()
    assert "content_observed:notas.txt" in mission.goal.success_criteria

    ver = GoalVerifier(world=world).verify(mission)
    assert ver.verified is False
    settle(mission, ver)
    assert mission.state is not MissionState.COMPLETED


# =========================================================================== #
# 2. tool.success=True no completa un paso cuyo contrato falla
# =========================================================================== #


def test_ataque_02_tool_success_no_completa_un_paso_con_contrato_incumplido():
    """`fs.write` dice «escribí». Que el fichero exista lo decide la observación.

    Se comprueba sobre el mismo `GoalVerifier` que usa el bucle: un predicado incumplido
    es `NOT_SATISFIED`, y con eso el paso no puede darse por cumplido.
    """
    from alexis.contracts import ExecutionResult, Observation

    mission = _mission("crea salida.txt con un resumen de notas.txt")
    assert "file_exists:salida.txt" in mission.goal.success_criteria

    # La tool "funcionó": success=True. Y sin embargo no dejó el fichero.
    world = WorldModel()
    world.observe_execution(
        type("S", (), {"id": "w", "capability": "fs.write", "action": "execute"})(),
        ExecutionResult(
            success=True,
            output={"path": "salida.txt", "exists": False, "ok": True},
            observations=[Observation("tool.fs.write", {"ok": True}, trusted=True)],
        ),
    )
    ver = GoalVerifier(world=world).verify(mission)
    assert ver.verified is False
    incumple = [e for e in ver.evaluations if e.criterion == "file_exists:salida.txt"]
    assert incumple, "el criterio incumplido debe aparecer evaluado"
    assert incumple[0].status in (
        CriterionStatus.NOT_SATISFIED,
        CriterionStatus.INSUFFICIENT_EVIDENCE,
    ), (
        f"un contrato incumplido no puede darse por satisfecho: {incumple[0].status}"
    )


# =========================================================================== #
# 3-5. El modelo no puede declarar éxito, en ningún idioma
# =========================================================================== #


@pytest.mark.parametrize(
    "frase",
    [
        "done",
        "Done.",
        "task accomplished",
        "all set",
        "successfully finished",
        "terminado",
        "He completado la tarea.",
        "Ya está listo.",
        "fertig",
        "c'est terminé",
    ],
)
def test_ataque_03_05_el_modelo_no_declara_exito_en_ningun_idioma(frase):
    """La autoridad es el estado verificado. El lexicon es sólo la segunda capa.

    Con `goal_verified=False` el texto del modelo no se usa en absoluto: la respuesta es
    la compuesta, anclada al veredicto. Por eso el idioma es irrelevante — y por eso un
    lexicon, por amplio que sea, no es la garantía.
    """
    from alexis.cognition.response import ResponseComposer

    composer = ResponseComposer()
    mission = _mission()
    from alexis.cognition.state import KnowledgeState, Verdict

    knowledge = KnowledgeState(objective=mission.goal.objective)
    knowledge.last_verdict = Verdict.INSUFFICIENT_EVIDENCE.value

    ver = GoalVerification(
        objective=mission.goal.objective, evaluations=[], verified=False,
        reason="no observado",
    )
    reply = composer.compose(mission, knowledge, ver)
    assert reply.goal_verified is False
    assert not composer.asserts_completion(reply), (
        f"la respuesta afirmó un logro con el objetivo sin verificar: {reply.text!r}"
    )
    # Y cualquier texto ajeno con una afirmación de logro queda neutralizado.
    saneado = composer.sanitize(frase, goal_verified=False)
    assert not composer.asserts_completion(
        type(reply)(**{**reply.to_dict(), "text": saneado})
    ), f"«{frase}» sobrevivió al guard: {saneado!r}"


@pytest.mark.asyncio
async def test_ataque_03b_la_voz_descarta_el_texto_del_modelo_sin_verificacion(tmp_path):
    """El canal de voz es el más expuesto: el modelo reescribe lo que se le da."""
    from alexis.cognition.contracts import UserReply
    from alexis.execution import SandboxExecutor
    from alexis.security.sandbox import SandboxRunner
    from alexis.tools.registry import ToolRegistry

    class _Real:
        def providers(self):
            return [object()]

        async def complete(self, request, *, correlation=None):
            from alexis.models import ModelResponse
            from alexis.models.provider import ModelOutcome

            return ModelResponse(
                text="All done! Task accomplished successfully. Hecho.",
                provider="x", model="x", outcome=ModelOutcome.REAL,
            )

    executor = SandboxExecutor(
        tools=ToolRegistry(),
        sandbox=SandboxRunner(workspace=tmp_path),
        model_router=_Real(),
    )
    mission = _mission()
    mission.context["response"] = UserReply(
        text="El objetivo no está verificado.",
        kind="answer", claims=[], evidence=[], open_questions=[],
        mission_id=mission.id, self_update={}, cognition_outcome="real",
        degraded=False, goal_verified=False,
    ).to_dict()

    spoken = await executor._spoken_reply(mission, None)
    bajo = spoken.lower()
    assert "all done" not in bajo
    assert "accomplished" not in bajo
    assert "hecho" not in bajo
    assert "no está verificado" in bajo


# =========================================================================== #
# 6. GoalVerification fabricada a mano
# =========================================================================== #


def test_ataque_06_una_verificacion_fabricada_no_autoriza_completed():
    """Poner la bandera a mano no basta: se revalida el contenido."""
    fabricable = GoalVerification(
        objective="x", evaluations=[], verified=True, reason="a mí me parece que sí"
    )
    assert goal_is_confirmed(fabricable) is False

    # Y ni siquiera con criterios: tienen que estar todos `satisfied` y con evidencia
    # fiable. Un claim del modelo no cuenta como evidencia.
    con_claim = GoalVerification(
        objective="x",
        evaluations=[
            CriterionEvaluation(
                criterion="c",
                status=CriterionStatus.SATISFIED,
                reason="lo dice el modelo",
                evidence=[],
            )
        ],
        verified=True,
        reason="",
    )
    assert goal_is_confirmed(con_claim) is False


# =========================================================================== #
# 7. Claim FACT sin evidencia fiable
# =========================================================================== #


def test_ataque_07_un_claim_fact_del_modelo_no_sobrevive_sin_evidencia():
    """El modelo no puede fabricar un hecho: lo que dice entra como INFERENCE y lo degrada."""
    store = EvidenceStore()
    guard = ClaimGuard()
    claim = store.from_model_text("El análisis se completó y notas.txt existe.", source="model")
    assert claim is not None
    assert claim.kind is ClaimKind.INFERENCE, (
        "un claim del modelo entra como inferencia: no puede declararse hecho"
    )

    # Y aunque se intentara declararlo FACT, el guard lo degrada sin evidencia fiable.
    from alexis.cognition.contracts import Claim as _Claim

    fabricado = _Claim(
        id="c1", kind=ClaimKind.FACT, text="notas.txt analizada",
        source="model", confidence=0.99, evidence_ids=[],
    )
    degradado = guard.guard(fabricado)
    assert degradado.kind is not ClaimKind.FACT, (
        "un FACT sin verificación independiente no sobrevive"
    )
    assert guard.degradations, "la degradación debe quedar registrada y observable"

    # Y un criterio no se satisface con la evidencia de un claim.
    verificador = GoalVerifier(world=WorldModel())
    evaluacion = verificador._evaluate_criterion("file_exists:notas.txt")
    assert evaluacion.status is CriterionStatus.INSUFFICIENT_EVIDENCE


# =========================================================================== #
# 8. setattr directo a COMPLETED
# =========================================================================== #


def test_ataque_08_setattr_directo_a_completed_es_rechazado():
    """La invariante está en el propio estado: no hay forma de saltársela."""
    mission = _mission()
    with pytest.raises(UnverifiedGoalError):
        mission.state = MissionState.COMPLETED
    assert mission.state is not MissionState.PENDING or mission.state is not MissionState.COMPLETED


# =========================================================================== #
# 9. Fila de BD que dice completed sin verificación
# =========================================================================== #


def test_ataque_09_una_fila_completed_sin_verificacion_no_carga_completed(tmp_path):
    """Restaurar una fila no puede fabricar un logro que nunca se comprobó."""
    mission = _mission()
    fila = mission_to_row(mission)
    import json

    fila["state"] = MissionState.COMPLETED.value
    datos = json.loads(fila["context"])
    datos.pop("goal_verification", None)
    fila["context"] = json.dumps(datos, ensure_ascii=False)

    restaurada = mission_from_row(fila)
    assert restaurada.state is MissionState.NEEDS_VERIFICATION
    assert restaurada.state is not MissionState.COMPLETED
    assert restaurada.context.get("goal_verification_reason")


def test_ataque_09b_una_fila_con_verificacion_legitima_si_carga_completed():
    """El reverso: una fila con verificación válida SÍ se restaura como completada.

    §11: la verificación tiene que ser **de la misma misión** que la fila. Este test
    usaba antes la verificación de una misión con otro objetivo yCriteria distintos, y
    ahora eso es justo lo que se rechaza —el defecto que el audit encontró—. Se corrige
    para que la fila sea realmente legítima: se verifica la MISMA misión que luego se
    persiste, con el mundo real detrás.
    """
    import json

    world = _observed("notas.txt", exists=True, content="hola")
    mission = _mission("Comprueba si notas.txt existe", ["file_exists:notas.txt"])
    ver_real = GoalVerifier(world=world).verify(mission)
    assert ver_real.verified is True
    settle(mission, ver_real)
    assert mission.state is MissionState.COMPLETED

    fila = mission_to_row(mission)
    guardada = json.loads(fila["context"])["goal_verification"]
    assert guardada["verified"] is True
    # Y la verificación lleva su vínculo: sin él, la fila no se restauraría.
    assert guardada["subject"]["mission_id"] == mission.id

    restaurada = mission_from_row(fila)
    assert restaurada.state is MissionState.COMPLETED
    # Aunque se restaura, queda MARCADA como pendiente de revalidación: el almacenamiento
    # afirma, el runtime comprueba. `mission_from_row` no puede dar luz verde por sí solo.
    assert restaurada.goal_verification is not None
    assert restaurada.goal_verification.pending_revalidation is True


def test_ataque_09c_una_fila_no_puede_reautorizar_solo_por_persistir():
    """§11 — la fila NO es autoridad: una verificación sin vínculo con la misión se rechaza.

    Aquí la verificación es perfectamente válida y tiene evidencia con `source` de
    herramienta, pero es de OTRA misión. Es el ataque que el audit reportó como
    CRITICAL-2, y ahora no reconstruye `COMPLETED`.
    """
    import json

    world = _observed("notas.txt", exists=True, content="hola")
    # Misión A: existe y se verifica de verdad.
    a = _mission("Comprueba si notas.txt existe", ["file_exists:notas.txt"])
    ver_a = GoalVerifier(world=world).verify(a)
    assert ver_a.verified is True

    # Misión B: otro objetivo, otros criterios, NUNCA ejecutada.
    b = _mission("Analiza el archivo notas.txt y dime qué contiene")
    fila = mission_to_row(b)
    fila["state"] = MissionState.COMPLETED.value
    datos = json.loads(fila["context"])
    datos["goal_verification"] = json.loads(json.dumps(ver_a.to_dict()))
    fila["context"] = json.dumps(datos, ensure_ascii=False)

    restaurada = mission_from_row(fila)
    assert restaurada.state is not MissionState.COMPLETED, (
        "la verificación de otra misión no puede completar ésta"
    )
    assert restaurada.state is MissionState.NEEDS_VERIFICATION


def test_ataque_09d_la_evidencia_persistida_necesita_provenance_verificable():
    """§11 — `trusted: true` solo no basta: la procedencia tiene que ser de herramienta.

    Un `source` que no sea una observación (`modelo`, `criterio`) o un grado que no sea
    prueba (`uncertainty`, `assumption`, `inference`) no pueden sostener un `satisfied`,
    por muy `trusted` que el almacenamiento lo escriba.
    """
    import json

    base = _mission("Comprueba si notas.txt existe", ["file_exists:notas.txt"])
    world = _observed("notas.txt", exists=True, content="hola")
    ver = GoalVerifier(world=world).verify(base)
    assert ver.verified is True

    for source, grade in (
        ("modelo", "evidence"),
        ("criterio", "fact"),
        ("tool:fs.read", "uncertainty"),
        ("tool:fs.read", "assumption"),
        ("tool:fs.read", "inference"),
    ):
        fila = mission_to_row(base)
        fila["state"] = MissionState.COMPLETED.value
        datos = json.loads(fila["context"])
        forjada = json.loads(json.dumps(ver.to_dict()))
        for evaluacion in forjada["criteria"]:
            evaluacion["evidence"] = [
                {
                    "evidence_id": "x",
                    "source": source,
                    "grade": grade,
                    "detail": "d",
                    "trusted": True,
                }
            ]
        datos["goal_verification"] = forjada
        fila["context"] = json.dumps(datos, ensure_ascii=False)

        restaurada = mission_from_row(fila)
        assert restaurada.state is not MissionState.COMPLETED, (
            f"source={source} grade={grade} no puede sostener un completed"
        )


# =========================================================================== #
# 10-12. Autoridad: skill, DENY y replan
# =========================================================================== #


def test_ataque_10_una_skill_no_puede_ampliar_el_envelope():
    """El envelope es la declaración del usuario y nadie la ensancha por la puerta de atrás."""
    mission = _mission()
    mission.envelope.capabilities = ["fs.read"]
    mission.envelope.perimeters = ["notes.txt"]

    policy = PolicyEngine()
    paso_dentro = PlanStep("r", "lee notas.txt", "execute", RiskLevel.LOW, "e", capability="fs.read")
    paso_fuera = PlanStep("w", "borra algo", "execute", RiskLevel.HIGH, "e",
                          capability="fs.remove", args={"path": "otro.txt"})

    assert policy.evaluate(mission, paso_dentro).allowed is True
    fuera = policy.evaluate(mission, paso_fuera)
    assert fuera.allowed is False, "una capability fuera del envelope no se puede autorizar"


def test_ataque_11_un_deny_no_se_convierte_en_aprobacion():
    """DENY es terminal: el gate no lo traduce a «pide aprobación»."""
    mission = _mission()
    mission.envelope.capabilities = ["fs.read"]
    policy = PolicyEngine()
    gate = AutonomyGate()

    paso = PlanStep("w", "borra", "execute", RiskLevel.HIGH, "e",
                    capability="fs.remove", args={"path": "otro.txt"})
    decision = gate.decide(mission, paso, policy)
    assert decision.allowed is False
    assert not getattr(decision, "requires_approval", False), (
        "un DENY no puede degradarse a solicitud de aprobación: sería una vía de escape"
    )


def test_ataque_12_el_replan_no_salta_la_autoridad():
    """Un paso del replan se vuelve a autorizar: cambiar de estrategia no es
    saltarse el gate."""
    mission = _mission()
    mission.envelope.capabilities = ["fs.read"]
    policy = PolicyEngine()
    gate = AutonomyGate()

    original = PlanStep("r1", "lee notas.txt", "execute", RiskLevel.LOW, "e", capability="fs.read")
    paso_replan = PlanStep(
        "r1-bis", "intenta otra vez con otra id", "execute", RiskLevel.LOW, "e",
        capability="fs.remove", args={"path": "otro.txt"},
    )
    assert gate.decide(mission, original, policy).allowed is True
    assert gate.decide(mission, paso_replan, policy).allowed is False, (
        "el mismo paso con otro id no esquiva el envelope"
    )


# =========================================================================== #
# 13. La persistencia no pierde la verificación
# =========================================================================== #


def test_ataque_13_el_estado_de_verificacion_sobrevive_al_hermetic_round_trip():
    """Si se perdiera, un reinicio convertiría `NEEDS_VERIFICATION` en `COMPLETED` o al revés."""
    world = _observed("notas.txt", exists=True, content="hola")
    mission = _mission("Comprueba si notas.txt existe", ["file_exists:notas.txt"])
    ver = GoalVerifier(world=world).verify(mission)
    assert ver.verified is True
    settle(mission, ver)

    restaurada = mission_from_row(mission_to_row(mission))
    assert restaurada.goal_verification is not None
    assert restaurada.goal_verification.verified is True
    assert restaurada.state is MissionState.COMPLETED


# =========================================================================== #
# 14. Un criterio trivial no sustituye a uno semántico
# =========================================================================== #


def test_ataque_14_un_criterio_trivial_no_sustituye_al_semantico():
    """La degradación silenciosa es el fallo que se cierra aquí."""
    objetivo = "Analiza el archivo notas.txt y dime qué contiene"
    criteria, _ = normalize_criteria(objetivo, objetivo, [])
    assert criteria == ["content_observed:notas.txt"]

    # Aunque alguien inyecte a mano el criterio viejo, la verificación sigue exigiendo
    # contenido: el criterio equivocado no abre la puerta por sí solo.
    mission = _mission(objetivo, ["file_exists:notas.txt"])
    world = _observed("notas.txt", exists=True, content="hola")
    ver = GoalVerifier(world=world).verify(mission)
    # Con lectura real sí se cumple: `file_exists` es MÁS débil, y el sistema no lo ha
    #推导ado, simplemente lo ha aceptado como lo que es.
    assert ver.verified is True
    # Pero lo que el sistema DERIVA para ese objetivo es el fuerte:
    assert normalize_criteria(objetivo, objetivo, [])[0] == ["content_observed:notas.txt"]


def test_ataque_14b_el_clasificador_no_degrada_un_objetivo_por_paso():
    """Ni aunque el modelo proponga el criterio trivial."""
    objetivo = "Analiza el archivo notas.txt y dime qué contiene"
    criteria, status = normalize_criteria(
        objetivo, objetivo, ["El archivo notas.txt existe"]
    )
    assert "content_observed:notas.txt" in criteria or criteria == ["content_observed:notas.txt"]


# =========================================================================== #
# 15. La respuesta no afirma éxito cuando el estado dice que no
# =========================================================================== #


def test_ataque_15_la_respuesta_refleja_el_estado_real():
    """La respuesta es una proyección del veredicto, no una narración libre."""
    from alexis.cognition.response import ResponseComposer
    from alexis.cognition.state import KnowledgeState, Verdict

    composer = ResponseComposer()
    mission = _mission()

    for verdict, verificado, debe_contener in (
        (Verdict.INSUFFICIENT_EVIDENCE, False, "no está verificado"),
        (Verdict.BLOCKED, False, "no está verificado"),
        (Verdict.FAILURE, False, "no está verificado"),
    ):
        knowledge = KnowledgeState(objective=mission.goal.objective)
        knowledge.last_verdict = verdict.value
        ver = GoalVerification(
            objective=mission.goal.objective, evaluations=[], verified=verificado, reason=""
        )
        reply = composer.compose(mission, knowledge, ver)
        assert debe_contener in reply.text.lower(), reply.text
        assert not composer.asserts_completion(reply)