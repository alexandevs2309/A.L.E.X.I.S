"""Búsqueda semántica (pgvector) de la memoria episódica. MINDMAP §4.

Contra PostgreSQL REAL: la migración, el índice y el operador `<=>` son parte de lo que se
prueba. Un doble no comprobaría que el cast `::vector` funciona ni que el `NULL` sale al
final, que es justo el comportamiento del que depende el fallback.

Los embeddings los produce `HashEmbeddingProvider` (determinista, sin dependencias). Ojo
con lo que eso significa y lo que no: **no entiende significado**, sólo reparte términos
por hash. Lo que estas pruebas demuestran es que el ARRANQUE, la consulta vectorial, el
fallback y los límites funcionan de verdad. Que la recuperación sea comprensible requiere
inyectar un modelo real; que esto funcione no lo requiere.
"""

from __future__ import annotations

import pytest

from alexis.memory.contracts import MemoryQuery
from alexis.memory.embeddings import (
    EMBEDDING_DIM,
    HashEmbeddingProvider,
    NullEmbeddingProvider,
)
from alexis.memory.provider import PostgresMemoryProvider, terms
from alexis.storage.repositories import MissionRepository, ObservationRepository


@pytest.fixture
def embedder():
    return HashEmbeddingProvider()


async def _mision(db, objetivo: str):
    from alexis.autonomy.mission import MissionEngine
    from alexis.contracts import AutonomyLevel, MissionEnvelope

    # Se PERSISTE: `observations.mission_id` tiene FK a `missions`. Una misión sólo
    # creada en memoria es una fila que no existe, y la inserción falla con FK.
    mission = MissionEngine().create(objetivo, MissionEnvelope(
        objetivo, autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=["read", "research", "execute", "verify"],
    ))
    await MissionRepository(db).upsert(mission)
    return mission


async def _observar(db, mission, payload: dict, *, trusted=True, embedding=None):
    """Inserta una observación y, si se pide, le escribe su vector."""
    repo = ObservationRepository(db)
    await repo.insert(mission.id, "fs.read", payload, trusted)
    if embedding is not None:
        fila = await db.fetch(
            "SELECT id FROM observations WHERE mission_id = %(m)s ORDER BY id DESC LIMIT 1",
            {"m": mission.id},
        )
        await PostgresMemoryProvider(db).store_embedding(fila[0]["id"], embedding)
    return mission


# --------------------------------------------------------------------- #
# 1 — Recuperación por similitud, no por coincidencia literal
# --------------------------------------------------------------------- #


async def test_busca_por_similitud_y_no_por_coincidencia_exacta(db, embedder):
    """La observación se recupera SIN que la consulta comparta ni una palabra.

    Es la diferencia entre una búsqueda vectorial funcional y una decorativa. Aquí el
    vector de la observación se construye a partir de un texto alineado con la consulta,
    que es lo que haría un modelo real al leer la observación: "esto va de incidencias
    de arquitectura". Con `HashEmbeddingProvider` la alineación es artificial; lo que se
    demuestra aquí es que el ORDEN y el FILTRO vienen de pgvector, no de los términos.
    """
    nota = "el servidor de base de datos quedo sin respuesta durante el despliegue"
    consulta = "incidente en la arquitectura"
    # Se comprueba la premisa del test: sin coincidencia literal no hay nada que recuperar
    # por la vía léxica. Si algún día compartieran términos, el test dejaría de demostrar
    # lo que dice.
    assert not (terms(consulta) & terms(nota)), "la consulta debe ser léxicamente ajena"

    mission = await _mision(db, "memoria")
    await _observar(db, mission, {"nota": nota},
                    embedding=await embedder.embed("incidente arquitectura"))

    provider = PostgresMemoryProvider(db, embedding_provider=embedder)
    vector_consulta = await embedder.embed("incidente arquitectura")
    semantico = await provider.retrieve(MemoryQuery(
        text=consulta, mission_id=mission.id, limit=5, query_embedding=vector_consulta,
    ))

    assert semantico.items, "la búsqueda vectorial debe encontrarlo sin coincidencia literal"
    assert semantico.items[0].semantic_score is not None, "la similitud la calcula pgvector"
    assert semantico.items[0].semantic_score > 0.9, "debería ser casi idéntico"
    assert semantico.items[0].score == 0.0, "y aun así sin coincidencia de términos"

    # El mismo provider SIN vector disponible: sólo puede buscar por términos, y no hay.
    solo_palabras = await PostgresMemoryProvider(db).retrieve(
        MemoryQuery(text=consulta, mission_id=mission.id, limit=5)
    )
    assert solo_palabras.items == [], "sin vector, esta consulta no tiene a qué agarrarse"

    # Con provider inyectado pero sin vector explícito, éste lo genera: el llamante no
    # necesita saber nada de embeddings.
    autogenerado = await provider.retrieve(
        MemoryQuery(text=consulta, mission_id=mission.id, limit=5)
    )
    assert autogenerado.items and autogenerado.items[0].semantic_score is not None


