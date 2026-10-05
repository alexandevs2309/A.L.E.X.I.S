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
    # P0 §11: copiar y volcar también PRODUCEN un artefacto. Sin ellos, "copia A en B"
    # no caía en ninguna familia y se quedaba sin criterio verificable.
    "copia", "copiar", "copie", "traduce", "traducir", "traduce",
    "extrae", "extraer", "transforma", "transformar", "generame",
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
    # P0 §11: las preguntas de existencia y de contenido también introducen el nombre del
    # fichero. Sin estas, "comprueba si notas.txt existe" no parecía nombrar una ruta
    # completa ("si" no es un artículo) y el objetivo se quedaba sin criterio.
    "si", "sí", "comprueba", "comprobar", "compruebe", "verifica", "verificar",
    "dime", "diga", "diga", "muéstrame", "muestrame", "muestra", "traduce", "traducir",
    "resume", "resumir", "resuma", "copia", "copiar", "extrae", "extraer", "quiero",
    "necesito", "debe", "deben", "tiene", "tienen", "hay", "existe", "existen", "esta",
    "está", "este", "esta", "datos", "dato", "archivo", "fichero", "documento",
    # P0 §11: los verbos de pregunta sobre el CONTENIDO también introducen el nombre.
    # Sin ellos, "dime qué contiene notas.txt" no parecía nombrar una ruta completa
    # (la palabra previa es "contiene") y el objetivo se quedaba sin criterio.
    "contiene", "contengan", "dice", "diga", "dicen", "tenia", "tenía", "saying",
    # §11: verbos que relacionan o transforman dos ficheros también introducen el nombre.
    # Sin ellos, "compara notas.txt con otros.txt" sólo reconocía la segunda ruta y el
    # sistema comparaba un fichero con un contrato sobre otro.
    "compara", "comparar", "comparation", "contrasta", "contrastar", "diferencia",
    "diferencias", "equipara", "equiparar", "usando", "contra", "versus", "traduce",
    "traducir", "extrae", "extraer", "transforma", "transformar", "y", "con",
    "poner", "pone", "ponega", "ocurre", "explica", "explicame", "resumen",
})


#: Patrón de un nombre de fichero del workspace dentro de un texto en lenguaje natural.
_PATH_TOKEN = re.compile(r"[\w./\-]+\.(?:txt|md|json|log|csv|py|ini|env|yaml|yml)")


def _admits_path(raw: str, match: re.Match) -> bool:
    """¿La coincidencia es un nombre de fichero COMPLETO, o un trozo de uno mayor?

    UNA sola regla de admisión, usada por el extractor simple y el múltiple. La palabra
    que introduce el nombre tiene que ser un artículo, preposición o verbo de tarea; si
    no lo es, probablemente el nombre tenga espacios ("informe final.txt") y adivinar
    "final.txt" sería comprobar un fichero que el usuario nunca nombró.

    También admite que la ruta abra la frase tras una interrogación ("¿existe
    datos.txt?"), donde el signo no es una palabra pero sí introduce el nombre.
    """
    before = (raw or "")[: match.start()].strip()
    if not before:
        return True  # la ruta es la primera palabra del texto
    # La puntuación puede ir pegada a la palabra ("¿existe"): se quita por ambos lados.
    lead = before.split()[-1].strip("\"'.,;:()[]!?").lstrip("¿?¡!").lower()
    if not lead:
        return True  # el texto previo era sólo signos de interrogación
    return lead in _PATH_LEAD_WORDS


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
    for match in _PATH_TOKEN.finditer(text or ""):
        if match.group(0) == path:
            return path if _admits_path(text, match) else None
    return None

def _wants_content(text: str) -> bool:
    return any(term in _norm(text) for term in _CONTENT_TERMS)


def _is_negated(text: str) -> bool:
    """True si el texto dice que el archivo NO debe existir ("ya no está", "borrado")."""
    low = _norm(text)
    return any(mark in low for mark in _NEGATION_MARKS)


def _derives_for_intent(intent: str, text: str, path: str) -> str | None:
    """Deprecated: usa `criteria_for_objective`. Se conserva para los tests que aún la
    invocan con (intent, text, path). Delega en la clasificación semántica."""
    return criteria_for_objective(text)[0] if criteria_for_objective(text) else None


