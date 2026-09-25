"""Conversación como puerta principal (slice F2.3) sobre el flujo de F0/F1.

Reglas que aplica (P3.1, P3.2, P3.3):

- `IntentKind.TASK` → crea `Mission` y sigue el flujo existente (cola/worker/gates/
  policy/verificación). No se toca el runtime.
- Cualquier otra kind (`greeting`, `capability_query`, `self_query`, `meta_query`…) →
  responde **sin crear misión**.

Lo que este módulo NO hace: autorizar capabilities. La propuesta del modelo viaja como
`CapabilityProposal` en el contexto de la misión, nunca como permiso; el envelope lo
construye la capa que ya lo hacía (`EnvelopeBuilder` en F2.5).
"""

import inspect
import json
import logging

from alexis.cognition.contracts import Intent, IntentKind, SelfBrief, UserReply
from alexis.models.provider import ModelOutcome

LOGGER = logging.getLogger("alexis.cognition.conversation")

TURN_STARTED = "conversation.turn_started"
TURN_FINISHED = "conversation.turn_finished"
INTENT_UNDERSTOOD = "cognition.intent_understood"
SELF_CONSULTED = "cognition.self_consulted"
REPLY_COMPOSED = "conversation.reply_composed"


class ConversationSession:
    """Recibe turnos del usuario y decide si hay misión o respuesta directa.

    Dependencias inyectadas: no conoce el demo, la API ni la cola. `create_mission` y
    `enqueue` son los mismos puntos de entrada que usa `POST /missions` hoy.
    """

    def __init__(
        self,
        *,
        classifier,
        self_model,
        bus=None,
        create_mission=None,
        enqueue=None,
        capability_registry=None,
    ):
        self.classifier = classifier
        self.self_model = self_model
        self.bus = bus
        self.create_mission = create_mission
        self.enqueue = enqueue
        self.capability_registry = capability_registry

    # ------------------------------------------------------------------ #

    def brief(self) -> SelfBrief:
        """SelfBrief desde el Self Model real (no datos inventados)."""
        if self.self_model is None:
            return SelfBrief()
        return SelfBrief.from_snapshot(self.self_model.snapshot())

    async def handle_turn(self, utterance: str, *, source: str = "user") -> UserReply:
        await self._publish(TURN_STARTED, {"source": source})
        brief = self.brief()
        await self._publish(SELF_CONSULTED, {"zones": len(brief.to_dict())})

        intent = await self.classifier.classify(utterance, brief)
        outcome = intent.model_meta.get("cognition_outcome") or (
            ModelOutcome.REAL.value if intent.model_meta.get("source") == "model" else ModelOutcome.DEGRADED.value
        )
        await self._publish(
            INTENT_UNDERSTOOD,
            {
                "kind": intent.kind.value,
                "source": intent.model_meta.get("source"),
                "cognition_outcome": outcome,
                "confidence": intent.confidence,
            },
        )

        if not intent.is_task:
            reply = self._direct_reply(intent, brief, outcome)
            await self._publish(
                REPLY_COMPOSED, {"kind": reply.kind, "cognition_outcome": outcome, "mission_id": None}
            )
            await self._publish(TURN_FINISHED, {"kind": intent.kind.value, "mission_id": None})
            return reply

        if self.create_mission is None or self.enqueue is None:
            reply = UserReply(
                text="Entendí que es una tarea, pero este canal no tiene runtime de misiones conectado.",
                kind="error",
                mission_id=None,
                cognition_outcome=outcome,
                degraded=outcome != ModelOutcome.REAL.value,
            )
            await self._publish(TURN_FINISHED, {"kind": intent.kind.value, "mission_id": None})
            return reply

        mission = self.create_mission(intent)
        await self._publish(TURN_FINISHED, {"kind": intent.kind.value, "mission_id": mission.id})
        if self.enqueue is not None:
            result = self.enqueue(mission)
            if inspect.isawaitable(result):
                await result
        return UserReply(
            text=self._task_acknowledgement(intent, outcome),
            kind="answer",
            mission_id=mission.id,
            open_questions=["¿Quieres que proceda?"] if intent.needs_clarification else [],
            cognition_outcome=outcome,
            degraded=outcome != ModelOutcome.REAL.value,
        )

    # ------------------------------------------------------------------ #

    def _direct_reply(self, intent: Intent, brief: SelfBrief, outcome: str) -> UserReply:
        """Respuesta sin misión. El texto es la experiencia principal (F2 §14)."""
        degraded = outcome != ModelOutcome.REAL.value
        if intent.kind is IntentKind.GREETING:
            identity = (brief.identity or {}).get("name", "ALEXIS")
            text = f"Hola. Soy {identity}. Dime qué necesitas y lo resolvemos."
        elif intent.kind is IntentKind.CAPABILITY_QUERY:
            text = self._capability_answer(brief)
        elif intent.kind is IntentKind.SELF_QUERY:
            answer = self.self_model.answer(intent.utterance) if self.self_model else None
            text = answer["answer"] if answer else "Ahora mismo estoy inactivo."
        elif intent.kind is IntentKind.META_QUERY:
            text = (
                "Soy ALEXIS. Converso, decido con un modelo qué hacer, elijo entre las "
                "capacidades que tengo habilitadas, lo ejecuto bajo política y verifico "
                "el resultado antes de dártlo por bueno. Lo que no puedo hacer no lo invento."
            )
        else:
            text = (
                "No he entendido la petición con suficiente claridad. ¿Me lo dices de otra "
                "forma, con el objetivo concreto?"
            )
        if degraded:
            text = f"{text}\n\n(Nota: esta respuesta se compuso sin un modelo real disponible.)"
        return UserReply(
            text=text,
            kind="question" if intent.needs_clarification else "answer",
            open_questions=[intent.ambiguity] if intent.needs_clarification and intent.ambiguity else [],
            cognition_outcome=outcome,
            degraded=degraded,
        )

    def _capability_answer(self, brief: SelfBrief) -> str:
        available = brief.available_capabilities
        if not available and self.capability_registry is not None:
            available = [s.id for s in self.capability_registry.enabled()]
        missing: list[str] = []
        if self.capability_registry is not None:
            missing = [s.id for s in self.capability_registry.specs() if s.status == "missing"]
        lines = ["Ahora mismo puedo hacer esto de verdad:"]
        lines.extend(f"  · {cid}" for cid in (available or ["(ninguna habilitada)"]))
        if missing:
            lines.append("Y estas son las que todavía NO puedo hacer (no están habilitadas):")
            lines.extend(f"  · {cid}" for cid in missing[:12])
            if len(missing) > 12:
                lines.append(f"  · … y {len(missing) - 12} más")
        return "\n".join(lines)

    def _task_acknowledgement(self, intent: Intent, outcome: str) -> str:
        base = f"Entendido: {intent.objective or intent.utterance}."
        if intent.requested_capabilities:
            base += f" Voy a necesitar: {', '.join(intent.requested_capabilities)}."
        base += " Lo paso por política antes de tocar nada."
        if outcome != ModelOutcome.REAL.value:
            base += " (Clasifiqué con reglas, no con un modelo real: no había provider utilizable.)"
        return base

    async def _publish(self, topic: str, payload: dict) -> None:
        if self.bus is None:
            return
        try:
            await self.bus.publish(topic, payload)
        except Exception as exc:  # noqa: BLE001 — publicar no puede romper el turno
            LOGGER.warning("conversation: no se pudo publicar %s: %s", topic, exc)


def conversation_snapshot(session: ConversationSession) -> str:
    """Estado serializable de la sesión (debug)."""
    classifier = session.classifier
    return json.dumps(
        {
            "has_router": getattr(classifier, "router", None) is not None,
            "has_model_classifier": getattr(classifier, "model", None) is not None,
            "registry_enabled": (
                [s.id for s in session.capability_registry.enabled()]
                if session.capability_registry
                else []
            ),
        },
        ensure_ascii=False,
    )


__all__ = ["ConversationSession", "conversation_snapshot"]
