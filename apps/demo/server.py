import asyncio
import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from mimetypes import guess_type
from pathlib import Path
from urllib.parse import urlparse

from apps.ui import PAGE
from alexis.agents.registry import AGENTS
from alexis.autonomy.gates import AutonomyGate
from alexis.autonomy.mission import MissionEngine
from alexis.autonomy.queue import MissionWorker, enqueue
from alexis.autonomy.scheduler import Scheduler
from alexis.autonomy.task_runner import TaskRunner
from alexis.capabilities import build_catalog
from alexis.cognition.conversation import ConversationSession
from alexis.cognition.intent_classifier import IntentClassifier
from alexis.cognition.loop import CognitiveRuntime
from alexis.cognition.planner_model import ModelPlanner, PlanValidator
from alexis.contracts import AutonomyLevel, MissionEnvelope, MissionState
from alexis.cognition.planner import Planner
from alexis.core.runtime import AlexisRuntime
from alexis.events.bus import EventBus
from alexis.execution import SandboxExecutor
from alexis.experience.presenter import present
from alexis.learning.system import ExperienceLearner
from alexis.memory.provider import InProcessMemoryProvider, PostgresMemoryProvider
from alexis.memory.store import InMemoryMemory
from alexis.models.config import ModelConfig
from alexis.models.degraded import EchoModel
from alexis.models.router import ModelRouter
from alexis.perception.activation import ACTIVATION_OBJECTIVE
from alexis.perception.clap_listener import DEFAULT_TOPIC, ClapListener
from alexis.security.policy import PolicyEngine
from alexis.security.sandbox import SandboxRunner
from alexis.self.model import SelfModel
from alexis.self.sync import SelfModelSync
from alexis.speech.tts import get_tts_provider, synthesize_with_fallback
from alexis.tools.desktop import build_desktop_tools
from alexis.tools.filesystem import build_filesystem_tools
from alexis.tools.testrunner import build_test_tools
from alexis.tools.registry import ToolRegistry
from alexis.verification import FilesystemVerifier
from alexis.world.model import WorldEntity, WorldModel

EVENTS = EventBus()
STORAGE = {}


def init_storage():
    try:
        from alexis.storage.db import Database
        from alexis.storage.repositories import (
            AuditRepository,
            CheckpointRepository,
            EventRepository,
            ExecutionRepository,
            MissionRepository,
            ObservationRepository,
            TaskRepository,
            VerificationRepository,
        )
    except ImportError as exc:
        print(f"[warn] persistencia no disponible (faltan dependencias deps): {exc}")
        return

    async def _open_and_migrate(db):
        await db.open()
        await db.migrate()

    try:
        db = Database()
        asyncio.run_coroutine_threadsafe(_open_and_migrate(db), LOOP).result(timeout=10)
        STORAGE["db"] = db
        STORAGE["mission"] = MissionRepository(db)
        STORAGE["event"] = EventRepository(db)
        STORAGE["audit"] = AuditRepository(db)
        STORAGE["verification"] = VerificationRepository(db)
        STORAGE["task"] = TaskRepository(db)
        STORAGE["execution"] = ExecutionRepository(db)
        STORAGE["observation"] = ObservationRepository(db)
        STORAGE["checkpoint"] = CheckpointRepository(db)
        print("Persistencia PostgreSQL activa (missions, tasks, executions, checkpoints, verifications, observations, audit).")
    except Exception as exc:
        print(f"[warn] persistencia no disponible: {exc}")


def recover_missions():
    repo = STORAGE.get("mission")
    if not repo:
        return

    async def _recover():
        open_states = ("pending", "planning", "running", "verifying", "waiting_approval")
        missions = await repo.list(limit=50)
        open_missions = [m for m in missions if m.state.value in open_states]
        if SCHEDULER is not None and RUNNER is not None:
            reclaimed = await SCHEDULER.recover_stale(lease_seconds=RUNNER.lease_seconds)
            if reclaimed:
                print(f"Scheduler: {reclaimed} tareas con lease expirada recuperadas.")
        # Reanudar solo misiones que estaban ejecutándose de verdad; las que quedaron
        # esperando aprobación se dejan en RUNNING (aprobables) sin cartel automático,
        # para no confundir al usuario con permisos de misiones viejas.
        active = [m for m in open_missions if m.state.value in ("planning", "running", "verifying")]
        for m in open_missions:
            RUNNING[m.id] = m
            if m.state.value != "waiting_approval":
                if WORKER is not None:
                    await enqueue(WORKER.mission_repo, m)
                else:
                    asyncio.ensure_future(run_mission(m))
        if active:
            STATE["mission_id"] = active[-1].id

    try:
        asyncio.run_coroutine_threadsafe(_recover(), LOOP).result(timeout=10)
        if STATE["mission_id"] is not None:
            print("Misiones abiertas recuperadas de PostgreSQL (reanudadas desde checkpoint).")
    except Exception as exc:
        print(f"[warn] recuperación de misiones: {exc}")


LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, daemon=True).start()
init_storage()

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FACE = os.path.join(_PROJECT_ROOT, "1000560050.jpg")
FACE_DIST = os.path.join(_PROJECT_ROOT, "apps", "face", "dist")

WORKSPACE = Path(os.environ.get("ALEXIS_WORKSPACE", os.path.join(_PROJECT_ROOT, "workspace"))).resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)
_README = WORKSPACE / "README.txt"
_README.write_text(
    "ALEXIS — workspace de tareas (perímetro actual del demo).\n\n"
    "ALEXIS trabaja por capacidades gobernadas por política. Hoy, en el demo, "
    "el perímetro habilitado es este directorio (fs.read/stat/write/remove), "
    "herramientas de escritorio y voz; el resto de capacidades se habilitan con su "
    "propia política, sandbox y aprobación (ver docs/AUTONOMY-V0.5-CAPABILITIES.md).\n"
    "En este perímetro: leer → automático · crear/editar → automático · borrar → "
    "DELICADO, pedirá tu aprobación (o corre solo si el envelope lo delega).\n"
    "Sin permisos arbitrarios y sin salir del perímetro autorizado.\n",
    encoding="utf-8",
)

SANDBOX = SandboxRunner(workspace=WORKSPACE)

# --- F1 Capabilities: catálogo honesto + registro habilitado ---
CAPABILITIES = build_catalog()
ENABLED_CAPABILITIES = [s.id for s in CAPABILITIES.enabled()]

FS_TOOLS = build_filesystem_tools(WORKSPACE)
TOOLS = ToolRegistry()
TOOLS.register_all(FS_TOOLS)
# P0 §5.4: execute.test tiene adaptador real; antes solo existía en el catálogo.
TOOLS.register_all(build_test_tools(WORKSPACE))
TOOLS.register_all(build_desktop_tools())

# Link tools ↔ capabilities (mapping post-registration, inmutable en demo)
TOOL_CAP_MAP = {
    "fs.read": "fs.read",
    "fs.stat": "fs.stat",
    "fs.write": "fs.write",
    "fs.remove": "fs.remove",
    "chrome.open_url": "desktop.tools",
    "spotify.play": "desktop.tools",
    "claude.open": "desktop.tools",
    "binance.open": "desktop.tools",
    "cursor.open": "desktop.tools",
    "tts.speak": "tts.speak",
}
SANDBOX_PROFILE_MAP = {
    "fs.read": "sandbox-project",
    "fs.stat": "sandbox-project",
    "fs.write": "sandbox-project",
    "fs.remove": "sandbox-project",
    "chrome.open_url": "host-delegated",
    "spotify.play": "host-delegated",
    "claude.open": "host-delegated",
    "binance.open": "host-delegated",
    "cursor.open": "host-delegated",
    "tts.speak": "host-delegated",
}
for t in TOOLS.list():
    cid = TOOL_CAP_MAP.get(t.name)
    if cid and CAPABILITIES.has(cid):
        t.capability_id = cid
        t.sandbox_profile = SANDBOX_PROFILE_MAP.get(cid)

RUNTIME = AlexisRuntime(
    planner=Planner(),
    policy=PolicyEngine(),
    executor=SandboxExecutor(
        tools=TOOLS,
        sandbox=SANDBOX,
        desktop_delegate="host" if os.environ.get("ALEXIS_DESKTOP_MODE", "host") == "host" else None,
        voice_mode_provider=lambda: voice_mode_on(),
    ),
    verifier=FilesystemVerifier(workspace=WORKSPACE),
    memory=InMemoryMemory(),
    learning=ExperienceLearner(),
    event_bus=EVENTS,
    mission_repo=STORAGE.get("mission"),
    event_repo=STORAGE.get("event"),
    audit_repo=STORAGE.get("audit"),
    verification_repo=STORAGE.get("verification"),
    task_runner=None,
    observation_repo=STORAGE.get("observation"),
    gate=AutonomyGate(),
)
RUNNER = None
if STORAGE.get("task") and STORAGE.get("execution"):
    RUNNER = TaskRunner(
        task_repo=STORAGE.get("task"),
        execution_repo=STORAGE.get("execution"),
        checkpoint_repo=STORAGE.get("checkpoint"),
        event_bus=EVENTS,
    )
    RUNTIME.task_runner = RUNNER
