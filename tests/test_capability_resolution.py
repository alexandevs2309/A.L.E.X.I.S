"""R-01 — una capacidad declarada se descartaba en silencio y se ejecutaba otra.

El catálogo dice que `git.read` y `git.commit` están `available`. Los adaptadores existen y
están registrados en el `ToolRegistry`. Un plan puede declararlos, el validador los acepta
porque están disponibles, la autoridad los autoriza... y entonces el executor ejecuta otra
cosa.

La causa está en `alexis/execution.py`: la resolución era

    override = _CAPABILITY_TOOL.get(step.capability)     # None si no está en la tabla
    ...
    tool_name = override or resolved_tool                # cae a la tabla por ACCIÓN

`_CAPABILITY_TOOL` sólo seis entradas (las de `fs.*`, `execute.test`, `browser.research`). Para
cualquier otra capability `override` era `None` y mandaba la acción: un paso que declara
`git.commit` con `action=execute` ejecutaba `fs.write`. Sin error, sin aviso, sin traza de
que la capability declarada se ignoró.

Eso rompe §4.4 «No fake capabilities» de una forma que el catálogo no puede detectar por sí
solo: `available()` se basa únicamente en `status`, y el catálogo no sabe nada de despacho
(cero menciones de adapter/dispatch/executor en `catalog.py`).

**Lo que NO es un defecto.** `research.filesystem` y `verification.filesystem` SÍ caen a las
tools de fs a propósito: son alias semánticos —«investiga el filesystem» ES leer el
filesystem— y hay pruebas que dependen de ello (`test_cognitive_self_model.py::
test_available_capabilities_still_execute_normally`). Este fichero fija esa frontera para que
la corrección no la arrase.
"""

from __future__ import annotations

import subprocess

import pytest

from alexis.autonomy.mission import MissionEngine
from alexis.contracts import AutonomyLevel, MissionEnvelope, PlanStep, RiskLevel
from alexis.execution import _CAPABILITY_TOOL, SandboxExecutor
from alexis.security.sandbox import SandboxRunner
from alexis.tools.filesystem import build_filesystem_tools
from alexis.tools.git import build_git_tools
from alexis.tools.terminal import build_terminal_tools
from alexis.tools.registry import ToolRegistry


def _repo(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "inicio"],
        cwd=ws, check=True,
    )
    return ws


def _entorno(ws, *, completo=True):
    """Registra las tools reales.

    `completo=True` añade las que no viven en el workspace (test, browser): sin ellas, la
    comprobación de que la tabla de resolución no apunta a tools inexistentes daría un
    falso positivo blaming al código por tools que el test simplemente no registró.
    """
    reg = ToolRegistry()
    for constructor in (build_filesystem_tools, build_git_tools, build_terminal_tools):
        for t in constructor(ws):
            reg.register(t)
    if completo:
        from alexis.tools.browser import build_browser_tools
        from alexis.tools.testrunner import build_test_tools
        for constructor in (build_test_tools, build_browser_tools):
            try:
                for t in constructor(ws):
                    reg.register(t)
            except TypeError:
                for t in constructor():
                    reg.register(t)
    return reg, SandboxExecutor(tools=reg, sandbox=SandboxRunner(workspace=ws))


def _mision(objetivo="leer el informe"):
    env = MissionEnvelope(
        objective=objetivo, autonomy=AutonomyLevel.SUPERVISED, allowed_actions=["read", "write"]
    )
    return MissionEngine().create(objetivo, env)


#: Campos que cambian entre dos ejecuciones idénticas. Compararlos sería comparar el reloj:
#: dos llamadas a la MISMA tool se declararían distintas y la prueba no distinguiría «se
#: ejecutó otra tool» de «tardó unos milisegundos más».
_VOLATILES = ("elapsed_ms", "verified_at")


def _firma(output):
    """La identidad estable del resultado de una tool."""
    if not isinstance(output, dict):
        return output
    return {k: v for k, v in output.items() if k not in _VOLATILES}


def _paso(capability, **args):
    return PlanStep(
        "s1", f"paso {capability}", "execute", RiskLevel.LOW, "executor",
        capability=capability, requires_approval=False, args=dict(args),
    )


# --------------------------------------------------------------------------- #
# 1 — la capability declarada tiene que ser la que se ejecuta
# --------------------------------------------------------------------------- #


async def test_01_una_capability_con_adaptador_propio_no_se_sustituye(tmp_path):
    """`git.read` declarada, `git.read` ejecutada.

    La comparación es contra el MISMO paso ejecutado con override explícito: si coinciden,
    la resolución respetó lo declarado. Si difieren —como antes de la corrección— se está
    ejecutando otra tool.
    """
    ws = _repo(tmp_path)
    reg, ex = _entorno(ws)
    m = _mision()
    paso = _paso("git.read", path="README.txt")

    assert reg.has("git.read"), "precondición: git.read tiene adaptador registrado"

    sin_override = await ex.execute(m, paso)
    con_override = await ex.execute(m, paso, tool_name="git.read")

    assert _firma(sin_override.output) == _firma(con_override.output), (
        f"la capability declarada no se respetó. Sin override corrió {sin_override.output!r}; "
        f"con override={con_override.output!r}. Son tools distintas: la resolución cayó a la "
        f"tabla por acción e ignoró `git.read`."
    )


