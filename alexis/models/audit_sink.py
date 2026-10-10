"""CORE-05 — `AuditSink`: persiste `model.routed` en `audit_log`.

`ModelRouter` publica `model.routed` con la procedencia completa de cada decisión. Ese
evento llegaba al bus y se perdía al reiniciar. Este sink lo escribe en `audit_log`,
que es donde una auditoría debe ser **durable** (requisito 18 del plan P0).

Tres propiedades lo definen, y las tres son la razón de que sea un observador y no un
participante del ciclo cognitivo:

1. **Observador.** No toca `outcome`, ni `policy`, ni el envelope. Una decisión ya se
   tomó cuando este evento existe; el sink sólo la registra. Si el sink falla, la
   cognición sigue igual de válida: se pierde el REGISTRO, nunca el ACTO.

2. **Sin autoridad.** `AuditRepository.record` hace un `INSERT` en `audit_log`. Ni
   siquiera puede ampliar permisos: no muta misiones ni envelopes. Un payload de
   auditoría, aunque viniera manipulado, no autoriza nada.

3. **No adivina la misión.** La correlación llega ya resuelta en el evento
   (`payload["correlation"]`). El sink no busca la misión, no consulta
   `STATE["mission_id"]`, no correlaciona timestamps. Su trabajo es copiarla.

Sobre `mission_id` inválido: `audit_log.mission_id` tiene clave foránea a `missions`,
así que un id que no existe hace fallar el `INSERT`. El sink NO lo degrada en silencio
a `NULL` (eso lo disfrazaría de operación pre-misión); lo marca
(`correlation_error="unknown_mission_id"`), lo persiste con `mission_id=NULL` para no
perder la procedencia, y lo avisa por log. Así un bug de correlación queda observable.
"""

from __future__ import annotations

import asyncio
import logging

LOGGER = logging.getLogger("alexis.models.audit_sink")

#: Evento del bus que este sink consume.
TOPIC_MODEL_ROUTED = "model.routed"

#: Tope de tiempo para drenar lo pendiente al apagar. Sin él, un `stop()` durante un
#: atasco de base de datos bloquearía el apagado indefinidamente.
_DRAIN_TIMEOUT_S = 5.0
#: Inactividad que cierra el drenaje: la cola ya está vacía.
_DRAIN_IDLE_S = 0.5
#: Umbral a partir del cual un acumulamiento se avisa (no se descarta).
_WARN_PENDING = 500

#: Campos de procedencia que el sink copia a `details`. Son los que
#: `ModelResponse.audit_event()` produce; el sink no los inventa ni los recalcula.
PROVENANCE_FIELDS = (
    "task",
    "provider",
    "model",
    "resolved_model",
    "resolved_provider",
    "outcome",
    "fallback_used",
    "fallback_from",
    "fallback_error",
    "chain",
    "latency_ms",
    "cost_usd",
    "error",
)