else:
    print("[warn] task runtime offline (sin DB): pasos vía sandbox sin tasks/checkpoints.")
SCHEDULER = Scheduler(task_repo=STORAGE.get("task"), event_bus=EVENTS) if STORAGE.get("task") else None
MISSIONS = MissionEngine()
RUNNING = {}
STATE = {"mission_id": None, "verification": None}
WORLD = WorldModel()
WORLD.upsert(WorldEntity("postgres", "database", "PostgreSQL pgvector", {"host": "127.0.0.1:5433", "db": "alexis"}))
WORLD.upsert(WorldEntity("workspace", "sandbox", "Workspace autorizado read/write", {"path": str(WORKSPACE), "tools": [t.name for t in TOOLS.list()]}))
WORLD.upsert(
    WorldEntity(
        "desktop",
        "tools",
        "Herramientas de escritorio (chrome/spotify/claude/binance/cursor/tts) registradas en el ToolRegistry",
        {"tools": [t.name for t in TOOLS.list() if t.name not in {f.name for f in FS_TOOLS}]},
    )
)
for _tool in TOOLS.list():
    WORLD.declare_tool(
        _tool.name,
        {
            "sandbox": getattr(_tool, "sandbox_profile", None),
            "timeout": getattr(_tool, "timeout", None),
        },
    )

# --- Self Model: autoconocimiento operacional + presencia (F0-Self + F1 Capabilities). ---
# Se actualiza consumiendo los eventos reales del bus; el frontend solo dibuja.
SELF = SelfModel(
    capabilities=[s.id for s in CAPABILITIES.specs()],  # catálogo completo (incl. missing)
    available=[s.id for s in CAPABILITIES.enabled()],  # lo que ALEXIS puede hacer de verdad
    resources={
        "workspace": str(WORKSPACE),
        "sandbox_no_network": True,
    },
)


def _resolve_mission():
    return RUNNING.get(STATE.get("mission_id"))


def _self_aux():
    return {
        "tools": TOOLS.list(),
        "commitments": [
            {"id": m.id, "objective": m.goal.objective, "state": m.state.value}
            for m in RUNNING.values()
        ],
        "lessons": [],
        "memory_items": getattr(RUNTIME.memory, "items", []),
        "verification": STATE.get("verification"),
        "available": ENABLED_CAPABILITIES,
    }


SELF_SYNC = SelfModelSync(SELF, _resolve_mission, aux=_self_aux)
SELF_SYNC.attach(EVENTS, LOOP)

# --- F2.3: Cognitive Core mínimo (clasificación de intención con modelo real) ----
# El provider se elige por entorno (P2: el Core no depende de ninguno en concreto).
# Sin provider real configurado, el router responde DEGRADED con EchoModel y el
# clasificador cae a reglas; en ambos casos la procedencia se reporta al usuario (P1).
MODEL_CONFIG = ModelConfig.from_env()
MODEL_ROUTER = ModelRouter(
    allow_degraded=MODEL_CONFIG.allow_degraded(),
    budget_usd=MODEL_CONFIG.budget_usd,
    event_bus=EVENTS,
)
for _provider in MODEL_CONFIG.build_providers():
    MODEL_ROUTER.register(_provider)
_degraded_provider = MODEL_CONFIG.build_degraded()
if _degraded_provider is not None:
    MODEL_ROUTER.register(_degraded_provider)
INTENT_CLASSIFIER = IntentClassifier(MODEL_ROUTER)

_real_providers = [p.id for p in MODEL_ROUTER.providers() if not p.degraded and p.available]
print(
    "[model] provider(s) real(es): "
    f"{_real_providers or 'ninguno (DEGRADED por configuración)'} "
    f"| fallback: {'degraded' if MODEL_CONFIG.allow_degraded() else 'none'}"
)