# --------------------------------------------------------------------------- #
# P0 §11 — SEMÁNTICA DEL OBJETIVO
#
# El fallo que este bloque cierra: la presencia de una ruta bastaba para derivar
# `file_exists:RUTA`. "Analiza notas.txt y dime qué contiene" quedaba reducido a
# "notas.txt existe", un hecho que ya era cierto antes de que ALEXIS hiciera nada, y la
# misión se cerraba sin que nadie hubiera leído el fichero.
#
# La regla nueva: una ruta NO habilita un predicado por sí sola. Hay que leer qué pide
# el objetivo, y cada familia semántica tiene su contrato de verificación:
#
#   existence      → file_exists:RUTA        (el objetivo ES que exista)
#   destructive    → file_missing:RUTA       (el objetivo ES que no exista)
#   creation       → file_exists:RUTA + tamaño no vacío (el objetivo ES el artefacto)
#   modification   → file_exists:RUTA        (el objetivo ES cambiarlo)
#   analysis       → content_observed:RUTA   (el objetivo es el CONTENIDO, no el fichero)
#   transformation → content_observed:ENTRADA + file_exists:SALIDA + tamaño (hay cadena)
#   query          → sin predicado            (no hay artefacto: hoy no es comprobable)
#
# Un objetivo semántico nunca se degrada a un criterio más débil: si no hay predicado
# que lo sostenga, queda unverifiable y la misión NO se cierra.
# --------------------------------------------------------------------------- #

#: families de objetivo. Los nombres son de contrato, no de marketing.
SEMANTICS = (
    "existence",
    "destructive",
    "creation",
    "modification",
    "analysis",
    "transformation",
    "comparison",
    "analysis-multi",
    "query",
)

#: Pistas de que el objetivo pide CONTENIDO, no existencia. "dime qué contiene" no es
#: "comprueba que existe": son dos preguntas distintas y van por caminos distintos.
_ANALYSIS_TERMS = (
    "analiza", "analizar", "analice", "análisis", "analisis", "resume", "resumir",
    "explica", "explicar", "explícame", "explicame", "describe", "describir",
    "qué contiene", "que contiene", "interpreta", "interpretar",
    "compara", "comparar", "calcula", "calcular", "revisa", "revisar", "investiga",
    "investigar", "sintetiza", "sintetizar", "extrae", "extraer", "busca dentro",
    "analyze", "summarize", "explain", "describe", "read and tell",
    "what does it contain", "inspecciona", "inspeccionar", "estudia", "estudiar",
    # P0 §11: traducir o extraer de un fichero pide su CONTENIDO, aunque no se use la
    # palabra "analiza". Sin esto, "traduce notas.txt" se habría leido como existencia.
    "traduce", "traducir", "extrae", "extraer",
    # §11: verbos que RELACIONAN dos ficheros. "compara A con B" no es un análisis de A:
    # es un análisis de A **y** B. Sin esta lista, B desaparecía del contrato y el sistema
    # podía cerrar una comparación sin haber leído nunca el segundo fichero.
    "compara", "comparar", "comparation", "contrasta", "contrastar", "diferencia",
    "equipara", "equiparar",
)

#: Palabras que describen el CONTENIDO pedido dentro de un objetivo de creación. No
#: convierten "crea X con el resumen de Y" en un objetivo de análisis: ahí "resumen" dice
#: QUÉ se escribe, no que haya que analizar. Por eso se listan aparte y se consultan sólo
#: cuando ya se ha decidido que el objetivo es una creación.
_CONTENT_NOUNS = ("resumen", "contenido", "texto", "datos", "copia", "traducción", "traduccion")

#: "…a partir de X", "basado en X", "con un resumen de X": hay una entrada y una salida.
_CHAIN_TERMS = (
    "a partir de", "basado en", "basada en", "con un resumen de", "con la resumen de",
    "con una copia de", "copia de", "traduce", "traducir", "transforma", "transformar",
    "extrae de", "extraído de", "extraido de", "copia", "copiar",
)

#: Pistas de que el objetivo es una consulta, no un artefacto sobre el workspace.
_QUERY_TERMS = (
    "qué puedes hacer", "que puedes hacer", "qué sabes", "que sabes", "quién eres",
    "quien eres", "cómo estás", "como estas", "ayuda", "help",
)

#: Verbos que hacen que la ÚNICA respuesta correcta sea "existe" o "no existe".
_EXISTENCE_VERBS = (
    "comprueba", "comprobar", "compruebe", "verifica", "verificar", "verifique",
    "existe", "existen", "hay", "check",
)


