import logging
import time

from alexis.autonomy.goal_state import settle
from alexis.contracts import Mission, MissionState, RiskLevel, Verification
from alexis.cognition.planner import plan_from_dict, plan_to_dict
from alexis.cognition.planner_model import PlanValidator
from alexis.cognition.state import NextAction
from alexis.learning.experience import EXPERIENCE_SOURCE
from alexis.meta.cognition import MetaCognition

log = logging.getLogger(__name__)


class AlexisRuntime:
    # ------------------------------------------------------------------ #
    # CORE-10 — Autonomous Recovery
    #
    # La regla que gobierna todo lo que hay debajo: CHECKPOINT ≠ VERDAD.
    #
    # Un checkpoint dice "esto creíamos que había ocurrido"; lo que ocurrió de verdad lo dice
    # el mundo, y sólo se pregunta ejecutando una observación real. Por eso la recuperación
    # tiene tres pasos y no dos: RESTORE → INSPECT WORLD → DECIDE. Quitar el del medio la
    # convierte en un replay con pasos, que para `fs.write` es escribir dos veces y para
    # `fs.remove` es borrar algo que quizá alguien ya reemplazó.
    #
    # Este código NO reimplementa el World Model, ni la evidencia, ni el plan, ni la policy:
    # observa con las herramientas que ya existen, se apoya en `CapabilitySpec.side_effects`
    # para saber qué es repetible, y devuelve una decisión que el Core ejecuta por su cuenta.
    # ------------------------------------------------------------------ #

    @staticmethod
    def _probe_path_of(step):
        """El `path` de un paso, si lo tiene: es lo que se observará para confirmar la acción.

        No se inventa un predicado. Se toma el objetivo real de la herramienta, de modo que la
        inspección sea la misma que haría la acción si se repitiera, y no una pregunta aparte
        que pudiera dar una respuesta distinta.
        """
        args = dict(getattr(step, "args", {}) or {})
        path = args.get("path")
        return path if isinstance(path, str) and path.strip() else None

    def _recovery_manager(self):
        """El `RecoveryManager` con el catálogo real, para preguntar por los side effects.

        Se construye por uso y no en el `__init__` porque el catálogo ya está inyectado en
        otra pieza; tomarlo de donde esté evita dos fuentes de verdad para "qué sabe hacer
        ALEXIS".
        """
        from alexis.autonomy.recovery import RecoveryManager

        catalog = getattr(self.cognitive, "catalog", None) if self.cognitive is not None else None
        if catalog is None:
            try:
                from alexis.capabilities import build_catalog

                catalog = build_catalog()
            except Exception:  # noqa: BLE001 — sin catálogo se asume efecto (fail-safe)
                catalog = None
        return RecoveryManager(catalog=catalog)

    async def _inspect_path(self, mission: Mission, path: str):
        """Mira el mundo real con la herramienta real. Devuelve `None` si no se pudo mirar.

        `None` no es "no existe": es "no sé", y recovery lo trata como `UNKNOWN`. Esa
        distinción evita que un fallo de inspección se convierta en un "no ocurrió" que
        provoke una repetición.
        """
        if self.executor is None:
            return None
        from alexis.contracts import PlanStep

        probe = PlanStep(
            id="__recovery_probe__",
            description="inspección de recuperación",
            action="research",
            risk=RiskLevel.LOW,
            agent="critic",
            capability="fs.stat",
            args={"path": path},
        )
        try:
            result = await self.executor.execute(mission, probe, tool_name="fs.stat")
        except Exception as exc:  # noqa: BLE001 — no poder mirar no es "no existe"
            log.warning("recovery: no pude inspeccionar %s (%s)", path, exc)
            return None
        if not result.success or not isinstance(result.output, dict):
            return {"exists": False, "error": str(result.error or "")}
        return {"exists": bool(result.output.get("exists")), "output": result.output}

    async def _recover_mission(self, mission: Mission) -> bool:
        """RESTORE → INSPECT WORLD → DECIDE. Devuelve si la misión puede continuar.

        El orden ES el diseño. Y la precedencia de la decisión también: primero lo que
        protege (preguntar), después lo que evita trabajo inútil (completar), y sólo al final
        lo optimista (reanudar). Invertirlo dejaría pasar una acción destructiva sin confirmar.
        """
        from alexis.autonomy.recovery import (
            ActionStatus,
            LastKnownAction,
            RecoveryDecision,
            RecoveryState,
        )

        manager = self._recovery_manager()
        # §11: una misión que quedó esperando aprobación NO se reanuda por su cuenta. Recovery
        # no concede excepciones: si había una aprobación pendiente antes del corte, sigue
        # pendiente. Continuar aquí ejecutaría algo que una persona todavía no autorizó.
        if mission.state is MissionState.WAITING_APPROVAL:
            mission.context.setdefault("recovery_hold", {})["reason"] = (
                "la misión estaba esperando aprobación antes del corte: no se reanuda sola"
            )
            await self._commit(mission, "recovery.hold", {
                "mission_id": mission.id,
                "reason": mission.context["recovery_hold"]["reason"],
            })
            return False
        state = RecoveryState.from_dict(mission.context.get("recovery")) or RecoveryState(
            mission_id=mission.id, created_at=time.time()
        )
        state.attempt += 1
        state.reason = "reanudación tras corte: el proceso anterior no registró el final"
        state.updated_at = time.time()

        action = LastKnownAction.from_dict(
            mission.context.get("inflight_action") or mission.context.get("last_action")
        )

        # --- INSPECCIÓN: el mundo, no el checkpoint ------------------------- #
        observation = None
        if action is not None and action.probe_path:
            observation = await self._inspect_path(mission, action.probe_path)
        state.observed = {"probe_path": action.probe_path if action else None,
                          "observation": observation}

        # --- QUÉ SE PUEDE AFIRMAR ------------------------------------------- #
        if action is None:
            state.decision_reason = "no quedó ninguna acción a medias; nada que contrastar"
            state.action_status, state.unconfirmed = {}, []
        else:
            status = manager.assess(action, observation)
            state.action_status = {action.step_id: status.value}
            # Aquí van TODAS las acciones sin confirmar,effects o no. Cuáles bloquean lo
            # decide `decide()`, que es quien tiene el mapa `step_id -> capability`: filtrar
            # aquí duplicaría el criterio en dos sitios y basta con que uno se quede viejo
            # para que una lectura se vuelva a ciegas.
            state.unconfirmed = [
                action.step_id
                for step_id, value in state.action_status.items()
                if value in (ActionStatus.UNKNOWN.value, ActionStatus.PARTIAL.value,
                             ActionStatus.CONFLICTED.value)
            ]

        manager.bind_capabilities({action.step_id: action.capability} if action else {})

        # --- DECISIÓN --------------------------------------------------------- #
        goal_ok = False
        if self.cognitive is not None and self.cognitive.goal_verifier is not None:
            goal_ok = bool(getattr(self.cognitive.goal_verifier.verify(mission), "verified", False))

        plan_valid = mission.plan is not None
        if plan_valid and self.plan_validator is not None:
            plan_valid = not self.plan_validator.validate(mission, mission.plan)

        decision = manager.decide(state, goal_satisfied=goal_ok, plan_still_valid=plan_valid)

        # --- AUDIT + PERSISTENCIA --------------------------------------------- #
        mission.context["recovery"] = state.to_dict()
        await self._commit(mission, f"recovery.{decision.value}", {
            "mission_id": mission.id,
            "decision": decision.value,
            "reason": state.decision_reason,
            "attempt": state.attempt,
            "action_status": state.action_status,
            "observed": state.observed,
        })
        await self.events.publish(f"recovery.{decision.value}", {
            "mission_id": mission.id,
            "reason": state.decision_reason,
            "action_status": state.action_status,
        })

        if decision is RecoveryDecision.ABORT:
            mission.state = MissionState.BLOCKED
            mission.context["recovery_aborted"] = state.decision_reason
            await self._commit(mission)
            return False

        if decision is RecoveryDecision.ASK_USER:
            # No se ejecuta nada automáticamente: esa acción sin confirmar es justo lo que
            # tiene que decidir una persona.
            mission.state = MissionState.WAITING_APPROVAL
            mission.context["recovery_ask"] = {
                "reason": state.decision_reason,
                "steps": list(state.unconfirmed),
                "capability": action.capability if action else None,
            }
            await self._commit(mission)
            return False

        if decision is RecoveryDecision.COMPLETE:
            return True

        # RESUME / REPLAN: lo confirmado no se repite.
        if action is not None:
            status = state.action_status.get(action.step_id)
            if status == ActionStatus.EXECUTED.value:
                # Se traduce "el mundo lo vio hecho" al idioma que el Core entiende, para que
                # `pending_steps()` no vuelva a ofrecerlo.
                recovered = mission.context.setdefault("recovered_completed_steps", [])
                if action.step_id not in recovered:
                    recovered.append(action.step_id)
            elif status == ActionStatus.FAILED.value:
                mission.context.setdefault("recovered_failed_steps", []).append(action.step_id)

        if decision is RecoveryDecision.REPLAN:
            # El plan guardado ya no vale contra el mundo. Se descarta y `_ensure_plan`
            # lo regenera; ese plan nuevo volverá a pasar por PlanValidator, Policy y Gate.
            mission.plan = None
            mission.context.pop("plan_steps", None)

        return True
    """Top-level orchestrator. Integrations are injected behind interfaces.

    Dos caminos, mismo runtime:

    - legacy (por defecto): recorre el plan una vez (`for step in plan`).
    - cognitivo (`cognitive` inyectado, `ALEXIS_COGNITIVE=1`): pide una decisión antes
      de cada acción y vuelve a decidir después de observar. El epílogo (verificación,
      learning, auditoría) es el mismo para ambos.
    """

    def __init__(
        self,
        planner,
        policy,
        executor,
        verifier,
        memory,
        learning,
        event_bus,
        mission_repo=None,
        event_repo=None,
        audit_repo=None,
        world_repo=None,
        verification_repo=None,
        task_runner=None,
        observation_repo=None,
        gate=None,
        recovery=None,
        cognitive=None,
        goal_verifier=None,
        world=None,
        plan_model=None,
        plan_validator=None,
        learning_repo=None,
    ):
        self.planner = planner
        self.policy = policy
        self.executor = executor
        self.verifier = verifier
        self.memory = memory
        self.learning = learning
        self.events = event_bus
        self.mission_repo = mission_repo
        self.event_repo = event_repo
        self.audit_repo = audit_repo
        self.world_repo = world_repo
        self.verification_repo = verification_repo
        self.task_runner = task_runner
        self.observation_repo = observation_repo
        self.gate = gate
        self.recovery = recovery
        self.cognitive = cognitive
        self.goal_verifier = goal_verifier
        self.world = world
        self.plan_model = plan_model
        self.plan_validator = plan_validator
        #: CORE-11: persistencia del ciclo de aprendizaje. Opcional para que los tests y las
        #: composiciones sin base sigan funcionando; cuando está, las skills sobreviven al
        #: reinicio, que es la diferencia entre un registro y un sistema que aprende.
        self.learning_repo = learning_repo
        #: CORE-08A-1. Por qué el `ModelPlanner` está activo o no. Sin esto, "planificó por
        #: reglas" es indistinguible de "el modelo estaba mal configurado": dos fallos
        #: distintos que se ven igual desde fuera.
        self.plan_planner_mode = "unset"
        self.plan_planner_reason = ""
        self.latest_verification = None

    async def _commit(self, mission: Mission, topic=None, payload=None):
        if self.mission_repo is not None:
            await self.mission_repo.upsert(mission)
        if self.event_repo is not None and topic is not None:
            await self.event_repo.append(topic, payload if payload is not None else {}, mission.id)

    # ------------------------------------------------------------------ #
    # P0 §13 — La ÚNICA operación de reanudación
    # ------------------------------------------------------------------ #

    async def resume_from_clarification(self, mission: Mission, answer: str) -> Mission:
        """Reanuda una misión en WAITING_CLARIFICATION con la respuesta del usuario.

        Es la única operación de reanudación del sistema: la usan tanto
        `POST /missions/{id}/clarify` como el canal `/chat`. No hay dos caminos que
        puedan divergir.

        Valida antes de tocar nada (misión existe, está esperando, la respuesta no está
        vacía, la misión no está terminada) y después reanuda el bucle cognitivo desde
        el punto en que se dejó, sin resetear nada.
        """
        if self.cognitive is None:
            raise RuntimeError("CognitiveRuntime no configurado")
        allowed, why = self.cognitive.can_clarify(mission)
        if not allowed:
            raise ValueError(why or "mission cannot be clarified")
        text = (answer or "").strip()
        if not text:
            raise ValueError("empty clarification response")

        before = self.cognitive.knowledge_for(mission)
        iterations_before = before.iterations
        replans_before = before.replans
        claims_before = len(before.claims)
        completed_before = set(before.completed_steps)

        knowledge = self.cognitive.resume_with_clarification(mission, text)

        # La reanudación NO reinicia: si se perdiera algo aquí, estos tres asserts lo delatan.
        if knowledge.iterations < iterations_before or knowledge.replans < replans_before:
            raise AssertionError("la reanudación reseteó los contadores")
        if len(knowledge.claims) < claims_before:
            raise AssertionError("la reanudación perdió evidencia previa")
        if not completed_before <= set(knowledge.completed_steps):
            raise AssertionError("la reanudación perdió pasos completados")

        await self._commit(
            mission, "mission.clarification_received",
            {"mission_id": mission.id, "provenance": "user_input",
             "iteration": knowledge.iterations, "length": len(text)},
        )
        await self.events.publish(
            "mission.clarification_received",
            {"mission_id": mission.id, "provenance": "user_input",
             "iteration": knowledge.iterations},
        )
        # A RUNNING y a seguir: el bucle cognitivo decide desde el estado congelado.
        mission.state = MissionState.RUNNING
        await self._commit(mission, "mission.resumed", {"mission_id": mission.id,
                                                        "iteration": knowledge.iterations})
        await self.events.publish("mission.resumed", {"mission_id": mission.id,
                                                      "iteration": knowledge.iterations})
        return await self._run_cognitive(mission, mission.plan or await self._ensure_plan(mission), 0)

    async def _flush_rejections(self, mission: Mission) -> int:
        """Publica y audita los rechazos del filtro de replan (P0 §12.8).

        Sin logger paralelo: evento en el `EventBus` y fila en `audit_log` vía el
        `AuditRepository` que el runtime ya tiene. Se guarda además en
        `mission.context["replan_rejections"]`, que la Storage persiste, para que
        "¿por qué descartó ALEXIS esta acción?" se pueda responder tras un reinicio.
        """
        pendientes = self.cognitive.drain_rejections() if self.cognitive is not None else []
        if not pendientes:
            return 0
        for row in pendientes:
            payload = {**row, "timestamp": time.time()}
            if self.audit_repo is not None:
                # `mission_id` va posicional en `record()`: si también fuera parte de
                # `details` el **kwargs chocaría con él. Se quita de los detalles.
                detalles = {k: v for k, v in payload.items() if k != "mission_id"}
                try:
                    await self.audit_repo.record(
                        "replan.action_rejected", "cognitive", mission.id, **detalles
                    )
                except Exception as exc:  # noqa: BLE001 — auditar no puede tumbar el bucle
                    log.warning("no se pudo auditar el rechazo de replan: %s", exc)
            await self.events.publish("replan.action_rejected", {"mission_id": mission.id, **payload})
        context = getattr(mission, "context", None)
        if context is not None:
            previo = context.get("replan_rejections") or []
            context["replan_rejections"] = (list(previo) + list(pendientes))[-20:]
        return len(pendientes)

    async def _persist_world(self, mission: Mission) -> bool:
        """Vuelca el WorldModel al store persistente. Devuelve si SE PERSISTIÓ.

        P0 §4.4. El store es la fuente de autoridad del mundo, así que se escribe ANTES
        que la proyección legacy: si el store falla, la proyección no puede fingir que el
        conocimiento está a salvo. Y si la proyección falla después, el store ya tiene la
        verdad.

        Vive aquí, en código async del runtime, y no en `WorldModel`: el Core es síncrono
        y no sabe de base de datos. `WorldModel.export()` es la costura.

        El valor de retorno es deliberado: quien llama puede distinguir "persistido" de
        "no persistido". Un fallo aquí NO se marca como éxito en ningún sitio.
        """
        world = getattr(self.cognitive, "world", None) if self.cognitive is not None else None
        if self.world_repo is None or world is None:
            return False
        scope = getattr(world, "scope", None)
        if scope is None:
            return False
        try:
            salida = world.export(scope=scope.id)
            await self.world_repo.save_snapshot(
                salida["entities"], salida["edges"], scope=scope.id
            )
            return True
        except Exception as exc:  # noqa: BLE001 — el store no puede tumbar el bucle cognitivo
            log.warning(
                "no se pudo persistir el WorldModel de la misión %s (sigue en memoria, "
                "NO consta como persistido): %s", mission.id, exc,
            )
            return False

    async def _close(self, mission: Mission, state: MissionState):
        # AUDIT es SECUNDARIO: el estado de la misión es lo PRINCIPAL. `audit_log.mission_id`
        # tiene FK a `missions`, así que si la fila padre no está —una misión que se cerró
        # sin persistir, o una que se borró— el INSERT revienta con ForeignKeyViolation y,
        # sin esta guarda, tumbaba el cierre entero y perdía el epílogo de §5.6. El mismo
        # degradado que ya usa `_flush_rejections` y el registro de experiencia.
        if self.audit_repo is not None:
            try:
                await self.audit_repo.record(
                    f"mission.{state.value}", "runtime", mission.id, mission=mission.id
                )
            except Exception as exc:  # noqa: BLE001 — auditar no puede tumbar el cierre
                log.warning("no se pudo auditar el cierre de la misión %s: %s", mission.id, exc)
        await self._close_cycle(mission)

    # ------------------------------------------------------------------ #
    # P0 §5.6.9 — cierre de ciclo: response → reflection → experience → learning
    # ------------------------------------------------------------------ #

    async def _close_cycle(self, mission: Mission) -> bool:
        """Compone el epílogo del ciclo y lo deja persistido. `False` si no aplicaba.

        Se ejecuta al cerrar la misión, y **sólo** si el camino cognitivo llegó a correr
        (hay `knowledge` en el contexto). Así el legacy no cambia de comportamiento y el
        epílogo nunca se adelanta al resultado: aquí el veredicto ya está asentado.

        El orden no admite atajos (§5.6.9): response → reflection → experience →
        learning. La frontera de aprendizaje corre dentro de `compose_epilogue`, y una
        misión sin objetivo verificado no produce aprendizaje.
        """
        if self.cognitive is None:
            return False
        context = getattr(mission, "context", {}) or {}
        if not context.get("knowledge"):
            return False

        knowledge = self.cognitive.knowledge_for(mission)
        reply, reflection, experience, verified = self.cognitive.compose_epilogue(
            mission, knowledge, model_outcome=self._model_outcome_of(mission)
        )
        self.cognitive.store_knowledge(mission, knowledge)
        await self._commit(mission, "mission.reflected", {"mission_id": mission.id})

        # §5.6.6: la experiencia se publica como observación para que la recupere la
        # memoria existente. `trusted=False`: es contexto, nunca autoridad.
        if self.observation_repo is not None:
            try:
                await self.observation_repo.insert(
                    mission.id, EXPERIENCE_SOURCE, verified.to_dict(), trusted=False
                )
            except Exception as exc:  # noqa: BLE001 — la memoria no puede tumbar el cierre
                log.warning("no se pudo publicar la experiencia: %s", exc)

        await self.events.publish(
            "mission.response",
            {
                "mission_id": mission.id,
                "text": reply.text,
                "verdict": reply.verdict,
                "goal_verified": reply.goal_verified,
                "blocked": reply.blocked,
                "needs_user": reply.needs_user,
                "pending": list(reply.pending),
                "cognition_outcome": reply.cognition_outcome,
            },
        )
        await self.events.publish(
            "mission.experience",
            {"mission_id": mission.id, **verified.to_dict()},
        )
        return True

    def _model_outcome_of(self, mission: Mission) -> str:
        """Procedencia del modelo en la última decisión: real | degraded | unavailable | none."""
        decisions = (getattr(mission, "context", {}) or {}).get("decisions") or {}
        for row in decisions.values():
            outcome = str((row or {}).get("cognition_outcome") or "")
            if outcome:
                return outcome
        return str((getattr(mission, "context", {}) or {}).get("cognition_outcome") or "none")

    async def _record_inflight_action(self, mission: Mission, step) -> None:
        """Escribe la acción EN VOLO antes de ejecutarla (CORE-10).

        Ésta es la pieza que hace que un corte sea reconstruible. Sin ella, si el proceso
        muere entre la intención y la ejecución, al volver no hay ningún rastro de que ALEXIS
        iba a hacer nada: el paso simplemente no está y parece que nunca se intentó.

        Con ella, el corte deja una marca explícita de "esto se intentó, resultado
        desconocido". Que es distinto de "no se intentó", y es la diferencia entre reintentar
        y preguntar.
        """
        from alexis.autonomy.recovery import LastKnownAction

        try:
            mission.context["inflight_action"] = LastKnownAction(
                step_id=str(step.id),
                capability=str(getattr(step, "capability", "") or ""),
                action=str(getattr(step, "action", "") or ""),
                args=dict(getattr(step, "args", {}) or {}),
                plan_generation=1,
                recorded_status="unknown",
                probe_path=self._probe_path_of(step),
                recorded_at=time.time(),
            ).to_dict()
        except Exception as exc:  # noqa: BLE001 — un checkpoint fallido no puede parar la misión
            log.warning("recovery: no pude registrar la acción en vuelo (%s)", exc)

    async def _settle_inflight_action(self, mission: Mission, step) -> None:
        """Cierra la marca de acción en vuelo cuando el paso terminó de verdad."""
        inflight = mission.context.get("inflight_action")
        if not inflight:
            return
        inflight = dict(inflight)
        inflight["recorded_status"] = "executed"
        inflight["settled_at"] = time.time()
        mission.context["last_action"] = inflight
        mission.context.pop("inflight_action", None)

    async def _ensure_plan(self, mission: Mission):
        """Deja en `mission.plan` un plan válido, venga de donde venga.

        Fuentes cubiertas, todas por la MISMA frontera (`PlanValidator`):
        `mission.plan` en memoria · `context["plan_steps"]` (deserialización/restart) ·
        `ModelPlanner` · `RuleBasedPlanner` (incluido el fallback).

        Si una fuente reutilizada no valida, se registra `plan.invalid` con su origen y se
        cae al planner por reglas; si ese tampoco valida, la misión se queda SIN plan y
        `run_mission` la termina con `failed` en vez de ejecutar un plan inválido.
        """
        if self.cognitive is not None and self.plan_validator is None:
            self.plan_validator = PlanValidator()

        if mission.plan is not None:
            if not self._plan_reasons(mission, mission.plan):
                self._record_accepted(mission, mission.plan, source="in_memory")
                return
            await self._reject_plan(mission, mission.plan, source="in_memory")
            mission.plan = None

        raw = mission.context.get("plan_steps")
        if raw:
            candidate = plan_from_dict(raw, mission.id)
            if not self._plan_reasons(mission, candidate):
                mission.plan = candidate
                self._record_accepted(mission, candidate, source="context")
                return
            await self._reject_plan(mission, candidate, source="context")
            mission.context.pop("plan_steps", None)
            # CORE-08B: si el plan base se descarta, el overlay que lo sustituía deja de
            # tener sentido — sus `replaces_step_ids` apuntan a pasos que ya no existen, y
            # `pending_steps()` los seguiría inyectando. Un overlay sin base sería ejecutar
            # una estrategia generada contra un plan que ya no está. Se descartan juntos.
            if mission.context.pop("dynamic_replan", None) is not None:
                await self.events.publish(
                    "plan.dynamic_replan_discarded",
                    {"mission_id": mission.id, "reason": "el plan base ya no valida"},
                )

        if self.plan_model is not None:
            plan, provenance = await self._plan_with_model(mission)
        else:
            plan, provenance = await self._plan_with_rules(mission, source="rule_based")
        if plan is None:
            return
        mission.plan = plan
        mission.context["plan_steps"] = plan_to_dict(plan)
        if provenance:
            mission.context["plan_provenance"] = provenance

    def _plan_reasons(self, mission: Mission, plan) -> list[str]:
        """Motivos por los que este plan no puede ejecutarse ahora. Sin validador no hay
        cambio de comportamiento (compatibilidad del camino legacy)."""
        if self.plan_validator is None or plan is None:
            return []
        reasons = list(self.plan_validator.validate(mission, plan))
        for step in plan.steps:
            reasons.extend(self.plan_validator.validate_args(step))
        return reasons

    def _record_accepted(self, mission: Mission, plan, *, source: str) -> None:
        """Trazabilidad de un plan reutilizado que SÍ valida: también queda registrado,
        para poder reconstruir el recorrido (validación → policy → ejecución)."""
        if self.plan_validator is None:
            return
        mission.context["plan_provenance"] = {
            "source": source,
            "accepted": True,
            "reasons": [],
            "steps": [getattr(s, "id", None) for s in (plan.steps or [])],
        }

    async def _reject_plan(self, mission: Mission, plan, *, source: str) -> None:
        """Registra el rechazo con su origen. Todos los rechazos se acumulan en
        `context["plan_rejected"]`: un rechazo temprano (p.ej. el plan persistido) no
        puede quedar tapado por la aceptación posterior del fallback."""
        reasons = self._plan_reasons(mission, plan)
        record = {
            "source": source,
            "accepted": False,
            "reasons": reasons,
            "steps": [getattr(s, "id", None) for s in (plan.steps or [])],
        }
        mission.context.setdefault("plan_rejected", []).append(record)
        mission.context["plan_provenance"] = record
        payload = {"mission_id": mission.id, **record}
        await self.events.publish("plan.invalid", payload)
        if self.event_repo is not None:
            await self.event_repo.append("plan.invalid", payload, mission.id)

    async def _plan_with_rules(self, mission: Mission, *, source: str):
        """Plan por reglas. Es el suelo: si tampoco valida, no hay plan ejecutable.

        CORE-12: primero la estrategia aprendida. Si una skill validada coincide con el
        objetivo, su `procedure` ES el plan — la plantilla es lo que se usa cuando no hay
        estrategia. No es una excepción: la skill plan se valida igual que la plantilla,
        y si no valida, cae a la plantilla y de ahí al rechazo de siempre.
        """
        plan, skill_source = self._plan_with_skill(mission, source=source)
        if plan is not None:
            reasons = self._plan_reasons(mission, plan)
            if not reasons:
                return plan, skill_source
            await self._reject_plan(mission, plan, source=source)
            plan = None

        plan = await self.planner.create_plan(mission)
        reasons = self._plan_reasons(mission, plan)
        if not reasons:
            if self.plan_validator is None:
                return plan, None
            return plan, {
                "proposed_by": "rule_based",
                "source": source,
                "accepted": True,
                # CORE-08A-1: si había un ModelPlanner y no se usó, el motivo por el que no
                # se usó es la mitad del diagnóstico. Sin esta línea, un plan por reglas en
                # un sistema con modelo disponible y un sistema sin modelo se ven iguales.
                **({"model_planner": {"mode": self.plan_planner_mode, "reason": self.plan_planner_reason}}
                   if self.plan_model is None else {}),
            }
        await self._reject_plan(mission, plan, source=source)
        mission.context["plan_invalid"] = {
            "source": source,
            "reasons": reasons,
            "note": "ni el plan proposing por el modelo ni el de reglas son ejecutables",
        }
        return None, {"proposed_by": "rule_based", "source": source, "accepted": False, "reasons": reasons}

    def _skill_match(self, mission):
        """CORE-12 — skill validada cuyo `applicability` coincide con el objetivo, o `None`.

        Se consultan las capabilities que la misión puede ofrecer: una skill que pida una
        capability ausente no aplica ni como candidato. La planificación nunca puede caer
        por un problema del registro.
        """
        try:
            registry = self.skill_registry()
            if registry is None or not registry.all():
                return None
            capabilities = [c for c in (getattr(mission.envelope, "capabilities", []) or [])]
            verdict, found, reasons = registry.match(
                mission.goal.objective, capabilities=capabilities
            )
            if found is None:
                return None
            return verdict, found, reasons
        except Exception as exc:  # noqa: BLE001 — las skills nunca tumbar planificar
            log.warning("learning: no se pudo consultar skills al planificar (%s)", exc)
            return None

    def _plan_with_skill(self, mission: Mission, *, source: str):
        """CORE-12 — plan desde la skill cuyo `MATCH` es seguro. `(None, None)` si no aplica.

        Sólo `MATCH` reutiliza. `UNCERTAIN` disfraza de estrategia lo que es conjetura, y
        CORE-11 se construyó para que una skill nunca aventaje por parecido débil.
        """
        from alexis.cognition.planner import Planner
        from alexis.learning.skill import SkillMatch

        matched = self._skill_match(mission)
        if matched is None:
            return None, None
        verdict, found, reasons = matched
        if verdict != SkillMatch.MATCH:
            return None, None
        plan = Planner.plan_from_skill(mission, found)
        if plan is None:
            return None, None
        provenance = {
            "proposed_by": "skill",
            "source": source,
            "accepted": True,
            "skill_id": found.skill_id,
            "skill_version": found.version,
            "reasons": reasons,
        }
        return plan, provenance

    def _plan_catalog_reason(self) -> str:
        """Por qué el validador no puede comprobar capabilities, o `""` si sí puede.

        Sólo se consulta en la ruta del `ModelPlanner`. El validador por defecto del
        runtime sigue siendo `PlanValidator()` (`require_catalog=False`) porque
        construye Missions de prueba sin catálogo y su comportamiento no debe cambiar:
        este hueco no es de un runtime cualquiera, es del que ACEPTA texto de un modelo
        como plan ejecutable.
        """
        if self.plan_validator is None:
            return "no hay validador de planes: el plan de un modelo no se puede comprobar"
        # El catálogo se exige AQUÍ, y no por el `require_catalog` del validador: ese flag es
        # del validador (compatibilidad con quien valida args), y esta ruta tiene una exigencia
        # propia y más fuerte. Confiar en el flag dejaría pasar un plan de modelo con un
        # `PlanValidator()` a secas, que es precisamente el agujero que este runtime no
        # puede permitirse: su salida se ejecuta.
        if getattr(self.plan_validator, "catalog", None) is None:
            return (
                "no hay catálogo de capabilities: no se puede comprobar que el plan use "
                "capacidades reales (un catálogo ausente NO significa 'todo disponible')"
            )
        checker = getattr(self.plan_validator, "_catalog_unusable", None)
        if callable(checker):
            return checker() or ""
        return ""

    async def _plan_without_catalog(self, mission: Mission, reason: str):
        """Sin catálogo no se llama al modelo: se registra el motivo y se cae a reglas.

        Mismo contrato que un plan del modelo inválido: `plan.invalid` con su motivo,
        `plan_provenance` con `accepted=False`, y el `RuleBasedPlanner` como suelo.
        """
        provenance = {
            "proposed_by": "model",
            "accepted": False,
            "cognition_outcome": "skipped",
            "reasons": [reason],
            "fallback": "rule_based_planner",
        }
        record = {
            "source": "model",
            "accepted": False,
            "reasons": [reason],
            "steps": [],
            "cognition_outcome": "skipped",
        }
        mission.context.setdefault("plan_rejected", []).append(record)
        await self.events.publish("plan.invalid", {"mission_id": mission.id, **record})
        if self.event_repo is not None:
            await self.event_repo.append("plan.invalid", {"mission_id": mission.id, **record}, mission.id)
        fallback, fallback_provenance = await self._plan_with_rules(mission, source="rule_based_fallback")
        provenance["fallback_validation"] = fallback_provenance
        provenance["fallback_steps"] = [s.id for s in (fallback.steps if fallback else [])]
        return fallback, provenance

    async def _plan_with_model(self, mission: Mission):
        """ModelPlanner → PlanValidator → (fallback) RuleBasedPlanner."""
        # CORE-08A-3: un plan propuesto por un modelo sólo puede aceptarse si el validador
        # puede comprobar sus capabilities. Sin catálogo, las reglas de existencia y de
        # disponibilidad están desactivadas por completo y una capability inventada se
        # aceptaría: el validador devolvería "válido" sin haber mirado nada. Se comprueba
        # ANTES de llamar al modelo, no después: así no se gasta una llamada (de hasta
        # 46-60s con un modelo local) para descartar su resultado, y la provenance dice
        # con honestidad que no llegó a haber plan.
        reason = self._plan_catalog_reason()
        if reason:
            return await self._plan_without_catalog(mission, reason)
        cognitive = self.cognitive
        brief = cognitive.self_brief() if cognitive is not None else None
        knowledge = cognitive.knowledge_for(mission) if cognitive is not None else None
        world = getattr(cognitive, "world", None) if cognitive is not None else None
        memory_context = None
        if cognitive is not None and getattr(cognitive, "memory", None) is not None:
            memory_context = await cognitive.recall(mission, knowledge)

        proposal = await self.plan_model.create_plan(
            mission, brief=brief, knowledge=knowledge, memory=memory_context, world=world
        )
        provenance = {"proposed_by": "model", **proposal.meta}

        if not proposal.ok:
            reasons = proposal.reasons
        else:
            reasons = self._plan_reasons(mission, proposal.plan)

        if not reasons and proposal.plan is not None:
            provenance["accepted"] = True
            await self.events.publish(
                "plan.created",
                {
                    "mission_id": mission.id,
                    "steps": [{"id": s.id, "capability": s.capability} for s in proposal.plan.steps],
                    "cognition_outcome": proposal.meta.get("cognition_outcome"),
                },
            )
            return proposal.plan, provenance

        provenance.update({"accepted": False, "reasons": reasons, "fallback": "rule_based_planner"})
        if proposal.plan is not None:
            await self._reject_plan(mission, proposal.plan, source="model")
        else:
            record = {
                "source": "model",
                "accepted": False,
                "reasons": reasons,
                "steps": [],
                "cognition_outcome": proposal.meta.get("cognition_outcome"),
            }
            mission.context.setdefault("plan_rejected", []).append(record)
            await self.events.publish(
                "plan.invalid",
                {
                    "mission_id": mission.id,
                    "source": "model",
                    "reasons": reasons,
                    "steps": [],
                    "cognition_outcome": proposal.meta.get("cognition_outcome"),
                },
            )
        fallback, fallback_provenance = await self._plan_with_rules(mission, source="rule_based_fallback")
        provenance["fallback_validation"] = fallback_provenance
        provenance["fallback_steps"] = [s.id for s in (fallback.steps if fallback else [])]
        return fallback, provenance

    def _record_decision(self, mission: Mission, step, decision):
        mission.context.setdefault("decisions", {})[step.id] = {
            "allowed": decision.allowed,
            "requires_approval": decision.requires_approval,
            "reason": decision.reason,
            "verdict": getattr(decision, "verdict", None),
            "matched_rule": getattr(decision, "matched_rule", None),
            "capability": getattr(step, "capability", None),
        }

    def _evaluate(self, mission: Mission, step_id: str):
        evidence = sum(1 for r in (mission.results or []) if r.get("success"))
        independent = 1 if len(mission.results or []) >= 2 else 0
        assumptions = len(
            [a for a in mission.envelope.approval_required if a in mission.envelope.allowed_actions]
        )
        conf = MetaCognition().assess(evidence, independent, assumptions)
        mission.context.setdefault("evaluations", {})[step_id] = {
            "confidence": round(conf.score, 3),
            "reasons": list(conf.reasons),
            "uncertainties": list(conf.uncertainties),
        }

    async def _persist_observations(self, mission: Mission, result):
        for obs in result.observations:
            await self.memory.store_observation(mission.id, obs)
            if self.observation_repo is not None:
                await self.observation_repo.insert(mission.id, obs.source, obs.content, obs.trusted)

    async def _approval_needed(self, mission: Mission, step, decision) -> bool:
        approved = set(mission.context.get("approved_step_ids", []))
        if self.gate is not None:
            return decision.requires_approval and step.id not in approved
        return (not decision.allowed) or (step.requires_approval and step.id not in approved)

    async def run_mission(self, mission: Mission):
        mission.state = MissionState.PLANNING
        await self._commit(mission, "mission.planning", {"mission_id": mission.id})
        await self.events.publish("mission.planning", mission.id)

        # Checkpointing: reanudar desde el último paso válido (no se reinicia la misión entera).
        start, checkpoint_payload = (
            await self.task_runner.resume(mission.id) if self.task_runner is not None else (0, {})
        )
        if start:
            mission.context["resumed_at_step"] = start
            saved_results = checkpoint_payload.get("results")
            if saved_results and len(mission.results) < len(saved_results):
                mission.results = list(saved_results)
            saved_context = checkpoint_payload.get("context")
            if isinstance(saved_context, dict):
                for key, value in saved_context.items():
                    mission.context.setdefault(key, value)

        await self._ensure_plan(mission)
        if mission.plan is None:
            mission.state = MissionState.FAILED
            await self._commit(mission, "mission.plan_invalid", {"mission_id": mission.id})
            await self._close(mission, mission.state)
            await self.events.publish("mission.failed", mission.id)
            return mission
        plan = mission.plan
        mission.state = MissionState.RUNNING
        await self._commit(mission)

        if self.cognitive is not None:
            # P0 GAP 3: antes de reanudar, se comprueba que el contexto cognitivo se
            # recuperó entero. Si no, la misión NO sigue a ciegas: queda en
            # NEEDS_VERIFICATION con el motivo. Nunca se asume éxito tras un recovery.
            if start:
                resume = self.cognitive.resume_cognition(mission)
                if not resume.safe:
                    mission.state = MissionState.NEEDS_VERIFICATION
                    mission.context["recovery_blocked"] = resume.to_dict()
                    await self._commit(mission, "mission.recovery_blocked", resume.to_dict())
                    await self._close(mission, mission.state)
                    await self.events.publish("mission.recovery_blocked", resume.to_dict())
                    return mission
                restored = self.cognitive.restore_world(mission)
                await self._commit(
                    mission, "mission.recovered",
                    {"mission_id": mission.id, "resumed_at_step": start, **resume.to_dict(),
                     "world_entities": restored},
                )
                await self.events.publish("mission.recovered", resume.to_dict())
                # CORE-10: restaurar el estado NO es decidir. Antes de continuar hay que
                # MIRAR el mundo y contrastarlo con lo que se creía, porque una acción pudo
                # ejecutarse justo antes de que el proceso muriera. Si esto devuelve False, la
                # misión quedó en WAITING_APPROVAL o BLOCKED y NO se ejecuta nada a ciegas.
                if not await self._recover_mission(mission):
                    await self._close(mission, mission.state)
                    return mission
                # La inspección puede haber invalidated el plan: se vuelve a asegurar.
                plan = mission.plan or await self._ensure_plan(mission)
                if plan is None:
                    mission.state = MissionState.FAILED
                    await self._commit(mission, "mission.recovery_unplannable",
                                      {"mission_id": mission.id})
                    await self._close(mission, mission.state)
                    return mission
            return await self._run_cognitive(mission, plan, start)

        for index, step in enumerate(plan.steps):
            if index < start:
                continue

            if self.gate is not None:
                decision = self.gate.decide(mission, step, self.policy)
                self._record_decision(mission, step, decision)
                await self.events.publish("policy.evaluated", {
                    "step": step.id,
                    "allowed": decision.allowed,
                    "requires_approval": decision.requires_approval,
                    "verdict": getattr(decision, "verdict", None),
                    "matched_rule": getattr(decision, "matched_rule", None),
                    "capability": getattr(decision, "capability", None) or getattr(step, "capability", None),
                    "reason": decision.reason,
                })
                await self.events.publish("mission.step_evaluated", {"step": step.id, "action": step.action})
                if not decision.allowed:
                    mission.state = MissionState.BLOCKED
                    mission.context["blocked_reason"] = decision.reason
                    await self._commit(mission, "mission.blocked", {"mission_id": mission.id, "reason": decision.reason})
                    await self._close(mission, mission.state)
                    await self.events.publish("mission.blocked", decision.reason)
                    return mission
            else:
                decision = self.policy.authorize(mission, step)
                await self.events.publish("policy.evaluated", {
                    "step": step.id,
                    "allowed": decision.allowed,
                    "requires_approval": decision.requires_approval,
                    "capability": getattr(step, "capability", None),
                    "reason": decision.reason,
                })
                await self.events.publish("mission.step_evaluated", {"step": step.id, "action": step.action})

            if await self._approval_needed(mission, step, decision):
                reason = (
                    decision.reason
                    if self.gate is not None or not decision.allowed
                    else "Requiere aprobación humana (efecto de escritura en el workspace)."
                )
                mission.state = MissionState.WAITING_APPROVAL
                mission.context["pending_approval"] = {
                    "step": step.id,
                    "action": step.action,
                    "risk": step.risk.value,
                    "reason": reason,
                }
                await self._commit(mission, "mission.approval_required", mission.context["pending_approval"])
                await self.events.publish("mission.approval_required", mission.context["pending_approval"])
                return mission

            await self.events.publish("mission.step_started", step.id)
            # CORE-10: antes de ejecutar, no después. Si el proceso muere entre aquí y el
            # resultado, el corte queda marcado como acción intentada de resultado
            # desconocido, que es lo que permite NO repetirla a ciegas al volver.
            await self._record_inflight_action(mission, step)
            if self.task_runner is not None:
                result, task = await self.task_runner.run_step(mission, step, self.executor.execute)
                if task.status.value == "failed":
                    await self.task_runner.close_open_tasks(mission.id, status="cancelled")
            else:
                result = await self.executor.execute(mission, step)

            mission.results.append({
                "step": step.id,
                "success": result.success,
                "task": step.id,
                "output": result.output,
                "error": result.error,
            })
            await self._commit(mission)
            await self.events.publish("mission.step_completed", {
                "step": step.id,
                "success": result.success,
                "output": result.output,
            })

            if not result.success:
                mission.state = MissionState.FAILED
                recovery_info = None
                if self.recovery is not None:
                    recovery_info = {
                        "action": self.recovery.next_action(result.error),
                        "retriable": self.recovery.should_retry(1),
                    }
                    mission.context["recovery"] = recovery_info
                await self._commit(mission, "mission.failed", {"mission_id": mission.id})
                await self._close(mission, mission.state)
                await self.events.publish("mission.failed", mission.id)
                if recovery_info and recovery_info.get("retriable"):
                    await self.events.publish("mission.replanning", {
                        "mission_id": mission.id,
                        "recovery_action": recovery_info.get("action"),
                    })
                return mission

            # P0 §5.5: el camino legacy también alimenta al WorldModel. Sin esto no
            # habría evidencia que el GoalVerifier pudiera usar y ninguna misión legacy
            # podría completarse legítimamente (caso 12).
            if self.world is not None:
                try:
                    self.world.observe_execution(step, result, mission)
                except Exception:  # noqa: BLE001 — el world model no puede tumbar la misión
                    pass
            await self._persist_observations(mission, result)
            self._evaluate(mission, step.id)

            if self.task_runner is not None:
                await self.task_runner.save_checkpoint(mission, index)
                await self._settle_inflight_action(mission, step)

        mission.state = MissionState.VERIFYING
        verification = await self.verifier.verify(mission, plan)
        # P0 §5.5: la verificación del PLAN no completa la misión. El estado final lo
        # decide `settle()` a partir del GoalVerifier, igual que en el camino cognitivo.
        if not verification.passed:
            mission.state = MissionState.BLOCKED
        else:
            settle(mission, self._verify_goal(mission))
        return await self._finalize(mission, verification, goal_verified=mission.state is MissionState.COMPLETED)

    def _verify_goal(self, mission: Mission):
        """Verificación del OBJETIVO (§5.5). Sin GoalVerifier no se completa nada.

        Delega en el cognitive si lo hay (comparte WorldModel y evidencia) y, si no, en el
        `goal_verifier` inyectado. Devolver `None` es una respuesta legítima: significa "no
        hay quién verifique", y entonces la misión no puede pasar a COMPLETED.
        """
        if self.cognitive is not None and getattr(self.cognitive, "goal_verifier", None) is not None:
            return self.cognitive.verify_goal(mission)
        if self.goal_verifier is not None:
            return self.goal_verifier.verify(mission)
        return None

    async def _advance_learning(self, mission: Mission) -> None:
        """CORE-11 — EXPERIENCE → OUTCOME → REFLECTION → LESSON → CANDIDATE → SKILL v1.

        Encadenado al epílogo de una misión VERIFICADA. Se ejecuta después de que la misión
        está resuelta y con `try/except` porque aprender nunca debe tumbar una misión que ya
        terminó bien: un fallo de aprendizaje se registra y se sigue.

        Lo que NO hace, y es lo importante: no valida skills por su cuenta. Llama al
        `SkillValidator`, que es determinista y pregunta al catálogo y a la policy. Y una
        lección nunca cambia envelope ni policy — lo que se guarda es CONOCIMIENTO, no
        autoridad.
        """
        from alexis.learning.lesson import (
            Lesson,
            Outcome,
            build_lesson_from_outcome,
            classify_outcome,
        )
        from alexis.learning.skill import (
            SkillRegistry,
            SkillValidator,
            skill_from_lesson,
        )

        try:
            experience = (getattr(mission, "context", {}) or {}).get("experience") or {}
            verified = (getattr(mission, "context", {}) or {}).get("verified_learning") or {}
            if not experience:
                return

            outcome = classify_outcome(
                goal_verified=bool(experience.get("goal_verified")),
                mission_state=str(getattr(mission.state, "value", "") or ""),
                blocked_reason=str((getattr(mission, "context", {}) or {}).get("blocked_reason") or ""),
                recovery_aborted=str((getattr(mission, "context", {}) or {}).get("recovery_aborted") or ""),
                failures=list(experience.get("failures") or []) or [
                    r for r in list(mission.results or []) if not r.get("success")
                ],
            )

            lesson = build_lesson_from_outcome(
                experience_id=str(experience.get("mission_id") or mission.id),
                statement=str(verified.get("lesson") or "").strip()
                or f"la estrategia de la misión terminó con outcome={outcome.value}",
                outcome=outcome,
                evidence=list(experience.get("evidence_refs") or []),
                scope=_lesson_scope(mission),
                applicability=_lesson_applicability(mission),
                contraindications=_lesson_contraindications(mission, outcome),
                prerequisites=list((getattr(mission.envelope, "capabilities", []) or []))[:3],
            )
            if not lesson.statement:
                return

            if self.learning_repo is not None:
                await self.learning_repo.save_lesson(lesson)
            mission.context["lesson"] = lesson.to_dict()
            await self.events.publish("learning.lesson_created", {
                "mission_id": mission.id, "lesson_id": lesson.lesson_id,
                "outcome": outcome.value, "confidence": lesson.confidence,
                "basis": lesson.confidence_basis, "scope": lesson.scope,
            })

            # Sólo un outcome verificado y con evidencia genera candidata. Un PARTIAL enseña
            # por qué falló, no cómo hacer las cosas bien.
            if not outcome.is_verified_success or not lesson.is_backed:
                return

            candidate = skill_from_lesson(lesson)
            if candidate is None:
                return
            candidate.required_capabilities = _capabilities_used(mission) or [
                c for c in (getattr(mission.envelope, "capabilities", []) or [])
            ]
            candidate.procedure = _procedure_from_mission(mission)
            # El riesgo se toma del PEOR paso de la procedure, no del envelope. Declarar
            # `medium` porque el envelope lo permite sería estimar por lo que ALEXIS PODRÍA
            # hacer en vez de por lo que esta estrategia HACE, que es lo que se valida.
            candidate.risk = max(
                (str(s.get("risk") or "low") for s in candidate.procedure),
                key=lambda r: ("low", "medium", "high", "critical").index(r)
                if r in ("low", "medium", "high", "critical") else 0,
                default="low",
            ) if any(s.get("side_effects") for s in candidate.procedure) else "low"

            registry = self.skill_registry()
            validator = SkillValidator(catalog=self._capability_catalog(), policy=self.policy)
            if self.learning_repo is not None:
                await self.learning_repo.save_candidate(candidate)
            await self.events.publish("learning.skill_candidate_created", {
                "mission_id": mission.id, "candidate_id": candidate.candidate_id,
                "name": candidate.name,
            })

            version = validator.promote(
                candidate,
                envelope=mission.envelope,
                existing=registry.all(),
            )
            if version is None:
                await self.events.publish("learning.skill_rejected", {
                    "mission_id": mission.id, "candidate_id": candidate.candidate_id,
                    "reasons": candidate.provenance.get("rejection_reasons") or [],
                })
                return

            registry.add(version)
            if self.learning_repo is not None:
                await self.learning_repo.save_skill_version(version)
            mission.context["skill_version"] = version.to_dict()
            await self.events.publish("learning.skill_validated", {
                "mission_id": mission.id, "skill_id": version.skill_id,
                "version": version.version, "name": version.name,
            })
            await self.events.publish("learning.skill_version_created", {
                "mission_id": mission.id, "skill_id": version.skill_id,
                "version": version.version,
            })
        except Exception as exc:  # noqa: BLE001 — aprender no puede tumbar una misión cerrada
            log.warning("learning: el ciclo de aprendizaje falló (%s)", exc)

    def skill_registry(self):
        """El registro de skills, construido una vez y reutilizado.

        Se guarda en la instancia porque las versiones deben acumular: una segunda misión que
        descubra la misma skill debe ver la que ya existe, no empezar de cero.
        """
        registry = getattr(self, "_skill_registry", None)
        if registry is None:
            from alexis.learning.skill import SkillRegistry

            registry = SkillRegistry()
            self._skill_registry = registry
        return registry

    def _capability_catalog(self):
        catalog = getattr(self.cognitive, "catalog", None) if self.cognitive is not None else None
        if catalog is None:
            try:
                from alexis.capabilities import build_catalog

                catalog = build_catalog()
            except Exception:  # noqa: BLE001
                catalog = None
        return catalog

    async def record_skill_performance(
        self, mission: Mission, *, skill_id: str, version: int, duration_s: float = 0.0,
    ) -> None:
        """CORE-11 §17 — cómo fue una vez que se usó una skill.

        Se registra DESPUÉS de la misión, y no degrada nada por sí sola: con menos de tres
        ejecuciones no se puede distinguir "esta skill es mala" de "tuve mala suerte".
        """
        from alexis.learning.skill import SkillPerformance

        record = SkillPerformance(
            skill_id=skill_id,
            version=version,
            mission_id=mission.id,
            outcome=str(getattr(mission.state, "value", "") or ""),
            verified=bool(getattr(getattr(mission, "goal_verification", None), "verified", False)),
            duration_s=duration_s,
            failures=sum(1 for r in (mission.results or []) if not r.get("success")),
            replans=int(((getattr(mission, "context", {}) or {}).get("knowledge") or {}).get("replans") or 0),
            recovered=bool(((getattr(mission, "context", {}) or {}).get("recovery"))),
            confidence=1.0 if (mission.results and all(r.get("success") for r in mission.results)) else 0.4,
        )
        self.skill_registry().record_performance(record)
        if self.learning_repo is not None:
            await self.learning_repo.save_performance(record)
        await self.events.publish("learning.skill_performance_recorded", {
            "mission_id": mission.id, "skill_id": skill_id, "version": version,
            "outcome": record.outcome, "verified": record.verified,
        })

    async def _finalize(self, mission: Mission, verification, goal_verified: bool = False):
        """Epílogo común a los dos caminos: learning, persistencia, auditoría, eventos.

        `goal_verified` lo calcula `settle()` (§5.5). El learning solo registra experiencia
        cuando el objetivo está demostrado de verdad: aprender de un plan que pasó no es
        aprender que el usuario consiguió lo que pidió.
        """
        if verification.passed and goal_verified and self.learning is not None:
            await self.learning.record_experience(mission, verification)
            # CORE-11: el ciclo completo. De una experiencia VERIFICADA sale una lección, y de
            # una lección con alcance y evidencia sale una candidata que se valida contra el
            # catálogo y la policy REALES. Si algo falla aquí, la misión ya está cerrada: no
            # se propaga, porque aprender no es un paso de la misión.
            await self._advance_learning(mission)

        if self.verification_repo is not None:
            await self.verification_repo.insert(
                mission.id,
                verification.passed,
                verification.confidence,
                verifier=type(self.verifier).__name__,
                evidence=list(verification.evidence),
                notes=verification.notes,
            )
        self.latest_verification = {
            "mission_id": mission.id,
            "state": mission.state.value,
            "confidence": verification.confidence,
            "evidence": list(verification.evidence),
            "notes": verification.notes,
        }

        await self._commit(mission, f"mission.{mission.state.value}", {"mission_id": mission.id})
        await self._close(mission, mission.state)
        await self.events.publish(f"mission.{mission.state.value}", mission.id)
        return mission

    async def _run_cognitive(self, mission: Mission, plan, start: int):
        """Bucle cognitivo: decide → policy → execute → observe → evaluate → decide.

        Reemplaza al `for step in plan`: la acción siguiente se elige DESPUÉS de
        observar, y por eso puede cambiar de estrategia, preguntar o abortar.
        """
        cognitive = self.cognitive
        knowledge = cognitive.knowledge_for(mission)

        while True:
            pending = cognitive.pending_steps(mission, plan, knowledge)
            outcome = await cognitive.step(mission, knowledge, pending_steps=pending, plan=plan)
            knowledge = outcome.knowledge
            # P0 §4.4 — orden de autoridad. `cognitive.step()` ya observó el mundo en
            # memoria. Aquí se persiste el store, y SOLO después `store_knowledge()`
            # escribe la proyección legacy en `mission.context`. Si el store falla, la
            # proyección no se escribe como si el conocimiento estuviera a salvo.
            await self._persist_world(mission)
            cognitive.store_knowledge(mission, knowledge)
            # P0 §12.8: el filtro pudo descartar repeticiones en este paso. Se publican y
            # se auditan AQUÍ, con la infraestructura que ya existe (EventBus + audit_log),
            # y además quedan en `mission.context` para sobrevivir a un reinicio.
            await self._flush_rejections(mission)
            await self._commit(mission, "cognition.step", {"mission_id": mission.id, **outcome.to_dict()})
            await self.events.publish(
                "cognition.step",
                {"mission_id": mission.id, **outcome.to_dict()},
            )

            if outcome.result is not None:
                step_id = outcome.decision.step_id or "cognitive"
                mission.results.append(
                    {
                        "step": step_id,
                        "success": outcome.result.success,
                        "task": step_id,
                        "output": outcome.result.output,
                        "error": outcome.result.error,
                    }
                )
                await self._persist_observations(mission, outcome.result)
                mission.context.setdefault("evaluations", {})[step_id] = {
                    "confidence": round(knowledge.confidence, 3),
                    "reasons": list(knowledge.known)[-3:],
                    "uncertainties": list(knowledge.uncertainties)[-3:],
                }

            if outcome.requires_approval:
                reason = outcome.error or "Requiere aprobación humana."
                mission.state = MissionState.WAITING_APPROVAL
                mission.context["pending_approval"] = {
                    "step": outcome.decision.step_id or "cognitive",
                    "action": outcome.decision.action.value,
                    "risk": RiskLevel.MEDIUM.value,
                    "reason": reason,
                }
                await self._commit(
                    mission, "mission.approval_required", mission.context["pending_approval"]
                )
                await self.events.publish("mission.approval_required", mission.context["pending_approval"])
                return mission

            if outcome.mission_state is MissionState.WAITING_CLARIFICATION:
                # P0 §13: la pregunta y su contexto se congelan enteros y se persisten.
                clarification = self.cognitive.ask_user(
                    mission, knowledge, outcome.question or "",
                    reason=outcome.decision.rationale,
                    action=outcome.decision.action.value,
                    capability=outcome.decision.capability,
                    step_id=outcome.decision.step_id,
                )
                await self._commit(mission, "mission.ask_user", clarification.to_dict())
                await self.events.publish("mission.ask_user", clarification.to_dict())
                await self.events.publish("mission.clarification_required", clarification.to_dict())
                # No se cierra la misión: está esperando, no terminada. `_close` audita,
                # pero el estado sigue siendo WAITING_CLARIFICATION y es reanudable.
                await self._close(mission, mission.state)
                return mission

            if outcome.mission_state in (MissionState.BLOCKED, MissionState.FAILED):
                mission.state = outcome.mission_state
                mission.context["cognitive_stop"] = {
                    "action": outcome.action.value,
                    "reason": outcome.error or outcome.decision.rationale,
                    "replans": knowledge.replans,
                    "iterations": knowledge.iterations,
                }
                await self._commit(mission, f"mission.{mission.state.value}", {"mission_id": mission.id})
                await self._close(mission, mission.state)
                await self.events.publish(f"mission.{mission.state.value}", mission.id)
                return mission

            if outcome.action is NextAction.VERIFY and outcome.verification is not None:
                await self.events.publish(
                    "cognition.verified",
                    {
                        "mission_id": mission.id,
                        "passed": outcome.verification.passed,
                        "confidence": outcome.verification.confidence,
                    },
                )
                if outcome.verification.passed:
                    # El plan verificó bien; el objetivo todavía no. `settle()` decide.
                    settle(mission, self._verify_goal(mission))
                    if mission.state is MissionState.COMPLETED:
                        return await self._finalize(
                            mission, outcome.verification, goal_verified=True
                        )
                mission.state = (
                    mission.state
                    if outcome.verification.passed
                    else MissionState.VERIFYING
                )
                await self._commit(mission)

            if outcome.action is NextAction.FINISH and outcome.done:
                # P0 §5.5: ya no se fabrica un `Verification(passed=True)` para poder
                # cerrar la misión. El estado viene de `settle()`; si el objetivo no está
                # verificado, la misión sigue viva en NEEDS_VERIFICATION o BLOCKED.
                settle(mission, self._verify_goal(mission))
                if mission.state is MissionState.COMPLETED:
                    return await self._finalize(
                        mission,
                        Verification(
                            passed=True,
                            evidence=list(knowledge.known),
                            confidence=knowledge.confidence,
                            notes=(
                                (mission.goal_verification.reason if mission.goal_verification else "")
                                or "objetivo verificado"
                            ),
                        ),
                        goal_verified=True,
                    )
                await self._commit(mission)

            if outcome.done:
                mission.state = outcome.mission_state or MissionState.RUNNING
                await self._close_cycle(mission)
                return mission

            if self.task_runner is not None:
                await self.task_runner.save_checkpoint(mission, len(knowledge.completed_steps))

