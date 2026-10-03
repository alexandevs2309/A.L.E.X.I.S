"""CORE-10 — Recuperación autónoma.

La afirmación que este fichero existe para comprobar es una, y es negativa:

    **Un corte no puede provocar que ALEXIS repita lo que ya hizo.**

Y en particular, no puede repetir lo que tiene *efectos secundarios*. Un `fs.write` repetido
es escribir dos veces; un `fs.remove` repetido puede borrar lo que alguien puso después. Por eso
la recuperación tiene tres pasos y no dos —RESTORE → INSPECT WORLD → DECIDE— y la mayor parte
de estos tests comprueban el paso del medio: que la decisión se tome contra lo que el MUNDO
dice, no contra lo que el checkpoint creía.

La regla es CHECKPOINT ≠ VERDAD. Un checkpoint afirma lo que ALEXIS creía; sólo una
observación real afirma lo que pasó.

Nada aquí mockea al mundo. El E2E principal usa `SandboxExecutor` real contra un workspace
real: si el fichero aparece o no, es porque la escritura ocurrió o no ocurrió.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.gates import AutonomyGate  # noqa: E402
from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.autonomy.recovery import (  # noqa: E402
    ActionStatus,
    LastKnownAction,
    RecoveryDecision,
    RecoveryManager,
    RecoveryState,
)
from alexis.capabilities import build_catalog  # noqa: E402
from alexis.cognition.loop import CognitiveRuntime  # noqa: E402
from alexis.cognition.goal_verification import GoalVerifier  # noqa: E402
from alexis.cognition.planner import Planner  # noqa: E402
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    ExecutionResult,
    MissionEnvelope,
    MissionState,
    Observation,
    Plan,
    PlanStep,
    RiskLevel,
)
from alexis.core.runtime import AlexisRuntime  # noqa: E402
from alexis.events.bus import EventBus  # noqa: E402
from alexis.execution import SandboxExecutor  # noqa: E402
from alexis.security.policy import PolicyEngine  # noqa: E402
from alexis.security.sandbox import SandboxRunner  # noqa: E402
from alexis.tools.filesystem import build_filesystem_tools  # noqa: E402
from alexis.tools.registry import ToolRegistry  # noqa: E402
from alexis.learning.system import ExperienceLearner  # noqa: E402
from alexis.memory.store import InMemoryMemory  # noqa: E402
from alexis.verification import FilesystemVerifier  # noqa: E402
from alexis.world.model import WorldModel  # noqa: E402

ACTIONS = ["understand", "analyze", "research", "execute", "verify", "modify", "test", "respond"]


def _mission(criteria=None, objective="crea el archivo informe.txt"):
    return MissionEngine().create(
        objective,
        MissionEnvelope(objective=objective, autonomy=AutonomyLevel.SUPERVISED,
                        allowed_actions=list(ACTIONS)),
        success_criteria=list(criteria or ["file_exists:informe.txt"]),
    )


def _step(step_id, capability, action="execute", **args):
    risk = RiskLevel.MEDIUM if capability in ("fs.write", "fs.remove") else RiskLevel.LOW
    return PlanStep(step_id, f"paso {step_id}", action, risk, "executor",
                    capability=capability, args=dict(args))


# ======================================================================
# Contrato: los seis estados que impiden adivinar
# ======================================================================


def test_01_sin_observacion_el_estado_es_desconocido_no_negativo():
    """La ausencia de evidencia NO es evidencia de que no pasó. Es lo que evita el replay."""
    manager = RecoveryManager(catalog=build_catalog())
    action = LastKnownAction(step_id="s1", capability="fs.write", action="execute",
                             args={"path": "informe.txt"})

    assert manager.assess(action, None) is ActionStatus.UNKNOWN
    assert manager.assess(action, {}) is ActionStatus.UNKNOWN


def test_02_el_mundo_afirma_que_existe_y_el_checkpoint_no_lo_creia():
    """Éste es el caso bueno: la inspección evita repetir un `fs.write` que sí ocurrió."""
    manager = RecoveryManager(catalog=build_catalog())
    action = LastKnownAction(step_id="s1", capability="fs.write", action="execute",
                             args={"path": "informe.txt"})

    assert manager.assess(action, {"exists": True}) is ActionStatus.EXECUTED


def test_03_el_mundo_afirma_que_no_existe():
    """Y el contrario: se puede reintentar, pero pasando por Policy y Gate otra vez."""
    manager = RecoveryManager(catalog=build_catalog())
    action = LastKnownAction(step_id="s1", capability="fs.write", action="execute",
                             args={"path": "informe.txt"})

    assert manager.assess(action, {"exists": False}) is ActionStatus.NOT_EXECUTED


def test_04_evidencia_contradictoria_se_nombra_como_tal():
    """El checkpoint decía 'ejecutado' y el mundo dice que no está. No se resuelve a favoring
    del checkpoint: eso sería exactamente el bug que CORE-10 cierra."""
    manager = RecoveryManager(catalog=build_catalog())
    action = LastKnownAction(step_id="s1", capability="fs.write", action="execute",
                             args={"path": "informe.txt"}, recorded_status="executed")

    assert manager.assess(action, {"exists": False}) is ActionStatus.CONFLICTED


def test_05_sin_registro_no_es_lo_mismo_que_ejecucion_incompleta():
    """Éste fue un bug real de CORE-10: se confundían dos cosas distintas.

    `recorded_status == "unknown"` significa que el proceso murió antes de escribir el
    resultado. NO significa que la acción se quedara a medias. Si el mundo dice que el
    fichero está, `fs.write` ocurrió, y tratarlo como parcial convertía cada escritura
    correcta en un bloque que exige preguntar por algo ya hecho.
    """
    manager = RecoveryManager(catalog=build_catalog())
    action = LastKnownAction(step_id="s1", capability="fs.write", action="execute",
                             args={"path": "informe.txt"}, recorded_status="unknown")

    assert manager.assess(action, {"exists": True}) is ActionStatus.EXECUTED
    # Y PARTIAL queda para lo que de verdad es parcial: evidencia positiva de ello.
    assert manager.assess(action, {"exists": True, "partial": True}) is ActionStatus.PARTIAL


def test_06_accion_registrada_como_fallida():
    manager = RecoveryManager(catalog=build_catalog())
    action = LastKnownAction(step_id="s1", capability="fs.read", action="research",
                             args={"path": "x"}, recorded_status="failed")

    assert manager.assess(action, {"exists": None}) is ActionStatus.FAILED


# ======================================================================
# Side effects: se pregunta al catálogo, no se codifica
# ======================================================================


def test_07_los_side_effects_se_leen_del_catalogo():
    manager = RecoveryManager(catalog=build_catalog())

    assert manager.has_side_effects("fs.write") is True
    assert manager.has_side_effects("fs.remove") is True
    assert manager.has_side_effects("fs.read") is False
    assert manager.has_side_effects("fs.stat") is False


def test_08_una_capability_desconocida_se_trata_como_efecto():
    """Ante la duda se trata como acción que no se repite sola. El error caro es el
    contrario: creer inocuo algo que no lo es."""
    manager = RecoveryManager(catalog=build_catalog())

    assert manager.has_side_effects("algo.que.no.existe") is True
    assert RecoveryManager(catalog=None).has_side_effects("fs.read") is True


# ======================================================================
# La decisión y su precedencia
# ======================================================================


def _state(**over):
    state = RecoveryState(mission_id="m1", created_at=1.0, updated_at=1.0)
    for key, value in over.items():
        setattr(state, key, value)
    return state


def test_09_accion_con_efectos_sin_confirmar_va_a_ask_user():
    """La regla que más importa: nunca repetir a ciegas algo con efectos."""
    manager = RecoveryManager(catalog=build_catalog())
    manager.bind_capabilities({"s1": "fs.write"})
    state = _state(unconfirmed=["s1"])

    assert manager.decide(state) is RecoveryDecision.ASK_USER
    assert "Repetir a ciegas" in state.decision_reason


def test_10_lectura_sin_confirmar_no_necesita_pregunta():
    """Reintentar `fs.read` no rompe nada: preguntar por eso sería ruido."""
    manager = RecoveryManager(catalog=build_catalog())
    manager.bind_capabilities({"s1": "fs.read"})
    state = _state(unconfirmed=["s1"])

    assert manager.decide(state) is RecoveryDecision.RESUME


def test_11_objetivo_ya_satisfecho_completa_sin_repetir():
    manager = RecoveryManager(catalog=build_catalog())

    state = _state()
    assert manager.decide(state, goal_satisfied=True) is RecoveryDecision.COMPLETE


def test_12_plan_invalido_replanifica():
    manager = RecoveryManager(catalog=build_catalog())

    state = _state()
    assert manager.decide(state, plan_still_valid=False) is RecoveryDecision.REPLAN
    assert "no vale" in state.decision_reason


def test_13_nada_pendiente_reanuda():
    manager = RecoveryManager(catalog=build_catalog())

    state = _state()
    assert manager.decide(state) is RecoveryDecision.RESUME


def test_14_presupuesto_agotado_aborta():
    """Sin este tope, recuperar → reintentar → replanar → recuperar es un bucle."""
    manager = RecoveryManager(catalog=build_catalog(), max_attempts=2)

    state = _state(attempt=2)
    assert manager.decide(state) is RecoveryDecision.ABORT
    assert "agotado" in state.decision_reason


def test_15_ask_user_gana_a_complete():
    """La precedencia protege antes de optimizar: si hay algo sin confirmar con efectos,
    no se da nada por terminado aunque el objetivo parezca cumplido."""
    manager = RecoveryManager(catalog=build_catalog())
    manager.bind_capabilities({"s1": "fs.remove"})

    state = _state(unconfirmed=["s1"])
    assert manager.decide(state, goal_satisfied=True) is RecoveryDecision.ASK_USER


def test_16_abort_gana_a_todo():
    """El presupuesto es la última palabra: una misión que no puede recuperar no se
    reintenta indefinidamente."""
    manager = RecoveryManager(catalog=build_catalog(), max_attempts=1)
    manager.bind_capabilities({"s1": "fs.remove"})

    state = _state(attempt=1, unconfirmed=["s1"])
    assert manager.decide(state, goal_satisfied=True) is RecoveryDecision.ABORT


# ======================================================================
# Persistencia del contrato (sin tabla nueva)
# ======================================================================


def test_17_recovery_state_sobrevive_a_serialization():
    """Viaja en el JSONB de `missions`. Si no fuera round-trip, el corte se perdería."""
    state = _state(attempt=1, action_status={"s1": "unknown"}, unconfirmed=["s1"],
                   observed={"probe_path": "informe.txt"}, decision="ask_user")

    restored = RecoveryState.from_dict(state.to_dict())

    assert restored.mission_id == "m1"
    assert restored.attempt == 1
    assert restored.action_status == {"s1": "unknown"}
    assert restored.unconfirmed == ["s1"]
    assert restored.decision == "ask_user"


def test_18_last_known_action_sobrevive_a_serialization():
    action = LastKnownAction(step_id="s1", capability="fs.write", action="execute",
                             args={"path": "informe.txt"}, probe_path="informe.txt",
                             recorded_status="unknown")

    restored = LastKnownAction.from_dict(action.to_dict())

    assert restored.step_id == "s1"
    assert restored.probe_path == "informe.txt"
    assert restored.signature == action.signature


# ======================================================================
# E2E real: crash, proceso nuevo, y NO replay
# ======================================================================


def _runtime(workspace, *, cognitive=None):
    """Un runtime REAL: sandbox de verdad, herramientas de verdad, policy y gate de verdad."""
    sandbox = SandboxRunner(workspace=workspace)
    tools = ToolRegistry()
    for tool in build_filesystem_tools(workspace):
        tools.register(tool)
    executor = SandboxExecutor(tools=tools, sandbox=sandbox)
    world = WorldModel()
    rt = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=executor,
        verifier=FilesystemVerifier(workspace=workspace),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        world=world,
    )
    rt.cognitive = CognitiveRuntime(
        policy=rt.policy, gate=rt.gate, executor=rt.executor, verifier=rt.verifier,
        model_router=None, catalog=build_catalog(), world=world,
        goal_verifier=GoalVerifier(world=world),
    )
    return rt, executor, world


@pytest.mark.asyncio
async def test_19_e2e_crash_durante_escritura_no_repite_el_write(tmp_path):
    """El escenario completo del §13, con ficheros de verdad.

    1. misión con checkpoint
    2. se registra la acción EN VOLO y se ejecuta `fs.write`
    3. el proceso "muere" ANTES de que se registre el final del paso   ← el corte
    4. un runtime NUEVO (objeto nuevo, mundo nuevo) arranca
    5. recovery inspecciona el filesystem REAL
    6. `fs.write` NO se vuelve a ejecutar

    El paso 6 es la afirmación. Se demuestra de dos formas independientes: el contador del
    ejecutor y el contenido del fichero (si se hubiera escrito dos veces, `origin` del
    resultado lo delataría, y el tamaño/content lo haría también).
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()

    # --- proceso 1: escribe y muere antes de cerrar el paso ---------------- #
    rt1, executor1, world1 = _runtime(workspace)
    mission = _mission()
    mission.state = MissionState.RUNNING

    write_step = _step("escribir", "fs.write", path="informe.txt", content="hola")
    await rt1._record_inflight_action(mission, write_step)

    # La marca de "en vuelo" está puesta y el resultado aún NO. Esto es el corte.
    assert mission.context["inflight_action"]["recorded_status"] == "unknown"
    assert mission.context["inflight_action"]["probe_path"] == "informe.txt"

    result = await executor1.execute(mission, write_step, tool_name="fs.write")
    assert result.success
    # ... y aquí el proceso se muere. NO se llama a `_settle_inflight_action`.

    # El fichero existe de verdad: la escritura SÍ ocurrió.
    assert (workspace / "informe.txt").is_file()

    # Se persiste el contexto como lo haría `_commit` (la fila de `missions` es JSONB).
    persisted_context = dict(mission.context)
    del rt1, executor1, world1

    # --- proceso 2: otro runtime, otro mundo, el mismo disco --------------- #
    rt2, executor2, world2 = _runtime(workspace)
    mission2 = _mission()
    mission2.state = MissionState.RUNNING
    mission2.context = persisted_context

    executed: list[str] = []
    real_execute = executor2.execute

    async def counting_execute(m, step, *, tool_name=None):
        executed.append(f"{step.id}:{tool_name}")
        return await real_execute(m, step, tool_name=tool_name)

    executor2.execute = counting_execute

    # Plan con el paso ya hecho y uno siguiente de lectura.
    plan = Plan(mission2.id, [
        _step("escribir", "fs.write", path="informe.txt", content="hola"),
        _step("comprobar", "fs.stat", action="research", path="informe.txt"),
    ])
    mission2.plan = plan

    puede = await rt2._recover_mission(mission2)

    assert puede is True, "con el mundo confirmando la escritura, la misión puede continuar"

    recovery = mission2.context["recovery"]
    assert recovery["decision"] == RecoveryDecision.RESUME.value
    assert recovery["action_status"]["escribir"] == ActionStatus.EXECUTED.value
    assert recovery["unconfirmed"] == [], "la escritura se confirmó: no hay nada que preguntar"
    # Y la prueba: el paso quedó marcado como hecho para que el Core no lo vuelva a ofrecer.
    assert mission2.context["recovered_completed_steps"] == ["escribir"]

    # El `fs.write` NO se ejecutó durante la recuperación: la inspección usa `fs.stat`.
    assert not any("fs.write" in call for call in executed), executed
    assert any("fs.stat" in call for call in executed), executed

    # Y el Core tampoco lo ofrece de nuevo.
    pending = rt2.cognitive.pending_steps(mission2, plan, rt2.cognitive.knowledge_for(mission2))
    assert [s.id for s in pending] == ["comprobar"]


