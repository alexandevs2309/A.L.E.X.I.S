from uuid import uuid4
from alexis.contracts import Goal, Mission, MissionEnvelope


class MissionEngine:
    def create(self, objective: str, envelope: MissionEnvelope) -> Mission:
        return Mission(
            id=str(uuid4()),
            goal=Goal(objective=objective),
            envelope=envelope,
        )

    def stop(self, mission: Mission, reason: str):
        mission.state = mission.state.STOPPED
        mission.results.append({"stopped": reason})
