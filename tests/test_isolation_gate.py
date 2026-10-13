"""PRUEBAS DE ACEPTACIÓN DEL AISLAMIENTO — se ejecutan EN VERDE dentro del namespace.

Este fichero NO es decorativo y NO está en verde hoy. Está en rojo a propósito: es la
definición ejecutable del estado que §4 del plan debe producir. Si alguien lo lee en verde
sin haber aplicado el aislamiento, el fichero está mintiendo y eso hay que investigarlo.

Qué se afirma, y por qué cada línea existe:

1. **No hay ruta a ninguna parte.** Un net-ns con 0 rutas no es «filtrado»: no hay a dónde
   ir. Deny-by-default real, no una lista de bloqueos que se pueda saltar.

2. **DNS muerto por los DOS canales.** Éste es el punto que la mayoría de diseños se
   saltan. Bloquear `nameserver 127.0.0.53` NO basta: `systemd-resolved` expone además un
   socket **Varlink** Unix (`/run/systemd/resolve/io.systemd.Resolve`, modo `srw-rw-rw-`)
   que `nss-resolve` consulta. Los sockets Unix atraviesan el net-ns sin problema, así que
   un proceso aislado podía resolver nombres —y filtrar datos codificados en ellos— a
   través del resolver DEL HOST, que sí tiene salida. Medido en esta máquina: con el
   net-ns puesto y sin más, `example.com` se resolvía. Con `/run/systemd/resolve` montado
   como tmpfs, no. Las dos comprobaciones de aquí son las dos puertas.

3. **Internet inalcanzable por TCP y por UDP**, incluido el bypass de libpq. Este es el
   motivo por el que el diseño no depende del hook de Python: aunque una librería nativa
   ignore la auditoría, el kernel no le ofrece ningún destino.

4. **PostgreSQL por socket Unix.** El aislamiento sólo es aceptable si el sistema sigue
   pudiendo persistir. Aquí la excepción no cuesta nada: un socket Unix no es red, así que
   no abre ninguna vía de salida. Ésa es toda la diferencia entre «aislado» y «aislado
   pero inservible».
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import pytest

#: Dónde debe estar el socket cuando PostgreSQL escuche sólo en Unix (PLAN §4).
SOCKET_DIR = Path(os.environ.get("ALEXIS_PG_SOCKET_DIR", "/home/auron/ALEXIS-pg-socket"))

#: Destinos que un proceso aislado NO debe poder alcanzar. Se prueban por IP y no por
#: nombre a propósito: usar un nombre reintroduciría la dependencia de DNS, que es
#: justamente lo que se está comprobando.
DESTINOS_PROHIBIDOS = [
    ("1.1.1.1", 443, "internet TCP"),
    ("8.8.8.8", 53, "DNS directo TCP"),
    ("2606:4700:4700::1111", 443, "internet IPv6"),
]


def _conexion_alcanzable(host: str, puerto: int, timeout: float = 2.0) -> bool:
    familia = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(familia, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, puerto))
        return True
    except OSError:
        return False
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# 1 — deny-by-default de verdad
# --------------------------------------------------------------------------- #


def test_01_no_hay_ninguna_ruta_de_salida():
    """Cero rutas. Un firewall con reglas es una lista; cero rutas es un vacío."""
    salida = subprocess.run(
        ["ip", "route", "show"], capture_output=True, text=True
    ).stdout.strip()
    assert salida == "", (
        f"hay rutas de salida dentro del aislamiento: {salida!r}. Con rutas, el "
        f"aislamiento depende de que las reglas sean correctas."
    )


def test_02_solo_existe_loopback():
    """`lo` y nada más. Si aparece una interfaz, alguien añadió un puente."""
    interfaces = subprocess.run(
        ["ip", "-o", "link", "show"], capture_output=True, text=True
    ).stdout
    nombres = [line.split(": ")[1].strip() for line in interfaces.splitlines() if ": " in line]
    assert nombres == ["lo"], (
        f"interfaces dentro del aislamiento: {nombres}. Se esperaba únicamente `lo`."
    )


# --------------------------------------------------------------------------- #
# 2 — DNS: las dos puertas
# --------------------------------------------------------------------------- #


def test_03_el_socket_varlark_de_systemd_resolved_esta_cerrado():
    """La puerta que se suele olvidar.

    Bloquear `nameserver 127.0.0.53` deja vivo `/run/systemd/resolve/io.systemd.Resolve`.
    Ese socket es Unix, y los sockets Unix no respetan el net-ns: el proceso aislado
    seguiría resolviendo nombres con el resolver del HOST, que tiene salida. Medido aquí:
    sin este cierre, `example.com` se resolvía DENTRO del namespace.
    """
    varlink = Path("/run/systemd/resolve/io.systemd.Resolve")
    assert not varlink.exists(), (
        f"{varlink} es accesible dentro del aislamiento. Es un bypass de DNS completo: "
        f"se puede filtrar datos codificados en nombres de dominio hacia fuera."
    )


def test_04_la_resolucion_de_nombres_esta_muerta():
    """Comprobación de efecto, no de mecanismo: el nombre tiene que fallar.

    Se usa un nombre IMPOSIBLE para que un fallo no pueda atribuirse a que el dominio no
    exista — así que además se prueba uno real, que resolvería cualquier caché local.
    """
    for nombre in ("example.com", "www.wikipedia.org"):
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo(nombre, None)


def test_05_el_stub_de_loopback_tampoco_responde():
    """Defensa en profundidad: aunque existiera el socket, el stub no debe contestar."""
    assert not _conexion_alcanzable("127.0.0.53", 53, timeout=1.0), (
        "el stub de systemd-resolved escucha dentro del aislamiento"
    )


# --------------------------------------------------------------------------- #
# 3 — internet, incluido el bypass de librerías nativas
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("host,puerto,que", DESTINOS_PROHIBIDOS)
def test_06_internet_inalcanzable(host, puerto, que):
    """Deny-by-default sobre destinos prohibidos, por IPv4 y por IPv6."""
    assert not _conexion_alcanzable(host, puerto), (
        f"{que} ({host}:{puerto}) es alcanzable desde dentro del aislamiento"
    )


def test_07_libpq_no_alcanza_internet_aunque_se_ignore_el_hook():
    """LA PRUEBA QUE JUSTIFICA EL DISEÑO, y la que más fácil sale falsa.

    El hook de auditoría de Python no ve `libpq` porque es C: ese bypass está
    documentado y es real. Este diseño no lo arregla —lo hace irrelevante—, y aquí se
    demuestra: `psycopg` abre su propio socket, saltándose cualquier auditoría de Python,
    y aun así no tiene a dónde ir.

    **Por qué se comprueba el errno y no simplemente «que falle».** Una versión anterior
    de esta prueba afirmaba sólo que la conexión falla. Eso es un falso positivo: medido
    en esta máquina, FUERA del aislamiento libpq también «falla», pero con
    `ConnectionTimeout` — los paquetes salieron y los perdió el cortafuegos. La conexión
    no sepixels llegaría nunca a establecer, pero la fuga ya habría ocurrido.

    Aquí la diferencia es la que importa:
      - `ENETUNREACH` / `EHOSTUNREACH` / `EADDRNOTAVAIL` → el kernel no ofrece ruta: nada
        salió. Esto es lo que exige el aislamiento.
      - `ETIMEDOUT` / `ECONNREFUSED` → hubo camino y los paquetes salieron. **FALLA.**
    """
    psycopg = pytest.importorskip("psycopg")
    dsn = "host=1.1.1.1 port=5432 dbname=alexis user=alexis connect_timeout=3"
    try:
        with psycopg.connect(dsn):
            pass
    except psycopg.Error as exc:
        # psycopg 3 no propaga el errno: `OperationalError` conserva sólo el texto que le
        # pasó libpq. Ese texto SÍ distingue los dos casos, y es la única señal que hay:
        #   "Network is unreachable" / "No route to host" -> el kernel no ofreció ruta
        #   "timeout expired" / "Connection refused"       -> los paquetes SALIERON
        mensaje = str(exc).lower()
        if "timeout" in mensaje or "refused" in mensaje:
            pytest.fail(
                f"libpq falló por {mensaje.strip()[-60:]!r}, no por falta de ruta: los "
                f"paquetes salieron del proceso y se perdieron fuera. La exfiltración por "
                f"librerías nativas sigue siendo posible con este diseño."
            )
        return
    pytest.fail(
        "libpq alcanzó un destino externo desde dentro del aislamiento: el bypass de "
        "librerías nativas SÍ es explotable con este diseño"
    )


# --------------------------------------------------------------------------- #
# 4 — y aun así, el sistema tiene que poder persistir
# --------------------------------------------------------------------------- #


def test_08_postgres_por_socket_unix_es_accesible():
    """El aislamiento que no deja trabajar no es una solución.

    Aquí está el motivo de que el plan sea viable: la excepción por PostgreSQL no es una
    excepción de RED, es un socket Unix. No abre ninguna vía de salida, así que no hay
    que tradeoff entre «aislar» y «funcionar».
    """
    socket_path = SOCKET_DIR / ".s.PGSQL.5432"
    assert socket_path.exists(), (
        f"no hay socket de PostgreSQL en {SOCKET_DIR}. Sin él, el aislamiento no es "
        f"viable: habría que elegir entre seguridad y persistencia."
    )
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(3)
    try:
        s.connect(str(socket_path))
    finally:
        s.close()


def test_09_postgres_ya_no_escucha_en_tcp():
    """Cerrar el listener TCP es lo que hace que AF_UNIX sea la única puerta.

    Con `listen_addresses=''` (§4 del plan) nadie alcanza PostgreSQL por red, ni siquiera
    desde el host. Sin esto, el socket Unix sería una puerta más, no la única.
    """
    assert not _conexion_alcanzable("127.0.0.1", 5433, timeout=1.0), (
        "PostgreSQL sigue escuchando en TCP dentro del aislamiento"
    )


def test_10_la_persistencia_funciona_de_verdad():
    """Escribir y leer de verdad, por el camino que ALEXIS usará.

    No basta con que el socket exista: tiene que soportar el trabajo real. Se usa la base
    `alexis_test` porque esta prueba escribe, y `alexis` es producción.
    """
    psycopg = pytest.importorskip("psycopg")
    dsn = (
        f"host={SOCKET_DIR} dbname=alexis_test user=alexis "
        f"password={os.environ.get('PGPASSWORD', 'change-me')}"
    )
    with psycopg.connect(dsn, connect_timeout=5, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("create table if not exists _aislamiento (n int)")
            cur.execute("insert into _aislamiento values (1)")
            cur.execute("select count(*) from _aislamiento")
            assert cur.fetchone()[0] >= 1
            cur.execute("drop table _aislamiento")


def test_11_sqlite_sobre_disco_sigue_disponible():
    """El almacenamiento local no depende de la red y no debe romperse.

    Comprobación de que el aislamiento no ha cerrado más puertas de las necesarias: si
    esto fallara, el diseño sería demasiado agresivo.
    """
    with tempfile.TemporaryDirectory() as d:
        con = sqlite3.connect(f"{Path(d) / 'prueba.db'}")
        con.execute("create table t (x int)")
        con.execute("insert into t values (7)")
        assert con.execute("select x from t").fetchone()[0] == 7
        con.close()


# --------------------------------------------------------------------------- #
# 5 — y lo que el aislamiento obliga a que el guardián haga
# --------------------------------------------------------------------------- #


def test_el_guardian_falla_cerrado_sin_dns():
    """SIN DNS, el guardián de salida tiene que DENEGAR, nunca(Exception) ni PERMITIR.

    Esto no es un extra: es la cobertura que sustituye a `test_egress_guard.py::
    test_06_el_dns_sigue_funcionando`, que queda marcado `external` porque exige un despliegue
    con red. Marcarlo fuera sin añadir esto sería **quitar seguridad sin sustituirla**, que
    es como se degradan las suites: se vuelve verde y nadie mira lo que dejó de comprobarse.

    El caso que importa: `resolve_public()` valida un host **resolviéndolo**. Sin DNS no
    puede saber si un dominio apunta a una IP interna, y la política del propio `net.py` dice
    «si el DNS falla, se rechaza: lo desconocido no se permite». Aquí se comprueba que eso
    sigue siendo cierto dentro del aislamiento, que es donde sevae.
    """
    from alexis.tools import net

    direcciones, motivo = net.resolve_public("example.com")
    assert direcciones == [], (
        f"sin DNS, resolve_public no debe devolver direcciones: {direcciones!r}"
    )
    assert motivo, "y debe devolver el motivo del rechazo: un silencio no es auditable"

    # Y la consecuencia operativa: un host no puede declararse público sin resolverlo.
    assert not net._host_is_public("example.com"), (
        "sin resolución, un dominio no puede clasificarse como público"
    )


async def test_el_browser_no_descarga_nada_sin_dns():
    """El browser tiene que omitir fuentes, no descargarlas «por si acaso».

    Es el otro lado del mismo invariant y el que de verdad importa: cuando la validación no
    se puede hacer, la respuesta correcta es no hacer nada. Se comprueba sobre el
    comportamiento real de la herramienta —qué URLs llega a pedir— y no sobre su código, que
    es donde se cuelan las regresiones.
    """
    from tests.test_browser_tool import BrowserResearchTool, _results

    pedidas = []

    async def fetch_fn(url, timeout=None, allowed_domains=None):
        pedidas.append(url)
        return "<html><body>contenido</body></html>"

    tool = BrowserResearchTool(
        search_fn=_results([
            {"url": "https://bueno.com/a", "title": "A", "snippet": ""},
            {"url": "https://malo.org/a", "title": "B", "snippet": ""},
        ]),
        fetch_fn=fetch_fn,
    )
    out = await tool.handler({"query": "q", "allowed_domains": ["bueno.com"]})

    assert pedidas == [], (
        f"sin DNS el browser no debe pedir NADA a la red, y pidió: {pedidas}. Pedir y luego "
        f"descartar ya es haber transmitido la petición."
    )
    assert out["source_count"] == 0, (
        f"y no debe devolver fuentes: {out['source_count']}"
    )
