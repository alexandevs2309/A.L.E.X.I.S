from uuid import uuid4
from alexis.contracts import Goal, Mission, MissionEnvelope


class MissionEngine:
    def create(
        self,
        objective: str,
        envelope: MissionEnvelope,
        success_criteria: list[str] | None = None,
    ) -> Mission:
        """P0 §5.1 — los criterios que produce el Intent Engine llegan al Goal.

        Antes el Goal se construía con ``Goal(objective=objective)`` y los criterios se
        perdían aquí: ninguna capa posterior los leía, así que el objetivo nunca pudo
        verificarse contra nada (docs/P0-COGNITIVE-CORE-GAP-ANALYSIS.md §4). El
        parámetro es opcional para no romper los call sites existentes y la lista se
        copia para que el Goal no dependa del Intent que la produjo.
        """
        return Mission(
            id=str(uuid4()),
            goal=Goal(
                objective=objective,
                success_criteria=list(success_criteria or []),
            ),
            envelope=envelope,
        )

    def stop(self, mission: Mission, reason: str):
        mission.state = mission.state.STOPPED
        mission.results.append({"stopped": reason})
