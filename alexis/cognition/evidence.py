"""Fase 1 del Cognitive Runtime: evidencia y claims con clasificación epistémica.

Regla que gobierna este módulo: **una salida de modelo nunca es un hecho**.

- Observación de herramienta → `EVIDENCE` (ocurrió, con su contenido).
- Verificación determinista independiente → `FACT` (comprobado).
- Razonamiento del modelo → `INFERENCE`, `ASSUMPTION` o `UNCERTAINTY`.
- Un `FACT` sin `verified=True` + `evidence_ids` no sobrevive: `ClaimGuard` lo degrada
  antes de que pueda llegar al texto final o al estado de la misión.

Usa `Claim`/`ClaimKind` de `alexis/cognition/contracts.py` (F2.0): no se duplican tipos.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from alexis.cognition.contracts import Claim, ClaimKind
from alexis.contracts import ExecutionResult, Observation, Verification, verification_can_approve


def new_claim_id() -> str:
    return f"claim-{uuid.uuid4().hex[:12]}"


def _text_of(content: Any, limit: int = 400) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:limit]
    try:
        return json.dumps(content, ensure_ascii=False, sort_keys=True)[:limit]
    except (TypeError, ValueError):
        return str(content)[:limit]


class ClaimGuard:
    """Degrada claims no sólidos. Determinista y sin excepciones.

    Cada degradación queda registrada en `degradations` para que sea observable y
    testeable: si el sistema nunca degrada nada, hay que poder demostrarlo.
    """

    def __init__(self):
        self.degradations: list[dict[str, Any]] = []

    def guard(self, claim: Claim) -> Claim:
        if not isinstance(claim, Claim):
            return claim
        if claim.kind is ClaimKind.FACT:
            if claim.source == "model":
                return self._downgrade(claim, ClaimKind.INFERENCE, "un claim del modelo no puede ser FACT")
            if not claim.verified or not claim.evidence_ids:
                target = ClaimKind.INFERENCE if claim.evidence_ids else ClaimKind.UNCERTAINTY
                return self._downgrade(claim, target, "FACT sin verificación independiente se degrada")
            return claim
        if claim.kind is ClaimKind.EVIDENCE and not claim.text:
            return self._downgrade(claim, ClaimKind.UNCERTAINTY, "evidencia sin contenido")
        return claim

    def guard_all(self, claims) -> list[Claim]:
        return [self.guard(c) for c in (claims or [])]

    def unsound_facts(self, claims) -> list[Claim]:
        return [c for c in (claims or []) if isinstance(c, Claim) and c.kind is ClaimKind.FACT and not c.is_sound()]

    def _downgrade(self, claim: Claim, kind: ClaimKind, reason: str) -> Claim:
        self.degradations.append(
            {
                "claim_id": claim.id or new_claim_id(),
                "from": claim.kind.value,
                "to": kind.value,
                "reason": reason,
                "text": claim.text[:200],
            }
        )
        return Claim(
            id=claim.id or new_claim_id(),
            kind=kind,
            text=claim.text,
            source=claim.source,
            evidence_ids=list(claim.evidence_ids),
            confidence=min(claim.confidence, 0.5),
            verified=False,
        )


class EvidenceStore:
    """Colecciona claims de una misión aplicando siempre el guard."""

    def __init__(self, guard: ClaimGuard | None = None):
        self.guard = guard or ClaimGuard()
        self.claims: list[Claim] = []

    # ------------------------------------------------------------------ #

    def add(self, claim: Claim) -> Claim:
        guarded = self.guard.guard(claim)
        self.claims.append(guarded)
        return guarded

    def add_many(self, claims) -> list[Claim]:
        return [self.add(c) for c in (claims or [])]

    def from_observation(self, obs: Observation) -> Claim:
        """Una observación de herramienta es evidencia de lo que la herramienta devolvió."""
        return self.add(
            Claim(
                id=new_claim_id(),
                kind=ClaimKind.EVIDENCE,
                text=_text_of(obs.content),
                source=f"observation:{obs.source}",
                evidence_ids=[],
                confidence=0.7 if obs.trusted else 0.4,
            )
        )

    def from_execution_result(self, result: ExecutionResult) -> list[Claim]:
        claims = [self.from_observation(o) for o in (result.observations or [])]
        if not claims and result.output is not None:
            claims.append(
                self.add(
                    Claim(
                        id=new_claim_id(),
                        kind=ClaimKind.EVIDENCE,
                        text=_text_of(result.output),
                        source="executor",
                        confidence=0.7 if result.success else 0.5,
                    )
                )
            )
        if not result.success:
            claims.append(
                self.add(
                    Claim(
                        id=new_claim_id(),
                        kind=ClaimKind.EVIDENCE,
                        text=f"la acción falló: {result.error or 'sin detalle'}",
                        source="executor",
                        confidence=0.8,
                    )
                )
            )
        return claims

    def from_verification(self, verification: Verification) -> list[Claim]:
        """Solo una verificación determinista independiente puede producir FACT."""
        can_approve = verification_can_approve(verification)
        kind = ClaimKind.FACT if (verification.passed and can_approve) else ClaimKind.UNCERTAINTY
        if verification.passed and not can_approve:
            kind = ClaimKind.INFERENCE
        claim = self.add(
            Claim(
                id=new_claim_id(),
                kind=kind,
                text=(verification.notes or "verificación sin notas")[:400],
                source="verifier",
                evidence_ids=[f"verification:{i}" for i in range(len(verification.evidence or []))],
                confidence=float(verification.confidence or 0.0),
                verified=bool(verification.passed and can_approve),
            )
        )
        claims = [claim]
        for item in verification.evidence or []:
            claims.append(
                self.add(
                    Claim(
                        id=new_claim_id(),
                        kind=ClaimKind.EVIDENCE,
                        text=str(item)[:400],
                        source="verifier:evidence",
                        confidence=0.8,
                    )
                )
            )
        return claims

    def from_model_claims(self, items, *, source: str = "model") -> list[Claim]:
        """Claims propuestos por el modelo. Nunca FACT: es la regla que el usuario exige."""
        claims: list[Claim] = []
        for item in items or []:
            if isinstance(item, str):
                text, kind_value, evidence = item, "inference", []
            elif isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                kind_value = str(item.get("kind") or "inference").strip().lower()
                evidence = [str(e) for e in (item.get("evidence_ids") or [])]
            else:
                continue
            if not text:
                continue
            kind = {
                "assumption": ClaimKind.ASSUMPTION,
                "uncertainty": ClaimKind.UNCERTAINTY,
                "evidence": ClaimKind.EVIDENCE,
                "inference": ClaimKind.INFERENCE,
                "fact": ClaimKind.INFERENCE,
            }.get(kind_value, ClaimKind.INFERENCE)
            if kind is ClaimKind.EVIDENCE:
                kind = ClaimKind.INFERENCE
            claims.append(
                self.add(
                    Claim(
                        id=new_claim_id(),
                        kind=kind,
                        text=text[:400],
                        source=source,
                        evidence_ids=evidence,
                        confidence=0.5,
                        verified=False,
                    )
                )
            )
        return claims

    def from_model_text(self, text: str, *, source: str = "model", confidence: float = 0.5) -> Claim | None:
        if not (text or "").strip():
            return None
        return self.add(
            Claim(
                id=new_claim_id(),
                kind=ClaimKind.INFERENCE,
                text=text[:400],
                source=source,
                confidence=confidence,
                verified=False,
            )
        )

    def from_assumptions(self, assumptions) -> list[Claim]:
        return [
            self.add(
                Claim(
                    id=new_claim_id(),
                    kind=ClaimKind.ASSUMPTION,
                    text=str(a)[:400],
                    source="runtime",
                    confidence=0.4,
                )
            )
            for a in (assumptions or [])
            if str(a).strip()
        ]

    # ------------------------------------------------------------------ #

    def facts(self) -> list[Claim]:
        return [c for c in self.claims if c.kind is ClaimKind.FACT]

    def by_kind(self, kind: ClaimKind) -> list[Claim]:
        return [c for c in self.claims if c.kind is kind]

    def to_dict(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self.claims]


__all__ = ["ClaimGuard", "EvidenceStore", "new_claim_id"]