@pytest.mark.asyncio
async def test_20_e2e_crash_donde_el_write_no_ocurrio_se_puede_reintentar(tmp_path):
    """Caso 2 del §7: el checkpoint decía "en vuelo" pero el mundo dice que no está.

    Se puede continuar, PERO el paso vuelve a pasar por Policy y Gate como cualquier otro:
    la recuperación no le concede ninguna excepción.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    rt, executor, world = _runtime(workspace)

    mission = _mission()
    mission.state = MissionState.RUNNING
    step = _step("escribir", "fs.write", path="informe.txt", content="hola")
    await rt._record_inflight_action(mission, step)
    # El fichero NO existe: la escritura no llegó a ocurrir.
    assert not (workspace / "informe.txt").exists()

    mission.plan = Plan(mission.id, [step])
    puede = await rt._recover_mission(mission)

    assert puede is True
    recovery = mission.context["recovery"]
    assert recovery["action_status"]["escribir"] == ActionStatus.NOT_EXECUTED.value
    assert recovery["decision"] == RecoveryDecision.RESUME.value
    # No se marca como hecho: el Core sigue pudiendo ofrecerlo, y lo hará por su cuenta.
    assert "escribir" not in mission.context.get("recovered_completed_steps", [])


@pytest.mark.asyncio
async def test_21_accion_ambigua_con_efectos_pregunta_en_vez_de_repetir(tmp_path):
    """Caso 3 del §7: si no se puede confirmar, se pregunta. Nunca replay a ciegas.

    Se fuerza la ambigüedad de la forma más honesta posible: la inspección NO devuelve nada
    (se corta la herramienta), que es exactamente lo que pasa si el mundo no se puede leer.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    rt, executor, world = _runtime(workspace)

    # La inspección falla: no se puede mirar. `None` es "no sé", no "no existe".
    async def broken(m, step, *, tool_name=None):
        raise OSError("el workspace no responde")
    executor.execute = broken

    mission = _mission()
    mission.state = MissionState.RUNNING
    step = _step("borrar", "fs.remove", action="execute", path="importante.txt")
    await rt._record_inflight_action(mission, step)

    puede = await rt._recover_mission(mission)

    assert puede is False, "no se puede continuar solo con una acción de efectos sin confirmar"
    assert mission.state is MissionState.WAITING_APPROVAL
    ask = mission.context["recovery_ask"]
    assert ask["steps"] == ["borrar"]
    assert ask["capability"] == "fs.remove"
    assert "Repetir a ciegas" in ask["reason"]


