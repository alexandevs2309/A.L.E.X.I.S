"""CORE-07 — Persistencia durable del estado aprendido del Self Model.

`SelfModelSync` acumulaba en memoria lo que ALEXIS aprendía: lecciones verificadas
(`can_teach`), auto-observaciones de fallos y reflexiones. Al reiniciar el proceso,
`SelfModel(...)` se reconstruía desde cero y **todo ese aprendizaje se perdía**. Este
módulo es la fuente durable de ese estado.

Qué persiste y qué NO, y por qué:

- **Persiste** `lessons`, `observations_about_self` y `reflections`: son aprendizaje
  acumulado que no se puede recalcular a partir de nada.
- **NO persiste** `current_goal`, `current_mission`, `decisions`, `task_results`,
  `confidence`, `permissions`, `current_policy`: son una *proyección* del objeto misión
  en vivo, que la tabla `missions` ya guarda. Copiarlos aquí crearía una segunda
  memoria que puede contradecir a la real (la misión es la autoridad).

Sobre `mission_id`: es **provenance opcional**, no identidad. Se guarda cuando el origen
del registro se conoce, y es `NULL` en caso contrario (una lección pre-misión, por
ejemplo, no tiene misión y eso es legítimo). Las lecciones no se particionan por
misión: son conocimiento global del Self Model.

Este módulo NO escribe por su cuenta: expone `save()`/`load()` y quien decide cuándo
sincronizar es `SelfModelSync` (el que ya sabe qué acaba de aprender). Así no hay dos
sitios escribiendo el mismo estado.
"""

from __future__ import annotations

import json
import logging

LOGGER = logging.getLogger("alexis.self.persistence")

#: Tipos de registro aprendido. Coinciden con el CHECK del schema.
KIND_LESSON = "lesson"
KIND_OBSERVATION = "observation"
KIND_REFLECTION = "reflection"
_KINDS = (KIND_LESSON, KIND_OBSERVATION, KIND_REFLECTION)

#: Tope de cada tipo al recuperar. Es el mismo criterio acotado que aplica en memoria
#: (`SelfModelSync.LESSON_HISTORY`, `note_self_observation`, `add_reflection`): lo
#: aprendido es un borde del Self Model, no su memoria completa.
_MAX = {"lesson": 5, "observation": 6, "reflection": 5}


class SelfModelPersistence:
    """Guarda y recupera el estado aprendido del Self Model.

    Una instancia por repositorio de base de datos. No cachea: `load()` lee siempre del
    almacenamiento durable, para que "lo que ALEXIS sabe" no dependa de lo que esta
    clase recuerde.
    """

    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------------ #
    # Escritura
    # ------------------------------------------------------------------ #

    async def save(self, *, lessons, observations, reflections, mission_id=None) -> None:
        """Persiste el estado aprendido.

        `mission_id` es la provenance opcional de estos registros. Si no se conoce el
        origen, se deja `NULL`: es preferible sin provenance a inventarla.
        """
        entradas = []
        for texto in lessons or []:
            entradas.append((KIND_LESSON, str(texto), {}))
        for obs in observations or []:
            # Las observaciones ya llevan su forma `{"at","text"}`; se conserva el resto.
            if isinstance(obs, dict):
                texto = str(obs.get("text") or "")
                extra = {"at": obs.get("at")}
            else:
                texto = str(obs)
                extra = {}
            entradas.append((KIND_OBSERVATION, texto, {k: v for k, v in extra.items() if v is not None}))
        for refl in reflections or []:
            if isinstance(refl, dict):
                texto = str(refl.get("text") or "")
                extra = {"at": refl.get("at")}
            else:
                texto = str(refl)
                extra = {}
            entradas.append((KIND_REFLECTION, texto, {k: v for k, v in extra.items() if v is not None}))

        if not entradas:
            return
        await self._insert(entradas, mission_id)

    async def _insert(self, entradas, mission_id) -> None:
        # `db.execute` abre una conexión por sentencia y no acepta batch, así que cada
        # registro va en su propio INSERT —igual que el resto de repositorios—. Lo que
        # se aprende es un borde pequeño y acotado del Self Model, no un flujo de
        # escritura que justifique otro camino.
        for kind, text, payload in entradas:
            await self.db.execute(
                """
                INSERT INTO self_learnings (kind, text, payload, mission_id)
                VALUES (%(kind)s, %(text)s, %(payload)s::jsonb, %(mission_id)s)
                """,
                {
                    "kind": kind,
                    "text": text,
                    "payload": json.dumps(payload, ensure_ascii=False),
                    "mission_id": mission_id,
                },
            )

    # ------------------------------------------------------------------ #
    # Lectura
    # ------------------------------------------------------------------ #

    async def load(self) -> dict:
        """Estado aprendido guardado, listo para `SelfModel.restore()`.

        Devuelve valores vacíos (no `None`) cuando no hay nada, para que `restore()`
        sea seguro y no haya que inventar datos.
        """
        por_tipo = {kind: [] for kind in _KINDS}
        mission_id = None

        for kind in _KINDS:
            propias = await self.db.fetch(
                """
                SELECT text, payload, mission_id FROM self_learnings
                WHERE kind = %(kind)s ORDER BY at DESC, id DESC LIMIT %(limit)s
                """,
                {"kind": kind, "limit": _MAX[kind]},
            )
            por_tipo[kind] = list(propias)
            if kind == KIND_LESSON and propias and mission_id is None:
                # Provenance de la última lección conocida; None si no la tenía.
                mission_id = propias[0]["mission_id"]

        lessons = [r["text"] for r in por_tipo[KIND_LESSON]]
        observations = [{"text": r["text"], **_payload(r)} for r in por_tipo[KIND_OBSERVATION]]
        reflections = [{"text": r["text"], **_payload(r)} for r in por_tipo[KIND_REFLECTION]]

        return {
            "lessons": lessons,
            "observations": observations,
            "reflections": reflections,
            "mission_id": mission_id,
        }

    # ------------------------------------------------------------------ #
    # Mantenimiento (acotado al tamaño en memoria, sin perder lo reciente)
    # ------------------------------------------------------------------ #

    async def prune(self, *, lessons, observations, reflections) -> None:
        """Deja en la base sólo lo que el Self Model sigue teniendo en memoria.

        El Self Model acota su historial (`LESSON_HISTORY`, auto-observaciones y
        reflexiones top-N). Sin esto la base crecería con historia que ALEXIS ya no
        recuerda, y lo guardado dejaría de ser "lo que sabe" para ser "todo lo que le
        pasó". Con lista vacía no se borra nada: un SelfModel recién creado, que no
        tiene nada en memoria, no debe poder vaciar lo aprendido.
        """
        if not any((lessons, observations, reflections)):
            return
        textos = {
            KIND_LESSON: [str(x) for x in lessons or []],
            KIND_OBSERVATION: [o.get("text") if isinstance(o, dict) else o for o in observations or []],
            KIND_REFLECTION: [r.get("text") if isinstance(r, dict) else r for r in reflections or []],
        }
        for kind, valores in textos.items():
            if not valores:
                continue
            await self.db.execute(
                """
                DELETE FROM self_learnings
                WHERE kind = %(kind)s AND NOT (text = ANY(%(textos)s))
                """,
                {"kind": kind, "textos": valores},
            )


def _payload(row) -> dict:
    """El `payload` JSONB de una fila (p.ej. el `at` de una observación)."""
    raw = row.get("payload") if hasattr(row, "get") else None
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return dict(raw)


__all__ = [
    "KIND_LESSON",
    "KIND_OBSERVATION",
    "KIND_REFLECTION",
    "SelfModelPersistence",
]