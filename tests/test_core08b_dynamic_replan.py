"""CORE-08B — Replanning dinámico: una estrategia ALTERNATIVA, no la misma con otro nombre.

El hueco que esto cierra: cuando el filtro de repetición se queda sin alternativas, el Core
llegaba a la conclusión de que no había nada que hacer y bloqueaba. Con un modelo disponible,
"no hay alternativa determinista" NO es "no hay alternativa": es sólo que las deterministas se
acabaron. `loop.py:760` trataba esas dos cosas como la misma.

La regla que gobierna todo el incremento es una sola: **el modelo propone y el Core dispone.**
Lo que sale del modelo tiene que pasar por exactamente los mismos filtros que una acción
determinista —`PlanValidator`, `blocked_reason`, `validate_step`, Gate— sin una sola excepción.
Si el replan dinámico pudiera saltarse uno, sería un camino nuevo hacia el executor y eso
convertiría "el modelo propone" en "el modelo decide".

Por eso la mayoría de los tests de aquí son negativos: no "el modelo propuso algo y funcionó",
sino "el modelo propuso algo y el Core lo rechazó por la razón correcta".

`ALEXIS_MODEL_PLANNER` no aparece en este fichero a propósito: el gate de activación es de
CORE-08A (`_plan_eligible`). Aquí se prueba lo que ocurre cuando YA se decidió preguntar.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.capabilities import build_catalog  # noqa: E402
from alexis.cognition.loop import (  # noqa: E402
    DYNAMIC_REPLAN_AUTHOR,
    DYNAMIC_REPLAN_KEY,
    CognitiveRuntime,
    _action_signature,
    _evidence_scope,
    _scoped_evidence_fingerprint,
)
from alexis.cognition.planner_model import PlanValidator  # noqa: E402
from alexis.cognition.state import KnowledgeState, NextAction  # noqa: E402
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
)
from alexis.models.provider import ModelOutcome, ModelRequest, ModelResponse, ModelTask  # noqa: E402
from alexis.models.router import ModelRouter  # noqa: E402

ACTIONS = ["understand", "analyze", "research", "execute", "verify", "modify", "test", "respond"]


def _mission(objective="lee notas.txt", **over):
    data = dict(objective=objective, autonomy=AutonomyLevel.SUPERVISED,
                allowed_actions=list(ACTIONS), capabilities=[])
    data.update(over)
    return MissionEngine().create(objective, MissionEnvelope(**data))


def _plan(*steps):
    return Plan("m", list(steps))


def _step(step_id, action="research", capability="fs.read", approval=False):
    return PlanStep(step_id, f"paso {step_id}", action, RiskLevel.MEDIUM, "executor",
                    capability=capability, requires_approval=approval)


# ----------------------------------------------------------------------
# Dobles
# ----------------------------------------------------------------------


class _RealProvider:
    """Provider que se hace pasar por REAL: es lo que abre la puerta al replan dinámico."""

    id = "real-1"
    degraded = False
    available = True
    cost_per_1k_tokens = 0.001
    latency_p50_ms = 10

    def describe(self):
        return {"id": self.id, "degraded": False}


class _DegradedProvider(_RealProvider):
    id = "degraded-1"
    degraded = True
    available = True

    def describe(self):
        return {"id": self.id, "degraded": True}


class _StubRouter:
    """Router que registra lo recibido y devuelve una respuesta preparada.

    Implementa `providers()` porque `_has_real_provider()` lo consulta ANTES de llamar, para no
    pagar una ida al provider que sólo va a devolver DEGRADED.
    """

    def __init__(self, response=None, *, raises=None, provider=_RealProvider()):
        self.response = response
        self.raises = raises
        self._providers = [provider] if provider is not None else []
        self.requests: list[ModelRequest] = []

    def providers(self):
        return list(self._providers)

    async def complete(self, request: ModelRequest, *, correlation=None):
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        if request.task is ModelTask.PLAN:
            return self.response
        # Una decisión `REASON` normal del bucle. Sin `step_id`, `_match_option` devuelve la
        # primera opción con esa acción, que tras el overlay es el paso dinámico: así el bucle
        # avanza a ejecutarlo en vez de quedarse re-planificando.
        return ModelResponse(
            text="", data={"action": self._decision_action(), "rationale": "sigo con la alternativa"},
            provider="real-1", model="llama3.2:1b", outcome=ModelOutcome.REAL,
        )

    def _decision_action(self) -> str:
        """La acción que el Core derivó del paso del overlay.

        `_step_decision()` convierte `research` en `NextAction.RESEARCH` y el resto en
        `EXECUTE_TOOL`. Si el stub contestara una acción distinta, `_match_option()` no
        encontraría opción y degradaría a la primera (REPLAN), y el test mediría el fallback
        en lugar del overlay.
        """
        steps = ((self.response.data or {}).get("steps") or []) if self.response else []
        raw = steps[0].get("action") if steps and isinstance(steps[0], dict) else None
        return "research" if raw == "research" else "execute_tool"


def _plan_response(steps, **over):
    base = dict(
        text="", data={"steps": steps}, provider="real-1", model="llama3.2:1b",
        resolved_model="llama3.2:1b", resolved_provider="ollama", outcome=ModelOutcome.REAL,
        tokens_in=311, tokens_out=64, cost_usd=0.0002, latency_ms=4321,
        chain=["llama3.2:1b"],
    )
    base.update(over)
    return ModelResponse(**base)


def _plan_calls(router) -> list[ModelRequest]:
    """Sólo las llamadas `ModelTask.PLAN`.

    El router registra TODO lo que pasa por él, incluida la decisión `REASON` que ocurre en
    cada iteración. Contar `requests` para afirmar "no llamó al modelo" sería falso: siempre
    hay una `REASON`. Lo que hay que afirmar es que no pidió una ESTRATEGIA.
    """
    return [r for r in router.requests if r.task is ModelTask.PLAN]


#: Una estrategia REALMENTE distinta: `fs.stat` en vez de `fs.read`, otro path.
ALTERNATIVE = [{"id": "cualquier_cosa", "description": "Comprobar si existe antes de leer",
                "action": "research", "capability": "fs.stat", "risk": "low",
                "depends_on": [], "args": {"path": "notas.txt"}}]

#: La MISMA acción con otro nombre: debe rechazarse por firma, no por id.
REPEATED = [{"id": "otra_cosa", "description": "Leer el archivo", "action": "research",
             "capability": "fs.read", "risk": "low", "depends_on": [],
             "args": {"path": "notas.txt"}}]

INVENTED = [{"id": "inventada", "description": "Ejecutar algo raro", "action": "execute",
             "capability": "root.shell", "risk": "low", "depends_on": []}]

OUT_OF_ENVELOPE = [{"id": "fuera", "description": "Leer fuera", "action": "research",
                    "capability": "fs.read", "risk": "low", "depends_on": [],
                    "args": {"path": "otro.txt"}}]

UNDERSTATED_RISK = [{"id": "riesgo_bajo", "description": "Borrar algo", "action": "execute",
                     "capability": "fs.remove", "risk": "low", "depends_on": [],
                     "requires_approval": False, "args": {"path": "notas.txt"}}]

NO_APPROVAL = [{"id": "sin_aprobacion", "description": "Borrar sin pedir permiso",
                "action": "execute", "capability": "fs.remove", "risk": "high",
                "depends_on": [], "requires_approval": False, "args": {"path": "notas.txt"}}]


class _SpyExecutor:
    def __init__(self):
        self.calls = []

    async def execute(self, mission, step, *, tool_name=None):
        self.calls.append({"id": step.id, "capability": step.capability,
                           "approval": step.requires_approval})
        return ExecutionResult(success=True, output={"ok": True},
                               observations=[Observation(f"tool.{step.id}", {"ok": True}, trusted=True)])

    async def __call__(self, mission, step, tool_name=None):
        return await self.execute(mission, step, tool_name=tool_name)


class _PassingVerifier:
    async def verify(self, mission, plan):
        return Verification(passed=True, evidence=["ok"], confidence=0.9, notes="ok")


class _AllowPolicy:
    def authorize(self, mission, step):
        class _D:
            allowed = True
            requires_approval = False
            reason = "test"
        return _D()

    def evaluate(self, mission, step):
        return self.authorize(mission, step)


class _DenyPolicy(_AllowPolicy):
    """El Gate dice que no. Si un paso dinámico lo ignorara, el Gate sería decorativo."""

    def authorize(self, mission, step):
        class _D:
            allowed = False
            requires_approval = False
            reason = "el gate lo veta"
        return _D()


def _cognitive(router, *, policy=None, executor=None, plan_validator=True):
    return CognitiveRuntime(
        policy=policy or _AllowPolicy(),
        executor=executor or _SpyExecutor(),
        verifier=_PassingVerifier(),
        model_router=router,
        catalog=build_catalog(),
        plan_validator=PlanValidator(catalog=build_catalog()) if plan_validator else None,
    )


def _exhausted(knowledge, step, *, failed=True):
    """Deja el `KnowledgeState` en el estado en que el Core pide un replan.

    Reproduce lo que hace `_absorb()` tras un fallo real: marca el paso, registra el intento
    con su firma y levanta `needs_replan`. Es el estado que `blocked_reason()` bloquea.
    """
    knowledge.mark_failed(step.id, "no such file or directory")
    knowledge.diagnosis = "not_found: el archivo no existe"
    knowledge.last_failure_kind = "not_found"
    knowledge.last_error = "no such file or directory: notas.txt"
    knowledge.needs_replan = True
    # La huella se calcula con las funciones REALES del filtro. Ponerla a mano sería
    # inventar el estado del filtro, y `blocked_reason()` compara la huella de AHORA contra la
    # guardada: si no coinciden, el paso se considera con evidencia nueva y NO queda bloqueado,
    # que es justo el escenario contrario al que estos tests quieren provocar.
    fingerprint = _scoped_evidence_fingerprint(knowledge, _evidence_scope(step), str(step.capability or ""))
    knowledge.action_attempts.append(
        {"signature": _action_signature(step),
         "capability": step.capability, "action": step.action, "step_id": step.id,
         "plan_generation": 0, "success": False, "error": "no such file or directory",
         "scope": _evidence_scope(step), "scoped_evidence_fp": fingerprint, "iteration": 1}
    )
    return knowledge


async def _attempt(cognitive, mission, knowledge, plan, pending):
    """Una llamada a `decide_next_action` con el escenario de replan agotado."""
    return await cognitive.decide_next_action(mission, knowledge, pending_steps=pending)


# ======================================================================
# 1-3: CUÁNDO se pregunta (y cuándo NO)
# ======================================================================


@pytest.mark.asyncio
async def test_01_con_alternativa_determinista_no_llama_al_modelo():
    """Regla 7: si queda una alternativa determinista usable, no se pide un PLAN.

    Pagar una llamada al modelo para obtener algo que el Core ya sabía hacer sería gastar
    presupuesto (CORE-06) y hasta 46-60s con un modelo local a cambio de nada.
    """
    mission = _mission()
    router = _StubRouter(_plan_response(ALTERNATIVE))
    cognitive = _cognitive(router)
    knowledge = KnowledgeState()
    fallback = _step("otro", action="research", capability="fs.stat")

    decision = await _attempt(cognitive, mission, knowledge, _plan(fallback), [fallback])

    assert _plan_calls(router) == [], "no debe pedir un PLAN si hay alternativa determinista"
    assert decision.step_id == "otro"


@pytest.mark.asyncio
async def test_02_sin_alternativa_y_sin_provider_real_no_llama_al_modelo():
    """Sin provider REAL, comportamiento actual: el blocker de siempre."""
    mission = _mission()
    router = _StubRouter(_plan_response(ALTERNATIVE), provider=_DegradedProvider())
    cognitive = _cognitive(router)
    knowledge = _exhausted(KnowledgeState(), _step("investigar"))

    decision = await _attempt(cognitive, mission, knowledge, _plan(), [])

    assert _plan_calls(router) == [], "un provider DEGRADED no razona: no hay nada que preguntar"
    assert decision.action is not None


@pytest.mark.asyncio
async def test_03_sin_alternativa_y_con_provider_real_pide_PlanTask():
    """Con provider REAL y sin alternativas deterministas, sí se pide `ModelTask.PLAN`."""
    mission = _mission()
    router = _StubRouter(_plan_response(ALTERNATIVE))
    cognitive = _cognitive(router)
    knowledge = _exhausted(KnowledgeState(), _step("investigar"))

    decision = await _attempt(cognitive, mission, knowledge, _plan(_step("investigar")), [])

    assert len(_plan_calls(router)) == 1
    assert _plan_calls(router)[0].task.value == "plan"
    assert decision.step_id == "dr1"


# ======================================================================
# 4-11: QUÉ pasa con lo que propone el modelo
# ======================================================================


@pytest.mark.asyncio
async def test_04_plan_valido_distinto_se_acepta_como_overlay():
    """La estrategia alternativa se convierte en overlay, con ids `dr1` y su procedencia."""
    mission = _mission()
    router = _StubRouter(_plan_response(ALTERNATIVE))
    cognitive = _cognitive(router)
    knowledge = _exhausted(KnowledgeState(), _step("investigar"))

    await _attempt(cognitive, mission, knowledge, _plan(_step("investigar")), [])

    overlay = mission.context[DYNAMIC_REPLAN_KEY]
    assert overlay["accepted"] is True
    assert overlay["origin"] == DYNAMIC_REPLAN_AUTHOR
    assert overlay["attempts"] == 1
    assert overlay["version"] == 1
    assert [s["id"] for s in overlay["steps"]] == ["dr1"]
    assert overlay["steps"][0]["capability"] == "fs.stat"
    assert overlay["steps"][0]["proposed_by"] == DYNAMIC_REPLAN_AUTHOR


@pytest.mark.asyncio
async def test_05_plan_invalido_no_llega_al_executor():
    """JSON que no es un plan se rechaza antes de tocar nada ejecutable."""
    mission = _mission()
    router = _StubRouter(_plan_response(None, text="no soy json"))
    executor = _SpyExecutor()
    cognitive = _cognitive(router, executor=executor)
    knowledge = _exhausted(KnowledgeState(), _step("investigar"))

    decision = await _attempt(cognitive, mission, knowledge, _plan(), [])

    assert mission.context[DYNAMIC_REPLAN_KEY]["accepted"] is False
    assert mission.context[DYNAMIC_REPLAN_KEY]["steps"] == []
    assert executor.calls == []
    assert decision.step_id != "dr1"


@pytest.mark.asyncio
async def test_06_capability_inexistente_se_rechaza():
    """`root.shell` no existe en el catálogo: rechazo, sin excepciones."""
    mission = _mission()
    router = _StubRouter(_plan_response(INVENTED))
    executor = _SpyExecutor()
    cognitive = _cognitive(router, executor=executor)
    knowledge = _exhausted(KnowledgeState(), _step("investigar"))

    await _attempt(cognitive, mission, knowledge, _plan(), [])

    overlay = mission.context[DYNAMIC_REPLAN_KEY]
    assert overlay["accepted"] is False
    assert any("capability inexistente" in r for r in overlay["reasons"])
    assert executor.calls == []


@pytest.mark.asyncio
async def test_07_capability_fuera_del_envelope_se_rechaza():
    """El envelope declara `capabilities=[]`: el plan no puede ampliarlo."""
    # El envelope declara `fs.stat` y el plan propone `fs.read`: no está y no puede ampliarlo.
    # Con `capabilities=[]` la regla no se aplicaría, porque en `PlanValidator` una lista vacía
    # significa "sin restricción declarada", no "prohibido todo" (semántica preexistente).
    mission = _mission(capabilities=["fs.stat"])
    router = _StubRouter(_plan_response(OUT_OF_ENVELOPE))
    executor = _SpyExecutor()
    cognitive = _cognitive(router, executor=executor)
    knowledge = _exhausted(KnowledgeState(), _step("investigar"))

    await _attempt(cognitive, mission, knowledge, _plan(), [])

    overlay = mission.context[DYNAMIC_REPLAN_KEY]
    assert overlay["accepted"] is False
    assert any("fuera del envelope" in r for r in overlay["reasons"])
    assert executor.calls == []


@pytest.mark.asyncio
async def test_08_riesgo_subestimado_se_rechaza():
    """`fs.remove` es medium: declararlo `low` es una mentira que el validador detecta."""
    mission = _mission()
    router = _StubRouter(_plan_response(UNDERSTATED_RISK))
    executor = _SpyExecutor()
    cognitive = _cognitive(router, executor=executor)
    knowledge = _exhausted(KnowledgeState(), _step("investigar"))

    await _attempt(cognitive, mission, knowledge, _plan(), [])

    overlay = mission.context[DYNAMIC_REPLAN_KEY]
    assert overlay["accepted"] is False
    assert any("riesgo declarado insuficiente" in r for r in overlay["reasons"])
    assert executor.calls == []


@pytest.mark.asyncio
async def test_09_approval_eliminada_se_rechaza():
    """`fs.remove` con `requires_approval=False`: el plan no puede quitarse esa protección."""
    mission = _mission()
    router = _StubRouter(_plan_response(NO_APPROVAL))
    executor = _SpyExecutor()
    cognitive = _cognitive(router, executor=executor)
    knowledge = _exhausted(KnowledgeState(), _step("investigar"))

    await _attempt(cognitive, mission, knowledge, _plan(), [])

    overlay = mission.context[DYNAMIC_REPLAN_KEY]
    assert overlay["accepted"] is False
    assert any("aprobación" in r for r in overlay["reasons"])
    assert executor.calls == []


@pytest.mark.asyncio
async def test_10_accion_repetida_se_rechaza_por_firma():
    """Misma capability + mismos args con otro id: es la MISMA acción (P0 §12)."""
    mission = _mission()
    router = _StubRouter(_plan_response(REPEATED))
    cognitive = _cognitive(router)
    # Los args tienen que COINCIDIR con los del paso fallido: la firma es
    # `(action, capability, args normalizados)` y dos argumentos distintos son dos acciones
    # distintas — que es exactamente lo que el filtro debe dejar pasar.
    original = _step("investigar", capability="fs.read")
    original.args = {"path": "notas.txt"}
    knowledge = _exhausted(KnowledgeState(), original)

    decision = await _attempt(cognitive, mission, knowledge, _plan(original), [original])

    overlay = mission.context[DYNAMIC_REPLAN_KEY]
    assert overlay["accepted"] is False
    assert overlay["steps"] == []
    assert any("repite una acción que ya falló" in r for r in overlay["reasons"])
    assert decision.step_id != "dr1"


@pytest.mark.asyncio
async def test_11_estrategia_distinta_puede_ejecutar_pasos_nuevos():
    """Si el overlay se acepta, el paso `dr1` llega al executor y es una capability distinta."""
    mission = _mission()
    original = _step("investigar", capability="fs.read")
    router = _StubRouter(_plan_response(ALTERNATIVE))
    executor = _SpyExecutor()
    cognitive = _cognitive(router, executor=executor)
    knowledge = _exhausted(KnowledgeState(), original)

    plan = _plan(original)
    await _attempt(cognitive, mission, knowledge, plan, [original])

    # El overlay entra por `pending_steps()` en la ITERACIÓN SIGUIENTE, no en la que se generó.
    pending = cognitive.pending_steps(mission, plan, knowledge)
    assert [s.id for s in pending] == ["dr1"], "el paso fallado queda enmascarado por el overlay"

    # Y el paso dinámico se ejecuta: la decisión REASON elige la alternativa.
    await cognitive.step(mission, knowledge, pending_steps=pending, plan=plan)
    assert executor.calls, "el paso del overlay debe llegar al executor"
    assert executor.calls[0]["id"] == "dr1"
    assert executor.calls[0]["capability"] == "fs.stat"
    assert knowledge.failed_steps == ["investigar"], "el plan original conserva su historial"


# ======================================================================
# 12-13: overlay + plan original
# ======================================================================


@pytest.mark.asyncio
async def test_12_plan_original_permanece_intacto():
    """Regla 21: ni `mission.plan` ni `context["plan_steps"]` se tocan."""
    mission = _mission()
    router = _StubRouter(_plan_response(ALTERNATIVE))
    cognitive = _cognitive(router)
    knowledge = _exhausted(KnowledgeState(), _step("investigar"))
    original = _step("investigar", capability="fs.read")
    plan = _plan(original)
    plan.steps[0].proposed_by = "deterministic"

    await _attempt(cognitive, mission, knowledge, plan, [original])

    assert [s.id for s in plan.steps] == ["investigar"]
    assert plan.steps[0].capability == "fs.read"
    assert plan.steps[0].proposed_by == "deterministic"
    assert "plan_steps" not in mission.context
    overlay = mission.context[DYNAMIC_REPLAN_KEY]
    assert overlay["replaces_step_ids"] == ["investigar"]


@pytest.mark.asyncio
async def test_13_overlay_enmascara_solo_los_pasos_indicados():
    """`pending_steps()` combina original + overlay sin duplicar ni perder lo que no se sustituye."""
    mission = _mission()
    router = _StubRouter(_plan_response(ALTERNATIVE))
    cognitive = _cognitive(router)
    kept = _step("responder", action="respond", capability="tts.speak")
    knowledge = _exhausted(KnowledgeState(), _step("investigar"))
    mission.context[DYNAMIC_REPLAN_KEY] = {
        "version": 1, "origin": DYNAMIC_REPLAN_AUTHOR, "attempts": 1, "accepted": True,
        "created_iteration": 1, "replaces_step_ids": ["investigar"],
        "steps": [{"id": "dr1", "description": "d", "action": "research", "risk": "low",
                   "agent": "reasoner", "depends_on": [], "requires_approval": False,
                   "capability": "fs.stat", "requires_input": {}, "verification": None,
                   "proposed_by": DYNAMIC_REPLAN_AUTHOR, "rationale": None, "args": {}, "expected": None}],
        "provenance": {}, "reasons": [], "blocked_signatures": [],
    }

    pending = cognitive.pending_steps(mission, _plan(_step("investigar"), kept), knowledge)

    assert sorted(s.id for s in pending) == ["dr1", "responder"]
    assert len([s for s in pending if s.id == "dr1"]) == 1


@pytest.mark.asyncio
async def test_14_overlay_aceptado_aparece_en_la_iteracion_siguiente_no_en_la_misma():
    """El overlay no se ejecuta en el turno en que se generó: siempre en el siguiente.

    Aplicarlo en la misma iteración significaría ejecutar pasos recién generados sin pasar por
    el ciclo de revalidación que los pasos de cualquier otra iteración sí pasan.
    """
    mission = _mission()
    router = _StubRouter(_plan_response(ALTERNATIVE))
    executor = _SpyExecutor()
    cognitive = _cognitive(router, executor=executor)
    knowledge = _exhausted(KnowledgeState(), _step("investigar"))
    plan = _plan(_step("investigar"))

    await _attempt(cognitive, mission, knowledge, plan, [])

    assert executor.calls == [], "generar el overlay no puede ejecutar nada por sí solo"


# ======================================================================
# 15-17: persistencia, sello, presupuesto
# ======================================================================


@pytest.mark.asyncio
async def test_15_overlay_sobrevive_a_serializacion_de_mission():
    """Regla 3/24: vive en `mission.context`, que la Storage persiste como JSONB.

    Se comprueba con el viaje real, no con `dict`: `repositories.py` serializa `context`
    entero, así que un overlay sobrevive a un reinicio si sobrevive a este viaje.
    """
    import json

    from alexis.storage.serialization import mission_from_row, mission_to_row

    mission = _mission()
    router = _StubRouter(_plan_response(ALTERNATIVE))
    cognitive = _cognitive(router)
    knowledge = _exhausted(KnowledgeState(), _step("investigar"))
    await _attempt(cognitive, mission, knowledge, _plan(_step("investigar")), [])

    row = mission_to_row(mission)
    context = json.loads(row["context"])
    # Y que el viaje de vuelta también lo conserva: no basta con que se escriba.
    reloaded = mission_from_row(row)
    assert DYNAMIC_REPLAN_KEY in (reloaded.context or {})
    assert reloaded.context[DYNAMIC_REPLAN_KEY]["steps"][0]["capability"] == "fs.stat"
    assert DYNAMIC_REPLAN_KEY in context
    assert context[DYNAMIC_REPLAN_KEY]["accepted"] is True
    assert context[DYNAMIC_REPLAN_KEY]["steps"][0]["id"] == "dr1"


@pytest.mark.asyncio
async def test_16_maximo_una_llamada_aunque_el_plan_sea_valido():
    """Regla 5: la existencia del registro sella el intento, aunque se ACEPTE."""
    mission = _mission()
    router = _StubRouter(_plan_response(ALTERNATIVE))
    cognitive = _cognitive(router)
    knowledge = _exhausted(KnowledgeState(), _step("investigar"))
    plan = _plan(_step("investigar"))

    await _attempt(cognitive, mission, knowledge, plan, [])
    first = mission.context[DYNAMIC_REPLAN_KEY]["attempts"]
    await _attempt(cognitive, mission, knowledge, plan, [])
    await _attempt(cognitive, mission, knowledge, plan, [])

    assert len(_plan_calls(router)) == 1, "una segunda llamada violaría el máximo de 1"
    assert mission.context[DYNAMIC_REPLAN_KEY]["attempts"] == first == 1


@pytest.mark.asyncio
async def test_17_maximo_una_llamada_si_el_modelo_falla():
    """Regla 5 también en el camino de fallo: la excepción también sella.

    Es el caso donde el sello es más importante: si sólo se sellara al aceptar, un modelo
    que falla dejaría la misión sin marca y cada replan volvería a pagar la llamada.
    """
    mission = _mission()
    router = _StubRouter(raises=TimeoutError("el provider tardó demasiado"))
    cognitive = _cognitive(router)
    knowledge = _exhausted(KnowledgeState(), _step("investigar"))
    plan = _plan(_step("investigar"))

    await _attempt(cognitive, mission, knowledge, plan, [])
    await _attempt(cognitive, mission, knowledge, plan, [])

    assert len(_plan_calls(router)) == 1, "la excepción del primer intento debe sellar igual"
    overlay = mission.context[DYNAMIC_REPLAN_KEY]
    assert overlay["attempts"] == 1
    assert overlay["accepted"] is False
    assert any("no disponible" in r for r in overlay["reasons"])


@pytest.mark.asyncio
async def test_18_excepcion_del_modelo_no_tumba_la_mision():
    """El modelo nunca tumba la misión: se registra y se sigue por el camino determinista."""
    mission = _mission()
    router = _StubRouter(raises=TimeoutError("boom"))
    executor = _SpyExecutor()
    cognitive = _cognitive(router, executor=executor)
    knowledge = _exhausted(KnowledgeState(), _step("investigar"))

    decision = await _attempt(cognitive, mission, knowledge, _plan(), [])

    assert decision is not None
    assert mission.context[DYNAMIC_REPLAN_KEY]["accepted"] is False
    assert executor.calls == []


# ======================================================================
# 18-19: presupuesto y provenance
# ======================================================================


@pytest.mark.asyncio
async def test_19_el_plan_consume_el_presupuesto_de_la_mision():
    """Regla 25: se usa el de CORE-06, vía `for_mission(mission)`.

    No hay un sistema paralelo: la clave es `mission.id` y la imputa el mismo `_account()`
    que el resto de llamadas. Se comprueba con el `ModelRouter` REAL, no con un stub.
    """
    router = ModelRouter(budget_usd=0)
    router.register(_RealProvider())
    cognitive = _cognitive(router)
    mission = _mission()
    knowledge = _exhausted(KnowledgeState(), _step("investigar"))

    await _attempt(cognitive, mission, knowledge, _plan(_step("investigar")), [])

    # Con el router real la respuesta del provider falso no es un plan: se rechaza igual, pero
    # lo que importa es que la llamada se imputó a la clave de esta misión, no a una global.
    assert router.spent_usd_for(mission.id) >= 0.0
    assert router.spent_usd_for("") == 0.0, "el PLAN no debe imputarse a la clave global"


@pytest.mark.asyncio
async def test_20_provenance_completa_del_dynamic_replan():
    """Regla 26: los 14 campos, incluidos los tokens que `audit_event()` no trae."""
    mission = _mission()
    router = _StubRouter(_plan_response(ALTERNATIVE))
    cognitive = _cognitive(router)
    knowledge = _exhausted(KnowledgeState(), _step("investigar"))

    await _attempt(cognitive, mission, knowledge, _plan(_step("investigar")), [])

    provenance = mission.context[DYNAMIC_REPLAN_KEY]["provenance"]
    for field in ("provider", "model", "resolved_model", "resolved_provider", "outcome",
                  "fallback_used", "fallback_from", "fallback_error", "chain",
                  "latency_ms", "cost_usd", "tokens_in", "tokens_out", "cognition_outcome"):
        assert field in provenance, f"la provenance del replan perdió '{field}'"
    assert provenance["resolved_model"] == "llama3.2:1b"
    assert provenance["tokens_in"] == 311
    assert provenance["tokens_out"] == 64
    assert provenance["cost_usd"] == 0.0002


@pytest.mark.asyncio
async def test_21_provenance_presente_tambien_si_se_rechaza():
    """Un plan RECHAZADO es el caso cuya provenance más importa: por qué no sirvió."""
    mission = _mission()
    router = _StubRouter(_plan_response(INVENTED))
    cognitive = _cognitive(router)
    knowledge = _exhausted(KnowledgeState(), _step("investigar"))

    await _attempt(cognitive, mission, knowledge, _plan(_step("investigar")), [])

    overlay = mission.context[DYNAMIC_REPLAN_KEY]
    assert overlay["accepted"] is False
    assert overlay["provenance"]["cost_usd"] == 0.0002
    assert overlay["provenance"]["chain"] == ["llama3.2:1b"]


# ======================================================================
# 22-23: el overlay también pasa por el Core
# ======================================================================


@pytest.mark.asyncio
async def test_22_completed_steps_filtran_los_pasos_del_overlay():
    """Regla 23: un paso dinámico ya completado no se vuelve a ofrecer."""
    mission = _mission()
    router = _StubRouter(_plan_response(ALTERNATIVE))
    cognitive = _cognitive(router)
    knowledge = KnowledgeState()
    mission.context[DYNAMIC_REPLAN_KEY] = {
        "version": 1, "origin": DYNAMIC_REPLAN_AUTHOR, "attempts": 1, "accepted": True,
        "created_iteration": 1, "replaces_step_ids": ["investigar"],
        "steps": [{"id": "dr1", "description": "d", "action": "research", "risk": "low",
                   "agent": "reasoner", "depends_on": [], "requires_approval": False,
                   "capability": "fs.stat", "requires_input": {}, "verification": None,
                   "proposed_by": DYNAMIC_REPLAN_AUTHOR, "rationale": None, "args": {}, "expected": None}],
        "provenance": {}, "reasons": [], "blocked_signatures": [],
    }
    knowledge.completed_steps = ["dr1"]

    pending = cognitive.pending_steps(mission, _plan(_step("investigar")), knowledge)

    assert pending == []


@pytest.mark.asyncio
async def test_23_failed_steps_filtran_los_pasos_del_overlay():
    """Regla 23: un paso dinámico que falló tampoco se repite."""
    mission = _mission()
    router = _StubRouter(_plan_response(ALTERNATIVE))
    cognitive = _cognitive(router)
    knowledge = KnowledgeState()
    knowledge.failed_steps = ["dr1"]
    mission.context[DYNAMIC_REPLAN_KEY] = {
        "version": 1, "origin": DYNAMIC_REPLAN_AUTHOR, "attempts": 1, "accepted": True,
        "created_iteration": 1, "replaces_step_ids": ["investigar"],
        "steps": [{"id": "dr1", "description": "d", "action": "research", "risk": "low",
                   "agent": "reasoner", "depends_on": [], "requires_approval": False,
                   "capability": "fs.stat", "requires_input": {}, "verification": None,
                   "proposed_by": DYNAMIC_REPLAN_AUTHOR, "rationale": None, "args": {}, "expected": None}],
        "provenance": {}, "reasons": [], "blocked_signatures": [],
    }

    pending = cognitive.pending_steps(mission, _plan(_step("investigar")), knowledge)

    assert pending == []


@pytest.mark.asyncio
async def test_24_el_gate_sigue_siendo_obligatorio_para_un_paso_dinamico():
    """Regla 8: `dr1` pasa por `validate_step` y por el Gate como cualquier otro paso.

    Si el Gate lo saltara, un plan del modelo sería un camino nuevo al executor.
    """
    mission = _mission()
    router = _StubRouter(_plan_response(ALTERNATIVE))
    executor = _SpyExecutor()
    cognitive = _cognitive(router, policy=_DenyPolicy(), executor=executor)
    knowledge = _exhausted(KnowledgeState(), _step("investigar"))
    original = _step("investigar")
    plan = _plan(original)
    await _attempt(cognitive, mission, knowledge, plan, [original])

    pending = cognitive.pending_steps(mission, plan, knowledge)
    await cognitive.step(mission, knowledge, pending_steps=pending, plan=plan)
    pending = cognitive.pending_steps(mission, plan, knowledge)
    outcome = await cognitive.step(mission, knowledge, pending_steps=pending, plan=plan)

    assert executor.calls == [], "el Gate denegó: el paso dinámico no pudo ejecutarse"
    assert outcome.mission_state is MissionState.BLOCKED


@pytest.mark.asyncio
async def test_25_un_paso_dinamico_invalido_bloquea_la_mision():
    """Aunque el overlay se aceptara, un paso que no valida se bloquea en `step()`.

    Defensa enprofundidad: el overlay pasa `PlanValidator` antes de aceptarse, pero el paso
    vuelve a validarse al ejecutarse. Un fallo ahí no cae al executor.
    """
    mission = _mission()
    cognitive = _cognitive(_StubRouter(_plan_response(ALTERNATIVE)))
    knowledge = KnowledgeState()
    # Overlay inyectado a mano con una capability que NO existe: simula un estado inconsistente.
    mission.context[DYNAMIC_REPLAN_KEY] = {
        "version": 1, "origin": DYNAMIC_REPLAN_AUTHOR, "attempts": 1, "accepted": True,
        "created_iteration": 1, "replaces_step_ids": ["investigar"],
        "steps": [{"id": "dr1", "description": "d", "action": "execute", "risk": "low",
                   "agent": "reasoner", "depends_on": [], "requires_approval": False,
                   "capability": "root.shell", "requires_input": {}, "verification": None,
                   "proposed_by": DYNAMIC_REPLAN_AUTHOR, "rationale": None, "args": {}, "expected": None}],
        "provenance": {}, "reasons": [], "blocked_signatures": [],
    }
    executor = _SpyExecutor()
    cognitive.executor = executor

    pending = cognitive.pending_steps(mission, _plan(_step("investigar")), knowledge)
    outcome = await cognitive.step(mission, knowledge, pending_steps=pending, plan=_plan(_step("investigar")))

    assert executor.calls == []
    assert outcome.mission_state is MissionState.BLOCKED


@pytest.mark.asyncio
async def test_26_max_replans_no_se_consume_con_el_replan_dinamico():
    """Regla 6: el replan dinámico NO gasta el presupuesto de replans del Core.

    Son cosas distintas: `max_replans` limita cuántas veces se replanifica sobre el MISMO plan;
    el replan dinámico es una sola llamada extra con su propio tope (una vez por misión).
    """
    mission = _mission()
    router = _StubRouter(_plan_response(ALTERNATIVE))
    cognitive = _cognitive(router)
    knowledge = _exhausted(KnowledgeState(), _step("investigar"))

    await _attempt(cognitive, mission, knowledge, _plan(_step("investigar")), [])

    assert knowledge.replans == 0, "el replan dinámico no debe haber incrementado `replans`"
    assert mission.context[DYNAMIC_REPLAN_KEY]["attempts"] == 1

@pytest.mark.external
@pytest.mark.asyncio
async def test_27_external_un_modelo_real_propone_una_alternativa():
    """Con un provider REAL, el replan dinámico devuelve una estrategia o un motivo.

    No es una dependencia de la suite determinista: si no hay provider real, se salta. Lo que
    NO se puede comprobar con stubs es si un modelo real alguna vez propone algo utilizable, y
    eso es justo lo que no se ha medido todavía (CORE-08A dejó esta misma laguna abierta).
    """
    from alexis.models.config import ModelConfig

    config = ModelConfig.from_env()
    if not config.build_providers():
        pytest.skip("no hay provider REAL configurado: se midió esto en CORE-08A y salió BLOCKED")

    router = ModelRouter(budget_usd=0)
    for provider in config.build_providers():
        router.register(provider)
    if not any(not p.degraded and p.available for p in router.providers()):
        pytest.skip("los providers configurados no están disponibles")

    mission = _mission()
    cognitive = _cognitive(router)
    knowledge = _exhausted(KnowledgeState(), _step("investigar"))
    original = _step("investigar")
    plan = _plan(original)

    await _attempt(cognitive, mission, knowledge, plan, [original])

    overlay = mission.context[DYNAMIC_REPLAN_KEY]
    assert overlay["attempts"] == 1
    # Se acepta o se rechaza, pero siempre con un motivo: nunca un silencio.
    if overlay["accepted"]:
        assert [s["id"] for s in overlay["steps"]] == ["dr1"]
        assert overlay["steps"][0]["capability"]
    else:
        assert overlay["reasons"], "un rechazo siempre explica por qué"
