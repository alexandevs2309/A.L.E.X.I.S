"""Sincronización del Self Model con los eventos reales del runtime.

El Self Model NO es un JSON estático: esta clase consume el EventBus durable y,
ante cada evento, re-deriva las zonas desde los objetos vivos del runtime
(resolvedores inyectados). Solo presencias transitorias (listening/speaking/
reflecting/evaluating/replanning/recovering) se marcan por evento real.
"""

import asyncio
import logging

from alexis.self.model import SelfModel

LOGGER = logging.getLogger("alexis.self.sync")


class SelfModelSync:
    #: Cuántas lecciones verificadas recuerda el Self Model. Acotado a propósito: el
    #: aprendizaje es un borde del Self Model, no su memoria (P0 §5.6.7).
    LESSON_HISTORY = 5

    def __init__(self, model, resolve_mission, aux=None, persistence=None, *, on_persist=None):
        self.model = model
        self.resolve_mission = resolve_mission  # () -> Mission | None
        self.aux = aux or (lambda: {})  # () -> dict(tools, commitments, lessons, memory_items, verification)
        self._sub = None
        self._task = None
        self._current_action = None
        #: Lecciones que autorizó la frontera de aprendizaje (P0 §5.6.5). Acumuladas por
        #: evento `mission.experience`; aquí no se decide nada, sólo se recuerda lo ya
        #: verificado. `self.aux()` es quien las inyecta en `update()`.
        self.lessons: list[str] = []
        #: CORE-07: fuente durable del estado aprendido (opcional). Si está, cada
        #: registro nuevo se guarda para que sobreviva a un reinicio. No se inventa
        #: nada: sin repositorio, el comportamiento es el de siempre (sólo memoria).
        self.persistence = persistence
        #: Hook opcional para que quien compone sepa cuándo hubo algo que persistir.
        self.on_persist = on_persist

    def apply_event(self, topic, payload):
        antes = (
            len(self.lessons),
            len(self.model.observations_about_self),
            len(self.model.reflections),
        )
        if topic == "presence.listening":
            self.model.set_transient(listening=True, speaking=False, reflecting=False)
        elif topic == "presence.speaking":
            self.model.set_transient(speaking=True, listening=False, reflecting=False)
        elif topic == "self.reflected":
            self.model.set_transient(listening=False, speaking=False, reflecting=True)
            text = payload.get("text") if isinstance(payload, dict) else str(payload or "")
            self.model.add_reflection(text)
        elif topic == "mission.experience":
            self._apply_experience(payload if isinstance(payload, dict) else {})
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
        self._persist_if_learned(antes, topic)

    def _persist_if_learned(self, antes, topic: str) -> None:
        """Vuelca a la fuente durable SOLO si este evento añadió algo aprendido.

        Se compara antes/después para no escribir en cada evento (presencia, transitorios)
        que no aporta conocimiento: persistir es caroso y la base debe reflejar lo que
        ALEXIS sabe, no cada latido del bus.
        """
        if self.persistence is None:
            return
        ahora = (
            len(self.lessons),
            len(self.model.observations_about_self),
            len(self.model.reflections),
        )
        if ahora == antes:
            return
        self._schedule_persist(topic)

    def _schedule_persist(self, topic: str) -> None:
        loop = getattr(self, "_loop", None)
        if loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._persist_now(topic), loop)

    async def _persist_now(self, topic: str) -> None:
        try:
            await self.persistence.save(
                lessons=self.lessons,
                observations=self.model.observations_about_self,
                reflections=self.model.reflections,
                mission_id=self._mission_id(),
            )
            if self.on_persist is not None:
                self.on_persist(topic)
        except Exception as exc:  # noqa: BLE001 — no recordar no puede tumbar el runtime
            LOGGER.warning(
                "no se pudo persistir el estado aprendido del Self Model: %s", exc
            )

    def _mission_id(self):
        mission = None
        try:
            mission = self.resolve_mission()
        except Exception:  # noqa: BLE001 — la provenance es opcional por diseño
            mission = None
        return getattr(mission, "id", None) if mission is not None else None

    def _apply_experience(self, payload: dict):
        """El Self Model recuerda la lección que la frontera autorizó (P0 §5.6.7).

        Sólo entra si `can_teach` es True: el Self Model no aprende de una misión sin
        verificar. Aquí no se decide nada; la frontera ya autorizó o vetó esta lección.
        """
        if not payload.get("can_teach"):
            return
        lesson = str(payload.get("lesson") or "").strip()
        if not lesson:
            return
        objective = str(payload.get("objective") or "").strip()
        entry = f"{objective}: {lesson}" if objective and not lesson.startswith(objective) else lesson
        self.lessons = ([entry] + [x for x in self.lessons if x != entry])[: self.LESSON_HISTORY]

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
        self._loop = loop
        self._task = asyncio.run_coroutine_threadsafe(self._consume(), loop)

    async def _consume(self):
        while True:
            item = await self._sub.get()
            self.apply_event(item.get("topic", ""), item.get("payload"))