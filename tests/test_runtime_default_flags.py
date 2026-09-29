"""FASE 3.2 — B1: el CognitiveRuntime pasa a ser el camino principal.

El default era lo que hacía invisible todo el trabajo de las fases anteriores. Con
`ALEXIS_COGNITIVE=0` por defecto, el demo ejecutaba el recorrido legacy del plan, y el
CognitiveRuntime —con Context, Self Model, memoria, World Model, selección, replanning y
verificación— sólo existía si alguien sabía que había una bandera.

Aquí se fija el comportamiento exacto, incluidos los tres estados que antes estaban
colapsados en una comparación (`os.environ.get(..., "0") == "1"` no distingue "ausente"
de "0": son la misma cosa).

La regla es asimétrica a propósito. Lo que exige petición EXPLÍCITA es el legacy, porque
es el camino antiguo y volver a él se pide a nombre. El default es el CognitiveRuntime, así
que un valor no reconocido no concede nada: cae al default y avisa.

`apps/demo/server.py` levanta la app entera al importarse, así que el resolver vive en su
propio módulo y aquí se importa de verdad. El call site de producción se verifica
estáticamente, siguiendo el precedente que ya usa `test_success_criteria_persistence.py`.
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
    resolve_cognitive_runtime,
)

SERVER = PROJECT_ROOT / "apps" / "demo" / "server.py"


# =========================================================================== #
# 1-3. Los tres estados, sin ambigüedad
# =========================================================================== #


def test_01_variable_ausente_usa_cognitive():
    """El default es el centro de decisión principal."""
    usar, motivo = resolve_cognitive_runtime({})
    assert usar is True
    assert DEFAULT_IS_COGNITIVE is True
    assert ALEXIS_COGNITIVE_ENV in motivo


def test_02_valor_uno_usa_cognitive():
    usar, motivo = resolve_cognitive_runtime({ALEXIS_COGNITIVE_ENV: "1"})
    assert usar is True
    assert "explícitamente" in motivo


def test_03_valor_cero_usa_legacy():
    """El legacy sigue disponible, pero hay que pedirlo."""
    usar, motivo = resolve_cognitive_runtime({ALEXIS_COGNITIVE_ENV: "0"})
    assert usar is False
    assert "legacy" in motivo.lower()


# =========================================================================== #
# 4-6. El default no toca la autoridad
# =========================================================================== #


def test_04_el_default_no_modifica_policy():
    """Cambiar de runtime no puede cambiar quién autoriza. Policy sigue siendo la misma."""
    source = SERVER.read_text(encoding="utf-8")
    bloque = source[source.index("if USAR_COGNITIVE:"):source.index("# --- Percepción")]
    assert "policy=RUNTIME.policy" in bloque
    assert "gate=RUNTIME.gate" in bloque
    assert "verifier=RUNTIME.verifier" in bloque


def test_05_el_default_no_modifica_gate():
    assert "RUNTIME.gate = " not in SERVER.read_text(encoding="utf-8"), (
        "el bloque de arranque no puede reasignar el gate"
    )


def test_06_el_default_no_modifica_verification():
    source = SERVER.read_text(encoding="utf-8")
    bloque = source[source.index("if USAR_COGNITIVE:"):source.index("# --- Percepción")]
    assert "verifier=" in bloque
    assert "BasicVerifier" not in bloque, "el cognitive no degrada el verificador"


# =========================================================================== #
# 7. El legacy sigue siendo ejecutable
# =========================================================================== #


@pytest.mark.parametrize("valor", sorted(LEGACY_VALUES))
def test_07_todas_las_formas_de_pedir_legacy_funcionan(valor):
    usar, _ = resolve_cognitive_runtime({ALEXIS_COGNITIVE_ENV: valor})
    assert usar is False, valor


@pytest.mark.parametrize("valor", sorted(COGNITIVE_VALUES))
def test_07b_todas_las_formas_de_pedir_cognitive_funcionan(valor):
    usar, _ = resolve_cognitive_runtime({ALEXIS_COGNITIVE_ENV: valor})
    assert usar is True, valor


def test_07c_el_legacy_sigue_existiendo_en_el_codigo():
    """No se eliminó: sigue siendo un camino de primera clase, no un commenting-out."""
    source = SERVER.read_text(encoding="utf-8")
    assert "else:" in source[source.index("if USAR_COGNITIVE:"):]
    assert "runtime legacy" in source


# =========================================================================== #
# 8. Nunca los dos runtimes para la misma misión
# =========================================================================== #


def test_08_no_hay_camino_donde_corran_ambos():
    """La bifurcación es un `if/else` sobre una única decisión, y la decisión es una vez.

    La auditoría mostró que `_run_cognitive()` y el `for step in
    plan` eran ramas excluyentes. B1 no lo cambia, pero como el default cambió, conviene
    que el test lo fije: un solo centro de decisión.
    """
    runtime_src = (PROJECT_ROOT / "alexis" / "core" / "runtime.py").read_text(encoding="utf-8")
    assert "if self.cognitive is not None:" in runtime_src
    assert "return await self._run_cognitive(mission, plan, start)" in runtime_src
    # El `for` legacy está DESPUÉS de ese return: no hay ruta que lo alcance con cognitive.
    cuerpo = runtime_src[runtime_src.index("if self.cognitive is not None:"):]
    return_ = cuerpo.index("return await self._run_cognitive")
    for_ = cuerpo.index("for index, step in enumerate(plan.steps)")
    assert return_ < for_, "el legacy sólo es alcanzable cuando no hay cognitive"


def test_08b_la_decision_del_demo_se_toma_una_sola_vez():
    # Sólo llamadas reales: los comentarios no invocan nada.
    llamadas = [
        l for l in SERVER.read_text(encoding="utf-8").splitlines()
        if "resolve_cognitive_runtime()" in l and not l.lstrip().startswith("#")
    ]
    assert len(llamadas) == 1, (
        f"la decisión se evalúa una vez al arrancar; si se llamara más veces, un cambio "
        f"de entorno a mitad de ejecución podría cambiar el runtime con misiones en "
        f"vuelo. Llamadas: {llamadas}"
    )


# =========================================================================== #
# Determinismo y ausencia de efectos
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
    env = {ALEXIS_COGNITIVE_ENV: "0"}
    assert resolve_cognitive_runtime(env) == resolve_cognitive_runtime(env)


def test_09c_la_funcion_no_toca_el_entorno_real():
    """`env` inyectado no puede filtrarse al `os.environ`."""
    import os

    os.environ.pop(ALEXIS_COGNITIVE_ENV, None)
    usar, _ = resolve_cognitive_runtime({ALEXIS_COGNITIVE_ENV: "0"})
    assert usar is False
    assert ALEXIS_COGNITIVE_ENV not in os.environ, "el resolver no escribe en el entorno"


def test_09d_el_call_site_usa_el_resolver():
    """Guarda de cableado: si alguien vuelve a una lectura en línea, el default se pierde."""
    source = SERVER.read_text(encoding="utf-8")
    assert 'environ.get("ALEXIS_COGNITIVE"' not in source, (
        "la lectura directa de ALEXIS_COGNITIVE desapareció el default otra vez"
    )
    assert "USAR_COGNITIVE, _COGNITIVE_MOTIVO = resolve_cognitive_runtime()" in source
