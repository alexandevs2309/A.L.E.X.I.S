import asyncio
import json
import logging
import os

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from apps.api.auth import require_api_token, token_matches
from apps.ui import PAGE
from alexis.autonomy.mission import MissionEngine
from alexis.contracts import AutonomyLevel, MissionEnvelope, MissionState
from alexis.cognition.planner import Planner
from alexis.core.runtime import AlexisRuntime
from alexis.events.bus import EventBus
from alexis.execution import LocalExecutor
from alexis.learning.system import ExperienceLearner
from alexis.memory.store import InMemoryMemory
from alexis.security.policy import PolicyEngine
from alexis.verification import BasicVerifier

logging.basicConfig(level=os.environ.get("ALEXIS_LOG_LEVEL", "INFO"))

app = FastAPI(title="ALEXIS", version="0.3.0")

events = EventBus()
runtime = AlexisRuntime(
    planner=Planner(),
    policy=PolicyEngine(),
    executor=LocalExecutor(),
    verifier=BasicVerifier(),
    memory=InMemoryMemory(),
    learning=ExperienceLearner(),
    event_bus=events,
)
missions = MissionEngine()
store: dict[str, object] = {}

# R4: en production el token es obligatorio (el arranque falla sin él); en development
# es opcional, pero la ausencia queda registrada como warning explícito.
API_TOKEN = require_api_token()
API_ENV = os.environ.get("ALEXIS_ENV", "development").strip().lower()


async def require_token(x_alexis_token: str = Header(default="")) -> None:
    """Exige el token configurado. Sin token (sólo development) no se exige nada."""
    if not token_matches(API_TOKEN, x_alexis_token):
        raise HTTPException(status_code=401, detail="Invalid or missing X-ALEXIS-Token")


class MissionRequest(BaseModel):
    objective: str
    autonomy: AutonomyLevel = AutonomyLevel.SUPERVISED
    max_runtime_minutes: int = 60


def payload(m):
    return {
        "id": m.id,
        "objective": m.goal.objective,
        "state": m.state.value,
        "context": m.context,
        "results": m.results,
    }


@app.get("/health")
async def health():
    """Sin auth a propósito: lo usan health checks y el orquestador."""
    return {
        "name": "ALEXIS",
        "version": "0.3.0",
        "status": "online",
        "env": API_ENV,
        "auth_required": bool(API_TOKEN),
    }


@app.get("/ui")
async def ui():
    return HTMLResponse(PAGE)


@app.post("/missions")
async def create_mission(req: MissionRequest, _: None = Depends(require_token)):
    envelope = MissionEnvelope(
        objective=req.objective,
        autonomy=req.autonomy,
        allowed_actions=["read", "research", "modify", "test", "commit", "execute"],
        max_runtime_minutes=req.max_runtime_minutes,
    )
    mission = missions.create(req.objective, envelope)
    store[mission.id] = mission
    return payload(mission)


@app.get("/missions")
async def list_missions(_: None = Depends(require_token)):
    return [payload(m) for m in store.values()]


@app.get("/missions/{mission_id}")
async def get_mission(mission_id: str, _: None = Depends(require_token)):
    mission = store.get(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    return payload(mission)


@app.post("/missions/{mission_id}/run")
async def run_mission(mission_id: str, _: None = Depends(require_token)):
    mission = store.get(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    mission.context.pop("pending_approval", None)
    asyncio.create_task(runtime.run_mission(mission))
    return payload(mission)


@app.post("/missions/{mission_id}/approve")
async def approve_mission(mission_id: str, _: None = Depends(require_token)):
    mission = store.get(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    if mission.state is not MissionState.WAITING_APPROVAL:
        raise HTTPException(status_code=409, detail="Mission is not waiting for approval")
    pending = mission.context.get("pending_approval")
    if pending:
        action = pending["action"]
        if action not in mission.envelope.allowed_actions:
            mission.envelope.allowed_actions.append(action)
        mission.context.pop("pending_approval", None)
    mission.results.clear()
    asyncio.create_task(runtime.run_mission(mission))
    return payload(mission)


@app.get("/stream")
async def stream(_: None = Depends(require_token)):
    async def feed():
        sub = events.subscribe_async()
        try:
            while True:
                try:
                    item = await asyncio.wait_for(sub.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        feed(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )