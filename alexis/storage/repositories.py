import json

from alexis.contracts import Checkpoint, Execution, Mission, Task, TaskState
from alexis.storage.db import Database
from alexis.storage.serialization import (
    mission_from_row,
    mission_to_row,
    task_from_row,
    task_to_row,
)


class MissionRepository:
    def __init__(self, db: Database):
        self.db = db

    async def upsert(self, mission: Mission):
        row = mission_to_row(mission)
        await self.db.execute(
            """
            INSERT INTO missions (id, state, autonomy, envelope, goal, context, results, updated_at)
            VALUES (%(id)s, %(state)s, %(autonomy)s, %(envelope)s::jsonb, %(goal)s::jsonb, %(context)s::jsonb, %(results)s::jsonb, now())
            ON CONFLICT (id) DO UPDATE SET
                state = EXCLUDED.state,
                autonomy = EXCLUDED.autonomy,
                envelope = EXCLUDED.envelope,
                goal = EXCLUDED.goal,
                context = EXCLUDED.context,
                results = EXCLUDED.results,
                updated_at = now()
            """,
            row,
        )

    async def get(self, mission_id: str) -> Mission | None:
        rows = await self.db.fetch("SELECT * FROM missions WHERE id = %(id)s", {"id": mission_id})
        return mission_from_row(rows[0]) if rows else None

    async def list(self, limit: int = 50) -> list[Mission]:
        rows = await self.db.fetch(
            "SELECT * FROM missions ORDER BY created_at DESC LIMIT %(limit)s",
            {"limit": limit},
        )
        return [mission_from_row(row) for row in rows]

    async def next_pending(self) -> Mission | None:
        """Devuelve la misión pendiente más antigua (cola FIFO persistente)."""
        rows = await self.db.fetch(
            "SELECT * FROM missions WHERE state = 'pending' ORDER BY created_at ASC LIMIT 1",
            {},
        )
        return mission_from_row(rows[0]) if rows else None


class EventRepository:
    def __init__(self, db: Database):
        self.db = db

    async def append(self, topic: str, payload, mission_id: str | None = None):
        await self.db.execute(
            """
            INSERT INTO mission_events (mission_id, topic, payload)
            VALUES (%(mission_id)s, %(topic)s, %(payload)s::jsonb)
            """,
            {
                "mission_id": mission_id,
                "topic": topic,
                "payload": json.dumps(payload, ensure_ascii=False),
            },
        )

    async def list(self, mission_id: str | None = None, limit: int = 100):
        if mission_id:
            rows = await self.db.fetch(
                "SELECT * FROM mission_events WHERE mission_id = %(mission_id)s ORDER BY id ASC LIMIT %(limit)s",
                {"mission_id": mission_id, "limit": limit},
            )
        else:
            rows = await self.db.fetch(
                "SELECT * FROM mission_events ORDER BY id DESC LIMIT %(limit)s",
                {"limit": limit},
            )
        return rows


class AuditRepository:
    def __init__(self, db: Database):
        self.db = db

    async def record(self, event: str, actor: str, mission_id: str | None = None, **details):
        await self.db.execute(
            """
            INSERT INTO audit_log (event, actor, mission_id, details)
            VALUES (%(event)s, %(actor)s, %(mission_id)s, %(details)s::jsonb)
            """,
            {
                "event": event,
                "actor": actor,
                "mission_id": mission_id,
                "details": json.dumps(details, ensure_ascii=False, default=str),
            },
        )

    async def list(self, limit: int = 100):
        return await self.db.fetch(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT %(limit)s",
            {"limit": limit},
        )


