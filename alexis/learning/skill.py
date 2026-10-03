"""CORE-11 — Skills: de "almacenar texto" a estrategia reutilizable.

Una skill no es una lección con otro nombre. La diferencia es que una skill tiene
`procedure`, `success_criteria` y `verification`: se puede EJECUTAR y comprobar si funcionó.
Y por eso necesita un ciclo de vida con estados, porque "la guardé" no es lo mismo que "la
validé", y la diferencia es la única cosa que separa una estrategia reutilizable de un texto
con nombre de fichero.

Las cinco decisiones de diseño que importan aquí:

1. **Nada se valida solo.** `SkillValidator` es determinista y comprueba contra el catálogo y la
   policy REALES. Un modelo puede proponer una skill; no puede validarla.

2. **Validar no es autorizar.** Una skill validada sigue siendo una ESTRATEGIA: cuando se usa,
   pasa por Policy y Gate como cualquier paso. No hay atajo, y `SkillValidator` comprueba
   explícitamente que la skill no pide capabilities fuera del envelope.

3. **Las versiones son inmutables.** Publicar una mejora es crear `v2`. Sobrescribir `v1` en
   silencio haría que una ejecución antigua de `v1` no se pudiera reconstruir, que es
   justamente lo que un registro de experiencia sirve para.

4. **Discovery devuelve incertidumbre.** `UNCERTAIN` no usa la skill. Se prefiere no
   reutilizar una estrategia dudosa antes que aplicar la que Perhaps no toca.

5. **Una sola experiencia no degrada una skill.** El rendimiento se acumula. Con una sola
   ejecución no se puede distinguir "esta skill es mala" de "tuve mala suerte", y degradar por
   ruido hace que el sistema aprenda a desconfiar de todo.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from alexis.learning.lesson import Lesson, Outcome

#: La misma escala de riesgo que usa el `PlanValidator`. No se reimplementa: dos escalas
#: distintas harían que una skill y un plan desaparecieran el riesgo de forma diferente.
from alexis.cognition.planner_model import _RISK_ORDER

#: ids válidos de skill. Sin guion, porque el Core ya rechaza ids con guion en los pasos del
#: plan (`_SAFE_ID`) y una skill se acaba convirtiendo en pasos.
_SKILL_NAME = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


class SkillStatus(str, Enum):
    PROPOSED = "proposed"
    UNDER_VALIDATION = "under_validation"
    VALIDATED = "validated"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class SkillMatch(str, Enum):
    """Lo que la búsqueda dice. `UNCERTAIN` es una respuesta de verdad, no un fallo."""

    MATCH = "match"
    NO_MATCH = "no_match"
    UNCERTAIN = "uncertain"


@dataclass
class SkillCandidate:
    """Una estrategia propuesta a partir de lecciones. Todavía NO es una skill."""

    name: str
    purpose: str
    source_lessons: list[str] = field(default_factory=list)
    source_experiences: list[str] = field(default_factory=list)
    prerequisites: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    #: Pasos concretos y ejecutables. Sin esto no hay skill, hay una descripción.
    procedure: list[dict[str, Any]] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    risk: str = "low"
    expected_effects: list[str] = field(default_factory=list)
    failure_modes: list[str] = field(default_factory=list)
    applicability: str = ""
    contraindications: list[str] = field(default_factory=list)
    proposed_version: int = 1
    status: str = SkillStatus.PROPOSED.value
    provenance: dict[str, Any] = field(default_factory=dict)
    candidate_id: str = ""

    def __post_init__(self):
        if not self.candidate_id:
            self.candidate_id = f"cand-{uuid.uuid4().hex[:12]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "name": self.name,
            "purpose": self.purpose,
            "source_lessons": list(self.source_lessons),
            "source_experiences": list(self.source_experiences),
            "prerequisites": list(self.prerequisites),
            "required_capabilities": list(self.required_capabilities),
            "procedure": [dict(p) for p in self.procedure],
            "success_criteria": list(self.success_criteria),
            "verification": list(self.verification),
            "risk": self.risk,
            "expected_effects": list(self.expected_effects),
            "failure_modes": list(self.failure_modes),
            "applicability": self.applicability,
            "contraindications": list(self.contraindications),
            "proposed_version": self.proposed_version,
            "status": self.status,
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "SkillCandidate | None":
        if not raw or not raw.get("name"):
            return None
        return cls(
            candidate_id=str(raw.get("candidate_id") or ""),
            name=str(raw["name"]),
            purpose=str(raw.get("purpose") or ""),
            source_lessons=list(raw.get("source_lessons") or []),
            source_experiences=list(raw.get("source_experiences") or []),
            prerequisites=list(raw.get("prerequisites") or []),
            required_capabilities=list(raw.get("required_capabilities") or []),
            procedure=[dict(p) for p in (raw.get("procedure") or [])],
            success_criteria=list(raw.get("success_criteria") or []),
            verification=list(raw.get("verification") or []),
            risk=str(raw.get("risk") or "low"),
            expected_effects=list(raw.get("expected_effects") or []),
            failure_modes=list(raw.get("failure_modes") or []),
            applicability=str(raw.get("applicability") or ""),
            contraindications=list(raw.get("contraindications") or []),
            proposed_version=int(raw.get("proposed_version") or 1),
            status=str(raw.get("status") or SkillStatus.PROPOSED.value),
            provenance=dict(raw.get("provenance") or {}),
        )


@dataclass
class SkillVersion:
    """Una skill validada y publicada. Inmutable."""

    skill_id: str
    version: int
    name: str
    procedure: list[dict[str, Any]] = field(default_factory=list)
    prerequisites: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    risk: str = "low"
    applicability: str = ""
    contraindications: list[str] = field(default_factory=list)
    #: Resultado de la validación que la autorizó. Sin esto, una skill no tiene por qué servir.
    validation: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    status: str = SkillStatus.VALIDATED.value
    created_at: float = 0.0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()

    @property
    def version_key(self) -> str:
        return f"{self.skill_id}:v{self.version}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "name": self.name,
            "procedure": [dict(p) for p in self.procedure],
            "prerequisites": list(self.prerequisites),
            "capabilities": list(self.capabilities),
            "success_criteria": list(self.success_criteria),
            "verification": list(self.verification),
            "risk": self.risk,
            "applicability": self.applicability,
            "contraindications": list(self.contraindications),
            "validation": dict(self.validation),
            "provenance": dict(self.provenance),
            "status": self.status,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "SkillVersion | None":
        if not raw or not raw.get("skill_id"):
            return None
        return cls(
            skill_id=str(raw["skill_id"]),
            version=int(raw.get("version") or 1),
            name=str(raw.get("name") or ""),
            procedure=[dict(p) for p in (raw.get("procedure") or [])],
            prerequisites=list(raw.get("prerequisites") or []),
            capabilities=list(raw.get("capabilities") or []),
            success_criteria=list(raw.get("success_criteria") or []),
            verification=list(raw.get("verification") or []),
            risk=str(raw.get("risk") or "low"),
            applicability=str(raw.get("applicability") or ""),
            contraindications=list(raw.get("contraindications") or []),
            validation=dict(raw.get("validation") or {}),
            provenance=dict(raw.get("provenance") or {}),
            status=str(raw.get("status") or SkillStatus.VALIDATED.value),
            created_at=float(raw.get("created_at") or 0.0),
        )


@dataclass
class SkillPerformance:
    """Una ejecución de una skill. Se acumula; una sola no degrada nada."""

    skill_id: str
    version: int
    mission_id: str
    outcome: str
    verified: bool
    duration_s: float = 0.0
    failures: int = 0
    replans: int = 0
    recovered: bool = False
    confidence: float = 0.0
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id, "version": self.version, "mission_id": self.mission_id,
            "outcome": self.outcome, "verified": self.verified, "duration_s": self.duration_s,
            "failures": self.failures, "replans": self.replans, "recovered": self.recovered,
            "confidence": self.confidence, "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "SkillPerformance | None":
        if not raw or not raw.get("mission_id"):
            return None
        return cls(
            skill_id=str(raw.get("skill_id") or ""), version=int(raw.get("version") or 1),
            mission_id=str(raw["mission_id"]), outcome=str(raw.get("outcome") or ""),
            verified=bool(raw.get("verified")), duration_s=float(raw.get("duration_s") or 0.0),
            failures=int(raw.get("failures") or 0), replans=int(raw.get("replans") or 0),
            recovered=bool(raw.get("recovered")), confidence=float(raw.get("confidence") or 0.0),
            timestamp=float(raw.get("timestamp") or 0.0),
        )


class SkillValidator:
    """La validación determinista. Un modelo propone; esto decide.

    Se consulta el catálogo REAL y la policy REAL. No hay forma de que una skill se valide
    pidiendo capabilities que ALEXIS no tiene, porque eso se comprueba contra el catálogo y no
    contra lo que la skill dice de sí misma.
    """

    def __init__(self, catalog=None, *, policy=None):
        self.catalog = catalog
        self.policy = policy

    def validate(self, candidate: SkillCandidate, *, envelope=None) -> tuple[bool, list[str]]:
        """`(válida, razones)`. Vacío de razones = validada.

        Cada razón dice QUÉ falla, para que el rechazo sea accionable y no un "no".
        """
        reasons: list[str] = []

        if not _SKILL_NAME.match(candidate.name or ""):
            reasons.append(f"nombre de skill inválido: {candidate.name!r} (minúsculas, dígitos y _)")
        if not candidate.purpose:
            reasons.append("la skill no dice para qué sirve")
        if not candidate.procedure:
            # Sin procedure no hay skill: hay una descripción de una skill.
            reasons.append("la skill no tiene procedure: no se puede ejecutar")
        if not candidate.success_criteria:
            reasons.append("la skill no declara success_criteria: no se puede comprobar si funcionó")
        if not candidate.verification:
            reasons.append("la skill no declara cómo se verifica")
        if not candidate.contraindications:
            # No es un capricho de formato: una skill que siempre aplica está mal, porque
            # entonces no aprendió nada del mundo sino del ejecutor.
            reasons.append(
                "la skill no declara contraindicaciones: una estrategia que siempre aplica "
                "no es una lección, es un comportamiento por defecto"
            )
        if candidate.risk not in ("low", "medium", "high", "critical"):
            reasons.append(f"riesgo inválido: {candidate.risk!r}")

        # Capabilities: contra el catálogo real, no contra lo que la skill afirme.
        for capability in candidate.required_capabilities:
            if self.catalog is not None and not self.catalog.has(capability):
                reasons.append(f"capability que no existe en el catálogo: {capability}")
                continue
            if envelope is not None:
                declared = set(getattr(envelope, "capabilities", []) or [])
                if declared and capability not in declared:
                    reasons.append(
                        f"la skill pide {capability}, fuera del envelope de esta misión"
                    )

        # La skill no puede elevar su propio riesgo por encima del de sus capabilities.
        if self.catalog is not None:
            declared_risk = _RISK_ORDER.get(candidate.risk, 0)
            for step in candidate.procedure:
                capability = str(step.get("capability") or "")
                if not capability or not self.catalog.has(capability):
                    continue
                spec = self.catalog.get(capability)
                if declared_risk < _RISK_ORDER.get(spec.default_risk, 0):
                    reasons.append(
                        f"riesgo de la skill ({candidate.risk}) menor que el de {capability} "
                        f"({spec.default_risk}): una skill no puede declarar menos riesgo "
                        f"del que la capability ya tiene"
                    )

        # Y no puede pedir approval de menos: si algún paso es delicado, lo declara.
        for step in candidate.procedure:
            capability = str(step.get("capability") or "")
            if self.catalog is not None and capability and self.catalog.has(capability):
                spec = self.catalog.get(capability)
                if _RISK_ORDER.get(spec.default_risk, 0) >= _RISK_ORDER.get("high", 2) and not step.get(
                    "requires_approval"
                ):
                    reasons.append(
                        f"el paso usa {capability} (riesgo {spec.default_risk}) sin pedir "
                        f"aprobación: una skill no puede quitarse una protección"
                    )

        return (not reasons), reasons

    def promote(
        self,
        candidate: SkillCandidate,
        *,
        envelope=None,
        existing: list[SkillVersion] | None = None,
        skill_id: str | None = None,
    ) -> SkillVersion | None:
        """SkillCandidate → SkillVersion, o `None` con las razones en el candidato.

        La versión se calcula sobre las versiones existentes: si ya hay una `v1`, esto es
        `v2`. Nunca sobrescribe. Una `v1` que cambió en silencio hace imposible reconstruir
        por qué una ejecución antigua salió como salió.
        """
        ok, reasons = self.validate(candidate, envelope=envelope)
        if not ok:
            candidate.status = SkillStatus.REJECTED.value
            candidate.provenance["rejection_reasons"] = reasons
            return None

        versions = list(existing or [])
        same = [v for v in versions if v.name == candidate.name]
        next_version = max((v.version for v in same), default=0) + 1
        resolved_id = skill_id or f"skill-{hashlib.sha256(candidate.name.encode()).hexdigest()[:10]}"

        # La versión anterior se marca como sustituida, pero NO se borra.
        for version in same:
            if version.status == SkillStatus.VALIDATED.value:
                version.status = SkillStatus.SUPERSEDED.value

        candidate.status = SkillStatus.VALIDATED.value
        return SkillVersion(
            skill_id=resolved_id,
            version=next_version,
            name=candidate.name,
            procedure=[dict(p) for p in candidate.procedure],
            prerequisites=list(candidate.prerequisites),
            capabilities=list(candidate.required_capabilities),
            success_criteria=list(candidate.success_criteria),
            verification=list(candidate.verification),
            risk=candidate.risk,
            applicability=candidate.applicability,
            contraindications=list(candidate.contraindications),
            validation={"ok": True, "checked_at": time.time()},
            provenance={
                "candidate_id": candidate.candidate_id,
                "source_lessons": list(candidate.source_lessons),
                "source_experiences": list(candidate.source_experiences),
                **dict(candidate.provenance),
            },
            status=SkillStatus.VALIDATED.value,
        )


class SkillRegistry:
    """Búsqueda determinista de skills aplicables. Sin embeddings: con lo que hay.

    Se busca por términos del objetivo, del propósito y de las capabilities, y se devuelve
    `UNCERTAIN` cuando no está claro. `UNCERTAIN` no usa la skill: es preferible no
    reutilizar una estrategia dudosa a aplicar la que Perhaps no toca.
    """

    #: Palabras que no deben decidir: aparecen en casi todos los objetivos y no aportan nada.
    _STOPWORDS = frozenset({
        "crea", "crear", "archivo", "file", "con", "para", "the", "and", "de", "del", "la",
        "el", "un", "una", "que", "por", "en", "y", "a", "un", "que", "su", "sus",
    })

    def __init__(self):
        self.versions: list[SkillVersion] = []
        self.performance: list[SkillPerformance] = []

    def add(self, version: SkillVersion) -> None:
        self.versions.append(version)

    def all(self) -> list[SkillVersion]:
        return list(self.versions)

    @staticmethod
    def _terms(text: str) -> set[str]:
        words = re.findall(r"[a-z0-9_]+", (text or "").lower())
        return {w for w in words if len(w) > 2 and w not in SkillRegistry._STOPWORDS}

    def match(self, objective: str, *, capabilities: list[str] | None = None) -> tuple[
        SkillMatch, SkillVersion | None, list[str]
    ]:
        """`(veredicto, skill, razones)`. Determinista y explicable.

        Tres reglas, en orden: la applicability escrita manda, luego las capabilities que la
        misión puede ofrecer, y sólo al final el parecido textual. Se empieza por lo concreto
        porque el parecido textual es el que más falsos positivos produce.
        """
        objective_terms = self._terms(objective)
        required = set(capabilities or [])

        scored: list[tuple[float, SkillVersion, str]] = []
        for version in self.versions:
            if version.status not in (SkillStatus.VALIDATED.value, SkillStatus.SUPERSEDED.value):
                continue
            reasons: list[str] = []

            # 1. La applicability escrita es la author's intención explícita.
            if version.applicability:
                app_terms = self._terms(version.applicability)
                if app_terms and not (app_terms & objective_terms):
                    continue
                if app_terms and (app_terms & objective_terms):
                    reasons.append("applicability coincide")
            else:
                reasons.append("sin applicability escrita")

            # 2. Capabilities: una skill que necesita algo que la misión no tiene no aplica.
            missing = [c for c in version.capabilities if c not in required]
            if required and missing:
                reasons.append(f"le faltan capabilities para esta misión: {', '.join(missing)}")
                continue

            # 3. Parecido textual. Es la señal más débil, y por eso va la última.
            #    Se pesa el applicability ESCRITO por la autora más que el nombre: los
            #    términos de applicability son los que describen CUÁNDO aplica, y el nombre
            #    es sólo un identificador. Sin esa ponderación, una skill escrita con
            #    precisión nunca alcanzaría el umbral por llevar nombres de capabilities en
            #    el texto que no aparecen en el objetivo.
            skill_terms = self._terms(version.applicability or "")
            name_terms = self._terms(version.name)
            overlap = len(objective_terms & skill_terms)
            name_overlap = len(objective_terms & name_terms)
            if not skill_terms:
                # Sin applicability escrita sólo se puede comparar por nombre, y por nombre no
                # se decide: se necesita el nombre para el resto del sistema, no para aplicar.
                continue
            if overlap == 0 and name_overlap == 0:
                continue
            score = (overlap / len(skill_terms)) * 0.8 + (name_overlap / max(len(name_terms), 1)) * 0.2
            reasons.append(f"términos en común: {overlap}/{len(skill_terms)} en applicability")
            scored.append((score, version, "; ".join(reasons)))

        if not scored:
            return SkillMatch.NO_MATCH, None, ["ninguna skill coincide con el objetivo"]

        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best, best_reason = scored[0]

        if best_score >= 0.35:
            return SkillMatch.MATCH, best, [best_reason]
        # Zonas intermedias: no se usa. Aplicar una skill "más o menos" es peor que no
        # aplicar ninguna, porque le añade pasos a un plan que funcionaba.
        return SkillMatch.UNCERTAIN, best, [f"coincidencia débil ({best_score:.0%}): {best_reason}"]

    def record_performance(self, record: SkillPerformance) -> None:
        self.performance.append(record)

    def performance_for(self, skill_id: str, version: int | None = None) -> list[SkillPerformance]:
        return [
            p for p in self.performance
            if p.skill_id == skill_id and (version is None or p.version == version)
        ]

    def health(self, skill_id: str, version: int) -> dict[str, Any]:
        """Rendimiento acumulado. Con menos de 3 ejecuciones no se juzga nada.

        Degradar por una sola ejecución es ruido, y un sistema que degrada por ruido aprende a
        desconfiar de todo. Por eso el mínimo son 3.
        """
        records = self.performance_for(skill_id, version)
        total = len(records)
        if total < 3:
            return {"samples": total, "sufficient": False, "verified_rate": None}
        verified = sum(1 for r in records if r.verified)
        return {
            "samples": total,
            "sufficient": True,
            "verified_rate": verified / total,
            "mean_failures": sum(r.failures for r in records) / total,
            "mean_replans": sum(r.replans for r in records) / total,
        }


def _skill_name_from_scope(scope: str, *, fallback: str = "derived_skill") -> str:
    """Convierte un scope de lección (`filesystem/derived`) en un nombre de skill válido.

    El scope usa `/` porque agrupa, pero un identificador de skill no admite guiones fuera del
    underscore: se validan contra el mismo criterio que los ids de paso del plan, y una skill
    acaba siendo un plan. Traducir aquí evita que el ámbito funcional se pierda —`filesystem`
    sigue siendo la primera palabra— por no ser un identificador.
    """
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", scope or "") if p]
    if not parts:
        return fallback
    return "_".join(p.lower() for p in parts)[:64] or fallback


def skill_from_lesson(lesson: Lesson, *, candidate_id: str = "") -> SkillCandidate | None:
    """Convierte una lección en candidata, si la lección tiene con qué hacerlo.

    Una lección que no declara applicability no produce candidata: no se sabe dónde aplicarla,
    y una candidata sin alcance no se puede validar ni buscar.
    """
    if not lesson or not lesson.applicability or not lesson.is_backed:
        return None
    return SkillCandidate(
        candidate_id=candidate_id or f"cand-{uuid.uuid4().hex[:12]}",
        name=_skill_name_from_scope(lesson.scope),
        purpose=lesson.statement,
        source_lessons=[lesson.lesson_id],
        source_experiences=[lesson.source_experience],
        prerequisites=list(lesson.prerequisites),
        success_criteria=[lesson.statement],
        verification=["verificar contra los criterios de la misión"],
        risk="medium" if lesson.confidence >= 0.4 else "low",
        applicability=lesson.applicability,
        contraindications=list(lesson.contraindications),
    )


__all__ = [
    "SkillCandidate",
    "SkillMatch",
    "SkillPerformance",
    "SkillRegistry",
    "SkillStatus",
    "SkillValidator",
    "SkillVersion",
    "skill_from_lesson",
]