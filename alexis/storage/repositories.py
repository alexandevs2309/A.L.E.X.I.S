import json

from alexis.contracts import Checkpoint, Execution, Mission, Task, TaskState
from alexis.storage.db import Database
from alexis.storage.serialization import (
    mission_from_row,
    mission_to_row,
    task_from_row,
    task_to_row,
)


class MissionRepository:
    def __init__(self, db: Database):
        self.db = db

    async def upsert(self, mission: Mission):
        row = mission_to_row(mission)
        await self.db.execute(
            """
            INSERT INTO missions (id, state, autonomy, envelope, goal, context, results, updated_at)
            VALUES (%(id)s, %(state)s, %(autonomy)s, %(envelope)s::jsonb, %(goal)s::jsonb, %(context)s::jsonb, %(results)s::jsonb, now())
            ON CONFLICT (id) DO UPDATE SET
                state = EXCLUDED.state,
                autonomy = EXCLUDED.autonomy,
                envelope = EXCLUDED.envelope,
                goal = EXCLUDED.goal,
                context = EXCLUDED.context,
                results = EXCLUDED.results,
                updated_at = now()
            """,
            row,
        )

    async def get(self, mission_id: str) -> Mission | None:
        rows = await self.db.fetch("SELECT * FROM missions WHERE id = %(id)s", {"id": mission_id})
        return mission_from_row(rows[0]) if rows else None

    async def list(self, limit: int = 50) -> list[Mission]:
        rows = await self.db.fetch(
            "SELECT * FROM missions ORDER BY created_at DESC LIMIT %(limit)s",
            {"limit": limit},
        )
        return [mission_from_row(row) for row in rows]

    async def next_pending(self) -> Mission | None:
        """Devuelve la misión pendiente más antigua (cola FIFO persistente)."""
        rows = await self.db.fetch(
            "SELECT * FROM missions WHERE state = 'pending' ORDER BY created_at ASC LIMIT 1",
            {},
        )
        return mission_from_row(rows[0]) if rows else None


class EventRepository:
    def __init__(self, db: Database):
        self.db = db

    async def append(self, topic: str, payload, mission_id: str | None = None):
        await self.db.execute(
            """
            INSERT INTO mission_events (mission_id, topic, payload)
            VALUES (%(mission_id)s, %(topic)s, %(payload)s::jsonb)
            """,
            {
                "mission_id": mission_id,
                "topic": topic,
                "payload": json.dumps(payload, ensure_ascii=False),
            },
        )

    async def list(self, mission_id: str | None = None, limit: int = 100):
        if mission_id:
            rows = await self.db.fetch(
                "SELECT * FROM mission_events WHERE mission_id = %(mission_id)s ORDER BY id ASC LIMIT %(limit)s",
                {"mission_id": mission_id, "limit": limit},
            )
        else:
            rows = await self.db.fetch(
                "SELECT * FROM mission_events ORDER BY id DESC LIMIT %(limit)s",
                {"limit": limit},
            )
        return rows


class AuditRepository:
    def __init__(self, db: Database):
        self.db = db

    async def record(self, event: str, actor: str, mission_id: str | None = None, **details):
        await self.db.execute(
            """
            INSERT INTO audit_log (event, actor, mission_id, details)
            VALUES (%(event)s, %(actor)s, %(mission_id)s, %(details)s::jsonb)
            """,
            {
                "event": event,
                "actor": actor,
                "mission_id": mission_id,
                "details": json.dumps(details, ensure_ascii=False, default=str),
            },
        )

    async def list(self, limit: int = 100):
        return await self.db.fetch(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT %(limit)s",
            {"limit": limit},
        )


class TaskRepository:
    def __init__(self, db: Database):
        self.db = db

    async def upsert(self, task: Task):
        row = task_to_row(task)
        await self.db.execute(
            """
            INSERT INTO tasks (id, mission_id, kind, agent, tool, args, status, attempts, max_attempts, error,
                               result, lease_until, deadline, updated_at)
            VALUES (%(id)s, %(mission_id)s, %(kind)s, %(agent)s, %(tool)s, %(args)s::jsonb, %(status)s,
                    %(attempts)s, %(max_attempts)s, %(error)s, %(result)s::jsonb, %(lease_until)s, %(deadline)s, now())
            ON CONFLICT (id) DO UPDATE SET
                kind = EXCLUDED.kind,
                agent = EXCLUDED.agent,
                tool = EXCLUDED.tool,
                args = EXCLUDED.args,
                status = EXCLUDED.status,
                attempts = EXCLUDED.attempts,
                max_attempts = EXCLUDED.max_attempts,
                error = EXCLUDED.error,
                result = EXCLUDED.result,
                lease_until = EXCLUDED.lease_until,
                deadline = EXCLUDED.deadline,
                updated_at = now()
            """,
            row,
        )

    async def get(self, task_id: str) -> Task | None:
        rows = await self.db.fetch("SELECT * FROM tasks WHERE id = %(id)s", {"id": task_id})
        return task_from_row(rows[0]) if rows else None

    async def list(self, mission_id: str | None = None, status: str | None = None, limit: int = 100):
        query = "SELECT * FROM tasks"
        clauses, params = [], {}
        if mission_id:
            clauses.append("mission_id = %(mission_id)s")
            params["mission_id"] = mission_id
        if status:
            clauses.append("status = %(status)s")
            params["status"] = status
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at ASC LIMIT %(limit)s"
        params["limit"] = limit
        rows = await self.db.fetch(query, params)
        return [task_from_row(row) for row in rows]

    async def next_claimable(self, mission_id: str | None = None) -> Task | None:
        clause = "status IN ('pending', 'queued', 'retrying')"
        params: dict = {}
        if mission_id:
            clause += " AND mission_id = %(mission_id)s"
            params["mission_id"] = mission_id
        rows = await self.db.fetch(
            f"SELECT * FROM tasks WHERE {clause} ORDER BY created_at ASC LIMIT 1",
            params,
        )
        return task_from_row(rows[0]) if rows else None


