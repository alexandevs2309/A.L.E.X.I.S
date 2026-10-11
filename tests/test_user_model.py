"""User Model — preferencias, proyectos y decisiones.

Todos los tests hablan con PostgreSQL real, no con un doble. Un `UserRepository` probado
con un mock que devuelve lo que se le pide no demuestra que la tabla existe, que el CHECK
acepta lo que se le pasa ni que el dato sobrevive al reinicio: demuestra que el mock
funciona.

La aserción que más se repite aquí es `get_active_projects()` VACÍA tras un
`remove_project`. No es un detalle: es la diferencia entre "el usuario lo cerró" y "se
olvidó de quitarlo", y un ALEXIS que ofrece trabajo sobre un proyecto cerrado es peor que
uno que no ofrece nada.
"""

from __future__ import annotations

import pytest

from alexis.cognition.loop import CognitiveRuntime
from alexis.self.model import SelfModel
from alexis.self.sync import SelfModelSync
from alexis.storage.db import Database
from alexis.user.model import UserContext, UserPreferences
from alexis.user.repository import UserRepository


@pytest.fixture
async def users(db):
    """Repositorio de preferencias sobre la base de test."""
    return UserRepository(db)


def _mision(mission_id: str, contexto: dict | None = None):
    """Una `Mission` mínima y REAL (con `envelope` y `goal` de verdad).

    No un `SimpleNamespace`: la inyección se escribe en `mission.context`, y si la misión
    de test fuera un objeto distinto al real, la prueba estaría midiendo un atributo que
    el runtime nunca toca.
    """
    from alexis.contracts import AutonomyLevel, Goal, Mission, MissionEnvelope

    envelope = MissionEnvelope(
        objective="objetivo de prueba",
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=["understand", "analyze"],
        forbidden_actions=[],
        approval_required=[],
        max_runtime_minutes=10,
    )
    return Mission(id=mission_id, goal=Goal(envelope.objective), envelope=envelope,
                   context=dict(contexto or {}))


# --------------------------------------------------------------------------- #
# 1 — CRUD
# --------------------------------------------------------------------------- #


async def test_crear_una_preferencia_y_recuperarla(users):
    await users.set("style", "tono", {"formal": False}, confidence=0.8, source="explicit")

    leida = await users.get("style", "tono")

    assert leida is not None
    assert leida.key == "tono"
    assert leida.value == {"formal": False}
    assert leida.confidence == pytest.approx(0.8)
    # `source` es lo que separa "lo dijo el usuario" de "lo dedujo ALEXIS". Si se pierde,
    # una inferencia se presenta como una orden.
    assert leida.source == "explicit"
    assert leida.id == "style:tono"


async def test_listar_preferencias_por_categoria(users):
    await users.set("style", "tono", "directo")
    await users.set("style", "idioma", "es")
    await users.set("approval", "deploy", False)

    estilo = await users.list_by_category("style")
    aprobaciones = await users.list_by_category("approval")

    assert {p.key for p in estilo} == {"tono", "idioma"}
    # La categoría es lo que separa una cosa de otra: 'deploy' no debe aparecer al
    # listar preferencias de estilo aunque comparta clave con alguna.
    assert {p.key for p in aprobaciones} == {"deploy"}


async def test_actualizar_una_preferencia_existente(users):
    await users.set("style", "tono", {"formal": True}, confidence=0.5)
    await users.set("style", "tono", {"formal": False}, confidence=0.9)

    leida = await users.get("style", "tono")

    assert leida.value == {"formal": False}
    assert leida.confidence == pytest.approx(0.9)
    # Actualizar no inserta una segunda fila: `UNIQUE(category, key)` y el `upsert` por
    # `id` lo garantizan. Si se duplicara, `get()` devolvería la primera y el cambio
    # parecería no haberse guardado.
    assert len(await users.list_by_category("style")) == 1