class AuditSink:
    """Suscriptor del bus oficial que persiste `model.routed` en `audit_log`.

    Se adjunta al bus oficial (`EventBus`); no crea ni posee un bus propio, coherente
    con la invariante de CORE-03 (un solo event bus por proceso).
    """

    topic = TOPIC_MODEL_ROUTED

    def __init__(self, audit_repo, *, event_repo=None):
        self.audit_repo = audit_repo
        self.event_repo = event_repo
        self._sub = None
        self._bus = None
        self._task = None

    # ------------------------------------------------------------------ #
    # Ciclo de vida
    # ------------------------------------------------------------------ #

    def attach(self, bus, loop) -> "AuditSink":
        """Se suscribe al bus oficial y empieza a consumir. Devuelve `self`.

        `critical=True` es obligatorio aquí, y no una preferencia: este consumidor es el
        ÚNICO camino de `model.routed` a `audit_log`. Con la cola acotada del bus, una
        saturación descartaba el registro de auditoría de una decisión de modelo sin
        dejar rastro (medido: 50 de 150). Con `critical=True` la entrega es sin pérdida.
        """
        if self._task is not None:
            return self
        self._bus = bus
        self._sub = bus.subscribe_async(critical=True)
        self._task = loop.create_task(self._consume())
        return self

    async def _consume(self):
        while True:
            try:
                item = await self._sub.get()
            except asyncio.CancelledError:
                # Apagado ordenado (`stop()`): salir sin ruido es lo esperado.
                return
            except RuntimeError:
                # El loop se cerró mientras esta tarea esperaba en la cola. `Queue.get()`
                # re-programa su getter con `call_soon` y eso levanta "Event loop is
                # closed" cuando el loop ya no existe. Sin este catch, el interpreter
                # la reporta como excepción ignorada y `-W error` convierte un cierre
                # ordenado en un fallo de test que no tiene nada que ver.
                return
            if item.get("topic") != self.topic:
                continue
            await self.handle(item.get("payload") or {})
            self._warn_if_backed_up()

    async def stop(self) -> None:
        """Detiene el consumo tras **drenar** lo pendiente. Para tests y apagado.

        Antes cancelaba la tarea directamente, lo que descartaba en silencio todo lo que
        ya estaba aceptado en la cola: esos eventos no se habían perdido, se habían
        DEJADO de persistir. Aquí primero se drena con un tope de tiempo y después se
        cancela. Lo que no se logre drenar dentro del tope se registra de forma visible;
        no se declara éxito en silencio.
        """
        if self._sub is not None and self._task is not None and not self._task.done():
            pendientes = self._sub.qsize()
            if pendientes:
                await asyncio.wait_for(self._drain(), timeout=_DRAIN_TIMEOUT_S)
                restantes = self._sub.qsize()
                if restantes:
                    LOGGER.warning(
                        "audit_sink: %d eventos sin persistir tras %.1fs de drenaje; "
                        "el registro de auditoría queda incompleto de forma visible",
                        restantes,
                        _DRAIN_TIMEOUT_S,
                    )
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._sub is not None and self._bus is not None:
            self._bus.unsubscribe_async(self._sub)
        self._sub = None
        self._bus = None

    async def _drain(self) -> None:
        """Consume lo pendiente sin bloquear el apagado más de `_DRAIN_TIMEOUT_S`."""
        while True:
            try:
                item = await asyncio.wait_for(self._sub.get(), timeout=_DRAIN_IDLE_S)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                return
            if item.get("topic") != self.topic:
                continue
            await self.handle(item.get("payload") or {})

    def _warn_if_backed_up(self) -> None:
        """Un acumulamiento sostenido es un problema observable, no un detalle.

        Con `critical=True` la cola no tiene cota, así que nada se pierde; lo que podría
        agotarse es la memoria si la base de datos deja de ir al ritmo. Eso se avisa.
        """
        pending = self._sub.qsize() if self._sub is not None else 0
        if pending >= _WARN_PENDING:
            LOGGER.warning(
                "audit_sink: %d eventos sin persistir; la escritura va más lenta que "
                "la producción. La entrega sigue sin pérdida, pero la memoria crece.",
                pending,
            )

    # ------------------------------------------------------------------ #
    # Escritura
    # ------------------------------------------------------------------ #

    async def handle(self, payload: dict) -> dict | None:
        """Persiste un `model.routed`. Devuelve la fila escrita, o `None` si no pudo.

        Nunca propaga excepciones: auditar no puede tumbar la cognición.
        """
        if self.audit_repo is None:
            return None

        correlation = payload.get("correlation") or {}
        mission_id = correlation.get("mission_id")
        details = {k: payload.get(k) for k in PROVENANCE_FIELDS}
        details["correlation"] = dict(correlation)

        try:
            await self._insert(mission_id, details)
        except Exception as exc:  # noqa: BLE001 — el registro se pierde, no la decisión
            LOGGER.warning(
                "audit_sink: no se pudo registrar model.routed (mission_id=%r): %s",
                mission_id,
                exc,
            )
            return None
        return {"mission_id": mission_id, "details": details}

    async def _insert(self, mission_id: str | None, details: dict) -> None:
        try:
            await self.audit_repo.record(
                "model.routed",
                "model_router",
                mission_id=mission_id,
                **details,
            )
        except Exception as exc:  # noqa: BLE001
            # `audit_log.mission_id` tiene clave foránea a `missions`. Si el id no existe,
            # el INSERT falla. NO lo degradamos en silencio a NULL: eso lo disfrazaría de
            # operación pre-misión. Lo marcamos explícitamente y lo avisamos, conservando
            # la procedencia (que es el dato valioso), pero dejando rastro observable del
            # bug de correlación.
            if mission_id is not None and _looks_like_fk_violation(exc):
                marked = dict(details)
                correlation = dict(marked.get("correlation") or {})
                correlation["correlation_error"] = "unknown_mission_id"
                correlation["rejected_mission_id"] = mission_id
                marked["correlation"] = correlation
                LOGGER.warning(
                    "audit_sink: model.routed con mission_id=%r que no existe; se registra "
                    "con mission_id NULL y correlation_error=unknown_mission_id",
                    mission_id,
                )
                await self.audit_repo.record(
                    "model.routed",
                    "model_router",
                    mission_id=None,
                    **marked,
                )
                return
            raise


def _looks_like_fk_violation(exc: Exception) -> bool:
    """Heurística mínima: ¿el fallo fue por la clave foránea de `audit_log`?

    No se intenta parsear el error del driver; sólo distingue "el mission_id no existe"
    de otros fallos (base caída, etc.), que se propagan para que el llamador los registre
    como warning sin reintentar.
    """
    text = str(exc).lower()
    return "foreign key" in text or ("violates" in text and "missions" in text)

    __all__ = ["AuditSink", "PROVENANCE_FIELDS", "TOPIC_MODEL_ROUTED"]