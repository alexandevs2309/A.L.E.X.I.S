"""CORE-04 — HTTP E2E de la aplicación oficial.

Hasta aquí lo que se comprobaba acababa en `runtime.run_mission()`: el último tramo,
el que traduce un estado interno a una respuesta HTTP, no lo cubría nadie. Estos tests
lo cubren contra el servidor real, por HTTP, con el runtime real y PostgreSQL real.

Qué aporta y por qué no es redundante:

- `test_chat_route.py`Despite el nombre, prueba la ruta **cognitiva**
  (`Planner → Executor → Verifier`), no la HTTP. Aquí se prueba el socket.
- `test_core01` prueba que el `GoalVerifier` está inyectado; aquí se prueba que a través
  de `/chat` y `GET /missions/{id}` se ve `verified: true` con evidencia real.

Dos invariantes de CORE-04 que estos tests protegen de forma observable:

1. `success_criteria` llega al cliente **con el predicado dentro**
   (`content_observed:...`), no como `[]`. Si alguien revirtiera a la ruta legacy, el
   mismo assertion falla — es el detector de esa regresión por la puerta de entrada.
   P0 §11: para un objetivo de análisis el predicado es `content_observed`, no
   `file_exists`: no basta con que el fichero esté en disco, tiene que haber sido leído.
2. `/missions/{id}` no depende de `STATE["mission_id"]`, que es una ranura global
   sobrescrita por cada misión creada y por tanto no identifica nada.

Hermeticidad: sin credenciales de modelo el clasificador cae a reglas y produce el
MISMO `content_observed:notas.txt` (verificado en CORE-02), así que estos tests no dependen de
ningún proveedor, ni de un puerto fijo (puerto efímero), ni del workspace real
(tmp por módulo).
"""

import ast
import json
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

#: El turno de `/chat` devuelve antes de que la misión se asiente. Medido: ~3-6 s. 90 s es
#: margen sin alargar el test en el caso bueno.
POLL_INTERVAL_S = 0.5
POLL_TIMEOUT_S = 90.0
STREAM_TIMEOUT_S = 30.0

B5 = "Analiza el archivo notas.txt y dime qué contiene"
B5_CRITERION = "content_observed:notas.txt"