def _create_mission_from_intent(intent):
    """Mismo envelope que usa POST /missions (permisos intactos); sólo cambia quién
    decide que esto es una tarea: el Intent, no una URL."""
    envelope = MissionEnvelope(
        objective=intent.objective or intent.utterance,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=[
            "understand", "analyze", "research", "execute", "verify",
            "modify", "test", "commit", "write", "respond",
        ],
        capabilities=ENABLED_CAPABILITIES,
    )
    mission = MISSIONS.create(
        envelope.objective,
        envelope,
        success_criteria=intent.success_criteria,
    )
    RUNNING[mission.id] = mission
    STATE["mission_id"] = mission.id
    # La propuesta del modelo se guarda como PROPUESTA auditable, nunca como permiso.
    mission.context["intent"] = intent.to_dict()
    if intent.requested_capabilities:
        mission.context["capability_proposal"] = {
            "capabilities": list(intent.requested_capabilities),
            "source": "model",
            "note": "sugerencia; no es permiso (P3.5). La autoriza catálogo+envelope+policy.",
        }
    return mission


CONVERSATION = ConversationSession(
    classifier=INTENT_CLASSIFIER,
    self_model=SELF,
    bus=EVENTS,
    create_mission=_create_mission_from_intent,
    enqueue=lambda mission: _enqueue_or_run(mission),
    capability_registry=CAPABILITIES,
)

# --- F2.1/Fase 1: Cognitive Runtime (ALEXIS_COGNITIVE=1) --------------------
# Con el flag apagado (por defecto) el runtime legacy recorre el plan una vez, igual
# que hasta ahora. Con el flag activo, `AlexisRuntime.run_mission` pide una decisión
# antes de cada acción y vuelve a decidir después de observar. Mismo executor, misma
# policy, mismo verifier: lo que cambia es quién decide el siguiente paso.
if os.environ.get("ALEXIS_COGNITIVE", "0") == "1":

    async def _cognitive_execute(mission, step, tool_name=None):
        if RUNNER is not None:
            async def _run(_mission, _step):
                return await RUNTIME.executor.execute(_mission, _step, tool_name=tool_name)

            result, task = await RUNNER.run_step(mission, step, _run)
            if task.status.value == "failed":
                await RUNNER.close_open_tasks(mission.id, status="cancelled")
            return result
        return await RUNTIME.executor.execute(mission, step, tool_name=tool_name)

    memory_provider = None
    if STORAGE.get("db") is not None:
        memory_provider = PostgresMemoryProvider(STORAGE["db"])
    else:
        memory_provider = InProcessMemoryProvider(RUNTIME.memory)
    print(f"[cognitive] memoria: {memory_provider.id}")

    RUNTIME.plan_validator = PlanValidator(catalog=CAPABILITIES, policy=RUNTIME.policy)
    if os.environ.get("ALEXIS_MODEL_PLANNER", "0") == "1":
        RUNTIME.plan_model = ModelPlanner(MODEL_ROUTER, catalog=CAPABILITIES)
        print("[cognitive] ModelPlanner activo (ALEXIS_MODEL_PLANNER=1): el modelo propone, el validador decide")
    else:
        print("[cognitive] plan por reglas (ALEXIS_MODEL_PLANNER != 1); el ModelPlanner está disponible")

    RUNTIME.cognitive = CognitiveRuntime(
        policy=RUNTIME.policy,
        gate=RUNTIME.gate,
        executor=RUNTIME.executor,
        verifier=RUNTIME.verifier,
        execute=_cognitive_execute,
        model_router=MODEL_ROUTER,
        memory=memory_provider,
        self_model=SELF,
        world=WORLD,
        plan_validator=RUNTIME.plan_validator,
    )
    print("[cognitive] CognitiveRuntime activo (ALEXIS_COGNITIVE=1): decide→policy→execute→observe→evaluate")
else:
    print("[cognitive] runtime legacy (ALEXIS_COGNITIVE != 1): el plan se recorre una vez")

# --- Percepción: la palmada es solo una FUENTE de eventos. -------------------
# El cereor es ALEXIS: el evento se convierte en una misión de activación que
# atraviesa el mismo pipeline (planner → policy → executor → verifier) que el chat.
ACTIVATION_COOLDOWN_S = 10.0


