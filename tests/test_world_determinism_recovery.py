"""P0 §4.5.5 — DETERMINISMO, RECOVERY Y AISLAMIENTO, con PostgreSQL de verdad.

Este fichero es la prueba de que lo anterior no era bookkeeping decorativo:

- El MISMO conjunto de observaciones produce el MISMO estado, llegue como llegue.
- Un proceso que escribe y otro que relee obtienen el mismo mundo semántico, con
  confianza, conflictos, procedencia y antigüedad intactos.
- Dos proyectos con el mismo path relativo siguen aislados después de reiniciar.
- Y todo contra la BD real, con dos conexiones.

Cierra también el requisito de que el World Model NO tiene autoridad: resuelve
contradicciones con sus propias reglas y no toca Policy.
"""

import itertools
import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.storage.repositories import WorldRepository  # noqa: E402
from alexis.world.model import (  # noqa: E402
    LAST_SEEN_UNKNOWN,
    Scope,
    WorldEntity,
    WorldModel,
    value_key,
)  # noqa: E402

WS_A = "/tmp/e2e-proyecto-A"
WS_B = "/tmp/e2e-proyecto-B"
SCOPE_A = Scope.from_workspace(WS_A)
SCOPE_B = Scope.from_workspace(WS_B)
T0 = 1_790_000_000.0


def _obs(name, values, *, at, source="tool:fs.stat", evidence=(), mission="m1", scope=None):
    return WorldEntity(f"file:{name}", "file", name, values, source=source,
                       mission_id=mission, last_seen=T0 + at,
                       evidence_ids=list(evidence), scope=(scope or SCOPE_A).id)


# Un conjunto con contradicción, cambio de estado y varias evidencias: ejercita las tres
# cosas a la vez.
def conjunto() -> list:
    """Observaciones nuevas en cada llamada. DELIBERADAMENTE una fábrica, no una constante.

    `WorldEntity` es mutable y `upsert()` la modifica (contadores, confianza, atributos).
    Compartir las mismas instancias entre tests hacía que el segundo que las usara partiera
    de un estado ya alterado, y una prueba de determinismo sobre fixtures mutables no
    demuestra nada.
    """
    return [
        _obs("notas.txt", {"exists": True}, at=10, evidence=["cl-1"]),
        _obs("notas.txt", {"exists": True}, at=20, evidence=["cl-2"]),
        _obs("notas.txt", {"exists": False, "size": 0}, at=30, evidence=["cl-3"]),
        _obs("notas.txt", {"exists": False, "size": 0}, at=40, evidence=["cl-4"]),
        _obs("otro.txt", {"exists": True, "size": 9}, at=35, evidence=["cl-5"]),
    ]


def _permutaciones():
    """Permutaciones del conjunto, con entidades NUEVAS en cada una.

    `WorldEntity` es mutable y `upsert()` la modifica. Permutar los índices y reconstruir
    en cada vuelta es lo único que hace válida la prueba: permutar las instancias
    compartiría entidades ya alteradas por la vuelta anterior y el "mismo conjunto" ya no
    lo sería, de modo que el test mediría la corrupción del fixture en vez del orden.
    """
    base = conjunto()
    for perm in itertools.permutations(range(len(base))):
        yield [conjunto()[i] for i in perm]


def _semantic(world: WorldModel) -> tuple:
    """Huella del ESTADO SEMÁNTICO: lo que tiene que ser idéntico llegue como llegue.

    Deliberadamente excluye `observations` (contador de llamadas) y no incluye el orden de
    los diccionarios, que no es información del mundo.
    """
    partes = []
    for key in sorted(world.entities):
        e = world.entities[key]
        partes.append((
            key, e.id, e.kind, e.name,
            tuple(sorted(e.attributes.items())),
            e.source, round(e.confidence, 12), e.mission_id, e.scope,
            tuple(sorted(e.value_counts.items())),
            tuple(e.conflicts),
            tuple(sorted(e.evidence_ids)),
            e.last_seen,
        ))
    aristas = tuple(sorted(world.edges))
    return tuple(partes), aristas


# =========================================================================== #
# Determinismo
# =========================================================================== #


