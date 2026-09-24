from alexis.contracts import MissionState

_STEP_LABELS = {
    "understand": "Entendiendo el objetivo",
    "research": "Recopilando contexto y evidencia",
    "execute": "Ejecutando la acción",
    "verify": "Verificando el resultado",
}

_STEP_VERBS = {
    "understand": "Entender el objetivo",
    "research": "Recopilar contexto y evidencia",
    "execute": "Ejecutar la acción",
    "verify": "Verificar el resultado",
}

_STEP_CONTEXTS = {
    "understand": "thinking",
    "research": "researching",
    "execute": "executing",
    "verify": "thinking",
}

_ACTION_VERBS = {
    "analyze": "Analizar la solicitud",
    "research": "Recopilar información",
    "execute": "Ejecutar la acción",
    "modify": "Modificar archivos",
    "test": "Ejecutar pruebas",
    "commit": "Confirmar cambios",
    "read": "Leer archivos",
    "verify": "Verificar el resultado",
}

_RISK_LABELS = {
    "low": "riesgo bajo",
    "medium": "riesgo medio",
    "high": "riesgo alto",
    "critical": "riesgo crítico",
}

_HEADLINES = {
    "idle": "¿En qué puedo ayudarte?",
    "thinking": "Estoy analizando la solicitud y preparando un plan.",
    "researching": "Estoy recopilando contexto y evidencia.",
    "executing": "Estoy ejecutando la acción planificada.",
    "waiting_approval": "Necesito tu decisión para continuar.",
    "completed": "La misión se completó.",
    "cancelled": "La misión fue cancelada.",
    "error": "La misión no se completó.",
}

_STATUS_LABELS = {
    "idle": "Inactivo",
    "thinking": "Pensando",
    "researching": "Investigando",
    "executing": "Ejecutando",
    "waiting_approval": "Requiere tu decisión",
    "completed": "Completada",
    "cancelled": "Cancelada",
    "error": "Con un problema",
}

_FINAL_STATES = {
    MissionState.COMPLETED.value: "completed",
    MissionState.FAILED.value: "error",
    MissionState.BLOCKED.value: "error",
    MissionState.STOPPED.value: "cancelled",
}

_INTERMEDIATE_STATES = {
    MissionState.PLANNING.value: "thinking",
    MissionState.VERIFYING.value: "thinking",
    MissionState.RECOVERING.value: "thinking",
}


def _actor_verb(action, fallback):
    return _ACTION_VERBS.get(action, _STEP_VERBS.get(fallback, "Acción en curso"))


def _steps_done(results):
    done = []
    for r in results or []:
        step = r.get("step")
        if step:
            done.append(_STEP_VERBS.get(step, step))
    return done


def _last_step(results):
    for r in reversed(results or []):
        step = r.get("step")
        if step:
            return step
    return None


def _evidence(snapshot, memory):
    verification = snapshot.get("verification")
    if verification:
        return list(verification.get("evidence") or [])
    return []


def _context(mission, results):
    if mission is None:
        return "idle"
    state = mission.get("state")
    if state == MissionState.WAITING_APPROVAL.value:
        return "waiting_approval"
    if state in _FINAL_STATES:
        return _FINAL_STATES[state]
    if state in _INTERMEDIATE_STATES:
        return _INTERMEDIATE_STATES[state]
    if state == MissionState.RUNNING.value:
        return _STEP_CONTEXTS.get(_last_step(results), "thinking")
    return "idle"


def _obs_text(item):
    obs = item[1] if isinstance(item, (tuple, list)) and len(item) == 2 else item
    content = obs.content if hasattr(obs, "content") else (obs.get("content") if isinstance(obs, dict) else None)
    return content.get("text") if isinstance(content, dict) else None


def _points(context, mission, results, snapshot, memory):
    points = []
    if context == "executing":
        points.extend(_steps_done(results))
        current = _STEP_LABELS.get(_last_step(results))
        if current and current not in points:
            points.append(f"{current}… (en curso)")
    if context in ("thinking", "researching"):
        for item in memory or []:
            text = _obs_text(item)
            if text:
                points.append(text)
    return points


def _decision(mission):
    pending = mission.get("pending_approval")
    if not pending:
        return None
    action = pending.get("action")
    return {
        "action": _actor_verb(action, pending.get("step")),
        "risk": _RISK_LABELS.get(pending.get("risk", ""), "riesgo sin clasificar"),
        "reason": "Necesito tu autorización para continuar con esta acción.",
        "objective": mission.get("objective"),
    }


def _result(snapshot, mission, verification):
    if not mission or mission.get("state") not in ("completed",):
        return None
    return {
        "summary": mission.get("objective"),
        "steps": _steps_done(mission.get("results")),
        "evidence": _evidence(snapshot, []),
        "confidence": int(round((verification and verification.get("confidence") or 0) * 100)),
        "next": "Cuéntame qué quieres hacer a continuación.",
    }


def _error(snapshot, mission):
    if not mission:
        return None
    state = mission.get("state")
    if state not in ("failed", "blocked"):
        return None
    needs = {
        "failed": "Necesito que revises el objetivo y vuelvas a intentarlo.",
        "blocked": "La verificación no fue suficiente; necesito un objetivo o contexto más claro.",
    }.get(state, "Necesito tu indicación para continuar.")
    return {"summary": "No pude completar la misión.", "needs": needs}


def present(snapshot):
    mission = snapshot.get("mission")
    memory = snapshot.get("memory") or []
    context = _context(mission, mission.get("results") if mission else None)
    verification = snapshot.get("verification")
    return {
        "context": context,
        "status": _STATUS_LABELS[context],
        "headline": _HEADLINES[context],
        "objective": mission.get("objective") if mission else None,
        "points": _points(context, mission, mission.get("results") if mission else None, snapshot, memory),
        "steps_done": _steps_done(mission.get("results")) if mission else [],
        "decision": _decision(mission) if mission else None,
        "result": _result(snapshot, mission, verification),
        "error": _error(snapshot, mission),
    }