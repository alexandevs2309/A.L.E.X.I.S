"""Reflection (P0 §5.6.3).

La reflexión es un **resumen estructurado de lo observable** al cerrar una misión. No es
chain-of-thought: no guarda el razonamiento del modelo, sólo lo que se ejecutó, lo que se
observó y qué calidad tiene la evidencia.

Invariante estructural: `Reflection` es un dataclass **congelado** y sus campos son
todos escalares o listas de texto. No tiene ningún campo que apunte a Policy, Gate,
permisos, catálogo de capabilities ni envelope. Al no poder referenciar la autoridad, no
puede modificarla: la reflexión observa y propone, nada más (P0 §5.6.3, tests 18-21).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from alexis.cognition.state import KnowledgeState, Verdict

#: Verdicts que NO pueden abrir una lección de éxito. El resto sí pueden, con matiz.
_NON_SUCCESS_VERDICTS = (Verdict.FAILURE.value, Verdict.INSUFFICIENT_EVIDENCE.value, Verdict.BLOCKED.value)


@dataclass(frozen=True)
class Reflection:
    """Resumen estructurado del resultado observable de una misión.

    Congelada a propósito: nadie puede mutar una reflexión para alterar lo que se
    aprendió de ella.
    """

    mission_id: str
    objective: str
    #: Estado final de la misión (completed | failed | blocked | needs_verification …).
    outcome: str
    #: Verdicts que se vieron durante la misión, en orden. El último es el que pesó.
    verdict: str = ""
    what_worked: list[str] = field(default_factory=list)
    what_failed: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    replans: int = 0
    #: Veredicto sobre la propia evidencia: "verified" | "partial" | "insufficient".
    evidence_quality: str = "insufficient"
    unresolved_questions: list[str] = field(default_factory=list)
    #: Lección PROPUESTA. No es aprendizaje: ver `alexis/learning/experience.py`, que
    #: decide si se vuelve conocimiento verificado.
    lessons_candidate: str = ""
    confidence: float = 0.0
    #: Procedencia del modelo en la decisión que cerró la misión: real | degraded | unavailable | none.
    model_outcome: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "objective": self.objective,
            "outcome": self.outcome,
            "verdict": self.verdict,
            "what_worked": list(self.what_worked),
            "what_failed": list(self.what_failed),
            "blockers": list(self.blockers),
            "replans": self.replans,
            "evidence_quality": self.evidence_quality,
            "unresolved_questions": list(self.unresolved_questions),
            "lessons_candidate": self.lessons_candidate,
            "confidence": self.confidence,
            "model_outcome": self.model_outcome,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "Reflection":
        return cls(
            mission_id=str(row.get("mission_id") or ""),
            objective=str(row.get("objective") or ""),
            outcome=str(row.get("outcome") or ""),
            verdict=str(row.get("verdict") or ""),
            what_worked=[str(x) for x in (row.get("what_worked") or [])],
            what_failed=[str(x) for x in (row.get("what_failed") or [])],
            blockers=[str(x) for x in (row.get("blockers") or [])],
            replans=int(row.get("replans") or 0),
            evidence_quality=str(row.get("evidence_quality") or "insufficient"),
            unresolved_questions=[str(x) for x in (row.get("unresolved_questions") or [])],
            lessons_candidate=str(row.get("lessons_candidate") or ""),
            confidence=float(row.get("confidence") or 0.0),
            model_outcome=str(row.get("model_outcome") or "none"),
        )

    def is_frozen(self) -> bool:
        """El invariante de §5.6.3, consultable sin mutar nada: la reflexión es inmutable."""
        return bool(getattr(type(self).__dataclass_params__, "frozen", False))


def build_reflection(
    mission,
    knowledge: KnowledgeState,
    verification=None,
    *,
    model_outcome: str = "none",
) -> Reflection:
    """Construye la reflexión desde el estado real. Determinista y sólo observacional."""
    verdict = str(getattr(knowledge, "last_verdict", "") or Verdict.INSUFFICIENT_EVIDENCE.value)
    goal_verified = bool(getattr(verification, "verified", False))
    quality = _evidence_quality(verification, knowledge)
    outcome = str(getattr(getattr(mission, "state", None), "value", "") or "")

    what_worked = [f"paso '{s}' completado" for s in list(getattr(knowledge, "completed_steps", []) or [])]
    what_failed = [f"paso '{s}': {reason}" for s, reason in _failed_pairs(knowledge)]
    blockers = _blockers(mission, knowledge)

    return Reflection(
        mission_id=str(getattr(mission, "id", "") or ""),
        objective=_objective(mission),
        outcome=outcome,
        verdict=verdict,
        what_worked=what_worked[-5:],
        what_failed=what_failed[-5:],
        blockers=blockers,
        replans=int(getattr(knowledge, "replans", 0) or 0),
        evidence_quality=quality,
        unresolved_questions=[str(u) for u in list(getattr(knowledge, "unknown", []) or [])[:5]],
        lessons_candidate=propose_lesson(verdict, goal_verified, quality, mission),
        confidence=round(float(getattr(knowledge, "confidence", 0.0) or 0.0), 3),
        model_outcome=model_outcome,
    )


def propose_lesson(verdict: str, goal_verified: bool, evidence_quality: str, mission) -> str:
    """Propone una lección, o la deja vacía si no hay nada aprendible con honestidad.

    Reglas de §5.6.5 aplicadas ya en la propuesta:
      - FAILURE / INSUFFICIENT_EVIDENCE / BLOCKED nunca producen lección de éxito.
      - Una lección de éxito exige `goal_verified` Y evidencia verificada.
    """
    if not goal_verified:
        return ""
    if verdict in _NON_SUCCESS_VERDICTS:
        # Se puede aprender del fallo, pero nunca como «el objetivo se consiguió».
        return (
            f"El objetivo se verificó, pero la última acción quedó en {verdict}: "
            "revisar por qué antes de repetir el mismo enfoque."
        )
    if evidence_quality != "verified":
        return (
            "El objetivo se verificó con evidencia parcial: la lección se queda como "
            "candidata, no como conocimiento firme."
        )
    return (
        f"El objetivo «{_objective(mission)}» se logró con evidencia verificada "
        f"(última acción: {verdict})."
    )


def _evidence_quality(verification, knowledge) -> str:
    if verification is None:
        return "insufficient"
    if not bool(getattr(verification, "verified", False)):
        return "insufficient"
    for evaluation in getattr(verification, "evaluations", []) or []:
        status = str(getattr(getattr(evaluation, "status", None), "value", ""))
        if status != "satisfied":
            return "partial"
    return "verified"


def _failed_pairs(knowledge) -> list[tuple[str, str]]:
    failed = list(getattr(knowledge, "failed_steps", []) or [])
    if failed and isinstance(failed[0], (tuple, list)) and len(failed[0]) == 2:
        return [(str(a), str(b)) for a, b in failed]
    return [(str(step), str(getattr(knowledge, "last_error", "") or "sin detalle")) for step in failed]


def _blockers(mission, knowledge) -> list[str]:
    context = getattr(mission, "context", {}) or {}
    blockers: list[str] = []
    if context.get("blocked_reason"):
        blockers.append(str(context["blocked_reason"]))
    if context.get("pending_approval"):
        blockers.append(f"esperando aprobación: {context['pending_approval'].get('reason', '')}")
    if str(getattr(knowledge, "last_verdict", "")) == Verdict.BLOCKED.value:
        blockers.append(str(getattr(knowledge, "verdict_reason", "") or "bloqueado por la autoridad"))
    return blockers


def _objective(mission) -> str:
    goal = getattr(mission, "goal", None)
    return str(getattr(goal, "objective", "") or "sin objetivo registrado")
