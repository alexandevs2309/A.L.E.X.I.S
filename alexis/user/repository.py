"""Persistencia del User Model.

Proyectos y decisiones NO tienen tabla propia: un proyecto activo es una preferencia de
categoría `project` con `value->>'active' = true`, y una decisión una de `context`. Una
sola tabla significa un solo CRUD y una sola clave (`id = "category:key"`).

Degradación: si la tabla no existe, `is_available()` es `False` y los lectores devuelven
defaults en vez de propagar. Un ALEXIS recién arrancado contra una base sin migrar debe
servir con defaults, no refuse a arrancar.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from alexis.storage.db import Database
from alexis.user.model import UserContext, UserPreferences

log = logging.getLogger(__name__)


class UserRepository:
    def __init__(self, db: Database):
        self.db = db
        self._available: bool | None = None

    async def is_available(self) -> bool:
        """Si la tabla existe. Se cachea: es una comprobación por arranque, no por lectura."""
        if self._available is None:
            try:
                await self.db.fetch("SELECT 1 FROM user_preferences LIMIT 1")
                self._available = True
            except Exception:  # noqa: BLE001 — sin tabla es un estado legítimo
                self._available = False
        return self._available

    # ---------------------------- CRUD ---------------------------- #

    async def get(self, category: str, key: str) -> UserPreferences | None:
        if not await self.is_available():
            return None
        rows = await self.db.fetch(
            "SELECT * FROM user_preferences WHERE category = %(c)s AND key = %(k)s",
            {"c": category, "k": key})
        return self._desde_fila(rows[0]) if rows else None

    async def set(self, category: str, key: str, value: Any, *,
                  confidence: float = 1.0, source: str | None = None) -> UserPreferences:
        """Crea o actualiza. Conserva `created_at`: reescribir una preferencia no la vuelve
        nueva, y perder la antigüedad haría imposible distinguir una corrección de un
        descubrimiento."""
        pref = UserPreferences(category=category, key=key, value=value,
                               confidence=confidence, source=source)
        if not await self.is_available():
            return pref
        await self.db.execute(
            """
            INSERT INTO user_preferences (id, category, key, value, confidence, source, updated_at)
            VALUES (%(id)s, %(c)s, %(k)s, %(v)s::jsonb, %(conf)s, %(src)s, now())
            ON CONFLICT (id) DO UPDATE SET value = EXCLUDED.value,
                confidence = EXCLUDED.confidence, source = EXCLUDED.source,
                updated_at = now()
            """,
            {"id": pref.id, "c": category, "k": key,
             "v": json.dumps(value, ensure_ascii=False, default=str),
             "conf": pref.confidence, "src": source})
        return pref

    async def list_by_category(self, category: str) -> list[UserPreferences]:
        if not await self.is_available():
            return []
        rows = await self.db.fetch(
            "SELECT * FROM user_preferences WHERE category = %(c)s ORDER BY key",
            {"c": category})
        return [self._desde_fila(r) for r in rows]

    async def list_all(self) -> list[UserPreferences]:
        if not await self.is_available():
            return []
        rows = await self.db.fetch("SELECT * FROM user_preferences ORDER BY category, key")
        return [self._desde_fila(r) for r in rows]

    async def delete(self, category: str, key: str) -> bool:
        """Borra por categoría y clave. `True` si había algo que borrar."""
        if not await self.is_available():
            return False
        rows = await self.db.fetch(
            "DELETE FROM user_preferences WHERE category = %(c)s AND key = %(k)s RETURNING 1",
            {"c": category, "k": key})
        return bool(rows)

    # -------------------------- Proyectos -------------------------- #

    async def get_active_projects(self) -> list[str]:
        """Sólo los marcados activos.

        Filtra por `value->>'active' = 'true'` y NO por "no hay clave `active`": sin esa
        clave el estado es DESCONOCIDO, no activo, y asumirlo inflaría la lista con
        proyectos que el usuario cerró.
        """
        if not await self.is_available():
            return []
        rows = await self.db.fetch(
            "SELECT key FROM user_preferences "
            "WHERE category = 'project' AND value->>'active' = 'true' ORDER BY key")
        return [r["key"] for r in rows]

    async def add_project(self, name: str, **details: Any) -> UserPreferences:
        return await self.set("project", name, {"active": True, **details}, source="explicit")

    async def remove_project(self, name: str) -> bool:
        """Marca INACTIVO en vez de borrar: borrarla perdería su historia y un
        `add_project` posterior la recrearía vacía."""
        actual = await self.get("project", name)
        if actual is None:
            return False
        valor = dict(actual.value) if isinstance(actual.value, dict) else {}
        valor["active"] = False
        await self.set("project", name, valor, confidence=actual.confidence,
                       source=actual.source)
        return True

    # -------------------------- Decisiones -------------------------- #

    async def record_decision(self, decision_id: str, context: Any, outcome: Any) -> bool:
        """Guarda `outcome` junto a `context` para poder comparar después: sin eso,
        `recent_decisions` es un historial —dice qué pasó, no qué repetir—."""
        if not await self.is_available():
            return False
        await self.set("context", decision_id,
                       {"context": context, "outcome": outcome, "at": int(time.time())},
                       source="explicit")
        return True

    async def get_recent_decisions(self, limit: int = 10) -> list[dict]:
        if not await self.is_available():
            return []
        rows = await self.db.fetch(
            "SELECT key, value FROM user_preferences WHERE category = 'context' "
            "ORDER BY updated_at DESC LIMIT %(n)s", {"n": int(limit)})
        salida = []
        for r in rows:
            valor = r["value"] or {}
            salida.append({"decision_id": r["key"], "context": valor.get("context"),
                           "outcome": valor.get("outcome"), "at": valor.get("at")})
        return salida

    # ---------------------------- Vistas ---------------------------- #

    async def context(self, *, decision_limit: int = 10) -> UserContext:
        """La proyección completa que consumen SelfModel, COGNICIÓN y la API."""
        if not await self.is_available():
            return UserContext()
        return UserContext(
            active_projects=await self.get_active_projects(),
            recent_decisions=await self.get_recent_decisions(decision_limit),
            technical_style={p.key: p.value
                             for p in await self.list_by_category("style")})

    @staticmethod
    def _desde_fila(row) -> UserPreferences:
        return UserPreferences(
            id=row["id"], category=row["category"], key=row["key"], value=row["value"],
            confidence=row["confidence"], source=row["source"],
            created_at=row.get("created_at"), updated_at=row.get("updated_at"))