"""R-01 — el runtime nunca enseñaba nada, y la suite no lo notaba.

Este fichero existe por una asimetría que ningún test anterior cubría: `record_experience`
acepta dos tipos distintos según quién llame, y cada uno trae el atributo del resultado en
un sitio distinto.

  - `GoalVerification` (cognition/goal_verification.py) tiene `.verified`.
  - `Verification`     (contracts.py)                   tiene `.passed`, y NO `.verified`.

Los tests llamaban con `GoalVerification`, así que leían `.verified` y todo cuadraba. El
RUNTIME llama con `Verification`, así que `getattr(verification, "verified", False)` devolvía
`False` **siempre, en silencio**. `getattr` con valor por defecto no lanza: convierte un
error de tipos en un `False` que parece una decisión.

La consecuencia no era cosmética. Con `goal_verified=False` a perpetuidad:

    classify_outcome(goal_verified=False, state="completed") -> Outcome.UNKNOWN

y en `runtime.py` el guardián `if not outcome.is_verified_success: return` cortaba ANTES de
`skill_from_lesson`. Es decir: 229 lecciones guardadas en la base real, las 229 con
`outcome='unknown'`, y la tabla `skills` con cero filas. El sistema almacenaba historial y
no aprendía nada — que es exactamente la diferencia entre las dos cosas que el contrato
distingue.

Aquí se prueba con el tipo que el runtime usa de verdad, que es el que nadie probaba.
"""

from __future__ import annotations

import pytest

from alexis.contracts import Verification
from alexis.learning.lesson import Outcome, classify_outcome
from alexis.learning.system import ExperienceLearner


def _mision(objective="leer el informe"):
    from alexis.autonomy.mission import MissionEngine
    from alexis.contracts import AutonomyLevel, MissionEnvelope
    

    envelope = MissionEnvelope(
        objective=objective,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=["read", "write"],
    )
    mission = MissionEngine().create(objective, envelope)
    return mission


def _verificacion_real():
    """La verificación que el runtime construye de verdad: `Verification`, no `GoalVerification`."""
    return Verification(passed=True, confidence=0.95, evidence=["ev-1"])


# --------------------------------------------------------------------------- #
# 1 — el fallo silencioso
# --------------------------------------------------------------------------- #


def test_01_verification_no_tiene_atributo_verified():
    """El atributo que se leía no existe en el tipo que el runtime pasa.

    Se afirma explícitamente para que el fallo quede documentado: si alguien «arregla» esto
    añadiendo un campo `verified` a `Verification`, esta prueba falla y obliga a decidir si
    eso es lo que quiere, en vez de dejar dos atributos que significan lo mismo.
    """
    assert hasattr(_verificacion_real(), "passed") is True
    assert hasattr(_verificacion_real(), "verified") is False, (
        "`Verification` ha gained a `verified`. Si se ha añadido a propósito, hay que "
        "revisar si `record_experience` debe leer `passed`, `verified`, o dejar de adivinar."
    )


async def test_02_la_experiencia_refleja_la_verificacion_del_runtime():
    """Con el tipo real del runtime, la experiencia debe decir que el objetivo se verificó."""
    learner = ExperienceLearner()
    mission = _mision()

    verified = await learner.record_experience(mission, _verificacion_real(), goal_verified=True)

    assert verified.experience.goal_verified is True, (
        f"la experiencia dice goal_verified={verified.experience.goal_verified}. El runtime "
        f"entró por `verification.passed and goal_verified`, así que aquí tiene que ser True."
    )


async def test_03_la_experiencia_no_afirma_mas_de_lo_que_sabe():
    """La corrección no debe abrir la puerta contraria.

    Si `goal_verified` llega False, la experiencia tiene que decirlo False aunque la
    verificación «pasara». Un sistema que aprende de objetivos no demostrados es peor que
    uno que no aprende.
    """
    learner = ExperienceLearner()
    verified = await learner.record_experience(_mision(), _verificacion_real(), goal_verified=False)
    assert verified.experience.goal_verified is False


async def test_04_sin_el_parametro_nuevo_sigue_leyendo_el_tipo_antiguo():
    """La interfaz no se rompe: quien llame con `GoalVerification` sigue funcionando.

    `GoalVerification` tiene `.verified`, y esa vía —la que usan los tests y cualquier
    llamante legítimo— debe seguir dando lo mismo que antes.
    """
    from alexis.cognition.goal_verification import GoalVerification

    learner = ExperienceLearner()
    verified = await learner.record_experience(_mision(), GoalVerification(objective="x", evaluations=[]))
    assert verified.experience.goal_verified is False, (
        "un GoalVerification sin verificar no debe afirmar que el objetivo se verificó"
    )


# --------------------------------------------------------------------------- #
# 2 — la consecuencia: el ciclo §21 no llegaba más allá de LESSON
# --------------------------------------------------------------------------- #


