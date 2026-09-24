import datetime as _dt
import time

from alexis.contracts import Task, TaskState


class Scheduler:
    """Cola de tareas reclamables + limpieza de huérfanas (leases expiradas)."""

    def __init__(self, task_repo, event_bus=None):
        self.task_repo = task_repo
        self.events = event_bus

    async def poll_claimable(self, mission_id: str | None = None) -> Task | None:
        task = await self.task_repo.next_claimable(mission_id)
        if task is None:
            return None
        if task.deadline is not None and time.time() > task.deadline:
            task.status = TaskState.FAILED
            task.error = "deadline excedido"
            await self.task_repo.upsert(task)
            return None
        return task

    async def recover_stale(self, lease_seconds: float = 60.0) -> int:
        """Recupera tareas 'running' cuya lease expiró y las vuelve a encolar (anti-huérfanas)."""
        db = self.task_repo.db
        now = _dt.datetime.now(_dt.timezone.utc)
        stale = await db.fetch(
            "SELECT * FROM tasks WHERE status = 'running' AND lease_until < %(now)s",
            {"now": now},
        )
        reclaimed = 0
        for row in stale:
            await db.execute(
                "UPDATE tasks SET status = 'queued', lease_until = NULL, updated_at = now() WHERE id = %(id)s",
                {"id": row["id"]},
            )
            reclaimed += 1
            if self.events is not None:
                await self.events.publish("task.reclaimed", {"task_id": row["id"], "stale": True})
        expired = await db.fetch(
            "SELECT * FROM tasks WHERE status IN ('queued','pending') AND deadline IS NOT NULL AND deadline < %(now)s",
            {"now": now},
        )
        for row in expired:
            await db.execute(
                "UPDATE tasks SET status = 'failed', error = %(err)s, updated_at = now() WHERE id = %(id)s",
                {"id": row["id"], "err": "deadline excedido"},
            )
            reclaimed += 1
        return reclaimed