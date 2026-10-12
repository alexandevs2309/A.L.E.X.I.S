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
import uuid

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
    # dependería de si la encolada ha levantado ya, que no es lo que se quiere probar.
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

# --------------------------------------------------------------------------- #
# 9 — el bucle del CORE obedece el estado escrito desde fuera
# --------------------------------------------------------------------------- #

def _mision_simple(mission_id, state, context=None):
    """Una Mission válida y mínima, con `goal` y `envelope` de verdad.

    No se puede construir con `objective=` a secas: `Mission` guarda un `Goal` y un
    `MissionEnvelope` estructurados. Se hace aquí para no repetirlo en cada prueba.
    """
    objetivo = "trabajar sin parar"
    return Mission(
        id=mission_id,
        goal=Goal(objective=objetivo),
        envelope=MissionEnvelope(objective=objetivo),
        state=state,
        context=dict(context or {}),
    )


def _runtime_minimo(real, cognitive, **overrides):
    """El runtime DE VERDAD con una sola pieza cambiada: el `cognitive`.

    Se clona el `__dict__` del runtime real en lugar de escribir un stub desde cero. Un
    stub tendría que declarar a mano cada atributo que toca `run_mission` (`extras`,
    `events`, `_close`, `_close_cycle`...) y fallaría por atributos que no tienen nada que
    ver con lo que se prueba: un fallo de la prueba, no del código.
    """
    clon = type(real).__new__(type(real))
    clon.__dict__.update(real.__dict__)
    clon.cognitive = cognitive
    for nombre, valor in overrides.items():
        setattr(clon, nombre, valor)
    return clon


class _Outcome:
    """Un `StepOutcome` que no decide nada.

    `__getattr__` devuelve None para lo que no esté declarado porque el bucle consulta
    muchos atributos del resultado y aquí no se prueba ninguno de ellos: lo que se prueba
    es que el bucle OBEDECE el estado de fuera. Declarar los que hacen falta a mano
    sería endless; fingirlos es más honesto que inventarles un comportamiento.
    """

    done = False
    mission_state = None
    requires_approval = False
    replan = False
    result = None
    action = None

    def __init__(self, knowledge):
        self.knowledge = knowledge

    def to_dict(self):
        return {"stub": True}

    def __getattr__(self, _nombre):
        return None


class _CognitoFalso:
    """Sustituye al cognitivo: devuelve el `StepOutcome` que le pidan y nada más.

    Lo que se prueba aquí no es pensar, es obedecer: que el bucle mire el estado externo
    y salga. Por eso el cognitivo es un borrador y el `mission_repo` es el de PostgreSQL
    de verdad — es ahí donde se escribe el `PAUSE` que el operador pondría.
    """

    def __init__(self, en_paso=None, al_salir=None):
        self.en_paso = en_paso      # se llama en cada vuelta: puede escribir en la base
        self.al_salir = al_salir    # se llama en `knowledge_for`: devuelve `completed_steps`
        self.vueltas = 0

    def knowledge_for(self, _mission):
        completed = self.al_salir() if self.al_salir else []
        return type("K", (), {"completed_steps": completed})()

    def pending_steps(self, *_a, **_k):
        return []

    def store_knowledge(self, *_a, **_k):
        # SÍNCRONO a propósito: el runtime lo llama sin await (runtime.py:1516). Si esta
        # doble fuera async, Python crearía una corrutina que nadie espera y saltaría el
        # RuntimeWarning que con -W error tumba la suite.
        return None

    def drain_rejections(self, *_a, **_k):
        return []

    async def step(self, mission, _knowledge, pending_steps=None, plan=None):
        self.vueltas += 1
        if self.en_paso is not None:
            await self.en_paso(self.vueltas)
        return _Outcome(self.knowledge_for(mission))


