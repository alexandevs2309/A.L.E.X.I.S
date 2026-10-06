import asyncio
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
