"""R-02: una inspección que FALLA no puede convertirse en «no ocurrió».

El defecto: `AlexisRuntime._inspect_path` devolvía `{"exists": False}` cuando la
herramienta de inspección fallaba, y `RecoveryManager.assess()` traducía eso a
`NOT_EXECUTED` → la acción se repetía. Traducido al funcionamiento real: si `fs.stat` no
podía leer la ruta —sin permisos, borrada mientras se inspeccionaba, un error de E/S—
ALEXIS daba por hecho que la escritura anterior NO se había hecho, y la repetía. Una
operación destructiva repetida porque **el observador falló**, no porque el mundo lo
dijera.

El docstring del propio método ya prometía lo contrario (`None` es "no sé", no "no
existe"). Estas pruebas comprueban que el código cumple lo que el documento dice.

Cada una tiene su contraprueba al final: si alguien devuelve otra vez `exists=False`,
estas pruebas tienen que romperse.
"""

from __future__ import annotations

import asyncio

from alexis.autonomy.recovery import (
    ActionStatus,
    LastKnownAction,
    RecoveryDecision,
    RecoveryManager,
    RecoveryState,
)
from alexis.contracts import (
    ExecutionResult,
    Goal,
    Mission,
    MissionEnvelope,
    MissionState,
    PlanStep,
    RiskLevel,
)

RUTA = "/tmp/informe.md"


def _mision():
    return Mission(
        id="mision-r02",
        goal=Goal(objective="escribir el informe"),
        envelope=MissionEnvelope(objective="escribir el informe"),
        state=MissionState.RUNNING,
    )


def _accion():
    """La última acción conocida, con efectos: una escritura sin confirmar."""
    return LastKnownAction(
        step_id="escribir",
        capability="fs.write",
        action="execute",
        args={"path": RUTA},
        recorded_status="unknown",
        probe_path=RUTA,
    )


def _probe():
    return PlanStep(
        id="__recovery_probe__",
        description="inspección de recuperación",
        action="research",
        risk=RiskLevel.LOW,
        agent="critic",
        capability="fs.stat",
        args={"path": RUTA},
    )


class _ExecutorQueFalla:
    """Sustituye al executor para poder observar QUÉ se le pasa y devolver un fallo.

    No replica lógica de recovery ni de política: lo que se prueba aquí es una frontera, y
    una frontera es justo lo que hay que poder simular para poder probarla.
    """

    def __init__(self, resultado):
        self.resultado = resultado
        self.llamadas: list = []

    async def execute(self, mission, step, tool_name=None):
        self.llamadas.append((getattr(step, "capability", None), tool_name))
        return self.resultado


def _inspeccionar(resultado):
    """Llama al método REAL de producción con un executor controlado."""
    from alexis.core.runtime import AlexisRuntime

    executor = _ExecutorQueFalla(resultado)
    runtime = AlexisRuntime.__new__(AlexisRuntime)   # sin __init__: no queremos la app
    runtime.executor = executor
    return asyncio.run(runtime._inspect_path(_mision(), RUTA)), executor


# --------------------------------------------------------------------------- #
# 1 — el defecto congelado: la herramienta falla y se afirma «no existe»
# --------------------------------------------------------------------------- #

def test_01_una_inspeccion_que_falla_no_afirma_que_no_exista():
    """El caso del defecto: la herramienta FALLÓ, así que no se sabe nada."""
    observacion, executor = _inspeccionar(
        ExecutionResult(success=False, error=f"Permission denied: {RUTA}")
    )

    assert observacion is None, (
        "no poder mirar no es «no existe»; la inspección fallida devolvió "
        f"{observacion!r}, que `assess()` lee como NOT_EXECUTED y hace repetir la escritura"
    )
    assert executor.llamadas == [("fs.stat", "fs.stat")], (
        "la capability del probe es hardcodeada: el modelo no elige qué se inspecciona"
    )


# --------------------------------------------------------------------------- #
# 2 — la corrección, hasta la decisión de recovery
# --------------------------------------------------------------------------- #