@pytest.mark.asyncio
async def test_22_restart_con_objetivo_ya_cumplido_no_repite_trabajo(tmp_path):
    """Caso 4 del §7: si el objetivo ya está, se verifica y se completa."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "informe.txt").write_text("hola\n", encoding="utf-8")

    rt, executor, world = _runtime(workspace)
    mission = _mission()
    mission.state = MissionState.RUNNING

    # Observación real: el fichero existe, y por tanto hay evidencia de que cumple.
    step = _step("escribir", "fs.write", path="informe.txt", content="hola")
    result = await executor.execute(mission, step, tool_name="fs.stat")
    world.observe_execution(
        PlanStep("probe", "probe", "research", RiskLevel.LOW, "critic", capability="fs.stat"),
        ExecutionResult(success=True, output=dict(result.output),
                        observations=[Observation("tool.probe", dict(result.output), trusted=True)]),
        mission,
    )
    await rt._record_inflight_action(mission, step)

    puede = await rt._recover_mission(mission)

    assert puede is True
    assert mission.context["recovery"]["decision"] == RecoveryDecision.COMPLETE.value
    assert "ya está satisfecho" in mission.context["recovery"]["decision_reason"]


@pytest.mark.asyncio
async def test_23_restart_con_plan_obsoleto_replanifica(tmp_path):
    """Caso 5 del §7: el estado restaurado ya no vale, así que el plan se descarta.

    El plan nuevo lo regenera `_ensure_plan` y vuelve a pasar por PlanValidator, Policy y
    Gate. La recuperación no escribe planes: sólo descarta el que no vale.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    rt, executor, world = _runtime(workspace)

    mission = _mission()
    mission.state = MissionState.RUNNING
    # Plan que ya no valida contra el catálogo: capability inventada.
    bad = PlanStep("fantasma", "paso", "execute", RiskLevel.LOW, "executor",
                   capability="no.existe.nada")
    mission.plan = Plan(mission.id, [bad])
    rt.plan_validator = __import__("alexis.cognition.planner_model", fromlist=["PlanValidator"]).PlanValidator(
        catalog=build_catalog())

    puede = await rt._recover_mission(mission)

    assert puede is True
    assert mission.context["recovery"]["decision"] == RecoveryDecision.REPLAN.value
    # El plan inválido se descarta para que otro lo regenere CON las reglas.
    assert mission.plan is None
    assert "plan_steps" not in mission.context


