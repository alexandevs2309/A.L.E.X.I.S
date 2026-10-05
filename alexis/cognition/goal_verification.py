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

import re

from alexis.contracts import Mission
from alexis.world.model import TEST

#: Predicados que una observación de herramienta puede comprobar de forma estructurada.
#: Formato en el texto del criterio: ``predicado:argumento[:extra]``.
PREDICATES = (
    "content_observed",
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


def is_meaningful_content(content) -> bool:
    """¿Este contenido cuenta como "contenido observado"?

    Una sola definición, compartida por las TRES capas que juzgan el mismo hecho
    (`GoalVerifier`, el contrato de paso y la observación del mundo). Que coincidan no es
    casualidad: si una acepta un fichero vacío y la otra no, el sistema se contradice
    consigo mismo y el resultado depende de por dónde se mire.

    No es contenido útil el `None`, ni una cadena vacía, ni una que sólo tenga espacio en
    blanco: leer un fichero vacío es una lectura real, pero no hay nada que analizar ni que
    reportar, y eso es exactamente lo que un objetivo semántico pedía.

    El `WorldModel` calcula esta misma condición al observar y la guarda como
    `content_meaningful`, para que el verificador no tenga que re-interpretar la cadena.
    """
    if not isinstance(content, str):
        return False
    return bool(content.strip())


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
    """Resultado de evaluar el objetivo de una misión criterio por criterio.

    `subject` es la IDENTIDAD de lo que se verificó: qué misión, con qué objetivo y con
    qué criterios. No es metadata decorativa: es lo que impide que la verificación de una
    misión satisfaga a otra (§11). Sin ese vínculo, copiar el objeto de la fila de la
    misión A a la de la B bastaba para declarar `COMPLETED` algo que nunca se ejecutó.
    """

    objective: str
    evaluations: list[CriterionEvaluation] = field(default_factory=list)
    verified: bool = False
    reason: str = ""
    #: Identidad verificada: `mission_id`, `objective` y `criteria` de esa misión.
    subject: dict[str, Any] = field(default_factory=dict)
    #: §11: True cuando esta verificación viene del ALMACENAMIENTO y aún no se ha
    #: contrastado con el mundo en la ejecución actual. Una fila guardada no autoriza
    #: por sí sola: se revalida. No se persiste (es estado de esta ejecución) precisamente
    #: porque su valor es justamente "esto aún no está comprobado aquí".
    pending_revalidation: bool = False

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
            "subject": dict(self.subject),
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
            subject=dict(raw.get("subject") or {}),
            reason=str(raw.get("reason") or ""),
        )


#: CORE-09. Verbos que, en un criterio sobre un fichero, expresan EXISTIR y no "fue escrito".
#: La distinción importa: "existe" se puede comprobar contra lo observado; "fue escrito" es una
#: afirmación sobre el PASADO que la observación de un `fs.stat` no demuestra, y por eso no se
#: acepta como equivalente. Un criterio que sólo dice "existe" sí se comprueba.
_EXISTS_VERBS = frozenset({"existe", "existen", "existente", "esta", "está", "hay", "creado", "creada"})

#: CORE-09. Una ruta con extensión dentro de un criterio en lenguaje natural.
_NAMED_PATH = re.compile(r"[\w\-./]+\.[A-Za-z0-9]{1,6}")


def infer_file_predicate(criterion: str) -> tuple[str, list[str]] | None:
    """ predicado implícito de un criterio en lenguaje natural, o `None`.

    CORE-09. `parse_predicate()` sólo acepta la forma explícita (`file_exists:notas.txt`), y un
    objetivo escrito en lenguaje natural —"el archivo informe.txt existe en el workspace"— no la
    tiene. Medido en la prueba real de CORE-08: con evidencia real completa (`fs.write` con
    `exists=true` observado por `verification.filesystem`), el criterio quedaba
    `insufficient_evidence` y la misión nunca cerraba. El verificador era correcto: no le
    habían dado nada que comprobar.

    Esto NO relaja la verificación: sólo traduce "hay una ruta y se afirma que existe" al
    predicado que ya existía. Sigue exigiendo observación real de una herramienta
    (`source` empieza por `tool:`), y sigue rechazando si el fichero no existe, si la evidencia
    es de otro path, o si no hay evidencia.

    Deliberadamente NO se infiere nada más:
    - si el criterio menciona "escrito"/"creado"/"modificado", NO se traduce a `file_exists`
      (ver `_EXISTS_VERBS`), porque afirmar que algo FUE escrito exige evidencia de escritura,
      no sólo que ahora exista;
    - si no hay ruta reconocible, se devuelve `None` y el criterio queda
      `insufficient_evidence`, como antes.
    """
    text = (criterion or "").strip()
    if not text:
        return None
    lowered = text.lower()
    # Un predicado explícito gana siempre: si está escrito, no hay nada que inferir.
    if any(f"{name}:" in lowered for name in PREDICATES):
        return None
    # Exige un verbo de existencia. Sin él, "no existe" o "falta informe.txt" no se traducirían.
    if not any(re.search(rf"\b{verb}\b", lowered) for verb in _EXISTS_VERBS):
        return None
    match = _NAMED_PATH.search(text)
    if not match:
        return None
    path = match.group(0).strip().strip("\"'.,;:()[]")
    if not path or path in (".", "..") or path.startswith("/"):
        # Una ruta absoluta no se infiere: el objetivo la nombraría con su predicado.
        return None
    # `no existe`/`falta` significan lo contrario: si se infiriera `file_exists` se invertiría
    # el sentido del criterio.
    if re.search(r"\bno (?:existe|existen|hay|está|esta)\b", lowered) or re.search(r"\bfalta\b", lowered):
        return None
    return "file_exists", [path]


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


