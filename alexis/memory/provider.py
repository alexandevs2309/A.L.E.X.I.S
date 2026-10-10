"""Fase 2, incremento 2: MemoryProvider mínimo con recuperación contextual REAL.

Por qué solo lectura: las escrituras de observación ya las hace el runtime
(`AlexisRuntime._persist_observations` → `MemoryStore` + `ObservationRepository`). Este
proveedor no duplica ese camino: lee lo que ya está guardado y lo devuelve como
contexto relevante para la decisión.

`InMemoryMemory.recall` no servía para nada (ignoraba la query y devolvía los últimos
N elementos). Aquí la recuperación es real: puntúa por términos compartidos y descarta
lo que no tiene relación con el objetivo.

El contenido no confiable se entrega como DATO marcado, nunca como instrucción: el
runtime usa `MemoryContext.as_prompt_lines()`, que pasa por
`alexis.security.untrusted.sanitize_untrusted`.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

from alexis.memory.contracts import MemoryContext, MemoryItem, MemoryQuery

_TOKEN = re.compile(r"[a-záéíóúñü0-9_./-]{3,}", re.IGNORECASE)

_STOPWORDS = frozenset(
    {
        "the", "and", "for", "con", "los", "las", "una", "uno", "que", "del",
        "por", "para", "este", "esta", "eso", "sus", "como", "más", "ale",
    }
)


def terms(text: str) -> set[str]:
    """Términos significativos de un texto, en minúsculas y sin palabras vacías."""
    return {t.lower() for t in _TOKEN.findall(text or "")} - _STOPWORDS


def as_text(content: Any, limit: int = 400) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:limit]
    try:
        return json.dumps(content, ensure_ascii=False, sort_keys=True)[:limit]
    except (TypeError, ValueError):
        return str(content)[:limit]


def relevance(query_terms: set[str], text: str) -> float:
    """Puntuación 0..1: fracción de términos de la consulta presentes en el texto."""
    if not query_terms:
        return 0.0
    item_terms = terms(text)
    if not item_terms:
        return 0.0
    shared = query_terms & item_terms
    return len(shared) / len(query_terms)


class MemoryProvider(ABC):
    """Responde «qué contexto relevante tengo para esto»."""

    id = "memory"
    available = True

    @abstractmethod
    async def retrieve(self, query: MemoryQuery) -> MemoryContext: ...


def _in_scope(item: MemoryItem, mission_id: str | None) -> bool:
    """¿Este recuerdo pertenece a la consulta?

    `MemoryQuery.mission_id` es un límite de aislamiento, no una pista de búsqueda. Con
    una misión indicada, sólo valen los recuerdos de ESA misión. Antes el campo no se
    usaba: una consulta de la misión `MIA` devolvía observaciones de `MISION-SECRETA`, y
    el `PostgresMemoryProvider` además traía las 200 filas más recientes sin filtrar nada.

    Para una búsqueda global el llamante deja `mission_id = None`, que es explícito.
    """
    if mission_id is None:
        return True
    return item.mission_id == mission_id


def _select(items: list[MemoryItem], query: MemoryQuery, provider_id: str) -> MemoryContext:
    query_terms = terms(query.text)
    scored: list[MemoryItem] = []
    for item in items:
        if not _in_scope(item, query.mission_id):
            continue
        text = f"{item.source} {item.content}"
        score = relevance(query_terms, text)
        if score <= 0.0:
            continue
        item.score = round(score, 3)
        scored.append(item)
    # El `id` actúa de desempate: sin él, dos recuerdos con la misma puntuación y la misma
    # fecha dependían del orden de llegada, y el mismo conjunto podía recuperar distinto
    # texto en dos ejecuciones.
    scored.sort(key=lambda i: (i.score, i.created_at or "", i.id), reverse=True)

    # `limit` y `token_budget` son las DOS cotas del contrato. Sólo se cumplía `limit`:
    # con `token_budget=100` se entregaban 2500 tokens. Ahora el presupuesto recorta por
    # orden de relevancia, que es el mismo orden en que se pide leerlos.
    selected: list[MemoryItem] = []
    used = 0
    for item in scored:
        cost = len(item.content.split())
        if len(selected) >= query.limit:
            break
        if used + cost > query.token_budget:
            continue
        selected.append(item)
        used += cost

    # Un presupuesto que deja el contexto VACÍO es peor que uno que se excede: el decisor
    # recibiría "no hay memoria relevante" cuando sí la hay, y esa mentira es más dañina que
    # un contexto grande. Se entrega el mejor recuerdo aunque no quepa, y `truncated` dice
    # que se recortó.
    if not selected and scored:
        selected = [scored[0]]
        used = len(scored[0].content.split())

    return MemoryContext(
        items=selected,
        sources=sorted({i.source for i in selected}),
        token_estimate=used,
        provider=provider_id,
        truncated=len(selected) < len(scored),
    )


class InProcessMemoryProvider(MemoryProvider):
    """Lee del `InMemoryMemory` del proceso (misión actual y anteriores en memoria)."""

    id = "in_process_memory"

    def __init__(self, store):
        self.store = store

    async def retrieve(self, query: MemoryQuery) -> MemoryContext:
        items: list[MemoryItem] = []
        for entry in list(getattr(self.store, "items", []) or []):
            try:
                mission_id, obs = entry
            except (TypeError, ValueError):
                continue
            items.append(
                MemoryItem(
                    id=f"obs-{mission_id}-{len(items)}",
                    kind="observations",
                    content=as_text(getattr(obs, "content", obs)),
                    source=f"observation:{getattr(obs, 'source', 'unknown')}",
                    mission_id=mission_id,
                    trusted=bool(getattr(obs, "trusted", False)),
                )
            )
        return _select(items, query, self.id)


class PostgresMemoryProvider(MemoryProvider):
    """Lee de la tabla `observations` (ya persistida) a través de la base de datos.

    Trae una ventana acotada de observaciones recientes y puntúa en Python: es simple,
    determinista y no exige embeddings ni infraestructura nueva.
    """

    id = "postgres_memory"

    def __init__(self, db, *, scan: int = 200):
        self.db = db
        self.scan = scan

    async def retrieve(self, query: MemoryQuery) -> MemoryContext:
        # El filtro de misión va en el SQL, no después: traer 200 filas de todas las
        # misiones para descartar en Python es gastar la base de datos para no hacer nada.
        params: dict[str, Any] = {"limit": self.scan}
        if query.mission_id is None:
            rows = await self.db.fetch(
                """
                SELECT mission_id, source, content, trusted, created_at
                FROM observations
                ORDER BY id DESC
                LIMIT %(limit)s
                """,
                params,
            )
        else:
            params["mission_id"] = query.mission_id
            rows = await self.db.fetch(
                """
                SELECT mission_id, source, content, trusted, created_at
                FROM observations
                WHERE mission_id = %(mission_id)s
                ORDER BY id DESC
                LIMIT %(limit)s
                """,
                params,
            )
        items = [
            MemoryItem(
                id=f"obs-{row['mission_id']}-{index}",
                kind="observations",
                content=as_text(row["content"]),
                source=f"observation:{row['source']}",
                mission_id=row["mission_id"],
                created_at=str(row["created_at"]),
                trusted=bool(row["trusted"]),
            )
            for index, row in enumerate(rows)
        ]
        return _select(items, query, self.id)


class NullMemoryProvider(MemoryProvider):
    """Sin memoria disponible. Devuelve contexto vacío y válido (no inventa)."""

    id = "null_memory"
    available = False

    async def retrieve(self, query: MemoryQuery) -> MemoryContext:
        return MemoryContext(items=[], sources=[], token_estimate=0, provider=self.id)


__all__ = [
    "MemoryProvider",
    "InProcessMemoryProvider",
    "PostgresMemoryProvider",
    "NullMemoryProvider",
    "relevance",
    "terms",
    "as_text",
]
