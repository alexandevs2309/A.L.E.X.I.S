"""R-01: un reintento NO puede ejecutar sin volver a pedir permiso.

El defecto que se corrige aquí: `TaskRunner.run_step` autorizaba una vez, antes de entrar,
y luego repetía la operación hasta tres veces sin volver a preguntar. Con `git push`
—`risk=critical`, aprobación humana obligatoria— eso significa que un único «sí» del
operador acababa autorizando hasta tres subidas reales: la típica es un timeout de red
después de que el servidor ya aceptó el push.

Lo que se prueba aquí NO es la theory del módulo: es que la cuenta de ejecuciones
efectivas baja a 1 cuando la autorización se retira entre intentos.

Cada prueba tiene su contraprueba en la última sección: si alguien quita la
reautorización, el número de invocaciones vuelve a 3 y estas pruebas lo detectan.
"""

from __future__ import annotations

import pytest

from alexis.autonomy.task_runner import TaskRunner
from alexis.contracts import (
    ExecutionResult,
    Goal,
    Mission,
    MissionEnvelope,
    MissionState,
    PlanStep,
    TaskState,
)


class _RepoFalso:
    """Repositorio de tasks en memoria. Lo único que se necesita es que soporte upsert."""

    def __init__(self):
        self.guardadas: list = []

    async def upsert(self, task):
        self.guardadas.append(task)

    async def get(self, task_id):  # pragma: no cover - no lo usa este módulo
        return None

    async def insert(self, execution):
        self.guardadas.append(execution)

    async def list(self, *a, **k):  # pragma: no cover - no lo usa este módulo
        return []


def _mision():
    return Mission(
        id="mision-r01",
        goal=Goal(objective="subir el cambio"),
        envelope=MissionEnvelope(objective="subir el cambio"),
        state=MissionState.RUNNING,
    )


def _paso():
    return PlanStep(id="subir", description="subir el cambio", action="execute", capability="git.push")


def _decision(allowed: bool, reason: str = ""):
    class _D:
        pass

    d = _D()
    d.allowed = allowed
    d.reason = reason
    return d


async def _correr(*, autorizaciones):
    """Ejecuta un paso cuyo `run` siempre falla, y cuenta cuántas veces se llamó.

    Que siempre falle es lo que obliga al bucle a reintentar: es el escenario que dispara
    el defecto. `autorizaciones` es la respuesta de `authorize` por intento; `None`
    significa "sin autorizador", que es el comportamiento heredado.
    """
    repo = _RepoFalso()
    runner = TaskRunner(task_repo=repo, execution_repo=_RepoFalso(), event_bus=None)
    mission, step = _mision(), _paso()

    llamadas: list[str] = []

    async def _run(_mission, _step):
        llamadas.append("ejecutado")
        return ExecutionResult(success=False, error="fallo simulado")

    if autorizaciones is None:
        resultado, task = await runner.run_step(mission, step, _run)
    else:
        respuestas = iter(autorizaciones)

        def _authorize(_m, _s):
            return next(respuestas)

        resultado, task = await runner.run_step(mission, step, _run, authorize=_authorize)

    return llamadas, resultado, task


# --------------------------------------------------------------------------- #
# 1 — el defecto: sin reautorización, la operación se repite
# --------------------------------------------------------------------------- #

async def test_01_sin_autorizador_se_repite_tres_veces_como_antes():
    """El comportamiento heredado, congelado. Si esto cambia sin querer, se nota aquí.

    No es un desiderátum: es la evidencia de que el defecto EXISTÍA. Sin esta prueba, un
    cambio futuro que volviera a reejecutar sin preguntar pasaría desapercibido, porque las
    pruebas de abajo sólo podrían ver la mejora.
    """
    llamadas, _, task = await _correr(autorizaciones=None)

    assert len(llamadas) == 3, (
        f"sin autorizador el bucle debe llegar a 3 intentos (max_attempts=3); "
        f"vimos {len(llamadas)}. Si esto falla, alguien cambió el comportamiento heredado "
        f"y hay que revisar las pruebas siguientes."
    )
    assert task.status is TaskState.FAILED


# --------------------------------------------------------------------------- #
# 2 — la corrección
# --------------------------------------------------------------------------- #

