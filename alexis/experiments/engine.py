from dataclasses import dataclass


@dataclass
class Experiment:
    name: str
    hypothesis: str
    control: str
    treatment: str


class ExperimentEngine:
    async def compare(self, experiment: Experiment, control_result, treatment_result):
        return {
            "experiment": experiment.name,
            "hypothesis": experiment.hypothesis,
            "control": control_result,
            "treatment": treatment_result,
        }
