"""P0-1: ningún evento de auditoría required puede perderse en silencio.

La demostración previa: 150 `publish` con un suscriptor sin consumir dejaban 100 en la
cola y **50 perdidos sin contador ni log**. Como `ModelRouter._audit()` sólo publica al
bus (el router no tiene `audit_repo`) y el `AuditSink` es el ÚNICO consumidor que
persiste en `audit_log`, esa pérdida es la pérdida del registro de auditoría de una
decisión de modelo. Contradice `docs/SECURITY.md`: *"Every consequential action should
record…"*.

Estas pruebas usan el `AuditSink` REAL y el repositorio REAL. No sustituyen el bus ni la
persistencia por dobles: el defecto estaba precisamente en que la saturación de la cola
era invisible, así que un doble no lo habría detectado.
"""

from __future__ import annotations

import asyncio

import pytest

from alexis.events.bus import EventBus
from alexis.models.audit_sink import TOPIC_MODEL_ROUTED, AuditSink
from alexis.storage.repositories import AuditRepository


def _payload(i: int) -> dict:
    """Payload con la forma que `AuditSink.handle()` espera."""
    return {
        "task": "reason",
        "provider": "echo",
        "model": "echo",
        "outcome": "real",
        "fallback_used": False,
        "chain": ["echo"],
        "latency_ms": 1,
        "cost_usd": 0.0,
        "correlation": {"mission_id": None, "seq": i},
    }


async def _drenar(db, sink: AuditSink, hasta: int, timeout: float = 20.0):
    """Espera a que el sink haya persistido `hasta` filas, o agota el tiempo."""
    limite = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < limite:
        await asyncio.sleep(0.01)
        filas = await db.fetch(
            "SELECT 1 FROM audit_log WHERE event = %(e)s", {"e": TOPIC_MODEL_ROUTED}
        )
        if len(filas) >= hasta:
            return True
    return False


# --------------------------------------------------------------------- #
# 1. Saturación con consumidor activo
# --------------------------------------------------------------------- #


async def test_saturacion_no_pierde_eventos_de_auditoria(db):
    """150 eventos publicados mientras el sink escribe: los 150 deben persistirse.

    Es la condición exacta que perdió 50. El sink está VIVO y consumiendo; por eso
    ninguna strategy de "descartar cuando el consumidor va lento" es aceptable aquí.
    """
    bus = EventBus()
    repo = AuditRepository(db)
    sink = AuditSink(repo)
    sink.attach(bus, asyncio.get_running_loop())
    try:
        for i in range(150):
            await bus.publish(TOPIC_MODEL_ROUTED, _payload(i))

        assert await _drenar(db, sink, 150), "el sink no alcanzó 150 filas a tiempo"

        filas = await repo.list(limit=500)
        assert len(filas) == 150, f"publicados 150, persistidos {len(filas)}"
    finally:
        await sink.stop()


async def test_no_hay_perdidas_silenciosas_observables(db):
    """Si algo se perdiera, tiene que haber un contador o un aviso. Nada de `pass`."""
    bus = EventBus()
    repo = AuditRepository(db)
    sink = AuditSink(repo)
    sink.attach(bus, asyncio.get_running_loop())
    try:
        for i in range(150):
            await bus.publish(TOPIC_MODEL_ROUTED, _payload(i))
        assert await _drenar(db, sink, 150)
    finally:
        await sink.stop()

    perdidas = getattr(bus, "dropped_events", 0)
    assert perdidas == 0, f"el bus registró {perdidas} pérdidas; no deben ser silenciosas"


# --------------------------------------------------------------------- #
# 2. Orden
# --------------------------------------------------------------------- #


async def test_los_eventos_llegan_en_el_mismo_orden(db):
    """`audit_log` es un registro de auditoría: el orden de publicación es un dato."""
    bus = EventBus()
    repo = AuditRepository(db)
    sink = AuditSink(repo)
    sink.attach(bus, asyncio.get_running_loop())
    try:
        for i in range(60):
            await bus.publish(TOPIC_MODEL_ROUTED, _payload(i))
        assert await _drenar(db, sink, 60)

        # `details->'correlation'->>'seq'` reconstruye el orden de publicación.
        filas = await db.fetch(
            "SELECT (details->'correlation'->>'seq')::int AS seq "
            "FROM audit_log WHERE event = %(e)s ORDER BY id ASC",
            {"e": TOPIC_MODEL_ROUTED},
        )
        seqs = [f["seq"] for f in filas]
        assert seqs == sorted(seqs), "los eventos se reordenaron"
        assert seqs == list(range(60))
    finally:
        await sink.stop()


# --------------------------------------------------------------------- #
# 3. Cierre: qué pasa con lo pendiente
# --------------------------------------------------------------------- #


async def test_al_cerrar_no_se_pierde_lo_ya_aceptado(db):
    """Publicar y cerrar de inmediato no puede descartar en silencio lo ya aceptado.

    Antes, `stop()` cancelaba la tarea y descartaba la cola. Un `await` de la
    persistencia durante el apagado es lo que convierte "aceptado" en "durable".
    """
    bus = EventBus()
    repo = AuditRepository(db)
    sink = AuditSink(repo)
    sink.attach(bus, asyncio.get_running_loop())
    for i in range(40):
        await bus.publish(TOPIC_MODEL_ROUTED, _payload(i))
    await sink.stop()  # sin dormir: todo sigue pendiente

    persistidas = await repo.list(limit=500)
    assert len(persistidas) == 40, (
        f"al cerrar había 40 eventos aceptados y se persistieron {len(persistidas)}"
    )


# --------------------------------------------------------------------- #
# 4. Buffer de diagnóstico: acotado y explícito
# --------------------------------------------------------------------- #


async def test_el_buffer_de_eventos_esta_acotado():
    """`EventBus.events` es memoria de diagnóstico, NO el registro de auditoría.

    Acotarlo no puede implicar pérdida de auditoría: eso vive en `audit_log`.
    """
    bus = EventBus()
    for i in range(5000):
        await bus.publish("t", {"i": i})
    assert len(bus.events) <= 1000, f"buffer sin cota: {len(bus.events)}"
    # Lo que sale del buffer no se pierde en silencio: queda el total acumulado.
    assert bus.published_count == 5000


async def test_publicar_despues_de_cerrar_es_visible_no_mudo(db):
    """Tras `close()`, publicar no puede aceptarse en silencio."""
    bus = EventBus()
    bus.close()
    with pytest.raises(RuntimeError):
        await bus.publish("t", {"i": 1})