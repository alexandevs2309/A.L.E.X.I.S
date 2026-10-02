"""CORE-08B.1 — El riesgo de un paso NO se deduce: se exige.

La causa raíz del E2E real de CORE-08B: `_plan_schema` pedía `risk` como campo OPCIONAL, así que
el modelo lo omitía (3 de 3 respuestas reales vinieron sin `risk`), y `parse()` traducía esa
ausencia a LOW con `raw.get("risk") or "low"`. Un `fs.write` —que es MEDIUM— llegaba al
validador declarado LOW y lo rechazaba por "riesgo declarado insuficiente".

El rechazo era correcto; la causa no. ALEXIS se estaba inventando el dato que luego fiscalizaba.
La diferencia importa: "el modelo dijo LOW y era mentira" y "el modelo no dijo nada" son fallos
distintos, y sólo el segundo es arreglable exigiendo el campo.

Esta corrección NO deriva el riesgo desde `CapabilitySpec.default_risk`. Derivarlo convertiría
al catálogo en quien decide el riesgo de cada acción, y el modelo podría bajar el de una
capability sin que nadie lo note. Aquí la ausencia es un motivo explícito de rechazo.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.capabilities import build_catalog  # noqa: E402
from alexis.cognition.planner_model import ModelPlanner, PlanValidator, _plan_schema  # noqa: E402
from alexis.contracts import AutonomyLevel, MissionEnvelope  # noqa: E402

ACTIONS = ["understand", "analyze", "research", "execute", "verify", "modify", "test", "respond"]


def _mission(capabilities=None):
    data = dict(objective="crea el archivo informe.txt", autonomy=AutonomyLevel.SUPERVISED,
                allowed_actions=list(ACTIONS))
    if capabilities is not None:
        data["capabilities"] = capabilities
    return MissionEngine().create(data["objective"], MissionEnvelope(**data))


def _steps(**over):
    step = {"id": "paso", "description": "d", "action": "research", "capability": "fs.read",
            "risk": "low"}
    step.update(over)
    return step


def _propose(**over):
    return ModelPlanner(router=None).parse(_mission(), {"steps": [_steps(**over)]})


# ----------------------------------------------------------------------
# 1) El schema pide lo que el validador va a exigir
# ----------------------------------------------------------------------


def test_01_el_schema_exige_risk():
    """El contrato que se LEE tiene que reflejar el que se EXIGE.

    Con `risk` opcional, un modelo que cumple el schema puede omitirlo, y entonces ALEXIS tiene
    que inventarlo. El schema es la primera línea de defensa, no una sugerencia.
    """
    required = _plan_schema(6)["properties"]["steps"]["items"]["required"]

    assert "risk" in required
    assert set(required) == {"id", "action", "capability", "description", "risk"}


def test_02_risk_sigue_declarando_los_cuatro_valores():
    """Obligatorio no significa libre: los valores siguen siendo los del vocabulario."""
    enum = _plan_schema(6)["properties"]["steps"]["items"]["properties"]["risk"]["enum"]

    assert enum == ["low", "medium", "high", "critical"]


# ----------------------------------------------------------------------
# 2) La ausencia de risk es un rechazo explícito, no un LOW
# ----------------------------------------------------------------------


def test_03_plan_sin_risk_es_rechazado():
    """El caso que mostró la medición real: el modelo omitió el campo."""
    proposal = _propose()
    proposal.data = None
    result = ModelPlanner(router=None).parse(
        _mission(), {"steps": [{"id": "p", "description": "d", "action": "research",
                               "capability": "fs.read"}]}
    )

    assert result.plan is None
    assert result.reasons
    assert any("no declara 'risk'" in r for r in result.reasons)


def test_04_la_ausencia_explica_que_asumir_low_seria_mentir():
    """El motivo tiene que decir POR QUÉ no se deduce, o alguien lo 'arregla' con un default."""
    result = ModelPlanner(router=None).parse(
        _mission(), {"steps": [{"id": "p", "description": "d", "action": "research",
                               "capability": "fs.read"}]}
    )

    joined = " ".join(result.reasons)
    assert "LOW" in joined
    assert "mentir" in joined or "inventar" in joined or "suponer" in joined


def test_05_risk_vacio_o_blanco_tambien_cuenta_como_ausente():
    """`"risk": ""` y `"risk": "   "` son ausencia, no un valor: el default `or` los habría
    convertible en LOW igual que la clave totalmente ausente."""
    for valor in ("", "   ", None):
        result = ModelPlanner(router=None).parse(
            _mission(), {"steps": [{"id": "p", "description": "d", "action": "research",
                                   "capability": "fs.read", "risk": valor}]}
        )
        assert result.plan is None, f"risk={valor!r} debió rechazarse"
        assert any("no declara 'risk'" in r for r in result.reasons)


def test_06_un_paso_ilegible_invalida_el_plan_entero():
    """No se devuelve "el resto de los pasos": se rechaza todo.

    Aceptar los pasos válidos de un plan que el modelo no respalda entero sería ejecutar parte
    de una estrategia a la que le falta información. Peor que no proponer nada.
    """
    result = ModelPlanner(router=None).parse(_mission(), {"steps": [
        {"id": "bueno", "description": "d", "action": "research", "capability": "fs.read", "risk": "low"},
        {"id": "malo", "description": "d", "action": "research", "capability": "fs.stat"},
    ]})

    assert result.plan is None
    assert any("no declara 'risk'" in r for r in result.reasons)


def test_07_todos_los_pasos_sin_risk_no_deja_un_plan_vacio_pero_invalido():
    """Todos ilegibles: el mensaje dice que no es utilizable, no que no propuso nada."""
    result = ModelPlanner(router=None).parse(_mission(), {"steps": [
        {"id": "a", "description": "d", "action": "research", "capability": "fs.read"},
        {"id": "b", "description": "d", "action": "research", "capability": "fs.stat"},
    ]})

    assert result.plan is None
    assert result.reasons
    assert all("no declara 'risk'" in r for r in result.reasons)


# ----------------------------------------------------------------------
# 3) Las garantías del validador siguen intactas
# ----------------------------------------------------------------------


def test_08_capability_inexistente_sigue_rechazada():
    """CORE-08B.1 no afloja nada: una capability fuera del catálogo se sigue rechazando.

    Es el segundo hallazgo de la medición real: el modelo propuso `capability="respond"`, que
    no existe (responder es `tts.speak`).

    `parse()` sólo traduce la forma; la existencia de la capability es del `PlanValidator`. Por
    eso aquí se propone con riesgo bien declarado y se valida: si el campo obligatorio hiciera
    que el plan se rechazara antes, el test mediría otra cosa.
    """
    for capability in ("respond", "git.magic_super_read"):
        proposal = _propose(capability=capability, action="respond" if capability == "respond" else "research")
        assert proposal.ok, f"{capability}: el riesgo está bien declarado, el rechazo debe venir del validador"

        reasons = PlanValidator(catalog=build_catalog()).validate(_mission(), proposal.plan)

        assert any("capability inexistente" in r for r in reasons), \
            f"{capability} debió rechazarse por no existir en el catálogo"


def test_09_risk_low_para_una_capability_medium_sigue_rechazado():
    """La regla que exigió la medición sigue valiendo: `fs.write` es MEDIUM."""
    proposal = _propose(action="execute", capability="fs.write", risk="low")
    validator = PlanValidator(catalog=build_catalog())

    reasons = validator.validate(_mission(), proposal.plan)

    assert any("riesgo declarado insuficiente" in r for r in reasons)


def test_10_risk_medium_para_una_capability_medium_sigue_funcionando():
    """El camino bueno también: exigir el campo no puede cerrar la puerta."""
    proposal = _propose(action="execute", capability="fs.write", risk="medium")
    validator = PlanValidator(catalog=build_catalog())

    assert validator.validate(_mission(), proposal.plan) == []


def test_11_risk_alto_para_una_capability_critica_sigue_rechazado():
    """Y el extremo de arriba: el rango completo se compara igual que antes."""
    proposal = _propose(action="execute", capability="fs.remove", risk="low")
    validator = PlanValidator(catalog=build_catalog())

    assert any("riesgo declarado insuficiente" in r
               for r in validator.validate(_mission(), proposal.plan))


def test_12_no_se_deriva_el_riesgo_desde_el_catalogo():
    """Un modelo puede DECIR low sobre fs.write: el validador lo rechaza.

    Si se derivara el riesgo desde `CapabilitySpec.default_risk`, este test no podría existir:
    el plan se aceptaría y la protección dependería de que el modelo cooperara.
    """
    proposal = _propose(action="execute", capability="fs.write", risk="low")
    spec = build_catalog().get("fs.write")

    assert spec.default_risk == "medium"
    assert proposal.plan.steps[0].risk.value == "low", "el riesgo declarado NO se corrige a escondidas"
    assert PlanValidator(catalog=build_catalog()).validate(_mission(), proposal.plan), \
        "declarar low sobre una capability medium debe rechazarse"


# ----------------------------------------------------------------------
# 4) El contrato no depende del path (plan inicial ni dynamic replan)
# ----------------------------------------------------------------------


def test_13_el_dynamic_replan_exige_el_mismo_riesgo():
    """El replan dinámico usa el mismo `parse()`, así que no puede saltarse el contrato."""
    proposal = ModelPlanner(router=None).parse(
        _mission(), {"steps": [{"id": "cualquiera", "description": "d", "action": "research",
                               "capability": "fs.stat"}]},
        proposed_by="model_dynamic_replan", remap_ids=True,
    )

    assert proposal.plan is None
    assert any("no declara 'risk'" in r for r in proposal.reasons)


def test_14_con_riesgo_declarado_el_remapeo_de_ids_sigue_funcionando():
    """CORE-08B.1 no rompe el renumerado `dr1`, `dr2`… del overlay."""
    proposal = ModelPlanner(router=None).parse(
        _mission(), {"steps": [
            {"id": "cualquiera", "description": "d", "action": "research",
             "capability": "fs.stat", "risk": "low"},
            {"id": "otro", "description": "d", "action": "execute",
             "capability": "fs.write", "risk": "medium", "depends_on": ["cualquiera"]},
        ]},
        proposed_by="model_dynamic_replan", remap_ids=True,
    )

    assert proposal.ok, proposal.reasons
    assert [s.id for s in proposal.plan.steps] == ["dr1", "dr2"]
    assert proposal.plan.steps[1].depends_on == ["dr1"]
    assert proposal.plan.steps[1].risk.value == "medium"


@pytest.mark.parametrize("risk,capability,esperado", [
    ("low", "fs.read", True),
    ("medium", "fs.write", True),
    ("low", "fs.write", False),
])
def test_15_matriz_de_riesgo_declarado(risk, capability, esperado):
    """Tabla mínima: declarar el riesgo bien abre la puerta; declararlo mal la cierra."""
    proposal = _propose(action="execute" if capability == "fs.write" else "research",
                        capability=capability, risk=risk)
    reasons = PlanValidator(catalog=build_catalog()).validate(_mission(), proposal.plan)

    assert (not reasons) is esperado