"""Presence: estados derivados del estado REAL del Core / Self Model.

Una sola fuente de verdad: `current_state` sale del Self Model (eventos reales del
runtime); el frontend solo dibuja. Nada de máquinas de estados paralelas que inventen
actividad.
"""

PRESENCE_ORDER = (
    "idle",
    "listening",
    "thinking",
    "planning",
    "evaluating",
    "researching",
    "working",
    "replanning",
    "recovering",
    "waiting_for_approval",
    "verifying",
    "reflecting",
    "speaking",
    "success",
    "warning",
    "error",
)

# Acción/paso en curso -> fuerte señal de presencia humana.
_ACTION_PRESENCE = {
    "understand": "thinking",
    "analyze": "thinking",
    "research": "researching",
    "read": "researching",
    "observe": "researching",
    "execute": "working",
    "write": "working",
    "modify": "working",
    "test": "working",
    "commit": "working",
    "verify": "verifying",
    "respond": "reflecting",
}

_STATE_PRESENCE = {
    "pending": "idle",
    "planning": "planning",
    "running": "working",
    "verifying": "verifying",
    "waiting_approval": "waiting_for_approval",
    "waiting_clarification": "waiting_for_approval",
    "recovering": "recovering",
    "completed": "success",
    "failed": "error",
    "blocked": "error",
    "stopped": "idle",
}


def _mission_state(mission) -> str:
    state = getattr(mission, "state", None)
    if state is None:
        return ""
    value = getattr(state, "value", None)
    return str(value if value is not None else state).lower()


def _last_step(mission):
    for result in reversed(mission.results or []):
        if isinstance(result, dict) and result.get("step"):
            return result["step"]
    return None


def _retriable_failure(mission) -> bool:
    try:
        recovery = (mission.context or {}).get("recovery") or {}
        return bool(recovery.get("retriable"))
    except AttributeError:
        return False


def derive_presence(mission, *, listening=False, speaking=False, reflecting=False, flag=None) -> str:
    """Devuelve el estado de presencia honesto a partir del objeto real de la misión.

    Prioridad: hablar (voz en curso) > fase transitoria (evaluating/replanning) >
    reflexión explícita > estado de misión/paso. Sin misión: `listening` si percepción
    activa, si no `idle`.
    """
    if speaking:
        return "speaking"
    if mission is None:
        return "listening" if listening else "idle"
    state = _mission_state(mission)
    if flag in ("evaluating", "replanning"):
        return flag
    if reflecting:
        return "reflecting"
    if state == "running":
        return _ACTION_PRESENCE.get(_last_step(mission), "working")
    if state == "failed" and _retriable_failure(mission):
        return "warning"
    return _STATE_PRESENCE.get(state, "idle")