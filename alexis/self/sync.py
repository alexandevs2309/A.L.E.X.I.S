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
        elif topic == "cognition.step":
            self._apply_cognition(payload if isinstance(payload, dict) else {})
        elif topic == "mission.approval_required":
            self.model.set_transient(listening=False, speaking=False, reflecting=False, flag=None)
        elif topic.startswith("mission.") or topic == "presence.idle":
            self.model.set_transient(listening=False, speaking=False, reflecting=False, flag=None)
        self.model.update(self.resolve_mission(), current_action=self._current_action, **self.aux())

    def _apply_cognition(self, payload: dict):
        """El Self Model también refleja lo que hizo el bucle cognitivo.

        No inventa actividad: solo refleja decisiones, fallos, diagnósticos y
        procedencia real que ya vienen en el evento `cognition.step`.
        """
        action = payload.get("action")
        decision = payload.get("decision") or {}
        step = decision.get("step_id")
        flag = None

        if step:
            self._current_action = step
        if action == "replan":
            flag = "replanning"
            self.model.note_self_observation(
                f"cambié de estrategia tras: {payload.get('diagnosis') or payload.get('error') or 'un fallo'}"
            )
        elif action in ("ask_user", "abort", "finish", "verify"):
            if action == "ask_user":
                self.model.note_self_observation(f"pregunté al usuario: {payload.get('question')}")
            elif action == "abort":
                self.model.note_self_observation(f"abandoné sin afirmar éxito: {decision.get('rationale')}")
            elif action == "finish":
                self.model.note_self_observation("terminé con verificación pasada")
        elif payload.get("success") is False:
            flag = "recovering"
            self.model.note_self_observation(
                f"el paso '{step or 'cognitivo'}' falló: {payload.get('error')}"
            )
        elif payload.get("success") is True and step:
            self.model.note_self_observation(f"el paso '{step}' terminó bien")

        outcome = decision.get("cognition_outcome")
        if outcome and outcome not in ("real", "none"):
            self.model.note_self_observation(
                f"decidí sin razonamiento real del modelo (procedencia: {outcome})"
            )

        self.model.set_transient(
            listening=False, speaking=False, reflecting=False, flag=flag
        )

    def attach(self, bus, loop):
        self._sub = bus.subscribe_async()
        self._task = asyncio.run_coroutine_threadsafe(self._consume(), loop)

    async def _consume(self):
        while True:
            item = await self._sub.get()
            self.apply_event(item.get("topic", ""), item.get("payload"))