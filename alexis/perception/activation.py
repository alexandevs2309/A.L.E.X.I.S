"""Semántica de activación de ALEXIS: la palmada NO decide acciones.

La doble palmada es un evento de percepción más (como un mensaje de texto o un
wake word). El "cerebro" (planner → policy → executor → verifier) interpreta la
activación como una misión de saludo y espera de instrucciones. Este módulo solo
declara el marcador de objetivo y la respuesta; ningún flujo de JARVIS aquí.
"""

from __future__ import annotations

ACTIVATION_OBJECTIVE = "El usuario pidió atención con una doble palmada (activación)."

ACTIVATION_MARKER = "doble palmada (activación)"


def is_activation_objective(objective: str) -> bool:
    """True solo si el objetivo fue GENERADO por el orchestrador ante una activación.

    No se activa con frases sueltas del usuario: requiere el marcador interno
    que ALEXIS pone al crear la misión de saludo tras un clap/wakeword."""
    low = (objective or "").lower()
    return ACTIVATION_MARKER in low


WELCOME_REPLY = (
    "¿En qué te ayudo? Puedo leer y crear archivos del workspace, "
    "y ejecutar tareas que autorices."
)


def activation_reply(objective: str) -> str:
    return WELCOME_REPLY