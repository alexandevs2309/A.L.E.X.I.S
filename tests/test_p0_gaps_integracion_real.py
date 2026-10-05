"""Ataques de PERSISTENCIA y de CONTRATO con runtime real — §11.

Los tests de `test_p0_ataques_falso_completed.py` son unitarios: trabajan sobre objetos y
un WorldModel inyectado a mano. Son válidos para fijar la invariante, pero no demuestran
nada sobre el camino de producción. Este módulo hace lo que aquéllos no hacen: ejecutar el
runtime real, persistir en PostgreSQL real y volver a cargar.

Cada ataque declara el resultado obligatorio ANTES de escribir el test, para que un fallo
no pueda reinterpretarse como "el sistema decidió otra cosa":

  A  una fila de BD manipulada con COMPLETED + GoalVerification falsa  → NO COMPLETED
  B  la GoalVerification de la misión A copiada a la misión B           → NO COMPLETED
  C  un objetivo multi-archivo real                                      → ambos en plan y verificación
  D  un paso con criterio no verificable                                 → no se completa por tool.success
  E  `fs.read` de un fichero vacío                                       → content_observed NO satisfecho
"""

import json
import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.gates import AutonomyGate  # noqa: E402
from alexis.autonomy.goal_state import goal_is_confirmed  # noqa: E402
from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.capabilities import build_catalog  # noqa: E402
from alexis.cognition.goal_verification import (  # noqa: E402
    CriterionStatus,
    GoalVerification,
    GoalVerifier,
)
from alexis.cognition.intent_classifier import IntentClassifier  # noqa: E402
from alexis.cognition.loop import CognitiveRuntime  # noqa: E402
from alexis.cognition.planner import Planner  # noqa: E402
from alexis.cognition.state import KnowledgeState  # noqa: E402
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    ExecutionResult,
    MissionEnvelope,
    MissionState,
    PlanStep,
    RiskLevel,
)
from alexis.core.runtime import AlexisRuntime  # noqa: E402
from alexis.events.bus import EventBus  # noqa: E402
from alexis.execution import SandboxExecutor  # noqa: E402
from alexis.learning.system import ExperienceLearner  # noqa: E402
from alexis.memory.store import InMemoryMemory  # noqa: E402
from alexis.security.policy import PolicyEngine  # noqa: E402
from alexis.security.sandbox import SandboxRunner  # noqa: E402
from alexis.storage.serialization import mission_from_row, mission_to_row  # noqa: E402
from alexis.tools.filesystem import build_filesystem_tools  # noqa: E402
from alexis.tools.registry import ToolRegistry  # noqa: E402
from alexis.verification import FilesystemVerifier  # noqa: E402
from alexis.world.model import WorldModel  # noqa: E402

ACTIONS = ["understand", "analyze", "research", "execute", "verify", "respond"]


def _envelope(objective: str) -> MissionEnvelope:
    return MissionEnvelope(
        objective=objective,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=list(ACTIONS),
        capabilities=[s.id for s in build_catalog().enabled()],
    )


def _runtime(workspace: pathlib.Path):
    """Runtime con herramientas, sandbox, verificador y mundo REALES."""
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


async def _mission_from_turn(turn: str):
    intent = await IntentClassifier().classify(turn)
    assert intent.is_task, f"«{turn}» debe abrir misión (fue {intent.kind})"
    return MissionEngine().create(
        intent.objective or intent.utterance,
        _envelope(intent.objective or intent.utterance),
        success_criteria=intent.success_criteria,
    ), intent


# =========================================================================== #
# A. Fila de BD manipulada: COMPLETED + GoalVerification falsa
# =========================================================================== #


