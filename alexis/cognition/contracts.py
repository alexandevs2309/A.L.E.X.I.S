"""Contratos del Cognitive Core (F2.0 — docs/COGNITIVE-CORE-F2.md §3).

Tipos de datos que atraviesan la cognición: intención, brief del Self Model, evidencia
(claims), selección de capabilities, replanning y respuesta al usuario.

Invariantes que este módulo hace explícitos (P3, docs §1.3):

1. Sólo `IntentKind.TASK` puede crear misión (`Intent.is_task`).
2. El modelo *propone* capabilities (`CapabilityProposal`), nunca las autoriza: la
   diferencia entre proponer y seleccionar es `CapabilityProposal` vs `Selection`, y
   sólo el segundo es una decisión del Core respaldada por catálogo + envelope.
3. Un `Claim` de tipo `FACT` exige evidencia y verificación (`Claim.is_sound`).
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class IntentKind(str, Enum):
    GREETING = "greeting"
    SMALL_TALK = "small_talk"
    SELF_QUERY = "self_query"
    CAPABILITY_QUERY = "capability_query"
    META_QUERY = "meta_query"
    TASK = "task"
    UNKNOWN = "unknown"


#: Única kind que autoriza crear una Mission (P3.3).
MISSION_KINDS: frozenset[IntentKind] = frozenset({IntentKind.TASK})

#: Kinds que responden de inmediato sin crear misión (P3.1, P3.2).
DIRECT_KINDS: frozenset[IntentKind] = frozenset(
    {
        IntentKind.GREETING,
        IntentKind.SMALL_TALK,
        IntentKind.SELF_QUERY,
        IntentKind.CAPABILITY_QUERY,
        IntentKind.META_QUERY,
    }
)


@dataclass
class Intent:
    """Intención comprendida de un turno de conversación.

    `requested_capabilities` es una *sugerencia del modelo*, no un permiso: el
    envelope y la policy son la autoridad (P3.4, P3.5).
    """

    kind: IntentKind
    utterance: str
    objective: str | None = None
    target: str | None = None
    success_criteria: list[str] = field(default_factory=list)
    requested_capabilities: list[str] = field(default_factory=list)
    side_effects_intent: str = "unknown"  # read | modify | delete | external | unknown
    ambiguity: str | None = None
    needs_clarification: bool = False
    confidence: float = 0.0
    model_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_task(self) -> bool:
        """True sólo para `TASK`: laMission se crea tras esta comprobación (P3.3)."""
        return self.kind in MISSION_KINDS

    @property
    def is_direct_answer(self) -> bool:
        """True si se responde sin crear misión (saludo, preguntas de self/capacidad)."""
        return self.kind in DIRECT_KINDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "utterance": self.utterance,
            "objective": self.objective,
            "target": self.target,
            "success_criteria": list(self.success_criteria),
            "requested_capabilities": list(self.requested_capabilities),
            "side_effects_intent": self.side_effects_intent,
            "ambiguity": self.ambiguity,
            "needs_clarification": self.needs_clarification,
            "confidence": self.confidence,
            "model_meta": dict(self.model_meta),
        }


@dataclass
class SelfBrief:
    """Vista del Self Model que la cognición consulta ANTES de decidir (F2 §7)."""

    identity: dict[str, Any] = field(default_factory=dict)
    state: str = "idle"
    goal: str | None = None
    mission_id: str | None = None
    available_capabilities: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    missing_capabilities: list[str] = field(default_factory=list)
    permissions: dict[str, Any] = field(default_factory=dict)
    envelope: dict[str, Any] = field(default_factory=dict)
    context: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    confidence: float | None = None
    pending_approvals: list[dict[str, Any]] = field(default_factory=list)
    recent_actions: list[dict[str, Any]] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    limits: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": dict(self.identity),
            "state": self.state,
            "goal": self.goal,
            "mission_id": self.mission_id,
            "available_capabilities": list(self.available_capabilities),
            "required_capabilities": list(self.required_capabilities),
            "missing_capabilities": list(self.missing_capabilities),
            "permissions": dict(self.permissions),
            "envelope": dict(self.envelope),
            "context": list(self.context),
            "uncertainties": list(self.uncertainties),
            "confidence": self.confidence,
            "pending_approvals": list(self.pending_approvals),
            "recent_actions": list(self.recent_actions),
            "dependencies": list(self.dependencies),
            "limits": list(self.limits),
        }

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> "SelfBrief":
        """Construye el brief desde `SelfModel.snapshot()` (F1) sin inventar datos."""
        available = list(snapshot.get("available_capabilities") or [])
        required = list(snapshot.get("required_capabilities") or [])
        return cls(
            identity=dict(snapshot.get("identity") or {}),
            state=snapshot.get("current_state") or "idle",
            goal=snapshot.get("current_goal"),
            mission_id=(snapshot.get("current_mission") or {}).get("id"),
            available_capabilities=available,
            required_capabilities=required,
            missing_capabilities=sorted(set(required) - set(available)),
            permissions=dict(snapshot.get("permissions") or {}),
            envelope=dict(snapshot.get("active_envelope") or {}),
            context=list(snapshot.get("active_context") or []),
            uncertainties=list(snapshot.get("uncertainties") or []),
            confidence=snapshot.get("confidence"),
            pending_approvals=list(snapshot.get("pending_approvals") or []),
            recent_actions=list(snapshot.get("recent_actions") or []),
            dependencies=list(snapshot.get("current_dependencies") or []),
            limits=list(snapshot.get("current_limits") or []),
        )


class ClaimKind(str, Enum):
    FACT = "fact"
    EVIDENCE = "evidence"
    INFERENCE = "inference"
    UNCERTAINTY = "uncertainty"
    ASSUMPTION = "assumption"


@dataclass
class Claim:
    """Afirmación con clasificación epistémica explícita (F2 §12).

    Un `FACT` sin evidencia verificada no es un hecho: `is_sound` lo delata y el
    ClaimGuard lo degrada antes de que llegue al texto final (P3.9).
    """

    id: str
    kind: ClaimKind
    text: str
    source: str  # tool | mission | memory | model | verifier
    evidence_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    verified: bool = False

    def is_sound(self) -> bool:
        if self.kind is ClaimKind.FACT:
            return bool(self.verified and self.evidence_ids)
        if self.kind is ClaimKind.EVIDENCE:
            return True
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "text": self.text,
            "source": self.source,
            "evidence_ids": list(self.evidence_ids),
            "confidence": self.confidence,
            "verified": self.verified,
        }


@dataclass
class CapabilityProposal:
    """Propuesta de capabilities hecha por el modelo. NO es una autorización."""

    capabilities: list[str]
    rationale: str = ""
    model_meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Selection:
    """Resultado de recortar la propuesta contra catálogo + envelope + policy."""

    selected: list[str] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)  # {capability, rule, reason}
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": list(self.selected),
            "rejected": list(self.rejected),
            "rationale": self.rationale,
        }


@dataclass
class ReplanDecision:
    """Decisión de replanning, siempre acotada (P3.8)."""

    action: str  # retry_same | retry_alternative | ask_user | abort
    alternative_capability: str | None = None
    reason: str = ""
    attempt: int = 0

    #: Acciones permitidas; ninguna implica loop infinito: el Core lleva la cuenta.
    ALLOWED_ACTIONS = ("retry_same", "retry_alternative", "ask_user", "abort")

    def is_valid(self) -> bool:
        return self.action in self.ALLOWED_ACTIONS

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "alternative_capability": self.alternative_capability,
            "reason": self.reason,
            "attempt": self.attempt,
        }


@dataclass
class UserReply:
    """Respuesta al usuario: `text` es la experiencia principal; lo demás es estructura."""

    text: str
    kind: str = "answer"  # answer | question | approval_request | error
    claims: list[Claim] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    mission_id: str | None = None
    self_update: dict[str, Any] = field(default_factory=dict)
    #: Procedencia del razonamiento (P1): real | degraded | unavailable | none
    cognition_outcome: str = "none"
    degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "kind": self.kind,
            "claims": [c.to_dict() for c in self.claims],
            "evidence": list(self.evidence),
            "open_questions": list(self.open_questions),
            "mission_id": self.mission_id,
            "self_update": dict(self.self_update),
            "cognition_outcome": self.cognition_outcome,
            "degraded": self.degraded,
        }
