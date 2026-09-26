"""Experience y Verified Learning Boundary (P0 §5.6.4 y §5.6.5).

Cierra el ciclo ``EXPERIENCE → EVALUATION → VERIFIED EXPERIENCE → LESSON CANDIDATE →
LEARNING``.

Dos decisiones de diseño, ambas para no duplicar infraestructura:

1. **La experiencia se persiste en `mission.context`**, que la capa de storage ya
   transporta dentro del JSONB de `missions` y ya rehidrata `mission_from_row`. Así
   sobrevive al reinicio sin una tabla nueva ni un repositorio paralelo (§5.6.8).
2. **La experiencia se publica además como observación** (``source="experience"``) usando el
   `ObservationRepository` que ya existe. Como `PostgresMemoryProvider` lee esa misma tabla,
   la experiencia queda disponible para recuperación futura sin tocar el sistema de memoria
   (§5.6.6: "no diseñes un nuevo sistema de memoria paralelo").

La frontera de aprendizaje es el punto donde el plan veta los atajos: nada que no tenga
evidencia verificada se convierte en conocimiento, y ninguna experiencia modifica la
autoridad de ALEXIS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from alexis.cognition.contracts import ClaimKind
from alexis.cognition.state import Verdict
from alexis.learning.reflection import Reflection

#: `source` con el que la experiencia se publica en la tabla `observations`.
EXPERIENCE_SOURCE = "experience"


@dataclass(frozen=True)
class Experience:
    """Lo que una misión enseñó, en forma recuperable.

    Congelada igual que `Reflection`: una experiencia no se edita a posteriori para
    improving lo que se aprendió.
    """

    mission_id: str
    objective: str
    #: Contexto relevante en el momento de la decisión (memoria que se consultó).
    context: list[str] = field(default_factory=list)
    #: Acciones realizadas, como ``step_id:capability``.
    actions: list[str] = field(default_factory=list)
    #: Resultados observados por acción.
    results: list[str] = field(default_factory=list)
    #: Referencias de evidencia (ids), no el texto del razonamiento.
    evidence_refs: list[str] = field(default_factory=list)
    verdict: str = ""
    outcome: str = ""
    goal_verified: bool = False
    reflection: dict[str, Any] = field(default_factory=dict)
    #: Procedencia del modelo que decidió: real | degraded | unavailable | none.
    model_outcome: str = "none"
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "objective": self.objective,
            "context": list(self.context),
            "actions": list(self.actions),
            "results": list(self.results),
            "evidence_refs": list(self.evidence_refs),
            "verdict": self.verdict,
            "outcome": self.outcome,
            "goal_verified": self.goal_verified,
            "reflection": dict(self.reflection),
            "model_outcome": self.model_outcome,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "Experience":
        return cls(
            mission_id=str(row.get("mission_id") or ""),
            objective=str(row.get("objective") or ""),
            context=[str(x) for x in (row.get("context") or [])],
            actions=[str(x) for x in (row.get("actions") or [])],
            results=[str(x) for x in (row.get("results") or [])],
            evidence_refs=[str(x) for x in (row.get("evidence_refs") or [])],
            verdict=str(row.get("verdict") or ""),
            outcome=str(row.get("outcome") or ""),
            goal_verified=bool(row.get("goal_verified")),
            reflection=dict(row.get("reflection") or {}),
            model_outcome=str(row.get("model_outcome") or "none"),
            timestamp=float(row.get("timestamp") or 0.0),
        )

    def is_frozen(self) -> bool:
        """El invariante de §5.6.4, consultable sin mutar nada."""
        return bool(getattr(type(self).__dataclass_params__, "frozen", False))


@dataclass(frozen=True)
class VerifiedExperience:
    """Experiencia evaluada por la frontera. `can_teach` es la decisión de aprendizaje."""

    experience: Experience
    reflection: Reflection
    #: `verified` | `partial` | `insufficient` | `blocked`.
    quality: str
    #: Lección candidata, o "" si no hay nada aprendible con honestidad.
    lesson: str = ""
    #: Clase epistémica que esta experiencia puede aportar. Nunca `FACT` sin verificación
    #: independiente: lo máximo es `EVIDENCE` (P0 §5.6.5).
    claim_kind: str = ClaimKind.EVIDENCE.value
    #: Por qué no puede enseñar, cuando `can_teach` es False.
    reason: str = ""

    def can_teach(self) -> bool:
        """¿Esta experiencia puede convertirse en aprendizaje?

        Reglas de §5.6.5:
          - Sin objetivo verificado, nunca.
          - ``INSUFFICIENT_EVIDENCE`` y ``BLOCKED`` nunca enseñan como éxito.
          - ``SUCCESS`` verificado sí.
          - ``PARTIAL_SUCCESS`` y ``FAILURE`` enseñan, pero sólo sobre sí mismos.
        """
        return bool(self.lesson) and self.quality != "insufficient"

    def is_frozen(self) -> bool:
        """El invariante de §5.6.5, consultable sin mutar nada."""
        return bool(getattr(type(self).__dataclass_params__, "frozen", False))

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.experience.mission_id,
            "objective": self.experience.objective,
            "quality": self.quality,
            "lesson": self.lesson,
            "claim_kind": self.claim_kind,
            "can_teach": self.can_teach(),
            "reason": self.reason,
            "verdict": self.experience.verdict,
            "goal_verified": self.experience.goal_verified,
        }


class LearningBoundary:
    """La frontera explícita EXPERIENCE → VERIFIED → LESSON (§5.6.5).

    Deliberadamente sin estado y sin referencia a Policy, Gate, catálogo ni envelope: la
    frontera no puede cambiar la autoridad de ALEXIS porque no la conoce (tests 20-21).
    """

    def evaluate(self, experience: Experience, reflection: Reflection) -> VerifiedExperience:
        verdict = experience.verdict
        quality = _quality(experience, reflection)

        if not experience.goal_verified:
            return VerifiedExperience(
                experience=experience,
                reflection=reflection,
                quality="insufficient",
                lesson="",
                claim_kind=ClaimKind.UNCERTAINTY.value,
                reason="el objetivo no está verificado: no hay conocimiento que extraer",
            )

        if verdict == Verdict.INSUFFICIENT_EVIDENCE.value or quality == "insufficient":
            return VerifiedExperience(
                experience=experience,
                reflection=reflection,
                quality="insufficient",
                lesson="",
                claim_kind=ClaimKind.UNCERTAINTY.value,
                reason="evidencia insuficiente: no se convierte en hecho",
            )

        if verdict == Verdict.BLOCKED.value:
            return VerifiedExperience(
                experience=experience,
                reflection=reflection,
                quality="blocked",
                lesson=_blocked_lesson(experience, reflection),
                claim_kind=ClaimKind.EVIDENCE.value,
                reason="se registra el bloqueo, nunca una conclusión de éxito",
            )

        if verdict == Verdict.FAILURE.value:
            return VerifiedExperience(
                experience=experience,
                reflection=reflection,
                quality="partial",
                lesson=(
                    f"El objetivo «{experience.objective}» se verificó pero la última acción "
                    f"falló ({_first_failure(reflection)}). No repetir el mismo enfoque sin "
                    "cambiar la estrategia."
                ),
                claim_kind=ClaimKind.EVIDENCE.value,
                reason="aprendizaje sobre el fallo, nunca conocimiento de éxito",
            )

        if verdict == Verdict.PARTIAL_SUCCESS.value:
            return VerifiedExperience(
                experience=experience,
                reflection=reflection,
                quality="partial",
                lesson=(
                    f"El objetivo «{experience.objective}» se verificó parcialmente. "
                    "Aprendizaje condicional: falta comprobar lo que quedó pendiente."
                ),
                claim_kind=ClaimKind.EVIDENCE.value,
                reason="aprendizaje limitado por resultado parcial",
            )

        # SUCCESS con objetivo verificado y evidencia verificada.
        return VerifiedExperience(
            experience=experience,
            reflection=reflection,
            quality=quality,
            lesson=reflection.lessons_candidate or f"«{experience.objective}» verificado.",
            claim_kind=ClaimKind.EVIDENCE.value,
            reason="objetivo verificado con evidencia",
        )


def promote_claims(verified: VerifiedExperience, claims: Iterable[Any] = ()) -> list[Any]:
    """Convierte una experiencia verificada en claims, sin salto epistémico.

    Un `ASSUMPTION` nunca sale como `FACT`, y un `INFERENCE` nunca sale como hecho
    verificado (P0 §5.6.5). Lo máximo que sale de aquí es `EVIDENCE`.
    """
    promoted: list[Any] = []
    for claim in claims:
        kind = getattr(getattr(claim, "kind", None), "value", None) or ClaimKind.ASSUMPTION.value
        if kind == ClaimKind.ASSUMPTION.value:
            new_kind = ClaimKind.ASSUMPTION.value  # sigue siendo supuesto
        elif kind == ClaimKind.INFERENCE.value:
            new_kind = ClaimKind.INFERENCE.value  # sigue siendo inferencia
        else:
            new_kind = ClaimKind.EVIDENCE.value  # lo máximo al que se llega
        try:
            clone = type(claim)(
                id=getattr(claim, "id", ""),
                kind=ClaimKind(new_kind),
                text=getattr(claim, "text", ""),
                source=f"experience:{verified.experience.mission_id}",
                evidence_ids=list(getattr(claim, "evidence_ids", []) or []),
                confidence=getattr(claim, "confidence", 0.0),
                verified=verified.can_teach() and new_kind == ClaimKind.EVIDENCE.value,
            )
        except Exception:  # noqa: BLE001 — un claim de otra forma no impide el resto
            continue
        promoted.append(clone)
    return promoted


class ExperienceStore:
    """Persistencia de la experiencia en la infraestructura que YA existe.

    `mission.context` es la vía de recuperación (viaja con la misión, §5.6.8);
    `observation_repo` publica la experiencia como observación para que el
    `PostgresMemoryProvider` la recupere en consultas futuras (§5.6.6).
    """

    def __init__(self, observation_repo=None):
        self.observation_repo = observation_repo

    @staticmethod
    def attach(mission, experience: Experience, verified: VerifiedExperience) -> None:
        """Guarda experiencia + reflexión + aprendizaje en `mission.context`."""
        context = getattr(mission, "context", None)
        if context is None:
            return
        context["experience"] = experience.to_dict()
        context["reflection"] = verified.reflection.to_dict()
        context["verified_learning"] = verified.to_dict()

    @staticmethod
    def load(mission) -> VerifiedExperience | None:
        """Recupera la experiencia tras reiniciar. `None` si la misión no tuvo epílogo."""
        context = getattr(mission, "context", {}) or {}
        raw = context.get("experience")
        if not raw:
            return None
        experience = Experience.from_dict(raw)
        reflection = Reflection.from_dict(context.get("reflection") or {})
        quality = str((context.get("verified_learning") or {}).get("quality") or "insufficient")
        lesson = str((context.get("verified_learning") or {}).get("lesson") or "")
        reason = str((context.get("verified_learning") or {}).get("reason") or "")
        verified = VerifiedExperience(
            experience=experience,
            reflection=reflection,
            quality=quality,
            lesson=lesson,
            claim_kind=str((context.get("verified_learning") or {}).get("claim_kind") or "evidence"),
            reason=reason,
        )
        return verified

    async def publish(self, verified: VerifiedExperience) -> bool:
        """Publica la experiencia como observación para la memoria. `False` si no hay repo."""
        if self.observation_repo is None:
            return False
        await self.observation_repo.insert(
            verified.experience.mission_id,
            EXPERIENCE_SOURCE,
            verified.to_dict(),
            # `trusted=False` por diseño: una experiencia es contexto, no autoridad (§5.6.6).
            trusted=False,
        )
        return True


def _quality(experience: Experience, reflection: Reflection) -> str:
    if not experience.goal_verified:
        return "insufficient"
    if experience.verdict == Verdict.BLOCKED.value:
        return "blocked"
    quality = str(reflection.evidence_quality or "insufficient")
    return quality if quality in ("verified", "partial") else "insufficient"


def _first_failure(reflection: Reflection) -> str:
    failures = list(reflection.what_failed)
    return failures[0] if failures else "sin detalle registrado"


def _blocked_lesson(experience: Experience, reflection: Reflection) -> str:
    blockers = list(reflection.blockers)
    detail = blockers[0] if blockers else "la autoridad detuvo la acción"
    return (
        f"El objetivo «{experience.objective}» quedó bloqueado: {detail}. "
        "Se registra el bloqueo; no es una conclusión de éxito."
    )