def verification_subject(mission, criteria) -> dict[str, Any]:
    """La identidad de lo verificado: misión, objetivo y criterios.

    Es lo que hace que una verificación NO sea reutilizable entre misiones. Se sellan los
    tres, y no sólo la misión: dos misiones distintas pueden pedir exactamente lo mismo,
    y aun así la verificación de una no prueba nada sobre la otra (se ejecutó en otro
    momento, sobre otro mundo, con otra evidencia).
    """
    goal = getattr(mission, "goal", None)
    return {
        "mission_id": str(getattr(mission, "id", "") or ""),
        "objective": str(getattr(goal, "objective", "") or ""),
        "criteria": [str(c) for c in (criteria or [])],
    }


def subject_matches(verification, mission) -> bool:
    """¿Esta verificación se hizo PARA esta misión, con estos criterios?"""
    subject = getattr(verification, "subject", None)
    if not isinstance(subject, dict) or not subject:
        return False
    expected = verification_subject(
        mission, getattr(getattr(mission, "goal", None), "success_criteria", []) or []
    )
    if str(subject.get("mission_id") or "") != expected["mission_id"]:
        return False
    if str(subject.get("objective") or "") != expected["objective"]:
        return False
    return [str(c) for c in (subject.get("criteria") or [])] == expected["criteria"]


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
                subject=verification_subject(mission, wanted),
            )

        evaluations = [self._evaluate_criterion(str(c)) for c in wanted]
        return GoalVerification(
            objective=objective,
            evaluations=evaluations,
            verified=self._is_verified(evaluations),
            reason=self._overall_reason(evaluations),
            subject=verification_subject(mission, wanted),
        )

    # ------------------------------------------------------------------ #
    # Un criterio
    # ------------------------------------------------------------------ #

    def _evaluate_criterion(self, criterion: str) -> CriterionEvaluation:
        claims = self._claims_about(criterion)
        # CORE-09: primero el predicado explícito; si no hay, se intenta el implícito. El
        # implícito devuelve `None` cuando no es inequívoco, y entonces el criterio queda
        # `insufficient_evidence` exactamente como antes: no se reemplaza un caso por otro.
        parsed = parse_predicate(criterion) or infer_file_predicate(criterion)
        inferred = parsed is not None and parse_predicate(criterion) is None
        if inferred:
            claims = list(claims) + [
                CriterionEvidence(
                    evidence_id="",
                    source="criterio",
                    grade=GRADE_EVIDENCE,
                    detail=(
                        "el criterio nombra una ruta y afirma que existe pero no declara "
                        f"predicado explícito; se comprobó como file_exists:{parsed[1][0]}"
                    ),
                    trusted=True,
                )
            ]

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
        if name == "content_observed" and args:
            return self._check_content_observed(criterion, args[0], claims=claims)

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
        # CORE-09: existencia afirmada por una capability que MUTA no es prueba de que el
        # fichero exista. `fs.write` con `ok: true` dice "escribí", que es compatible con haber
        # escrito en otro sitio o con que otro lo borrara después. Sólo una capability que
        # OBSERVA (`fs.stat`, `fs.read`, `verification.filesystem`) miró el fichero de verdad.
        # Sin esto, un criterio implícito se daba por cumplido con la evidencia del propio
        # paso que lo cumpliría:asking- PlanValidator一样, un circuito cerrado.
        if exists and expected_exists and not self._is_independent_observation(observed):
            return CriterionEvaluation(
                criterion=criterion,
                status=CriterionStatus.INSUFFICIENT_EVIDENCE,
                reason=(
                    f"la existencia de {path} sólo la afirmó {observed.source}, que escribe en "
                    f"lugar de mirar el fichero: hace falta una observación independiente "
                    f"(fs.stat, fs.read o verification.filesystem) para dar el criterio por cumplido"
                ),
                predicate=name,
                evidence=[CriterionEvidence(
                    evidence_id=observed.id,
                    source=observed.source,
                    grade=GRADE_EVIDENCE,
                    detail=f"afirmación de escritura, no de existencia observada: {observed.source}",
                    trusted=True,
                ), *claims],
            )
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

    def _check_content_observed(self, criterion, path, claims) -> CriterionEvaluation:
        """P0 §11 — "el contenido de RUTA fue observado", no "RUTA existe".

        Es la diferencia que un objetivo semántico necesita: "analiza notas.txt y dime qué
        contiene" NO se cumple porque el fichero esté en disco. Se cumple porque una
        herramienta le devolvió a ALEXIS su contenido.

        Tres exigencias, todas verificables contra lo observado:
          1. que alguien observara la ruta con una tool;
          2. que esa observación trajera contenido de verdad (`content_observed`), no
             sólo metadatos — un `fs.stat` no lee el fichero;
          3. que la capability sea observadora. Una capability que MUTA no puede atestiguar
             que leyó: sería auto-atestación, el circuito cerrado que §5.3 veta.
        """
        observed = self._observed_entity(path)
        if observed is None:
            return self._no_observation(criterion, "content_observed", path, claims)

        if not bool(observed.attributes.get("content_observed", False)):
            length = observed.attributes.get("content_length")
            return CriterionEvaluation(
                criterion=criterion,
                status=CriterionStatus.INSUFFICIENT_EVIDENCE,
                reason=(
                    f"se observó {path} pero sin su contenido: la herramienta afirmó "
                    f"existencia/metadatos, no el texto. Estar en disco no es haberlo leído"
                    + (f" (la observación declara content_length={length})" if length else "")
                ),
                predicate="content_observed",
                evidence=[CriterionEvidence(
                    evidence_id=observed.id,
                    source=observed.source,
                    grade=GRADE_EVIDENCE,
                    detail=(
                        f"observación sin contenido de {path} (fuente: {observed.source}): "
                        "existe no es leer"
                    ),
                    trusted=True,
                ), *claims],
            )

        # §11 — un fichero VACIO no es "contenido observado". Se registró que la tool
        # devolvió la cadena, y la cadena no tenía nada: hay que decirlo con el mismo
        # criterio que usa el contrato de paso, o las dos capas se contradirían (una
        # aceptando un vacío y la otra no).
        if not observed.attributes.get("content_meaningful", False):
            return CriterionEvaluation(
                criterion=criterion,
                status=CriterionStatus.INSUFFICIENT_EVIDENCE,
                reason=(
                    f"se leyó {path} pero su contenido está vacío: no hay nada que "
                    f"analizar ni que reportar. Estar en disco no es haberlo leído, y "
                    f"un fichero vacío tampoco es haberlo leído"
                ),
                predicate="content_observed",
                evidence=[CriterionEvidence(
                    evidence_id=observed.id,
                    source=observed.source,
                    grade=GRADE_EVIDENCE,
                    detail=(
                        f"la herramienta leyó {path} y devolvió una cadena vacía "
                        f"(fuente: {observed.source})"
                    ),
                    trusted=True,
                ), *claims],
            )

        if not self._is_independent_observation(observed):
            return CriterionEvaluation(
                criterion=criterion,
                status=CriterionStatus.INSUFFICIENT_EVIDENCE,
                reason=(
                    f"la lectura de {path} la afirmó {observed.source}, que cambia el fichero "
                    "en lugar de mirarlo: una capability que muta no puede atestiguar que leyó"
                ),
                predicate="content_observed",
                evidence=[CriterionEvidence(
                    evidence_id=observed.id,
                    source=observed.source,
                    grade=GRADE_EVIDENCE,
                    detail=f"afirmación de una capability que muta, no de lectura: {observed.source}",
                    trusted=True,
                ), *claims],
            )

        length = observed.attributes.get("content_length")
        evidence = CriterionEvidence(
            evidence_id=observed.id,
            source=observed.source,
            grade=GRADE_EVIDENCE,
            detail=(
                f"una herramienta leyó el contenido de {path} y devolvió "
                f"{length if length is not None else '?'} caracter(es) "
                f"(fuente: {observed.source}, {observed.observations} observación/es)"
            ),
            trusted=True,
        )
        return CriterionEvaluation(
            criterion=criterion,
            status=CriterionStatus.SATISFIED,
            reason=f"cumplido: {evidence.detail}",
            predicate="content_observed",
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

    def _is_independent_observation(self, observed) -> bool:
        """¿La entidad la produjo una capability que MIRA el fichero, o una que lo cambia?

        No es una lista escrita a mano: se lee del CapabilityCatalog, que ya declara
        `side_effects` por capability. Si el catálogo no está disponible se acepta el hecho,
        porque entonces no hay forma de distinguir y bloquear sería inventar una duda.
        """
        source = str(getattr(observed, "source", "") or "")
        capability = source.split("tool:", 1)[1] if "tool:" in source else source
        try:
            from alexis.capabilities import build_catalog

            spec = build_catalog().get(capability)
        except Exception:  # noqa: BLE001 — sin catálogo no se bloquea por falta de información
            return True
        return not bool(getattr(spec, "side_effects", False))

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
