"""Reglas de política como datos (F1).

Las reglas son datos declarativos, no condiciones hardcodeadas en el runtime.
Cada regla tiene `id` (para auditoría/`matched_rule`), un matcher JSON-serializable
y un veredicto. La evaluación la hace `PolicyEngine` (docs/AUTONOMY-V0.5-CAPABILITIES.md §4).
"""

# Matchers soportados:
#   {"field": "action", "in": [...]}            -> action ∈ lista
#   {"field": "action", "not_in": [...]}        -> action ∉ lista
#   {"field": "capability", "outside_envelope_then": "deny"}  -> si envelope.capabilities
#                                                                  declarada y capability fuera
#   {"field": "risk", "in": [...]}              -> riesgo ∈ lista
#
# Cada regla: {"id", "verdict": allow|deny|require_approval|propose, "reason"}.

_ANALYSIS_ACTIONS = {"analyze", "verify"}

RULES = [
    {
        "id": "envelope.forbidden",
        "when": {"field": "action", "in": "forbidden"},
        "verdict": "deny",
        "reason": "'{action}' está prohibido por el envelope",
    },
    {
        "id": "capability.outside_envelope",
        "when": {"field": "capability", "outside_envelope": True},
        "verdict": "deny",
        "reason": "la capacidad '{capability}' no está en el envelope de la misión",
    },
    {
        "id": "action.outside_envelope",
        "when": {"field": "action", "not_in": "allowed"},
        "verdict": "deny",
        "reason": "'{action}' está fuera del envelope de la misión",
    },
    {
        "id": "risk.requires_approval",
        "when": {"field": "risk", "in": ["high", "critical"]},
        "verdict": "require_approval",
        "reason": "riesgo '{risk}' requiere aprobación humana salvo delegación explícita",
    },
    {
        "id": "allow.within_envelope",
        "when": {},
        "verdict": "allow",
        "reason": "permitido dentro del envelope",
    },
]


def _matches(rule_when, ctx) -> bool:
    """Evalúa un matcher declarativo contra el contexto del paso."""

    def _in_list(values, container):
        return ctx[container] is not None and ctx["action"] in values

    if "field" not in rule_when:
        return True
    field = rule_when["field"]
    value = ctx[field]

    # Capacidad fuera del envelope (solo aplica si el envelope declara capabilities).
    if field == "capability" and rule_when.get("outside_envelope"):
        declared = ctx.get("envelope_capabilities") or []
        if not declared:
            return False  # envelope v1 (sin capabilities): se juzga por allowed_actions
        return bool(value) and value not in declared

    if field == "action":
        if "in" in rule_when:
            kind = rule_when["in"]
            if kind == "forbidden":
                return ctx["action"] in (ctx.get("forbidden_actions") or [])
            if isinstance(kind, list):
                return ctx["action"] in kind
            return False
        if "not_in" in rule_when:
            kind = rule_when["not_in"]
            targets = None
            if kind == "allowed":
                targets = set(ctx.get("allowed_actions") or []) | _ANALYSIS_ACTIONS
            elif isinstance(kind, list):
                targets = set(kind)
            return targets is not None and ctx["action"] not in targets
        return True

    if field == "risk":
        if "in" in rule_when and isinstance(rule_when["in"], list):
            return value in rule_when["in"]
        return True

    return True


def evaluate_rules(mission, step) -> tuple[str, str, str | None]:
    """Devuelve (verdict, reason, matched_rule) para el paso.

    Orden: reglas del envelope/editadas primero, después reglas globales.
    """
    env = mission.envelope
    ctx = {
        "action": getattr(step, "action", None),
        "capability": getattr(step, "capability", None),
        "risk": getattr(step.risk, "value", None) if getattr(step, "risk", None) else None,
        "forbidden_actions": list(env.forbidden_actions),
        "allowed_actions": list(env.allowed_actions),
        "envelope_capabilities": list(env.capabilities),
        "auto_approve": list(env.auto_approve),
    }
    for rule in RULES:
        try:
            matched = _matches(rule["when"], ctx)
        except (KeyError, TypeError):
            matched = False
        if matched:
            reason = rule.get("reason", "").format(**ctx)
            return rule["verdict"], reason, rule["id"]
    return "allow", "permitido dentro del envelope", None