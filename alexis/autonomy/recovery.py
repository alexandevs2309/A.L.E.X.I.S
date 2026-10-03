"""CORE-10 — Recuperación autónoma.

La regla que gobierna este módulo, y que el resto del sistema tiene que poder asumir:

    **CHECKPOINT ≠ VERDAD**

Un checkpoint dice *"esto es lo que ALEXIS creía que había ocurrido"*. Lo que ocurrió de
verdad lo dice el mundo, y sólo se puede preguntar ejecutando una observación o una
verificación. Por eso la recuperación tiene tres pasos y no dos:

    RESTORE → INSPECT WORLD → DECIDE

Si se falta el segundo, la recuperación es un replay con pasos: se vuelve a ejecutar lo que el
proceso alcanzó a hacer antes de morir, y para `fs.write` eso es escribir dos veces, y para
`fs.remove` es borrar algo que quizá alguien ya reemplazó.

Este módulo NO toca el World Model, ni la evidencia, ni el plan, ni la policy. Decide; el
runtime y el Core ejecutan. Y decide con dos piezas que ya existían:

- `CapabilitySpec.side_effects` del catálogo, para saber si una acción puede repetirse sin más
  (aquí no se codifica ninguna capability: se pregunta al catálogo).
- `_action_signature` del Core, para comparar lo que se hizo con lo que se iba a hacer.

Los seis estados de `ActionStatus` son la distinción que hace falta para no adivinar. La
ventana de incertidumbre —la acción pudo ejecutarse y el proceso morir antes de registrarlo— es
exactamente el caso que `POSSIBLY_EXECUTED` existe para nombrar, y es el único que, cuando hay
efectos secundarios, obliga a preguntar en vez de reintentar.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

#: Reutilizada del Core: la firma de una acción. Importarla (y no reimplementarla) garantiza
#: que "la misma acción" significa lo mismo aquí y en el filtro de replan.
from alexis.cognition.loop import _action_signature

#: Tope de intentos de recuperación por misión. Sin esto, recuperar → reintentar → replanar →
#: recuperar es un bucle infinito, y un sistema que no se puede recuperar tampoco es autónomo:
#: es un sistema que se cuelga.
DEFAULT_RECOVERY_BUDGET = 3


class ActionStatus(str, Enum):
    """Lo que se puede afirmar sobre una acción concreta tras un corte.

    No es un `bool`: la diferencia entre "no se ejecutó" y "no sé si se ejecutó" es
    exactamente la que separa un reintento de una pregunta al usuario.
    """

    #: Confirmadamente no llegó a ejecutarse. Reintentar es seguro.
    NOT_EXECUTED = "not_executed"
    #: Confirmadamente se ejecutó y terminó bien. No repetir.
    EXECUTED = "executed"
    #: Hay evidencia de que empezó pero no de que terminara. Reintentar puede duplicar.
    PARTIAL = "partial"
    #: Se ejecutó y se sabe que falló. Reintentar pasa por Policy/Gate otra vez.
    FAILED = "failed"
    #: No hay evidencia en ningún sentido: el corte cayó en la ventana de incertidumbre.
    UNKNOWN = "unknown"
    #: La evidencia es contradictoria: una observación dice que sí y otra que no.
    CONFLICTED = "conflicted"


class RecoveryDecision(str, Enum):
    """Qué hacer con una misión tras restaurar el estado. Explícito y exhaustivo."""

    #: El mundo dice que las acciones pendientes no ocurrieron: se siguen por el mismo plan.
    RESUME = "resume"
    #: El mundo cambió de forma que invalida el plan: se replanifica (PlanValidator + Policy).
    REPLAN = "replan"
    #: Hay un acción posiblemente ejecutada con efectos: no se decide solo.
    ASK_USER = "ask_user"
    #: Los límites de la misión se agotaron. Se dice por qué y se para.
    ABORT = "abort"
    #: El objetivo ya está satisfecho según evidencia observada.
    COMPLETE = "complete"


@dataclass
class LastKnownAction:
    """Lo que ALEXIS sabía de la última acción, para poder contrastarlo con el mundo.

    No es `step.completed`: es la información necesaria para decidir si repetir es seguro.
    `signature` es la que permite reconocerla; `has_side_effects` es la que decide si la
    repetición necesita confirmación.
    """

    step_id: str
    capability: str
    action: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    plan_generation: int = 0
    #: Resultado registrado antes del corte. `None` significa "no se registró": no es
    #: "funcionó", es que el proceso murió antes de escribirlo.
    recorded_status: str | None = None
    #: Qué se observe para contrastar. Es un objetivo (`path`), no un predicado, porque lo
    #: decide la evidencia y no el checkpoint.
    probe_path: str | None = None
    recorded_at: float = 0.0
    error: str = ""

    @property
    def signature(self) -> str:
        return _action_signature(_as_step(self))

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "capability": self.capability,
            "action": self.action,
            "args": dict(self.args),
            "plan_generation": self.plan_generation,
            "recorded_status": self.recorded_status,
            "probe_path": self.probe_path,
            "recorded_at": self.recorded_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "LastKnownAction | None":
        if not raw or not raw.get("step_id"):
            return None
        return cls(
            step_id=str(raw.get("step_id") or ""),
            capability=str(raw.get("capability") or ""),
            action=str(raw.get("action") or ""),
            args=dict(raw.get("args") or {}),
            plan_generation=int(raw.get("plan_generation") or 0),
            recorded_status=raw.get("recorded_status"),
            probe_path=raw.get("probe_path"),
            recorded_at=float(raw.get("recorded_at") or 0.0),
            error=str(raw.get("error") or ""),
        )


def _as_step(action: "LastKnownAction"):
    """Construye el `PlanStep` mínimo que la firma necesita, sin duplicar su lógica."""
    from alexis.contracts import PlanStep, RiskLevel

    return PlanStep(
        id=action.step_id,
        description=action.step_id,
        action=action.action or "execute",
        risk=RiskLevel.MEDIUM,
        agent="executor",
        capability=action.capability or None,
        args=dict(action.args or {}),
    )


@dataclass
class RecoveryState:
    """El contrato de recuperación (§3): qué se restauró, qué se inspeccionó y qué se decidió.

    Se persiste en `mission.context["recovery"]`, que ya viaja en el JSONB de `missions`. No
    hay tabla nueva: el estado de recovery es parte del estado de la misión, y separarlo
    obligaría a sincronizar dos almacenes que describen lo mismo.
    """

    mission_id: str
    #: Se incrementa en cada intento. Es el presupuesto (§15): cuando se agota, ABORT.
    attempt: int = 0
    reason: str = ""
    #: `Checkpoint` es un dataclass aparte, pero se conserva el enlace al que se restauró.
    checkpoint_step_index: int = 0
    #: Lo que se creía antes de mirar el mundo.
    believed_completed: list[str] = field(default_factory=list)
    #: Lo que el mundo dijo, después de mirar.
    observed: dict[str, Any] = field(default_factory=dict)
    #: `step_id -> ActionStatus`. La conclusión por acción.
    action_status: dict[str, str] = field(default_factory=dict)
    #: Acciones con efectos que no se pudieron confirmar. Si hay alguna y hay que decidir, van
    #: a `ask_user`.
    unconfirmed: list[str] = field(default_factory=list)
    decision: str = RecoveryDecision.RESUME.value
    decision_reason: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "attempt": self.attempt,
            "reason": self.reason,
            "checkpoint_step_index": self.checkpoint_step_index,
            "believed_completed": list(self.believed_completed),
            "observed": dict(self.observed),
            "action_status": dict(self.action_status),
            "unconfirmed": list(self.unconfirmed),
            "decision": self.decision,
            "decision_reason": self.decision_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "RecoveryState | None":
        if not raw or not raw.get("mission_id"):
            return None
        return cls(
            mission_id=str(raw["mission_id"]),
            attempt=int(raw.get("attempt") or 0),
            reason=str(raw.get("reason") or ""),
            checkpoint_step_index=int(raw.get("checkpoint_step_index") or 0),
            believed_completed=list(raw.get("believed_completed") or []),
            observed=dict(raw.get("observed") or {}),
            action_status=dict(raw.get("action_status") or {}),
            unconfirmed=list(raw.get("unconfirmed") or []),
            decision=str(raw.get("decision") or RecoveryDecision.RESUME.value),
            decision_reason=str(raw.get("decision_reason") or ""),
            created_at=float(raw.get("created_at") or 0.0),
            updated_at=float(raw.get("updated_at") or 0.0),
        )


class RecoveryManager:
    """Decide cómo continuar una misión cuyo proceso desapareció a mitad.

    Reemplaza al placeholder de seis líneas que no decidía nada. Todo lo que hace aquí es
    determinista y comprobable: recibe lo que se restauró, recibe lo que el mundo observó, y
    devuelve una decisión. No ejecuta nada y no toca el World Model: quien observa es el
    runtime, con las herramientas reales.
    """

    def __init__(self, catalog=None, *, max_attempts: int = DEFAULT_RECOVERY_BUDGET):
        self.catalog = catalog
        self.max_attempts = max_attempts
        #: `step_id -> capability`, para que `decide()` sepa si los efectos importan.
        self._capabilities: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # Efectos secundarios: se pregunta al catálogo, no se codifica aquí
    # ------------------------------------------------------------------ #

    def has_side_effects(self, capability: str) -> bool:
        """¿Esta capability puede cambiar el mundo?

        Se lee de `CapabilitySpec.side_effects`. Si no hay catálogo, se asume que SÍ tiene
        efectos: ante la duda se trata como acción que no se repite sola. Un false negativo
        (creer que algo es inocuo cuando no lo es) es el error caro; el otro sólo cuesta una
        pregunta de más.
        """
        if not capability or self.catalog is None:
            return True
        try:
            spec = self.catalog.get(capability)
        except Exception:  # noqa: BLE001 — capability desconocida = efecto desconocido
            return True
        return bool(getattr(spec, "side_effects", True))

    # ------------------------------------------------------------------ #
    # Qué se puede afirmar sobre una acción
    # ------------------------------------------------------------------ #

    def assess(
        self,
        action: LastKnownAction,
        observation: dict[str, Any] | None,
    ) -> ActionStatus:
        """Contradice lo que se creía con lo que el mundo observó.

        `observation` es lo que la herramienta de verdad devolvió al inspeccionar. `None` o
        `{}` significa que no se obtuvo nada, y eso NO es "no ocurrió": es `UNKNOWN`.
        """
        if observation is None:
            return ActionStatus.UNKNOWN

        # Evidencia contradictoria: el mundo dijo una cosa y el checkpoint otra, y el
        # checkpoint no manda. Se nombra como tal para que quien decida lo vea.
        believed_executed = observation.get("believed_executed")
        observed_exists = observation.get("exists")
        if believed_executed is True and observed_exists is False:
            return ActionStatus.CONFLICTED

        # Lo que el proceso llegó a registrar. Es dato, no autoridad: si contradice al mundo,
        # `CONFLICTED` ya lo ha marcado antes.
        recorded = (action.recorded_status or "").strip().lower()
        if recorded == "executed" and observed_exists is False:
            return ActionStatus.CONFLICTED
        if recorded in ("failed", "error"):
            return ActionStatus.FAILED

        if observed_exists is True:
            # El mundo dice que está. Da igual que el proceso muriera antes de registrarlo:
            # `recorded_status == "unknown"` significa "no se escribió el resultado", NO
            # "la acción se quedó a medias". Confundir las dos cosas convertía cada
            # escritura correcta en un PARTIAL, que con efectos bloquea y obliga a preguntar
            # por algo que ya está hecho.
            #
            # PARTIAL sólo existe para evidencia POSITIVA de ejecución incompleta.
            if observation.get("partial") is True:
                return ActionStatus.PARTIAL
            return ActionStatus.EXECUTED

        if observed_exists is False:
            # El mundo lo miró y no está. Eso sí es una afirmación: no ocurrió.
            return ActionStatus.NOT_EXECUTED

        # Hay observación pero sin el campo que responde ("exists" ausente).
        return ActionStatus.UNKNOWN

    # ------------------------------------------------------------------ #
    # La decisión
    # ------------------------------------------------------------------ #

    def decide(
        self,
        state: RecoveryState,
        *,
        goal_satisfied: bool = False,
        plan_still_valid: bool = True,
    ) -> RecoveryDecision:
        """Qué hacer, y por qué.

        El orden importa y no es arbitrario: primero lo que protege (preguntar), después lo que
        evita trabajo inútil (completar), y sólo al final lo optimista (reanudar). Si se
        invirtiera, una acción destructiva sin confirmar se "reanudaría" sin preguntar nunca.
        """
        # 1. Presupuesto agotado. Un sistema que se cuelga recuperar no es autónomo (§15).
        if state.attempt >= self.max_attempts:
            state.decision = RecoveryDecision.ABORT.value
            state.decision_reason = (
                f"presupuesto de recuperación agotado ({state.attempt}/{self.max_attempts}): "
                f"reintentar otra vez no cambiaría el mundo"
            )
            return RecoveryDecision.ABORT

        # 2. Acción con efectos que no se pudo confirmar: preguntar. Nunca repetir a ciegas.
        blocking = [
            step_id
            for step_id in state.unconfirmed
            if self.has_side_effects(self.capability_of(step_id))
        ]
        if blocking:
            state.decision = RecoveryDecision.ASK_USER.value
            state.decision_reason = (
                f"no se puede confirmar si se ejecutó: {', '.join(sorted(blocking))}. "
                f"Repetir a ciegas una acción con efectos es exactamente lo que no se hace"
            )
            return RecoveryDecision.ASK_USER

        # 3. El objetivo ya está，并根据 evidencia observada. No repetir trabajo.
        if goal_satisfied:
            state.decision = RecoveryDecision.COMPLETE.value
            state.decision_reason = "el objetivo ya está satisfecho según evidencia observada"
            return RecoveryDecision.COMPLETE

        # 4. El mundo invalidó el plan.
        if not plan_still_valid:
            state.decision = RecoveryDecision.REPLAN.value
            state.decision_reason = "el estado restaurado ya no describe el mundo: el plan no vale"
            return RecoveryDecision.REPLAN

        # 5. Nada de lo anterior: continuar por el mismo plan, pasando por Policy y Gate.
        state.decision = RecoveryDecision.RESUME.value
        state.decision_reason = "el mundo confirma que las acciones pendientes no ocurrieron"
        return RecoveryDecision.RESUME

    # ------------------------------------------------------------------ #

    def capability_of(self, step_id: str) -> str:
        return self._capabilities.get(step_id, "")

    def bind_capabilities(self, mapping: dict[str, str]) -> None:
        """Asocia `step_id -> capability` para que `decide()` sepa si los efectos importan.

        Se pasa explícitamente en vez de deducirlo del plan dentro de esta clase: el plan es
        del runtime, y que aquí se montara solo significaría que la decisión depende de un
        segundo mundo que puede desincronizarse.
        """
        self._capabilities = dict(mapping or {})
        return None


__all__ = [
    "ActionStatus",
    "DEFAULT_RECOVERY_BUDGET",
    "LastKnownAction",
    "RecoveryDecision",
    "RecoveryManager",
    "RecoveryState",
]