class TaskRepository:
    def __init__(self, db: Database):
        self.db = db

    async def upsert(self, task: Task):
        row = task_to_row(task)
        await self.db.execute(
            """
            INSERT INTO tasks (id, mission_id, kind, agent, tool, args, status, attempts, max_attempts, error,
                               result, lease_until, deadline, updated_at)
            VALUES (%(id)s, %(mission_id)s, %(kind)s, %(agent)s, %(tool)s, %(args)s::jsonb, %(status)s,
                    %(attempts)s, %(max_attempts)s, %(error)s, %(result)s::jsonb, %(lease_until)s, %(deadline)s, now())
            ON CONFLICT (id) DO UPDATE SET
                kind = EXCLUDED.kind,
                agent = EXCLUDED.agent,
                tool = EXCLUDED.tool,
                args = EXCLUDED.args,
                status = EXCLUDED.status,
                attempts = EXCLUDED.attempts,
                max_attempts = EXCLUDED.max_attempts,
                error = EXCLUDED.error,
                result = EXCLUDED.result,
                lease_until = EXCLUDED.lease_until,
                deadline = EXCLUDED.deadline,
                updated_at = now()
            """,
            row,
        )

    async def get(self, task_id: str) -> Task | None:
        rows = await self.db.fetch("SELECT * FROM tasks WHERE id = %(id)s", {"id": task_id})
        return task_from_row(rows[0]) if rows else None

    async def list(self, mission_id: str | None = None, status: str | None = None, limit: int = 100):
        query = "SELECT * FROM tasks"
        clauses, params = [], {}
        if mission_id:
            clauses.append("mission_id = %(mission_id)s")
            params["mission_id"] = mission_id
        if status:
            clauses.append("status = %(status)s")
            params["status"] = status
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at ASC LIMIT %(limit)s"
        params["limit"] = limit
        rows = await self.db.fetch(query, params)
        return [task_from_row(row) for row in rows]

    async def next_claimable(self, mission_id: str | None = None) -> Task | None:
        clause = "status IN ('pending', 'queued', 'retrying')"
        params: dict = {}
        if mission_id:
            clause += " AND mission_id = %(mission_id)s"
            params["mission_id"] = mission_id
        rows = await self.db.fetch(
            f"SELECT * FROM tasks WHERE {clause} ORDER BY created_at ASC LIMIT 1",
            params,
        )
        return task_from_row(rows[0]) if rows else None


class ExecutionRepository:
    def __init__(self, db: Database):
        self.db = db

    async def insert(self, execu: Execution):
        await self.db.execute(
            """
            INSERT INTO executions (task_id, tool, args_hash, ok, output, error, finished_at)
            VALUES (%(task_id)s, %(tool)s, %(args_hash)s, %(ok)s, %(output)s::jsonb, %(error)s, now())
            """,
            {
                "task_id": execu.task_id,
                "tool": execu.tool,
                "args_hash": execu.args_hash,
                "ok": execu.ok,
                "output": json.dumps(execu.output, ensure_ascii=False, default=str),
                "error": execu.error,
            },
        )

    async def list(self, task_id: str) -> list:
        return await self.db.fetch(
            "SELECT * FROM executions WHERE task_id = %(task_id)s ORDER BY id ASC",
            {"task_id": task_id},
        )


class ObservationRepository:
    def __init__(self, db: Database):
        self.db = db

    async def insert(self, mission_id: str, source: str, content, trusted: bool = False):
        await self.db.execute(
            """
            INSERT INTO observations (mission_id, source, content, trusted)
            VALUES (%(mission_id)s, %(source)s, %(content)s::jsonb, %(trusted)s)
            """,
            {
                "mission_id": mission_id,
                "source": source,
                "content": json.dumps(content, ensure_ascii=False, default=str),
                "trusted": trusted,
            },
        )

    async def list(self, mission_id: str, limit: int = 100) -> list:
        return await self.db.fetch(
            "SELECT * FROM observations WHERE mission_id = %(mission_id)s ORDER BY id ASC LIMIT %(limit)s",
            {"mission_id": mission_id, "limit": limit},
        )


class VerificationRepository:
    def __init__(self, db: Database):
        self.db = db

    async def insert(
        self,
        mission_id: str,
        passed: bool,
        confidence: float,
        verifier: str,
        evidence: list,
        notes: str = "",
    ):
        await self.db.execute(
            """
            INSERT INTO verifications (mission_id, passed, confidence, verifier, evidence, notes)
            VALUES (%(mission_id)s, %(passed)s, %(confidence)s, %(verifier)s, %(evidence)s::jsonb, %(notes)s)
            """,
            {
                "mission_id": mission_id,
                "passed": passed,
                "confidence": confidence,
                "verifier": verifier,
                "evidence": json.dumps(evidence, ensure_ascii=False, default=str),
                "notes": notes,
            },
        )

    async def list(self, mission_id: str, limit: int = 50) -> list:
        return await self.db.fetch(
            "SELECT * FROM verifications WHERE mission_id = %(mission_id)s ORDER BY id ASC LIMIT %(limit)s",
            {"mission_id": mission_id, "limit": limit},
        )


