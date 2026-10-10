"""COGNITION: el modelo no puede inventar argumentos que el paso validado no declaró.

## El defecto

`_ask_model()` hacía `args=dict(data.get("args") or {})`: los argumentos que propone el
modelo **sustituían** los del paso, sin ninguna validación. Demostrado: un paso de
`fs.write` que declara `{"contenido": ...}` recibía del modelo `{"path": "nota.txt"}` y
`fs.write` obedecía — el plan validado decía "crea este contenido" y se ejecutó "crea
este fichero", con un nombre que `PlanValidator` nunca aprobó.

El perímetro del executor sigue conteniendo el daño dentro del workspace, así que esto
NO es una fuga de seguridad. Es un defecto de integridad: **el Core valida un paso y
ejecuta otro**, y además lo escribe en la traza de auditoría como si fuera lo planeado.

## Lo que sí puede hacer el modelo

Refinar los valores de las claves que el paso YA declara. Es una capacidad legítima —"usa
`informe.md` en vez de `notas.txt`" es un ajuste de parámetro dentro del mismo paso— y
sigue sujeta al perímetro, que es la frontera de seguridad real.

## Lo que no puede

Introducir una clave que el paso no declaraba. Si el modelo la trae, se descarta y queda
registrada en `rejected`: no se ignora en silencio, porque un descarte invisible es
justo lo que hace imposible auditar por qué se ejecutó algo.
"""

from __future__ import annotations

import asyncio

import pytest

from test_cognitive_runtime import (
    _PassingVerifier,
    _ScriptedExecutor,
    _StubRouter,
    _mission,
    _response,
    _runtime,
)

from alexis.contracts import PlanStep, RiskLevel
from alexis.models.provider import ModelOutcome


def _step(step_id, *, capability="fs.read", args=None, action="execute", approval=False):
    """Paso con `args` declarados.

    El `_step` compartido no acepta `args`, y esta prueba necesita precisamente eso: el
    defecto consiste en que el modeloIntroduce claves que el paso NO declara, así que sin
    un paso con args declarados la prueba no observaría nada.
    """
    return PlanStep(
        step_id,
        f"paso {step_id}",
        action,
        RiskLevel.MEDIUM,
        "executor",
        capability=capability,
        requires_approval=approval,
        args=dict(args or {}),
    )


def _plan(*steps):
    from alexis.contracts import Plan

    return Plan(mission_id="m-de-prueba", steps=list(steps))


def _runtime_con_respuesta(data):
    return _runtime(
        _ScriptedExecutor({"leer": True, "responder": True}),
        _PassingVerifier(),
        model_router=_StubRouter(_response(data, outcome=ModelOutcome.REAL)),
    )


async def _decidir(data, *, pasos=None):
    cog = _runtime_con_respuesta(data)
    mission = _mission("crea un informe")
    plan = pasos or _plan(
        _step("leer"),
        _step("responder", action="respond", capability="tts.speak"),
    )
    knowledge = cog.knowledge_for(mission)
    pending = cog.pending_steps(mission, plan, knowledge)
    decision = await cog.decide_next_action(mission, knowledge, pending_steps=pending)
    return cog, decision


# --------------------------------------------------------------------- #
# El defecto
# --------------------------------------------------------------------- #


async def test_el_modelo_no_puede_añadir_claves_que_el_paso_no_declaró():
    """El caso que motivó el arreglo: `fs.write` sin `path` en el paso."""
    _, decision = await _decidir(
        {
            "action": "execute_tool",
            "step_id": "leer",
            "args": {"path": "nota_del_modelo.txt", "contenido": "inventado"},
        },
        pasos=_plan(
            _step("leer", capability="fs.read"),
            _step("responder", action="respond", capability="tts.speak"),
        ),
    )

    assert "path" not in decision.args, (
        "el modelo introdujo una clave que el paso no declaraba: "
        f"{decision.args}"
    )