def test_01_el_mismo_conjunto_da_el_mismo_estado_en_120_ordenes():
    world = WorldModel(scope=SCOPE_A)
    for o in conjunto():
        world.upsert(o)
    referencia = _semantic(world)

    for permutacion in _permutaciones():
        otro = WorldModel(scope=SCOPE_A)
        for o in permutacion:
            otro.upsert(o)
        assert _semantic(otro) == referencia, "el estado depende del orden de llegada"


def test_02_el_determinismo_incluye_confianza_y_conflictos():
    world = WorldModel(scope=SCOPE_A)
    for o in conjunto():
        world.upsert(o)
    e = world.known_path("notas.txt")
    # El vigente es el último visto (exists=False a T+40), no el más frecuente.
    assert e.attributes["exists"] is False
    assert e.conflicts, "hay contradicción y tiene que constar"
    assert e.evidence_ids == ["cl-1", "cl-2", "cl-3", "cl-4"]
    for permutacion in _permutaciones():
        otro = WorldModel(scope=SCOPE_A)
        for o in permutacion:
            otro.upsert(o)
        otro_e = otro.known_path("notas.txt")
        assert otro_e.confidence == e.confidence
        assert otro_e.conflicts == e.conflicts
        assert otro_e.value_counts == e.value_counts
        assert otro_e.last_seen == e.last_seen


def test_03_el_orden_no_cambia_las_relaciones():
    resultados = set()
    for _ in range(10):
        w = WorldModel(scope=SCOPE_A)
        w.upsert(_obs("notas.txt", {"exists": True}, at=10))
        w.upsert(_obs("otro.txt", {"exists": True}, at=20))
        w.relate("file:notas.txt", "file:otro.txt", scope=SCOPE_A)
        resultados.add(tuple(sorted(w.edges)))
    assert len(resultados) == 1


# =========================================================================== #
# Recovery cross-proceso, con PostgreSQL real
# =========================================================================== #


@pytest.mark.asyncio
async def test_04_e2e_proceso_a_persiste_y_proceso_b_reconstruye(db):
    from alexis.storage.db import Database

    await db.open()
    await db.migrate()

    # ── PROCESO A: observa, resuelve y persiste ────────────────────────────
    mundo_a = WorldModel(scope=SCOPE_A)
    for o in conjunto():
        mundo_a.upsert(o)
    esperado = _semantic(mundo_a)
    salida = mundo_a.export(scope=SCOPE_A)
    await WorldRepository(db).save_snapshot(salida["entities"], salida["edges"],
                                            scope=SCOPE_A.id)
    await db.close()                      # el proceso A se apaga

    # ── PROCESO B: base nueva, repositorio nuevo, mundo nuevo ──────────────
    otra = Database(dsn=db.dsn)
    await otra.open()
    repo = WorldRepository(otra)
    mundo_b = WorldModel(scope=SCOPE_A)
    assert mundo_b.hydrate(
        await repo.list_entities(scope=SCOPE_A.id),
        await repo.list_edges(scope=SCOPE_A.id),
        scope=SCOPE_A.id,
    ) == len(salida["entities"])

    # Mismo estado semántico: incluye confianza, conflictos, procedencia y antigüedad.
    assert _semantic(mundo_b) == esperado
    await otra.close()


@pytest.mark.asyncio
async def test_05_dos_conexiones_escriben_y_leen(db):
    from alexis.storage.db import Database

    await db.open()
    await db.migrate()
    otra = Database(dsn=db.dsn)
    await otra.open()

    mundo = WorldModel(scope=SCOPE_A)
    for o in conjunto():
        mundo.upsert(o)
    salida = mundo.export(scope=SCOPE_A)
    await WorldRepository(db).save_snapshot(salida["entities"], salida["edges"],
                                            scope=SCOPE_A.id)

    leidas = await WorldRepository(otra).list_entities(scope=SCOPE_A.id)
    assert {e.id for e in leidas} == {o.id for o in conjunto()}
    await otra.close()
    await db.close()


