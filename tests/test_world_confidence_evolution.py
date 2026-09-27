"""P0 §4.5.2 — EVOLUTION DE CONFIANZA del World Model.

Antes de este incremento `confidence` se fijaba UNA vez al construir la entidad
(`0.7` observación de tool, `0.9` test run, `1.0` declarado) y `upsert()` ni la miraba:
no existía regla, sólo un número. Aquí la confianza es una **función pura de los
contadores**, lo que además la hace independiente del orden de llegada.

El requisito que manda es el último: *la evolución no debe depender del orden accidental
de iteración*. Por eso no se acumula (`conf += 0.1`), sino que se recalcula desde cero.
"""

import pathlib
import sys
import time

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.world.model import (  # noqa: E402
    ABSENCE_TTL_S,
    CONFIDENCE_CEILING,
    LAST_SEEN_UNKNOWN,
    Scope,
    WorldEntity,
    WorldModel,
    evolved_confidence,
    source_rank,
)

SCOPE = Scope.from_workspace("/tmp/conf-ws")


#: Instantes FIJOS. El determinismo es "el mismo conjunto de observaciones produce el
#: mismo estado", así que el conjunto tiene que ser realmente el mismo entre permutaciones.
#: Con `time.time()` en cada llamada cada permutación sería un conjunto distinto y el test
#: no probaría nada.
T0 = 1_790_000_000.0


def _obs(source="tool:fs.stat", exists=True, age=0.0, at=None):
    """Una observación de tool, como la que produce `observe_execution`."""
    return WorldEntity("file:notas.txt", "file", "notas.txt", {"exists": exists},
                       source=source, mission_id="m1",
                       last_seen=(T0 + at) if at is not None else (T0 - age),
                       scope=SCOPE.id)


# =========================================================================== #
# Cómo nace
# =========================================================================== #


def test_01_una_observacion_nace_en_la_confianza_de_su_origen():
    """Sin corroborar, vale EXACTAMENTE lo que vale su fuente. Ni más ni menos."""
    w = WorldModel(scope=SCOPE)
    e = w.upsert(_obs(source="tool:fs.stat"))
    assert e.confidence == pytest.approx(0.7)
    w2 = WorldModel(scope=SCOPE)
    d = w2.upsert(_obs(source="test:execute.test"))
    assert d.confidence == pytest.approx(0.9)
    w3 = WorldModel(scope=SCOPE)
    t = w3.upsert(WorldEntity("postgres", "database", "PostgreSQL", source="registry",
                              scope=SCOPE.id))
    assert t.confidence == pytest.approx(1.0)


def test_02_un_origen_desconocido_es_confianza_media():
    w = WorldModel(scope=SCOPE)
    e = w.upsert(_obs(source="alguien-desconocido"))
    assert e.confidence == pytest.approx(0.5)


# =========================================================================== #
# Cómo cambia con nueva evidencia
# =========================================================================== #


def test_03_la_corroboracion_sube_la_confianza():
    w = WorldModel(scope=SCOPE)
    previa = None
    for i in range(5):
        e = w.upsert(_obs())
        if previa is not None:
            assert e.confidence > previa, f"la observación {i+1} no subió la confianza"
        previa = e.confidence


def test_04_la_subida_satura_y_no_alcanza_uno():
    """Ver algo repetido no es haberlo PROBADO: el techo es < 1.0."""
    w = WorldModel(scope=SCOPE)
    for _ in range(60):
        e = w.upsert(_obs())
    assert e.confidence < 1.0
    assert e.confidence == pytest.approx(CONFIDENCE_CEILING)


def test_05_corroborar_un_hecho_declarado_no_lo_rebaja():
    """El techo nunca queda por debajo de la base."""
    w = WorldModel(scope=SCOPE)
    e = w.upsert(WorldEntity("workspace", "sandbox", "ws", source="registry", scope=SCOPE.id))
    assert e.confidence == pytest.approx(1.0)
    for _ in range(10):
        e = w.upsert(WorldEntity("workspace", "sandbox", "ws", source="registry",
                                 scope=SCOPE.id))
    assert e.confidence == pytest.approx(1.0), "corroborar no puede hacer bajar un hecho"


