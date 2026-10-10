"""Bus de eventos del CORE: uno por proceso, con politica de saturacion explicita.

## Por qué existe una política y no un `pass`

La version anterior hacia `except queue.Full: pass`. Medido: 150 eventos publicados con
un suscriptor sin consumir dejaban 100 en la cola y **50 perdidos sin contador ni log**.

Eso no era un detalle de implementación, porque `ModelRouter._audit()` **sólo publica al
bus** (el router no tiene `audit_repo`) y el `AuditSink` es el **único** consumidor que
persiste en `audit_log`. Una cola saturada no perdia un evento de interfaz: perdia el
registro de auditoria de una decision de modelo, en silencio. `docs/SECURITY.md` exige que
toda accion relevante quede registrada.

Por eso cada suscriptor declara si su entrega es **lossless** o **best-effort**:

- `critical=True` (auditoría): cola sin cota, porque perder un registro no es una opción
  aceptable. La memoria no crece sin límite en la práctica porque el consumidor nunca se
  bloquea: `AuditSink.handle()` captura el fallo de escritura y sigue consumiendo, así
  que la cola sólo crece mientras la base de datos va más lenta que la producción. Si aun
  así se acumula, el propio sink avisa por log (`_WARN_PENDING`), de modo que un
  atasco es **visible** y no silencioso.
- Best-effort (SSE, Self Model): cola acotada a 100. Si se llena, **deja de perder en
  silencio**: se cuenta en `dropped_events` y se registra en el log.

En ambos casos el orden de publicación se conserva: las colas son FIFO.
"""

from __future__ import annotations

import asyncio
import logging
import queue
from collections import deque
from typing import Any

LOGGER = logging.getLogger("alexis.events")

#: Tope de la cola de un suscriptor best-effort. Es memoria de trabajo, no un registro.
BEST_EFFORT_MAXSIZE = 100

#: Tamaño del buffer de diagnóstico `EventBus.events`. **NO es el registro de
#: auditoría**: eso vive en la tabla `audit_log`, que es durable y no se acota aquí.
#: Acotarlo sólo evita que un proceso de larga vida acumule eventos sin límite.
DIAGNOSTIC_BUFFER = 1000


class EventBus:
    def __init__(self, *, diagnostic_buffer: int = DIAGNOSTIC_BUFFER):
        self.events: deque[dict[str, Any]] = deque(maxlen=diagnostic_buffer)
        #: Total publicado desde el arranque. Lo que sale del buffer sigue siendo
        #: contable: acotar el buffer no hace desaparecer nada en silencio.
        self.published_count = 0
        #: Eventos que un suscriptor best-effort no pudo recibir. Con esto, una pérdida
        #: es un número observable, no un hueco en un `pass`.
        self.dropped_events = 0
        self._closed = False
        self._sync_subs: list[queue.Queue] = []
        self._async_subs: list[asyncio.Queue] = []

    # ------------------------------------------------------------------ #
    # Publicación
    # ------------------------------------------------------------------ #

    async def publish(self, topic: str, payload):
        if self._closed:
            # Publicar en un bus cerrado aceptaría el evento sin destino posible: el
            # registro de auditoría desaparecería sin dejar hueco. Es un error de
            # ciclo de vida, no algo que deba tragarse.
            raise RuntimeError(
                f"EventBus cerrado: no se puede publicar {topic!r}. "
                "Un bus cerrado no es un bus que ignora eventos."
            )
        item = {"topic": topic, "payload": payload}
        self.published_count += 1
        self.events.append(item)
        for sub in list(self._sync_subs):
            try:
                sub.put_nowait(item)
            except queue.Full:
                self._dropped(topic, "sync")
        for sub in list(self._async_subs):
            try:
                sub.put_nowait(item)
            except asyncio.QueueFull:
                self._dropped(topic, "async")
        return item

    def _dropped(self, topic: str, kind: str) -> None:
        self.dropped_events += 1
        LOGGER.warning(
            "event_bus: un suscriptor %s no pudo recibir %r (cola llena). "
            "Total descartado: %d. El suscriptor de auditoría no debe tener "
            "semántica best-effort: suscríbete con critical=True.",
            kind,
            topic,
            self.dropped_events,
        )

    # ------------------------------------------------------------------ #
    # Suscripción
    # ------------------------------------------------------------------ #

    def subscribe(self, *, critical: bool = False) -> queue.Queue:
        """Suscripción síncrona. `critical=True` → entrega sin pérdida (cola sin cota)."""
        sub = queue.Queue(maxsize=0 if critical else BEST_EFFORT_MAXSIZE)
        sub.critical = critical  # type: ignore[attr-defined]
        self._sync_subs.append(sub)
        return sub

    def unsubscribe(self, sub: queue.Queue) -> None:
        """Desregistra un consumidor sync para permitir conexiones SSE limpias."""
        try:
            self._sync_subs.remove(sub)
        except ValueError:
            pass

    def subscribe_async(self, *, critical: bool = False) -> asyncio.Queue:
        """Suscripción async. `critical=True` → entrega sin pérdida (cola sin cota)."""
        sub = asyncio.Queue(maxsize=0 if critical else BEST_EFFORT_MAXSIZE)
        sub.critical = critical  # type: ignore[attr-defined]
        self._async_subs.append(sub)
        return sub

    def unsubscribe_async(self, sub: asyncio.Queue) -> None:
        """Desregistra un consumidor async para permitir reinicios limpios."""
        try:
            self._async_subs.remove(sub)
        except ValueError:
            pass

    # ------------------------------------------------------------------ #
    # Cierre
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        """Cierra el bus. Publicar después lanza `RuntimeError`, no se ignora.

        No se vacían las colas: lo pendiente corresponde a sus consumidores, que son
        quienes saben si lo han durablemente persistido. Descartarlo aquí sí sería una
        pérdida silenciosa.
        """
        self._closed = True

    @property
    def is_closed(self) -> bool:
        return self._closed

    def pending_async(self, sub: asyncio.Queue) -> int:
        """Eventos aún sin consumir por un suscriptor async. Satélite de diagnóstico."""
        return sub.qsize()


__all__ = [
    "BEST_EFFORT_MAXSIZE",
    "DIAGNOSTIC_BUFFER",
    "EventBus",
]