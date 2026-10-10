"""CORE-03 — La aplicación OFICIAL de ALEXIS, y el único punto de construcción del runtime.

Este módulo existe para cerrar la duplicación que el audit maestro detectó: había dos
recorridos incompatibles y ambos seAutoproclamaban "el runtime" (`apps/demo` con
CognitiveRuntime y PostgreSQL, `apps/api` con `LocalExecutor` + `BasicVerifier` + un
`store` en un dict). Aquí vive la ruta oficial, y es la ÚNICA que construye:

    EventBus · CapabilityCatalog · PolicyEngine · AutonomyGate · SandboxExecutor
    · CognitiveRuntime (+ GoalVerifier) · AlexisRuntime · ConversationSession
    · MissionEngine · WorldModel · SelfModel · ModelRouter · IntentClassifier

Reglas estructurales que este módulo sostiene:

- **Una sola construcción.** `build_official_runtime()` es el único sitio donde se
  instancia un `AlexisRuntime`, un `CognitiveRuntime`, un `EventBus`, un
  `AutonomyGate`, un `PolicyEngine` o un `GoalVerifier`. `get_official_runtime()`
  devuelve SIEMPRE la misma instancia dentro del proceso: dos peticiones no pueden
  acabar con dos runtimes distintos.
- **El `CognitiveRuntime` no es opcional.** No hay rama que lo apague. Un runtime
  oficial sin `goal_verifier` no se puede construir, porque `settle()` sólo autoriza
  `COMPLETED` con un `GoalVerification` real (CORE-01).
- **PostgreSQL es la fuente de verdad.** El estado de una misión se lee y se escribe en
  los repositorios; el dict en memoria es una vista del proceso, no el estado.
- **Importable sin efectos secundarios.** Importar este módulo no abre puertos, no
  conecta con la base de datos ni arranca hilos. Los efectos (loop, storage, worker,
  clap, voz, cara) son del lanzador (`apps/demo/server.py`).

Lo que NO vive aquí: la presentación del demo (avatar/face/clap/voz). Eso se inyecta
como `presentation` y lo sirve `server.py`; la superficie oficial —`/health`, `/ui`,
`/chat`, `/missions*`, `/stream`, `/state`, `/self`, `/capabilities`— es de esta
aplicación y no depende del demo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from mimetypes import guess_type
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from apps.demo.runtime_flags import resolve_cognitive_runtime
from apps.ui import PAGE
from alexis.agents.registry import AGENTS
from alexis.autonomy.gates import AutonomyGate
from alexis.autonomy.mission import MissionEngine
from alexis.autonomy.queue import MissionWorker, enqueue
from alexis.autonomy.scheduler import Scheduler
from alexis.autonomy.task_runner import TaskRunner
from alexis.capabilities import build_catalog
from alexis.cognition.conversation import ConversationSession
# CORE-02 no se modifica ni se reimplementa aquí: `normalize_criteria` se REUTILIZA tal
# cual, para que /missions y /chat apliquen el MISMO contrato de success_criteria.
from alexis.cognition.criteria import normalize_criteria
from alexis.cognition.goal_verification import GoalVerifier
from alexis.cognition.intent_classifier import IntentClassifier
from alexis.cognition.loop import CognitiveRuntime
from alexis.cognition.planner import Planner
from alexis.cognition.planner_model import ModelPlanner, PlanValidator
from alexis.contracts import AutonomyLevel, MissionEnvelope, MissionState
from alexis.core.runtime import AlexisRuntime
from alexis.events.bus import EventBus
from alexis.execution import SandboxExecutor
from alexis.experience.presenter import present
from alexis.learning.system import ExperienceLearner
from alexis.memory.provider import InProcessMemoryProvider, PostgresMemoryProvider
from alexis.memory.store import InMemoryMemory
from alexis.models.config import ModelConfig
from alexis.models.router import ModelRouter
from alexis.security.policy import PolicyEngine
from alexis.security.sandbox import SandboxRunner
from alexis.self.model import SelfModel
from alexis.tools.browser import build_browser_tools
from alexis.tools.desktop import build_desktop_tools
from alexis.tools.filesystem import build_filesystem_tools
from alexis.tools.testrunner import build_test_tools
from alexis.tools.registry import ToolRegistry
from alexis.verification import FilesystemVerifier
from alexis.world.model import Scope, WorldEntity, WorldModel

LOGGER = logging.getLogger("alexis.app")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Acciones permitidas en el envelope de una misión creada por la vía oficial. Es el
#: mismo conjunto que usaba el demo; vive aquí porque la aplicación oficial, y no el
#: demo, es quien concede estos permisos.
ALLOWED_ACTIONS = [
    "understand", "analyze", "research", "execute", "verify",
    "modify", "test", "commit", "write", "respond",
]

#: Enlace tool ↔ capability. Post-registro e inmutable: describe lo que YA está
#: construido, no lo autoriza.
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
    "browser.research": "browser.research",
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
    # Perfil de red declarado. NO confiere aislamiento de kernel: es la etiqueta que
    # autoriza el egress a nivel de aplicación (docs/SECURITY.md).
    "browser.research": "browser-sandbox",
}

WORKSPACE_README = (
    "ALEXIS — workspace de tareas (perímetro actual del demo).\n\n"
    "ALEXIS trabaja por capacidades gobernadas por política. Hoy, en el demo, "
    "el perímetro habilitado es este directorio (fs.read/stat/write/remove), "
    "herramientas de escritorio y voz; el resto de capacidades se habilitan con su "
    "propia política, sandbox y aprobación (ver docs/AUTONOMY-V0.5-CAPABILITIES.md).\n"
    "En este perímetro: leer → automático · crear/editar → automático · borrar → "
    "DELICADO, pedirá tu aprobación (o corre solo si el envelope lo delega).\n"
    "Sin permisos arbitrarios y sin salir del perímetro autorizado.\n"
)


# --------------------------------------------------------------------------- #
# Persistencia: PostgreSQL es la fuente de verdad
# --------------------------------------------------------------------------- #


def init_storage(loop: asyncio.AbstractEventLoop) -> dict:
    """Abre los repositorios de PostgreSQL. Sin base, devuelve `{}` y lo dice.

    Se degrada a memoria sin morir, pero lo dice: un arranque sin base no es lo mismo
    que un arranque con la base y cero misiones.
    """
    storage: dict[str, Any] = {}
    try:
        from alexis.storage.db import Database
        from alexis.storage.repositories import (
            AuditRepository,
            CheckpointRepository,
            LearningRepository,
            EventRepository,
            ExecutionRepository,
            MissionRepository,
            ObservationRepository,
            TaskRepository,
            VerificationRepository,
            WorldRepository,
        )
    except ImportError as exc:
        print(f"[warn] persistencia no disponible (faltan dependencias deps): {exc}")
        return storage

    async def _open_and_migrate(db):
        await db.open()
        await db.migrate()

    try:
        db = Database()
        asyncio.run_coroutine_threadsafe(_open_and_migrate(db), loop).result(timeout=10)
        storage["db"] = db
        storage["mission"] = MissionRepository(db)
        storage["event"] = EventRepository(db)
        storage["audit"] = AuditRepository(db)
        storage["verification"] = VerificationRepository(db)
        storage["task"] = TaskRepository(db)
        storage["execution"] = ExecutionRepository(db)
        storage["observation"] = ObservationRepository(db)
        storage["checkpoint"] = CheckpointRepository(db)
        storage["world"] = WorldRepository(db)
        # CORE-11: el aprendizaje persiste en la MISMA base, no en una segunda. Una skill que
        # sólo vive en RAM no es una skill: es un texto que se pierde al reiniciar.
        storage["learning"] = LearningRepository(db)
        print("Persistencia PostgreSQL activa (missions, tasks, executions, checkpoints, "
              "verifications, observations, audit, world, learning).")
    except Exception as exc:  # noqa: BLE001 — sin base se arranca igual, pero se avisa
        print(f"[warn] persistencia no disponible: {exc}")
    return storage


# --------------------------------------------------------------------------- #
# El runtime oficial
# --------------------------------------------------------------------------- #


@dataclass
class OfficialRuntime:
    """Todo el runtime oficial, en un solo objeto y una sola instancia.

    El dataclass no es adorno: obliga a que la construcción pase por
    `build_official_runtime()` y a que quien necesite una pieza la pida de aquí, en
    lugar de construir la suya al lado.
    """

    loop: asyncio.AbstractEventLoop
    workspace: Path
    sandbox: SandboxRunner
    capabilities: Any
    enabled_capabilities: list[str]
    tools: ToolRegistry
    events: EventBus
    storage: dict
    runtime: AlexisRuntime
    cognitive: CognitiveRuntime
    missions: MissionEngine
    running: dict
    state: dict
    world: WorldModel
    self_model: SelfModel
    model_config: ModelConfig
    model_router: ModelRouter
    intent_classifier: IntentClassifier
    conversation: ConversationSession
    runner: Any = None
    scheduler: Any = None
    worker: Any = None
    plan_validator: Any = None
    hydrated_entities: int = 0
    extras: dict = field(default_factory=dict)

    # -- Atajos de lectura -------------------------------------------------- #
    @property
    def has_persistence(self) -> bool:
        return bool(self.storage.get("mission"))

    def mission_repo(self):
        return self.storage.get("mission")

    def run_mission(self, mission):
        return self.extras["run_mission"](mission)

    def enqueue_or_run(self, mission):
        return self.extras["enqueue_or_run"](mission)

    def pending_mission(self, mission_id):
        return self.extras["pending_mission"](mission_id)

    def payload(self, mission) -> dict:
        """Representación de una misión. Misma forma en toda la superficie oficial."""
        return {
            "id": mission.id,
            "state": mission.state.value,
            "objective": mission.goal.objective,
            "autonomy": mission.envelope.autonomy.value,
            "success_criteria": list(mission.goal.success_criteria),
        }

    def mission_detail(self, mission) -> dict:
        """Lectura por recurso: el payload común más lo que prueba CORE-01 y CORE-02.

        `goal_verified` NO se deduce de `state == "completed"`: sale de
        `goal_is_confirmed()`, la misma autoridad que usa `settle()`. Si algún día un
        estado quedara mal, este endpoint no puede afirmar más de lo que el sistema
        demostró — que es justo lo que se quiere poder comprobar por HTTP.
        """
        from alexis.autonomy.goal_state import goal_is_confirmed

        verification = getattr(mission, "goal_verification", None)
        context = getattr(mission, "context", {}) or {}
        return {
            **self.payload(mission),
            "goal_verified": bool(goal_is_confirmed(verification, mission)),
            "goal_verification": (
                verification.to_dict() if verification is not None else None
            ),
            "goal_verification_reason": context.get("goal_verification_reason"),
            "results": list(getattr(mission, "results", []) or []),
            "plan": [s["id"] for s in context.get("plan_steps", [])],
            "decisions": list((context.get("decisions") or {}).values()),
            "pending_approval": context.get("pending_approval"),
            "blocked_reason": context.get("blocked_reason"),
            "updated_at": getattr(mission, "updated_at", None),
        }

    def find_mission(self, mission_id: str):
        """Misión por identificador, o `None`. Sólo lectura: no ejecuta ni muta.

        Dos niveles, por dos razones distintas:

        - El objeto vivo (`running`) cuando existe: es la misma referencia que muta el
          worker, así que refleja lo que está pasando AHORA. Una misión `running` puede
          no tener todavía su fila actualizada, y el repo iría por detrás.
        - La fila persistida cuando no está en memoria: cubre misiones de arranques
          anteriores, que el proceso recuperó pero no mantiene todas en `running`.

        No se escribe nada en ninguno de los dos caminos, y no se toca `state` global.
        """
        live = self.running.get(mission_id)
        if live is not None:
            return live
        repo = self.mission_repo()
        if repo is None:
            return None
        try:
            return asyncio.run_coroutine_threadsafe(
                repo.get(mission_id), self.loop
            ).result(timeout=5)
        except Exception as exc:  # noqa: BLE001 — leer el estado no puede tumbar la ruta
            LOGGER.warning("GET /missions/%s: no se pudo leer de PostgreSQL: %s", mission_id, exc)
            return None


def _plan_eligible(router, envelope) -> tuple[bool, str]:
    """¿Tiene sentido pedirle una estrategia al modelo ahora mismo?

    CORE-08A-1. Esto NO decide si el plan será válido: eso es trabajo del `PlanValidator`,
    que sigue siendo la autoridad determinista. Sólo decide si hay a quién preguntar y si
    es affordable. Que haya un provider registrado NO basta: un provider puede estar
    DEGRADED, o ser real y estar agotado.

    No se hace aquí ninguna llamada de red. Preguntar al modelo y que su respuesta acabe en
    el mismo fallback puede costar 46-60s con un modelo local, y ese precio no se paga para
    descubrir algo que ya se sabe mirando el router.

    Tres modos (`ALEXIS_MODEL_PLANNER`):
      `0`       → OFF absoluto; ni se consulta esta función.
      `1`       → ON explícito, aunque no haya provider real (para depurar).
      otro/auto → se consulta esta función.

    El motivo que devuelve acaba en `plan_provenance`, para que "planificó por reglas" sea
    una decisión reconstruible y no un silencio.
    """
    reales = [p for p in router.providers() if not p.degraded and p.available]
    if not reales:
        return False, "no hay ningún provider REAL disponible (DEGRADED/UNAVAILABLE)"
    # Presupuesto con la misma lectura que hace el router (CORE-06): `max_cost_usd == 0`
    # significa sin límite. Se consulta con la clave vacía porque en el arranque aún no hay
    # misión; el presupuesto de una misión se evalúa más tarde, en `_plan_with_model`.
    gastado = router.spent_usd_for("")
    limites = [x for x in (getattr(router, "budget_usd", 0), getattr(envelope, "max_cost_usd", 0)) if x > 0]
    if limites and gastado >= min(limites):
        return False, (
            f"sin presupuesto para planificar (gastado {gastado:.4f} USD, "
            f"límite {min(limites):.4f})"
        )
    return True, f"provider REAL elegible: {', '.join(p.id for p in reales)}"


def build_official_runtime(
    *,
    loop: asyncio.AbstractEventLoop,
    storage: dict | None = None,
    workspace: str | Path | None = None,
    voice_mode_provider: Callable[[], bool] | None = None,
    on_mission_end: Callable[[Any], Any] | None = None,
    on_mission_activity: Callable[[], Any] | None = None,
    model_planner_env: str | None = None,
) -> OfficialRuntime:
    """Construye el runtime oficial. Este es el ÚNICO punto de construcción.

    No hay rama legacy: el `CognitiveRuntime` se construye siempre, y con él el
    `GoalVerifier` de CORE-01. `resolve_cognitive_runtime()` decide si se admite pedir
    el recorrido antiguo; si se pide, aborta en vez de devolver un runtime que no puede
    cerrar objetivos (ver `runtime_flags.LegacyRuntimeProhibited`).
    """
    storage = dict(storage or {})
    voice_mode_provider = voice_mode_provider or (lambda: False)

    # `USAR_COGNITIVE` se resuelve UNA vez, aquí. Su default es el CognitiveRuntime y
    # el recorrido legacy está prohibido: no existe una rama que ejecute una misión sin
    # GoalVerifier, porque `settle()` no podría autorizar nunca un COMPLETED.
    USAR_COGNITIVE, _COGNITIVE_MOTIVO = resolve_cognitive_runtime()
    if not USAR_COGNITIVE:  # pragma: no cover — el resolver aborta antes; red de seguridad
        raise RuntimeError("el runtime oficial exige CognitiveRuntime")

    # --- Perímetro ------------------------------------------------------- #
    workspace_path = Path(
        workspace or os.environ.get("ALEXIS_WORKSPACE", str(PROJECT_ROOT / "workspace"))
    ).resolve()
    workspace_path.mkdir(parents=True, exist_ok=True)
    (workspace_path / "README.txt").write_text(WORKSPACE_README, encoding="utf-8")
    WORKSPACE = workspace_path

    SANDBOX = SandboxRunner(workspace=WORKSPACE)

    # --- Capacidades y tools ---------------------------------------------- #
    CAPABILITIES = build_catalog()
    ENABLED_CAPABILITIES = [s.id for s in CAPABILITIES.enabled()]

    FS_TOOLS = build_filesystem_tools(WORKSPACE)
    TOOLS = ToolRegistry()
    TOOLS.register_all(FS_TOOLS)
    # P0 §5.4: execute.test tiene adaptador real; antes sólo existía en el catálogo.
    TOOLS.register_all(build_test_tools(WORKSPACE))
    TOOLS.register_all(build_desktop_tools())
    for t in TOOLS.list():
        cid = TOOL_CAP_MAP.get(t.name)
        if cid and CAPABILITIES.has(cid):
            t.capability_id = cid
            t.sandbox_profile = SANDBOX_PROFILE_MAP.get(cid)

    # --- Mundo ----------------------------------------------------------- #
    # El ámbito se declara explícitamente y se deriva del workspace: sin esto el
    # mundo sería global del proceso y `file:notas.txt` significaría lo mismo en
    # cualquier proyecto.
    WORLD = WorldModel(scope=Scope.from_workspace(WORKSPACE))
    _WORLD_HYDRATED = False

    def hydrate_world_once() -> int:
        """Carga el mundo persistente UNA vez (P0 §4.4).

        Si la base no está, no se muere el arranque: se avisa y se sigue con el mundo
        en memoria. Importa NO interpretar "no pude hidratar" como "el mundo está
        vacío": son cosas distintas.
        """
        nonlocal _WORLD_HYDRATED
        repo = storage.get("world")
        if repo is None or _WORLD_HYDRATED:
            return 0

        async def _hydrate():
            scope = WORLD.scope.id
            entidades = await repo.list_entities(scope=scope)
            aristas = await repo.list_edges(scope=scope)
            return WORLD.hydrate(entidades, aristas, scope=scope)

        try:
            n = asyncio.run_coroutine_threadsafe(_hydrate(), loop).result(timeout=10)
            _WORLD_HYDRATED = True
            print(f"World Model hidratado desde PostgreSQL: {n} entidades, ámbito {WORLD.scope.id}.")
            return n
        except Exception as exc:  # noqa: BLE001 — sin base, ALEXIS arranca igual
            print(f"[warn] World Model NO hidratado (la base no respondió): {exc}. "
                  "El mundo sigue en memoria y VOLVERÁ A LEERSE al próximo arranque.")
            return 0

    hydrated = hydrate_world_once()
    WORLD.upsert(WorldEntity("postgres", "database", "PostgreSQL pgvector",
                             {"host": "127.0.0.1:5433", "db": "alexis"}))
    WORLD.upsert(WorldEntity("workspace", "sandbox", "Workspace autorizado read/write",
                             {"path": str(WORKSPACE), "tools": [t.name for t in TOOLS.list()]}))
    WORLD.upsert(WorldEntity(
        "desktop", "tools",
        "Herramientas de escritorio (chrome/spotify/claude/binance/cursor/tts) registradas en el ToolRegistry",
        {"tools": [t.name for t in TOOLS.list() if t.name not in {f.name for f in FS_TOOLS}]},
    ))
    for _tool in TOOLS.list():
        WORLD.declare_tool(_tool.name, {
            "sandbox": getattr(_tool, "sandbox_profile", None),
            "timeout": getattr(_tool, "timeout", None),
        })

    # --- Self Model ------------------------------------------------------- #
    SELF = SelfModel(
        capabilities=[s.id for s in CAPABILITIES.specs()],
        available=[s.id for s in CAPABILITIES.enabled()],
        resources={"workspace": str(WORKSPACE), "sandbox_no_network": True},
    )

    # --- Estado de ejecución ---------------------------------------------- #
    # PostgreSQL manda. Esto es la vista del proceso, no la fuente de verdad: se
    # rellena al recuperar y se mantiene para responder rápido.
    RUNNING: dict[str, Any] = {}
    STATE: dict[str, Any] = {"mission_id": None, "verification": None}

    EVENTS = EventBus()

    # browser.research se registra DESPUÉS de EVENTS porque su `announce` publica ahí.
    # Registrarlo antes capturaría un EVENTS todavía no definido (UnboundLocalError).
    TOOLS.register_all(build_browser_tools(announce=EVENTS.publish))

    # --- Runtime y cola --------------------------------------------------- #
    RUNTIME = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=SandboxExecutor(
            tools=TOOLS,
            sandbox=SANDBOX,
            desktop_delegate="host" if os.environ.get("ALEXIS_DESKTOP_MODE", "host") == "host" else None,
            voice_mode_provider=voice_mode_provider,
        ),
        verifier=FilesystemVerifier(workspace=WORKSPACE),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EVENTS,
        mission_repo=storage.get("mission"),
        event_repo=storage.get("event"),
        audit_repo=storage.get("audit"),
        world_repo=storage.get("world"),
        verification_repo=storage.get("verification"),
        task_runner=None,
        observation_repo=storage.get("observation"),
        learning_repo=storage.get("learning"),
        gate=AutonomyGate(),
    )

    RUNNER = None
    if storage.get("task") and storage.get("execution"):
        RUNNER = TaskRunner(
            task_repo=storage.get("task"),
            execution_repo=storage.get("execution"),
            checkpoint_repo=storage.get("checkpoint"),
            event_bus=EVENTS,
        )
        RUNTIME.task_runner = RUNNER
    else:
        print("[warn] task runtime offline (sin DB): pasos vía sandbox sin tasks/checkpoints.")
    SCHEDULER = Scheduler(task_repo=storage.get("task"), event_bus=EVENTS) if storage.get("task") else None
    MISSIONS = MissionEngine()

    # --- Modelos ---------------------------------------------------------- #
    # El provider se elige por entorno (P2: el Core no depende de ninguno en
    # concreto). Sin provider real, el router responde DEGRADED y el clasificador cae
    # a reglas; en ambos casos la procedencia se reporta (P1).
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
    if INTENT_CLASSIFIER.model is not None:
        INTENT_CLASSIFIER.model.max_tokens = MODEL_CONFIG.max_tokens
    _real_providers = [p.id for p in MODEL_ROUTER.providers() if not p.degraded and p.available]
    print("[model] provider(s) real(es): "
          f"{_real_providers or 'ninguno (DEGRADED por configuración)'} "
          f"| fallback: {'degraded' if MODEL_CONFIG.allow_degraded() else 'none'}")

    # --- Ejecución de una misión ------------------------------------------ #
    async def _cognitive_execute(mission, step, tool_name=None):
        if RUNNER is not None:
            async def _run(_mission, _step):
                return await RUNTIME.executor.execute(_mission, _step, tool_name=tool_name)

            result, task = await RUNNER.run_step(mission, step, _run)
            if task.status.value == "failed":
                await RUNNER.close_open_tasks(mission.id, status="cancelled")
            return result
        return await RUNTIME.executor.execute(mission, step, tool_name=tool_name)

    def _build_model_planner():
        """Un único sitio donde se decide CÓMO se construye el planner.

        Los tres modos comparten construcción; lo que cambia es si se llega a construirlo.
        """
        return ModelPlanner(
            MODEL_ROUTER,
            catalog=CAPABILITIES,
            max_tokens=MODEL_CONFIG.max_tokens,
            deadline_ms=MODEL_CONFIG.deadline_ms,
        )

    RUNTIME.plan_validator = PlanValidator(
        catalog=CAPABILITIES, policy=RUNTIME.policy, require_catalog=True
    )
    _planner_flag = model_planner_env if model_planner_env is not None else os.environ.get("ALEXIS_MODEL_PLANNER", "auto")
    _planner_reason = ""
    if _planner_flag == "0":
        RUNTIME.plan_planner_mode = "off"
        _planner_reason = "ALEXIS_MODEL_PLANNER=0: ModelPlanner desactivado explícitamente"
    elif _planner_flag == "1":
        RUNTIME.plan_model = _build_model_planner()
        RUNTIME.plan_planner_mode = "on"
        _planner_reason = "ALEXIS_MODEL_PLANNER=1: ModelPlanner activado explícitamente"
    else:
        # Sin `envelope`: en el arranque no hay misión, y el presupuesto de una misión se
        # evalúa en `_plan_with_model`, ya con ella. Aquí sólo es comprobable el global.
        _eligible, _planner_reason = _plan_eligible(MODEL_ROUTER, None)
        if _eligible:
            RUNTIME.plan_model = _build_model_planner()
            RUNTIME.plan_planner_mode = "auto"
        else:
            RUNTIME.plan_planner_mode = "auto-fallback"
    RUNTIME.plan_planner_reason = _planner_reason
    print(f"[cognitive] ModelPlanner={RUNTIME.plan_planner_mode} ({_planner_reason}); "
          f"plan por reglas disponible como suelo")

    RUNTIME.cognitive = CognitiveRuntime(
        policy=RUNTIME.policy,
        gate=RUNTIME.gate,
        executor=RUNTIME.executor,
        verifier=RUNTIME.verifier,
        execute=_cognitive_execute,
        model_router=MODEL_ROUTER,
        memory=(
            PostgresMemoryProvider(storage["db"])
            if storage.get("db") is not None
            else InProcessMemoryProvider(RUNTIME.memory)
        ),
        self_model=SELF,
        world=WORLD,
        catalog=CAPABILITIES,
        goal_verifier=GoalVerifier(world=WORLD),
        plan_validator=RUNTIME.plan_validator,
        # CORE-08B: el MISMO planner que la aplicación ya construyó y activó según
        # `ALEXIS_MODEL_PLANNER`. Sin esto, el replan dinámico construía uno propio con el
        # deadline de DECISIÓN, ignorando el `max_tokens`/`deadline_ms` del planner de
        # producción: dos configuraciones distintas para el mismo trabajo. Compartir la
        # instancia también hace que su provenance sea comparable con la del plan inicial.
        plan_model=RUNTIME.plan_model,
        decision_max_tokens=MODEL_CONFIG.max_tokens,
        decision_deadline_ms=MODEL_CONFIG.deadline_ms,
    )
    print(f"[cognitive] CognitiveRuntime ({_COGNITIVE_MOTIVO}): "
          "decide→policy→execute→observe→evaluate")

    # --- Misión desde una intención --------------------------------------- #
    def _create_mission_from_intent(intent):
        """Mismo envelope que usa POST /missions (permisos intactos); sólo cambia quién
        decide que esto es una tarea: el Intent, no una URL.

        Los `success_criteria` que salen del Intent ya vienen canónicos del contrato
        CORE-02; aquí sólo se copian al Goal.
        """
        envelope = MissionEnvelope(
            objective=intent.objective or intent.utterance,
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=list(ALLOWED_ACTIONS),
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
        # P0 requisito 2: la conversación es una de las diez fuentes del contexto.
        mission.context["conversation"] = [intent.utterance or intent.objective or ""]
        if intent.requested_capabilities:
            mission.context["capability_proposal"] = {
                "capabilities": list(intent.requested_capabilities),
                "source": "model",
                "note": "sugerencia; no es permiso (P3.5). La autoriza catálogo+envelope+policy.",
            }
        return mission

    def _pending_clarification():
        """Misión en WAITING_CLARIFICATION con pregunta pendiente, o `None` (P0 §13)."""
        for mission in list(RUNNING.values()):
            if mission.state is MissionState.WAITING_CLARIFICATION and (
                mission.context.get("clarification") or {}
            ).get("question"):
                return mission
        return None

    def _pending_approval(mission_id):
        mission = RUNNING.get(mission_id)
        if mission is None or mission.state is not MissionState.WAITING_APPROVAL:
            return None
        return mission

    async def _run_mission(mission):
        # Sincronizar YA la vista en memoria con el objeto que el worker está
        # mutando: /state y /self ven el estado real durante la ejecución.
        RUNNING[mission.id] = mission
        await RUNTIME.run_mission(mission)
        RUNNING[mission.id] = mission
        verification = RUNTIME.latest_verification
        STATE["verification"] = (
            verification if verification and verification["mission_id"] == mission.id else None
        )
        if on_mission_activity is not None:
            on_mission_activity()
        if on_mission_end is not None:
            await on_mission_end(mission)

    WORKER = MissionWorker(runner=_run_mission, mission_repo=storage.get("mission")) if storage.get("mission") else None

    def _enqueue_or_run(mission):
        """Con cola: encola (la procesa el worker). Sin cola: corre directo (offline)."""
        if WORKER is not None:
            asyncio.run_coroutine_threadsafe(enqueue(WORKER.mission_repo, mission), loop)
            return
        asyncio.run_coroutine_threadsafe(_run_mission(mission), loop)

    CONVERSATION = ConversationSession(
        classifier=INTENT_CLASSIFIER,
        self_model=SELF,
        bus=EVENTS,
        create_mission=_create_mission_from_intent,
        enqueue=lambda mission: _enqueue_or_run(mission),
        capability_registry=CAPABILITIES,
        # P0 §13: si hay una misión esperando respuesta, este canal la reanuda.
        pending_mission=_pending_clarification,
        resume=lambda mission, answer: RUNTIME.resume_from_clarification(mission, answer),
    )

    return OfficialRuntime(
        loop=loop,
        workspace=WORKSPACE,
        sandbox=SANDBOX,
        capabilities=CAPABILITIES,
        enabled_capabilities=ENABLED_CAPABILITIES,
        tools=TOOLS,
        events=EVENTS,
        storage=storage,
        runtime=RUNTIME,
        cognitive=RUNTIME.cognitive,
        missions=MISSIONS,
        running=RUNNING,
        state=STATE,
        world=WORLD,
        self_model=SELF,
        model_config=MODEL_CONFIG,
        model_router=MODEL_ROUTER,
        intent_classifier=INTENT_CLASSIFIER,
        conversation=CONVERSATION,
        runner=RUNNER,
        scheduler=SCHEDULER,
        worker=WORKER,
        plan_validator=RUNTIME.plan_validator,
        hydrated_entities=hydrated,
        extras={
            "run_mission": _run_mission,
            "enqueue_or_run": _enqueue_or_run,
            "pending_mission": _pending_approval,
            "pending_clarification": _pending_clarification,
            "self_aux": _self_aux_factory(RUNTIME, TOOLS, RUNNING, STATE, SELF, ENABLED_CAPABILITIES),
            "resolve_mission": lambda: RUNNING.get(STATE.get("mission_id")),
        },
    )


def _self_aux_factory(RUNTIME, TOOLS, RUNNING, STATE, SELF, enabled, self_sync=None):
    """Vista auxiliar del Self Model: herramientas, compromisos y estado real.

    CORE-07: las lecciones se leen de `self_sync.lessons`, que es quien las autoriza
    (`can_teach`). Antes se leía `SELF.lessons`, un atributo que `SelfModel` no define
    (el real es `lessons_learned`); como era un `getattr` con default, no lanzaba y las
    lecciones verificadas se perdían en silencio: el Self Model no mostraba nada
    aprendido. `self_sync` es opcional para no romper otras compositions.
    """
    def _self_aux():
        return {
            "tools": TOOLS.list(),
            "commitments": [
                {"id": m.id, "objective": m.goal.objective, "state": m.state.value}
                for m in RUNNING.values()
            ],
            "lessons": list(getattr(self_sync, "lessons", []) or []) if self_sync else [],
            "memory_items": getattr(RUNTIME.memory, "items", []),
            "verification": STATE.get("verification"),
            "available": enabled,
        }

    return _self_aux


# --------------------------------------------------------------------------- #
# La aplicación HTTP oficial
# --------------------------------------------------------------------------- #


def create_handler(runtime: OfficialRuntime, presentation: Any = None):
    """Clase `BaseHTTPRequestHandler` con la superficie oficial.

    Los nombres (`RUNTIME`, `RUNNING`, `EVENTS`…) son variables de cierre de esta
    función, exactamente igual que eran globales del módulo antes: por eso el cuerpo de
    los métodos no cambia y la lógica no se reescribe.

    `presentation` es opcional y lo inyecta el lanzador del demo para servir sus rutas
    (avatar/face/clap/voz). La superficie oficial no depende de que exista.
    """

    RUNTIME = runtime.runtime
    COGNITIVE = runtime.cognitive
    RUNNING = runtime.running
    STATE = runtime.state
    STORAGE = runtime.storage
    TOOLS = runtime.tools
    WORLD = runtime.world
    WORKSPACE = runtime.workspace
    SANDBOX = runtime.sandbox
    EVENTS = runtime.events
    WORKER = runtime.worker
    RUNNER = runtime.runner
    CONVERSATION = runtime.conversation
    CAPABILITIES = runtime.capabilities
    ENABLED_CAPABILITIES = runtime.enabled_capabilities
    SELF = runtime.self_model
    MODEL_CONFIG = runtime.model_config
    MODEL_ROUTER = runtime.model_router
    LOOP = runtime.loop
    MISSIONS = runtime.missions
    _enqueue_or_run = runtime.extras["enqueue_or_run"]
    _pending_mission = runtime.extras["pending_mission"]
    _run_mission = runtime.extras["run_mission"]

    class OfficialHandler(BaseHTTPRequestHandler):
        """Superficie oficial de ALEXIS. Una sola app, un solo runtime."""

        def log_message(self, *args):
            pass

        # -- utilidades ---------------------------------------------------- #
        def _send_json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str, code=200):
            body = html.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _get_mission(self, mission_id: str):
            """`GET /missions/{id}`. Lectura pura: no ejecuta, no encola, no muta."""
            if not mission_id:
                self._send_json({"error": "not found"}, 404)
                return
            mission = runtime.find_mission(mission_id)
            if mission is None:
                self._send_json({"error": "mission not found"}, 404)
                return
            self._send_json(runtime.mission_detail(mission))

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
                    "success_criteria": list(mission.goal.success_criteria),
                    "goal_verification": mission.context.get("goal_verification"),
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
                "present": present({
                    "mission": mission_payload,
                    "verification": STATE.get("verification"),
                    "memory": memory,
                }),
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
                    {"id": m.id, "state": m.state.value, "objective": m.goal.objective,
                     "autonomy": m.envelope.autonomy.value}
                    for m in RUNNING.values()
                ],
                "queue": {
                    "enabled": WORKER is not None,
                    "active": (WORKER.active.id if WORKER is not None and WORKER.active is not None else None),
                },
                "runtime": {
                    "official": True,
                    "cognitive": COGNITIVE is not None,
                    "goal_verifier": COGNITIVE is not None and COGNITIVE.goal_verifier is not None,
                    "persistence": bool(STORAGE.get("mission")),
                },
            }

        def _stream(self):
            """SSE sobre el EventBus OFICIAL: la misma instancia que usan el resto de
            rutas del proceso. Nunca un bus nuevo por petición.

            La suscripción se crea ANTES de anunciar la respuesta. El `EventBus` no tiene
            replay, así que el orden importa: si primero se mandan las cabeceras y después
            se suscribe, todo evento publicado en esa ventana se pierde para siempre, y
            el cliente ya creía que estaba escuchando. Suscribiendo primero, el 200
            significa lo que dice: "ya puedes recibir"."""
            sub = EVENTS.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
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
            finally:
                EVENTS.unsubscribe(sub)

        # -- GET ------------------------------------------------------------ #
        def do_GET(self):
            path = urlparse(self.path).path
            if presentation is not None and presentation.try_get(self, path):
                return
            if path == "/health":
                self._send_json({
                    "name": "ALEXIS",
                    "status": "online",
                    "entrypoint": "official",
                    "runtime": {
                        "cognitive": COGNITIVE is not None,
                        "goal_verifier": COGNITIVE is not None and COGNITIVE.goal_verifier is not None,
                        "persistence": bool(STORAGE.get("mission")),
                    },
                })
            elif path == "/ui":
                self._send_html(PAGE)
            elif path == "/stream":
                self._stream()
            elif path == "/missions":
                self._send_json([runtime.payload(m) for m in RUNNING.values()])
            elif path.startswith("/missions/"):
                # Lectura por recurso. `/state` NO sirve para esto: representa la última
                # misión del proceso, no la que se pide, y se sobrescribe con cada
                # misión nueva. Aquí se pregunta por un identificador concreto.
                self._get_mission(path.split("/")[2])
            elif path == "/self":
                self._send_json({
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
                })
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
            else:
                self._send_json({"error": "not found"}, 404)

        # -- POST ----------------------------------------------------------- #
        def do_POST(self):
            path = urlparse(self.path).path
            if presentation is not None and presentation.try_post(self, path):
                return
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
                # Los criterios de éxito pueden llegar por la URL, y se canónicos aquí
                # con el MISMO contrato que usa el IntentClassifier (CORE-02): el
                # endpoint no puede crear una misión con criterios que el GoalVerifier no
                # sepa leer.
                criteria, status = normalize_criteria(objective, objective, data.get("success_criteria"))
                envelope = MissionEnvelope(
                    objective=objective,
                    autonomy=autonomy,
                    allowed_actions=list(ALLOWED_ACTIONS),
                    capabilities=envelope_capabilities,
                )
                mission = MISSIONS.create(objective, envelope, success_criteria=criteria)
                RUNNING[mission.id] = mission
                STATE["mission_id"] = mission.id
                if status["verifiable"] == 0 and not criteria:
                    mission.context["criteria_status"] = status
                _enqueue_or_run(mission)
                self._send_json({
                    **runtime.payload(mission),
                    "criteria_status": status,
                })
            elif path.startswith("/missions/") and path.endswith("/approve"):
                mission_id = path.split("/")[2]
                mission = _pending_mission(mission_id)
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
                self._send_json(runtime.payload(mission))
            elif path.startswith("/missions/") and path.endswith("/clarify"):
                # P0 §13: respuesta a una pregunta pendiente. Delega en la MISMA
                # operación de reanudación que usa /chat; no hay lógica duplicada.
                mission_id = path.split("/")[2]
                mission = runtime.extras["pending_clarification"]()
                if mission is None or mission.id != mission_id:
                    mission = RUNNING.get(mission_id)
                    if mission is None:
                        self._send_json({"error": "mission not found"}, 404)
                        return
                # Este handler sólo leía el body en /chat; aquí hay que leerlo.
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                answer = payload.get("response") or payload.get("text") or ""
                if COGNITIVE is None:
                    self._send_json({"error": "cognitive runtime not configured"}, 503)
                    return
                allowed, why = COGNITIVE.can_clarify(mission)
                if not allowed:
                    self._send_json({"error": why, "state": mission.state.value}, 409)
                    return
                if not str(answer).strip():
                    self._send_json({"error": "empty response"}, 400)
                    return
                try:
                    asyncio.run_coroutine_threadsafe(
                        RUNTIME.resume_from_clarification(mission, str(answer)), LOOP
                    ).result(timeout=180)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, 409)
                    return
                except Exception as exc:  # noqa: BLE001
                    self._send_json({"error": f"resume failed: {exc}"}, 500)
                    return
                if STORAGE.get("mission"):
                    asyncio.run_coroutine_threadsafe(STORAGE["mission"].upsert(mission), LOOP)
                self._send_json({
                    "type": "clarification_received",
                    "mission_id": mission.id,
                    "state": mission.state.value,
                })
            elif path.startswith("/missions/") and path.endswith("/deny"):
                mission_id = path.split("/")[2]
                mission = _pending_mission(mission_id)
                if mission is None:
                    self._send_json({"error": "mission not found or not waiting approval"}, 409)
                    return
                MISSIONS.stop(mission, "Denied by human")
                mission.context.pop("pending_approval", None)
                if STORAGE.get("mission"):
                    asyncio.run_coroutine_threadsafe(STORAGE["mission"].upsert(mission), LOOP)
                asyncio.run_coroutine_threadsafe(EVENTS.publish("mission.cancelled", mission.id), LOOP)
                self._send_json(runtime.payload(mission))
            else:
                self._send_json({"error": "not found"}, 404)

    return OfficialHandler


def _recover_missions(rt: OfficialRuntime) -> bool:
    """Recupera de PostgreSQL las misiones abiertas y reanuda las que se estaban
    ejecutando de verdad. Lógica movida tal cual desde el lanzador: no se cambia qué se
    recupera ni qué se reanuda, sólo dónde vive.
    """
    repo = rt.mission_repo()
    if not repo:
        return False

    async def _recover():
        open_states = ("pending", "planning", "running", "verifying", "waiting_approval")
        missions = await repo.list(limit=50)
        open_missions = [m for m in missions if m.state.value in open_states]
        if rt.scheduler is not None and rt.runner is not None:
            reclaimed = await rt.scheduler.recover_stale(lease_seconds=rt.runner.lease_seconds)
            if reclaimed:
                print(f"Scheduler: {reclaimed} tareas con lease expirada recuperadas.")
        active = [m for m in open_missions if m.state.value in ("planning", "running", "verifying")]
        for m in open_missions:
            rt.running[m.id] = m
            if m.state.value != "waiting_approval":
                if rt.worker is not None:
                    await enqueue(rt.worker.mission_repo, m)
                else:
                    asyncio.ensure_future(rt.run_mission(m))
        if active:
            rt.state["mission_id"] = active[-1].id

    try:
        asyncio.run_coroutine_threadsafe(_recover(), rt.loop).result(timeout=10)
        if rt.state["mission_id"] is not None:
            print("Misiones abiertas recuperadas de PostgreSQL (reanudadas desde checkpoint).")
        return True
    except Exception as exc:  # noqa: BLE001 — recuperar es mejor que no arrancar
        print(f"[warn] recuperación de misiones: {exc}")
        return False


def start_services(rt: OfficialRuntime) -> dict:
    """Arranca los servicios del runtime oficial. Idéntico para producción y tests.

    `build_runtime()` CONSTRUYE; esto ENCIENDE. Estaba partido: el arranque vivía
    dentro de `main()`, que además abre el puerto y bloquea, así que no se podía
    reutilizar. Un E2E tenía que replicar esas líneas a mano, con lo que el arranque
    bajo prueba no era el de producción. Ahora hay un solo camino.

    Qué arranca, y por qué aquí:

    - **SelfModelSync**: el Self Model se actualiza consumiendo los eventos del bus
      oficial.
    - **Worker de la cola**: sin él, lo encolado se queda en `pending` —nadie lo
      consume— y ninguna misión llega a ejecutarse.
    - **Recuperación**: misiones abiertas de una ejecución anterior.

    NO incluye la escucha de palmadas: eso es presentación del demo (voz y activación) y
    depende de cosas que viven en el lanzador. Moverlo aquí arrastraría la demo dentro de
    la aplicación oficial, que es justo lo que CORE-03 separó.

    La lógica de cada servicio NO se toca: mismo orden (self-sync → worker →
    recuperación), mismos mensajes, mismos timeouts. Devuelve lo que arrancó, para que
    quien lo llame pueda comprobarlo y para que un test pueda afirmar sobre ello.
    """
    from alexis.self.sync import SelfModelSync

    # Idempotencia. Arrancar dos veces crearía un segundo worker, un segundo SelfModelSync
    # y un segundo AuditSink: tres consumidores más del mismo bus escribiendo en el mismo
    # PostgreSQL. `stop_services()` ya era idempotente; aquí faltaba la garantía simétrica,
    # y `test_core04_http_e2e.py` afirmaba en un comentario que repetirla no rompía nada
    # mientras ninguna prueba lo comprobara.
    #
    # Se devuelve el estado YA en marcha en lugar de relanzar: quien llama (test o
    # producción) recibe la misma forma de retorno y puede afirmar sobre una sola
    # instancia, en vez de tener que distinguir dos caminos.
    #
    # OJO con la simetría: `stop_services()` CIERRA la base de datos, así que es
    # terminal para ese runtime, no una pausa. "Parar y volver a arrancar" no es una
    # operación soportada sobre el mismo objeto: tras un stop hay que construir un runtime
    # nuevo, que es lo que hacen la fixture y `main()`. La guarda no cambia ese contrato.
    if rt.extras.get("services_started"):
        return {
            "self_sync": rt.extras.get("self_sync"),
            "worker_started": rt.extras.get("worker_task") is not None,
            "recovered": [],
            "audit_sink": rt.extras.get("audit_sink"),
            "worker_task": rt.extras.get("worker_task"),
            "already_started": True,
        }

    # CORE-07: antes de encender nada, el Self Model recupera lo que aprendió en
    # ejecuciones anteriores. Sin esto, `SelfModel(...)` nacía vacío y toda lección,
    # auto-observación y reflexión verificadas se perdían al reiniciar.
    if rt.storage.get("db") is not None:
        from alexis.self.persistence import SelfModelPersistence

        try:
            aprendido = asyncio.run_coroutine_threadsafe(
                SelfModelPersistence(rt.storage["db"]).load(), rt.loop
            ).result(timeout=10)
            rt.self_model.restore(aprendido)
            if aprendido.get("lessons") or aprendido.get("observations") or aprendido.get("reflections"):
                print(
                    "[self] estado aprendido recuperado: "
                    f"{len(aprendido.get('lessons') or [])} lección/es, "
                    f"{len(aprendido.get('observations') or [])} observación/es, "
                    f"{len(aprendido.get('reflections') or [])} reflexión/es."
                )
        except Exception as exc:  # noqa: BLE001 — no poder recordar no puede tumbar el arranque
            print(f"[warn] no se pudo recuperar el estado aprendido del Self Model: {exc}")

    # Cierre CORE-11/12: las skills y su rendimiento también vuelven al arranque. El Self
    # Model se recuperó arriba; esto recupera el REGISTRO que permite reutilizarlas, porque
    # una skill que sólo vive en RAM no es una skill: es un texto que se pierde al reiniciar.
    if rt.storage.get("learning") is not None:
        try:
            asyncio.run_coroutine_threadsafe(
                rt.runtime.hydrate_learning(), rt.loop
            ).result(timeout=10)
        except Exception as exc:  # noqa: BLE001 — recordar mal no puede tumbar el arranque
            print(f"[warn] no se pudo recuperar las skills del registro: {exc}")

    # Self Model: se actualiza consumiendo los eventos reales del bus oficial.
    self_sync = SelfModelSync(
        rt.self_model, rt.extras["resolve_mission"], aux=rt.extras["self_aux"],
        persistence=(
            SelfModelPersistence(rt.storage["db"]) if rt.storage.get("db") is not None else None
        ),
    )
    # CORE-07: el `aux` se reconstruye apuntando al sync recién creado, para que las
    # lecciones que éste autoriza lleguen al modelo (antes se leía un atributo
    # inexistente y se perdían en silencio).
    rt.extras["self_aux"] = _self_aux_factory(
        rt.runtime, rt.tools, rt.running, rt.state, rt.self_model,
        rt.enabled_capabilities, self_sync,
    )
    self_sync.attach(rt.events, rt.loop)

    # Cola de misiones: una a la vez en orden, con checkpoint resumible.
    worker_started = False
    if rt.worker is not None:
        async def _worker_loop():
            await rt.worker.loop()

        worker_task = asyncio.run_coroutine_threadsafe(_worker_loop(), rt.loop)
        rt.extras["worker_task"] = worker_task
        worker_started = True
        print("[cola] worker de misiones activo (FIFO persistente en PostgreSQL)")

    recovered = _recover_missions(rt)

    # CORE-05: el sink que persiste `model.routed` en `audit_log`. Se adjunta al bus
    # OFICIAL (no crea uno propio), y va después del resto para no alterar el orden de
    # arranque ya probado por CORE-04.
    audit_sink = None
    audit_repo = rt.storage.get("audit")
    if audit_repo is not None:
        from alexis.models.audit_sink import AuditSink

        audit_sink = AuditSink(audit_repo, event_repo=rt.storage.get("event")).attach(
            rt.events, rt.loop
        )
        print("[audit] sink de model.routed activo (audit_log)")

    rt.extras["self_sync"] = self_sync
    rt.extras["audit_sink"] = audit_sink
    rt.extras["services_started"] = True

    return {
        "self_sync": self_sync,
        "worker_started": worker_started,
        "recovered": recovered,
        "audit_sink": audit_sink,
        "worker_task": rt.extras.get("worker_task"),
        "already_started": False,
    }


async def _cancel_concurrent_future(future) -> None:
    if future is None or future.done():
        return
    future.cancel()
    await asyncio.gather(asyncio.wrap_future(future), return_exceptions=True)


async def stop_services(rt: OfficialRuntime, *, worker_timeout: float = 5.0) -> dict:
    """Apaga los servicios iniciados por start_services.

    El orden es inverso al arranque: primero deja de producir trabajo, luego detiene
    observadores del bus y por último cierra la persistencia. Es idempotente.
    """
    if not rt.extras.get("services_started"):
        return {"stopped": False, "reason": "services_not_started"}

    worker = rt.worker
    worker_task = rt.extras.get("worker_task")
    if worker is not None:
        worker.stop()

    if worker_task is not None and not worker_task.done():
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(worker_task)),
                timeout=worker_timeout,
            )
        except asyncio.TimeoutError:
            await _cancel_concurrent_future(worker_task)

    self_sync = rt.extras.get("self_sync")
    if self_sync is not None:
        await self_sync.stop()

    audit_sink = rt.extras.get("audit_sink")
    if audit_sink is not None:
        await audit_sink.stop()

    db = rt.storage.get("db")
    if db is not None:
        await db.close()

    rt.extras["worker_task"] = None
    rt.extras["self_sync"] = None
    rt.extras["audit_sink"] = None
    rt.extras["services_started"] = False
    return {"stopped": True}



def create_app(runtime: OfficialRuntime, presentation: Any = None) -> ThreadingHTTPServer:
    """Servidor HTTP listo para `serve_forever()` sobre la superficie oficial."""
    return ThreadingHTTPServer(("127.0.0.1", 0), create_handler(runtime, presentation))


# --------------------------------------------------------------------------- #
# Instancia única por proceso
# --------------------------------------------------------------------------- #

_INSTANCE: OfficialRuntime | None = None
_LOCK = threading.Lock()


class OfficialRuntimeNotRunning(RuntimeError):
    """Se pidió el runtime oficial en un proceso que no lo tiene.

    Es la invariante que hace único a ALEXIS (CORE-03). Antes, este módulo construía el
    runtime bajo demanda, así que `uvicorn apps.api.main:app` levantaba un ALEXIS
    entero y independiente: otro `EventBus`, otro `CognitiveRuntime`, otro estado, y
    ninguno el que la documentación señalaba.

    Ahora la construcción es exclusiva del proceso oficial (el que arranca
    `apps.demo.server` o el que llama a `install_official_runtime()`). Cualquier otro
    proceso que intente obtenerlo se encuentra con esta excepción en vez de con un
    segundo ALEXIS. No hace falta IPC para evitarlo: basta con que la construcción no
    sea un efecto secundario de importar o de recibir una petición.
    """


def install_official_runtime(runtime: OfficialRuntime) -> OfficialRuntime:
    """Instala LA instancia oficial de este proceso. La reserva es del lanzador.

    `build_runtime()` de `apps/demo/server.py` es quien la crea. A partir de aquí,
    `get_official_runtime()` la devuelve siempre y no puede haber una segunda.
    """
    global _INSTANCE
    with _LOCK:
        if _INSTANCE is not None and _INSTANCE is not runtime:
            raise OfficialRuntimeNotRunning(
                "este proceso ya tiene una instancia oficial distinta; no puede haber dos"
            )
        _INSTANCE = runtime
        return runtime


def get_official_runtime() -> OfficialRuntime:
    """La instancia oficial del proceso. Siempre la misma; nunca se construye aquí.

    Se usa bajo demanda por rutas de la fachada y por los tests. Si el proceso no es el
    oficial, lanza `OfficialRuntimeNotRunning`: preferimos un error honesto a un segundo
    ALEXIS.
    """
    with _LOCK:
        if _INSTANCE is None:
            raise OfficialRuntimeNotRunning(
                "este proceso no es el proceso oficial de ALEXIS. El runtime se construye "
                "una vez, en el proceso oficial (python3 -m apps.demo.server). Esta fachada "
                "no es un entrypoint: para usar ALEXIS, arranca el runtime oficial."
            )
        return _INSTANCE


def peek_official_runtime() -> OfficialRuntime | None:
    """La instancia oficial si este proceso la tiene; `None` si no.

    Para lo que sólo necesita *describirse* (un `/health`, un diagnóstico) y no debe
    provocar la construcción.
    """
    with _LOCK:
        return _INSTANCE


def set_official_runtime(runtime: OfficialRuntime | None) -> None:
    """Fija o limpia la instancia del proceso. Sólo lanzador y tests."""
    global _INSTANCE
    with _LOCK:
        _INSTANCE = runtime


def build_runtime(loop: asyncio.AbstractEventLoop, storage: dict | None = None, **kwargs) -> OfficialRuntime:
    """Construye E INSTALA el runtime oficial de este proceso.

    El único camino para tener un ALEXIS en marcha. `apps/api` no lo llama, y por eso no
    puede convertirse en un segundo ALEXIS.
    """
    return install_official_runtime(
        build_official_runtime(loop=loop, storage=storage, **kwargs)
    )


__all__ = [
    "ALLOWED_ACTIONS",
    "OfficialRuntime",
    "OfficialRuntimeNotRunning",
    "build_official_runtime",
    "build_runtime",
    "create_app",
    "create_handler",
    "get_official_runtime",
    "init_storage",
    "install_official_runtime",
    "peek_official_runtime",
    "set_official_runtime",
    "start_services",
]
