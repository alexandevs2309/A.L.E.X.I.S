"""P0 §5.3 — GoalVerifier: el objetivo se verifica con evidencia por criterio.

Estos tests atacan la mentira que §4 del audit documenta: que `ACTION SUCCESS` se
convierta en `OBJECTIVE SUCCESS`. La cadena que se exige es

    criterio → evidencia → evaluación → resultado

y ninguna evidencia es inventada: lo que sostiene un `satisfied` es una observación
real de una herramienta de filesystem sobre el workspace temporal del test.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.cognition.contracts import ClaimKind  # noqa: E402
from alexis.cognition.evidence import ClaimGuard, EvidenceStore  # noqa: E402
from alexis.cognition.state import Claim  # noqa: E402
from alexis.cognition.goal_verification import (  # noqa: E402
    CriterionStatus,
    GoalVerification,
    GoalVerifier,
    parse_predicate,
)
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    MissionEnvelope,
    PlanStep,
    RiskLevel,
)
from alexis.execution import SandboxExecutor  # noqa: E402
from alexis.security.sandbox import SandboxRunner  # noqa: E402
from alexis.storage.serialization import mission_from_row, mission_to_row  # noqa: E402
from alexis.tools.filesystem import build_filesystem_tools  # noqa: E402
from alexis.tools.registry import ToolRegistry  # noqa: E402
from alexis.world.model import WorldModel  # noqa: E402

ACTIONS = ["understand", "analyze", "research", "execute", "verify", "modify", "respond"]


def _mission(objective, criteria=None, **over):
    data = dict(
        objective=objective,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=list(ACTIONS),
        capabilities=[],
    )
    data.update(over)
    return MissionEngine().create(
        objective,
        MissionEnvelope(**data),
        success_criteria=criteria,
    )


def _real_executor(tmp_path):
    """Ejecutor con las herramientas de filesystem REALES sobre el workspace del test."""
    registry = ToolRegistry()
    registry.register_all(build_filesystem_tools(tmp_path))
    return SandboxExecutor(tools=registry, sandbox=SandboxRunner(workspace=tmp_path))


def _stat_step(path, step_id="mirar"):
    return PlanStep(
        step_id,
        f"comprobar {path}",
        "research",
        RiskLevel.LOW,
        "executor",
        capability="fs.stat",
        args={"path": path},
    )


async def _observe(executor, world, mission, step):
    """Ejecuta una tool real y mete su observación en el WorldModel."""
    result = await executor.execute(mission, step, tool_name="fs.stat")
    world.observe_execution(step, result, mission)
    return result


# ----------------------------------------------------------------------
# 1. Criterio satisfecho con evidencia válida (observación real)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_criterio_satisfecho_con_observacion_real(tmp_path):
    (tmp_path / "informe.md").write_text("contenido real del informe", encoding="utf-8")
    mission = _mission(
        "redacta el informe",
        ["El archivo file_exists:informe.md está escrito"],
    )
    world = WorldModel()
    executor = _real_executor(tmp_path)

    result = await _observe(executor, world, mission, _stat_step("informe.md"))
    assert result.success is True

    verification = GoalVerifier(world=world).verify(mission)

    assert verification.verified is True
    assert len(verification.evaluations) == 1
    evaluation = verification.evaluations[0]
    assert evaluation.status is CriterionStatus.SATISFIED
    assert evaluation.predicate == "file_exists"
    trusted = [e for e in evaluation.evidence if e.trusted]
    assert trusted, "un satisfied sin evidencia de confianza no puede sostenerse"
    assert trusted[0].source.startswith("tool:")


# ----------------------------------------------------------------------
# 2. Criterio no satisfecho
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_criterio_no_satisfecho_tras_observar_que_no_existe(tmp_path):
    mission = _mission("redacta el informe", ["El archivo file_exists:informe.md está escrito"])
    world = WorldModel()
    executor = _real_executor(tmp_path)

    await _observe(executor, world, mission, _stat_step("informe.md"))

    verification = GoalVerifier(world=world).verify(mission)

    assert verification.verified is False
    assert verification.evaluations[0].status is CriterionStatus.NOT_SATISFIED
    assert "no cumplido" in verification.evaluations[0].reason
    assert verification.unsatisfied()


@pytest.mark.asyncio
async def test_file_missing_se_satisface_cuando_realmente_no_existe(tmp_path):
    mission = _mission("limpia el workspace", ["El archivo file_missing:basura.txt no está"])
    world = WorldModel()
    executor = _real_executor(tmp_path)

    await _observe(executor, world, mission, _stat_step("basura.txt"))

    verification = GoalVerifier(world=world).verify(mission)

    assert verification.verified is True
    assert verification.evaluations[0].status is CriterionStatus.SATISFIED


@pytest.mark.asyncio
async def test_criterio_de_tamano_se_cumple_y_se_incumple(tmp_path):
    (tmp_path / "datos.txt").write_text("x" * 50, encoding="utf-8")
    world = WorldModel()
    executor = _real_executor(tmp_path)

    mission_ok = _mission("crea el archivo", ["El archivo file_size_at_least:datos.txt:50 tiene contenido"])
    await _observe(executor, world, mission_ok, _stat_step("datos.txt"))
    ok = GoalVerifier(world=world).verify(mission_ok)
    assert ok.verified is True
    assert ok.evaluations[0].status is CriterionStatus.SATISFIED

    mission_ko = _mission("crea el archivo", ["El archivo file_size_at_least:datos.txt:5000 tiene contenido"])
    ko = GoalVerifier(world=world).verify(mission_ko)
    assert ko.verified is False
    assert ko.evaluations[0].status is CriterionStatus.NOT_SATISFIED
    assert "5000" in ko.evaluations[0].reason


# ----------------------------------------------------------------------
# 3. Criterio sin evidencia suficiente
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_criterio_sin_observar_queda_sin_evidencia_suficiente(tmp_path):
    mission = _mission("investiga", ["El archivo file_exists:secreto.txt existe"])
    world = WorldModel()

    verification = GoalVerifier(world=world).verify(mission)

    assert verification.verified is False
    assert verification.evaluations[0].status is CriterionStatus.INSUFFICIENT_EVIDENCE
    assert "nadie ha observado" in verification.evaluations[0].reason


def test_criterio_sin_checker_no_se_da_por_cumplido():
    """Un criterio en lenguaje natural sin predicado observable no se puede comprobar."""
    mission = _mission("arregla el proyecto", ["Los tests del proyecto pasan"])

    verification = GoalVerifier(world=WorldModel()).verify(mission)

    assert verification.verified is False
    assert verification.evaluations[0].status is CriterionStatus.INSUFFICIENT_EVIDENCE
    assert verification.evaluations[0].predicate is None
    assert "no hay checker" in verification.evaluations[0].reason


def test_sin_criterios_no_se_puede_declarar_verificado():
    """GAP-P1 deja el Goal vacío cuando el modelo está degradado: eso no es verificación."""
    mission = _mission("haz algo útil", [])

    verification = GoalVerifier(world=WorldModel()).verify(mission)

    assert verification.verified is False
    assert verification.evaluations == []
    assert "no tiene criterios" in verification.reason


def test_entidad_declarada_no_cuenta_como_evidencia():
    """Lo declarado por el registro de capabilities no es una observación."""
    from alexis.world.model import FILE, WorldEntity

    world = WorldModel()
    world.upsert(
        WorldEntity(
            id=f"{FILE}:informe.md",
            kind=FILE,
            name="informe.md",
            attributes={"exists": True},
            source="declared",
        )
    )

    verification = GoalVerifier(world=world).verify(
        _mission("obj", ["El archivo file_exists:informe.md existe"])
    )

    assert verification.verified is False
    assert verification.evaluations[0].status is CriterionStatus.INSUFFICIENT_EVIDENCE


# ----------------------------------------------------------------------
# 4. Múltiples criterios con resultados distintos
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiples_criterios_con_resultados_distintos(tmp_path):
    (tmp_path / "informe.md").write_text("hola", encoding="utf-8")
    mission = _mission(
        "deja el workspace en orden",
        [
            "El archivo file_exists:informe.md existe",
            "El archivo file_exists:pendiente.md existe",
            "Los tests del proyecto pasan",
        ],
    )
    world = WorldModel()
    executor = _real_executor(tmp_path)
    await _observe(executor, world, mission, _stat_step("informe.md"))
    await _observe(executor, world, mission, _stat_step("pendiente.md"))

    verification = GoalVerifier(world=world).verify(mission)

    statuses = [e.status for e in verification.evaluations]
    assert statuses == [
        CriterionStatus.SATISFIED,
        CriterionStatus.NOT_SATISFIED,
        CriterionStatus.INSUFFICIENT_EVIDENCE,
    ]
    assert verification.verified is False
    assert verification.counts()["satisfied"] == 1
    assert verification.counts()["not_satisfied"] == 1
    assert verification.counts()["insufficient_evidence"] == 1
    assert "no cumple" in verification.reason or "NO verificado" in verification.reason


# ----------------------------------------------------------------------
# 5 y 6. Observación real vs. tool exitosa que NO prueba el objetivo
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_una_tool_exitosa_que_no_prueba_el_objetivo(tmp_path):
    """Leemos un archivo real con éxito: eso prueba la acción, no el objetivo."""
    (tmp_path / "notas.txt").write_text("contenido", encoding="utf-8")
    mission = _mission(
        "lee las notas y arregla el bug de normalización",
        ["Los tests del proyecto pasan", "El bug de normalización está corregido"],
    )
    world = WorldModel()
    executor = _real_executor(tmp_path)
    result = await _observe(executor, world, mission, _stat_step("notas.txt"))
    assert result.success is True

    verification = GoalVerifier(world=world).verify(mission)

    assert verification.verified is False, "una tool exitosa no verifica el objetivo"
    assert all(e.status is CriterionStatus.INSUFFICIENT_EVIDENCE for e in verification.evaluations)
    assert verification.reason == (
        "objetivo NO verificado: 2 criterio/s sin evidencia suficiente"
    )


@pytest.mark.asyncio
async def test_la_verificacion_no_recibe_el_resultado_de_la_tool(tmp_path):
    """Separación estructural: verify() no admite un ExecutionResult."""
    import inspect

    params = list(inspect.signature(GoalVerifier.verify).parameters)

    assert params == ["self", "mission", "criteria"]


# ----------------------------------------------------------------------
# 7. Un claim del modelo no es evidencia verificada
# ----------------------------------------------------------------------


def test_un_claim_del_modelo_no_satisface_un_criterio():
    guard = ClaimGuard()
    store = EvidenceStore(guard=guard)
    store.add(
            Claim(
                id="c1",
                kind=ClaimKind.INFERENCE,
                text="El archivo file_exists:informe.md está escrito, ya lo he comprobado",
                source="model",
            )
    )
    mission = _mission("obj", ["El archivo file_exists:informe.md está escrito"])

    verification = GoalVerifier(world=WorldModel(), evidence=store).verify(mission)

    assert verification.verified is False
    evaluation = verification.evaluations[0]
    assert evaluation.status is CriterionStatus.INSUFFICIENT_EVIDENCE
    assert evaluation.evidence, "el claim debe quedar registrado aunque no cuente"
    assert all(e.trusted is False for e in evaluation.evidence)
    assert "un claim no es evidencia" in evaluation.reason


def test_un_fact_sonante_tampoco_sustituye_a_la_observacion():
    """Aunque un claim llegue a FACT, sin observación del criterio sigue sin probarlo."""
    guard = ClaimGuard()
    store = EvidenceStore(guard=guard)
    store.add(
            Claim(
                id="c1",
                kind=ClaimKind.FACT,
                text="El archivo file_exists:informe.md existe",
                source="verification",
                evidence_ids=["e1"],
                verified=True,
            )
    )
    mission = _mission("obj", ["El archivo file_exists:informe.md existe"])

    verification = GoalVerifier(world=WorldModel(), evidence=store).verify(mission)

    assert verification.verified is False
    assert verification.evaluations[0].status is CriterionStatus.INSUFFICIENT_EVIDENCE


@pytest.mark.asyncio
async def test_claim_junto_a_observacion_no_invalida_la_evidencia(tmp_path):
    (tmp_path / "informe.md").write_text("x", encoding="utf-8")
    guard = ClaimGuard()
    store = EvidenceStore(guard=guard)
    store.add(
            Claim(
                id="c1",
                kind=ClaimKind.INFERENCE,
                text="El archivo file_exists:informe.md está escrito, ya lo he comprobado",
                source="model",
            )
    )
    mission = _mission("obj", ["El archivo file_exists:informe.md está escrito"])
    world = WorldModel()
    await _observe(_real_executor(tmp_path), world, mission, _stat_step("informe.md"))

    evaluation = GoalVerifier(world=world, evidence=store).verify(mission).evaluations[0]

    assert evaluation.status is CriterionStatus.SATISFIED
    assert any(e.trusted for e in evaluation.evidence)
    assert any(not e.trusted for e in evaluation.evidence)


# ----------------------------------------------------------------------
# 8. Persistencia / serialización
# ----------------------------------------------------------------------


def test_la_verificacion_se_serializa_y_vuelve(tmp_path):
    mission = _mission("obj", ["El archivo file_exists:informe.md existe", "Los tests pasan"])
    verification = GoalVerifier(world=WorldModel()).verify(mission)

    payload = verification.to_dict()
    reloaded = GoalVerification.from_dict(payload)

    assert reloaded.verified is False
    assert len(reloaded.evaluations) == 2
    assert reloaded.objective == verification.objective
    assert [e.status for e in reloaded.evaluations] == [e.status for e in reloaded.evaluations]
    assert reloaded.reason == verification.reason


def test_la_verificacion_sobrevive_a_un_reinicio(tmp_path):
    mission = _mission("obj", ["El archivo file_exists:informe.md existe"])
    mission.context["goal_verification"] = GoalVerifier(world=WorldModel()).verify(mission).to_dict()
    row = mission_to_row(mission)
    row["state"] = "running"
    row["results"] = "[]"

    reloaded = mission_from_row(row)

    stored = GoalVerification.from_dict(reloaded.context.get("goal_verification"))
    assert stored is not None
    assert stored.verified is False
    assert stored.evaluations[0].criterion == "El archivo file_exists:informe.md existe"


def test_estado_desconocido_al_releer_no_se_convierte_en_verificado():
    raw = {
        "objective": "obj",
        "verified": True,
        "reason": "",
        "criteria": [{"criterion": "x", "status": "verificado_mágico", "reason": ""}],
    }

    reloaded = GoalVerification.from_dict(raw)

    assert reloaded.evaluations[0].status is CriterionStatus.INSUFFICIENT_EVIDENCE


# ----------------------------------------------------------------------
# 9. No false success
# ----------------------------------------------------------------------


def test_verified_nunca_es_true_si_un_criterio_no_lo_esta(tmp_path):
    mission = _mission(
        "obj",
        ["El archivo file_exists:a.md existe", "El archivo file_exists:b.md existe"],
    )
    world = WorldModel()

    verification = GoalVerifier(world=world).verify(mission)

    assert verification.verified is False


def test_un_satisfied_sin_evidencia_fiable_no_verifica():
    """Contrato interno: la guarda exige evidencia de confianza por criterio."""
    from alexis.cognition.goal_verification import CriterionEvaluation

    evaluation = CriterionEvaluation(
        criterion="c",
        status=CriterionStatus.SATISFIED,
        reason="lo dice el modelo",
    )

    assert GoalVerifier._is_verified([evaluation]) is False


def test_parse_predicate_no_inventa_predicados():
    assert parse_predicate("Los tests del proyecto pasan") is None
    assert parse_predicate("El archivo file_exists:notas.txt está escrito") == (
        "file_exists",
        ["notas.txt"],
    )
    assert parse_predicate("file_size_at_least:datos.txt:100") == (
        "file_size_at_least",
        ["datos.txt", "100"],
    )


# ----------------------------------------------------------------------
# 10. Integración con el runtime (sin tocar el estado final: eso es §5.5)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_el_runtime_registra_la_verificacion_sin_tocar_el_estado(tmp_path):
    from alexis.cognition.loop import CognitiveRuntime

    (tmp_path / "informe.md").write_text("x", encoding="utf-8")
    mission = _mission("obj", ["El archivo file_exists:informe.md existe"])
    world = WorldModel()
    runtime = CognitiveRuntime(
        policy=None,
        verifier=None,
        world=world,
        goal_verifier=GoalVerifier(world=world),
    )
    await _observe(_real_executor(tmp_path), world, mission, _stat_step("informe.md"))

    verification = runtime.verify_goal(mission)

    assert verification.verified is True
    assert mission.context["goal_verification"]["verified"] is True
    assert mission.state.value == "pending", "§5.5 es quien decidirá el estado final"


@pytest.mark.asyncio
async def test_sin_goal_verifier_no_pasa_nada(tmp_path):
    from alexis.cognition.loop import CognitiveRuntime

    mission = _mission("obj", ["El archivo file_exists:informe.md existe"])
    runtime = CognitiveRuntime(policy=None, verifier=None, world=WorldModel())

    assert runtime.verify_goal(mission) is None
    assert "goal_verification" not in mission.context


@pytest.mark.asyncio
async def test_verificar_no_consume_criterios_para_other_evaluations(tmp_path):
    """`criteria` explícito: se puede evaluar un subconjunto sin tocar el Goal."""
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    mission = _mission("obj", ["El archivo file_exists:a.md existe", "Los tests pasan"])
    world = WorldModel()
    await _observe(_real_executor(tmp_path), world, mission, _stat_step("a.md"))

    full = GoalVerifier(world=world).verify(mission)
    subset = GoalVerifier(world=world).verify(mission, criteria=[mission.goal.success_criteria[0]])

    assert full.verified is False
    assert subset.verified is True
    assert len(subset.evaluations) == 1
