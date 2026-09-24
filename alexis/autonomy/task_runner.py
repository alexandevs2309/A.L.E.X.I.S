import hashlib
import json
import time

from alexis.contracts import Checkpoint, Execution, ExecutionResult, Mission, PlanStep, Task, TaskState


def args_hash(args: dict) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class TaskRunner:
    """Ciclo de vida real de una tarea: queued → lease (running) → completed/failed/retrying.

    Valor por defecto: lease de ~60s renovable con heartbeat. El checkpoint se guarda
    por step_index para reanudar sin reiniciar la misión entera.
    """

    def __init__(
        self,
        task_repo,
        execution_repo,
        checkpoint_repo=None,
        event_bus=None,
        lease_seconds: float = 60.0,
    ):
        self.task_repo = task_repo
        self.execution_repo = execution_repo
        self.checkpoint_repo = checkpoint_repo
        self.events = event_bus
        self.lease_seconds = lease_seconds

    async def _publish(self, topic: str, payload):
        if self.events is not None:
            await self.events.publish(topic, payload)

    async def create_task(self, mission: Mission, step: PlanStep) -> Task:
        task = Task(
            id=f"{mission.id}:{step.id}",
            mission_id=mission.id,
            kind="step",
            agent=step.agent,
            tool=step.action,
            args={"step": step.id, "action": step.action, "description": step.description},
            status=TaskState.QUEUED,
            max_attempts=3,
            deadline=time.time() + mission.envelope.max_runtime_minutes * 60,
        )
        await self.task_repo.upsert(task)
        await self._publish("task.queued", {"task_id": task.id, "mission_id": mission.id, "tool": task.tool})
        return task

    async def claim(self, task: Task) -> Task:
        task.status = TaskState.RUNNING
        task.lease_until = time.time() + self.lease_seconds
        await self.task_repo.upsert(task)
        await self._publish("task.started", {"task_id": task.id, "mission_id": task.mission_id})
        return task

    async def heartbeat(self, task: Task) -> Task:
        task.lease_until = time.time() + self.lease_seconds
        await self.task_repo.upsert(task)
        return task

    async def record_execution(self, task: Task, result: ExecutionResult):
        await self.execution_repo.insert(
            Execution(
                task_id=task.id,
                tool=task.tool or task.kind,
                args_hash=args_hash(task.args),
                ok=result.success,
                output=result.output,
                error=result.error,
            )
        )

    async def complete(self, task: Task, result: ExecutionResult):
        await self.record_execution(task, result)
        if result.success:
            task.status = TaskState.COMPLETED
            task.error = None
            task.result = result.output
            await self.task_repo.upsert(task)
            await self._publish("task.completed", {"task_id": task.id, "mission_id": task.mission_id, "ok": True})
            return
        task.attempts += 1
        task.error = result.error
        if task.attempts >= task.max_attempts:
            task.status = TaskState.FAILED
            task.result = result.output
            await self.task_repo.upsert(task)
            await self._publish(
                "task.failed",
                {"task_id": task.id, "mission_id": task.mission_id, "attempts": task.attempts, "error": task.error},
            )
        else:
            task.status = TaskState.RETRYING
            await self.task_repo.upsert(task)
            await self._publish(
                "task.retrying",
                {"task_id": task.id, "mission_id": task.mission_id, "attempt": task.attempts},
            )

    async def run_step(self, mission: Mission, step: PlanStep, run):
        """Claim → ejecutar (con heartbeat + reintentos) → registrar → finalizar."""
        task = await self.create_task(mission, step)
        last_result: ExecutionResult | None = None
        while task.status not in {TaskState.COMPLETED, TaskState.FAILED} and task.attempts < task.max_attempts:
            await self.claim(task)
            await self.heartbeat(task)
            last_result = await run(mission, step)
            await self.complete(task, last_result)
        if last_result is None:
            last_result = ExecutionResult(success=False, error="sin ejecución")
        return last_result, task

    async def save_checkpoint(self, mission: Mission, step_index: int):
        if self.checkpoint_repo is None:
            return
        await self.checkpoint_repo.save(
            Checkpoint(
                mission_id=mission.id,
                step_index=step_index,
                payload={"state": mission.state.value, "results": mission.results, "context": mission.context},
            )
        )

    async def resume(self, mission_id: str) -> tuple[int, dict]:
        """Devuelve (próximo step_index, payload del último checkpoint) o (0, {})."""
        if self.checkpoint_repo is None:
            return 0, {}
        latest = await self.checkpoint_repo.latest(mission_id)
        if latest is None:
            return 0, {}
        payload = latest.get("payload") or {}
        return int(latest["step_index"]) + 1, payload

    async def close_open_tasks(self, mission_id: str, status: str = "cancelled"):
        if self.checkpoint_repo is not None:
            await self.checkpoint_repo.close_open_tasks(mission_id, status)