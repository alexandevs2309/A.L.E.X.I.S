"""Identidad de un paso, y a qué se ata una aprobación humana.

Existe por una razón concreta: `approved_step_ids` vincula el «sí» de una persona a un
paso **por su identificador**, así que el identificador *es* la frontera de autorización.
Si dos pasos distintos comparten identidad, uno ejecuta con la aprobación del otro.

El identificador tiene que cumplir tres cosas a la vez:

1. **Estable**: el mismo paso, rebuilding sus veces, conserva su identidad. Sin esto, una
   aprobación válida dejaría de servir y la acción pediría permiso otra vez.
2. **Único**: dos pasos concretos distintos nunca comparten identidad. Un replan crea un
   paso nuevo; si hereda la del anterior, la aprobación viaja con él.
3. **Única fuente de verdad**: el paso que se genera y el que se comprueba deben pasar por
   AQUÍ. Es lo que evita que dos constantes distintos (`"cognitive"` y `"cognitive-step"`)
   sereonviadas y la aprobación nunca se satisfaga.

No es un generador de ids aleatorios a propósito: una identidad legible vale más cuando
hay que ir a buscarla en un log o en una auditoría.
"""

from __future__ import annotations

from typing import Any, Iterable

#: Clave del contexto de misión donde se registran las identidades ya gastadas.
CLAVE_IDENTIDADES = "step_identities_used"

#: Identidad base cuando ni el modelo ni el plan dan una. Deliberadamente fija: el
#: desambiguado de abajo es lo que garantiza que sea única, no un número de iteración.
IDENTIDAD_POR_DEFECTO = "cognitive-step"


def identidades_usadas(mission: Any) -> set[str]:
    """Las identidades que esta misión ya consumió, en cualquier momento de su vida.

    Se leen del CONTEXTO de la misión y no de la instancia del runtime a propósito: la
    aprobación se persiste en la misión, así que su frontera también tiene que persistir.
    Con un contador en memoria, un reinicio lo pondría a cero y el siguiente paso volvería
    a la identidad inicial —que ya está aprobada—.
    """
    context = getattr(mission, "context", None) or {}
    return set(context.get(CLAVE_IDENTIDADES) or [])


def registrar_identidad(mission: Any, identidad: str) -> str:
    """Registra una identidad como usada y la devuelve.

    Idempotente: registrar dos veces la misma no la duplica. Sin misión no hay contexto
    donde persistirla, y en ese caso la identidad se devuelve tal cual — elruntime sin
    misión no tiene nada que reiniciar.
    """
    context = getattr(mission, "context", None)
    if context is None:
        return identidad
    usadas = identidades_usadas(mission)
    usadas.add(identidad)
    context[CLAVE_IDENTIDADES] = sorted(usadas)
    return identidad


def identidad_de_paso(candidata: "str | None", ya_usadas: Iterable[str]) -> str:
    """`(candidata, ya_usadas) -> identidad única de ESTE paso`.

    Es pura: no toca la misión. Quien llama decide si registrar el resultado, y así el
    mismo paso se resuelve igual cada vez hasta que se registra.

    - Sin candidata → `IDENTIDAD_POR_DEFECTO`.
    - Candidata libre → se respeta tal cual. **No se renumera nada que no choque**: un plan
      que da ids propios los conserva, y con ellos su historial de aprobaciones.
    - Candidata repetida → sufijo `#2`, `#3`, … mientras no esté ocupado.
    """
    base = (candidata or "").strip() or IDENTIDAD_POR_DEFECTO
    ocupadas = set(ya_usadas or ())
    if base not in ocupadas:
        return base
    n = 2
    while f"{base}#{n}" in ocupadas:
        n += 1
    return f"{base}#{n}"


def nueva_identidad(mission: Any, candidata: "str | None" = None) -> str:
    """Resuelve la identidad de un paso nuevo y la registra como usada.

    Es la puerta que usa el runtime al CONSTRUIR un paso. Al registrar en el mismo acto,
    el siguiente paso que pida la misma candidata sale desambiguado en vez de repetirse.
    """
    identidad = identidad_de_paso(candidata, identidades_usadas(mission))
    return registrar_identidad(mission, identidad)


__all__ = [
    "CLAVE_IDENTIDADES",
    "IDENTIDAD_POR_DEFECTO",
    "identidad_de_paso",
    "identidades_usadas",
    "nueva_identidad",
    "registrar_identidad",
]
