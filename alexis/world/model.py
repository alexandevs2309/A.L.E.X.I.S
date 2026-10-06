"""World Model operacional: lo que ALEXIS sabe del mundo en el que actúa.

No es un almacén: existe para que el runtime **decida con conocimiento del mundo**, no
solo con el texto del objetivo. Por eso tiene tres capacidades:

1. **Observación**: `observe_execution()` extrae entidades de lo que una herramienta
   devolvió de verdad (un `fs.stat` que dice `exists=false` es evidencia de que el
   archivo no existe, no una suposición).
2. **Consulta**: `query()`, `for_objective()`, `neighbors()`, `known_path()`.
3. **Relaciones**: `relate()` + `neighbors()` para el grafo de dependencias.

Cada entidad conserva su `source` y cuántas veces se ha observado. Una entidad
construida desde la salida de una herramienta es EVIDENCIA, no un hecho verificado:
por eso `confidence` es 0.7 y no 1.0, y por eso el runtime la usa para **preguntar o
corregir**, no para afirmar.

El API previo (`upsert`/`get`/`snapshot`) se conserva: el demo ya lo usa.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from alexis.memory.provider import terms

FILE = "file"
TOOL = "tool"
SERVICE = "service"
PROJECT = "project"
SYSTEM = "system"
TASK = "task"
RESOURCE = "resource"
DEPENDENCY = "dependency"
TEST = "test"

#: Capabilities que fallan de forma previsible si el mundo ya observó que la ruta no
#: existe. `fs.write` NO está: crear lo que falta es exactamente su trabajo.
_FAILS_ON_MISSING = {"fs.read", "fs.stat", "research.filesystem", "fs.remove"}


#: Identidad del ámbito cuando no se declara ninguno. Es explícita, no accidental: las
#: entidades que entren por esta vía son tan globales como el mundo del proceso, que es
#: justo lo que #4.3 tendrá que acotar.
IMPLICIT_SCOPE = "default"

#: `last_seen` de una entidad cuya fila no lo traía (anterior a §4.2).
#:
#: Deliberadamente NO es `time.time()`. Una fila legacy no tiene edad conocida; ponerle
#: "ahora" sería inventar que se acaba de observar, y un conocimiento viejo con fecha de
#: hoy es exactamente el fallo que §4.5 (staleness) tiene que poder detectar. Con este
#: valor, "no sé cuándo se vio" queda DISTINGUIBLE de "lo vi hace un segundo", y el
#: Portanto quien lo lea puede exigir re-observación en vez de confiar.
LAST_SEEN_UNKNOWN = 0.0

#: Cuánto se cree una observación de AUSENCIA (P0 §4.5, parcial).
#:
#: Es un plazo corto a propósito. La observación existe para no repetir un fallo que se
#: acaba de producir; a partir de un par de minutos, lo que el usuario quiere puede haber
#: cambiado. Ante la duda, la checker de la herramienta decide: preguntar al usuario
#: interrumpe su autonomía, un `fs.stat` devuelve la verdad.
#:
#: NO caducan las presencias: una presencia vieja sólo provoca un fallo honesto y barato si
#: el archivo ya no está. Ver `is_absence_fresh()` para el argumento completo.
ABSENCE_TTL_S = 120.0


@dataclass(frozen=True)
class Scope:
    """La identidad del ámbito en el que una entidad del mundo significa algo.

    Existe por una razón concreta: las herramientas de filesystem devuelven rutas
    **relativas al workspace** (`p.relative_to(root)`), así que `notas.txt` en el proyecto
    A y `notas.txt` en el proyecto B son, para el path, la misma cadena. Sin un ámbito en
    la identidad, un World Model compartido —que es lo que hará #4.3— afirmaría como
    hechos del proyecto A cosas observadas en el B.

    ## Qué es y qué no es parte de la identidad

    Sólo `id` entra en la clave. `label` y `root` son informative: renombrar el directorio
    no debe partir el conocimiento en dos, y mover el proyecto sí, porque su `id` se deriva
    de la ruta resuelta.

    ## La representación todavía puede cambiar

    `id` es hoy un hash corto de la ruta resuelta. Es una REPRESENTACIÓN, no un contrato:
    el único sitio que la produce es `from_workspace()`, así que #4.3 puede adoptar otra
    forma (una ruta legible, un id de proyecto explícito) sin tocar ni el resto del
    WorldModel ni las filas ya guardadas, que llevan el `scope` que se les dio.

    No se deduce de variables de entorno ni de estado global: el ámbito se pasa
    explícitamente al construir el `WorldModel`, o en la llamada.
    """

    id: str
    kind: str = "workspace"
    label: str = ""
    root: str = ""

    @classmethod
    def from_workspace(cls, path: str | Path) -> "Scope":
        """Ámbito de un workspace, derivado de su ruta ABSOLUTA resuelta.

        Se resuelve antes de hashear: `/tmp/a` y `/tmp/a/` deben ser el mismo ámbito, y
        un symlink y su destino también. Determinista: la misma ruta da siempre el mismo
        `id`, en cualquier proceso y en cualquier máquina.
        """
        resolved = str(Path(path).expanduser().resolve())
        return cls(
            id=hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12],
            kind="workspace",
            label=Path(resolved).name or resolved,
            root=resolved,
        )

    @classmethod
    def implicit(cls) -> "Scope":
        """El ámbito por defecto: el mundo global del proceso, sin proyecto acotado."""
        return cls(id=IMPLICIT_SCOPE, kind="implicit", label="")

    @classmethod
    def coerce(cls, value: "Scope | str | Mapping[str, Any] | None") -> "Scope":
        """Acepta `Scope`, un id suelto, un mapping serializado o `None`.

        Un string se interpreta como un `id` ya calculado, para que un `scope` serializado
        pueda volver tal cual sin volver a hashearlo. Un mapping se reconstruye con todos sus
        campos, que es lo que hará #4.3 al rehidratar de un store.
        """
        if value is None:
            return cls.implicit()
        if isinstance(value, Scope):
            return value
        if isinstance(value, Mapping):
            data = dict(value)
            scope_id = str(data.get("id") or "").strip()
            if not scope_id or scope_id == IMPLICIT_SCOPE:
                return cls.implicit()
            return cls(id=scope_id,
                       kind=str(data.get("kind") or "workspace"),
                       label=str(data.get("label") or ""),
                       root=str(data.get("root") or ""))
        text = str(value).strip()
        if not text or text == IMPLICIT_SCOPE:
            return cls.implicit()
        return cls(id=text)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "label": self.label, "root": self.root}


import json
import math

#: Base de confianza por procedencia. Es la confianza de UNA observación, no un techo.
#: Un hecho declarado no vale más que lo que vale verlo con una tool: vale lo que vale
#: su ORIGEN, y luego lo que lo corrobore.
CONFIDENCE_BY_SOURCE = {
    "tool": 0.7,          # observado por una herramienta
    "test": 0.9,          # salida de una suite ejecutada
    "registry": 1.0,      # declarado por el catálogo
    "declared": 1.0,      # declarado a mano
    "recovered": 0.0,     # sin confianza: no se sabe de dónde viene
}

#: Tope de confianza por observación corroborada. No llega a 1.0 nunca: ver algo una vez
#: y verlo cuatro no es lo mismo que haberlo PROBADO, y el GoalVerifier tiene su propio
#: camino para eso. El mundo habla de lo que ha visto, no de certeza.
CONFIDENCE_CEILING = 0.99

#: Cuánto pesa una contradicción frente a un apoyo. Una contradicción vale más que un
#: apoyo: conseguir que algo diga lo contrario cuesta más que que lo repita, así que debe
#: mover más la aguja.
CONTRADICTION_WEIGHT = 1.5

#: Rango de procedencia, para desempatar de forma determinista. Mayor = más fiable.
#: Sólo se usa para desempatar: el valor vigente NO depende de esto salvo que el número
#: de apoyos sea exactamente igual.
_SOURCE_RANK = {
    "registry": 4, "declared": 4, "test": 3, "tool": 2, "recovered": 1,
}


def _normalize_bucket(vals: dict) -> dict:
    """Deja cada valor como `{"n": int, "rank": int}`.

    Tolera las filas escritas antes de que existiera `rank` (que eran `{"true": 1}`), así
    que una base con datos de §4.5.2 no necesita migración: se leen igual.
    """
    out: dict[str, dict] = {}
    for clave, dato in (vals or {}).items():
        if isinstance(dato, dict):
            out[str(clave)] = {"n": int(dato.get("n") or 0),
                               "rank": int(dato.get("rank") or 0),
                               "seen": float(dato.get("seen") or 0.0)}
        else:
            out[str(clave)] = {"n": int(dato or 0), "rank": 0, "seen": 0.0}
    return out


def _merge_value_counts(previous: dict, incoming: dict, rank: int = 0,
                        seen: float = 0.0) -> dict:
    """Suma frecuencias de valores. Conmutativa: A+B == B+A.

    Cada valor guarda cuántas veces se vio Y la procedencia más fiable con que se vio. El
    `rank` es un máximo, también conmutativo, así que el determinismo no depende de que el
    orden sea estable.

    Es una copia profunda a propósito: el `previous` no se toca, para que la entidad que
    estaba en el modelo no cambie por debajo cuando otra la referencia.
    """
    conteo = {attr: _normalize_bucket(vals) for attr, vals in (previous or {}).items()}
    for atributo, valor in (incoming or {}).items():
        bucket = conteo.setdefault(atributo, {})
        clave = value_key(valor)
        actual = bucket.get(clave) or {"n": 0, "rank": 0, "seen": 0.0}
        bucket[clave] = {
            "n": actual["n"] + 1,
            "rank": max(actual["rank"], int(rank)),
            # El INSTANTE más reciente en que se vio este valor. Es un máximo, así que
            # sigue siendo conmutativo: depende de la observación, no de cuándo llegó.
            "seen": max(actual.get("seen", 0.0), float(seen)),
        }
    return conteo


def resolve_vigente(conteo: dict) -> dict:
    """Valor vigente de cada atributo. P0 §4.5.3. Determinista y sin LLM.

    ## Qué es un conflicto

    Un atributo se ha visto con más de un valor distinto. Eso es todo: no hace falta que
    las observaciones vinieran de fuentes distintas ni que una sea más antigua. "El mundo
    me dice dos cosas sobre lo mismo" es un conflicto por sí mismo.

    ## Cómo se decide el vigente

    Tres reglas, en este orden:

    ## Por qué manda la RECENCIA y no la frecuencia

    Un primer intento de esta regla era "gana la frecuencia". Es un error, y el test de
    borrar un archivo lo cazó: `fs.stat` ve el archivo, `verify` lo vuelve a ver y
    `fs.remove` lo borra. Queda `exists=True` DOS veces y `exists=False` UNA, así que la
    mayoría gana y el mundo afirma que el archivo existe DESPUÉS de haberlo borrado.

    El motivo de fondo es que los atributos de un mundo son MUTABLES: el estado actual es
    el último observado, y la frecuencia dice cuántos lo respaldan, no si es el presente.
    Confundir ambas cosas produce un mundo que se contradice a sí mismo.

    ## Las reglas, en orden

    1. **Gana la observación más reciente.** Se usa el `seen` de cada valor, que es un
       MÁXIMO sobre las observaciones de ese valor. Al ser un máximo depende del conjunto
       de observaciones y NO del momento en que llegaron, así que el determinismo se
       conserva: es conmutativo como todo máximo.
    2. **Empate de fecha -> la más frecuente.** Sólo decide si dos valores se vieron por
       última vez exactamente en el mismo instante.
    3. **Empate total -> la procedencia más fiable.** Mayor `rank`
       (registry > test > tool > recovered). Es el mismo criterio que usa la base de
       confianza, así que el sistema razona igual sobre "de quién es esto" y "cuánto
       confío".
    4. **Empate total -> orden de la clave.** Como último recurso, la clave del valor
       ordena alfabéticamente. Es arbitrario pero FIJO: dos ejecuciones con los mismos
       datos dan el mismo resultado, que es lo único que se le pide a un desempate.

    ## Por qué no decide el LLM

    Un modelo puede elegir razonablemente, pero no de forma reproducible, y su criterio
    cambiaría con el prompt. Aquí la contradicción se resuelve con una regla del World
    Model. El LLM puede proponer una OBSERVACIÓN; nunca puede decidir cuál es la verdad.
    La autoridad sigue estando en Policy; esto es conocimiento, no permiso.
    """
    vigente: dict = {}
    for atributo, valores in (conteo or {}).items():
        if not valores:
            continue
        if len(valores) == 1:
            clave = next(iter(valores))
            vigente[atributo] = _decode_value(clave)
            continue
        mejor = sorted(
            valores.items(),
            key=lambda par: (-par[1].get("seen", 0.0), -par[1]["n"], -par[1]["rank"], par[0]),
        )[0]
        vigente[atributo] = _decode_value(mejor[0])
    return vigente


def _decode_value(clave: str) -> Any:
    """Inverso de `value_key`. Si la clave no es JSON, se devuelve tal cual."""
    try:
        return json.loads(clave)
    except (ValueError, TypeError):
        return clave


def build_conflicts(conteo: dict) -> list[dict]:
    """Registro de TODAS las contradicciones vistas, sin silenciar ninguna. §4.5.3.

    Un atributo con más de un valor genera una entrada por cada valor, con su recuento y
    la procedencia más fiable con que se vio. Aunque el vigente cambie, las entradas
    permanecen: una contradicción que se borra al resolverse es una contradicción
    silenciada, que es justo lo que el requisito prohíbe.
    """
    registros: list[dict] = []
    for atributo, valores in sorted((conteo or {}).items()):
        if len(valores) < 2:
            continue
        for clave, dato in sorted(valores.items(), key=lambda par: par[0]):
            registros.append({
                "attribute": atributo,
                "value": _decode_value(clave),
                "observations": int(dato["n"]),
                "best_source_rank": int(dato["rank"]),
                # `last_observed` es el MÁXIMO de los instantes en que se vio ESE valor,
                # y por tanto conmutativo. No se guarda el `last_seen` de la entidad: ese
                # es el de la observación que llegó última, y haría que el registro
                # dependiera del orden de llegada.
                "last_observed": float(dato.get("seen", 0.0) or 0.0),
            })
    return registros


def _tally(conteo: dict) -> tuple[int, int]:
    """`(apoyo, contradicciones)` del conjunto entero de observaciones.

    Para cada atributo, el valor MÁS FRECUENTO es el vigente y cuenta como apoyo; el resto
    de valores son contradicciones. Sin rango mínimo: si un valor no se ha visto, no se
    cuenta como apoyo ni como contradicción.
    """
    apoyo = 0
    contra = 0
    for valores in (conteo or {}).values():
        if not valores:
            continue
        mayor = max(v["n"] for v in valores.values())
        ganadores = [v for v in valores.values() if v["n"] == mayor]
        if len(ganadores) > 1:
            # Empate: no hay valor mayoritario, luego nada apoya y todo queda en
            # disputa. Ver el porqué en el docstring.
            contra += sum(v["n"] for v in valores.values())
            continue
        for v in valores.values():
            if v["n"] == mayor:
                apoyo += v["n"]
            else:
                contra += v["n"]
    return apoyo, contra


def value_key(value: Any) -> str:
    """Clave estable y reversible de un valor de atributo.

    `json.dumps` con `sort_keys` hace que la clave no dependa del orden de las claves de
    un dict, y `loads` la devuelve intacta. Sin esto, `{exists: True}` y `{exists: "True"}`
    — o dos claves con distinto orden — serían o no el mismo valor.
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _merge_evidence_ids(previous: WorldEntity, current: WorldEntity) -> list:
    """Une los ids de evidencia sin duplicar y sin depender del orden (P0 §4.5.4).

    Es un conjunto ordenado: el mismo conjunto de evidencias produce la misma lista llegue
    como llegue, que es lo que exige el determinismo de §4.5.5.
    """
    return sorted({*(previous.evidence_ids or []), *(current.evidence_ids or [])})