def classify_objective_semantics(text: str, *, path: str | None = None) -> str:
    """La familia semántica del objetivo. Decide QUÉ se puede verificar, no QUÉ se hizo.

    La precedencia es deliberada y la explica cada regla:

    1.BORRADO manda sobre todo: si el objetivo es que algo NO exista, ninguna otra
      lectura es posible.
    2. EXISTENCIA: la pregunta ES si existe. Se exige que NO pida contenido, porque
      "comprueba si el informe existe" es existencia y "comprueba qué dice el informe" no.
    3. TRANSFORMACIÓN: dos rutas y una relación entre ellas. Va antes que creación porque
      "crea Y con un resumen de X" tiene dos rutas y es una cadena, no una escritura aislada.
    4. CREACIÓN / 5. MODIFICACIÓN: el artefacto es el objetivo, así que su contenido es
      exigible. Un verbo de creación gana a un sustantivo de contenido.
    6. ANÁLISIS: una sola ruta y se pide su contenido.
    7. CONSULTA: no hay artefacto que comprobar. Hoy no es verificable, y se dice.

    NUNCA degrada: si la familia no tiene predicado que la sostenga, devuelve una lista
    vacía y la misión queda en `insufficient_evidence`, que es un estado honesto.
    """
    low = _norm(text)
    if not low:
        return "query"

    paths = _all_safe_paths(text)
    if path is not None and path not in paths:
        paths = [path, *paths]

    # 1. Borrado.
    if classify_objective_intent(text) == "destructive" or _is_negated(text):
        return "destructive"
    if any(term in low for term in _DELETE_TERMS):
        return "destructive"

    wants_content = _mentions_analysis(low)

    # 2. Existencia: se pregunta por la existencia y NO por el contenido.
    if not wants_content and any(
        re.search(rf"\b{verb}\b", low) for verb in _EXISTENCE_VERBS
    ):
        return "existence"

    # 3. Cadena entrada→salida: dos rutas y una relación que las une.
    #    Sólo cuando el objetivo nombra explícitamente una producción o una relación entre
    #    las dos. No basta con que pida contenido: "analiza A y B" pide contenido de las
    #    dos y NO es una transformación — no hay salida que producir. Confundirlas
    #    obligaba a escribir un fichero que el objetivo no pidió.
    has_chain = any(term in low for term in _CHAIN_TERMS)
    wants_relationship = _mentions_relationship(low)
    produces_output = any(term in low for term in _CREATE_TERMS)
    # Un verbo que CONVIERTE ("traduce", "copia", "extrae", "transforma") declara que la
    # segunda ruta es una SALIDA: existe para producirla. Eso lo distingue de "analiza A y
    # B", donde las dos rutas son de entrada y no hay nada que escribir.
    converts = any(term in low for term in _CONVERT_TERMS)
    if len(paths) >= 2 and has_chain and (produces_output or converts):
        return "transformation"
    if len(paths) >= 2 and wants_relationship and not produces_output:
        return "comparison"
    if len(paths) >= 2 and produces_output and any(
        term in low for term in _EDIT_TERMS
    ):
        return "transformation"
    # Dos rutas, se pide contenido de las dos y no hay verbo de creación: es un análisis
    # de las dos ("analiza A y B"), no una transformación.
    if len(paths) >= 2 and wants_content and not produces_output:
        return "analysis-multi"

    # 3-bis. Comparación: dos rutas que se RELACIONAN, sin que ninguna sea la salida.
    # "compara A con B" no transforma nada: exige observar LAS DOS. Es una familia propia
    # porque su contrato no puede reducirse al de un análisis de un solo fichero: si sólo
    # se leyera A, la comparación no se habría hecho.
    if len(paths) >= 2 and _mentions_relationship(low):
        return "comparison"

    # 4. Creación: el verbo manda sobre el sustantivo ("con el resumen de" describe QUÉ
    # se escribe, no pide un análisis).
    if any(term in low for term in _CREATE_TERMS):
        return "creation"

    # 5. Modificación.
    if any(term in low for term in _EDIT_TERMS):
        return "modification"

    # 6. ANÁLISIS vs LECTURA: son preguntas distintas y sus contratos también.
    #    "analiza notas.txt" pide el CONTENIDO: hay que leerlo y concluir algo sobre él.
    #    "lee notas.txt" pide OBSERVAR: basta una lectura, sin etapa de síntesis. Tratarlas
    #    como la misma añadía un paso `analyze` que el objetivo no pedía, y el plan dejaba
    #    de corresponder a lo solicitado.
    if wants_content and paths:
        return "analysis"

    # 6-bis. Lectura pura: el objetivo nombra una ruta y no pide contenido, análisis ni
    # comparación. Su contrato sigue siendo `content_observed` —hay que LEERLA para poder
    # usarla—, pero la FORMA del plan es una sola observación, sin síntesis. El plan tiene
    # que hacer lo que se le pidió, ni un paso más.
    if paths and classify_objective_intent(text) == "read":
        return "read"

    # 7. Consulta.
    return "query"


