"""R4 — política de autenticación de la API.

Regla: el token es **opcional en development** (pero su ausencia se avisa en el log) y
**obligatorio en production** (el arranque falla sin él). La comparación del token es en
tiempo constante para no filtrar el valor por temporización.
"""

import logging
import os
import secrets

LOGGER = logging.getLogger("alexis.api.auth")

#: Entornos en los que el token es obligatorio.
STRICT_ENVS = {"production", "prod"}

#: Entornos en los que se permite arrancar sin token (con warning explícito).
PERMISSIVE_ENVS = {"development", "dev", "local", "test", "testing"}


class MissingAPIToken(RuntimeError):
    """El arranque requiere ALEXIS_API_TOKEN y no está definido (R4)."""


def resolve_environment(env: str | None = None) -> str:
    return (env or os.environ.get("ALEXIS_ENV") or "development").strip().lower()


def is_strict(env: str | None = None) -> bool:
    return resolve_environment(env) in STRICT_ENVS


def require_api_token(
    token: str | None = None,
    env: str | None = None,
    *,
    environ: dict | None = None,
    logger: logging.Logger | None = None,
) -> str:
    """Devuelve el token de la API, o falla si el entorno lo exige.

    - `production`: sin token → `MissingAPIToken` (el arranque muere, no se degrada).
    - resto: sin token → warning explícito en el log y se continúa sin auth.
    """
    source = environ if environ is not None else os.environ
    value = (token if token is not None else source.get("ALEXIS_API_TOKEN", "")) or ""
    value = value.strip()
    environment = resolve_environment(env if env is not None else source.get("ALEXIS_ENV"))
    log = logger or LOGGER

    if not value:
        if environment in STRICT_ENVS:
            raise MissingAPIToken(
                "ALEXIS_API_TOKEN es obligatorio en "
                f"ALEXIS_ENV={environment}. Defínelo en el entorno (ver .env.example) "
                "y reinicia la API."
            )
        if environment not in PERMISSIVE_ENVS:
            log.warning(
                "ALEXIS_ENV=%s no reconocido: se permite arrancar sin ALEXIS_API_TOKEN. "
                "La API queda SIN autenticación; no la expongas fuera de localhost.",
                environment,
            )
        else:
            log.warning(
                "ALEXIS_API_TOKEN no definido: la API arranca SIN autenticación en "
                "ALEXIS_ENV=%s. Sólo es aceptable en local; en production es un error "
                "de arranque.",
                environment,
            )
        return ""
    return value


def token_matches(expected: str, provided: str | None) -> bool:
    """Compara en tiempo constante. Sin token configurado, no se exige nada."""
    if not expected:
        return True
    if not provided:
        return False
    return secrets.compare_digest(expected, provided)