def _base_confidence(source: str) -> float:
    """Confianza de una observación según de dónde viene. Determinista y explicable."""
    text = (source or "").strip().lower()
    if ":" in text:
        text = text.split(":", 1)[0]
    return CONFIDENCE_BY_SOURCE.get(text, 0.5)


def source_rank(source: str) -> int:
    """Fiabilidad de una procedencia. Sólo desempata; no decide por sí sola."""
    text = (source or "").strip().lower()
    if ":" in text:
        text = text.split(":", 1)[0]
    return _SOURCE_RANK.get(text, 0)


def evolved_confidence(base: float, support: int, contradictions: int) -> float:
    """Confianza como FUNCIÓN PURA de los contadores. P0 §4.5.2.

    ## Por qué pura y no acumulativa

    La forma intuitiva —`conf += 0.1` por cada corroboración— depende del ORDEN: el mismo
    conjunto de observaciones daría valores distintos según llegaran en un orden u otro, y
    dos máquinas con la misma evidencia discreparían. Aquí no hay memoria del valor
    anterior: `confidence` se recalcula desde cero a partir de (base, support,
    contradictions). Como contar es conmutativo, el resultado final depende del CONJUNTO y
    no de la secuencia. Ése es el determinismo que pide el requisito.

    ## Las cuatro respuestas, en una función

    - **cómo nace**: una observación sin corroborar vale exactamente su BASE, la de su
      origen. Una tool que ve algo una vez → 0.7. Ni más ni menos.
    - **cómo sube con evidencia**: cada apoyo posterior la acerca al techo
      `CONFIDENCE_CEILING` (0.99), con rendimientos decrecientes: los primeros apoyos valen
      mucho y a partir de ahí cada uno aporta menos. Nunca llega a 1.0 porque ver algo
      repetido no es haberlo PROBADO, y el GoalVerifier tiene su propio camino para eso.
    - **qué pasa con contradicción**: una contradicción pesa más que un apoyo
      (`CONTRADICTION_WEIGHT`), así que puede bajar el hecho por debajo de una observación
      sola. Un 1-1 es genuinamente una incógnita y sale 0.1: hubo observaciones, sólo que
      se contradicen, y eso no es ausencia de conocimiento.
    - **qué pasa con nada**: sin apoyos ni contradicciones no hay nada que afirmar → 0.0.
    """
    base = min(max(float(base), 0.0), 1.0)
    contradictions = max(0, int(contradictions))
    # Se resta 1: la PRIMERA observación no corrobora nada, así que vale lo que vale su
    # fuente. Sólo a partir de la segunda hay corroboración que sumar.
    corroborated = max(0.0, float(support) - 1.0)
    # El techo nunca puede quedar POR DEBAJO de la base: corroborar algo que una fuente
    # fiable declaró no puede rebajarlo. Un hecho de catálogo corroborado sigue siendo 1.0.
    ceiling = max(CONFIDENCE_CEILING, base)

    if contradictions <= 0:
        if corroborated <= 0.0:
            return base
    else:
        effective = corroborated - CONTRADICTION_WEIGHT * contradictions
        if effective <= 0.0:
            return 0.1
        corroborated = effective

    # 1 - exp(-0.5 * n): n=1 -> 0.39, n=3 -> 0.78, n=9 -> 0.99. Saturante, y sin
    # parámetros que haya que calibrar contra nada externo.
    growth = 1.0 - math.exp(-0.5 * corroborated)
    return min(base + (ceiling - base) * growth, ceiling)

