"""P0 §5.5 — la política del estado final: `COMPLETED` solo con objetivo verificado.

Este módulo es la respuesta a la pregunta "¿quién decide el estado final?". La respuesta es
una sola: la función `settle()` de aquí, y la invariante de `Mission.__setattr__` en
`alexis/contracts.py` es la que hace que ninguna otra respuesta sea posible.

La asimetría es deliberada:

- `NEEDS_VERIFICATION` significa "todavía no", no "falló". Un objetivo no demostrado deja la
  misión viva y explicable. `FAILED` queda para cuando hay un fallo de verdad.
- `BLOCKED` se reserva a la autoridad: policy, gate, aprobación o capability ausente.
- `COMPLETED` es el único estado que exige `GoalVerification.verified is True`.

`goal_is_confirmed()` revalida la verificación desde sus propios criterios antes de
autorizar nada. Así, un `GoalVerification(verified=True)` fabricado a mano, o uno que venga
serializado de una base de datos con `verified: true` pero sin evidencia, no puede abrir la
puerta: se vuelve a comprobar criterio por criterio. La autoridad es la **evidencia**, no
una bandera.
"""

from __future__ import annotations

from alexis.contracts import MissionState


def goal_is_confirmed(verification) -> bool:
    """¿Esta verificación autoriza `COMPLETED`? Se revalida, no se trusts.

    Exige que sea una `GoalVerification` real, que diga `verified=True`, que tenga al menos
    un criterio, y que **todos** los criterios estén `satisfied` con al menos una evidencia
    fiable. Un objeto con la bandera puesta y la lista vacía no pasa.
    """
    if verification is None:
        return False
    if type(verification).__name__ != "GoalVerification":
        return False
    if getattr(verification, "verified", None) is not True:
        return False
    evaluations = list(getattr(verification, "evaluations", []) or [])
    if not evaluations:
        return False
    for evaluation in evaluations:
        status = getattr(evaluation, "status", None)
        if getattr(status, "value", status) != "satisfied":
            return False
        if not any(getattr(e, "trusted", False) for e in getattr(evaluation, "evidence", []) or []):
            return False
    return True


def settle(mission, verification) -> MissionState:
    """Única autoridad sobre el estado final. Devuelve el estado y lo aplica.

    `mission.state = COMPLETED` solo se ejecuta cuando `verification` realmente lo
    autoriza; en cualquier otro caso la misión queda en un estado honesto y el motivo
    queda escrito en el contexto para que la decisión sea auditable.
    """
    from alexis.cognition.goal_verification import GoalVerification

    if verification is not None and not isinstance(verification, GoalVerification):
        raise TypeError(
            "settle() solo acepta un GoalVerification del GoalVerifier; "
            f"recibió {type(verification).__name__}"
        )

    # La verificación se adjunta SIEMPRE, verificada o no: es el registro de por qué la
    # misión quedó como quedó, y es lo que la invariante de `Mission` consulta.
    if verification is not None:
        mission.goal_verification = verification

    if goal_is_confirmed(verification):
        # También en el context: sin esto la fila persistida no lleva la verificación y
        # al recuperarla no se podría reconstruir un `completed` legítimo (§5.5).
        mission.context["goal_verification"] = verification.to_dict()
        mission.context["goal_verification_reason"] = verification.reason
        mission.state = MissionState.COMPLETED
        return mission.state

    mission.context["goal_verification"] = (verification.to_dict() if verification else None)
    mission.context["goal_verification_reason"] = _reason(verification)
    if verification is not None and getattr(verification, "unsatisfied", None) is not None:
        failing = verification.unsatisfied()
        mission.state = MissionState.BLOCKED if failing else MissionState.NEEDS_VERIFICATION
    else:
        mission.state = MissionState.NEEDS_VERIFICATION
    return mission.state


def _reason(verification) -> str:
    if verification is None:
        return (
            "no hay GoalVerifier en esta ejecución: sin verificación del objetivo no se "
            "puede declarar COMPLETED (P0 §5.5)"
        )
    reason = str(getattr(verification, "reason", "") or "")
    return reason or "el objetivo no está verificado"


__all__ = ["goal_is_confirmed", "settle"]
