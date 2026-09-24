"""Proveedores de contingencia determinista (F2.1).

`DegradedProvider` NO es un modelo: es comportamiento determinista de contingencia. Su
respuesta siempre lleva `outcome=DEGRADED` (P1) y su texto está marcado para que ni el
Core ni el usuario puedan confundirlo con razonamiento real.

`EchoModel` es la implementación de referencia: misma entrada → misma salida, sin red,
sin coste. Se usa en tests y como último recurso cuando todos los providers reales
fallan y la contingencia está permitida.
"""

import time

from alexis.models.provider import (
    ModelOutcome,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelTask,
)

#: Todos los pasos cognitivos que un plan puede pedir.
ALL_TASKS: set[ModelTask] = set(ModelTask)


class DegradedProvider(ModelProvider):
    """Base de providers deterministas de contingencia."""

    supports = ALL_TASKS
    priority = 900  # siempre el último
    cost_per_1k_tokens = 0.0
    latency_p50_ms = 1
    privacy_max = "secret"  # no sale nada a ningún sitio
    available = True
    degraded = True

    def _text(self, request: ModelRequest) -> str:
        return f"[degraded] sin modelo real para task={request.task.value}"

    def _data(self, request: ModelRequest) -> dict | None:
        return None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        started = time.monotonic()
        return ModelResponse(
            text=self._text(request),
            data=self._data(request),
            provider=self.id,
            model=self.id,
            outcome=ModelOutcome.DEGRADED,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


class EchoModel(DegradedProvider):
    """Devuelve texto/datos deterministas configurados. Para tests y modo offline.

    `replies` permite fijar la respuesta por tarea; el texto devuelto lleva siempre el
    prefijo `[degraded]` para que no pueda confundirse con salida de un modelo real.
    """

    def __init__(self, replies: dict[ModelTask, str] | None = None, data: dict | None = None):
        self.id = "echo"
        self.replies = replies or {}
        self._static_data = data

    def _text(self, request: ModelRequest) -> str:
        base = self.replies.get(request.task)
        if base is None:
            return f"[degraded] {request.task.value}: sin modelo real configurado"
        return f"[degraded] {base}"

    def _data(self, request: ModelRequest) -> dict | None:
        return self._static_data
