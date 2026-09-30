"""CORE-02 — Contrato de `success_criteria`: texto libre → predicado verificable.

Este módulo es un helper PURO. No toca `GoalVerifier`, no inventa predicados y no
conoce el runtime. Su trabajo es único y acotado: convertir lo que el Intent Engine
produjo (texto libre, a menudo vacío) en criterios CANÓNICOS que el vocabulario ya
existente del `GoalVerifier` sabe comprobar.

El vocabulario NO se define aquí: `PREDICATES` y `parse_predicate` se importan desde
`goal_verification` para que esta capa no pueda divergir de la que verifica. Este
módulo sólo decide QUÉ predicado usar para un objetivo o un texto dado.

Reglas (las del contrato CORE-02):

- Un criterio que YA trae un predicado válido se conserva tal cual (regla A).
- Un criterio en lenguaje natural que contiene una ruta verificable se canoniza
  (regla B): "El archivo notas.txt existe" → "file_exists:notas.txt".
- Si el objetivo es lectura/inspección de una ruta → "file_exists:RUTA" (regla C).
- Si es write/create/edit → "file_exists:RUTA" (+ "file_size_at_least:RUTA:1" si el
  texto pide contenido no vacío) (regla D).
- Si es destructive/remove → "file_missing:RUTA" (regla E).
- Si el texto habla de la suite pasando → "tests_passing:..." (regla F).
- Si habla de la suite fallando → "tests_failing:..." (regla G).
- La ruta se extrae SIEMPRE del `utterance` original antes que del `objective`, que
  el modelo puede parafrasear (regla H).
- Máximo 5 criterios (regla I), en orden estable.
- Un criterio que NO se puede vincular a un predicado no se descarta en silencio:
  se conserva tal cual y se cuenta como `unverifiable` (regla J).
- La procedencia queda en `Intent.model_meta["criteria_status"]` (regla K).

`normalize_criteria()` es la única API pública. Devuelve una lista canónica; quien la
consuma decide qué hacer con la trazabilidad. La demo la invoca desde
`_create_mission_from_intent`; el test la ejerce directamente.
"""

from __future__ import annotations

import re
from typing import Any

from alexis.cognition.goal_verification import PREDICATES, parse_predicate
from alexis.tools.filesystem import classify_objective_intent, extract_workspace_path

#: Tope de criterios (regla I). Mismo límite que el que ya imponía `parse()` del
#: clasificador por modelo, ahora también para la derivación determinista.
MAX_CRITERIA = 5

#: Sufijo que usan los predicados de test. `parse_predicate` exige "predicado:arg" con
#: dos puntos: "tests_passing" a secas NO parsea y el GoalVerifier lo dejaría en
#: "no hay checker". Por eso los dos predicados de suite llevan SIEMPRE ":suite".
_TESTS_ARG = "suite"

#: Marcadores que indican que la tarea habla de la suite de tests.
_TESTS_WORDS = ("test", "tests", "suite", "pruebas", "prueba")
#: Marcadores de "la suite pasa".
_TESTS_PASS = ("pasa", "pasan", "pasen", "passing", "passed", "en verde", "verde",
               "sin fallar", "sin fallos", "correctas")
#: Marcadores de "la suite falla". "fallo/falla" NO bastan solos porque un objetivo
#: puede ser "corrige el fallo de X" sin hablar de la suite: eso NO es tests_failing.
#: Se exige que el fallo vaya pegado a la palabra de suite ("tests fallan",
#: "tests fallando", "la suite falla") o sea una forma de fallo inequívoca.
_TESTS_FAIL = ("tests fallan", "tests fallando", "tests fallidos", "tests fallidas",
               "suite falla", "suite fallando", "pruebas fallan", "test falla",
               "fallando los tests", "fallan las pruebas", "failing", "failed",
               "tests rojos", "suite en rojo", "en rojo")
#: "los tests están fallando": la suite y el verbo de fallo están separados por el verbo
#: "estar". Se cubre buscando el verbo de fallo y exigiendo que la suite esté cerca.
_FALL_VERB = ("falla", "fallan", "fallando", "fallaron", "fallido", "fallida", "roto", "rota")
#: Una ventana de palabras tan corta que "el fallo de normalización" (un bug, no la
#: suite) no entra, pero "los tests están fallando" sí.
_NEAR_WINDOW = 5
_NEGATION_MARKS = ("no existe", "no esta", "no está", "no queda", "no debe",
                   "no debe existir", "borrado", "borrada", "eliminado", "eliminada",
                   "ya no", "desaparecido", "ausente")

