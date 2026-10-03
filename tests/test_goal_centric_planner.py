import asyncio

from alexis.autonomy.mission import MissionEngine
from alexis.capabilities import build_catalog
from alexis.cognition.planner import Planner
from alexis.cognition.planner_model import ModelPlanner, PlanValidator
from alexis.contracts import AutonomyLevel, MissionEnvelope
from alexis.models.provider import ModelOutcome, ModelRequest, ModelResponse


def mission(objective="Crea el archivo notas.txt", criteria=None):
    m = MissionEngine().create(
        objective,
        MissionEnvelope(
            objective=objective,
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=["read", "research", "execute", "write", "modify", "verify"],
            capabilities=[s.id for s in build_catalog().enabled()],
        ),
        success_criteria=criteria or ["file_exists:notas.txt"],
    )
    return m


def test_rule_planner_populates_step_goal_contract():
    m = mission()
    plan = asyncio.run(Planner().create_plan(m))
    assert plan.steps
    for step in plan.steps:
        assert step.objective
        assert step.success_criteria
        assert step.expected
        assert step.capability
        assert step.risk is not None
        assert isinstance(step.requires_approval, bool)


def test_rule_planner_verification_step_inherits_mission_success_criteria():
    m = mission(criteria=["file_exists:notas.txt", "file_size_at_least:notas.txt:1"])
    plan = asyncio.run(Planner().create_plan(m))
    verify = next(step for step in plan.steps if step.id == "verify")
    assert verify.success_criteria == m.goal.success_criteria


def test_plan_serialization_preserves_goal_contract():
    m = mission()
    plan = asyncio.run(Planner().create_plan(m))
    from alexis.cognition.planner import plan_from_dict, plan_to_dict

    restored = plan_from_dict(plan_to_dict(plan), m.id)
    for original, current in zip(plan.steps, restored.steps):
        assert current.objective == original.objective
        assert current.success_criteria == original.success_criteria
        assert current.expected == original.expected


class Router:
    async def complete(self, request: ModelRequest, *, correlation=None):
        return ModelResponse(
            data={"steps": [{
                "id": "write",
                "description": "Crear archivo",
                "action": "execute",
                "capability": "fs.write",
                "depends_on": [],
                "risk": "medium",
                "requires_approval": False,
                "objective": "crear el archivo solicitado",
                "success_criteria": ["file_exists:notas.txt"],
                "expected": "observación de notas.txt existente",
                "args": {"path": "notas.txt", "content": "hola"},
            }]},
            provider="test",
            model="test",
            outcome=ModelOutcome.REAL,
        )


def test_model_plan_requires_goal_contract():
    planner = ModelPlanner(Router(), catalog=build_catalog())
    m = mission()
    bad = planner.parse(m, {"steps": [{
        "id": "write",
        "description": "Crear archivo",
        "action": "execute",
        "capability": "fs.write",
        "risk": "medium",
        "requires_approval": False,
        "expected": "archivo creado",
    }]})
    assert not bad.ok
    assert any("objective" in reason for reason in bad.reasons)
    assert any("success_criteria" in reason for reason in bad.reasons)


def test_model_plan_with_goal_contract_is_structured():
    planner = ModelPlanner(Router(), catalog=build_catalog())
    m = mission()
    proposal = planner.parse(m, {
        "steps": [{
            "id": "write",
            "description": "Crear archivo",
            "action": "execute",
            "capability": "fs.write",
            "depends_on": [],
            "risk": "medium",
            "requires_approval": False,
            "objective": "crear el archivo solicitado",
            "success_criteria": ["file_exists:notas.txt"],
            "expected": "observación de notas.txt existente",
            "args": {"path": "notas.txt", "content": "hola"},
        }]
    })
    assert proposal.ok
    step = proposal.plan.steps[0]
    assert step.objective == "crear el archivo solicitado"
    assert step.success_criteria == ["file_exists:notas.txt"]
    assert step.expected == "observación de notas.txt existente"
