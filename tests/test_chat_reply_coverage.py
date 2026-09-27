"""Chat: toda intención que no abre misión tiene una respuesta HONESTA.

El fallo que motivó este fichero: `IntentKind` tenía nueve miembros y `_direct_reply()`
sólo cuatro ramas. Los otros cuatro —`small_talk`, `question`, `clarification`, `unknown`—
caían en un `else` que decía siempre "No he entendido la petición con suficiente claridad".

Eso era falso. "me ayudas?" se clasifica como `question` con normalidad y "¿qué quieres que
hagas?" igual: el sistema no ignoraba al usuario, lo entendía y se quedaba mudo. Y lo
peor: el texto afirmaba ignorancia donde había un `kind` correcto.

Cada `IntentKind` que no abre misión tiene que producir algo verdadero y utilizable.
"""

import pathlib
import re
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.cognition.conversation import ConversationSession  # noqa: E402
from alexis.cognition.contracts import Intent, IntentKind, SelfBrief  # noqa: E402
from alexis.cognition.intent_classifier import IntentClassifier  # noqa: E402

CAPS = ["fs.read", "fs.stat", "fs.write", "fs.remove", "execute.test"]
#: La frase que mentía sobre lo que el sistema había hecho.
MENTIRA = "No he entendido la petición con suficiente claridad"


def _session():
    return ConversationSession(classifier=IntentClassifier(), self_model=None)


def _brief(caps=CAPS):
    return SelfBrief(identity={"name": "ALEXIS"}, available_capabilities=list(caps))


def _reply(session, utterance, brief=None, outcome="degraded"):
    import asyncio

    brief = brief or _brief()
    intent = asyncio.run(session.classifier.classify(utterance, brief))
    return intent, session._direct_reply(intent, brief, outcome)


# --------------------------------------------------------------------------- #
# Cobertura: NINGÚN kind sin misión puede caer en la respuesta falsa
# --------------------------------------------------------------------------- #


def test_01_todo_kind_no_tarea_tiene_respuesta():
    """El invariante que estaba roto. `TASK` es el único que abre misión."""
    session = _session()
    sin_tarea = [k for k in IntentKind if k is not IntentKind.TASK]
    assert sin_tarea, "debe haber kinds que no abren misión"
    for kind in sin_tarea:
        intent = Intent(kind=kind, utterance="x", confidence=0.5)
        reply = session._direct_reply(intent, _brief(), "degraded")
        assert reply.text.strip(), f"{kind.value} devolvió texto vacío"
        assert MENTIRA not in reply.text, f"{kind.value} sigue con la respuesta falsa"


def test_02_la_frase_falsa_no_aparece_en_ningun_turno_real():
    """Ni una sola utterance del_language debe producir la respuesta que mentía."""
    session = _session()
    for u in ["hola", "me ayudas?", "¿Qué quieres que hagas?", "gracias", "vale",
              "¿qué puedes hacer?", "quién eres", "qwertyuiop", "adiós", "ok",
              "gracias por eso", "¿cómo estás?"]:
        _intent, reply = _reply(session, u)
        assert MENTIRA not in reply.text, f"{u!r} produce la respuesta falsa"


# --------------------------------------------------------------------------- #
# question: la clase más común, y la que se respondía peor
# --------------------------------------------------------------------------- #


def test_03_question_responde_con_capacidades_reales():
    session = _session()
    intent, reply = _reply(session, "me ayudas?")
    assert intent.kind is IntentKind.QUESTION
    assert "Sí" in reply.text
    for cap in CAPS[:4]:
        assert cap in reply.text, f"no menciona la capacidad real {cap}"


def test_04_question_no_promete_una_lista_inventada():
    """Las capacidades del texto tienen que salir del brief, no de la imaginación."""
    session = _session()
    _intent, reply = _reply(session, "me ayudas?", brief=_brief(["fs.read"]))
    assert "fs.read" in reply.text
    assert "fs.write" not in reply.text, "no debe prometer capacidades que no tiene"


def test_05_question_sin_capacidades_lo_dice():
    session = _session()
    _intent, reply = _reply(session, "me ayudas?", brief=_brief([]))
    assert "ninguna capacidad" in reply.text.lower()


def test_06_question_pide_el_objetivo_concreto():
    session = _session()
    _intent, reply = _reply(session, "¿Qué quieres que hagas?")
    assert "objetivo" in reply.text.lower()


# --------------------------------------------------------------------------- #
# small_talk: la cortesía no es una petición
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("frase", ["gracias", "gracias por eso", "vale", "ok", "perfecto",
                                    "de nada", "hasta luego", "adiós", "genial"])
def test_07_la_cortesia_no_es_unknown(frase):
    session = _session()
    intent, reply = _reply(session, frase)
    assert intent.kind is IntentKind.SMALL_TALK, f"{frase!r} -> {intent.kind.value}"
    assert MENTIRA not in reply.text
    assert "Cuando quieras" in reply.text


def test_08_la_cortesia_no_tiene_que_crear_mision():
    _intent, _reply_ = None, None
    intent, reply = _reply(_session(), "gracias")
    assert intent.is_task is False, "un 'gracias' no puede abrir misión"
    assert reply.kind in ("answer", "question")


