import dataclasses
import datetime as _dt
import json

from alexis.contracts import (
    AutonomyLevel,
    Goal,
    Mission,
    MissionEnvelope,
    MissionState,
    Task,
    TaskState,
)


def _to_tz(value) -> _dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value
    return _dt.datetime.fromtimestamp(float(value), tz=_dt.timezone.utc)


def _to_epoch(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.timestamp()
    return float(value)


def mission_to_row(mission: Mission) -> dict:
    return {
        "id": mission.id,
        "objective": mission.goal.objective,
        "state": mission.state.value,
        "autonomy": mission.envelope.autonomy.value,
        "envelope": json.dumps(dataclasses.asdict(mission.envelope), ensure_ascii=False),
        "goal": json.dumps(dataclasses.asdict(mission.goal), ensure_ascii=False),
        "context": json.dumps(mission.context, ensure_ascii=False),
        "results": json.dumps(mission.results, ensure_ascii=False),
    }


def _decoded(value):
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def mission_from_row(row: dict) -> Mission:
    envelope_data = _decoded(row["envelope"])
    goal_data = _decoded(row["goal"])
    envelope = MissionEnvelope(
        objective=envelope_data["objective"],
        autonomy=AutonomyLevel(envelope_data["autonomy"]),
        allowed_actions=envelope_data["allowed_actions"],
        forbidden_actions=envelope_data["forbidden_actions"],
        approval_required=envelope_data["approval_required"],
        max_runtime_minutes=envelope_data["max_runtime_minutes"],
        max_cost_usd=envelope_data["max_cost_usd"],
        capabilities=envelope_data.get("capabilities") or [],
        perimeters=envelope_data.get("perimeters") or [],
        auto_approve=envelope_data.get("auto_approve") or [],
    )
    goal = Goal(
        objective=goal_data["objective"],
        constraints=goal_data["constraints"],
        success_criteria=goal_data.get("success_criteria") or [],
    )
    context = _decoded(row["context"])
    stored_state = MissionState(row["state"])
    # P0 §5.5: `completed` no se puede construir sin la verificación del objetivo. La
    # fila no tiene columna para ella, así que viaja en el context (que ya se persiste) y
    # se restaura aquí ANTES de fijar el estado. Una fila `completed` sin verificación
    # utilizable no se carga como completada: se degrada a needs_verification con el motivo
    # escrito, porque una fila anterior a §5.5 no puede convertirse en un Logro.
    mission = Mission(
        id=row["id"],
        goal=goal,
        envelope=envelope,
        state=MissionState.NEEDS_VERIFICATION if stored_state is MissionState.COMPLETED else stored_state,
        context=context,
        results=_decoded(row["results"]),
    )
    if stored_state is not MissionState.COMPLETED:
        return mission

    from alexis.autonomy.goal_state import goal_is_confirmed
    from alexis.cognition.goal_verification import GoalVerification

    verification = GoalVerification.from_dict(context.get("goal_verification"))
    if goal_is_confirmed(verification):
        mission.goal_verification = verification
        mission.state = MissionState.COMPLETED
        return mission

    mission.context["goal_verification_reason"] = (
        "la fila decía 'completed' pero su verificación de objetivo no es válida: no se "
        "confía y la misión queda pendiente de verificar (P0 §5.5)"
    )
    return mission


def task_to_row(task: Task) -> dict:
    return {
        "id": task.id,
        "mission_id": task.mission_id,
        "kind": task.kind,
        "agent": task.agent,
        "tool": task.tool,
        "args": json.dumps(task.args, ensure_ascii=False),
        "status": task.status.value,
        "attempts": task.attempts,
        "max_attempts": task.max_attempts,
        "error": task.error,
        "result": json.dumps(task.result, ensure_ascii=False) if task.result is not None else None,
        "lease_until": _to_tz(task.lease_until),
        "deadline": _to_tz(task.deadline),
    }


def task_from_row(row: dict) -> Task:
    return Task(
        id=row["id"],
        mission_id=row["mission_id"],
        kind=row["kind"],
        agent=row["agent"],
        tool=row["tool"],
        args=_decoded(row["args"]),
        status=TaskState(row["status"]),
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        error=row["error"],
        result=_decoded(row["result"]) if row["result"] is not None else None,
        lease_until=_to_epoch(row["lease_until"]),
        deadline=_to_epoch(row["deadline"]),
    )