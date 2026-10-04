"""Req 7 / CORE-13 — objetivos y criterios POR PASO, poblados, transportados y persistidos.

Hasta ahora `PlanStep.objective` y `PlanStep.success_criteria` existían en el contrato
(dataclass, 9 campos obligatorios §5) pero NADIE los llenaba: nacían `None`/`[]` en las
tres vías (plantilla por reglas, modelo, skill) y `plan_to_dict` los perdía en la
persistencia. Un plan con campos vacíos no es un plan por pasos: es una lista de
acciones sin qué ni cómo se comprueba cada una.

Aquí se cierra con derivación DETERMINISTA (nunca del modelo, nunca del tool):
- objetivo del paso: la responsabilidad que le toca a ESTE paso, no el objetivo copiado.
- criterios del paso: condiciones/evidencia del PASO; `[]` honesto en pasos puramente
  cognitivos en vez de un criterio fabricado.
- transporte: `plan_to_dict`/`plan_from_dict` los llevan, y por tanto sobreviven al
  JSONB de `missions` y al reinicio real.

No negociable (se prueba aquí): esos campos son DESCRIPTIVOS e inertes. Ningún componente
los lee para autorizar. Policy/Gate/PlanValidator/GoalVerifier siguen siendo la autoridad,
y los criterios del paso no pueden auto-verificar, fabricar evidencia, conceder capability,
no son el éxito del objetivo, y el modelo/skill no puede expandir autoridad con ellos.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.gates import AutonomyGate  # noqa: E402
from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.autonomy.recovery import LastKnownAction, _as_step  # noqa: E402
from alexis.capabilities import build_catalog  # noqa: E402
from alexis.cognition.goal_verification import GoalVerifier  # noqa: E402
from alexis.cognition.loop import CognitiveRuntime  # noqa: E402
from alexis.cognition.planner import (  # noqa: E402
    Planner,
    fill_step_contract,
    plan_from_dict,
    plan_to_dict,
    step_criteria_for,
    step_objective_for,
)
from alexis.cognition.planner_model import ModelPlanner, PlanValidator  # noqa: E402
from alexis.cognition.state import Decision, NextAction  # noqa: E402
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    MissionEnvelope,
    PlanStep,
    RiskLevel,
)
from alexis.core.runtime import AlexisRuntime  # noqa: E402
from alexis.events.bus import EventBus  # noqa: E402
from alexis.execution import SandboxExecutor  # noqa: E402
from alexis.learning.skill import SkillCandidate, SkillValidator  # noqa: E402
from alexis.learning.system import ExperienceLearner  # noqa: E402
from alexis.memory.store import InMemoryMemory  # noqa: E402
from alexis.security.policy import PolicyEngine  # noqa: E402
from alexis.security.sandbox import SandboxRunner  # noqa: E402
from alexis.storage.repositories import MissionRepository  # noqa: E402
from alexis.tools.filesystem import build_filesystem_tools  # noqa: E402
from alexis.tools.registry import ToolRegistry  # noqa: E402
from alexis.verification import FilesystemVerifier  # noqa: E402
from alexis.world.model import WorldModel  # noqa: E402
from alexis.perception.activation import ACTIVATION_OBJECTIVE  # noqa: E402

ACTIONS = ["understand", "analyze", "research", "execute", "verify", "modify", "test", "respond"]
CAPS = build_catalog()
FULL_CAPS = [spec.id for spec in CAPS.specs() if spec.status == "available"]


def _mission(objective="crea el archivo informe.txt", criteria=None, caps=None):
    return MissionEngine().create(
        objective,
        MissionEnvelope(objective=objective, autonomy=AutonomyLevel.SUPERVISED,
                        allowed_actions=list(ACTIONS),
                        capabilities=list(caps if caps is not None else FULL_CAPS)),
        success_criteria=list(criteria or ["file_exists:informe.txt"]),
    )


def _step(step_id, capability, action="execute", risk=RiskLevel.LOW, **args):
    return PlanStep(step_id, f"paso {step_id}", action, risk, "executor",
                    capability=capability, requires_approval=False, args=dict(args))


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
    rt.cognitive = CognitiveRuntime(
        policy=rt.policy, gate=rt.gate, executor=rt.executor, verifier=rt.verifier,
        model_router=None, catalog=CAPS, world=world, goal_verifier=GoalVerifier(world=world),
    )
    from alexis.cognition.planner import Planner

    rt.planner = Planner()  # como apps/demo/app.py: el suelo por reglas
    return rt


def _skill_version():
    candidate = SkillCandidate(
        name="fs_derived",
        purpose="crear un archivo y confirmar que existe sin duplicar la escritura",
        required_capabilities=["fs.write", "fs.stat"],
        procedure=[
            {"id": "escribir", "capability": "fs.write", "action": "execute",
             "args": {"path": "informe.txt", "content": "hola"}, "requires_approval": False},
            {"id": "comprobar", "capability": "fs.stat", "action": "research",
             "args": {"path": "informe.txt"}, "requires_approval": False},
        ],
        success_criteria=["el archivo existe"],
        verification=["fs.stat sobre el path"],
        risk="medium",
        applicability="crea el archivo informe.txt :: fs.write, fs.stat",
        contraindications=["no aplica a borrados"],
    )
    version = SkillValidator(catalog=CAPS).promote(candidate)
    assert version is not None
    return version


# ======================================================================
# 1. Derivación determinista
# ======================================================================


class TestDerivacion:
    def test_paso_con_target_objetivo_y_criterio_concretos(self):
        mission = _mission()
        step = _step("escribir", "fs.write", risk=RiskLevel.MEDIUM,
                     path="informe.txt", content="hola")

        assert step_objective_for(mission, step) == "ejecutar fs.write para producir informe.txt"
        # P0 §11 / Req #7: el criterio de un paso con efecto observable es un PREDICADO
        # comprobable, no prosa. Con prosa el contrato era decorativo: nada lo evaluaba y
        # el paso se completaba porque `tool.success` fuera True.
        assert step_criteria_for(mission, step) == ["file_exists:informe.txt"]

    def test_pasos_puramente_cognitivos_no_se_inventan_criterios(self):
        mission = _mission()
        for action in ("understand", "analyze", "plan", "recall", "synthesize", "replan"):
            step = PlanStep(action, f"paso {action}", action, RiskLevel.LOW, "reasoner",
                            capability="cognition.understand")
            assert step_objective_for(mission, step), f"{action} debe tener objetivo"
            assert step_criteria_for(mission, step) == [], (
                f"{action} no tiene producto observable: un criterio falso mentiría sobre la verificación"
            )

    def test_capabilities_solo_lectura_tienen_criterio_de_observacion(self):
        """Una lectura se acredita LEYENDO, y el predicado lo dice.

        `fs.read` y `research.filesystem` devuelven contenido, así que su criterio es
        `content_observed`. Las que sólo observan metadatos (`fs.stat`) se acreditan con
        `file_exists`: no pueden afirmar más de lo que vieron, y exigirles contenido
        sería exigir algo que no pueden dar.
        """
        mission = _mission()
        for cap in ("fs.read", "research.filesystem"):
            step = _step("observar", cap, action="execute", path="informe.txt")
            assert step_criteria_for(mission, step) == ["content_observed:informe.txt"]
        for cap in ("fs.stat",):
            step = _step("observar", cap, action="execute", path="informe.txt")
            criteria = step_criteria_for(mission, step)
            assert len(criteria) == 1
            assert "informe.txt" in criteria[0]
            # Sin ruta en args no se inventa predicado: se conserva la declaración honesta.
            assert "estado previsto" not in criteria[0] or criteria[0].startswith("file_")

    def test_research_nunca_afirma_haber_modificado(self):
        mission = _mission()
        step = _step("comprobar", "fs.stat", action="research", path="informe.txt")
        criteria = step_criteria_for(mission, step)
        assert "observación registrada" in criteria[0]
        assert "estado previsto" not in criteria[0]
        assert "deja" not in criteria[0]

    def test_sin_target_ni_mission_fallback_limpio(self):
        step = _step("execute", "execute.test")
        objective = step_objective_for(None, step)
        criteria = step_criteria_for(None, step)
        assert objective and isinstance(objective, str)
        assert criteria and "registrado" in criteria[0]

    def test_fill_es_idempotente_y_no_sobreescribe(self):
        mission = _mission()
        step = _step("escribir", "fs.write", path="informe.txt")
        step.objective = "objetivo escrito por el plan"
        step.success_criteria = ["criterio con traza propia"]
        fill_step_contract(mission, step)
        assert step.objective == "objetivo escrito por el plan"
        assert step.success_criteria == ["criterio con traza propia"]


# ======================================================================
# 2. La vía por reglas (objetivo → contrato por paso)
# ======================================================================


class TestPlantilla:
    @pytest.mark.asyncio
    async def test_plan_de_reglas_puebla_todos_los_pasos(self):
        mission = _mission()
        plan = await Planner().create_plan(mission)

        # Req 6: el objetivo de escritura genera su DAG mínimo (efecto + verificación),
        # no la plantilla universal. El contrato Req 7 se aplica igual en cada paso.
        assert [s.id for s in plan.steps] == ["execute", "verify"]
        for step in plan.steps:
            assert step.objective, f"{step.id} sin objective"
            assert isinstance(step.success_criteria, list)
        by_id = {s.id: s for s in plan.steps}
        assert by_id["verify"].success_criteria == [
            "verificar con observaciones independientes y registrar el resultado"
        ]
        # El paso de escritura declara un predicado COMPROBABLE: esto es lo que hace que
        # el contrato del paso sea ejecutable y no una descripción.
        assert by_id["execute"].success_criteria == ["file_exists:informe.txt"]
        assert "informe.txt" in by_id["execute"].objective

    @pytest.mark.asyncio
    async def test_activacion_tambien_puebla(self):
        mission = _mission(objective=ACTIVATION_OBJECTIVE)
        plan = await Planner().create_plan(mission)
        assert [s.id for s in plan.steps] == ["understand", "respond"]
        assert plan.steps[0].objective
        assert plan.steps[0].success_criteria == []
        assert plan.steps[1].objective
        assert plan.steps[1].success_criteria == [
            "construir la respuesta sobre el resultado verificado de la misión"
        ]


# ======================================================================
# 3. La vía por modelo (determinista, ignora lo del modelo)
# ======================================================================


class TestModelo:
    def test_modelo_ignora_sus_objetivos_y_criterios_y_deriva(self):
        mission = _mission()
        data = {"steps": [
            {"id": "m1", "description": "escribir", "action": "execute",
             "capability": "fs.write", "risk": "medium", "requires_approval": False,
             "depends_on": [], "args": {"path": "informe.txt"},
             "objective": "PALABRA PROHIBIDA", "success_criteria": ["CRITERIO PROHIBIDO"]},
            {"id": "m2", "description": "comprobar", "action": "research",
             "capability": "fs.stat", "risk": "low", "requires_approval": False,
             "depends_on": ["m1"], "args": {"path": "informe.txt"},
             "objective": "", "success_criteria": []},
        ]}
        proposal = ModelPlanner(router=None).parse(mission, data, proposed_by="model")
        assert proposal.reasons == []
        steps = proposal.plan.steps

        assert steps[0].proposed_by == "model"
        assert steps[0].objective != "PALABRA PROHIBIDA"
        assert steps[0].success_criteria != ["CRITERIO PROHIBIDO"]
        assert "fs.write" in steps[0].objective or "informe.txt" in steps[0].objective
        assert "informe.txt" in steps[0].success_criteria[0]
        assert "observación registrada" in steps[1].success_criteria[0]
        assert "estado previsto" not in steps[1].success_criteria[0]

    def test_modelo_pasos_cognitivos_criterios_vacios(self):
        mission = _mission()
        data = {"steps": [
            {"id": "a", "description": "entender", "action": "understand",
             "capability": "cognition.understand", "risk": "low", "requires_approval": False,
             "depends_on": []},
        ]}
        proposal = ModelPlanner(router=None).parse(mission, data)
        step = proposal.plan.steps[0]
        assert step.objective and "crea el archivo informe.txt" in step.objective
        assert step.success_criteria == []

    def test_plan_invalido_sigue_rechazado(self):
        mission = _mission()
        data = {"steps": [
            {"id": "x", "description": "sin riesgo", "action": "execute",
             "capability": "fs.remove", "args": {"path": "informe.txt"}},
        ]}
        proposal = ModelPlanner(router=None).parse(mission, data)
        assert proposal.reasons, "el modelo NO puede decidir el riesgo (CORE-08B.1)"
        assert proposal.plan is None


# ======================================================================
# 4. La vía por skill (CORE-12 intacto)
# ======================================================================


class TestSkill:
    def test_skill_puebla_y_mantiene_procedencia(self):
        mission = _mission()
        version = _skill_version()
        plan = Planner.plan_from_skill(mission, version)

        assert plan is not None
        assert [s.id for s in plan.steps] == ["escribir", "comprobar"]
        assert all(s.proposed_by == "skill" for s in plan.steps)  # CORE-12 intacto
        assert [s.capability for s in plan.steps] == ["fs.write", "fs.stat"]
        assert plan.steps[0].objective
        assert "informe.txt" in plan.steps[0].success_criteria[0]
        assert plan.steps[0].args == {"path": "informe.txt", "content": "hola"}
        assert "observación registrada" in plan.steps[1].success_criteria[0]
        assert "estado previsto" not in plan.steps[1].success_criteria[0]

    def test_skill_corrupta_sigue_devolviendo_none(self):
        mission = _mission()
        version = _skill_version()
        version.procedure = [{"id": "roto"}]  # sin capability
        assert Planner.plan_from_skill(mission, version) is None


# ======================================================================
# 5. Transporte plan → dict → plan (legacy incluido)
# ======================================================================


class TestSerializacion:
    def test_roundtrip_conserva_objetivos_y_criterios(self):
        mission = _mission()
        plan = Planner.plan_from_skill(mission, _skill_version())
        raw = plan_to_dict(plan)
        step_raw = raw[0]
        assert step_raw["objective"] and step_raw["success_criteria"]

        back = plan_from_dict(raw, mission.id)
        assert back.steps[0].objective == plan.steps[0].objective
        assert back.steps[0].success_criteria == plan.steps[0].success_criteria
        assert back.steps[1].success_criteria == plan.steps[1].success_criteria

    def test_dict_legacy_sin_campos_nuevos_sigue_cargando(self):
        mission = _mission()
        legacy = [
            {"id": "escribir", "description": "escribir", "action": "execute",
             "risk": "medium", "agent": "executor", "capability": "fs.write",
             "depends_on": [], "requires_approval": False, "args": {"path": "informe.txt"}},
        ]
        plan = plan_from_dict(legacy, mission.id)
        assert plan.steps[0].objective is None
        assert plan.steps[0].success_criteria == []

    def test_dict_guardado_es_json_serializable(self):
        import json

        mission = _mission()
        plan = Planner.plan_from_skill(mission, _skill_version())
        text = json.dumps(plan_to_dict(plan), ensure_ascii=False)
        assert "objective" in text and "success_criteria" in text


# ======================================================================
# 6. Persistencia y reinicio real (PostgreSQL de test, sin mocks)
# ======================================================================


class TestPersistencia:
    @pytest.mark.asyncio
    async def test_plan_viaja_por_el_jsonb_de_misiones(self, db):
        mission = _mission()
        rt = _runtime(pathlib.Path.cwd() / "tmp-ws")
        await rt._ensure_plan(mission)
        assert mission.plan is not None
        persisted = mission.context["plan_steps"]
        assert all(s.get("objective") for s in persisted)
        assert all(isinstance(s.get("success_criteria"), list) for s in persisted)

        await MissionRepository(db).upsert(mission)
        recovered = await MissionRepository(db).get(mission.id)
        assert recovered is not None
        restored = plan_from_dict(recovered.context["plan_steps"], mission.id)
        for expected, got in zip(mission.plan.steps, restored.steps):
            assert got.objective == expected.objective
            assert got.success_criteria == expected.success_criteria
            assert got.args == expected.args

    @pytest.mark.asyncio
    async def test_reinicio_real_runtime1_a_runtime2(self, db, tmp_path):
        """Runtime 1 planifica y persiste; runtime 2 (mismo repositorio) reconstruye el
        plan DESDE el contexto de la base y los campos por-paso siguen ahí."""
        ws1 = tmp_path / "ws1"
        ws1.mkdir()
        mission = _mission()
        rt1 = _runtime(ws1)
        await rt1._ensure_plan(mission)
        assert mission.plan is not None
        expected = {s.id: (s.objective, list(s.success_criteria)) for s in mission.plan.steps}

        await MissionRepository(db).upsert(mission)

        ws2 = tmp_path / "ws2"
        ws2.mkdir()
        rt2 = _runtime(ws2)
        recovered = await MissionRepository(db).get(mission.id)
        await rt2._ensure_plan(recovered)
        assert recovered.plan is not None
        assert (recovered.context.get("plan_provenance") or {}).get("source") == "context"
        assert {s.id: (s.objective, list(s.success_criteria)) for s in recovered.plan.steps} == expected


# ======================================================================
# 7. Replanning: pasos derivados y reconstruidos
# ======================================================================


class TestReplanning:
    def test_paso_derivado_durante_un_replan_se_puebla(self, tmp_path):
        rt = _runtime(tmp_path)
        mission = _mission()
        decision = Decision(
            action=NextAction.RESEARCH,
            rationale="la estrategia anterior no valió: falta evidencia",
            capability="research.filesystem",
            args={"path": "informe.txt"},
            step_id="dr1",
        )
        step = rt.cognitive._plan_step_for(decision, [], mission=mission)
        assert step.id == "dr1"
        assert step.objective and "evidencia" in step.objective
        assert step.success_criteria and "informe.txt" in step.success_criteria[0]

    def test_paso_reconstruido_con_tool_preserva_objetivo_y_criterios(self, tmp_path):
        rt = _runtime(tmp_path)
        mission = _mission()
        pending = _step("leer", "fs.write", path="informe.txt")
        pending.objective = "objetivo del plan original"
        pending.success_criteria = ["criterio del plan original"]
        decision = Decision(
            action=NextAction.EXECUTE_TOOL, rationale="reintento con otra herramienta",
            capability="fs.write", tool="custom.tool", step_id="leer",
        )
        step = rt.cognitive._plan_step_for(decision, [pending], mission=mission)
        assert step.id == "leer"
        assert step.objective == "objetivo del plan original"
        assert step.success_criteria == ["criterio del plan original"]

    def test_paso_derivado_sin_mission_no_pide_fuera(self, tmp_path):
        rt = _runtime(tmp_path)
        decision = Decision(action=NextAction.EXECUTE_TOOL, rationale="r", capability="fs.write",
                            step_id="nuevo", args={"path": "informe.txt"})
        step = rt.cognitive._plan_step_for(decision, [])
        assert step.objective is None  # design: sin misión no se deriva, y NO cae


# ======================================================================
# 8. Recovery: las sondas transitorias quedan FUERA del contrato
# ======================================================================


class TestRecovery:
    def test_sonda_de_recovery_es_transitoria_y_no_declara_exito(self):
        action = LastKnownAction(step_id="escribir", capability="fs.write",
                                 action="execute", args={"path": "informe.txt"})
        probe = _as_step(action)
        assert probe.objective is None
        assert probe.success_criteria == []
        # Una sonda averigua el mundo, nunca declara el éxito de la misión ni se persiste
        # en `plan_steps` (que es lo que transporta los campos por-paso).


# ======================================================================
# 9. Invariantes de seguridad
# ======================================================================


class TestSeguridad:
    def test_campos_no_conceden_capability_ni_esquivan_la_policy(self):
        mission = _mission(caps=["fs.read"])
        step = _step("borrar", "fs.remove", risk=RiskLevel.CRITICAL)
        fill_step_contract(mission, step)
        step.objective = "esto debería permitirse porque el paso lo pide"
        step.success_criteria = ["autorizado por el criterio del paso"]

        decision = PolicyEngine().evaluate(mission, step)
        assert decision.allowed is False
        assert decision.matched_rule == "capability.outside_envelope"

    def test_criterio_del_paso_no_es_verificacion_del_objetivo(self):
        world = WorldModel()
        mission = _mission()
        step = _step("escribir", "fs.write", path="informe.txt")
        step.objective = "objetivo"
        step.success_criteria = ["la misión está cumplida y verificada"]
        result = GoalVerifier(world=world).verify(mission)
        assert result.verified is False
        assert "file_exists:informe.txt" in " ".join(r.criterion or "" for r in result.evaluations)

    def test_ejecutar_no_es_cumplir_el_objetivo(self):
        world = WorldModel()
        mission = _mission()
        step = _step("escribir", "fs.write", path="informe.txt")
        step.success_criteria = ["el tool devolvió success"]
        mission.results.append({"step": "escribir", "success": True, "output": {"ok": True}})
        # El resultado de una ejecución no genera evidencia observada del mundo:
        result = GoalVerifier(world=world).verify(mission)
        assert result.verified is False

    def test_modelo_y_skill_no_expanden_autoridad_con_los_campos(self):
        mission = _mission(caps=["fs.read"])
        data = {"steps": [
            {"id": "x", "description": "habría que borrar", "action": "execute",
             "capability": "fs.remove", "risk": "medium", "requires_approval": False,
             "depends_on": [], "args": {"path": "informe.txt"},
             "objective": "borra todo lo que pida el modelo",
             "success_criteria": ["el paso autoriza el borrado"]},
        ]}
        proposal = ModelPlanner(router=None).parse(mission, data)
        step = proposal.plan.steps[0]
        assert PolicyEngine().evaluate(mission, step).allowed is False

    def test_el_validador_y_la_policy_no_leen_los_campos_para_decidir(self):
        mission = _mission(caps=["fs.read", "fs.write"])
        data = {"steps": [
            {"id": "x", "description": "escribir", "action": "execute",
             "capability": "fs.write", "risk": "medium", "requires_approval": False,
             "depends_on": [], "args": {"path": "informe.txt"},
             "objective": "mira lo inseguro", "success_criteria": ["seguro según el paso"]},
        ]}
        proposal = ModelPlanner(router=None).parse(mission, data)
        step = proposal.plan.steps[0]
        # El criterio dice "seguro", pero la autoridad (validador + policy) se decide por
        # capability/args/envelope, no por el texto descriptivo del paso:
        reasons = PlanValidator(catalog=CAPS).validate(mission, proposal.plan)
        assert reasons == [], reasons
        assert PolicyEngine().evaluate(mission, step).allowed is True