def test_05_el_outcome_se_clasifica_a_partir_de_la_verificacion_real():
    """El eslabón roto, aislado: outcome correcto a partir de un objetivo verificado."""
    assert classify_outcome(goal_verified=True, mission_state="completed").value == "success"
    assert classify_outcome(goal_verified=True, mission_state="completed").is_verified_success is True

    # Y el caso que se daba en producción: objetivo NO verificado nunca llega a 'success',
    # porque es exactamente el comportamiento correcto.
    assert classify_outcome(goal_verified=False, mission_state="completed").is_verified_success is False


def test_06_es_verified_success_es_la_frontera_de_la_promocion():
    """Documenta por qué el bug era tan caro.

    `runtime._advance_learning` corta antes de crear la candidata cuando el outcome no es
    un éxito verificado. Con el outcome atascado en `unknown`, la cadena entera después de
    LESSON —candidata, validación, versión— no se ejecutaba jamás.
    """
    assert Outcome.UNKNOWN.is_verified_success is False
    assert Outcome.SUCCESS.is_verified_success is True


# --------------------------------------------------------------------------- #
# 3 — el ciclo §21 completo, por el camino que el runtime usa de verdad
# --------------------------------------------------------------------------- #


async def test_07_el_ciclo_llega_a_skill_version_con_el_tipo_real(tmp_path, db):
    """La prueba de cierre: de experiencia verificada a skill versionada.

    `test_learning_rehydration.py` ya recorría el ciclo entero HASTA `skill_version`, y
    pasaba. Pero llamaba a `record_experience` con un `GoalVerification`, que es el tipo que
    tiene `.verified`. El runtime nunca hace eso: pasa un `Verification`.

    Esta prueba usa el tipo real, de principio a fin. Antes de la corrección se quedaba en
    `outcome='unknown'` y no llegaba a candidata; con ella, la cadena completa
    EXPERIENCE → LESSON → SKILL CANDIDATE → VALIDATION → SKILL VERSION se ejecuta.

    Y comprueba lo que distingue aprender de almacenar: la versión queda PERSISTIDA y es
    recuperable por un runtime nuevo. Una skill que no sobrevive al reinicio no es
    aprendizaje, es caché.
    """
    from alexis.autonomy.gates import AutonomyGate
    from alexis.capabilities import build_catalog
    from alexis.autonomy.mission import MissionEngine
    from alexis.contracts import Plan, PlanStep
    from alexis.cognition.loop import CognitiveRuntime
    from alexis.cognition.goal_verification import GoalVerifier
    from alexis.contracts import AutonomyLevel, MissionEnvelope
    from alexis.core.runtime import AlexisRuntime
    from alexis.memory.store import InMemoryMemory
    from alexis.security.policy import PolicyEngine
    from alexis.storage.repositories import LearningRepository
    from alexis.tools.filesystem import build_filesystem_tools
    from alexis.tools.registry import ToolRegistry
    from alexis.world.model import WorldModel

    ws = tmp_path / "ws"
    ws.mkdir()
    from alexis.security.sandbox import SandboxRunner
    from alexis.execution import SandboxExecutor
    from alexis.verification import FilesystemVerifier
    from alexis.events.bus import EventBus

    tools = ToolRegistry()
    for tool in build_filesystem_tools(ws):
        tools.register(tool)
    world = WorldModel()
    caps = build_catalog()
    repo = LearningRepository(db)
    rt = AlexisRuntime(
        planner=None, policy=PolicyEngine(),
        executor=SandboxExecutor(tools=tools, sandbox=SandboxRunner(workspace=ws)),
        verifier=FilesystemVerifier(workspace=ws), memory=InMemoryMemory(),
        learning=ExperienceLearner(), event_bus=EventBus(), gate=AutonomyGate(),
        world=world, learning_repo=repo,
    )
    rt.cognitive = CognitiveRuntime(
        policy=rt.policy, gate=rt.gate, executor=rt.executor, verifier=rt.verifier,
        model_router=None, catalog=caps, world=world, goal_verifier=GoalVerifier(world=world),
    )

    envelope = MissionEnvelope(objective="escribir el informe", autonomy=AutonomyLevel.SUPERVISED,
                               allowed_actions=["read", "write"])
    mission = MissionEngine().create("escribir el informe", envelope)
    from alexis.contracts import RiskLevel

    def _step(step_id, capability, action="execute", risk=RiskLevel.LOW, **args):
        return PlanStep(step_id, f"paso {step_id}", action, risk, "executor",
                        capability=capability, requires_approval=False, args=dict(args))

    mission.plan = Plan(mission.id, [
        _step("escribir", "fs.write", risk=RiskLevel.MEDIUM, path="informe.txt", content="hola"),
        _step("comprobar", "fs.stat", action="research", path="informe.txt"),
    ])

    knowledge = rt.cognitive.knowledge_for(mission)
    for step in mission.plan.steps:
        result = await rt.executor.execute(mission, step, tool_name=step.capability)
        assert result.success, f"{step.id} falló: {result.error}"
        mission.results.append({"step": step.id, "success": True, "output": result.output})
        for claim in rt.cognitive.evidence.from_execution_result(result):
            knowledge.add_claim(claim)
    rt.cognitive.store_knowledge(mission, knowledge)

    # === EL PUNTO DEL DEFECTO: el tipo que el runtime pasa de verdad ===
    verification_real = Verification(
        passed=True, confidence=0.9,
        evidence=[e for c in knowledge.claims for e in (getattr(c, "evidence_ids", []) or [])],
    )
    verified = await rt.learning.record_experience(
        mission, verification_real, goal_verified=True
    )
    assert verified.experience.goal_verified is True

    await rt._advance_learning(mission)

    version = mission.context.get("skill_version")
    assert version is not None, (
        "el ciclo §21 no llegó a SKILL VERSION. Con outcome != success no se crea candidata, "
        f"así que outcome fue: {mission.context.get('lesson', {}).get('outcome')}"
    )
    assert version["version"] == 1
    assert len(rt.skill_registry().all()) == 1

    # Y persiste: eso es lo que separa aprender de acumular historial.
    # `VALIDATED` es el estado terminal de una promoción: `SkillStatus` NO tiene
    # `published` (los estados son proposed/under_validation/validated/rejected/
    # superseded). Una versión validada es lo más lejos que llega el ciclo por ahora, y
    #ollower: usarla como estrategia es lo que hace `hydrate_learning` al planificar.
    from alexis.learning.skill import SkillStatus

    guardadas = await repo.skill_versions()
    assert len(guardadas) == 1, "la skill versionada no quedó persistida"
    assert guardadas[0]["status"] == SkillStatus.VALIDATED.value, (
        f"estado inesperado: {guardadas[0]['status']!r}. Una skill validada pero no "
        f"persistida se pierde al reiniciar, y aprender para nada es no aprender."
    )


