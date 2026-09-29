"""P0 requisito 3 — Self Model: la zona de supuestos («qué supone»).

El plan pide que ALEXIS sepa, entre otras cosas, *qué supone*. La auditoría encontró que
ese era el único hueco que quedaba: el Self Model tenía `uncertainties` (lo que NO sabe)
y no tenía dónde_answer lo que da por hecho sin comprobarlo.

Este fichero cierra el hueco entero, que son tres cortes de la misma cadena:

1. `SelfModel` no tenía zona de supuestos ni sabía contestarla.
2. `SelfBrief` no los llevaba, así que no llegaban a la decisión.
3. El ensamblado del contexto no los exponía, así que aunque llegaran no se veían.

La distinción que da sentido a la zona: `unknown` es IGNORANCIA, `assumptions` es una
POSICIÓN DE TRABAJO sin verificar. Fundirlas sería exactamente el fallo que el ClaimGuard
prohíbe en el resto del sistema, así que aquí se comprueba que no se confunden.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.cognition.contracts import SelfBrief  # noqa: E402
from alexis.cognition.context import assemble as assemble_context  # noqa: E402
from alexis.cognition.state import KnowledgeState  # noqa: E402
from alexis.self.model import SelfModel  # noqa: E402


class _Knowledge:
    """Doble mínimo con la forma que `note_knowledge` lee por `getattr`."""

    def __init__(self, *, assumptions=(), hypotheses=(), uncertainties=()):
        self.assumptions = list(assumptions)
        self.hypotheses = list(hypotheses)
        self.uncertainties = list(uncertainties)


# =========================================================================== #
# 1. La zona existe y se alimenta del conocimiento REAL
# =========================================================================== #


def test_01_la_zona_de_supuestos_existe():
    m = SelfModel()
    assert m.assumptions == []
    assert m.hypotheses == []
    assert "assumptions" in m.snapshot()
    assert "hypotheses" in m.snapshot()


def test_02_se_alimenta_del_conocimiento_real():
    m = SelfModel()
    m.note_knowledge(_Knowledge(assumptions=["el informe está en /tmp"],
                                hypotheses=["falta el fichero de entrada"]))
    assert m.snapshot()["assumptions"] == ["el informe está en /tmp"]
    assert m.snapshot()["hypotheses"] == ["falta el fichero de entrada"]


def test_03_funciona_con_un_KnowledgeState_de_verdad():
    """No se prueba sólo el doble: el KnowledgeState real tiene esos campos."""
    k = KnowledgeState()
    k.add_assumption("el objetivo sigue vigente")
    k.add_hypothesis("falta una entrada")
    m = SelfModel()
    m.note_knowledge(k)
    assert m.snapshot()["assumptions"] == ["el objetivo sigue vigente"]
    assert m.snapshot()["hypotheses"] == ["falta una entrada"]


def test_04_asigna_y_no_acumula_un_supuesto_ya_resuelto():
    """La zona es una proyección del estado vivo: un supuesto caído desaparece solo.

    Si acumulara, el Self Model seguiría affirmsiendo cosas que el Core ya descartó.
    """
    m = SelfModel()
    m.note_knowledge(_Knowledge(assumptions=["a", "b"]))
    m.note_knowledge(_Knowledge(assumptions=["b"]))
    assert m.snapshot()["assumptions"] == ["b"]


def test_05_un_supuesto_que_pasa_a_hecho_se_registra():
    m = SelfModel()
    m.note_knowledge(_Knowledge(assumptions=["el fichero existe"]))
    m.note_knowledge(_Knowledge(assumptions=[]))
    assert m.snapshot()["assumptions"] == []


def test_06_none_no_inventa_supuestos():
    m = SelfModel()
    m.note_knowledge(None)
    assert m.snapshot()["assumptions"] == []
    assert m.snapshot()["hypotheses"] == []


# =========================================================================== #
# 2. La auto-pregunta «qué supone» se responde de verdad
# =========================================================================== #


@pytest.mark.parametrize(
    "pregunta",
    ["¿qué supone?", "¿Qué asumes?", "qué supongo", "qué suposiciones tienes",
     "¿qué estás suponiendo?", "dime tus asunciones"],
)
def test_07_la_auto_pregunta_de_supuestos_se_responde(pregunta):
    m = SelfModel()
    m.note_knowledge(_Knowledge(assumptions=["el informe está en /tmp"]))
    r = m.answer(pregunta)
    assert r["source"] == "assumed", pregunta
    assert "el informe está en /tmp" in r["answer"]


def test_08_sin_supuestos_lo_dice_y_no_inventa():
    m = SelfModel()
    r = m.answer("¿qué supone?")
    assert r["source"] == "assumed"
    assert "suponiendo nada" in r["answer"].lower()


def test_09_supuestos_e_incertidumbre_son_zonas_distintas():
    """La confusion entre ignorance y posición de trabajo es el fallo a evitar."""
    m = SelfModel()
    m.note_knowledge(
        _Knowledge(assumptions=["la ruta es /tmp/x"], uncertainties=["no sé si existe"])
    )
    assert "la ruta es /tmp/x" in m.answer("¿qué supone?")["answer"]
    # Y la pregunta de lo que NO sabe no debe mencionar el supuesto.
    assert "la ruta es /tmp/x" not in m.answer("¿qué no sé?")["answer"]


# =========================================================================== #
# 3. Llega a la decisión
# =========================================================================== #


def test_10_el_brief_lleva_los_supuestos():
    m = SelfModel()
    m.note_knowledge(_Knowledge(assumptions=["el informe está en /tmp"]))
    brief = SelfBrief.from_snapshot(m.snapshot())
    assert brief.assumptions == ["el informe está en /tmp"]
    assert brief.to_dict()["assumptions"] == ["el informe está en /tmp"]


def test_11_un_brief_sin_supuestos_no_falla():
    brief = SelfBrief.from_snapshot(SelfModel().snapshot())
    assert brief.assumptions == []
    assert brief.hypotheses == []


def test_12_los_supuestos_llegan_al_contexto_de_decision():
    """El punto que faltaba: el Self Model declaraba, pero la decisión no lo veía."""
    from alexis.contracts import AutonomyLevel, MissionEnvelope
    from alexis.autonomy.mission import MissionEngine

    mission = MissionEngine().create(
        "Revisar el informe",
        MissionEnvelope(objective="Revisar el informe",
                        autonomy=AutonomyLevel.SUPERVISED, allowed_actions=["analyze"]),
    )
    m = SelfModel()
    m.note_knowledge(_Knowledge(assumptions=["el informe está en /tmp"]))
    ctx = assemble_context(
        mission,
        KnowledgeState(),
        self_brief=SelfBrief.from_snapshot(m.snapshot()),
    )
    lineas = ctx.source("self_model").lines
    assert any("supongo" in l and "/tmp" in l for l in lineas), lineas


def test_13_el_context_no_cambia_de_contrato():
    """Los supuestos van por la fuente `self_model`: no se crea una fuente nueva."""
    from alexis.contracts import AutonomyLevel, MissionEnvelope
    from alexis.autonomy.mission import MissionEngine

    mission = MissionEngine().create(
        "Revisar el informe",
        MissionEnvelope(objective="Revisar el informe",
                        autonomy=AutonomyLevel.SUPERVISED, allowed_actions=["analyze"]),
    )
    m = SelfModel()
    m.note_knowledge(_Knowledge(assumptions=["x"]))
    ctx = assemble_context(
        mission, KnowledgeState(), self_brief=SelfBrief.from_snapshot(m.snapshot())
    )
    assert set(ctx.to_dict()["sources"]) == set(
        assemble_context(mission, KnowledgeState(), self_brief=None).to_dict()["sources"]
    )


def test_14_sin_supuestos_no_se_inventa_linea_de_contexto():
    from alexis.contracts import AutonomyLevel, MissionEnvelope
    from alexis.autonomy.mission import MissionEngine

    mission = MissionEngine().create(
        "Revisar el informe",
        MissionEnvelope(objective="Revisar el informe",
                        autonomy=AutonomyLevel.SUPERVISED, allowed_actions=["analyze"]),
    )
    ctx = assemble_context(
        mission, KnowledgeState(), self_brief=SelfBrief.from_snapshot(SelfModel().snapshot())
    )
    assert not any("supongo" in l for l in ctx.source("self_model").lines)


# =========================================================================== #
# 4. No-regresión del resto de zonas del Self Model
# =========================================================================== #


@pytest.mark.parametrize(
    ("pregunta", "zona"),
    [
        ("¿quién soy?", "identity"),
        ("¿qué estás haciendo?", "doing"),
        ("¿cuál es tu objetivo?", "goal"),
        ("¿qué puedes hacer?", "capabilities"),
        ("¿qué no puedes hacer?", "limits"),
        ("¿qué sabes?", "knowledge"),
        ("¿qué no sabes?", "unknown"),
        ("¿qué pasó?", "happened"),
        ("¿qué debo hacer después?", "next"),
    ],
)
def test_15_las_auto_preguntas_existentes_no_se_mueven(pregunta, zona):
    r = SelfModel().answer(pregunta)
    assert r["source"] == zona, f"{pregunta} -> {r['source']}, se esperaba {zona}"