def test_06_la_base_es_el_maximo_de_las_fuentes_vistas():
    """Una fuente fiable observada después sigue siendo la base: el máximo es conmutativo."""
    w = WorldModel(scope=SCOPE)
    w.upsert(_obs(source="tool:fs.stat"))                      # base 0.7
    e = w.upsert(WorldEntity("file:notas.txt", "file", "notas.txt", {"exists": True},
                             source="registry", scope=SCOPE.id))
    assert e.base_confidence == pytest.approx(1.0)


# =========================================================================== #
# Qué ocurre con evidencia contradictoria
# =========================================================================== #


def test_07_una_contradiccion_es_explicita_no_silenciosa():
    """P0 §4.5.2: qué ocurre con evidencia contradictoria."""
    w = WorldModel(scope=SCOPE)
    w.upsert(_obs(source="tool:fs.stat", exists=True))
    antes = w.known_path("notas.txt").confidence
    e = w.upsert(_obs(source="tool:fs.stat", exists=False))   # contradice
    # True y False quedan 1-1: empate, y un empate pone a las DOS en disputa. Por eso son
    # 2 y no 1. Ninguna observación se decantó por mayoría, así que ninguna apoya.
    assert e.contradictions == 2, "una contradicción tiene que quedar contada"
    assert e.confidence < antes, "una contradicción tiene que bajar la confianza"


def test_08_una_contradiccion_puede_dejar_el_hecho_por_debajo_del_umbral():
    w = WorldModel(scope=SCOPE)
    w.upsert(_obs(exists=True))
    e = w.upsert(_obs(exists=False))
    assert e.confidence < 0.7, "no puede seguir valiendo lo que valía una observación sola"


def test_09_el_empate_es_incognita_no_promedio():
    """1 apoyo y 1 contradicción: no se sabe. Se dice que no se sabe."""
    w = WorldModel(scope=SCOPE)
    w.upsert(_obs(exists=True))
    e = w.upsert(_obs(exists=False))
    assert e.confidence < 0.5, "un empate no es un hecho a medias"


def test_07b_muchos_apoyos_pueden_vencer_a_una_contradiccion():
    w = WorldModel(scope=SCOPE)
    for _ in range(12):
        w.upsert(_obs(exists=True))
    antes = w.known_path("notas.txt").confidence
    e = w.upsert(_obs(exists=False))
    assert e.confidence > 0.7, "con mucho apoyo, una contradicción aislada no lo tumbó"
    assert e.confidence < antes


# =========================================================================== #
# Qué ocurre con evidencia antigua / stale
# =========================================================================== #


def test_10_la_confianza_no_depende_de_la_antiguedad():
    """La confianza mide CUÁNTO se ha visto, no CUÁNDO. La antigüedad la gobierna
    `ABSENCE_TTL_S` (§4.5.1), y son cosas distintas: un dato viejo sigue siendo tan
   "fuerte" como uno nuevo; lo que caduca es el poder de BLOQUEAR con el."""
    w1 = WorldModel(scope=SCOPE)
    w2 = WorldModel(scope=SCOPE)
    for _ in range(3):
        w1.upsert(_obs(age=0.0))
        w2.upsert(_obs(age=99_999))
    assert w1.known_path("notas.txt").confidence == pytest.approx(
        w2.known_path("notas.txt").confidence
    )


def test_11_una_ausencia_vencida_sigue_teniendo_confianza_pero_no_bloquea():
    """Las dos reglas conviven: la confianza no caduca, el bloqueo sí."""
    w = WorldModel(scope=SCOPE)
    for _ in range(3):
        w.upsert(_obs(exists=False, age=ABSENCE_TTL_S + 10))
    e = w.known_path("notas.txt")
    assert e.confidence > 0.7, "tres observaciones restos de confianza"
    assert w.missing_paths(["notas.txt"]) == [], "pero ya no puede bloquear"