async def test_ordena_por_similitud_descendente(db, embedder):
    mission = await _mision(db, "orden")
    # Dos textos con distinto parecido respecto de la consulta.
    cercano = await embedder.embed("base de datos caida en el despliegue")
    lejano = await embedder.embed("hola que tal todo bien aqui")
    await _observar(db, mission, {"n": "lejano"}, embedding=lejano)
    await _observar(db, mission, {"n": "cercano"}, embedding=cercano)

    provider = PostgresMemoryProvider(db, embedding_provider=embedder)
    consulta = "base de datos"
    contexto = await provider.retrieve(MemoryQuery(
        text=consulta, mission_id=mission.id, limit=5,
        query_embedding=await embedder.embed(consulta),
    ))

    assert len(contexto.items) == 2
    assert contexto.items[0].semantic_score >= contexto.items[1].semantic_score
    assert "cercano" in contexto.items[0].content


# --------------------------------------------------------------------- #
# 2 — Fallback a keyword
# --------------------------------------------------------------------- #


async def test_fallback_a_keyword_si_el_embedding_es_null(db, embedder):
    """Observación sin vector + consulta con vector: la léxica sigue answering."""
    mission = await _mision(db, "fallback")
    await _observar(db, mission, {"nota": "el informe anual de ventas"}, embedding=None)

    provider = PostgresMemoryProvider(db, embedding_provider=embedder)
    consulta = "informe ventas"
    contexto = await provider.retrieve(MemoryQuery(
        text=consulta, mission_id=mission.id, limit=5,
        query_embedding=await embedder.embed(consulta),
    ))

    assert contexto.items, "una fila sin embedding no puede desaparecer"
    assert "informe anual de ventas" in contexto.items[0].content
    assert contexto.items[0].semantic_score is None, "sin vector no hay similitud medida"
    assert contexto.items[0].score > 0, "pero sí hay coincidencia de términos"


async def test_sin_embeddings_la_base_es_una_base_normal(db, embedder):
    """Retrocompatibilidad: cero embeddings debe comportarse como antes del cambio."""
    mission = await _mision(db, "compat")
    await _observar(db, mission, {"nota": "nota de despliegue del servicio"})

    provider = PostgresMemoryProvider(db, embedding_provider=embedder)
    contexto = await provider.retrieve(MemoryQuery(text="despliegue servicio", mission_id=mission.id))

    assert [i.semantic_score for i in contexto.items] == [None]
    assert "nota de despliegue del servicio" in contexto.items[0].content


async def test_provider_nulo_deja_el_camino_lexico_intacto(db):
    mission = await _mision(db, "nulo")
    await _observar(db, mission, {"nota": "revision del presupuesto anual"})

    provider = PostgresMemoryProvider(db, embedding_provider=NullEmbeddingProvider())
    contexto = await provider.retrieve(MemoryQuery(text="presupuesto", mission_id=mission.id))

    assert contexto.items and "presupuesto anual" in contexto.items[0].content
    assert contexto.items[0].semantic_score is None


# --------------------------------------------------------------------- #
# 3 — El presupuesto se respeta también con orden por vector
# --------------------------------------------------------------------- #


