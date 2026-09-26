"""Sistema de aprendizaje (P0 §5.6.4).

`ExperienceLearner` ya no es un `append` a una lista en memoria: delega en la
implementación real (`alexis.learning.experience`) y expone lo aprendido a través del
mismo contrato. `self.experiences` se conserva por compatibilidad con los tests que lo
leen, pero la fuente de verdad es `records`.
"""

from abc import ABC, abstractmethod

from alexis.learning.experience import Experience, LearningBoundary, VerifiedExperience
from alexis.learning.reflection import build_reflection
from alexis.cognition.state import KnowledgeState, Verdict


class LearningSystem(ABC):
    @abstractmethod
    async def record_experience(self, mission, verification): ...


class ExperienceLearner(LearningSystem):
    """Registra la experiencia de una misión y la frontera de aprendizaje.

    Sólo aprende de misiones cuyo objetivo está verificado: una herramienta que fue bien
    no es un objetivo conseguido (`ACTION SUCCESS -> OBJECTIVE SUCCESS`).
    """

    def __init__(self):
        self.experiences: list[dict] = []
        self.records: list[VerifiedExperience] = []
        self.boundary = LearningBoundary()

    async def record_experience(self, mission, verification):
        goal_verified = bool(getattr(verification, "verified", False))
        knowledge = self._knowledge(mission)
        reflection = build_reflection(mission, knowledge, verification)
        experience = Experience(
            mission_id=str(getattr(mission, "id", "") or ""),
            objective=str(getattr(getattr(mission, "goal", None), "objective", "") or ""),
            verdict=str(getattr(knowledge, "last_verdict", "") or ""),
            outcome=str(getattr(getattr(mission, "state", None), "value", "") or ""),
            goal_verified=goal_verified,
            reflection=reflection.to_dict(),
        )
        verified = self.boundary.evaluate(experience, reflection)
        self.records.append(verified)
        self.experiences.append(
            {
                "mission": mission.id,
                "objective": experience.objective,
                "passed": goal_verified,
                "confidence": reflection.confidence,
                "evidence": experience.evidence_refs,
                "verdict": experience.verdict,
                "quality": verified.quality,
                "lesson": verified.lesson,
            }
        )
        return verified

    def lessons(self) -> list[str]:
        """Lecciones que la frontera autorizó enseñar. Lo demás no sale de aquí."""
        return [r.lesson for r in self.records if r.can_teach() and r.lesson]

    @staticmethod
    def _knowledge(mission) -> KnowledgeState:
        raw = (getattr(mission, "context", {}) or {}).get("knowledge")
        if raw:
            return KnowledgeState.from_dict(raw, str(getattr(getattr(mission, "goal", None), "objective", "")))
        knowledge = KnowledgeState(objective=str(getattr(getattr(mission, "goal", None), "objective", "")))
        knowledge.last_verdict = Verdict.INSUFFICIENT_EVIDENCE.value
        return knowledge