class CheckpointRepository:
    def __init__(self, db: Database):
        self.db = db

    async def save(self, checkpoint: Checkpoint):
        await self.db.execute(
            """
            INSERT INTO checkpoints (mission_id, step_index, payload)
            VALUES (%(mission_id)s, %(step_index)s, %(payload)s::jsonb)
            """,
            {
                "mission_id": checkpoint.mission_id,
                "step_index": checkpoint.step_index,
                "payload": json.dumps(checkpoint.payload, ensure_ascii=False, default=str),
            },
        )

    async def latest(self, mission_id: str) -> dict | None:
        rows = await self.db.fetch(
            "SELECT * FROM checkpoints WHERE mission_id = %(mission_id)s ORDER BY step_index DESC LIMIT 1",
            {"mission_id": mission_id},
        )
        return rows[0] if rows else None

    async def close_open_tasks(self, mission_id: str, status: str = "cancelled"):
        await self.db.execute(
            """
            UPDATE tasks SET status = %(status)s, updated_at = now()
            WHERE mission_id = %(mission_id)s AND status IN ('pending', 'queued', 'retrying')
            """,
            {"mission_id": mission_id, "status": status},
        )

class WorldRepository:
    """Almacén persistente del World Model (P0 §4.3).

    Éste es el store de AUTORIDAD del mundo. `mission.context["world"]` sigue existiendo
    como proyección de compatibilidad, pero no manda: se puede perder, truncarse a 50 o no
    existir, y el mundo se recupera igual desde aquí.

    Respeta los dos incrementos anteriores como contrato, no como detalle:

    - **Identidad** `(scope_id, entity_id)`, que es la clave primaria de la tabla. El path
      relativo no basta: dos proyectos pueden tener `file:notas.txt`.
    - **Temporalidad** `last_seen` en `DOUBLE PRECISION`, que vuelve exacto. El `0.0` de
      `LAST_SEEN_UNKNOWN` se persiste como `0.0`, nunca como `now()`: una entidad sin
      fecha conocida debe seguir sin fecha conocida después de un reinicio.

    No escribe en `observations`. Allí vive la evidencia cruda con su procedencia; aquí el
    estado ya interpretado. Son dos cosas distintas con dos ciclos de vida distintos.
    """

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ #
    # Entidades
    # ------------------------------------------------------------------ #

    async def upsert_entity(self, entity) -> None:
        """Guarda una entidad en su ámbito. La clave primaria decide la unicidad.

        `last_seen` se escribe tal cual, incluido `0.0`. No se usa `now()` como default:
        la edad desconocida es información, y rellenarla sería mentir sobre el freshness.
        """
        await self.db.execute(
            """
            INSERT INTO world_entities
                (scope_id, entity_id, kind, name, attributes, source, confidence,
                 mission_id, observations, last_seen,
                 base_confidence, support, contradictions, evidence_ids, conflicts,
                 value_counts)
            VALUES (%(scope_id)s, %(entity_id)s, %(kind)s, %(name)s, %(attributes)s::jsonb,
                    %(source)s, %(confidence)s, %(mission_id)s, %(observations)s, %(last_seen)s,
                    %(base_confidence)s, %(support)s, %(contradictions)s,
                    %(evidence_ids)s::jsonb, %(conflicts)s::jsonb, %(value_counts)s::jsonb)
            ON CONFLICT (scope_id, entity_id) DO UPDATE SET
                kind = EXCLUDED.kind,
                name = EXCLUDED.name,
                attributes = EXCLUDED.attributes,
                source = EXCLUDED.source,
                confidence = EXCLUDED.confidence,
                mission_id = EXCLUDED.mission_id,
                observations = EXCLUDED.observations,
                last_seen = EXCLUDED.last_seen,
                base_confidence = EXCLUDED.base_confidence,
                support = EXCLUDED.support,
                contradictions = EXCLUDED.contradictions,
                evidence_ids = EXCLUDED.evidence_ids,
                conflicts = EXCLUDED.conflicts,
                value_counts = EXCLUDED.value_counts
            """,
            # El ámbito sale de la ENTIDAD, no del argumento: así no puede acabarse
            # escribiendo en el ámbito equivocado por descuido. Es el mismo motivo por el
            # que la clave del `WorldModel` usa `entity.scope`.
            {"scope_id": entity.scope, **_entity_params(entity)},
        )

    async def get_entity(self, entity_id: str, *, scope: str) -> object | None:
        rows = await self.db.fetch(
            """
            SELECT * FROM world_entities
            WHERE scope_id = %(scope_id)s AND entity_id = %(entity_id)s
            """,
            {"scope_id": scope, "entity_id": entity_id},
        )
        return _entity_from_row(rows[0]) if rows else None

    async def list_entities(self, *, scope: str, kind: str | None = None,
                            limit: int | None = None) -> list:
        """Entidades de UN ámbito.

        `limit=None` por defecto, a propósito: el tope de 50 era de la proyección en
        `mission.context`, no una propiedad del mundo. Un proyecto con 400 archivos tiene
        400 entidades que conocer, y truncarlas sin avisar sería pérdida de conocimiento
        disfrazada de detalle de implementación. Aquí no se recorta nada salvo que se pida.
        """
        clauses = ["scope_id = %(scope_id)s"]
        params: dict = {"scope_id": scope}
        if kind:
            clauses.append("kind = %(kind)s")
            params["kind"] = kind
        sql = f"SELECT * FROM world_entities WHERE {' AND '.join(clauses)}"
        sql += " ORDER BY last_seen DESC, entity_id ASC"
        if limit is not None:
            sql += " LIMIT %(limit)s"
            params["limit"] = limit
        return [_entity_from_row(r) for r in await self.db.fetch(sql, params)]

    async def count_entities(self, *, scope: str) -> int:
        rows = await self.db.fetch(
            "SELECT count(*) AS n FROM world_entities WHERE scope_id = %(scope_id)s",
            {"scope_id": scope},
        )
        return int(rows[0]["n"]) if rows else 0

    async def delete_entity(self, entity_id: str, *, scope: str) -> bool:
        """Borra la entidad de su ámbito. `False` si no estaba.

        No se usa en el camino normal: §4.2/[§4.5] decidirán cuándo un hecho deja de ser
        válido. Existe para que la baja sea explícita y no un `DELETE` repartido por el
        código.
        """
        rows = await self.db.fetch(
            "DELETE FROM world_entities WHERE scope_id = %(scope_id)s AND entity_id = %(entity_id)s RETURNING entity_id",
            {"scope_id": scope, "entity_id": entity_id},
        )
        return bool(rows)

    # ------------------------------------------------------------------ #
    # Relaciones
    # ------------------------------------------------------------------ #

    async def save_edge(self, parent_id: str, child_id: str, relation: str = "depends_on",
                        *, scope: str) -> None:
        await self.db.execute(
            """
            INSERT INTO world_edges (scope_id, parent_id, child_id, relation)
            VALUES (%(scope_id)s, %(parent_id)s, %(child_id)s, %(relation)s)
            ON CONFLICT (scope_id, parent_id, child_id, relation) DO NOTHING
            """,
            {"scope_id": scope, "parent_id": parent_id, "child_id": child_id,
             "relation": relation},
        )

    async def list_edges(self, *, scope: str) -> list[tuple[str, str, str]]:
        rows = await self.db.fetch(
            """
            SELECT parent_id, child_id, relation FROM world_edges
            WHERE scope_id = %(scope_id)s
            ORDER BY parent_id ASC, child_id ASC, relation ASC
            """,
            {"scope_id": scope},
        )
        return [(r["parent_id"], r["child_id"], r["relation"]) for r in rows]

    async def dependencies(self, entity_id: str, *, scope: str) -> list[str]:
        rows = await self.db.fetch(
            """
            SELECT child_id FROM world_edges
            WHERE scope_id = %(scope_id)s AND parent_id = %(parent_id)s AND relation = 'depends_on'
            ORDER BY child_id ASC
            """,
            {"scope_id": scope, "parent_id": entity_id},
        )
        return [r["child_id"] for r in rows]

    async def neighbors(self, entity_id: str, *, scope: str) -> list[str]:
        rows = await self.db.fetch(
            """
            SELECT DISTINCT other_id FROM (
                SELECT child_id AS other_id FROM world_edges
                WHERE scope_id = %(scope_id)s AND parent_id = %(entity_id)s
                UNION
                SELECT parent_id AS other_id FROM world_edges
                WHERE scope_id = %(scope_id)s AND child_id = %(entity_id)s
            ) t ORDER BY other_id ASC
            """,
            {"scope_id": scope, "entity_id": entity_id},
        )
        return [r["other_id"] for r in rows]

    # ------------------------------------------------------------------ #
    # Guardado completo, atómico
    # ------------------------------------------------------------------ #

    async def save_snapshot(self, entities, edges=(), *, scope: str) -> int:
        """Guarda entidades Y relaciones en una sola transacción.

        Un `WorldModel` se persiste entero o no se persiste: guardar medio grafo deja
        relaciones que apuntan a entidades que no existen, y eso no se ve hasta que algo
        lo consulta. Se usa la transacción del driver, no una secuencia de `execute()`.

        Sólo se escribe lo que viene en `entities`: no se vacía el ámbito. La decisión de
        qué es válido (§4.5) es de otro incremento, y un borrado masivo desde aquí sería
        destruir conocimiento por no haber decidido todavía.
        """
        async with self.db.transaction() as conn:
            for entity in entities:
                await conn.execute(
                    """
                    INSERT INTO world_entities
                        (scope_id, entity_id, kind, name, attributes, source, confidence,
                         mission_id, observations, last_seen,
                         base_confidence, support, contradictions, evidence_ids, conflicts,
                        value_counts)
                    VALUES (%(scope_id)s, %(entity_id)s, %(kind)s, %(name)s, %(attributes)s::jsonb,
                            %(source)s, %(confidence)s, %(mission_id)s, %(observations)s, %(last_seen)s,
                            %(base_confidence)s, %(support)s, %(contradictions)s,
                            %(evidence_ids)s::jsonb, %(conflicts)s::jsonb, %(value_counts)s::jsonb)
                    ON CONFLICT (scope_id, entity_id) DO UPDATE SET
                        kind = EXCLUDED.kind,
                        name = EXCLUDED.name,
                        attributes = EXCLUDED.attributes,
                        source = EXCLUDED.source,
                        confidence = EXCLUDED.confidence,
                        mission_id = EXCLUDED.mission_id,
                        observations = EXCLUDED.observations,
                        last_seen = EXCLUDED.last_seen,
                        base_confidence = EXCLUDED.base_confidence,
                        support = EXCLUDED.support,
                        contradictions = EXCLUDED.contradictions,
                        evidence_ids = EXCLUDED.evidence_ids,
                        conflicts = EXCLUDED.conflicts,
                        value_counts = EXCLUDED.value_counts
                    """,
                    {**_entity_params(entity), "scope_id": scope},
                )
            for parent_id, child_id, *rest in edges:
                relation = rest[0] if rest else "depends_on"
                await conn.execute(
                    """
                    INSERT INTO world_edges (scope_id, parent_id, child_id, relation)
                    VALUES (%(scope_id)s, %(parent_id)s, %(child_id)s, %(relation)s)
                    ON CONFLICT (scope_id, parent_id, child_id, relation) DO NOTHING
                    """,
                    {"scope_id": scope, "parent_id": parent_id, "child_id": child_id,
                     "relation": relation},
                )
        return len(entities)


