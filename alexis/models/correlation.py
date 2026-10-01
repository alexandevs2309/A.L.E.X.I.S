"""CORE-05 — Contexto genérico de correlación para el routing de modelos.

El `ModelRouter` publica un evento `model.routed` con la procedencia completa de cada
decisión (provider, modelo resuelto, outcome, fallback, coste, latencia). Ese evento
llegaba sólo al bus y a la memoria del proceso: al reiniciar, se perdía. Para
persistirlo en `audit_log` hace falta saber **a qué misión pertenecía**, y eso no lo
sabía nadie.

Este módulo define el dato que resuelve el problema, y lo hace de la forma más
estrecha posible:

- **No conoce `Mission`.** Es un contenedor genérico de correlación. Quien lo llena
  decide qué es una misión; este módulo sólo transporta. Si mañana `correlation_id`
  pasa a ser un `trace_id`, el Router no cambia.
- **No se mete en `ModelRequest`.** Ese es el contrato con los *providers*; meter ahí
  la identidad de una misión los ataría al dominio y los obligaría a conocer el
  Concepto de misión.
- **No es un `ContextVar`.** La correlación viaja explícita en la llamada: si dos
  misiones se intercalan, cada una lleva la suya. Con estado ambiental el dato se
  perdería en silencio al cambiar de tarea o de hilo, y no se podría distinguir
  "no había misión" de "se perdió el contexto".

El transporte es un parámetro keyword-only de `ModelRouter.complete()`, con
`correlation=None` por defecto, de modo que **todos los llamadores existentes siguen
funcionando sin cambios**. El Router sólo copia este objeto al payload del evento,
antes de publicarlo; no interpreta su contenido.

Sobre `mission_id` inválido: este módulo no lo valida (no tiene acceso a la base, y no
le corresponde). La validación es del `AuditSink`, que escribe en `audit_log` y por
tanto es quien choca con la clave foránea. Aquí la regla es sólo de honestidad: un
`mission_id` ausente significa `kind="pre_mission"`, y nunca se rellena con un valor
inventado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: La llamada no venía de ninguna misión: es una operación legítimamente pre-misión
#: (por ejemplo, clasificar la intención de un turno, que ocurre antes de que exista
#: misión). Es un caso NORMAL, no un error, y se distingue de un `mission_id` inválido.
KIND_PRE_MISSION = "pre_mission"
#: La llamada viene de una ejecución de misión concreta.
KIND_MISSION = "mission"
#: La llamada no pertenece al ciclo cognitivo (por ejemplo, una tarea de fondo).
KIND_SYSTEM = "system"


@dataclass
class RoutingCorrelation:
    """Metadatos genéricos de correlación para UNA llamada al router.

    Deliberadamente opaco: quien lo usa decide qué significa cada campo, y el
    `ModelRouter` se limita a adjuntarlo al evento `model.routed`.

    `mission_id` es opcional a propósito. El campo que distingue "no hay misión" de
    "hay misión pero el id es inválido" NO es este (que puede estar ausente en ambos
    casos) sino `kind`: un `KIND_PRE_MISSION` con `mission_id=None` es coherente, y
    una incoherencia la detecta el `AuditSink` al escribir, porque la clave foránea de
    `audit_log` no acepta un id que no existe. Así el fallo de correlación es
    observable en lugar de disfrazarse de operación normal.
    """

    correlation_id: str | None = None
    kind: str = KIND_PRE_MISSION
    mission_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_pre_mission(self) -> bool:
        return self.kind == KIND_PRE_MISSION

    def to_dict(self) -> dict[str, Any]:
        """Forma serializada que viaja en el evento `model.routed`.

        `mission_id` se emite siempre (aunque sea `None`) para que el consumidor no
        tenga que adivinar si la ausencia significa "no había" o "se perdió": la
        respuesta está en `kind`, que va explícito.
        """
        return {
            "correlation_id": self.correlation_id,
            "kind": self.kind,
            "mission_id": self.mission_id,
            "metadata": dict(self.metadata),
        }


def for_mission(mission, **metadata: Any) -> RoutingCorrelation:
    """Correlación para una ejecución de misión.

    Toma el objeto `mission` porque quien llama lo tiene a mano, pero este helper NO
    importa `Mission`: sólo lee un identificador. Así el transporte sigue siendo
    genérico y el dominio no se filtra al Router.
    """
    mission_id = getattr(mission, "id", None)
    return RoutingCorrelation(
        correlation_id=mission_id,
        kind=KIND_MISSION,
        mission_id=mission_id,
        metadata=dict(metadata),
    )


def pre_mission(**metadata: Any) -> RoutingCorrelation:
    """Correlación para una llamada que ocurre antes de que exista misión."""
    return RoutingCorrelation(
        correlation_id=None,
        kind=KIND_PRE_MISSION,
        mission_id=None,
        metadata=dict(metadata),
    )


__all__ = [
    "KIND_MISSION",
    "KIND_PRE_MISSION",
    "KIND_SYSTEM",
    "RoutingCorrelation",
    "for_mission",
    "pre_mission",
]