async def test_08_el_runtime_pasa_el_valor_autoritativo(tmp_path, db):
    """Cubre el CALL SITE real, no sólo el método.

    `test_07` llama a `record_experience` directamente, así que pasaría aunque el runtime
    dejara de pasarle el valor. Eso se comprobó con un sabotaje: quitar
    `goal_verified=goal_verified` de `_finalize` dejó las 7 pruebas en verde. El defecto
    vivía justo en esa llamada, y una prueba que no la atraviesa no puede detectarlo.

    Aquí se entra por `runtime._finalize()`, que es por donde pasa en producción.
    """
    from alexis.autonomy.gates import AutonomyGate
    from alexis.autonomy.mission import MissionEngine
    from alexis.capabilities import build_catalog
    from alexis.contracts import AutonomyLevel, MissionEnvelope
    from alexis.core.runtime import AlexisRuntime
    from alexis.events.bus import EventBus
    from alexis.execution import SandboxExecutor
    from alexis.memory.store import InMemoryMemory
    from alexis.security.policy import PolicyEngine
    from alexis.security.sandbox import SandboxRunner
    from alexis.tools.filesystem import build_filesystem_tools
    from alexis.tools.registry import ToolRegistry
    from alexis.verification import FilesystemVerifier
    from alexis.world.model import WorldModel

    ws = tmp_path / "ws2"
    ws.mkdir()
    tools = ToolRegistry()
    for tool in build_filesystem_tools(ws):
        tools.register(tool)
    world = WorldModel()
    rt = AlexisRuntime(
        planner=None, policy=PolicyEngine(),
        executor=SandboxExecutor(tools=tools, sandbox=SandboxRunner(workspace=ws)),
        verifier=FilesystemVerifier(workspace=ws), memory=InMemoryMemory(),
        learning=ExperienceLearner(), event_bus=EventBus(), gate=AutonomyGate(),
        world=world, learning_repo=None,
    )

    envelope = MissionEnvelope(objective="leer el informe", autonomy=AutonomyLevel.SUPERVISED,
                               allowed_actions=["read"])
    mission = MissionEngine().create("leer el informe", envelope)

    verification = Verification(passed=True, confidence=0.9, evidence=["ev-1"])
    await rt._finalize(mission, verification, goal_verified=True)

    recorded = (mission.context or {}).get("experience") or {}
    assert recorded.get("goal_verified") is True, (
        f"_finalize registró goal_verified={recorded.get('goal_verified')!r}. El runtime "
        f"entró por `verification.passed and goal_verified`, así que aquí tiene que ser True. "
        f"Si esto falla, el runtime ha vuelto a no pasar el valor."
    )
