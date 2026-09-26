"""Self Model operacional de ALEXIS (SELF MODEL / F0.

Un modelo interno PERSISTENTE de identidad, estado, capacidades, autoridad,
objetivos, acciones, incertidumbre y experiencia. NO es conciencia subjetiva: es
autoconocimiento operacional y metacognición. Se actualiza consumiendo los eventos
REALES del runtime (docs/SELF-MODEL.md). Después de haber definido self, este ciclo
alimenta Presence Engine: la presencia no tiene una máquina de estados paralela.
"""

import time

from alexis.security.untrusted import observation_context_line
from alexis.self.presence import derive_presence

DEFAULT_IDENTITY = {
    "name": "ALEXIS",
    "version": "0.3.0",
    "nature": "Autonomous Learning, Execution & Intelligence System",
    "claim": "autoconocimiento operacional; sin conciencia subjetiva",
}

_QUESTION_ZONE = [
    (("quién soy", "que soy", "qué soy", "quien eres", "qué eres", "que eres", "what am i"), "identity"),
    (("no puedo", "limite", "límite", "no soy capaz", "te esta prohibido"), "limits"),
    (("haciendo", "que haces", "qué haces", "estas haciendo", "estás haciendo"), "doing"),
    (("objetivo", "hacia donde", "intentando alcanzar", "goal", "qué objetivo"), "goal"),
    (("puedo hacer", "capacidad", "qué puedes", "que puedes", "what can"), "capabilities"),
    (("autorizad", "permis", "allowed", "tengo derecho"), "permissions"),
    (("necesito", "para continuar", "me hace falta", "que me falta", "needs"), "needed"),
    (("qué sé", "que se", "sé que", "conozco", "que se"), "knowledge"),
    (("que no sé", "qué no sé", "no sé", "no se", "desconozco", "ignoro"), "unknown"),
    (("segura", "seguro", "confianza", "confidence", "certeza"), "confidence"),
    (("ocurri", "pasó", "paso", "acaba de ocurrir", "que paso", "que pasó"), "happened"),
    (("por que tome", "por qué tomé", "porque tomé", "por qué toma", "decision", "decisión"), "decisions"),
    (("ahora", "debería", "siguiente", "next", "que hago"), "next"),
]


def _tool_names(tools):
    if tools is None:
        return []
    if isinstance(tools, list):
        return [getattr(t, "name", t) if hasattr(t, "name") or isinstance(t, str) else t for t in tools]
    if hasattr(tools, "list"):
        return [t.name for t in tools.list()]
    return []


