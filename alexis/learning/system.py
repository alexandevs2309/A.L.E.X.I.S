from abc import ABC, abstractmethod


class LearningSystem(ABC):
    @abstractmethod
    async def record_experience(self, mission, verification): ...


class ExperienceLearner(LearningSystem):
    def __init__(self):
        self.experiences = []

    async def record_experience(self, mission, verification):
        self.experiences.append({
            "mission": mission.id,
            "objective": mission.goal.objective,
            "passed": verification.passed,
            "confidence": verification.confidence,
            "evidence": verification.evidence,
        })
