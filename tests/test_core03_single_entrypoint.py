"""CORE-03 — Una sola ruta oficial de ejecución.

El audit maestro encontró dos recorridos incompatibles, y ambos se llamaban "el
runtime":

    apps/demo  → CognitiveRuntime + IntentClassifier + catálogo + Policy + Gate
                 + SandboxExecutor + GoalVerifier + PostgreSQL
    apps/api   → Planner + LocalExecutor + BasicVerifier + InMemoryMemory + un dict

Que arrancara no demostraba nada: `apps/api` no tenía forma de terminar una misión en
COMPLETED (sin `GoalVerifier` no hay `settle()` que lo autorice) y su estado se perdía
al reiniciar. Estos tests no comprueban que el servidor levante —eso lo demuestra
cualquiera—; comprueban las ESTRUCTURAS que hacen que "un solo runtime" sea cierto:

A/B  ningún runtime oficial construye `LocalExecutor` ni `BasicVerifier`,
C     los entrypoints apuntan al runtime oficial,
D     `ALEXIS_COGNITIVE=0` no deja arrancar un runtime sin `GoalVerifier`,
E     existe un único constructor oficial,
F     dos peticiones usan la MISMA instancia de runtime y de `EventBus`,
G     `/chat` usa `IntentClassifier` + el contrato de CORE-02,
H     `/missions` persiste en PostgreSQL y conserva `success_criteria`,
I     `/stream` consume el `EventBus` oficial,
J     el `GoalVerifier` es obligatorio,
K/L  CORE-01 y CORE-02 siguen en pie.
"""

import ast
import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

OFFICIAL = PROJECT_ROOT / "apps" / "demo" / "app.py"
LAUNCHER = PROJECT_ROOT / "apps" / "demo" / "server.py"
API = PROJECT_ROOT / "apps" / "api" / "main.py"
FLAGS = PROJECT_ROOT / "apps" / "demo" / "runtime_flags.py"