class SelfModel:
    def __init__(self, *, identity=None, capabilities=None, available=None, resources=None):
        self.identity = dict(identity or DEFAULT_IDENTITY)
        self.capabilities = list(capabilities or [])  # catálogo total (incl. no disponibles)
        #: Lo que ALEXIS puede hacer DE VERDAD. Si no se declara, se supone todo el
        #: catálogo (comportamiento histórico), pero un runtime que conoce sus
        #: capabilities debe pasar la lista habilitada: afirmar que tiene una
        #: capacidad `missing` es exactamente la deshonestidad que este modelo evita.
        self.available = list(available if available is not None else (capabilities or []))
        self.resources = dict(resources or {})
        self.current_state = "idle"
        self.transient = {"listening": False, "speaking": False, "reflecting": False, "flag": None}
        self.reflections: list[dict] = []
        self.observations_about_self: list[dict] = []
        self._reset()

    def _reset(self):
        self.current_goal = None
        self.current_mission = None
        self.active_context = []
        self.required_capabilities = []
        self.permissions = {}
        self.current_policy = {}
        self.active_envelope = {}
        self.current_action = None
        self.recent_actions = []
        self.decisions = []
        self.uncertainties = []
        self.confidence = None
        self.pending_approvals = []
        self.commitments = []
        self.task_results = []
        self.lessons_learned = []
        #: Veredicto de COGNICIÓN (no de policy): el de la última acción evaluada.
        #: P0 §5.6.7. Es distinto de `current_policy['verdict']`, que es allow/deny.
        self.last_verdict = ""
        self.current_limits = []
        self.available_tools = []
        self.failures = []
        self.current_dependencies = []

    def set_transient(self, **kw):
        for key in ("listening", "speaking", "reflecting", "flag"):
            if key in kw:
                self.transient[key] = kw[key] if key == "flag" else bool(kw[key])

    def add_reflection(self, text):
        if not text:
            return
        self.reflections.append({"at": int(time.time()), "text": text})
        self.reflections = self.reflections[-5:]

    def note_self_observation(self, text):
        if not text:
            return
        self.observations_about_self.append({"at": int(time.time()), "text": text})
        self.observations_about_self = self.observations_about_self[-6:]

    # ------------------------------------------------------------------ #

    def update(
        self,
        mission,
        *,
        tools=None,
        commitments=None,
        lessons=None,
        memory_items=None,
        verification=None,
        available=None,
        current_action=None,
    ):
        """Deriva todas las zonas desde el objeto real de la misión y sus fuentes."""
        self._reset()
        self.available_tools = _tool_names(tools)
        if available is not None:
            self.available = list(available)
        if mission is None:
            self.current_state = derive_presence(None, **self.transient)
            return

        self.current_goal = mission.goal.objective
        self.current_mission = {
            "id": mission.id,
            "state": getattr(mission.state, "value", str(mission.state)),
            "autonomy": mission.envelope.autonomy.value,
        }
        self.active_envelope = {
            "objective": mission.envelope.objective,
            "autonomy": mission.envelope.autonomy.value,
            "allowed_actions": list(mission.envelope.allowed_actions),
            "forbidden_actions": list(mission.envelope.forbidden_actions),
            "approval_required": list(mission.envelope.approval_required),
            "capabilities": list(mission.envelope.capabilities),
            "auto_approve": list(mission.envelope.auto_approve),
            "perimeters": list(mission.envelope.perimeters),
            "max_runtime_minutes": mission.envelope.max_runtime_minutes,
            "max_cost_usd": mission.envelope.max_cost_usd,
        }
        self.permissions = {
            "autonomy": mission.envelope.autonomy.value,
            "allowed_actions": list(mission.envelope.allowed_actions),
            "forbidden_actions": list(mission.envelope.forbidden_actions),
            "approval_required": list(mission.envelope.approval_required),
            "auto_approve": list(mission.envelope.auto_approve),
            "last_decisions": list((mission.context.get("decisions") or {}).values()),
        }

        # required_capabilities: las capabilities declaradas por el plan activo.
        plan_steps = []
        if mission.plan is not None:
            plan_steps = mission.plan.steps
        elif mission.context.get("plan_steps"):
            plan_steps = mission.context["plan_steps"]
        for step in plan_steps:
            capability = getattr(step, "capability", None)
            if not capability and isinstance(step, dict):
                capability = step.get("capability")
            if not capability and isinstance(step, dict):
                capability = None
            if capability and capability not in self.required_capabilities:
                self.required_capabilities.append(capability)

        # current_policy: última decisión auditada (matched_rule) + autonomía.
        decisions = list((mission.context.get("decisions") or {}).values())
        self.decisions = decisions
        last_decision = decisions[-1] if decisions else {}
        self.current_policy = {
            "autonomy": mission.envelope.autonomy.value,
            "step": last_decision.get("step") or self.current_mission.get("state"),
            "verdict": self._verdict_of(last_decision),
            "matched_rule": last_decision.get("matched_rule"),
            "reason": last_decision.get("reason"),
        }

        # Límites (honestos) y dependencias del objetivo actual.
        limits = []
        if self.resources.get("workspace"):
            limits.append(f"perímetro autorizado: {self.resources['workspace']}")
        if mission.envelope.max_runtime_minutes:
            limits.append(f"budget runtime: {mission.envelope.max_runtime_minutes} min")
        if mission.envelope.max_cost_usd:
            limits.append(f"budget coste: {mission.envelope.max_cost_usd} USD")
        if mission.envelope.forbidden_actions:
            limits.append("prohibido por envelope: " + ", ".join(mission.envelope.forbidden_actions))
        if self.resources.get("sandbox_no_network"):
            limits.append("sandbox sin red (capa actual del demo)")
        self.current_limits = limits
        self.current_dependencies = (
            list(self.required_capabilities)
            + [p for p in mission.envelope.perimeters if isinstance(p, str)]
        )

        # current_action: el paso en curso (evento step_started) o el último result.
        self.current_action = current_action
        if not self.current_action:
            for result in reversed(mission.results or []):
                if isinstance(result, dict) and result.get("step"):
                    self.current_action = result.get("step")
                    break

        # acciones recientes, resultados de tareas, fallos, confianza/incertidumbre.
        results = mission.results or []
        evaluations = mission.context.get("evaluations") or {}
        self.recent_actions = [
            {
                "step": r.get("step"),
                "success": bool(r.get("success")),
                "error": r.get("error"),
                "capability": next(
                    (
                        getattr(s, "capability", None)
                        for s in (mission.plan.steps if mission.plan else [])
                        if s.id == r.get("step")
                    ),
                    None,
                ),
            }
            for r in results[-6:]
        ]
        self.failures = [
            {"step": r.get("step"), "error": r.get("error")}
            for r in results
            if not r.get("success") and r.get("error")
        ]
        if self.failures and mission.state.value in ("failed", "blocked"):
            reason = mission.context.get("blocked_reason") or mission.context.get("recovery")
            if reason:
                self.failures.append({"state": mission.state.value, "context": reason})
        self.task_results = []
        latest_eval = None
        for r in results:
            evaluation = evaluations.get(r.get("step"))
            if evaluation:
                latest_eval = evaluation
            self.task_results.append(
                {
                    "step": r.get("step"),
                    "success": bool(r.get("success")),
                    "confidence": (evaluation or {}).get("confidence"),
                }
            )
        self.task_results = self.task_results[-3:]
        if latest_eval:
            self.confidence = latest_eval.get("confidence")
            self.uncertainties = list(latest_eval.get("uncertainties") or [])
        if verification:
            self.confidence = verification.get("confidence")
            if latest_eval:
                self.uncertainties = list(latest_eval.get("uncertainties") or [])

        # compromisos y lecciones aprendidas.
        self.commitments = [
            {"id": c["id"], "objective": c["objective"], "state": c["state"]}
            for c in (commitments or [])
        ]
        self.lessons_learned = list(lessons or [])
        # P0 §5.6.7: el veredicto de cognición vive en el contexto de la misión.
        ctx = getattr(mission, "context", {}) or {}
        knowledge = ctx.get("knowledge") or {}
        self.last_verdict = str(
            knowledge.get("last_verdict")
            or (ctx.get("verified_learning") or {}).get("verdict")
            or ""
        )

        # Contexto activo desde observaciones reales de memoria (últimas 5).
        # R3: una observación no confiable entra como DATO marcado y neutralizado
        # (ver alexis/security/untrusted.py), nunca como instrucción del Core.
        context_lines = []
        for _, obs in (memory_items or [])[-5:]:
            line = observation_context_line(obs, max_len=200)
            if line:
                context_lines.append(line)
        if mission.goal.objective:
            context_lines.append(f"objetivo: {mission.goal.objective}")
        self.active_context = context_lines

        pending = mission.context.get("pending_approval")
        if mission.state.value == "waiting_approval" and pending:
            self.pending_approvals = [pending]

        if mission.state.value in ("waiting_approval", "recovering", "failed", "blocked"):
            if not self.transient.get("flag"):
                self.transient["flag"] = None

        self.current_state = derive_presence(mission, **self.transient)

    def _verdict_of(self, decision) -> str | None:
        if not decision:
            return None
        if not decision.get("allowed"):
            return "deny"
        if decision.get("requires_approval"):
            return "require_approval"
        return "allow"

    # ------------------------------------------------------------------ #

    def snapshot(self):
        return {
            "identity": dict(self.identity),
            "current_state": self.current_state,
            "status": self.current_state,  # alias para clientes de /state
            "transient": dict(self.transient),
            "current_goal": self.current_goal,
            "current_mission": self.current_mission,
            "active_context": list(self.active_context),
            "capabilities": list(self.capabilities),
            "available_capabilities": list(self.available),
            "required_capabilities": list(self.required_capabilities),
            "permissions": dict(self.permissions),
            "current_policy": dict(self.current_policy),
            "active_envelope": dict(self.active_envelope),
            "current_action": self.current_action,
            "recent_actions": list(self.recent_actions),
            "decisions": list(self.decisions),
            "observations_about_self": list(self.observations_about_self),
            "uncertainties": list(self.uncertainties),
            "confidence": self.confidence,
            "pending_approvals": list(self.pending_approvals),
            "active_commitments": list(self.commitments),
            "failures": list(self.failures),
            "lessons": list(self.lessons_learned),
            "last_verdict": self.last_verdict,
            "current_dependencies": list(self.current_dependencies),
            "current_limits": list(self.current_limits),
            "available_tools": list(self.available_tools),
            "task_results": list(self.task_results),
            "reflections": list(self.reflections),
        }

    # ------------------------------------------------------------------ #

    def answer(self, question):
        """Responde las auto-preguntas operativas desde el modelo (honesto)."""
        q = (question or "").strip().lower()
        if not q:
            return {"question": question, "answer": "No tengo pregunta.", "source": None}
        for keys, zone in _QUESTION_ZONE:
            if any(k in q for k in keys):
                return {"question": question, "answer": self._answer_zone(zone), "source": zone}
        return {
            "question": question,
            "answer": "No reconozco esa pregunta; puedo responder sobre qué soy, qué hago, "
            "mi objetivo, qué puedo/ no puedo, permisos, decisiones, confianza, y qué pasó.",
            "source": None,
        }

    def _answer_zone(self, zone):
        if zone == "identity":
            return f"Soy {self.identity['name']} v{self.identity['version']}: {self.identity['nature']} ({self.identity['claim']})."
        if zone == "doing":
            if self.current_goal:
                return f"Estoy {self.current_state}; objetivo actual: {self.current_goal}."
            return "Estoy inactivo; no tengo un objetivo en curso."
        if zone == "goal":
            if self.current_goal:
                return f"Objetivo actual: {self.current_goal}."
            return "Todavía no tengo un objetivo asignado en esta sesión."
        if zone == "capabilities":
            have = ", ".join(self.available) or "ninguna"
            tools = ", ".join(self.available_tools) or "ninguna"
            return f"Puedo: {have}. Herramientas disponibles: {tools}."
        if zone == "limits":
            need = sorted(set(self.required_capabilities) - set(self.available))
            missing = f" No tengo: {', '.join(need)}." if need else ""
            limits = "; ".join(self.current_limits) or "sin límites declarados"
            return f"No puedo hacer lo que excede mis límites: {limits}.{missing}"
        if zone == "permissions":
            perms = self.permissions
            return (
                f"Nivel de autonomía: {perms.get('autonomy')}. Permitido: "
                f"{', '.join(perms.get('allowed_actions') or []) or 'nada'}. Requiere aprobación: "
                f"{', '.join(perms.get('approval_required') or []) or 'nada'}."
            )
        if zone == "needed":
            needs = []
            if self.pending_approvals:
                needs.append(f"aprobación para: {self.pending_approvals[0].get('action')}")
            missing = sorted(set(self.required_capabilities) - set(self.available))
            if missing:
                needs.append("capacidad de la que carezco: " + ", ".join(missing))
            if not needs:
                return "No necesito nada para continuar; tengo lo que requiere el plan actual."
            return "Necesito: " + "; ".join(needs) + "."
        if zone == "knowledge":
            results = "; ".join(f"{r['step']}:{r['success']}" for r in self.task_results) or "sin resultados recientes"
            lessons = "; ".join(self.lessons_learned) or "sin lecciones registradas"
            return f"Sé los resultados de tareas ({results}) y lecciones ({lessons})."
        if zone == "unknown":
            if self.uncertainties:
                return "No estoy seguro de: " + "; ".join(self.uncertainties) + "."
            return "No tengo incertidumbres explícitas registradas en el último paso."
        if zone == "confidence":
            conf = self.confidence
            if conf is None:
                return "Todavía no hay paso evaluado en esta misión; mi confianza de arranque es 0.35 (MetaCognition)."
            return f"La confianza de mi conclusión actual es {round(float(conf), 3)}."
        if zone == "happened":
            done = "; ".join(f"{a['step']}:{'ok' if a['success'] else 'fallo'}" for a in self.recent_actions) or "nada aún"
            errors = "; ".join(f"{e.get('step')}:{e.get('error')}" for e in self.failures) or "sin errores"
            return f"Acaba de ocurrir: {done}. Fallos: {errors}."
        if zone == "decisions":
            if not self.decisions:
                return "Todavía no tomé decisiones en esta misión."
            last = self.decisions[-1]
            return (
                f"Decidí {('ejecutar' if last.get('allowed') else 'no ejecutar')} el paso "
                f"'{last.get('step')}': {last.get('reason')}."
            )
        if zone == "next":
            if self.pending_approvals:
                return "Debería esperar tu decisión sobre la aprobación pendiente."
            if self.current_mission:
                return f"Sigo trabajando en {self.current_mission['id']} (estado {self.current_state})."
            return "Debería quedar en `idle` esperando un objetivo o una palmada."
        return "Sin información."