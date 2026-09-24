"""Sincronización del Self Model con los eventos reales del runtime.

El Self Model NO es un JSON estático: esta clase consume el EventBus durable y,
ante cada evento, re-deriva las zonas desde los objetos vivos del runtime
(resolvedores inyectados). Solo presencias transitorias (listening/speaking/
reflecting/evaluating/replanning/recovering) se marcan por evento real.
"""

import asyncio

from alexis.self.model import SelfModel


class SelfModelSync:
    def __init__(self, model, resolve_mission, aux=None):
        self.model = model
        self.resolve_mission = resolve_mission  # () -> Mission | None
        self.aux = aux or (lambda: {})  # () -> dict(tools, commitments, lessons, memory_items, verification)
        self._sub = None
        self._task = None
        self._current_action = None

    def apply_event(self, topic, payload):
        if topic == "presence.listening":
            self.model.set_transient(listening=True, speaking=False, reflecting=False)
        elif topic == "presence.speaking":
            self.model.set_transient(speaking=True, listening=False, reflecting=False)
        elif topic == "self.reflected":
            self.model.set_transient(listening=False, speaking=False, reflecting=True)
            text = payload.get("text") if isinstance(payload, dict) else str(payload or "")
            self.model.add_reflection(text)
        elif topic == "mission.step_started":
            self._current_action = payload if isinstance(payload, str) else (payload or {}).get("step")
            self.model.set_transient(listening=False, speaking=False, reflecting=False, flag=None)
        elif topic == "mission.step_completed":
            step = payload.get("step") if isinstance(payload, dict) else payload
            self.model.set_transient(listening=False, speaking=False, reflecting=False, flag=None)
            success = payload.get("success") if isinstance(payload, dict) else True
            if not success:
                self.model.note_self_observation(f"paso '{step}' falló: {payload.get('error')}")
        elif topic == "policy.evaluated":
            self.model.set_transient(listening=False, speaking=False, reflecting=False, flag="evaluating")
            rule = payload.get("matched_rule") if isinstance(payload, dict) else None
            if rule:
                self.model.note_self_observation(f"política evaluó paso '{payload.get('step')}' → regla {rule}")
        elif topic == "mission.replanning":
            self.model.set_transient(listening=False, speaking=False, reflecting=False, flag="replanning")
        elif topic == "mission.approval_required":
            self.model.set_transient(listening=False, speaking=False, reflecting=False, flag=None)
        elif topic.startswith("mission.") or topic == "presence.idle":
            self.model.set_transient(listening=False, speaking=False, reflecting=False, flag=None)
        self.model.update(self.resolve_mission(), current_action=self._current_action, **self.aux())

    def attach(self, bus, loop):
        self._sub = bus.subscribe_async()
        self._task = asyncio.run_coroutine_threadsafe(self._consume(), loop)

    async def _consume(self):
        while True:
            item = await self._sub.get()
            self.apply_event(item.get("topic", ""), item.get("payload"))