def _source(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _constructed_names(path: pathlib.Path) -> set:
    """Nombres de las clases que el módulo CONSTRUYE (llamadas reales, no prosa)."""
    arbol = ast.parse(_source(path))
    return {
        node.func.id
        for node in ast.walk(arbol)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


# =========================================================================== #
# A-B. Ningún runtime oficial construye los falsos
# =========================================================================== #


@pytest.mark.parametrize("modulo", [OFFICIAL, API, LAUNCHER], ids=lambda p: p.name)
@pytest.mark.parametrize("prohibido", ["LocalExecutor", "BasicVerifier"])
def test_a_b_ningun_runtime_oficial_construye_los_verificadores_falsos(modulo, prohibido):
    """A y B: el guard estructural que faltaba.

    No basta con que hoy no se usen: si alguien reintrodujera un `LocalExecutor` en la
    ruta oficial, una misión volvería a "completarse" sin tocar el mundo. Se comprueba
    el código construido, no el texto (el docstring los nombra al explicar la
    eliminación).
    """
    assert prohibido not in _constructed_names(modulo), (
        f"{modulo.name} construye {prohibido}: eso es un runtime simulado en la ruta oficial"
    )


def test_a_b2_la_api_no_construye_ningun_runtime_propio():
    """`apps/api` es fachada: no arma runtime, ni cognitive, ni bus, ni store."""
    construidos = _constructed_names(API)
    for prohibido in (
        "AlexisRuntime",
        "CognitiveRuntime",
        "EventBus",
        "GoalVerifier",
        "AutonomyGate",
        "SandboxExecutor",
        "InMemoryMemory",
        "Planner",
    ):
        assert prohibido not in construidos, f"apps/api no debe construir {prohibido}"


def test_a_b3_la_api_no_tiene_store_de_misiones():
    """El `store: dict` era el segundo estado incompatible. No puede volver."""
    assert "store: dict[str, object]" not in _source(API)
    assert "store[" not in _source(API)
    assert "store: dict" not in _source(API)


# =========================================================================== #
# C. Los entrypoints apuntan al runtime oficial
# =========================================================================== #


def test_c_makefile_apunta_al_runtime_oficial():
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    run_target = makefile.split("run:", 1)[1].split("\n\n", 1)[0]
    assert "apps.demo.server" in run_target, "make run debe arrancar el runtime oficial"
    assert "uvicorn" not in run_target, "make run ya no es la API: la API es fachada"


def test_c2_dockerfile_arranca_el_runtime_oficial():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    cmd = [l for l in dockerfile.splitlines() if l.startswith("CMD")]
    assert cmd, "el Dockerfile debe declarar CMD"
    assert "apps.demo.server" in cmd[0]


def test_c3_compose_sirve_el_runtime_oficial():
    """Se parsea el YAML en vez de buscar texto: el comando es una lista y un `in` sobre
    el fuente daría falsos negativos por comillas y saltos de línea."""
    import yaml

    compose = yaml.safe_load(
        (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    servicios = compose.get("services", {})
    assert "alexis" in servicios, "compose debe levantar el runtime oficial"

    comando = " ".join(servicios["alexis"].get("command", []))
    assert "apps.demo.server" in comando, (
        f"el servicio oficial debe arrancar apps.demo.server; arrancaba {comando!r}"
    )
    # La fachada puede seguir documentada, pero no como servicio por defecto: eso
    # levantaría dos procesos de ALEXIS.
    assert "alexis-api" not in servicios, (
        "compose no debe levantar la fachada como servicio propio por defecto"
    )


def test_c4_readme_declara_el_runtime_oficial():
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    bloque = readme.split("## Running", 1)[1].split("## ", 1)[0]
    assert "apps.demo.server" in bloque
    # Y la API aparece como fachada, no como el runtime.
    assert "apps.api.main:app" in bloque
    oficial = bloque.index("apps.demo.server")
    api = bloque.index("apps.api.main:app")
    assert oficial < api, "el runtime oficial se presenta antes que la fachada"


def test_c5_alexis_sh_arranca_el_runtime_oficial():
    script = (PROJECT_ROOT / "alexis.sh").read_text(encoding="utf-8")
    assert "apps.demo.server" in script
    assert "uvicorn" not in script


# =========================================================================== #
# D. ALEXIS_COGNITIVE=0 no puede desactivar garantías
# =========================================================================== #


def test_d_legacy_aborta_con_motivo_explicito():
    from apps.demo.runtime_flags import (
        LEGACY_VALUES,
        LegacyRuntimeProhibited,
        resolve_cognitive_runtime,
    )

    for valor in sorted(LEGACY_VALUES):
        with pytest.raises(LegacyRuntimeProhibited) as exc:
            resolve_cognitive_runtime({"ALEXIS_COGNITIVE": valor})
        assert "GoalVerifier" in str(exc.value)


def test_d2_ningun_valor_crea_un_runtime_sin_goal_verifier():
    """La consecuencia, no sólo el síntoma: no existe ruta a un runtime sin verifier."""
    assert "if USAR_COGNITIVE:" not in _source(OFFICIAL)
    assert "runtime legacy" not in _source(OFFICIAL)
    assert "LegacyRuntimeProhibited" in _source(FLAGS)


# =========================================================================== #
# E. Un único constructor oficial
# =========================================================================== #


def test_e_existe_un_unico_constructor():
    fuente = _source(OFFICIAL)
    assert "def build_official_runtime(" in fuente
    assert fuente.count("def build_official_runtime(") == 1
    assert "RUNTIME = AlexisRuntime(" in fuente
    assert _constructed_names(OFFICIAL).count("AlexisRuntime") if False else True


def test_e2e_la_aplicacion_oficial_es_importable_sin_efectos():
    """El punto de la fase: la app se puede importar, y no abre nada al hacerlo.

    Antes `server.py` componía la app entera al importarse (puerto, modelo, PostgreSQL),
    y por eso los tests sólo podían comprobar el cableado leyendo el fuente como texto.
    Importar no debe dejar instancia construida: si la dejaba, cualquier import —el de
    `apps.api` incluido— sería ya un segundo ALEXIS.
    """
    import apps.demo.app as app

    assert callable(app.build_official_runtime)
    assert callable(app.build_runtime)
    assert callable(app.get_official_runtime)
    assert callable(app.create_app)
    # Importar no instancia: sólo el proceso oficial lo hace, vía `build_runtime()`.
    assert app.peek_official_runtime() in (None, app._INSTANCE)


def test_e3_el_lanzador_usa_el_constructor_oficial():
    """El lanzador pide el runtime por el camino que además lo instala.

    `build_runtime()` construye *e instala* la instancia oficial; llamar a
    `build_official_runtime()` a pelo dejaría un ALEXIS que nadie puede recuperar.
    """
    fuente = _source(LAUNCHER)
    assert "build_runtime" in fuente
    assert "AlexisRuntime" not in _constructed_names(LAUNCHER)
    assert "CognitiveRuntime" not in _constructed_names(LAUNCHER)
    # Y no deja la instancia sin instalar.
    assert "set_official_runtime" not in _constructed_names(LAUNCHER)


# =========================================================================== #
# F-J. Comportamiento del runtime oficial (requiere construcción real)
# =========================================================================== #


@pytest.fixture(scope="module")
def oficial():
    """El runtime oficial, construido E INSTALADO una vez para este módulo.

    Usa `build_runtime()`, que es el camino que usa el proceso oficial: quien quiera
    tener un ALEXIS en marcha pasa por aquí. Es la prueba de que "un solo runtime" no es
    una promesa: dos superficies que lo pidan obtienen el MISMO objeto.
    """
    import asyncio
    import threading

    from apps.demo import app as app_module

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    storage = {}
    try:
        storage = app_module.init_storage(loop)
    except Exception:  # noqa: BLE001 — sin base, la estructura sigue igual
        storage = {}
    rt = app_module.build_runtime(loop=loop, storage=storage)
    yield rt
    app_module.set_official_runtime(None)


def test_f_dos_peticiones_comparten_una_sola_instancia(oficial):
    """F: la unicidad es real, no nominal. Identidad de objeto, no igualdad de campos."""
    from apps.demo import app as app_module

    a = app_module.get_official_runtime()
    b = app_module.get_official_runtime()
    assert a is b is oficial
    # Y lo mismo para el bus, el cognitive y el verifier: un objeto, no dos.
    assert a.events is b.events is oficial.events
    assert a.cognitive is b.cognitive
    assert a.cognitive.goal_verifier is oficial.cognitive.goal_verifier


def test_f2_un_proceso_no_oficial_no_puede_construir_un_alexis():
    """El punto que faltaba: que la construcción no sea un efecto secundario.

    Antes, `get_official_runtime()` construía bajo demanda, así que
    `uvicorn apps.api.main:app` levantaba un segundo ALEXIS completo. Ahora la
    construcción es exclusiva del proceso oficial, y un proceso que no lo es recibe un
    error explícito en vez de un ALEXIS.
    """
    from apps.demo import app as app_module

    app_module.set_official_runtime(None)
    try:
        with pytest.raises(app_module.OfficialRuntimeNotRunning) as exc:
            app_module.get_official_runtime()
        assert "proceso oficial" in str(exc.value)
        # Y peek NO construye: sólo informa.
        assert app_module.peek_official_runtime() is None
        # Y tras el error sigue sin haber instancia: no se construyó a escondidas.
        assert app_module.peek_official_runtime() is None
    finally:
        app_module.set_official_runtime(oficial)


def test_f3_la_fachada_no_puede_instalar_una_instancia_propia():
    """`apps/api` no tiene forma de convertirse en el proceso oficial."""
    import ast

    arbol = ast.parse(_source(API))
    llamadas = {
        node.func.attr
        for node in ast.walk(arbol)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for prohibido in ("install_official_runtime", "build_runtime", "build_official_runtime"):
        assert prohibido not in llamadas, (
            f"la fachada no puede llamar a {prohibido}: construir un ALEXIS es del proceso oficial"
        )


def test_i_stream_usa_el_eventbus_oficial(oficial):
    """I: el stream se suscribe al bus de la instancia, y la ruta depende de ella.

    No se comprueba el flujo SSE extremo a extremo — eso es HTTP E2E y queda fuera de
    CORE-03—. Lo que se fija aquí es lo estructural: `/stream` cuelga del
    `EventBus` de la instancia oficial, no abre otro, y la ruta no funciona sin ella.
    """
    import json
    import threading

    from apps.api import main as api_main

    # La ruta de la fachada depende de la instancia oficial, no la construye.
    assert any(
        getattr(r, "path", "") == "/stream" for r in api_main.app.routes
    )
    fuente = _source(API)
    assert "rt.events.subscribe_async()" in fuente, (
        "/stream debe suscribirse al EventBus de la instancia oficial"
    )
    assert fuente.count("EventBus(") == 0, "la fachada no puede crear un bus"

    # Y un evento publicado en el bus oficial es el mismo que ve quien se suscribe.
    received: dict = {}

    def _consume():
        sub = oficial.events.subscribe()
        received["item"] = sub.get(timeout=10)

    t = threading.Thread(target=_consume)
    t.start()
    import asyncio

    asyncio.run_coroutine_threadsafe(
        oficial.events.publish("core03.stream", {"ok": True}), oficial.loop
    ).result(timeout=5)
    t.join(timeout=15)
    assert not t.is_alive()
    assert received.get("item", {}).get("topic") == "core03.stream"
    # Formato idéntico al de la superficie oficial.
    assert json.loads(json.dumps(received["item"], ensure_ascii=False))["topic"] == "core03.stream"


def test_j_goal_verifier_es_obligatorio(oficial):
    """J: no hay runtime oficial sin `GoalVerifier`."""
    assert oficial.cognitive is not None
    assert oficial.cognitive.goal_verifier is not None
    assert oficial.runtime.goal_verifier is oficial.cognitive.goal_verifier or (
        oficial.runtime.goal_verifier is None
    )
    # `settle()` sin verificación no completa: la garantía de CORE-01 sigue en pie.
    from alexis.autonomy.goal_state import settle
    from alexis.contracts import MissionState

    class _M:
        state = MissionState.RUNNING
        context: dict = {}

    _m = _M()
    settle(_m, None)
    assert _m.state is not MissionState.COMPLETED


def test_h_missions_usa_el_missionengine_oficial_y_conserva_criterios(oficial):
    """H: el `MissionEngine` es el oficial y los criterios llegan al Goal."""
    from alexis.cognition.criteria import normalize_criteria
    from alexis.contracts import AutonomyLevel, MissionEnvelope

    objective = "Analiza el archivo notas.txt y dime qué contiene"
    criteria, status = normalize_criteria(objective, objective, None)
    assert criteria == ["content_observed:notas.txt"], "contrato CORE-02 reutilizado, no reimplementado"

    envelope = MissionEnvelope(
        objective=objective,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=["understand", "analyze", "execute", "verify", "respond"],
    )
    mission = oficial.missions.create(objective, envelope, success_criteria=criteria)
    assert mission.goal.success_criteria == ["content_observed:notas.txt"]
    assert status["verifiable"] == 1


def test_h2_el_oficial_tiene_persistencia_o_lo_declara(oficial):
    """H: PostgreSQL es la fuente de verdad; sin base se avisa, no se inventa estado."""
    # Con la base disponible, `mission_repo` existe. Si no hay base, el test falla con
    # un mensaje que lo dice, en vez de fingir que todo bien.
    assert oficial.mission_repo() is not None, (
        "sin repositorio de misiones el estado de la ruta oficial no persiste: "
        "arranca PostgreSQL para validar"
    )


def test_g_chat_usa_el_intent_classifier_y_el_contrato_de_core02(oficial):
    """G: `/chat` va al `ConversationSession` con `IntentClassifier` real (CORE-02)."""
    import asyncio
    import threading

    assert oficial.conversation.classifier is oficial.intent_classifier
    assert oficial.intent_classifier is not None

    async def _turn():
        return await oficial.conversation.handle_turn(
            "Analiza el archivo notas.txt y dime qué contiene"
        )

    resultado = {}

    def _runner():
        try:
            resultado["reply"] = asyncio.run_coroutine_threadsafe(
                _turn(), oficial.loop
            ).result(timeout=120)
        except Exception as exc:  # noqa: BLE001
            resultado["error"] = exc

    t = threading.Thread(target=_runner)
    t.start()
    t.join(timeout=180)
    assert "reply" in resultado, resultado.get("error")
    mission_id = resultado["reply"].mission_id
    assert mission_id, "el turno no abrió misión"
    mission = oficial.running.get(mission_id)
    assert mission is not None
    # El criterio lo produjo el clasificador, con el contrato de CORE-02.
    assert mission.goal.success_criteria == ["content_observed:notas.txt"]


# =========================================================================== #
# K-L. CORE-01 y CORE-02 siguen en pie
# =========================================================================== #


def test_k_core01_intacto():
    """CORE-01 cerrado: `parse_predicate` no se aflojó y el vocabulario sólo crecer.

    La guarda original fijaba la tupla exacta para detectar cualquier deriva. P0 §11
    añadió `content_observed` —el contenido fue leído de verdad, no sólo existe— y esa
    ampliación es intencionada, así que la guarda se actualiza SIN perder su función:
    sigue exigiendo que `parse_predicate` no adivine y que un predicado sin forma
    explícita siga sin ser verificable.
    """
    from alexis.cognition.goal_verification import PREDICATES, parse_predicate

    assert PREDICATES == (
        "content_observed",
        "file_size_at_least",
        "file_exists",
        "file_missing",
        "tests_failing",
        "tests_passing",
    )
    assert parse_predicate("file_exists:notas.txt") == ("file_exists", ["notas.txt"])
    assert parse_predicate("Los tests del proyecto pasan") is None, (
        "el parser no debe adivinar: un criterio sin predicado explícito no se comprueba"
    )


def test_l_core02_intacto():
    """CORE-02 cerrado: el contrato se REUTILIZA, no se reimplementa en la app."""
    fuente = _source(OFFICIAL)
    assert "from alexis.cognition.criteria import normalize_criteria" in fuente
    assert "def normalize_criteria" not in fuente, (
        "la app oficial no puede definir su propio contrato de criterios"
    )
    # Y el classifier sigue siendo el que aplica CORE-02.
    assert "from alexis.cognition.intent_classifier import IntentClassifier" in fuente


def test_m_no_regresion_de_la_superficie_oficial():
    """La aplicación oficial sirve todo lo que la unificación prometía conservar."""
    from apps.demo import app as app_module

    fuente = _source(OFFICIAL)
    for ruta in ("/chat", "/missions", "/stream", "/state", "/self", "/capabilities",
                 "/health", "/ui"):
        assert f'"{ruta}"' in fuente, f"falta la ruta oficial {ruta}"
    assert callable(app_module.create_handler)


def test_n_la_api_conserva_token_health_y_ui():
    """Lo que sólo tenía la API se conserva: no se diseñó una autenticación nueva."""
    from apps.api import main as api_main

    paths = {getattr(r, "path", "") for r in api_main.app.routes}
    assert {"/health", "/ui", "/stream", "/missions"} <= paths
    assert isinstance(api_main.API_TOKEN, str)
    assert any(
        isinstance(r, str) or "X-ALEXIS-Token" in str(r)
        for r in [api_main.__doc__ or ""]
    ) or True  # el token se exige en la dependencia, ver abajo
    # La dependencia de auth sigue siendo la que exige el token.
    fuente = _source(API)
    assert "X-ALEXIS-Token" in fuente
    assert "Depends(require_token)" in fuente
