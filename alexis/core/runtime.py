from alexis.autonomy.goal_state import settle
from alexis.contracts import Mission, MissionState, RiskLevel, Verification
from alexis.cognition.planner import plan_from_dict, plan_to_dict
from alexis.cognition.planner_model import PlanValidator
from alexis.cognition.state import NextAction
from alexis.meta.cognition import MetaCognition


class AlexisRuntime:
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
        self.latest_verification = None

    async def _commit(self, mission: Mission, topic=None, payload=None):
        if self.mission_repo is not None:
            await self.mission_repo.upsert(mission)
        if self.event_repo is not None and topic is not None:
            await self.event_repo.append(topic, payload if payload is not None else {}, mission.id)

    async def _close(self, mission: Mission, state: MissionState):
        if self.audit_repo is not None:
            await self.audit_repo.record(f"mission.{state.value}", "runtime", mission.id, mission=mission.id)

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
        """Plan por reglas. Es el suelo: si tampoco valida, no hay plan ejecutable."""
        plan = await self.planner.create_plan(mission)
        reasons = self._plan_reasons(mission, plan)
        if not reasons:
            if self.plan_validator is None:
                return plan, None
            return plan, {"proposed_by": "rule_based", "source": source, "accepted": True}
        await self._reject_plan(mission, plan, source=source)
        mission.context["plan_invalid"] = {
            "source": source,
            "reasons": reasons,
            "note": "ni el plan proposing por el modelo ni el de reglas son ejecutables",
        }
        return None, {"proposed_by": "rule_based", "source": source, "accepted": False, "reasons": reasons}

    async def _plan_with_model(self, mission: Mission):
        """ModelPlanner → PlanValidator → (fallback) RuleBasedPlanner."""
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

    async def _finalize(self, mission: Mission, verification, goal_verified: bool = False):
        """Epílogo común a los dos caminos: learning, persistencia, auditoría, eventos.

        `goal_verified` lo calcula `settle()` (§5.5). El learning solo registra experiencia
        cuando el objetivo está demostrado de verdad: aprender de un plan que pasó no es
        aprender que el usuario consiguió lo que pidió.
        """
        if verification.passed and goal_verified and self.learning is not None:
            await self.learning.record_experience(mission, verification)

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
            cognitive.store_knowledge(mission, knowledge)
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
                mission.state = MissionState.WAITING_CLARIFICATION
                mission.context["clarification"] = {
                    "question": outcome.question,
                    "reason": outcome.decision.rationale,
                    "known": list(knowledge.known),
                    "unknown": list(knowledge.unknown),
                    "hypotheses": list(knowledge.hypotheses),
                    "iterations": knowledge.iterations,
                }
                await self._commit(
                    mission, "mission.clarification_required", mission.context["clarification"]
                )
                await self.events.publish("mission.clarification_required", mission.context["clarification"])
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
                return mission

            if self.task_runner is not None:
                await self.task_runner.save_checkpoint(mission, len(knowledge.completed_steps))