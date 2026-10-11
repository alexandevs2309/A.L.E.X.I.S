"""Estructuras del User Model.

El detalle que las hace útiles es que `UserPreferences` conserva `source`: lo que el
usuario dijo y lo que ALEXIS dedujo se guardan en la misma fila, y ese campo es lo que
impide presentar la segunda como si fuera la primera.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Categorías cerradas. La base las impone con un CHECK; aquí se documentan.
CATEGORIES: tuple[str, ...] = ("project", "style", "approval", "context")
#: `explicit` lo dijo el usuario, `inferred` lo dedujo ALEXIS, `default` es de fábrica.
SOURCES: tuple[str, ...] = ("explicit", "inferred", "default")


@dataclass(frozen=True)
class UserPreferences:
    """Una preferencia persistida.

    `id` es determinista (`"category:key"`), así que el `upsert` resuelve por clave
    primaria sin preguntar aparte por `category`/`key`. La confianza se acota en lugar de
    rechazarse: fuera de [0, 1] no es "mucha confianza", es un dato corrupto que un `MAX()`
    elegiría como la entrada más fiable; pero un `confidence=7` en la API debe guardarse,
    no perder lo que el usuario quiso decir.
    """

    category: str
    key: str
    value: Any
    confidence: float = 1.0
    source: str | None = None
    id: str = ""
    created_at: Any = None
    updated_at: Any = None

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(self, "id", f"{self.category}:{self.key}")
        object.__setattr__(self, "confidence", max(0.0, min(1.0, float(self.confidence))))

    @property
    def active(self) -> bool:
        """Si es un proyecto activo. Se lee del VALOR: el estado cambia, la categoría no."""
        return isinstance(self.value, dict) and self.value.get("active") is True

    def to_dict(self) -> dict:
        return {"id": self.id, "category": self.category, "key": self.key,
                "value": self.value, "confidence": self.confidence, "source": self.source}


@dataclass
class UserContext:
    """Proyección del User Model para SelfModel, COGNICIÓN y la API.

    Es una COPIA, no una vista: `active_projects` viaja al contexto de decisión y a la
    API, y si compartiera referencia con el repositorio un `append` de quien lo consume
    mutaría el estado sin pasar por una escritura.
    """

    active_projects: list[str] = field(default_factory=list)
    recent_decisions: list[dict] = field(default_factory=list)
    technical_style: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"active_projects": list(self.active_projects),
                "recent_decisions": list(self.recent_decisions),
                "technical_style": dict(self.technical_style)}