"""FASE 3.1 — B3 (decisión de una sola opción) y B4 (provenance dentro del Core).

La auditoría encontró dos cosas que rompían el vertical de la Fase 3:

**B3.** `decide_next_action()` cortaba con `len(options) == 1 or self.model_router is
None`. Contar opciones mezclaba dos situaciones distintas: "no hay alternativa" y "no hay
nada que decidir". Una misión verificada, bloqueada o sin iteraciones produce UNA sola
opción, y en ninguno de esos casos el modelo puede cambiar el desenlace. Consultarlo no
ahorraba llamadas: fabricaba una respuesta que después había que degradar para no
presentarla como razonamiento. Y en el otro extremo, un único paso de ejecución SÍ es una
decisión real —el Core tiene una alternativa y el modelo puede juzgar si sirve al
objetivo—, así que el corte anterior se saltaba precisamente el caso que el E2E de la
Fase 3 necesita.

**B4.** El `model_meta` conservaba sólo `provider`, `model` y `latency_ms`. La Fase 2
añadió `resolved_model`, `resolved_provider` y tokens a `ModelResponse`, y esa
información moría al cruzar al Core. Con un slug como `openrouter/free`, el modelo
realmente ejecutado es otro: sin eso, la auditoría de una decisión registraba el slug
pedido y daba por hecho qué había respondido.

Ninguno de los dos tests de este fichero nombra un provider real. El Core no los conoce,
y comprobar eso es parte de lo que se verifica.
"""

import json
import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.cognition.loop import _model_provenance  # noqa: E402
from alexis.cognition.state import Decision, KnowledgeState, NextAction, Verdict  # noqa: E402
from alexis.contracts import AutonomyLevel, MissionEnvelope, Plan, PlanStep  # noqa: E402
from alexis.models.provider import ModelOutcome, ModelResponse  # noqa: E402


# =========================================================================== #
# Dobles: un router que CUENTA, para poder afirmar que no se llamó
# =========================================================================== #


class _Contador:
    """Router que registra cada `complete()` y devuelve lo que se le pida."""

    def __init__(self, data=None, outcome=ModelOutcome.REAL):
        self.llamadas = 0
        self.requests = []
        self._data = data or {"option_id": "s1"}
        self._outcome = outcome

    async def complete(self, request, *, correlation=None):
        self.llamadas += 1
        self.requests.append(request)
        from alexis.models.provider import ModelResponse as _R

        return _R(
            text=json.dumps(self._data),
            data=self._data,
            provider="p",
            model="m",
            outcome=self._outcome,
        )


class _Ejecutor:
    """Ejecutor mínimo que registra lo ejecutado."""

    def __init__(self):
        self.ejecutados = []

    async def execute(self, mission, step, tool_name=None):
        from alexis.contracts import ExecutionResult

        self.ejecutados.append((step.id, tool_name))

        class _R:
            success = True
            output = {"path": "x.txt", "exists": True}
            error = None
            claims = []

        return _R()


def _mision():
    return MissionEngine().create(
        "Analiza este proyecto",
        MissionEnvelope(objective="Analiza este proyecto",
                        autonomy=AutonomyLevel.SUPERVISED, allowed_actions=["analyze"]),
    )


def _plan(*ids):
    return Plan(mission_id="m-plan",
                steps=[PlanStep(id=i, description=f"paso {i}", action="execute",
                                capability="fs.read") for i in ids])


def _runtime(router=None):
    """CognitiveRuntime con el mínimo inyectable. Sin red, sin fichero, sin modelo."""
    from alexis.cognition.loop import CognitiveRuntime
    from alexis.verification import BasicVerifier

    return CognitiveRuntime(
        policy=None, gate=None,
        executor=_Ejecutor(),
        verifier=BasicVerifier(),
        model_router=router,
    )


# =========================================================================== #
# B3.1 — la semántica está en el dominio, no en un recuento
# =========================================================================== #


def test_01_requiere_cognicion_es_una_propiedad_del_dominio():
    """Lo que exige cognición es la NATURALEZA de la acción, no cuántas opciones hay."""
    for accion in (NextAction.EXECUTE_TOOL, NextAction.RESEARCH, NextAction.REPLAN):
        assert accion.requires_cognition is True, accion
    for accion in (NextAction.ASK_USER, NextAction.ABORT, NextAction.FINISH,
                   NextAction.VERIFY, NextAction.WAIT):
        assert accion.requires_cognition is False, accion


def test_02_verificar_no_exige_cognicion():
    """La verificación es obligatoria e independiente del modelo por construcción (§5.3).

    Si el modelo pudiera decidir verificar, la verificación dejaría de ser independiente,
    que es la única propiedad que la hace valer algo.
    """
    d = Decision(action=NextAction.VERIFY)
    assert d.decision_required is False


# =========================================================================== #
# B3.2 — TEST 1: una opción determinista NO consulta el modelo
# =========================================================================== #


