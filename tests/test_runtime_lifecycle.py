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
