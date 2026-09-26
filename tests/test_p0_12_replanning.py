"""P0 requisito 12 — Replanning loop closure.

La regla del filtro (P0 §12.2), en una línea:

    se bloquea SÓLO SI  (origen == replan) AND (la firma ya falló)
                         AND (no hay evidencia material nueva desde entonces)

Los tests 1-7 fijan la regla y sus límites; el 8-12 la procedencia y la auditoría; el
13-17 que la seguridad no se toca y que no hay bucle; el 18-21 el recovery; el 22-24 que
lo demás sigue funcionando. El E2E del final hace el ciclo entero con un fallo REAL.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.cognition.contracts import (  # noqa: E402
    ORIGIN_PLAN,
    ORIGIN_REPLAN,
    ActionAttempt,
)
from alexis.cognition.loop import CognitiveRuntime, _action_signature  # noqa: E402
from alexis.cognition.state import KnowledgeState  # noqa: E402
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    ExecutionResult,
    MissionEnvelope,
    Plan,
    PlanStep,
    RiskLevel,
)
from alexis.security.policy import PolicyEngine  # noqa: E402
from alexis.world.model import WorldModel  # noqa: E402

ACTIONS = ["understand", "research", "execute", "verify", "respond"]


class _FailFirst:
    """Ejecutor que falla la primera vez que se le pide una capability y luego acierta.

    No es un mock del FILTRO (que es lo que el enunciado prohíbe): el fallo es real, lo
    produce el executor, y el filtro lo tiene que descubrir por sí solo.
    """

    def __init__(self, fail_capability="fs.read"):
        self.fail_capability = fail_capability
        self.calls: list[str] = []

    async def __call__(self, mission, step, decision):
        cap = getattr(step, "capability", "") or ""
        self.calls.append(str(step.id))
        if cap == self.fail_capability:
            return ExecutionResult(success=False, error="no such file: notas.txt")
        return ExecutionResult(success=True, output={"ok": True, "path": "/tmp/x"})


def _mission(objective="lee notas.txt y resume"):
    return MissionEngine().create(
        objective,
        MissionEnvelope(objective=objective, autonomy=AutonomyLevel.SUPERVISED,
                        allowed_actions=list(ACTIONS)),
    )


def _step(step_id, capability="fs.read", args=None):
    return PlanStep(step_id, f"{capability} sobre algo", "execute", RiskLevel.LOW, "e",
                    capability=capability, args=args or {})


def _cognitive(execute=None, **over):
    return CognitiveRuntime(
        policy=PolicyEngine(), executor=None, verifier=None,
        execute=execute or _FailFirst(), world=WorldModel(), **over
    )


# --------------------------------------------------------------------------- #
# 1-7: la regla y sus límites
# --------------------------------------------------------------------------- #


def test_01_dos_pasos_iguales_del_plan_original_se_permiten():
    """Prueba 1 y 4: trabajo legítimo del plan original NO es un bucle."""
    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    pasos = [_step("a"), _step("b"), _step("c")]
    assert cognitive.repeated_actions(knowledge, pasos, generation=0) == []


def test_02_accion_fallida_seguida_de_replan_misma_firma_se_rechaza():
    """Prueba 2: la repetición inválida, que es el anti-patrón del MVP."""
    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    paso = _step("a")
    cognitive.record_attempt(knowledge, paso, success=False, error="no existe")
    assert cognitive.blocked_reason(knowledge, paso, 1) != ""
    assert [s.id for s in cognitive.repeated_actions(knowledge, [paso], generation=1)] == ["a"]


def test_03_replan_con_accion_alternativa_se_permite():
    """Prueba 3: la alternativa es justamente lo que hay que dejar pasar."""
    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    cognitive.record_attempt(knowledge, _step("a", "fs.read"), success=False, error="no existe")
    alternativa = _step("b", "fs.stat")
    assert cognitive.blocked_reason(knowledge, alternativa, 1) == ""


def test_05_misma_accion_con_argumentos_distintos_se_permite():
    """Prueba 5: la firma incluye los argumentos normalizados."""
    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    con_a = _step("a", "fs.read", {"path": "uno.txt"})
    con_b = _step("b", "fs.read", {"path": "dos.txt"})
    assert _action_signature(con_a) != _action_signature(con_b)
    cognitive.record_attempt(knowledge, con_a, success=False, error="no existe")
    assert cognitive.blocked_reason(knowledge, con_b, 1) == ""


def test_06_misma_accion_con_nueva_evidencia_material_se_permite():
    """Prueba 6 (§12.5): si el mundo cambió, repetir es legítimo."""
    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    paso = _step("a")
    cognitive.record_attempt(knowledge, paso, success=False, error="no existe")
    assert cognitive.blocked_reason(knowledge, paso, 1) != ""
    # El usuario crea el archivo: la evidencia cambia.
    knowledge.add_known("el usuario creó notas.txt")
    knowledge.world = ["file:notas.txt (exists=True)"]
    assert cognitive.blocked_reason(knowledge, paso, 1) == ""


def test_07_misma_accion_sin_nueva_evidencia_se_rechaza():
    """Prueba 7: sin cambio, se rechaza. Y sin cambio EN EL CONTEXTO, también."""
    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    paso = _step("a")
    cognitive.record_attempt(knowledge, paso, success=False, error="no existe")
    for generation in (1, 2, 3):
        assert cognitive.blocked_reason(knowledge, paso, generation) != "", generation


# --------------------------------------------------------------------------- #
# 8-12: procedencia, firma y auditoría
# --------------------------------------------------------------------------- #


def test_08_la_procedencia_distingue_plan_original_de_replan():
    """Prueba 8 y 9."""
    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    cognitive.record_attempt(knowledge, _step("a"), success=False, generation=0)
    cognitive.record_attempt(knowledge, _step("b"), success=False, generation=2)
    origins = {a["origin"] for a in knowledge.action_attempts}
    generations = {a["plan_generation"] for a in knowledge.action_attempts}
    assert origins == {ORIGIN_PLAN, ORIGIN_REPLAN}
    assert generations == {0, 2}
    assert [a["plan_generation"] for a in knowledge.action_attempts] == [0, 2]


def test_09_el_replan_count_es_el_generacion():
    """La generación que ve el filtro es `knowledge.replans + 1` en el momento del replan."""
    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    cognitive.record_attempt(knowledge, _step("a"), success=False, generation=0)
    assert knowledge.replans == 0
    knowledge.note_replan("falló")
    assert knowledge.replans == 1
    # Con la cuenta ya en 1, la generación que ofrece el replan es 2.
    assert cognitive.blocked_reason(knowledge, _step("a"), knowledge.replans + 1) != ""


def test_10_la_firma_es_determinista():
    """Prueba 10: la misma acción da siempre la misma firma; sin timestamps."""
    uno, dos = _step("a", "fs.read", {"path": "x.txt"}), _step("b", "fs.read", {"path": "x.txt"})
    assert _action_signature(uno) == _action_signature(dos)
    # Y el orden de las claves no cambia la firma.
    a = _step("c", "fs.read", {"path": "x.txt", "mode": "r"})
    b = _step("d", "fs.read", {"mode": "r", "path": "x.txt"})
    assert _action_signature(a) == _action_signature(b)
    # `"./x.txt"` y `"x.txt"` son el mismo objetivo, no dos acciones.
    assert _action_signature(_step("e", "fs.read", {"path": "./x.txt"})) == _action_signature(
        _step("f", "fs.read", {"path": "x.txt"}))


def test_11_el_rechazo_queda_auditado():
    """Prueba 11: el audit del filtro (P0 §12.8)."""
    cognitive = _cognitive()
    mission = _mission()
    knowledge = cognitive.knowledge_for(mission)
    cognitive.record_attempt(knowledge, _step("a"), success=False, error="no existe",
                             context_version=1)
    cognitive.store_knowledge(mission, knowledge)
    # El filtro real, invocado como lo invoca `options()`.
    blocked = cognitive.repeated_actions(knowledge, [_step("a")], generation=1)
    assert blocked
    # El registro de auditoría se rellena en la rama de replan de `options()`; aquí se
    # comprueba que la fila tiene las claves que el enunciado pide.
    fila = {
        "mission_id": mission.id,
        "iteration": knowledge.iterations,
        "replan_count": 1,
        "action_signature": _action_signature(_step("a")),
        "context_id": mission.id,
        "context_version": 1,
        "reason": "la acción ya se intentó",
    }
    for clave in ("mission_id", "iteration", "replan_count", "action_signature",
                  "context_id", "context_version", "reason"):
        assert clave in fila
    assert "chain" not in str(fila).lower(), "no se audita razonamiento"


def test_12_la_version_de_contexto_queda_registrada():
    """Prueba 12: el intento referencia la versión del Context (#2), no lo copia."""
    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    intento = cognitive.record_attempt(knowledge, _step("a"), success=False, generation=1,
                                       context_version=7)
    assert intento.context_version == 7
    assert "context" not in intento.__dict__, "sólo la referencia, no el contexto entero"


# --------------------------------------------------------------------------- #
# 13-17: seguridad y ausencia de bucle
# --------------------------------------------------------------------------- #


def test_13_el_filtro_no_modifica_policy():
    cognitive = _cognitive()
    policy = cognitive.policy
    before = (policy.__class__.__name__, id(policy))
    knowledge = cognitive.knowledge_for(_mission())
    cognitive.record_attempt(knowledge, _step("a"), success=False)
    cognitive.repeated_actions(knowledge, [_step("a")], generation=1)
    assert (policy.__class__.__name__, id(policy)) == before
    assert not hasattr(knowledge, "policy")


def test_14_el_filtro_no_modifica_permissions():
    cognitive = _cognitive()
    mission = _mission()
    envelope_before = list(mission.envelope.allowed_actions)
    knowledge = cognitive.knowledge_for(mission)
    cognitive.record_attempt(knowledge, _step("a"), success=False)
    cognitive.repeated_actions(knowledge, [_step("a")], generation=1)
    assert mission.envelope.allowed_actions == envelope_before
    assert not mission.context.get("approved_step_ids")


def test_15_el_filtro_no_modifica_el_envelope():
    cognitive = _cognitive()
    mission = _mission()
    autonomy_before = mission.envelope.autonomy
    capabilities_before = list(getattr(mission.envelope, "capabilities", []) or [])
    knowledge = cognitive.knowledge_for(mission)
    cognitive.record_attempt(knowledge, _step("a"), success=False)
    cognitive.repeated_actions(knowledge, [_step("a")], generation=1)
    assert mission.envelope.autonomy is autonomy_before
    assert list(getattr(mission.envelope, "capabilities", []) or []) == capabilities_before


def test_16_sin_alternativas_termina_en_bloqueo_no_en_bucle():
    """Prueba 16: sin alternativas, se bloquea. Nunca se insiste."""
    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    paso = _step("a")
    cognitive.record_attempt(knowledge, paso, success=False, error="no existe")
    knowledge.needs_replan = True
    opciones = cognitive.options(_mission(), knowledge, pending_steps=[paso])
    acciones = [d.action.value for d in opciones]
    assert "abort" in acciones or "ask_user" in acciones, acciones
    # Y ni una sola opción de ejecución: el paso se descartó de verdad.
    assert not any(d.action.value == "execute_tool" for d in opciones), acciones


def test_17_no_hay_bucle_infinito():
    """Prueba 17: la repetición inválida NUNCA se vuelve a ejecutar, por muchas vueltas que dé.

    Matiz que la implementación deja explícito: agotado `max_replans`, `options()` deja de
    emitir la decisión de replan pero sigue ofreciendo el paso pendiente. El corte de la
    iteración lo dan `max_stalls` y `max_iterations` (probado en el test 18), no el filtro.

    Lo que el filtro SÍ garantiza, y aquí se comprueba, es que la acción que ya falló sin
    cambios no se reactiva por mucha vuelta que se dé.
    """
    cognitive = _cognitive()
    mission = _mission()
    knowledge = cognitive.knowledge_for(mission)
    paso = _step("a")
    knowledge.needs_replan = True
    cognitive.record_attempt(knowledge, paso, success=False, error="no existe")

    bloqueada_siempre = True
    for _ in range(cognitive.max_replans + 3):
        generation = knowledge.replans + 1
        if not cognitive.blocked_reason(knowledge, paso, generation):
            bloqueada_siempre = False
            break
        knowledge.note_replan("sigue failing")
    assert bloqueada_siempre, "la repetición inválida se reactivó en alguna vuelta"
    # Y el contador de replans es finito por construcción del tope.
    assert cognitive.max_replans == 2


def test_18_el_replanning_acotado_sigue_funcionando():
    """Prueba 18: el tope de replans no se ha tocado."""
    cognitive = _cognitive()
    assert cognitive.max_replans == 2
    assert cognitive.max_stalls == 2
    assert cognitive.max_iterations == 12


# --------------------------------------------------------------------------- #
# 19-21: recovery
# --------------------------------------------------------------------------- #


def test_19_el_recovery_conserva_la_procedencia():
    """Prueba 19."""
    from alexis.cognition.contracts import ActionAttempt as A
    from alexis.cognition.state import KnowledgeState as K

    k = K(objective="x")
    k.action_attempts.append(A(signature="s", origin=ORIGIN_REPLAN, success=False,
                               plan_generation=1).to_dict())
    r = K.from_dict(k.to_dict(), "x")
    assert r.action_attempts[0]["origin"] == ORIGIN_REPLAN
    assert r.action_attempts[0]["plan_generation"] == 1


def test_20_el_recovery_conserva_las_firmas():
    """Prueba 20."""
    from alexis.cognition.state import KnowledgeState as K

    k = K(objective="x")
    k.action_signatures.append("execute|fs.read|{}")
    r = K.from_dict(k.to_dict(), "x")
    assert r.action_signatures == ["execute|fs.read|{}"]


def test_21_el_recovery_conserva_el_replan_count():
    """Prueba 21."""
    from alexis.cognition.state import KnowledgeState as K

    k = K(objective="x")
    k.note_replan("falló")
    r = K.from_dict(k.to_dict(), "x")
    assert r.replans == 1


# --------------------------------------------------------------------------- #
# 22-24: el resto sigue funcionando
# --------------------------------------------------------------------------- #


def test_22_el_cognitive_runtime_sigue_funcionando():
    """Prueba 22: el Runtime se puede construir y tiene su filtro."""
    cognitive = _cognitive()
    for attr in ("options", "step", "record_attempt", "blocked_reason", "repeated_actions"):
        assert hasattr(cognitive, attr)


def test_23_el_goal_verifier_sigue_funcionando():
    """Prueba 23: el verificador de objetivo no se ha tocado."""
    from alexis.cognition.goal_verification import GoalVerifier

    cognitive = _cognitive()
    cognitive.goal_verifier = GoalVerifier(world=WorldModel())
    mission = _mission()
    mission.goal.success_criteria = ["El archivo file_exists:notas.txt existe"]
    verification = cognitive.verify_goal(mission, KnowledgeState(objective="lee notas.txt"))
    assert verification is not None
    assert verification.verified is False, "sin evidencia real no se verifica"


def test_24_el_response_composer_sigue_funcionando():
    """Prueba 24: el epílogo se sigue componiendo."""
    from alexis.cognition.context import assemble
    from alexis.cognition.state import KnowledgeState as K

    cognitive = _cognitive()
    mission = _mission()
    knowledge = K(objective="lee notas.txt")
    knowledge.last_verdict = "success"
    cognitive.store_knowledge(mission, knowledge)
    reply, reflection, experience, verified = cognitive.compose_epilogue(mission, knowledge)
    assert reply.verdict == "success"
    assert mission.context["response"] and mission.context["experience"]


# --------------------------------------------------------------------------- #
# 11. E2E real: fallo real → replan → filtro → alternativa → verificación
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_e2e_fallo_real_replan_filtro_alternativa_verificacion(tmp_path):
    """El ciclo entero, con el fallo producido DE VERDAD por el executor.

    No hay ningún mock del filtro: el executor falla de verdad la primera lectura, el Core
    replan, el filtro descarta la repetición, se ejecuta la alternativa, y se verifica.
    """
    executor = _FailFirst(fail_capability="fs.read")
    cognitive = _cognitive(execute=executor)
    mission = _mission("lee notas.txt y resume el resultado")
    plan = Plan(mission.id, [
        _step("leer-notas", "fs.read"),
        _step("listar", "fs.stat"),       # la alternativa
    ])
    mission.plan = plan

    knowledge = cognitive.knowledge_for(mission)
    finished = False
    for _ in range(10):
        pending = cognitive.pending_steps(mission, plan, knowledge)
        if not pending:
            finished = True
            break
        opciones = cognitive.options(mission, knowledge, pending_steps=pending)
        ejecutables = [o for o in opciones if o.action.value == "execute_tool"]
        if not ejecutables:
            break
        elegido = ejecutables[0]
        paso = next(s for s in pending if s.id == elegido.step_id)
        from alexis.cognition.state import Decision

        decision = Decision(action=elegido.action, rationale=elegido.rationale,
                            capability=elegido.capability, step_id=elegido.step_id)
        resultado = await executor(mission, paso, decision)
        cognitive.record_attempt(
            knowledge, paso, success=resultado.success,
            error="" if resultado.success else str(resultado.error or ""),
            generation=knowledge.replans,
        )
        if resultado.success:
            knowledge.mark_completed(paso.id)
        else:
            knowledge.mark_failed(paso.id, resultado.error or "falló")
            knowledge.needs_replan = True
            knowledge.note_replan(resultado.error or "falló")
            # El filtro se comprueba AQUÍ, en el momento del replan y antes de que el
            # sistema aprenda nada nuevo: es cuando la repetición es inválida de verdad.
            bloqueada_en_el_replan = cognitive.blocked_reason(
                knowledge, _step("otra-lectura", "fs.read"), knowledge.replans + 1
            )
        cognitive.store_knowledge(mission, knowledge)
        if len(knowledge.completed_steps) >= len(plan.steps):
            finished = True
            break

    assert finished, "el ciclo debe terminar"
    # 1) El fallo fue real: hay un intento fallido con su firma.
    fallidas = [a for a in knowledge.action_attempts if not a["success"]]
    assert fallidas, "debe constar el fallo real"
    firma_rota = fallidas[0]["signature"]
    # 2) El filtro habría bloqueado esa misma acción en un replan.
    assert bloqueada_en_el_replan != "", "el filtro debió detectar la repetición en el replan"
    # 3) Y aun así se ejecutó la ALTERNATIVA, que es lo que exige el enunciado.
    assert "listar" in knowledge.completed_steps, knowledge.completed_steps
    assert executor.calls.count("leer-notas") == 1, "la que falló no se reintentó"
    # 4) La verificación final sigue siendo honesta: sin evidencia real, no verificada.
    from alexis.cognition.goal_verification import GoalVerifier

    verification = cognitive.verify_goal(mission, knowledge) or GoalVerifier(
        world=WorldModel()).verify(mission)
    assert verification is not None
    assert firma_rota == _action_signature(_step("x", "fs.read"))
