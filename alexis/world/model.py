"""World Model operacional: lo que ALEXIS sabe del mundo en el que actúa.

No es un almacén: existe para que el runtime **decida con conocimiento del mundo**, no
solo con el texto del objetivo. Por eso tiene tres capacidades:

1. **Observación**: `observe_execution()` extrae entidades de lo que una herramienta
   devolvió de verdad (un `fs.stat` que dice `exists=false` es evidencia de que el
   archivo no existe, no una suposición).
2. **Consulta**: `query()`, `for_objective()`, `neighbors()`, `known_path()`.
3. **Relaciones**: `relate()` + `neighbors()` para el grafo de dependencias.

Cada entidad conserva su `source` y cuántas veces se ha observado. Una entidad
construida desde la salida de una herramienta es EVIDENCIA, no un hecho verificado:
por eso `confidence` es 0.7 y no 1.0, y por eso el runtime la usa para **preguntar o
corregir**, no para afirmar.

El API previo (`upsert`/`get`/`snapshot`) se conserva: el demo ya lo usa.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from alexis.memory.provider import terms

FILE = "file"
TOOL = "tool"
SERVICE = "service"
PROJECT = "project"
SYSTEM = "system"
TASK = "task"
RESOURCE = "resource"
DEPENDENCY = "dependency"

#: Capabilities que fallan de forma previsible si el mundo ya observó que la ruta no
#: existe. `fs.write` NO está: crear lo que falta es exactamente su trabajo.
_FAILS_ON_MISSING = {"fs.read", "fs.stat", "research.filesystem", "fs.remove"}


@dataclass
class WorldEntity:
    id: str
    kind: str
    name: str
    attributes: dict = field(default_factory=dict)
    source: str = "declared"
    confidence: float = 1.0
    mission_id: str | None = None
    observations: int = 1
    last_seen: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "attributes": dict(self.attributes),
            "source": self.source,
            "confidence": self.confidence,
            "mission_id": self.mission_id,
            "observations": self.observations,
        }

    def to_line(self) -> str:
        details = ", ".join(f"{k}={v}" for k, v in sorted(self.attributes.items()) if v is not None)
        return f"{self.kind}:{self.name}" + (f" ({details})" if details else "")


class WorldModel:
    def __init__(self):
        self.entities: dict[str, WorldEntity] = {}
        self.edges: list[tuple[str, str, str]] = []

    # ------------------------------------------------------------------ #
    # API previo (conservado)
    # ------------------------------------------------------------------ #

    def upsert(self, entity: WorldEntity) -> WorldEntity:
        previous = self.entities.get(entity.id)
        if previous is not None:
            entity.attributes = {**previous.attributes, **entity.attributes}
            entity.observations = previous.observations + 1
        self.entities[entity.id] = entity
        return entity

    def get(self, entity_id: str):
        return self.entities.get(entity_id)

    def snapshot(self):
        return list(self.entities.values())

    # ------------------------------------------------------------------ #
    # Consulta
    # ------------------------------------------------------------------ #

    def query(self, *, kind: str | None = None, text: str | None = None, limit: int = 20) -> list[WorldEntity]:
        items = list(self.entities.values())
        if kind:
            items = [e for e in items if e.kind == kind]
        if text:
            query_terms = terms(text)
            scored = []
            for entity in items:
                haystack = terms(f"{entity.id} {entity.name} {' '.join(map(str, entity.attributes.values()))}")
                shared = query_terms & haystack
                if shared:
                    scored.append((len(shared) / max(1, len(query_terms)), entity))
            scored.sort(key=lambda pair: pair[0], reverse=True)
            items = [entity for _, entity in scored]
        return items[:limit]

    def for_objective(self, objective: str, limit: int = 6) -> list[WorldEntity]:
        return self.query(text=objective, limit=limit)

    def known_path(self, path: str) -> WorldEntity | None:
        if not path:
            return None
        return self.entities.get(f"{FILE}:{path}")

    def missing_paths(self, paths) -> list[str]:
        """Rutas que el mundo ya observó como inexistentes."""
        missing = []
        for path in paths or []:
            entity = self.known_path(path)
            if entity is not None and entity.attributes.get("exists") is False:
                missing.append(path)
        return missing

    def neighbors(self, entity_id: str) -> list[WorldEntity]:
        related = {
            child if parent == entity_id else parent
            for parent, child, _relation in self.edges
            if parent == entity_id or child == entity_id
        }
        return [self.entities[eid] for eid in sorted(related) if eid in self.entities]

    def dependencies(self, entity_id: str) -> list[WorldEntity]:
        return [
            self.entities[child]
            for parent, child, relation in self.edges
            if parent == entity_id and relation == "depends_on" and child in self.entities
        ]

    def relate(self, parent_id: str, child_id: str, relation: str = "depends_on") -> None:
        edge = (parent_id, child_id, relation)
        if edge not in self.edges:
            self.edges.append(edge)

    def to_prompt_lines(self, limit: int = 6) -> list[str]:
        return [f"- {entity.to_line()} [evidencia de {entity.source}]" for entity in self.snapshot()[:limit]]

    # ------------------------------------------------------------------ #
    # Observación: la parte que lo llena de realidad
    # ------------------------------------------------------------------ #

    def observe_execution(self, step, result, mission=None) -> list[WorldEntity]:
        """Registra en el mundo lo que una ejecución REAL devolvió.

        Solo extrae hechos que la herramienta afirma explícitamente (ruta, existencia,
        tamaño). No interpreta intenciones ni inventa entidades.
        """
        observed: list[WorldEntity] = []
        output = getattr(result, "output", None)
        if not isinstance(output, dict):
            return observed
        success = bool(getattr(result, "success", False))

        path = output.get("path")
        if isinstance(path, str) and path:
            attributes: dict[str, Any] = {}
            if "exists" in output:
                attributes["exists"] = bool(output["exists"])
            elif success:
                attributes["exists"] = True
            if output.get("size") is not None:
                attributes["size"] = output["size"]
            if output.get("truncated") is not None:
                attributes["truncated"] = bool(output["truncated"])
            if not success and not attributes.get("exists"):
                attributes["exists"] = False
            observed.append(
                self.upsert(
                    WorldEntity(
                        id=f"{FILE}:{path}",
                        kind=FILE,
                        name=path,
                        attributes=attributes,
                        source=f"tool:{getattr(step, 'capability', None) or 'executor'}",
                        confidence=0.7,
                        mission_id=getattr(mission, "id", None),
                    )
                )
            )
        return observed

    def declare_tool(self, name: str, attributes: dict | None = None) -> WorldEntity:
        return self.upsert(
            WorldEntity(
                id=f"{TOOL}:{name}",
                kind=TOOL,
                name=name,
                attributes=dict(attributes or {}),
                source="registry",
                confidence=1.0,
            )
        )

    def fails_on_missing(self, capability: str | None) -> bool:
        """True si esta capability no puede funcionar sobre una ruta observada como ausente."""
        return capability in _FAILS_ON_MISSING


__all__ = [
    "WorldEntity",
    "WorldModel",
    "FILE",
    "TOOL",
    "SERVICE",
    "PROJECT",
    "SYSTEM",
    "TASK",
    "RESOURCE",
    "DEPENDENCY",
]