def test_09_la_cortesia_no_tapa_a_las_ordenes():
    """`ok` suelta es cortesía; con contenido es una orden."""
    session = _session()
    intent, _r = _reply(session, "ok, borra el directorio temporal")
    assert intent.kind is IntentKind.TASK, f"-> {intent.kind.value}"
    assert intent.is_task is True


def test_10_small_talk_no_abre_mision_para_una_lista():
    session = _session()
    for frase in ("vale", "ok", "perfecto", "gracias", "de nada"):
        intent, _r = _reply(session, frase)
        assert intent.is_task is False, frase


# --------------------------------------------------------------------------- #
# unknown: aquí SÍ es verdad que no se entendió
# --------------------------------------------------------------------------- #


def test_11_unknown_sin_mentir_sobre_que_no_entendio():
    session = _session()
    intent, reply = _reply(session, "qwertyuiop asdfgh")
    assert intent.kind is IntentKind.UNKNOWN
    assert "No he logrado entender" in reply.text
    assert MENTIRA not in reply.text


def test_12_unknown_explica_como_ayudar():
    session = _session()
    _intent, reply = _reply(session, "qwertyuiop")
    bajo = reply.text.lower()
    assert "qué quieres que ocurra" in bajo, "debe decir qué necesita de ti"
    assert "sobre qué" in bajo


# --------------------------------------------------------------------------- #
# Lo que ya funcionaba, intacto
# --------------------------------------------------------------------------- #


def test_13_greeting_no_cambia():
    session = _session()
    intent, reply = _reply(session, "hola")
    assert intent.kind is IntentKind.GREETING
    assert reply.text.startswith("Hola. Soy ALEXIS.")


def test_14_capability_query_no_cambia():
    session = _session()
    intent, reply = _reply(session, "¿qué puedes hacer?")
    assert intent.kind is IntentKind.CAPABILITY_QUERY
    assert "Ahora mismo puedo hacer esto de verdad" in reply.text


def test_15_meta_query_no_cambia():
    session = _session()
    intent, reply = _reply(session, "quién eres")
    assert intent.kind is IntentKind.META_QUERY
    assert "Soy ALEXIS" in reply.text


def test_16_la_nota_de_degraded_sigue_apareciendo():
    session = _session()
    _intent, reply = _reply(session, "me ayudas?", outcome="degraded")
    assert "sin un modelo real disponible" in reply.text
    assert reply.degraded is True


def test_17_sin_degraded_no_hay_nota():
    session = _session()
    _intent, reply = _reply(session, "me ayudas?", outcome="real")
    assert "sin un modelo real" not in reply.text
    assert reply.degraded is False


# --------------------------------------------------------------------------- #
# Guardia: el texto no puede llevar basura noASCII inattendida
# --------------------------------------------------------------------------- #


def test_18_las_respuestas_no_llevan_caracteres_no_latinos():
    """CJK, cirílico o coreano en un reply es un fallo de generación, no de ALEXIS.

    Se añadió porque es exactamente el tipo de basura que se ha colado en comentarios y
    literales durante el desarrollo: si aparece en un string, hay que verlo.
    """
    session = _session()
    # Rangos en escapes, no literales: así este fichero es ASCII puro y cumple
    # la misma regla que exige. Un guardián que se incumple a sí mismo no sirve.
    patron = re.compile("[\\u3040-\\u30ff\\u4e00-\\u9fff\\uac00-\\ud7af\\u0400-\\u04ff]")
    for u in ["hola", "me ayudas?", "¿Qué quieres que hagas?", "gracias", "vale",
              "qwertyuiop", "¿qué puedes hacer?", "quién eres"]:
        _intent, reply = _reply(session, u)
        assert not patron.search(reply.text), f"{u!r} → {reply.text!r}"


# --------------------------------------------------------------------------- #
# El clasificador: SMALL_TALK tiene que existir de verdad
# --------------------------------------------------------------------------- #


def test_19_small_talk_deja_de_ser_inalcanzable():
    """Antes ningún camino del clasificador devolvía `SMALL_TALK`."""
    import asyncio

    session = _session()
    brief = _brief()
    for frase in ("gracias", "vale", "ok", "perfecto", "adiós"):
        intent = asyncio.run(session.classifier.classify(frase, brief))
        assert intent.kind is IntentKind.SMALL_TALK, f"{frase!r} -> {intent.kind.value}"


def test_20_las_tareas_siguen_siendo_tareas():
    import asyncio

    session = _session()
    brief = _brief()
    for frase in ["lee notas.txt", "borra el directorio temporal", "escribe en salida.txt"]:
        intent = asyncio.run(session.classifier.classify(frase, brief))
        assert intent.kind is IntentKind.TASK, f"{frase!r} -> {intent.kind.value}"


def test_21_question_sigue_siendo_question():
    import asyncio

    session = _session()
    brief = _brief()
    for frase in ["me ayudas?", "¿Qué quieres que hagas?", "¿por qué?"]:
        intent = asyncio.run(session.classifier.classify(frase, brief))
        assert intent.kind is IntentKind.QUESTION, f"{frase!r} -> {intent.kind.value}"