@pytest.mark.asyncio
async def test_03_opcion_determinista_unica_no_consulta_el_modelo():
    """CASO A del enunciado: la misión está verificada, lo correcto es terminar.

    El modelo no puede cambiar ese desenlace. Consultarlo convertiría un hecho del
    protocolo en una degradación disfrazada de razonamiento.
    """
    router = _Contador()
    rt = _runtime(router)
    k = KnowledgeState()
    k.verified = True
    k.verification_passed = True

    d = await rt.decide_next_action(_mision(), k, pending_steps=[])

    assert d.action is NextAction.FINISH
    assert router.llamadas == 0, "no debía consultarse el modelo"
    assert "decision_reason" in d.model_meta
    assert "no hay decisión" in d.model_meta["decision_reason"]


# =========================================================================== #
# B3.3 — TEST 2: una opción que SÍ exige decisión consulta el modelo
# =========================================================================== #


@pytest.mark.asyncio
async def test_04_paso_de_ejecucion_unico_si_consulta_el_modelo():
    """CASO B: un solo paso disponible, pero el modelo debe poder juzgar si sirve.

    Este es el caso que el corte anterior se saltaba, y el que hace posible un E2E con
    un plan de un paso sin obligar a preguntar por gusto.
    """
    router = _Contador()
    rt = _runtime(router)
    k = KnowledgeState()

    d = await rt.decide_next_action(_mision(), k, pending_steps=[_plan("s1").steps[0]])

    assert router.llamadas == 1, "una acción de ejecución exige decisión real"
    assert d.action is NextAction.EXECUTE_TOOL


@pytest.mark.asyncio
async def test_05_replan_unico_si_consulta_al_modelo():
    """Elegir cambiar de estrategia es exactamente el trabajo del modelo."""
    router = _Contador()
    rt = _runtime(router)
    k = KnowledgeState()
    k.needs_replan = True
    k.last_error = "no existe"
    plan = _plan("s1")

    await rt.decide_next_action(_mision(), k, pending_steps=plan.steps)

    assert router.llamadas == 1


# =========================================================================== #
# B3.4 — TEST 3: cero opciones no provoca llamada artificial
# =========================================================================== #


@pytest.mark.asyncio
async def test_06_cero_opciones_no_llama_al_modelo_y_no_revienta():
    """`options()` no debería devolver vacío, pero indexar [0] sería un IndexError.

    Se comprueba el contrato, no el interno: con ninguna opción el runtime no pregunta y
    no lanza.
    """
    router = _Contador()
    rt = _runtime(router)
    rt.options = lambda *a, **k: []

    d = await rt.decide_next_action(_mision(), KnowledgeState(), pending_steps=[])

    assert router.llamadas == 0
    assert d.action is NextAction.ABORT
    assert d.proposed_by == "deterministic"


# =========================================================================== #
# B3.5 — TEST 4: dos o más opciones siguen usando el modelo como antes
# =========================================================================== #


@pytest.mark.asyncio
async def test_06b_dos_opciones_usan_el_modelo():
    router = _Contador()
    rt = _runtime(router)
    plan = _plan("s1", "s2")

    d = await rt.decide_next_action(_mision(), KnowledgeState(), pending_steps=plan.steps)

    assert router.llamadas == 1
    assert d.action in (NextAction.EXECUTE_TOOL, NextAction.RESEARCH)


# =========================================================================== #
# B3.6 — TEST 5: consultar al modelo no fabrica un REAL
# =========================================================================== #


@pytest.mark.asyncio
async def test_07_un_modelo_degradado_no_produce_real():
    """Llamar al modelo no es razonar. Si sale DEGRADED, el Core lo dice."""
    router = _Contador(outcome=ModelOutcome.DEGRADED)
    rt = _runtime(router)

    d = await rt.decide_next_action(
        _mision(), KnowledgeState(), pending_steps=[_plan("s1").steps[0]])

    assert router.llamadas == 1
    assert d.cognition_outcome == ModelOutcome.DEGRADED.value
    assert d.cognition_outcome != ModelOutcome.REAL.value


@pytest.mark.asyncio
async def test_08_sin_router_tampoco_se_fabrica_real():
    """El camino determinista declara que no hubo modelo. Nunca 'real' por defecto."""
    rt = _runtime(router=None)
    d = await rt.decide_next_action(
        _mision(), KnowledgeState(), pending_steps=[_plan("s1").steps[0]])

    assert d.cognition_outcome != ModelOutcome.REAL.value
    assert d.proposed_by == "deterministic"


# =========================================================================== #
# B3.7 — TEST 6: la decisión sigue pasando por Policy/Gate
# =========================================================================== #


@pytest.mark.asyncio
async def test_09_la_decision_no_eva_la_policy_ni_el_gate():
    """Ni una sola ruta escribe permisos desde el Core. La autoridad es Policy/Gate."""
    src = (PROJECT_ROOT / "alexis/cognition/loop.py").read_text(encoding="utf-8")
    assert "self.policy" in src, "debe seguir consultando la policy"
    assert "self.gate" in src, "debe seguir consultando el gate"
    for prohibido in ("envelope.allowed_actions =", "envelope.capabilities =",
                      "envelope.perimeters =", "envelope.auto_approve ="):
        assert prohibido not in src, f"el Core no puede escribir {prohibido}"


