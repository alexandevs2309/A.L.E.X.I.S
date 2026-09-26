from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AutonomyLevel(str, Enum):
    ASSIST = "assist"
    SUPERVISED = "supervised"
    AUTONOMOUS = "autonomous"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class MissionState(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    VERIFYING = "verifying"
    #: El objetivo no está demostrado todavía. No es un fallo: es "todavía no".
    NEEDS_VERIFICATION = "needs_verification"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_CLARIFICATION = "waiting_clarification"
    BLOCKED = "blocked"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass
class Goal:
    objective: str
    constraints: dict[str, Any] = field(default_factory=dict)
    success_criteria: list[str] = field(default_factory=list)


@dataclass
class MissionEnvelope:
    objective: str
    autonomy: AutonomyLevel = AutonomyLevel.SUPERVISED
    allowed_actions: list[str] = field(default_factory=lambda: ["read", "research"])
    forbidden_actions: list[str] = field(default_factory=list)
    approval_required: list[str] = field(default_factory=lambda: ["destructive", "production", "external_communication"])
    max_runtime_minutes: int = 60
    max_cost_usd: float = 0.0
    # Envelope v2 (autonomía por capacidades, ver docs/AUTONOMY-V0.5-CAPABILITIES.md §5).
    # Vacía => se mantiene el comportamiento legacy por allowed_actions.
    capabilities: list[str] = field(default_factory=list)
    perimeters: list[dict] = field(default_factory=list)
    auto_approve: list[str] = field(default_factory=list)


class UnverifiedGoalError(RuntimeError):
    """Se intentó declarar COMPLETED sin que el GoalVerifier lo autorice (P0 §5.5).

    No es un aviso: es la invariante. `ACTION SUCCESS -> COMPLETED` es exactamente el
    salto que el plan veta, y este error hace que no se pueda escribir el estado.
    """


@dataclass
class Mission:
    id: str
    goal: Goal
    envelope: MissionEnvelope
    state: MissionState = MissionState.PENDING
    context: dict[str, Any] = field(default_factory=dict)
    results: list[dict[str, Any]] = field(default_factory=list)
    plan: "Plan | None" = None
    #: Verificación del OBJETIVO. La fija el GoalVerifier (§5.3); nadie más.
    goal_verification: "Any | None" = None

    def __setattr__(self, name, value):
        """Única puerta a `COMPLETED`, y solo con objetivo verificado (§5.5).

        No es una condición superficial en el sitio donde se marca el estado: es el propio
        estado el que se niega. Cualquier camino —cognitivo, legacy, API, script, tests—
        que intente `mission.state = MissionState.COMPLETED` sin un `GoalVerification`
        confirmado recibe `UnverifiedGoalError`. Los caminos que sí pueden completarse usan
        `MissionEngine.settle()`, que es la política; esta es la redacción.
        """
        if name == "state" and value == MissionState.COMPLETED:
            from alexis.autonomy.goal_state import goal_is_confirmed

            if not goal_is_confirmed(self.goal_verification):
                raise UnverifiedGoalError(
                    f"la misión {getattr(self, 'id', '?')} no puede pasar a COMPLETED: "
                    "su GoalVerification no dice verified=True con todos los criterios "
                    "satisfechos y evidencia fiable. ACTION SUCCESS no es OBJECTIVE SUCCESS."
                )
        object.__setattr__(self, name, value)


@dataclass
class PlanStep:
    id: str
    description: str
    action: str
    risk: RiskLevel = RiskLevel.LOW
    agent: str = "general"
    depends_on: list[str] = field(default_factory=list)
    requires_approval: bool = False
    capability: str | None = None
    # F2 — Cognitive Core (docs/COGNITIVE-CORE-F2.md §3): campos opcionales añadidos al
    # final para no romper la construcción posicional de F0/F1.
    requires_input: dict[str, Any] = field(default_factory=dict)
    verification: str | None = None
    proposed_by: str | None = None
    rationale: str | None = None
    # F2.5 — ModelPlanner: argumentos que el plan propone para la capability y qué
    # se espera que ocurra (criterio de éxito del paso). Al final, retrocompatible.
    args: dict[str, Any] = field(default_factory=dict)
    expected: str | None = None


@dataclass
class Plan:
    mission_id: str
    steps: list[PlanStep]


@dataclass
class Observation:
    source: str
    content: Any
    trusted: bool = False


@dataclass
class Verification:
    passed: bool
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0
    notes: str = ""
    # F2 — Cognitive Core: verificación estructurada y trazable a claims (opcionales).
    checks: list["VerificationCheck"] = field(default_factory=list)
    claim_ids: list[str] = field(default_factory=list)
    independent: bool = False


@dataclass
class VerificationCheck:
    """Un check individual de la verificación independiente (F2 §13).

    `kind="deterministic"` es el único que puede sostener `passed=True` por sí solo
    (P3.6/P3.7: el crítico de modelo no aprueba solo)."""

    id: str
    kind: str                    # deterministic | model_critic | human
    passed: bool
    detail: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    verifier: str = "unknown"
    can_approve: bool = False    # True solo para checks deterministas independientes

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "passed": self.passed,
            "detail": self.detail,
            "evidence_ids": list(self.evidence_ids),
            "verifier": self.verifier,
            "can_approve": self.can_approve,
        }


def verification_can_approve(verification: Verification) -> bool:
    """Regla P3.6/P3.7: solo un check determinista independiente puede aprobar.

    Un `ModelCritic` (kind="model_critic") nunca aprueba por sí solo, aunque todos sus
    checks pasen; en ese caso la verificación se considera *no concluyente*."""
    return any(check.passed and check.can_approve for check in (verification.checks or []))


@dataclass
class ExecutionResult:
    success: bool
    output: Any = None
    error: str | None = None
    observations: list[Observation] = field(default_factory=list)


class TaskState(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRYING = "retrying"


@dataclass
class Task:
    id: str
    mission_id: str
    kind: str = "task"
    agent: str = "general"
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    status: TaskState = TaskState.PENDING
    attempts: int = 0
    max_attempts: int = 3
    deadline: float | None = None
    lease_until: float | None = None
    error: str | None = None
    result: Any = None


@dataclass
class Execution:
    task_id: str
    tool: str
    args_hash: str = ""
    ok: bool | None = None
    output: Any = None
    error: str | None = None


@dataclass
class Checkpoint:
    mission_id: str
    step_index: int
    payload: dict[str, Any] = field(default_factory=dict)