#: Objetivos de escritura/creación. La derivación de lectura usa el clasificador de
#: intención de filesystem (read | write | destructive | unsupported), no estas
#: palabras sueltas, para no duplicar reglas.
_CREATE_TERMS = (
    "crea", "crear", "escribe", "escribir", "genera", "generar", "guarda", "guardar",
    "redacta", "redactar", "prepara", "preparar", "monta", "montar", "construye",
    "construir", "añade", "anade", "agrega", "creame", "escribeme", "hazme",
)
_EDIT_TERMS = (
    "modifica", "modificar", "edita", "editar", "actualiza", "actualizar",
    "cambia", "cambiar", "corrige", "corregir", "arregla", "arreglar", "repara",
    "reparar", "añade contenido", "amplia", "ampliar", "sustituye",
)
_DELETE_TERMS = (
    "borra", "borrar", "elimina", "eliminar", "quita", "quitar", "suprime",
    "suprimir", "borrame", "eliminar", "clean", "borra el archivo", "borra la",
)
#: "…con contenido", "no vacío", "con al menos N…" → justifies file_size_at_least.
_CONTENT_TERMS = (
    "con contenido", "con texto", "con datos", "con al menos", "no vacío",
    "no vacio", "no está vacío", "no esta vacio", "con información", "con info",
    "con una línea", "con una linea", "algo dentro", "con algo",
)

#: Un nombre de fichero NO puede contener espacios. Si la extracción devuelve algo que
#: es sólo un trozo de un nombre más largo con espacios, no se inventa ruta: se
#: devuelve None y el criterio queda unverifiable (regla: rutas con espacios no se
#: inventan).
_SPACE_RE = re.compile(r"\s")


def _norm(text: str | None) -> str:
    return " ".join((text or "").split()).lower()


def _mentions_tests(text: str) -> bool:
    low = _norm(text)
    return any(word in low for word in _TESTS_WORDS)


def _tests_predicate(text: str) -> str | None:
    """`tests_passing:suite` / `tests_failing:suite` si el texto habla de la suite.

    Devuelve None si el texto no es (o no distingue) la suite. Se apoya en el mismo
    vocabulario cerrado del `GoalVerifier`; no añade predicados.
    """
    if not _mentions_tests(text):
        return None
    low = _norm(text)
    if any(mark in low for mark in _TESTS_FAIL):
        return f"tests_failing:{_TESTS_ARG}"
    if _suite_near_failure(low):
        return f"tests_failing:{_TESTS_ARG}"
    if any(mark in low for mark in _TESTS_PASS):
        return f"tests_passing:{_TESTS_ARG}"
    return None


def _suite_near_failure(low: str) -> bool:
    """`la suite está fallando` → sí es la suite la que falla, no un bug suelto.

    Exige que un verbo de fallo aparezca a pocas palabras de la palabra de suite. Sin
    esa cercanía, "corrige el fallo de normalización" (que no habla de suite) se
    confundiría con un criterio de suite, y ese falso `tests_failing` inventaría un
    objetivo que el usuario nunca pidió.
    """
    tokens = low.replace(".", " ").replace(",", " ").split()
    for index, token in enumerate(tokens):
        if not any(word in token for word in _TESTS_WORDS):
            continue
        window = tokens[max(0, index - _NEAR_WINDOW): index + _NEAR_WINDOW + 1]
        if any(any(verb in w for verb in _FALL_VERB) for w in window):
            return True
    return False


#: Palabras que, seguidas de la ruta, señalan que la ruta es el NOMBRE COMPLETO del
#: fichero: "el archivo notas.txt", "lee notas.txt", "analiza notas.txt". Si la
#: palabra anterior no es una de estas, el nombre probablemente tiene espacios
#: ("informe final.txt") y adivinarlo sería comprobar un fichero que no existe: no se
#: deriva y el criterio queda unverifiable.
_PATH_LEAD_WORDS = frozenset({
    # artículo / preposición / conector
    "el", "la", "los", "las", "un", "una", "de", "del", "en", "a", "al", "y", "con",
    "the", "a", "an", "of", "in", "to", "and", "this", "that", "my", "su", "sus",
    # nombre genérico de fichero
    "archivo", "archivos", "fichero", "ficheros", "documento", "documentos",
    "file", "files", "doc", "docs",
    # verbos de tarea que toman el nombre del fichero como objeto directo
    "analiza", "analizar", "analice", "lee", "leer", "leeme", "borra", "borrar",
    "borre", "elimina", "eliminar", "crea", "crear", "escribe", "escribir",
    "genera", "generar", "modifica", "modificar", "edita", "editar", "actualiza",
    "actualizar", "corrige", "corregir", "arregla", "arreglar", "repara", "revisar",
    "revisa", "resume", "resumir", "abre", "abrir", "muestra", "muéstrame", "muestrame",
    "quita", "quitar", "suprime", "mira", "busca", "buscar", "investiga", "investigar",
    "analyze", "read", "open", "show", "delete", "remove", "create", "write", "update",
    "fix", "review", "summarize", "explain", "report",
})


