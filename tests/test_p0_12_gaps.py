"""P0 §12 — cierre de los dos GAPs.

GAP 1: qué es "evidencia material" para readmitir una acción fallida.
GAP 2: el rechazo queda auditado con la infraestructura existente y sobrevive al reinicio.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.cognition.contracts import ActionAttempt  # noqa: E402
from alexis.cognition.loop import (  # noqa: E402
    CognitiveRuntime,
    _evidence_scope,
    _scoped_evidence_fingerprint,
)
from alexis.cognition.state import KnowledgeState  # noqa: E402
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    MissionEnvelope,
    PlanStep,
    RiskLevel,
)
from alexis.security.policy import PolicyEngine  # noqa: E402
from alexis.world.model import WorldModel  # noqa: E402

ACTIONS = ["understand", "research", "execute", "verify", "respond"]


def _cognitive(**over):
    return CognitiveRuntime(policy=PolicyEngine(), executor=None, verifier=None,
                            execute=None, world=WorldModel(), **over)


def _mission():
    return MissionEngine().create(
        "lee notas.txt",
        MissionEnvelope(objective="lee notas.txt", autonomy=AutonomyLevel.SUPERVISED,
                        allowed_actions=list(ACTIONS)),
    )


def _read(path="notas.txt", step_id="a"):
    return PlanStep(step_id, "lee", "execute", RiskLevel.LOW, "e",
                    capability="fs.read", args={"path": path})


# =========================================================================== #
# GAP 1 — evidencia material
# =========================================================================== #


def test_g1_01_misma_evidencia_repeticion_bloqueada():
    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    paso = _read()
    cognitive.record_attempt(knowledge, paso, success=False, error="no existe")
    for generation in (1, 2, 3):
        assert cognitive.blocked_reason(knowledge, paso, generation) != "", generation


def test_g1_02_evidencia_material_del_mismo_target_readmite():
    """El caso del enunciado: fs.stat(notas.txt) confirma que ahora existe."""
    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    paso = _read()
    cognitive.record_attempt(knowledge, paso, success=False, error="no existe")
    assert cognitive.blocked_reason(knowledge, paso, 1) != ""
    knowledge.world = ["file:notas.txt (exists=True, size=30)"]
    assert cognitive.blocked_reason(knowledge, paso, 1) == ""


def test_g1_03_evidencia_no_relacionada_sigue_bloqueada():
    """fs.stat de OTRO archivo no habilita leer notas.txt."""
    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    paso = _read()
    cognitive.record_attempt(knowledge, paso, success=False, error="no existe")
    knowledge.world = ["file:otro.txt (exists=True, size=10)"]
    assert cognitive.blocked_reason(knowledge, paso, 1) != "", \
        "la evidencia de otro archivo no debe readmitir"


def test_g1_04_completar_otra_tarea_no_readmite():
    """Éste era el GAP: `known` recibía "«otro» completado" y bastaba."""
    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    paso = _read()
    cognitive.record_attempt(knowledge, paso, success=False, error="error")
    knowledge.mark_completed("otro-paso")
    knowledge.add_known("'otro-paso' completado: contenido")
    assert cognitive.blocked_reason(knowledge, paso, 1) != "", \
        "completar otra tarea no es evidencia sobre notas.txt"


def test_g1_05_cambiar_replans_no_readmite():
    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    paso = _read()
    cognitive.record_attempt(knowledge, paso, success=False, error="error")
    knowledge.note_replan("otra vez")
    knowledge.note_replan("y otra")
    assert cognitive.blocked_reason(knowledge, paso, knowledge.replans + 1) != ""


def test_g1_06_cambiar_unknown_no_readmite():
    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    paso = _read()
    cognitive.record_attempt(knowledge, paso, success=False, error="error")
    knowledge.add_unknown("no sé por qué falló")
    assert cognitive.blocked_reason(knowledge, paso, 1) != ""


def test_g1_07_cambiar_contadores_no_readmite():
    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    paso = _read()
    cognitive.record_attempt(knowledge, paso, success=False, error="error")
    knowledge.iterations += 5
    knowledge.stalls += 3
    knowledge.confidence = 0.99
    assert cognitive.blocked_reason(knowledge, paso, 1) != ""


def test_g1_08_world_relevante_readmite():
    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    paso = _read()
    cognitive.record_attempt(knowledge, paso, success=False, error="no existe")
    knowledge.world = ["file:notas.txt (exists=True)"]
    assert cognitive.blocked_reason(knowledge, paso, 1) == ""


def test_g1_09_claim_relevante_readmite():
    from alexis.cognition.contracts import Claim, ClaimKind

    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    paso = _read()
    cognitive.record_attempt(knowledge, paso, success=False, error="no existe")
    knowledge.add_claim(Claim(id="c1", kind=ClaimKind.EVIDENCE,
                              text="notas.txt existe ahora", source="fs.stat",
                              evidence_ids=["ev-1"], verified=True))
    assert cognitive.blocked_reason(knowledge, paso, 1) == ""


def test_g1_10_el_scope_se_deriva_del_argumento_target():
    assert _evidence_scope(_read("notas.txt")) == "notas.txt"
    assert _evidence_scope(PlanStep("s", "d", "execute", RiskLevel.LOW, "e",
                                    capability="tts.speak", args={})) == ""


def test_g1_11_la_regla_es_determinista():
    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    paso = _read()
    cognitive.record_attempt(knowledge, paso, success=False, error="error")
    knowledge.world = ["file:notas.txt (exists=True)"]
    huellas = {_scoped_evidence_fingerprint(knowledge, "notas.txt") for _ in range(5)}
    assert len(huellas) == 1, "la huella debe ser estable"


def test_g1_12_el_recovery_conserva_la_decision():
    """Tras serializar, la huella con scope sigue ahí y la decisión no cambia."""
    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    paso = _read()
    cognitive.record_attempt(knowledge, paso, success=False, error="no existe")
    knowledge.world = ["file:otro.txt (exists=True)"]

    recuperado = KnowledgeState.from_dict(knowledge.to_dict(), "lee notas.txt")
    bloqueada_antes = bool(cognitive.blocked_reason(knowledge, paso, 1))
    bloqueada_despues = bool(cognitive.blocked_reason(recuperado, paso, 1))
    assert bloqueada_antes is bloqueada_despues is True

    # Y con la evidencia relevante, tras el reinicio, sí se readmite.
    recuperado.world = ["file:notas.txt (exists=True)"]
    assert cognitive.blocked_reason(recuperado, paso, 1) == ""


def test_g1_13_el_scope_sobrevive_en_el_intento():
    intento = ActionAttempt(signature="s", scope="notas.txt", scoped_evidence_fp="abc")
    assert ActionAttempt.from_dict(intento.to_dict()).scope == "notas.txt"
    assert ActionAttempt.from_dict(intento.to_dict()).scoped_evidence_fp == "abc"


def test_g1_14_una_accion_sin_scope_usa_evidencia_global():
    """Sin target identificable no se puede probar relevancia, pero tampoco se bloquea."""
    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    paso = PlanStep("d", "habla", "respond", RiskLevel.LOW, "e", capability="tts.speak")
    cognitive.record_attempt(knowledge, paso, success=False, error="sin voz")
    assert cognitive.blocked_reason(knowledge, paso, 1) != ""
    knowledge.world = ["tts:hablé con éxito"]
    assert cognitive.blocked_reason(knowledge, paso, 1) == ""


def test_g1_15_el_autoparte_del_fallo_no_readmite():
    """REGRESIÓN: el parte que el sistema se hace a sí mismo no es evidencia.

    Este es el fallo que la integración destapó. Al ejecutar, el Core registra dos claims
    propios: `observation:tool.fs.read` y `executor`, y sus textos citan la ruta del
    objetivo. Contarlos como evidencia hacía que la huella cambiase en CADA intento, de
    modo que la guarda se autorizaba siempre y el sistema repetía la lectura para
    siempre — el bucle exacto que §12.5 prohíbe. Con el WorldModel vacío.
    """
    from alexis.cognition.contracts import Claim, ClaimKind

    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    paso = _read()
    cognitive.record_attempt(knowledge, paso, success=False, error="no existe")
    huella_al_fallar = knowledge.action_attempts[-1]["scoped_evidence_fp"]

    for i in range(3):  # tres intentos fallidos más, con su autoparte
        knowledge.add_claim(Claim(id=f"claim-{i}", kind=ClaimKind.EVIDENCE,
                                  text='{"args": {"path": "notas.txt"}, "ok": false}',
                                  source="observation:tool.fs.read", evidence_ids=[]))
        knowledge.add_claim(Claim(id=f"claim-e{i}", kind=ClaimKind.EVIDENCE,
                                  text="la acción falló: no existe: /tmp/notas.txt",
                                  source="executor", evidence_ids=[]))
        cognitive.record_attempt(knowledge, paso, success=False, error="no existe")

    assert cognitive.blocked_reason(knowledge, paso, 1) != "", \
        "el autoparte del fallo no puede readmitir la repetición"
    assert _scoped_evidence_fingerprint(knowledge, "notas.txt", "fs.read") == huella_al_fallar


def test_g1_16_repetir_el_mismo_hecho_no_es_hecho_nuevo():
    """El mismo hecho registrado dos veces no es evidencia nueva.

    Los ids de claim son únicos por registro, así que hashear el `id` hacía que un
    duplicado pareciera un hecho nuevo. Se hashea el contenido.
    """
    from alexis.cognition.contracts import Claim, ClaimKind

    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    antes = _scoped_evidence_fingerprint(knowledge, "notas.txt", "fs.read")
    for i in range(2):
        knowledge.add_claim(Claim(id=f"claim-{i}", kind=ClaimKind.EVIDENCE,
                                  text="el usuario ya creó el archivo", source="user",
                                  evidence_ids=[]))
    assert _scoped_evidence_fingerprint(knowledge, "notas.txt", "fs.read") == antes


def test_g1_17_el_autoparte_de_otra_capacidad_no_oculta_la_evidencia():
    """Se excluye el autoparte de LA MISMA capability, no el de cualquier otra.

    Si `fs.read` falla y luego `fs.write` deja un hecho real, ese hecho sí cuenta.
    """
    from alexis.cognition.contracts import Claim, ClaimKind

    cognitive = _cognitive()
    knowledge = cognitive.knowledge_for(_mission())
    paso = _read()
    cognitive.record_attempt(knowledge, paso, success=False, error="no existe")
    knowledge.add_claim(Claim(id="c1", kind=ClaimKind.EVIDENCE,
                              text="notas.txt ya existe", source="observation:tool.fs.stat",
                              evidence_ids=[]))
    assert cognitive.blocked_reason(knowledge, paso, 1) == "", \
        "la observación de fs.stat SÍ es evidencia sobre notas.txt"


# =========================================================================== #
# GAP 2 — auditoría
# =========================================================================== #


class _AuditRepo:
    def __init__(self):
        self.rows = []

    async def record(self, event, actor, mission_id=None, **details):
        self.rows.append((event, actor, mission_id, details))

    async def list(self, limit=100):
        return self.rows


class _Bus:
    def __init__(self):
        self.events = []

    async def publish(self, topic, payload):
        self.events.append((topic, payload))


@pytest.mark.asyncio
async def test_g2_01_el_rechazo_se_publica_y_se_audita(tmp_path):
    """Flujo real: falla → replan → el filtro rechaza → evento + audit_log."""
    from alexis.core.runtime import AlexisRuntime
    from alexis.verification import FilesystemVerifier

    cognitive = _cognitive()
    bus, audit = _Bus(), _AuditRepo()
    runtime = AlexisRuntime(
        planner=None, policy=PolicyEngine(), executor=None,
        verifier=FilesystemVerifier(workspace=tmp_path), memory=None, learning=None,
        event_bus=bus, audit_repo=audit, cognitive=cognitive,
    )
    mission = _mission()
    knowledge = cognitive.knowledge_for(mission)
    paso = _read()
    cognitive.record_attempt(knowledge, paso, success=False, error="no existe")
    cognitive.store_knowledge(mission, knowledge)

    knowledge.needs_replan = True
    cognitive.options(mission, knowledge, pending_steps=[paso])
    assert cognitive.rejected_actions, "el filtro debió acumular el rechazo"

    n = await runtime._flush_rejections(mission)
    assert n == 1
    topicos = {t for t, _ in bus.events}
    assert "replan.action_rejected" in topicos
    assert audit.rows and audit.rows[0][0] == "replan.action_rejected"
    assert audit.rows[0][2] == mission.id


@pytest.mark.asyncio
async def test_g2_02_el_evento_trae_la_provenance_pedida(tmp_path, db):
    from alexis.core.runtime import AlexisRuntime
    from alexis.storage.repositories import AuditRepository
    from alexis.verification import FilesystemVerifier

    # `audit_log.mission_id` tiene FK a `missions`: la misión se persiste primero, como
    # hace `run_mission` en producción. No es un truco del test, es la regla del esquema.
    await db.open()
    await db.migrate()
    from alexis.storage.repositories import MissionRepository

    mission = _mission()
    await MissionRepository(db).upsert(mission)   # la MISMA misión que se audita
    cognitive = _cognitive()
    bus, audit = _Bus(), AuditRepository(db)
    runtime = AlexisRuntime(
        planner=None, policy=PolicyEngine(), executor=None,
        verifier=FilesystemVerifier(workspace=tmp_path), memory=None, learning=None,
        event_bus=bus, audit_repo=audit, cognitive=cognitive,
    )
    knowledge = cognitive.knowledge_for(mission)
    paso = _read()
    cognitive.record_attempt(knowledge, paso, success=False, error="no existe",
                             generation=0, context_version=1)
    cognitive.store_knowledge(mission, knowledge)
    knowledge.needs_replan = True
    cognitive.options(mission, knowledge, pending_steps=[paso])
    await runtime._flush_rejections(mission)

    filas = await audit.list(limit=10)
    fila = filas[0]["details"]
    # `mission_id` va en su propia columna de `audit_log` (con FK a `missions`), que es el
    # diseño del esquema; el resto de la provenance va en `details`.
    assert filas[0]["mission_id"] == mission.id
    assert filas[0]["event"] == "replan.action_rejected"
    for clave in ("iteration", "replan_count", "action_signature", "capability",
                  "step_id", "context_id", "context_version", "reason", "timestamp"):
        assert clave in fila, f"falta {clave} en la auditoría"
    # Y el evento del bus lleva el mission_id dentro del payload, que es lo que consume la UI.
    evento = dict(bus.events)["replan.action_rejected"]
    assert evento["mission_id"] == mission.id
    assert fila["reason"] and "falló" in fila["reason"]
    assert "chain" not in str(fila).lower(), "no se audita razonamiento"
    await db.close()


@pytest.mark.asyncio
async def test_g2_03_la_auditoria_sobrevive_al_reinicio(tmp_path, db):
    """La fila queda en `audit_log`, que es lo que se consulta tras reiniciar."""
    from alexis.core.runtime import AlexisRuntime
    from alexis.storage.repositories import AuditRepository
    from alexis.verification import FilesystemVerifier

    await db.open()
    await db.migrate()
    real = AuditRepository(db)
    bus = _Bus()
    cognitive = _cognitive()
    runtime = AlexisRuntime(
        planner=None, policy=PolicyEngine(), executor=None,
        verifier=FilesystemVerifier(workspace=tmp_path), memory=None, learning=None,
        event_bus=bus, audit_repo=real, cognitive=cognitive,
    )
    from alexis.storage.repositories import MissionRepository

    mission = _mission()
    await MissionRepository(db).upsert(mission)
    knowledge = cognitive.knowledge_for(mission)
    paso = _read()
    cognitive.record_attempt(knowledge, paso, success=False, error="no existe")
    knowledge.needs_replan = True
    cognitive.options(mission, knowledge, pending_steps=[paso])
    await runtime._flush_rejections(mission)

    # "Reinicio": otro objeto, otra conexión, y la auditoría sigue ahí.
    from alexis.storage.db import Database

    otra = Database(dsn=db.dsn)
    await otra.open()
    filas = await AuditRepository(otra).list(limit=10)
    assert any(f["event"] == "replan.action_rejected" for f in filas), filas
    # Y el motivo se puede leer, que es lo que se pedía poder responder.
    detalle = next(f["details"] for f in filas if f["event"] == "replan.action_rejected")
    assert detalle["reason"]
    await otra.close()
    await db.close()


@pytest.mark.asyncio
async def test_g2_04_queda_tambien_en_mission_context(tmp_path):
    """Sin base de datos, el motivo sigue disponible en el contexto de la misión."""
    from alexis.core.runtime import AlexisRuntime
    from alexis.verification import FilesystemVerifier

    cognitive = _cognitive()
    runtime = AlexisRuntime(
        planner=None, policy=PolicyEngine(), executor=None,
        verifier=FilesystemVerifier(workspace=tmp_path), memory=None, learning=None,
        event_bus=_Bus(), audit_repo=None, cognitive=cognitive,
    )
    mission = _mission()
    knowledge = cognitive.knowledge_for(mission)
    paso = _read()
    cognitive.record_attempt(knowledge, paso, success=False, error="no existe")
    knowledge.needs_replan = True
    cognitive.options(mission, knowledge, pending_steps=[paso])
    await runtime._flush_rejections(mission)

    guardados = mission.context["replan_rejections"]
    assert guardados and guardados[-1]["reason"]

    # Y tras "reiniciar" (rehidratar la misión) siguen ahí.
    from alexis.storage.serialization import mission_from_row, mission_to_row

    recuperado = mission_from_row(mission_to_row(mission))
    assert recuperado.context["replan_rejections"][-1]["reason"]


def test_g2_05_el_filtro_sigue_siendo_solo_una_decision_de_cognicion():
    """Ni Policy, ni envelope, ni registry se tocan por el filtro."""
    cognitive = _cognitive()
    mission = _mission()
    envelope_before = list(mission.envelope.allowed_actions)
    policy_id = id(cognitive.policy)
    registry_before = {s.id for s in (cognitive.catalog.enabled() if cognitive.catalog else [])}

    knowledge = cognitive.knowledge_for(mission)
    paso = _read()
    cognitive.record_attempt(knowledge, paso, success=False, error="no existe")
    knowledge.needs_replan = True
    cognitive.options(mission, knowledge, pending_steps=[paso])
    cognitive.drain_rejections()

    assert mission.envelope.allowed_actions == envelope_before
    assert id(cognitive.policy) == policy_id
    assert {s.id for s in (cognitive.catalog.enabled() if cognitive.catalog else [])} == registry_before
    assert not hasattr(knowledge, "policy")
    assert not mission.context.get("approved_step_ids")


# =========================================================================== #
# GAP 2 — integración: el ciclo completo por run_mission
# =========================================================================== #


@pytest.mark.asyncio
async def test_g2_06_run_mission_real_producece_la_auditoria(tmp_path, db):
    """El filtro y la auditoría, en el flujo de producción, no sólo en unidad.

    Nada de mocks: el executor es un `SandboxExecutor` real sobre un workspace donde
    `notas.txt` NO existe, así que el primer `fs.read` falla de verdad. El plan tiene un
    segundo paso con la MISMA firma, que es exactamente la repetición que §12.5 prohíbe.
    Se comprueba que: el rechazo ocurrió en producción, quedó en `audit_log` con la
    infra real, y la segunda lectura nunca llegó a ejecutarse.
    """
    from alexis.autonomy.gates import AutonomyGate
    from alexis.contracts import Plan
    from alexis.core.runtime import AlexisRuntime
    from alexis.events.bus import EventBus
    from alexis.execution import SandboxExecutor
    from alexis.learning.system import ExperienceLearner
    from alexis.memory.store import InMemoryMemory
    from alexis.security.sandbox import SandboxRunner
    from alexis.storage.repositories import AuditRepository, MissionRepository
    from alexis.tools.filesystem import build_filesystem_tools
    from alexis.tools.registry import ToolRegistry
    from alexis.verification import FilesystemVerifier

    await db.open()
    await db.migrate()
    audit = AuditRepository(db)

    registry = ToolRegistry()
    registry.register_all(build_filesystem_tools(tmp_path))
    executor = SandboxExecutor(tools=registry, sandbox=SandboxRunner(tmp_path))
    cognitive = CognitiveRuntime(
        policy=PolicyEngine(), gate=AutonomyGate(), executor=executor,
        verifier=FilesystemVerifier(workspace=tmp_path), world=WorldModel(),
    )
    runtime = AlexisRuntime(
        planner=None, policy=PolicyEngine(), executor=executor,
        verifier=FilesystemVerifier(workspace=tmp_path), memory=InMemoryMemory(),
        learning=ExperienceLearner(), event_bus=EventBus(), audit_repo=audit,
        mission_repo=MissionRepository(db),   # sin esto la misión no existe en `missions`
        cognitive=cognitive,
    )

    mission = MissionEngine().create(
        "lee notas.txt dos veces",
        MissionEnvelope(objective="lee notas.txt dos veces", autonomy=AutonomyLevel.SUPERVISED,
                        allowed_actions=list(ACTIONS)),
    )
    # Los ids de paso pasan por el `PlanValidator` real: sin guion.
    mission.plan = Plan(mission.id, [_read("notas.txt", "leer_1"),
                                    _read("notas.txt", "leer_2")])
    await runtime.run_mission(mission)

    # 1) El rechazo ocurrió en el flujo real.
    rechazos = mission.context.get("replan_rejections") or []
    assert rechazos, "el ciclo real debió registrar un rechazo"
    # 2) Quedó en `audit_log` con la infra real, con su motivo.
    filas = [f for f in await audit.list(limit=50)
             if f["event"] == "replan.action_rejected" and f["mission_id"] == mission.id]
    assert filas, "no hay fila replan.action_rejected para esta misión"
    assert filas[0]["details"]["reason"]
    # 3) Y la repetición NO se ejecutó: el fallo real está, el segundo intento no.
    fallidas = [a for a in cognitive.knowledge_for(mission).action_attempts
                if not a["success"]]
    assert len(fallidas) == 1, fallidas
    await db.close()
