"""Mínimo privilegio en la ejecución: `Tool.permissions` se validan contra el envelope.

Por qué este chequeo existe y no basta el de `PolicyEngine`: la policy juzga lo que el
PASO declara. Un paso `action="execute"` sin `capability` explícita se resuelve a una tool
de escritura, y el envelope podía no haber concedido esa capability. Aquí se juzga lo que
la TOOL exige, justo antes de invocarla (regla 13 de VISION.md).

Una tool que no declara `permissions` ni `capability_id` no exige nada y se ejecuta igual:
es el caso de retrocompatibilidad, y no por eje el permiso.
"""

from __future__ import annotations

import pytest

from alexis.contracts import AutonomyLevel, Goal, Mission, MissionEnvelope, PlanStep, RiskLevel
from alexis.execution import SandboxExecutor, _required_capabilities
from alexis.security.sandbox import SandboxRunner
from alexis.tools.filesystem import build_filesystem_tools
from alexis.tools.registry import Tool, ToolRegistry


def _mission(capabilities, *, actions=("read", "research", "write", "execute")) -> Mission:
    return Mission(
        "m-permiso",
        goal=Goal(objective="escribe el informe", constraints={},
                  success_criteria=["file_exists:informe.txt"]),
        envelope=MissionEnvelope(
            objective="escribe el informe",
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=list(actions),
            capabilities=list(capabilities),
        ),
    )


def _executor(tmp_path, tools) -> SandboxExecutor:
    registry = ToolRegistry()
    registry.register_all(tools)
    return SandboxExecutor(tools=registry, sandbox=SandboxRunner(workspace=tmp_path))


def _step(capability=None, action="execute"):
    return PlanStep("s1", "escribe el informe", action, RiskLevel.LOW, "executor",
                    capability=capability, args={"path": "informe.txt", "content": "hola"})


# --------------------------------------------------------------------------- #
# Derivación de requisitos
# --------------------------------------------------------------------------- #


def test_una_tool_que_no_declara_nada_no_exige_nada():
    """Retrocompatibilidad: sin `permissions` ni `capability_id`, no hay requisito."""
    assert _required_capabilities(Tool(name="t", description="d")) == []


def test_capability_id_es_el_requisito_autoritativo():
    tool = Tool(name="terminal.run", description="d", capability_id="terminal.run")
    assert _required_capabilities(tool) == ["terminal.run"]


def test_las_banderas_de_permissions_se_traducen_a_capabilities():
    tool = Tool(name="x", description="d", permissions={"write": True, "read": True})
    assert _required_capabilities(tool) == ["fs.write", "fs.read"]


# --------------------------------------------------------------------------- #
# Comportamiento en ejecución
# --------------------------------------------------------------------------- #


async def test_tool_que_exige_fuera_del_envelope_se_deniega_sin_ejecutarse(tmp_path):
    """El caso que PolicyEngine no cubría: la tool pide `fs.write`, el envelope no la da."""
    ejecutadas: list[str] = []

    async def _espia(args):  # pragma: no cover - no debe llegar a ejecutarse
        ejecutadas.append("ejecutada")
        return {"ok": True}

    tool = Tool(
        name="fs.write",
        description="escribe",
        capability_id="fs.write",
        permissions={"write": True},
        handler=_espia,
        schema={"type": "object"},
    )
    executor = _executor(tmp_path, [tool])
    mission = _mission(["fs.read"])  # envelope SÓLO lectura

    resultado = await executor.execute(mission, _step(capability="fs.write"))

    assert resultado.success is False
    assert "permiso denegado" in resultado.error
    assert "fs.write" in resultado.error
    assert ejecutadas == [], "la tool no debe ejecutarse si su permiso falta"


async def test_tool_con_permiso_dentro_del_envelope_se_ejecuta(tmp_path):
    tool = Tool(
        name="fs.write",
        description="escribe",
        capability_id="fs.write",
        permissions={"write": True},
        handler=lambda args: _ok(),
        schema={"type": "object"},
    )
    executor = _executor(tmp_path, [tool])
    mission = _mission(["fs.write", "fs.read"])

    resultado = await executor.execute(mission, _step(capability="fs.write"))

    assert resultado.success is True, resultado.error
    assert resultado.output == {"ok": True}


async def test_tool_sin_permissions_sigue_ejecutandose(tmp_path):
    """Sin declaración no hay requisito: es el caso de retrocompatibilidad."""
    tool = Tool(
        name="tool.sin.declarar",
        description="no declara nada",
        handler=lambda args: _ok(),
        schema={"type": "object"},
    )
    executor = _executor(tmp_path, [tool])
    mission = _mission([])  # envelope sin capabilities

    # `tool_name` explícito: sin capability el executor resolvería por acción.
    resultado = await executor.execute(
        mission, _step(capability=None), tool_name="tool.sin.declarar"
    )

    assert resultado.success is True, resultado.error


async def test_envelope_sin_capabilities_no_restringe(tmp_path):
    """Misma semántica que la policy: si el envelope no declara, no restringe."""
    tool = Tool(
        name="fs.write",
        description="escribe",
        capability_id="fs.write",
        permissions={"write": True},
        handler=lambda args: _ok(),
        schema={"type": "object"},
    )
    executor = _executor(tmp_path, [tool])
    resultado = await executor.execute(_mission([]), _step(capability="fs.write"))
    assert resultado.success is True, resultado.error


async def test_una_lectura_con_permiso_de_escritura_tambien_se_deniega(tmp_path):
    """`fs.read` declara `read`; exigir `fs.write` debe denegar igual."""
    tools = {t.name: t for t in build_filesystem_tools(tmp_path)}
    assert _required_capabilities(tools["fs.read"]) == ["fs.read"]
    # Y una tool de escritura en un envelope que sólo concede lectura, deniega.
    executor = _executor(tmp_path, [tools["fs.write"]])
    resultado = await executor.execute(_mission(["fs.read"]), _step(capability="fs.write"))
    assert resultado.success is False
    assert "permiso denegado" in resultado.error


@pytest.mark.parametrize("envelope_caps,espera_ok", [
    (["fs.write", "fs.read"], True),
    (["fs.read"], False),
    ([], True),           # sin declaración no restringe
    (["fs.stat"], False),
])
async def test_matriz_de_minimo_privilegio(tmp_path, envelope_caps, espera_ok):
    tool = Tool(
        name="fs.write",
        description="escribe",
        capability_id="fs.write",
        permissions={"write": True},
        handler=lambda args: _ok(),
        schema={"type": "object"},
    )
    executor = _executor(tmp_path, [tool])
    resultado = await executor.execute(_mission(envelope_caps), _step(capability="fs.write"))
    assert resultado.success is espera_ok, resultado.error


async def _ok():
    return {"ok": True}