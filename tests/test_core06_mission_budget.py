"""CORE-06 — Presupuesto de modelos por misión, con aislamiento real.

El `ModelRouter` es un singleton de proceso (`apps/demo/app.py` lo construye una vez y
lo comparte entre el clasificador de intención, el `CognitiveRuntime` y el
`ModelPlanner`). Su contabilidad, `spent_usd`, también es de instancia. Eso convertía
el presupuesto de misión en un presupuesto **global de ALEXIS**: `reset_budget()` al
empezar cada misión ponía el contador a cero, así que dos misiones no se limitaban entre
sí y el gasto PRE-misión del clasificador se imputaba a la misión siguiente.

Estos tests fijan el contrato correcto:

- El presupuesto es **de la misión**, identificado por su correlación (CORE-05).
- Dos misiones **no comparten** gasto, aunque el router sea el mismo.
- Las llamadas PRE-misión no contaminan el presupuesto posterior.
- `max_cost_usd == 0` conserva la semántica de "sin límite" (P1: no es un límite de 0).
- El coste que se descuenta es el **real** (`ModelResponse.cost_usd`), no una estimación.
- El presupuesto agotado **no** es éxito: cae a `DEGRADED` y queda auditable.

Todos deben fallar con la implementación anterior, que usaba un contador único.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from alexis.models.correlation import KIND_MISSION, for_mission, pre_mission  # noqa: E402
from alexis.models.provider import (  # noqa: E402
    ModelOutcome,
    ModelRequest,
    ModelTask,
)
from alexis.models.router import ModelRouter  # noqa: E402

from test_model_router import FakeProvider  # noqa: E402


class _Mision:
    """Sólo un id: el presupuesto se identifica por correlación, no por el dominio."""

    def __init__(self, id_):
        self.id = id_


def _router(*providers, budget_usd=0.0):
    router = ModelRouter(budget_usd=budget_usd)
    for provider in providers:
        router.register(provider)
    return router


def _coste_por_llamada(valor: float, *, por_1k: float = 1.0):
    """Provider con un `cost_usd` REAL indicado, y un precio por token coherente.

    La comprobación de presupuesto es necesariamente una ESTIMACIÓN previa (el coste
    real sólo se conoce después de llamar), y esa estimación sale de
    `cost_per_1k_tokens`. Un provider con precio 0 se estima gratis y es correctamente
    seleccionable; por eso el provider de test declara precio, y su `cost_usd` real
    es lo que se descuenta al acumular.
    """
    class _P(FakeProvider):
        def __init__(self, id_, **kw):
            super().__init__(id_, cost=por_1k, **kw)
            self._real = valor

        async def complete(self, request, **kw):
            resp = await super().complete(request)
            resp.cost_usd = self._real
            return resp

    return _P



# ---------------------------------------------------------------------- #
# 1. Dos misiones NO comparten gasto
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_1_dos_misiones_no_comparten_presupuesto():
    """El defecto: con un contador único, el gasto de A resta del presupuesto de B.

    Misión A gasta 0.06 con límite 0.05. Misión B empieza con límite 0.10 y debe poder
    gastar sus 0.10 completos; si compartieran contador, a B le quedarían 0.04.
    """
    router = _router(_coste_por_llamada(0.06, por_1k=0.01)("caro"))
    a, b = _Mision("mision-a"), _Mision("mision-b")

    await router.complete(
        ModelRequest(task=ModelTask.REASON, max_cost_usd=0.05),
        correlation=for_mission(a),
    )
    # B entra después y con MUCHO presupuesto: debe respetarlo íntegro.
    resp_b = await router.complete(
        ModelRequest(task=ModelTask.REASON, max_cost_usd=0.10),
        correlation=for_mission(b),
    )

    assert resp_b.outcome is ModelOutcome.REAL, (
        "B debe poder gastar su propio presupuesto aunque A ya haya gastado"
    )
    gasto_b = router.spent_usd_for(b.id)
    assert gasto_b == pytest.approx(0.06), "el gasto se contabiliza por misión"


@pytest.mark.asyncio
async def test_1b_el_gasto_de_a_no_imputable_en_b():
    router = _router(_coste_por_llamada(0.06, por_1k=0.01)("caro"))
    a, b = _Mision("a"), _Mision("b")

    await router.complete(
        ModelRequest(task=ModelTask.REASON, max_cost_usd=0.05),
        correlation=for_mission(a),
    )
    await router.complete(
        ModelRequest(task=ModelTask.REASON, max_cost_usd=0.50),
        correlation=for_mission(b),
    )

    # Lo que A gastó NO puede aparecer en la partida de B.
    assert router.spent_usd_for(b.id) == pytest.approx(0.06)


# ---------------------------------------------------------------------- #
# 2. Acumulación dentro de una misma misión
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_2_dos_llamadas_de_la_misma_mision_acumulan():
    """El presupuesto es por MISIÓN, no por llamada: varias decisiones suman."""
    router = _router(_coste_por_llamada(0.03, por_1k=0.01)("p"))
    a = _Mision("a")

    await router.complete(
        ModelRequest(task=ModelTask.REASON, max_cost_usd=0.10),
        correlation=for_mission(a),
    )
    await router.complete(
        ModelRequest(task=ModelTask.UNDERSTAND, max_cost_usd=0.10),
        correlation=for_mission(a),
    )

    assert router.spent_usd_for(a.id) == pytest.approx(0.06), (
        "dos llamadas de la misma misión deben acumular, no reiniciarse"
    )


@pytest.mark.asyncio
async def test_2b_agotar_el_presupuesto_dentro_de_la_mision():
    router = _router(_coste_por_llamada(0.04, por_1k=0.01)("p"))
    a = _Mision("a")

    await router.complete(
        ModelRequest(task=ModelTask.REASON, max_cost_usd=0.10),
        correlation=for_mission(a),
    )
    # Segunda: 0.04 + 0.04 = 0.08 < 0.10, aún entra.
    segunda = await router.complete(
        ModelRequest(task=ModelTask.UNDERSTAND, max_cost_usd=0.10),
        correlation=for_mission(a),
    )
    assert segunda.outcome is ModelOutcome.REAL

    # Tercera: 0.08 + 0.04 = 0.12 > 0.10, ya no entra.
    tercera = await router.complete(
        ModelRequest(task=ModelTask.PLAN, max_cost_usd=0.10),
        correlation=for_mission(a),
    )
    assert tercera.outcome is not ModelOutcome.REAL, (
        "agotado el presupuesto no puede seguir exiting como REAL"
    )
    assert router.spent_usd_for(a.id) == pytest.approx(0.08), (
        "lo que NO se ejecutó no se contabiliza"
    )


# ---------------------------------------------------------------------- #
# 3. Pre-misión no contamina
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_3_pre_mision_no_contamina_una_mision_posterior():
    """El clasificador de intención es PRE-misión: su gasto no es de ninguna misión.

    Con un contador único, ese gasto se pagaba de la misión siguiente.
    """
    router = _router(_coste_por_llamada(0.05, por_1k=0.01)("p"))

    # Gasto pre-misión, sin límite (no pertenece a una misión).
    await router.complete(ModelRequest(task=ModelTask.UNDERSTAND))
    # Misión con presupuesto justo para 0.05.
    a = _Mision("a")
    resp = await router.complete(
        ModelRequest(task=ModelTask.REASON, max_cost_usd=0.05),
        correlation=for_mission(a),
    )

    assert resp.outcome is ModelOutcome.REAL, (
        "el gasto pre-misión no debe descontar del presupuesto de la misión"
    )
    assert router.spent_usd_for(a.id) == pytest.approx(0.05)


# ---------------------------------------------------------------------- #
# 4. max_cost_usd = 0 → sin límite
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_4_max_cost_cero_es_sin_limite():
    """P1: 0 significa SIN LÍMITE, no "presupuesto cero"."""
    router = _router(_coste_por_llamada(0.50)("caro"))
    a = _Mision("a")

    resp = await router.complete(
        ModelRequest(task=ModelTask.REASON, max_cost_usd=0.0),
        correlation=for_mission(a),
    )

    assert resp.outcome is ModelOutcome.REAL
    assert router.spent_usd_for(a.id) == pytest.approx(0.50), (
        "sin límite, se gasta lo que haga falta"
    )


# ---------------------------------------------------------------------- #
# 5. Un provider que excede el presupuesto no se selecciona
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_5_provider_que_excede_el_presupuesto_no_se_selecciona():
    router = _router(_coste_por_llamada(0.50)("caro"))
    a = _Mision("a")

    resp = await router.complete(
        ModelRequest(task=ModelTask.REASON, max_cost_usd=0.10),
        correlation=for_mission(a),
    )

    # No es éxito fingido: si no puede pagar, no puede decidir, y se dice.
    assert resp.outcome is not ModelOutcome.REAL
    assert router.spent_usd_for(a.id) == pytest.approx(0.0), (
        "un provider descartado no cuesta nada"
    )


@pytest.mark.asyncio
async def test_5b_el_presupuesto_agotado_no_es_exito_ni_fallo_oculto():
    """Presupuesto agotado: outcome honesto, nunca `REAL`."""
    router = _router(_coste_por_llamada(0.30, por_1k=1.0)("p"))
    a = _Mision("a")

    await router.complete(
        ModelRequest(task=ModelTask.REASON, max_cost_usd=0.30),
        correlation=for_mission(a),
    )
    segunda = await router.complete(
        ModelRequest(task=ModelTask.UNDERSTAND, max_cost_usd=0.30),
        correlation=for_mission(a),
    )

    # Sin provider de contingencia registrado el desenlace honesto es `UNAVAILABLE`.
    # Lo que NO puede ocurrir es `REAL`: presupuesto agotado no es éxito.
    assert segunda.outcome is not ModelOutcome.REAL
    assert segunda.outcome in (ModelOutcome.UNAVAILABLE, ModelOutcome.DEGRADED)
    assert segunda.error or segunda.fallback_error, (
        "debe quedar constancia de por qué no se pudo decidir"
    )


# ---------------------------------------------------------------------- #
# 6. Fallback respeta el presupuesto restante
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_6_fallback_respeta_el_presupuesto_restante():
    """Si el barato no cabe en lo que queda, cae al de contingencia si cabe.

    Lo que no puede ocurrir es que el caro se cuele por el hueco de
    `pool = affordable or [...]`, saltándose el límite.
    """
    # Estimación por llamada ≈ 0.03 (por_1k=0.01), coste real declarado 0.03.
    router = _router(_coste_por_llamada(0.03, por_1k=0.01)("caro"))
    a = _Mision("a")

    # Límite 0.05: la 1ª entra (0.03 estimado ≤ 0.05) y gasta 0.03 real; tras ella
    # quedan 0.02 y la 2ª ya no cabe. Lo que se comprueba es que no se cuela por el
    # hueco `affordable or [...]`, que devolvía los providers "gratis" sin mirar el
    # límite y podía dejar pasar uno caro.
    await router.complete(
        ModelRequest(task=ModelTask.REASON, max_cost_usd=0.05),
        correlation=for_mission(a),
    )
    segunda = await router.complete(
        ModelRequest(task=ModelTask.UNDERSTAND, max_cost_usd=0.05),
        correlation=for_mission(a),
    )

    assert segunda.outcome is not ModelOutcome.REAL, (
        "agotado el presupuesto no se puede seguir entrando"
    )
    assert router.spent_usd_for(a.id) == pytest.approx(0.03), (
        "sólo se contabiliza lo que realmente se ejecutó"
    )


# ---------------------------------------------------------------------- #
# 7. model.routed conserva correlación y coste real
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_7_el_audit_conserva_correlacion_y_coste_real():
    """El `cost_usd` que descuenta el presupuesto es el real, y la correlación sigue viva."""
    from alexis.events.bus import EventBus

    bus = EventBus()
    router = ModelRouter(event_bus=bus)
    router.register(_coste_por_llamada(0.02, por_1k=0.01)("p"))
    a = _Mision("mision-audit")

    resp = await router.complete(
        ModelRequest(task=ModelTask.REASON, max_cost_usd=0.10),
        correlation=for_mission(a),
    )
    await __import__("asyncio").sleep(0.05)

    assert resp.cost_usd == pytest.approx(0.02)
    eventos = [e for e in bus.events if e["topic"] == "model.routed"]
    assert eventos, "debe publicarse model.routed"
    payload = eventos[-1]["payload"]
    assert payload["cost_usd"] == pytest.approx(0.02), "el audit lleva el coste real"
    assert payload["correlation"]["mission_id"] == a.id
    assert payload["correlation"]["kind"] == KIND_MISSION


# ---------------------------------------------------------------------- #
# 8. Concurrencia
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_8_dos_misiones_concurrentes_no_contaminan_sus_presupuestos():
    """Intercaladas y con límites distintos: cada una lleva su cuenta.

    Éste es el caso que un `reset_budget()` shared rompe de forma más brutal.
    """
    import asyncio as aio

    router = _router(_coste_por_llamada(0.04, por_1k=0.01)("p", delay=0.01))
    a, b = _Mision("conc-a"), _Mision("conc-b")

    async def rutar(mision, limite):
        return await router.complete(
            ModelRequest(task=ModelTask.REASON, max_cost_usd=limite),
            correlation=for_mission(mision),
        )

    resultados = await aio.gather(
        rutar(a, 0.10),
        rutar(b, 0.10),
        rutar(a, 0.10),
        rutar(b, 0.10),
    )

    # A y B hicieron 2 llamadas cada una a 0.04 → 0.08 cada una.
    assert router.spent_usd_for(a.id) == pytest.approx(0.08)
    assert router.spent_usd_for(b.id) == pytest.approx(0.08)
    assert all(r.outcome is ModelOutcome.REAL for r in resultados)


# ---------------------------------------------------------------------- #
# 9. El presupuesto global del operador sigue siendo global
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_9_el_presupuesto_global_se_conserva():
    """`ALEXIS_MODEL_BUDGET_USD` es del operador y sigue aplicando a todo el proceso.

    El per-mission NO puede anularlo: si el global es más bajo, manda el más bajo.
    """
    router = _router(_coste_por_llamada(0.50, por_1k=1.0)("p"), budget_usd=0.20)
    a = _Mision("a")

    resp = await router.complete(
        ModelRequest(task=ModelTask.REASON, max_cost_usd=1.00),
        correlation=for_mission(a),
    )

    assert resp.outcome is not ModelOutcome.REAL, (
        "el límite global del operador debe seguir mandando sobre el de misión"
    )
    assert router.budget_usd == pytest.approx(0.20), "el global no se reescribe por misión"