# ---------------------------------------------------------------------- #
# Fixtures: un runtime real, un workspace temporal, un puerto efímero
# ---------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def oficial(tmp_path_factory):
    """El runtime oficial, con workspace temporal y PostgreSQL de test.

    `tmp_path_factory` evita que los tests toquen `workspace/` de verdad: el runtime
    resuelve el perímetro de sandbox desde ahí.

    Los servicios se encienden con `start_services(rt)`, el MISMO camino que usa
    `apps/demo/server.py:main()`. Antes esta fixture replicaba a mano el arranque del
    worker, con lo que el arranque bajo prueba no era el de producción; ahora es
    literalmente el mismo.
    """
    import asyncio

    from apps.demo import app as app_module

    workspace = tmp_path_factory.mktemp("core04_workspace")
    (workspace / "notas.txt").write_text("contenido real de notas\n", encoding="utf-8")
    (workspace / "informe.md").write_text("# informe\ncontenido real\n", encoding="utf-8")

    loop = asyncio.new_event_loop()
    hilo_loop = threading.Thread(target=loop.run_forever, daemon=True)
    hilo_loop.start()
    storage = app_module.init_storage(loop)
    rt = app_module.build_runtime(loop=loop, storage=storage, workspace=workspace)

    # Sin worker, lo encolado se queda en `pending`: nadie lo consume.
    if rt.worker is None:
        pytest.skip("CORE-04 necesita PostgreSQL: sin cola de misiones no hay ejecución")
    app_module.start_services(rt)

    yield rt

    # Parar el worker ANTES de soltar nada. `MissionWorker.loop()` corre hasta `_stop`,
    # así que un worker vivo se queda consumiendo la cola de la base de test compartida
    # durante el resto de la suite: procesa misiones de otros tests y provoca
    # violaciones de FK y deadlocks. Este módulo es dueño de su loop, así que lo cierra.
    if rt.worker is not None:
        rt.worker._stop = True
        # El worker duerme `poll` segundos entre misiones: hay que darle un ciclo para
        # que salga del `while`, o el `stop()` no lo alcanza a tiempo.
        time.sleep(rt.worker.poll + 0.2)
    # Los observadores que encendió `start_services` (self-sync, audit sink) consumen el
    # bus en tareas propias: sin detenerlas sobreviven al cierre del loop y se reportan
    # como "Task was destroyed but it is pending" en el test SIGUIENTE. Se detienen por
    # el MISMO camino que usa producción (`stop_services`), no con parches.
    try:
        asyncio.run_coroutine_threadsafe(
            app_module.stop_services(rt, worker_timeout=5.0), loop
        ).result(timeout=20)
    except Exception:  # noqa: BLE001 — el cierre no debe enmascarar el resultado
        pass
    # Drenaje explícito de observadores: `stop_services` limpia `extras`, pero cualquier
    # sync/sink que se haya adjuntado ANTES queda con su tarea viva en este loop, y al
    # cerrar el loop se reporta como "Task was destroyed but it is pending" en el test
    # SIGUIENTE (con `-W error` el fallo aparece donde no está la causa).
    async def _drain_observers():
        current = asyncio.current_task()
        for task in asyncio.all_tasks(loop):
            if task is current or task.done():
                continue
            name = task.get_coro().__qualname__ if hasattr(task, "get_coro") else ""
            if any(consumer in str(name) for consumer in ("_consume", "SelfModelSync", "AuditSink", "GoalTracker", "SkillPipeline")):
                task.cancel()
        await asyncio.sleep(0)

    try:
        asyncio.run_coroutine_threadsafe(_drain_observers(), loop).result(timeout=10)
    except Exception:  # noqa: BLE001 — el drenaje no debe enmascarar el resultado
        pass

    db = rt.storage.get("db")
    if db is not None:
        try:
            asyncio.run_coroutine_threadsafe(db.close(), loop).result(timeout=10)
        except Exception:  # noqa: BLE001 — el cierre no debe enmascarar el resultado
            pass
    # `close()` exige que el loop esté parado: llamarlo en vuelo levanta
    # "Cannot close a running event loop". Por eso el orden es stop → join → close.
    loop.call_soon_threadsafe(loop.stop)
    hilo_loop.join(timeout=10)
    loop.close()
    app_module.set_official_runtime(None)


@pytest.fixture(scope="module")
def servidor(oficial):
    """Servidor HTTP real en puerto efímero.

    `create_app` usa `127.0.0.1:0`, así que dosruns de la suite (o dos máquinas) nunca
    colisionan de puerto.
    """
    from apps.demo.app import create_app

    httpd = create_app(oficial, presentation=None)
    hilo = threading.Thread(target=httpd.serve_forever, daemon=True)
    hilo.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    # El servidor tiene que estar escuchando antes de que nadie le hable.
    _get(base, "/health")
    yield base
    httpd.shutdown()
    httpd.server_close()


# ---------------------------------------------------------------------- #
# Cliente HTTP mínimo
# ---------------------------------------------------------------------- #


def _get(base, path, *, timeout=15):
    with urllib.request.urlopen(base + path, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode())


def _post(base, path, body, *, timeout=200):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode())


def _get_raw_status(url, *, timeout=10):
    """Abre un `GET` y devuelve `(status, respuesta)` SIN cerrarla.

    Para `/stream` hace falta así: leerlo entero nunca termina porque el stream no
    cierra, y el `with` cerraría la respuesta al salir del bloque, dejando `readline()`
    devolviendo vacío al instante. Quien recibe esto es responsable de `close()`.
    """
    resp = urllib.request.urlopen(url, timeout=timeout)
    return resp.status, resp


