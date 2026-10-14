"""R-01/R-02 — el parser de cron de §27 estaba roto en los dos casos más comunes.

`alexis/autonomy/schedule_rules.py` implementa el modo `cron` de §27 (tareas recurrentes).
Es lógica pura, sin efectos: se puede y debe demostrar aquí. Lo que se encontró son dos
defectos que ninguna prueba cubría, porque **`schedule_rules.py` no tenía ni una sola prueba
funcional** — `test_migrations.py` sólo comprueba que la tabla existe, que no es lo mismo que
que el código que la llena haga lo que dice.

**R-01 — los rangos Lanzaban `TypeError`.**

    (int(x) for x in base.split("-")) + (1,)

Une un *generator* con una tupla, y eso no se puede hacer: `TypeError: unsupported operand
type(s) for +: 'generator' and 'tuple'`. Todo cron con rango se caía, y un rango es la
sintaxis más común que existe después del `*`: `0 8 * * 1-5` es "laborables a las 8".

**R-02 — el día-del-mes no se respetaba cuando el día-de-semana era `*`.**

La condición era

    (candidate.day in days or candidate.weekday() in dows)

Con DOW = `*`, `dows` contiene los siete días, así que la segunda mitad del `or` es siempre
cierta y **la restricción de día se ignora por completo**. `0 0 1 * *` —"el día 1 de cada
mes"— devolvía el día 15. Consecuencia: una tarea de mantenimiento mensual se ejecutaría
**todos los días**.

Cron estándar (Vixie/ISC) no funciona así: si **ambos** campos están restringidos se usa OR;
si uno es `*`, el otro manda solo (AND). Ahí está la corrección.
"""

from __future__ import annotations

import datetime as dt

import pytest

from alexis.autonomy.schedule_rules import ScheduleRule, cron_next, initial_next_run

BASE = dt.datetime(2026, 10, 14, 7, 30, tzinfo=dt.timezone.utc)
#: 2026-10-14 es miércoles.
#:
#: CONVENCIÓN DE DÍA-DE-SEMANA: este módulo compara el campo de día-de-semana con
#: `datetime.weekday()` de Python, donde 0=LUNES … 6=DOMINGO. El cron estándar usa
#: 0=DOMINGO … 6=SÁBADO. Son convenciones distintas y esta es la que hay.
#:
#: No se cambia aquí porque `VISION.md` §27 no especifica cuál es la correcta y el módulo no
#: tiene ningún consumidor: elegirla es una decisión de contrato, no un arreglo. Queda
#: registrada como hallazgo abierto.


# --------------------------------------------------------------------------- #
# R-01 — los rangos no pueden lanzar
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "expr,esperado",
    [
        ("0 8 * * 1-5", dt.datetime(2026, 10, 14, 8, 0, tzinfo=dt.timezone.utc)),  # mié=2, dentro de 1-5
        ("30 9 * * 5", dt.datetime(2026, 10, 17, 9, 30, tzinfo=dt.timezone.utc)),  # ver convención
        ("0 0-2 * * *", dt.datetime(2026, 10, 15, 0, 0, tzinfo=dt.timezone.utc)),  # primer tramo
        ("0 12 * 10-12 *", dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.timezone.utc)),
    ],
)
def test_r01_los_rangos_no_lanzan(expr, esperado):
    """Un rango es sintaxis ordinaria de cron; si lanza, la tarea nunca se programa.

    El último caso importa por la semántica: `10-12` en el campo mes significa octubre a
    diciembre, y desde el 14 de octubre la siguiente ocurrencia es el día 15, no el 1 de
    noviembre — porque el día-de-mes es `*`.
    """
    assert cron_next(BASE, expr) == esperado


def test_r01_un_rango_no_se_confunde_con_una_lista():
    """`1-5` es un rango; `1,5` son dos valores. No pueden dar el mismo resultado."""
    lista = cron_next(BASE, "0 8 * * 1,3,5")
    rango = cron_next(BASE, "0 8 * * 1-3,5")
    assert lista != rango, "la lista y el rango se están tratando igual"


# --------------------------------------------------------------------------- #
# R-02 — día-del-mes y día-de-semana con semántica de cron estándar
# --------------------------------------------------------------------------- #