def on_clap_event(event) -> None:
    set_voice_mode(True)
    now = time.time()
    if now - STATE.get("last_clap_at", 0.0) < ACTIVATION_COOLDOWN_S:
        return
    STATE["last_clap_at"] = now
    asyncio.run_coroutine_threadsafe(EVENTS.publish("presence.listening", {"source": "clap"}), LOOP)
    mission = RUNNING.get(STATE.get("mission_id"))
    if mission is not None and mission.state.value in ("planning", "running", "verifying"):
        return
    envelope = MissionEnvelope(
        objective=ACTIVATION_OBJECTIVE,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=[
            "understand", "analyze", "research", "execute", "verify",
            "modify", "test", "commit", "write", "respond",
        ],
        capabilities=ENABLED_CAPABILITIES,
    )
    mission = MISSIONS.create(ACTIVATION_OBJECTIVE, envelope)
    RUNNING[mission.id] = mission
    STATE["mission_id"] = mission.id
    _enqueue_or_run(mission)


CLAP = ClapListener()
_clap_sub = EVENTS.subscribe_async()


async def _clap_consumer():
    while True:
        item = await _clap_sub.get()
        if item["topic"] == DEFAULT_TOPIC:
            on_clap_event(item["payload"])


asyncio.run_coroutine_threadsafe(_clap_consumer(), LOOP)
if os.environ.get("ALEXIS_CLAP_ENABLED", "1") == "1":
    ok, note = CLAP.start(EVENTS, DEFAULT_TOPIC, LOOP)
    print(f"[clap] listener: {note}")


async def run_mission(mission):
    # Sincronizar YA la vista en memoria (RUNNING) con el objeto que el worker/runner
    # está mutando: /state y /self ven el estado real durante la ejecución, no al final.
    RUNNING[mission.id] = mission
    await RUNTIME.run_mission(mission)
    # Con cola DB, el worker corre el objeto recargado de Postgres; mantener en
    # sincronía la vista para que /state y /missions muestren el estado real.
    RUNNING[mission.id] = mission
    verification = RUNTIME.latest_verification
    STATE["verification"] = verification if verification and verification["mission_id"] == mission.id else None
    _touch_voice()
    await _announce_voice(mission)


# --- Cola de misiones: una a la vez en orden, con checkpoint resumible. ----
WORKER = MissionWorker(runner=run_mission, mission_repo=STORAGE.get("mission")) if STORAGE.get("mission") else None


async def _worker_loop():
    await WORKER.loop()


if WORKER is not None:
    asyncio.run_coroutine_threadsafe(_worker_loop(), LOOP)
    print("[cola] worker de misiones activo (FIFO persistente en PostgreSQL)")


def _enqueue_or_run(mission):
    """Con cola: encola (la procesa el worker). Sin cola: corre directo (offline)."""
    if WORKER is not None:
        asyncio.run_coroutine_threadsafe(enqueue(WORKER.mission_repo, mission), LOOP)
        return
    asyncio.run_coroutine_threadsafe(run_mission(mission), LOOP)


def _voice_dir() -> Path:
    override_cfg = (os.environ.get("ALEXIS_TTS_CACHE_DIR") or os.environ.get("JARVIS_WELCOME_CACHE_DIR") or "").strip()
    if override_cfg:
        return Path(override_cfg).expanduser().resolve()
    return Path(_PROJECT_ROOT) / ".cache" / "tts"


# --- Modo voz (palmada = trigger, estilo ChatGPT). --------------------------
# Es una SESIÓN: la palmada lo ACTIVA y ALEXIS habla mientras haya interacción
# de voz/mic; si no hay actividad durante _VOICE_IDLE_S vuelve solo a texto.
# El default es texto (OFF): solo la palmada (o el toggle manual) lo enciende.
_VOICE_MODE_DEFAULT = os.environ.get("ALEXIS_VOICE_MODE_DEFAULT", "0").strip().lower() in ("1", "on", "true", "yes")
_VOICE_IDLE_S = float(os.environ.get("ALEXIS_VOICE_IDLE_S", "30"))


def _touch_voice() -> None:
    STATE["voice_last_activity"] = time.time()


def voice_mode_on() -> bool:
    """Estado REAL del modo voz; se apaga solo tras _VOICE_IDLE_S sin actividad."""
    if not STATE.get("voice_mode", _VOICE_MODE_DEFAULT):
        return False
    if time.time() - STATE.get("voice_last_activity", 0.0) > _VOICE_IDLE_S:
        STATE["voice_mode"] = False
        asyncio.run_coroutine_threadsafe(
            EVENTS.publish("voice.mode", {"enabled": False, "reason": "idle"}), LOOP
        )
        return False
    return True