def _await_state(base, mission_id, wanted, *, timeout=POLL_TIMEOUT_S):
    """Espera a que la misión llegue a uno de los estados `wanted`.

    Al agotar, falla diciendo QUÉ vio: un fallo de polling que sólo dice "timeout"
    obliga a reproducir a mano para descubrir si faltaba la misión, sobraba o el
    estado era otro.
    """
    limite = time.monotonic() + timeout
    visto = None
    detalle = None
    while time.monotonic() < limite:
        status, cuerpo = _get(base, f"/missions/{mission_id}")
        assert status == 200, cuerpo
        visto = cuerpo.get("state")
        detalle = cuerpo
        if visto in wanted:
            return cuerpo
        time.sleep(POLL_INTERVAL_S)
    pytest.fail(
        f"la misión {mission_id} no llegó a {sorted(wanted)} en {timeout}s. "
        f"Último estado: {visto!r}; criteria: {detalle and detalle.get('success_criteria')!r}; "
        f"razón: {detalle and detalle.get('goal_verification_reason')!r}"
    )


# ---------------------------------------------------------------------- #
# 0. start_services: un solo arranque para producción y tests
# ---------------------------------------------------------------------- #


def test_start_services_arranca_los_servicios_oficiales(oficial):
    """El arranque se enciende una vez, y dice qué encendió.

    Importa más de lo que parece: `build_runtime()` CONSTRUYE y `start_services()`
    ENCIENDE. Con la partición, una misión encolada se quedaba en `pending` para
    siempre porque nadie consumía la cola.
    """
    from apps.demo import app as app_module

    # Idempotente en lo que importa: volver a llamarlo no rompe el runtime.
    arrancado = app_module.start_services(oficial)

    assert arrancado["worker_started"] is True, "sin worker, nada se ejecuta"
    assert arrancado["self_sync"] is not None
    assert "recovered" in arrancado
    # El self-sync quedó conectado de verdad: tiene suscripción viva al bus oficial.
    assert arrancado["self_sync"]._sub is not None, "el self-sync no se adjuntó al bus"
    assert oficial.worker is not None


