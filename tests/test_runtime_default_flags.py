"""CORE-03 — El runtime oficial es el CognitiveRuntime, y es el único.

Antes este fichero fijaba una decisión de DOS caminos: `ALEXIS_COGNITIVE=0` pedía el
recorrido legacy del plan. CORE-03 cerró ese camino, y no por limpieza: el recorrido
legacy dejaba `RUNTIME.cognitive` a `None` y nadie inyectaba un `GoalVerifier` en su
lugar, así que `_verify_goal()` devolvía `None`, `settle()` no confirmaba nada y
**ninguna misión podía terminar en COMPLETED**. La bandera no elegía otro modo de
funcionar: desactivaba en silencio la garantía de CORE-01.

Lo que queda fijado aquí, entonces, es más fuerte que un default:

- el `CognitiveRuntime` es obligatorio y no hay rama que lo apague,
- pedir el legacy **aborta**, con un motivo que explica la consecuencia,
- el runtime oficial se construye con la MISMA policy, el MISMO gate y el MISMO
  verifier que el resto del sistema, y siempre con `GoalVerifier`,
- la decisión se evalúa una sola vez, y la construcción ocurre en un único sitio.

`apps/demo/app.py` es importable a propósito (CORE-03), así que el resolver se importa
de verdad y los call sites se verifican leyendo el módulo oficial.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from apps.demo.runtime_flags import (  # noqa: E402
    ALEXIS_COGNITIVE_ENV,
    COGNITIVE_VALUES,
    DEFAULT_IS_COGNITIVE,
    LEGACY_VALUES,
    LegacyRuntimeProhibited,
    resolve_cognitive_runtime,
)

#: CORE-03: la construcción del runtime vive en el módulo oficial. `server.py` es el
#: lanzador y no construye nada.
OFFICIAL = PROJECT_ROOT / "apps" / "demo" / "app.py"
LAUNCHER = PROJECT_ROOT / "apps" / "demo" / "server.py"
OFFICIAL_SOURCE = OFFICIAL.read_text(encoding="utf-8")
LAUNCHER_SOURCE = LAUNCHER.read_text(encoding="utf-8")


def _bloque_de_llamada(fuente: str, cabecera: str) -> str:
    """El texto de una llamada, hasta su paréntesis de cierre REAL.

    Cortar por el primer `)` no vale: `memory=(PostgresMemoryProvider(...) if ...)`
    tiene paréntesis anidados y truncaría antes de tiempo.
    """
    inicio = fuente.index(cabecera)
    i, depth = inicio, 0
    while i < len(fuente):
        if fuente[i] == "(":
            depth += 1
        elif fuente[i] == ")":
            depth -= 1
            if depth == 0:
                return fuente[inicio : i + 1]
        i += 1
    raise AssertionError(f"llamada sin cerrar: {cabecera}")


def _constructed_calls(fuente: str) -> set:
    """Clases que el código CONSTRUYE, no las que menciona en prosa."""
    import ast

    return {
        node.func.id
        for node in ast.walk(ast.parse(fuente))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


# =========================================================================== #
# 1-2. El default y el valor explícito
# =========================================================================== #


def test_01_variable_ausente_usa_cognitive():
    """El default es el centro de decisión principal."""
    usar, motivo = resolve_cognitive_runtime({})
    assert usar is True
    assert DEFAULT_IS_COGNITIVE is True
    assert ALEXIS_COGNITIVE_ENV in motivo


@pytest.mark.parametrize("valor", sorted(COGNITIVE_VALUES))
def test_02_las_formas_de_pedir_cognitive_funcionan(valor):
    usar, _ = resolve_cognitive_runtime({ALEXIS_COGNITIVE_ENV: valor})
    assert usar is True, valor


# =========================================================================== #
# 3. El legacy está PROHIBIDO, no disponible
# =========================================================================== #


@pytest.mark.parametrize("valor", sorted(LEGACY_VALUES))
def test_03_pedir_legacy_aborta_el_arranque(valor):
    """`ALEXIS_COGNITIVE=0` no arranca un runtime alterno: no existe.

    Es la invariante que cierra CORE-03. Si esto volviera a devolver `(False, ...)`,
    el arranque volvería a admitir un recorrido sin `GoalVerifier`, y con él volvería
    la posibilidad de que una misión se quedara sin poder cerrar nunca.
    """
    with pytest.raises(LegacyRuntimeProhibited) as exc:
        resolve_cognitive_runtime({ALEXIS_COGNITIVE_ENV: valor})
    # El motivo explica la consecuencia, no sólo que el valor ya no existe.
    assert "GoalVerifier" in str(exc.value)
    assert "COMPLETED" in str(exc.value)


def test_03b_legacy_esta_prohibido_para_todo_valor_de_esa_familia():
    assert LEGACY_VALUES, "la familia de valores legacy tiene que seguir existiendo: se rechazan"
    for valor in LEGACY_VALUES:
        with pytest.raises(LegacyRuntimeProhibited):
            resolve_cognitive_runtime({ALEXIS_COGNITIVE_ENV: valor.upper()})


# =========================================================================== #
# 4-6. El runtime oficial no se degrada
# =========================================================================== #


def test_04_el_runtime_oficial_siempre_construye_el_cognitive():
    """No hay rama condicional: el `CognitiveRuntime` se construye siempre."""
    assert "RUNTIME.cognitive = CognitiveRuntime(" in OFFICIAL_SOURCE
    assert "if USAR_COGNITIVE:" not in OFFICIAL_SOURCE, (
        "CORE-03 quitó la rama: el CognitiveRuntime no es opcional"
    )


def test_05_el_cognitive_comparte_policy_gate_y_verifier_del_runtime():
    """Cambiar de runtime no puede cambiar quién autoriza."""
    bloque = _bloque_de_llamada(OFFICIAL_SOURCE, "RUNTIME.cognitive = CognitiveRuntime(")
    assert "policy=RUNTIME.policy" in bloque
    assert "gate=RUNTIME.gate" in bloque
    assert "verifier=RUNTIME.verifier" in bloque


def test_06_el_default_no_modifica_verification():
    """El cognitive no degrada el verificador a uno simulado.

    Se comprueba el CÓDIGO, no el texto: el módulo oficial nombra `BasicVerifier` y
    `LocalExecutor` en su docstring, precisamente para explicar qué se retiró. Un
    `in` sobre el fuente daría un falso positivo documentando lo que se eliminó.
    """
    import ast

    arbol = ast.parse(OFFICIAL_SOURCE)
    construidos = {
        node.func.id
        for node in ast.walk(arbol)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "BasicVerifier" not in construidos
    assert "LocalExecutor" not in construidos
    assert "FilesystemVerifier" in construidos


def test_06b_el_lanzador_no_construye_ningun_runtime():
    """`server.py` es el lanzador: si construye piezas, vuelve a haber dos sitios."""
    import ast

    arbol = ast.parse(LAUNCHER_SOURCE)
    construidos = {
        node.func.id
        for node in ast.walk(arbol)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    for prohibido in (
        "AlexisRuntime",
        "CognitiveRuntime",
        "EventBus",
        "GoalVerifier",
        "AutonomyGate",
        "PolicyEngine",
        "SandboxExecutor",
    ):
        assert prohibido not in construidos, f"el lanzador no debe construir {prohibido}"


# =========================================================================== #
# 7-8. Un único centro de decisión y una única construcción
# =========================================================================== #


def test_07_la_decision_se_toma_una_sola_vez():
    """Una sola evaluación del flag: cambiarla con misiones en vuelo rompería el
    criterio de que la autoridad no cambia a mitad de ejecución."""
    import ast

    arbol = ast.parse(OFFICIAL_SOURCE)
    llamadas = [
        node for node in ast.walk(arbol)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "resolve_cognitive_runtime"
    ]
    assert len(llamadas) == 1, [
        f"{len(llamadas)} llamadas al resolver: cambiarla con misiones en vuelo podría "
        "cambiar el runtime a mitad de ejecución"
    ]


def test_07c_el_legacy_no_existe_como_codigo_ejecutable():
    """No es un camino de primera clase commented-out: no hay rama que lo recorra."""
    assert "runtime legacy" not in OFFICIAL_SOURCE
    assert "else:" not in OFFICIAL_SOURCE.split("RUNTIME.cognitive = CognitiveRuntime(")[1][:200]


def test_08_no_hay_camino_donde_corran_ambos():
    """`_run_cognitive()` y el `for step in plan` siguen siendo ramas excluyentes: el
    legacy sólo es alcanzable si no hay `cognitive`, y ahora el runtime oficial siempre
    lo tiene."""
    runtime_src = (PROJECT_ROOT / "alexis" / "core" / "runtime.py").read_text(encoding="utf-8")
    assert "if self.cognitive is not None:" in runtime_src
    assert "return await self._run_cognitive(mission, plan, start)" in runtime_src
    cuerpo = runtime_src[runtime_src.index("if self.cognitive is not None:"):]
    return_ = cuerpo.index("return await self._run_cognitive")
    for_ = cuerpo.index("for index, step in enumerate(plan.steps)")
    assert return_ < for_, "el legacy sólo es alcanzable cuando no hay cognitive"


def test_08b_hay_un_unico_punto_de_construccion_del_runtime():
    """`build_official_runtime()` es el único sitio que instancia el runtime."""
    import ast

    arbol = ast.parse(OFFICIAL_SOURCE)
    construidos = [
        node.func.id
        for node in ast.walk(arbol)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "def build_official_runtime(" in OFFICIAL_SOURCE
    assert "AlexisRuntime" in construidos
    assert construidos.count("AlexisRuntime") == 1, (
        "más de un AlexisRuntime construido: volvería a haber dos runtimes"
    )
    # Y el lanzador lo pide por `build_runtime()`, que además lo instala como la
    # instancia oficial del proceso.
    assert "build_runtime" in LAUNCHER_SOURCE
    assert "AlexisRuntime" not in _constructed_calls(LAUNCHER_SOURCE)


# =========================================================================== #
# 9. Determinismo y ausencia de efectos
# =========================================================================== #


def test_09_un_valor_no_reconocido_no_concede_autoridad(caplog):
    """Un typo en la configuración no puede apartar ALEXIS del default en silencio."""
    import logging

    with caplog.at_level(logging.WARNING, logger="alexis.demo.runtime_flags"):
        usar, motivo = resolve_cognitive_runtime({ALEXIS_COGNITIVE_ENV: "banana"})
    assert usar is DEFAULT_IS_COGNITIVE
    assert "no reconocido" in motivo
    assert any("no es un valor reconocido" in r.message for r in caplog.records), (
        "un valor no reconocido debe avisar, no aplicarse en silencio"
    )


def test_09b_la_funcion_es_determinista():
    env = {ALEXIS_COGNITIVE_ENV: "1"}
    assert resolve_cognitive_runtime(env) == resolve_cognitive_runtime(env)


def test_09c_la_funcion_no_toca_el_entorno_real():
    """`env` inyectado no puede filtrarse al `os.environ`."""
    import os

    os.environ.pop(ALEXIS_COGNITIVE_ENV, None)
    usar, _ = resolve_cognitive_runtime({ALEXIS_COGNITIVE_ENV: "1"})
    assert usar is True
    assert ALEXIS_COGNITIVE_ENV not in os.environ, "el resolver no escribe en el entorno"


def test_09d_el_call_site_usa_el_resolver():
    """Guarda de cableado: si alguien vuelve a una lectura en línea, el default se pierde."""
    assert 'environ.get("ALEXIS_COGNITIVE"' not in OFFICIAL_SOURCE
    assert "USAR_COGNITIVE, _COGNITIVE_MOTIVO = resolve_cognitive_runtime()" in OFFICIAL_SOURCE
