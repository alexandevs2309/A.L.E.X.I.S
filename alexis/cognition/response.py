"""Response Composer (P0 §5.6.1).

Compone la respuesta final **exclusivamente** a partir del estado real de la misión:
`Mission`, `GoalVerification`, `Verdict`, evidencia, `KnowledgeState`, metadata de
decisiones y procedencia del modelo.

Dos invariantes gobiernan este módulo:

1. ``ACTION SUCCESS -> OBJECTIVE SUCCESS`` está prohibido. Que las herramientas fueran
   bien NO permite decir que el objetivo se consiguió: sólo `GoalVerification.verified`
   lo permite (`asserts_completion`).
2. Si el modelo está DEGRADED o UNAVAILABLE, la respuesta lo conserva. Un fallback
   determinista no se disfraza de respuesta de modelo.

No hay chain-of-thought: el texto se arma con plantillas sobre hechos observados. El
modelo, si existe y es REAL, sólo puede *reformular* un texto que este compositor ya
compuso; nunca sustituye los hechos.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from alexis.cognition.contracts import DecisionRecord, UserReply
from alexis.cognition.state import KnowledgeState, Verdict

#: Palabras que afirman logro. Si el objetivo no está verificado, ninguna puede
#: aparecer en la respuesta final (P0 §5.6.1, tests 1-5).
#:
#: Tres decisiones deliberadas:
#:
#: 1. Se buscan con LÍMITES DE PALABRA: sin ellos, «hecho» engancharía «hechos» y el
#:    guard destrozaría una frase honesta.
#: 2. Incluyen las formas conjugadas (completé, consiguió, terminó…), no sólo el
#:    participio: si sólo se listara «completado», «se terminó» colaría.
#: 3. Se excluyen las ambiguas. «hecho»/«hecha» también significan *hecho* = dato
#:    («los hechos observados») y «lista» = enumeración («la lista de»). Un guard que
#:    sana un texto veraz es peor que no tener guard.
COMPLETION_CLAIMS = (
    # completar
    "completado", "completada", "completados", "completadas",
    "completé", "complete", "completa", "completar", "completó", "completaron",
    # conseguir
    "conseguido", "conseguida", "conseguidos", "conseguidas",
    "consiguió", "consiguiu", "consigue", "conseguir", "conseguimos",
    # terminar
    "terminado", "terminada", "terminados", "terminadas",
    "terminó", "termina", "terminar", "terminaron",
    # finalizar
    "finalizado", "finalizada", "finalizados", "finalizadas",
    "finalizó", "finaliza", "finalizar",
    # lograr
    "logro", "logros", "logra", "logró", "logramos", "logrado", "lograda",
    # otros
    "listo",
)

_COMPLETION_RE = re.compile(
    r"\b(" + "|".join(re.escape(word) for word in COMPLETION_CLAIMS) + r")\b",
    re.IGNORECASE,
)


@dataclass
class ResponseComposer:
    """Compone la respuesta final desde el estado real. No inventa ni rellena."""

    #: Cuántos pasos intentados se mencionan como máximo (la respuesta es un resumen).
    max_steps: int = 4
    #: Cuánta evidencia se cita como máximo.
    max_evidence: int = 4

    def compose(
        self,
        mission,
        knowledge: KnowledgeState,
        verification=None,
        decisions: Iterable[DecisionRecord] = (),
        *,
        model_outcome: str = "none",
        model_error: str | None = None,
        self_brief: Any = None,
    ) -> UserReply:
        """Devuelve la respuesta honesta para el estado actual de la misión."""
        verdict = _verdict_of(knowledge)
        goal_verified = bool(getattr(verification, "verified", False))
        reason = str(getattr(verification, "reason", "") or knowledge.verdict_reason or "")

        attempted = list(getattr(knowledge, "completed_steps", []) or [])
        failed = list(getattr(knowledge, "failed_steps", []) or [])
        pending = self._pending(mission, knowledge, verification)
        blocked = self._blocked(mission, knowledge)
        needs_user = self._needs_user(mission, knowledge, verdict)
        evidence = self._evidence(knowledge, verification)

        text = self._text(
            mission=mission,
            verdict=verdict,
            goal_verified=goal_verified,
            reason=reason,
            attempted=attempted,
            failed=failed,
            pending=pending,
            blocked=blocked,
            needs_user=needs_user,
            evidence=evidence,
            model_outcome=model_outcome,
            replans=knowledge.replans,
        )
        # Guarda contra el falso éxito: sin verificación del objetivo, la respuesta no
        # puede contener ninguna afirmación de logro (ver `asserts_completion`).
        text, stripped = self._forbid_false_completion(text, goal_verified)

        reply = UserReply(
            text=text,
            kind="answer",
            claims=list(getattr(knowledge, "claims", []) or []),
            evidence=evidence,
            open_questions=self._open_questions(knowledge),
            mission_id=getattr(mission, "id", None),
            cognition_outcome=model_outcome,
            degraded=model_outcome != "real",
            verdict=verdict,
            goal_verified=goal_verified,
            pending=pending,
            blocked=blocked,
            needs_user=needs_user,
        )
        reply.self_update = self._self_update(verdict, goal_verified, model_outcome, stripped)
        return reply

    # ------------------------------------------------------------------ #
    # Texto
    # ------------------------------------------------------------------ #

    def _text(
        self,
        *,
        mission,
        verdict: str,
        goal_verified: bool,
        reason: str,
        attempted: list[str],
        failed: list[str],
        pending: list[str],
        blocked: bool,
        needs_user: bool,
        evidence: list[dict],
        model_outcome: str,
        replans: int,
    ) -> str:
        objective = _objective(mission)
        lines: list[str] = [f"Objetivo: {objective}"]

        if goal_verified:
            lines.append(
                "El objetivo está verificado con evidencia: cada criterio de éxito tiene "
                "su prueba observada."
            )
        else:
            lines.append(
                "El objetivo NO está verificado: que las herramientas funcionaran no "
                "demuestra que se cumpliera el objetivo."
            )

        lines.append(f"Verdicto de la última acción: {_label(verdict)}.")
        if reason:
            lines.append(f"Motivo: {reason}")

        if attempted:
            lines.append("Se intentó: " + ", ".join(attempted[-self.max_steps:]) + ".")
        if failed:
            lines.append("Falló: " + ", ".join(failed[-self.max_steps:]) + ".")
        if replans:
            lines.append(f"Hubo {replans} replan(s) antes de settle.")
        if evidence:
            lines.append(f"Evidencia disponible: {len(evidence)} referencia(s).")
        else:
            lines.append("No hay evidencia registrada que respalde el objetivo.")
        if pending:
            lines.append("Quedó pendiente: " + "; ".join(pending) + ".")
        if blocked:
            lines.append("Bloqueado: la autoridad (policy/gate/aprobación) detuvo la acción.")
        if needs_user:
            lines.append("Necesita tu intervención para poder continuar.")

        # La procedencia del modelo se conserva cuando afecta a la confiabilidad.
        if model_outcome == "degraded":
            lines.append(
                "Aviso: el razonamiento vino de un proveedor DEGRADED (respuesta de "
                "respaldo, no de un modelo real); los hechos de arriba son observados."
            )
        elif model_outcome == "unavailable":
            lines.append(
                "Aviso: no hubo modelo disponible (UNAVAILABLE); esta respuesta se compuso "
                "sólo con evidencia observada."
            )
        return "\n".join(lines)

    def _forbid_false_completion(self, text: str, goal_verified: bool) -> tuple[str, list[str]]:
        """Quita afirmaciones de logro si el objetivo no está verificado.

        Devuelve el texto saneado y las palabras retiradas, para poder auditarlas.
        """
        if goal_verified:
            return text, []
        hits = sorted({match.group(0) for match in _COMPLETION_RE.finditer(text)})
        if not hits:
            return text, []
        return _COMPLETION_RE.sub("(logro no verificado)", text), hits

    # ------------------------------------------------------------------ #
    # Piezas
    # ------------------------------------------------------------------ #

    def _pending(self, mission, knowledge, verification) -> list[str]:
        pending: list[str] = []
        for evaluation in getattr(verification, "evaluations", []) or []:
            status = getattr(evaluation, "status", None)
            if str(getattr(status, "value", status)) != "satisfied":
                criterion = getattr(evaluation, "criterion", "") or "criterio"
                pending.append(f"criterio «{criterion}» sin satisfacer")
        if not pending and not bool(getattr(verification, "verified", False)):
            pending.append("verificar el objetivo contra sus criterios")
        for step in list(getattr(knowledge, "failed_steps", []) or [])[-2:]:
            pending.append(f"resolver el fallo de «{step}»")
        return pending[:4]

    def _blocked(self, mission, knowledge) -> bool:
        if getattr(knowledge, "last_verdict", "") == Verdict.BLOCKED.value:
            return True
        return bool((getattr(mission, "context", {}) or {}).get("blocked_reason"))

    def _needs_user(self, mission, knowledge, verdict: str) -> bool:
        if (getattr(mission, "context", {}) or {}).get("pending_approval"):
            return True
        if verdict == Verdict.BLOCKED.value:
            return True
        return bool((getattr(mission, "context", {}) or {}).get("clarification"))

    def _evidence(self, knowledge, verification) -> list[dict]:
        evidence: list[dict] = []
        for claim in list(getattr(knowledge, "claims", []) or []):
            ids = list(getattr(claim, "evidence_ids", []) or [])
            if ids:
                evidence.append(
                    {
                        "claim": getattr(claim, "text", ""),
                        "kind": str(getattr(getattr(claim, "kind", None), "value", "")),
                        "evidence_ids": ids,
                    }
                )
        for evaluation in getattr(verification, "evaluations", []) or []:
            ids = _evidence_ids_of(evaluation)
            if ids:
                evidence.append(
                    {
                        "criterion": getattr(evaluation, "criterion", ""),
                        "status": str(getattr(getattr(evaluation, "status", None), "value", "")),
                        "evidence_ids": ids,
                    }
                )
        return evidence[: self.max_evidence]

    def _open_questions(self, knowledge) -> list[str]:
        return [str(u) for u in list(getattr(knowledge, "unknown", []) or [])[:3]]

    def _self_update(self, verdict: str, goal_verified: bool, model_outcome: str, stripped: list[str]) -> dict:
        return {
            "last_verdict": verdict,
            "goal_verified": goal_verified,
            "model_outcome": model_outcome,
            "unverified_completion_claims": stripped,
        }

    # ------------------------------------------------------------------ #
    # Invariante consultable
    # ------------------------------------------------------------------ #

    def asserts_completion(self, reply: UserReply) -> bool:
        """¿La respuesta afirma que algo se completó sin verificación del objetivo?

        Con `goal_verified=True` no puede pasar. Sin ella, devuelve `True` sólo si
        sobrevive alguna palabra de logro, que es exactamente el falso éxito que §5.6.1
        prohíbe.
        """
        if reply.goal_verified:
            return False
        return _COMPLETION_RE.search(reply.text or "") is not None

    def sanitize(self, text: str, goal_verified: bool) -> str:
        """Reaplica el guard sobre texto que venga de otro sitio (p. ej. un modelo).

        Un modelo que reformula la respuesta puede reintroducir una afirmación de logro.
        Todo canal que presente texto ajeno debe pasar por aquí antes de emit it.
        """
        if goal_verified:
            return text
        return _COMPLETION_RE.sub("(logro no verificado)", text or "")


# --------------------------------------------------------------------------- #
# Canales: una sola fuente semántica, varias presentaciones
# --------------------------------------------------------------------------- #


def read_composed(mission) -> UserReply | None:
    """Lee la respuesta ya compuesta que §5.6.9 dejó en `mission.context`.

    Es la fuente semántica común: los canales (voz, texto, avatar) la leen de aquí y sólo
    la presentan. Si no existe todavía, devuelve `None` y el canal debe decirlo, no
    inventar.
    """
    raw = (getattr(mission, "context", {}) or {}).get("response")
    if not raw:
        return None
    try:
        return UserReply(
            text=str(raw.get("text") or ""),
            kind=str(raw.get("kind") or "answer"),
            claims=[],
            evidence=list(raw.get("evidence") or []),
            open_questions=[str(q) for q in (raw.get("open_questions") or [])],
            mission_id=raw.get("mission_id"),
            self_update=dict(raw.get("self_update") or {}),
            cognition_outcome=str(raw.get("cognition_outcome") or "none"),
            degraded=bool(raw.get("degraded", False)),
            verdict=str(raw.get("verdict") or ""),
            goal_verified=bool(raw.get("goal_verified", False)),
            pending=[str(p) for p in (raw.get("pending") or [])],
            blocked=bool(raw.get("blocked", False)),
            needs_user=bool(raw.get("needs_user", False)),
        )
    except Exception:  # noqa: BLE001 — una fila vieja no puede tumbar un canal
        return None


def render_for_voice(reply: UserReply, *, max_sentences: int = 3) -> str:
    """Versión hablable de la respuesta compuesta. Un solo párrafo, sin markdown.

    No inventa: reformula lo que el compositor ya decidió a partir del estado real.
    """
    verdict = str(reply.verdict or Verdict.INSUFFICIENT_EVIDENCE.value)
    sentences: list[str] = []
    if reply.goal_verified:
        sentences.append("El objetivo está verificado con evidencia.")
    else:
        sentences.append("El objetivo no está verificado.")
    sentences.append(_LABEL_VOICE.get(verdict, "No hay veredicto."))
    if reply.blocked:
        sentences.append("La autoridad detuvo la acción, así que necesito tu intervención.")
    elif reply.needs_user:
        sentences.append("Necesito tu intervención para poder continuar.")
    elif reply.pending:
        sentences.append("Quedó pendiente: " + "; ".join(reply.pending[:2]) + ".")
    if reply.cognition_outcome == "degraded":
        sentences.append("Aviso: el razonamiento vino de un proveedor degradado.")
    elif reply.cognition_outcome == "unavailable":
        sentences.append("Aviso: no había modelo disponible.")
    return " ".join(sentences[:max_sentences])


_LABEL_VOICE = {
    Verdict.SUCCESS.value: "La última acción salió bien.",
    Verdict.PARTIAL_SUCCESS.value: "La última acción salió a medias, sin prueba completa.",
    Verdict.FAILURE.value: "La última acción falló.",
    Verdict.INSUFFICIENT_EVIDENCE.value: "No hay evidencia suficiente para afirmar nada.",
    Verdict.BLOCKED.value: "La acción quedó bloqueada.",
}

def _verdict_of(knowledge) -> str:
    verdict = str(getattr(knowledge, "last_verdict", "") or "")
    return verdict or Verdict.INSUFFICIENT_EVIDENCE.value


def _evidence_ids_of(evaluation) -> list[str]:
    """Ids de evidencia de un `CriterionEvaluation`.

    `CriterionEvaluation` no expone `evidence_ids`: lleva `evidence`, una lista de
    `CriterionEvidence` (`goal_verification.py:108`). Aquí se aplana a ids.
    """
    ids: list[str] = []
    for item in getattr(evaluation, "evidence", []) or []:
        evidence_id = getattr(item, "evidence_id", None)
        if evidence_id:
            ids.append(str(evidence_id))
    return ids


def _objective(mission) -> str:
    goal = getattr(mission, "goal", None)
    return str(getattr(goal, "objective", "") or "sin objetivo registrado")


_LABELS = {
    Verdict.SUCCESS.value: "SUCCESS (la acción se ejecutó y hay prueba de eso)",
    Verdict.PARTIAL_SUCCESS.value: "PARTIAL_SUCCESS (la acción corrió sin prueba suficiente)",
    Verdict.FAILURE.value: "FAILURE (la acción se intentó y no consiguió su propósito)",
    Verdict.INSUFFICIENT_EVIDENCE.value: "INSUFFICIENT_EVIDENCE (no se puede afirmar ni sí ni no)",
    Verdict.BLOCKED.value: "BLOCKED (la autoridad detuvo la acción)",
}


def _label(verdict: str) -> str:
    return _LABELS.get(verdict, verdict or "sin veredicto")
