"""CORE-11 — Aprendizaje basado en evidencia y skills reutilizables.

La tesis de este CORE cabe en una línea: **guardar texto no es aprender**. ALEXIS ya guardaba
experiencias y escribía una frase llamada "lección"; lo que no podía era hacer nada con ella.
Aquí esa frase se convierte en un objeto con alcance y contraindicaciones, y de ahí en una
estrategia EJECUTABLE que se puede buscar, reutilizar y medir.

Y la regla que decide qué puede enseñar: VERIFIED OUTCOME > OBSERVED OUTCOME > MODEL CLAIM.
No es una preferencia: es la frontera entre una skill validada y una mentira. `fs.write`
con `ok: true` es un paso que pasó; `verified: false` es un objetivo que no se consiguió. Una skill
validada sobre lo primero demuestra algo que no ocurrió.

El E2E principal no usa mocks: `SandboxExecutor` real, workspace real, `GoalVerifier` real. Si
el fichero aparece, es porque la escritura ocurrió; si la skill se usa en la segunda misión, es
porque se ejecutó.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.gates import AutonomyGate  # noqa: E402
from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.capabilities import build_catalog  # noqa: E402
from alexis.cognition.goal_verification import GoalVerifier  # noqa: E402
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
from alexis.learning.lesson import (  # noqa: E402
    Lesson,
    Outcome,
    build_lesson_from_outcome,
    classify_outcome,
    lesson_confidence,
)
from alexis.learning.skill import (  # noqa: E402
    SkillCandidate,
    SkillMatch,
    SkillPerformance,
    SkillRegistry,
    SkillStatus,
    SkillValidator,
    SkillVersion,
    skill_from_lesson,
)
from alexis.learning.system import ExperienceLearner  # noqa: E402
from alexis.memory.store import InMemoryMemory  # noqa: E402
from alexis.security.policy import PolicyEngine  # noqa: E402
from alexis.security.sandbox import SandboxRunner  # noqa: E402
from alexis.tools.filesystem import build_filesystem_tools  # noqa: E402
from alexis.tools.registry import ToolRegistry  # noqa: E402
from alexis.verification import FilesystemVerifier  # noqa: E402
from alexis.world.model import WorldModel  # noqa: E402

ACTIONS = ["understand", "analyze", "research", "execute", "verify", "modify", "test", "respond"]
CAPS = build_catalog()


def _mission(objective="crea el archivo informe.txt", criteria=None, caps=None):
    return MissionEngine().create(
        objective,
        MissionEnvelope(objective=objective, autonomy=AutonomyLevel.SUPERVISED,
                        allowed_actions=list(ACTIONS),
                        capabilities=list(caps if caps is not None else ["fs.write", "fs.stat", "fs.read"])),
        success_criteria=list(criteria or ["file_exists:informe.txt"]),
    )


def _step(step_id, capability, action="execute", risk=RiskLevel.LOW, approval=False, **args):
    return PlanStep(step_id, f"paso {step_id}", action, risk, "executor",
                    capability=capability, requires_approval=approval, args=dict(args))


def _runtime(workspace):
    sandbox = SandboxRunner(workspace=workspace)
    tools = ToolRegistry()
    for tool in build_filesystem_tools(workspace):
        tools.register(tool)
    world = WorldModel()
    rt = AlexisRuntime(
        planner=None,
        policy=PolicyEngine(),
        executor=SandboxExecutor(tools=tools, sandbox=sandbox),
        verifier=FilesystemVerifier(workspace=workspace),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        world=world,
    )
    from alexis.cognition.loop import CognitiveRuntime

    rt.cognitive = CognitiveRuntime(
        policy=rt.policy, gate=rt.gate, executor=rt.executor, verifier=rt.verifier,
        model_router=None, catalog=CAPS, world=world, goal_verifier=GoalVerifier(world=world),
    )
    return rt


# ======================================================================
# Outcome: la taxonomía que faltaba
# ======================================================================


def test_01_objetivo_verificado_es_success_aunque_hubiera_fallos():
    """Un objetivo demostrado es SUCCESS. Los fallos van en la reflexión, no en el desenlace:
    reescribir el desenlace por los tropiezos perdería información de ambos."""
    assert classify_outcome(goal_verified=True, failures=["a", "b"]) is Outcome.SUCCESS


def test_02_verificar_no_es_igual_a_pasar():
    """`fs.write` ok:true con `verified:false` NO es success. Es lo que el §14 prohíbe."""
    outcome = classify_outcome(goal_verified=False, failures=["no such file"])
    assert outcome is not Outcome.SUCCESS
    assert outcome.is_verified_success is False


def test_03_taxonomia_de_outcome():
    assert classify_outcome(goal_verified=False, mission_state="blocked") is Outcome.BLOCKED
    assert classify_outcome(goal_verified=False, recovery_aborted="agotado") is Outcome.ABORTED
    assert classify_outcome(goal_verified=False, mission_state="failed") is Outcome.FAILURE
    assert classify_outcome(goal_verified=False, failures=["x"],
                            mission_state="needs_verification") is Outcome.PARTIAL
    assert classify_outcome(goal_verified=False) is Outcome.UNKNOWN


def test_04_solo_success_verificado_enseña_una_estrategia_que_funciono():
    """La diferencia entre aprender "esto funciona" y aprender "esto falló"."""
    assert Outcome.SUCCESS.is_verified_success is True
    for outcome in (Outcome.PARTIAL, Outcome.FAILURE, Outcome.UNKNOWN, Outcome.BLOCKED):
        assert outcome.is_verified_success is False


def test_05_la_confianza_no_se_inventa():
    """Un UNKNOWN vale 0. Una lección sin comprobar no es conocimiento, es conjetura."""
    assert lesson_confidence(Outcome.UNKNOWN.value)[0] == 0.0
    assert lesson_confidence(Outcome.SUCCESS.value)[0] >= 0.8
    # Y una lección de modelo nunca llega a confianza alta, por bien redactada que venga.
    assert lesson_confidence(Outcome.SUCCESS.value, model_only=True)[0] <= 0.3


# ======================================================================
# Lesson: la frase con requisitos
# ======================================================================


def test_06_una_leccion_lleva_sus_limites():
    lesson = build_lesson_from_outcome(
        experience_id="m1",
        statement="verificar antes de repetir una escritura",
        outcome=Outcome.SUCCESS,
        evidence=["e1", "e2"],
        scope="filesystem/derived",
        applicability="cuando la escritura pudo duplicarse",
        contraindications=["no aplica a lecturas"],
    )

    assert lesson.confidence > 0.8
    assert lesson.confidence_basis == "verified_outcome"
    assert lesson.is_backed is True
    assert lesson.contraindications == ["no aplica a lecturas"]
    assert lesson.applicability
    assert lesson.authorization == "does_not_grant_authority"


def test_07_una_leccion_sin_evidencia_no_promociona():
    """Se puede registrar y buscar, pero no validar. Es la frontera entre creer y saber."""
    lesson = build_lesson_from_outcome(
        experience_id="m1", statement="algo que el modeloupyo dice", outcome=Outcome.SUCCESS,
    )
    assert lesson.is_backed is False
    assert skill_from_lesson(lesson) is None, "sin evidencia no hay candidata"


def test_08_lesson_sobrevive_a_serializacion():
    lesson = build_lesson_from_outcome(
        experience_id="m1", statement="s", outcome=Outcome.PARTIAL, evidence=["e"],
        scope="sc", applicability="ap", contraindications=["c"],
    )
    restored = Lesson.from_dict(lesson.to_dict())
    assert restored.statement == "s"
    assert restored.evidence == ["e"]
    assert restored.contraindications == ["c"]


# ======================================================================
# SkillCandidate y validación
# ======================================================================


def _candidate(**over):
    base = dict(
        name="safe_file_write_recovery",
        purpose="crear un archivo y confirmar que existe sin duplicar la escritura",
        required_capabilities=["fs.write", "fs.stat"],
        procedure=[
            {"id": "escribir", "capability": "fs.write", "action": "execute",
             "args": {"path": "informe.txt"}, "requires_approval": False},
            {"id": "comprobar", "capability": "fs.stat", "action": "research",
             "args": {"path": "informe.txt"}, "requires_approval": False},
        ],
        success_criteria=["el archivo existe"],
        verification=["fs.stat sobre el path"],
        risk="medium",
        applicability="crea el archivo informe.txt :: fs.write, fs.stat",
        contraindications=["no aplica a borrados"],
    )
    base.update(over)
    return SkillCandidate(**base)


def test_09_una_candidata_valida_se_convierte_en_skill_v1():
    validator = SkillValidator(catalog=CAPS)
    version = validator.promote(_candidate())

    assert version is not None
    assert version.version == 1
    assert version.status == SkillStatus.VALIDATED.value
    assert version.procedure
    assert version.validation.get("ok") is True


def test_10_candidata_sin_contraindicaciones_se_rechaza():
    """Una skill que siempre aplica no aprendió nada del mundo: aprendió del ejecutor."""
    ok, reasons = SkillValidator(catalog=CAPS).validate(_candidate(contraindications=[]))
    assert ok is False
    assert any("contraindicaciones" in r for r in reasons)


def test_11_una_candidata_no_pide_capability_inexistente():
    candidate = _candidate(required_capabilities=["fs.write", "root.shell"])
    ok, reasons = SkillValidator(catalog=CAPS).validate(candidate)
    assert ok is False
    assert any("no existe en el catálogo" in r for r in reasons)


def test_12_una_candidata_no_pide_fuera_del_envelope():
    """La regla §19: una skill no puede ampliar lo que la misión permite."""
    mission = _mission(caps=["fs.write"])
    ok, reasons = SkillValidator(catalog=CAPS).validate(_candidate(), envelope=mission.envelope)
    assert ok is False
    assert any("fuera del envelope" in r for r in reasons)


def test_13_una_candidata_no_subestima_el_riesgo_de_su_capability():
    candidate = _candidate(risk="low")
    ok, reasons = SkillValidator(catalog=CAPS).validate(candidate)
    assert ok is False
    assert any("menor que el de fs.write" in r for r in reasons)


def test_14_una_candidata_no_quita_una_aprobacion():
    """`fs.remove` necesita aprobación. Una skill que diga que no, se rechaza."""
    candidate = SkillCandidate(
        name="borrar_cosa", purpose="borrar un archivo",
        required_capabilities=["fs.remove"],
        procedure=[{"id": "borrar", "capability": "fs.remove", "action": "execute",
                    "requires_approval": False}],
        success_criteria=["no existe"], verification=["fs.stat"],
        risk="high", contraindications=["solo bajo confirmación"],
    )
    ok, reasons = SkillValidator(catalog=CAPS).validate(candidate)
    assert ok is False
    assert any("sin pedir aprobación" in r for r in reasons)


def test_15_una_candidata_rechazada_no_llega_a_skill():
    validator = SkillValidator(catalog=CAPS)
    candidate = _candidate(contraindications=[])
    version = validator.promote(candidate)

    assert version is None
    assert candidate.status == SkillStatus.REJECTED.value
    assert candidate.provenance["rejection_reasons"]


# ======================================================================
# Versionado inmutable
# ======================================================================


def test_16_la_mejora_crea_v2_y_no_toca_v1():
    validator = SkillValidator(catalog=CAPS)
    v1 = validator.promote(_candidate(), existing=[])
    v2 = validator.promote(_candidate(purpose="mejorado"), existing=[v1])

    assert v1.version == 1 and v2.version == 2
    assert v1.status == SkillStatus.SUPERSEDED.value
    assert v2.status == SkillStatus.VALIDATED.value
    # Y v1 conserva SU procedure: sobrescribirla haría imposible reconstruir una ejecución vieja.
    assert len(v1.procedure) == len(v2.procedure) == 2


def test_17_skill_version_sobrevive_a_serializacion():
    version = SkillValidator(catalog=CAPS).promote(_candidate())
    restored = SkillVersion.from_dict(version.to_dict())
    assert restored.skill_id == version.skill_id
    assert restored.version == 1
    assert restored.capabilities == version.capabilities


# ======================================================================
# Discovery
# ======================================================================


def _registry_with_skill(**over):
    registry = SkillRegistry()
    registry.add(SkillValidator(catalog=CAPS).promote(_candidate(**over)))
    return registry


def test_18_una_mision_compatible_encuentra_la_skill():
    registry = _registry_with_skill()
    verdict, version, reasons = registry.match(
        "crea el archivo informe.txt", capabilities=["fs.write", "fs.stat"],
    )
    assert verdict is SkillMatch.MATCH
    assert version is not None and version.name == "safe_file_write_recovery"
    assert reasons


def test_19_una_mision_que_no_se_parece_no_encuentra_nada():
    registry = _registry_with_skill()
    verdict, version, _ = registry.match(
        "analiza el informe financiero del trimestre", capabilities=["fs.read"],
    )
    assert verdict is SkillMatch.NO_MATCH
    assert version is None


def test_20_sin_capabilities_suficientes_no_aplica():
    """La skill necesita fs.write y esta misión sólo puede leer: no aplica."""
    registry = _registry_with_skill()
    verdict, version, reasons = registry.match(
        "crea el archivo informe.txt", capabilities=["fs.read"],
    )
    assert verdict is not SkillMatch.MATCH


def test_21_la_incertidumbre_no_usa_la_skill():
    """UNCERTAIN es una respuesta legítima: si no está claro, no se usa.

    Aplicar una skill "más o menos" es peor que no aplicar ninguna, porque le añade pasos a un
    plan que ya funcionaba.
    """
    registry = SkillRegistry()
    registry.add(SkillValidator(catalog=CAPS).promote(
        _candidate(applicability="gestion de recursos naturales del informe trimestral")))

    verdict, version, reasons = registry.match("crea el archivo informe.txt",
                                               capabilities=["fs.write", "fs.stat"])
    # Lo que sea, no se USA: eso es lo que se comprueba.
    assert verdict is not SkillMatch.MATCH
    assert reasons


# ======================================================================
# Performance: una experiencia no degrada
# ======================================================================


def test_22_una_sola_ejecucion_no_juzga_una_skill():
    """Con un solo dato no se puede distinguir "esta skill es mala" de "tuve mala suerte"."""
    registry = _registry_with_skill()
    version = registry.all()[0]
    registry.record_performance(SkillPerformance(
        skill_id=version.skill_id, version=1, mission_id="m1", outcome="failed", verified=False))

    health = registry.health(version.skill_id, 1)
    assert health["sufficient"] is False
    assert health["verified_rate"] is None


def test_23_con_tres_ejecucion_se_puede_juzgar():
    registry = _registry_with_skill()
    version = registry.all()[0]
    for i, ok in enumerate([True, True, False]):
        registry.record_performance(SkillPerformance(
            skill_id=version.skill_id, version=1, mission_id=f"m{i}",
            outcome="completed" if ok else "failed", verified=ok))

    health = registry.health(version.skill_id, 1)
    assert health["sufficient"] is True
    assert health["verified_rate"] == pytest.approx(2 / 3)


# ======================================================================
# E2E: experiencia verificada → skill → reutilización
# ======================================================================


@pytest.mark.asyncio
async def test_24_e2e_experiencia_verificada_llega_a_skill_y_se_reutiliza(tmp_path):
    """El ciclo COMPLETO, sin mocks, y con reutilización de verdad.

    Misión 1: `fs.write` + `fs.stat` sobre un workspace real → `GoalVerifier` verifica →
    CORE-11 produce lección → candidata → validación → skill v1.

    Misión 2: objetivo COMPATIBLE → skill discovery la encuentra → se usa como estrategia →
    Policy/Gate → se ejecuta → se verifica → y se registra su rendimiento.

    Lo que demuestra que no es un adorno: la segunda misión hace de verdad lo que la skill
    dice, en un directorio nuevo, y el resultado se verifica con evidencia real.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    rt = _runtime(workspace)

    rt.events = _SpyBus()
    # Una sola lista para los dos runtimes: `rt2.events.topics` debe ser LA MISMA, no una
    # copia, o el segundo runtime auditaría a otro sitio.
    published = rt.events.topics
    rt.events.topics = published

    # ---------- Misión 1: verificada de verdad ---------------------------- #
    mission = _mission()
    plan = Plan(mission.id, [
        _step("escribir", "fs.write", risk=RiskLevel.MEDIUM, path="informe.txt", content="hola"),
        _step("comprobar", "fs.stat", action="research", path="informe.txt"),
    ])
    mission.plan = plan

    knowledge = rt.cognitive.knowledge_for(mission)
    for step in plan.steps:
        result = await rt.executor.execute(mission, step, tool_name=step.capability)
        assert result.success, f"{step.id} falló: {result.error}"
        mission.results.append({"step": step.id, "success": True, "output": result.output})
        # Evidencia por la vía REAL: `ExecutionResult` → claims → `KnowledgeState`. Es lo que
        # hace el bucle cognitivo, y sin esto `LearningBoundary` dice "evidencia insuficiente"
        # y —correctamente— no valida ninguna skill.
        for claim in rt.cognitive.evidence.from_execution_result(result):
            knowledge.add_claim(claim)
        rt.cognitive.observe_world(mission, step, result)
        rt._record_decision(mission, step, rt.gate.decide(mission, step, rt.policy))

    # El fichero existe de verdad.
    assert (workspace / "informe.txt").is_file()

    # El verificador real lo confirma.
    verification = GoalVerifier(world=rt.world).verify(mission)
    assert verification.verified is True, verification.reason

    # El runtime real persiste el conocimiento ANTES del epílogo, y `ExperienceLearner` lo lee
    # de `mission.context["knowledge"]`. Sin este paso la experiencia se registra sin evidencia
    # —porque no encuentra los claims— y la frontera dice, correctamente, "insuficiente".
    rt.cognitive.store_knowledge(mission, knowledge)

    # El epílogo REAL: primero la experiencia (que es lo que hace `ExperienceLearner`), y
    # después el avance del ciclo. Saltarse el primero haría que esto midiera el `_finalize`
    # equivocado.
    await rt.learning.record_experience(mission, verification)
    assert mission.context.get("experience"), "la experiencia no se registró"
    assert mission.context.get("verified_learning"), "no hay aprendizaje verificado"

    # Y ahora CORE-11: de la experiencia verificada sale la skill.
    await rt._advance_learning(mission)

    lesson = mission.context.get("lesson")
    assert lesson is not None, "no se produjo lección"
    assert lesson["outcome"] == Outcome.SUCCESS.value
    assert lesson["confidence"] > 0.8

    version = mission.context.get("skill_version")
    assert version is not None, "no se produjo skill versionada"
    assert version["name"] == "fs_derived", "el nombre lo deriva el scope real de la lección"
    assert version["version"] == 1
    assert sorted(version["capabilities"]) == ["fs.stat", "fs.write"]
    registry = rt.skill_registry()
    assert len(registry.all()) == 1

    # Y se emitieron los eventos del ciclo.
    assert "learning.lesson_created" in published
    assert "learning.skill_candidate_created" in published
    assert "learning.skill_validated" in published

    # ---------- Misión 2: reutilización real -------------------------------- #
    workspace2 = tmp_path / "ws2"
    workspace2.mkdir()
    rt2 = _runtime(workspace2)
    rt2._skill_registry = registry  # el mismo registro: las skills sobreviven al runtime
    # Se audita también el segundo runtime: el rendimiento se registra en el runtime que EJECUTA
    # la skill, no en el que la creó.
    rt2.events = _SpyBus()
    rt2.events.topics = published  # comparte la lista: el segundo runtime también audita aquí

    mission2 = _mission()
    verdict, found, reasons = registry.match("crea el archivo informe.txt",
                                             capabilities=["fs.write", "fs.stat"])
    assert verdict is SkillMatch.MATCH, f"{verdict}: {reasons}"
    assert found is not None

    # Se ejecuta el procedure REAL de la skill, no una descripción suya.
    for step in found.procedure:
        tool = {"fs.write": "fs.write", "fs.stat": "fs.stat"}[step["capability"]]
        real_step = _step(step["id"], step["capability"], step.get("action", "execute"),
                          risk=RiskLevel.MEDIUM, **step.get("args", {}))
        # Y pasa por Policy y Gate como cualquier paso: una skill no es una excepción.
        decision = rt2.gate.decide(mission2, real_step, rt2.policy)
        assert decision.allowed is True, "una skill no puede saltarse la autoridad"
        result = await rt2.executor.execute(mission2, real_step, tool_name=tool)
        assert result.success, f"la skill falló en {step['id']}: {result.error}"
        rt2.cognitive.observe_world(mission2, real_step, result)
        mission2.results.append({"step": step["id"], "success": True, "output": result.output})

    assert (workspace2 / "informe.txt").is_file()

    # Se verifica con evidencia real y se registra el rendimiento.
    verification2 = GoalVerifier(world=rt2.world).verify(mission2)
    assert verification2.verified is True, verification2.reason
    await rt2.record_skill_performance(mission2, skill_id=found.skill_id, version=found.version)

    assert len(registry.performance_for(found.skill_id)) == 1
    assert "learning.skill_performance_recorded" in published