def test_09_el_bucle_suelta_la_mision_al_ver_que_la_pausaron(servidor):
    """La prueba que justifica todo el cambio.

    Reproduce lo que hace el operador: se escribe PAUSE en la fila mientras el bucle tiene
    la misión en memoria. Si el bucle no releyera, seguiría trabajando y esta prueba no
    haría falta. Si releyera pero ignorase el estado, no terminaría nunca. Sólo pasa si
    hace las dos cosas.
    """
    _base, runtime, loop, storage = servidor
    real = runtime.runtime  # el adaptador OfficialRuntime envuelve al AlexisRuntime real

    async def _ciclo():
        mission = _mision_simple("pausa-de-prueba", MissionState.RUNNING)
        await storage["mission"].upsert(mission)

        async def _pausa_al_tercera_vuelta(vuelta):
            if vuelta == 3:
                await storage["mission"].upsert(
                    _mision_simple(mission.id, MissionState.PAUSE,
                                   {"paused_reason": "el operador"})
                )

        llamadas = {"topics": [], "close": 0}

        async def _commit(_m, topic=None, payload=None):
            # El bucle hace commit en CADA paso, así que contar commits no dice nada. Lo
            # que tiene que aparecer es el commit de la pausa y sólo uno.
            llamadas["topics"].append(topic)

        async def _close(_m, _state, *_a, **_k):
            llamadas["close"] += 1

        cog = _CognitoFalso(en_paso=_pausa_al_tercera_vuelta)
        clon = _runtime_minimo(real, cog, _commit=_commit, _close=_close)
        salida = await type(real)._run_cognitive(clon, mission, None, 0)
        return salida, cog.vueltas, llamadas

    salida, vueltas, llamadas = asyncio.run_coroutine_threadsafe(
        _ciclo(), loop).result(timeout=30)

    assert salida.state is MissionState.PAUSE, "el bucle debe adopting el PAUSE de la fila"
    assert vueltas == 3, f"debe trabajar hasta la pausa y parar ahí (hizo {vueltas})"
    pausas = [t for t in llamadas["topics"] if t == "mission.paused"]
    assert len(pausas) == 1, f"la pausa se registra una vez (vi {llamadas['topics']})"
    assert llamadas["close"] == 0, "pausar NO cierra la misión: queda viva para reanudarla"


def test_10_stop_es_oido_igual_que_pause_pero_si_la_cierra(servidor):
    """`STOPPED` se obedece igual, con la diferencia de que termina la misión."""
    _base, runtime, loop, storage = servidor
    real = runtime.runtime

    async def _ciclo(estado_externo):
        mission = _mision_simple("stop-de-prueba", MissionState.RUNNING)
        await storage["mission"].upsert(mission)

        async def _para(_vuelta):
            await storage["mission"].upsert(_mision_simple(mission.id, estado_externo))

        cerrados = {"n": 0}

        async def _close(_m, _state, *_a, **_k):
            cerrados["n"] += 1

        async def _commit(*_a, **_k):
            # NOop a propósito: aquí se prueba sólo la guarda. El `_commit` real escribe
            # la misión EN MEMORIA, y eso abre una ventana que esta prueba no debe
            # medir — está medida aparte, en `test_13`.
            return None

        cog = _CognitoFalso(en_paso=_para)
        clon = _runtime_minimo(real, cog, _close=_close, _commit=_commit)
        salida = await type(real)._run_cognitive(clon, mission, None, 0)
        return salida, cerrados

    salida, cerrados = asyncio.run_coroutine_threadsafe(
        _ciclo(MissionState.STOPPED), loop).result(timeout=30)

    assert salida.state is MissionState.STOPPED
    assert cerrados["n"] == 1, "parar sí cierra la misión, a diferencia de pausar"


def test_11_la_guarda_solo_mira_los_finales_que_pone_el_operador(servidor):
    """La trampa de esta técnica, cerrada a propósito y por tabla.

    El bucle pone él mismo `completed`, `failed`, `waiting_approval`... al normalizar. Si la
    guarda mirara cualquiera de esos estados, leería su propia escritura y se frenaría a
    mitad del trabajo. El invariante es exacto: de todos los estados posibles, sólo dos
    paran el bucle.
    """
    _base, runtime, loop, storage = servidor
    real = runtime.runtime

    async def _ciclo(estado_externo):
        mission = _mision_simple("tabla-estados", MissionState.RUNNING)
        await storage["mission"].upsert(mission)
        # El estado se escribe por SQL y no con un `Mission(...)`: la puerta de
        # `COMPLETED` no deja construir una misión en ese estado, y en una fila real
        # importada de otra versión sí puede aparecer. La pregunta de esta prueba es
        # qué hace el bucle SI LA FILA DICE X, y la fila dice lo que dice.
        await storage["mission"].db.execute(
            "UPDATE missions SET state = %(estado)s WHERE id = %(id)s",
            {"estado": estado_externo.value, "id": mission.id},
        )

        cog = _CognitoFalso()
        clon = _runtime_minimo(real, cog)
        return await type(real)._abandonada_externamente(clon, mission.id)

    deben_parar = {MissionState.PAUSE, MissionState.STOPPED}
    for estado in MissionState:
        veredicto = asyncio.run_coroutine_threadsafe(
            _ciclo(estado), loop).result(timeout=30)
        if estado in deben_parar:
            assert veredicto is True, f"{estado.value} debe sacar la misión del bucle"
        else:
            assert veredicto is False, (
                f"{estado.value} NO debe parar el bucle: es un estado que el propio "
                f"bucle se pone y se frenaría a sí mismo"
            )


