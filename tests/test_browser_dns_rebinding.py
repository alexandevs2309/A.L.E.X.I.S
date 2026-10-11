"""Aceptación del riesgo de DNS rebinding: una URL que PASA el filtro no puede
terminar conectándose a una dirección prohibida.

Este fichero NO arregla nada: sólo mide. Los casos van escritos contra el código
actual y deben fallar. Un test de seguridad que nace verde no demuestra nada —no está
probando el agujero, lo está suponiendo— y en esta sesión ya han salido dos falsos
positivos exactamente así.

**Por qué estos tests no pueden pasar en vacío.** La primera versión de este fichero
DABA VERDE y era mentira, por dos motivos que costaron encontrar:

1. `socket.getaddrinfo` recibe el host como **bytes** (`b'ejemplo'`), no `str`. Un
   resolvedor de test que compara con `isinstance(host, str)` no casa nunca y finge
   controlar el DNS.
2. `check_url` sólo admite los puertos 80/443, así que una URL de test con puerto
   efímero se bloquea ANTES de intentar conectar. El registro de conexiones queda vacío
   y "no se conectó" se cumple porque no se intentó nada.

Por eso cada test que observa ausencia de conexión comprueba ADEMÁS que el resolvedor
fue consultado y que el servidor local estaba escuchando. Si no hubo resolución, el
test falla con un mensaje que lo dice, en vez de pasar en silencio.

Se sustituye el RESOLVEDOR y se registra `socket.connect`: el DNS es el ENTORNO, y el
entorno se sustituye igual que el reloj en un test de tiempo. `check_url` y
`_host_is_public` siguen siendo la implementación real, sin tocar. Mismo patrón que
`test_browser_redirects.py`.

Ningún test depende de DNS público, de internet ni de un destino real de metadatos: las
direcciones son sintéticas y el único servidor real escucha en un puerto efímero.
"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from alexis.tools import browser as browser_mod
from alexis.tools.browser import (
    FetchBlocked,
    _host_is_public,
    check_url,
    http_fetch,
)

# IP pública de ejemplo. Nunca se conecta a ella: sólo se usa como valor que
# check_url() debe aceptar y cuya validación debe condicionar la conexión.
IP_PUBLICA = "93.184.216.34"
IP_LOOPBACK = "127.0.0.1"
IP_METADATOS = "169.254.169.254"
IP_PRIVADA_10 = "10.0.0.5"
IP_PRIVADA_192 = "192.168.1.17"


# --------------------------------------------------------------------- #
# Infraestructura
# --------------------------------------------------------------------- #


class _ServidorLocal:
    """Servidor HTTP real en 127.0.0.1:puerto_efímero. Registra si recibió algo."""

    def __init__(self):
        self.recibido = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 — firma de BaseHTTPRequestHandler
                self.server.recibido.set()
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        self._srv = HTTPServer(("127.0.0.1", 0), Handler)
        self._srv.recibido = self.recibido
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    @property
    def puerto(self) -> int:
        return self._srv.server_address[1]

    def cierra(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()


@pytest.fixture
def servidor():
    s = _ServidorLocal()
    yield s
    s.cierra()


@pytest.fixture
def dns(monkeypatch):
    """Sustituye el RESOLVEDOR. Devuelve `dns(nombre, ...)` y `dns.llamadas`.

    Acepta respuestas SECUENCIALES para modelar el rebinding de verdad:
    `responde("x", IP_PUBLICA, IP_LOOPBACK)` devuelve la pública la primera vez y la
    loopback a partir de la segunda. Es lo que hace un atacante con un TTL bajo, y es lo
    que separa "validó y fijó" de "validó y volvió a preguntar".

    `llamadas` cuenta las resoluciones pedidas. Un test que no mira `llamadas` no puede
    distinguir "no se consultó el DNS" de "se consultó y dio una IP buena": por eso los
    tests de conexión lo comprueban siempre.
    """
    tabla: dict[str, list[list[str]]] = {}
    contador: dict[str, int] = {}

    def responde(nombre: str, *ips: str) -> None:
        """`responde("x", A)` respuesta estable; `responde("x", A, B)` encadena A→B.

        Cada argumento es una respuesta DISTINTA, no una lista de direcciones para el
        mismo instante: la primera se devuelve en la primera resolución y la última en
        todas las siguientes. Esa diferencia es la que modela el rebinding.
        """
        clave = nombre.rstrip(".").lower()
        tabla[clave] = [[ip] for ip in ips]
        contador[clave] = 0

    def tras(n: int, nombre: str, primero: str, despues: str) -> None:
        """`n` resoluciones limpias y, a partir de ahí, la respuesta mala.

        Hace falta porque el código consulta el DNS más de una vez: hoy `check_url`
        resuelve y `http_fetch` vuelve a resolver para fijar. Un atacante que sólo
        devuelve la IP mala en la SEGUNDA consulta lo Stopaea el propio fail-closed, sin
        necesidad del fijado —y el test pasaría dando por bueno algo que no está
        probando. La.IP mal tiene que aparecer en la consulta que abre el socket, que es
        la última.
        """
        clave = nombre.rstrip(".").lower()
        tabla[clave] = [[primero]] * n + [[despues]]
        contador[clave] = 0

    def conjunto(nombre: str, *ips: str) -> None:
        """Una sola resolución que devuelve VARIAS direcciones a la vez.

        Es un caso distinto al de `responde` y no puede compartir la misma sintaxis: aquí
        el host tiene varios registros A (RR) y todos son válidos a la vez. Confundir
        "varias respuestas sucesivas" con "varias direcciones en la misma" haría que un
        test pasara midiendo rebinding cuando lo que quería medir era el conjunto.
        """
        clave = nombre.rstrip(".").lower()
        tabla[clave] = [list(ips)]
        contador[clave] = 0

    responde.llamadas: list[str] = []

    real = socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):
        # anyio/httpx pasan el host como BYTES. Comparar con str no casa nunca.
        nombre = host.decode() if isinstance(host, (bytes, bytearray)) else str(host)
        responde.llamadas.append(nombre.rstrip(".").lower())
        clave = nombre.rstrip(".").lower()
        if clave in tabla:
            opciones = tabla[clave]
            i = min(contador[clave], len(opciones) - 1)
            contador[clave] += 1
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))
                for ip in opciones[i]
            ]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    responde.conjunto = conjunto
    responde.tras = tras
    return responde


@pytest.fixture
def conexiones(monkeypatch):
    """Registra las direcciones a las que se intenta conectar DE VERDAD."""
    vistas: list[tuple] = []
    real = socket.socket.connect

    def connect(self, address):
        try:
            vistas.append(tuple(address))
        except TypeError:
            vistas.append((address,))
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    return vistas


@pytest.fixture
def puerto_de_test(monkeypatch, servidor):
    """Permite el puerto efímero del servidor, sin tocar NADA más de la política.

    `check_url` sólo admite 80/443. Sin esto la petición se bloquea antes de conectar y
    el registro de conexiones queda vacío: el test pasaría sin comprobar nada. Se
    amplía SÓLO el conjunto de puertos; `_host_is_public`, el esquema y la allowlist
    siguen siendo los reales.
    """
    monkeypatch.setattr(
        browser_mod, "ALLOWED_PORTS", frozenset({None, 80, 443, servidor.puerto})
    )
    return servidor.puerto


# --------------------------------------------------------------------- #
# 1-3 — un nombre que resuelve a dirección prohibida NO pasa el filtro
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "nombre,ip,por_que",
    [
        ("loopback.example", IP_LOOPBACK, "loopback"),
        ("metadatos.example", IP_METADATOS, "metadatos de instancia"),
        ("privada10.example", IP_PRIVADA_10, "rfc1918 10/8"),
        ("privada192.example", IP_PRIVADA_192, "rfc1918 192.168/16"),
    ],
)
def test_un_nombre_que_resuelve_a_direccion_prohibida_se_bloquea(dns, nombre, ip, por_que):
    """CASOS 1, 2 y 3. El nombre es legal; lo que lo condemnaba era la dirección.

    Falla hoy porque `_host_is_public()` responde `True` a cualquier nombre que no sea
    una IP literal: no resuelve, así que no puede saber a dónde va a acabar.
    """
    dns(nombre, ip)

    permitido, _ = check_url(f"http://{nombre}/")

    assert not permitido, (
        f"{nombre} resuelve a {ip} ({por_que}) y check_url lo permite: se valida el "
        "NOMBRE, no la DIRECCIÓN a la que se conectará"
    )


def test_una_ip_literal_prohibida_sigue_bloqueada_sin_dns(dns):
    """Control: la IP literal se rechaza SIN resolver. Es lo único que hoy funciona.

    Si este test empezara a fallar, la regresión no estaría en el rebinding sino en el
    filtro básico, que es el que no hay que romper.
    """
    assert not check_url(f"http://{IP_LOOPBACK}/")[0]
    assert not check_url(f"http://{IP_METADATOS}/")[0]


def test_un_nombre_que_no_resuelve_se_bloquea(dns, monkeypatch):
    """CASO 6. Fallo de DNS = desconocido, y lo desconocido NO se permite.

    Permitir por omisión cuando no se puede comprobar sería justo el fallo que este
    riesgo describe: una transición a la nada cruzaría el filtro.
    """
    def revienta(host, port, *args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "nombre inexistente")

    monkeypatch.setattr(socket, "getaddrinfo", revienta)

    permitido, _ = check_url("http://no-existe.example/")
    assert not permitido, "un nombre que no resuelve debe bloquearse, no permitirse por omisión"


# --------------------------------------------------------------------- #
# 4 — lo que se valida es lo que se conecta
# --------------------------------------------------------------------- #


def test_un_nombre_que_resuelve_solo_a_ip_publica_se_permite(dns):
    """CASO 4a: una IP pública legítima NO se bloquea.

    El arreglo no puede cerrar la puerta a internet: `browser.research` existe para eso.
    """
    dns("publico.example", IP_PUBLICA)

    assert check_url("http://publico.example/")[0], (
        "una IP pública válida debe seguir permitiéndose tras el arreglo"
    )


async def test_un_nombre_que_resuelve_a_loopback_no_llega_a_conectarse(
    dns, conexiones, puerto_de_test, servidor
):
    """CASO 4b — el agujero, demostrado de punta a punta.

    El nombre parece inocuo, así que `check_url` lo deja pasar; luego httpx resuelve y
    conecta contra el servidor local de este test. Lo que se afirma NO es "el filtro
    falló" sino "hubo una conexión a una dirección prohibida", que es el daño.

    Las dos guardas anti-vacío son lo que hace este test honesto: si el DNS no se
    consultó o el servidor no estaba vivo, el fallo dice por qué en vez de pasar.
    """
    dns("portal.example", IP_LOOPBACK)

    # Sin envolver en `pytest.raises` a propósito: que la descarga SUCCEED es la prueba
    # del agujero. Si el servicio local responde, el nombre público cualquiera alcanzó un
    # servicio que no es público. Una versión anterior exigía una excepción y por eso
    # reportaba un fallo sin explicar nada; ahora el resultado se lee en las aserciones.
    try:
        await http_fetch(f"http://portal.example:{puerto_de_test}/", timeout=5)
    except (FetchBlocked, OSError):
        pass

    assert dns.llamadas, (
        "el resolvedor nunca se consultó: el test noMidió nada. "
        "Si check_url() no resuelve, este caso no se puede observar así."
    )

    loopback = [c for c in conexiones if c and c[0] == IP_LOOPBACK and c[1] == puerto_de_test]
    assert not loopback, (
        f"se conectó a {IP_LOOPBACK}:{puerto_de_test}. Un nombre público cualquiera "
        "alcanzó el servicio local: es exactamente el rebinding."
    )


async def test_el_rebinding_entre_validacion_y_conexion_no_llega_a_la_ip_mala(
    dns, conexiones, puerto_de_test, servidor
):
    """CASO 4c — ESTE test separa el Nivel A del Nivel B.

    El atacante responde con una IP pública la PRIMERA vez que se le pregunta (que es lo
    que valida el filtro) y con una loopback la segunda (que es lo que usa la conexión).

    - Nivel A (valida pero no fija): al conectar vuelve a preguntar → le llega loopback →
      conecta a loopback → **falla**.
    - Nivel B (valida y fija): la conexión va a la dirección validada → pasa.

    Una corrección sobre cómo se sabía esto: la primera versión exigía DOS resoluciones
    y era un error. Fijar la dirección significa que la conexión **no vuelve a resolver**,
    así que un arreglo correcto hace UNA sola resolución, no dos. Exigir dos habría
    Obligado a no fijar y a dejar la ventana abierta.

    El caso "sin arreglo" no lo cazaba este test: también pasa, porque sin resolver en la
    validación la única resolución es la de la conexión y le llega la IP pública. Quien lo
    caza son los casos 1-3, que exigen que un nombre que resuelve a loopback quede
    bloqueado. Este test tiene un trabajo más estrecho y lo dice: separar A de B.
    """
    # Las dos primeras resoluciones —validación y fijado— limpias. La terceira, la que
    # abre el socket, devuelve la interna. Ése es el punto exacto del ataque.
    dns.tras(2, "rebind.example", IP_PUBLICA, IP_LOOPBACK)

    # `timeout` corto a propósito: sin arreglo este caso termina intentando conectar a una
    # IP pública que no existe en la red de test, y esperar a que agote el plazo no
    # aporta señal —la señal está en `conexiones` y en `dns.llamadas`.
    try:
        await http_fetch(f"http://rebind.example:{puerto_de_test}/", timeout=2)
    except (FetchBlocked, OSError, Exception):
        pass

    assert dns.llamadas, (
        "el resolvedor nunca se consultó: este caso no midió nada. Sin resolución no se "
        "puede observar ni la validación ni el rebinding."
    )

    loopback = [c for c in conexiones if c and c[0] == IP_LOOPBACK and c[1] == puerto_de_test]
    assert not loopback, (
        f"se conectó a {IP_LOOPBACK}:{puerto_de_test} tras validar {IP_PUBLICA}: el DNS se "
        "consultó dos veces y la validación no se aplica a la dirección realmente usada."
    )


# --------------------------------------------------------------------- #
# 5 — una sola dirección mala entre varias condena el conjunto
# --------------------------------------------------------------------- #


def test_una_sola_ip_mala_entre_varias_bloquea_el_conjunto(dns):
    """CASO 5. Con varias A, la elección del destino la hace el cliente, no el filtro.

    ALEXIS no controla qué dirección Connect elegirá de la lista. Si una sola es interna,
    el atacante conserva esa opción para cuando quiera, así que quedarse con "la buena"
    es permitir la explotación con otro formato.
    """
    # Varios registros A en la MISMA resolución, no dos respuestas sucesivas: por eso
    # se usa `conjunto` y no `responde`.
    dns.conjunto("mixto.example", IP_PUBLICA, IP_LOOPBACK)

    permitido, _ = check_url("http://mixto.example/")

    assert not permitido, (
        "el conjunto resuelve a una IP interna y a una pública: hay que rechazar el "
        "conjunto, no quedarse con la buena"
    )


# --------------------------------------------------------------------- #
# 7 — el mismo criterio en cada salto de redirección
# --------------------------------------------------------------------- #


def test_un_salto_a_un_nombre_que_resuelve_a_loopback_tambien_se_bloquea(dns):
    """CASO 7. Un salto a `127.0.0.1` literal ya se bloquea; a un NOMBRE malicioso, no.

    La cobertura de "el criterio se aplica a cada salto" con servidores reales está en
    `test_browser_redirects.py`. Lo que falta, y lo que se mide aquí, es que ese criterio
    incluya la resolución: una redirección a `loopback.example` debería rechazarse por la
    misma razón que una redirección a la IP literal.
    """
    dns("loopback.example", IP_LOOPBACK)

    directo, _ = check_url("http://127.0.0.1/")
    salto, _ = check_url("http://loopback.example/")

    assert not directo
    assert not salto, (
        "una redirección a un NOMBRE que resuelve a loopback pasa el filtro: el salto se "
        "valida por nombre igual que el destino inicial"
    )


# --------------------------------------------------------------------- #
# Controles: la parte que ya está bien y no debe romperse
# --------------------------------------------------------------------- #


def test_el_filtro_de_ips_no_deja_pasar_ninguna_clase_reservada():
    """La regla de clasificación, sin DNS. Pasa hoy y debe seguir pasando.

    Un arreglo del rebinding que empeorara esto sería un hallazgo nuevo: cerrar una vía
    abriendo otra no es un arreglo.
    """
    for ip in (IP_LOOPBACK, IP_METADATOS, IP_PRIVADA_10, IP_PRIVADA_192,
               "10.255.255.254", "192.168.0.1", "172.16.0.1", "::1", "fd00::1"):
        assert not _host_is_public(ip), f"{ip} debería estar bloqueada"
    assert _host_is_public(IP_PUBLICA)