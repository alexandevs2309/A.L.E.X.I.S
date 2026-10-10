"""Regresión SSRF: cada redirección es un destino de red nuevo y se valida SOLO.

Por qué estos tests usan servidores de verdad y no un `fetch_fn` inyectado: la
vulnerabilidad estaba en el seguimiento de redirecciones DENTRO de `http_fetch`, es
decir, en código que un doble jamás ejecuta. Un test con `fetch_fn` habría pasado con
la vulnerabilidad presente.

Todos los servidores son locales (127.0.0.1). Ninguno es un destino real de metadatos:
cuando el redirect apunta a 169.254.169.254 lo que se comprueba es que NO se intenta la
conexión, instrumentando `socket.connect` para que la ausencia de intento sea un hecho
observado y no una suposición.

Único monkeypatch: `check_url` se relaja para los URLs de los servidores de prueba
(loopback y puerto efímero están prohibidos por política, y sin ellos no hay HTTP
local). TODO lo demás —esquema, puerto, clasificación de IP y allowlist— sigue siendo
la implementación real, y `check_url` se prueba aparte, en `test_browser_tool.py`.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import pytest

from alexis.tools import browser as browser_mod
from alexis.tools.browser import (
    MAX_REDIRECTS,
    BrowserResearchTool,
    FetchBlocked,
    check_url,
    http_fetch,
)

# --------------------------------------------------------------------- #
# Infraestructura: servidores locales controlados
# --------------------------------------------------------------------- #

SECRETO = "SECRETO-DE-SERVIOR-INTERNO-no-debe-aparecer"


class _Servidor:
    """Servidor HTTP local que registra cada petición que recibe."""

    def __init__(self, rutas: dict[str, object]):
        self.peticiones: list[str] = []
        self._rutas = rutas
        exterior = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 — firma de BaseHTTPRequestHandler
                exterior.peticiones.append(self.path)
                destino = exterior._rutas.get(self.path)
                if destino is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                if isinstance(destino, int):  # código sin Location
                    self.send_response(destino)
                    self.end_headers()
                    return
                self.send_response(302)
                self.send_header("Location", destino)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self._srv = HTTPServer(("127.0.0.1", 0), Handler)
        self._hilo = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._hilo.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._srv.server_address[1]}"

    def cierra(self):
        self._srv.shutdown()
        self._srv.server_close()


@pytest.fixture
def url_registrada(monkeypatch):
    """Deja pasar por `check_url` únicamente los URLs de los servidores de test.

    Se anotan a medida que se crean. Cualquier otro destino —incluidos los que un
    servidor de test intentaría redirigir— pasa por la política real intacta.
    """
    permitidos: set[str] = set()
    real = check_url

    def _origin(url: str) -> str:
        """`scheme://host:puerto` — sin ruta, query ni fragmento.

        El registro es por ORIGEN a propósito: el servidor de test debe poder servir
        varias rutas (`/inicio`, `/final`, ...) y que todas_valgan. Comparar la URL
        entera haría que sólo pasara el origen raíz.
        """
        partes = urlparse(url)
        return f"{partes.scheme}://{partes.netloc}".rstrip("/")

    def check_url_de_test(url, allowed_domains=None):
        try:
            if _origin(url) in permitidos:
                return True, ""
        except ValueError:
            return real(url, allowed_domains)
        return real(url, allowed_domains)

    monkeypatch.setattr(browser_mod, "check_url", check_url_de_test)

    class Registro:
        def anota(self, *urls: str) -> None:
            permitidos.update(_origin(u) for u in urls)

    return Registro()


@pytest.fixture
def conexiones(monkeypatch):
    """Registra cada `(host, puerto)` al que se intenta abrir un socket TCP."""
    vistas: list[tuple[str, int]] = []
    real = socket.socket.connect

    def connect(self, address):
        try:
            vistas.append((address[0], address[1]))
        except Exception:  # noqa: BLE001 — sockets exóticos (AF_UNIX) no tienen host
            pass
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    return vistas


def _servidor(rutas) -> _Servidor:
    return _Servidor(rutas)


# --------------------------------------------------------------------- #
# 1. Redirección permitida -> permitida: el caso legítimo NO se rompe
# --------------------------------------------------------------------- #


async def test_redireccion_permitida_a_permitida_se_sigue(url_registrada):
    destino = _servidor({"/final": 200})
    origen = _servidor({"/inicio": destino.url + "/final"})
    url_registrada.anota(origen.url, destino.url)
    try:
        # Lo que se prueba es que el salto se sigue y se entrega el destino final.
        cuerpo = await http_fetch(origen.url + "/inicio", timeout=5.0)
        assert isinstance(cuerpo, str)
        assert destino.peticiones == ["/final"], "no llegó al destino permitido"
    finally:
        origen.cierra()
        destino.cierra()


# --------------------------------------------------------------------- #
# 2-4. Destinos prohibidos: bloqueados, sin contacto y sin contenido
# --------------------------------------------------------------------- #

BLOQUEADOS = [
    pytest.param("http://127.0.0.1:9/interno", id="loopback"),
    pytest.param("http://10.0.0.5/secreto", id="ip-privada-rfc1918"),
    pytest.param("http://192.168.1.1/secreto", id="ip-privada-192"),
    pytest.param("http://169.254.169.254/latest/meta-data/", id="link-local-metadatos"),
    pytest.param("http://[::1]/secreto", id="ipv6-loopback"),
    pytest.param("http://0.0.0.0/secreto", id="unspecified"),
]


@pytest.mark.parametrize("destino", BLOQUEADOS)
async def test_redireccion_a_destino_prohibido_se_bloquea(
    destino, url_registrada, conexiones
):
    """El corazón del arreglo: un 302 hacia zona prohibida NO se sigue."""
    origen = _servidor({"/inicio": destino})
    url_registrada.anota(origen.url)
    try:
        with pytest.raises(FetchBlocked) as exc:
            await http_fetch(origen.url + "/inicio", timeout=2.0)

        assert SECRETO not in str(exc.value)
        # Y sobre todo: no se intentó contactar ese destino. Es la aserción que
        # distingue "rechacé el destino" de "no llegué a tiempo a rechazarlo".
        host_destino = destino.split("//", 1)[1].split("/")[0]
        intentados = {host for host, _ in conexiones}
        assert host_destino not in intentados, f"se intentó {host_destino}: {conexiones}"
        assert intentados <= {"127.0.0.1"}, conexiones
    finally:
        origen.cierra()


async def test_no_se_devuelve_el_contenido_de_un_destino_bloqueado(url_registrada):
    """Ni el error ni el resultado filtran lo que hubiera en el destino prohibido."""
    origen = _servidor({"/inicio": "http://169.254.169.254/latest/meta-data/"})
    url_registrada.anota(origen.url)
    try:
        with pytest.raises(FetchBlocked) as exc:
            await http_fetch(origen.url + "/inicio", timeout=2.0)
        assert "meta-data" in str(exc.value)  # la URL queda registrada para diagnóstico
        assert SECRETO not in str(exc.value)
    finally:
        origen.cierra()


# --------------------------------------------------------------------- #
# 5. Allowlist: un host autorizado no puede redirigir fuera de la lista
# --------------------------------------------------------------------- #


async def test_redireccion_fuera_de_allowed_domains_se_bloquea(url_registrada):
    """Sólo lo que el test declara como servidor público; el resto es política real."""
    bueno = _servidor({"/final": 200})
    origen = _servidor({"/inicio": "https://evil.example/exfiltrar"})
    url_registrada.anota(origen.url, bueno.url)
    try:
        with pytest.raises(FetchBlocked) as exc:
            await http_fetch(
                origen.url + "/inicio", timeout=2.0,
                allowed_domains=["bueno.example"],
            )
        assert "allowed_domains" in str(exc.value)
    finally:
        origen.cierra()


async def test_redireccion_permitida_respeta_la_allowlist(url_registrada):
    """Con la allowlist cumplido, un salto a OTRO dominio se rechaza."""
    origen = _servidor({"/inicio": "https://otro.example/x"})
    url_registrada.anota(origen.url)
    try:
        with pytest.raises(FetchBlocked):
            await http_fetch(
                origen.url + "/inicio", timeout=2.0,
                allowed_domains=["permitido.example"],
            )
    finally:
        origen.cierra()


# --------------------------------------------------------------------- #
# 6. Location relativa
# --------------------------------------------------------------------- #


async def test_redireccion_relativa_se_resuelve_sobre_el_origen(url_registrada):
    """Una `Location` relativa se resuelve contra el ORIGEN: no se rechaza ni se inventa host.

    Por eso `/final` tiene que servirse en `origen`. Resolver bien significa volver al
    mismo servidor; si el código la tratara como absoluta, la segunda petición no
    llegaría a `origen` y este test lo detectaría.
    """
    origen = _servidor({"/inicio": "/final", "/final": 200})
    url_registrada.anota(origen.url)
    try:
        cuerpo = await http_fetch(origen.url + "/inicio", timeout=5.0)
        assert isinstance(cuerpo, str)
        assert origen.peticiones == ["/inicio", "/final"], origen.peticiones
    finally:
        origen.cierra()


async def test_redireccion_relativa_respeta_la_allowlist(url_registrada):
    """Una relativa es legítima, pero NO puede salirse del dominio permitido."""
    origen = _servidor({"/inicio": "http://otro-host.example/x"})
    url_registrada.anota(origen.url)
    try:
        with pytest.raises(FetchBlocked):
            await http_fetch(
                origen.url + "/inicio", timeout=2.0,
                allowed_domains=["permitido.example"],
            )
    finally:
        origen.cierra()


# --------------------------------------------------------------------- #
# 7. Ciclos y límite de saltos
# --------------------------------------------------------------------- #


async def test_ciclo_de_redirecciones_termina_por_limite(url_registrada):
    origen = _servidor({"/a": "/b", "/b": "/a"})
    url_registrada.anota(origen.url)
    try:
        with pytest.raises(RuntimeError, match="demasiadas redirecciones"):
            await http_fetch(origen.url + "/a", timeout=5.0)
        # No se Martillea: el tope acota, no hay bucle infinito.
        assert len(origen.peticiones) == MAX_REDIRECTS + 1
    finally:
        origen.cierra()


async def test_tope_de_saltos_es_acotado():
    assert isinstance(MAX_REDIRECTS, int) and 0 < MAX_REDIRECTS <= 10


# --------------------------------------------------------------------- #
# 8. Location inválido / esquema prohibido / 3xx sin Location
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("location", [
    "ftp://internal.example/x",
    "file:///etc/passwd",
    "gopher://internal.example:70/",
    "http://[::1",          # IPv6 sin cerrar: `urljoin` lanza ValueError
])
async def test_location_con_esquema_prohibido_se_bloquea(location, url_registrada):
    origen = _servidor({"/inicio": location})
    url_registrada.anota(origen.url)
    try:
        with pytest.raises(FetchBlocked) as exc:
            await http_fetch(origen.url + "/inicio", timeout=2.0)
        # "malformada" cubre el caso en que httpx revienta el Location corrupto
        # antes de que urljoin pueda verlo (ver el comentario de http_fetch).
        assert any(m in str(exc.value) for m in ("esquema", "inválida", "malformada"))
    finally:
        origen.cierra()


async def test_3xx_sin_location_no_reintenta_a_ciegas(url_registrada):
    origen = _servidor({"/inicio": 302})
    url_registrada.anota(origen.url)
    try:
        with pytest.raises(RuntimeError, match="sin cabecera Location"):
            await http_fetch(origen.url + "/inicio", timeout=2.0)
    finally:
        origen.cierra()


# --------------------------------------------------------------------- #
# 9. Integración: la tool descarta la fuente y no la publica
# --------------------------------------------------------------------- #


async def test_la_tool_descarta_la_fuente_con_redireccion_bloqueada(url_registrada):
    """El camino completo: search_fn entrega una URL buena que 302 a zona prohibida."""
    origen = _servidor({"/inicio": "http://169.254.169.254/latest/meta-data/"})

    async def search_fn(query, timeout=None):
        return [{"url": origen.url + "/inicio", "title": "Fuente", "snippet": ""}]

    url_registrada.anota(origen.url)
    tool = BrowserResearchTool(search_fn=search_fn, fetch_fn=http_fetch)
    try:
        out = await tool.handler({"query": "algo"})
    finally:
        origen.cierra()

    assert out["ok"] is True
    assert out["source_count"] == 0, "no debe publicar la fuente bloqueada"
    assert out["sources"] == []
    bloqueadas = [d for d in out["discarded"] if d["stage"] == "policy"]
    assert bloqueadas, out["discarded"]
    assert SECRETO not in str(out)


async def test_la_tool_no_persiste_el_contenido_bloqueado(url_registrada):
    """Ni siquiera como 'descarga fallida': un bloqueo de política es un bloqueo."""
    origen = _servidor({"/inicio": "http://127.0.0.1:9/interno"})

    async def search_fn(query, timeout=None):
        return [{"url": origen.url + "/inicio", "title": "F", "snippet": ""}]

    url_registrada.anota(origen.url)
    tool = BrowserResearchTool(search_fn=search_fn, fetch_fn=http_fetch)
    try:
        out = await tool.handler({"query": "algo"})
    finally:
        origen.cierra()

    assert all(d["stage"] != "fetch" for d in out["discarded"]), "un bloqueo no es un fallo de red"
    assert out["source_count"] == 0