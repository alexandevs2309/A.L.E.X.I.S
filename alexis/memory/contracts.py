"""Contratos de Memory Provider (F2.0 — docs/COGNITIVE-CORE-F2.md §6).

El Core pregunta «¿qué contexto relevante tengo para esta solicitud?» y recibe
`MemoryContext` estructurado. Los providers concretos (InMemory, Postgres, Obsidian)
se implementan en F2.2; aquí solo los datos.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MemoryQuery:
    text: str
    mission_id: str | None = None
    kinds: list[str] = field(
        default_factory=lambda: ["observations", "episodic", "semantic"]
    )
    limit: int = 10
    token_budget: int = 4000


@dataclass
class MemoryItem:
    id: str
    kind: str  # observations | episodic | semantic | procedural
    content: str
    source: str
    score: float = 0.0
    mission_id: str | None = None
    created_at: str | None = None
    trusted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "content": self.content,
            "source": self.source,
            "score": self.score,
            "mission_id": self.mission_id,
            "created_at": self.created_at,
            "trusted": self.trusted,
        }


@dataclass
class MemoryContext:
    items: list[MemoryItem] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    token_estimate: int = 0
    provider: str = "none"

    def as_prompt_lines(self) -> list[str]:
        """Contexto como líneas de datos para el prompt.

        R3: cada línea pasa por el filtro de `alexis.security.untrusted`, de modo que un
        item no confiable queda envuelto en `[[UNTRUSTED_DATA …]]` y con sus
        metainstrucciones neutralizadas. Contenido no confiable = datos, no
        instrucciones (`docs/AUTONOMY-V0.5-CAPABILITIES.md §9.4`).
        """
        from alexis.security.untrusted import sanitize_untrusted

        return [
            sanitize_untrusted(item.content, source=f"memory:{item.source}")
            for item in self.items
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [i.to_dict() for i in self.items],
            "sources": list(self.sources),
            "token_estimate": self.token_estimate,
            "provider": self.provider,
        }
