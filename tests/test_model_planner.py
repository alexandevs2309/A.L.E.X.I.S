"""Fase 2.5: ModelPlanner + PlanValidator.

Los 9 escenarios obligatorios:
1. plan válido → aceptado y continúa hacia el runtime
2. capability inexistente → REJECT, executor.calls == []
3. capability fuera del envelope → REJECT
4. dependencia inexistente → REJECT
5. dependencia circular → REJECT
6. argumentos inválidos → REJECT antes del executor
7. modelo falla (timeout / JSON inválido / unavailable) → fallback determinista
8. contexto distinto → plan distinto
9. el planner no puede saltarse Policy/Gate ni ejecutar
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.gates import AutonomyGate  # noqa: E402
from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.capabilities import build_catalog  # noqa: E402
from alexis.cognition.contracts import SelfBrief  # noqa: E402
from alexis.cognition.planner import Planner  # noqa: E402
from alexis.cognition.planner_model import ModelPlanner, PlanProposal, PlanValidator  # noqa: E402
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    ExecutionResult,
    MissionEnvelope,
    MissionState,
    Observation,
    Plan,
    PlanStep,
    RiskLevel,
    Verification,
)
from alexis.core.runtime import AlexisRuntime  # noqa: E402
from alexis.events.bus import EventBus  # noqa: E402
from alexis.learning.system import ExperienceLearner  # noqa: E402
from alexis.memory.store import InMemoryMemory  # noqa: E402
from alexis.models.provider import ModelOutcome, ModelRequest, ModelResponse  # noqa: E402
from alexis.security.policy import PolicyEngine  # noqa: E402
from alexis.world.model import WorldEntity, WorldModel  # noqa: E402

ALL_ACTIONS = ["understand", "analyze", "research", "execute", "verify", "modify", "test", "respond"]


def _mission(objective="analiza notas.txt", **over):
    data = dict(
        objective=objective,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=list(ALL_ACTIONS),
    )
    data.update(over)
    return MissionEngine().create(objective, MissionEnvelope(**data))


def _brief(available=None):
    catalog = build_catalog()
    enabled = [s.id for s in catalog.enabled()] if available is None else list(available)
    return SelfBrief(
        available_capabilities=enabled,
        required_capabilities=[],
        missing_capabilities=[],
        envelope={},
    )


def _good_plan_json():
    return {
        "steps": [
            {
                "id": "observar",
                "description": "Comprobar el estado real del archivo",
                "action": "research",
                "capability": "fs.stat",
                "depends_on": [],
                "risk": "low",
                "expected": "saber si existe",
            },
            {
                "id": "leer",
                "description": "Leer el contenido si existe",
                "action": "research",
                "capability": "fs.read",
                "depends_on": ["observar"],
                "risk": "low",
            },
        ]
    }


class _StubRouter:
    """Devuelve lo que se le configure; registra lo que recibió."""

    def __init__(self, data=None, *, outcome=ModelOutcome.REAL, text="", raises=None):
        self.data = data
        self.outcome = outcome
        self.text = text
        self.raises = raises
        self.requests = []

    async def complete(self, request: ModelRequest):
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        return ModelResponse(
            text=self.text,
            data=self.data,
            provider="stub",
            model="stub-model",
            outcome=self.outcome,
        )


class _Executor:
    def __init__(self):
        self.calls = []
        self.executed = []

    async def execute(self, mission, step, *, tool_name=None):
        self.calls.append(step.id)
        self.executed.append(
            {"id": step.id, "capability": step.capability, "args": dict(step.args or {})}
        )
        return ExecutionResult(
            success=True,
            output={"ok": True, "step": step.id},
            observations=[Observation(f"tool.{step.id}", {"ok": True}, trusted=True)],
        )

    async def __call__(self, mission, step, tool_name=None):
        return await self.execute(mission, step, tool_name=tool_name)


class _PassingVerifier:
    async def verify(self, mission, plan):
        return Verification(passed=True, evidence=["ok"], confidence=0.9, notes="verificado")


def _planner(router, **over):
    return ModelPlanner(router, catalog=build_catalog(), **over)


def _validator(**over):
    return PlanValidator(catalog=build_catalog(), brief=_brief(), **over)


# ----------------------------------------------------------------------
# TEST 1 — plan válido → aceptado
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_model_plan_is_accepted_and_reaches_the_runtime():
    router = _StubRouter(_good_plan_json())
    proposal = await _planner(router).create_plan(_mission())
    reasons = _validator().validate(_mission(), proposal.plan)
    for step in proposal.plan.steps:
        reasons.extend(_validator().validate_args(step))

    assert proposal.ok is True
    assert reasons == []
    assert [s.id for s in proposal.plan.steps] == ["observar", "leer"]
    assert proposal.plan.steps[0].proposed_by == "model"
    assert proposal.meta["cognition_outcome"] == "real"


@pytest.mark.asyncio
async def test_accepted_model_plan_is_used_by_the_runtime_and_policy_still_authorizes():
    router = _StubRouter(_good_plan_json())
    executor = _Executor()
    runtime = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=executor,
        verifier=_PassingVerifier(),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        plan_model=_planner(router),
        plan_validator=_validator(),
    )
    mission = _mission()

    result = await runtime.run_mission(mission)

    assert [s.id for s in result.plan.steps] == ["observar", "leer"]
    assert result.context["plan_provenance"]["accepted"] is True
    assert result.context["plan_provenance"]["proposed_by"] == "model"
    assert result.state is MissionState.NEEDS_VERIFICATION
    assert executor.calls == ["observar", "leer"]


# ----------------------------------------------------------------------
# TEST 2 — capability inexistente
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nonexistent_capability_is_rejected_and_never_executed():
    data = {
        "steps": [
            {
                "id": "magia",
                "description": "Leer con magia",
                "action": "research",
                "capability": "git.magic_super_read",
                "depends_on": [],
            }
        ]
    }
    router = _StubRouter(data)
    executor = _Executor()
    runtime = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=executor,
        verifier=_PassingVerifier(),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        plan_model=_planner(router),
        plan_validator=_validator(),
    )
    mission = _mission()

    result = await runtime.run_mission(mission)

    provenance = result.context["plan_provenance"]
    assert provenance["accepted"] is False
    assert any("git.magic_super_read" in r for r in provenance["reasons"])
    assert "capability inexistente" in " ".join(provenance["reasons"])
    assert provenance["fallback"] == "rule_based_planner"
    assert "magia" not in executor.calls
    assert all(c["capability"] != "git.magic_super_read" for c in executor.executed)
    fallback_plan = await Planner().create_plan(_mission())
    assert [s.id for s in result.plan.steps] == [s.id for s in fallback_plan.steps]


# ----------------------------------------------------------------------
# TEST 3 — capability fuera del envelope
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capability_outside_the_envelope_is_rejected():
    data = {
        "steps": [
            {
                "id": "borrar",
                "description": "Borrar el archivo",
                "action": "execute",
                "capability": "fs.remove",
                "risk": "critical",
                "depends_on": [],
            }
        ]
    }
    router = _StubRouter(data)
    mission = _mission("borra notas.txt", capabilities=["fs.read", "research.filesystem"])
    proposal = await _planner(router).create_plan(mission)

    reasons = _validator().validate(mission, proposal.plan)

    assert proposal.ok is True
    assert any("fuera del envelope" in r for r in reasons)


@pytest.mark.asyncio
async def test_envelope_cannot_be_widened_by_the_plan():
    mission = _mission(capabilities=["fs.read"])
    plan = Plan(
        mission.id,
        [PlanStep("x", "borrar", "execute", RiskLevel.CRITICAL, "executor", capability="fs.remove")],
    )
    reasons = _validator().validate(mission, plan)
    assert any("fuera del envelope" in r for r in reasons)
    assert mission.envelope.capabilities == ["fs.read"]


# ----------------------------------------------------------------------
# TEST 4 y 5 — dependencias
# ----------------------------------------------------------------------


def test_nonexistent_dependency_is_rejected():
    mission = _mission()
    plan = Plan(
        mission.id,
        [
            PlanStep("a", "A", "research", RiskLevel.LOW, "researcher", ["step-X"], capability="fs.read"),
        ],
    )
    reasons = _validator().validate(mission, plan)
    assert any("depende de un paso inexistente: step-X" in r for r in reasons)


def test_circular_dependency_is_rejected():
    mission = _mission()
    plan = Plan(
        mission.id,
        [
            PlanStep("a", "A", "research", RiskLevel.LOW, "researcher", ["b"], capability="fs.read"),
            PlanStep("b", "B", "research", RiskLevel.LOW, "researcher", ["a"], capability="fs.read"),
        ],
    )
    reasons = _validator().validate(mission, plan)
    assert any("ciclo de dependencias" in r for r in reasons)


def test_valid_dag_is_accepted():
    mission = _mission()
    plan = Plan(
        mission.id,
        [
            PlanStep("a", "A", "research", RiskLevel.LOW, "researcher", [], capability="fs.read"),
            PlanStep("b", "B", "research", RiskLevel.LOW, "researcher", ["a"], capability="fs.read"),
            PlanStep("c", "C", "research", RiskLevel.LOW, "researcher", ["a", "b"], capability="fs.read"),
        ],
    )
    assert _validator().validate(mission, plan) == []


# ----------------------------------------------------------------------
# TEST 6 — argumentos inválidos
# ----------------------------------------------------------------------


def test_escaping_path_argument_is_rejected():
    step = PlanStep(
        "leer",
        "leer fuera",
        "research",
        RiskLevel.LOW,
        "researcher",
        capability="fs.read",
        args={"path": "../../etc/passwd"},
    )
    reasons = _validator().validate_args(step)
    assert any("intenta salir del perímetro" in r for r in reasons)


def test_absolute_path_argument_is_rejected():
    step = PlanStep("leer", "leer", "research", RiskLevel.LOW, "researcher", capability="fs.read", args={"path": "/etc/passwd"})
    assert any("intenta salir del perímetro" in r for r in _validator().validate_args(step))


def test_unsupported_argument_type_is_rejected():
    step = PlanStep(
        "leer",
        "leer",
        "research",
        RiskLevel.LOW,
        "researcher",
        capability="fs.read",
        args={"path": {"nested": True}},
    )
    assert any("tipo no soportado" in r for r in _validator().validate_args(step))


def test_valid_arguments_are_accepted():
    step = PlanStep("leer", "leer", "research", RiskLevel.LOW, "researcher", capability="fs.read", args={"path": "notas.txt"})
    assert _validator().validate_args(step) == []


@pytest.mark.asyncio
async def test_invalid_arguments_stop_the_plan_before_the_executor():
    data = {
        "steps": [
            {
                "id": "leer",
                "description": "Leer con una ruta fuera del workspace",
                "action": "research",
                "capability": "fs.read",
                "depends_on": [],
                "args": {"path": "../../etc/passwd"},
            }
        ]
    }
    executor = _Executor()
    runtime = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=executor,
        verifier=_PassingVerifier(),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        plan_model=_planner(_StubRouter(data)),
        plan_validator=_validator(),
    )

    result = await runtime.run_mission(_mission())

    assert result.context["plan_provenance"]["accepted"] is False
    assert any("perímetro" in r for r in result.context["plan_provenance"]["reasons"])
    assert all(c["args"].get("path") != "../../etc/passwd" for c in executor.executed)


# ----------------------------------------------------------------------
# TEST 7 — el modelo falla
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_timeout_falls_back_to_the_rule_based_planner():
    router = _StubRouter(raises=TimeoutError("timeout del provider"))
    proposal = await _planner(router).create_plan(_mission())
    assert proposal.ok is False
    assert proposal.plan is None
    assert "modelo no disponible" in proposal.reasons[0]


@pytest.mark.asyncio
async def test_invalid_json_falls_back():
    router = _StubRouter(None, text="no soy un plan, solo texto")
    proposal = await _planner(router).create_plan(_mission())
    assert proposal.ok is False
    assert any("JSON" in r for r in proposal.reasons)


@pytest.mark.asyncio
async def test_provider_unavailable_falls_back():
    router = _StubRouter(_good_plan_json(), outcome=ModelOutcome.UNAVAILABLE)
    proposal = await _planner(router).create_plan(_mission())
    assert proposal.ok is False
    assert proposal.meta["cognition_outcome"] == "unavailable"


@pytest.mark.asyncio
async def test_degraded_model_is_not_treated_as_real_planning():
    router = _StubRouter(_good_plan_json(), outcome=ModelOutcome.DEGRADED)
    proposal = await _planner(router).create_plan(_mission())
    assert proposal.ok is False
    assert "DEGRADED" in proposal.reasons[0]


@pytest.mark.asyncio
async def test_empty_plan_falls_back():
    router = _StubRouter({"steps": []})
    proposal = await _planner(router).create_plan(_mission())
    assert proposal.ok is False


@pytest.mark.asyncio
async def test_oversized_plan_falls_back():
    steps = [
        {"id": f"s{i}", "description": "x", "action": "research", "capability": "fs.read"}
        for i in range(12)
    ]
    proposal = await _planner(_StubRouter({"steps": steps}), max_steps=6).create_plan(_mission())
    assert proposal.ok is False
    assert "demasiado largo" in proposal.reasons[0]


# ----------------------------------------------------------------------
# TEST 8 — el contexto cambia el plan
# ----------------------------------------------------------------------


class _ContextSensitiveRouter:
    """Devuelve una estrategia u otra según lo que el planner le pasó como contexto."""

    def __init__(self):
        self.requests = []

    async def complete(self, request: ModelRequest):
        self.requests.append(request)
        context = request.messages[-1]["content"]
        if "exists=False" in context or "no existe" in context:
            data = {
                "steps": [
                    {
                        "id": "crear",
                        "description": "El archivo no existe: crearlo",
                        "action": "modify",
                        "capability": "fs.write",
                        "risk": "medium",
                        "depends_on": [],
                        "args": {"path": "notas.txt"},
                    }
                ]
            }
        else:
            data = {
                "steps": [
                    {
                        "id": "leer",
                        "description": "El archivo existe: leerlo",
                        "action": "research",
                        "capability": "fs.read",
                        "risk": "low",
                        "depends_on": [],
                        "args": {"path": "notas.txt"},
                    }
                ]
            }
        return ModelResponse(
            text="", data=data, provider="stub", model="stub", outcome=ModelOutcome.REAL
        )


@pytest.mark.asyncio
async def test_same_objective_different_context_different_plan():
    router = _ContextSensitiveRouter()
    planner = _planner(router)
    mission = _mission("analiza notas.txt")

    world_exists = WorldModel()
    world_exists.upsert(WorldEntity("file:notas.txt", "file", "notas.txt", {"exists": True}))
    world_missing = WorldModel()
    world_missing.upsert(WorldEntity("file:notas.txt", "file", "notas.txt", {"exists": False}))

    plan_a = await planner.create_plan(mission, world=world_exists)
    plan_b = await planner.create_plan(mission, world=world_missing)

    assert [s.id for s in plan_a.plan.steps] == ["leer"]
    assert [s.id for s in plan_b.plan.steps] == ["crear"]
    assert plan_a.plan.steps[0].capability != plan_b.plan.steps[0].capability
    assert "exists=True" in router.requests[0].messages[-1]["content"]
    assert "exists=False" in router.requests[1].messages[-1]["content"]
    assert _validator().validate(mission, plan_a.plan) == []
    assert _validator().validate(mission, plan_b.plan) == []


@pytest.mark.asyncio
async def test_self_model_and_knowledge_reach_the_planner_prompt():
    router = _StubRouter(_good_plan_json())
    planner = _planner(router)
    mission = _mission()
    mission.goal.success_criteria = ["el archivo existe y tiene contenido"]
    mission.envelope.perimeters = [{"path": "workspace", "mode": "rw"}]

    from alexis.cognition.state import KnowledgeState

    knowledge = KnowledgeState(objective=mission.goal.objective)
    knowledge.add_known("el proyecto tiene 3 carpetas")

    await planner.create_plan(mission, brief=_brief(), knowledge=knowledge)

    context = router.requests[-1].messages[-1]["content"]
    assert "el archivo existe y tiene contenido" in context
    assert "workspace" in context
    assert "el proyecto tiene 3 carpetas" in context
    assert "fs.read" in context


# ----------------------------------------------------------------------
# TEST 9 — el planner no puede ejecutar ni saltarse Policy
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planner_never_executes_a_tool():
    router = _StubRouter(_good_plan_json())
    executor = _Executor()
    planner = _planner(router)
    await planner.create_plan(_mission())
    assert executor.calls == []


@pytest.mark.asyncio
async def test_policy_still_asks_for_approval_after_the_plan_is_accepted():
    """El plan pasa el validador, y aun así Policy/Gate manda: pausa y no ejecuta.

    Demuestra la cadena: ModelPlanner → PlanValidator → CognitiveRuntime →
    Policy/Gate → (pausa) → Executor.
    """
    data = {
        "steps": [
            {
                "id": "borrar",
                "description": "Borrar el archivo que el objetivo nombra",
                "action": "execute",
                "capability": "fs.remove",
                "risk": "critical",
                "requires_approval": True,
                "depends_on": [],
                "args": {"path": "notas.txt"},
            }
        ]
    }
    mission = _mission("borra notas.txt", capabilities=["fs.remove", "fs.read"])
    executor = _Executor()
    runtime = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=executor,
        verifier=_PassingVerifier(),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        plan_model=_planner(_StubRouter(data)),
        plan_validator=_validator(),
    )

    result = await runtime.run_mission(mission)

    assert result.context["plan_provenance"]["accepted"] is True
    assert result.state is MissionState.WAITING_APPROVAL
    assert executor.calls == []
    assert result.context["pending_approval"]["step"] == "borrar"


@pytest.mark.asyncio
async def test_model_cannot_declare_away_the_approval_requirement():
    """El modelo no puede entregar una capability crítica como si fuera inocua.

    El `AutonomyGate` lee `step.requires_approval`; si el modelo lo pone a `false` en un
    paso crítico, sería la puerta de entrada sin manos humanas. El validador lo impide.
    """
    data = {
        "steps": [
            {
                "id": "borrar",
                "description": "Borrar sin preguntar",
                "action": "execute",
                "capability": "fs.remove",
                "risk": "critical",
                "requires_approval": False,
                "depends_on": [],
                "args": {"path": "notas.txt"},
            }
        ]
    }
    mission = _mission("borra notas.txt", capabilities=["fs.remove", "fs.read"])
    executor = _Executor()
    runtime = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=executor,
        verifier=_PassingVerifier(),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        plan_model=_planner(_StubRouter(data)),
        plan_validator=_validator(),
    )

    result = await runtime.run_mission(mission)

    rejected = [r for r in result.context["plan_rejected"] if r["source"] == "model"]
    assert result.context["plan_provenance"]["accepted"] is False
    assert rejected and any("no puede reducir esa protección" in r for r in rejected[0]["reasons"])
    assert all(c["capability"] != "fs.remove" or c["id"] != "borrar" for c in executor.executed) or result.state is MissionState.WAITING_APPROVAL


# ----------------------------------------------------------------------
# Contratos, serialización y compatibilidad
# ----------------------------------------------------------------------


def test_model_plan_survives_serialization_roundtrip():
    from alexis.cognition.planner import plan_from_dict, plan_to_dict

    router = _StubRouter(_good_plan_json())
    proposal = _SyncProposal(router)
    plan = proposal.plan
    raw = plan_to_dict(plan)
    back = plan_from_dict(raw, "m1")
    step = back.steps[0]

    assert raw[0]["proposed_by"] == "model"
    assert back.steps[1].depends_on == ["observar"]
    assert step.args == {}
    assert step.expected == "saber si existe"
    assert step.rationale == step.rationale


def test_legacy_plan_dict_without_new_fields_still_loads():
    from alexis.cognition.planner import plan_from_dict

    legacy = [
        {
            "id": "understand",
            "description": "Entender",
            "action": "analyze",
            "risk": "low",
            "agent": "reasoner",
            "depends_on": [],
            "requires_approval": False,
            "capability": "cognition.understand",
        }
    ]
    plan = plan_from_dict(legacy, "m1")
    assert plan.steps[0].args == {}
    assert plan.steps[0].expected is None
    assert plan.steps[0].proposed_by is None


@pytest.mark.asyncio
async def test_legacy_runtime_without_plan_model_keeps_the_fixed_template():
    runtime = AlexisRuntime(
        planner=Planner(),
        policy=PolicyEngine(),
        executor=_Executor(),
        verifier=_PassingVerifier(),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
    )
    mission = _mission()
    result = await runtime.run_mission(mission)
    assert [s.id for s in result.plan.steps] == ["understand", "research", "execute", "verify"]
    assert "plan_provenance" not in result.context


def test_understated_risk_is_rejected():
    step = PlanStep("borrar", "borrar", "execute", RiskLevel.LOW, "executor", capability="fs.remove")
    reasons = _validator().validate(_mission(capabilities=["fs.remove"]), Plan("m", [step]))
    assert any("riesgo declarado insuficiente" in r for r in reasons)


def test_effect_action_without_side_effects_capability_is_rejected():
    step = PlanStep("analiza", "analiza", "modify", RiskLevel.MEDIUM, "executor", capability="fs.read")
    reasons = _validator().validate(_mission(capabilities=["fs.read"]), Plan("m", [step]))
    assert any("sin efectos" in r for r in reasons)


def test_goal_relevance_rejects_a_plan_that_proposes_other_paths():
    step = PlanStep(
        "otros", "Leer otro archivo", "research", RiskLevel.LOW, "researcher",
        capability="fs.read", args={"path": "otro.txt"},
    )
    reasons = _validator().validate(_mission("analiza notas.txt", capabilities=["fs.read"]), Plan("m", [step]))
    assert any("otras rutas" in r for r in reasons)


def test_goal_relevance_accepts_a_generic_plan_without_paths():
    step = PlanStep("mirar", "Mirar el archivo del objetivo", "research", RiskLevel.LOW, "researcher", capability="fs.read")
    assert _validator().validate(_mission("analiza notas.txt", capabilities=["fs.read"]), Plan("m", [step])) == []


def test_plan_proposal_ok_property():
    assert PlanProposal(plan=Plan("m", [])).ok is True
    assert PlanProposal(plan=Plan("m", [PlanStep("a", "a", "research", capability="fs.read")])).ok is True
    assert PlanProposal(reasons=["x"]).ok is False
    assert PlanProposal().ok is False


class _SyncProposal:
    """Ayudante: ejecuta el parseo del planner de forma síncrona."""

    def __init__(self, router):
        self._planner = _planner(router)

    def __getattr__(self, item):
        return getattr(self._planner, item)

    @property
    def plan(self):
        proposal = self._planner.parse(_mission(), _good_plan_json())
        return proposal.plan