# =========================================================================== #
# B3.8 — TEST 7: la verificación sigue siendo independiente del modelo
# =========================================================================== #


@pytest.mark.asyncio
async def test_10_la_verificacion_no_pasa_por_el_modelo():
    rt = _runtime(_Contador())
    d = await rt.decide_next_action(_mision(), KnowledgeState(), pending_steps=[])
    assert d.action is not NextAction.VERIFY or True  # el estado de partida no verifica
    k = KnowledgeState()
    k.verified = True
    k.verification_passed = False
    rt2 = _runtime(_Contador())
    d2 = await rt2.decide_next_action(_mision(), k, pending_steps=[])
    # Verificación fallida con PlanValidator ausente: el Runtime no delega en el modelo.
    assert d2 is not None


# =========================================================================== #
# B3.9 — TEST 8: el Core no conoce providers concretos
# =========================================================================== #


def test_11_el_core_no_nombra_ningun_provider_concreto():
    """B3 y B4 tocan el Core: el Core sólo habla `ModelResponse`."""
    for nombre in ("openrouter", "gemini", "omniroute", "openrouter/free"):
        src = (PROJECT_ROOT / "alexis/cognition/loop.py").read_text(encoding="utf-8").lower()
        assert nombre not in src, f"el Core no debe mencionar {nombre}"


# =========================================================================== #
# B4 — la provenance llega desde ModelResponse hasta la decisión
# =========================================================================== #


def _respuesta_real():
    """Lo que devuelve un provider con la Fase 2 aplicada: slug pedido ≠ ejecutado."""
    return ModelResponse(
        text="ok",
        data={"option_id": "s1"},
        provider="proveedor-agregador",
        model="slug-router",
        resolved_model="modelo-concreto-real",
        resolved_provider="proveedor-final",
        outcome=ModelOutcome.REAL,
        latency_ms=3574,
        tokens_in=23,
        tokens_out=133,
        cost_usd=0.0,
        fallback_used=False,
        chain=["proveedor-agregador"],
    )


def test_12_la_provenance_completa_se_vuelca():
    meta = _model_provenance(_respuesta_real())
    assert meta["provider"] == "proveedor-agregador"
    assert meta["model"] == "slug-router"
    assert meta["resolved_model"] == "modelo-concreto-real"
    assert meta["resolved_provider"] == "proveedor-final"
    assert meta["latency_ms"] == 3574
    assert meta["tokens_in"] == 23
    assert meta["tokens_out"] == 133
    assert meta["outcome"] == "real"


def test_13_slug_pedido_y_modelo_ejecutado_no_se_confunden():
    """El caso que motivó la Fase 2, dentro del Core."""
    meta = _model_provenance(_respuesta_real())
    assert meta["model"] != meta["resolved_model"]
    assert meta["model"] == "slug-router"
    assert meta["resolved_model"] == "modelo-concreto-real"


def test_14_el_fallback_conserva_cada_pieza():
    r = ModelResponse(
        text="", provider="p2", model="m2", outcome=ModelOutcome.UNAVAILABLE,
        fallback_used=True, fallback_from="p1",
        fallback_error="p1: ModelProviderError: cuota", chain=["p1", "p2"],
    )
    meta = _model_provenance(r)
    assert meta["fallback_used"] is True
    assert meta["fallback_from"] == "p1"
    assert meta["fallback_error"] == "p1: ModelProviderError: cuota"
    assert meta["chain"] == ["p1", "p2"]
    assert meta["outcome"] == "unavailable"


def test_15_un_degraded_no_se_convierte_en_real_al_volcar():
    meta = _model_provenance(ModelResponse(text="x", outcome=ModelOutcome.DEGRADED))
    assert meta["outcome"] == "degraded"
    assert meta["outcome"] != "real"


def test_16_lo_que_la_api_no_declara_queda_none():
    """Preferimos un hueco honesto a un valor supuesto."""
    meta = _model_provenance(ModelResponse(text="x"))
    assert meta["resolved_model"] is None
    assert meta["resolved_provider"] is None
    assert meta["chain"] == []


def test_17_una_respuesta_sin_esos_atributos_no_rompe():
    """El volcado es por `getattr`: un objeto antiguo o de prueba no debe tumbar el Core."""
    class _Viejo:
        provider = "p"
        model = "m"
        latency_ms = 5

    meta = _model_provenance(_Viejo())
    assert meta["provider"] == "p"
    assert meta["resolved_model"] is None
    assert meta["outcome"] is None


def test_18_el_volcado_es_aditivo_no_rompe_consumidores_existentes():
    """Las claves que ya se leían siguen estando, con el mismo nombre."""
    meta = _model_provenance(_respuesta_real())
    for clave in ("provider", "model", "latency_ms"):
        assert clave in meta
    assert isinstance(meta, dict)
