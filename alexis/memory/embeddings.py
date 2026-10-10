"""Proveedores de embeddings para la búsqueda semántica de memoria.

## Por qué es inyectable

Meter un modelo de embeddings en el CORE lo ataría a un runtime concreto: `transformers`
son cientos de MB, o un endpoint remoto que puede no estar. La dependencia correcta es la
inversa: el CORE define el PUERTO (`EmbeddingProvider`) y quien despliega decide el modelo.

## Lo que `HashEmbeddingProvider` es y lo que NO es

Es un **truco determinista de hashing**: cada término cae en una de las 384 dimensiones por
su hash, y el vector es la bolsa de términos normalizada. Con eso dos textos que comparten
palabras dan vectores parecidos, y el recorrido por `pgvector` se puede probar de verdad.

**No entiende significado.** "servidor caído" y "host no disponible" no se parecen porque
no comparten palabras, aunque signifiquen lo mismo. Para eso hace falta un modelo real
inyectado por quien despliega. Está aquí como implementación por defecto sin dependencias
y como doble determinista en tests, no como búsqueda semántica real. Decirlo de otro modo
sería vender algo que no hace.
"""

from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod
from typing import Any

#: Dimensión del vector. Debe coincidir con `vector(384)` en el esquema: si divergen, la
#: consulta a pgvector falla ruidosamente, que es mejor que devolver similitudes falsas.
EMBEDDING_DIM = 384

_TOKEN = re.compile(r"[a-záéíóúñü0-9]{3,}", re.IGNORECASE)


class EmbeddingProvider(ABC):
    """Convierte texto en un vector denso. La ausencia se expresa devolviendo `None`."""

    id = "embedding"
    available = True
    dim = EMBEDDING_DIM

    @abstractmethod
    async def embed(self, text: str) -> list[float] | None:
        """Vector de `text`, o `None` si este provider no puede emitirlo.

        `None` NO es un error: es la señal de "usa coincidencia de términos". Es lo que
        permite que el sistema degrade con honestidad en vez de fallar.
        """


class NullEmbeddingProvider(EmbeddingProvider):
    """Sin embeddings. La búsqueda semántica queda desactivada; el keyword sigue."""

    id = "null_embedding"
    available = False

    async def embed(self, text: str) -> list[float] | None:
        return None


class HashEmbeddingProvider(EmbeddingProvider):
    """Embedding determinista por hashing de términos. Sin dependencias, sin red."""

    id = "hash_embedding"

    def __init__(self, dim: int = EMBEDDING_DIM):
        self.dim = dim

    async def embed(self, text: str) -> list[float] | None:
        vector = self.embed_sync(text)
        return vector

    def embed_sync(self, text: str) -> list[float]:
        """Versión síncrona, para callers que ya están en un hilo bloqueante."""
        vector = [0.0] * self.dim
        tokens = _TOKEN.findall((text or "").lower())
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dim
            # El signo también viene del hash: evita que todos los términos sumen siempre
            # en el mismo sentido y se anulen entre sí.
            signo = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[bucket] += signo
        norma = math.sqrt(sum(v * v for v in vector))
        if norma == 0.0:
            return vector
        return [v / norma for v in vector]


class CallableEmbeddingProvider(EmbeddingProvider):
    """Envuelve una función sync o async. Es el punto de entrada de un modelo real.

    Se capturan los fallos y se degradan a `None`: si el modelo real no está disponible,
    la memoria sigue funcionando por término, en vez de tumbar la decisión.
    """

    id = "callable_embedding"

    def __init__(self, fn: Any, *, provider_id: str = "callable_embedding", dim: int = EMBEDDING_DIM):
        self._fn = fn
        self.id = provider_id
        self.dim = dim

    async def embed(self, text: str) -> list[float] | None:
        try:
            vector = self._fn(text)
            if hasattr(vector, "__await__"):
                vector = await vector
            if vector is None:
                return None
            vector = [float(v) for v in vector]
            if len(vector) != self.dim:
                raise ValueError(
                    f"el embedding tiene {len(vector)} dimensiones y el esquema espera {self.dim}"
                )
            return vector
        except Exception:  # noqa: BLE001 — degradar a keyword, nunca romper la memoria
            return None


__all__ = [
    "EMBEDDING_DIM",
    "CallableEmbeddingProvider",
    "EmbeddingProvider",
    "HashEmbeddingProvider",
    "NullEmbeddingProvider",
]