class ExecutionRepository:
    def __init__(self, db: Database):
        self.db = db

    async def insert(self, execu: Execution):
        await self.db.execute(
            """
            INSERT INTO executions (task_id, tool, args_hash, ok, output, error, finished_at)
            VALUES (%(task_id)s, %(tool)s, %(args_hash)s, %(ok)s, %(output)s::jsonb, %(error)s, now())
            """,
            {
                "task_id": execu.task_id,
                "tool": execu.tool,
                "args_hash": execu.args_hash,
                "ok": execu.ok,
                "output": json.dumps(execu.output, ensure_ascii=False, default=str),
                "error": execu.error,
            },
        )

    async def list(self, task_id: str) -> list:
        return await self.db.fetch(
            "SELECT * FROM executions WHERE task_id = %(task_id)s ORDER BY id ASC",
            {"task_id": task_id},
        )


class ObservationRepository:
    def __init__(self, db: Database):
        self.db = db

    async def insert(self, mission_id: str, source: str, content, trusted: bool = False):
        await self.db.execute(
            """
            INSERT INTO observations (mission_id, source, content, trusted)
            VALUES (%(mission_id)s, %(source)s, %(content)s::jsonb, %(trusted)s)
            """,
            {
                "mission_id": mission_id,
                "source": source,
                "content": json.dumps(content, ensure_ascii=False, default=str),
                "trusted": trusted,
            },
        )

    async def list(self, mission_id: str, limit: int = 100) -> list:
        return await self.db.fetch(
            "SELECT * FROM observations WHERE mission_id = %(mission_id)s ORDER BY id ASC LIMIT %(limit)s",
            {"mission_id": mission_id, "limit": limit},
        )


class VerificationRepository:
    def __init__(self, db: Database):
        self.db = db

    async def insert(
        self,
        mission_id: str,
        passed: bool,
        confidence: float,
        verifier: str,
        evidence: list,
        notes: str = "",
    ):
        await self.db.execute(
            """
            INSERT INTO verifications (mission_id, passed, confidence, verifier, evidence, notes)
            VALUES (%(mission_id)s, %(passed)s, %(confidence)s, %(verifier)s, %(evidence)s::jsonb, %(notes)s)
            """,
            {
                "mission_id": mission_id,
                "passed": passed,
                "confidence": confidence,
                "verifier": verifier,
                "evidence": json.dumps(evidence, ensure_ascii=False, default=str),
                "notes": notes,
            },
        )

    async def list(self, mission_id: str, limit: int = 50) -> list:
        return await self.db.fetch(
            "SELECT * FROM verifications WHERE mission_id = %(mission_id)s ORDER BY id ASC LIMIT %(limit)s",
            {"mission_id": mission_id, "limit": limit},
        )


class CheckpointRepository:
    def __init__(self, db: Database):
        self.db = db

    async def save(self, checkpoint: Checkpoint):
        await self.db.execute(
            """
            INSERT INTO checkpoints (mission_id, step_index, payload)
            VALUES (%(mission_id)s, %(step_index)s, %(payload)s::jsonb)
            """,
            {
                "mission_id": checkpoint.mission_id,
                "step_index": checkpoint.step_index,
                "payload": json.dumps(checkpoint.payload, ensure_ascii=False, default=str),
            },
        )

    async def latest(self, mission_id: str) -> dict | None:
        rows = await self.db.fetch(
            "SELECT * FROM checkpoints WHERE mission_id = %(mission_id)s ORDER BY step_index DESC LIMIT 1",
            {"mission_id": mission_id},
        )
        return rows[0] if rows else None

    async def close_open_tasks(self, mission_id: str, status: str = "cancelled"):
        await self.db.execute(
            """
            UPDATE tasks SET status = %(status)s, updated_at = now()
            WHERE mission_id = %(mission_id)s AND status IN ('pending', 'queued', 'retrying')
            """,
            {"mission_id": mission_id, "status": status},
        )