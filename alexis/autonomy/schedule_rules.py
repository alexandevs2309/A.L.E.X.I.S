"""Reglas de scheduling: dataclass, repositorio PostgreSQL, cron y validación.

La tabla `schedule_rules` persiste QUÉ misión generar (`objective_template` +
`envelope_template`) y CUÁNDO (`one_time` | `interval` | `cron`). Separado del motor
(`alexis/autonomy/scheduler.py`) para que el archivo del scheduler quede en el límite
de tamaño y esta capa sea la única que habla con la base.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass

EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def iso(dt) -> str | None:
    if dt is None or isinstance(dt, str):
        return dt
    return dt.isoformat()


def parse_iso(value) -> _dt.datetime | None:
    if not value:
        return None
    return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _cron_field(spec, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        base, _, step = part.partition("/")
        step_n = int(step) if step.isdigit() else 1
        rng = range(lo, hi + 1) if base in ("", "*") else (
            range(*((int(x) for x in base.split("-")) + (1,) if "-" in base else (int(base), int(base) + 1)))
        )
        values.update(rng[::step_n])
    return values


def cron_next(now: _dt.datetime, expr: str) -> _dt.datetime:
    """Próxima ocurrencia de un cron de 5 campos (min, hora, día, mes, día-semana)."""
    parts = str(expr).strip().split()
    if len(parts) != 5:
        raise ValueError(f"cron debe tener 5 campos: {expr!r}")
    minutes = _cron_field(parts[0], 0, 59)
    hours = _cron_field(parts[1], 0, 23)
    days = _cron_field(parts[2], 1, 31)
    months = _cron_field(parts[3], 1, 12)
    dows = _cron_field(parts[4], 0, 6)
    candidate = now.replace(second=0, microsecond=0) + _dt.timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        if (
            candidate.minute in minutes
            and candidate.hour in hours
            and candidate.month in months
            and (candidate.day in days or candidate.weekday() in dows)
        ):
            return candidate
        candidate += _dt.timedelta(minutes=1)
    raise ValueError(f"cron sin próxima ocurrencia en el horizonte: {expr!r}")


def validate_envelope_template(
    template: dict,
    *,
    capabilities,
    enabled: set[str] | None = None,
) -> list[str]:
    """Valida un `envelope_template` y devuelve sus capacidades canónicas.

    Fail-closed: una capacidad desconocida, fuera del límite global habilitado o una
    destructiva (`side_effects`) sin `auto_approve_destructive` hace que la regla no se
    pueda crear (POST /schedule) ni despachar (Scheduler).
    """
    enabled = enabled or set()
    caps = [str(c).strip() for c in (template.get("capabilities") or []) if str(c).strip()]
    unknown = [c for c in caps if capabilities is not None and not capabilities.has(c)]
    if unknown:
        raise ValueError(f"capacidades desconocidas para el scheduler: {unknown}")
    outside = [c for c in caps if c not in enabled]
    if outside:
        raise ValueError(f"capacidades fuera del límite global permitido: {outside}")
    destructive = [
        c for c in caps
        if capabilities is not None
        and not bool(getattr(capabilities.get(c), "side_effects", False)) is False
        and bool(getattr(capabilities.get(c), "side_effects", False))
    ]
    if destructive and not bool(template.get("auto_approve_destructive")):
        raise ValueError(
            f"capacidades destructivas exigen auto_approve_destructive=true: {destructive}"
        )
    return caps


def initial_next_run(rule: "ScheduleRule", now: _dt.datetime | None = None) -> str | None:
    """Primer `next_run_at`: one_time usa su 'at'; interval/cron arranca de inmediato."""
    now = now or utcnow()
    value = rule.schedule_value or {}
    if rule.schedule_type == "one_time":
        return iso(parse_iso(value.get("at")))
    if rule.schedule_type == "interval":
        seconds = int(value.get("seconds") or 0)
        if seconds <= 0:
            raise ValueError("interval requiere schedule_value.seconds > 0")
        return iso(now)
    if rule.schedule_type == "cron":
        if not (value.get("cron") or "").strip():
            raise ValueError("cron requiere schedule_value.cron")
        return iso(now)
    raise ValueError(f"schedule_type desconocido: {rule.schedule_type!r}")


@dataclass
class ScheduleRule:
    id: str
    name: str
    schedule_type: str
    schedule_value: dict
    envelope_template: dict
    objective_template: str
    goal_id: str | None = None
    enabled: bool = True
    last_run_at: str | None = None
    next_run_at: str | None = None
    last_failure: str | None = None
    consecutive_failures: int = 0
    max_consecutive_failures: int = 3
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_dict(cls, row: dict) -> "ScheduleRule":
        return cls(
            id=str(row["id"]), name=str(row["name"]),
            schedule_type=str(row["schedule_type"]),
            schedule_value=dict(row["schedule_value"] or {}),
            goal_id=row["goal_id"], objective_template=str(row["objective_template"]),
            envelope_template=dict(row["envelope_template"] or {}),
            enabled=bool(row["enabled"]), last_run_at=iso(row.get("last_run_at")),
            next_run_at=iso(row.get("next_run_at")), last_failure=row.get("last_failure"),
            consecutive_failures=int(row.get("consecutive_failures") or 0),
            max_consecutive_failures=int(row.get("max_consecutive_failures") or 3),
            created_at=iso(row.get("created_at")), updated_at=iso(row.get("updated_at")),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "schedule_type": self.schedule_type,
            "schedule_value": self.schedule_value, "goal_id": self.goal_id,
            "objective_template": self.objective_template,
            "envelope_template": self.envelope_template, "enabled": self.enabled,
            "last_run_at": self.last_run_at, "next_run_at": self.next_run_at,
            "last_failure": self.last_failure,
            "consecutive_failures": self.consecutive_failures,
            "max_consecutive_failures": self.max_consecutive_failures,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


def _params(rule: ScheduleRule) -> dict:
    return {
        "id": rule.id, "name": rule.name, "schedule_type": rule.schedule_type,
        "schedule_value": json.dumps(rule.schedule_value, ensure_ascii=False),
        "goal_id": rule.goal_id, "objective_template": rule.objective_template,
        "envelope_template": json.dumps(rule.envelope_template, ensure_ascii=False),
        "enabled": rule.enabled, "last_run_at": parse_iso(rule.last_run_at),
        "next_run_at": parse_iso(rule.next_run_at), "last_failure": rule.last_failure,
        "consecutive_failures": rule.consecutive_failures,
        "max_consecutive_failures": rule.max_consecutive_failures,
        "created_at": parse_iso(rule.created_at), "updated_at": parse_iso(rule.updated_at),
    }


class ScheduleRuleRepository:
    """Persistencia de reglas + contabilidad de despacho + rate limit desde missions."""

    _COLUMNS = (
        "id, name, schedule_type, schedule_value, goal_id, objective_template, "
        "envelope_template, enabled, last_run_at, next_run_at, last_failure, "
        "consecutive_failures, max_consecutive_failures, created_at, updated_at"
    )

    def __init__(self, db):
        self.db = db

    async def create(self, rule: ScheduleRule) -> ScheduleRule:
        await self.db.execute(
            f"""
            INSERT INTO schedule_rules ({self._COLUMNS})
            VALUES (
                %(id)s, %(name)s, %(schedule_type)s, %(schedule_value)s, %(goal_id)s,
                %(objective_template)s, %(envelope_template)s, %(enabled)s, %(last_run_at)s,
                %(next_run_at)s, %(last_failure)s, %(consecutive_failures)s,
                %(max_consecutive_failures)s, COALESCE(%(created_at)s, now()), now()
            ) ON CONFLICT (id) DO NOTHING
            """,
            _params(rule),
        )
        return rule

    async def get(self, rule_id: str) -> ScheduleRule | None:
        rows = await self.db.fetch(
            f"SELECT {self._COLUMNS} FROM schedule_rules WHERE id = %(id)s",
            {"id": rule_id},
        )
        return ScheduleRule.from_dict(rows[0]) if rows else None

    async def list(self, *, enabled: bool | None = None, goal_id: str | None = None) -> list[ScheduleRule]:
        clauses, params = [], {}
        if enabled is not None:
            clauses.append("enabled = %(enabled)s")
            params["enabled"] = enabled
        if goal_id is not None:
            clauses.append("goal_id = %(goal_id)s")
            params["goal_id"] = goal_id
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = await self.db.fetch(
            f"SELECT {self._COLUMNS} FROM schedule_rules {where} ORDER BY created_at ASC",
            params,
        )
        return [ScheduleRule.from_dict(r) for r in rows]

    async def update(self, rule: ScheduleRule) -> None:
        await self.db.execute(
            """
            UPDATE schedule_rules SET
                name = %(name)s, schedule_type = %(schedule_type)s,
                schedule_value = %(schedule_value)s, goal_id = %(goal_id)s,
                objective_template = %(objective_template)s,
                envelope_template = %(envelope_template)s, enabled = %(enabled)s,
                next_run_at = %(next_run_at)s,
                max_consecutive_failures = %(max_consecutive_failures)s,
                updated_at = now()
            WHERE id = %(id)s
            """,
            _params(rule),
        )

    async def set_enabled(self, rule_id: str, enabled: bool) -> ScheduleRule | None:
        await self.db.execute(
            "UPDATE schedule_rules SET enabled = %(e)s, updated_at = now() WHERE id = %(id)s",
            {"id": rule_id, "e": enabled},
        )
        return await self.get(rule_id)

    async def record_success(self, rule_id: str, *, last_run_at, next_run_at) -> None:
        await self.db.execute(
            """
            UPDATE schedule_rules SET last_run_at = %(l)s, next_run_at = %(n)s,
                consecutive_failures = 0, last_failure = NULL, updated_at = now()
            WHERE id = %(id)s
            """,
            {"id": rule_id, "l": last_run_at, "n": next_run_at},
        )

    async def register_failure(self, rule_id: str, error: str) -> tuple[int, bool]:
        rows = await self.db.fetch(
            "SELECT consecutive_failures, max_consecutive_failures FROM schedule_rules WHERE id = %(id)s",
            {"id": rule_id},
        )
        if not rows:
            return 0, False
        consecutive = int(rows[0]["consecutive_failures"]) + 1
        disabled = consecutive >= (int(rows[0]["max_consecutive_failures"]) or 3)
        await self.db.execute(
            """
            UPDATE schedule_rules SET consecutive_failures = %(c)s,
                last_failure = %(e)s, updated_at = now()
            WHERE id = %(id)s
            """,
            {"id": rule_id, "c": consecutive, "e": str(error)[:1000]},
        )
        if disabled:
            await self.set_enabled(rule_id, False)
        return consecutive, disabled

    async def reset(self, rule_id: str) -> ScheduleRule | None:
        await self.db.execute(
            """
            UPDATE schedule_rules SET consecutive_failures = 0, last_failure = NULL,
                enabled = true, next_run_at = COALESCE(next_run_at, now()),
                updated_at = now()
            WHERE id = %(id)s
            """,
            {"id": rule_id},
        )
        return await self.get(rule_id)

    async def delete(self, rule_id: str) -> bool:
        await self.db.execute("DELETE FROM schedule_rules WHERE id = %(id)s", {"id": rule_id})
        return True

    async def due(self, *, limit: int = 100) -> list[ScheduleRule]:
        rows = await self.db.fetch(
            f"""
            SELECT {self._COLUMNS} FROM schedule_rules
            WHERE enabled AND next_run_at IS NOT NULL AND next_run_at <= now()
            ORDER BY next_run_at ASC LIMIT %(limit)s
            """,
            {"limit": limit},
        )
        return [ScheduleRule.from_dict(r) for r in rows]

    async def count_scheduler_missions(self, window_s: int) -> int:
        rows = await self.db.fetch(
            """
            SELECT count(*) AS n FROM missions
            WHERE context->>'via' = 'scheduler' AND created_at > now() - make_interval(secs => %(w)s)
            """,
            {"w": float(window_s)},
        )
        return int(rows[0]["n"])

    async def oldest_scheduler_mission(self, window_s: int):
        rows = await self.db.fetch(
            """
            SELECT min(created_at) AS m FROM missions
            WHERE context->>'via' = 'scheduler' AND created_at > now() - make_interval(secs => %(w)s)
            """,
            {"w": float(window_s)},
        )
        return rows[0]["m"] if rows else None