"""CORE-08A — Activación inteligente del ModelPlanner, provenance y catálogo exigido.

CORE-08A no cambia QUÉ puede hacer el planner: sigue proponiendo y el `PlanValidator`
sigue siendo la autoridad. Cambia tres cosas que eran silenciosamente incorrectas.

**08A-1 — Activación.** `ALEXIS_MODEL_PLANNER` pasó de "por defecto apagado, y apagado
significaba sólo 'no'` a tres modos: `0` OFF absoluto, `1` ON explícito, auto (por defecto)
que consulta si hay un provider REAL *y* presupuesto. Antes, con el flag sin definir, el
código de producción no distinguía "no quise usar el modelo" de "el modelo no estaba": dos
fallos distintos que se ven igual desde fuera. El motivo queda en `plan_provenance`.

**08A-2 — Provenance.** `ModelPlanner` reconstruía a mano cuatro campos
(`provider`, `model`, `latency_ms`, `cognition_outcome`) mientras
`ModelResponse.audit_event()` calculaba doce. Se perdía lo que sólo el router sabe
resolver: modelo pedido frente a respondiente, cadena de fallback y coste. Sin eso,
auditar por qué un plan salió como salió exige reconstruirlo a mano.

**08A-3 — Catálogo exigido.** `PlanValidator(catalog=None)` tenía un agujero real: sin
catálogo, las comprobaciones de existencia y disponibilidad se saltaban *enteras* ( tanto
`catalog is None` como `available()` vacío son falsos), y una capability inventada pasaba
el filtro. `require_catalog` convierte ese silencio en un motivo explícito que cae al
`RuleBasedPlanner`. Es opt-in porque el validador sin catálogo aparece en tests y
componentes que validan argumentos, y ahí no debe cambiar nada.

Regla que comparten los 15 tests: **"hay un provider" no es "el plan será válido"**.
La elegibilidad pregunta a quién preguntarle; el `PlanValidator` decide.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.gates import AutonomyGate  # noqa: E402
from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.capabilities import build_catalog  # noqa: E402
from alexis.cognition.planner import Planner  # noqa: E402
from alexis.cognition.planner_model import ModelPlanner, PlanValidator  # noqa: E402
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    ExecutionResult,
    MissionEnvelope,
    Observation,
    Verification,
)
from alexis.core.runtime import AlexisRuntime  # noqa: E402
from alexis.events.bus import EventBus  # noqa: E402
from alexis.learning.system import ExperienceLearner  # noqa: E402
from alexis.memory.store import InMemoryMemory  # noqa: E402
from alexis.models.provider import ModelOutcome, ModelRequest, ModelResponse  # noqa: E402
from alexis.models.router import ModelRouter  # noqa: E402
from alexis.security.policy import PolicyEngine  # noqa: E402

ALL_ACTIONS = ["understand", "analyze", "research", "execute", "verify", "modify", "test", "respond"]


def _mission(objective="analiza notas.txt", **over):
    data = dict(objective=objective, autonomy=AutonomyLevel.SUPERVISED, allowed_actions=list(ALL_ACTIONS))
    data.update(over)
    return MissionEngine().create(objective, MissionEnvelope(**data))


# ----------------------------------------------------------------------
# Dobles deterministas. NINGÚN test de CORE-08A toca la red.
# ----------------------------------------------------------------------


class _Provider:
    """Provider fingido: `degraded`/`available` son los dos ejes que importan."""

    def __init__(self, pid="real-1", *, degraded=False, available=True):
        self.id = pid
        self.degraded = degraded
        self.available = available
        self.cost_per_1k_tokens = 0.001
        self.latency_p50_ms = 50

    def describe(self):
        return {"id": self.id, "degraded": self.degraded}


class _StubRouter:
    """Router que devuelve una respuesta construida a medida y registra lo que recibió."""

    def __init__(self, response: ModelResponse):
        self.response = response
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest, *, correlation=None):
        self.requests.append(request)
        return self.response


def _plan_response(**over):
    """Respuesta REAL con un plan válido, con la provenance completa puesta a prueba."""
    data = {
        "steps": [
            {
                "id": "leer",
                "description": "Leer el archivo",
                "action": "research",
                "capability": "fs.read",
                "risk": "low",
                "depends_on": [],
                "args": {"path": "notas.txt"},
            }
        ]
    }
    base = dict(
        text="",
        data=data,
        provider="openrouter",
        model="openai/gpt-4o-mini",
        resolved_model="poolside/laguna-xs-2.1:free",
        resolved_provider="openrouter",
        outcome=ModelOutcome.REAL,
        fallback_used=True,
        fallback_from="anthropic/claude-sonnet",
        fallback_error="429 del primero",
        tokens_in=412,
        tokens_out=88,
        cost_usd=0.0017,
        latency_ms=1234,
        chain=["anthropic/claude-sonnet", "openai/gpt-4o-mini"],
    )
    base.update(over)
    return ModelResponse(**base)


class _SpyExecutor:
    def __init__(self):
        self.calls = []

    async def execute(self, mission, step, *, tool_name=None):
        self.calls.append(step.id)
        return ExecutionResult(
            success=True,
            output={"ok": True, "step": step.id},
            observations=[Observation(f"tool.{step.id}", {"ok": True}, trusted=True)],
        )

    async def __call__(self, mission, step, tool_name=None):
        return await self.execute(mission, step, tool_name=tool_name)


class _PassingVerifier:
    async def verify(self, mission, plan):
        return Verification(passed=True, evidence=["ok"], confidence=0.9, notes="verificado")


def _runtime(*, router, plan_model, plan_validator):
    return AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=_SpyExecutor(),
        verifier=_PassingVerifier(),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        plan_model=plan_model,
        plan_validator=plan_validator,
    )


# ======================================================================
# 08A-1 — ACTIVACIÓN INTELIGENTE
# ======================================================================


def test_1_auto_activa_con_provider_real_elegible():
    """El modo por defecto (auto) enciende el planner si hay a quién preguntarle."""
    from apps.demo.app import _plan_eligible

    router = ModelRouter(budget_usd=0)
    router.register(_Provider("openrouter"))

    eligible, reason = _plan_eligible(router, None)

    assert eligible is True
    assert "openrouter" in reason


def test_2_auto_no_activa_con_degraded():
    """DEGRADED no cuenta como provider REAL: preguntar cuesta igual y no razona."""
    from apps.demo.app import _plan_eligible

    router = ModelRouter(budget_usd=0, allow_degraded=True)
    router.register(_Provider("local-1b", degraded=True))

    eligible, reason = _plan_eligible(router, None)

    assert eligible is False
    assert "REAL" in reason


def test_3_auto_no_activa_con_unavailable():
    """Un provider registrado pero no disponible no puede recibir una petición."""
    from apps.demo.app import _plan_eligible

    router = ModelRouter(budget_usd=0)
    router.register(_Provider("openrouter", available=False))

    eligible, reason = _plan_eligible(router, None)

    assert eligible is False
    assert "REAL" in reason


def test_4_mixto_degraded_y_real_gana_el_real():
    """Un REAL disponible gana: el DEGRADED no puede desplazar al que sí razona."""
    from apps.demo.app import _plan_eligible

    router = ModelRouter(budget_usd=0, allow_degraded=True)
    router.register(_Provider("local-1b", degraded=True))
    router.register(_Provider("openrouter"))

    eligible, _ = _plan_eligible(router, None)

    assert eligible is True


def test_5_auto_no_activa_sin_presupuesto():
    """Provider real pero sin presupuesto: no se pregunta. `max_cost_usd == 0` es sin límite."""
    from apps.demo.app import _plan_eligible

    router = ModelRouter(budget_usd=0.01)
    router.register(_Provider("openrouter"))
    router._spent_by_correlation[""] = 0.01

    eligible, reason = _plan_eligible(router, None)

    assert eligible is False
    assert "presupuesto" in reason


def test_6_presupuesto_cero_significa_sin_limite():
    """CORE-06: `budget_usd=0` es 'sin límite', no 'presupuesto cero'."""
    from apps.demo.app import _plan_eligible

    router = ModelRouter(budget_usd=0)
    router.register(_Provider("openrouter"))
    router._spent_by_correlation[""] = 99.0

    eligible, _ = _plan_eligible(router, None)

    assert eligible is True


def test_7_motivo_de_fallback_queda_registrado_en_plan_provenance():
    """Si no hubo ModelPlanner, `plan_provenance` dice por qué.

    Sin esto, un plan por reglas en un sistema con modelo disponible y un sistema sin
    modelo son indistinguibles desde fuera: dos fallos distintos, misma apariencia.
    """
    router = _StubRouter(_plan_response())
    runtime = _runtime(
        router=router, plan_model=None, plan_validator=PlanValidator(catalog=build_catalog())
    )
    runtime.plan_planner_mode = "auto-fallback"
    runtime.plan_planner_reason = "no hay ningún provider REAL disponible (DEGRADED/UNAVAILABLE)"

    import asyncio

    result = asyncio.run(runtime.run_mission(_mission()))

    provenance = result.context["plan_provenance"]
    assert provenance["proposed_by"] == "rule_based"
    assert provenance["accepted"] is True
    assert provenance["model_planner"]["mode"] == "auto-fallback"
    assert "REAL" in provenance["model_planner"]["reason"]


def test_8_plan_por_modelo_no_lleva_el_motivo_de_no_uso():
    """Si el planner SÍ se usó, `model_planner` no aparece: no hay nada que explicar."""
    router = _StubRouter(_plan_response())
    runtime = _runtime(
        router=router,
        plan_model=ModelPlanner(router, catalog=build_catalog()),
        plan_validator=PlanValidator(catalog=build_catalog()),
    )
    runtime.plan_planner_mode = "auto"
    runtime.plan_planner_reason = "provider REAL elegible"

    import asyncio

    result = asyncio.run(runtime.run_mission(_mission()))

    assert result.context["plan_provenance"]["proposed_by"] == "model"
    assert "model_planner" not in result.context["plan_provenance"]


# ======================================================================
# 08A-2 — PROVENANCE COMPLETA
# ======================================================================


def test_9_provenance_completa_del_plan():
    """Los doce campos que `audit_event()` sabe dar, más tokens."""
    router = _StubRouter(_plan_response())
    planner = ModelPlanner(router, catalog=build_catalog())

    import asyncio

    proposal = asyncio.run(planner.create_plan(_mission()))

    assert proposal.ok, proposal.reasons
    meta = proposal.meta
    for field in (
        "provider", "model", "resolved_model", "resolved_provider", "outcome",
        "fallback_used", "fallback_from", "fallback_error", "chain",
        "latency_ms", "cost_usd", "tokens_in", "tokens_out", "cognition_outcome",
    ):
        assert field in meta, f"la provenance del plan perdió '{field}'"


def test_10_resolved_model_y_resolved_provider_sobreviven():
    """El slug pedido no es el modelo que respondió. Perder eso es perder la auditoría."""
    router = _StubRouter(_plan_response())
    planner = ModelPlanner(router, catalog=build_catalog())

    import asyncio

    meta = asyncio.run(planner.create_plan(_mission())).meta

    assert meta["model"] == "openai/gpt-4o-mini"
    assert meta["resolved_model"] == "poolside/laguna-xs-2.1:free"
    assert meta["resolved_provider"] == "openrouter"


def test_11_chain_fallback_y_coste_sobreviven():
    """Saber que hubo fallback, por qué, y cuánto costó: sin esto no se audita el coste."""
    router = _StubRouter(_plan_response())
    planner = ModelPlanner(router, catalog=build_catalog())

    import asyncio

    meta = asyncio.run(planner.create_plan(_mission())).meta

    assert meta["fallback_used"] is True
    assert meta["fallback_from"] == "anthropic/claude-sonnet"
    assert meta["fallback_error"] == "429 del primero"
    assert meta["chain"] == ["anthropic/claude-sonnet", "openai/gpt-4o-mini"]
    assert meta["cost_usd"] == 0.0017


def test_12_tokens_sobreviven():
    """`audit_event()` no incluye tokens: se leen de la respuesta. Cuestan, y limitan."""
    router = _StubRouter(_plan_response())
    planner = ModelPlanner(router, catalog=build_catalog())

    import asyncio

    meta = asyncio.run(planner.create_plan(_mission())).meta

    assert meta["tokens_in"] == 412
    assert meta["tokens_out"] == 88


def test_13_la_provenance_llega_a_plan_provenance():
    """No basta con que `ModelPlanner` la tenga: tiene que llegar al contexto de la misión."""
    router = _StubRouter(_plan_response())
    runtime = _runtime(
        router=router,
        plan_model=ModelPlanner(router, catalog=build_catalog()),
        plan_validator=PlanValidator(catalog=build_catalog()),
    )

    import asyncio

    result = asyncio.run(runtime.run_mission(_mission()))

    provenance = result.context["plan_provenance"]
    assert provenance["resolved_model"] == "poolside/laguna-xs-2.1:free"
    assert provenance["cost_usd"] == 0.0017
    assert provenance["tokens_in"] == 412
    assert provenance["chain"]


def test_14_provenance_por_plan_invalido_igualmente_queda():
    """Un plan RECHAZADO es justo el caso cuya provenance más importa: por qué no sirvió."""
    data = {"steps": [{"id": "x", "description": "x", "action": "research",
                       "capability": "git.magic_super_read", "risk": "low"}]}
    router = _StubRouter(_plan_response(data=data))
    runtime = _runtime(
        router=router,
        plan_model=ModelPlanner(router, catalog=build_catalog()),
        plan_validator=PlanValidator(catalog=build_catalog()),
    )

    import asyncio

    result = asyncio.run(runtime.run_mission(_mission()))

    provenance = result.context["plan_provenance"]
    assert provenance["accepted"] is False
    assert provenance["cost_usd"] == 0.0017


# ======================================================================
# 08A-3 — CATÁLOGO EXIGIDO
# ======================================================================


def _plan_with(capability):
    return ModelPlanner(router=None).parse(
        _mission(),
        {"steps": [{"id": "paso", "description": "d", "action": "research",
                    "capability": capability, "risk": "low"}]},
    ).plan


def test_15_sin_catalogo_y_require_rechaza():
    """El agujero: sin catálogo, `catalog is None` y `available()` vacío son AMBOS falsos,
    así que las dos comprobaciones se saltaban y `root.shell` pasaba el filtro."""
    validator = PlanValidator(require_catalog=True)

    reasons = validator.validate(_mission(), _plan_with("fs.read"))

    assert reasons, "sin catálogo el validador_no puede afirmar que el plan es válido"
    assert any("catálogo" in r for r in reasons)
    assert any("NO significa 'todo disponible'" in r for r in reasons)


def test_16_sin_catalogo_require_false_conserva_compatibilidad():
    """El default sigue en False: los componentes que validan args no cambian de comportamiento."""
    assert PlanValidator().require_catalog is False
    assert PlanValidator().validate(_mission(), _plan_with("fs.read")) == []


def test_17_catalogo_vacio_tambien_rechaza():
    """Un catálogo sin capabilities habilitadas es tan inútil como no tenerlo, y no puede
    significar 'todo disponible'."""
    validator = PlanValidator(catalog=build_catalog(enable_available=False), require_catalog=True)

    reasons = validator.validate(_mission(), _plan_with("fs.read"))

    assert reasons
    assert any("ninguna capability habilitada" in r for r in reasons)


def test_18_capability_inventada_no_pasa_con_require():
    """Con catálogo real, una capability inexistente se rechaza (esto ya funcionaba)."""
    validator = PlanValidator(catalog=build_catalog(), require_catalog=True)

    reasons = validator.validate(_mission(), _plan_with("git.magic_super_read"))

    assert any("capability inexistente" in r for r in reasons)


def test_19_validate_step_tambien_exige_catalogo():
    """La frontera es la MISMA para el plan entero y para un paso suelto: sin catálogo, ambos."""
    validator = PlanValidator(require_catalog=True)

    reasons = validator.validate_step(_mission(), _plan_with("fs.read").steps[0])

    assert reasons
    assert any("catálogo" in r for r in reasons)


def test_20_require_true_con_catalogo_real_no_rechaza_por_nada():
    """`require_catalog=True` con un catálogo sano NO bloquea: el veto es por ausencia, no por
    estilo. Si no, Production se quedaría sin planes."""
    validator = PlanValidator(catalog=build_catalog(), require_catalog=True)

    assert validator.validate(_mission(), _plan_with("fs.read")) == []


def test_21_runtime_con_modelo_y_sin_catalogo_cae_a_reglas():
    """El requisito I-7: si el runtime que usa ModelPlanner no tiene catálogo válido, el plan
    generado NO se ejecuta y se registra el motivo."""
    router = _StubRouter(_plan_response())
    executor = _SpyExecutor()
    runtime = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=executor,
        verifier=_PassingVerifier(),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        plan_model=ModelPlanner(router, catalog=build_catalog()),
        plan_validator=PlanValidator(),  # sin catálogo, require_catalog=False
    )

    import asyncio

    result = asyncio.run(runtime.run_mission(_mission()))

    provenance = result.context["plan_provenance"]
    assert provenance["proposed_by"] == "model"
    assert provenance["accepted"] is False
    assert provenance["cognition_outcome"] == "skipped"
    assert any("catálogo" in r for r in provenance["reasons"])
    # No se llegó a preguntar al modelo: gastar 46-60s para descartar el resultado sería peor.
    assert router.requests == []
    assert result.plan is not None


def test_22_runtime_con_modelo_y_sin_validador_tampoco_acepta():
    """Sin validador, un plan de modelo no se puede comprobar: se cae a reglas, no a fe."""
    router = _StubRouter(_plan_response())
    runtime = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=_SpyExecutor(),
        verifier=_PassingVerifier(),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        plan_model=ModelPlanner(router, catalog=build_catalog()),
        plan_validator=None,
    )

    import asyncio

    result = asyncio.run(runtime.run_mission(_mission()))

    assert result.context["plan_provenance"]["accepted"] is False
    assert router.requests == []


def test_23_plan_invalido_sigue_cayendo_al_rule_based_planner():
    """El suelo no cambia: un plan del modelo que no valida se descarta y manda el de reglas."""
    data = {"steps": [{"id": "hack", "description": "x", "action": "research",
                       "capability": "root.shell", "risk": "low"}]}
    router = _StubRouter(_plan_response(data=data))
    executor = _SpyExecutor()
    runtime = _runtime(
        router=router,
        plan_model=ModelPlanner(router, catalog=build_catalog()),
        plan_validator=PlanValidator(catalog=build_catalog()),
    )
    runtime.executor = executor

    import asyncio

    result = asyncio.run(runtime.run_mission(_mission()))

    provenance = result.context["plan_provenance"]
    assert provenance["accepted"] is False
    assert provenance["fallback"] == "rule_based_planner"
    assert any("capability inexistente" in r for r in provenance["reasons"])
    # El plan del modelo se descartó: NINGÚN paso suyo llegó al executor. Lo que sí se
    # ejecuta es el del RuleBasedPlanner, que es el suelo y usa capabilities reales.
    assert executor.calls, "el RuleBasedPlanner debe seguir ejecutando: es el suelo"
    assert [c for c in executor.calls if c == "hack"] == []
    assert all(c in {"understand", "research", "execute", "verify", "analyze"} for c in executor.calls)
    assert result.plan.steps, "el RuleBasedPlanner sigue siendo el suelo"