def test_12_el_engine_pausa_reanuda_y_no_inventa_estados():
    """Semántica de `MissionEngine`: pausar deja viva la misión, reanudar la devuelve."""
    from alexis.autonomy.mission import MissionEngine

    engine = MissionEngine()
    m = _mision_simple("engine", MissionState.RUNNING)

    engine.pause(m, "el operador")
    assert m.state is MissionState.PAUSE
    assert m.context["paused_reason"] == "el operador", "el motivo tiene que sobrevivir"

    engine.resume(m)
    assert m.state is MissionState.PENDING, "se reencola; el worker la pondrá en marcha"
    assert "paused_reason" not in m.context

    # Reanudar algo que no estaba pausado no inventa un cambio de estado.
    engine.resume(m)
    assert m.state is MissionState.PENDING

    # Reanudar una misión que NO está pausada no inventa un cambio de estado. Se usa
    # RUNNING y no un terminal porque un terminal sin GoalVerification no se puede ni
    # construir: la puerta de COMPLETED lo impide, que es exactamente lo que hay que
    # respetar al escribir la prueba.
    m2 = _mision_simple("engine-2", MissionState.RUNNING)
    assert engine.resume(m2) is None, "no se puede reanudar lo que no está pausado"
    assert m2.state is MissionState.RUNNING


def test_13_el_commit_del_bucle_tapa_un_stop_externo(servidor):
    """Una limitación REAL y medida, no una contraprueba para dejar todo en verde.

    La guarda lee el estado al PRINCIPIO de cada vuelta. Si el operador escribe `stopped`
    justo después de esa lectura, el `_commit` del final de esa vuelta —que escribe la
    misión EN MEMORIA— vuelve a poner `running` y el `stopped` se pierde.

    No es un fallo del diseño: la ventana es de un paso. Pero el `stopped` que se pierde
    es un `stopped` de verdad, así que queda como limitación conocida y no como bug
    oculto. El arreglo limpio es que `_commit` respete los finales externos, y eso toca
    TODO el runtime: es una decisión, no un detalle.

    Se mide el MECANISMO (el commit tapa el stop) y no el bucle entero: correr el bucle
    para demostrar que se cuelga sería lento y frágil, y el mecanismo es lo que importa.
    """
    _base, runtime, loop, storage = servidor
    real = runtime.runtime

    async def _ciclo():
        mission = _mision_simple("ventana", MissionState.RUNNING)
        await storage["mission"].upsert(mission)

        # 1. El operador para la misión.
        await storage["mission"].db.execute(
            "UPDATE missions SET state = 'stopped' WHERE id = %(id)s", {"id": mission.id}
        )
        antes = (await storage["mission"].get(mission.id)).state

        # 2. El bucle, que sigue con SU copia en memoria (RUNNING), hace commit.
        await real._commit(mission)

        # 3. ¿Y ahora qué dice la fila?
        despues = (await storage["mission"].get(mission.id)).state
        return antes, despues

    antes, despues = asyncio.run_coroutine_threadsafe(_ciclo(), loop).result(timeout=30)

    assert antes is MissionState.STOPPED, "el operador sí pudo parar la misión"
    assert despues is MissionState.RUNNING, (
        "el commit del bucle tapó el stop: por eso la guarda mira al PRINCIPIO de la "
        "vuelta y por eso existe una ventana de un paso"
    )


# --------------------------------------------------------------------------- #
# 14 — el operador puede pausar, reanudar y parar desde la web
# --------------------------------------------------------------------------- #

