"""P0 §5.4 — `execute.test`: tests REALES, evidencia REAL.

Nada de este archivo simula el resultado de una suite. Cada test crea un proyecto
temporal de verdad, con sus tests de verdad, y ejecuta pytest de verdad a través del
mismo `SandboxRunner` que usa el resto del sistema. El timeout se provoca con un test que
duerme de verdad; el fallo, con un test que falla de verdad.

Lo que se prueba aquí es la cadena entera:

    argv fijo → sandbox → Policy → Gate → ejecución → Observation → evidencia

y que un `execute.test` que pasa NO verifica el objetivo por sí solo: eso lo decide el
`GoalVerifier` de §5.3, que sigue siendo la autoridad.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.gates import AutonomyGate  # noqa: E402
from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.cognition.goal_verification import (  # noqa: E402
    CriterionStatus,
    GoalVerifier,
)
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    MissionEnvelope,
    Plan,
    PlanStep,
    RiskLevel,
)
from alexis.execution import SandboxExecutor  # noqa: E402
from alexis.security.policy import PolicyEngine  # noqa: E402
from alexis.security.sandbox import SandboxRunner  # noqa: E402
from alexis.tools.registry import ToolRegistry  # noqa: E402
from alexis.tools.testrunner import (  # noqa: E402
    ALLOWED_RUNNERS,
    FORBIDDEN_KEYS,
    TestRunnerTool,
    build_test_tools,
    parse_pytest_counts,
)
from alexis.world.model import WorldModel  # noqa: E402

ACTIONS = ["understand", "analyze", "research", "execute", "test", "verify", "modify", "respond"]


def _project(tmp_path, files: dict) -> pathlib.Path:
    """Proyecto de tests real en un directorio temporal."""
    root = tmp_path / "proyecto"
    root.mkdir(exist_ok=True)
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


PASSING = {
    "tests/__init__.py": "",
    "tests/test_ok.py": "def test_uno():\n    assert 1 + 1 == 2\n\n\ndef test_dos():\n    assert 'a' in 'abc'\n",
}

FAILING = {
    "tests/__init__.py": "",
    "tests/test_ko.py": "def test_pasa():\n    assert True\n\n\ndef test_falla():\n    assert 1 + 1 == 3\n",
}


def _tool(root, **over):
    return TestRunnerTool(root, **over)


def _executor(root):
    registry = ToolRegistry()
    registry.register_all(build_test_tools(root))
    return SandboxExecutor(tools=registry, sandbox=SandboxRunner(root))


def _mission(objective="ejecuta los tests", criteria=None, **over):
    data = dict(
        objective=objective,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=list(ACTIONS),
        capabilities=[],
    )
    data.update(over)
    return MissionEngine().create(objective, MissionEnvelope(**data), success_criteria=criteria)


def _test_step(**over):
    data = dict(
        id="correr-tests",
        description="ejecuta la suite de tests",
        action="test",
        risk=RiskLevel.MEDIUM,
        agent="executor",
        capability="execute.test",
        # `path` explícito: sin args, el executor cae al path legacy de filesystem
        # (README.txt), que no significa nada para una suite.
        args={"path": "."},
    )
    data.update(over)
    return PlanStep(**data)


# ----------------------------------------------------------------------
# 1, 3, 4, 5, 13, 14 — suite real que pasa
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suite_real_que_pasa(tmp_path):
    root = _project(tmp_path, PASSING)

    output = await _tool(root).handler({})

    assert output["ok"] is True
    assert output["exit_code"] == 0, output
    assert output["tests_passed"] == 2
    assert output["tests_failed"] == 0
    assert output["timed_out"] is False
    assert "2 passed" in output["stdout"]
    assert output["duration_ms"] >= 0
    assert output["command"][:3] == [sys.executable, "-m", "pytest"]
    assert output["cwd"] == str(root)


@pytest.mark.asyncio
async def test_exit_code_real_de_suite_que_falla(tmp_path):
    root = _project(tmp_path, FAILING)

    output = await _tool(root).handler({})

    assert output["ok"] is False
    assert output["exit_code"] == 1, output
    assert output["tests_passed"] == 1
    assert output["tests_failed"] == 1
    assert "1 failed" in output["stdout"]


@pytest.mark.asyncio
async def test_stderr_y_stdout_se_capturan(tmp_path):
    root = _project(tmp_path, FAILING)

    output = await _tool(root).handler({})

    assert isinstance(output["stdout"], str) and output["stdout"].strip()
    assert "test_falla" in output["stdout"], "el nombre del test fallido debe estar en la salida"
    assert isinstance(output["stderr"], str)


@pytest.mark.asyncio
async def test_la_salida_real_de_un_fallo_captura_el_traceback(tmp_path):
    """pytest con -q solo vuelca la salida del test cuando este falla.

    No se añaden flags para forzar `-s`: la evidencia es la que da pytest por defecto,
    y por eso el test que falla y el que pasa se distinguen solos.
    """
    root = _project(
        tmp_path,
        {
            "tests/test_fallo.py": (
                "import sys\n\n\ndef test_falla_con_mensaje():\n"
                "    print('AVISO REAL', file=sys.stderr)\n    assert False, 'FALLO CONCRETO'\n"
            )
        },
    )

    output = await _tool(root).handler({})

    assert output["ok"] is False
    assert "FALLO CONCRETO" in output["stdout"], "el traceback real debe estar en stdout"
    assert "test_falla_con_mensaje" in output["stdout"]


@pytest.mark.asyncio
async def test_seleccion_de_un_test_concreto(tmp_path):
    root = _project(
        tmp_path,
        {
            "tests/test_uno.py": "def test_a():\n    assert True\n",
            "tests/test_dos.py": "def test_b():\n    assert True\n",
        },
    )

    output = await _tool(root).handler({"selectors": ["tests/test_uno.py"]})

    assert output["ok"] is True
    assert output["tests_passed"] == 1
    assert output["selectors"] == ["tests/test_uno.py"]
    assert "test_dos" not in output["stdout"]


# ----------------------------------------------------------------------
# 2 — suite real que falla (cubierto arriba y aquí con el counts check)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conteos_de_una_suite_que_falla(tmp_path):
    root = _project(
        tmp_path,
        {
            "tests/test_ko.py": (
                "def test_a():\n    assert True\n\n\ndef test_b():\n    assert False\n\n\ndef test_c():\n    assert False\n"
            )
        },
    )

    output = await _tool(root).handler({})

    assert output["tests_passed"] == 1
    assert output["tests_failed"] == 2
    assert output["counts_parsed"] is True


# ----------------------------------------------------------------------
# 6 — timeout real
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_real_mata_la_suite(tmp_path):
    root = _project(
        tmp_path,
        {"tests/test_lento.py": "import time\n\n\ndef test_dormir():\n    time.sleep(30)\n"},
    )

    output = await _tool(root).handler({"timeout": 1.0})

    assert output["ok"] is False
    assert output["timed_out"] is True
    assert "timeout" in output["error"]
    assert output["duration_ms"] < 25_000, "no debería esperar a que el test acabe solo"


@pytest.mark.asyncio
async def test_el_timeout_se_acota_a_un_rango_sano(tmp_path):
    root = _project(tmp_path, PASSING)
    tool = _tool(root, default_timeout=99999)

    output = await tool.handler({"timeout": 100000})

    assert output["timeout"] <= 300.0


# ----------------------------------------------------------------------
# 7 — sandbox aplicado
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_se_puede_salir_del_workspace(tmp_path):
    root = _project(tmp_path, PASSING)

    output = await _tool(root).handler({"path": "../../etc"})

    assert output["ok"] is False
    assert output["rejected"] is True
    assert "fuera del workspace" in output["error"] or "path no permitido" in output["error"]


@pytest.mark.asyncio
async def test_cwd_siempre_dentro_del_workspace(tmp_path):
    root = _project(tmp_path, PASSING)

    output = await _tool(root).handler({})

    assert output["cwd"] == str(root.resolve())
    assert str(root.resolve()) in output["cwd"]


@pytest.mark.asyncio
async def test_path_absoluto_fuera_del_workspace_se_rechaza(tmp_path):
    root = _project(tmp_path, PASSING)

    output = await _tool(root).handler({"path": "/etc"})

    assert output["ok"] is False
    assert output["rejected"] is True


@pytest.mark.asyncio
async def test_el_sandbox_limita_la_salida(tmp_path):
    """El tope de salida del sandbox se respeta: no se traga una suite enorme."""
    root = _project(
        tmp_path,
        {
            "tests/test_noise.py": (
                "def test_ruido():\n    print('X' * 5000)\n    assert True\n"
            )
        },
    )
    runner = SandboxRunner(root, max_output=2048)

    output = await _tool(root, sandbox=runner).handler({})

    assert len(output["stdout"]) <= 2048


@pytest.mark.asyncio
async def test_los_procesos_hijos_heredan_los_limites(tmp_path):
    """El hijo corre con el env limpio del sandbox: sin credenciales de ALEXIS."""
    root = _project(
        tmp_path,
        {
            "tests/test_env.py": (
                "import os\n\n\ndef test_env_limpio():\n"
                "    assert 'ALEXIS_API_TOKEN' not in os.environ\n"
                "    assert 'ELEVENLABS_API_KEY' not in os.environ\n"
            )
        },
    )

    output = await _tool(root).handler({})

    assert output["ok"] is True, output
    assert output["tests_passed"] == 1


# ----------------------------------------------------------------------
# 8 — red: lo que el sandbox garantiza de verdad
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_la_tool_no_pide_credenciales_ni_proxy(tmp_path):
    """El sandbox no inyecta red ni secretos: el env del hijo es el mínimo.

    Límite honesto: `SandboxRunner` no puede bloquear la red sin namespaces con
    privilegios (lo dice su propio docstring). Lo que se comprueba aquí es que la
    ejecución no recibe credenciales ni proxy, y que la capability se declara sin red.
    """
    root = _project(tmp_path, PASSING)
    tool = _tool(root)

    assert tool.permissions["network"] is False
    assert tool.permissions["shell"] is False
    assert "http_proxy" not in str(tool.permissions)
    assert tool.sandbox_profile == "sandbox-project"

    output = await tool.handler({})
    assert output["ok"] is True


@pytest.mark.asyncio
async def test_la_tool_no_puede_inyectar_un_proxy(tmp_path):
    root = _project(tmp_path, PASSING)

    output = await _tool(root).handler({"env": {"http_proxy": "http://atacante:8080"}})

    assert output["ok"] is False
    assert output["rejected"] is True
    assert "env" in output["error"]


# ----------------------------------------------------------------------
# 9, 10, 14 — argumentos inválidos y shell injection
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_argumento_desconocido_se_rechaza(tmp_path):
    root = _project(tmp_path, PASSING)

    output = await _tool(root).handler({"verbose": True})

    assert output["ok"] is False
    assert output["rejected"] is True
    assert "verbose" in output["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(FORBIDDEN_KEYS))
async def test_claves_de_shell_siempre_rechazadas(tmp_path, key):
    root = _project(tmp_path, PASSING)

    output = await _tool(root).handler({key: "rm -rf /"})

    assert output["ok"] is False
    assert output["rejected"] is True
    assert key in output["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selector",
    [
        "tests/test_ok.py; rm -rf /",
        "tests/test_ok.py && curl http://x",
        "tests/test_ok.py | tee /etc/passwd",
        "tests/test_ok.py$(whoami)",
        "tests/test_ok.py`id`",
        "../../etc/passwd.py",
        "tests/test_ok.py > /tmp/escape",
    ],
)
async def test_shell_injection_en_el_selector_rechazada(tmp_path, selector):
    root = _project(tmp_path, PASSING)

    output = await _tool(root).handler({"selectors": [selector]})

    assert output["ok"] is False
    assert output["rejected"] is True, f"selector_permitido indebidamente: {selector!r}"


@pytest.mark.asyncio
async def test_el_argv_no_puede_contener_un_shell(tmp_path):
    """El argv se construye en código: los args del modelo solo ocupan posiciones.fija."""
    root = _project(tmp_path, PASSING)
    tool = _tool(root)

    output = await tool.handler({"selectors": ["tests/test_ok.py"]})
    argv = output["command"]

    assert not any(token in ("sh", "-c", "bash", "/bin/sh") for token in argv)
    assert argv[1:3] == ["-m", "pytest"]
    assert argv[-1] == "tests/test_ok.py"


@pytest.mark.asyncio
async def test_runner_fuera_de_la_lista_blanca_rechazado(tmp_path):
    root = _project(tmp_path, PASSING)

    output = await _tool(root).handler({"runner": "unittest"})

    assert output["ok"] is False
    assert output["rejected"] is True
    assert ALLOWED_RUNNERS == ("pytest",)


@pytest.mark.asyncio
async def test_timeout_no_numerico_rechazado(tmp_path):
    root = _project(tmp_path, PASSING)

    output = await _tool(root).handler({"timeout": "10; whoami"})

    assert output["ok"] is False
    assert output["rejected"] is True


@pytest.mark.asyncio
async def test_selectors_deben_ser_lista(tmp_path):
    root = _project(tmp_path, PASSING)

    output = await _tool(root).handler({"selectors": "tests/test_ok.py"})

    assert output["ok"] is False
    assert output["rejected"] is True


# ----------------------------------------------------------------------
# 11, 12 — Policy y Gate
# ----------------------------------------------------------------------


def test_policy_bloquea_execute_test_fuera_del_envelope(tmp_path):
    _project(tmp_path, PASSING)
    mission = _mission(allowed_actions=["understand", "analyze"])
    decision = PolicyEngine().evaluate(mission, _test_step())

    assert decision.allowed is False
    assert decision.matched_rule == "action.outside_envelope"


def test_policy_exige_aprobacion_para_riesgo_alto(tmp_path):
    _project(tmp_path, PASSING)
    mission = _mission()
    decision = PolicyEngine().evaluate(mission, _test_step(risk=RiskLevel.HIGH))

    assert decision.requires_approval is True
    assert decision.matched_rule == "risk.requires_approval"


def test_policy_permite_riesgo_medio_dentro_del_envelope(tmp_path):
    _project(tmp_path, PASSING)
    mission = _mission()
    decision = PolicyEngine().evaluate(mission, _test_step(risk=RiskLevel.MEDIUM))

    assert decision.allowed is True
    assert decision.requires_approval is False


def test_el_gate_aplica_la_aprobacion_de_la_policy(tmp_path):
    _project(tmp_path, PASSING)
    mission = _mission()
    step = _test_step(risk=RiskLevel.HIGH)
    policy = PolicyEngine()

    pending = AutonomyGate().decide(mission, step, policy)
    assert pending.requires_approval is True
    assert pending.matched_rule == "risk.requires_approval"

    ok = AutonomyGate().decide(mission, _test_step(risk=RiskLevel.MEDIUM), policy)
    assert ok.allowed is True


@pytest.mark.asyncio
async def test_la_policy_impide_ejecutar_por_la_via_cognitiva(tmp_path):
    """Policy + Gate cortan ANTES de la ejecución: la suite ni se lanza.

    `SandboxExecutor` es la capa baja y no conoce la Policy; la autoridad vive en el
    CognitiveRuntime (gate + policy antes de `_run`). La prueba de que la tool no llegó a
    ejecutarse es que el WorldModel no registra ninguna suite observada.
    """
    from alexis.cognition.loop import CognitiveRuntime
    from alexis.cognition.state import Verdict

    root = _project(tmp_path, FAILING)
    mission = _mission("ejecuta los tests", allowed_actions=["understand", "analyze"])
    executor = _executor(root)
    world = WorldModel()
    cognitive = CognitiveRuntime(
        policy=PolicyEngine(),
        gate=AutonomyGate(),
        executor=executor,
        verifier=None,
        world=world,
    )
    knowledge = cognitive.knowledge_for(mission)
    step = _test_step()
    plan = Plan("m", [step])

    outcome = await cognitive.step(mission, knowledge, pending_steps=[step], plan=plan)

    assert outcome.verdict is Verdict.BLOCKED
    assert "fuera del envelope" in (outcome.error or "")
    assert world.query(kind="test") == [], "la suite no debía ejecutarse"


@pytest.mark.asyncio
async def test_un_paso_de_test_de_riesgo_alto_queda_esperando_aprobacion(tmp_path):
    """Riesgo alto: el gate no lo ejecuta solo, aunque la policy lo permita."""
    from alexis.cognition.loop import CognitiveRuntime

    root = _project(tmp_path, PASSING)
    mission = _mission("ejecuta los tests")
    executor = _executor(root)
    world = WorldModel()
    cognitive = CognitiveRuntime(
        policy=PolicyEngine(),
        gate=AutonomyGate(),
        executor=executor,
        verifier=None,
        world=world,
    )
    knowledge = cognitive.knowledge_for(mission)
    step = _test_step(risk=RiskLevel.HIGH)
    plan = Plan("m", [step])

    outcome = await cognitive.step(mission, knowledge, pending_steps=[step], plan=plan)

    assert outcome.requires_approval is True
    assert outcome.mission_state is not None and outcome.mission_state.value == "waiting_approval"
    assert world.query(kind="test") == []


# ----------------------------------------------------------------------
# 13, 14 — Observation y evidencia por la vía real del executor
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observation_estructurada_con_la_evidencia(tmp_path):
    root = _project(tmp_path, PASSING)
    mission = _mission()
    executor = _executor(root)

    result = await executor.execute(mission, _test_step(), tool_name="execute.test")

    assert result.success is True
    assert result.output["tests_passed"] == 2
    assert result.observations, "una ejecución debe producir observación"
    observation = result.observations[0]
    assert observation.trusted is True
    assert observation.source == "tool.execute.test"
    data = observation.content
    for key in (
        "command",
        "cwd",
        "exit_code",
        "stdout",
        "stderr",
        "duration_ms",
        "timed_out",
        "tests_passed",
        "tests_failed",
    ):
        assert key in data, f"falta {key} en la observación"


@pytest.mark.asyncio
async def test_el_executor_registra_los_args_en_la_observacion(tmp_path):
    root = _project(tmp_path, PASSING)
    executor = _executor(root)

    result = await executor.execute(
        _mission(),
        _test_step(args={"selectors": ["tests/test_ok.py"]}),
        tool_name="execute.test",
    )

    assert result.observations[0].content["args"] == {"selectors": ["tests/test_ok.py"]}


# ----------------------------------------------------------------------
# 18 — no false success: la suite no verifica el objetivo por sí sola
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suite_que_pasa_no_verifica_el_objetivo_sin_observerla(tmp_path):
    """Tool exitosa, objetivo sin verificar: la separación sigue en pie."""
    root = _project(tmp_path, PASSING)
    mission = _mission(criteria=["tests_passing:los tests del proyecto pasan"])
    executor = _executor(root)

    result = await executor.execute(mission, _test_step(), tool_name="execute.test")
    assert result.success is True

    verification = GoalVerifier(world=WorldModel()).verify(mission)

    assert verification.verified is False
    assert verification.evaluations[0].status is CriterionStatus.INSUFFICIENT_EVIDENCE
    assert "nadie ha observado" in verification.evaluations[0].reason


@pytest.mark.asyncio
async def test_verificar_el_criterio_de_tests_tras_observar_la_suite(tmp_path):
    """Con la suite observada, el criterio se puede evaluar con evidencia real."""
    root = _project(tmp_path, PASSING)
    mission = _mission(criteria=["tests_passing:los tests del proyecto pasan"])
    executor = _executor(root)
    world = WorldModel()
    step = _test_step()

    result = await executor.execute(mission, step, tool_name="execute.test")
    world.observe_execution(step, result, mission)

    verification = GoalVerifier(world=world).verify(mission)

    assert verification.verified is True
    evaluation = verification.evaluations[0]
    assert evaluation.status is CriterionStatus.SATISFIED
    assert evaluation.evidence[0].trusted is True
    assert evaluation.evidence[0].source.startswith("tool:")


@pytest.mark.asyncio
async def test_suite_que_falla_da_criterio_no_cumplido(tmp_path):
    root = _project(tmp_path, FAILING)
    mission = _mission(criteria=["tests_passing:los tests del proyecto pasan"])
    executor = _executor(root)
    world = WorldModel()
    step = _test_step()

    result = await executor.execute(mission, step, tool_name="execute.test")
    world.observe_execution(step, result, mission)

    verification = GoalVerifier(world=world).verify(mission)

    assert verification.verified is False
    assert verification.evaluations[0].status is CriterionStatus.NOT_SATISFIED
    assert "falló" in verification.evaluations[0].reason


@pytest.mark.asyncio
async def test_el_world_model_registra_la_suite_como_hecho(tmp_path):
    root = _project(tmp_path, PASSING)
    world = WorldModel()
    executor = _executor(root)
    step = _test_step()
    mission = _mission()

    result = await executor.execute(mission, step, tool_name="execute.test")
    observed = world.observe_execution(step, result, mission)

    assert observed, "la suite ejecutada debe quedar en el mundo"
    entity = observed[0]
    assert entity.kind == "test"
    assert entity.attributes["tests_passed"] == 2
    assert entity.source == "tool:execute.test"


# ----------------------------------------------------------------------
# Auditoría y contrato
# ----------------------------------------------------------------------


def test_el_registro_de_tools_expone_la_capability(tmp_path):
    root = _project(tmp_path, PASSING)
    registry = ToolRegistry()
    registry.register_all(build_test_tools(root))

    assert registry.has("execute.test") is True
    tool = registry.get("execute.test")
    assert tool.capability_id == "execute.test"
    assert tool.risk == "medium"
    assert tool.schema["additionalProperties"] is False


def test_el_catalogo_declara_esta_capability_sin_red():
    from alexis.capabilities.catalog import build_catalog

    spec = next(c for c in build_catalog().specs() if c.id == "execute.test")

    assert spec.network is False
    assert spec.plans_action == "test"
    assert spec.sandbox_profile == "sandbox-project"


def test_parse_pytest_counts_no_inventa_ceros():
    assert parse_pytest_counts("3 passed, 1 failed in 0.10s") == {"passed": 3, "failed": 1}
    assert parse_pytest_counts("") == {}
    assert parse_pytest_counts("salida sin resumen") == {}


def test_el_plan_deja_constancia_del_comando(tmp_path):
    _project(tmp_path, PASSING)
    plan = Plan("m", [_test_step(args={"path": "tests"})])

    assert plan.steps[0].capability == "execute.test"
    assert plan.steps[0].args == {"path": "tests"}
