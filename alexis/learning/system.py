"""Sistema de aprendizaje (P0 §5.6.4).

`ExperienceLearner` ya no es un `append` a una lista en memoria: delega en la
implementación real (`alexis.learning.experience`) y expone lo aprendido a través del
mismo contrato. `self.experiences` se conserva por compatibilidad con los tests que lo
leen, pero la fuente de verdad es `records`.
"""

from abc import ABC, abstractmethod
from dataclasses import replace

from alexis.learning.experience import (
    Experience,
    ExperienceStore,
    LearningBoundary,
    VerifiedExperience,
)
from alexis.learning.reflection import build_reflection
from alexis.cognition.state import KnowledgeState, Verdict


class LearningSystem(ABC):
    @abstractmethod
    async def record_experience(self, mission, verification, goal_verified: bool | None = None): ...


class ExperienceLearner(LearningSystem):
    """Registra la experiencia de una misión y la frontera de aprendizaje.

    Sólo aprende de misiones cuyo objetivo está verificado: una herramienta que fue bien
    no es un objetivo conseguido (`ACTION SUCCESS -> OBJECTIVE SUCCESS`).
    """

    def __init__(self):
        self.experiences: list[dict] = []
        self.records: list[VerifiedExperience] = []
        self.boundary = LearningBoundary()
        # R-02: se eliminó `self.store`. Sólo servía para una llamada a `publish()` que
        # nunca podía tener efecto (sin `observation_repo` devuelve `False` sin publicar).
        # La recuperación tras reinicio la hace `ExperienceStore.load()`, que lee de
        # `mission.context` — y eso lo escribe `ExperienceStore.attach()`, un staticmethod
        # que no necesita instancia.

    async def record_experience(self, mission, verification, goal_verified: bool | None = None):
        """Registra la experiencia y la frontera de aprendizaje.

        `goal_verified` es el valor que el runtime ya calculó en `settle()`, y es el único
        que significa «el OBJETIVO se consiguió», no «la herramienta funcionó». Por eso se
        pasa explícitamente en vez de deducirlo aquí.

        **Por qué antes se adivinaba, y por qué adivinar rompía el ciclo entero.**
        La versión anterior hacía `getattr(verification, "verified", False)`. Ese atributo
        existe en `GoalVerification`, pero el runtime pasa un `Verification`, que trae
        `.passed`. `getattr` con valor por defecto no lanza: devolvía `False` siempre y en
        silencio. Los tests no lo detectaban porque llamaban con `GoalVerification`, el tipo
        que sí tiene `.verified`.

        Consecuencia medida en la base real: 466 verificaciones exitosas, 431 misiones
        completadas, y 229 lecciones guardadas — las 229 con `outcome='unknown'` y la tabla
        `skills` vacía. `classify_outcome(goal_verified=False)` nunca devuelve SUCCESS, y
        `runtime._advance_learning` corta antes de `skill_from_lesson`. El sistema guardaba
        historial y no aprendía nada.

        Sin `goal_verified` explícito se sigue leyendo `.verified`, que es la vía correcta
        para quien llega con un `GoalVerification`. Deliberadamente NO se deduce de
        `.passed`: `passed=True` significa que la herramienta pasó, y el módulo entero
        sostiene la distinción ACTION SUCCESS → OBJECTIVE SUCCESS. Adivinar aquí
        reintroduciría exactamente el error que se acaba de corregir.
        """
        if goal_verified is None:
            goal_verified = bool(getattr(verification, "verified", False))
        knowledge = self._knowledge(mission)
        reflection = build_reflection(mission, knowledge, verification)
        experience = Experience(
            mission_id=str(getattr(mission, "id", "") or ""),
            objective=str(getattr(getattr(mission, "goal", None), "objective", "") or ""),
            verdict=str(getattr(knowledge, "last_verdict", "") or ""),
            outcome=str(getattr(getattr(mission, "state", None), "value", "") or ""),
            goal_verified=goal_verified,
            reflection=reflection.to_dict(),
        )
        verified = self.boundary.evaluate(experience, reflection)
        # CORE-11: los `evidence_refs` viajan vacíos. La `LearningBoundary` puede afirmar sin
        # ellos, pero una `Experience` sin evidencia no puede sostener una `Lesson` que sí la
        # tenga: el aprendizaje se cortaba justo después de verificarse. Se rellenan desde los
        # claims REALES de la misión, los mismos que respaldan la verificación.
        #
        # `Experience` es un dataclass CONGELADO (una experiencia es un hecho, no se edita a
        # posteriori), así que se reconstruye con `replace` en vez de asignar el campo.
        if not experience.evidence_refs:
            refs = sorted({
                str(evidence_id)
                for claim in list(getattr(knowledge, "claims", []) or [])
                for evidence_id in (getattr(claim, "evidence_ids", []) or [])
            })[:8]
            if not refs:
                # Sin `evidence_ids` explícitos, el identificador del propio claim es la
                # referencia mínima defendible: existe y es trazable.
                refs = sorted({
                    str(getattr(claim, "id", "") or "")
                    for claim in list(getattr(knowledge, "claims", []) or [])
                    if getattr(claim, "id", "")
                })[:8]
            experience = replace(experience, evidence_refs=refs)
        # Se adjunta al contexto ANTES de publicarla, para que sobreviva al reinicio. El
        # `verified` es un dataclass congelado: se adjunta la reflexión que ya lleva dentro.
        ExperienceStore.attach(mission, experience, verified)
        self.records.append(verified)
        self.experiences.append(
            {
                "mission": mission.id,
                "objective": experience.objective,
                "passed": goal_verified,
                "confidence": reflection.confidence,
                "evidence": experience.evidence_refs,
                "verdict": experience.verdict,
                "quality": verified.quality,
                "lesson": verified.lesson,
            }
        )
        # R-02: aquí antes había `await self.store.publish(verified)`, que NUNCA hacía nada.
        # `ExperienceStore.publish()` sólo publica si tiene un `observation_repo`, y el
        # learner se construía sin él, así que devolvía `False` en silencio. La publicación
        # real la hace `runtime.py` —que sí tiene el repo— justo antes de cerrar la misión.
        #
        # No se inyecta el repo aquí a propósito: entonces se publicaría DOS veces la misma
        # experiencia (una por el learner, otra por el runtime) y `observations` se
        # duplicaría sin que nada lo pidiera. El runtime es el único dueño de esa vía.
        return verified

    def lessons(self) -> list[str]:
        """Lecciones que la frontera autorizó enseñar. Lo demás no sale de aquí."""
        return [r.lesson for r in self.records if r.can_teach() and r.lesson]

    @staticmethod
    def _knowledge(mission) -> KnowledgeState:
        raw = (getattr(mission, "context", {}) or {}).get("knowledge")
        if raw:
            return KnowledgeState.from_dict(raw, str(getattr(getattr(mission, "goal", None), "objective", "")))
        knowledge = KnowledgeState(objective=str(getattr(getattr(mission, "goal", None), "objective", "")))
        knowledge.last_verdict = Verdict.INSUFFICIENT_EVIDENCE.value
        return knowledge
