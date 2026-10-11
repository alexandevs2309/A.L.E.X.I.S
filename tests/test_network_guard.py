"""Guardián de la salida a red: la protección no puede depender de que alguien la recuerde.

Desde que `alexis/tools/net.py` es la única puerta de salida a red, la SSRF de
`browser.research` está cubierta Y las tools que vengan después heredan la protección
gratis… siempre que usen ese módulo. Este test es lo que convierte esa regla de
convenio en una regla que CI exige.

Qué hace: recorre `alexis/` buscando primitivas de red crudas y falla si aparece una en
un fichero que no esté en el registro. Cada entrada del registro declara **de dónde sale
su URL**, que es la pregunta que decide si un destino es atacable: una URL que viene de
un resultado de búsqueda sí lo es; una que viene de la configuración del operador, no.

**Por qué un registro y no una prohibición total.** No se puede prohibir `urlopen`: ALEXIS
habla con su propia base de datos y con los bridges de escritorio, y todo eso pasa por
`127.0.0.1`. Un guardián que bloqueara loopback rompería el runtime. La pregunta útil no
es "¿este fichero usa red?" sino "¿esta URL puede elegirla alguien que no sea el
operador?".

**Qué NO garantiza.** El registro es una lista, y una lista se puede ampliar sin motivo
legítimo. Lo que consigue es que la ampliación sea **visible en el diff del PR**, con la
pregunta escrita al lado. Hoy nadie hace esa pregunta; a partir de aquí, la hace el
diff.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ALEXIS = pathlib.Path(__file__).resolve().parents[1] / "alexis"

#: Primitivas que abren un socket o piden una URL. Se buscan por nombre en el AST, no con
#: grep: así un `# httpx` en un comentario no dispara la alarma y un `httpx` usado con
#: otro alias tampoco se esconde.
PRIMITIVAS_RED = {
    "urlopen": "urllib.request.urlopen",
    "create_connection": "socket.create_connection",
    "open_connection": "asyncio.open_connection",
}

MODULOS_RED = {"httpx", "requests", "aiohttp", "urllib3"}

#: Ficheros con red, con la procedencia de su URL y por qué es aceptable.
#:
#: Añadir una entrada aquí NO es un trámite: obliga a responder de dónde sale la URL.
#: Si la respuesta es "de una respuesta del modelo" o "de una página", la ruta debería
#: pasar por `net.fetch_text()` en lugar de abrir su propio socket.
REGISTRO: dict[str, str] = {
    "alexis/tools/net.py": (
        "LA puerta de salida. Resuelve, valida y fija la dirección antes de conectar; "
        "es el módulo que este test protege."
    ),
    "alexis/models/providers/http_base.py": (
        "`base_url` viene de la CONFIGURACIÓN del operador (fichero .env de secrets/), "
        "nunca de contenido remoto. Un `base_url` malicioso es una malconfiguración que "
        "el operador escribe a mano, no un vector de ataque. Cuando haya credenciales de "
        "tercero conviene validar el host, pero no es un SSRF: nadie externo elige esa URL."
    ),
    "alexis/perception/bridge_host.py": (
        "El puente del HOST que captura el micrófono. Su URL viene del flag `--url` de "
        "la línea de comandos del operador (por defecto `http://127.0.0.1:8100`). Fija en "
        "el despliegue y no la elige ningún dato remoto."
    ),
    "alexis/perception/desktop_bridge.py": (
        "El puente del HOST que vigila el escritorio. Igual que el anterior: la URL es "
        "el flag `--url` de la CLI. Además `chrome.open_url` NO descarga nada aquí —pasa "
        "la URL al navegador del usuario—, así que tampoco es una salida SSRF de este "
        "proceso."
    ),
}

#: Ficheros exentos por completo, con motivo. Un exento sin motivo no entra en el
#: registro: si no hace red, no debería aparecer aquí.
EXENTOS: dict[str, str] = {
    "alexis/tools/net.py": "Es el registro; no se vigila a sí mismo por escaneo.",
}


def _red_en_fichero(ruta: pathlib.Path) -> list[str]:
    """Primitivas de red que aparecen en el fichero, como texto `línea: qué`."""
    try:
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    encontradas = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            for alias in nodo.names:
                raiz = alias.name.split(".")[0]
                if raiz in MODULOS_RED:
                    encontradas.append(f"L{nodo.lineno}: import {alias.name}")
        elif isinstance(nodo, ast.ImportFrom):
            raiz = (nodo.module or "").split(".")[0]
            if raiz in MODULOS_RED:
                encontradas.append(f"L{nodo.lineno}: from {nodo.module}")
            for alias in nodo.names:
                if alias.name in PRIMITIVAS_RED:
                    encontradas.append(
                        f"L{nodo.lineno}: from {nodo.module} import {alias.name}"
                    )
        elif isinstance(nodo, ast.Call):
            nombre = getattr(nodo.func, "attr", None) or getattr(nodo.func, "id", None)
            if nombre in PRIMITIVAS_RED:
                encontradas.append(f"L{nodo.lineno}: {nombre}()")
    return encontradas


def _ficheros_python() -> list[pathlib.Path]:
    return sorted(
        p for p in ALEXIS.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def test_el_registro_cubre_toda_la_red_de_alexis():
    """Cada fichero que hace red está registrado, y cada registro tiene su motivo.

    Este es el test que falla cuando alguien añade `urlopen` a un fichero nuevo. No dice
    "no uses red": dice "si haces red, explica de dónde sale la URL", que es la pregunta
    que separa un puente del operador de una tool que sigue enlaces de una web.
    """
    sin_registro: dict[str, list[str]] = {}
    for ruta in _ficheros_python():
        rel = str(ruta.relative_to(ALEXIS.parent))
        if rel in EXENTOS:
            continue
        usos = _red_en_fichero(ruta)
        if usos and rel not in REGISTRO:
            sin_registro[rel] = usos

    assert not sin_registro, (
        "ficheros con salida de red que NO están en el registro de "
        "`tests/test_network_guard.py`:\n"
        + "\n".join(f"  {f}: {', '.join(u)}" for f, u in sorted(sin_registro.items()))
        + "\n\nSi tu URL viene de un resultado de búsqueda, de una respuesta del modelo o "
        "de una página, la ruta correcta es `alexis.tools.net.fetch_text()`: resuelve, "
        "valida y fija la dirección. Añadir la entrada al registro es legítimo sólo si la "
        "URL la elige el operador, no un atacante."
    )


def test_toda_entrada_del_registro_existe_y_justifica():
    """El registro no se pudre: entradas que ya no aplican, o sin motivo, se detectan."""
    sin_motivo = [f for f, motivo in REGISTRO.items() if len(motivo) < 30]
    assert not sin_motivo, f"entradas del registro sin motivo leveraged: {sin_motivo}"

    inexistentes = [f for f in REGISTRO if not (ALEXIS.parent / f).is_file()]
    assert not inexistentes, (
        f"el registro apunta a ficheros que ya no existen: {inexistentes}. "
        "Un registro obsoleto es un registro en el que nadie confía."
    )


def test_net_no_depende_de_browser():
    """La puerta de red no puede depender de una tool concreta.

    Si `net` importara `browser`, la política de SSRF volvería a depender de que esa tool
    esté bien, que es exactamente la dependencia que este refactor elimina.
    """
    fuente = (ALEXIS / "tools" / "net.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and (nodo.module or "").startswith("alexis.tools.browser"):
            pytest.fail(f"net.py importa de browser en la línea {nodo.lineno}: crea un ciclo")
        if isinstance(nodo, ast.Import):
            for alias in nodo.names:
                if alias.name.startswith("alexis.tools.browser"):
                    pytest.fail(f"net.py importa browser en la línea {nodo.lineno}")


def test_browser_reexporta_la_superficie_publica():
    """Quien ya importaba de `browser` sigue funcionando.

    El refactor movió la implementación a `net`. Romper los imports existentes habría
    cambiado la superficie pública de un módulo que otras tools y tests usan.
    """
    from alexis.tools import browser, net

    for nombre in ("check_url", "resolve_public", "FetchBlocked", "http_fetch"):
        assert hasattr(browser, nombre), f"browser ya no expone {nombre}"
    assert browser.check_url is net.check_url
    assert browser.http_fetch is net.fetch_text