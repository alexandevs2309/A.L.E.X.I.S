"""Qué runtime ejecuta el demo: el CognitiveRuntime o el recorrido legacy del plan.

Vive en su propio módulo, y no como una línea dentro de `server.py`, por dos razones
concretas:

1. `apps/demo/server.py` compone toda la app al importarse (puerto, modelo, PostgreSQL),
   así que no se puede importar en un test. La decisión de runtime es exactamente la que
   más necesita test, porque su DEFAULT es lo que decide qué centre de decisión tiene
   ALEXIS en producción.
2. La decisión tiene TRES estados, no dos: variable ausente, "0" explícito y "1"
   explícito. Con un `os.environ.get(..., "0") == "1"` los tres colapsan en una sola
   comparación y el default queda escondido dentro de un literal. Aquí están nombrados.

La regla es deliberadamente asimétrica. Lo que requiere petición EXPLÍCITA es el legacy:
es decir el camino anterior, y volver a él se pide a nombre. El default es el
CognitiveRuntime, así que un valor no reconocido no otorga nada nuevo: cae al default y
avisa, en vez de concederse por accidente una autoridad que nadie pidió.

No hay efectos secundarios: esta función no lee ficheros, no toca la red y no muta el
entorno. `env` es inyectable para poder probarla sin `monkeypatch`.
"""

import logging
import os
from typing import Mapping

LOGGER = logging.getLogger("alexis.demo.runtime_flags")

ALEXIS_COGNITIVE_ENV = "ALEXIS_COGNITIVE"

#: Valores que piden el runtime legacy de forma explícita. Son los únicos que pueden
#: apartarse del default.
LEGACY_VALUES = frozenset({"0", "false", "no", "off", "legacy"})

#: Valores que piden el CognitiveRuntime de forma explícita.
COGNITIVE_VALUES = frozenset({"1", "true", "yes", "on", "cognitive"})

#: El camino por defecto. El CognitiveRuntime es el centro de decisión principal; el
#: legacy es el camino antiguo, y por eso necesita que se le pida.
DEFAULT_IS_COGNITIVE = True


def resolve_cognitive_runtime(env: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """Devuelve `(usar_cognitive_runtime, motivo)`.

    Determinista y sin efectos: el mismo entorno produce siempre la misma respuesta, y
    llamarla dos veces no cambia nada.
    """
    source = os.environ if env is None else env
    raw = source.get(ALEXIS_COGNITIVE_ENV)
    if raw is None:
        return DEFAULT_IS_COGNITIVE, (
            f"{ALEXIS_COGNITIVE_ENV} sin definir: CognitiveRuntime es el camino principal"
        )
    value = str(raw).strip().lower()
    if value in LEGACY_VALUES:
        return False, f"{ALEXIS_COGNITIVE_ENV}={raw!r}: legacy solicitado explícitamente"
    if value in COGNITIVE_VALUES:
        return True, f"{ALEXIS_COGNITIVE_ENV}={raw!r}: CognitiveRuntime solicitado explícitamente"
    # Valor no reconocido. No es una autorización: se aplica el default y se avisa, para
    # que un typo en la configuración no pase desapercibido.
    LOGGER.warning(
        "%s=%r no es un valor reconocido (legacy: %s | cognitive: %s); se usa el default, "
        "CognitiveRuntime. Para el legacy hay que pedirlo explícitamente.",
        ALEXIS_COGNITIVE_ENV, raw,
        ", ".join(sorted(LEGACY_VALUES)), ", ".join(sorted(COGNITIVE_VALUES)),
    )
    return DEFAULT_IS_COGNITIVE, (
        f"{ALEXIS_COGNITIVE_ENV}={raw!r} no reconocido: se usa el default (CognitiveRuntime)"
    )


__all__ = [
    "ALEXIS_COGNITIVE_ENV",
    "COGNITIVE_VALUES",
    "DEFAULT_IS_COGNITIVE",
    "LEGACY_VALUES",
    "resolve_cognitive_runtime",
]
