from dataclasses import dataclass, field

from alexis.security.policy_rules import evaluate_rules


@dataclass
class PolicyDecision:
    allowed: bool
    reason: str
    requires_approval: bool = False
    verdict: str = "allow"  # allow | deny | require_approval | propose
    matched_rule: str | None = None


class PolicyEngine:
    """Engine de política por reglas (F1).

    Evalúa contexto del paso contra reglas declarativas (envelope primero, luego
    globales) y devuelve una decisión granular con `verdict` y `matched_rule` para
    auditoría. `authorize()` se conserva como compatibilidad (mismo veredicto,
    solo los campos legados).
    """

    def evaluate(self, mission, step) -> PolicyDecision:
        verdict, reason, rule = evaluate_rules(mission, step)
        if verdict == "deny":
            return PolicyDecision(False, reason, False, verdict, rule)
        if verdict == "require_approval":
            return PolicyDecision(True, reason, True, verdict, rule)
        if verdict == "propose":
            return PolicyDecision(True, reason, True, verdict, rule)
        return PolicyDecision(True, reason, False, "allow", rule)

    def authorize(self, mission, step) -> PolicyDecision:
        decision = self.evaluate(mission, step)
        return PolicyDecision(
            decision.allowed,
            decision.reason,
            decision.requires_approval,
        )