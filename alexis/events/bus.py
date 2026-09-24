import asyncio
import queue


class EventBus:
    def __init__(self):
        self.events = []
        self._sync_subs: list[queue.Queue] = []
        self._async_subs: list[asyncio.Queue] = []

    async def publish(self, topic: str, payload):
        item = {"topic": topic, "payload": payload}
        self.events.append(item)
        for sub in list(self._sync_subs):
            try:
                sub.put_nowait(item)
            except queue.Full:
                pass
        for sub in list(self._async_subs):
            try:
                sub.put_nowait(item)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> queue.Queue:
        sub = queue.Queue(maxsize=100)
        self._sync_subs.append(sub)
        return sub

    def subscribe_async(self) -> asyncio.Queue:
        sub = asyncio.Queue(maxsize=100)
        self._async_subs.append(sub)
        return sub