def _safe_path(text: str) -> str | None:
    """Ruta del workspace citada en el texto, o None si no es un nombre de un token.

    Envuelve `extract_workspace_path` (la extracción determinista que ya usa el resto
    del sistema) y le añade una salvaguarda contra inventar rutas. El problema: para
    "informe final.txt" la extracción devuelve "final.txt" (el espacio corta la
    expresión), pero el fichero real se llama "informe final.txt" y el `GoalVerifier`
    sólo comprobaría un token. Derivar "final.txt" sería observar un fichero
    equivocado y podría dar por cumplido un objetivo que no se midió.

    Regla conservadora: la ruta sólo se acepta si la palabra que la precede es un
    artículo/preposición o un nombre genérico de fichero ("el archivo notas.txt"), o
    si la ruta es la primera palabra del texto ("notas.txt"). En cualquier otro caso
    ("informe final.txt") se devuelve None: es preferible un criterio unverifiable a
    uno que compruebe el fichero equivocado.
    """
    path = extract_workspace_path(text)
    if not path:
        return None
    if _SPACE_RE.search(path):
        return None

    # Localiza el match para inspeccionar la palabra anterior.
    match = None
    for candidate in re.finditer(
        r"[\w./\-]+\.(?:txt|md|json|log|csv|py|ini|env|yaml|yml)", text or ""
    ):
        if candidate.group(0) == path:
            match = candidate
            break
    if match is None:
        return None

    before = (text or "")[: match.start()].strip()
    if not before:
        return path  # la ruta es la primera palabra del texto
    lead = before.split()[-1].strip("\"'.,;:()[]!?").lower()
    if lead in _PATH_LEAD_WORDS:
        return path
    # La palabra previa no introduce un nombre de fichero completo: probablemente hay
    # espacios en el nombre. No se inventa la ruta.
    return None

def _wants_content(text: str) -> bool:
    return any(term in _norm(text) for term in _CONTENT_TERMS)


def _is_negated(text: str) -> bool:
    """True si el texto dice que el archivo NO debe existir ("ya no está", "borrado")."""
    low = _norm(text)
    return any(mark in low for mark in _NEGATION_MARKS)


def _derives_for_intent(intent: str, text: str, path: str) -> str | None:
    """Predicado canónico para un objetivo con ruta, según read/write/destructive.

    - negated     → file_missing:RUTA            ("ya no está", "borrado")
    - destructive → file_missing:RUTA            (regla E)
    - write/edit  → file_exists:RUTA             (regla D)
    - read        → file_exists:RUTA             (regla C)
    """
    low = _norm(text)

    if _is_negated(text) or any(term in low for term in _DELETE_TERMS):
        return f"file_missing:{path}"
    if any(term in low for term in _CREATE_TERMS) or any(term in low for term in _EDIT_TERMS):
        return f"file_exists:{path}"
    if classify_objective_intent(text) == "read":
        return f"file_exists:{path}"
    return None


def _classify_criterion(text: str, intent: str) -> tuple[str | None, str]:
    """Clasifica UN criterio. Devuelve `(canónico o None, veredicto)`.

    Veredictos posibles:

    - ``"model"``: ya traía un predicado válido; se conserva tal cual (regla A).
    - ``"canonical"``: era texto libre y se ha canonizado a un predicado (reglas
      B, F, G).
    - ``"unverifiable"``: no se puede comprobar hoy. NO se descarta: el texto original
      se conserva en la lista (regla J) y queda trazado en `criteria_status`.

    `intent` (read | write | destructive) es la lectura de la operación que el
    clasificador de filesystem ya calcula; decide el predicado de fichero.
    """
    raw = (text or "").strip()
    if not raw:
        return None, "unverifiable"

    # Regla A: ya es un predicado que el GoalVerifier sabe leer.
    if parse_predicate(raw) is not None:
        return raw, "model"

    # Reglas F/G: habla de la suite. No necesita ruta, así que va antes.
    tests = _tests_predicate(raw)
    if tests is not None:
        return tests, "canonical"

    # Reglas B/C/D/E: contiene una ruta del workspace.
    path = _safe_path(raw)
    if path is not None:
        return _derives_for_intent(intent, raw, path), "canonical"

    # Regla J: nada que comprobar, pero el criterio del modelo no se pierde.
    return None, "unverifiable"


