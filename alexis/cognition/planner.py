from alexis.contracts import Plan, PlanStep, RiskLevel
from alexis.capabilities import ACTION_TO_CAPABILITY
from alexis.perception.activation import is_activation_objective
from alexis.tools.desktop import desktop_tool_for
from alexis.tools.filesystem import classify_objective_intent, is_informational_objective


class Planner:
    """Planner por capacidades (F1).

    Cada paso declara la `capability` que necesita (`fs.read`, `fs.write`…). El DAG
    de etapas es dinámico según objetivo y capacidad habilitada; las rutas actuales
    (activación, desktop, filesystem) se conservan como etapas opcionales y siguen
    usando los mismos ids para no romper checkpoint/resume/approval.
    """

    def _select_execute_capability(self, mission, objective: str, intent: str):
        """Capability de ejecución elegida por selección dinámica, o `None` si no se puede.

        Se consulta al catálogo REAL. Si no hay catálogo (tests legacy, entornos sin
        capabilities registradas), devuelve `None` y quien llama usa el respaldo.
        """
        from alexis.capabilities.catalog import build_catalog
        from alexis.cognition.selection import CapabilitySelector

        try:
            catalog = build_catalog()
        except Exception:  # noqa: BLE001 — sin catálogo no hay selección que hacer
            return None
        if not catalog.enabled():
            return None
        selector = CapabilitySelector(catalog=catalog)
        selection = selector.best(objective, envelope=getattr(mission, "envelope", None))
        if not selection.selected:
            # Nada seleccionable: `decide_unavailable` dice si preguntar, replanear o abortar.
            selector.decide_unavailable(selection)
            return None
        return selection.selected[0]

    async def create_plan(self, mission) -> Plan:
        objective = mission.goal.objective
        if is_activation_objective(objective):
            return Plan(mission.id, [
                PlanStep("understand", "Understand the activation request", "analyze", RiskLevel.LOW, "reasoner",
                         capability="cognition.understand"),
                PlanStep("respond", "Greet the user and ask for instructions", "respond", RiskLevel.LOW, "responder",
                         ["understand"], capability="tts.speak"),
            ])

        desktop = desktop_tool_for(objective)
        if desktop is not None:
            tool_name = desktop[0]
            return Plan(mission.id, [
                PlanStep("understand", f"Understand objective: {objective}", "analyze", RiskLevel.LOW, "reasoner",
                         capability="cognition.understand"),
                PlanStep("execute", f"Dispatch desktop tool {tool_name}", "execute", RiskLevel.MEDIUM,
                         "executor", ["understand"], capability="desktop.tools"),
                PlanStep("respond", "Confirm the desktop action", "respond", RiskLevel.LOW, "responder",
                         ["execute"], capability="tts.speak"),
            ])

        intent = classify_objective_intent(objective)
        if is_informational_objective(objective):
            return Plan(mission.id, [
                PlanStep("understand", f"Understand the question: {objective}", "analyze", RiskLevel.LOW,
                         "reasoner", capability="cognition.understand"),
                PlanStep("respond", "Answer conversationally in Spanish", "respond", RiskLevel.LOW,
                         "responder", ["understand"], capability="tts.speak"),
            ])

        delicate = intent == "destructive"
        # P0 §5.6 / requisito 6: la capability ya NO se elige con un `dict.get` sobre la
        # intención. La decide `CapabilitySelector` contra el catálogo real, el envelope y
        # la policy. El `dict` queda sólo como respaldo cuando no hay catálogo disponible.
        execute_capability = self._select_execute_capability(mission, objective, intent)
        if execute_capability is None:
            # Nada seleccionable: no se inventa una capability. Se deja el paso sin
            # capability para que Policy/Gate decidan, o se bloquea más abajo.
            execute_capability = {
                "write": "fs.write",
                "destructive": "fs.remove",
                "unsupported": "execution.sandbox",
            }.get(intent, "fs.read")
        steps = [
            PlanStep("understand", f"Understand objective: {objective}", "analyze", RiskLevel.LOW, "reasoner",
                     capability="cognition.understand"),
            PlanStep("research", "Gather relevant evidence and context", "research", RiskLevel.LOW, "researcher",
                     ["understand"], capability="research.filesystem"),
            PlanStep(
                "execute",
                "Execute the smallest useful action",
                "execute",
                RiskLevel.MEDIUM,
                "executor",
                ["research"],
                requires_approval=delicate,
                capability=execute_capability,
            ),
            PlanStep("verify", "Independently verify the result", "verify", RiskLevel.LOW, "critic",
                     ["execute"], capability="verification.filesystem"),
        ]
        return Plan(mission.id, steps)


def _step_capability(step) -> str | None:
    """Capability del paso: explícita del plan, o mapeada por acción si viene de obra
    legada (persistencia antigua)."""
    cap = getattr(step, "capability", None)
    if cap:
        return cap
    return ACTION_TO_CAPABILITY.get(getattr(step, "action", ""))


def plan_to_dict(plan: Plan) -> list[dict]:
    return [
        {
            "id": s.id,
            "description": s.description,
            "action": s.action,
            "risk": s.risk.value,
            "agent": s.agent,
            "depends_on": list(s.depends_on),
            "requires_approval": s.requires_approval,
            "capability": _step_capability(s),
            "requires_input": dict(getattr(s, "requires_input", {}) or {}),
            "verification": getattr(s, "verification", None),
            "proposed_by": getattr(s, "proposed_by", None),
            "rationale": getattr(s, "rationale", None),
            "args": dict(getattr(s, "args", {}) or {}),
            "expected": getattr(s, "expected", None),
        }
        for s in plan.steps
    ]


def plan_from_dict(raw: list[dict], mission_id: str) -> Plan:
    steps = [
        PlanStep(
            id=s["id"],
            description=s.get("description", ""),
            action=s.get("action", "analyze"),
            risk=RiskLevel(s.get("risk", RiskLevel.LOW.value)),
            agent=s.get("agent", "general"),
            depends_on=list(s.get("depends_on", [])),
            requires_approval=bool(s.get("requires_approval", False)),
            capability=s.get("capability"),
            requires_input=dict(s.get("requires_input") or {}),
            verification=s.get("verification"),
            proposed_by=s.get("proposed_by"),
            rationale=s.get("rationale"),
            args=dict(s.get("args") or {}),
            expected=s.get("expected"),
        )
        for s in raw
    ]
    return Plan(mission_id, steps)