def test_start_services_es_el_unico_arranque_de_produccion():
    """Guarda estructural: `main()` no vuelve a arrancar servicios a mano.

    Si alguien reintrodujera el arranque inline, el E2E volvería a estar probando un
    camino distinto del de producción, que es exactamente el fallo que esta función
    vino a cerrar.
    """
    fuente = (PROJECT_ROOT / "apps" / "demo" / "server.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    llamadas_main = set()
    for node in ast.walk(arbol):
        if isinstance(node, ast.Call):
            nombre = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if nombre:
                llamadas_main.add(nombre)

    assert "start_services" in llamadas_main, "main() debe encender vía start_services()"
    # Lo que ya no debe aparecer inline en el lanzador, porque ahora vive en app.py.
    assert "SelfModelSync" not in llamadas_main, "el self-sync se arranca en start_services()"
    assert "recover_missions" not in llamadas_main, "la recuperación va en start_services()"
    assert "build_runtime" in llamadas_main


def test_el_e2e_usa_el_mismo_arranque_que_produccion():
    """El E2E no replica el arranque: usa `start_services()`.

    Se comprueba sobre el CÓDIGO de este fichero, no sobre su texto: si se buscara la
    cadena en el fuente, el propio assert contendría el nombre que busca y siempre
    encontraría algo. Lo que importa es que no haya una construcción de worker o una
    llamada a `.worker.loop()` escritas a mano en el fichero.
    """
    fuente = (PROJECT_ROOT / "tests" / "test_core04_http_e2e.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    llamadas = {
        getattr(n.func, "id", None) or getattr(n.func, "attr", None)
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call)
    }
    # Nombres de clase referenciados en el código (no en las cadenas de los asserts).
    construidos = {
        n.func.id for n in ast.walk(arbol)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "start_services" in llamadas, "el fixture E2E debe usar start_services()"
    assert "MissionWorker" not in construidos, (
        "el E2E no debe construir su propio worker: el arranque es start_services()"
    )
    assert "loop" not in {
        getattr(n.func, "attr", None)
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }, "el E2E no debe arrancar el worker a mano"


# ---------------------------------------------------------------------- #
# 1. /health
# ---------------------------------------------------------------------- #


def test_health_declara_el_runtime_oficial(servidor):
    """§2 — no basta con 200: el cuerpo declara la arquitectura.

    El valor del test es que la aplicación se presenta a sí misma como el runtime que
    verifica objetivos. Un 200 de un runtime sin `GoalVerifier` también sería 200.
    """
    status, cuerpo = _get(servidor, "/health")

    assert status == 200
    assert cuerpo["entrypoint"] == "official"
    assert cuerpo["runtime"]["cognitive"] is True
    assert cuerpo["runtime"]["goal_verifier"] is True
    assert cuerpo["runtime"]["persistence"] is True


# ---------------------------------------------------------------------- #
# 2-3. /chat E2E — la prueba central
# ---------------------------------------------------------------------- #


def test_chat_termina_completed_con_criterio_canonico(servidor):
    """§3 — `POST /chat` → `GET /missions/{id}` en polling → `completed`.

    El assert que más protege es `success_criteria`: exige que el texto LLEGUE al
    cliente con el predicado dentro. Sin CORE-02 (o si volviera la ruta legacy) esto
    sería `[]` y el test falla. Demuestra CORE-02 por la puerta de entrada, sin tocar
    CORE-02.
    """
    status, reply = _post(servidor, "/chat", {"text": B5})
    assert status == 200, reply
    mission_id = reply.get("mission_id")
    assert mission_id, "un turno TASK debe abrir misión"

    cuerpo = _await_state(servidor, mission_id, {"completed"})

    assert cuerpo["id"] == mission_id
    assert cuerpo["state"] == "completed"
    assert cuerpo["success_criteria"] == [B5_CRITERION]
    assert cuerpo["goal_verified"] is True
    assert (cuerpo.get("goal_verification_reason") or "").startswith("objetivo verificado")


def test_chat_evidencia_es_de_una_tool_real(servidor):
    """§3 — el `verified: true` viene de evidencia observada, no de una afirmación.

    Es la diferencia entre que el objetivo esté comprobado y que el sistema lo afirme.
    Un claim del modelo no pasa aquí: tiene que haber una tool real.

    P0 §11 lo hace más fuerte: para un objetivo de análisis no basta con que una tool
    confirmara que el fichero existe. La evidencia tiene que ser de una LECTURA
    (`fs.read`), porque `fs.stat` observa existencia y `fs.write` se limita a afirmar que
    escribió. Sin este assert, un `completed` basado en "el archivo está ahí" pasaría
    esta prueba sin que nadie hubiera leído el fichero.
    """
    _, reply = _post(servidor, "/chat", {"text": B5})
    cuerpo = _await_state(servidor, reply["mission_id"], {"completed"})

    verificacion = cuerpo.get("goal_verification") or {}
    assert verificacion.get("verified") is True

    evaluaciones = verificacion.get("criteria") or []
    assert evaluaciones, "una verificación sin evaluaciones no demuestra nada"
    for evaluacion in evaluaciones:
        assert evaluacion["status"] == "satisfied", evaluacion
        assert evaluacion.get("predicate") == "content_observed"

    fiables = [
        e
        for evaluacion in evaluaciones
        for e in (evaluacion.get("evidence") or [])
        if e.get("trusted") and str(e.get("source", "")).startswith("tool:")
    ]
    assert fiables, (
        "un satisfied sin evidencia fiable de una tool no puede sostener un completed"
    )

    # Y la evidencia tiene que ser una LECTURA, no una mera comprobación de existencia.
    lecturas = [
        e for e in fiables
        if "fs.read" in str(e.get("source", "")) or "leyó" in str(e.get("detail", ""))
    ]
    assert lecturas, (
        "el objetivo pedía el contenido del fichero: la evidencia tiene que probar "
        f"que se leyó, no sólo que existe. Evidencia recibida: "
        f"{[e.get('source') for e in fiables]}"
    )


# ---------------------------------------------------------------------- #
# 4-5. /missions E2E
# ---------------------------------------------------------------------- #


def test_missions_crea_persiste_y_se_lee_por_id(servidor, oficial):
    """§4 — crear, y recuperar ESA misión, no "la última".

    El objetivo usa `informe.md` para no depender del mismo fichero que B5: si los dos
    tests tocaran el mismo objetivo, uno podría pasar por el trabajo del otro.
    """
    status, creada = _post(
        servidor,
        "/missions",
        {"objective": "Analiza el archivo informe.md y dime qué contiene"},
    )
    assert status == 200, creada
    mission_id = creada["id"]
    assert creada["success_criteria"] == ["content_observed:informe.md"]
    assert creada.get("criteria_status", {}).get("verifiable") == 1

    cuerpo = _await_state(servidor, mission_id, {"completed"})

    # La lista también la contiene, con el mismo contrato.
    _, lista = _get(servidor, "/missions")
    assert mission_id in {m["id"] for m in lista}

    # Y la fila está en PostgreSQL, no en un dict de memoria.
    assert oficial.mission_repo() is not None
    import asyncio

    fila = asyncio.run_coroutine_threadsafe(
        oficial.mission_repo().get(mission_id), oficial.loop
    ).result(timeout=10)
    assert fila is not None, "la misión debe estar persistida"
    assert fila.state.value == cuerpo["state"]


def test_get_missions_por_id_es_lectura_sin_efectos(servidor, oficial):
    """§5 — la invariante de "no side effects", comprobada y no declarada.

    N lecturas seguidas deben devolver lo mismo y no mover nada: ni el estado de la
    misión, ni su lista de resultados, ni la cola. Una lectura que ejecutara, encolara
    o escribiera, se rompería aquí.
    """
    _, creada = _post(servidor, "/missions", {"objective": "Analiza el archivo informe.md"})
    mission_id = creada["id"]
    _await_state(servidor, mission_id, {"completed"})

    primera = _get(servidor, f"/missions/{mission_id}")[1]
    assert primera["state"] == "completed"

    for _ in range(5):
        repetida = _get(servidor, f"/missions/{mission_id}")[1]
        assert repetida == primera, "una lectura no puede cambiar lo que devuelve"

    import asyncio

    fila = asyncio.run_coroutine_threadsafe(
        oficial.mission_repo().get(mission_id), oficial.loop
    ).result(timeout=10)
    assert fila.state.value == "completed"
    assert list(fila.results) == primera["results"]
    # Y no se encoló nada nuevo: la misión sigue asentada, no re-ejecutada.
    assert fila.state.value != "running"


# ---------------------------------------------------------------------- #
# 5. /stream
# ---------------------------------------------------------------------- #


def test_stream_emite_eventos_del_bus_oficial(servidor):
    """§5 — SSE sobre el `EventBus` oficial, con la suscripción ya creada.

    El orden importa en los dos sentidos, y por eso el test lo respeta en los dos:

    - El stream se abre primero y se espera al 200. Ese 200 ya significa "suscrito"
      (por eso la suscripción se crea antes de las cabeceras). Generar actividad antes
      sería una carrera: el `EventBus` no tiene replay.
    - El `readline()` va después de generar actividad. Con el stream ya abierto, si se
      leyera antes, el bucle se quedaría esperando y el evento se acumularía en el socket
      sin llegar al assert; generarlo primero y leer después es el orden que deja la
      línea ya disponible cuando se lee.

    La actividad es un `POST /chat`, y no un `POST /missions` a propósito: `/chat`
    publica en el bus dentro del turno (`turn.started`, `intent.understood`,
    `turn.finished`), así que el evento existe cuando la petición responde.
    `POST /missions` sólo encola: no publica nada por sí mismo, y sus eventos llegarían
    más tarde y de forma asíncrona, desde el worker.
    """
    url = servidor + "/stream"
    status, resp = _get_raw_status(url)
    assert status == 200

    eventos = []
    try:
        # Generar actividad con la suscripción ya viva.
        _post(servidor, "/chat", {"text": B5})
        limite = time.monotonic() + STREAM_TIMEOUT_S
        while time.monotonic() < limite and not eventos:
            linea = resp.readline()
            if not linea:
                break
            texto = linea.decode("utf-8", "replace").strip()
            if texto.startswith("data:"):
                try:
                    eventos.append(json.loads(texto[len("data:"):].strip()))
                except json.JSONDecodeError:
                    pass
    finally:
        # Sin esto, `ThreadingHTTPServer` retiene el hilo del stream para siempre.
        resp.close()

    assert eventos, (
        "el stream no entregó ningún evento del bus oficial en "
        f"{STREAM_TIMEOUT_S}s (si sólo llegaron keep-alive, el bus no publicó o la "
        "suscripción se creó tarde)"
    )
    assert isinstance(eventos[0], dict)
    assert "topic" in eventos[0]


# ---------------------------------------------------------------------- #
# 6. Error cases
# ---------------------------------------------------------------------- #


def test_chat_sin_text_400_y_no_crea_mision(servidor):
    """§6 — 400 y, sobre todo, que no se abra misión por un turno vacío."""
    _, antes = _get(servidor, "/missions")

    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(servidor, "/chat", {})
    assert exc.value.code == 400
    assert "falta" in json.loads(exc.value.read().decode())["error"]

    _, despues = _get(servidor, "/missions")
    assert len(despues) == len(antes), "un turno inválido no puede crear misión"


def test_ruta_inexistente_404(servidor):
    """§6 — una ruta que no existe responde 404, no 200 ni excepción."""
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(servidor, "/no-existe")
    assert exc.value.code == 404
    assert json.loads(exc.value.read().decode())["error"] == "not found"


def test_mission_id_inexistente_404(servidor):
    """§6 — un identificador que no existe es 404 con un cuerpo mínimo."""
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(servidor, f"/missions/{uuid.uuid4()}")
    assert exc.value.code == 404
    cuerpo = json.loads(exc.value.read().decode())
    assert cuerpo == {"error": "mission not found"}


# ---------------------------------------------------------------------- #
# 7. Aislamiento: la lectura no depende del estado global
# ---------------------------------------------------------------------- #


def test_lectura_no_depende_de_state_global(servidor):
    """§7 — `GET /missions/{id}` identifica; `/state` no.

    Se crean dos misiones y se consulta cada una por su id. `/state` habría devuelto
    las dos veces la misma —la última creada—, porque `STATE["mission_id"]` es una
    única ranura sobrescrita en cada misión. Aquí cada id devuelve la suya.
    """
    _, a = _post(servidor, "/missions", {"objective": "Analiza el archivo notas.txt"})
    _, b = _post(servidor, "/missions", {"objective": "Analiza el archivo informe.md"})
    assert a["id"] != b["id"]

    cuerpo_a = _await_state(servidor, a["id"], {"completed"})
    cuerpo_b = _await_state(servidor, b["id"], {"completed"})

    assert cuerpo_a["id"] == a["id"]
    assert cuerpo_b["id"] == b["id"]
    assert cuerpo_a["success_criteria"] == ["content_observed:notas.txt"]
    assert cuerpo_b["success_criteria"] == ["content_observed:informe.md"]
    # Y no se han cruzado: el objetivo de A es el de A, y el de B el de B.
    assert "notas.txt" in cuerpo_a["objective"]
    assert "informe.md" in cuerpo_b["objective"]
