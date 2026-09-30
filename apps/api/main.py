"""CORE-03 — `apps.api` deja de ser un runtime: es una FACHADA del oficial.

Antes este módulo construía su propio `AlexisRuntime` con `LocalExecutor`,
`BasicVerifier`, `InMemoryMemory` y un `store` de misiones en un `dict`. Eso era un
segundo ALEXIS, incompatible con el del demo: sin `CognitiveRuntime`, sin
`AutonomyGate`, sin catálogo de capacidades, sin `GoalVerifier` y sin persistencia. Por
construcción no podía terminar una misión en `COMPLETED`, y su estado se perdía al
reiniciar.

Ahora este módulo no construye NADA. Pide la instancia oficial a
`apps.demo.app.get_official_runtime()` —la misma que usa el resto de superficies del
proceso— y le delega. Si alguien lo importa, obtiene el runtime oficial; no obtiene un
segundo runtime.

Se conservan las rutas que sólo existían aquí y que la superficie oficial no tenía:
`/health` (sin auth, para health checks) y el requisito de `X-ALEXIS-Token`. El resto
(`/ui`, `/missions`, `/missions/{id}/run`, `/missions/{id}/approve`, `/stream`) ya lo
sirve la aplicación oficial y aquí se delegan en ella.

**Este módulo NO es un entrypoint.** El proceso oficial de ALEXIS es
`python3 -m apps.demo.server` (`apps/demo/app.py`). Si alguien lanza esta fachada por su
cuenta con `uvicorn apps.api.main:app`, las rutas que necesitan el runtime responden
`503` con el motivo: no puede construir un ALEXIS propio, porque la construcción es
exclusiva del proceso oficial. Antes sí podía —y por eso esto importaba—: este módulo
tenía su propio `AlexisRuntime` con `LocalExecutor` y `BasicVerifier`, y levantarse era
tener un segundo ALEXIS con otro bus, otro estado y otra verdad.

Lo que se ha perdido a propósito, y por qué es correcto: que este módulo pudiera
ejecutar una misión con un ejecutor simulado, y que pudiera ser una segunda instancia.
Eso no era una función, era un riesgo.
"""

import logging
import os

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse

from apps.api.auth import require_api_token, token_matches
from apps.demo.app import (
    OfficialRuntimeNotRunning,
    get_official_runtime,
    peek_official_runtime,
)
from apps.ui import PAGE

logging.basicConfig(level=os.environ.get("ALEXIS_LOG_LEVEL", "INFO"))

app = FastAPI(title="ALEXIS", version="0.3.0", description="Fachada del runtime oficial")

#: R4: en production el token es obligatorio (el arranque falla sin él); en development
#: es opcional, pero la ausencia queda registrada como warning explícito.
API_TOKEN = require_api_token()
API_ENV = os.environ.get("ALEXIS_ENV", "development").strip().lower()


def official():
    """La instancia oficial, exigida: no se construye aquí.

    Si este proceso no es el oficial, `get_official_runtime()` lanza
    `OfficialRuntimeNotRunning`. Es deliberado — antes esta función construía un ALEXIS
    entero, y `uvicorn apps.api.main:app` bastaba para tener un segundo ALEXIS con su
    propio bus, su `CognitiveRuntime` y su estado—. Una fachada no puede ser un segundo
    entrypoint, y la forma de no serlo es no poder construir lo que dice representar.
    """
    return get_official_runtime()


async def require_official_runtime():
    """Dependencia: convierte el error de "no soy el proceso oficial" en un 503 claro."""
    try:
        return official()
    except OfficialRuntimeNotRunning as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def require_token(x_alexis_token: str = Header(default="")) -> None:
    """Exige el token configurado. Sin token (sólo development) no se exige nada."""
    if not token_matches(API_TOKEN, x_alexis_token):
        raise HTTPException(status_code=401, detail="Invalid or missing X-ALEXIS-Token")