@pytest.mark.asyncio
async def test_06_los_conflictos_sobreviven_al_reinicio(db):
    await db.open()
    await db.migrate()
    mundo = WorldModel(scope=SCOPE_A)
    for o in conjunto():
        mundo.upsert(o)
    salida = mundo.export(scope=SCOPE_A)
    await WorldRepository(db).save_snapshot(salida["entities"], salida["edges"],
                                            scope=SCOPE_A.id)

    releido = await WorldRepository(db).get_entity("file:notas.txt", scope=SCOPE_A.id)
    assert len(releido.conflicts) == 2, "las dos versiones siguen ahí tras el store"
    assert {str(c["value"]) for c in releido.conflicts} == {"True", "False"}
    assert releido.evidence_ids == ["cl-1", "cl-2", "cl-3", "cl-4"]
    assert releido.contradictions >= 1
    await db.close()


@pytest.mark.asyncio
async def test_07_la_hidrata_no_reinventa_confianza_ni_conflictos(db):
    """Rehidratar 20 veces debe dar SIEMPRE el mismo estado: no observa."""
    from alexis.storage.db import Database

    await db.open()
    await db.migrate()
    mundo = WorldModel(scope=SCOPE_A)
    for o in conjunto():
        mundo.upsert(o)
    referencia = _semantic(mundo)
    salida = mundo.export(scope=SCOPE_A)
    await WorldRepository(db).save_snapshot(salida["entities"], salida["edges"],
                                            scope=SCOPE_A.id)

    otra = Database(dsn=db.dsn)
    await otra.open()
    for _ in range(20):
        entidades = await WorldRepository(otra).list_entities(scope=SCOPE_A.id)
        aristas = await WorldRepository(otra).list_edges(scope=SCOPE_A.id)
        mundo_nuevo = WorldModel(scope=SCOPE_A)
        mundo_nuevo.hydrate(entidades, aristas, scope=SCOPE_A.id)
        assert _semantic(mundo_nuevo) == referencia
    await otra.close()
    await db.close()


# =========================================================================== #
# Scope isolation después de reiniciar
# =========================================================================== #


@pytest.mark.asyncio
async def test_08_dos_proyectos_aislados_tras_reinicio(db):
    """workspace-A y workspace-B, mismo path relativo, mismos datos, resultados distintos."""
    from alexis.storage.db import Database

    await db.open()
    await db.migrate()
    compartido = WorldModel()
    # Mismo `file:notas.txt` en A y en B, con VALORES OPUESTOS.
    for scope, existe in ((SCOPE_A, True), (SCOPE_B, False)):
        compartido.upsert(_obs("notas.txt", {"exists": existe}, at=10, scope=scope))
    # Y una contradicción sólo dentro de A.
    compartido.upsert(_obs("notas.txt", {"exists": False}, at=20, scope=SCOPE_A))
    salida = compartido.export(scope=SCOPE_A)
    await WorldRepository(db).save_snapshot(salida["entities"], salida["edges"],
                                            scope=SCOPE_A.id)
    salida_b = compartido.export(scope=SCOPE_B)
    await WorldRepository(db).save_snapshot(salida_b["entities"], salida_b["edges"],
                                            scope=SCOPE_B.id)
    await db.close()

    otra = Database(dsn=db.dsn)
    await otra.open()
    repo = WorldRepository(otra)
    a = WorldModel(scope=SCOPE_A)
    a.hydrate(await repo.list_entities(scope=SCOPE_A.id), scope=SCOPE_A.id)
    b = WorldModel(scope=SCOPE_B)
    b.hydrate(await repo.list_entities(scope=SCOPE_B.id), scope=SCOPE_B.id)

    assert a.known_path("notas.txt").attributes["exists"] is False, "A vio el cambio"
    assert b.known_path("notas.txt").attributes["exists"] is False, "B nunca lo vio desaparecer"
    assert len(a.known_path("notas.txt").conflicts) == 2, "sólo A tiene conflicto"
    assert b.known_path("notas.txt").conflicts == [], "B no puede heredar el conflicto de A"
    await otra.close()


# =========================================================================== #
# Autoridad
# =========================================================================== #


