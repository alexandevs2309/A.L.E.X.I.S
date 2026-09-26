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
        return cls(
            id=str(data.get("id") or ""),
            kind=str(data.get("kind") or "unknown"),
            name=str(data.get("name") or data.get("id") or ""),
            attributes=dict(data.get("attributes") or {}),
            source=str(data.get("source") or "declared"),
            confidence=float(data.get("confidence") or 0.0),
            mission_id=data.get("mission_id"),
            observations=int(data.get("observations") or 0),
            last_seen=last_seen,
            scope=str(data.get("scope") or ""),
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
        if not entity.scope:
            entity.scope = self.scope.id
        # La clave se calcula con el ámbito de la ENTIDAD, no con el de la instancia: una
        # entidad con `scope` propio se guarda en su ámbito aunque el modelo tenga otro por
        # defecto. Es lo que permite que un solo modelo aloje varios proyectos.
        key = self._key(entity.id, entity.scope)
        previous = self.entities.get(key)
        if previous is not None:
            entity.attributes = {**previous.attributes, **entity.attributes}
            entity.observations = previous.observations + 1
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
        if not path:
            return None
        return self.entities.get(self._key(f"{FILE}:{path}", scope))

    def missing_paths(self, paths, *, scope: Scope | str | None = None) -> list[str]:
        """Rutas que ESTE ámbito ya observó como inexistentes.

        Con `scope` explícito no puede devolver una ruta que otro proyecto ya observó
        como ausente: es la lectura que antes cruzaba proyectos.
        """
        missing = []
        for path in paths or []:
            entity = self.known_path(path, scope=scope)
            if entity is not None and entity.attributes.get("exists") is False:
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
                          scope: Scope | str | None = None) -> list[WorldEntity]:
        """Registra en el mundo lo que una ejecución REAL devolvió.

        Solo extrae hechos que la herramienta afirma explícitamente (ruta, existencia,
        tamaño). No interpreta intenciones ni inventa entidades.

        Lo observado entra en el ámbito indicado; por defecto, el de la instancia. La
        herramienta ya viene del workspace de ese ámbito, así que observing en otro
        declararía un hecho sobre un proyecto del que no salió.
        """
        target = self._scope(scope)
        observed: list[WorldEntity] = []
        output = getattr(result, "output", None)
        if not isinstance(output, dict):
            return observed
        success = bool(getattr(result, "success", False))

        if output.get("test_run"):
            observed.append(self._observe_test_run(step, output, mission, target))

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
                    )
                )
            )
        return observed

    def _observe_test_run(self, step, output: dict, mission=None,
                          scope: Scope | None = None) -> WorldEntity:
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
