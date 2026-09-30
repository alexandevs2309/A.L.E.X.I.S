"""Qué runtime ejecuta ALEXIS: el CognitiveRuntime, o nada.

CORE-03 cambió la respuesta. Antes este módulo resolvía una decisión de DOS caminos
(`CognitiveRuntime` frente al recorrido legacy del plan) y la rama legacy era
ejecutable. Hoy la pregunta es distinta: el runtime oficial es el `CognitiveRuntime`, y
pedir el recorrido antiguo no activa una alternativa —aborta.

Vive en su propio módulo, y no como una línea dentro de `app.py`, por una razón
concreta: `app.py` es importable (eso es lo que permite convertirlo en la aplicación
oficial), así que la decisión se puede probar de verdad sin levantar la app entera.

La regla es deliberadamente asimétrica. Lo que requiere petición EXPLÍCITA es el
CognitiveRuntime; lo que no se puede pedir es el legacy. Y un valor no reconocido no
concede nada: cae al default y avisa, para que un typo en la configuración no pase
desapercibido.

No hay efectos secundarios: estas funciones no leen ficheros, no tocan la red y no
mutan el entorno. `env` es inyectable para poder probarlas sin `monkeypatch`.
"""

import logging
import os
from typing import Mapping

LOGGER = logging.getLogger("alexis.demo.runtime_flags")

ALEXIS_COGNITIVE_ENV = "ALEXIS_COGNITIVE"

#: Valores que piden el runtime legacy de forma explícita. CORE-03 los convierte en
#: PROHIBIDOS: ver `LegacyRuntimeProhibited`.
LEGACY_VALUES = frozenset({"0", "false", "no", "off", "legacy"})

#: Valores que piden el CognitiveRuntime de forma explícita.
COGNITIVE_VALUES = frozenset({"1", "true", "yes", "on", "cognitive"})

#: El camino por defecto. El CognitiveRuntime es el centro de decisión principal.
DEFAULT_IS_COGNITIVE = True


class LegacyRuntimeProhibited(RuntimeError):
    """Se pidió el recorrido legacy, y ya no existe como camino ejecutable.

    CORE-03 cerró la segunda ruta de ejecución. El motivo concreto por el que esto no
    es un capricho: el recorrido legacy deja `RUNTIME.cognitive` a `None` y nadie
    inyecta un `GoalVerifier` en su lugar, así que `_verify_goal()` devuelve `None`,
    `settle()` no confirma nada y **ninguna misión puede terminar en COMPLETED**. Es
    decir, el flag no elegía otro modo de funcionar: desactivaba la garantía de
    CORE-01 sin avisar.

    Por eso el error es explícito y el arranque se detiene, en vez de caer a un
    runtime más débil como si nada.
    """


def resolve_cognitive_runtime(env: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """Devuelve `(usar_cognitive_runtime, motivo)`.

    Determinista y sin efectos: el mismo entorno produce siempre la misma respuesta, y
    llamarla dos veces no cambia nada.

    Levanta `LegacyRuntimeProhibited` si se pide el recorrido antiguo: el runtime
    oficial es el CognitiveRuntime, y no hay un camino alternativo que lo ejecute.
    """
    source = os.environ if env is None else env
    raw = source.get(ALEXIS_COGNITIVE_ENV)
    if raw is None:
        return DEFAULT_IS_COGNITIVE, (
            f"{ALEXIS_COGNITIVE_ENV} sin definir: CognitiveRuntime es el camino principal"
        )
    value = str(raw).strip().lower()
    if value in LEGACY_VALUES:
        raise LegacyRuntimeProhibited(
            f"{ALEXIS_COGNITIVE_ENV}={raw!r} ya no es una opción: el recorrido legacy "
            "se ha retirado (CORE-03). No puede verificar objetivos —sin CognitiveRuntime "
            "no hay GoalVerifier y `settle()` nunca autoriza un COMPLETED—, así que "
            f"arrancar con él dejaría a ALEXIS sin poder cerrar ninguna misión. Deja la "
            f"variable sin definir o ponla a '1'."
        )
    if value in COGNITIVE_VALUES:
        return True, f"{ALEXIS_COGNITIVE_ENV}={raw!r}: CognitiveRuntime solicitado explícitamente"
    # Valor no reconocido. No es una autorización: se aplica el default y se avisa, para
    # que un typo en la configuración no pase desapercibido.
    LOGGER.warning(
        "%s=%r no es un valor reconocido (reconocido: %s); se usa el default, "
        "CognitiveRuntime.",
        ALEXIS_COGNITIVE_ENV, raw, ", ".join(sorted(COGNITIVE_VALUES)),
    )
    return DEFAULT_IS_COGNITIVE, (
        f"{ALEXIS_COGNITIVE_ENV}={raw!r} no reconocido: se usa el default (CognitiveRuntime)"
    )


__all__ = [
    "ALEXIS_COGNITIVE_ENV",
    "COGNITIVE_VALUES",
    "DEFAULT_IS_COGNITIVE",
    "LEGACY_VALUES",
    "LegacyRuntimeProhibited",
    "resolve_cognitive_runtime",
]