def _mentions_analysis(low: str) -> bool:
    return any(term in low for term in _ANALYSIS_TERMS)


#: Verbos que relacionan dos recursos sin que uno sea la salida del otro.
#: Verbos que declaran una CONVERSIÓN: la segunda ruta existe para producirla.
_CONVERT_TERMS = (
    "traduce", "traducir", "copia", "copiar", "extrae", "extraer",
    "transforma", "transformar", "resume a", "convertir", "convierte",
)

_RELATIONSHIP_TERMS = (
    "compara", "comparar", "comparation", "contrasta", "contrastar", "diferencia",
    "diferencias", "equipara", "equiparar", "frente a", "contra", "versus", " vs ",
)


def _mentions_relationship(low: str) -> bool:
    return any(term in low for term in _RELATIONSHIP_TERMS)


def _all_safe_paths(text: str) -> list[str]:
    """Todas las rutas del workspace citadas, en orden y sin inventar nombres.

    Delega la admitcion en `_safe_path` para cada coincidencia: así hay UNA sola regla de
    admisión y el extractor multi-ruta no puede divergir del simple. Esa duplicación ya
    costó un bug: la versión múltiple no limpiaba la puntuación de la pregunta y
    "¿existe datos.txt?" se quedaba sin ruta, mientras la simple la aceptaba.
    """
    raw = text or ""
    found: list[str] = []
    for match in _PATH_TOKEN.finditer(raw):
        candidate = match.group(0)
        if not candidate or candidate in found:
            continue
        if _admits_path(raw, match):
            found.append(candidate)
    return found


def _output_path(text: str, paths: list[str]) -> str:
    """Cuál de las rutas es la que el objetivo PRODUCE.

    Dos formas, en orden:

    1. Un verbo de CREACIÓN pegado a la ruta —"crea salida.txt con un resumen de
       notas.txt"—: la ruta que sigue al verbo es la salida, y la otra es la entrada.
    2. Sin verbo de creación —"copia notas.txt en copia.txt", "traduce A en B"—: en una
       cadena el destino es la ÚLTIMA ruta y el origen la primera. Aquí está la razón de
       no tratar "copia" como creación: en "copia notas.txt en copia.txt" el verbo
       precede al ORIGEN, y tomarlo por el nombre de la salida habría pedido crear
       notas.txt — la operación exactamente inversa a la pedida.

    Sin esta distinción "traduce A en B" invertía entrada y salida, y el sistema habría
    exigido leer B y haber creado A: el criterio describía justo lo contrario.
    """
    low = text or ""
    for term in _CREATE_TERMS:
        index = low.find(term)
        if index == -1:
            continue
        for path in paths:
            position = low.find(path)
            if index < position <= index + 30:
                return path
    return paths[-1] if paths else ""