def test_r02_el_dia_del_mes_manda_cuando_el_dow_es_estrella():
    """`0 0 1 * *` es el día 1 de cada mes. Antes devolvía el día 15: se disparaba a diario.

    Con DOW = `*` los siete días están en el conjunto, así que un `or` hace que la
    restricción de día no importe nada. Aquí la fecha siguiente correcta es el 1 de
    noviembre.
    """
    resultado = cron_next(BASE, "0 0 1 * *")
    assert resultado == dt.datetime(2026, 11, 1, 0, 0, tzinfo=dt.timezone.utc), (
        f"\"0 0 1 * *\" devolvió {resultado}: el día-del-mes se ignoró porque el "
        f"día-de-semana es `*` y un `or` siempre es cierto."
    )


def test_r02_un_dia_de_semana_aislado_sigue_funcionando():
    """Regresión del otro lado: si sólo se restringe DOW, ese filtro debe mandar."""
    # lunes=0. Desde el miércoles 14, el próximo lunes es el 19.
    resultado = cron_next(BASE, "0 9 * * 0")
    assert resultado == dt.datetime(2026, 10, 19, 9, 0, tzinfo=dt.timezone.utc)


def test_r02_ambos_restringidos_usan_or():
    """Con los dos restringidos, cron estándar usa OR: el 1 del mes O cualquier lunes."""
    # Desde el 30 de septiembre: el «día 1» más cercano es el 1 de octubre y el lunes más
    # cercano el 5. Con los dos campos restringidos manda el OR de cron estándar, así que
    # gana el que aparezca ANTES en el calendario: el 1 de octubre. Si fuese AND, sería el
    # primer día 1 que además sea lunes (el 5 de octubre) — que es lo que distingue un OR
    # bien implementado de uno que sólo dice «cualquier día».
    d = dt.datetime(2026, 9, 30, 7, 30, tzinfo=dt.timezone.utc)
    resultado = cron_next(d, "0 0 1 * 0")
    assert resultado == dt.datetime(2026, 10, 1, 0, 0, tzinfo=dt.timezone.utc), (
        f"con día y día-de-semana restringidos debe aplicarse el OR de cron estándar; "
        f"devolvió {resultado}"
    )


# --------------------------------------------------------------------------- #
# Lo que ya funcionaba y no debe romperse
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "expr,esperado",
    [
        ("* * * * *", dt.datetime(2026, 10, 14, 7, 31, tzinfo=dt.timezone.utc)),
        ("0 8 * * *", dt.datetime(2026, 10, 14, 8, 0, tzinfo=dt.timezone.utc)),
        ("*/15 * * * *", dt.datetime(2026, 10, 14, 7, 45, tzinfo=dt.timezone.utc)),
        ("30 9 * * 5", dt.datetime(2026, 10, 17, 9, 30, tzinfo=dt.timezone.utc)),
    ],
)
def test_lo_que_ya_funcionaba_se_conserva(expr, esperado):
    assert cron_next(BASE, expr) == esperado


@pytest.mark.parametrize("expr", ["99 * * * *", "", "a b c d e", "0 8 * *"])
def test_las_expresiones_invalidas_siguen_rechazadas(expr):
    """Fail-closed: una expresión que no se entiende lanza, no se adivina."""
    with pytest.raises(ValueError):
        cron_next(BASE, expr)


# --------------------------------------------------------------------------- #
# initial_next_run — el otro punto de entrada del mismo módulo
# --------------------------------------------------------------------------- #


def test_initial_next_run_por_tipo():
    def regla(tipo, valor):
        return ScheduleRule(id="r", name="r", schedule_type=tipo, schedule_value=valor,
                            envelope_template={}, objective_template="o")

    referencia = dt.datetime(2026, 10, 14, 7, 30, tzinfo=dt.timezone.utc)

    unico = initial_next_run(regla("one_time", {"at": "2026-10-20T08:00:00+00:00"}), referencia)
    assert unico.startswith("2026-10-20T08:00:00"), f"one_time mal resuelto: {unico!r}"

    assert initial_next_run(regla("interval", {"seconds": 3600}), referencia) == "2026-10-14T07:30:00+00:00"

    assert initial_next_run(regla("cron", {"cron": "0 8 * * *"}), referencia) == "2026-10-14T07:30:00+00:00"


def test_initial_next_run_rechaza_lo_incompleto():
    """Sin `seconds` o sin `cron` no hay regla que programar: se rechaza, no se inventa."""
    def regla(tipo, valor):
        return ScheduleRule(id="r", name="r", schedule_type=tipo, schedule_value=valor,
                            envelope_template={}, objective_template="o")

    with pytest.raises(ValueError, match="seconds"):
        initial_next_run(regla("interval", {}))
    with pytest.raises(ValueError, match="cron"):
        initial_next_run(regla("cron", {}))
    with pytest.raises(ValueError, match="desconocido"):
        initial_next_run(regla("cada_rato", {}))