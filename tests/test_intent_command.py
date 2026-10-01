"""P0 requisito 1 — `command`: control conversacional sobre ALEXIS.

Este fichero cierra el último hueco funcional del Intent Engine. `clarification` ya
existía (P0 §13) y no se toca aquí: su cobertura sigue en `test_p0_13_ask_user.py` y
`test_p0_closure.py`, y abajo sólo se usa como no-regresión.

La definición es estrecha a propósito, y esa estrechez es lo que se prueba:

- `command` = instrucciones que controlan el COMPORTAMIENTO de ALEXIS ("para", "cancela
  eso", "repite", "status"). No piden trabajo sobre el mundo, así que no abren misión.
- Los imperativos operativos ("abre la terminal", "borra el archivo") siguen siendo
  `TASK`, porque necesitan plan, policy, ejecución y verificación.

El error que este test existe para impedir es doble: no puede ser que un comando de
control abra una misión, ni que un imperativo operativo deje de abrirla.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.cognition.contracts import (  # noqa: E402
    DIRECT_KINDS,
    MISSION_KINDS,
    Intent,
    IntentKind,
)
from alexis.cognition.conversation import ConversationSession  # noqa: E402
from alexis.cognition.intent_classifier import (  # noqa: E402
    RuleBasedIntentClassifier,
    _SYSTEM_PROMPT,
)

CLASIFICADOR = RuleBasedIntentClassifier()


def _kind(utterance: str, *, pending: bool = False) -> IntentKind:
    return CLASIFICADOR.classify(utterance, pending_clarification=pending).kind


# =========================================================================== #
# 1. El kind existe y el contrato lo coloca donde corresponde
# =========================================================================== #


def test_01_command_existe_como_kind_propio():
    assert IntentKind.COMMAND.value == "command"
    assert "command" in {k.value for k in IntentKind}


def test_02_command_es_directo_y_nunca_una_mision():
    """La regla P3.3 no se toca: sólo `TASK` abre misión."""
    assert MISSION_KINDS == frozenset({IntentKind.TASK})
    intent = Intent(kind=IntentKind.COMMAND, utterance="para")
    assert not intent.is_task
    assert intent.is_direct_answer
    assert IntentKind.COMMAND in DIRECT_KINDS


# =========================================================================== #
# 2. Clasificación determinista de órdenes de control
# =========================================================================== #


@pytest.mark.parametrize(
    "utterance",
    [
        "para",
        "para ya",
        "detente",
        "detén",
        "cancela",
        "cancela eso",
        "Cancela esto.",
        "cancela la operación",
        "repite",
        "repite eso",
        "olvida",
        "olvida esto",
        "reanuda",
        "reanuda la tarea",
        "status",
        "estado actual",
    ],
)
def test_03_las_ordenes_de_control_son_command(utterance):
    assert _kind(utterance) is IntentKind.COMMAND


# =========================================================================== #
# 3. command ≠ TASK: los imperativos operativos no se han movido
# =========================================================================== #


@pytest.mark.parametrize(
    "utterance",
    [
        "abre la terminal",
        "lee este archivo",
        "busca el informe",
        "modifica este archivo",
        "borra el archivo",
        "investiga el proyecto",
        "revisa el código",
    ],
)
def test_04_los_imperativos_operativos_siguen_siendo_task(utterance):
    """El hueco se cierra SIN quitar verbos de `_TASK_VERBS`."""
    assert _kind(utterance) is IntentKind.TASK
    assert Intent(kind=IntentKind.TASK, utterance=utterance).is_task


@pytest.mark.parametrize(
    ("control", "operativo"),
    [
        ("cancela eso", "cancela el informe del trimestre"),
        ("para", "para mañana"),
        ("estado actual", "estado del proyecto"),
        ("olvida esto", "olvida el archivo"),
    ],
)
def test_05_la_distincion_depende_del_objeto_no_del_verbo(control, operativo):
    """El mismo verbo cambia de clase según lo que se le aplique.

    Es la prueba de que `command` no es sinónimo de "imperativo": "cancela eso" controla
    a ALEXIS, "cancela el informe" es trabajo sobre el mundo.
    """
    assert _kind(control) is IntentKind.COMMAND
    assert _kind(operativo) is not IntentKind.COMMAND


def test_06_el_espanol_frecuente_no_se_convierte_en_orden():
    """`para` y `estado` son palabras comunes: un matching laxo las haría comandos."""
    for frase in ("para mañana", "para ti", "estado del proyecto", "estoy bien"):
        assert _kind(frase) is not IntentKind.COMMAND, frase


# =========================================================================== #
# 4. Consumidor real: la respuesta directa
# =========================================================================== #


@pytest.mark.asyncio
async def test_07_command_llega_a_la_respuesta_directa_sin_mision():
    creadas = []

    def _crear(intent):
        creadas.append(intent)
        raise AssertionError("un comando de control no debe crear misión")

    from alexis.cognition.intent_classifier import IntentClassifier

    sesion = ConversationSession(
        classifier=IntentClassifier(),  # el `classify` de la sesión es asíncrono
        self_model=None,
        create_mission=_crear,
        enqueue=lambda m: None,
    )
    reply = await sesion.handle_turn("cancela eso")

    assert not creadas, "no debe pasar por create_mission"
    assert reply.mission_id is None
    assert reply.text


def test_08_la_respuesta_no_afirma_que_se_ejecuto_algo():
    """Un comando no se ejecuta: la respuesta no puede fingir lo contrario."""
    sesion = ConversationSession(classifier=CLASIFICADOR, self_model=None)
    texto = sesion._command_answer()
    for mentira in ("Listo, parado", "Hecho", "Ejecutado", "completado"):
        assert mentira not in texto


# =========================================================================== #
# 5. El modelo: contrato completo y ningún comando abre misión
# =========================================================================== #


def test_09_el_system_prompt_describe_todos_los_kinds():
    """El prompt ya no describe un subconjunto: le faltaban cuatro."""
    for kind in IntentKind:
        assert kind.value in _SYSTEM_PROMPT, f"el prompt no menciona {kind.value}"


def test_10_el_prompt_distingue_command_de_task():
    assert "command" in _SYSTEM_PROMPT
    assert "task" in _SYSTEM_PROMPT
    # La definición negativa es la que evita la confusión en el modelo pequeño.
    assert "NO es un imperativo cualquiera" in _SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_11_un_modelo_que_ve_un_comando_como_task_no_abre_mision():
    """Guard simétrico: el vocabulario de control es exacto, y por eso manda.

    Sin esto, un modelo pequeño que clasificara "para" como `task` abriría una misión con
    el único propósito de dejar de hacer nada.
    """

    class _FakeResponse:
        outcome = __import__("alexis.models.provider", fromlist=["ModelOutcome"]).ModelOutcome.REAL
        text = ""
        data = {
            "kind": "task",
            "objective": "Parar",
            "requested_capabilities": [],
            "side_effects_intent": "read",
            "confidence": 0.9,
        }

        def audit_event(self, task):
            return {"source": "model"}

    class _FakeModel:
        source = "model"

        def build_request(self, utterance, brief):
            return object()

        def parse(self, utterance, brief, data):
            from alexis.cognition.intent_classifier import ModelIntentClassifier

            return ModelIntentClassifier(None).parse(utterance, brief, data)

    class _FakeRouter:
        async def complete(self, request, *, correlation=None):
            return _FakeResponse()

    from alexis.cognition.intent_classifier import IntentClassifier

    clasificador = IntentClassifier(router=_FakeRouter(), model=_FakeModel())
    intent = await clasificador.classify("para ya")

    assert intent.kind is IntentKind.COMMAND, "las reglas exactas corrigen al modelo"
    assert not intent.is_task
    assert intent.model_meta.get("cognition_outcome") == "degraded"


# =========================================================================== #
# 6. No-regresión: el resto de kinds y clarification intactos
# =========================================================================== #


@pytest.mark.parametrize(
    ("utterance", "esperado"),
    [
        ("Hola ALEXIS", IntentKind.GREETING),
        ("¿Qué puedes hacer?", IntentKind.CAPABILITY_QUERY),
        ("¿Qué hiciste?", IntentKind.SELF_QUERY),
        ("¿Cómo funcionas?", IntentKind.META_QUERY),
        ("gracias", IntentKind.SMALL_TALK),
        ("¿me ayudas?", IntentKind.QUESTION),
        ("borra el archivo", IntentKind.TASK),
        ("asdkjhaskdjh", IntentKind.UNKNOWN),
    ],
)
def test_12_los_demas_kinds_no_se_mueven(utterance, esperado):
    assert _kind(utterance) is esperado


def test_13_clarification_sigue_siendo_clarification():
    """No se tocó: sigue produciéndose igual que con §13."""
    assert _kind("src/main.py", pending=True) is IntentKind.CLARIFICATION
    # Y una aclaración no es un comando, ni al revés.
    assert _kind("para", pending=True) is IntentKind.CLARIFICATION


@pytest.mark.parametrize(
    "utterance",
    ["para", "cancela eso", "repite", "status", "borra el archivo", "revisa el código"],
)
def test_14_ningun_turno_abre_mision_salvo_task(utterance):
    assert (_kind(utterance) is IntentKind.TASK) is Intent(
        kind=_kind(utterance), utterance=utterance
    ).is_task