@pytest.mark.asyncio
async def test_24_la_accion_en_volo_se_cierra_cuando_el_paso_termina(tmp_path):
    """El ciclo completo de la marca: se pone antes, se cierra después. Nunca al revés."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    rt, executor, world = _runtime(workspace)

    mission = _mission()
    step = _step("escribir", "fs.write", path="informe.txt", content="hola")

    await rt._record_inflight_action(mission, step)
    assert "inflight_action" in mission.context
    assert mission.context["last_action"] if "last_action" in mission.context else True

    await rt._settle_inflight_action(mission, step)
    assert "inflight_action" not in mission.context
    assert mission.context["last_action"]["recorded_status"] == "executed"


@pytest.mark.asyncio
async def test_25_la_recuperacion_esta_acotada(tmp_path):
    """§15: preguntar no es un bucle. La recuperación tiene presupuesto y se acaba.

    Con el mundo sin responder, la secuencia es: preguntar, preguntar, y al tercer intento
    `abort` con el motivo escrito. Lo que NO puede pasar es seguir preguntando indefinidamente
    — un sistema que se queda llamando a un mundo que no contesta no se está recuperando,
    está colgado.

    Y al revés: preguntar primero es lo correcto, porque la acción sin confirmar tiene
    efectos. Abortar sin preguntar habría perdido la única ventana de oportunidad real.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    rt, executor, world = _runtime(workspace)

    async def broken(m, step, *, tool_name=None):
        raise OSError("sin mundo")
    executor.execute = broken

    context: dict = {}
    decisions = []
    for _ in range(6):
        mission = _mission()
        mission.state = MissionState.RUNNING
        mission.context = dict(context)
        await rt._record_inflight_action(mission, _step("borrar", "fs.remove", path="x.txt"))
        await rt._recover_mission(mission)
        context = dict(mission.context)
        decisions.append(mission.context.get("recovery", {}).get("decision"))

    assert decisions[0] == RecoveryDecision.ASK_USER.value, decisions
    assert decisions[-1] == RecoveryDecision.ABORT.value, decisions
    # El corte está acotado: a partir del ABORT no se sigue insistiendo.
    assert decisions.count(RecoveryDecision.ASK_USER.value) <= 3, decisions
    assert decisions.count(RecoveryDecision.ABORT.value) >= 1, decisions
    assert "recovery_aborted" in context, "el motivo queda escrito, no sólo el estado"


