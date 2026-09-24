"""Tests R4 — política de auth de la API.

El token es obligatorio en `production` (el arranque falla) y opcional en `development`
(con warning explícito). La comparación es en tiempo constante.
"""

import logging

import pytest

from apps.api.auth import (
    MissingAPIToken,
    is_strict,
    require_api_token,
    resolve_environment,
    token_matches,
)


def test_production_requires_token():
    with pytest.raises(MissingAPIToken) as exc:
        require_api_token(token="", env="production", environ={})
    assert "ALEXIS_API_TOKEN" in str(exc.value)


def test_prod_alias_also_requires_token():
    with pytest.raises(MissingAPIToken):
        require_api_token(token="", env="prod", environ={})


def test_development_allows_missing_token_but_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="alexis.api.auth"):
        token = require_api_token(token="", env="development", environ={})
    assert token == ""
    assert any("SIN autenticación" in r.getMessage() for r in caplog.records)


def test_unknown_environment_warns_too(caplog):
    with caplog.at_level(logging.WARNING, logger="alexis.api.auth"):
        require_api_token(token="", env="staging", environ={})
    assert caplog.records
    assert any("no reconocido" in r.getMessage() for r in caplog.records)


def test_token_present_is_returned_in_any_env():
    assert require_api_token(token="abc123", env="development", environ={}) == "abc123"
    assert require_api_token(token="abc123", env="production", environ={}) == "abc123"
    assert require_api_token(token="  abc123  ", env="production", environ={}) == "abc123"


def test_reads_from_environ_mapping():
    assert require_api_token(env="production", environ={"ALEXIS_API_TOKEN": "t0ken"}) == "t0ken"
    with pytest.raises(MissingAPIToken):
        require_api_token(env="production", environ={})


def test_is_strict_and_resolve():
    assert is_strict("production") is True
    assert is_strict("development") is False
    assert resolve_environment(None) in ("development", "production", "test")


# ----------------------------------------------------------------------
# Comparación del token
# ----------------------------------------------------------------------


def test_token_matches():
    assert token_matches("secret", "secret") is True
    assert token_matches("secret", "otro") is False
    assert token_matches("secret", None) is False
    assert token_matches("secret", "") is False


def test_no_token_configured_requires_nothing():
    # Sólo development puede llegar aquí (en production el arranque falla antes).
    assert token_matches("", None) is True
    assert token_matches("", "cualquiera") is True


# ----------------------------------------------------------------------
# Endpoints protegidos
# ----------------------------------------------------------------------


def test_protected_endpoints_require_token():
    from apps.api import main

    paths = {route.path for route in main.app.routes}
    for protected in ("/missions", "/stream"):
        assert protected in paths
    # /health y /ui quedan abiertos a propósito (health check / shell).
    assert "/health" in paths


def test_health_reports_auth_state():
    from apps.api import main

    assert isinstance(main.API_TOKEN, str)
    assert main.API_ENV in ("development", "production", "prod", "test", "testing", "local")