def test_02_el_probe_fallido_llega_a_unknown_y_no_a_una_repeticion():
    """Lo que importa no es sólo que `_inspect_path` devuelva None: es lo que decide recovery.

    Un `None` que acabara en `NOT_EXECUTED` arreglaría la mitad de nada.
    """
    observacion, _ = _inspeccionar(ExecutionResult(success=False, error="ruta inaccesible"))
    assert observacion is None, "premisa: la inspección fallida no observa nada"

    manager = RecoveryManager()
    accion = _accion()

    estado = manager.assess(accion, observacion)
    assert estado is ActionStatus.UNKNOWN, (
        f"no poder mirar es «no sé», nunca «no ocurrió»; se obtuvo {estado.value}"
    )

    # Y la decisión que sale de ahí NO es repetir a ciegas.
    manager.bind_capabilities({accion.step_id: accion.capability})
    recovery_state = RecoveryState(
        mission_id="mision-r02",
        attempt=0,
        unconfirmed=[accion.step_id],
        action_status={accion.step_id: estado.value},
    )
    decision = manager.decide(recovery_state)

    assert decision is not RecoveryDecision.RESUME, (
        f"una acción con efectos sin confirmar nunca se reanuda sola; se decidió "
        f"{decision.value}"
    )
    assert decision is RecoveryDecision.ASK_USER, (
        f"lo correcto es preguntar al humano, no adivinar; se decidió {decision.value}"
    )


def test_03_un_exists_false_real_sigue_siendo_un_no_ocurrio():
    """El otro lado: la corrección no debe cerrar el reintento por exceso.

    Si `fs.stat` funciona y dice que el fichero no está, entonces SÍ se puede afirmar que
    la escritura no ocurrió. Romper esto dejaría misiones atascadas sin poder reintentar.
    """
    observacion, _ = _inspeccionar(
        ExecutionResult(success=True, output={"exists": False})
    )
    assert observacion == {"exists": False, "output": {"exists": False}}, (
        "una inspección que FUNCIONÓ y dice que no existe es una afirmación válida y hay "
        f"que conservarla; se recibió {observacion!r}"
    )

    estado = RecoveryManager().assess(_accion(), observacion)
    assert estado is ActionStatus.NOT_EXECUTED, (
        f"con una inspección correcta, «no está» significa que no ocurrió; "
        f"se obtuvo {estado.value}"
    )


def test_04_la_salida_que_no_es_dict_tambien_es_desconocida():
    """Una tool que devuelve algo que no es un dict no es un «no existe»."""
    observacion, _ = _inspeccionar(
        ExecutionResult(success=True, output="texto cualquiera")
    )
    assert observacion is None, (
        f"una salida no estructurada no se puede interpretar; devolvió {observacion!r}"
    )


# --------------------------------------------------------------------------- #
# 3 — contrapruebas
# --------------------------------------------------------------------------- #

def test_05_contraprueba_el_defecto_original_producia_una_repeticion():
    """Se reproduce el código ANTERIOR a propósito y se comprueba que era peligroso.

    No se toca el fichero: la versión defectuosa se ejecuta como una función aparte. Así
    la contraprueba no puede quedar inerte por un sabotaje mal aplicado.
    """
    async def _inspect_path_saboteado(self, mission, path):
        """Copía literal de lo que había antes de la corrección."""
        result = await self.executor.execute(mission, _probe(), tool_name="fs.stat")
        if not result.success or not isinstance(result.output, dict):
            return {"exists": False, "error": str(result.error or "")}
        return {"exists": bool(result.output.get("exists")), "output": result.output}

    from alexis.core.runtime import AlexisRuntime

    executor = _ExecutorQueFalla(ExecutionResult(success=False, error="Permission denied"))
    runtime = AlexisRuntime.__new__(AlexisRuntime)
    runtime.executor = executor

    observacion = asyncio.run(
        _inspect_path_saboteado(runtime, _mision(), RUTA)
    )

    assert observacion == {"exists": False, "error": "Permission denied"}, (
        "el sabotaje tenía que reproducir el defecto y no lo hizo: la contraprueba "
        f"no vale ({observacion!r})"
    )

    # Y con ese valor, el sistema repetía a ciegas. Eso es lo que se corrigió.
    manager = RecoveryManager()
    estado = manager.assess(_accion(), observacion)
    assert estado is ActionStatus.NOT_EXECUTED, (
        f"el defecto producía NOT_EXECUTED (y con él, la repetición); "
        f"se obtuvo {estado.value}"
    )