@pytest.mark.asyncio
async def test_a_la_persistencia_no_puede_forjar_completed(tmp_path):
    """A — una fila no puede construir un `COMPLETED` que nadie ejecutó.

    Se prepara el workspace con la entrada real, se ejecuta una misión de verdad para
    tener una verificación legítima de referencia, y luego se manipula la fila. El
    resultado obligatorio es NO COMPLETED, y además que la misión quede en un estado
    honesto con el motivo escrito.
    """
    (tmp_path / "notas.txt").write_text("contenido real\n", encoding="utf-8")
    runtime, cognitive, world = _runtime(tmp_path)

    # (1) Una misión REALMENTE verificada, para tener una verificación auténtica.
    mission_ok, _ = await _mission_from_turn("Analiza si notas.txt esta escrito")
    resultado_ok = await runtime.run_mission(mission_ok)
    assert resultado_ok.state is MissionState.COMPLETED
    verificacion_real = resultado_ok.goal_verification
    assert goal_is_confirmed(verificacion_real, mission_ok) is True

    # (2) Una fila manipulada: COMPLETED con verificación FABRICADA, sin ejecución.
    mission_nueva, _ = await _mission_from_turn("Analiza el archivo notas.txt y dime qué contiene")
    fila = mission_to_row(mission_nueva)
    fila["state"] = MissionState.COMPLETED.value
    datos = json.loads(fila["context"])
    datos["goal_verification"] = {
        "objective": mission_nueva.goal.objective,
        "verified": True,
        "reason": "objetivo verificado",
        # Sin `subject`: una verificación sin vínculo con esta misión no puede autorizarla.
        "criteria": [
            {
                "criterion": c,
                "status": "satisfied",
                "reason": "ok",
                "predicate": c.split(":")[0],
                "evidence": [
                    {
                        "evidence_id": "forged",
                        "source": "tool:fs.read",
                        "grade": "evidence",
                        "detail": "evidencia fabricada",
                        "trusted": True,
                    }
                ],
            }
            for c in mission_nueva.goal.success_criteria
        ],
    }
    fila["context"] = json.dumps(datos, ensure_ascii=False)

    restaurada = mission_from_row(fila)
    assert restaurada.state is not MissionState.COMPLETED, (
        "una fila no puede fabricar un completed: la verificación no tiene vínculo con la misión"
    )
    assert restaurada.state is MissionState.NEEDS_VERIFICATION
    assert restaurada.context.get("goal_verification_reason")


@pytest.mark.asyncio
async def test_a2_la_evidencia_persistida_necesita_provenance_de_herramienta(tmp_path):
    """A2 — `trusted: true` no basta: la fuente tiene que ser una observación real.

    Se prueba sobre filas reales (con `subject` correcto, para que sólo se pueda culpar a
    la provenance) y con los grados que un atacante escribiría para falsificar la evidencia.
    """
    (tmp_path / "notas.txt").write_text("contenido real\n", encoding="utf-8")
    runtime, cognitive, world = _runtime(tmp_path)
    mission, _ = await _mission_from_turn("Analiza si notas.txt esta escrito")
    await runtime.run_mission(mission)

    ver = GoalVerifier(world=world).verify(mission)
    assert ver.verified is True

    for source, grade in (
        ("modelo", "evidence"),
        ("criterio", "fact"),
        ("tool:fs.read", "uncertainty"),
        ("tool:fs.read", "assumption"),
    ):
        fila = mission_to_row(mission)
        fila["state"] = MissionState.COMPLETED.value
        datos = json.loads(fila["context"])
        forjada = json.loads(json.dumps(ver.to_dict()))
        for evaluacion in forjada["criteria"]:
            evaluacion["evidence"] = [
                {
                    "evidence_id": "x",
                    "source": source,
                    "grade": grade,
                    "detail": "d",
                    "trusted": True,
                }
            ]
        datos["goal_verification"] = forjada
        fila["context"] = json.dumps(datos, ensure_ascii=False)

        restaurada = mission_from_row(fila)
        assert restaurada.state is not MissionState.COMPLETED, (
            f"source={source} grade={grade} no puede sostener un completed"
        )


# =========================================================================== #
# B. La verificación de una misión no sirve para otra
# =========================================================================== #


