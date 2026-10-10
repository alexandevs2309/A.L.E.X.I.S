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
    #: Vector de la consulta. Si viene, la recuperación ordena por distancia de coseno
    #: contra `observations.embedding` y cae a coincidencia de términos para lo que no
    #: tenga vector. `None` mantiene el comportamiento léxico de siempre.
    query_embedding: list[float] | None = None


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
    #: Similitud 0..1 calculada en PostgreSQL. `None` = este item no tiene vector y su
    #: puntuación viene de términos. La distinción importa: una fila sin embedding no es
    #: "poco relevante", es "no medida en la misma escala".
    semantic_score: float | None = None

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
            "semantic_score": self.semantic_score,
        }


@dataclass
class MemoryContext:
    items: list[MemoryItem] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    token_estimate: int = 0
    provider: str = "none"
    #: Hubo recuerdos relevantes que NO cabían en `limit` o `token_budget`. Sin esto, un
    #: contexto recortado es indistinguible de uno que no tenía nada más que dar, y el
    #: sistema no puede saber si está perdiendo contexto o no.
    truncated: bool = False

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
            "truncated": self.truncated,
        }