def test_12_edad_desconocida_no_inventa_confianza():
    w = WorldModel(scope=SCOPE)
    e = w.put(WorldEntity("file:notas.txt", "file", "notas.txt", {"exists": False},
                          source="recovered", confidence=0.0, support=1,
                          base_confidence=0.0, last_seen=LAST_SEEN_UNKNOWN, scope=SCOPE.id))
    assert e.base_confidence == 0.0, "sin procedencia conocida no hay confianza"


# =========================================================================== #
# Determinismo: el MISMO conjunto, el mismo resultado
# =========================================================================== #


def test_13_el_mismo_conjunto_da_el_mismo_resultado_en_cualquier_orden():
    """El requisito que justifica la fórmula pura."""
    fuentes = ["tool:fs.stat", "tool:fs.read", "registry", "tool:fs.stat", "test:execute.test"]
    import itertools

    # Cada fuente tiene su instante fijo: el conjunto es el mismo en las 120 permutaciones.
    instantes = {s: 10.0 * (i + 1) for i, s in enumerate(sorted(set(fuentes)))}
    resultados = set()
    for permutacion in itertools.permutations(fuentes):
        w = WorldModel(scope=SCOPE)
        for source in permutacion:
            w.upsert(_obs(source=source, at=instantes[source]))
        e = w.known_path("notas.txt")
        resultados.add((
            round(e.confidence, 12),
            e.support,
            e.contradictions,
            round(e.base_confidence, 12),
        ))
    assert len(resultados) == 1, f"el resultado dependió del orden: {resultados}"


def test_14_el_orden_no_cambia_las_atributos_del_observado():
    import itertools

    # Observaciones en instantes FIJOS y distintos: T+10 dice exists=True y T+20 dice
    # False, así que el vigente debe ser False llegue como llegue.
    instantes = {True: 10.0, False: 20.0}
    resultados = set()
    for permutacion in itertools.permutations([True, False, True]):
        w = WorldModel(scope=SCOPE)
        for existe in permutacion:
            w.upsert(_obs(exists=existe, at=instantes[existe]))
        e = w.known_path("notas.txt")
        # (False, True, True) contaría 1 contradicción y (True, False, True) contaría 2
        # si se comparase cada observación con el valor "actual". Con frequencies, no.
        resultados.add((e.support, e.contradictions, round(e.confidence, 12)))
    assert len(resultados) == 1, f"el conteo dependió del orden: {resultados}"
    # 2 True (T+10) y 1 False (T+20): el vigente es False por ser lo último visto, aunque
    # True sea más frecuente. Support/contradicciones siguen 2 y 1 porfrequency.
    soporte, contra, _conf = resultados.pop()
    assert (soporte, contra) == (2, 1)


def test_15_evolved_confidence_es_funcion_pura():
    """La misma entrada da la misma salida, siempre. Sin estado oculto."""
    for base in (0.0, 0.5, 0.7, 1.0):
        for support in range(0, 8):
            for contra in range(0, 4):
                a = evolved_confidence(base, support, contra)
                b = evolved_confidence(base, support, contra)
                assert a == b
                assert 0.0 <= a <= 1.0


def test_16_source_rank_es_total_y_determinista():
    assert source_rank("registry") > source_rank("test:execute.test")
    assert source_rank("test:execute.test") > source_rank("tool:fs.stat")
    assert source_rank("desconocido") < source_rank("tool:fs.stat")


# =========================================================================== #
# hydrate NO produce confianza artificial
# =========================================================================== #


