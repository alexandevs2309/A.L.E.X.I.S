"""CORE-11 — Outcome, Lesson y el ciclo que las convierte en algo reutilizable.

Hasta aquí ALEXIS aprendía de dos maneras: guardaba la experiencia y escribía una frase como
"lección". La frase era texto. Este módulo la convierte en un objeto con requisitos,
aplicabilidad y contraindicaciones, y la frankly explícita sobre cuándo NO debe usarse.

Y antes, la taxonomía que faltaba: `Outcome`. Existía `verdict` (el juicio del Core sobre un
paso) y `mission.state` (dónde está la misión), pero ninguno respondía a "cómo acabó la
operación". Confundirlas era un riesgo real: `fs.write` con `ok: true` es un paso que pasó, y
`verified: false` es un objetivo que NO seGotó. El primero no es el segundo.

    VERIFIED OUTCOME  >  OBSERVED OUTCOME  >  MODEL CLAIM

Esa jerarquía no es una preferencia: decide qué puede enseñar. Una lección que se apoya en que
una herramienta funcionó, cuando el objetivo no se demostró, no es una lección sobre cómo
conseguir objetivos. Es una lección sobre cómo lewentó bien una herramienta.

La regla que atraviesa el módulo, y que `SkillValidator` vuelve a comprobar desde el otro
lado: **una lección no cambia la autoridad**. Describe qué hacer dentro de las capacidades que
ya están autorizadas, y por eso una `Lesson` lleva `scope` y `applicability`: para que el
descubrimiento decida si encaja, no para que ensanche lo que puede.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Outcome(str, Enum):
    """Cómo acabó la operación, no el paso ni el estado del bookkeeping."""

    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"
    ABORTED = "aborted"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"

    @property
    def is_verified_success(self) -> bool:
        """El único que puede enseñar una estrategia que FUNCIONÓ.

        `PARTIAL` y `UNKNOWN` no. Un objetivo a medio hacer es exactamente el caso donde una
        skill validada sería una mentira: demostraría que un procedimiento funciona cuando sólo
        funcionó a ratos.
        """
        return self is Outcome.SUCCESS

    @property
    def is_teachable(self) -> bool:
        """Todos los finales enseñan ALGO; no todos enseñan a hacer las cosas bien.

        Un fallo teaches "esto no funciona así", que es conocimiento real. Un abort teaches por
        qué se paró. Lo que no se puede enseñar es un éxito no verificado.
        """
        return self is not Outcome.UNKNOWN


def classify_outcome(
    *,
    goal_verified: bool,
    mission_state: str = "",
    blocked_reason: str = "",
    recovery_aborted: str = "",
    failures: list[str] | None = None,
) -> Outcome:
    """Deriva el `Outcome` de lo que REALMENTE pasó, no de lo que la herramienta dijo.

    El orden importa. `blocked` y `aborted` se comprueban antes que `failure` porque una misión
    bloqueada también tiene "fallos" y contarla como fallida perdería la información de que
    nadie pudo decidir nada.
    """
    if recovery_aborted:
        return Outcome.ABORTED
    state = (mission_state or "").strip().lower()
    if state in ("blocked", "waiting_approval"):
        return Outcome.BLOCKED
    if blocked_reason:
        return Outcome.BLOCKED
    if goal_verified:
        # Verificado y con fallos por el camino: el objetivo se gotó igualmente. Eso es SUCCESS,
        # y la historia de los fallos vive en la reflexión, no en el desenlace.
        return Outcome.SUCCESS
    if state in ("failed", "cancelled"):
        return Outcome.FAILURE
    if failures:
        return Outcome.PARTIAL
    if state in ("needs_verification", "pending", "running", "planning", "verifying"):
        # Ni chegou a comprobarse. No es fallo: no se sabe.
        return Outcome.PARTIAL if failures else Outcome.UNKNOWN
    return Outcome.UNKNOWN


@dataclass
class Lesson:
    """Una conclusión operacional con requisitos, alcance y contraindicaciones.

    Lo que la separa de una frase guardada es que responde `applicability` y
    `contraindications`. Sin ellas, cualquier lección se aplicaría en cualquier parte, y una
    lección sin contraindicaciones es una regla que no puede ser correcta: si algo vale
    siempre, no era una lección sobre el mundo sino sobre el ejecutor.
    """

    lesson_id: str
    statement: str
    source_experience: str
    outcome: str = Outcome.UNKNOWN.value
    #: Evidence ids que respaldan la lección. Vacía = no es conocimiento, es conjetura.
    evidence: list[str] = field(default_factory=list)
    #: Confianza 0..1, y de dónde sale. Un MODEL CLAIM no puede producir confianza alta.
    confidence: float = 0.0
    confidence_basis: str = ""
    #: Dónde aplica: `filesystem/write-recovery`, por ejemplo. Es la clave de la búsqueda.
    scope: str = ""
    prerequisites: list[str] = field(default_factory=list)
    applicability: str = ""
    contraindications: list[str] = field(default_factory=list)
    #: Una lección NUNCA cambia estas cosas. Se declara para que la validación lo compruebe.
    authorization: str = "does_not_grant_authority"
    version: int = 1
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "lesson_id": self.lesson_id,
            "statement": self.statement,
            "source_experience": self.source_experience,
            "outcome": self.outcome,
            "evidence": list(self.evidence),
            "confidence": self.confidence,
            "confidence_basis": self.confidence_basis,
            "scope": self.scope,
            "prerequisites": list(self.prerequisites),
            "applicability": self.applicability,
            "contraindications": list(self.contraindications),
            "authorization": self.authorization,
            "version": self.version,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "Lesson | None":
        if not raw or not raw.get("statement"):
            return None
        return cls(
            lesson_id=str(raw.get("lesson_id") or ""),
            statement=str(raw.get("statement") or ""),
            source_experience=str(raw.get("source_experience") or ""),
            outcome=str(raw.get("outcome") or Outcome.UNKNOWN.value),
            evidence=list(raw.get("evidence") or []),
            confidence=float(raw.get("confidence") or 0.0),
            confidence_basis=str(raw.get("confidence_basis") or ""),
            scope=str(raw.get("scope") or ""),
            prerequisites=list(raw.get("prerequisites") or []),
            applicability=str(raw.get("applicability") or ""),
            contraindications=list(raw.get("contraindications") or []),
            authorization=str(raw.get("authorization") or "does_not_grant_authority"),
            version=int(raw.get("version") or 1),
            created_at=float(raw.get("created_at") or 0.0),
        )

    @property
    def is_backed(self) -> bool:
        """¿Hay algo detrás, o es una afirmación?

        Una lección sin evidencia se puede registrar, se puede buscar y NO se puede promover a
        skill. Es la frontera entre "lo que ALEXIS cree" y "lo que ALEXIS sabe".
        """
        return bool(self.evidence)


# ---------------------------------------------------------------------- #
# De Outcome a Lesson: qué se puede enseñar y con qué confianza
# ---------------------------------------------------------------------- #

#: Un MODEL CLAIM es una propuesta. No puede sostener una confianza alta por muy bien redactada
#: que venga: la procedencia manda sobre la eloquencia.
_BASIS_BY_OUTCOME = {
    Outcome.SUCCESS.value: ("verified_outcome", 0.9),
    Outcome.PARTIAL.value: ("observed_outcome", 0.4),
    Outcome.FAILURE.value: ("observed_outcome", 0.5),
    Outcome.BLOCKED.value: ("observed_outcome", 0.5),
    Outcome.ABORTED.value: ("observed_outcome", 0.4),
    Outcome.UNKNOWN.value: ("unverified", 0.0),
}


def lesson_confidence(outcome: str, *, model_only: bool = False) -> tuple[float, str]:
    """Confianza y su base. Nunca se inventa confianza para algo no verificado."""
    basis, confidence = _BASIS_BY_OUTCOME.get(outcome, ("unverified", 0.0))
    if model_only:
        # Una lección que viene de un modelo sigue siendo una PROPUESTA. Se le recorta la
        # confianza y se dice de dónde sale, para que nadie lapromocione creyendo que hay más.
        return min(confidence, 0.3), "model_claim"
    return confidence, basis


def build_lesson_from_outcome(
    *,
    experience_id: str,
    statement: str,
    outcome: Outcome,
    evidence: list[str] | None = None,
    scope: str = "",
    applicability: str = "",
    contraindications: list[str] | None = None,
    prerequisites: list[str] | None = None,
    model_only: bool = False,
) -> Lesson:
    """La lección, con sus límites. `contraindications` no es opcional en la práctica.

    Se construye con los límites puestos, no se descubren después: una lección sin
    contraindicación es la que más daño hace, porque se aplica donde no debe.
    """
    confidence, basis = lesson_confidence(outcome.value, model_only=model_only)
    return Lesson(
        lesson_id=f"lesson-{uuid.uuid4().hex[:12]}",
        statement=statement,
        source_experience=experience_id,
        outcome=outcome.value,
        evidence=list(evidence or []),
        confidence=confidence,
        confidence_basis=basis,
        scope=scope,
        prerequisites=list(prerequisites or []),
        applicability=applicability,
        contraindications=list(contraindications or []),
        created_at=time.time(),
    )


__all__ = [
    "Lesson",
    "Outcome",
    "build_lesson_from_outcome",
    "classify_outcome",
    "lesson_confidence",
]