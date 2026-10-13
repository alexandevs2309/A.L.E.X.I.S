"""El bypass de `libpq` está documentado —esto lo vuelve CONTROL.

`docs/SECURITY.md` registra que el guardián de sockets de Python **no** ve las conexiones
que abre `libpq` (que es C, y no genera evento de auditoría). Eso está bien: documentar un
riesgo es correcto. Pero un riesgo documentado y no aplicado es una promesa, y una promesa
no es un control.

Este fichero convierte el análisis en algo que MUERDE: comprueba que el único destino que
libpq puede alcanzar sigue siendo el que fija el ENTORNO, y que nadie ha abierto una vía
para que una entrada de ALEXIS —una tool, una ruta HTTP, una capability— elija el host.

**Lo que NO se afirma aquí, y conviene no confundir:**

- El guardián sigue sin interceptar libpq. Esto no lo arregla.
- El aislamiento a nivel de kernel sigue sin existir. `net-ns` no está aplicado.
- Este control no permite salir a la red; sólo constriñe DÓNDE puede ir libpq, y ese
  destino es la base local del propio ALEXIS.

Lo que sí garantiza: si algún día alguien construye un DSN a partir de entrada externa —
la vía por la que este bypass dejaría de ser latente y pasaría a ser explotable—, la
suite falla.
"""

from __future__ import annotations

import ast
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]

#: Todo lo que `alexis/` y `apps/` ejecutan en producción. `tests/` queda fuera a
#: propósito: una prueba puede construir el DSN que quiera, no es superficie de producción.
RAIZES_PRODUCCION = ("alexis", "apps")

#: La única construcción de conexión a libpq que debe existir. Todo lo demás que abra un
#: socket a PostgreSQL con un host dinámico es exactamente el agujero que esto vigila.
CONSTRUCTORES_PERMITIDOS = {"AsyncConnectionPool"}


def _ficheros_python():
    for raiz in RAIZES_PRODUCCION:
        for path in sorted((RAIZ / raiz).rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path


def test_01_ninguna_tool_importa_psycopg():
    """El kernel y las tools no tocan libpq.

    Si una tool pudiera abrir una conexión, el destino dejaría de estar fijado por el
    entorno y pasaría a depender de lo que lo que la tool mande.
    """
    ofensores = []
    for path in _ficheros_python():
        if path.parts[-2:] == ("tools", "__pycache__"):
            continue
        texto = path.read_text(encoding="utf-8", errors="ignore")
        if "psycopg" in texto and "tools" in path.parts:
            ofensores.append(str(path.relative_to(RAIZ)))
    assert not ofensores, (
        f"una tool importa psycopg y puede abrir un socket que el guardián no ve: "
        f"{ofensores}"
    )


def test_02_la_unica_conexion_libpq_usa_el_dsn_del_entorno():
    """Un solo punto de entrada, y no recibe nada de fuera.

    Se recorre el AST buscando construcciones de conexión a libpq y se comprueba que
    ninguna recibe un DSN construido a partir de un argumento. Un
    `psycopg.connect(dsn_de_usuario)` sería el bypass hecho explotable.
    """
    constructors = {"psycopg.connect", "psycopg2.connect", "AsyncConnectionPool"}
    found: list[tuple[str, int]] = []
    for path in _ficheros_python():
        arbol = ast.parse(path.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call):
                continue
            func = nodo.func
            nombre = (
                func.attr if isinstance(func, ast.Attribute)
                else getattr(func, "id", None)
            )
            if nombre in constructors:
                found.append((str(path.relative_to(RAIZ)), nodo.lineno))

    # Debe existir exactamente un punto de entrada, en el almacenamiento.
    assert len(found) == 1, (
        f"se esperaba UN solo punto de conexión a libpq y hay {len(found)}: {found}. "
        f"Cada punto nuevo es una oportunidad de que el destino deje de ser fijo."
    )
    fichero, _ = found[0]
    assert fichero == "alexis/storage/db.py", (
        f"el punto de conexión está en {fichero}, no en el almacenamiento: el destino "
        f"deja de estar controlado por la configuración"
    )


def test_03_el_dsn_produccion_no_toma_argumentos_de_usuario():
    """`Database()` se construye SIN argumentos en toda la producción.

    Con `DEFAULT_DSN` resuelto al importar desde `ALEXIS_DATABASE_URL`, un argumento
    dinámico sería la vía por la que un atacante elige host. Cero llamadas con argumento es
    lo que hace que el bypass sea latente y no explotable.
    """
    con_argumentos = []
    for path in _ficheros_python():
        arbol = ast.parse(path.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if (isinstance(nodo, ast.Call)
                    and getattr(nodo.func, "id", None) == "Database"
                    and (nodo.args or nodo.keywords)):
                con_argumentos.append(
                    f"{path.relative_to(RAIZ)}:{nodo.lineno}"
                )
    assert not con_argumentos, (
        f"Database() recibe argumentos en producción: {con_argumentos}. Mientras reciba "
        f"cero argumentos, el host lo fija el entorno y el bypass de libpq no es alcanzable."
    )


def test_04_no_hay_capability_de_base_de_datos_en_el_catalogo():
    """No se puede PEDIR una conexión a base de datos como capacidad.

    Aunque el DSN fuera fijo, si existiera una capability `db.query` alguien podría
    usarla como canal. No existe.
    """
    from alexis.capabilities.catalog import build_catalog

    ids = {s.id for s in build_catalog().specs()}
    sospechosos = {
        i for i in ids
        if any(p in i.lower() for p in ("db.", "database", "postgres", "sql", "query"))
    }
    assert not sospechosos, (
        f"el catálogo declara capacidades de base de datos: {sospechosos}. Una de ellas "
        f"sería un camino hacia libpq que el guardián no cubre."
    )


def test_05_el_guardian_no_se_afirma_como_aislamiento():
    """El propio código debe seguir declarando lo que NO es.

    Si alguien escribiera que el guardián proporciona aislamiento, esta prueba falla. Es
    la defensa contra que la documentación y el código vuelvan a divergir —que fue
    exactamente lo que pasó con el umbral de R-03 y con `git.push` en R-06.
    """
    import alexis.tools.net as net_mod

    texto = Path(net_mod.__file__).read_text(encoding="utf-8")
    assert "No es aislamiento de red" in texto, (
        "net.py debe seguir declarando que no es aislamiento de red"
    )

    # Y el hueco de las librerías nativas tiene que seguir DOCUMENTADO. El riesgo no
    # desaparece por existir este fichero: desaparece documentado y vigilado.
    seguridad = (RAIZ / "docs" / "SECURITY.md").read_text(encoding="utf-8")
    assert "libpq" in seguridad, (
        "SECURITY.md debe seguir nombrando que libpq esquiva el guardián"
    )
    assert "nativas" in seguridad, (
        "y que las librerías nativas quedan fuera del hook"
    )