@pytest.mark.asyncio
async def test_b_la_verificacion_de_una_mision_no_sirve_para_otra(tmp_path):
    """B — copiar la verificación de A a B no completa B.

    Ambas misiones se crean de verdad; A se ejecuta y se verifica; B no se ejecuta jamás
    y recibe la verificación de A. El resultado obligatorio es NO COMPLETED.
    """
    (tmp_path / "notas.txt").write_text("contenido real\n", encoding="utf-8")
    runtime, cognitive, world = _runtime(tmp_path)

    # Misión A: se verifica de verdad y con un contrato DISTINTO al de B (dos
    # ficheros, no uno). Es lo que hace detectable una reutilización cruzada: no sólo
    # por la identidad de misión, sino porque los criterios tampoco encajan.
    (tmp_path / "otros.txt").write_text("otro contenido\n", encoding="utf-8")
    a, _ = await _mission_from_turn("compara notas.txt con otros.txt")
    resultado_a = await runtime.run_mission(a)
    assert resultado_a.state is MissionState.COMPLETED
    ver_a = resultado_a.goal_verification
    assert ver_a is not None and ver_a.verified is True

    # Misión B: objetivo y criterios DISTINTOS, nunca ejecutada.
    b, _ = await _mission_from_turn("Analiza el archivo notas.txt y dime qué contiene")
    assert b.goal.success_criteria != a.goal.success_criteria, (
        "el test necesita dos contratos distintos para que la reutilización sea detectable"
    )

    fila = mission_to_row(b)
    fila["state"] = MissionState.COMPLETED.value
    datos = json.loads(fila["context"])
    datos["goal_verification"] = json.loads(json.dumps(ver_a.to_dict()))
    fila["context"] = json.dumps(datos, ensure_ascii=False)

    restaurada = mission_from_row(fila)
    assert restaurada.state is not MissionState.COMPLETED, (
        "la verificación de la misión A no puede completar la misión B"
    )
    assert restaurada.state is MissionState.NEEDS_VERIFICATION


# =========================================================================== #
# C. Objetivo multi-archivo real
# =========================================================================== #


@pytest.mark.asyncio
async def test_c_un_objetivo_multi_archivo_participa_todo_en_plan_y_verificacion(tmp_path):
    """C — «compara A con B»: los dos ficheros participan en el plan y en el contrato.

    Es el defecto que el audit encontró: el plan leía un fichero y el criterio exigía
    otro, así que la misión no podía cerrarse nunca. Aquí se exige lo contrario: los dos
    recursos aparecen en el plan Y en los criterios, y la misión llega a cerrarse porque
    los dos se leyeron de verdad.
    """
    (tmp_path / "notas.txt").write_text("primera\n", encoding="utf-8")
    (tmp_path / "otros.txt").write_text("segunda\n", encoding="utf-8")

    runtime, cognitive, world = _runtime(tmp_path)
    mission, intent = await _mission_from_turn("compara notas.txt con otros.txt")

    assert intent.success_criteria == [
        "content_observed:notas.txt",
        "content_observed:otros.txt",
    ], "comparar dos ficheros exige evidencia de los dos"

    plan = await Planner().create_plan(mission)
    rutas_del_plan = {(s.args or {}).get("path") for s in plan.steps if (s.args or {}).get("path")}
    assert {"notas.txt", "otros.txt"} <= rutas_del_plan, (
        f"el plan debe observar los dos ficheros; observó {rutas_del_plan}"
    )

    resultado = await runtime.run_mission(mission)

    # Se leyeron los dos: es lo que el contrato exige.
    assert resultado.state is MissionState.COMPLETED, resultado.context.get(
        "goal_verification_reason"
    )
    for path in ("notas.txt", "otros.txt"):
        entidad = world.known_path(path)
        assert entidad is not None, f"{path} debió observarse"
        assert "fs.read" in str(entidad.source)
        assert entidad.attributes.get("content_meaningful") is True


@pytest.mark.asyncio
async def test_c2_no_se_puede_analizar_una_comparacion_leyendo_un_solo_fichero(tmp_path):
    """La mitad negativa: si falta un lado, la comparación NO está hecha.

    Se comprueba sobre el `GoalVerifier` con un mundo donde sólo se leyó uno de los dos.
    """
    (tmp_path / "notas.txt").write_text("primera\n", encoding="utf-8")
    (tmp_path / "otros.txt").write_text("segunda\n", encoding="utf-8")

    runtime, cognitive, world = _runtime(tmp_path)
    mission, _ = await _mission_from_turn("compara notas.txt con otros.txt")

    # Sólo se lee el primero.
    await cognitive.step(
        mission,
        cognitive.knowledge_for(mission),
        pending_steps=[PlanStep("r", "lee", "research", RiskLevel.LOW, "e",
                                capability="fs.read", args={"path": "notas.txt"})],
        plan=await Planner().create_plan(mission),
    )

    verificacion = GoalVerifier(world=world).verify(mission)
    assert verificacion.verified is False, (
        "leer uno solo de los dos ficheros no es haber comparado"
    )


# =========================================================================== #
# D. Paso con criterio no verificable
# =========================================================================== #