def _lesson_scope(mission: Mission) -> str:
    """Ámbito de la lección, deducido de las capabilities que la misión usó.

    Es la clave por la que se buscan después. Se deriva del dominio real —qué capabilities
    hizo falta— y no del texto del objetivo, porque dos misiones con el mismo objetivo pueden
    necesitar capacidades distintas, y el texto no lo dice.
    """
    caps = _capabilities_used(mission)
    if not caps:
        return ""
    roots = sorted({c.split(".", 1)[0] for c in caps})
    return "/".join(roots) + "/derived"


def _lesson_applicability(mission: Mission) -> str:
    """Cuándo aplica la lección: la combinación de objetivo y capacidades.

    Es lo que la búsqueda compara después. Se escribe con la misma fuente que `scope` para que
    una lección nunca afirme un alcance distinto del que sus capacidades justifican.
    """
    caps = _capabilities_used(mission)
    return f"{str(getattr(mission.goal, 'objective', '') or '')[:120]} :: {', '.join(caps)}".strip(" ::")


def _lesson_contraindications(mission: Mission, outcome) -> list[str]:
    """Cuándo NO aplicar la lección.

    Se rellena con las condiciones que hacen que esta estrategia no valga: es la parte que
    impide que una lección se aplique donde no toca. Se construye siempre, incluso vacía, para
    que la ausencia se vea como ausencia y no como olvido.
    """
    notes = ["no aplicar fuera del envelope de la misión"]
    if outcome.value == "partial":
        notes.append("el objetivo quedó a medias: no demuestra que la estrategia funcione")
    if any(not r.get("success") for r in (mission.results or [])):
        notes.append("hubo pasos fallidos: revisar antes de repetir la estrategia")
    if not (getattr(mission.envelope, "auto_approve", None) or []):
        notes.append("sin auto_approve: cualquier acción que lo necesite requiere aprobación")
    return notes