def _entity_params(entity) -> dict:
    """Fila de parámetros de una entidad. Compartido por `upsert_entity` y
    `save_snapshot` para que las dos vías no puedan separarse nunca."""
    return {
        "entity_id": entity.id,
        "kind": entity.kind,
        "name": entity.name,
        "attributes": json.dumps(entity.attributes, ensure_ascii=False, default=str),
        "source": entity.source,
        "confidence": float(entity.confidence),
        "mission_id": entity.mission_id,
        "observations": int(entity.observations),
        "last_seen": float(entity.last_seen),
        "base_confidence": float(getattr(entity, "base_confidence", 0.0) or 0.0),
        "support": int(getattr(entity, "support", 1) or 0),
        "contradictions": int(getattr(entity, "contradictions", 0) or 0),
        "evidence_ids": json.dumps(list(getattr(entity, "evidence_ids", []) or []),
                                   ensure_ascii=False),
        "conflicts": json.dumps([dict(c) for c in (getattr(entity, "conflicts", []) or [])],
                                ensure_ascii=False, default=str),
        "value_counts": json.dumps(getattr(entity, "value_counts", {}) or {},
                                   ensure_ascii=False, default=str),
    }


def _entity_from_row(row) -> object:
    """Fila de `world_entities` → `WorldEntity`.

    Delega en `WorldEntity.from_dict` (§4.2), que es el punto único de rehidratación y
    donde vive el trato de `last_seen`: un `0.0` de la base sigue siendo "edad
    desconocida", no "observado ahora". `scope_id` de la columna pasa a `scope` porque ése
    es el nombre del campo en la entidad.
    """
    from alexis.world.model import WorldEntity

    return WorldEntity.from_dict({
        "id": row["entity_id"],
        "kind": row["kind"],
        "name": row["name"],
        "attributes": row["attributes"] or {},
        "source": row["source"],
        "confidence": row["confidence"],
        "mission_id": row["mission_id"],
        "observations": row["observations"],
        "last_seen": row["last_seen"],
        "scope": row["scope_id"],
        "base_confidence": row.get("base_confidence") or 0.0,
        "support": row.get("support") or 1,
        "contradictions": row.get("contradictions") or 0,
        "evidence_ids": row.get("evidence_ids") or [],
        "conflicts": row.get("conflicts") or [],
        "value_counts": row.get("value_counts") or {},
    })