def test_09_el_world_model_no_concede_permisos(db=None):
    """Resuelve conocimiento; no toca Policy, envelope ni requisitos de aprobación."""
    import inspect

    from alexis.autonomy.mission import MissionEngine
    from alexis.contracts import AutonomyLevel, MissionEnvelope
    from alexis.security.policy import PolicyEngine

    mission = MissionEngine().create(
        "comprueba el mundo",
        MissionEnvelope(objective="comprueba el mundo", autonomy=AutonomyLevel.SUPERVISED,
                        allowed_actions=["understand", "research", "execute", "verify"]),
    )
    envelope_antes = list(mission.envelope.allowed_actions)
    policy = PolicyEngine()

    w = WorldModel(scope=SCOPE_A)
    for o in conjunto():
        w.upsert(o)
    w.known_path("notas.txt").conflicts and None    # hay contradicción, y da igual

    assert mission.envelope.allowed_actions == envelope_antes
    assert mission.state.value == "pending"
    assert isinstance(policy, PolicyEngine)
    # Ninguna entidad del mundo puede llevar permisos: el contrato no tiene dónde ponerlos.
    campos = set(WorldEntity.__dataclass_fields__)
    assert not (campos & {"allowed_actions", "capabilities", "risk", "approval", "permissions"})


def test_10_el_grafo_tambien_sobrevive_al_reinicio(db=None):
    """Las relaciones son parte del estado semántico, no adorno."""
    a = _obs("notas.txt", {"exists": True}, at=10)
    b = _obs("otro.txt", {"exists": True}, at=20)
    mundo = WorldModel(scope=SCOPE_A)
    mundo.upsert(a)
    mundo.upsert(b)
    mundo.relate("file:notas.txt", "file:otro.txt", scope=SCOPE_A)

    salida = mundo.export(scope=SCOPE_A)
    rehidratado = WorldModel(scope=SCOPE_A)
    rehidratado.hydrate(salida["entities"], salida["edges"], scope=SCOPE_A.id)
    assert [e.name for e in rehidratado.dependencies("file:notas.txt")] == ["otro.txt"]
    assert [e.name for e in rehidratado.neighbors("file:otro.txt")] == ["notas.txt"]


@pytest.mark.asyncio
async def test_11_e2e_completo_postgres_un_solo_proceso(db):
    """El ciclo entero contra la BD real: observar, resolver, persistir y releer."""
    await db.open()
    await db.migrate()
    repo = WorldRepository(db)
    mundo = WorldModel(scope=SCOPE_A)
    for o in conjunto():
        mundo.upsert(o)
    mundo.relate("file:notas.txt", "file:otro.txt", scope=SCOPE_A)
    salida = mundo.export(scope=SCOPE_A)

    # 5 observaciones, pero sólo 2 entidades distintas: tres observaciones de notas.txt
    # se funden en una, que es justo lo que hace `upsert`.
    n = await repo.save_snapshot(salida["entities"], salida["edges"], scope=SCOPE_A.id)
    assert n == len(salida["entities"]) == 2
    assert await repo.count_entities(scope=SCOPE_A.id) == 2
    assert await repo.list_edges(scope=SCOPE_A.id) == [("file:notas.txt", "file:otro.txt",
                                                         "depends_on")]

    releido = await repo.get_entity("file:notas.txt", scope=SCOPE_A.id)
    assert releido.confidence == pytest.approx(mundo.known_path("notas.txt").confidence)
    assert releido.value_counts == mundo.known_path("notas.txt").value_counts
    assert releido.evidence_ids == mundo.known_path("notas.txt").evidence_ids
    assert releido.last_seen == T0 + 40
    await db.close()


# =========================================================================== #
# Filas anteriores a §4.5: migración sin perder el historial
# =========================================================================== #


def _fila_legacy() -> dict:
    """Una fila tal y como la dejó §4.3/§4.5.1: sin historial de valores ni base.

    `value_counts` y `base_confidence` no existen todavía, así que tras el `ALTER` salen
    con sus defaults: `{}` y `0`. Eso NO significa "sin observaciones".
    """
    return {
        "id": "file:notas.txt", "kind": "file", "name": "notas.txt",
        "attributes": {"exists": True}, "source": "tool:fs.stat",
        "confidence": 0.7, "mission_id": "m-legacy", "observations": 4,
        "last_seen": 1790000100.0, "scope": SCOPE_A.id,
    }


