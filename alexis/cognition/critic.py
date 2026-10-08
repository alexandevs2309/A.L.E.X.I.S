"""ModelCritic: capa de crítica independiente de modelos (§31).

PRINCIPIO (§69 rule 27): NO reemplaza al PlanValidator determinista ni a la
PolicyEngine. NUNCA puede aprobar por sí solo; solo observa el plan y aconseja. Un
plan rechazado por el PlanValidator nunca recibe un APPROVE crudo del crítico: la
invariante de validator_rejected se fuerza a REJECT desde dentro.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from alexis.models.provider import ModelOutcome, ModelRequest, ModelTask, ModelResponse


class Risk(BaseModel):
    severity: str = "low"  # low | medium | high | critical
    description: str = ""


class CritiqueReport(BaseModel):
    verdict: Literal["APPROVE", "ADVISE_CHANGES", "REJECT"] = "APPROVE"
    risks_identified: list[Risk] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    assumptions_challenged: list[str] = Field(default_factory=list)
    failure_modes: list[str] = Field(default_factory=list)
    #: Confianza del crítico 0..1: baja=posible falso positivo.
    confidence: float = 0.0
    #: Provenance TRl-estado (P1): REAL | DEGRADED | UNAVAILABLE.
    provenance: Literal["REAL", "DEGRADED", "UNAVAILABLE"] = "UNAVAILABLE"


def _extract_json(text: str) -> dict[str, Any]:
    import json
    import re

    if not text:
        return {}
    candidate = text.strip()
    try:
        dict_ = json.loads(candidate)
        if isinstance(dict_, dict):
            return dict_
    except Exception:
        pass
    match = re.search(r"\{.*\}", candidate, re.S)
    if match:
        return json.loads(match.group(0))
    return {}


def _serialize_plan(plan: Any) -> list[dict[str, Any]]:
    steps = getattr(plan, "steps", None) if plan is not None else []
    out: list[dict[str, Any]] = []
    for s in steps or []:
        out.append(
            {
                "id": getattr(s, "id", "?"),
                "capability": getattr(s, "capability", ""),
                "action": getattr(s, "action", ""),
                "risk": getattr(getattr(s, "risk", None), "value", str(getattr(s, "risk", ""))),
                "tool": getattr(s, "tool", "") or "",
                "description": getattr(s, "description", "") or "",
            }
        )
    return out


_CRITIC_SYSTEM = (
    "Eres un crítico técnico de ALEXIS: evalúa el plan contra el contexto, la evidencia "
    "disponible y los modos de fallo. Devuelve SOLO un JSON con las claves del esquema. "
    "No apruebes lo que no puedas verificar; si hay evidencia faltante, contradicción o "
    "modo de fallo plausible, usa ADVISE_CHANGES o REJECT."
)


def _critique_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "verdict": {"enum": ["APPROVE", "ADVISE_CHANGES", "REJECT"]},
            "risks_identified": {"type": "array", "items": {"type": "object", "properties": {"severity": {"type": "string"}, "description": {"type": "string"}}}},
            "missing_evidence": {"type": "array", "items": {"type": "string"}},
            "contradictions": {"type": "array", "items": {"type": "string"}},
            "assumptions_challenged": {"type": "array", "items": {"type": "string"}},
            "failure_modes": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
        },
        "required": ["verdict"],
    }


class ModelCritic:
    """Capa de crítica de planes. Informedial: nunca cambia el plan ni la policy."""

    def __init__(self, router, *, logger=None):
        self.router = router
        self.logger = logger

    async def critique(
        self,
        plan,
        context: dict[str, Any],
        evidence_store=None,
    ) -> CritiqueReport:
        if self.router is None:
            return CritiqueReport(provenance="UNAVAILABLE")

        evidence_ids: list[str] = []
        for claim in getattr(evidence_store, "claims", []) or []:
            cid = getattr(claim, "id", None)
            if cid:
                evidence_ids.append(str(cid))

        request = ModelRequest(
            task=ModelTask.CRITIQUE,
            system=_CRITIC_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Plan y contexto, en JSON. Evalúa: riesgos, evidencia faltante, "
                        "contradicciones, suposiciones no verificadas y modos de fallo.\n"
                        f"plan={_serialize_plan(plan)}\ncontext={context}\n"
                        f"evidence_ids={evidence_ids}"
                    ),
                }
            ],
            schema=_critique_schema(),
            max_tokens=1500,
            temperature=0.0,
        )
        try:
            response = await self.router.complete(request)
        except Exception as exc:  # noqa: BLE001 — crítica no puede tumbar la misión
            if self.logger:
                self.logger.warning("ModelCritic: modelo falló (%s)", exc)
            return CritiqueReport(provenance="UNAVAILABLE")

        if response.outcome is ModelOutcome.UNAVAILABLE:
            return CritiqueReport(provenance="UNAVAILABLE")
        if response.outcome is ModelOutcome.DEGRADED:
            # Un provider degradado nunca puede bloquear ni dejar de aprobar: vacío e
            # informativo. Evita que la crítica invente autoridad desde contingencia.
            return CritiqueReport(provenance="DEGRADED")

        data = response.data or _extract_json(response.text)
        try:
            report = CritiqueReport.model_validate(data)
        except Exception:  # noqa: BLE001
            if self.logger:
                self.logger.warning("ModelCritic: respuesta no parseable")
            return CritiqueReport(provenance="UNAVAILABLE")

        report.provenance = "REAL"
        # Invariante §69 rule 27: ni el crítico puede reescribir un plan ya rechazado
        # por el validator. El bloqueo ya es definitivo.
        if bool(context.get("validator_rejected")):
            report.verdict = "REJECT"
        return report

    def safe_advice(self, report: CritiqueReport) -> dict[str, Any]:
        """Retorna UNA copia consultable; nunca modifica el plan."""
        return {
            "verdict": report.verdict,
            "risks_identified": [risk.model_dump() for risk in report.risks_identified],
            "missing_evidence": list(report.missing_evidence),
            "contradictions": list(report.contradictions),
            "assumptions_challenged": list(report.assumptions_challenged),
            "failure_modes": list(report.failure_modes),
            "confidence": report.confidence,
            "provenance": report.provenance,
        }