@dataclass
class WorldEntity:
    id: str
    kind: str
    name: str
    attributes: dict = field(default_factory=dict)
    source: str = "declared"
    confidence: float = 1.0
    mission_id: str | None = None
    observations: int = 1
    last_seen: float = field(default_factory=time.time)
    #: Ámbito al que pertenece la entidad. Va AL FINAL a propósito: el servidor construye
    #: `WorldEntity(...)` posicionalmente y ese orden es parte de la API pública.
    scope: str = ""
    # ── P0 §4.5.2: contadores que hacen la confianza EVOLUTIVA y DETERMINISTA ──
    #: Base de confianza = la de la procedencia MÁS FIABLE que ha observado esto, como
    #: MÁXIMO de todas las vistas. El máximo es conmutativo, así que el resultado no
    #: depende del orden de llegada. Sin este campo, la base sería la de la última
    #: observación y dos máquinas con la misma evidencia darían distinta confianza.
    base_confidence: float = 0.0
    #: Observaciones que APOYAN el estado vigente. Empieza en 1: la observación que creó
    #: la entidad es un apoyo. Lo cuenta la observación, no la repetición: una tool que
    #: dice lo mismo diez veces sigue siendo un dato, no diez datos.
    support: int = 1
    #: Observaciones que CONTRADICEN el estado vigente.
    contradictions: int = 0
    #: Ids de los claims que respaldan esta entidad (§4.5.4). Los crea el mismo
    #: `ExecutionResult` que la observación, así que el enlace existe en los datos.
    evidence_ids: list = field(default_factory=list)
    #: Cuántas veces se ha visto CADA VALOR de cada atributo: `{atributo: {valor: n}}`.
    #:
    #: Esto es lo que hace la confianza INDEPENDIENTE DEL ORDEN. Comparar cada
    #: observación con el valor "actual" parece más sencillo pero es secuencial: con
    #: (True, False, True) da dos contradicciones y con (False, True, True) da una, para
    #: el mismo conjunto de observaciones. Contar FRECUENCIAS de cada valor es
    #: conmutativo, así que el resultado depende del conjunto y no de la secuencia, y de
    #: aquí salen el estado vigente y el recuento de contradicciones.
    value_counts: dict = field(default_factory=dict)
    #: Contradicciones REALES que se han visto sobre esta entidad (§4.5.3). Se conservan
    #: aunque el estado vigente ya no sea el de la otra parte: una contradicción que se
    #: borra es una contradicción silenciada.
    conflicts: list = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "attributes": dict(self.attributes),
            "source": self.source,
            "confidence": self.confidence,
            "mission_id": self.mission_id,
            "observations": self.observations,
            "scope": self.scope,
            # §4.5.2/.3/.4: contadores, conflictos y procedencia también viajan. Si no,
            # al restaurar una entidad volvería a nacer con confianza de primera
            # observación y sin rastro de que hubo contradicciones.
            "base_confidence": self.base_confidence,
            "support": self.support,
            "contradictions": self.contradictions,
            "evidence_ids": list(self.evidence_ids),
            "value_counts": {a: dict(v) for a, v in (self.value_counts or {}).items()},
            "conflicts": [dict(c) for c in (self.conflicts or [])],
            # §4.2: sin esto, la edad real de la observación se perdía al serializar y al
            # restaurar la entidad parecía recién observada. El float viaja tal cual: se
            # comprobó que un float64 sobrevive al viaje por JSONB sin perder un bit.
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "WorldEntity":
        """Rehidrata una entidad desde su fila serializada.

        Punto único de rehidratación del World Model. Tolera filas legacy: si `last_seen`
        no está —o no es un número— se usa `LAST_SEEN_UNKNOWN`, nunca la hora actual. Ver
        la constante para el porqué; la regla es que **no se inventa un presente**.
        """
        data = dict(row or {})
        raw_seen = data.get("last_seen")
        # `bool` es subclase de `int` en Python: sin este aviso, `True` se aceptaría como
        # 1.0, es decir, "observado en 1970". Un booleano no es una marca de tiempo.
        if isinstance(raw_seen, bool) or raw_seen is None:
            last_seen = LAST_SEEN_UNKNOWN
        else:
            try:
                last_seen = float(raw_seen)
            except (TypeError, ValueError):
                last_seen = LAST_SEEN_UNKNOWN
        if last_seen <= 0.0:
            # Cero, negativo o ausente: edad desconocida, no "observado en el epoch".
            last_seen = LAST_SEEN_UNKNOWN
        atributos = dict(data.get("attributes") or {})
        source = str(data.get("source") or "declared")
        confianza = float(data.get("confidence") or 0.0)
        conteos = {
            str(a): _normalize_bucket(v)
            for a, v in (data.get("value_counts") or {}).items()
        }
        # ── Reparación de filas anteriores a §4.5 ──────────────────────────────
        # Esas filas no tienen `value_counts` (la columna nace con `{}`) ni
        # `base_confidence` (nace con 0). Sin repararlas, la PRIMERA observación nueva
        # que llegara después de migrar fusionaría contra un historial vacío: el conteo
        # anterior se perdía y la base pasaba a ser sólo la de esa observación, borrando
        # de hecho la confianza que la fila sí tenía guardada.
        #
        # Lo que se puede reconstruir sin inventar nada es el estado vigente: hay al
        # menos una observación, y la marca de tiempo es la de la entidad.
        if not conteos and atributos:
            conteos = _merge_value_counts(
                {}, atributos, source_rank(source), last_seen)
        # La base es la confianza que la fuente tenía; es la misma regla con la que §4.3
        # generó el `confidence` de esa fila, así que se recupera en vez de inventarse.
        # `confidence` NO se toca: rehidratar no observa, y aquí no se reescribe nada.
        base = float(data.get("base_confidence") or 0.0)
        if base <= 0.0 and confianza > 0.0:
            base = _base_confidence(source)
        return cls(
            id=str(data.get("id") or ""),
            kind=str(data.get("kind") or "unknown"),
            name=str(data.get("name") or data.get("id") or ""),
            attributes=atributos,
            source=source,
            confidence=confianza,
            mission_id=data.get("mission_id"),
            observations=int(data.get("observations") or 0),
            last_seen=last_seen,
            scope=str(data.get("scope") or ""),
            base_confidence=base,
            support=int(data.get("support") or 0) or 1,
            contradictions=int(data.get("contradictions") or 0),
            evidence_ids=list(data.get("evidence_ids") or []),
            # `_normalize_bucket` deja cada valor como {"n":..,"rank":..} y tolera las
            # filas antiguas que eran un entero suelto.
            value_counts=conteos,
            conflicts=[dict(c) for c in (data.get("conflicts") or []) if isinstance(c, dict)],
        )

    def to_line(self) -> str:
        details = ", ".join(f"{k}={v}" for k, v in sorted(self.attributes.items()) if v is not None)
        return f"{self.kind}:{self.name}" + (f" ({details})" if details else "")


