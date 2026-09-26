"""P0 §5.3 — GoalVerifier: comprobar el OBJETIVO, no la acción.

Este módulo existe para tapar el hueco que §4 del audit describes: hasta ahora la
misión podía quedar `COMPLETED` porque el último paso terminó sin error. Eso es
`ACTION SUCCESS`, y el plan veta explícitamente convertirlo en `OBJECTIVE SUCCESS`.

La separación es estructural, no de estilo:

    ACTION → OBSERVATION → VERDICT (§5.2) → GOAL VERIFICATION (aquí) → FINAL STATE (§5.5)

`GoalVerifier.verify()` no recibe un `ExecutionResult` ni un paso: recibe la misión (su
objetivo y sus `success_criteria`, persistidas en §5.1) y las fuentes de evidencia. No hay
forma de que le pasen "la tool terminó bien" como si eso probara el objetivo.

Reglas que este módulo sostiene:

- Un criterio solo se marca `satisfied` con evidencia de observación real de una
  herramienta. Un claim del modelo no basta nunca, ni aunque el modelo esté seguro.
- Un criterio sin checker no se da por cumplido: se queda en `insufficient_evidence`. La
  ausencia de evidencia no es evidencia de ausencia, y tampoco de cumplimiento.
- `verified` exige TODOS los criterios `satisfied` y al menos uno. Una misión sin
  criterios no es verificable: `verified=False`.
- Cada evaluación lleva su evidencia y su motivo, para que el resultado sea auditable
  criterio por criterio.

El vocabulario de predicados es deliberadamente estrecho y explícito. Se acepta solo lo
que una herramienta puede observar hoy de forma estructurada (rutas del workspace). Los
criterios que speak de otra cosa —"los tests pasan"— quedan en `insufficient_evidence`
hasta que exista su checker (§5.4). Preferimos eso a un verificador que adivine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from alexis.contracts import Mission
from alexis.world.model import TEST

#: Predicados que una observación de herramienta puede comprobar de forma estructurada.
#: Formato en el texto del criterio: ``predicado:argumento[:extra]``.
PREDICATES = (
    "file_size_at_least",
    "file_exists",
    "file_missing",
    "tests_failing",
    "tests_passing",
)

#: Grados de evidencia. Solo los observados por herramientas y los FACT verificados
#: independientemente pueden sostener un ``satisfied``.
GRADE_EVIDENCE = "evidence"
GRADE_FACT = "fact"
GRADE_INFERENCE = "inference"
GRADE_ASSUMPTION = "assumption"
GRADE_UNCERTAINTY = "uncertainty"


class CriterionStatus(str, Enum):
    """Veredicto de UN criterio de éxito, no de la acción ni de la misión."""

    SATISFIED = "satisfied"
    NOT_SATISFIED = "not_satisfied"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass
class CriterionEvidence:
    """Una evidencia concreta usada (o descartada) para evaluar un criterio."""

    evidence_id: str
    source: str
    grade: str
    detail: str
    trusted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "source": self.source,
            "grade": self.grade,
            "detail": self.detail,
            "trusted": self.trusted,
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> "CriterionEvidence | None":
        if not isinstance(raw, dict) or not raw.get("evidence_id"):
            return None
        return cls(
            evidence_id=str(raw.get("evidence_id") or ""),
            source=str(raw.get("source") or ""),
            grade=str(raw.get("grade") or GRADE_UNCERTAINTY),
            detail=str(raw.get("detail") or ""),
            trusted=bool(raw.get("trusted")),
        )


@dataclass
class CriterionEvaluation:
    """Criterio → evidencia → veredicto. La trazabilidad vive aquí dentro."""

    criterion: str
    status: CriterionStatus
    reason: str
    predicate: str | None = None
    evidence: list[CriterionEvidence] = field(default_factory=list)

    @property
    def is_satisfied(self) -> bool:
        return self.status is CriterionStatus.SATISFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion": self.criterion,
            "status": self.status.value,
            "reason": self.reason,
            "predicate": self.predicate,
            "evidence": [e.to_dict() for e in self.evidence],
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> "CriterionEvaluation | None":
        if not isinstance(raw, dict) or not raw.get("criterion"):
            return None
        status_value = str(raw.get("status") or CriterionStatus.INSUFFICIENT_EVIDENCE.value)
        try:
            status = CriterionStatus(status_value)
        except ValueError:
            status = CriterionStatus.INSUFFICIENT_EVIDENCE
        return cls(
            criterion=str(raw.get("criterion") or ""),
            status=status,
            reason=str(raw.get("reason") or ""),
            predicate=raw.get("predicate") if isinstance(raw.get("predicate"), str) else None,
            evidence=[e for e in (CriterionEvidence.from_dict(x) for x in raw.get("evidence") or []) if e],
        )


@dataclass
class GoalVerification:
    """Resultado de evaluar el objetivo de una misión criterio por criterio."""

    objective: str
    evaluations: list[CriterionEvaluation] = field(default_factory=list)
    verified: bool = False
    reason: str = ""

    def counts(self) -> dict[str, int]:
        return {
            status.value: sum(1 for e in self.evaluations if e.status is status)
            for status in CriterionStatus
        }

    def unsatisfied(self) -> list[CriterionEvaluation]:
        return [e for e in self.evaluations if e.status is CriterionStatus.NOT_SATISFIED]

    def unknown(self) -> list[CriterionEvaluation]:
        return [e for e in self.evaluations if e.status is CriterionStatus.INSUFFICIENT_EVIDENCE]

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "verified": self.verified,
            "reason": self.reason,
            "counts": self.counts(),
            "criteria": [e.to_dict() for e in self.evaluations],
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> "GoalVerification | None":
        if not isinstance(raw, dict):
            return None
        evaluations = [e for e in (CriterionEvaluation.from_dict(x) for x in raw.get("criteria") or []) if e]
        return cls(
            objective=str(raw.get("objective") or ""),
            evaluations=evaluations,
            verified=bool(raw.get("verified")),
            reason=str(raw.get("reason") or ""),
        )


def parse_predicate(criterion: str) -> tuple[str, list[str]] | None:
    """Extrae un predicado conocido del texto del criterio.

    Devuelve ``None`` si el criterio no nombra ninguno. No hay fuzzy matching: un
    criterio en lenguaje natural sin predicado explícito no se puede comprobar, y se
    dice en vez de suponer.
    """
    text = (criterion or "").strip()
    lowered = text.lower()
    for name in PREDICATES:
        marker = f"{name}:"
        index = lowered.find(marker)
        if index == -1:
            continue
        rest = text[index + len(marker) :].strip()
        # Solo el primer token: `file_exists:notas.txt está escrito` da el argumento
        # `notas.txt`, no la frase entera. Una ruta con espacios no se comprueba (el
        # lookup fallará y quedará en insufficient_evidence), que es lo honesto.
        head = rest.split()[0] if rest.split() else ""
        args = [part.strip().strip("\"'.,;()[]") for part in head.split(":")]
        return name, [arg for arg in args if arg]
    return None


class GoalVerifier:
    """Comprueba los `success_criteria` de una misión contra evidencia observada."""

    def __init__(self, world=None, evidence=None):
        self.world = world
        self.evidence = evidence

    # ------------------------------------------------------------------ #
    # API
    # ------------------------------------------------------------------ #

    def verify(self, mission: Mission, criteria: list[str] | None = None) -> GoalVerification:
        """Evalúa el objetivo de la misión. No recibe resultados de herramientas.

        `criteria` permite verificar un subconjunto; por defecto usa los
        `mission.goal.success_criteria` que §5.1 dejó persistidos.
        """
        goal = getattr(mission, "goal", None)
        objective = str(getattr(goal, "objective", "") or "")
        wanted = list(criteria if criteria is not None else getattr(goal, "success_criteria", []) or [])

        if not wanted:
            return GoalVerification(
                objective=objective,
                evaluations=[],
                verified=False,
                reason=(
                    "el objetivo no tiene criterios de éxito: sin criterios no hay nada "
                    "que verificar y no se puede declarar verificado"
                ),
            )

        evaluations = [self._evaluate_criterion(str(c)) for c in wanted]
        return GoalVerification(
            objective=objective,
            evaluations=evaluations,
            verified=self._is_verified(evaluations),
            reason=self._overall_reason(evaluations),
        )

    # ------------------------------------------------------------------ #
    # Un criterio
    # ------------------------------------------------------------------ #

    def _evaluate_criterion(self, criterion: str) -> CriterionEvaluation:
        claims = self._claims_about(criterion)
        parsed = parse_predicate(criterion)

        if parsed is None:
            return CriterionEvaluation(
                criterion=criterion,
                status=CriterionStatus.INSUFFICIENT_EVIDENCE,
                reason=(
                    "no hay checker para este criterio: hoy solo se pueden comprobar "
                    "predicados observados por herramientas ("
                    + ", ".join(PREDICATES)
                    + ")"
                ),
                evidence=claims,
            )

        name, args = parsed
        if name in ("tests_passing", "tests_failing"):
            return self._check_test_run(criterion, name, claims)
        if name == "file_exists" and args:
            return self._check_file(criterion, name, args[0], expected_exists=True, claims=claims)
        if name == "file_missing" and args:
            return self._check_file(criterion, name, args[0], expected_exists=False, claims=claims)
        if name == "file_size_at_least" and len(args) >= 2:
            try:
                minimum = int(args[1])
            except (TypeError, ValueError):
                return CriterionEvaluation(
                    criterion=criterion,
                    status=CriterionStatus.INSUFFICIENT_EVIDENCE,
                    reason=f"el tamaño mínimo de '{criterion}' no es un número: {args[1]!r}",
                    predicate=name,
                    evidence=claims,
                )
            return self._check_file_size(criterion, name, args[0], minimum, claims=claims)

        return CriterionEvaluation(
            criterion=criterion,
            status=CriterionStatus.INSUFFICIENT_EVIDENCE,
            reason=f"el predicado '{name}' necesita argumentos que el criterio no da",
            predicate=name,
            evidence=claims,
        )

    def _check_test_run(self, criterion, name, claims) -> CriterionEvaluation:
        """Criterios sobre la suite: se apoyan en la última suite REALmente observada.

        Se exige `tests_passed > 0` para dar `tests_passing` por cumplido: una suite que
        no recogió ningún test no demuestra que los tests pasan. Y si el conteo no se pudo
        leer (`counts_parsed=False`), el veredicto es `insufficient_evidence`, no un 0
        disfrazado de hecho.
        """
        run = self._last_test_run()
        if run is None:
            return self._no_observation(criterion, name, "la suite de tests", claims)

        passed = int(run.attributes.get("tests_passed") or 0)
        failed = int(run.attributes.get("tests_failed") or 0)
        if not bool(run.attributes.get("counts_parsed", False)):
            return CriterionEvaluation(
                criterion=criterion,
                status=CriterionStatus.INSUFFICIENT_EVIDENCE,
                reason=(
                    "la suite se ejecutó pero su resumen no se pudo leer: no hay conteos "
                    "fiables para emitir un veredicto"
                ),
                predicate=name,
                evidence=[self._test_evidence(run), *claims],
            )

        evidence = self._test_evidence(run)
        failing = failed > 0
        if name == "tests_failing":
            satisfied = failing
            reason = (
                f"cumplido: se observaron {failed} test/s fallando"
                if satisfied
                else f"no cumplido: la suite pasó {passed} test/s sin fallos"
            )
        else:
            satisfied = (not failing) and passed > 0
            reason = (
                f"cumplido: la suite pasó {passed} test/s sin fallos"
                if satisfied
                else (
                    f"no cumplido: la suite falló ({failed} test/s)"
                    if failing
                    else f"no cumplido: la suite pasó {passed} test/s, insufficient para afirmar que los tests pasan"
                )
            )
        return CriterionEvaluation(
            criterion=criterion,
            status=(
                CriterionStatus.SATISFIED if satisfied else CriterionStatus.NOT_SATISFIED
            ),
            reason=reason,
            predicate=name,
            evidence=[evidence, *claims],
        )

    def _last_test_run(self):
        """La última suite observada por una tool, si la hubo."""
        if self.world is None:
            return None
        query = getattr(self.world, "query", None)
        if not callable(query):
            return None
        try:
            runs = [e for e in query(kind=TEST, limit=20) if str(getattr(e, "source", "")).startswith("tool:")]
        except Exception:  # noqa: BLE001 — un world model raro no puede romper la verificación
            return None
        if not runs:
            return None
        return max(runs, key=lambda e: float(getattr(e, "last_seen", 0.0) or 0.0))

    @staticmethod
    def _test_evidence(run) -> CriterionEvidence:
        attributes = run.attributes or {}
        return CriterionEvidence(
            evidence_id=run.id,
            source=run.source,
            grade=GRADE_EVIDENCE,
            detail=(
                f"una tool ejecutó la suite y observó "
                f"{attributes.get('tests_passed')} passed / {attributes.get('tests_failed')} failed "
                f"(exit {attributes.get('exit_code')}, timeout={attributes.get('timed_out')})"
            ),
            trusted=True,
        )

    def _check_file(self, criterion, name, path, *, expected_exists, claims) -> CriterionEvaluation:
        observed = self._observed_entity(path)
        if observed is None:
            return self._no_observation(criterion, name, path, claims)

        exists = bool(observed.attributes.get("exists"))
        evidence = CriterionEvidence(
            evidence_id=observed.id,
            source=observed.source,
            grade=GRADE_EVIDENCE,
            detail=(
                f"una herramienta observó {path} y existe={exists} "
                f"(fuente: {observed.source}, {observed.observations} observación/es)"
            ),
            trusted=True,
        )
        if exists is expected_exists:
            return CriterionEvaluation(
                criterion=criterion,
                status=CriterionStatus.SATISFIED,
                reason=f"cumplido: {evidence.detail}",
                predicate=name,
                evidence=[evidence, *claims],
            )
        return CriterionEvaluation(
            criterion=criterion,
            status=CriterionStatus.NOT_SATISFIED,
            reason=(
                f"no cumplido: se esperaba que {path} "
                f"{'existente' if expected_exists else 'no existente'} y se observó lo contrario"
            ),
            predicate=name,
            evidence=[evidence, *claims],
        )

    def _check_file_size(self, criterion, name, path, minimum, claims) -> CriterionEvaluation:
        observed = self._observed_entity(path)
        if observed is None:
            return self._no_observation(criterion, name, path, claims)

        size = observed.attributes.get("size")
        if not isinstance(size, (int, float)):
            return CriterionEvaluation(
                criterion=criterion,
                status=CriterionStatus.INSUFFICIENT_EVIDENCE,
                reason=f"se observó {path} pero sin tamaño: no se puede comparar con {minimum}",
                predicate=name,
                evidence=[
                    CriterionEvidence(
                        evidence_id=observed.id,
                        source=observed.source,
                        grade=GRADE_EVIDENCE,
                        detail=f"la observación de {path} no incluye tamaño",
                        trusted=True,
                    ),
                    *claims,
                ],
            )

        evidence = CriterionEvidence(
            evidence_id=observed.id,
            source=observed.source,
            grade=GRADE_EVIDENCE,
            detail=f"una herramienta observó {path} con tamaño {size} (mínimo exigido: {minimum})",
            trusted=True,
        )
        if size >= minimum:
            return CriterionEvaluation(
                criterion=criterion,
                status=CriterionStatus.SATISFIED,
                reason=f"cumplido: {evidence.detail}",
                predicate=name,
                evidence=[evidence, *claims],
            )
        return CriterionEvaluation(
            criterion=criterion,
            status=CriterionStatus.NOT_SATISFIED,
            reason=f"no cumplido: {path} pesa {size} y se exigían al menos {minimum}",
            predicate=name,
            evidence=[evidence, *claims],
        )

    def _no_observation(self, criterion, name, path, claims) -> CriterionEvaluation:
        reason = f"nadie ha observado '{path}' todavía: no hay evidencia para emitir un veredicto"
        if claims:
            reason += (
                "; hay claims relacionados pero son del modelo y un claim no es evidencia"
            )
        return CriterionEvaluation(
            criterion=criterion,
            status=CriterionStatus.INSUFFICIENT_EVIDENCE,
            reason=reason,
            predicate=name,
            evidence=list(claims),
        )

    # ------------------------------------------------------------------ #
    # Fuentes de evidencia
    # ------------------------------------------------------------------ #

    def _observed_entity(self, path: str):
        """La entidad del mundo que una HERRAMIENTA observó, no la declarada.

        `source` debe venir de una tool: lo declarado por el registro de capabilities no
        cuenta como evidencia de nada, y por eso no se acepta aquí.
        """
        if self.world is None:
            return None
        getter = getattr(self.world, "known_path", None)
        entity = getter(path) if callable(getter) else None
        if entity is None:
            return None
        if not str(getattr(entity, "source", "")).startswith("tool:"):
            return None
        return entity

    def _claims_about(self, criterion: str) -> list[CriterionEvidence]:
        """Claims que mencionan el criterio, siempre marcados como no fiables.

        Se incluyen para que la evaluación sea auditable y para dejar constancia
        explícita de que un claim del modelo se pesó y no se aceptó como prueba.
        """
        if self.evidence is None:
            return []
        # `EvidenceStore` guarda en `claims`; otras fuentes pueden exponer `items`.
        claims = getattr(self.evidence, "claims", None)
        if not isinstance(claims, list):
            claims = list(getattr(self.evidence, "items", None) or [])
        needle = (criterion or "").strip().lower()
        found: list[CriterionEvidence] = []
        for claim in claims:
            text = str(getattr(claim, "text", "") or "").lower()
            if not text:
                continue
            if needle and needle not in text and not _shares_terms(needle, text):
                continue
            kind = getattr(getattr(claim, "kind", None), "value", "inference")
            grade = {
                "fact": GRADE_FACT,
                "evidence": GRADE_EVIDENCE,
                "assumption": GRADE_ASSUMPTION,
                "uncertainty": GRADE_UNCERTAINTY,
            }.get(str(kind), GRADE_INFERENCE)
            found.append(
                CriterionEvidence(
                    evidence_id=str(getattr(claim, "id", "") or ""),
                    source=f"claim:{getattr(claim, 'source', 'model')}",
                    grade=grade,
                    detail=f"claim del modelo sin verificación independiente: {getattr(claim, 'text', '')}",
                    trusted=False,
                )
            )
        return found

    # ------------------------------------------------------------------ #
    # Veredicto global
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_verified(evaluations: list[CriterionEvaluation]) -> bool:
        if not evaluations:
            return False
        for evaluation in evaluations:
            if evaluation.status is not CriterionStatus.SATISFIED:
                return False
            if not any(e.trusted for e in evaluation.evidence):
                return False
        return True

    @staticmethod
    def _overall_reason(evaluations: list[CriterionEvaluation]) -> str:
        counts = {
            status.value: sum(1 for e in evaluations if e.status is status)
            for status in CriterionStatus
        }
        if counts[CriterionStatus.NOT_SATISFIED.value]:
            return (
                f"objetivo NO verificado: {counts[CriterionStatus.NOT_SATISFIED.value]} criterio/s "
                "no cumplido/s"
            )
        if counts[CriterionStatus.INSUFFICIENT_EVIDENCE.value]:
            return (
                "objetivo NO verificado: "
                f"{counts[CriterionStatus.INSUFFICIENT_EVIDENCE.value]} criterio/s sin evidencia "
                "suficiente"
            )
        return f"objetivo verificado: {counts[CriterionStatus.SATISFIED.value]} criterio/s con evidencia"


def _shares_terms(needle: str, text: str) -> bool:
    """Coincidencia laxa por términos, solo para ADJUNTAR claims al criterio.

    No decide ningún veredicto: como mucho deja constancia de que el modelo dijo algo
    sobre el asunto. Un claim jamás satisface un criterio por mucho que coincide.
    """
    if len(needle) < 12:
        return False
    stop = {"el", "la", "los", "las", "de", "del", "un", "una", "que", "con", "para", "por"}
    terms = {t for t in needle.split() if len(t) > 3 and t not in stop}
    if not terms:
        return False
    other = {t for t in text.split() if len(t) > 3}
    return len(terms & other) >= max(2, len(terms) // 2)


__all__ = [
    "CriterionEvaluation",
    "CriterionEvidence",
    "CriterionStatus",
    "GoalVerification",
    "GoalVerifier",
    "PREDICATES",
    "parse_predicate",
]
