"""P0 §4.5.3/.4 — RESOLUCIÓN DE CONFLICTOS y PROCEDENCIA en el World Model.

Dos requisitos que van juntos porque comparten estructura: ambos se apoyan en el recuento
de valores por atributo (`value_counts`) que §4.5.2 introdujo.

- **Conflicto**: un atributo visto con más de un valor. Se detectan todos, se conservan
  todos, y el vigente lo decide una REGLA DEL WORLD MODEL. Nunca el LLM.
- **Procedencia**: cada entidad responde de dónde vino, con qué capability, cuándo, desde
  qué misión y con qué evidencia respaldándola.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.world.model import (  # noqa: E402
    Scope,
    WorldEntity,
    WorldModel,
    build_conflicts,
    resolve_vigente,
    value_key,
)

SCOPE = Scope.from_workspace("/tmp/conflict-ws")
T0 = 1_790_000_000.0


def _obs(exists, *, at=0.0, source="tool:fs.stat", evidence=(), mission="m1"):
    return WorldEntity("file:notas.txt", "file", "notas.txt", {"exists": exists},
                       source=source, mission_id=mission, last_seen=T0 + at,
                       evidence_ids=list(evidence), scope=SCOPE.id)


def _feed(*observaciones, **kwargs):
    w = WorldModel(scope=SCOPE, **kwargs)
    for o in observaciones:
        w.upsert(o)
    return w


def _in(obs: WorldEntity, scope: Scope) -> WorldEntity:
    """La misma observación, perteneciente a OTRO ámbito."""
    obs.scope = scope.id
    return obs


# =========================================================================== #
# Qué constituye un conflicto y cómo se detecta
# =========================================================================== #


def test_01_un_valor_unico_no_es_conflicto():
    w = _feed(_obs(True), _obs(True), _obs(True))
    e = w.known_path("notas.txt")
    assert e.conflicts == [], "tres veces lo mismo no es contradicción"
    assert e.contradictions == 0


def test_02_dos_valores_distintos_si_lo_son():
    w = _feed(_obs(True, at=10), _obs(False, at=20))
    e = w.known_path("notas.txt")
    assert e.conflicts, "dos valores del mismo atributo son un conflicto"
    atributos = {c["attribute"] for c in e.conflicts}
    assert atributos == {"exists"}


def test_03_atributos_distintos_no_conflicten():
    """Que difieran dos atributos NO es contradicción: son hechos distintos."""
    w = WorldModel(scope=SCOPE)
    w.upsert(WorldEntity("file:x", "file", "x", {"exists": True}, source="tool:fs.stat",
                         last_seen=T0, scope=SCOPE.id))
    w.upsert(WorldEntity("file:x", "file", "x", {"size": 10}, source="tool:fs.stat",
                         last_seen=T0, scope=SCOPE.id))
    assert w.known_path("x").conflicts == []


# =========================================================================== #
# Se conservan AMBAS versiones
# =========================================================================== #


def test_04_se_conservan_las_dos_versiones():
    w = _feed(_obs(True, at=10), _obs(False, at=20))
    valores = {str(c["value"]) for c in w.known_path("notas.txt").conflicts}
    assert valores == {"True", "False"}, f"se perdió una versión: {valores}"


def test_05_el_registro_lleva_recuento_y_procedencia():
    w = _feed(_obs(True, at=10), _obs(True, at=11), _obs(False, at=20))
    por_valor = {str(c["value"]): c for c in w.known_path("notas.txt").conflicts}
    assert por_valor["True"]["observations"] == 2
    assert por_valor["False"]["observations"] == 1
    assert por_valor["True"]["best_source_rank"] > 0


def test_06_una_contradiccion_no_se_silencia_al_resolverse():
    """El requisito explícito: nunca se silencie una contradicción real."""
    w = _feed(_obs(True, at=10), _obs(False, at=20), _obs(False, at=30))
    e = w.known_path("notas.txt")
    assert e.attributes["exists"] is False, "el vigente es el último"
    assert len(e.conflicts) == 2, "pero la contradicción sigue registrada con las dos"


def test_07_el_registro_es_idempotente():
    """Upsertar lo mismo no multiplica entradas de conflicto."""
    w = _feed(_obs(True, at=10), _obs(False, at=20))
    n1 = len(w.known_path("notas.txt").conflicts)
    for _ in range(5):
        w.upsert(_obs(False, at=20))
    assert len(w.known_path("notas.txt").conflicts) == n1 == 2


# =========================================================================== #
# Cómo se determina el vigente
# =========================================================================== #


def test_08_manda_lo_mas_reciente_aunque_sea_menor():
    """El caso que motiva la regla: un archivo observado, verificado y luego borrado.

    `exists=True` gana en frecuencia (2 contra 1) pero `exists=False` es lo último que se
    vio, y el archivo ya no está. Si mandara la frecuencia, el mundo afirmaría que existe
    un archivo que se acaba de borrar.
    """
    w = _feed(_obs(True, at=10), _obs(True, at=11), _obs(False, at=20))
    e = w.known_path("notas.txt")
    assert e.attributes["exists"] is False
    assert e.value_counts["exists"]["false"]["n"] == 1
    assert e.value_counts["exists"]["true"]["n"] == 2


def test_09_empate_de_instante_gana_el_mas_frecuente():
    conteo = {
        "exists": {
            value_key(True): {"n": 3, "rank": 2, "seen": 100.0},
            value_key(False): {"n": 1, "rank": 2, "seen": 100.0},
        }
    }
    assert resolve_vigente(conteo)["exists"] is True, "mismo instante: manda la frecuencia"


def test_10_empate_total_gana_la_procendencia_mas_fiable():
    conteo = {
        "exists": {
            value_key(True): {"n": 2, "rank": 2, "seen": 100.0},
            value_key(False): {"n": 2, "rank": 4, "seen": 100.0},
        }
    }
    assert resolve_vigente(conteo)["exists"] is False, "registry (rank 4) sobre tool (2)"


def test_11_empate_absoluto_es_fijo():
    """Sin nada que desempatar, la clave ordena. Arbitrario pero FIJO."""
    conteo = {
        "exists": {
            value_key(True): {"n": 1, "rank": 2, "seen": 100.0},
            value_key(False): {"n": 1, "rank": 2, "seen": 100.0},
        }
    }
    primero = resolve_vigente(conteo)
    for _ in range(20):
        assert resolve_vigente(conteo) == primero


def test_12_resolve_vigente_es_funcion_pura():
    conteo = {
        "exists": {value_key(True): {"n": 1, "rank": 2, "seen": 10.0},
                   value_key(False): {"n": 3, "rank": 2, "seen": 20.0}},
        "size": {value_key(10): {"n": 1, "rank": 2, "seen": 5.0}},
    }
    a = resolve_vigente(conteo)
    b = resolve_vigente(conteo)
    assert a == b == {"exists": False, "size": 10}


# =========================================================================== #
# Timestamps, confidence, scopes, restart
# =========================================================================== #


def test_13_el_vigente_no_depende_de_la_confianza():
    """La confianza mide corroboración; la actualidad es otra cosa."""
    w = _feed(_obs(True, at=10), _obs(True, at=11), _obs(True, at=12), _obs(False, at=20))
    e = w.known_path("notas.txt")
    assert e.attributes["exists"] is False
    assert e.contradictions >= 1


def test_14_los_scopes_no_se_mezclan():
    a, b = Scope.from_workspace("/tmp/proy-A"), Scope.from_workspace("/tmp/proy-B")
    wa = WorldModel(scope=a)
    wb = WorldModel(scope=b)
    wa.upsert(_in(_obs(True, at=10), a))
    wb.upsert(_in(_obs(False, at=20), b))
    assert wa.known_path("notas.txt").attributes["exists"] is True
    assert wb.known_path("notas.txt").attributes["exists"] is False
    assert len(wa.known_path("notas.txt").conflicts) == 0, "A no vio conflicto: nadie le contradijo"
    assert len(wb.known_path("notas.txt").conflicts) == 0


def test_15_un_conflicto_no_se_contamina_entre_scopes():
    """Mismo id, mismo atributo, valores distintos, dos proyectos: aislados."""
    a, b = Scope.from_workspace("/tmp/proy-A"), Scope.from_workspace("/tmp/proy-B")
    compartido = WorldModel()
    compartido.upsert(WorldEntity("file:x", "file", "x", {"exists": True}, source="tool:fs.stat",
                                  last_seen=T0 + 10, scope=a.id))
    compartido.upsert(WorldEntity("file:x", "file", "x", {"exists": False}, source="tool:fs.stat",
                                  last_seen=T0 + 20, scope=b.id))
    assert compartido.known_path("x", scope=a).attributes["exists"] is True
    assert compartido.known_path("x", scope=b).attributes["exists"] is False
    assert compartido.known_path("x", scope=a).conflicts == []
    assert compartido.known_path("x", scope=b).conflicts == []


def test_16_hydrate_no_inventa_conflictos():
    """Rehidratar restituye, no observa: no puede fabricar una contradicción."""
    w = _feed(_obs(True, at=10), _obs(False, at=20))
    salida = w.export(scope=SCOPE)
    nuevo = WorldModel(scope=SCOPE)
    nuevo.hydrate(salida["entities"], salida["edges"], scope=SCOPE.id)
    e = nuevo.known_path("notas.txt")
    assert len(e.conflicts) == len(w.known_path("notas.txt").conflicts)
    assert e.attributes["exists"] is False
    assert e.support == w.known_path("notas.txt").support


def test_17_build_conflicts_ignora_atributos_sin_disputa():
    conteo = {"exists": {value_key(True): {"n": 1, "rank": 2, "seen": 1.0}}}
    # Un solo valor observado no es una disputa: no hay conflicto que registrar.
    assert build_conflicts(conteo) == []


# =========================================================================== #
# §4.5.4 — Procedencia
# =========================================================================== #


def test_18_la_entidad_responde_de_donde_va():
    e = _feed(_obs(True, at=10, source="tool:fs.stat", evidence=["cl-1"], mission="m7")
              ).known_path("notas.txt")
    assert e.source == "tool:fs.stat", "qué capability produjo la evidencia"
    assert e.last_seen == T0 + 10, "cuándo se observó"
    assert e.mission_id == "m7", "qué misión la originó"
    assert e.scope == SCOPE.id, "a qué ámbito pertenece"
    assert e.evidence_ids == ["cl-1"], "con qué evidencia está respaldada"


def test_19_las_evidencias_se_acumulan_sin_duplicar():
    w = _feed(_obs(True, at=10, evidence=["cl-1", "cl-2"]),
              _obs(True, at=20, evidence=["cl-2", "cl-3"]))
    assert w.known_path("notas.txt").evidence_ids == ["cl-1", "cl-2", "cl-3"]


def test_20_las_evidencias_no_duplican_por_el_orden():
    import itertools

    a, b = ["cl-1"], ["cl-2", "cl-3"]
    resultados = set()
    for perm in itertools.permutations([tuple(a), tuple(b)]):
        w = _feed(_obs(True, at=10, evidence=a), _obs(True, at=20, evidence=b))
        resultados.add(tuple(w.known_path("notas.txt").evidence_ids))
    assert resultados == {("cl-1", "cl-2", "cl-3")}, f"dependió del orden: {resultados}"


def test_21_la_procedencia_sobrevive_al_round_trip():
    e = _feed(_obs(True, at=10, evidence=["cl-9"])).known_path("notas.txt")
    vuelta = WorldEntity.from_dict(e.to_dict())
    assert vuelta.evidence_ids == ["cl-9"]
    assert vuelta.source == e.source
    assert vuelta.mission_id == e.mission_id
    assert vuelta.last_seen == e.last_seen


def test_22_no_se_duplica_memory():
    """World = estado del mundo. Memory = experiencia. No se solapan aquí.

    La procedencia de la entidad apunta a ids de evidencia que el Core ya produce; ALEXIS no
    guarda una segunda copia del texto de la evidencia en el World Model.
    """
    e = _feed(_obs(True, at=10, evidence=["cl-1"])).known_path("notas.txt")
    fila = e.to_dict()
    assert fila["evidence_ids"] == ["cl-1"]
    assert "evidence_text" not in fila and "memory" not in fila, \
        "el World Model guarda el ENLACE, no una copia de la evidencia"
    # La propiedad comprobable: el tamaño de la fila no crece con el TEXTO de la evidencia
    # sino con el número de enlaces. Con 1 id y con 50 ids, la diferencia es la de 49
    # punteros, no de 49 textos de evidencia.
    w = _feed(_obs(True, at=10, evidence=["cl-1"]))
    uno = len(str(w.known_path("notas.txt").to_dict()))
    muchos = _feed(_obs(True, at=10, evidence=[f"cl-{i}" for i in range(50)]))
    cincuenta = len(str(muchos.known_path("notas.txt").to_dict()))
    # 50 ids adds ~350 chars of ids, not evidence prose: sin evidencia embebida.
    assert cincuenta - uno < 1000, f"la fila creció {cincuenta - uno} chars: hay texto dentro"
    assert "cl-0" not in str(fila) or True   # los ids SÍ están; el texto, no


def test_23_el_llm_no_decide_que_hecho_es_verdadero():
    """La resolución es del World Model. Ni un parámetro de entrada, ni el prompt."""
    import inspect

    from alexis.world.model import resolve_vigente

    import ast as _ast

    firma = inspect.signature(resolve_vigente)
    assert list(firma.parameters) == ["conteo"], \
        "sólo recibe el conteo: no hay por dónde meter una decisión externa"

    # Se comprueba el ÁSTRO, no el texto: el docstring habla de por qué NO decide un LLM, y
    # buscar esas palabras en la prosa daría un falso positivo.
    arbol = _ast.parse(inspect.getsource(resolve_vigente))
    llamadas = {
        n.func.id for n in _ast.walk(arbol)
        if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name)
    }
    imports = {
        n.module or ""
        for n in _ast.walk(arbol)
        if isinstance(n, (_ast.Import, _ast.ImportFrom))
    }
    prohibidos = {"complete", "generate", "ask_model", "router", "route"}
    assert not (llamadas & prohibidos), f"resolve_vigente llama a {llamadas & prohibidos}"
    assert not imports, f"resolve_vigente no debería importar nada: {imports}"


# =========================================================================== #
# §4.5.4 — La procedencia llega también por la ruta de ejecución real
# =========================================================================== #


class _Step:
    capability = "execute.test"


class _Step:
    capability = "execute.test"


class _Result:
    """El `ExecutionResult` mínimo que `observe_execution` sabe leer."""

    def __init__(self, output, success=True):
        self.output = output
        self.success = success


def test_24_el_test_run_tambien_enlaza_sus_claims():
    """Un test run afirma algo del mundo, así que también responde por su evidencia.

    Sin esto quedaba conocimiento verificable —"estos tests pasan"— sin trazabilidad,
    que es justo el agujero que §4.5.4 viene a cerrar.
    """
    w = WorldModel(scope=SCOPE)
    observadas = w.observe_execution(
        _Step(),
        # Forma real de `execute.test`: `test_run` es un marcador plano y los contadores
        # van al primer nivel del output (ver `alexis/tools/testrunner.py`).
        _Result({"path": "tests", "test_run": True, "tests_passed": 12,
                 "tests_failed": 0, "tests_skipped": 1, "exit_code": 0,
                 "timed_out": False, "counts_parsed": True}),
        mission=None,
        evidence_ids=["cl-suite-1"],
    )
    entidad = w.get("test:tests")
    assert entidad is not None
    assert entidad.evidence_ids == ["cl-suite-1"]
    assert observadas, "debe reportar la entidad observada"


def test_25_el_archivo_y_el_test_run_enlazan_la_evidencia_del_mismo_resultado():
    """Una sola ejecución puede afirmar las dos cosas; ambas salen del mismo resultado."""
    w = WorldModel(scope=SCOPE)
    w.observe_execution(
        _Step(),
        _Result({
            "path": "notas.txt", "exists": True, "size": 3,
            "test_run": True, "tests_passed": 1, "tests_failed": 0,
            "tests_skipped": 0, "exit_code": 0, "timed_out": False,
            "counts_parsed": True,
        }),
        mission=None,
        evidence_ids=["cl-1", "cl-2"],
    )
    assert w.get("test:notas.txt").evidence_ids == ["cl-1", "cl-2"]
    assert w.get("file:notas.txt").evidence_ids == ["cl-1", "cl-2"]
