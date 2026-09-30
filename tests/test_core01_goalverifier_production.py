"""FASE CORE-01 — GoalVerifier en producción (Blocker #1 del MASTER AUDIT).

`CognitiveRuntime` ya sabía verificar el objetivo; lo que no existía era el wiring:
`apps/demo/server.py` construía el runtime productivo sin `goal_verifier` ni `catalog`,
así que ninguna misión podía terminar en `COMPLETED` y el E2E B5 real quedaba en
`needs_verification` ("no hay GoalVerifier en esta ejecución").

Esta fase inyecta ambas piezas en la instancia productiva y deja dos garantías:

1. Vertical completa real (a nivel runtime, porque `apps/demo/server.py` no es
   importable al componer la app entera):

       misión TASK → CognitiveRuntime real → fs.read real → observación real
       → GoalVerifier real → verified=True → settle → COMPLETED

2. B5 honesto sin criterios: con el verificador vivo y sin criterios verificables
   (el clasificador determinista no genera predicados), la misión NO se forja como
   cumplida: queda en `needs_verification` con el motivo real del verifier.

`run_mission` es EL mismo punto de entrada que usa el worker de cola del demo
(`_enqueue_or_run` → `run_mission`), así que el test ejercita la ruta productiva.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from alexis.autonomy.gates import AutonomyGate  # noqa: E402
from alexis.autonomy.goal_state import goal_is_confirmed  # noqa: E402
from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.capabilities import build_catalog  # noqa: E402
from alexis.cognition.contracts import IntentKind  # noqa: E402
from alexis.cognition.goal_verification import (  # noqa: E402
    CriterionStatus,
    GoalVerifier,
    parse_predicate,
)
from alexis.cognition.intent_classifier import IntentClassifier  # noqa: E402
from alexis.cognition.loop import CognitiveRuntime  # noqa: E402
from alexis.cognition.planner import Planner  # noqa: E402
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    MissionEnvelope,
    MissionState,
)
from alexis.core.runtime import AlexisRuntime  # noqa: E402
from alexis.events.bus import EventBus  # noqa: E402
from alexis.execution import SandboxExecutor  # noqa: E402
from alexis.learning.system import ExperienceLearner  # noqa: E402
from alexis.memory.store import InMemoryMemory  # noqa: E402
from alexis.security.policy import PolicyEngine  # noqa: E402
from alexis.security.sandbox import SandboxRunner  # noqa: E402
from alexis.tools.filesystem import build_filesystem_tools  # noqa: E402
from alexis.tools.registry import ToolRegistry  # noqa: E402
from alexis.verification import FilesystemVerifier  # noqa: E402
from alexis.world.model import WorldModel  # noqa: E402

ALLOWED_ACTIONS = [
    "understand", "analyze", "research", "execute", "verify",
    "modify", "test", "commit", "write", "respond",
]


def _cognitivo(tmp_path, catalog, world):
    registry = ToolRegistry()
    registry.register_all(build_filesystem_tools(tmp_path))
    executor = SandboxExecutor(tools=registry, sandbox=SandboxRunner(tmp_path))
    return CognitiveRuntime(
        policy=PolicyEngine(),
        gate=AutonomyGate(),
        executor=executor,
        verifier=FilesystemVerifier(workspace=tmp_path),
        world=world,
        # CORE-01: el catálogo real viaja al contexto de decisión (P0 requisito 2)…
        catalog=catalog,
        # CORE-01: … y el GoalVerifier real cierra `COMPLETED` (P0 §5.5). Mismo wiring
        # que la instancia productiva de `apps/demo/server.py`.
        goal_verifier=GoalVerifier(world=world),
    ), executor


def _runtime(tmp_path, executor, cognitive):
    return AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=executor,
        verifier=FilesystemVerifier(workspace=tmp_path),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        cognitive=cognitive,
    )


def _mision(utterance, criteria):
    catalog = build_catalog()
    return MissionEngine().create(
        utterance,
        MissionEnvelope(
            objective=utterance,
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=list(ALLOWED_ACTIONS),
            capabilities=[s.id for s in catalog.enabled()],
        ),
        success_criteria=criteria,
    )


def test_la_instancia_productiva_inyecta_goalverifier_y_catalog():
    """Guarda estática del wiring de producción.

    CORE-03 movió la construcción del runtime a `apps/demo/app.py`, que es el módulo
    oficial e importable; `server.py` quedó como lanzador. La guarda se mueve con ella:
    lo que se comprueba es el MISMO invariante —el runtime oficial inyecta
    `GoalVerifier` y el catálogo—, sólo que leído donde ahora vive el cableado.

    Si alguien revierte el cableado de CORE-01, este test lo detecta aunque la suite
    ya no pueda importar el módulo que compone la app entera.
    """
    fuente = (PROJECT_ROOT / "apps" / "demo" / "app.py").read_text(encoding="utf-8")

    assert "from alexis.cognition.goal_verification import GoalVerifier" in fuente

    bloque = _bloque_de_llamada(fuente, "RUNTIME.cognitive = CognitiveRuntime(")
    assert "goal_verifier=GoalVerifier(world=WORLD)" in bloque
    assert "catalog=CAPABILITIES" in bloque


def _bloque_de_llamada(fuente: str, cabecera: str) -> str:
    """El texto de una llamada, hasta su paréntesis de cierre REAL.

    Cortar por el primer `)` no vale: `memory=(PostgresMemoryProvider(...) if ...)`
    tiene paréntesis anidados y truncaría el bloque antes de tiempo, dejando pasar
    un cableado que ya no inyecta el `GoalVerifier`.
    """
    inicio = fuente.index(cabecera)
    i, depth = inicio, 0
    while i < len(fuente):
        if fuente[i] == "(":
            depth += 1
        elif fuente[i] == ")":
            depth -= 1
            if depth == 0:
                return fuente[inicio : i + 1]
        i += 1
    raise AssertionError(f"llamada sin cerrar: {cabecera}")


@pytest.mark.asyncio
async def test_vertical_completa_completed_con_todo_real(tmp_path):
    (tmp_path / "notas.txt").write_text("contenido real de notas\n", encoding="utf-8")
    catalog = build_catalog()
    world = WorldModel()
    cognitive, executor = _cognitivo(tmp_path, catalog, world)
    runtime = _runtime(tmp_path, executor, cognitive)

    mission = _mision(
        "lee el archivo notas.txt",
        ["El archivo file_exists:notas.txt está escrito"],
    )
    assert mission.state is MissionState.PENDING

    result = await runtime.run_mission(mission)

    # 1. Estado final COMPLETED, porque el GoalVerifier real lo autorizó.
    assert result.state is MissionState.COMPLETED
    assert result.goal_verification is not None
    assert result.goal_verification.verified is True
    assert goal_is_confirmed(result.goal_verification) is True
    assert (result.context.get("goal_verification_reason") or "").startswith(
        "objetivo verificado"
    )

    # 2. GoalVerification presente con evidencia fiable en cada criterio.
    assert result.goal_verification.evaluations
    for evaluation in result.goal_verification.evaluations:
        assert evaluation.status is CriterionStatus.SATISFIED
        assert any(e.trusted for e in evaluation.evidence), (
            "un criterio SATISFIED sin evidencia fiable no puede abrir COMPLETED"
        )

    # 3. fs.read ejecutado REALMENTE: el mundo guarda la observación de la tool.
    entity = world.known_path("notas.txt")
    assert entity is not None, "el World Model debe haber observado notas.txt"
    assert str(entity.source).startswith("tool:"), f"fuente no observada por tool: {entity.source}"
    assert entity.attributes.get("exists") is True
    assert "size" in entity.attributes
    ejecutadas = [r for r in (result.results or []) if r.get("step") == "execute"]
    assert ejecutadas and ejecutadas[0].get("success") is True, "fs.read real debe ejecutarse"

    # 4. No simulated success: toda evidencia acreditada viene de una tool, nunca
    #    de un claim del modelo.
    acreditada = {
        str(e.source)
        for evaluation in result.goal_verification.evaluations
        for e in evaluation.evidence
        if e.trusted
    }
    assert acreditada, "debe haber al menos una evidencia acreditada"
    assert all(s.startswith("tool:") for s in acreditada)

    # 5. No bypass del Gate: la ejecución pasó por el ciclo real (política y gate reales),
    #    no se marcó COMPLETED a mano y las decisiones quedaron auditadas con allow.
    decisions = result.context.get("decisions") or {}
    assert decisions, "el camino cognitivo real debe registrar decisiones"
    assert any(d.get("policy_verdict") == "allow" for d in decisions.values())


@pytest.mark.asyncio
async def test_una_task_sin_criterio_verificable_no_forja_completed(tmp_path):
    """Regresión CORE-01, adaptada al contrato CORE-02.

    La premisa de CORE-01 sigue intacta: con el GoalVerifier inyectado, una misión cuyos
    criterios no se pueden comprobar NO se forja como COMPLETED. Lo que cambió en
    CORE-02 es que una TASK como "analiza notas.txt" ya no llega sin criterios (el
    clasificador deriva ``file_exists:notas.txt``), así que aquí se usa un objetivo sin
    ruta ni suite derivable: sus criterios quedan sin checker y la misión termina en
    NEEDS_VERIFICATION con el motivo real del verifier, NUNCA en COMPLETED.
    """
    catalog = build_catalog()
    world = WorldModel()
    cognitive, executor = _cognitivo(tmp_path, catalog, world)
    runtime = _runtime(tmp_path, executor, cognitive)

    intent = await IntentClassifier().classify("Revisa el proyecto entero y dime si está bien")
    assert intent.kind is IntentKind.TASK
    # Sin ruta ni suite no hay predicado derivable: los criterios no son comprobables.
    assert not any(parse_predicate(c) for c in intent.success_criteria)

    mission = _mision(intent.objective or intent.utterance, intent.success_criteria)
    result = await runtime.run_mission(mission)

    assert result.state is MissionState.NEEDS_VERIFICATION
    assert result.goal_verification is not None
    assert result.goal_verification.verified is False
    reason = result.context.get("goal_verification_reason") or ""
    # El motivo ya no es "no hay GoalVerifier": el verificador está vivo y dijo por qué.
    assert "no hay GoalVerifier" not in reason
    assert "criterios" in reason or "verificable" in reason or "evidencia" in reason