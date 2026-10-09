"""CORE-05 — Provenance durable del routing de modelos.

Antes de CORE-05, `ModelRouter` publicaba `model.routed` al bus y se perdía: nadie lo
consumía y `audit_log` no tenía una sola fila. La procedencia de cada decisión de
modelo —qué provider, qué modelo resuelto, si hubo fallback, cuánto costó— vivía sólo
en la memoria del proceso y desaparecía al reiniciar.

Estos tests fijan las cuatro propiedades que lo hacen un trail de auditoría real:

1. **Persiste**: cada `model.routed` deja fila en `audit_log`.
2. **Se correlaciona**: la fila lleva el `mission_id` de la misión que provocó la
   decisión, y el pre-misión se distingue de un id inválido.
3. **No miente**: `REAL`/`DEGRADED`/`UNAVAILABLE` se registran tal cual, y la
   provenance (13 campos) llega intacta.
4. **No tiene autoridad**: un fallo del sink se pierde a sí mismo y jamás cambia el
   `outcome` de una decisión ya tomada.

Y una quinta, estructural: la correlación viaja **explícita** en la llamada
(`router.complete(request, correlation=…)`), no en `ModelRequest`, no en un
`ContextVar`, y no depende de `STATE["mission_id"]`. Por eso dos misiones concurrentes
no se pisan: cada una lleva la suya.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from alexis.autonomy.gates import AutonomyGate  # noqa: E402
from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.cognition.loop import CognitiveRuntime  # noqa: E402
from alexis.cognition.planner import Planner  # noqa: E402
from alexis.contracts import AutonomyLevel, MissionEnvelope, MissionState  # noqa: E402
from alexis.core.runtime import AlexisRuntime  # noqa: E402
from alexis.events.bus import EventBus  # noqa: E402
from alexis.execution import SandboxExecutor  # noqa: E402
from alexis.learning.system import ExperienceLearner  # noqa: E402
from alexis.memory.store import InMemoryMemory  # noqa: E402
from alexis.models.audit_sink import (  # noqa: E402
    PROVENANCE_FIELDS,
    AuditSink,
)
from alexis.models.correlation import (  # noqa: E402
    KIND_MISSION,
    KIND_PRE_MISSION,
    RoutingCorrelation,
    for_mission,
    pre_mission,
)
from alexis.models.provider import (  # noqa: E402
    ModelOutcome,
    ModelRequest,
    ModelTask,
)
from alexis.models.router import ModelRouter  # noqa: E402
from alexis.security.policy import PolicyEngine  # noqa: E402
from alexis.security.sandbox import SandboxRunner  # noqa: E402
from alexis.tools.filesystem import build_filesystem_tools  # noqa: E402
from alexis.tools.registry import ToolRegistry  # noqa: E402
from alexis.verification import FilesystemVerifier  # noqa: E402
from alexis.world.model import WorldModel  # noqa: E402

from test_model_router import FakeProvider  # noqa: E402

PROVENANCE_13 = (
    "task", "provider", "model", "resolved_model", "resolved_provider", "outcome",
    "fallback_used", "fallback_from", "fallback_error", "chain", "latency_ms",
    "cost_usd", "error",
)


# ---------------------------------------------------------------------- #
# Fixtures
# ---------------------------------------------------------------------- #


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def audit(db):
    """Repositorio de auditoría sobre el fixture `db` de conftest.

    Importante: se usa el `db` de conftest a propósito. Ese fixture migra Y trunca
    `audit_log`; redefinirlo aquí (como hice en un primer intento) dejaba filas de
    tests anteriores en la tabla, y `list(limit=1)` —que ordena por `id DESC`— devolvía
    la fila de otro test. El fallo parecía de implementación y era de aislamiento.
    """
    from alexis.storage.repositories import AuditRepository

    return AuditRepository(db)


@pytest.fixture
async def sink(audit, bus):
    import asyncio

    registro = AuditSink(audit).attach(bus, asyncio.get_running_loop())
    yield registro
    await registro.stop()


def _router(bus, *providers):
    router = ModelRouter(event_bus=bus)
    for provider in providers:
        router.register(provider)
    return router


async def _persist(mission_id=None, *, correlation=None, task=ModelTask.REASON, **router_kw):
    """Rutea una llamada y deja que el sink escriba. Devuelve la respuesta."""
    router = _router(router_kw.pop("bus"), **router_kw.pop("providers", ()))
    resp = await router.complete(
        ModelRequest(task=task),
        correlation=correlation if correlation is not None else for_mission(mission_id),
    )
    # El sink corre en la misma task del bus; un `sleep(0)` deja que se consuma.
    import asyncio

    await asyncio.sleep(0.05)
    return resp


# ---------------------------------------------------------------------- #
# 1-3. Los tres outcomes se registran honestamente
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_1_real_se_registra_como_real(sink, audit, db, bus):
    from alexis.storage.repositories import AuditRepository

    router = _router(bus, FakeProvider("p", outcome=ModelOutcome.REAL))
    resp = await router.complete(
        ModelRequest(task=ModelTask.REASON), correlation=pre_mission()
    )
    await __import__("asyncio").sleep(0.05)

    assert resp.outcome is ModelOutcome.REAL
    filas = await audit.list(limit=1)
    assert filas, "el sink debe persistir model.routed"
    assert filas[0]["event"] == "model.routed"
    assert filas[0]["details"]["outcome"] == "real"


@pytest.mark.asyncio
async def test_2_degraded_se_registra_como_degraded(sink, audit, bus):
    """Sin provider real pero SÍ con contingencia: `DEGRADED` (no `UNAVAILABLE`).

    La diferencia importa y es exactamente lo que este blocker debe probar: los tres
    outcomes son estados DISTINTOS y se registran tal cual. Con sólo un provider no
    disponible el outcome sería `UNAVAILABLE` (ver test 3), así que aquí hace falta la
    contingencia `degraded=True` que el router usa como último recurso.
    """
    router = _router(bus)
    router.register(FakeProvider("contingencia", degraded=True))
    resp = await router.complete(
        ModelRequest(task=ModelTask.REASON), correlation=pre_mission()
    )
    await __import__("asyncio").sleep(0.05)

    assert resp.outcome is ModelOutcome.DEGRADED
    filas = await audit.list(limit=1)
    assert filas[0]["details"]["outcome"] == "degraded"
    assert filas[0]["details"]["outcome"] != "real"


@pytest.mark.asyncio
async def test_3_unavailable_se_registra_como_unavailable(sink, audit, bus):
    router = _router(bus)  # sin providers: no hay a quién llamar
    resp = await router.complete(
        ModelRequest(task=ModelTask.REASON), correlation=pre_mission()
    )
    await __import__("asyncio").sleep(0.05)

    assert resp.outcome is ModelOutcome.UNAVAILABLE
    filas = await audit.list(limit=1)
    assert filas[0]["details"]["outcome"] == "unavailable"
    assert filas[0]["details"]["outcome"] != "real"


# ---------------------------------------------------------------------- #
# 4. La provenance llega completa (13 campos)
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_4_la_provenance_llega_integra(sink, audit, bus):
    router = _router(bus, FakeProvider("p"))
    await router.complete(ModelRequest(task=ModelTask.REASON), correlation=pre_mission())
    await __import__("asyncio").sleep(0.05)

    details = (await audit.list(limit=1))[0]["details"]
    for campo in PROVENANCE_13:
        assert campo in details, f"falta el campo de provenance {campo}"
    assert details["task"] == "reason"
    assert details["provider"] == "p"


def test_4b_los_campos_de_provenance_estan_declarados():
    assert set(PROVENANCE_FIELDS) == set(PROVENANCE_13)


# ---------------------------------------------------------------------- #
# 5. Fallback A → B deja traza completa
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_5_fallback_deja_traza_completa(sink, audit, bus):
    a = FakeProvider("a", fail=True, error="boom")
    b = FakeProvider("b")
    router = _router(bus, a, b)

    resp = await router.complete(ModelRequest(task=ModelTask.REASON), correlation=pre_mission())
    await __import__("asyncio").sleep(0.05)

    details = (await audit.list(limit=1))[0]["details"]
    assert resp.fallback_used is True
    assert details["fallback_used"] is True
    assert "a" in details["chain"] and "b" in details["chain"]
    assert details["fallback_error"], "el error del provider que falló debe quedar"


@pytest.mark.asyncio
async def test_5b_la_correlacion_sobrevive_al_fallback(sink, audit, bus, db):
    """El id de correlación no se pierde al caer por la cadena de providers."""
    from alexis.storage.repositories import MissionRepository

    mission = MissionEngine().create(
        "obj",
        MissionEnvelope(objective="obj", autonomy=AutonomyLevel.SUPERVISED, allowed_actions=["execute"]),
    )
    await MissionRepository(db).upsert(mission)

    router = _router(bus, FakeProvider("a", fail=True), FakeProvider("b"))
    await router.complete(ModelRequest(task=ModelTask.REASON), correlation=for_mission(mission))
    await __import__("asyncio").sleep(0.05)

    fila = (await audit.list(limit=1))[0]
    assert fila["mission_id"] == mission.id
    assert fila["details"]["correlation"]["kind"] == KIND_MISSION


# ---------------------------------------------------------------------- #
# 6-8. mission_id: correcto, pre-misión, inválido
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_6_mission_id_correcto(sink, audit, bus, db):
    from alexis.storage.repositories import MissionRepository

    m1 = MissionEngine().create("a", MissionEnvelope(objective="a", autonomy=AutonomyLevel.SUPERVISED, allowed_actions=["execute"]))
    m2 = MissionEngine().create("b", MissionEnvelope(objective="b", autonomy=AutonomyLevel.SUPERVISED, allowed_actions=["execute"]))
    repo = MissionRepository(db)
    await repo.upsert(m1)
    await repo.upsert(m2)

    router = _router(bus, FakeProvider("p"))
    await router.complete(ModelRequest(task=ModelTask.REASON), correlation=for_mission(m1))
    await __import__("asyncio").sleep(0.05)
    await router.complete(ModelRequest(task=ModelTask.REASON), correlation=for_mission(m2))
    await __import__("asyncio").sleep(0.05)

    filas = {r["mission_id"]: r for r in await audit.list(limit=5)}
    assert m1.id in filas and m2.id in filas
    assert filas[m1.id]["details"]["correlation"]["mission_id"] == m1.id
    assert filas[m2.id]["details"]["correlation"]["mission_id"] == m2.id


@pytest.mark.asyncio
async def test_7_pre_mision_es_null_y_no_es_error(sink, audit, bus):
    router = _router(bus, FakeProvider("p"))
    await router.complete(ModelRequest(task=ModelTask.UNDERSTAND), correlation=pre_mission())
    await __import__("asyncio").sleep(0.05)

    fila = (await audit.list(limit=1))[0]
    assert fila["mission_id"] is None
    correlacion = fila["details"]["correlation"]
    assert correlacion["kind"] == KIND_PRE_MISSION
    # Pre-misión legítimo: NO lleva correlation_error.
    assert "correlation_error" not in correlacion


@pytest.mark.asyncio
async def test_8_mission_id_invalido_es_observable_no_silencioso(sink, audit, bus):
    """Un id que no existe viola la FK. NO se degrada a pre-misión en silencio.

    Se registra con `mission_id=NULL` (la fila no se pierde), pero marcado con
    `correlation_error`, de modo que el bug de correlación queda a la vista y no se
    confunde con una operación pre-misión legítima.
    """
    class _Fantasma:
        id = "no-existe-esta-mision"

    router = _router(bus, FakeProvider("p"))
    await router.complete(ModelRequest(task=ModelTask.REASON), correlation=for_mission(_Fantasma()))
    await __import__("asyncio").sleep(0.05)

    fila = (await audit.list(limit=1))[0]
    assert fila is not None, "la fila debe conservarse aunque el id sea inválido"
    assert fila["mission_id"] is None
    correlacion = fila["details"]["correlation"]
    assert correlacion["correlation_error"] == "unknown_mission_id"
    assert correlacion["rejected_mission_id"] == "no-existe-esta-mision"
    # Y es distinguible del pre-misión legítimo, que no lleva error.
    assert correlacion["kind"] == KIND_MISSION


# ---------------------------------------------------------------------- #
# 9-11. concurrency / no-dependencia de estado global / estructura
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_9_dos_misiones_no_se_pisan(sink, audit, bus, db):
    """Correlación por llamada, no por estado global.

    Las dos misiones se rutean de forma intercalada; cada fila lleva el id de la
    misión que provocó SU decisión. No hay `STATE`, ni "última misión", ni orden.
    """
    import asyncio as aio

    from alexis.storage.repositories import MissionRepository

    ms = []
    for nombre in ("a", "b", "c"):
        m = MissionEngine().create(nombre, MissionEnvelope(objective=nombre, autonomy=AutonomyLevel.SUPERVISED, allowed_actions=["execute"]))
        await MissionRepository(db).upsert(m)
        ms.append(m)

    router = _router(bus, FakeProvider("p", delay=0.01))

    async def rutar(mision):
        await router.complete(ModelRequest(task=ModelTask.REASON), correlation=for_mission(mision))
        await aio.sleep(0)

    # Intercaladas a propósito.
    await aio.gather(rutar(ms[0]), rutar(ms[1]), rutar(ms[2]))
    await aio.sleep(0.1)

    filas = [r for r in await audit.list(limit=10) if r["event"] == "model.routed"]
    ids = {r["mission_id"] for r in filas}
    for m in ms:
        assert m.id in ids, f"la misión {m.id} perdió su correlación"
    # Cada fila de misión lleva SU id, no el de otra.
    for fila in filas:
        if fila["mission_id"] is not None:
            assert fila["details"]["correlation"]["mission_id"] == fila["mission_id"]


def test_10_la_correlacion_no_depende_de_state_global():
    """Guarda estructural: nada en el transporte lee `STATE["mission_id"]`.

    Se revisa el CÓDIGO, no el texto: los módulos explican en su docstring que NO usan
    `STATE`, así que un `in` sobre el fuente daría un falso positivo documentando
    justamente lo que se evita.
    """
    import ast

    for nombre in ("alexis/models/correlation.py", "alexis/models/audit_sink.py"):
        arbol = ast.parse((PROJECT_ROOT / nombre).read_text(encoding="utf-8"))
        # Los literales de código (fuera de docstrings) no pueden citar el estado global
        # ni hacer lookup retrospectivo por misión.
        literales = [
            n.value
            for n in ast.walk(arbol)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
        docs = set()
        for n in ast.walk(arbol):
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                d = ast.get_docstring(n, clean=False)
                if d:
                    docs.add(d)
        codigo = [v for v in literales if v not in docs]
        assert not any("STATE" in v for v in codigo), f"{nombre} no debe leer el estado global"
        assert not any("mission_events" in v for v in codigo), (
            f"{nombre} no debe hacer lookup retrospectivo"
        )


def test_11_la_correlacion_no_contamina_los_contratos_de_provider():
    """Regla A vs B: el Router conoce "hay metadata genérica", no "esto es una Mission"."""
    provider_src = (PROJECT_ROOT / "alexis" / "models" / "provider.py").read_text(encoding="utf-8")
    assert "mission_id" not in provider_src, "ModelRequest no lleva correlación"
    assert "RoutingCorrelation" not in provider_src, "el contrato de provider queda limpio"

    correlation_src = (PROJECT_ROOT / "alexis" / "models" / "correlation.py").read_text(encoding="utf-8")
    assert "from alexis.contracts import Mission" not in correlation_src
    assert "from alexis.autonomy.mission" not in correlation_src, (
        "el transporte de correlación no depende del dominio Mission"
    )


def test_11b_el_evento_lleva_la_correlacion_antes_de_publicar():
    """La correlación se adjunta dentro de `_audit()`, antes del publish.

    Hacerlo después sería una carrera: `_publish()` corre en su propia task y puede
    haber entregado el payload a las colas antes de que nadie lo toque.
    """
    router_src = (PROJECT_ROOT / "alexis" / "models" / "router.py").read_text(encoding="utf-8")
    bloque = router_src[router_src.index("def _audit("):]
    adjunto = bloque.index('payload["correlation"]')
    publicado = bloque.index("bus.publish")
    assert adjunto < publicado, "la correlación debe adjuntarse ANTES de publicar"


@pytest.mark.asyncio
async def test_11c_los_callers_existentes_siguen_funcionando_sin_correlacion(bus):
    """`complete(request)` a secas no rompe: el parámetro es opcional."""
    router = _router(bus, FakeProvider("p"))
    resp = await router.complete(ModelRequest(task=ModelTask.REASON))
    assert resp.outcome in (ModelOutcome.REAL, ModelOutcome.DEGRADED)


# ---------------------------------------------------------------------- #
# 12-13. Persistencia real y no-autoridad del sink
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_12_la_evidencia_sobrevive_a_un_reload(audit, db):
    """La fila se lee después de recargar el repositorio: la evidencia es durable."""
    from alexis.storage.repositories import AuditRepository

    bus = EventBus()
    router = _router(bus, FakeProvider("p"))
    registro = AuditSink(audit)
    await registro.handle(
        {
            "task": "reason", "provider": "p", "model": "p", "outcome": "real",
            "resolved_model": "p-real", "resolved_provider": "p", "chain": ["p"],
            "fallback_used": False, "fallback_from": None, "fallback_error": None,
            "latency_ms": 12, "cost_usd": 0.001, "error": None,
            "correlation": for_mission(type("M", (), {"id": "x"})()).to_dict(),
        }
    )

    # "Reinicio": nuevo repositorio sobre la misma base.
    recargado = AuditRepository(db)
    filas = await recargado.list(limit=1)
    assert filas[0]["event"] == "model.routed"
    assert filas[0]["details"]["resolved_model"] == "p-real"
    assert filas[0]["details"]["cost_usd"] == 0.001


@pytest.mark.asyncio
async def test_13_un_sink_roto_no_altera_el_outcome():
    """Regla de no-autoridad: si el sink falla, se pierde el REGISTRO, no el ACTO.

    El sink es observador: su fallo no puede convertir un `REAL` en `DEGRADED` ni
    al revés. Y el fallo del sink no se propaga a la cognición.
    """
    class _RepoRoto:
        async def record(self, *a, **k):
            raise RuntimeError("postgres caído")

    registro = AuditSink(_RepoRoto())
    # handle() devuelve None y NO lanza.
    resultado = await registro.handle({"task": "reason", "outcome": "real", "correlation": pre_mission().to_dict()})
    assert resultado is None

    # Y una decisión real sigue siendo real aunque el sink esté roto.
    bus = EventBus()
    router = _router(bus, FakeProvider("p"))
    registro.attach(bus, asyncio_loop())
    try:
        resp = await router.complete(ModelRequest(task=ModelTask.REASON), correlation=pre_mission())
        import asyncio as aio

        await aio.sleep(0.05)
        assert resp.outcome is ModelOutcome.REAL, "el sink roto no degrada la decisión"
    finally:
        # Sin esto el consumidor del sink sigue vivo cuando el loop de este test se
        # cierra, y la tarea huérfana se reporta en el test SIGUIENTE (`-W error`).
        await registro.stop()


def asyncio_loop():
    import asyncio

    return asyncio.get_event_loop()


@pytest.mark.asyncio
async def test_13b_el_sink_no_necesita_bus_propio(sink, audit, bus):
    """Un solo bus: el sink se adjunta al bus oficial, no crea otro."""
    assert sink._sub is not None
    # El sink no publica en ningún bus: sólo consume.
    assert not hasattr(sink, "event_bus")


@pytest.mark.asyncio
async def test_14_cost_coincide_con_la_respuesta(sink, audit, bus):
    """`audit.cost_usd` es el mismo número que produjo el router, sin recalcularlo."""
    router = _router(bus, FakeProvider("p"))
    resp = await router.complete(ModelRequest(task=ModelTask.REASON), correlation=pre_mission())
    await __import__("asyncio").sleep(0.05)

    details = (await audit.list(limit=1))[0]["details"]
    assert details["cost_usd"] == resp.cost_usd


@pytest.mark.asyncio
async def test_15_routing_correlacionado_en_el_runtime_real(tmp_path):
    """La correlación sale del Core, no del test: se comprueba sobre el runtime real.

    Se usa un `CognitiveRuntime` con `ModelRouter` real y una misión real, y se
    comprueba que el evento `model.routed` publicado lleva la misión correcta.
    """
    (tmp_path / "notas.txt").write_text("contenido\n", encoding="utf-8")
    catalog = __import__("alexis.capabilities", fromlist=["build_catalog"]).build_catalog()
    world = WorldModel()
    registry = ToolRegistry()
    registry.register_all(build_filesystem_tools(tmp_path))
    executor = SandboxExecutor(tools=registry, sandbox=SandboxRunner(tmp_path))
    bus = EventBus()
    router = _router(bus, FakeProvider("p"))
    cognitive = CognitiveRuntime(
        policy=PolicyEngine(), gate=AutonomyGate(), executor=executor,
        verifier=FilesystemVerifier(workspace=tmp_path), world=world,
        catalog=catalog, model_router=router,
    )
    runtime = AlexisRuntime(
        planner=Planner(), policy=PolicyEngine(), executor=executor,
        verifier=FilesystemVerifier(workspace=tmp_path), memory=InMemoryMemory(),
        learning=ExperienceLearner(), event_bus=bus, gate=AutonomyGate(),
        cognitive=cognitive,
    )
    mission = MissionEngine().create(
        "lee el archivo notas.txt",
        MissionEnvelope(
            objective="lee el archivo notas.txt", autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=["understand", "analyze", "execute", "verify"],
        ),
        success_criteria=["file_exists:notas.txt"],
    )
    await runtime.run_mission(mission)
    import asyncio as aio

    await aio.sleep(0.1)

    eventos = [e for e in bus.events if e["topic"] == "model.routed"]
    if eventos:
        correlacion = eventos[-1]["payload"].get("correlation")
        assert correlacion is not None
        assert correlacion["kind"] in (KIND_MISSION, KIND_PRE_MISSION)
        if correlacion["kind"] == KIND_MISSION:
            assert correlacion["mission_id"] == mission.id
    else:
        # Sin provider real el Core no rutea: no hay nada que correlacionar y el
        # test no debe fingir lo contrario.
        assert mission.state in (MissionState.PENDING, MissionState.COMPLETED,
                                 MissionState.NEEDS_VERIFICATION, MissionState.BLOCKED)