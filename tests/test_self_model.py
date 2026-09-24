import asyncio

from alexis.contracts import (
    AutonomyLevel,
    Goal,
    Mission,
    MissionEnvelope,
    MissionState,
    Plan,
    PlanStep,
    RiskLevel,
)
from alexis.capabilities import ACTION_TO_CAPABILITY
from alexis.events.bus import EventBus
from alexis.self.model import SelfModel
from alexis.self.presence import PRESENCE_ORDER, derive_presence
from alexis.self.sync import SelfModelSync


def _mission(state=MissionState.PENDING, autonomy=AutonomyLevel.SUPERVISED, **over):
    env = MissionEnvelope(
        objective="mejora el rendimiento del workspace",
        autonomy=autonomy,
        allowed_actions=["understand", "analyze", "research", "execute", "verify"],
        forbidden_actions=over.pop("forbidden_actions", ["commit"]),
        approval_required=over.pop("approval_required", ["destructive", "production", "external_communication"]),
        max_runtime_minutes=10,
        max_cost_usd=5.0,
    )
    mission = Mission(id="m1", goal=Goal(env.objective), envelope=env, state=state)
    mission.plan = Plan(
        mission.id,
        [
            PlanStep("understand", "U", "analyze", RiskLevel.LOW, "reasoner", capability="cognition.understand"),
            PlanStep("research", "R", "research", RiskLevel.LOW, "researcher", capability="research.filesystem"),
            PlanStep("execute", "X", "execute", RiskLevel.MEDIUM, "executor", capability="execution.sandbox"),
            PlanStep("verify", "V", "verify", RiskLevel.LOW, "critic", capability="verification.filesystem"),
        ],
    )
    if over.get("results"):
        mission.results = over["results"]
    if over.get("decisions"):
        mission.context["decisions"] = over["decisions"]
    if over.get("evaluations"):
        mission.context["evaluations"] = over["evaluations"]
    if over.get("pending_approval"):
        mission.context["pending_approval"] = over["pending_approval"]
    return mission


# ----------------------------------------------------------------------
# Presence: derivación honesta desde el estado real
# ----------------------------------------------------------------------


def test_presence_order_contains_all_required_states():
    required = {
        "idle", "listening", "thinking", "planning", "evaluating", "researching", "working",
        "replanning", "recovering", "waiting_for_approval", "verifying", "reflecting", "speaking",
        "success", "warning", "error",
    }
    assert set(PRESENCE_ORDER) == required


def test_presence_maps_mission_states():
    cases = {
        MissionState.PLANNING: "planning",
        MissionState.WAITING_APPROVAL: "waiting_for_approval",
        MissionState.VERIFYING: "verifying",
        MissionState.RECOVERING: "recovering",
        MissionState.COMPLETED: "success",
        MissionState.FAILED: "error",
        MissionState.BLOCKED: "error",
        MissionState.STOPPED: "idle",
    }
    for state, expected in cases.items():
        mission = _mission(state=state)
        assert derive_presence(mission) == expected, state


def test_presence_running_details_from_last_step():
    base = {"success": True}
    assert derive_presence(_mission(MissionState.RUNNING)) == "working"
    research = _mission(MissionState.RUNNING, results=[{**base, "step": "research"}])
    assert derive_presence(research) == "researching"
    verify = _mission(MissionState.RUNNING, results=[{**base, "step": "verify"}])
    assert derive_presence(verify) == "verifying"

    none_mission = None
    assert derive_presence(none_mission) == "idle"
    assert derive_presence(None, listening=True) == "listening"


def test_presence_speaking_dominates():
    mission = _mission(MissionState.RUNNING)
    assert derive_presence(mission, speaking=True) == "speaking"
    assert derive_presence(mission, listening=True) == "working"
    assert derive_presence(_mission(MissionState.COMPLETED), reflecting=True) == "reflecting"


# ----------------------------------------------------------------------
# SelfModel: las zonas se derivan de objetos reales
# ----------------------------------------------------------------------


def _completed_mission():
    mission = _mission(state=MissionState.COMPLETED)
    mission.results = [
        {"step": "understand", "success": True, "error": None},
        {"step": "research", "success": True, "error": None},
        {"step": "execute", "success": True, "error": None},
        {"step": "verify", "success": True, "error": None},
    ]
    mission.context["decisions"] = {
        "execute": {"allowed": True, "requires_approval": False, "reason": "dentro del envelope"}
    }
    mission.context["evaluations"] = {
        "execute": {"confidence": 0.8, "reasons": ["2 evidence"], "uncertainties": ["1 asunción"]}
    }
    return mission