class LearningRepository:
    """Persistencia del ciclo de aprendizaje (CORE-11).

    Reutiliza la misma capa y la misma conexión que el resto del storage: no hay una segunda
    base de datos para el aprendizaje, porque dos almacenes que guardan el mismo conocimiento
    acaban discrepando y entonces ninguno es verdad.

    `skill_versions` NO tiene `update` a propósito. Una versión publicada es inmutable; una
    mejora es una versión nueva. Si se pudiera sobrescribir, una ejecución antigua dejaría de
    poder reconstruirse contra la skill que realmente se usó, que es justo para lo que sirve un
    registro de experiencia.
    """

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ #
    # Lecciones
    # ------------------------------------------------------------------ #

    async def save_lesson(self, lesson) -> bool:
        """Guarda la lección. `False` si ya existía: una lección es idempotente por id."""
        await self.db.execute(
            """
            INSERT INTO lessons (lesson_id, statement, scope, source_experience, outcome,
                                 confidence, payload)
            VALUES (%(lesson_id)s, %(statement)s, %(scope)s, %(source_experience)s, %(outcome)s,
                    %(confidence)s, %(payload)s::jsonb)
            ON CONFLICT (lesson_id) DO NOTHING
            """,
            {
                "lesson_id": lesson.lesson_id,
                "statement": lesson.statement,
                "scope": lesson.scope or "",
                "source_experience": lesson.source_experience or "",
                "outcome": lesson.outcome,
                "confidence": float(lesson.confidence or 0.0),
                "payload": json.dumps(lesson.to_dict(), ensure_ascii=False, default=str),
            },
        )
        return True

    async def lessons(self, *, limit: int = 200, scope: str | None = None) -> list[dict]:
        if scope:
            rows = await self.db.fetch(
                "SELECT payload FROM lessons WHERE scope = %(scope)s ORDER BY created_at DESC LIMIT %(limit)s",
                {"scope": scope, "limit": limit},
            )
        else:
            rows = await self.db.fetch(
                "SELECT payload FROM lessons ORDER BY created_at DESC LIMIT %(limit)s",
                {"limit": limit},
            )
        return [json.loads(row["payload"]) for row in rows]

    # ------------------------------------------------------------------ #
    # Candidatas
    # ------------------------------------------------------------------ #

    async def save_candidate(self, candidate) -> bool:
        await self.db.execute(
            """
            INSERT INTO skill_candidates (candidate_id, name, status, payload)
            VALUES (%(candidate_id)s, %(name)s, %(status)s, %(payload)s::jsonb)
            ON CONFLICT (candidate_id) DO UPDATE SET status = EXCLUDED.status
            """,
            {
                "candidate_id": candidate.candidate_id,
                "name": candidate.name,
                "status": candidate.status,
                "payload": json.dumps(candidate.to_dict(), ensure_ascii=False, default=str),
            },
        )
        return True

    async def candidates(self, *, status: str | None = None, limit: int = 200) -> list[dict]:
        if status:
            rows = await self.db.fetch(
                "SELECT payload FROM skill_candidates WHERE status = %(status)s ORDER BY created_at DESC LIMIT %(limit)s",
                {"status": status, "limit": limit},
            )
        else:
            rows = await self.db.fetch(
                "SELECT payload FROM skill_candidates ORDER BY created_at DESC LIMIT %(limit)s",
                {"limit": limit},
            )
        return [json.loads(row["payload"]) for row in rows]

    # ------------------------------------------------------------------ #
    # Versiones (inmutables)
    # ------------------------------------------------------------------ #

    async def save_skill_version(self, version) -> bool:
        await self.db.execute(
            """
            INSERT INTO skill_versions (skill_id, version, name, status, payload)
            VALUES (%(skill_id)s, %(version)s, %(name)s, %(status)s, %(payload)s::jsonb)
            ON CONFLICT (skill_id, version) DO NOTHING
            """,
            {
                "skill_id": version.skill_id,
                "version": version.version,
                "name": version.name,
                "status": version.status,
                "payload": json.dumps(version.to_dict(), ensure_ascii=False, default=str),
            },
        )
        return True

    async def skill_versions(self, *, status: str | None = None, limit: int = 200) -> list[dict]:
        """Las skills utilizables. Superseded sigue saliendo: se puede auditar qué se usó."""
        if status:
            rows = await self.db.fetch(
                "SELECT payload FROM skill_versions WHERE status = %(status)s ORDER BY created_at DESC LIMIT %(limit)s",
                {"status": status, "limit": limit},
            )
        else:
            rows = await self.db.fetch(
                "SELECT payload FROM skill_versions ORDER BY created_at DESC LIMIT %(limit)s",
                {"limit": limit},
            )
        return [json.loads(row["payload"]) for row in rows]

    # ------------------------------------------------------------------ #
    # Rendimiento
    # ------------------------------------------------------------------ #

    async def save_performance(self, record) -> bool:
        await self.db.execute(
            """
            INSERT INTO skill_performance (skill_id, version, mission_id, verified, payload)
            VALUES (%(skill_id)s, %(version)s, %(mission_id)s, %(verified)s, %(payload)s::jsonb)
            """,
            {
                "skill_id": record.skill_id,
                "version": record.version,
                "mission_id": record.mission_id,
                "verified": bool(record.verified),
                "payload": json.dumps(record.to_dict(), ensure_ascii=False, default=str),
            },
        )
        return True

    async def performance(self, *, skill_id: str, version: int | None = None, limit: int = 500) -> list[dict]:
        if version is not None:
            rows = await self.db.fetch(
                """
                SELECT payload FROM skill_performance
                WHERE skill_id = %(skill_id)s AND version = %(version)s
                ORDER BY created_at DESC LIMIT %(limit)s
                """,
                {"skill_id": skill_id, "version": version, "limit": limit},
            )
        else:
            rows = await self.db.fetch(
                """
                SELECT payload FROM skill_performance
                WHERE skill_id = %(skill_id)s ORDER BY created_at DESC LIMIT %(limit)s
                """,
                {"skill_id": skill_id, "limit": limit},
            )
        return [json.loads(row["payload"]) for row in rows]