def _capabilities_used(mission: Mission) -> list[str]:
    """Las capabilities que la misión usó de verdad, según sus decisiones registradas."""
    used = {
        str((entry or {}).get("capability") or "")
        for entry in ((getattr(mission, "context", {}) or {}).get("decisions") or {}).values()
    }
    return sorted(c for c in used if c)


def _procedure_from_mission(mission: Mission) -> list[dict]:
    """La estrategia que la misión siguió, en forma ejecutable.

    Se construye desde el plan REAL que se ejecutó, no desde una descripción: una skill cuyo
    procedure no coincide con lo que ALEXIS sabe hacer es una skill que fallaría la primera vez
    que se use.
    """
    plan = getattr(mission, "plan", None)
    if plan is None:
        return []
    from alexis.capabilities import build_catalog

    try:
        catalog = build_catalog()
    except Exception:  # noqa: BLE001
        catalog = None
    steps: list[dict] = []
    for step in plan.steps or []:
        capability = str(getattr(step, "capability", "") or "")
        side_effects = False
        if catalog is not None and capability and catalog.has(capability):
            side_effects = bool(getattr(catalog.get(capability), "side_effects", False))
        steps.append({
            "id": str(getattr(step, "id", "")),
            "action": str(getattr(step, "action", "")),
            "capability": capability,
            "risk": str(getattr(getattr(step, "risk", None), "value", "low") or "low"),
            "args": dict(getattr(step, "args", {}) or {}),
            "requires_approval": bool(getattr(step, "requires_approval", False)),
            "side_effects": side_effects,
        })
    return steps
