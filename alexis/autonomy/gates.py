from dataclasses import dataclass

from alexis.contracts import AutonomyLevel
from alexis.meta.cognition import MetaCognition
from alexis.tools.filesystem import classify_objective_intent

READ_ONLY_ACTIONS = {"analyze", "understand", "research", "verify"}
WRITE_ACTIONS = {"execute", "modify", "test", "commit", "write"}


@dataclass
class GateDecision:
    allowed: bool
    reason: str
    requires_approval: bool = False
    verdict: str | None = None
    matched_rule: str | None = None
    capability: str | None = None


class AutonomyGate:
    """Decide qué puede hacer ALEXIS sola según su nivel de autonomía.

    ASSIST: propone efectos de escritura, ejecuta solo lectura/investigación.
    SUPERVISED: ejecuta lo permitido en el envelope; lo delicado del plan pide
        aprobación humana antes de tocar el workspace.
    AUTONOMOUS: ejecuta sola todo lo que esté dentro del envelope (allowed_actions);
        lo que el usuario marcó en approval_required vuelve a pedir manos humanas.

    Fuera del envelope (forbidden / no nombrado en allowed_actions): la puerta se
    cierra (BLOCKED real). El envelope es la declaración del usuario: si no está,
    no se puede ejecutar, no es "preguntar".
    """

    def __init__(self, confidence_threshold: float = 0.50):
        self.threshold = confidence_threshold
        self.metacognition = MetaCognition()

    def _confidence(self, mission) -> tuple[float, object]:
        evidence = sum(1 for r in (mission.results or []) if r.get("success"))
        independent = 1 if len(mission.results or []) >= 2 else 0
        assumptions = len(
            [a for a in mission.envelope.approval_required if a in mission.envelope.allowed_actions]
        )
        conf = self.metacognition.assess(evidence, independent, assumptions)
        return conf.score, conf

    def decide(self, mission, step, policy) -> GateDecision:
        action = step.action
        level = mission.envelope.autonomy
        capability = getattr(step, "capability", None)

        base = policy.evaluate(mission, step)
        if not base.allowed:
            return GateDecision(
                False,
                f"({level.value}): no ejecuto '{action}' — {base.reason}",
                matched_rule=base.matched_rule,
                capability=capability,
            )

        # La exigencia de la Policy es vinculante en cualquier nivel de autonomía (H3):
        # un paso no puede anularla. El flag del paso, en cambio, es asesor y lo
        # interpreta el nivel: en ASSIST/SUPERVISED exige aprobación; en AUTONOMOUS
        # manda el envelope (`approval_required`), que es la declaración del usuario.
        policy_required = bool(base.requires_approval)
        required = bool(step.requires_approval or policy_required)

        if action in READ_ONLY_ACTIONS:
            # Ser read-only NO es un permiso: la policy se consulta igual. Lo único que
            # la lectura no compra es saltarse una aprobación que la policy o el propio
            # paso exigen (p. ej. una capability crítica marcada como tal).
            if required:
                return GateDecision(
                    True,
                    f"({level.value}): paso de lectura que sí requiere aprobación — {base.reason}",
                    requires_approval=True,
                    matched_rule=base.matched_rule,
                    capability=capability,
                )
            return GateDecision(
                True,
                f"({level.value}): paso sin efectos de escritura ({action}) — {base.reason}",
                matched_rule=base.matched_rule,
                capability=capability,
            )

        if action == "respond":
            return GateDecision(True, f"({level.value}): respuesta directa", capability=capability)

        score, _ = self._confidence(mission)
        intent = classify_objective_intent(mission.goal.objective) if action == "execute" else None

        if level is AutonomyLevel.AUTONOMOUS:
            if (
                action == "execute"
                and intent == "destructive"
                and "destructive" in mission.envelope.approval_required
            ):
                return GateDecision(
                    True,
                    f"(autonomous): 'borrar' está en approval_required del envelope → pido "
                    f"aprobación humana (confianza={score:.2f}, umbral={self.threshold:.2f})",
                    requires_approval=True,
                    capability=capability,
                )
            if action in WRITE_ACTIONS and action not in mission.envelope.allowed_actions:
                return GateDecision(
                    True,
                    f"(autonomous): '{action}' no está en allowed_actions; si quieres que lo haga "
                    f"sola, decláralo en el envelope (confianza={score:.2f})",
                    requires_approval=True,
                    capability=capability,
                )
            if policy_required:
                return GateDecision(
                    True,
                    f"({level.value}): la policy exige aprobación para este paso "
                    f"({base.reason}; matched_rule={base.matched_rule})",
                    requires_approval=True,
                    matched_rule=base.matched_rule,
                    capability=capability,
                )
            return GateDecision(
                True,
                f"({level.value}): ejecuto sola dentro del envelope (intent={intent or action}, "
                f"confianza={score:.2f} ≥ umbral={self.threshold:.2f})",
                capability=capability,
            )

        if level is AutonomyLevel.ASSIST:
            if action in WRITE_ACTIONS or required:
                return GateDecision(
                    True,
                    "(assist): propongo el efecto, no lo ejecuto sin tu aprobación "
                    f"(intent={intent or action}" + (f"; {base.reason}" if required else "") + ")",
                    requires_approval=True,
                    matched_rule=base.matched_rule,
                    capability=capability,
                )
            return GateDecision(
                True, f"(assist): ejecuto automáticamente lo read-only ({action})", capability=capability
            )

        if required:
            return GateDecision(
                True,
                f"({level.value}): requiere aprobación (paso={step.requires_approval}, "
                f"policy={base.requires_approval}; {base.reason})",
                requires_approval=True,
                matched_rule=base.matched_rule,
                capability=capability,
            )
        return GateDecision(
            True, "(supervised): efecto permitido en el envelope", capability=capability
        )