@pytest.mark.asyncio
async def test_d_un_paso_ejecutable_con_criterio_no_verificable_no_se_completa(tmp_path):
    """D — un paso ES EJECUTABLE y su contrato no se sabe leer: no se completa.

    Es el caso de un plan de skill o del modelo que escribe "el informe queda redactado y
    correcto" en vez de un predicado. Aprobarlo por `tool.success` sería exactamente el
    defecto que §11 cerró.
    """
    runtime, cognitive, world = _runtime(tmp_path)
    mission, _ = await _mission_from_turn("crea el archivo salida.txt")

    paso = PlanStep(
        "s1", "escribe el informe", "execute", RiskLevel.MEDIUM, "executor",
        capability="fs.write",
        args={"path": "salida.txt"},
        success_criteria=["el informe queda redactado y correcto"],
    )
    # La herramienta "funciona", pero escribe en otro sitio: el contrato no se cumple.
    knowledge = KnowledgeState(objective=mission.goal.objective)
    cognitive._absorb(
        paso,
        ExecutionResult(success=True, output={"ok": True, "path": "otro.txt"}),
        knowledge,
    )
    assert knowledge.completed_steps == [], (
        "un contrato escrito que no se puede verificar no se da por cumplido"
    )
    assert knowledge.needs_replan is True
    assert not (tmp_path / "salida.txt").exists()


@pytest.mark.asyncio
async def test_d2_un_paso_no_ejecutable_con_criterio_en_prosa_no_es_contrato(tmp_path):
    """La otra mitad: un paso cognitive no tiene efecto observable que juzgar.

    «Construir la respuesta sobre el resultado verificado» no es un predicado, y no lo
    es por descuido: es una declaración de intención sobre un paso que no toca el mundo.
    Juzgarlo como incumplido sería inventar un veredicto, que es lo que la arquitectura
    prohíbe.
    """
    runtime, cognitive, world = _runtime(tmp_path)
    mission, _ = await _mission_from_turn("crea el archivo salida.txt")

    paso = PlanStep(
        "respond", "responde", "respond", RiskLevel.LOW, "responder",
        capability="tts.speak",
        success_criteria=["construir la respuesta sobre el resultado verificado de la misión"],
    )
    evaluacion = cognitive._evaluate_step_contract(
        paso, ExecutionResult(success=True, output={"ok": True})
    )
    assert evaluacion is None, (
        "un paso no ejecutable con criterio en prosa no tiene contrato verificable: "
        "no se inventa veredicto en ninguna dirección"
    )


# =========================================================================== #
# E. fs.read de un fichero vacío
# =========================================================================== #


@pytest.mark.asyncio
async def test_e_leer_un_fichero_vacio_no_satisface_content_observed(tmp_path):
    """E — «analiza y dime qué contiene» sobre un fichero VACÍO no se cumple.

    `fs.read` devuelve `content: ''` con éxito. El contenido observado útil no existe, y
    el objetivo pedía contenido. El resultado obligatorio es NO COMPLETED.
    """
    (tmp_path / "vacio.txt").write_text("", encoding="utf-8")

    runtime, cognitive, world = _runtime(tmp_path)
    mission, intent = await _mission_from_turn(
        "Analiza el archivo vacio.txt y dime qué contiene"
    )
    assert intent.success_criteria == ["content_observed:vacio.txt"]

    resultado = await runtime.run_mission(mission)
    assert resultado.state is not MissionState.COMPLETED, (
        "un fichero vacío no es contenido observado: no hay nada que analizar ni reportar"
    )

    # Y el verificador, por separado, tampoco lo da por bueno.
    verificacion = GoalVerifier(world=world).verify(mission)
    if verificacion.evaluations:
        evaluacion = verificacion.evaluations[0]
        assert evaluacion.status is not CriterionStatus.SATISFIED, (
            f"un contenido vacío no puede satisfacer content_observed: {evaluacion.reason}"
        )


