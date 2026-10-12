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

    def pause(self, mission: Mission, reason: str):
        """Pausa: la misión sigue viva pero el bucle debe soltarla.

        Distinta de `stop` en eso: `stop` la termina, `pause` la deja reanudable. Sin
        esa diferencia no había forma de decir "para un momento" — sólo "sigue" o
        "muere". El motivo va al contexto y no sólo a `results` porque el bucle, al
        soltar la misión, necesita saber por qué lo hizo y eso tiene que sobrevivir a un
        reinicio.
        """
        mission.state = mission.state.PAUSE
        mission.context["paused_reason"] = reason
        mission.results.append({"paused": reason})
        return mission

    def resume(self, mission: Mission):
        """Reanuda una misión pausada. Devuelve `None` si no estaba pausada.

        Vuelve a `PENDING`, no a `RUNNING`: lo que se reencola es la MÍSICA y es el
        worker quien la marca `running` cuando realmente la empieza. Ponerla `running`
        aquí sería mentir sobre lo que está pasando ahora mismo.
        """
        if mission.state is not mission.state.PAUSE:
            return None
        mission.state = mission.state.PENDING
        mission.context.pop("paused_reason", None)
        mission.results.append({"resumed": True})
        return mission
