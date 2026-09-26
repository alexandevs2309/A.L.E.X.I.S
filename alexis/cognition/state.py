"""Fase 1 del Cognitive Runtime: estado epistémico y decisión.

Tres piezas:

- `NextAction`: el verbo de la decisión. La acción se elige DESPUÉS de observar, no
  antes: el plan es una hipótesis de trabajo, no un guion.
- `KnowledgeState`: lo que ALEXIS sabe, lo que no sabe, lo que hypothesize y con qué
  confianza. Es la diferencia entre un runtime que ejecuta y uno que razona.
- `Decision`: una propuesta de acción con su justificación, su capability y sus claims.
  Una `Decision` es una PROPUESTA: la autoriza `PolicyEngine`/`AutonomyGate`.

El enum de claims (`ClaimKind`) y el dataclass `Claim` viven en
`alexis/cognition/contracts.py` (F2.0) y se reutilizan sin duplicar.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from alexis.cognition.contracts import Claim, ClaimKind


class NextAction(str, Enum):
    """Qué decide hacer ALEXIS ahora mismo."""

    EXECUTE_TOOL = "execute_tool"
    RESEARCH = "research"
    ASK_USER = "ask_user"
    WAIT = "wait"
    REPLAN = "replan"
    VERIFY = "verify"
    FINISH = "finish"
    ABORT = "abort"

    @property
    def is_execution(self) -> bool:
        return self in (NextAction.EXECUTE_TOOL, NextAction.RESEARCH)

    @property
    def is_terminal(self) -> bool:
        return self in (NextAction.FINISH, NextAction.ABORT, NextAction.ASK_USER, NextAction.WAIT)


class Verdict(str, Enum):
    """Veredicto de evaluar UNA ACCIÓN. Nunca del objetivo (P0 §5.2).

    Contrato, valor por valor:

    - ``SUCCESS``: la acción se ejecutó y hay una observación concreta de que hizo lo que
      se le pedía. No dice nada sobre el objetivo: eso lo verifica `GoalVerifier` (§5.3).
    - ``PARTIAL_SUCCESS``: la acción corrió sin error pero sin observación que permita
      afirmar que hizo lo esperado. Se ejecutó; no consta que sirviera.
    - ``FAILURE``: la acción se intentó y no consiguió lo suyo, o no hubo avance real.
    - ``INSUFFICIENT_EVIDENCE``: no se puede afirmar ni sí ni no. Es el veredicto por
      defecto cuando nada se ha evaluado, y también el de FINISH mientras no exista
      verificación a nivel de objetivo (§5.5 pendiente).
    - ``BLOCKED``: la autoridad (policy, gate, aprobación, validez del plan o
      disponibilidad de la capability) impidió la acción.

    Prohibido, por contrato: usar ``SUCCESS`` para afirmar que el objetivo se consiguió.
    ``ACTION SUCCESS -> OBJECTIVE SUCCESS`` es exactamente el salto que el plan veta.
    """

    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILURE = "failure"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    BLOCKED = "blocked"

    @property
    def is_conclusive(self) -> bool:
        """Si el veredicto dice algo sobre lo que se evaluó."""
        return self in (Verdict.SUCCESS, Verdict.PARTIAL_SUCCESS, Verdict.FAILURE)


@dataclass
class KnowledgeState:
    """Lo que la mente sabe del objetivo mientras avanza la misión.

    `known` es lo observado; `unknown` es lo que reconoce que no sabe; `hypotheses`
    son explicaciones posibles todavía no comprobadas; `assumptions` son supuestos
    declarados. Un supuesto no se esconde: viaja explícito.
    """

    objective: str = ""
    known: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    hypotheses: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    completed_steps: list[str] = field(default_factory=list)
    failed_steps: list[str] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    confidence: float = 0.0
    replans: int = 0
    #: Firmas de las acciones ya intentadas: (capability, args canónicos). Es lo que
    #: permite distinguir "cambié de estrategia" de "reintento con otro id" (P0 §5.7).
    action_signatures: list[str] = field(default_factory=list)
    iterations: int = 0
    stalls: int = 0
    verified: bool = False
    verification_passed: bool = False
    verification_notes: str = ""
    diagnosis: str = ""
    last_error: str = ""
    last_failure_kind: str = ""
    last_verdict: str = ""
    verdict_reason: str = ""
    needs_replan: bool = False
    clarification: str = ""
    memory: list[str] = field(default_factory=list)
    world: list[str] = field(default_factory=list)
    experience: str = ""

    # ------------------------------------------------------------------ #

    def add_known(self, text: str) -> None:
        if text and text not in self.known:
            self.known.append(text)

    def add_unknown(self, text: str) -> None:
        if text and text not in self.unknown:
            self.unknown.append(text)

    def add_hypothesis(self, text: str) -> None:
        if text and text not in self.hypotheses:
            self.hypotheses.append(text)

    def add_assumption(self, text: str) -> None:
        if text and text not in self.assumptions:
            self.assumptions.append(text)

    def add_uncertainty(self, text: str) -> None:
        if text and text not in self.uncertainties:
            self.uncertainties.append(text)

    def add_claim(self, claim: Claim) -> Claim:
        self.claims.append(claim)
        return claim

    def mark_completed(self, step_id: str) -> None:
        if step_id and step_id not in self.completed_steps:
            self.completed_steps.append(step_id)

    def mark_failed(self, step_id: str, error: str = "") -> None:
        if step_id and step_id not in self.failed_steps:
            self.failed_steps.append(step_id)
        if error:
            self.last_error = error
            self.add_uncertainty(error)

    def note_replan(self, diagnosis: str, hypotheses: list[str] | None = None) -> None:
        self.replans += 1
        self.needs_replan = False
        if diagnosis:
            self.diagnosis = diagnosis
        for hypothesis in hypotheses or []:
            self.add_hypothesis(hypothesis)

    def note_verification(self, passed: bool, confidence: float = 0.0, notes: str = "") -> None:
        self.verified = True
        self.verification_passed = bool(passed)
        self.verification_notes = notes or ""
        if confidence:
            self.confidence = float(confidence)

    def progress_fingerprint(self) -> str:
        """Huella de progreso real. Si no cambia entre iteraciones, no hay avance."""
        payload = {
            "completed": sorted(self.completed_steps),
            "failed": sorted(self.failed_steps),
            "verified": self.verified,
            "passed": self.verification_passed,
            "claims": len(self.claims),
            "known": sorted(self.known),
            "unknown": sorted(self.unknown),
            "replans": self.replans,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]

    def facts(self) -> list[Claim]:
        return [c for c in self.claims if c.kind is ClaimKind.FACT]

    def inferences(self) -> list[Claim]:
        return [c for c in self.claims if c.kind is ClaimKind.INFERENCE]

    def summary(self, limit: int = 600) -> str:
        """Resumen compacto para el prompt de decisión."""
        lines = [
            f"objetivo: {self.objective}",
            f"known: {'; '.join(self.known) or '(nada observado todavía)'}",
            f"unknown: {'; '.join(self.unknown) or '(sin huecos declarados)'}",
            f"hypotheses: {'; '.join(self.hypotheses) or '(ninguna)'}",
            f"assumptions: {'; '.join(self.assumptions) or '(ninguna)'}",
            f"completed_steps: {', '.join(self.completed_steps) or '-'}",
            f"failed_steps: {', '.join(self.failed_steps) or '-'}",
            f"replans: {self.replans}  iteraciones: {self.iterations}  confianza: {self.confidence:.2f}",
        ]
        if self.diagnosis:
            lines.append(f"diagnosis: {self.diagnosis}")
        if self.last_error:
            lines.append(f"last_error: {self.last_error}")
        if self.verified:
            lines.append(f"verification: passed={self.verification_passed} ({self.verification_notes})")
        text = "\n".join(lines)
        return text[:limit]

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "known": list(self.known),
            "unknown": list(self.unknown),
            "hypotheses": list(self.hypotheses),
            "assumptions": list(self.assumptions),
            "uncertainties": list(self.uncertainties),
            "completed_steps": list(self.completed_steps),
            "failed_steps": list(self.failed_steps),
            "action_signatures": list(self.action_signatures),
            "claims": [c.to_dict() for c in self.claims],
            "confidence": self.confidence,
            "replans": self.replans,
            "iterations": self.iterations,
            "stalls": self.stalls,
            "verified": self.verified,
            "verification_passed": self.verification_passed,
            "verification_notes": self.verification_notes,
            "diagnosis": self.diagnosis,
            "last_error": self.last_error,
            "last_failure_kind": self.last_failure_kind,
            "last_verdict": self.last_verdict,
            "verdict_reason": self.verdict_reason,
            "needs_replan": self.needs_replan,
            "clarification": self.clarification,
            "memory": list(self.memory),
            "world": list(self.world),
            "experience": self.experience,
        }

    @classmethod
    def from_dict(cls, raw: dict | None, objective: str = "") -> "KnowledgeState":
        raw = dict(raw or {})
        state = cls(objective=raw.get("objective") or objective)
        for key in (
            "known",
            "unknown",
            "hypotheses",
            "assumptions",
            "uncertainties",
            "completed_steps",
            "failed_steps",
              "action_signatures",
        ):
            value = raw.get(key)
            if isinstance(value, list):
                setattr(state, key, [str(v) for v in value])
        state.claims = _claims_from_dict(raw.get("claims"))
        for key in ("confidence", "replans", "iterations", "stalls"):
            value = raw.get(key)
            if isinstance(value, (int, float)):
                setattr(state, key, value if key == "confidence" else int(value))
        state.verified = bool(raw.get("verified"))
        state.verification_passed = bool(raw.get("verification_passed"))
        state.verification_notes = str(raw.get("verification_notes") or "")
        state.diagnosis = str(raw.get("diagnosis") or "")
        state.last_error = str(raw.get("last_error") or "")
        state.last_failure_kind = str(raw.get("last_failure_kind") or "")
        state.last_verdict = str(raw.get("last_verdict") or "")
        state.verdict_reason = str(raw.get("verdict_reason") or "")
        state.needs_replan = bool(raw.get("needs_replan"))
        state.clarification = str(raw.get("clarification") or "")
        if isinstance(raw.get("memory"), list):
            state.memory = [str(v) for v in raw["memory"]]
        if isinstance(raw.get("world"), list):
            state.world = [str(v) for v in raw["world"]]
        state.experience = str(raw.get("experience") or "")
        return state


def _claims_from_dict(raw: Any) -> list[Claim]:
    if not isinstance(raw, list):
        return []
    claims: list[Claim] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            kind = ClaimKind(item.get("kind") or "uncertainty")
        except ValueError:
            kind = ClaimKind.UNCERTAINTY
        claims.append(
            Claim(
                id=str(item.get("id") or ""),
                kind=kind,
                text=str(item.get("text") or ""),
                source=str(item.get("source") or "unknown"),
                evidence_ids=[str(e) for e in (item.get("evidence_ids") or [])],
                confidence=float(item.get("confidence") or 0.0),
                verified=bool(item.get("verified")),
            )
        )
    return claims


@dataclass
class Decision:
    """Una acción propuesta con su justificación. La policy decide si puede ocurrir."""

    action: NextAction
    rationale: str = ""
    capability: str | None = None
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    step_id: str | None = None
    description: str = ""
    question: str | None = None
    success_check: str | None = None
    diagnosis: str = ""
    claims: list[Claim] = field(default_factory=list)
    proposed_by: str = "deterministic"
    cognition_outcome: str = "unavailable"
    model_meta: dict[str, Any] = field(default_factory=dict)
    rejected: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "rationale": self.rationale,
            "capability": self.capability,
            "tool": self.tool,
            "args": dict(self.args),
            "step_id": self.step_id,
            "description": self.description,
            "question": self.question,
            "success_check": self.success_check,
            "diagnosis": self.diagnosis,
            "claims": [c.to_dict() for c in self.claims],
            "proposed_by": self.proposed_by,
            "cognition_outcome": self.cognition_outcome,
            "model_meta": dict(self.model_meta),
            "rejected": list(self.rejected),
        }


__all__ = ["NextAction", "KnowledgeState", "Decision", "Claim", "ClaimKind", "Verdict"]