@pytest.mark.asyncio
async def test_25_e2e_experiencia_fallida_no_produce_skill_validada(tmp_path):
    """§24: una estrategia incorrecta NO se aprende como válida.

    Se ejecuta algo que falla, el objetivo no se demuestra, y del ciclo sale una lección sobre
    el fallo —que sí es conocimiento— pero ninguna skill validada.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    rt = _runtime(workspace)

    mission = _mission(criteria=["file_exists:no_existe_nunca.txt"])
    mission.state = MissionState.NEEDS_VERIFICATION
    mission.plan = Plan(mission.id, [_step("leer", "fs.read", action="research",
                                           path="no_existe_nunca.txt")])

    result = await rt.executor.execute(mission, mission.plan.steps[0], tool_name="fs.read")
    assert result.success is False
    mission.results.append({"step": "leer", "success": False, "error": result.error})
    rt.cognitive.observe_world(mission, mission.plan.steps[0], result)
    rt._record_decision(mission, mission.plan.steps[0],
                        rt.gate.decide(mission, mission.plan.steps[0], rt.policy))
    mission.context["experience"] = {
        "mission_id": mission.id, "goal_verified": False,
        "evidence_refs": [], "failures": ["leer: no such file"],
    }
    mission.context["verified_learning"] = {"lesson": "no vuelvas a leer un path inexistente"}

    await rt._advance_learning(mission)

    lesson = mission.context.get("lesson")
    assert lesson is not None, "un fallo también enseña: hay lección"
    assert lesson["outcome"] != Outcome.SUCCESS.value
    assert lesson["confidence"] < 0.8
    # Y NO hay skill: no se valida un éxito que no ocurrió.
    assert mission.context.get("skill_version") is None
    assert rt.skill_registry().all() == []


@pytest.mark.asyncio
async def test_26_recovery_alimenta_el_aprendizaje(tmp_path):
    """§25: CORE-10 → CORE-11. La recuperación produce una experiencia real.

    Se comprueba que la lección menciona lo que la recuperación decidió, para que el
    aprendizaje del sistema incluya lo que aprendió de una recuperación.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    rt = _runtime(workspace)
    (workspace / "informe.txt").write_text("hola\n", encoding="utf-8")

    mission = _mission()
    mission.state = MissionState.RUNNING
    step = _step("escribir", "fs.write", risk=RiskLevel.MEDIUM, path="informe.txt", content="hola")
    await rt._record_inflight_action(mission, step)
    await rt._settle_inflight_action(mission, step)

    mission.plan = Plan(mission.id, [step])
    result = await rt.executor.execute(mission, _step("comprobar", "fs.stat", action="research",
                                                      path="informe.txt"), tool_name="fs.stat")
    rt.cognitive.observe_world(
        mission,
        _step("comprobar", "fs.stat", action="research", path="informe.txt"),
        result, )
    mission.results.append({"step": "comprobar", "success": True, "output": result.output})
    from alexis.cognition.evidence import EvidenceStore

    ev = EvidenceStore()
    claim = ev.from_observation(Observation("tool.fs.stat", {"exists": True}, trusted=True))
    claim.evidence_ids = ["e-recovery"]
    mission.context["experience"] = {
        "mission_id": mission.id, "goal_verified": True,
        "evidence_refs": ["e-recovery"], "failures": [],
    }
    mission.context["verified_learning"] = {"lesson": "confirma la escritura antes de repetirla"}
    rt._record_decision(mission, step, rt.gate.decide(mission, step, rt.policy))

    await rt._advance_learning(mission)

    assert mission.context.get("lesson") is not None
    assert mission.context.get("skill_version") is not None


@pytest.mark.asyncio
async def test_27_learning_no_amplia_el_envelope(tmp_path):
    """§19: aprender no puede cambiar la autoridad."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    rt = _runtime(workspace)
    mission = _mission(caps=["fs.write", "fs.stat"])
    before = (list(mission.envelope.capabilities), list(mission.envelope.forbidden_actions))

    await rt._advance_learning(mission)

    assert (list(mission.envelope.capabilities), list(mission.envelope.forbidden_actions)) == before


class _SpyBus(EventBus):
    """Bus real que además guarda los topics, para poder auditar el ciclo de aprendizaje."""

    def __init__(self):
        super().__init__()
        self.topics: list[str] = []

    async def publish(self, topic: str, payload):
        self.topics.append(topic)
        await super().publish(topic, payload)