def _mision_registrada(servidor, state):
    """Misión real, persistida y en el registro EN MEMORIA que usa la API."""
    _base, runtime, _loop, storage = servidor

    async def _crear():
        # Id único por llamada: con un id fijo, pedir dos misiones en el mismo estado
        # machacaba la primera del registro y la prueba mentía.
        mission = _mision_simple(f"mision-{uuid.uuid4().hex[:12]}", state)
        await storage["mission"].upsert(mission)
        runtime.running[mission.id] = mission
        return mission.id

    return asyncio.run_coroutine_threadsafe(_crear(), _loop).result(timeout=10)


@pytest.mark.parametrize("sufijo", ["/pause", "/resume", "/stop"])
def test_14_las_rutas_de_ciclo_de_vida_tambien_exigen_token(servidor, sufijo):
    """No por prudencia: cambiar el estado de una misión en marcha ES poder sobre ella.

    Si `/pause` quedara abierta, no serviría de nada haber puesto llave a `/approve`.
    """
    _base, _runtime, _loop, _storage = servidor
    mission_id = _mision_registrada(servidor, MissionState.RUNNING)

    codigo, _ = _post(_base, f"/missions/{mission_id}{sufijo}", {"reason": "prueba"})
    assert codigo == 401, f"{sufijo} sin token tiene que ser 401, no {codigo}"


def test_15_pausar_pone_pause_y_reanudar_devuelve_a_pending(servidor):
    _base, _runtime, _loop, _storage = servidor
    token = _token_valido()
    mission_id = _mision_registrada(servidor, MissionState.RUNNING)

    codigo, cuerpo = _post(_base, f"/missions/{mission_id}/pause",
                           {"reason": "bajar la tensión"}, token=token)
    assert codigo == 200, cuerpo
    assert cuerpo["state"] == "pause", "pausar tiene que dejar la misión en pause"

    # Reanudar sin estar pausada no es un error de servidor: es un conflicto (409).
    otra = _mision_registrada(servidor, MissionState.RUNNING)
    codigo, cuerpo = _post(_base, f"/missions/{otra}/resume", {}, token=token)
    assert codigo == 409, f"reanudar lo que no está pausado es 409, no {codigo}: {cuerpo}"

    codigo, cuerpo = _post(_base, f"/missions/{mission_id}/resume", {}, token=token)
    assert codigo == 200, cuerpo
    assert cuerpo["state"] == "pending", (
        f"reanudar deja la misión reencolada, no 'running': el worker es quien la "
        f"pone en marcha, y decir 'running' aquí sería mentir (vi {cuerpo['state']})"
    )


def test_16_parar_es_distinto_de_pausar(servidor):
    _base, _runtime, loop, storage = servidor
    token = _token_valido()
    mission_id = _mision_registrada(servidor, MissionState.RUNNING)

    codigo, cuerpo = _post(_base, f"/missions/{mission_id}/stop", {"reason": "fin"}, token=token)
    assert codigo == 200, cuerpo
    assert cuerpo["state"] == "stopped"

    # `payload()` es la vista resumida y no trae `results`; el motivo se comprueba en la
    # fila, que es donde lo guarda el motor.
    async def _resultados():
        return list((await storage["mission"].get(mission_id)).results)

    resultados = asyncio.run_coroutine_threadsafe(_resultados(), loop).result(timeout=10)
    assert {"stopped": "fin"} in resultados, f"el motivo queda registrado (vi {resultados})"


def test_17_pausar_escribe_en_la_base_y_no_solo_en_memoria(servidor):
    """Si sólo se guardara en memoria, el bucle —que vive en otro sitio— no lo vería."""
    _base, runtime, loop, storage = servidor
    token = _token_valido()
    mission_id = _mision_registrada(servidor, MissionState.RUNNING)

    _post(_base, f"/missions/{mission_id}/pause", {"reason": "prueba"}, token=token)

    async def _leer():
        fila = await storage["mission"].get(mission_id)
        return fila.state, fila.context.get("paused_reason")

    estado, motivo = asyncio.run_coroutine_threadsafe(_leer(), loop).result(timeout=10)
    assert estado is MissionState.PAUSE
    assert motivo == "prueba", "el motivo tiene que sobrevivir en la fila"
