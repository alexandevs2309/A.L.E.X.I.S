from dataclasses import dataclass


@dataclass
class Confidence:
    score: float
    reasons: list[str]
    uncertainties: list[str]


class MetaCognition:
    def assess(self, evidence_count: int, independent_checks: int, assumptions: int) -> Confidence:
        score = min(1.0, 0.35 + 0.15 * evidence_count + 0.15 * independent_checks - 0.10 * assumptions)
        return Confidence(
            score=max(0.0, score),
            reasons=[f"{evidence_count} evidence items", f"{independent_checks} independent checks"],
            uncertainties=[f"{assumptions} explicit assumptions"] if assumptions else [],
        )