def set_voice_mode(enabled: bool) -> dict:
    STATE["voice_mode"] = bool(enabled)
    if enabled:
        _touch_voice()
    asyncio.run_coroutine_threadsafe(
        EVENTS.publish("voice.mode", {"enabled": STATE["voice_mode"], "reason": "manual"}), LOOP
    )
    return {"enabled": STATE["voice_mode"]}


STATE["voice_mode"] = _VOICE_MODE_DEFAULT
_touch_voice()


def _publish_voice(path: str, mission_id: str) -> bool:
    """Copia el audio sintetizado con nombre ÚNICO en la carpeta que vigila el
    `voice_bridge` del host: así se reproduce por los altavoces aunque el mismo
    texto ya se haya dicho antes (cada respuesta es un archivo nuevo)."""
    src = Path(path)
    if not src.is_file():
        return False
    dst_dir = _voice_dir()
    dst_dir.mkdir(parents=True, exist_ok=True)
    ext = (src.suffix or ".wav").lstrip(".")
    dst = dst_dir / f"{mission_id}-{int(time.time() * 1000)}.{ext}"
    try:
        import shutil

        shutil.copy2(src, dst)
        return True
    except OSError:
        return False


async def _announce_voice(mission) -> None:
    """Hace hablar a ALEXIS al terminar la misión (host altavoces vía bridge).

    - Si el plan ya respondió por voz (activación por palmada, misiones desktop),
      solo se publica una copia única de ese audio.
    - El resto de misiones sin paso `respond` reciben un resumen hablado honesto.
    """
    if mission.state not in (MissionState.COMPLETED, MissionState.FAILED, MissionState.BLOCKED):
        return
    if not voice_mode_on():
        return
    for res in reversed(mission.results or []):
        out = res.get("output") or {}
        if isinstance(out, dict) and out.get("action") == "respond":
            tts = out.get("tts") or {}
            if tts.get("ok") and tts.get("path"):
                _publish_voice(str(tts.get("path")), mission.id)
            return
    objective = mission.goal.objective
    if mission.state is MissionState.COMPLETED:
        text = f"Listo, completé la misión: {objective}."
    elif mission.state is MissionState.FAILED:
        text = "No pude completar esa misión. Revisa el objetivo y vuelve a intentarlo."
    else:
        text = "No logré confirmar el resultado. Dame más contexto o un objetivo más claro."
    asyncio.run_coroutine_threadsafe(EVENTS.publish("presence.speaking", {"text": text}), LOOP)
    tts_result = await synthesize_with_fallback(text, provider=get_tts_provider())
    if tts_result.ok and tts_result.path:
        _publish_voice(str(tts_result.path), mission.id)
    asyncio.run_coroutine_threadsafe(EVENTS.publish("presence.idle", {}), LOOP)


recover_missions()