def _derive_from_utterance(utterance: str, objective: str) -> list[str]:
    """Criterios deterministas desde el texto del usuario (reglas C-G, H).

    Prioriza el `utterance` ORIGINAL sobre el `objective` del modelo: el modelo puede
    parafrasear e inventar o cambiar la ruta, el utterance no (regla H). Si del
    utterance sale algún criterio, no se mira el objective: una ruta que el modelo
    inventó no puede colarse en el contrato.
    """
    for text in (utterance, objective):
        if not (text or "").strip():
            continue
        intent = classify_objective_intent(text)
        criteria: list[str] = []

        tests = _tests_predicate(text)
        if tests is not None:
            criteria.append(tests)

        path = _safe_path(text)
        if path is not None:
            derived = _derives_for_intent(intent, text, path)
            if derived is not None:
                criteria.append(derived)
                # Regla D (segunda parte): si además se pide contenido no vacío en un
                # objetivo de escritura/edición, se añade el criterio de tamaño.
                if (
                    _wants_content(text)
                    and derived.startswith("file_exists")
                    and any(term in _norm(text) for term in _CREATE_TERMS + _EDIT_TERMS)
                ):
                    criteria.append(f"file_size_at_least:{path}:1")

        if criteria:
            return criteria[:MAX_CRITERIA]
    return []


def _is_verifiable(criterion: str) -> bool:
    return parse_predicate(criterion) is not None


def canonicalize(criterion: str) -> str | None:
    """Criterio canónico a partir de un texto, o None si no hay predicado derivable.

    Atajo de una sola métrica sobre `_classify_criterion`, sin derivar nada del
    objetivo. `normalize_criteria` es la API que usa el clasificador; esto es sólo
    para quien quiera canonizar un criterio suelto.
    """
    canonical, _ = _classify_criterion(criterion, classify_objective_intent(criterion))
    return canonical


def normalize_criteria(
    utterance: str,
    objective: str,
    raw_criteria: list[str] | None,
) -> tuple[list[str], dict[str, Any]]:
    """Convierte los criterios del Intent en una lista canónica y trazada.

    Devuelve ``(criteria, criteria_status)``:

    - ``criteria``: lo que viaja a ``Mission.goal.success_criteria``. Contiene los
      criterios CANÓNICOS (los que el `GoalVerifier` sabe comprobar) y además los
      criterios del modelo que no se pudieron canonizar, CONSERVADOS tal cual (regla J):
      no se descartan en silencio, y el `GoalVerifier` los dejará honestamente en
      "no hay checker". Borrarlos haría desaparecer del contexto la razón por la que
      el objetivo no cerró.
    - ``criteria_status``: trazabilidad (regla K) con el recuento de verificables y no
      verificables, el motivo y el texto de los no verificables. Es sólo metadata: no
      altera la lista.

    Orden de aplicación:
      1. Clasificar cada criterio recibido (A, B, F, G, J).
      2. Si NO queda ningún criterio verificable, derivar de utterance→objective
         (C, D, E, H).
      3. Tope de 5, sin reordenar (I).
    """
    raw: list[str] = [str(c) for c in (raw_criteria or []) if str(c or "").strip()]

    criteria: list[str] = []
    unverifiable: list[str] = []

    for item in raw:
        canonical, verdict = _classify_criterion(item, classify_objective_intent(item))
        if verdict == "unverifiable" or canonical is None:
            # Regla J: el criterio del modelo no se descarta, queda trazado.
            if item not in unverifiable:
                unverifiable.append(item)
            if len(criteria) < MAX_CRITERIA and item not in criteria:
                criteria.append(item)
            continue
        if canonical not in criteria and len(criteria) < MAX_CRITERIA:
            criteria.append(canonical)

    if not any(_is_verifiable(c) for c in criteria):
        for derived in _derive_from_utterance(utterance, objective):
            if derived not in criteria and len(criteria) < MAX_CRITERIA:
                criteria.append(derived)

    criteria = criteria[:MAX_CRITERIA]
    verifiable = [c for c in criteria if _is_verifiable(c)]

    if not criteria:
        reason = (
            "el turno no nombra ningún objetivo comprobable: no hay ruta del workspace "
            "ni suite que verificar con el vocabulario actual"
        )
    elif not verifiable:
        reason = (
            f"{len(unverifiable)} criterio/s no se pueden comprobar hoy: no nombran una "
            "ruta del workspace ni la suite, así que no hay checker para ellos"
        )
    else:
        reason = f"{len(verifiable)} criterio/s verificable/s con el vocabulario actual"

    status: dict[str, Any] = {
        "verifiable": len(verifiable),
        "unverifiable": len(unverifiable),
        "reason": reason,
        "unverifiable_criteria": list(unverifiable),
    }
    return criteria, status


__all__ = [
    "MAX_CRITERIA",
    "PREDICATES",
    "canonicalize",
    "normalize_criteria",
]
