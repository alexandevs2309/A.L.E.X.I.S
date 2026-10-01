"""CORE-02 — Contrato de `success_criteria`: de texto libre a predicado verificable.

Estos tests atacan el hueco que B5 destapó al cerrar CORE-01: el `GoalVerifier` ya
estaba inyectado y vivo en producción, pero las misiones llegaban con
`success_criteria=[]`, así que no había nada que verificar y `COMPLETED` era
inalcanzable por la vía real.

El contrato que se prueba:

    IntentClassifier (modelo o reglas)
        → criteria.normalize_criteria
        → Intent.success_criteria CANÓNICOS
        → Mission.goal.success_criteria
        → GoalVerifier (sin tocar)  → COMPLETED

Dos propiedades hacen que esto sea útil y no un atajo:

1. Los criterios salen del CLASIFICADOR, no del test. El caso B5 de este fichero
   (`test_b5_el_vertical_real_cierra_completed_solo_por_el_contrato`) NO inyecta
   `file_exists:notas.txt` en la misión: lo pide al `IntentClassifier` y luego verifica
   que la misión lo recibió. Si el normalizador desapareciera, ese test falla.
2. `GoalVerifier` no se toca. Todo lo que se afirma sobre el vocabulario se comprueba
   contra el `parse_predicate` real, no contra una copia de la lista.

`file_exists` acredita que el archivo EXISTE y fue observado por una herramienta; no
acredita que ALEXIS entendiera su contenido. Los objetivos sobre contenido siguen
siendo `unverifiable` con el vocabulario actual, y hay tests que lo fijan.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from alexis.autonomy.gates import AutonomyGate  # noqa: E402
from alexis.autonomy.goal_state import goal_is_confirmed  # noqa: E402
from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.capabilities import build_catalog  # noqa: E402
from alexis.cognition.contracts import IntentKind  # noqa: E402
from alexis.cognition.criteria import (  # noqa: E402
    MAX_CRITERIA,
    canonicalize,
    normalize_criteria,
)
from alexis.cognition.goal_verification import (  # noqa: E402
    PREDICATES,
    CriterionStatus,
    GoalVerifier,
    parse_predicate,
)
from alexis.cognition.intent_classifier import IntentClassifier  # noqa: E402
from alexis.cognition.loop import CognitiveRuntime  # noqa: E402
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    MissionEnvelope,
    MissionState,
)
from alexis.core.runtime import AlexisRuntime  # noqa: E402
from alexis.events.bus import EventBus  # noqa: E402
from alexis.execution import SandboxExecutor  # noqa: E402
from alexis.learning.system import ExperienceLearner  # noqa: E402
from alexis.memory.store import InMemoryMemory  # noqa: E402
from alexis.models import ModelResponse  # noqa: E402
from alexis.security.policy import PolicyEngine  # noqa: E402
from alexis.security.sandbox import SandboxRunner  # noqa: E402
from alexis.tools.filesystem import build_filesystem_tools  # noqa: E402
from alexis.tools.registry import ToolRegistry  # noqa: E402
from alexis.verification import FilesystemVerifier  # noqa: E402
from alexis.world.model import WorldModel  # noqa: E402

B5 = "Analiza el archivo notas.txt y dime qué contiene"

ALLOWED_ACTIONS = [
    "understand", "analyze", "research", "execute", "verify",
    "modify", "test", "commit", "write", "respond",
]


def _mission_con(criteria):
    """Misión mínima con estos criterios: para evaluates los con el verificador real."""
    return MissionEngine().create(
        "obj",
        MissionEnvelope(
            objective="obj",
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=list(ALLOWED_ACTIONS),
        ),
        success_criteria=criteria,
    )


# ---------------------------------------------------------------------- #
# 0. El vocabulario NO se inventa: todo canónico sale de PREDICATES
# ---------------------------------------------------------------------- #


def test_todo_criterio_generado_usa_el_vocabulario_del_verifier():
    """La razón de ser de `criteria.py`: no puede divergir de lo que el verifier lee."""
    textos = [
        B5,
        "Borra el archivo notas.txt",
        "Crea el archivo notas.txt con contenido",
        "Los tests del proyecto pasan",
        "Los tests están fallando",
        "El archivo notas.txt existe",
    ]
    for texto in textos:
        criteria, _ = normalize_criteria(texto, texto, [])
        for criterio in criteria:
            parsed = parse_predicate(criterio)
            assert parsed is not None, f"{criterio!r} no lo sabe leer el GoalVerifier"
            assert parsed[0] in PREDICATES, f"{parsed[0]!r} no está en PREDICATES"
            assert criterio.startswith(f"{parsed[0]}:"), (
                "los predicados necesitan el separador ':' para ser legibles"
            )


# ---------------------------------------------------------------------- #
# 1-5. Derivación determinista por tipo de objetivo
# ---------------------------------------------------------------------- #


def test_1_read_con_path_deriva_file_exists():
    criteria, status = normalize_criteria(B5, B5, [])
    assert criteria == ["file_exists:notas.txt"]
    assert status["verifiable"] == 1
    assert status["unverifiable"] == 0


def test_2_write_create_edit_deriva_file_exists():
    for texto in (
        "Crea el archivo notas.txt",
        "Escribe el archivo notas.txt",
        "Modifica el archivo notas.txt",
    ):
        criteria, _ = normalize_criteria(texto, texto, [])
        assert criteria == ["file_exists:notas.txt"], texto


def test_2b_write_que_pide_contenido_anade_el_criterio_de_tamanio():
    criteria, _ = normalize_criteria("Crea el archivo notas.txt con contenido", "", [])
    assert "file_exists:notas.txt" in criteria
    assert "file_size_at_least:notas.txt:1" in criteria


def test_2c_no_se_inventa_el_criterio_de_tamanio_si_no_corresponde():
    """Regla D, segunda parte: el tamaño sólo aparece si el texto pide contenido."""
    criteria, _ = normalize_criteria("Borra el archivo notas.txt", "", [])
    assert criteria == ["file_missing:notas.txt"]
    assert not [c for c in criteria if c.startswith("file_size_at_least")]


def test_3_destructive_deriva_file_missing():
    for texto in (
        "Borra el archivo notas.txt",
        "Elimina el archivo notas.txt",
    ):
        criteria, _ = normalize_criteria(texto, texto, [])
        assert criteria == ["file_missing:notas.txt"], texto


def test_4_tests_pasan_deriva_tests_passing():
    criteria, _ = normalize_criteria("Los tests del proyecto pasan", "", [])
    assert criteria == ["tests_passing:suite"]
    assert parse_predicate(criteria[0])[0] == "tests_passing"


def test_5_tests_fallan_deriva_tests_failing():
    for texto in (
        "Los tests del proyecto fallan",
        "Los tests están fallando",
    ):
        criteria, _ = normalize_criteria(texto, texto, [])
        assert criteria == ["tests_failing:suite"], texto
        assert parse_predicate(criteria[0])[0] == "tests_failing"


def test_un_bug_no_es_la_suite_fallando():
    """Falso positivo que NO puede pasar: "el fallo de normalización" no es la suite."""
    criteria, status = normalize_criteria("Arregla", "Arregla", ["El fallo de normalización está corregido"])
    assert not [c for c in criteria if c.startswith("tests_")], "un bug suelto no es la suite"
    assert status["verifiable"] == 0
    assert status["unverifiable"] == 1
    # El criterio se conserva (regla J) pero no se convierte en un predicado de suite.
    assert "El fallo de normalización está corregido" in criteria


# ---------------------------------------------------------------------- #
# 6-7. Lo que dice el modelo
# ---------------------------------------------------------------------- #


def test_6_un_predicado_valido_del_modelo_se_conserva():
    criteria, status = normalize_criteria("Borra el informe", "Borra el informe", ["file_exists:notas.txt"])
    assert criteria == ["file_exists:notas.txt"], "un predicado válido se respeta tal cual"
    assert status["verifiable"] == 1


def test_7_texto_natural_con_path_se_canoniza():
    criteria, _ = normalize_criteria("Borra el informe", "Borra el informe", ["El archivo notas.txt existe"])
    assert criteria == ["file_exists:notas.txt"]


def test_7b_canonize_es_puro_y_no_toca_el_entorno():
    assert canonicalize("file_exists:notas.txt") == "file_exists:notas.txt"
    assert canonicalize("El archivo notas.txt existe") == "file_exists:notas.txt"
    assert canonicalize("El bug está corregido") is None


# ---------------------------------------------------------------------- #
# 8-9. La lista vacía: el B5 real
# ---------------------------------------------------------------------- #


def test_8_criteria_vacio_mas_path_en_utterance_deriva():
    """Este es el caso real de B5: el modelo respondió `success_criteria: []`."""
    criteria, status = normalize_criteria(B5, "Analyze the file notas.txt and report its contents.", [])
    assert criteria == ["file_exists:notas.txt"]
    assert status["verifiable"] == 1


def test_8b_la_utterance_manda_sobre_el_objective_parafraseado():
    """Regla H: el modelo puede inventar una ruta en su objective. El utterance no."""
    criteria, _ = normalize_criteria(
        "Analiza el archivo notas.txt",
        "Analyze the file完全不同.txt and report",  # ruta que el modelo inventó
        [],
    )
    assert criteria == ["file_exists:notas.txt"]
    assert not [c for c in criteria if "完全不同" in c]


def test_9_criteria_vacio_sin_path_ni_suite_es_unverifiable():
    criteria, status = normalize_criteria("Arregla el proyecto entero", "Arregla el proyecto entero", [])
    assert criteria == []
    assert status["verifiable"] == 0
    assert status["reason"]


# ---------------------------------------------------------------------- #
# 10-11. Criterio inválido: se conserva, no se inventa
# ---------------------------------------------------------------------- #


def test_10_criterio_invalido_queda_unverifiable():
    criteria, status = normalize_criteria("Arregla", "Arregla", ["El informe cita las tres métricas"])
    assert status["unverifiable"] == 1
    assert status["verifiable"] == 0
    assert status["unverifiable_criteria"] == ["El informe cita las tres métricas"]


def test_11_no_hay_drop_silencioso():
    """Un criterio que no se puede comprobar sigue viajando a la misión.

    No se borra: si se borrara, la traza de por qué el objetivo no cerró desaparecería
    del contexto. GoalVerifier lo dejará en "no hay checker", que es la verdad.
    """
    criteria, status = normalize_criteria("Revisa el proyecto", "Revisa el proyecto", ["El informe está bien redactado"])
    assert "El informe está bien redactado" in criteria, "el criterio del modelo no se descarta"
    assert status["unverifiable"] == 1


def test_11b_verificable_e_inverificable_conviven_sin_que_el_malo_contamine():
    criteria, status = normalize_criteria(
        "Revisa el proyecto", "Revisa el proyecto", ["file_exists:notas.txt", "El informe está bien redactado"]
    )
    assert "file_exists:notas.txt" in criteria
    assert status["verifiable"] == 1
    assert status["unverifiable"] == 1


# ---------------------------------------------------------------------- #
# 12-13. Tope y trazabilidad
# ---------------------------------------------------------------------- #


def test_12_maximo_cinco_criterios():
    muchos = [f"file_exists:f{i}.txt" for i in range(9)]
    criteria, _ = normalize_criteria("Revisa", "Revisa", muchos)
    assert len(criteria) == MAX_CRITERIA == 5


def test_13_trazabilidad_en_model_meta():
    """Regla K: el por qué viaja con la intención, no se pierde al crear la misión."""

    class _Router:
        def providers(self):
            return [object()]

        async def complete(self, request, *, correlation=None):
            return ModelResponse(
                text='{"kind":"task","objective":"%s","success_criteria":[],"confidence":0.9}' % B5
            )

    import asyncio

    intent = asyncio.run(IntentClassifier(_Router()).classify(B5))
    status = intent.model_meta.get("criteria_status")
    assert status is not None
    assert status["verifiable"] == 1
    assert status["unverifiable"] == 0
    assert status["reason"]


def test_13b_un_turno_que_no_es_task_no_lleva_criteria_status():
    """Un comando de control no abre misión: no hay contrato que cumplir."""
    import asyncio

    intent = asyncio.run(IntentClassifier().classify("cancela eso"))
    assert intent.kind is IntentKind.COMMAND
    assert not intent.is_task
    assert "criteria_status" not in intent.model_meta


# ---------------------------------------------------------------------- #
# 14. Rutas con espacios: no se inventan
# ---------------------------------------------------------------------- #


def test_14_ruta_con_espacios_no_se_inventa():
    """`informe final.txt` NO es `final.txt`.

    El GoalVerifier lee un solo token, así que derivar "file_missing:final.txt"
    comprobaría un fichero que quizá no existe y podría dar el objetivo por cumplido
    sin medir lo que el usuario pidió. Se prefiere conservar el criterio como
    `unverifiable` antes que canonizar una ruta inventada.
    """
    criteria, status = normalize_criteria(
        "Borra el informe", "Borra el informe", ["El archivo informe final.txt ya no está"]
    )
    assert not [c for c in criteria if parse_predicate(c)], (
        "una ruta con espacios no genera predicado: sería inventar el fichero"
    )
    assert status["verifiable"] == 0
    assert status["unverifiable"] == 1
    assert "El archivo informe final.txt ya no está" in criteria, "se conserva la traza"


def test_14b_una_ruta_de_un_token_si_se_deriva():
    criteria, _ = normalize_criteria("Analiza notas.txt", "Analiza notas.txt", [])
    assert criteria == ["file_exists:notas.txt"]


# ---------------------------------------------------------------------- #
# 15-18. No-regresión
# ---------------------------------------------------------------------- #


def test_15_los_kinds_no_se_mueven():
    """`command` sigue siendo control y `task` sigue siendo trabajo."""
    import asyncio

    clasificador = IntentClassifier()
    assert asyncio.run(clasificador.classify("cancela eso")).kind is IntentKind.COMMAND
    assert asyncio.run(clasificador.classify("borra el archivo")).kind is IntentKind.TASK
    assert asyncio.run(clasificador.classify("hola")).kind is IntentKind.GREETING


def test_16_los_criterios_llegan_al_goal_intactos():
    """El contrato P0 §5.1 sigue vigente: lo que produce la intención llega al Goal."""
    import asyncio

    intent = asyncio.run(IntentClassifier().classify(B5))
    mission = MissionEngine().create(
        intent.objective or intent.utterance,
        MissionEnvelope(
            objective=intent.objective or intent.utterance,
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=list(ALLOWED_ACTIONS),
        ),
        success_criteria=intent.success_criteria,
    )
    assert mission.goal.success_criteria == intent.success_criteria
    assert mission.goal.success_criteria == ["file_exists:notas.txt"]


def test_17_el_goal_verifier_no_se_ha_tocado():
    """Guarda de no-regresión: el vocabulario y el parser siguen siendo los de CORE-01."""
    assert PREDICATES == (
        "file_size_at_least",
        "file_exists",
        "file_missing",
        "tests_failing",
        "tests_passing",
    )
    assert parse_predicate("Los tests del proyecto pasan") is None
    assert parse_predicate("file_exists:notas.txt") == ("file_exists", ["notas.txt"])


def test_18_un_canonico_es_siempre_legible_por_el_verifier_real():
    """La comprobación cruzada: producir un canónico NO es crear un predicado nuevo.

    Cada criterio generado pasa por el `parse_predicate` real, y el `GoalVerifier` real
    lo evalúa: sin observación de una tool, nada puede darse por cumplido.
    """
    from alexis.cognition.goal_verification import CriterionEvaluation

    for texto in (B5, "Borra el archivo notas.txt", "Los tests pasan"):
        criteria, _ = normalize_criteria(texto, texto, [])
        assert criteria, texto
        for criterio in criteria:
            parsed = parse_predicate(criterio)
            assert parsed is not None and parsed[0] in PREDICATES, criterio
            # El verificador real lo evalúa de verdad y, sin tool observada, no verifica.
            verificacion = GoalVerifier(world=WorldModel()).verify(
                _mission_con(criterio)
            )
            assert verificacion.verified is False
            assert all(
                e.status is CriterionStatus.INSUFFICIENT_EVIDENCE
                for e in verificacion.evaluations
            )
    # Y un `satisfied` sin evidencia fiable tampoco verifica (guarda interna real).
    evaluacion = CriterionEvaluation(
        criterion="x", status=CriterionStatus.SATISFIED, reason="lo dice el modelo"
    )
    assert GoalVerifier._is_verified([evaluacion]) is False


# ---------------------------------------------------------------------- #
# E2E B5: el criterio sale del clasificador y la misión cierra COMPLETED
# ---------------------------------------------------------------------- #


def _runtime(tmp_path):
    catalog = build_catalog()
    world = WorldModel()
    registry = ToolRegistry()
    registry.register_all(build_filesystem_tools(tmp_path))
    executor = SandboxExecutor(tools=registry, sandbox=SandboxRunner(tmp_path))
    cognitive = CognitiveRuntime(
        policy=PolicyEngine(),
        gate=AutonomyGate(),
        executor=executor,
        verifier=FilesystemVerifier(workspace=tmp_path),
        world=world,
        catalog=catalog,
        goal_verifier=GoalVerifier(world=world),
    )
    runtime = AlexisRuntime(
        planner=__import__("alexis.cognition.planner", fromlist=["Planner"]).Planner(),
        policy=PolicyEngine(),
        executor=executor,
        verifier=FilesystemVerifier(workspace=tmp_path),
        memory=InMemoryMemory(),
        learning=ExperienceLearner(),
        event_bus=EventBus(),
        gate=AutonomyGate(),
        cognitive=cognitive,
    )
    return runtime, world


@pytest.mark.asyncio
async def test_b5_el_vertical_real_cierra_completed_solo_por_el_contrato(tmp_path):
    """B5 de punta a punta, y el criterio NO se inyecta a mano en la misión.

    La cadena exigida es exactamente la del enunciado:

        "Analiza el archivo notas.txt y dime qué contiene"
            → IntentClassifier            (sin modelo: fallback determinista)
            → success_criteria            ["file_exists:notas.txt"]
            → Mission.goal.success_criteria
            → run_mission                 (fs.read real)
            → WorldModel                  (observación real de la tool)
            → GoalVerifier                verified=True
            → COMPLETED

    El criterio se pide al clasificador; si este test lo escribiera a mano, seguiría
    pasando aunque el contrato desapareciera, que es justo lo que hay que evitar.
    """
    (tmp_path / "notas.txt").write_text("contenido real de notas\n", encoding="utf-8")

    # 1. El criterio lo produce el CLASIFICADOR, no el test.
    intent = await IntentClassifier().classify(B5)
    assert intent.is_task is True
    assert intent.success_criteria == ["file_exists:notas.txt"]
    assert intent.model_meta["criteria_status"]["verifiable"] == 1

    # 2. La misión lo recibe sin que nadie lo escriba a mano.
    mission = MissionEngine().create(
        intent.objective or intent.utterance,
        MissionEnvelope(
            objective=intent.objective or intent.utterance,
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=list(ALLOWED_ACTIONS),
            capabilities=[s.id for s in build_catalog().enabled()],
        ),
        success_criteria=intent.success_criteria,
    )
    assert mission.goal.success_criteria == ["file_exists:notas.txt"]

    # 3. Vertical real: fs.read + observación + GoalVerifier + settle.
    runtime, world = _runtime(tmp_path)
    result = await runtime.run_mission(mission)

    assert result.state is MissionState.COMPLETED, result.context.get("goal_verification_reason")
    assert result.goal_verification is not None
    assert result.goal_verification.verified is True
    assert goal_is_confirmed(result.goal_verification) is True
    assert result.context.get("goal_verification_reason", "").startswith("objetivo verificado")

    # 4. La evidencia es de una tool real, no un claim.
    entity = world.known_path("notas.txt")
    assert entity is not None
    assert str(entity.source).startswith("tool:")

    # 5. Sin bypass: pasó por el ciclo con policy y gate reales.
    decisions = result.context.get("decisions") or {}
    assert decisions
    assert any(d.get("policy_verdict") == "allow" for d in decisions.values())


@pytest.mark.asyncio
async def test_b5_sin_archivo_no_cierra_completed(tmp_path):
    """El contrato no compra falsos positivos: sin observación real, no hay COMPLETED.

    Sin `notas.txt` en el workspace, el `fs.read` falla, el objetivo no puede
    verificarse y la misión no se marca completada. La evidencia que exige el
    `GoalVerifier` es una observación real de una herramienta; aquí no existe.
    """
    intent = await IntentClassifier().classify(B5)
    assert intent.success_criteria == ["file_exists:notas.txt"]

    mission = MissionEngine().create(
        intent.objective or intent.utterance,
        MissionEnvelope(
            objective=intent.objective or intent.utterance,
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=list(ALLOWED_ACTIONS),
        ),
        success_criteria=intent.success_criteria,
    )
    runtime, _ = _runtime(tmp_path)
    result = await runtime.run_mission(mission)

    assert result.state is not MissionState.COMPLETED, (
        "sin archivo observado no puede haber objetivo verificado"
    )
    # Y el verificador, si se consulta, dice por qué: no hay entidad observada.
    verificacion = GoalVerifier(world=WorldModel()).verify(mission)
    assert verificacion.verified is False
    assert verificacion.evaluations[0].status is CriterionStatus.INSUFFICIENT_EVIDENCE
    assert "nadie ha observado" in verificacion.evaluations[0].reason


@pytest.mark.asyncio
async def test_un_objetivo_sobre_contenido_sigue_siendo_unverifiable(tmp_path):
    """La limitación que el audit ya reconocía, fijada como test.

    `file_exists:notas.txt` acredita que el fichero EXISTE, no que ALEXIS entendiera su
    contenido. El vocabulario no llega más lejos, así que un criterio que sólo se
    puede comprobar leyendo el contenido del fichero no puede darse por cumplido: se
    conserva y queda en `insufficient_evidence`, y la misión NO cierra como
    COMPLETED aunque el archivo exista y `fs.read` funcione.
    """
    (tmp_path / "notas.txt").write_text("contenido real de notas\n", encoding="utf-8")

    criteria, status = normalize_criteria(
        "Analiza el archivo notas.txt y dime qué contiene", "", []
    )
    assert criteria == ["file_exists:notas.txt"]

    # El objetivo real ("dime qué contiene") NO es comprobable con el vocabulario
    # actual: se conserva el criterio, pero no puede satisfied.
    objetivo = "Analiza el contenido de notas.txt y dime qué dice"
    criteria = criteria + ["El análisis menciona las tres ideas del documento"]
    mission = MissionEngine().create(
        objetivo,
        MissionEnvelope(
            objective=objetivo,
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=list(ALLOWED_ACTIONS),
        ),
        success_criteria=criteria,
    )
    runtime, _ = _runtime(tmp_path)
    result = await runtime.run_mission(mission)

    assert result.state is not MissionState.COMPLETED, (
        "un criterio sobre el contenido del fichero no puede satisfiedse con file_exists"
    )
    assert status["verifiable"] == 1