class WorldModel:
    def __init__(self, scope: Scope | str | None = None):
        """`scope` es el ámbito POR DEFECTO de este World Model.

        Se declara aquí, en la composición, y no se deduce de nada global. Dos instancias
        con ámbitos distintos no se ven entre sí; una instancia con un `scope=` distinto en
        la llamada tampoco. Sin `scope`, se usa el ámbito implícito y el comportamiento es
        el de siempre: un único mundo para el proceso.
        """
        self.scope = Scope.coerce(scope)
        # La clave es (ámbito, id). El id solo no identifica: dos proyectos pueden tener
        # `file:notas.txt`.
        self.entities: dict[tuple[str, str], WorldEntity] = {}
        self.edges: list[tuple[str, str, str, str]] = []  # (ámbito, padre, hijo, relación)

    # ------------------------------------------------------------------ #
    # Ámbito
    # ------------------------------------------------------------------ #

    def _scope(self, scope: "Scope | str | None" = None) -> Scope:
        """Resuelve el ámbito de una llamada: el explícito, o el de la instancia."""
        return self.scope if scope is None else Scope.coerce(scope)

    def _key(self, entity_id: str, scope: "Scope | str | None" = None) -> tuple[str, str]:
        return (self._scope(scope).id, str(entity_id))

    def scopes(self) -> list[str]:
        """Ámbitos presentes en el modelo. Vacío si no hay entidades."""
        return sorted({sid for sid, _ in self.entities})

    def put(self, entity: WorldEntity) -> WorldEntity:
        """Inserta o SUSTITUYE, sin fusionar atributos ni contar otra observación.

        `upsert` es la vía de la observación: fusiona y suma. Ésta es la vía de la
        restauración, que debe reponer lo que había tal cual estaba. Es también lo que
        hace el World Model reemplazable por un store persistente en #4.3.
        """
        if not entity.scope:
            entity.scope = self.scope.id
        self.entities[self._key(entity.id, entity.scope)] = entity
        return entity

    # ------------------------------------------------------------------ #
    # API previo (conservado)
    # ------------------------------------------------------------------ #

    def upsert(self, entity: WorldEntity) -> WorldEntity:
        """Registra una observación. Es la ÚNICA vía que evoluciona el estado (§4.5).

        `put()` no pasa por aquí: rehidratar no es observar, así que un restore del
        store NO puede subir la confianza de nada.
        """
        if not entity.scope:
            entity.scope = self.scope.id
        # La clave se calcula con el ámbito de la ENTIDAD, no con el de la instancia: una
        # entidad con `scope` propio se guarda en su ámbito aunque el modelo tenga otro por
        # defecto. Es lo que permite que un solo modelo aloje varios proyectos.
        key = self._key(entity.id, entity.scope)
        previous = self.entities.get(key)
        base = _base_confidence(entity.source)
        if previous is not None:
            # Lo que DICE esta observación, antes de fusionar. Hace falta para poder
            # distinguir "apoya lo que ya sabíamos" de "dice lo contrario".
            dicho = dict(entity.attributes)
            # Instante al que se vio ESTA observación. Se captura antes de tocar
            # `last_seen` más abajo: ese campo pasa a ser el máximo histórico de la
            # entidad, y usarlo aquí atribuiría a un valor el instante de otro hecho.
            observado_en = float(entity.last_seen or 0.0)
            entity.observations = previous.observations + 1
            # `last_seen` es el MÁXIMO de los instantes observados, no el de la
            # observación que llegó última. Con lo segundo, una observación que llega tarde
            # (un reintento, un resultado encolado) rebobina el reloj de la entidad y la
            # haría parecer más fresca de lo que es: justo lo que gobierna la caducidad de
            # §4.5.1. El máximo es conmutativo, así que además quita una fuente de
            # dependencia del orden.
            entity.last_seen = max(
                float(previous.last_seen or 0.0), float(entity.last_seen or 0.0)
            )
            # La base es el MÁXIMO de las procedencias vistas. Máximo es conmutativo,
            # así que el orden de llegada no cambia el resultado.
            base = max(base, previous.base_confidence or 0.0)
            # Cada atributo se cuenta a favor o en contra SEGÚN SU VALOR. Un `exists`
            # nuevo que coincide apoya; uno distinto contradice. La cuenta es por
            # comparación, no por tiempo, así que no depende del orden.
            # Se acumula la FRECUENCIA de cada valor, no una comparación secuencial.
            conteo = _merge_value_counts(
                previous.value_counts, dicho, source_rank(entity.source), observado_en)
            entity.value_counts = conteo
            apoyo, contra = _tally(conteo)
            entity.support = apoyo
            entity.contradictions = contra
            entity.evidence_ids = _merge_evidence_ids(previous, entity)
            # P0 §4.5.3: el estado vigente se RESUELVE, no es "el último que escribió".
            # Antes `{**previo, **nuevo}` hacía que el valor dependiera del orden de
            # llegada, que es justo lo que el determinismo prohíbe. Con el conteo de
            # frecuencias, el vigente sale de una regla fija y es el mismo llegue como
            # llegue el conjunto de observaciones.
            entity.attributes = {**previous.attributes, **resolve_vigente(conteo)}
        else:
            # Primera observación: su valor es el único conocido, y cuenta como apoyo.
            entity.value_counts = _merge_value_counts(
                {}, entity.attributes, source_rank(entity.source),
                float(entity.last_seen or 0.0))
            apoyo, contra = _tally(entity.value_counts)
            entity.support = apoyo
            entity.contradictions = contra
        entity.base_confidence = base
        entity.confidence = evolved_confidence(base, entity.support, entity.contradictions)
        # §4.5.3: el registro de contradicciones se reconstruye desde el conteo, de modo
        # que se puede hacer idempotente y no depende de cuántas veces se haya llamado a
        # `upsert`. Conserva TODOS los valores vistos, vigente incluido.
        entity.conflicts = build_conflicts(entity.value_counts)
        self.entities[key] = entity
        return entity

    def get(self, entity_id: str, *, scope: Scope | str | None = None):
        return self.entities.get(self._key(entity_id, scope))

    def snapshot(self, *, scope: Scope | str | None = None):
        """Entidades de UN ámbito. Por defecto, el de la instancia: nunca mezcla."""
        target = self._scope(scope).id
        return [e for (sid, _), e in self.entities.items() if sid == target]

    # ------------------------------------------------------------------ #
    # Consulta
    # ------------------------------------------------------------------ #

    def query(self, *, kind: str | None = None, text: str | None = None,
              limit: int = 20, scope: Scope | str | None = None) -> list[WorldEntity]:
        target = self._scope(scope).id
        items = [e for (sid, _), e in self.entities.items() if sid == target]
        if kind:
            items = [e for e in items if e.kind == kind]
        if text:
            query_terms = terms(text)
            scored = []
            for entity in items:
                haystack = terms(f"{entity.id} {entity.name} {' '.join(map(str, entity.attributes.values()))}")
                shared = query_terms & haystack
                if shared:
                    scored.append((len(shared) / max(1, len(query_terms)), entity))
            scored.sort(key=lambda pair: pair[0], reverse=True)
            items = [entity for _, entity in scored]
        return items[:limit]

    def for_objective(self, objective: str, limit: int = 6,
                      *, scope: Scope | str | None = None) -> list[WorldEntity]:
        return self.query(text=objective, limit=limit, scope=scope)

    def known_path(self, path: str, *, scope: Scope | str | None = None) -> WorldEntity | None:
        """La ÚLTIMA observación de esa ruta, aunque sea vieja. Lectura cruda.

        No decide nada: `GoalVerifier` la usa como evidencia sin mirar `exists`. Para
        *actuar* sobre una ausencia está `observed_absent()`, que sí exige frescura.
        """
        if not path:
            return None
        return self.entities.get(self._key(f"{FILE}:{path}", scope))

    def is_absence_fresh(self, entity: WorldEntity, *, now: float | None = None) -> bool:
        """¿Esta observación de ausencia sigue siendo creíble?

        Una ausencia envejece. La herramienta dijo "aquí no hay nada" en el momento T; el
        usuario puede crear el archivo un segundo después, y a partir de ese momento
        esa afirmación es falsa. Un `exists: False` muy viejo no es información, es una suposición
        con aspecto de evidencia.

        ## Por qué sólo caducan las ausencias, y no las presencias

        Una presencia vieja no hace daño por sí sola: si el archivo se borró, la lectura
        falla y ALEXIS se entera con un error HONESTO y barato. Una ausencia vieja sí hace
        daño: impide actuar y lanza una pregunta al usuario sobre algo que sí existe. El
        coste de equivocarse es asimétrico, así que sólo la ausencia caduca.

        ## Por qué el plazo es corto

        Lo que esta comprobación evita es repetir un fallo que acabamos de ver. Eso vale
        durante un instante, no durante una tarde. Y ante la duda se difiere a la
        herramienta: preguntar al usuario cuando se podía actuar interrumpe su autonomía,
        mientras que un `fs.stat` que devuelve "no existe" es un dato correcto y barato.

        ## Edad desconocida

        `LAST_SEEN_UNKNOWN` (0.0) cuenta como VIEJA. No es posible afirmar que algo se acaba
        de observar si no se sabe cuándo se observó, y tratarlo como fresco sería volver a
        convertir "no lo sé" en "lo sé".
        """
        if entity is None or entity.attributes.get("exists") is not False:
            return False
        seen = float(getattr(entity, "last_seen", LAST_SEEN_UNKNOWN) or LAST_SEEN_UNKNOWN)
        if seen <= 0.0:
            return False
        return (time.time() if now is None else now) - seen <= ABSENCE_TTL_S

    def observed_absent(self, path: str, *, scope: Scope | str | None = None,
                        now: float | None = None) -> WorldEntity | None:
        """La entidad si ACABA de observarse como ausente; `None` si no se puede afirmar.

        `None` significa "el mundo no sostiene la ausencia": o nunca se observó, o se
        observó positiva, o la observación es demasiado vieja. En los tres casos la
        respuesta correcta es dejar que la herramienta lo compruebe, no afirmarlo.
        """
        entity = self.known_path(path, scope=scope)
        if entity is None or not self.is_absence_fresh(entity, now=now):
            return None
        return entity

    def missing_paths(self, paths, *, scope: Scope | str | None = None,
                      now: float | None = None) -> list[str]:
        """Rutas que ESTE ámbito acaba de observar como inexistentes.

        Con `scope` explícito no puede devolver una ruta que otro proyecto ya observó
        como ausente: es la lectura que antes cruzaba proyectos. Y con `scope=None` no
        devuelve una ausencia vencida, por la misma razón que `observed_absent`.
        """
        missing = []
        for path in paths or []:
            if self.observed_absent(path, scope=scope, now=now) is not None:
                missing.append(path)
        return missing

    def neighbors(self, entity_id: str, *, scope: Scope | str | None = None) -> list[WorldEntity]:
        target = self._scope(scope).id
        related = {
            child if parent == entity_id else parent
            for sid, parent, child, _relation in self.edges
            if sid == target and (parent == entity_id or child == entity_id)
        }
        return [e for e in (self.entities.get((target, eid)) for eid in sorted(related)) if e]
        
    def dependencies(self, entity_id: str, *, scope: Scope | str | None = None) -> list[WorldEntity]:
        target = self._scope(scope).id
        return [
            e
            for sid, parent, child, relation in self.edges
            for e in [self.entities.get((sid, child))]
            if sid == target and parent == entity_id and relation == "depends_on" and e
        ]

    def relate(self, parent_id: str, child_id: str, relation: str = "depends_on",
               *, scope: Scope | str | None = None) -> None:
        edge = (self._scope(scope).id, parent_id, child_id, relation)
        if edge not in self.edges:
            self.edges.append(edge)

    def to_prompt_lines(self, limit: int = 6, *, scope: Scope | str | None = None) -> list[str]:
        """Líneas de UN ámbito, para que lo que va al prompt no mezcle proyectos."""
        return [f"- {entity.to_line()} [evidencia de {entity.source}]"
                for entity in self.snapshot(scope=scope)[:limit]]

    # ------------------------------------------------------------------ #
    # Observación: la parte que lo llena de realidad
    # ------------------------------------------------------------------ #

    def observe_execution(self, step, result, mission=None, *,
                          scope: Scope | str | None = None,
                          evidence_ids=None) -> list[WorldEntity]:
        """Registra en el mundo lo que una ejecución REAL devolvió.

        Solo extrae hechos que la herramienta afirma explícitamente (ruta, existencia,
        tamaño). No interpreta intenciones ni inventa entidades.

        Lo observado entra en el ámbito indicado; por defecto, el de la instancia. La
        herramienta ya viene del workspace de ese ámbito, así que observing en otro
        declararía un hecho sobre un proyecto del que no salió.
        """
        target = self._scope(scope)
        # Ids de la evidencia que respalda esta observación (§4.5.4). Son enlaces, no
        # copias: el texto de la evidencia vive en el `EvidenceStore`, y duplicarlo aquí
        # haría que el mundo dejara de ser "estado" para ser también "memoria".
        evidencia = list(evidence_ids or [])
        observed: list[WorldEntity] = []
        output = getattr(result, "output", None)
        if not isinstance(output, dict):
            return observed
        success = bool(getattr(result, "success", False))

        if output.get("test_run"):
            observed.append(
                self._observe_test_run(step, output, mission, target, evidencia)
            )

        path = output.get("path")
        if isinstance(path, str) and path:
            attributes: dict[str, Any] = {}
            if "exists" in output:
                attributes["exists"] = bool(output["exists"])
            elif success:
                attributes["exists"] = True
            if output.get("size") is not None:
                attributes["size"] = output["size"]
            if output.get("truncated") is not None:
                attributes["truncated"] = bool(output["truncated"])
            if not success and not attributes.get("exists"):
                attributes["exists"] = False
            # P0 §11 — "existe" y "he leído su contenido" son hechos distintos, y solo el
            # segundo sostiene un objetivo semántico ("analiza y dime qué contiene"). La
            # señal NO es una lista de capabilities: es lo que la tool devolvió de verdad.
            # `fs.read` entrega `content`; `fs.stat` sólo metadatos. Se registra el hecho
            # observado, no la intención de la tool.
            #
            # `content_observed` significa contenido CON OBSERVACIÓN ÚTIL, no contenido
            # presente: un fichero vacío o en blanco se registra como observado con
            # contenido vacío, y quien decide si eso satisface el objetivo lo hace con
            # `content_length`. La coherencia entre las tres capas está en
            # `_check_content_observed`, que exige contenido real; aquí sólo se registra
            # el hecho de que la tool devolvió la cadena, sin juzgar si sirve.
            from alexis.cognition.goal_verification import is_meaningful_content

            content = output.get("content")
            if isinstance(content, str):
                attributes["content_observed"] = True
                attributes["content_length"] = len(content)
                attributes["content_meaningful"] = is_meaningful_content(content)
            else:
                attributes["content_observed"] = False
                attributes["content_length"] = None
                attributes["content_meaningful"] = False
            observed.append(
                self.upsert(
                    WorldEntity(
                        id=f"{FILE}:{path}",
                        kind=FILE,
                        name=path,
                        attributes=attributes,
                        source=f"tool:{getattr(step, 'capability', None) or 'executor'}",
                        confidence=0.7,
                        mission_id=getattr(mission, "id", None),
                        scope=target.id,
                        evidence_ids=evidencia,
                    )
                )
            )
        return observed

    def _observe_test_run(self, step, output: dict, mission=None,
                          scope: Scope | None = None,
                          evidence_ids=None) -> WorldEntity:
        """El resultado de una suite ejecutada es un hecho del mundo, no una opinión.

        Se registra aunque la suite falle: un fallo observado es exactamente el tipo de
        evidencia que el GoalVerifier necesita para marcar un criterio como no cumplido.
        """
        target = self._scope(scope)
        ent = str(output.get("path") or ".")
        return self.upsert(
            WorldEntity(
                id=f"{TEST}:{ent}",
                kind=TEST,
                name=ent,
                attributes={
                    "tests_passed": int(output.get("tests_passed") or 0),
                    "tests_failed": int(output.get("tests_failed") or 0),
                    "tests_skipped": int(output.get("tests_skipped") or 0),
                    "exit_code": output.get("exit_code"),
                    "timed_out": bool(output.get("timed_out")),
                    "counts_parsed": bool(output.get("counts_parsed")),
                },
                source=f"tool:{getattr(step, 'capability', None) or 'executor'}",
                confidence=0.9,
                mission_id=getattr(mission, "id", None),
                scope=target.id,
                # Un test run afirma algo del mundo ("esto pasa"), así que responde por sus
                # claims igual que una lectura de archivo. Dejarlo sin ellos dejaba
                # conocimiento verificable sin trazabilidad.
                evidence_ids=list(evidence_ids or []),
            )
        )

    def declare_tool(self, name: str, attributes: dict | None = None, *,
                     scope: Scope | str | None = None) -> WorldEntity:
        return self.upsert(
            WorldEntity(
                id=f"{TOOL}:{name}",
                kind=TOOL,
                name=name,
                attributes=dict(attributes or {}),
                source="registry",
                confidence=1.0,
                scope=self._scope(scope).id,
            )
        )

    def fails_on_missing(self, capability: str | None) -> bool:
        """True si esta capability no puede funcionar sobre una ruta observada como ausente."""
        return capability in _FAILS_ON_MISSING

    # ------------------------------------------------------------------ #
    # Puente con el store persistente (P0 §4.3)
    # ------------------------------------------------------------------ #

    def export(self, *, scope: "Scope | str | None" = None) -> dict[str, list]:
        """Lo que hay que persistir de UN ámbito: entidades y relaciones.

        Sincrono a propósito. `WorldModel` no depende de la base de datos ni de `async`;
        quien la tiene (el runtime) la llama y guarda lo que devuelve. Así el store se
        puede cambiar sin tocar el Core, y un `WorldModel` se puede probar sin PostgreSQL.
        """
        target = self._scope(scope).id
        entidades = [e for (sid, _), e in self.entities.items() if sid == target]
        relaciones = [(p, c, r) for sid, p, c, r in self.edges if sid == target]
        return {"entities": entidades, "edges": relaciones}

    def hydrate(self, entities, edges=(), *, scope: "Scope | str | None" = None) -> int:
        """Repone entidades y relaciones recibidas del store. Devuelve cuántas entró.

        Usa `put()`, no `upsert()`: rehidratar no es observar. Si se fusionara, cada
        reinicio sumaría una observación y movería `last_seen` de una entidad que en
        realidad nadie ha vuelto a mirar.
        """
        target = self._scope(scope).id
        restore = 0
        for entity in entities or ():
            if not entity.scope:
                entity.scope = target
            self.put(entity)
            restore += 1
        for parent_id, child_id, *rest in edges or ():
            relation = rest[0] if rest else "depends_on"
            self.relate(parent_id, child_id, relation, scope=target)
        return restore


__all__ = [
    "WorldEntity",
    "WorldModel",
    "Scope",
    "IMPLICIT_SCOPE",
    "FILE",
    "TOOL",
    "SERVICE",
    "PROJECT",
    "SYSTEM",
    "TASK",
    "RESOURCE",
    "DEPENDENCY",
    "TEST",
]