async def test_el_upsert_conserva_la_fecha_de_creacion(db, users):
    import asyncio

    await users.set("style", "tono", "a")
    await asyncio.sleep(0.02)
    await users.set("style", "tono", "b")

    leida = await users.get("style", "tono")

    assert leida.created_at is not None
    # Reescribir no convierte la preferencia en nueva. Si `created_at` se moviera, no
    # habría forma de distinguir "se corrige" de "se acaba de descubrir". La ventana de
    # 20 ms hace que la comparación sea significativa y no una casualidad del reloj.
    [original] = await db.fetch(
        "SELECT created_at, updated_at FROM user_preferences WHERE id = 'style:tono'"
    )
    assert leida.created_at == original["created_at"]
    assert leida.updated_at == original["updated_at"]


# --------------------------------------------------------------------------- #
# 2 — Persistencia real
# --------------------------------------------------------------------------- #


async def test_las_preferencias_sobreviven_a_reiniciar_el_proceso(db):
    """Se escribe con una conexión, se cierra, y otra conexión nueva lo lee.

    Cierra `db` y abre OTRO `Database`. Reutilizar el mismo objeto probaría el dict de
    psycopg, no la base de datos.
    """
    repo = UserRepository(db)
    await repo.set("style", "tono", {"formal": False}, confidence=0.7)
    await repo.add_project("ALEXIS", path="/srv/alexis")
    await db.close()

    try:
        nuevo = Database(db.dsn)
        await nuevo.open()
        try:
            otra = UserRepository(nuevo)
            leida = await otra.get("style", "tono")
            assert leida is not None and leida.value == {"formal": False}
            assert leida.confidence == pytest.approx(0.7)
            assert await otra.get_active_projects() == ["ALEXIS"]
        finally:
            await nuevo.close()
    finally:
        await db.open()  # el fixture cierra `db` al final del test


async def test_la_base_rechaza_una_categoria_fuera_del_conjunto(db):
    """La lista de categorías es un CHECK, no un comentario.

    Un comentario no rechaza nada: `category='proyects'` se escribiría sin error y no lo
    leería nadie, así que la preferencia quedaría guardada pero invisible.
    """
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        await db.execute(
            "INSERT INTO user_preferences (id, category, key, value) "
            "VALUES ('x:1', 'proyects', '1', '{}'::jsonb)"
        )


# --------------------------------------------------------------------------- #
# 3 — Proyectos
# --------------------------------------------------------------------------- #


async def test_los_proyectos_activos_se_gestionan(users):
    await users.add_project("ALEXIS", path="/srv/alexis")
    await users.add_project("otro")
    assert await users.get_active_projects() == ["ALEXIS", "otro"]

    assert await users.remove_project("otro") is True
    assert await users.get_active_projects() == ["ALEXIS"]

    # Eliminar es reversible y conserva el historial. Borrar la fila lo perdería, y un
    # `add_project` posterior la recrearía vacía: el detalle del proyecto desaparecería.
    guardada = await users.get("project", "otro")
    assert guardada is not None and guardada.active is False

    await users.add_project("otro")
    assert await users.get_active_projects() == ["ALEXIS", "otro"]


async def test_un_proyecto_sin_marca_activa_no_cuenta_como_activo(users):
    """Ausencia de `active` es DESCONOCIDO, no activo.

    Escribir un proyecto sin esa clave (a mano, por SQL, por una versión anterior) no
    autoriza a ofrecer trabajo sobre él. La diferencia entre "no sé" y "sí" es la misma
    que se exige en cualquier otro borde de este runtime.
    """
    await users.set("project", "sin_marca", {"path": "/tmp/x"})

    assert "sin_marca" not in await users.get_active_projects()


async def test_eliminar_un_proyecto_inexistente_no_inventa_nada(users):
    assert await users.remove_project("nunca-existio") is False


# --------------------------------------------------------------------------- #
# 4 — Decisiones
# --------------------------------------------------------------------------- #


async def test_las_decisiones_se_registran_y_se_recuperan(users):
    await users.record_decision("d1", {"tarea": "migrar"}, {"resultado": "ok"})
    await users.record_decision("d2", {"tarea": "borrar"}, {"resultado": "fallo"})

    recientes = await users.get_recent_decisions(limit=10)

    assert len(recientes) == 2
    ids = {d["decision_id"] for d in recientes}
    assert ids == {"d1", "d2"}
    por_id = {d["decision_id"]: d for d in recientes}
    # Se guarda el `outcome` junto al `context` para poder comparar. Sin eso, esto sería
    # un historial: diría qué pasó, no qué repetir.
    assert por_id["d1"]["context"] == {"tarea": "migrar"}
    assert por_id["d2"]["outcome"] == {"resultado": "fallo"}


