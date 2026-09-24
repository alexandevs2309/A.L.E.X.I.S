from alexis.contracts import Mission, MissionState
from alexis.cognition.planner import plan_from_dict, plan_to_dict
from alexis.meta.cognition import MetaCognition


class AlexisRuntime:
    """Top-level orchestrator. Integrations are injected behind interfaces."""

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
        """Reusa el plan guardado en el context (persistido en DB) o lo crea una sola vez."""
        if mission.plan is not None:
            return
        raw = mission.context.get("plan_steps")
        if raw:
            mission.plan = plan_from_dict(raw, mission.id)
            return
        plan = await self.planner.create_plan(mission)
        mission.plan = plan
        mission.context["plan_steps"] = plan_to_dict(plan)

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
        plan = mission.plan
        mission.state = MissionState.RUNNING
        await self._commit(mission)

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

            await self._persist_observations(mission, result)
            self._evaluate(mission, step.id)

            if self.task_runner is not None:
                await self.task_runner.save_checkpoint(mission, index)

        mission.state = MissionState.VERIFYING
        verification = await self.verifier.verify(mission, plan)
        mission.state = MissionState.COMPLETED if verification.passed else MissionState.BLOCKED
        if verification.passed:
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