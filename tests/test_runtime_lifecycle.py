import asyncio
import concurrent.futures
from types import SimpleNamespace

import pytest

from alexis.events.bus import EventBus
from alexis.models.audit_sink import AuditSink
from alexis.self.sync import SelfModelSync


@pytest.mark.asyncio
async def test_event_bus_async_subscription_can_be_removed():
    bus = EventBus()
    sub = bus.subscribe_async()
    assert sub in bus._async_subs

    bus.unsubscribe_async(sub)
    bus.unsubscribe_async(sub)

    assert sub not in bus._async_subs


def test_event_bus_sync_subscription_can_be_removed():
    bus = EventBus()
    sub = bus.subscribe()
    assert sub in bus._sync_subs

    bus.unsubscribe(sub)
    bus.unsubscribe(sub)

    assert sub not in bus._sync_subs


@pytest.mark.asyncio
async def test_self_model_sync_stop_removes_subscription_and_task():
    bus = EventBus()
    sync = SelfModelSync(SimpleNamespace(
        observations_about_self=[],
        reflections=[],
        update=lambda *args, **kwargs: None,
    ), lambda: None)

    sync.attach(bus, asyncio.get_running_loop())
    await asyncio.sleep(0)

    assert sync._task is not None
    assert sync._sub in bus._async_subs

    await sync.stop()

    assert sync._task is None
    assert sync._sub is None
    assert not bus._async_subs


class _AuditRepo:
    async def record(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_audit_sink_stop_removes_subscription_and_task():
    bus = EventBus()
    sink = AuditSink(_AuditRepo()).attach(bus, asyncio.get_running_loop())
    await asyncio.sleep(0)

    assert sink._task is not None
    assert sink._sub in bus._async_subs

    await sink.stop()
    await sink.stop()

    assert sink._task is None
    assert sink._sub is None
    assert not bus._async_subs



@pytest.mark.asyncio
async def test_stop_services_stops_worker_observers_and_database():
    from apps.demo.app import stop_services

    loop = asyncio.get_running_loop()
    stopped = []

    class _Worker:
        def __init__(self):
            self.event = asyncio.Event()

        def stop(self):
            stopped.append("worker")
            self.event.set()

    worker = _Worker()

    async def worker_loop():
        await worker.event.wait()

    worker_task = asyncio.run_coroutine_threadsafe(worker_loop(), loop)

    class _Service:
        async def stop(self):
            stopped.append("service")

    class _DB:
        async def close(self):
            stopped.append("db")

    rt = SimpleNamespace(
        worker=worker,
        extras={
            "services_started": True,
            "worker_task": worker_task,
            "self_sync": _Service(),
            "audit_sink": _Service(),
        },
        storage={"db": _DB()},
    )

    result = await stop_services(rt)

    assert result == {"stopped": True}
    assert stopped == ["worker", "service", "service", "db"]
    assert rt.extras["services_started"] is False
    assert rt.extras["worker_task"] is None

    assert await stop_services(rt) == {
        "stopped": False,
        "reason": "services_not_started",
    }


# ---------------------------------------------------------------------- #
# P1-3: `start_services()` es idempotente
# ---------------------------------------------------------------------- #


def _runtime_minimo():
    """Runtime con la forma que `start_services`/`stop_services` tocan.

    Se construye aquí, y no con el runtime real del E2E, porque `stop_services` **cierra
    la base de datos**: parar los servicios de un runtime compartido deja al resto de los
    tests sinalmacén de misiones, y el daño se manifiesta lejos de su causa.
    """
    from apps.demo import app as app_module

    class _DB:
        async def close(self):
            pass

    class _Service:
        def __init__(self):
            self.stopped = False

        async def stop(self):
            self.stopped = True

    rt = SimpleNamespace(
        worker=None,
        extras={
            "services_started": True,
            # Una `Future` real: `stop_services` la cancela/espera como a una real.
            "worker_task": concurrent.futures.Future(),
            "self_sync": _Service(),
            "audit_sink": _Service(),
        },
        storage={"db": _DB()},
        events=EventBus(),
        loop=None,
        runtime=None, tools=None, running=None, state=None, self_model=None,
        enabled_capabilities=[],
    )
    return rt, app_module


def test_arrancar_sobre_servicios_ya_encendidos_no_crea_duplicados():
    """La guarda evita un segundo worker, sync y audit sink.

    Sin ella, cada llamada añadía otro consumidor del mismo bus. El runtime ya llega
    encendido aquí, que es exactamente la condición que se quiere proteger.
    """
    rt, app_module = _runtime_minimo()

    resultado = app_module.start_services(rt)

    assert resultado["already_started"] is True
    # Lo que estaba en marcha sigue siendo lo único en marcha.
    assert rt.extras["worker_task"] is resultado["worker_task"]
    assert rt.extras["services_started"] is True


def test_arrancar_tres_veces_es_la_misma_operacion():
    rt, app_module = _runtime_minimo()

    seen = [app_module.start_services(rt) for _ in range(3)]

    assert [r["already_started"] for r in seen] == [True, True, True]
    assert len({id(r["worker_task"]) for r in seen}) == 1


# `stop_services()` cierra la base de datos y `start_services()` no la reabre: parar es
# TERMINAL para un runtime, no una pausa. Por eso aquí NO hay un test de "parar y volver a
# arrancar" sobre el mismo objeto: no es una operación soportada, y una prueba que la
# fingiera daría una garantía falsa. La terminalidad está documentada en `start_services`
# y la fixture `oficial` construye un runtime nuevo, que es el camino real.