def test_self_model_derives_real_zones():
    model = SelfModel(resources={"workspace": "/workspace", "sandbox_no_network": True})
    mission = _completed_mission()
    model.update(
        mission,
        tools=["fs.read", "fs.write", "fs.remove"],
        commitments=[{"id": "m1", "objective": mission.goal.objective, "state": "completed"}],
        verification={"mission_id": "m1", "confidence": 0.8, "evidence": [], "notes": "ok"},
    )
    snap = model.snapshot()
    assert snap["current_goal"] == "mejora el rendimiento del workspace"
    assert snap["current_mission"]["id"] == "m1"
    assert snap["status"] == "success"
    assert snap["available_tools"] == ["fs.read", "fs.write", "fs.remove"]
    assert set(snap["required_capabilities"]) == {
        "cognition.understand",
        "research.filesystem",
        "execution.sandbox",
        "verification.filesystem",
    }
    assert snap["permissions"]["allowed_actions"]
    assert snap["permissions"]["last_decisions"] == [
        {"allowed": True, "requires_approval": False, "reason": "dentro del envelope"}
    ]
    assert "perímetro autorizado" in snap["current_limits"][0]
    assert any("budget runtime" in l for l in snap["current_limits"])
    assert any("prohibido por envelope" in l for l in snap["current_limits"])
    assert len(snap["recent_actions"]) == 4
    assert snap["confidence"] == 0.8
    assert snap["uncertainties"] == ["1 asunción"]
    assert snap["active_commitments"][0]["id"] == "m1"
    assert snap["reflections"] == []


def test_self_model_waiting_approval_exposes_pending():
    mission = _mission(state=MissionState.WAITING_APPROVAL)
    mission.context["pending_approval"] = {
        "step": "execute",
        "action": "execute",
        "risk": "medium",
        "reason": "efecto de escritura",
    }
    model = SelfModel()
    model.update(mission)
    snap = model.snapshot()
    assert snap["status"] == "waiting_for_approval"
    assert snap["pending_approvals"] == [mission.context["pending_approval"]]
    answer = model.answer("¿qué necesito para continuar?")
    assert "aprobación" in answer["answer"]


def test_self_model_honest_about_missing_capabilities():
    mission = _mission()
    mission.plan.steps.append(PlanStep("commit", "C", "commit", RiskLevel.LOW, "executor", capability="git.commit"))
    model = SelfModel()
    model.update(mission)
    assert "git.commit" in model.snapshot()["required_capabilities"]
    assert "git.commit" not in model.snapshot()["available_capabilities"]
    answer = model.answer("¿qué no puedo hacer?")
    assert "git.commit" in answer["answer"]
    who = model.answer("¿qué soy?")["answer"]
    assert "sin conciencia subjetiva" in who


def test_self_model_answers_all_questions():
    model = SelfModel()
    model.update(_completed_mission(), tools=["fs.read"])
    questions = [
        "¿qué soy?",
        "¿qué estoy haciendo?",
        "¿qué objetivo estoy intentando alcanzar?",
        "¿qué puedo hacer?",
        "¿qué no puedo hacer?",
        "¿qué estoy autorizado a hacer?",
        "¿qué necesito para continuar?",
        "¿qué sé?",
        "¿qué no sé?",
        "¿qué tan segura es mi conclusión?",
        "¿qué acaba de ocurrir?",
        "¿qué debería hacer ahora?",
    ]
    for q in questions:
        answer = model.answer(q)
        assert answer["source"] is not None, q
        assert answer["answer"]


# ----------------------------------------------------------------------
# Sync: el Self Model evoluciona con los eventos reales del bus
# ----------------------------------------------------------------------


async def test_sync_evolves_on_real_bus_events():
    bus = EventBus()
    holder = {"mission": None}
    model = SelfModel()
    sync = SelfModelSync(model, lambda: holder["mission"], aux=lambda: {"tools": ["fs.read"]})
    sync._sub = bus.subscribe_async()
    consumer = asyncio.create_task(sync._consume())

    await bus.publish("mission.planning", None)
    await asyncio.sleep(0)
    assert model.current_state == "idle"

    holder["mission"] = _mission(state=MissionState.PLANNING, autonomy=AutonomyLevel.AUTONOMOUS)
    await bus.publish("mission.planning", "m1")
    await asyncio.sleep(0)
    assert model.current_state == "planning"

    holder["mission"].state = MissionState.RUNNING
    holder["mission"].results = [{"step": "execute", "success": True, "error": None}]
    await bus.publish("mission.step_completed", {"step": "execute", "success": True, "output": None})
    await asyncio.sleep(0)
    assert model.current_state == "working"
    assert [r["step"] for r in model.snapshot()["recent_actions"]] == ["execute"]

    await bus.publish("presence.speaking", {"text": "listo"})
    await asyncio.sleep(0)
    assert model.current_state == "speaking"
    assert model.transient["speaking"] is True

    await bus.publish("self.reflected", {"text": "debería haber verificado antes"})
    await asyncio.sleep(0)
    assert model.current_state == "reflecting"
    assert len(model.snapshot()["reflections"]) == 1
    assert model.snapshot()["reflections"][0]["text"] == "debería haber verificado antes"

    await bus.publish("mission.completed", "m1")
    await asyncio.sleep(0)
    holder["mission"].state = MissionState.COMPLETED
    await bus.publish("mission.completed", "m1")
    await asyncio.sleep(0)
    assert model.current_state == "success"
    assert model.transient["speaking"] is False

    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)