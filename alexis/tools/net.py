"""La ÚNICA puerta de salida a red de ALEXIS.

Existe por una razón concreta: hasta ahora cada tool decidía por su cuenta cómo
conectarse, y la protección contra SSRF vivía dentro de `browser.py`. Eso protege
`browser.research` y a nada más. Una tool futura podía llamar a `urllib` y saltárselo
entero, sin que nada lo notase —ni un test, ni una revisión, ni un aviso.

Aquí la seguridad no es una CROSS que se cruza: es la forma de hacer la petición. Si
`fetch_text()` es la única manera de descargar algo, no hay nada que recordar y nada que
olvidar. `tests/test_network_guard.py` vigila que siga siendo verdad: si aparece una
llamada de red cruda fuera del registro que documenta ese fichero, la suite falla.

**Lo que este módulo NO hace**, y conviene no confundir con lo que sí:

- **No es aislamiento de red.** Decide a qué dirección puede conectar ESTE proceso. No
  impide que otro proceso del sistema conecte a donde quiera.
- **No es una lista blanca de destinos.** Lo que bloquea son direcciones no públicas
  (loopback, privadas, link-local, reservadas) más una `allowed_domains` opcional. Un
  dominio público equivocado pasa el filtro: para eso hay policy y gate.
- **No cubre todavía las capabilities de red que faltan.** `api.http`, `mcp.run` e
  `iot.mqtt` están declaradas pero sin implementar. Cuando se escriban, deben llamar
  aquí; el guardián lo exigirá.

Sobre el DNS rebinding: `check_url()` RESUELVE el nombre y comprueba todas las
direcciones, y `fetch_text()` conecta a la que comprobó sin volver a preguntar. Sin esa
segunda mitad, validar el nombre y dejar que el cliente resuelva al conectar deja una
ventana que el atacante controla con el TTL. Ver `docs/SECURITY.md`.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

#: `https` y `http` únicamente. Sin FTP, sin `file://`, sin gopher: los esquemas raros
#: son territorio de cliente y suelen acabar en ficheros locales.
SCHEMES = frozenset({"http", "https"})

#: Sólo 80 y 443. Salir a otro puerto es salir a otro servicio, y decidir eso corresponde
#: a la policy del envelope, no a la comprobación de que el destino es público.
ALLOWED_PORTS = frozenset({None, 80, 443})

DEFAULT_TIMEOUT = 60.0

#: Tope de redirecciones. Cada salto es un destino de red nuevo: sin límite, un atacante
#: con un servidor que devuelve 302 hacia 302 puede hacer que se visited N destinos.
MAX_REDIRECTS = 5

#: El texto que sale de la red se trunca SIEMPRE. Una respuesta puede medir lo que quiera;
#: lo que entre en el contexto no.
MAX_TEXT_BYTES = 10 * 1024

_NOMBRES_LOCALES = {"localhost", "localhost.localdomain"}


def _ip_is_public(address: "ipaddress._BaseAddress") -> bool:
    """Clasifica una IP ya resuelta. Sin red: sólo mira el número."""
    return not (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_reserved or address.is_multicast or address.is_unspecified
    )


def resolve_public(host: str, puerto: "int | None" = None) -> tuple[list[str], str]:
    """`(direcciones públicas, "")` o `([], motivo)` si el host no es seguro.

    **Aquí estaba el agujero.** Antes `_host_is_public()` devolvía `True` a cualquier
    nombre que no fuese una IP literal: validaba la CADENA, no la DIRECCIÓN a la que se
    acabaría conectando. Un dominio hostil que devuelve `127.0.0.1` o `169.254.169.254`
    pasaba el filtro sin mirar.

    Ahora un nombre se resuelve y se comprueban TODAS las direcciones que devuelve:

    - Si **alguna** es privada, se rechaza el conjunto entero. La elección final entre
      ellas la hace el cliente HTTP, no este código, así que quedarse con "la buena"
      dejaría la interna disponible para cuando al atacante le convenga.
    - Si el DNS **falla**, se rechaza. Lo desconocido no se permite: dejarlo pasar sería
      exactamente la transición a la nada que este riesgo describe.

    `puerto` no cambia la clasificación —la IP es la misma—, pero viaja para que quien
    relaje esta política en un test pueda distinguir el servidor de pruebas de otro
    destino en el mismo host. Sin él, un servidor local y un loopback de redirección
    serían indistinguibles.

    Es síncrono a propósito: `check_url()` lo es, y una resolución con `await` aquí
    obligaría a arrastrar el cambio por toda la política de destinos.
    """
    if not host:
        return [], "host vacío"
    lowered = host.strip("[]").lower()
    if lowered in _NOMBRES_LOCALES or lowered.endswith(".local"):
        return [], f"nombre local: {host}"

    # ¿IP literal? No requiere red: es una clasificación del número.
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        pass
    else:
        if not _ip_is_public(address):
            return [], f"dirección no pública: {lowered}"
        return [lowered], ""

    try:
        # AF_UNSPEC para no descartar IPv6: una AAAA interna sirve igual que una A
        # interna para hacer rebinding, y filtrar sólo IPv4 dejaría la puerta abierta.
        infos = socket.getaddrinfo(lowered, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        return [], f"no se pudo resolver {host}: {exc.__class__.__name__}"

    direcciones: list[str] = []
    for info in infos:
        # Se quita el %scope de las IPv6 con interfaz: no es parte de la dirección.
        literal = str(info[4][0]).split("%")[0]
        try:
            resuelta = ipaddress.ip_address(literal)
        except ValueError:
            return [], f"resolución con dirección ininterpretable: {literal!r}"
        if not _ip_is_public(resuelta):
            return [], f"{host} resuelve a una dirección no pública: {literal}"
        direcciones.append(literal)

    if not direcciones:
        return [], f"{host} no resuelve a ninguna dirección"
    return direcciones, ""


def _host_is_public(host: str) -> bool:
    """El host resuelve a direcciones públicas.

    Un nombre NO se acepta sin resolver: ése es el requisito entero del DNS rebinding.
    """
    return bool(resolve_public(host)[0])


def check_url(
    url: str,
    allowed_domains: list[str] | None = None,
    *,
    resuelto: "tuple[list[str], str] | None" = None,
) -> tuple[bool, str]:
    """`(permitida, razón)`. Se llama ANTES de descargar cada URL.

    Esta función es TOTAL: nunca lanza. La razón es que desde que las redirecciones se
    siguen aquí, la URL a validar la elige un atacante (va en la cabecera `Location`), y
    una excepción en este punto dejaría la descarga a medias en vez de rechazar el
    destino. `urlparse(...).port` lanza `ValueError` con puertos fuera de rango o no
    numéricos, así que se captura explícitamente en lugar de confiar en el parseo.
    """
    try:
        parts = urlparse(url)
    except ValueError as exc:
        return False, f"URL inválida: {exc}"
    if parts.scheme not in SCHEMES:
        return False, f"esquema no permitido: {parts.scheme or '(vacío)'}"
    try:
        port = parts.port
    except ValueError as exc:
        return False, f"puerto inválido: {exc}"
    if port not in ALLOWED_PORTS:
        return False, f"puerto no permitido (sólo 80/443): {port}"
    host = (parts.hostname or "").lower()
    # La allowlist se consulta ANTES de resolver, por dos razones. Una: un host fuera de
    # la lista va a rechazarse igual, así que preguntarle al DNS es trabajo tirado — y
    # una consulta saliente a un dominio rechazado es una fuga innecesaria. Dos: el
    # motivo que se reporta es el útil. Al revés, un dominio que ni resuelve oculta el
    # motivo real del rechazo y el diagnóstico se vuelve mucho más difícil.
    if allowed_domains:
        wanted = {d.strip().lower().lstrip(".") for d in allowed_domains if str(d).strip()}
        if not any(host == d or host.endswith(f".{d}") for d in wanted):
            return False, f"dominio fuera de allowed_domains: {host}"

    # `resuelto` es la resolución que `http_fetch` ya hizo para FIJAR la dirección. Se
    # reutiliza en vez de repetirla: dos consultas al DNS dejan abierta, ella misma, la
    # ventana que el fijado acaba de cerrar — entre una y otra el atacante puede cambiar
    # su respuesta—. Con una sola consulta, lo validado y lo conectado son lo mismo.
    direcciones, motivo = resuelto if resuelto is not None else resolve_public(host, port)
    if not direcciones:
        return False, f"host no público (red local/metadatos): {motivo}"
    return True, ""


class FetchBlocked(RuntimeError):
    """Un destino —el inicial o el de una redirección— no pasó `check_url`.

    Deliberadamente NO es un error genérico: quien lo captura necesita distinguir "el
    destino se rechazó por política" de "la descarga falló", porque sólo el primero es un
    intento de acceso que conviene registrar y no reintentar.
    """



# --------------------------------------------------------------------------- #
# Fijado de la dirección — cierra la ventana entre validar y conectar
# --------------------------------------------------------------------------- #


class _BackendFijado:
    """Conecta a la dirección YA validada, conservando el nombre para el TLS.

    Sin esto, validar el nombre y después dejar que httpx vuelva a resolver deja una
    ventana: entre las dos consultas el atacante cambia su DNS y la conexión acaba
    donde le conviene. Validar sin fijar es exactamente el Nivel A, y no para al
    atacante para quien existe — él controla el TTL y elige el momento.

    Fijar la dirección no rompe el certificado: httpcore pide `connect_tcp` con el host
    ORIGINAL y aplica el TLS aparte con `server_hostname=origin.host`, así que SNI y
    `Host` siguen siendo el nombre de verdad. Lo único que cambia es a qué IP se abre el
    socket: la que se comprobó.

    El resto de métodos se delegan tal cual. Un backend que no delegara `sleep()` o
    `connect_unix_socket()` rompería el pool de formas difíciles de diagnosticar.
    """

    def __init__(self, fijadas: dict[str, str]):
        self._fijadas = fijadas
        self._real = None

    def _backend(self):
        if self._real is None:
            from httpcore._backends.auto import AutoBackend

            self._real = AutoBackend()
        return self._real

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        ip = self._fijadas.get(host)
        # Sin entrada en el mapa: es un host que NO pasó por `resolve_public`, así que
        # no se fija y se deja el comportamiento por defecto. La validación ocurre antes
        # y ese camino ya debería haber bloqueado; fijar aquí a ciegas sería peor.
        destino = ip if ip is not None else host
        return await self._backend().connect_tcp(
            destino, port, timeout=timeout, local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return await self._backend().connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds):
        return await self._backend().sleep(seconds)


def transporte_fijado(fijadas: dict[str, str]):
    """Transporte de httpx que conecta a las direcciones ya validadas.

    Se toca `_pool._network_backend` porque httpx no expone el pool. Es superficie
    privada de httpx 0.28/httpcore 1.0: si cambia, falla al construir el transporte y
    no en silencio. Se comprueba aquí para que el fallo sea temprano y ruidoso.
    """
    import httpx

    transporte = httpx.AsyncHTTPTransport()
    pool = getattr(transporte, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        raise RuntimeError(
            "no se puede fijar la dirección: httpx/httpcore cambiaron la estructura del "
            "pool. Sin esto el DNS rebinding vuelve a estar abierto y la validación "
            "sólo sería del NOMBRE."
        )
    pool._network_backend = _BackendFijado(fijadas)
    return transporte


async def fetch_text(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
    allowed_domains: list[str] | None = None,
) -> str:
    """Descarga el texto de una URL, con la política aplicada en cada salto.

    Es la función que las tools deberían llamar para hablar con la red. Cliente nuevo por
    petición: sin cookies, sin sesión, sin estado entre destinos.

    Las redirecciones se siguen AQUÍ, una a una, y NO con `follow_redirects=True`.

    Por qué no se delega en el cliente: una redirección es un destino de red NUEVO. La
    URL inicial la autorizó quien la pidió; la que acaba en `Location` la eligió el
    servidor al que acabamos de conectar, y ese servidor no es de fiar — responder 302
    hacia `169.254.169.254` o hacia un host fuera de la allowlist es el ataque más
    barato contra un buscador. Con `follow_redirects=True` el cliente decide a dónde ir
    y esa decisión no pasa por `check_url()`: se comprobó que acababa conectando a la IP
    de metadatos pese a estar marcada como no pública. Así que cada salto se valida con
    la MISMA política antes de abrir la conexión siguiente, y un destino rechazado no se
    contacta ni se devuelve.

    `allowed_domains` viaja aquí a propósito: sin él, la allowlist sólo filtraría la
    puerta de entrada y cualquier redirect la convertiría en decorativa.
    """
    import httpx

    permitido, razon = check_url(url, allowed_domains)
    if not permitido:
        raise FetchBlocked(f"destino bloqueado: {razon}")

    actual = url
    for salto in range(MAX_REDIRECTS + 1):
        # Se resuelve y se FIJA el host de este salto. `check_url` ya lo validó, pero
        # aquí se vuelve a resolver para CONGELAR el resultado: si se conectara con el
        # que dio la validación, entre ambas consultas el DNS pudo cambiar, y eso es
        # exactamente la ventana que el fijado cierra.
        host_actual = (urlparse(actual).hostname or "").lower()
        fijadas, motivo = resolve_public(host_actual, urlparse(actual).port)
        if not fijadas:
            raise FetchBlocked(f"destino bloqueado: {motivo}")
        # Se valida con ESTA resolución, no con una nueva: lo que se comprueba y lo que
        # se conecta son la misma dirección consultada una vez.
        permitido, razon = check_url(actual, allowed_domains, resuelto=(fijadas, motivo))
        if not permitido:
            raise FetchBlocked(f"destino bloqueado: {razon}")

        # Un cliente por salto: cada uno puede ser un host distinto, y cada host tiene su
        # dirección fijada. No queda estado entre destinos —igual que no quedan cookies—,
        # que es la propiedad que hace `http_fetch` segura de reutilizar.
        try:
            async with httpx.AsyncClient(
                follow_redirects=False,  # los saltos se validan uno a uno más abajo
                timeout=timeout,
                headers={"User-Agent": "ALEXIS-research/0.3 (static text only)"},
                cookies={},
                transport=transporte_fijado({host_actual: fijadas[0]}),
            ) as client:
                response = await client.get(actual)
        except httpx.RemoteProtocolError as exc:
            # httpx construye SIEMPRE la request de redirección, incluso con
            # `follow_redirects=False` (sólo la guarda en `response.next_request`).
            # Un `Location` corrupto lo revienta ahí, antes de que este módulo pueda
            # mirarlo. Es un rechazo de destino disfrazado de error de red, y quien
            # llama debe poder distinguirlo.
            raise FetchBlocked(f"redirección malformada (salto {salto + 1}): {exc}") from exc

        if not response.is_redirect:
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            encoding = response.charset_encoding or "utf-8"
            return response.text[:MAX_TEXT_BYTES * 4]  # margen para el tag-stripping

        # Un 3xx sin `Location` no lleva a ninguna parte: no se reintenta a ciegas.
        location = response.headers.get("location") or ""
        if not location.strip():
            raise RuntimeError(f"HTTP {response.status_code} sin cabecera Location")

        # `urljoin` contra la URL actual: una `Location` relativa ("/otra") es
        # legítima y debe resolverse sobre el origen, no rechazarse ni tomarse como
        # absoluta. `urljoin` puede lanzar `ValueError` con referencias malformadas
        # (`http://[::1` → IPv6 sin cerrar) y el `Location` lo elige el servidor
        # remoto: sin capturarlo, un Location corrupto tumbaría la descarga en vez
        # de rechazarse.
        try:
            siguiente = urljoin(actual, location.strip())
        except ValueError as exc:
            raise FetchBlocked(f"redirección inválida (salto {salto + 1}): {exc}") from exc
        permitido, razon = check_url(siguiente, allowed_domains)
        if not permitido:
            raise FetchBlocked(
                f"redirección bloqueada (salto {salto + 1} hacia {siguiente}): {razon}"
            )
        actual = siguiente

    raise RuntimeError(f"demasiadas redirecciones (más de {MAX_REDIRECTS})")


# --------------------------------------------------------------------------- #
# La tool
# --------------------------------------------------------------------------- #
