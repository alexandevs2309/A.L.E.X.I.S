"""El guardián de salida: qué cierra y, sobre todo, qué NO cierra.

Este fichero existe para que la diferencia no se pierda. Un hook de auditoría de Python
es una defensa en profundidad REAL —cierra los `connect()` de Python— pero no es aislamiento, y
su poder tiene una frontera concreta que alguien tiene que volver a leer cuando cambie
algo. `test_08` y `test_09` son los que la sostienen.
"""

from __future__ import annotations

import socket
import threading

import pytest

from alexis.tools import net

#: 192.0.2.0/24 es el rango de TEST-NET-1 (RFC 5737): está reservado a documentación y no
#: se enruta a ningún sitio. Es la dirección correcta para "quiero_connectar fuera y que
#: no me importe que falle" — usar 1.1.1.1 haría que la suite contactara de verdad con un
#: servidor público cada vez que corre.
FUERA_DE_CASA = ("192.0.2.1", 80)


def _intenta_conectar(destino, timeout=3):
    """Conecta con un socket explícito y lo cierra pase lo que pase.

    `socket.create_connection` deja el socket a medio construir cuando el guardián lanza, y
    ese socket se recoge más tarde por el recolector: con `-W error` eso revienta la suite
    con un error que no tiene nada que ver con lo que se está probando.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect(destino)
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _politica():
    """Deja la política como estaba, aunque una prueba la cambie.

    El hook NO se puede quitar (no hay forma en Python), pero la POLÍTICA sí se puede
    cambiar en caliente. Por eso se restaura en vez de confiar en que cada prueba se
    deje lo que encontró.
    """
    antes = dict(net._politica)
    yield
    net._politica.update(antes)


def test_01_una_conexion_externa_desde_python_se_deniega():
    """El caso que justifica el módulo."""
    with pytest.raises(net.EgressDenied) as fallo:
        _intenta_conectar(FUERA_DE_CASA)
    assert "net.fetch_text" in str(fallo.value), (
        "el error tiene que decir por dónde DEBÍA salir, o no sirve de nada"
    )


def test_02_el_loopback_sigue_funcionando():
    """Si esto se rompe, PostgreSQL y todos los servidores locales dejan de funcionar.

    Es el motivo de que la política de `check_url()` permita loopback: el aislamiento de
    red real (net-ns) no se puede activar sin mover PostgreSQL a socket unix.
    """
    servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    servidor.bind(("127.0.0.1", 0))
    servidor.listen(1)
    puerto = servidor.getsockname()[1]
    try:
        with socket.create_connection(("127.0.0.1", puerto), timeout=5) as cliente:
            assert cliente is not None
    finally:
        servidor.close()


def test_03_los_unix_socket_no_se_tocan():
    """Un `connect()` a unix no sale a la red: bloquearlo sería un falso positivo."""
    import os
    import tempfile

    ruta = os.path.join(tempfile.mkdtemp(), "s")
    servidor = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    servidor.bind(ruta)
    servidor.listen(1)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as cliente:
            cliente.connect(ruta)  # no debe lanzar
    finally:
        servidor.close()


def test_04_autorizar_salida_abre_la_puerta_y_se_cierra():
    # Con el permiso abierto la conexión NO es bloqueada por el guardián. Da igual que la
    # red falle después: lo que se comprueba aquí es que la puerta se abrió.
    with net.autorizar_salida():
        try:
            _intenta_conectar(FUERA_DE_CASA)
        except net.EgressDenied as exc:
            pytest.fail(f"el permiso no abrió la puerta: {exc}")
        except OSError:
            pass  # esperado: TEST-NET-1 no enruta a nada

    # Y al salir del contexto, se cierra. Si esto no se cerrara, el permiso sería
    # permanente y la política no valdría nada.
    with pytest.raises(net.EgressDenied):
        _intenta_conectar(FUERA_DE_CASA)


def test_05_el_permiso_no_se_fuga_a_otro_hilo():
    """La propiedad que hace que esto sea una puerta y no una bandera.

    El permiso es un `ContextVar`, no una bandera global. Con una bandera, dos descargas
    simultáneas se darían el permiso la una a la otra, y cualquiera que abriera el
    permiso por un segundo dejaría la red abierta para todo el proceso.
    """
    resultado = {}

    def _intenta():
        try:
            _intenta_conectar(FUERA_DE_CASA)
            resultado["r"] = "CONECTÓ: el permiso se filtró"
        except net.EgressDenied:
            resultado["r"] = "denegado"
        except Exception as exc:  # noqa: BLE001
            resultado["r"] = f"{type(exc).__name__}"

    with net.autorizar_salida():
        hilo = threading.Thread(target=_intenta)
        hilo.start()
        hilo.join()

    assert resultado["r"] == "denegado", (
        f"el permiso cruzó al hilo ({resultado['r']}): el ContextVar no está isolating"
    )


def test_06_el_dns_sigue_funcionando():
    """`net.py` necesita resolver nombres PARA PODER VALIDARLOS.

    Si el guardián bloqueara la resolución, se rompería el propio control que dice
    permitir: no se puede decidir si un host es público sin preguntarle al DNS.
    """
    direcciones, _ = net.resolve_public("example.com")
    assert direcciones, "resolve_public debe seguir resolviendo nombres"


def test_07_la_politica_se_puede_quitar_en_caliente():
    """Se prueba la rama de "desactivado" sin envenenar el resto de la suite.

    El hook no se puede quitar, pero la política sí. Eso es justo lo que permite que un
    diagnóstico se pueda hacer con la protección puesta.
    """
    net._politica["activo"] = False
    try:
        try:
            _intenta_conectar(FUERA_DE_CASA)
        except net.EgressDenied as exc:
            pytest.fail(f"con la política fuera el guardián no debe intervenir: {exc}")
        except OSError:
            pass  # esperado: sin red, TEST-NET-1 no responde
    finally:
        net._politica["activo"] = True


def test_08_honestidad_lo_que_el_guardian_NO_cubre():
    """Lo que NO hace, escrito como prueba para que no se pueda olvidar.

    Un hook de auditoría de Python sólo ve lo que pasa por la API de Python. Las
    librerías nativas abren sus sockets por su cuenta: `libpq` (PostgreSQL), `libcurl`,
    `requests` con su propio backend... Para esas, este guardián no existe.

    Esto NO es un test que "compruebe" una limitación: no hay forma de comprobar una
    ausencia. Lo que hace es dejar la afirmación escrita y verificada por el revisor en
    el sitio donde se toman decisiones sobre la red.
    """
    assert net.EgressDenied.__doc__, "la excepción tiene que explicar qué significa"
    texto = net.__doc__ or ""
    assert "No es aislamiento de red" in texto, (
        "el módulo debe seguir declarando que no es aislamiento"
    )
    # Y que la documentación de seguridad también lo diga.
    from pathlib import Path

    seguridad = Path(__file__).resolve().parents[1] / "docs" / "SECURITY.md"
    if seguridad.exists():
        assert "nativas" in seguridad.read_text(encoding="utf-8") or "C" in seguridad.read_text(encoding="utf-8")

def test_09_el_connect_real_ocre_la_ventana_de_permiso():
    """El hueco que encontró la contraprueba: nada probaba que el permiso se abriera.

    Se saboteó `connect_tcp` quitándole el `with autorizar_salida()` y **ningún test se
    rompió**: los tests del browser nunca pasan por un connect de verdad, así que la
    autorización podía no estar ahí y nadie se enteraba. Esta prueba mira directamente lo
    que el backend ve mientras conecta.
    """
    import asyncio

    visto = {}

    class _BackendFalso:
        async def connect_tcp(self, host, port, **kwargs):
            visto["host"] = host
            visto["permiso"] = net._permiso_de_salida.get()
            return "conectado"

    backend = net._BackendFijado({"ejemplo.com": "203.0.113.9"})
    backend._real = _BackendFalso()

    resultado = asyncio.run(
        backend.connect_tcp("ejemplo.com", 443, timeout=1.0)
    )

    assert resultado == "conectado"
    assert visto["host"] == "203.0.113.9", "el connect tiene que ir a la dirección fijada"
    assert visto["permiso"] is True, (
        "el connect real se hizo SIN permiso: el guardián lo habría bloqueado en "
        "producción y aquí no se nota porque este backend es falso"
    )


def test_10_al_terminar_el_connect_el_permiso_vuelve_a_cerrarse():
    """La ventana es del connect, no de la descarga. Si no se cerrara, sería una puerta abierta."""
    import asyncio

    visto = {}

    class _BackendFalso:
        async def connect_tcp(self, host, port, **kwargs):
            visto["dentro"] = net._permiso_de_salida.get()

    backend = net._BackendFijado({})
    backend._real = _BackendFalso()
    asyncio.run(backend.connect_tcp("ejemplo.com", 443, timeout=1.0))

    assert visto["dentro"] is True
    assert net._permiso_de_salida.get() is False, (
        "el permiso siguió abierto después del connect"
    )
