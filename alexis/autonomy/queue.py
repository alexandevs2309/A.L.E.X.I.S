import asyncio

from alexis.contracts import MissionState


async def enqueue(mission_repo, mission):
    """Encola una misión: estado UNA misión a la vez (FIFO por created_at)."""
    mission.state = MissionState.PENDING
    mission.context.pop("pending_approval", None)
    mission.context.pop("blocked_reason", None)
    await mission_repo.upsert(mission)


class MissionWorker:
    """Cola de misiones: consume la más antigua en estado pending, una a la vez.

    - Corre misiones en orden estricto (FIFO).
    - Si una misión queda esperando aprobación (WAITING_APPROVAL) o bloqueada,
      el worker sigue con la siguiente pendiente; la misión vuelve a la cola
      cuando el humano la aprueba (se re-encola como pending).
    - `recover_startup` re-encola misiones que quedaron a medias tras un corte
      (planning/running/verifying → pending), para reanudar desde el checkpoint.
    """

    def __init__(self, runner, mission_repo=None, poll_seconds: float = 0.5):
        self.runner = runner
        self.mission_repo = mission_repo
        self.poll = poll_seconds
        self._stop = False
        self.active: object | None = None

    async def next_pending(self):
        if self.mission_repo is None:
            return None
        return await self.mission_repo.next_pending()

    async def loop(self):
        while not self._stop:
            mission = await self.next_pending()
            if mission is not None:
                self.active = mission
                try:
                    await self.runner(mission)
                except Exception as exc:
                    await self._log_failure(mission, exc)
                    mission.state = MissionState.FAILED
                    if self.mission_repo is not None:
                        mission.context["queue_error"] = str(exc)
                        await self.mission_repo.upsert(mission)
                finally:
                    self.active = None
            else:
                await asyncio.sleep(self.poll)

    async def _log_failure(self, mission, exc):
        print(f"[cola] error en la misión {mission.id}: {exc}", flush=True)

    def stop(self):
        self._stop = True

    async def recover_startup(self, open_states=("planning", "running", "verifying")):
        """Tras un reinicio, re-encola misiones que quedaron a medias."""
        if self.mission_repo is None:
            return
        for mission in await self.mission_repo.list(limit=500):
            if mission.state.value in open_states:
                mission.state = MissionState.PENDING
                await self.mission_repo.upsert(mission)