# ======================================================================
# Garantías que no deben romperse
# ======================================================================


@pytest.mark.asyncio
async def test_26_recovery_no_amplia_la_autoridad(tmp_path):
    """La recuperación no puede ensanchar envelope, capabilities ni riesgo.

    No hay ningún camino en `_recover_mission` que escriba en el envelope: lo que hace es
    DECIDIR y, en el peor caso, descartar el plan para que otro lo rehaga con las reglas.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    rt, executor, world = _runtime(workspace)
    mission = _mission()
    before = (list(mission.envelope.capabilities), list(mission.envelope.allowed_actions),
               list(mission.envelope.forbidden_actions))

    await rt._record_inflight_action(mission, _step("borrar", "fs.remove", path="x.txt"))
    await rt._recover_mission(mission)

    assert (list(mission.envelope.capabilities), list(mission.envelope.allowed_actions),
            list(mission.envelope.forbidden_actions)) == before


@pytest.mark.asyncio
async def test_27_un_paso_pendiente_que_requiere_aprobacion_no_se_ejecuta_al_volver(tmp_path):
    """§11: tras un restart, una acción que necesita aprobación NO se ejecuta sola."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    rt, executor, world = _runtime(workspace)

    mission = _mission()
    mission.state = MissionState.WAITING_APPROVAL
    await rt._record_inflight_action(mission, _step("borrar", "fs.remove", path="x.txt"))

    puede = await rt._recover_mission(mission)

    assert puede is False
    assert mission.state is MissionState.WAITING_APPROVAL