async def test_token_budget_se_respeta_con_orden_por_similitud(db, embedder):
    """El vector cambia el ORDEN, no puede saltarse el presupuesto: eso acota el contexto."""
    mission = await _mision(db, "presupuesto")
    for i in range(8):
        vector = await embedder.embed("palabra clave " * 20)
        await _observar(db, mission, {"n": f"nota numero {i} " + "palabra clave " * 20},
                        embedding=vector)

    provider = PostgresMemoryProvider(db, embedding_provider=embedder)
    consulta = "palabra clave"
    contexto = await provider.retrieve(MemoryQuery(
        text=consulta, mission_id=mission.id, limit=8, token_budget=120,
        query_embedding=await embedder.embed(consulta),
    ))

    assert contexto.token_estimate <= 120, f"entregados {contexto.token_estimate}"
    assert len(contexto.items) < 8
    assert contexto.truncated is True


# --------------------------------------------------------------------- #
# 4 — Aislamiento entre misiones, también en el camino vectorial
# --------------------------------------------------------------------- #


async def test_la_busqueda_semantica_no_mezcla_misiones(db, embedder):
    """Vectorial y aislado: una misión no recibe los recuerdos de otra."""
    mia = await _mision(db, "mía")
    otra = await _mision(db, "otra")
    vector = await embedder.embed("la clave del servidor esta aqui")
    await _observar(db, mia, {"nota": "mi nota con la clave del servidor"}, embedding=vector)
    await _observar(db, otra, {"nota": "su nota con la clave del servidor"}, embedding=vector)

    provider = PostgresMemoryProvider(db, embedding_provider=embedder)
    consulta = "clave del servidor"
    contexto = await provider.retrieve(MemoryQuery(
        text=consulta, mission_id= mia.id, limit=10,
        query_embedding=await embedder.embed(consulta),
    ))

    assert contexto.items
    assert {i.mission_id for i in contexto.items} == {mia.id}
    assert "su nota" not in " ".join(i.content for i in contexto.items)


# --------------------------------------------------------------------- #
# Migración e índice: que exista es parte del contrato
# --------------------------------------------------------------------- #


async def test_la_columna_y_el_indice_existen(db):
    """Si la migración no se aplicó, todo lo demás pasa por casualidad."""
    columnas = await db.fetch(
        "SELECT udt_name FROM information_schema.columns "
        "WHERE table_name = 'observations' AND column_name = 'embedding'"
    )
    assert columnas and columnas[0]["udt_name"] == "vector"

    indices = await db.fetch(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'observations' "
        "AND indexname = 'ix_observations_embedding'"
    )
    assert indices, "falta el índice HNSW de embeddings"


def test_la_dimension_coincide_con_el_esquema():
    """Si divergen, pgvector falla ruidosamente. Es mejor que devolver similitudes falsas."""
    import re
    import pathlib

    schema = pathlib.Path("alexis/storage/schema.py").read_text(encoding="utf-8")
    dims = {int(m) for m in re.findall(r"vector\((\d+)\)", schema)}
    assert dims == {EMBEDDING_DIM}, f"el esquema declara {dims}, el código {EMBEDDING_DIM}"


async def test_un_provider_real_que_falla_degrada_a_keyword(db):
    """Si el modelo de embeddings no está, la memoria sigue funcionando."""
    from alexis.memory.embeddings import CallableEmbeddingProvider

    mission = await _mision(db, "degradado")
    await _observar(db, mission, {"nota": "un hecho relevante sobre el tema"})

    def _explota(_text):
        raise RuntimeError("el modelo no está cargado")

    provider = PostgresMemoryProvider(
        db, embedding_provider=CallableEmbeddingProvider(_explota)
    )
    contexto = await provider.retrieve(MemoryQuery(text="relevante", mission_id=mission.id))

    assert contexto.items, "un provider roto no puede vaciar la memoria"
    assert "hecho relevante" in contexto.items[0].content


async def test_dimensions_incorrectas_no_pasan_silenciosamente():
    """Un vector de otra dimensión se descarta, no se convierte en similitud inventada."""
    from alexis.memory.embeddings import CallableEmbeddingProvider

    provider = CallableEmbeddingProvider(lambda _t: [0.1, 0.2, 0.3])  # 3 dims, no 384
    assert await provider.embed("texto") is None