async def test_02_git_commit_no_se_convierte_en_fs_write(tmp_path):
    """El caso peligroso: `git.commit` con `action=execute` acababa escribiendo un archivo.

    Escribir un fichero no es hacer un commit, y el paso podía reportar `success=True` sobre
    una operación que nunca ocurrió.
    """
    ws = _repo(tmp_path)
    reg, ex = _entorno(ws)
    m = _mision("confirmar los cambios")
    paso = _paso("git.commit", message="inicio", add_all=True)

    resultado = await ex.execute(m, paso)
    esperado = await ex.execute(m, paso, tool_name="git.commit")

    assert _firma(resultado.output) == _firma(esperado.output), (
        f"git.commit se ejecutó como otra cosa: {resultado.output!r} != {esperado.output!r}"
    )


# --------------------------------------------------------------------------- #
# 2 — la frontera que NO hay que cruzar
# --------------------------------------------------------------------------- #


async def test_03_los_alias_semanticos_siguen_resolviendo_a_sus_tools(tmp_path):
    """`research.filesystem` ES leer el filesystem, y hay contrato para que siga siéndolo.

    Si esta prueba falla, la corrección fue demasiado agresiva: se rompió un alias
    intencionado en vez de arreglar una sustitución indebida.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notas.txt").write_text("contenido", encoding="utf-8")
    _, ex = _entorno(ws)
    m = _mision("lee notas.txt")

    paso = PlanStep(
        "s1", "investigar", "research", RiskLevel.LOW, "executor",
        capability="research.filesystem", requires_approval=False, args={"path": "notas.txt"},
    )
    resultado = await ex.execute(m, paso)

    assert resultado.success is True, (
        f"el alias research.filesystem dejó de resolver a las tools de fs: {resultado.error!r}"
    )
    assert "notas.txt" in str(resultado.output) or "contenido" in str(resultado.output), (
        f"no leyó el archivo: {resultado.output!r}"
    )


# --------------------------------------------------------------------------- #
# 3 — el invariante que evita que la clase de defecto vuelva
# --------------------------------------------------------------------------- #


async def test_04_toda_capability_operativa_con_adaptador_se_despacha_de_verdad(tmp_path):
    """Ninguna capability `available` con adaptador real puede acabar ejecutando otra cosa.

    El invariante que faltaba era éste: el catálogo declaraba disponibilidad sin que nadie
    comprobara que la capability fuera ejecutable. `available()` mira sólo `status`, y el
    catálogo no sabe nada de despacho —cero menciones de adapter/dispatch/executor en
    `catalog.py`—, así que la incoherencia era invisible.

    La comprobación es COMPORTAMENTAL, no estructural: se ejecuta un paso con la capability
    declarada y se compara contra el mismo paso con override explícito. Si coinciden, la
    resolución usó la capability declarada. Mirar si el id está en una tabla sería comprobar
    la implementación en vez del contrato, y pasaría en verde con el defecto vivo.
    """
    from alexis.capabilities import build_catalog

    ws = _repo(tmp_path)
    reg, ex = _entorno(ws)
    m = _mision()
    catalogo = build_catalog()

    sospechosos = [
        s.id for s in catalogo.specs()
        if s.status in ("available", "base") and reg.has(s.id)
    ]
    assert sospechosos, "precondición: hay capabilities operativas con adaptador real"

    sustituidas = []
    for cid in sospechosos:
        # Cada ejecución parte del MISMO estado: `fs.remove` es destructivo y la primera
        # pasada borra el archivo, así que la segunda fallaría con «no existe» y la
        # comparación daría un falso positivo de sustitución.
        (ws / "README.txt").write_text("contenido", encoding="utf-8")
        paso = _paso(cid, path="README.txt", message="x", command="x", query="x")
        declarado = await ex.execute(m, paso)
        (ws / "README.txt").write_text("contenido", encoding="utf-8")
        explicito = await ex.execute(m, paso, tool_name=cid)
        if _firma(declarado.output) != _firma(explicito.output):
            sustituidas.append(cid)

    assert not sustituidas, (
        f"capabilities declaradas operativas, con adaptador registrado, y que el ejecutor "
        f"sustituye por otra: {sustituidas}. El paso pide una capacidad y se ejecuta otra: "
        f"el catálogo dice `available`, la autoridad autoriza, y ocurre algo distinto."
    )


def test_05_la_tabla_de_resolucion_no_contradice_al_registro(tmp_path):
    """La tabla no puede apuntar a una tool que no exista.

    Una entrada en `_CAPABILITY_TOOL` que nadie registró es un fallo diferido: el paso pasa
    la validación y revienta en ejecución, en producción, con un error poco informativo.
    """
    ws = _repo(tmp_path)
    reg, _ = _entorno(ws)

    huerfanas = [cid for cid in _CAPABILITY_TOOL if not reg.has(_CAPABILITY_TOOL[cid])]

    assert not huerfanas, (
        f"entradas de _CAPABILITY_TOOL sin tool registrada: {huerfanas} -> "
        f"{ {c: _CAPABILITY_TOOL[c] for c in huerfanas} }"
    )
