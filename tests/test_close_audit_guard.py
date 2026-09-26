"""HOTFIX — `_close` no puede caer por una auditoría secundaria.

`audit_log.mission_id` tiene FK a `missions(id)`. La escritura de auditoría de `_close`
no estaba guardada, así que una misión sin fila padre la tumbaba al cerrarse: la excepción
sube desde el `record()`, `_close_cycle()` no llega a ejecutarse y con él se pierde el
epílogo de §5.6 (response → reflection → experience → learning).

Principio: MISSION STATE es PRINCIPAL, AUDIT es SECUNDARIO.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    Mission,
    MissionEnvelope,
    MissionState,
)
from alexis.core.runtime import AlexisRuntime  # noqa: E402
from alexis.security.policy import PolicyEngine  # noqa: E402

ACTIONS = ["understand", "research", "execute", "verify", "respond"]


def _mission():
    return MissionEngine().create(
        "cierra esta misión",
        MissionEnvelope(objective="cierra esta misión", autonomy=AutonomyLevel.SUPERVISED,
                        allowed_actions=list(ACTIONS)),
    )


def _runtime(**over):
    return AlexisRuntime(
        planner=None, policy=PolicyEngine(), executor=None, verifier=None,
        memory=None, learning=None, **over,
    )


class _AuditRepo:
    """Doble que registra lo que se le pide y puede fallar como la BD real."""

    def __init__(self, fail: bool = False):
        self.rows: list[tuple] = []
        self.fail = fail

    async def record(self, event, actor, mission_id=None, **details):
        if self.fail:
            # Mismo tipo de error que lanza el FK de `audit_log` en PostgreSQL.
            raise RuntimeError(
                'insert or update on table "audit_log" violates foreign key constraint '
                f'"audit_log_mission_id_fkey" DETAIL: Key (mission_id)=({mission_id}) '
                "is not present in table \"missions\"."
            )
        self.rows.append((event, actor, mission_id, details))

    async def list(self, limit=100):
        return self.rows


class _Bus:
    def __init__(self):
        self.events: list[tuple] = []

    async def publish(self, topic, payload):
        self.events.append((topic, payload))


# --------------------------------------------------------------------------- #
# 1. Con la fila padre presente → auditoría normal (comportamiento intacto)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_01_close_con_mision_existente_audita_normal():
    audit = _AuditRepo()
    runtime = _runtime(audit_repo=audit, event_bus=_Bus())
    mission = _mission()

    await runtime._close(mission, MissionState.COMPLETED)

    assert len(audit.rows) == 1
    evento, actor, mission_id, detalles = audit.rows[0]
    assert evento == "mission.completed"
    assert actor == "runtime"
    assert mission_id == mission.id
    assert detalles == {"mission": mission.id}


@pytest.mark.asyncio
async def test_02_el_audit_es_una_lectura_y_una_sola_fila():
    """La firma no cambia: sigue siendo `record(event, actor, mission_id, **details)`."""
    audit = _AuditRepo()
    runtime = _runtime(audit_repo=audit, event_bus=_Bus())

    await runtime._close(_mission(), MissionState.FAILED)
    await runtime._close(_mission(), MissionState.BLOCKED)

    assert [r[0] for r in audit.rows] == ["mission.failed", "mission.blocked"]


# --------------------------------------------------------------------------- #
# 2. Sin la fila padre → no explota
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_03_close_sin_mision_padre_no_explota():
    audit = _AuditRepo(fail=True)   # simula el FK de `audit_log`
    runtime = _runtime(audit_repo=audit, event_bus=_Bus())
    mission = _mission()

    await runtime._close(mission, MissionState.COMPLETED)   # no debe lanzar


@pytest.mark.asyncio
async def test_04_el_cierre_principal_no_queda_invalidado(caplog):
    """El estado de la misión es PRINCIPAL: el fallo del audit no lo altera."""
    audit = _AuditRepo(fail=True)
    runtime = _runtime(audit_repo=audit, event_bus=_Bus())
    mission = _mission()
    # Así es como la llaman de verdad: el caller fija el estado y luego audita.
    # (`COMPLETED` no se usa aquí porque §5.5 exige objetivo verificado para asignarlo.)
    mission.state = MissionState.FAILED

    with caplog.at_level("WARNING"):
        await runtime._close(mission, mission.state)

    # PRINCIPAL intacto: el estado que puso el caller sigue puesto.
    assert mission.state is MissionState.FAILED
    # Y queda constancia del fallo del audit, que es lo único que se pierde.
    assert any("no se pudo auditar el cierre" in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
async def test_05_el_epilogo_sigue_corriendo_aunque_el_audit_falle():
    """Lo que de verdad se perdía no era sólo la fila: era `_close_cycle()`.

    Antes, la excepción en el `record()` impedía llegar a `_close_cycle()`, así que el
    epílogo de §5.6 no se componía. Ahora se compone igual.
    """
    audit = _AuditRepo(fail=True)
    runtime = _runtime(audit_repo=audit, event_bus=_Bus())
    mission = _mission()
    mission.context["knowledge"] = {"iterations": 1, "claims": [], "world": [],
                                    "decisions": {}, "evaluations": {}}

    llamado = {}

    async def _fake_cycle(m):
        llamado["ok"] = True
        return True

    runtime._close_cycle = _fake_cycle
    await runtime._close(mission, MissionState.COMPLETED)

    assert llamado.get("ok") is True, "el epílogo no debe perderse por un fallo de audit"


# --------------------------------------------------------------------------- #
# 3. Con la BD real: el FK de verdad
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_06_fk_real_no_tumba_el_cierre(db):
    """Con PostgreSQL real, no con un doble: la ausencia de la fila padre."""
    from alexis.storage.repositories import AuditRepository

    await db.open()
    await db.migrate()
    audit = AuditRepository(db)
    runtime = _runtime(audit_repo=audit, event_bus=_Bus())
    mission = _mission()   # nunca se inserta en `missions` → el FK no puede resolverse

    await runtime._close(mission, MissionState.COMPLETED)   # no debe lanzar

    filas = await audit.list(limit=10)
    assert not [f for f in filas if f["event"] == "mission.completed"], \
        "sin fila padre no debe poder escribirse la fila de audit"
    await db.close()


@pytest.mark.asyncio
async def test_07_fk_real_con_mision_persistente_si_audita(db):
    """El caso normal, con BD real: si la misión existe, la fila se escribe."""
    from alexis.storage.repositories import AuditRepository, MissionRepository

    await db.open()
    await db.migrate()
    audit = AuditRepository(db)
    runtime = _runtime(audit_repo=audit, event_bus=_Bus())
    mission = _mission()
    await MissionRepository(db).upsert(mission)

    await runtime._close(mission, MissionState.COMPLETED)

    filas = [f for f in await audit.list(limit=10) if f["event"] == "mission.completed"]
    assert filas and filas[0]["mission_id"] == mission.id
    await db.close()


# --------------------------------------------------------------------------- #
# 4. El resto del runtime sigue igual
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_08_sin_audit_repo_sigue_funcionando():
    """El audit es opcional: `_close` no debe depender de él."""
    runtime = _runtime(audit_repo=None, event_bus=_Bus())
    await runtime._close(_mission(), MissionState.COMPLETED)


@pytest.mark.asyncio
async def test_09_flush_rejections_sigue_funcionando():
    """El otrowriter de audit, el de §12.8, no se ha tocado."""
    from alexis.cognition.loop import CognitiveRuntime
    from alexis.world.model import WorldModel

    cognitive = CognitiveRuntime(policy=PolicyEngine(), executor=None, verifier=None,
                                 execute=None, world=WorldModel())
    audit = _AuditRepo()
    runtime = _runtime(audit_repo=audit, event_bus=_Bus(), cognitive=cognitive)
    cognitive.rejected_actions = [{"reason": "ya falló", "iteration": 1,
                                  "action_signature": "execute|fs.read|path=notas.txt"}]

    n = await runtime._flush_rejections(_mission())

    assert n == 1
    assert audit.rows[0][0] == "replan.action_rejected"
    assert audit.rows[0][3]["reason"] == "ya falló"


@pytest.mark.asyncio
async def test_10_flush_rejections_tampoco_tumba_si_el_audit_falla():
    """Comportamiento preexistente, ahora fijado por un test."""
    from alexis.cognition.loop import CognitiveRuntime
    from alexis.world.model import WorldModel

    cognitive = CognitiveRuntime(policy=PolicyEngine(), executor=None, verifier=None,
                                 execute=None, world=WorldModel())
    runtime = _runtime(audit_repo=_AuditRepo(fail=True), event_bus=_Bus(), cognitive=cognitive)
    cognitive.rejected_actions = [{"reason": "ya falló", "iteration": 1}]

    assert await runtime._flush_rejections(_mission()) == 1   # no debe lanzar
