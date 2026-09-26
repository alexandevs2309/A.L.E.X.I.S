"""P0 §5.1 — los success_criteria del Intent Engine llegan al Goal y sobreviven al reinicio.

Regresión sobre el GAP #11 (BROKEN) del audit: `MissionEngine.create()` construía
`Goal(objective=objective)` y descartaba los criterios, así que ninguna capa posterior
tenía nada contra lo que verificar el objetivo.

Se prueba el camino completo: Intent (producido por el clasificador real) → Mission → Goal,
incluido el round-trip de persistencia, que es donde los criterios deben seguir disponibles
durante todo el ciclo cognitivo.
"""

import json
import pathlib

import pytest

from alexis.autonomy.mission import MissionEngine
from alexis.cognition.intent_classifier import IntentClassifier
from alexis.contracts import AutonomyLevel, MissionEnvelope
from alexis.models import ModelResponse
from alexis.storage.serialization import mission_from_row, mission_to_row

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Criterios tal y como el path con modelo los produce (ver intent_classifier.py:233-245).
MODEL_TASK_JSON = json.dumps(
    {
        "kind": "task",
        "objective": "Investigar y corregir el fallo de normalización de orden en utils.py",
        "target": "utils.py",
        "success_criteria": [
            "Los tests del proyecto pasan",
            "El cambio está aplicado en utils.py",
        ],
        "requested_capabilities": ["fs.read", "fs.write"],
        "side_effects_intent": "write",
        "ambiguity": None,
        "needs_clarification": False,
        "confidence": 0.9,
    }
)


class _ModelWithCriteria:
    """Router mínimo con un provider que devuelve un Intent TASK con criterios."""

    def providers(self):
        return [object()]

    async def complete(self, request):
        return ModelResponse(text=MODEL_TASK_JSON)


def _envelope(objective: str) -> MissionEnvelope:
    return MissionEnvelope(
        objective=objective,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=["understand", "analyze", "execute", "verify", "respond"],
    )


@pytest.mark.asyncio
async def test_los_criterios_del_intent_llegan_al_goal():
    """Intent → Mission → Goal: la cadena real, sin atajos."""
    intent = await IntentClassifier(_ModelWithCriteria()).classify(
        "investiga y corrige el fallo de normalización de orden en utils.py"
    )
    assert intent.is_task is True
    assert intent.success_criteria, "el clasificador debe producir criterios en el path con modelo"

    mission = MissionEngine().create(
        intent.objective,
        _envelope(intent.objective),
        success_criteria=intent.success_criteria,
    )

    assert mission.goal.success_criteria == intent.success_criteria
    assert mission.goal.success_criteria == [
        "Los tests del proyecto pasan",
        "El cambio está aplicado en utils.py",
    ]


def test_los_criterios_no_se_pierden_al_crear_la_mision():
    """Regresión directa del defecto: create() ya no descarta lo que se le pasa."""
    criteria = ["El informe existe", "El informe cita evidencia"]

    mission = MissionEngine().create("Redacta el informe", _envelope("Redacta el informe"), success_criteria=criteria)

    assert mission.goal.success_criteria == criteria


def test_el_orden_de_los_criterios_se_conserva():
    """El orden es semántico: el primer criterio es el que se verifica primero."""
    criteria = ["primero", "segundo", "tercero"]

    mission = MissionEngine().create("obj", _envelope("obj"), success_criteria=criteria)

    assert mission.goal.success_criteria == ["primero", "segundo", "tercero"]


def test_sin_criterios_la_lista_queda_vacia():
    """Retrocompatible: los ~20 call sites existentes siguen siendo válidos."""
    mission = MissionEngine().create("obj", _envelope("obj"))

    assert mission.goal.success_criteria == []


def test_el_goal_no_comparte_la_lista_del_intent():
    """El Goal no debe quedar atado al Intent que lo produjo (aliasing)."""
    criteria = ["criterio original"]

    mission = MissionEngine().create("obj", _envelope("obj"), success_criteria=criteria)
    criteria.append("criterio añadido después")

    assert mission.goal.success_criteria == ["criterio original"]


def test_los_criterios_sobreviven_a_un_reinicio():
    """Persistencia: tras recargar la misión de la BD los criterios siguen disponibles."""
    mission = MissionEngine().create(
        "Investiga y corrige el fallo de normalización de orden en utils.py",
        _envelope("obj"),
        success_criteria=["Los tests del proyecto pasan", "El cambio está aplicado en utils.py"],
    )
    row = mission_to_row(mission)
    row["state"] = "running"
    row["results"] = "[]"

    reloaded = mission_from_row(row)

    assert reloaded.goal.success_criteria == mission.goal.success_criteria


def test_una_mision_vieja_sin_criterios_aun_carga():
    """Una fila guardada antes de P0 §5.1 no tiene la clave: no debe romper la carga."""
    mission = MissionEngine().create("obj", _envelope("obj"), success_criteria=["criterio"])
    row = mission_to_row(mission)
    row["state"] = "running"
    row["results"] = "[]"
    goal = json.loads(row["goal"])
    goal.pop("success_criteria", None)
    row["goal"] = json.dumps(goal, ensure_ascii=False)

    reloaded = mission_from_row(row)

    assert reloaded.goal.success_criteria == []


def test_el_servidor_de_demo_pasa_los_criterios_del_intent():
    """Guarda de cableado del call site de producción.

    `apps/demo/server.py` compone toda la app al importarse (levanta el puerto, Ollama y
    PostgreSQL), así que no se puede importar en un test. Se verifica estáticamente que
    `_create_mission_from_intent` sigue pasando los criterios: si alguien revierte ese
    argumento, el GAP #11 vuelve a estar BROKEN aunque MissionEngine siga bien.
    """
    source = (REPO_ROOT / "apps" / "demo" / "server.py").read_text(encoding="utf-8")
    start = source.index("def _create_mission_from_intent(")
    body = source[start : source.index("\ndef ", start)]

    assert "success_criteria=intent.success_criteria" in body
