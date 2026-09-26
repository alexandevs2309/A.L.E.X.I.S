"""Ayudas de test para §5.5: completar una misión solo con verificación real.

Un test que necesita una misión en `COMPLETED` tiene que demostrar su objetivo de verdad:
un archivo real observado por una tool real y un criterio que ese hecho satisfaga. Estas
ayudas hacen exactamente eso, sin atajos: si el test no puede demostrar su objetivo, no
puede esperar `COMPLETED`.
"""

from alexis.autonomy.goal_state import settle
from alexis.cognition.goal_verification import GoalVerifier
from alexis.contracts import ExecutionResult, Observation, PlanStep, RiskLevel
from alexis.world.model import WorldModel


def world_with_file(name: str, exists: bool = True, size: int = 10) -> WorldModel:
    """WorldModel con la observación que una tool real habría dejado por ese archivo."""
    world = WorldModel()
    step = PlanStep("observar", "observar", "research", RiskLevel.LOW, "executor", capability="fs.stat")
    result = ExecutionResult(
        success=exists,
        output={"path": name, "exists": exists, "size": size if exists else 0},
        observations=[Observation("tool.fs.stat", {"path": name}, trusted=True)],
    )
    world.observe_execution(step, result)
    return world


def complete_mission(mission, world=None):
    """Verifica el objetivo de la misión y aplica el estado que resulte (`settle`).

    Sin criterios no se puede verificar nada, y entonces devuelve el estado honesto
    (`NEEDS_VERIFICATION`). No es un atajo: es el mismo camino que usa el runtime.
    """
    verifier = GoalVerifier(world=world if world is not None else WorldModel())
    return settle(mission, verifier.verify(mission)), verifier


def mission_state_after_completion(mission, world=None):
    return complete_mission(mission, world)[0]
