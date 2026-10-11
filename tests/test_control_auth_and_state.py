"""A-1 (control sin auth) y A-2/A-3 (guardias de estado).

Dos cosas que se mezclan aquí a propósito: un endpoint de control sin autenticación y un
campo de estado que cualquiera puede reasignar. Las dos son la misma clase de fallo —
**el poder de cambiar algo que no se comprueba al cambiarlo** — y por eso se prueban
juntas.

Las peticiones van por HTTP real contra la aplicación oficial montada en un puerto
efímero. No se llama al handler por dentro: una prueba que invoque `do_POST` saltaría
justo el enrutado y el `return` que corta antes de ensanchar el envelope, que es donde
está el fallo.
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request

import pytest

from alexis.contracts import (
    AutonomyLevel,
    Goal,
    InvalidStateTransitionError,
    Mission,
    MissionEnvelope,
    MissionState,
)


# --------------------------------------------------------------------------- #
# El servidor
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def servidor():
    """La app oficial, con PostgreSQL real, en un puerto efímero."""
    from apps.demo import app as app_module

    loop = asyncio.new_event_loop()
    hilo_loop = threading.Thread(target=loop.run_forever, daemon=True)
    hilo_loop.start()
    storage = app_module.init_storage(loop)
    if storage.get("mission") is None:
        pytest.skip("la API de control necesita PostgreSQL")
    runtime = app_module.build_runtime(loop=loop, storage=storage, workspace="/tmp")
    httpd = app_module.create_app(runtime)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, runtime, loop, storage

    # Teardown calcado del de `test_user_model.py`, que es el patrón que YA convive con
    # `test_core04_http_e2e` sin contaminar. Lo que se probó y se descartó, y por qué:
    #   - `stop_services()` + drenaje: limpian las tareas, pero PARAN singletons que otros
    #     ficheros de la suite siguen usando → fallo en un test ajeno.
    #   - Sólo drenaje: deja vivo el worker del pool; su error aflora como `unraisable
    #     exception` en un test posterior y tumba la suite en el fichero equivocado.
    # Betterar el aislamiento de ALEXIS está bien; colarse en el estado compartido de la
    # suite mientras se hace, no.
    httpd.shutdown()
    httpd.server_close()
    try:
        asyncio.run_coroutine_threadsafe(storage["db"].close(), loop).result(timeout=10)
    except Exception:  # noqa: BLE001
        pass

    # `build_runtime()` deja vivos `AsyncScheduler.run` y tres workers del pool de psycopg
    # (comprobado: 5 tareas al construir). Si el loop se para con ellas colgando, el
    # intérprete las destruye pendientes al salir y eso aparece como `unraisable
    # exception` en el test que esté corriendo en ese instante — con `-W error` tumba la
    # suite en un fichero que no ha fallado, y el síntoma cambiaba de test en cada
    # ejecución, que es la firma de un problema de estado compartido y no de una aserción.
    async def _drenar():
        actual = asyncio.current_task()
        for tarea in asyncio.all_tasks(loop):
            if tarea is not actual and not tarea.done():
                tarea.cancel()
        await asyncio.sleep(0)

    try:
        asyncio.run_coroutine_threadsafe(_drenar(), loop).result(timeout=10)
    except Exception:  # noqa: BLE001
        pass

    # `build_runtime()` instala un runtime OFICIAL ÚNICO por proceso. Sin soltarlo aquí, el
    # siguiente fichero que monte la app recibe `OfficialRuntimeNotRunning` — es lo que pasó
    # con `test_user_model.py`. No es un defecto del runtime: es lo que impide dos runtimes
    # en un mismo proceso, y `test_core04_http_e2e` lo respeta igual.
    app_module.set_official_runtime(None)

    # El loop se CIERRA de verdad, como hace `test_core04_http_e2e`: se para el hilo y
    # luego se cierra el loop. Sin `close()`, el intérprete destruye al final las tareas
    # que quedaran colgando y eso sale como `unraisable exception` en otro test. Con
    # `-W error` la suite falla donde no hay nada roto, y el fallo cambiaba de fichero en
    # cada ejecución.
    loop.call_soon_threadsafe(loop.stop)
    hilo_loop.join(timeout=10)
    try:
        loop.close()
    except Exception:  # noqa: BLE001 — si ya estaba cerrado, mejor que seguir
        pass


def _post(base, ruta, cuerpo=None, token=None):
    """POST real. Devuelve `(código, cuerpo)`, includedo el error HTTP."""
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = token
    peticion = urllib.request.Request(
        base + ruta, data=json.dumps(cuerpo or {}).encode(),
        headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(peticion, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _token_valido():
    from apps.demo import app as app_module

    return f"Bearer {app_module._token_del_proceso()}"


def _mision_pendiente(servidor):
    """Una misión real en `waiting_approval`, persistida y lista para aprobar."""
    _base, runtime, _loop, storage = servidor
    from alexis.autonomy.mission import MissionEngine
    from alexis.contracts import MissionEnvelope

    async def _crear():
        engine = MissionEngine()
        env = MissionEnvelope(
            objective="revisar notas", autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=["read"], forbidden_actions=[],
            approval_required=["destructive"], auto_approve=[],
            capabilities=["fs.read"],
        )
        m = engine.create("revisar notas", env)
        m.state = MissionState.WAITING_APPROVAL
        m.context["pending_approval"] = {"step": "leer"}
        await storage["mission"].upsert(m)
        # `/approve` resuelve por el registro EN MEMORIA (`RUNNING`), no por la base: una
        # misión que sólo está en PostgreSQL devuelve 409. Se registra en ambos sitios
        # para que el test exercised el camino real.
        runtime.running[m.id] = m
        return m.id

    return asyncio.run_coroutine_threadsafe(_crear(), _loop).result(timeout=10)


# --------------------------------------------------------------------------- #
# 1-3 — autenticación en las rutas de control
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("sufijo", ["/approve", "/deny", "/clarify"])
def test_01_sin_token_una_ruta_de_control_responde_401(servidor, sufijo):
    """Sin cabecera `Authorization` no se toca la misión.

    Se comprueba también que el envelope NO se ha ampliado: el 401 tiene que llegar antes
    del ensanchamiento, no después. Es el orden que hace que este hole exista.
    """
    base, runtime, loop, storage = servidor
    mission_id = _mision_pendiente(servidor)

    codigo, cuerpo = _post(base, f"/missions/{mission_id}{sufijo}")

    assert codigo == 401, f"{sufijo} respondió {codigo}, no 401"
    assert cuerpo.get("error") == "unauthorized"

    async def _envelope():
        repo = storage["mission"]
        m = await repo.get(mission_id)
        return m.envelope.allowed_actions

    acciones = asyncio.run_coroutine_threadsafe(_envelope(), loop).result(timeout=10)
    assert "execute" not in acciones, (
        "el envelope se amplió pese al 401: la autenticación va DESPUÉS del ensanchamiento"
    )


@pytest.mark.parametrize("sufijo", ["/approve", "/deny", "/clarify"])
def test_02_con_token_invalido_responde_403(servidor, sufijo):
    base, runtime, loop, storage = servidor
    mission_id = _mision_pendiente(servidor)

    codigo, cuerpo = _post(base, f"/missions/{mission_id}{sufijo}", token="Bearer no-es-el-token")

    assert codigo == 403, f"{sufijo} respondió {codigo}, no 403"
    assert cuerpo.get("error") == "forbidden"

    async def _envelope():
        m = await storage["mission"].get(mission_id)
        return m.envelope.allowed_actions

    acciones = asyncio.run_coroutine_threadsafe(_envelope(), loop).result(timeout=10)
    assert "execute" not in acciones, "el envelope se amplió con un token inválido"


def test_03_con_token_valido_aprueba_y_amplia_el_envelope(servidor):
    """Con token válido, `/approve` funciona y amplía `allowed_actions`.

    El ensanchamiento NO se elimina: es parte de la semántica de aprobar (un paso que
    requería `execute` no puede aprobarse sin que el envelope lo permita). Lo que se
    garantiza es que ocurre DESPUÉS de autenticar.
    """
    base, runtime, loop, storage = servidor
    mission_id = _mision_pendiente(servidor)

    codigo, cuerpo = _post(base, f"/missions/{mission_id}/approve", token=_token_valido())

    assert codigo == 200, f"con token válido respondió {codigo}: {cuerpo}"

    # `/approve` muta el objeto en memoria y encola; la fila se actualiza después. Se
    # reintenta unos instantes en vez de leer una vez y asumir: sin espera, el test
    # dependería de si la encolada ha，你怎么看 ya, que no es lo que se quiere probar.
    async def _estado():
        for _ in range(40):
            m = await storage["mission"].get(mission_id)
            if m is not None and "execute" in m.envelope.allowed_actions:
                return m.envelope.allowed_actions, sorted(
                    m.context.get("approved_step_ids", [])
                )
            await asyncio.sleep(0.05)
        m = await storage["mission"].get(mission_id)
        return (m.envelope.allowed_actions if m else []), []

    acciones, aprobados = asyncio.run_coroutine_threadsafe(_estado(), loop).result(timeout=10)
    assert "execute" in acciones, (
        f"con token válido el envelope debe ampliarse; llegó: {acciones}"
    )
    assert "leer" in aprobados, f"el paso aprobado debe quedar registrado; llegó: {aprobados}"


def test_04_rutas_que_no_son_de_control_no_exigen_token(servidor):
    """Guardia de la guardia: si `todo` exigiera token, la API sería inservible."""
    from apps.demo.app import _RUTA_ES_DE_CONTROL

    for ruta in ("/missions/m1/approve", "/missions/m1/deny?motivo=x",
                 "/missions/m1/clarify", "/missions/m1/approve/"):
        assert _RUTA_ES_DE_CONTROL(ruta) is True, ruta
    for ruta in ("/chat", "/missions", "/state", "/self", "/capabilities",
                 "/missions/m1", "/user/preferences"):
        assert _RUTA_ES_DE_CONTROL(ruta) is False, ruta


# --------------------------------------------------------------------------- #
# 4-5 — guardias de estado
# --------------------------------------------------------------------------- #


def _mision(state=MissionState.PENDING) -> Mission:
    env = MissionEnvelope(
        objective="objetivo", autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=["read"], forbidden_actions=[], approval_required=[],
        auto_approve=[],
    )
    m = Mission(
        id="m-estado",
        goal=Goal("objetivo", success_criteria=["la nota existe"]),
        envelope=env,
    )
    m.state = state
    return m


def _verificacion_valida(mission):
    """Una GoalVerification REAL: criterio satisfecho con evidencia de herramienta.

    No se puede poner `verified=True` a mano: `goal_is_confirmed()` exige que el criterio
    tenga evidencia `trusted` y con `source` de herramienta, que es justo lo que impide que
    una fila manipulada declare completada una misión que no corrió.
    """
    from alexis.cognition.goal_verification import (
        CriterionEvaluation,
        CriterionEvidence,
        CriterionStatus,
        GoalVerification,
    )

    from alexis.cognition.goal_verification import verification_subject

    return GoalVerification(
        objective=mission.goal.objective,
        # `subject` es la identidad de la verificación: misión, objetivo y criterios.
        # Sin él `subject_matches()` la rechaza —que es lo que impide reutilizar la
        # verificación de la misión A para completar la B.
        subject=verification_subject(
            mission, mission.goal.success_criteria,
        ),
        evaluations=[CriterionEvaluation(
            criterion=mission.goal.success_criteria[0],
            status=CriterionStatus.SATISFIED,
            reason="leída con fs.read",
            # `source` debe llevar el prefijo `tool:`. Un `trusted=True` solo no basta:
            # `trusted` es un booleano que cualquiera puede escribir, mientras que el
            # prefijo declara QUE observó el mundo.
            evidence=[CriterionEvidence(
                evidence_id="ev-1", source="tool:fs.read", grade="evidence",
                detail="notas.txt existe", trusted=True,
            )],
        )],
        verified=True,
        reason="criterio satisfecho con evidencia de herramienta",
    )


@pytest.mark.parametrize("reiniciante", [MissionState.RUNNING, MissionState.PENDING])
def test_05_un_estado_terminal_no_vuelve_a_estar_en_curso(reiniciante):
    """`FAILED` no vuelve a `RUNNING` ni a `PENDING`."""
    m = _mision(MissionState.RUNNING)
    m.state = MissionState.FAILED

    with pytest.raises(InvalidStateTransitionError):
        m.state = reiniciante


def test_05b_completed_tampoco_vuelve_a_estar_en_curso():
    """`COMPLETED` exige verificación para alcanzarse, y una vez alcanzado es terminal."""
    m = _mision(MissionState.RUNNING)
    m.goal_verification = _verificacion_valida(m)
    m.state = MissionState.COMPLETED

    with pytest.raises(InvalidStateTransitionError):
        m.state = MissionState.RUNNING


def test_06_run_pause_run_es_legal():
    """PAUSE sirve para eso: parar sin terminar la misión."""
    m = _mision(MissionState.RUNNING)

    m.state = MissionState.PAUSE
    assert m.state is MissionState.PAUSE

    m.state = MissionState.RUNNING
    assert m.state is MissionState.RUNNING, "PAUSE debe ser reversible: una pausa, no una parada"


def test_07_completed_sigue_requiriendo_verificacion_del_objetivo():
    """La guarda de estado NO relaja la invariante de verificación."""
    from alexis.contracts import UnverifiedGoalError

    m = _mision(MissionState.RUNNING)
    with pytest.raises(UnverifiedGoalError):
        m.state = MissionState.COMPLETED


def test_08_el_estado_se_puede_asignar_desde_un_dataclass_nuevo():
    """`mission_from_row()` construye con estado por defecto: la guarda no debe morder."""
    m = Mission(
        id="nueva", goal=Goal("o"),
        envelope=MissionEnvelope(
            objective="o", autonomy=AutonomyLevel.SUPERVISED, allowed_actions=["read"],
            forbidden_actions=[], approval_required=[], auto_approve=[],
        ),
    )
    assert m.state is MissionState.PENDING