async def test_las_decisiones_se_ordenan_de_mas_reciente_a_mas_antigua(users):
    import asyncio

    for i in range(5):
        await users.record_decision(f"d{i}", {"n": i}, {"ok": True})
        await asyncio.sleep(0.01)

    recientes = await users.get_recent_decisions(limit=3)

    assert [d["decision_id"] for d in recientes] == ["d4", "d3", "d2"]


# --------------------------------------------------------------------------- #
# 5 — Integración: SelfModel
# --------------------------------------------------------------------------- #


async def test_el_self_model_actualiza_su_zona_user(db, users):
    await users.set("style", "tono", "directo")
    await users.add_project("ALEXIS")
    await users.record_decision("d1", {"t": 1}, {"ok": True})

    modelo = SelfModel()
    sync = SelfModelSync(modelo, resolve_mission=lambda: None)
    sync.user_repository = users

    assert await sync.refresh_user_preferences() is True

    assert modelo.active_projects == ["ALEXIS"]
    assert modelo.user_preferences["style"] == {"tono": "directo"}
    assert [d["decision_id"] for d in modelo.recent_decisions] == ["d1"]
    # La zona se expone en `/self`: si no, lo que se afirma no es observable.
    assert modelo.snapshot()["active_projects"] == ["ALEXIS"]


async def test_la_zona_user_sobrevive_al_reset_de_mision(db, users):
    """`_reset()` limpia estado de MISIÓN. El usuario no cambia al terminar una misión."""
    modelo = SelfModel()
    modelo.note_user(active_projects=["ALEXIS"], preferences={"style": {"tono": "directo"}})

    modelo._reset()

    assert modelo.active_projects == ["ALEXIS"]
    assert modelo.user_preferences == {"style": {"tono": "directo"}}


async def test_refrescar_sin_repositorio_no_rompe_nada():
    modelo = SelfModel()
    sync = SelfModelSync(modelo, resolve_mission=lambda: None)

    assert await sync.refresh_user_preferences() is False
    assert modelo.active_projects == []


# --------------------------------------------------------------------------- #
# 6 — Integración: COGNICIÓN
# --------------------------------------------------------------------------- #


def _runtime(user_repository=None) -> CognitiveRuntime:
    return CognitiveRuntime(
        policy=object(),
        verifier=object(),
        goal_verifier=object(),
        model_router=None,
        user_repository=user_repository,
    )


async def test_las_preferencias_se_inyectan_en_el_contexto_de_la_mision(db, users):
    
    await users.add_project("ALEXIS", path="/srv/alexis")
    await users.set("style", "tono", "directo")

    mission = _mision("m-user")
    runtime = _runtime(user_repository=users)

    await runtime._inyectar_preferencias(mission)

    inyectado = mission.context["user_preferences"]
    assert inyectado["active_projects"] == ["ALEXIS"]
    assert inyectado["style"] == {"tono": "directo"}


async def test_sin_user_repository_el_contexto_no_se_toca(db):
    """Retrocompatibilidad: sin User Model, COGNICIÓN decide igual que antes."""
    
    mission = _mision("m-sin-user", {"otra": "zona"})
    runtime = _runtime(user_repository=None)

    await runtime._inyectar_preferencias(mission)

    # Ni aparece la clave ni se pisa lo que ya había.
    assert "user_preferences" not in mission.context
    assert mission.context["otra"] == "zona"


