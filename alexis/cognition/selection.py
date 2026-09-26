"""Capability Selection dinámica (P0 §5.6 → cierra el requisito 6, el único MISSING).

El plan lo dice sin rodeos: *"La selección de herramientas/capabilities debe ser dinámica.
NO usar una secuencia fija como: understand → research → execute → verify. El plan debe
depender del objetivo."* Antes, `RuleBasedPlanner` elegía la capability con un `dict.get`
sobre la intención del objetivo, y `Selection` era un contrato que no se usaba en ningún
sitio. Aquí vive la selección de verdad.

La frontera es la del plan (F2 §2, P3):

    el modelo PROPONE  ·  el Core DETERMINA qué hay  ·  Policy/Gate DECIDE si se puede

Por eso `select()` nunca confía en una propuesta: la recorta contra el catálogo real, el
envelope, la policy y el gate, y deja constancia de cada rechazo con su regla. Una
capability que no existe no se ejecuta nunca: se convierte en `ASK_USER`, `REPLAN`, `WAIT`
o `ABORT`, según por qué fue rechazada.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from alexis.cognition.contracts import CapabilityProposal, Selection

#: Verbos que delatan una capability de escritura/borrado aunque el objetivo no la nome.
#: Destructivo va aparte de escritura: "borra el archivo" con `fs.write` NO es una
#: diferencia de estilo, es un error de comportamiento (escribe en vez de borrar).
_REMOVE_HINTS = ("borra", "borrar", "borre", "elimina", "eliminar", "suprime",
                 "suprimir", "basta")
_WRITE_HINTS = ("escrib", "crea", "crear", "guarda", "guardar", "sobreescrib",
                "renombra", "mueve")
_READ_HINTS = ("lee", "leer", "lee el", "muestra", "dime", "qué hay", "cual es", "busca",
               "abre", "revisa", "consulta", "compara")
_SPEAK_HINTS = ("habla", "dime en voz", "lee en voz", "responde en voz", "cuéntame")
_DESKTOP_HINTS = ("abre", "launch", "inicia", "ejecuta el programa", "navega")
_ANALYZE_HINTS = ("analiza", "analizar", "explica", "explicar", "compara", "calcula", "resume")

#: Pesos por señal. El objetivo manda, pero la evidencia observada también: seleccionar
#: ignorando lo ya observado es tan fijo como seleccionar ignorando el objetivo.
_WEIGHTS = {
    "objective": 5.0,
    "evidence": 2.0,
    "required": 1.5,
    "verb": 1.0,
    "family_conflict_penalty": -6.0,
    "status_penalty": -4.0,
    "side_effect_penalty": -0.5,
}

#: Qué capabilities son ELLO para una intención, y cuáles la contradicen. Es la parte que
#: hace que "escribe un archivo" elija `fs.write` y no `fs.read`.
_INTENT_FAMILIES = {
    "write": {
        "want": ("fs.write", "fs.remove"),
        "avoid": ("fs.read", "fs.stat", "verification.filesystem", "research.filesystem"),
    },
    "read": {
        "want": ("fs.read", "fs.stat", "research.filesystem", "verification.filesystem"),
        "avoid": ("fs.write", "fs.remove"),
    },
    "remove": {
        "want": ("fs.remove",),
        "avoid": ("fs.read", "fs.stat", "fs.write", "tts.speak", "cognition.understand",
                  "cognition.analyze"),
    },
}

#: Reglas por las que un objetivo puede quedarse sin nada que hacer, y qué hacer entonces.
#: El plan pide exactamente esta distinción: una capability ausente NO se finge ejecutada.
_UNAVAILABLE_DECISIONS = {
    "not_in_catalog": "ask_user",
    "missing": "ask_user",
    "not_enabled": "replan",
    "not_authorized": "ask_user",
    "requires_approval": "wait",
    "no_match": "ask_user",
}


@dataclass
class CapabilitySelector:
    """Selecciona capabilities a partir del objetivo, lo observado y lo realmente disponible.

    No conoce al modelo: recibe una `CapabilityProposal` y la trata como lo que es, una
    propuesta. Quien decide es este Core, contra el catálogo.
    """

    catalog: Any
    policy: Any = None
    gate: Any = None
    min_score: float = 1.0
    #: Se guarda la traza de la última selección para poder auditarla.
    last_trace: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # API principal
    # ------------------------------------------------------------------ #

    def unsatisfiable_verbs(self, objective: str) -> dict[str, str]:
        """Verbos del objetivo cuya capability no existe. `{"renombra": "fs.rename"}`.

        Es el caso que un selector por similitud lexical resuelve mal: "renombra el
        archivo" contiene la palabra "archivo", así que `fs.write` puntúa alto y parece
        una respuesta razonable. No lo es: ALEXIS no sabe renombrar. Cuando aparece un
        verbo insatisfacible, sus sustitutos quedan BLOQUEADOS y, si no queda nada, la
        decisión es `ask_user`.
        """
        folded = _fold(objective)
        missing: dict[str, str] = {}
        for verb, needed in _VERB_REQUIRES.items():
            if verb in folded and not self._exists(needed):
                missing[verb] = needed
        return missing

    def select(
        self,
        objective: str,
        *,
        brief: Any = None,
        knowledge: Any = None,
        proposal: CapabilityProposal | None = None,
        envelope: Any = None,
        mission: Any = None,
        step: Any = None,
    ) -> Selection:
        """Elige capabilities para este objetivo. Devuelve `Selection` con los rechazos."""
        proposed_ids = {str(c) for c in (getattr(proposal, "capabilities", []) or [])}
        invented = self._invented(proposed_ids, {s.id for s in self._all_specs()})
        unsatisfiable = self.unsatisfiable_verbs(objective)
        candidates = self._candidates(objective, brief=brief, knowledge=knowledge,
                                      proposal=proposal, envelope=envelope)
        selection = Selection(rejected=list(invented))
        blocked = self._substitutes_for(unsatisfiable) if unsatisfiable else set()
        for verb, needed in unsatisfiable.items():
            selection.rejected.append(
                {"capability": needed, "rule": "capability_does_not_exist",
                 "reason": f"el objetivo pide '{verb}' y ALEXIS no tiene '{needed}'",
                 "score": 0.0}
            )
        for capability_id, signal, ranked, why in candidates:
            if capability_id in blocked:
                selection.rejected.append(
                    {"capability": capability_id, "rule": "substitute_forbidden",
                     "reason": (
                         f"sustituto de una operación que ALEXIS no puede hacer "
                         f"({', '.join(sorted(unsatisfiable))}); no se finge ejecución"
                     ),
                     "score": ranked}
                )
                continue
            verdict = self._authorize(mission, step, capability_id, envelope)
            if verdict["rejected"]:
                selection.rejected.append(
                    {"capability": capability_id, "rule": verdict["rule"],
                     "reason": verdict["reason"], "score": ranked}
                )
                continue
            if signal < self.min_score:
                selection.rejected.append(
                    {"capability": capability_id, "rule": "below_min_score",
                     "reason": f"señal {signal:.1f} < {self.min_score}", "score": ranked}
                )
                continue
            selection.selected.append(capability_id)
            entry = {
                "capability": capability_id, "score": round(ranked, 2),
                "signal": round(signal, 2), "why": why,
                "rule": verdict.get("rule") or "allowed",
            }
            selection.trace.append(entry)
            self.last_trace.append(entry)
        selection.rationale = self._rationale(selection, objective)
        return selection

    def best(self, objective: str, **kwargs) -> Selection:
        """Atajo: la mejor capability, o `Selection` vacía con su rechazo."""
        selection = self.select(objective, **kwargs)
        selection.selected = selection.selected[:1]
        return selection

    def decide_unavailable(self, selection: Selection) -> str:
        """Qué hacer cuando no hay nada seleccionable: ask_user | replan | wait | abort.

        Es la traducción de `UNAVAILABLE_DECISIONS` sobre los rechazos reales. Si no se
        conoce ninguna capability que haga falta, no hay nada que replanear: se pregunta.
        """
        if selection.selected:
            return "continue"
        rules = {row.get("rule") for row in selection.rejected}
        if not rules:
            return "abort"
        decisions = {_UNAVAILABLE_DECISIONS.get(rule, "ask_user") for rule in rules}
        if decisions == {"replan"}:
            return "replan"
        if "ask_user" in decisions:
            return "ask_user"
        if "wait" in decisions:
            return "wait"
        return "abort"

    # ------------------------------------------------------------------ #
    # Candidatos
    # ------------------------------------------------------------------ #

    def _candidates(self, objective, *, brief, knowledge, proposal, envelope):
        terms = _terms(objective)
        evidence_terms = self._evidence_terms(knowledge)
        required = set(getattr(brief, "required_capabilities", []) or [])
        proposed = {str(c) for c in (getattr(proposal, "capabilities", []) or [])}
        verb_hints, verb_family = _verb_hints(objective)

        allowed = self._allowed_ids(envelope)
        rows: list[tuple[str, float, float, str]] = []
        for spec in self._enabled_specs():
            capability_id = spec.id
            signal, penalty, why = self._score(
                spec, terms, evidence_terms, required, proposed, verb_hints,
                objective, verb_family,
            )
            ranked = signal + penalty
            if capability_id not in allowed:
                rows.append((capability_id, signal, ranked, f"fuera del envelope ({ranked:.1f})"))
                continue
            rows.append((capability_id, signal, ranked, why))
        # Se ordena por la puntuación con penalización, pero la SEÑAL es la que decide
        # si la capability califica: un penalizador ordena, no descalifica.
        rows.sort(key=lambda row: row[2], reverse=True)
        return rows

    def _invented(self, proposed: set[str], known: set[str]) -> list[dict[str, Any]]:
        """Capabilities que el modelo propuso y no existen. Queda constancia del rechazo.

        Sin esto, una capability inventada se ignora en silencio: no se ejecuta (correcto),
        pero no hay prueba de que se rechazara. El plan exige que el descarte sea auditable,
        y `not_in_catalog` es la regla que lo dice.
        """
        return [
            {
                "capability": capability_id,
                "rule": "not_in_catalog",
                "reason": f"'{capability_id}' no existe en el catálogo: el modelo no inventa capabilities",
                "score": 0.0,
                "proposed_by_model": True,
            }
            for capability_id in sorted(proposed - known)
        ]

    def _score(self, spec, terms, evidence_terms, required, proposed, verb_hints,
               objective, verb_family="read"):
        """Puntúa la capability. Devuelve `(señal, penalización, why)`.

        Los penalizadores (`family_conflict_penalty`, `side_effect_penalty`) se suman al
        final y sólo ordenan: una penalización descalifica al candidato equivocado, pero
        no puede descalificar lo que el usuario pidió explícitamente. Por eso
        "elimina el directorio" selecciona `fs.remove` aunque tenga efectos: pedir un
        borrado es una orden, no una sugerencia.
        """
        score = 0.0
        penalty = 0.0
        why: list[str] = []
        haystack = " ".join(
            filter(None, [spec.id, spec.sphere, spec.description, spec.plans_action or "",
                          " ".join(spec.resources or [])])
        ).lower()
        words = _terms(haystack)
        overlap = terms & words
        if overlap:
            score += _WEIGHTS["objective"] * min(len(overlap), 3)
            why.append(f"objetivo:{sorted(overlap)[:3]}")
        if evidence_terms and (evidence_terms & words):
            score += _WEIGHTS["evidence"]
            why.append("evidencia")
        if spec.id in required:
            score += _WEIGHTS["required"]
            why.append("requerida por el Self Model")
        if spec.id in proposed:
            # La propuesta del modelo suma, pero NO decide: si no está en el catálogo, se
            # rechaza más abajo. Un modelo no puede concederse nada con esto.
            score += 1.0
            why.append("propuesta por el modelo")
        for hint, capability_prefix in verb_hints:
            if not spec.id.startswith(capability_prefix):
                continue
            needed = _VERB_REQUIRES.get(hint)
            if needed is not None and not self._exists(needed):
                # El verbo pide algo que ALEXIS no tiene. No se sustituye por un
                # pariente: se registra y la capability no qualifies.
                why.append(f"verbo '{hint}' necesita '{needed}', que no existe")
                continue
            score += _WEIGHTS["verb"]
            why.append(f"verbo:{hint}")
            break
        # La intención discrimina DENTRO de la familia. Sin esto, "escribe un archivo"
        # empataba entre fs.read y fs.write y ganaba la lectura: el selector dinámico
        # tienen que saber leer, no sólo puntuar.
        conflict = _conflicting_family(spec.id, verb_family)
        if conflict:
            penalty += _WEIGHTS["family_conflict_penalty"]
            why.append(f"familia en conflicto ({conflict})")
        if getattr(spec, "status", "available") != "available":
            penalty += _WEIGHTS["status_penalty"]
            why.append(f"status={spec.status}")
        if getattr(spec, "side_effects", False):
            penalty += _WEIGHTS["side_effect_penalty"]
            why.append("con efectos secundarios")
        return score, penalty, "; ".join(why) or "sin señal"

    def _enabled_specs(self):
        registry = self.catalog
        specs = getattr(registry, "enabled", None)
        if callable(specs):
            specs = specs()
        if specs is None:
            specs = getattr(registry, "specs", lambda: [])()
        return [s for s in specs if not getattr(s, "status", "available") == "missing"]

    def _allowed_ids(self, envelope) -> set[str]:
        """Capabilities que el envelope permite. Sin envelope, sólo las del catálogo."""
        if envelope is None:
            return {spec.id for spec in self._all_specs()}
        allowed_actions = getattr(envelope, "allowed_actions", None)
        declared = getattr(envelope, "allowed_capabilities", None)
        if declared:
            return {str(c) for c in declared}
        if not allowed_actions:
            return {spec.id for spec in self._all_specs()}
        # El envelope declara ACCIONES, no capabilities: se mapea la más cercana.
        from alexis.cognition.planner import _step_capability  # Import local: evita ciclo

        mapped: set[str] = set()
        for action in allowed_actions:
            capability = _ACTION_TO_CAPABILITY.get(str(action))
            if capability:
                mapped.add(capability)
        return mapped or {spec.id for spec in self._all_specs()}

    def _all_specs(self):
        specs = getattr(self.catalog, "specs", None)
        return specs() if callable(specs) else list(specs or [])

    # ------------------------------------------------------------------ #
    # Autoridad
    # ------------------------------------------------------------------ #

    def _authorize(self, mission, step, capability_id: str, envelope) -> dict:
        """Policy/Gate tienen la última palabra. Aquí no se concede nada."""
        if not self._exists(capability_id):
            return {"rejected": True, "rule": "not_in_catalog",
                    "reason": f"'{capability_id}' no existe en el catálogo"}
        if mission is None or step is None:
            return {"rejected": False, "rule": "allowed", "reason": ""}
        probe = _probe_step(step, capability_id)
        try:
            if self.gate is not None:
                decision = self.gate.decide(mission, probe, self.policy)
            elif self.policy is not None:
                decision = self.policy.authorize(mission, probe)
            else:
                return {"rejected": False, "rule": "allowed", "reason": ""}
        except Exception as exc:  # noqa: BLE001 — la excepción ES el rechazo
            return {"rejected": True, "rule": "not_authorized",
                    "reason": f"la autoridad falló: {exc}"}
        # El orden importa: "requiere aprobación" es una espera, no un veto. Si se
        # mirase `allowed` primero, ambas condiciones colapsarían en `not_authorized` y
        # la decisiónControlled sería `ask_user` cuando lo correcto es `wait`.
        if getattr(decision, "requires_approval", False):
            return {"rejected": True, "rule": "requires_approval",
                    "reason": str(getattr(decision, "reason", "") or "requiere aprobación")}
        if not getattr(decision, "allowed", False):
            return {"rejected": True, "rule": "not_authorized",
                    "reason": str(getattr(decision, "reason", "") or "no autorizado")}
        return {"rejected": False, "rule": "allowed", "reason": ""}

    def _substitutes_for(self, unsatisfiable: dict[str, str]) -> set[str]:
        """Capabilities que se parecerían a la que falta y NO deben suplantarla."""
        blocked: set[str] = set()
        for needed in unsatisfiable.values():
            family = needed.split(".")[0]
            for spec in self._all_specs():
                if spec.id.startswith(family + "."):
                    blocked.add(spec.id)
        return blocked

    def _exists(self, capability_id: str) -> bool:
        has = getattr(self.catalog, "has", None)
        if callable(has):
            try:
                return bool(has(capability_id))
            except Exception:  # noqa: BLE001
                return False
        return capability_id in {s.id for s in self._all_specs()}

    def _evidence_terms(self, knowledge) -> set[str]:
        """Términos de lo ya observado. Seleccionar ignorando la evidencia es tan fijo
        como ignorar el objetivo."""
        if knowledge is None:
            return set()
        terms: set[str] = set()
        for item in list(getattr(knowledge, "known", []) or [])[-5:]:
            terms |= _terms(str(item))
        for claim in list(getattr(knowledge, "claims", []) or []):
            terms |= _terms(str(getattr(claim, "text", "")))
        for failure in list(getattr(knowledge, "failed_steps", []) or [])[-3:]:
            terms |= _terms(str(failure))
        return terms

    def _rationale(self, selection: Selection, objective: str) -> str:
        if not selection.selected:
            return f"nada seleccionable para {objective!r}"
        head = selection.selected[0]
        trace = next((t for t in self.last_trace if t["capability"] == head), None)
        base = f"seleccionada {head}"
        if trace and trace["why"]:
            base += f" ({trace['why']})"
        if selection.rejected:
            base += f"; {len(selection.rejected)} descartada(s) por las reglas de autoridad"
        return base


_ACTION_TO_CAPABILITY = {
    "analyze": "cognition.understand",
    "research": "research.filesystem",
    "execute": "fs.read",
    "respond": "tts.speak",
    "verify": "verification.filesystem",
}

#: Verbo → capability que REALLY lo cumple. Un verbo sólo suma si esa capability existe en
#: el catálogo. Sin esta tabla, "renombra el archivo" (que ALEXIS no sabe renombrar)
#: heredaba `fs.write` por ser de la familia "escritura" y parecía funcionar. Es el
#: sustituto silencioso que el plan prohíbe: `test_unsupported_intent_fails_honest`.
_VERB_REQUIRES = {
    "renombra": "fs.rename", "renombrar": "fs.rename",
    "mueve": "fs.move", "mover": "fs.move",
    "escrib": "fs.write", "crea": "fs.write", "crear": "fs.write",
    "guarda": "fs.write", "guardar": "fs.write", "sobreescrib": "fs.write",
    "borra": "fs.remove", "borrar": "fs.remove", "borre": "fs.remove",
    "elimina": "fs.remove", "eliminar": "fs.remove",
    "suprime": "fs.remove", "suprimir": "fs.remove",
    "lee": "fs.read", "leer": "fs.read",
    "abre": "desktop.tools", "navega": "desktop.tools",
}

_VERB_HINTS = (
    (_REMOVE_HINTS, "fs."),
    (_WRITE_HINTS, "fs."),
    (_READ_HINTS, "fs."),
    (_SPEAK_HINTS, "tts."),
    (_DESKTOP_HINTS, "desktop."),
    (_ANALYZE_HINTS, "cognition."),
)


def _fold(text: str) -> str:
    """Minúsculas y SIN acentos. Sin esto, "háblame" no casa con el hint "habla"."""
    import unicodedata

    decomposed = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _verb_hints(objective: str) -> tuple[list[tuple[str, str]], str]:
    """Devuelve los (verbo, prefijo) detectados y la familia de intención dominante."""
    lowered = _fold(objective)
    hits: list[tuple[str, str]] = []
    for hints, prefix in _VERB_HINTS:
        for hint in hints:
            if hint in lowered:
                hits.append((hint, prefix))
                break
    family = "read"
    if any(_fold(hint) in lowered for hint in _REMOVE_HINTS):
        family = "remove"
    elif any(_fold(hint) in lowered for hint in _WRITE_HINTS):
        family = "write"
    return hits, family


def _conflicting_family(capability_id: str, family: str) -> str:
    """Nombre de la familia que contradice a esta capability, o "" si no hay conflicto."""
    if family not in _INTENT_FAMILIES:
        return ""
    rules = _INTENT_FAMILIES[family]
    if capability_id in rules["avoid"]:
        return family
    return ""


#: Plurales y formas，吾它们的 tocante: "archivo" tiene que casar con "archivos" o el
#: selector puntúa 0 una capability que sí encaja. No es un stemmer lingü completo; es
#: la normalización mínima que evita el fallo observado.
def _stem(word: str) -> str:
    if len(word) > 5 and word.endswith("es"):
        return word[:-2]
    if len(word) > 4 and word.endswith("s"):
        return word[:-1]
    return word


def _terms(text: str) -> set[str]:
    """Términos normalizados. Sin `_stem`, "archivo" no casa con "archivos"."""
    return {
        _stem(word)
        for word in re.findall(r"[a-z0-9áéíóúñ]{3,}", (text or "").lower())
    }


def _probe_step(step, capability_id: str):
    """Clona el paso con otra capability, para consultar a la autoridad sin mutar el plan."""
    import copy

    probe = copy.copy(step)
    probe.capability = capability_id
    return probe