def test_17_hydrate_no_inventa_confianza():
    """Rehidratar no es observar. Es el requisito explícito."""
    w = WorldModel(scope=SCOPE)
    antes = None
    for _ in range(3):
        antes = w.upsert(_obs())
    confianza = antes.confidence
    apoyo = antes.support

    # Rehidratamos 20 veces desde el estado persistido.
    salida = w.export(scope=SCOPE)
    for _ in range(20):
        nuevo = WorldModel(scope=SCOPE)
        nuevo.hydrate(salida["entities"], salida["edges"], scope=SCOPE.id)
        e = nuevo.known_path("notas.txt")
        assert e.confidence == pytest.approx(confianza)
        assert e.support == apoyo
        assert e.observations == antes.observations, "rehidratar no observa"


def test_18_put_no_evoluciona_nada():
    """`put()` REPONE, no recalcula: es la vía de rehidratación.

    Se distinctions importante: `put()` no aplica la regla de evolución, así que deja
    intactos tanto la confianza como los contadores que traía la entidad. Por eso una
    entidad construida a mano conserva su `confidence` literal (aquí el default del
    dataclass, 1.0) en vez de recalcularse a la base de su origen.
    """
    w = WorldModel(scope=SCOPE)
    traida = _obs()
    traida.confidence = 0.42
    traida.support = 9
    w.put(traida)
    e = w.known_path("notas.txt")
    assert e.confidence == pytest.approx(0.42), "put() no aplica la regla de evolución"
    assert e.support == 9, "ni inventa ni ajusta contadores"


# =========================================================================== #
# Sobrevive a PostgreSQL
# =========================================================================== #


@pytest.mark.asyncio
async def test_19_la_confianza_sobrevive_al_store(db):
    from alexis.storage.repositories import WorldRepository

    await db.open()
    await db.migrate()
    w = WorldModel(scope=SCOPE)
    for _ in range(4):
        w.upsert(_obs())
    e = w.known_path("notas.txt")
    salida = w.export(scope=SCOPE)
    await WorldRepository(db).save_snapshot(salida["entities"], salida["edges"],
                                            scope=SCOPE.id)

    recargado = await WorldRepository(db).get_entity("file:notas.txt", scope=SCOPE.id)
    assert recargado.confidence == pytest.approx(e.confidence)
    assert recargado.support == e.support
    assert recargado.base_confidence == pytest.approx(e.base_confidence)
    await db.close()


@pytest.mark.asyncio
async def test_20_la_confianza_tras_reiniciar_no_se_recalcula(db):
    """Recuperar NO observa: la confianza leída es la que se persistió, no la de 1."""
    from alexis.storage.db import Database
    from alexis.storage.repositories import WorldRepository

    await db.open()
    await db.migrate()
    w = WorldModel(scope=SCOPE)
    for _ in range(5):
        w.upsert(_obs())
    persistida = w.known_path("notas.txt").confidence
    salida = w.export(scope=SCOPE)
    await WorldRepository(db).save_snapshot(salida["entities"], salida["edges"],
                                            scope=SCOPE.id)

    otra = Database(dsn=db.dsn)
    await otra.open()
    entidades = await WorldRepository(otra).list_entities(scope=SCOPE.id)
    mundo = WorldModel(scope=SCOPE)
    mundo.hydrate(entidades, scope=SCOPE.id)
    assert mundo.known_path("notas.txt").confidence == pytest.approx(persistida)
    assert mundo.known_path("notas.txt").support == 5
    await otra.close()
    await db.close()


@pytest.mark.asyncio
async def test_21_la_migracion_es_idempotente(db):
    """`migrate()` puede correr las veces que haga falta: las columnas son IF NOT EXISTS."""
    await db.open()
    await db.migrate()
    await db.migrate()
    rows = await db.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='world_entities' ORDER BY column_name"
    )
    columnas = {r["column_name"] for r in rows}
    assert {"base_confidence", "support", "contradictions", "evidence_ids",
            "conflicts"} <= columnas
    await db.close()