async def test_una_fallo_de_usuario_no_impide_decidir(db, users, monkeypatch):
    """Personalizar es una mejora, no un requisito para pensar.

    Si la consulta de preferencias revienta, la excepción debe tragarse y la misión
    seguir siendo decidible. Propagarla dejaría a ALEXIS sin pensar por un problema de
    personalización, que es la peor de las dos degradaciones posibles.
    """
    
    async def revienta(*args, **kwargs):
        raise RuntimeError("la base de preferencias está caída")

    monkeypatch.setattr(users, "get_active_projects", revienta)

    mission = _mision("m-error")
    runtime = _runtime(user_repository=users)

    await runtime._inyectar_preferencias(mission)  # no debe propagar

    assert "user_preferences" not in mission.context


async def test_sin_preferencias_no_se_escribe_una_zona_vacia(db, users):
    """No se inyecta una zona vacía: ensuciaría el contexto de cada misión sin aportar nada."""
    
    mission = _mision("m-vacia")
    runtime = _runtime(user_repository=users)

    await runtime._inyectar_preferencias(mission)

    assert "user_preferences" not in mission.context


# --------------------------------------------------------------------------- #
# 7 — Retrocompatibilidad
# --------------------------------------------------------------------------- #


async def test_sin_la_tabla_el_repositorio_degrada_en_silencio(db, monkeypatch):
    """Un ALEXIS recién arrancado contra una base sin migrar sirve con defaults.

    Es el requisito de retrocompatibilidad: la ausencia del User Model no puede ser un
    error, sólo un default.
    """
    monkeypatch.setattr(UserRepository, "is_available", lambda self: _falso(self))
    repo = UserRepository(db)

    assert await repo.get("style", "tono") is None
    assert await repo.list_by_category("style") == []
    assert await repo.get_active_projects() == []
    assert await repo.get_recent_decisions() == []
    # `set` no falla: devuelve la preferencia en memoria para que la API pueda responder.
    pref = await repo.set("style", "tono", "x")
    assert pref.key == "tono"
    assert isinstance(await repo.context(), UserContext)


async def _falso(self) -> bool:
    return False


# --------------------------------------------------------------------------- #
# 8 — Modelo
# --------------------------------------------------------------------------- #


def test_la_confianza_se_acota_a_cero_y_uno():
    """Fuera de rango no es "mucha confianza": es un dato corrupto que un MAX() elige."""
    assert UserPreferences("style", "k", 1, confidence=7.0).confidence == 1.0
    assert UserPreferences("style", "k", 1, confidence=-3.0).confidence == 0.0
    assert UserPreferences("style", "k", 1, confidence=0.25).confidence == 0.25


def test_el_contexto_es_una_copia():
    """`UserContext` no comparte referencias con quien lo recibe.

    Si compartiera la lista, un `append` de la API mutaría el estado sin pasar por una
    escritura: la zona en memoria dejaría de reflejar lo que hay en la base.
    """
    ctx = UserContext(active_projects=["a"], technical_style={"t": "x"})
    copia = ctx.to_dict()
    copia["active_projects"].append("b")

    assert ctx.active_projects == ["a"]

# --------------------------------------------------------------------------- #
# 9 — API REST (servidor real, por HTTP)
# --------------------------------------------------------------------------- #


class _Cliente:
    """Cliente HTTP mínimo: GET, POST y DELETE contra el servidor real.

    `post` propaga la excepción en un 4xx, así que `crudo` la captura: hay que poder
    comprobar el CÓDIGO (un 400 por categoría inválida es comportamiento esperado, no
    un fallo del test).
    """

    def __init__(self, base: str):
        self.base = base

    def _peticion(self, metodo, ruta, cuerpo=None):
        import urllib.request

        datos = None
        cabeceras = {}
        if cuerpo is not None:
            import json as _json

            datos = _json.dumps(cuerpo).encode()
            cabeceras["Content-Type"] = "application/json"
        peticion = urllib.request.Request(self.base + ruta, data=datos,
                                          headers=cabeceras, method=metodo)
        import json as _json

        with urllib.request.urlopen(peticion, timeout=10) as r:
            return r.status, _json.loads(r.read())

    def get(self, ruta):
        return self._peticion("GET", ruta)

    def post(self, ruta, cuerpo):
        return self._peticion("POST", ruta, cuerpo)

    def delete(self, ruta):
        return self._peticion("DELETE", ruta)

    def crudo(self, metodo, ruta, cuerpo=None):
        import urllib.error

        try:
            return self._peticion(metodo, ruta, cuerpo)
        except urllib.error.HTTPError as exc:
            import json as _json

            return exc.code, _json.loads(exc.read())