def criteria_for_objective(text: str) -> list[str]:
    """Criterios que el objetivo SOSTIENE, según su familia semántica.

    Devuelve una lista vacía cuando el objetivo no nombra un artefacto comprobable. Es
    preferible devolver vacío a devolver un predicado que no mide lo pedido: una lista
    vacía deja la misión en `insufficient_evidence`, que es un estado honesto.
    """
    paths = _all_safe_paths(text)
    semantics = classify_objective_semantics(text)

    if semantics == "destructive":
        if not paths:
            return []
        return [f"file_missing:{paths[0]}"]

    if semantics == "existence":
        if not paths:
            return []
        return [f"file_exists:{paths[0]}"]

    if semantics == "creation":
        if not paths:
            return []
        target = paths[0]
        # El objetivo de una creación ES el artefacto con contenido: un fichero vacío no
        # es "crear el archivo con el resumen", es no haberlo hecho. Por eso el tamaño
        # mínimo es parte del contrato, no un extra opcional.
        return [f"file_exists:{target}", f"file_size_at_least:{target}:1"]

    if semantics == "modification":
        if not paths:
            return []
        return [f"file_exists:{paths[0]}"]

    if semantics == "read":
        if not paths:
            return []
        # Leer un fichero exige haber leído su CONTENIDO, no sólo que exista: por eso el
        # criterio es `content_observed` y no `file_exists`. La diferencia con `analysis`
        # está en la FORMA del plan, no en lo que hay que comprobar.
        return [f"content_observed:{paths[0]}"]

    if semantics in ("analysis", "analysis-multi"):
        if not paths:
            return []
        # Éste es el cambio que cierra el gap: contenido observado, NO existencia. Con
        # varias rutas se exigen todas: "analiza A y B" no se cumple leyendo sólo A.
        return [f"content_observed:{path}" for path in paths[:MAX_CRITERIA]]

    if semantics == "comparison":
        # Comparar A con B exige haber leído A Y B. El contrato lo dice: si sólo se
        # cumple una de las dos, la comparación no se ha hecho.
        return [f"content_observed:{path}" for path in paths[:MAX_CRITERIA]]

    if semantics == "transformation":
        if len(paths) < 2:
            return [f"content_observed:{paths[0]}"] if paths else []
        target = _output_path(text, paths)
        source = next((p for p in paths if p != target), paths[0])
        return [
            # Se leyó la entrada...
            f"content_observed:{source}",
            # ...y se produjo una salida con contenido.
            f"file_exists:{target}",
            f"file_size_at_least:{target}:1",
        ]

    return []


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

    # P0 §11: contiene una ruta del workspace. Lo que decide el predicado es la
    # SEMÁNTICA del criterio, no la mera presencia de la ruta.
    path = _safe_path(raw)
    if path is not None:
        derived = criteria_for_objective(raw)
        if derived:
            return derived[0], "canonical"
        return None, "unverifiable"

    # Regla J: nada que comprobar, pero el criterio del modelo no se pierde.
    return None, "unverifiable"


def _derive_from_utterance(utterance: str, objective: str) -> list[str]:
    """Criterios deterministas desde el texto del usuario (P0 §11 + reglas H).

    Prioriza el `utterance` ORIGINAL sobre el `objective` del modelo: el modelo puede
    parafrasear e inventar o cambiar la ruta, el utterance no (regla H). Si del
    utterance sale algún criterio, no se mira el objective: una ruta que el modelo
    inventó no puede colarse en el contrato.

    P0 §11: lo que decide QUÉ se verifica es la SEMÁNTICA del objetivo, no que el texto
    nombre una ruta. Se construye con `criteria_for_objective`, que clasifica la familia
    y devuelve el contrato de esa familia. Los predicados de suite (reglas F/G) siguen
    teniendo prioridad: hablan de la suite y no de una ruta.
    """
    for text in (utterance, objective):
        if not (text or "").strip():
            continue
        intent = classify_objective_intent(text)
        criteria: list[str] = []

        tests = _tests_predicate(text)
        if tests is not None:
            criteria.append(tests)

        # P0 §11: la semántica del objetivo decide el contrato. Una ruta sola NO basta
        # para derivar `file_exists`.
        for derived in criteria_for_objective(text):
            if derived not in criteria:
                criteria.append(derived)

        if criteria:
            return criteria[:MAX_CRITERIA]
    return []


def all_objective_paths(text: str) -> list[str]:
    """Rutas que el objetivo nombra, validadas y en orden. API compartida.

    El planner la consume para no tener su propia idea de qué es un "target": si el plan
    y el contrato seExtracen por rutas distintas, la misión se contradice (§11).
    """
    return _all_safe_paths(text)


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

    # P0 §11: la semántica del objetivo se consulta SIEMPRE, no sólo cuando el modelo no
    # proposed nada. Antes, si el modelo proponía un criterio trivial —"El archivo
    # notas.txt existe"— para un objetivo de análisis, ese criterio se aceptaba y el
    # fuerte nunca se derivaba: el modelo podía degradar su propio contrato y el
    # objetivo quedaba saldado con la mitad de lo que se le pidió.
    #
    # Los predicados EXPUESTOS por el modelo se conservan tal cual (regla A): si el
    # objetivo trae `file_exists:X` escrito, nadie lo reemplaza. Lo que se añade es el
    # criterio que la semántica del objetivo exige y el modelo no propuso — nunca al
    # revés, y nunca un criterio más débil.
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
    "SEMANTICS",
    "all_objective_paths",
    "canonicalize",
    "classify_objective_semantics",
    "criteria_for_objective",
    "normalize_criteria",
]