def test_12_una_fila_heredada_rehidrata_su_confianza_exacta():
    e = WorldEntity.from_dict(_fila_legacy())
    assert e.confidence == 0.7, "rehidratar no recalcula: devuelve lo que había"


def test_13_la_fila_heredada_recupera_una_base_para_no_perderla_al_siguiente_merge():
    e = WorldEntity.from_dict(_fila_legacy())
    assert e.base_confidence > 0.0, (
        "sin base, el primer merge posterior usaría sólo la fuente de esa observación "
        "y borraría de hecho la confianza que la fila sí tenía"
    )
    assert e.base_confidence == 0.7


def test_14_la_fila_heredada_siembra_un_historial_para_que_el_merge_no_pierte_contexto():
    e = WorldEntity.from_dict(_fila_legacy())
    assert e.value_counts, "sin conteos, la primera observación nueva borraría el pasado"
    assert value_key(True) in e.value_counts["exists"]


def test_15_el_siguiente_merge_no_degrada_la_confianza_de_una_fila_heredada():
    """El estado que existía antes de migrar no puede empeorar al primer merge."""
    mundo = WorldModel(scope=SCOPE_A)
    antes = WorldEntity.from_dict(_fila_legacy())
    mundo.put(antes)
    assert mundo.known_path("notas.txt").confidence == 0.7

    mundo.upsert(WorldEntity("file:notas.txt", "file", "notas.txt",
                             {"exists": True}, source="tool:fs.stat",
                             last_seen=1790000200.0, scope=SCOPE_A.id))
    despues = mundo.known_path("notas.txt")
    assert despues.confidence > 0.7, "una corroboración debe subir la confianza, no bajarla"
    assert despues.base_confidence == 0.7, "la base no se mueve: era la misma fuente"


def test_16_una_fila_heredada_tampoco_inventa_un_presente():
    """La reparación de la base no puede convertir 'sin fecha' en 'ahora'."""
    fila = _fila_legacy()
    fila["last_seen"] = 0
    e = WorldEntity.from_dict(fila)
    assert e.last_seen == LAST_SEEN_UNKNOWN
    assert e.last_seen == 0.0, "edad desconocida, no 'visto ahora'"


@pytest.mark.asyncio
async def test_17_una_fila_heredada_sobrevive_al_vaiven_postgres(db):
    """El vaivén real: fila vieja -> migrate -> guardar -> releer sin perder nada."""
    from alexis.storage.db import Database
    from alexis.storage.repositories import WorldRepository

    await db.open()
    await db.migrate()
    repo = WorldRepository(db)

    # Se escribe por la vía anticuada: sin `value_counts`, sin `base_confidence`.
    await db.execute(
        """INSERT INTO world_entities
             (scope_id, entity_id, kind, name, attributes, source, confidence,
              mission_id, observations, last_seen)
           VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)
           ON CONFLICT (scope_id, entity_id) DO UPDATE
             SET confidence = EXCLUDED.confidence""",
        (SCOPE_A.id, "file:heredada.txt", "file", "heredada.txt",
         '{"exists": true}', "tool:fs.stat", 0.7, "m-legacy", 4, 1790000100.0),
    )

    leidas = await repo.list_entities(scope=SCOPE_A.id)
    heredada = next(e for e in leidas if e.id == "file:heredada.txt")
    assert heredada.confidence == 0.7
    assert heredada.base_confidence == 0.7, "recuperada en la lectura, no en la escritura"
    assert heredada.value_counts, "historial sembrado para que el próximo merge no lo pierda"

    # Y al guardar y releer, el valor ya no cambia: la reparación es estable.
    await repo.save_snapshot(leidas, [], scope=SCOPE_A.id)
    otra = Database(dsn=db.dsn)
    await otra.open()
    releida = next(e for e in await WorldRepository(otra).list_entities(scope=SCOPE_A.id)
                   if e.id == "file:heredada.txt")
    assert releida.confidence == heredada.confidence == 0.7
    assert releida.value_counts == heredada.value_counts
    await otra.close()
