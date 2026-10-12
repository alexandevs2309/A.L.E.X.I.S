"""R-04: la suite no puede alcanzar la base de desarrollo aunque nadie le diga nada.

El defecto: `tests/conftest.py` sólo montaba su red de seguridad (redirigir
`ALEXIS_DATABASE_URL` a la base de test ANTES de que se importe `alexis.storage.db`) si
había una variable de entorno configurada. Sin ella, la red no se montaba y
`alexis.storage.db.DEFAULT_DSN` caía en el literal hardcodeado
`postgresql://alexis:change-me@localhost:5433/alexis` — la base de DESARROLLO.

Los tests que llaman a `init_storage()` —los HTTP de `test_core04_http_e2e`,
`test_control_auth_and_state` y `test_user_model`— conectaban a ella de verdad, y su
`pytest.skip` sólo salta si `storage["mission"] is None`. Con el PostgreSQL de
desarrollo levantado, no saltaba. Traducido al funcionamiento real: **una corrida de
tests sin configurar la base escribía misiones, checkpoints y auditoría sobre los datos
reales**, y nada en la salida lo delataba.

Estas pruebas comprueban la cadena, no el mensaje.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

RAIZ = Path(__file__).resolve().parents[1]


def _sin_entorno(cmd: list[str]) -> subprocess.CompletedProcess:
    """Ejecuta `cmd` con las tres variables de DSN ausentes del entorno.

    Se lanza un subproceso en vez de limpiar `os.environ` aquí porque
    `alexis.storage.db.DEFAULT_DSN` ya está evaluated al importar: en este proceso ya no
    se puede cambiar. La única forma de observar la cadena desde cero es un proceso
    nuevo, y es justo lo que importa: así es como arranca pytest de verdad.
    """
    import os

    env = {k: v for k, v in os.environ.items()
           if k not in ("ALEXIS_DATABASE_URL", "ALEXIS_DEV_DATABASE_URL",
                        "ALEXIS_TEST_DATABASE_URL")}
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env,
                          cwd=str(RAIZ))


# --------------------------------------------------------------------------- #
# 1 — la cadena que provoca el defecto
# --------------------------------------------------------------------------- #

def test_01_el_literal_por_defecto_apunta_a_la_base_de_desarrollo():
    """El hecho que hace posible el defecto: existe un DSN por defecto que es real.

    No es un fallo por sí mismo —la app arranca sin configurar nada a propósito— pero
    significa que «no configurar» no es «no conectar a nada».
    """
    fuente = (RAIZ / "alexis" / "storage" / "db.py").read_text(encoding="utf-8")
    assert "localhost:5433/alexis" in fuente, (
        "el literal por defecto ha cambiado: hay que releer qué base alcanza ahora un "
        "Database() sin argumentos antes de dar por buena la protección"
    )

    dsn = "postgresql://alexis:<password>@localhost:5433/alexis"
    partes = urlsplit(dsn)
    assert partes.path.lstrip("/") == "alexis", (
        "el literal por defecto es la base de DESARROLLO, no una de test"
    )


def test_02_sin_dsn_configurada_el_proceso_no_apunta_a_ninguna_base_real():
    """La corrección: sin DSN declarada, el entorno del proceso se neutraliza.

    Se pregunta al proceso NUEVO, que es el único donde la cadena importa: aquí
    `DEFAULT_DSN` ya se calculó al importar.
    """
    # Se ejecuta el bloque `else` del conftest aisladamente, con la misma entrada que
    # tendría pytest: sin ninguna de las tres variables.
    bloque = (
        "import os, sys; sys.path.insert(0, '.'); sys.path.insert(0, 'tests');\n"
        "import conftest\n"
        "print(os.environ.get('ALEXIS_DATABASE_URL',''))\n"
    )
    resultado = _sin_entorno([sys.executable, "-c", bloque])

    dsn = resultado.stdout.strip().splitlines()[-1] if resultado.stdout.strip() else ""
    assert dsn, (
        "el conftest debería dejar una DSN neutralizada; no dejó ninguna. "
        f"stdout={resultado.stdout!r} stderr={resultado.stderr[-400:]!r}"
    )
    assert urlsplit(dsn).path.lstrip("/") != "alexis", (
        f"el proceso sigue apuntando a la base de desarrollo: {dsn}"
    )
    assert "tests-sin-base-configurada" in dsn, (
        f"se esperaba la DSN neutralizada explícita; se obtuvo {dsn}"
    )


# --------------------------------------------------------------------------- #
# 2 — el efecto observable
# --------------------------------------------------------------------------- #

def test_03_los_tests_http_se_saltan_y_no_levantan_la_app():
    """La prueba que demuestra el efecto en el mundo real, no en el código.

    Si algún día un fichero HTTP deja de saltarse con la base sin configurar, aparece
    aquí un fallo — y ese fichero sería justo uno que escribe.
    """
    resultado = _sin_entorno([
        sys.executable, "-m", "pytest",
        "tests/test_control_auth_and_state.py",
        "-p", "no:cacheprovider", "-q", "--tb=no", "-rs",
    ])
    salida = resultado.stdout + resultado.stderr

    assert "la API de control necesita PostgreSQL" in salida, (
        "sin base configurada los tests de la API de control deben saltarse con ese "
        f"motivo; en su lugar:\n{salida[-800:]}"
    )


def test_04_no_se_escriben_misiones_en_la_base_de_desarrollo(dev_dsn):
    """El cierre del riesgo: la cuenta de misiones de `alexis` no se mueve.

    Es la comprobación que importa para quien tiene datos ahí dentro. Sólo LEE: no
    escribe, no trunca y no altera nada.
    """
    import psycopg

    # La DSN viene del entorno (`dev_dsn` de conftest), no incrustada aquí: un fichero
    # nuevo no es el sitio para escribir una contraseña, aunque sea la de desarrollo.
    try:
        with psycopg.connect(dev_dsn, connect_timeout=5) as c:
            antes = c.execute("select count(*) from missions").fetchone()[0]
    except Exception as exc:  # noqa: BLE001 — sin servidor, esta prueba no puede correr
        pytest.skip(f"PostgreSQL no disponible: {type(exc).__name__}")

    _sin_entorno([
        sys.executable, "-m", "pytest",
        "tests/test_control_auth_and_state.py",
        "-p", "no:cacheprovider", "-q", "--tb=no",
    ])

    with psycopg.connect(dev_dsn, connect_timeout=5) as c:
        despues = c.execute("select count(*) from missions").fetchone()[0]

    assert antes == despues, (
        f"una corrida sin base configurada escribió en la base de desarrollo: "
        f"{antes} → {despues} misiones"
    )