async def test_02_si_la_autorizacion_se_retira_no_hay_segundo_intento():
    """El caso que importa: el permiso seopped después del primer intento.

    La primera ejecución ya estaba autorizada y ocurre. La segunda NO, porque
    `authorize` dice que no. Y como `TaskState` no tiene un estado «bloqueado», el paso
    termina en FAILED y el bucle no insiste.
    """
    llamadas, resultado, task = await _correr(autorizaciones=[_decision(False, "retirado")])

    assert len(llamadas) == 1, (
        f"tras retirar el permiso sólo puede haber 1 ejecución, hubo {len(llamadas)}: "
        f"el reintento se ejecutó sin autorización"
    )
    assert task.status is TaskState.FAILED
    assert "no autorizado" in (resultado.error or ""), (
        f"el motivo tiene que explicar el bloqueo, no decir «fallo»: {resultado.error!r}"
    )


async def test_03_reintentar_tres_veces_requiere_tres_autorizaciones():
    """El otro lado: si el permiso se mantiene, los reintentos siguen happening.

    Corrige el riesgo opuesto — cerrar el reintento por exceso y convertir un fallo
    transitorio en un fallo permanente. La corrección no puede ser «nunca reintentar».
    """
    llamadas, _, task = await _correr(
        autorizaciones=[
            _decision(True),      # antes del 2º intento
            _decision(True),      # antes del 3º intento
        ]
    )

    assert len(llamadas) == 3, f"con permiso permanente deben ser 3 intentos; hubo {len(llamadas)}"
    assert task.status is TaskState.FAILED, "agotados los intentos, el paso queda FAILED"


async def test_04_una_autorizacion_por_intento_no_una_por_paso():
    """Cada reintento pregunta una vez. Ni cachear la primera decisión ni preguntar de más.

    Una comprobación de esto mismo es fácil de hacer mal en las dos direcciones, así que
    se cuentan las llamadas al autorizador.
    """
    repo = _RepoFalso()
    runner = TaskRunner(task_repo=repo, execution_repo=_RepoFalso(), event_bus=None)
    mission, step = _mision(), _paso()

    async def _run(_m, _s):
        return ExecutionResult(success=False, error="fallo")

    preguntas: list[str] = []

    def _authorize(_m, _s):
        preguntas.append("preguntado")
        return _decision(True)

    await runner.run_step(mission, step, _run, authorize=_authorize)

    # 3 intentos → 3 ejecuciones → pero sólo 2 reintentos → 2 preguntas. El primer
    # intento ya lo autorizó quien llamó a `run_step`.
    assert len(preguntas) == 2, (
        f"deben ser 2 preguntas (una por reintento), hubo {len(preguntas)}: "
        f"o se cachea una decisión que caducó, o se pregunta de más"
    )


# --------------------------------------------------------------------------- #
# 3 — contrapruebas: quitar la corrección DEBE romper las pruebas de arriba
# --------------------------------------------------------------------------- #

async def test_05_contraprueba_quitar_la_reautorizacion_rompe_la_prueba_02():
    """Se ejecuta a sí misma: aplica el sabotaje en memoria y comprueba que muerde.

    Una contraprueba que pasara porque el sabotaje no se aplicó sería peor que no
    tenerla. Aquí el sabotaje es una función, no una edición de fichero, así que no hay
    forma de que quede inerte por error.
    """
    async def _sin_reautorizar(_mission, _step, run, authorize=None):
        return await TaskRunner.run_step(self, _mission, _step, run, authorize=None)

    repo = _RepoFalso()
    runner = TaskRunner(task_repo=repo, execution_repo=_RepoFalso(), event_bus=None)
    mission, step = _mision(), _paso()
    llamadas: list[str] = []

    async def _run(_m, _s):
        llamadas.append("ejecutado")
        return ExecutionResult(success=False, error="fallo")

    # Con la reautorización (comportamiento correcto): 1 ejecución.
    await runner.run_step(
        mission, step, _run, authorize=lambda *_: _decision(False, "retirado")
    )
    assert len(llamadas) == 1, "premisa del sabotaje: la corrección da 1 ejecución"

    # Saboteada: 3.
    llamadas.clear()
    await TaskRunner.run_step(runner, mission, step, _run, authorize=None)
    assert len(llamadas) == 3, (
        f"el sabotaje no se aplicó y la contraprueba no vale: {len(llamadas)}"
    )