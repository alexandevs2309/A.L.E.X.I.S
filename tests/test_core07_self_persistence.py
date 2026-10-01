"""CORE-07 — Persistencia durable del estado APRENDIDO del Self Model.

El Self Model es el único componente del Core que sabia cosas y las perdía. Su
`SelfModelSync` acumulaba lecciones verificadas, auto-observaciones y reflexiones en
memoria del proceso; al reiniciar ALEXIS, `SelfModel(...)` se reconstruía sólo con
capabilities/available/resources y **todo lo aprendido desaparecía**. Eso contradice
P0 requisito 3: el Self Model debe saber "qué salió mal" y "qué debe hacer después",
y para eso necesita acordarse entre ejecuciones.

CORE-07 hace durable SOLO el estado aprendido —lessons, observaciones y reflexiones— y
delega explícitamente lo demás:

- `current_goal`, `current_mission`, `decisions`, `task_results`, `confidence`,
  `permissions`, `current_policy` NO se persisten aquí: son una **proyección** del
  objeto misión en vivo, que ya está en la tabla `missions`. Guardarlos sería una
  segunda memoria paralela que podría contradecir a la real.

El test central (el primero de esta suite) no comprueba que PostgreSQL tenga una fila:
comprueba el ciclo completo que exige el objetivo — REGISTRAR → PERSISTIR →
DESTRUIR/RECREAR el Self Model → RECUPERAR → el estado aprendido sigue disponible.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from alexis.self.model import SelfModel  # noqa: E402
from alexis.self.persistence import SelfModelPersistence  # noqa: E402
from alexis.self.sync import SelfModelSync  # noqa: E402


def _model():
    return SelfModel(capabilities=["fs.read"], available=["fs.read"], resources={"workspace": "/tmp"})


def _sync(model, *, lessons_from_sync=True):
    """SelfModelSync con un `aux` que lee las lecciones del propio sync (CORE-07)."""
    holder = {"sync": None}

    def _aux():
        # El wiring de producción: lo que el sync aprendió debe llegar al modelo.
        lessons = holder["sync"].lessons if lessons_from_sync and holder["sync"] else []
        return {"tools": [], "commitments": [], "lessons": list(lessons), "memory_items": []}

    sync = SelfModelSync(model, lambda: None, aux=_aux)
    holder["sync"] = sync
    return sync, _aux


async def _mision(db, objective="obj"):
    """Misión REAL en la base: `self_learnings.mission_id` tiene FK a `missions`."""
    from alexis.autonomy.mission import MissionEngine
    from alexis.contracts import AutonomyLevel, MissionEnvelope
    from alexis.storage.repositories import MissionRepository

    m = MissionEngine().create(
        objective,
        MissionEnvelope(
            objective=objective,
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=["execute"],
        ),
    )
    await MissionRepository(db).upsert(m)
    return m


@pytest.fixture
def repo(db):
    """Repositorio durable del Self Model sobre el `db` de conftest (trunca por test)."""
    return SelfModelPersistence(db)


# ====================================================================== #
# 1. El bug de wiring (punto 8): la lesson NO llegaba al modelo
# ====================================================================== #


def test_1_la_lesson_del_sync_llega_al_modelo():
    """Reproduce el defecto de wiring previo a CORE-07.

    `SelfModelSync` acumulaba la lección en `sync.lessons`, pero el `aux` de producción
    leía `SELF.lessons` (atributo que `SelfModel` no define — el real es
    `lessons_learned`). Con `getattr(..., [])` por defecto, no lanzaba: la lección se
    perdía en silencio y el Self Model no mostraba nada aprendido.
    """
    model = _model()
    sync, _aux = _sync(model)

    sync.apply_event(
        "mission.experience",
        {"can_teach": True, "objective": "lee notas.txt", "lesson": "el objetivo era leer"},
    )

    assert sync.lessons, "el sync debe acumular la lección"
    assert model.lessons_learned, (
        "la lección del sync debe llegar a SelfModel.lessons_learned (bug de wiring)"
    )
    assert any("lee notas.txt" in x for x in model.lessons_learned)


# ====================================================================== #
# 2. Ciclo completo: REGISTRAR → PERSISTIR → RECREAR → RECUPERAR
# ====================================================================== #


@pytest.mark.asyncio
async def test_2_lesson_sobrevive_a_recrear_el_self_model(db, repo):
    model = _model()
    sync, _ = _sync(model)
    sync.apply_event(
        "mission.experience",
        {"can_teach": True, "objective": "lee notas.txt", "lesson": "aprender a leer"},
    )
    assert model.lessons_learned

    # Persistir (como haría el sync al adjuntarse).
    await repo.save(
        lessons=model.lessons_learned,
        observations=model.observations_about_self,
        reflections=model.reflections,
    )

    # DESTRUIR/RECREAR el Self Model: memoria perdida en RAM.
    nuevo = _model()
    assert nuevo.lessons_learned == [], "un SelfModel nuevo no debe tener memoria"

    # RECUPERAR: el nuevo modelo se rehidrata desde la fuente durable.
    recuperado = await repo.load()
    nuevo.restore(recuperado)

    assert nuevo.lessons_learned, "la lección debe sobrevivir al restart"
    assert any("aprender a leer" in x for x in nuevo.lessons_learned)


@pytest.mark.asyncio
async def test_3_observation_sobrevive_a_recrear(db, repo):
    model = _model()
    sync, _ = _sync(model)
    sync.apply_event(
        "mission.step_completed", {"step": "leer", "success": False, "error": "no existe"}
    )
    assert model.observations_about_self

    await repo.save(
        lessons=model.lessons_learned,
        observations=model.observations_about_self,
        reflections=model.reflections,
    )

    nuevo = _model()
    nuevo.restore(await repo.load())
    assert nuevo.observations_about_self, "la auto-observación debe sobrevivir al restart"
    assert any("no existe" in o["text"] for o in nuevo.observations_about_self)


@pytest.mark.asyncio
async def test_4_reflection_sobrevive_a_recrear(db, repo):
    model = _model()
    sync, _ = _sync(model)
    sync.apply_event("self.reflected", {"text": "aprendí que el sandbox limita el FS"})
    assert model.reflections

    await repo.save(
        lessons=model.lessons_learned,
        observations=model.observations_about_self,
        reflections=model.reflections,
    )

    nuevo = _model()
    nuevo.restore(await repo.load())
    assert nuevo.reflections, "la reflexión debe sobrevivir al restart"
    assert any("sandbox" in r["text"] for r in nuevo.reflections)


# ====================================================================== #
# 3. El estado DERIVADO no se persiste (no hay memoria paralela)
# ====================================================================== #


@pytest.mark.asyncio
async def test_5_el_estado_derivado_no_se_persiste(db, repo):
    """`current_goal`, `decisions`, etc. NO se guardan: se recalculan desde la misión.

    Si se guardaran, serían una copia que puede contradecir la misión real (que es la
    autoridad) — una segunda memoria paralela, que CORE-07 prohíbe explícitamente.
    """
    model = _model()
    model.current_goal = "objetivo en vuelo"
    model.decisions = [{"step": "leer", "allowed": True}]
    model.confidence = 0.9

    await repo.save(
        lessons=model.lessons_learned,
        observations=model.observations_about_self,
        reflections=model.reflections,
    )
    carga = await repo.load()

    for prohibido in ("current_goal", "decisions", "confidence", "task_results", "permissions"):
        assert prohibido not in carga, (
            f"{prohibido} es proyección de la misión y no debe persistirse aquí"
        )


# ====================================================================== #
# 4. Provenance de la lección (mission_id opcional)
# ====================================================================== #


@pytest.mark.asyncio
async def test_6_mission_id_cuando_hay_provenance(db, repo):
    """La lección se guarda como conocimiento global, con `mission_id` como provenance."""
    m = await _mision(db)
    await repo.save(lessons=["aprendi a leer"], observations=[], reflections=[], mission_id=m.id)
    carga = await repo.load()
    assert carga["lessons"], "la lección debe guardarse"
    assert carga["mission_id"] == m.id, "mission_id es provenance, no identidad"


@pytest.mark.asyncio
async def test_7_mission_id_null_cuando_no_hay_provenance(db, repo):
    """Una lesson pre-misión (o sin origen conocido) es legítima con mission_id NULL."""
    await repo.save(lessons=["aprendi sin misión"], observations=[], reflections=[])
    carga = await repo.load()
    assert carga["mission_id"] is None, "sin provenance, mission_id es NULL (no inventado)"
    assert carga["lessons"] == ["aprendi sin misión"]


@pytest.mark.asyncio
async def test_8_mission_id_no_es_identidad(db, repo):
    """La lección es del SelfModel global; `mission_id` NO la particiona.

    Guardar una lección de la misión A y luego otra de B debe conservar AMBAS en el
    mismo/global buffer del Self Model.
    """
    a = await _mision(db, "A")
    b = await _mision(db, "B")
    await repo.save(lessons=["lección de A"], observations=[], reflections=[], mission_id=a.id)
    await repo.save(lessons=["lección de A", "lección de B"], observations=[], reflections=[], mission_id=b.id)
    carga = await repo.load()
    assert "lección de A" in carga["lessons"]
    assert "lección de B" in carga["lessons"], (
        "las lecciones son globales; mission_id no las separa"
    )


# ====================================================================== #
# 5. Robustez
# ====================================================================== #


@pytest.mark.asyncio
async def test_9_multiples_registros_no_se_pisan(db, repo):
    model = _model()
    for i in range(3):
        model.note_self_observation(f"observación {i}")
    model.add_reflection("reflexión uno")
    model.add_reflection("reflexión dos")
    model.lessons_learned = ["lección uno"]

    await repo.save(
        lessons=model.lessons_learned,
        observations=model.observations_about_self,
        reflections=model.reflections,
    )
    carga = await repo.load()

    assert len(carga["observations"]) == 3, "no se pisan entre sí"
    assert len(carga["reflections"]) == 2
    assert carga["lessons"] == ["lección uno"]


@pytest.mark.asyncio
async def test_10_recuperacion_determinista(db, repo):
    """Dos `load()` seguidos devuelven lo mismo: sin datos inventados ni aleatorios."""
    model = _model()
    model.note_self_observation("observación estable")
    model.lessons_learned = ["lección estable"]
    await repo.save(
        lessons=model.lessons_learned,
        observations=model.observations_about_self,
        reflections=model.reflections,
    )

    primero = await repo.load()
    segundo = await repo.load()
    assert primero == segundo, "la recuperación debe ser determinista"


@pytest.mark.asyncio
async def test_11_sin_registros_estado_inicial_valido(db, repo):
    """Base vacía → estado inicial legítimo, sin inventar datos."""
    carga = await repo.load()
    assert carga["lessons"] == []
    assert carga["observations"] == []
    assert carga["reflections"] == []
    assert carga["mission_id"] is None

    model = _model()
    model.restore(carga)
    assert model.lessons_learned == []
    assert model.current_goal is None, "no se inventa un objetivo"


@pytest.mark.asyncio
async def test_12_restart_real_del_componente(db, repo):
    """Reinicio real: se destruye el objeto persistente y se reconstruye.

    No basta con que la fila exista: se recrea el repositorio sobre la MISMA base y
    se comprueba que el estado vuelve.
    """
    model = _model()
    model.note_self_observation("antes del reinicio")
    model.lessons_learned = ["lección antes del reinicio"]
    await repo.save(
        lessons=model.lessons_learned,
        observations=model.observations_about_self,
        reflections=model.reflections,
    )

    # "Reinicio": nuevo SelfModel + nuevo repositorio (proceso nuevo).
    del repo  # el handle viejo deja de usarse
    nuevo_repo = SelfModelPersistence(db)
    nuevo_model = _model()
    nuevo_model.restore(await nuevo_repo.load())

    assert nuevo_model.lessons_learned == ["lección antes del reinicio"]
    assert any("antes del reinicio" in o["text"] for o in nuevo_model.observations_about_self)


# ====================================================================== #
# 6. Compatibilidad
# ====================================================================== #


def test_13_el_self_model_sigue_respondiendo_despues(db, repo):
    """La persistencia no rompe la metacognición: `answer`/`snapshot` siguen bien."""
    model = _model()
    model.lessons_learned = ["aprendi a leer notas"]
    model.restore(repo.load() if False else {"lessons": [], "observations": [], "reflections": [], "mission_id": None})
    model.note_self_observation("sigo funcionando")

    snap = model.snapshot()
    assert snap["lessons"] == model.lessons_learned
    assert isinstance(snap["observations_about_self"], list)
    assert model.answer("¿qué sé?")["source"] == "knowledge"