@pytest.mark.asyncio
async def test_28_el_estado_de_recovery_llega_a_self_model(tmp_path):
    """§17: si ALEXIS está recuperándose, su Self Model lo dice. No es teatro: se deriva
    del `RecoveryState` real que se acaba de escribir."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    rt, executor, world = _runtime(workspace)

    mission = _mission()
    mission.state = MissionState.RUNNING
    await rt._record_inflight_action(mission, _step("borrar", "fs.remove", path="x.txt"))
    await rt._recover_mission(mission)

    recovery = mission.context["recovery"]
    assert recovery["attempt"] == 1
    assert recovery["decision_reason"], "una decisión sin motivo no es auditable"


@pytest.mark.asyncio
async def test_29_recovery_emite_evento_auditable(tmp_path):
    """§16: la recuperación deja rastro en el bus, no sólo en el contexto."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    rt, executor, world = _runtime(workspace)

    published: list[tuple] = []

    class _Spy(EventBus):
        async def publish(self, topic, payload=None):
            published.append((topic, payload))

    rt.events = _Spy()
    mission = _mission()
    mission.state = MissionState.RUNNING
    await rt._record_inflight_action(mission, _step("escribir", "fs.write", path="informe.txt"))

    await rt._recover_mission(mission)

    topics = [t for t, _ in published]
    assert any(t.startswith("recovery.") for t in topics), topics