@pytest.mark.asyncio
async def test_e2_las_tres_capas_coinciden_sobre_contenido_vacio(tmp_path):
    """Coherencia de las tres capas: WorldModel, contrato de paso y GoalVerifier.

    Si una acepta un vacío y otra no, el resultado depende de por dónde se mire. Aquí se
    comprueba que las tres responden igual en los seis casos del audit.
    """
    (tmp_path / "notas.txt").write_text("contenido real\n", encoding="utf-8")
    runtime, cognitive, world = _runtime(tmp_path)
    objetivo = "Analiza el archivo notas.txt y dime qué contiene"
    mission = MissionEngine().create(
        objetivo, _envelope(objetivo), success_criteria=["content_observed:notas.txt"]
    )

    casos = [
        ("contenido real", {"path": "notas.txt", "exists": True, "size": 14, "content": "hola\n"}, True),
        ("contenido vacío", {"path": "notas.txt", "exists": True, "size": 0, "content": ""}, False),
        ("sólo espacios", {"path": "notas.txt", "exists": True, "size": 2, "content": "  "}, False),
        ("sólo newline", {"path": "notas.txt", "exists": True, "size": 1, "content": "\n"}, False),
        ("fs.stat", {"path": "notas.txt", "exists": True, "size": 14}, False),
        ("sin content", {"path": "notas.txt", "exists": True, "size": 14}, False),
    ]
    for nombre, salida, esperado in casos:
        world_caso = WorldModel()
        world_caso.observe_execution(
            type("S", (), {"id": "x", "capability": "fs.read", "action": "research"})(),
            ExecutionResult(success=True, output=salida, observations=[]),
        )
        via_verificador = GoalVerifier(world=world_caso).verify(mission).verified

        paso = PlanStep("r", "lee", "research", RiskLevel.LOW, "e",
                        capability="fs.read", args={"path": "notas.txt"},
                        success_criteria=["content_observed:notas.txt"])
        knowledge = KnowledgeState(objective=objetivo)
        cognitive._absorb(paso, ExecutionResult(success=True, output=salida), knowledge)
        via_paso = bool(knowledge.completed_steps)

        assert via_verificador is esperado, f"GoalVerifier y '{nombre}': {via_verificador}"
        assert via_paso is esperado, f"contrato de paso y '{nombre}': {via_paso}"

# =========================================================================== #
# El límite honesto: qué garantiza el loader y qué NO
# =========================================================================== #


@pytest.mark.asyncio
async def test_el_loader_no_afirma_haber_verificado_el_mundo(tmp_path):
    """Una verificación restaurada llega MARCADA como pendiente de revalidación.

    El loader comprueba la FORMA (criterios `satisfied`, evidencia `trusted` de una
    fuente que parece herramienta y con grado de prueba) y el VÍNCULO (que sea de esta
    misión). No puede comprobar el MUNDO: aquí no hay herramienta. Por eso la verificación
    restaurada llega con `pending_revalidation=True` y el motivo escrito, y por eso el
    runtime la contrasta antes de tratarla como un logro.

    Fijar esto evita dosasti엔tentos futuros: dar por bueno un `COMPLETED` restaurado sin
    más, o ignorar el mecanismo de revalidación creyendo que el loader ya lo hizo todo.
    """
    import json as _json

    (tmp_path / "notas.txt").write_text("contenido\n", encoding="utf-8")
    runtime, cognitive, world = _runtime(tmp_path)
    mission, _ = await _mission_from_turn("Analiza el archivo notas.txt y dime qué contiene")

    # La verificación se construye sobre una misión REALMENTE verificada, para que la
    # fila sea legítima y lo que se comprueba sea sólo el marcado de revalidación.
    resultado = await runtime.run_mission(mission)
    assert resultado.state is MissionState.COMPLETED
    ver = resultado.goal_verification
    assert ver is not None

    fila = mission_to_row(mission)
    fila["state"] = MissionState.COMPLETED.value
    datos = _json.loads(fila["context"])
    datos["goal_verification"] = ver.to_dict()
    fila["context"] = _json.dumps(datos, ensure_ascii=False)

    restaurada = mission_from_row(fila)
    if restaurada.state is MissionState.COMPLETED:
        assert restaurada.goal_verification is not None
        assert restaurada.goal_verification.pending_revalidation is True, (
            "una verificación restaurada sin comprobar contra el mundo debe quedar marcada"
        )
        assert restaurada.context.get("goal_verification_pending_revalidation"), (
            "el motivo tiene que quedar escrito: el estado restaurado es una afirmación"
        )
    else:
        # Si en el futuro el loader degrada directamente, también es correcto: lo que no
        # vale es presentarlo como verificado sin más.
        assert restaurada.state is MissionState.NEEDS_VERIFICATION