def pending_mission(mission_id):
    mission = RUNNING.get(mission_id)
    if mission is None:
        return None
    if mission.state is not MissionState.WAITING_APPROVAL:
        return None
    return mission


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _state_payload(self):
        mission = RUNNING.get(STATE.get("mission_id"))
        mission_payload = None
        if mission is not None:
            mission_payload = {
                "id": mission.id,
                "state": mission.state.value,
                "autonomy": mission.envelope.autonomy.value,
                "objective": mission.goal.objective,
                "results": mission.results,
                "plan": [s["id"] for s in mission.context.get("plan_steps", [])],
                "pending_approval": mission.context.get("pending_approval"),
                "blocked_reason": mission.context.get("blocked_reason"),
                "decisions": list((mission.context.get("decisions") or {}).values()),
                "evaluations": mission.context.get("evaluations"),
                "resumed_at_step": mission.context.get("resumed_at_step"),
                "approved_steps": mission.context.get("approved_step_ids", []),
                "allowed_actions": mission.envelope.allowed_actions,
                "forbidden_actions": mission.envelope.forbidden_actions,
            }
        tasks = []
        checkpoint = None
        if RUNNER is not None and mission is not None:
            async def _load():
                return {
                    "tasks": [
                        {
                            "id": t.id,
                            "tool": t.tool,
                            "status": t.status.value,
                            "attempts": t.attempts,
                            "max_attempts": t.max_attempts,
                            "error": t.error,
                        }
                        for t in await STORAGE["task"].list(mission_id=mission.id, limit=50)
                    ],
                    "checkpoint": await STORAGE["checkpoint"].latest(mission.id),
                }
            try:
                data = asyncio.run_coroutine_threadsafe(_load(), LOOP).result(timeout=5)
                tasks = data["tasks"]
                checkpoint = data["checkpoint"]
            except Exception:
                tasks, checkpoint = [], None
        memory = [
            {"mission_id": mid, "source": obs.source, "content": obs.content}
            for mid, obs in getattr(RUNTIME.memory, "items", [])
        ]
        return {
            "mission": mission_payload,
            "verification": STATE.get("verification"),
            "present": present(
                {
                    "mission": mission_payload,
                    "verification": STATE.get("verification"),
                    "memory": memory,
                }
            ),
            "tasks": tasks,
            "checkpoint_step": int(checkpoint["step_index"]) if checkpoint else None,
            "agents": [{"name": name, "description": desc} for name, desc in AGENTS.items()],
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "risk": t.risk,
                    "schema": t.schema,
                    "timeout": t.timeout,
                    "permissions": t.permissions,
                    "limits": t.limits,
                }
                for t in TOOLS.list()
            ],
            "world": [
                {"id": e.id, "kind": e.kind, "name": e.name, "attributes": e.attributes}
                for e in WORLD.snapshot()
            ],
            "workspace": {"path": str(WORKSPACE), "sandbox": {"timeout_default_s": SANDBOX.timeout}},
            "memory": memory,
            "events_count": len(EVENTS.events),
            "missions": [
                {"id": m.id, "state": m.state.value, "objective": m.goal.objective, "autonomy": m.envelope.autonomy.value}
                for m in RUNNING.values()
            ],
            "queue": {
                "enabled": WORKER is not None,
                "active": (WORKER.active.id if WORKER is not None and WORKER.active is not None else None),
            },
            "voice_mode": voice_mode_on(),
        }

    def _send_file(self, abspath: str):
        if not os.path.isfile(abspath):
            self._send_json({"error": "not found"}, 404)
            return
        ctype, _ = guess_type(abspath)
        with open(abspath, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _face_index(self):
        self._send_file(os.path.join(FACE_DIST, "index.html"))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/avatar":
            if os.path.isdir(FACE_DIST):
                self._face_index()
            else:
                self._send_json({"error": "face dist no construido (npm run build en apps/face)"}, 503)
        elif path == "/classic":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/face/"):
            rel = path[len("/face/"):]
            if not rel:
                self._face_index()
            else:
                self._send_file(os.path.join(FACE_DIST, rel))
        elif path == "/face.jpg":
            if os.path.exists(FACE):
                with open(FACE, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send_json({"error": "face image not found"}, 404)
        elif path == "/stream":
            self._stream()
        elif path == "/missions":
            self._send_json([
                {"id": m.id, "state": m.state.value, "objective": m.goal.objective}
                for m in RUNNING.values()
            ])
        elif path == "/self":
            self._send_json(
                {
                    **SELF.snapshot(),
                    "questions": {
                        "queCreesQueSoy": SELF.answer("¿qué soy?"),
                        "queEstasHaciendo": SELF.answer("¿qué estoy haciendo?"),
                        "quePuedesHacer": SELF.answer("¿qué puedo hacer?"),
                        "queNoPuedesHacer": SELF.answer("¿qué no puedo hacer?"),
                        "confianza": SELF.answer("¿qué tan segura es mi conclusión?"),
                        "queNecesitas": SELF.answer("¿qué necesito para continuar?"),
                        "queSabes": SELF.answer("¿qué sé?"),
                        "queNoSabes": SELF.answer("¿qué no sé?"),
                        "queHaPasado": SELF.answer("¿qué acaba de ocurrir?"),
                    },
                }
            )
        elif path == "/capabilities":
            self._send_json({
                "specs": [
                    {
                        "id": s.id,
                        "sphere": s.sphere,
                        "network": s.network,
                        "side_effects": s.side_effects,
                        "trust_domain": s.trust_domain,
                        "sandbox_profile": s.sandbox_profile,
                        "default_risk": s.default_risk,
                        "audit": s.audit,
                        "plans_action": s.plans_action,
                        "requires_input": s.requires_input,
                        "status": s.status,
                        "resources": s.resources,
                        "description": s.description,
                        "enabled": s.id in ENABLED_CAPABILITIES,
                    }
                    for s in CAPABILITIES.specs()
                ],
                "enabled": ENABLED_CAPABILITIES,
                "missing": [s.id for s in CAPABILITIES.specs() if s.status == "missing"],
                "base": [s.id for s in CAPABILITIES.specs() if s.status == "base"],
                "model": {
                    "provider_config": MODEL_CONFIG.provider,
                    "allow_degraded": MODEL_CONFIG.allow_degraded(),
                    "providers": MODEL_ROUTER.describe(),
                    "routings": MODEL_ROUTER.routings[-10:],
                },
            })
        elif path == "/state":
            self._send_json(self._state_payload())
        elif path == "/voice-mode":
            self._send_json({"enabled": voice_mode_on()})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/chat":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            utterance = (data.get("text") or data.get("utterance") or "").strip()
            if not utterance:
                self._send_json({"error": "falta 'text'"}, 400)
                return

            async def _turn():
                return await CONVERSATION.handle_turn(utterance, source=data.get("source", "user"))

            try:
                reply = asyncio.run_coroutine_threadsafe(_turn(), LOOP).result(timeout=180)
            except Exception as exc:  # noqa: BLE001 — el turno responde error, no cuelga
                self._send_json({"error": f"turno fallido: {type(exc).__name__}: {exc}"}, 500)
                return
            self._send_json(reply.to_dict())
        elif path == "/missions":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            objective = data.get("objective", "Objetivo sin especificar")
            requested_autonomy = data.get("autonomy", "supervised")
            try:
                autonomy = AutonomyLevel(requested_autonomy)
            except ValueError:
                autonomy = AutonomyLevel.SUPERVISED
            requested_capabilities = data.get("capabilities")
            if requested_capabilities is not None:
                envelope_capabilities = [cid for cid in requested_capabilities if CAPABILITIES.has(cid)]
            else:
                envelope_capabilities = ENABLED_CAPABILITIES
            envelope = MissionEnvelope(
                objective=objective,
                autonomy=autonomy,
                allowed_actions=[
                    "understand", "analyze", "research", "execute", "verify",
                    "modify", "test", "commit", "write", "respond",
                ],
                capabilities=envelope_capabilities,
            )
            mission = MISSIONS.create(objective, envelope)
            RUNNING[mission.id] = mission
            STATE["mission_id"] = mission.id
            _enqueue_or_run(mission)
            self._send_json({"id": mission.id, "state": mission.state.value, "objective": objective, "autonomy": autonomy.value, "capabilities": envelope_capabilities})
        elif path.startswith("/missions/") and path.endswith("/approve"):
            mission_id = path.split("/")[2]
            mission = pending_mission(mission_id)
            if mission is None:
                self._send_json({"error": "mission not found or not waiting approval"}, 409)
                return
            if "execute" not in mission.envelope.allowed_actions:
                mission.envelope.allowed_actions.append("execute")
            approved = set(mission.context.get("approved_step_ids", []))
            step_id = (mission.context.get("pending_approval") or {}).get("step")
            if step_id:
                approved.add(step_id)
                mission.context["approved_step_ids"] = sorted(approved)
            mission.context.pop("pending_approval", None)
            mission.results.clear()
            _enqueue_or_run(mission)
            self._send_json({"id": mission.id, "state": mission.state.value})
        elif path.startswith("/missions/") and path.endswith("/deny"):
            mission_id = path.split("/")[2]
            mission = pending_mission(mission_id)
            if mission is None:
                self._send_json({"error": "mission not found or not waiting approval"}, 409)
                return
            MISSIONS.stop(mission, "Denied by human")
            mission.context.pop("pending_approval", None)
            if STORAGE.get("mission"):
                asyncio.run_coroutine_threadsafe(STORAGE["mission"].upsert(mission), LOOP)
            asyncio.run_coroutine_threadsafe(EVENTS.publish("mission.cancelled", mission.id), LOOP)
            self._send_json({"id": mission.id, "state": mission.state.value})
        elif path == "/voice-mode":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            self._send_json(set_voice_mode(bool(data.get("enabled", voice_mode_on()))))
        elif path == "/clap":
            on_clap_event({"source": "simulated"})
            self._send_json({"ok": True, "note": "clap simulated - modo voz activado y pipeline de activación disparado"})
        else:
            self._send_json({"error": "not found"}, 404)

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        sub = EVENTS.subscribe()
        try:
            while True:
                try:
                    item = sub.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(item, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    port = int(__import__("os").environ.get("ALEXIS_DEMO_PORT", "8100"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"ALEXIS demo en http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()