"""P0 §11 — Verificación SEMÁNTICA real: el objetivo que se verifica es el que se pidió.

Este módulo existe por el hallazgo del hostile re-audit: el sistema verificaba objectives
reducidos a un predicado trivial. "Analiza el archivo notas.txt y dime qué contiene" se
canonizaba a `file_exists:notas.txt`, un hecho que ya era cierto antes de que ALEXIS
hiciera nada, de modo que la misión se cerraba sin que nadie hubiera leído el fichero.

Aquí se cierra con E2E reales y con ATAQUES. Nada se pre-siembra del resultado que el
objetivo pretende producir:

- caso A  el objetivo pide CONTENIDO: se cumple porque ALEXIS leyó, no porque el fichero
          estuviera;
- caso B  el objetivo pide una TRANSFORMACIÓN: se cumple porque se leyó la entrada Y se
          produjo una salida con contenido, y existe relación entre ambas;
- caso C  el objetivo NO es comprobable: no se convierte en `file_exists:*` y la misión
          no se cierra.

Las tools son reales (`fs.read`/`fs.write` en sandbox), el WorldModel es real y el
`GoalVerifier` es real. No hay dobles en el camino de ejecución ni de verificación.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.gates import AutonomyGate  # noqa: E402
from alexis.autonomy.goal_state import goal_is_confirmed, settle  # noqa: E402
from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.capabilities import build_catalog  # noqa: E402
from alexis.cognition.intent_classifier import IntentClassifier  # noqa: E402
from alexis.cognition.criteria import (  # noqa: E402
    classify_objective_semantics,
    criteria_for_objective,
    normalize_criteria,
)
from alexis.cognition.goal_verification import (  # noqa: E402
    CriterionStatus,
    GoalVerification,
    GoalVerifier,
)
from alexis.cognition.loop import CognitiveRuntime  # noqa: E402
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    MissionEnvelope,
    MissionState,
    PlanStep,
    UnverifiedGoalError,
)
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

ALLOWED_ACTIONS = ["understand", "analyze", "research", "execute", "verify", "respond"]

#: Contenido de la entrada, distinto de cualquier salida esperada: si un test passa sin
#: que la entrada se lea de verdad, el resultado sigue siendo imposible de explicar.
ENTRADA = "primera linea\nsegunda linea\ntercera linea\n"


def _envelope(objective: str) -> MissionEnvelope:
    return MissionEnvelope(
        objective=objective,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=list(ALLOWED_ACTIONS),
        capabilities=[s.id for s in build_catalog().enabled()],
    )


def _real_runtime(workspace: pathlib.Path):
    """Runtime con herramientas, sandbox, verificador y mundo REALES.

    Lo único que no se ejercita es el modelo de lenguaje: es una dependencia externa
    ausente, y se documenta como tal en vez de simularlo en silencio.
    """
    tools = ToolRegistry()
    tools.register_all(build_filesystem_tools(workspace))
    world = WorldModel()
    policy = PolicyEngine()
    executor = SandboxExecutor(tools=tools, sandbox=SandboxRunner(workspace=workspace))

    cognitive = CognitiveRuntime(
        policy=policy,
        gate=AutonomyGate(),
        executor=executor,
        verifier=FilesystemVerifier(workspace=workspace),
        goal_verifier=GoalVerifier(world=world),
        world=world,
    )

    from alexis.core.runtime import AlexisRuntime
    from alexis.cognition.planner import Planner

    runtime = AlexisRuntime(
        planner=Planner(),
        policy=policy,
        executor=executor,
        verifier=FilesystemVerifier(workspace=workspace),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        cognitive=cognitive,
        goal_verifier=GoalVerifier(world=world),
    )
    cognitive.goal_verifier = GoalVerifier(world=world)
    return runtime, cognitive, world


async def _mission_from_turn(turn: str, workspace: pathlib.Path):
    """Misión tal y como la abriría `/chat`: los criterios los produce el clasificador."""
    intent = await IntentClassifier().classify(turn)
    assert intent.is_task, f"«{turn}» debería abrir misión; no lo hizo ({intent.kind})"
    return MissionEngine().create(
        intent.objective or intent.utterance,
        _envelope(intent.objective or intent.utterance),
        success_criteria=intent.success_criteria,
    ), intent


# =========================================================================== #
# A. Objetivo de contenido: se cumple por LEER, no por existir
# =========================================================================== #


@pytest.mark.asyncio
async def test_a1_analizar_un_archivo_existente_exige_que_se_leyera(tmp_path):
    """Caso A — el objetivo pide el contenido; el fichero YA existe y aun así hay que leer.

    Este es el ataque original: pre-existe `notas.txt`, así que `file_exists` estaba
    satisfecho antes de que ALEXIS hiciera nada. Con el contrato vigente la misión no
    puede cerrarse sin una observación que traiga el CONTENIDO.
    """
    (tmp_path / "notas.txt").write_text(ENTRADA, encoding="utf-8")
    objetivo = "Analiza el archivo notas.txt y dime qué contiene"

    criteria, _ = normalize_criteria(objetivo, objetivo, [])
    assert criteria == ["content_observed:notas.txt"], (
        "un objetivo de análisis no puede reducirse a file_exists: eso ya era cierto "
        "antes de que ALEXIS hiciera nada"
    )

    # Y el verificador lo confirma: sin lectura real, no se cumple aunque exista.
    world_solo_stat = WorldModel()
    ver_stat = GoalVerifier(world=world_solo_stat).verify(
        MissionEngine().create(
            objetivo, _envelope(objetivo), success_criteria=criteria
        )
    )
    assert ver_stat.verified is False

    # Con lectura real, se cumple — y la evidencia lo dice.
    runtime, cognitive, world = _real_runtime(tmp_path)
    plan = await _plan_de_lectura(cognitive, objetivo)
    knowledge = cognitive.knowledge_for(_dummy_mission())
    for _ in range(8):
        pending = cognitive.pending_steps(_dummy_mission(), plan, knowledge)
        if not pending:
            break
        outcome = await cognitive.step(_dummy_mission(), knowledge, pending_steps=pending, plan=plan)
        knowledge = outcome.knowledge
        if outcome.done:
            break

    entidad = world.known_path("notas.txt")
    assert entidad is not None, "el WorldModel real debe haber observado el fichero"
    assert "fs.read" in str(entidad.source), (
        f"la observación debe venir de una lectura real, no de {entidad.source}"
    )
    assert entidad.attributes.get("content_observed") is True

    ver = GoalVerifier(world=world).verify(
        MissionEngine().create(objetivo, _envelope(objetivo), success_criteria=criteria)
    )
    assert ver.verified is True
    assert ver.evaluations[0].status is CriterionStatus.SATISFIED


@pytest.mark.asyncio
async def test_a2_sin_lectura_la_mision_no_cierra_aunque_el_archivo_exista(tmp_path):
    """El caso honesto: si nadie leyó, el objetivo de análisis NO está cumplido.

    Es el reverso del anterior y evita que la cadena se cierre por un atajo: existe el
    fichero, no hay lectura, luego `NEEDS_VERIFICATION` y nunca `COMPLETED`.
    """
    (tmp_path / "notas.txt").write_text(ENTRADA, encoding="utf-8")
    objetivo = "Analiza el archivo notas.txt y dime qué contiene"
    criteria, _ = normalize_criteria(objetivo, objetivo, [])
    mission = MissionEngine().create(
        objetivo, _envelope(objetivo), success_criteria=criteria
    )

    # Mundo vacío: nadie ha observado nada.
    ver = GoalVerifier(world=WorldModel()).verify(mission)
    assert ver.verified is False
    settle(mission, ver)
    assert mission.state is not MissionState.COMPLETED


@pytest.mark.asyncio
async def test_a3_respuesta_no_verificada_no_dice_que_lo_analizó(tmp_path):
    """El criterio es sobre el contenido; la respuesta no puede vender algo que no leyó."""
    from alexis.cognition.response import ResponseComposer

    composer = ResponseComposer()
    mission = _dummy_mission()
    knowledge = cognitive_knowledge()
    verification = GoalVerification(
        objective="Analiza el archivo notas.txt y dime qué contiene",
        evaluations=[],
        verified=False,
        reason="el contenido no fue observado",
    )
    reply = composer.compose(mission, knowledge, verification)
    assert reply.goal_verified is False
    assert not composer.asserts_completion(reply)


# =========================================================================== #
# B. Objetivo de transformación: entrada leída Y salida producida
# =========================================================================== #


@pytest.mark.asyncio
async def test_b1_crear_un_resumen_exige_leer_la_entrada_y_producir_la_salida(tmp_path):
    """Caso B — el contrato es una CADENA, y las dos mitades se comprueban.

    "crea salida.txt con un resumen de notas.txt" exige dos cosas distintas: que se leyó
    la entrada y que se produjo una salida con contenido. Comprobar sólo la salida dejaría
    pasar un `cp` sin leer nada; comprobar sólo la entrada dejaría pasar un análisis que no
    escribió el fichero pedido.
    """
    (tmp_path / "notas.txt").write_text(ENTRADA, encoding="utf-8")
    objetivo = "crea salida.txt con un resumen de notas.txt"

    criteria, _ = normalize_criteria(objetivo, objetivo, [])
    assert criteria == [
        "content_observed:notas.txt",
        "file_exists:salida.txt",
        "file_size_at_least:salida.txt:1",
    ], "una transformación exige la cadena completa, no el último eslabón"

    # La salida NO existe todavía: se prepara el estado inicial, no el resultado.
    assert not (tmp_path / "salida.txt").exists()

    # Con la entrada leída pero sin salida, el objetivo NO está cumplido.
    world_solo_entrada = WorldModel()
    _observar_lectura(world_solo_entrada, "notas.txt", ENTRADA)
    mission = MissionEngine().create(
        objetivo, _envelope(objetivo), success_criteria=criteria
    )
    ver_parcial = GoalVerifier(world=world_solo_entrada).verify(mission)
    assert ver_parcial.verified is False
    unmet = {e.criterion for e in ver_parcial.evaluations if e.status is not CriterionStatus.SATISFIED}
    assert "file_exists:salida.txt" in unmet

    # La escritura por sí sola NO basta para acreditar que el fichero existe: el guard
    # CORE-09 exige una observación independiente (la hace el paso `verify` del plan).
    _observar_escritura(world_solo_entrada, "salida.txt", "resumen real\n")
    ver_solo_write = GoalVerifier(world=world_solo_entrada).verify(mission)
    assert ver_solo_write.verified is False, (
        "la propia escritura no puede atestiguar que su resultado existe: eso sería "
        "el circuito cerrado que §5.3 veta"
    )

    # Con la verificación independiente, sí.
    _observar_verificacion(world_solo_entrada, "salida.txt", "resumen real\n")
    ver_completo = GoalVerifier(world=world_solo_entrada).verify(mission)
    assert ver_completo.verified is True


@pytest.mark.asyncio
async def test_b2_salida_vacia_no_cumple_la_transformacion(tmp_path):
    """Un fichero vacío no es «un resumen»: el contrato exige contenido real."""
    (tmp_path / "notas.txt").write_text(ENTRADA, encoding="utf-8")
    objetivo = "crea salida.txt con un resumen de notas.txt"
    criteria, _ = normalize_criteria(objetivo, objetivo, [])
    mission = MissionEngine().create(
        objetivo, _envelope(objetivo), success_criteria=criteria
    )

    world = WorldModel()
    _observar_lectura(world, "notas.txt", ENTRADA)
    _observar_escritura(world, "salida.txt", "")
    _observar_verificacion(world, "salida.txt", "")
    ver = GoalVerifier(world=world).verify(mission)
    assert ver.verified is False
    fallidos = {e.criterion for e in ver.evaluations if e.status is not CriterionStatus.SATISFIED}
    assert "file_size_at_least:salida.txt:1" in fallidos


# =========================================================================== #
# C. Objetivo no comprobable: NO se degrada a file_exists
# =========================================================================== #


def test_c1_un_objetivo_semantico_sin_ruta_no_se_convierte_en_file_exists():
    """Caso C — si no hay predicado que lo sostenga, NO se inventa uno más débil."""
    objetivo = "haz que el sistema sea más inteligente"
    criteria, status = normalize_criteria(objetivo, objetivo, [])
    assert criteria == [], (
        "un objetivo sin artefacto comprobable no puede convertirse en file_exists:*"
    )
    assert status["verifiable"] == 0
    assert not [c for c in criteria if c.startswith("file_exists")]


def test_c2_una_ruta_mencionada_no_habilita_file_exists_por_si_sola():
    """La regla que cierra el agujero: la ruta NO basta para derivar `file_exists`.

    Si mencionar una ruta fuera suficiente, cualquier objetivo con un nombre de fichero
    acabaría acreditado como «existe», que es el bug original.
    """
    for objetivo in (
        "Analiza notas.txt y dime qué contiene",
        "lee notas.txt",
        "resume notas.txt",
        "dime qué dice notas.txt",
    ):
        criteria, _ = normalize_criteria(objetivo, objetivo, [])
        assert criteria, objetivo
        assert "file_exists:notas.txt" not in criteria, (
            f"«{objetivo}» no puede verificarse comprobando que el fichero existe"
        )


# =========================================================================== #
# D. Regresión: no se puede volver a degradar un objetivo semántico
# =========================================================================== #


@pytest.mark.parametrize(
    "familia",
    ["existence", "destructive", "creation", "modification", "analysis", "transformation", "query"],
)
def test_d1_las_familias_semanticas_siguen_siendo_distintas(familia):
    """Guarda de no-regresión: cada familia semántica existe y es distinguishable.

    Sin esto, una simplificación futura podría volver a mapear todo a `existence` y el
    sistema recuperaría el bug sin que ningún test lo nombrara.
    """
    from alexis.cognition.criteria import SEMANTICS

    assert familia in SEMANTICS
    assert classify_objective_semantics(
        {
            "existence": "comprueba si notas.txt existe",
            "destructive": "borra el archivo temporal.txt",
            "creation": "crea el archivo salida.txt con contenido",
            "modification": "modifica el archivo config.txt",
            "analysis": "analiza el contenido de reporte.txt",
            "transformation": "crea salida.txt con un resumen de notas.txt",
            "query": "qué puedes hacer por mí",
        }[familia]
    ) == familia


def test_d2_un_objetivo_analitico_nunca_deriva_file_exists():
    """El criterio de análisis exige contenido, en cualquier redacción del objetivo."""
    for redaccion in (
        "analiza notas.txt",
        "analiza el archivo notas.txt y dime qué contiene",
        "dime qué contiene notas.txt",
        "lee el contenido de notas.txt",
        "resume notas.txt",
        "explica qué dice notas.txt",
    ):
        criteria = criteria_for_objective(redaccion)
        assert criteria == ["content_observed:notas.txt"], redaccion


def test_d3_content_observed_exige_observacion_de_una_tool():
    """Un `content_observed` sin observación de tool alguna no se puede cumplir."""
    from alexis.cognition.goal_verification import parse_predicate

    assert parse_predicate("content_observed:notas.txt") == ("content_observed", ["notas.txt"])
    verificador = GoalVerifier(world=WorldModel())
    evaluacion = verificador._evaluate_criterion("content_observed:notas.txt")
    assert evaluacion.status is CriterionStatus.INSUFFICIENT_EVIDENCE
    assert "nadie ha observado" in evaluacion.reason


# =========================================================================== #
# E. E2E real a través del runtime OFICIAL, sin pre-siembrar el resultado
# =========================================================================== #


@pytest.mark.asyncio
async def test_e1_caso_a_analizar_un_fichero_real_no_pre_existente(tmp_path):
    """Caso A de punta a punta: el objetivo se cumple porque ALEXIS leyó.

    El fichero de ENTRADA se prepara (es el material de trabajo, no el resultado), pero
    nada del resultado queda escrito antes de que ALEXIS actúe. Y el verificador de la
    misión se mira después, sobre el mundo real.
    """
    (tmp_path / "notas.txt").write_text(ENTRADA, encoding="utf-8")
    runtime, cognitive, world = _real_runtime(tmp_path)

    mission, intent = await _mission_from_turn(
        "Analiza el archivo notas.txt y dime qué contiene", tmp_path
    )
    assert intent.success_criteria == ["content_observed:notas.txt"]

    resultado = await runtime.run_mission(mission)

    assert resultado.state is MissionState.COMPLETED, resultado.context.get(
        "goal_verification_reason"
    )
    assert resultado.goal_verification is not None
    assert goal_is_confirmed(resultado.goal_verification) is True

    # Y la evidencia es una LECTURA real, no una mera existencia.
    entidad = world.known_path("notas.txt")
    assert entidad is not None
    assert "fs.read" in str(entidad.source), str(entidad.source)
    assert entidad.attributes.get("content_observed") is True


@pytest.mark.asyncio
async def test_e2_caso_b_la_transformacion_se_encadena_y_se_verifica(tmp_path):
    """Caso B de punta a punta: se lee la entrada y se produce la salida.

    `salida.txt` NO existe al empezar: el resultado no está sembrado. El plan tiene
    que encadenar lectura → escritura → verificación, y el objetivo sólo se cumple si las
    treslegs ocurrieron con evidencia real.
    """
    (tmp_path / "notas.txt").write_text(ENTRADA, encoding="utf-8")
    assert not (tmp_path / "salida.txt").exists(), (
        "el resultado NO puede estar prepared antes: sería pre-sembrar la evidencia"
    )

    runtime, cognitive, world = _real_runtime(tmp_path)
    mission, intent = await _mission_from_turn(
        "crea salida.txt con un resumen de notas.txt", tmp_path
    )
    assert intent.success_criteria == [
        "content_observed:notas.txt",
        "file_exists:salida.txt",
        "file_size_at_least:salida.txt:1",
    ]

    # El plan encadena la transformación: sin leer, no se puede escribir.
    plan = await _plan_de_lectura(cognitive, mission.goal.objective)
    assert [s.id for s in plan.steps] == ["research", "execute", "verify"]
    por_id = {s.id: s for s in plan.steps}
    assert por_id["execute"].depends_on == ["research"]
    assert por_id["research"].args["path"] == "notas.txt"
    assert por_id["execute"].args["path"] == "salida.txt"

    resultado = await runtime.run_mission(mission)

    # Lo que se verifica es la cadena, no sólo que el fichero exista.
    verificacion = resultado.goal_verification
    assert verificacion is not None
    for evaluacion in verificacion.evaluations:
        assert evaluacion.status is CriterionStatus.SATISFIED, (
            f"{evaluacion.criterion}: {evaluacion.reason}"
        )
    assert resultado.state is MissionState.COMPLETED, resultado.context.get(
        "goal_verification_reason"
    )
    assert (tmp_path / "salida.txt").exists()


@pytest.mark.asyncio
async def test_e3_caso_c_un_objetivo_no_comprobable_no_se_convierte_en_file_exists(tmp_path):
    """Caso C: un objetivo sin artefacto comprobable NO se degrada a un criterio trivial.

    Es la mitad negativa del E2E: si el sistema no puede comprobar lo que se le pide, tiene
    que decirlo. Convertirlo en `file_exists:*` era exactamente el fallo que cierra §11.
    """
    objetivo = "haz que el sistema sea más inteligente"

    criteria, status = normalize_criteria(objetivo, objetivo, [])
    assert criteria == [], "un objetivo sin ruta ni suite no admite predicado inventado"
    assert status["verifiable"] == 0
    assert "no nombran" in status["reason"] or status["reason"]

    runtime, cognitive, world = _real_runtime(tmp_path)
    mission = MissionEngine().create(
        objetivo,
        _envelope(objetivo),
        success_criteria=criteria,
    )
    resultado = await runtime.run_mission(mission)

    assert resultado.state is not MissionState.COMPLETED, (
        "un objetivo que no se puede comprobar no puede cerrar como COMPLETED"
    )
    # Y tampoco se ha disfrazado de existencia: no hay ningún file_exists en su contrato.
    assert not [c for c in mission.goal.success_criteria if c.startswith("file_exists")]


@pytest.mark.asyncio
async def test_e4_regresion_el_contrato_semantico_no_vuelve_a_degradarse(tmp_path):
    """Guarda de no-regresión sobre el E2E completo.

    Si alguien reintrodujera la degradación (una ruta → `file_exists`), el objetivo de
    análisis volvería a cerrarse sin leer nada. Aquí se comprueba sobre el runtime real.
    """
    (tmp_path / "notas.txt").write_text(ENTRADA, encoding="utf-8")
    runtime, cognitive, world = _real_runtime(tmp_path)

    mission, _ = await _mission_from_turn(
        "Analiza el archivo notas.txt y dime qué contiene", tmp_path
    )
    contrato = list(mission.goal.success_criteria)
    assert contrato == ["content_observed:notas.txt"], (
        "el contrato de un objetivo de contenido no puede volver a ser file_exists"
    )

    # Y con un mundo donde NO se leyó nada, el mismo objetivo NO cierra.
    ver = GoalVerifier(world=WorldModel()).verify(mission)
    assert ver.verified is False
    settle(mission, ver)
    assert mission.state is not MissionState.COMPLETED


# =========================================================================== #
# Helpers
# =========================================================================== #


def _dummy_mission():
    return MissionEngine().create(
        "lee el archivo notas.txt",
        _envelope("lee el archivo notas.txt"),
        success_criteria=["content_observed:notas.txt"],
    )


def cognitive_knowledge():
    from alexis.cognition.state import KnowledgeState, Verdict

    knowledge = KnowledgeState(objective="Analiza el archivo notas.txt y dime qué contiene")
    knowledge.last_verdict = Verdict.INSUFFICIENT_EVIDENCE.value
    return knowledge


async def _plan_de_lectura(cognitive, objetivo):
    from alexis.cognition.planner import Planner

    mission = MissionEngine().create(objetivo, _envelope(objetivo))
    return await Planner().create_plan(mission)


def _observar_lectura(world: WorldModel, path: str, contenido: str) -> None:
    """Observa una LECTURA real (con contenido), como lo haría `fs.read`."""
    from alexis.contracts import ExecutionResult, Observation

    world.observe_execution(
        type("S", (), {"id": "read", "capability": "fs.read", "action": "research"})(),
        ExecutionResult(
            success=True,
            output={"path": path, "exists": True, "size": len(contenido), "content": contenido},
            observations=[Observation("tool.fs.read", {"read": True}, trusted=True)],
        ),
    )


def _observar_escritura(world: WorldModel, path: str, contenido: str) -> None:
    """Observa una ESCRITURA, como la haría `fs.write` (que no puede atestiguar lectura)."""
    from alexis.contracts import ExecutionResult, Observation

    world.observe_execution(
        type("S", (), {"id": "write", "capability": "fs.write", "action": "execute"})(),
        ExecutionResult(
            success=True,
            output={"path": path, "exists": True, "size": len(contenido), "ok": True},
            observations=[Observation("tool.fs.write", {"ok": True}, trusted=True)],
        ),
    )


def _observar_verificacion(world: WorldModel, path: str, contenido: str) -> None:
    """Observación INDEPENDIENTE, como la del paso `verify` (`verification.filesystem`).

    Es la que puede acreditar existencia y tamaño: mira el fichero en lugar de escribirlo.
    """
    from alexis.contracts import ExecutionResult, Observation

    world.observe_execution(
        type("S", (), {"id": "v", "capability": "verification.filesystem", "action": "verify"})(),
        ExecutionResult(
            success=True,
            output={"path": path, "exists": True, "size": len(contenido)},
            observations=[Observation("tool.verification.filesystem", {"verified": True}, trusted=True)],
        ),
    )


def test_a1_sin_verificador_de_paso_esto_no_pasaria():
    """Auto-comprobación del test: sin el predicado nuevo, el objetivo NO cerraría.

    Si alguien revirtiera la clasificación semántica, este test falla Y el de arriba también:
    los dos lados del contrato están fijados.
    """
    objetivo = "Analiza el archivo notas.txt y dime qué contiene"
    criteria, _ = normalize_criteria(objetivo, objetivo, [])
    assert criteria == ["content_observed:notas.txt"]
    # El predicado viejo NO sería suficiente: con un mundo vacío tampoco se cumple.
    viejo = MissionEngine().create(
        objetivo, _envelope(objetivo), success_criteria=["file_exists:notas.txt"]
    )
    assert GoalVerifier(world=WorldModel()).verify(viejo).verified is False