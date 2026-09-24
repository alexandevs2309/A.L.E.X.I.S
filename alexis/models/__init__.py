"""Capa de modelos: contrato de provider, router y adapters (F2.1).

Invariante P1: toda respuesta cognitiva lleva `outcome` explícito
(`real | degraded | unavailable`); la contingencia determinista nunca se presenta como
razonamiento real.
"""

from alexis.models.degraded import DegradedProvider, EchoModel
from alexis.models.provider import (
    ModelOutcome,
    ModelProvider,
    ModelProviderError,
    ModelProviderUnavailable,
    ModelRequest,
    ModelResponse,
    ModelTask,
)
from alexis.models.router import ModelRouter, build_router_from_config

__all__ = [
    "DegradedProvider",
    "EchoModel",
    "ModelOutcome",
    "ModelProvider",
    "ModelProviderError",
    "ModelProviderUnavailable",
    "ModelRequest",
    "ModelResponse",
    "ModelRouter",
    "ModelTask",
    "build_router_from_config",
]
