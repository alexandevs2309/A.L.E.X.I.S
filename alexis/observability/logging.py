"""Logs estructurados con structlog (JSON línea a línea) + correlation por misión.

Restricciones:
- NO secretos, tokens, prompts crudos ni contenido de archivos.
- No reemplaza el logging estándar; solo enriquece con campos estructurales.
"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.contextvars import bind_contextvars, unbind_contextvars

_CONFIGURED = False


def setup_structlog(json_logs: bool = True, level: str = "INFO") -> None:
    """JSON output + timestamp + level + contextvars (correlation_id, mission_id…)."""
    global _CONFIGURED
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.EventRenamer("event"),
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer(),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=False,
    )
    _CONFIGURED = True


def ensure_structlog(level: str = "INFO") -> None:
    if not _CONFIGURED:
        setup_structlog(level=level)


def get_logger(name: str = "alexis"):
    ensure_structlog()
    return structlog.get_logger(name)


def emit(event: str, level: str = "info", **fields) -> None:
    """Un log estructurado: emit("mission.started", mission_id=..., autonomy=...)

    NO usar `structlog.get_logger()` con PrintLoggerFactory: ese queda pegado a una
    única stream y estalla con `I/O operation on closed file` cuando un test cierra
    stdout. Aquí se serializa todo en JSON y se `print` sobre el stdout actual.
    """
    import datetime as _dt
    import json as _json

    data = dict(structlog.contextvars.get_contextvars())
    data.update(fields)
    data["event"] = event
    data["level"] = str(level)
    data["timestamp"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")
    print(_json.dumps(data, ensure_ascii=False, default=str), file=sys.stdout)


def bind_mission_context(mission_id: str, envelope_id=None, user_id=None) -> None:
    bind_contextvars(
        correlation_id=mission_id,
        mission_id=mission_id,
        envelope_id=envelope_id,
        user_id=user_id,
    )


def unbound_mission_context() -> None:
    unbind_contextvars("correlation_id", "mission_id", "envelope_id", "user_id")


__all__ = [
    "setup_structlog",
    "ensure_structlog",
    "get_logger",
    "emit",
    "bind_mission_context",
    "unbound_mission_context",
]
