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
        mission_criteria = list(getattr(mission.goal, "success_criteria", []) or [])

        def step_contract(step_objective: str, criteria: list[str], expected: str) -> dict:
            return {
                "objective": step_objective,
                "success_criteria": list(criteria),
                "expected": expected,
            }

        if is_activation_objective(objective):
            return Plan(mission.id, [
                PlanStep(
                    "understand", "Understand the activation request", "analyze", RiskLevel.LOW, "reasoner",
                    capability="cognition.understand",
                    **step_contract(
                        objective,
                        ["la solicitud de activación queda comprendida"],
                        "contexto suficiente para responder al usuario",
                    ),
                ),
                PlanStep(
                    "respond", "Greet the user and ask for instructions", "respond", RiskLevel.LOW, "responder",
                    ["understand"], capability="tts.speak",
                    **step_contract(
                        "responder a la solicitud de activación sin inventar una tarea",
                        ["la respuesta fue emitida al usuario"],
                        "respuesta de activación entregada",
                    ),
                ),
            ])

        desktop = desktop_tool_for(objective)
        if desktop is not None:
            tool_name = desktop[0]
            return Plan(mission.id, [
                PlanStep(
                    "understand", f"Understand objective: {objective}", "analyze", RiskLevel.LOW, "reasoner",
                    capability="cognition.understand",
                    **step_contract(objective, ["el objetivo queda comprendido"], "contexto suficiente para seleccionar la acción")),
                PlanStep(
                    "execute", f"Dispatch desktop tool {tool_name}", "execute", RiskLevel.MEDIUM,
                    "executor", ["understand"], capability="desktop.tools",
                    **step_contract(
                        objective,
                        ["la acción de escritorio fue ejecutada"],
                        "resultado observable de la herramienta de escritorio",
                    ),
                ),
                PlanStep(
                    "respond", "Confirm the desktop action", "respond", RiskLevel.LOW, "responder",
                    ["execute"], capability="tts.speak",
                    **step_contract(
                        "comunicar el resultado real de la acción de escritorio",
                        ["la respuesta refleja el resultado observado"],
                        "respuesta basada en la observación de la herramienta",
                    ),
                ),
            ])

        intent = classify_objective_intent(objective)
        if is_informational_objective(objective):
            return Plan(mission.id, [
                PlanStep(
                    "understand", f"Understand the question: {objective}", "analyze", RiskLevel.LOW,
                    "reasoner", capability="cognition.understand",
                    **step_contract(objective, ["la pregunta queda comprendida"], "contexto suficiente para responder"),
                ),
                PlanStep(
                    "respond", "Answer conversationally in Spanish", "respond", RiskLevel.LOW,
                    "responder", ["understand"], capability="tts.speak",
                    **step_contract(
                        "responder la pregunta usando la evidencia disponible",
                        ["la respuesta fue emitida y no afirma hechos no observados"],
                        "respuesta final basada en contexto y evidencia",
                    ),
                ),
            ])

        delicate = intent == "destructive"
        execute_capability = self._select_execute_capability(mission, objective, intent)
        if execute_capability is None:
            execute_capability = {
                "write": "fs.write",
                "destructive": "fs.remove",
                "unsupported": "execution.sandbox",
            }.get(intent, "fs.read")

        execute_criteria = mission_criteria or [
            "la acción produce una observación útil para alcanzar el objetivo"
        ]
        verify_criteria = mission_criteria or ["el resultado queda verificado"]

        steps = [
            PlanStep(
                "understand", f"Understand objective: {objective}", "analyze", RiskLevel.LOW, "reasoner",
                capability="cognition.understand",
                **step_contract(
                    objective,
                    ["el objetivo queda comprendido"],
                    "contexto suficiente para elegir una estrategia",
                ),
            ),
            PlanStep(
                "research", "Gather relevant evidence and context", "research", RiskLevel.LOW, "researcher",
                ["understand"], capability="research.filesystem",
                **step_contract(
                    "obtener evidencia relevante para el objetivo",
                    ["existe evidencia observable relacionada con el objetivo"],
                    "observaciones del workspace relevantes al objetivo",
                ),
            ),
            PlanStep(
                "execute",
                "Execute the smallest useful action",
                "execute",
                RiskLevel.MEDIUM,
                "executor",
                ["research"],
                requires_approval=delicate,
                capability=execute_capability,
                **step_contract(
                    objective,
                    execute_criteria,
                    "resultado observable de la capability ejecutada",
                ),
            ),
            PlanStep(
                "verify", "Independently verify the result", "verify", RiskLevel.LOW, "critic",
                ["execute"], capability="verification.filesystem",
                **step_contract(
                    "comprobar de forma independiente el resultado respecto al objetivo",
                    verify_criteria,
                    "evidencia independiente suficiente para evaluar el objetivo",
                ),
            ),
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
            "objective": getattr(s, "objective", None),
            "success_criteria": list(getattr(s, "success_criteria", []) or []),
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
            objective=s.get("objective"),
            success_criteria=list(s.get("success_criteria") or []),
            args=dict(s.get("args") or {}),
            expected=s.get("expected"),
        )
        for s in raw
    ]
    return Plan(mission_id, steps)