async def test_la_clave_descartada_queda_registrada():
    """Un descarte invisible es lo que hace imposible auditar la ejecución."""
    _, decision = await _decidir(
        {
            "action": "execute_tool",
            "step_id": "leer",
            "args": {"path": "nota.txt", "contenido": "x"},
        },
        pasos=_plan(
            _step("leer", capability="fs.read"),
            _step("responder", action="respond", capability="tts.speak"),
        ),
    )

    descartadas = [
        clave
        for r in decision.rejected
        for clave in (r.get("args") or {})
    ]
    assert "path" in descartadas, (
        f"la clave descartada debe quedar en `rejected` para poder auditarla: {decision.rejected}"
    )


async def test_el_paso_que_no_declara_args_no_acepta_ninguno_del_modelo():
    """Un paso sin args declarados no es un hueco que el modelo pueda llenar."""
    cog = _runtime_con_respuesta(
        {"action": "execute_tool", "step_id": "responder", "args": {"texto": "hola"}}
    )
    mission = _mission("responde")
    plan = _plan(_step("responder", action="respond", capability="tts.speak"))
    knowledge = cog.knowledge_for(mission)
    pending = cog.pending_steps(mission, plan, knowledge)
    decision = await cog.decide_next_action(mission, knowledge, pending_steps=pending)

    assert "texto" not in decision.args


# --------------------------------------------------------------------- #
# Lo que el modelo SÍ puede hacer: refinar claves ya declaradas
# --------------------------------------------------------------------- #


async def test_el_modelo_sí_puede_refinar_una_clave_declarada():
    """Refinar un valor NO es inventar una clave, y sigue siendo útil."""
    _, decision = await _decidir(
        {
            "action": "execute_tool",
            "step_id": "leer",
            "args": {"path": "informe.md"},
        },
        pasos=_plan(
            _step("leer", capability="fs.read", args={"path": "notas.txt"}),
            _step("responder", action="respond", capability="tts.speak"),
        ),
    )

    assert decision.args.get("path") == "informe.md", (
        "ajustar el valor de una clave declarada es legítimo y debe seguir funcionando"
    )


async def test_un_paso_sin_args_que_el_modelo_no_propone_args_conserva_los_del_paso():
    """Regresión: cuando el modelo no propone args, mandan los del paso."""
    _, decision = await _decidir(
        {"action": "execute_tool", "step_id": "leer"},
        pasos=_plan(
            _step("leer", capability="fs.read", args={"path": "notas.txt"}),
            _step("responder", action="respond", capability="tts.speak"),
        ),
    )

    assert decision.args.get("path") == "notas.txt"


async def test_la_decisión_sigue_siendo_del_modelo():
    """El arreglo limita los ARGUMENTOS, no la decisión: el Core sigueeligiendo."""
    _, decision = await _decidir(
        {
            "action": "execute_tool",
            "step_id": "responder",
            "args": {},
        },
    )

    assert decision.proposed_by == "model"
    assert decision.action.value == "execute_tool"


async def test_el_fallback_sigue_marcando_la_decisión():
    """Sin cambios en el camino DEGRADED: el arreglo no lo toca."""
    cog = _runtime(
        _ScriptedExecutor({"leer": True, "responder": True}),
        _PassingVerifier(),
        model_router=_StubRouter(
            _response(
                {"action": "accion_inventada"},
                outcome=ModelOutcome.DEGRADED,
                text="[degraded] sin modelo",
            )
        ),
    )
    mission = _mission("crea un informe")
    plan = _plan(_step("leer"), _step("responder", action="respond", capability="tts.speak"))
    knowledge = cog.knowledge_for(mission)
    pending = cog.pending_steps(mission, plan, knowledge)
    decision = await cog.decide_next_action(mission, knowledge, pending_steps=pending)

    assert decision.proposed_by == "deterministic"
    assert decision.cognition_outcome != ModelOutcome.REAL.value