@app.get("/health")
async def health():
    """Sin auth a propósito: lo usan health checks y el orquestador.

    Se describe con `peek`, no con `official`: un `/health` no debe construir nada. Y si
    este proceso no es el oficial, lo DICE en vez de fingir que hay un ALEXIS detrás.
    """
    rt = peek_official_runtime()
    return {
        "name": "ALEXIS",
        "status": "online" if rt is not None else "facade_only",
        "env": API_ENV,
        "auth_required": bool(API_TOKEN),
        "entrypoint": "facade",
        "is_official_process": rt is not None,
        "official_runtime": (
            None if rt is None else {
                "cognitive": rt.cognitive is not None,
                "goal_verifier": rt.cognitive is not None and rt.cognitive.goal_verifier is not None,
                "catalog": rt.capabilities is not None,
                "persistence": rt.has_persistence,
            }
        ),
    }


@app.get("/ui")
async def ui():
    return HTMLResponse(PAGE)


@app.get("/stream")
async def stream(rt=Depends(require_official_runtime), _: None = Depends(require_token)):
    """SSE sobre el `EventBus` OFICIAL, con su suscripción asíncrona.

    No se abre un bus nuevo ni se reimplementa el formato: es la misma suscripción y el
    mismo `EventBus` que usan `/chat` y `/missions`. Por eso este stream ve los mismos
    eventos que el de la aplicación oficial.
    """
    import asyncio
    import json

    from fastapi.responses import StreamingResponse

    sub = rt.events.subscribe_async()

    async def feed():
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


@app.post("/missions")
async def create_mission(req: dict, rt=Depends(require_official_runtime), _: None = Depends(require_token)):
    """Crear misión por HTTP, contra el runtime oficial.

    Los criterios de éxito se canónicos con el contrato de CORE-02 (el mismo que usa
    `/chat` a través del `IntentClassifier`), de modo que ninguna misión creada por
    esta fachada llega al `GoalVerifier` con criterios que no sabe leer.
    """
    from alexis.cognition.criteria import normalize_criteria
    from alexis.contracts import AutonomyLevel, MissionEnvelope

    objective = req.get("objective", "Objetivo sin especificar")
    try:
        autonomy = AutonomyLevel(req.get("autonomy", "supervised"))
    except ValueError:
        autonomy = AutonomyLevel.SUPERVISED
    requested = req.get("capabilities")
    capabilities = (
        [cid for cid in requested if rt.capabilities.has(cid)]
        if requested is not None
        else rt.enabled_capabilities
    )
    criteria, status = normalize_criteria(objective, objective, req.get("success_criteria"))
    envelope = MissionEnvelope(
        objective=objective,
        autonomy=autonomy,
        allowed_actions=[
            "understand", "analyze", "research", "execute", "verify",
            "modify", "test", "commit", "write", "respond",
        ],
        capabilities=capabilities,
    )
    mission = rt.missions.create(objective, envelope, success_criteria=criteria)
    rt.running[mission.id] = mission
    rt.state["mission_id"] = mission.id
    rt.enqueue_or_run(mission)
    return {**rt.payload(mission), "criteria_status": status}


@app.get("/missions")
async def list_missions(rt=Depends(require_official_runtime), _: None = Depends(require_token)):
    return [rt.payload(m) for m in rt.running.values()]


@app.get("/missions/{mission_id}")
async def get_mission(mission_id: str, rt=Depends(require_official_runtime), _: None = Depends(require_token)):
    mission = rt.running.get(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    return rt.payload(mission)


@app.post("/missions/{mission_id}/run")
async def run_mission(mission_id: str, rt=Depends(require_official_runtime), _: None = Depends(require_token)):
    mission = rt.running.get(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    mission.context.pop("pending_approval", None)
    rt.enqueue_or_run(mission)
    return rt.payload(mission)


@app.post("/missions/{mission_id}/approve")
async def approve_mission(mission_id: str, rt=Depends(require_official_runtime), _: None = Depends(require_token)):
    from alexis.contracts import MissionState

    mission = rt.running.get(mission_id)
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
    rt.enqueue_or_run(mission)
    return rt.payload(mission)