@pytest.fixture(scope="module")
def servidor():
    """La aplicación oficial en un puerto efímero, con PostgreSQL real.

    Se levanta por HTTP de verdad en vez de invocar los handlers: no hay router, así que
    "probar el endpoint" es poner un socket delante. Llamar a los métodos del handler
    saltaría justo el enrutado y el reparto entre `do_GET`/`do_POST`/`do_DELETE`, que es
    donde se cuelan los 404 silenciosos.
    """
    import asyncio
    import threading

    from apps.demo import app as app_module

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    storage = app_module.init_storage(loop)
    if storage.get("user") is None:
        loop.call_soon_threadsafe(loop.stop)
        pytest.skip("la API de usuario necesita PostgreSQL")
    runtime = app_module.build_runtime(loop=loop, storage=storage, workspace="/tmp")
    httpd = app_module.create_app(runtime)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    cliente = _Cliente(f"http://127.0.0.1:{httpd.server_address[1]}")

    yield cliente

    httpd.shutdown()
    httpd.server_close()
    try:
        asyncio.run_coroutine_threadsafe(storage["db"].close(), loop).result(timeout=10)
    except Exception:  # noqa: BLE001 — el cierre no debe enmascarar el resultado
        pass
    loop.call_soon_threadsafe(loop.stop)


def test_la_api_devuelve_las_preferencias_creadas(servidor):
    _, prefs = servidor.post("/user/preferences",
                            {"category": "style", "key": "tono",
                             "value": {"formal": False}, "source": "explicit"})
    assert prefs["key"] == "tono"

    estado, todas = servidor.get("/user/preferences")
    assert estado == 200
    assert any(p["key"] == "tono" for p in todas)

    # La misma fila se ve desde el repositorio: la API no tiene estado propio.
    assert all({"category", "key", "value", "confidence", "source"} <= set(p) for p in todas)


def test_la_api_filtra_por_categoria(servidor):
    servidor.post("/user/preferences", {"category": "approval", "key": "deploy", "value": False})

    estado, estilo = servidor.get("/user/preferences/style")

    assert estado == 200
    assert all(p["category"] == "style" for p in estilo)


def test_la_api_rechaza_un_cuerpo_incompleto(servidor):
    estado, cuerpo = servidor.crudo("POST", "/user/preferences", {"category": "style"})
    assert estado == 400
    assert "error" in cuerpo


def test_la_api_gestiona_proyectos(servidor):
    servidor.post("/user/projects", {"name": "proyecto-api", "details": {"path": "/tmp"}})

    estado, payload = servidor.get("/user/projects")
    assert estado == 200
    assert "proyecto-api" in payload["active_projects"]

    estado, borrado = servidor.delete("/user/projects/proyecto-api")
    assert estado == 200 and borrado == {"removed": "proyecto-api"}

    _, payload = servidor.get("/user/projects")
    assert "proyecto-api" not in payload["active_projects"]

    # Borrar de nuevo da 404, no un 200 vacío: `remove_project` es idempotente, pero
    # "borré algo" cuando no había nada sería mentir sobre el efecto.
    estado, _ = servidor.crudo("DELETE", "/user/projects/no-existe")
    assert estado == 404


async def test_la_api_devuelve_las_decisiones(servidor, db):
    """`record_decision` no tiene endpoint: es interna de COGNICIÓN.

    El contrato de la superficie es exactamente ese —se escribe por dentro y se lee por
    fuera—, así que el test escribe por el repositorio y comprueba que la API lo ve. Sin
    esa mitad, `GET /user/decisions` podría devolver siempre `[]` y nadie lo notaría.
    """
    await UserRepository(db).record_decision("d-api", {"tarea": "desplegar"}, {"ok": True})

    estado, decisiones = servidor.get("/user/decisions")

    assert estado == 200
    assert any(d["decision_id"] == "d-api" for d in decisiones)
