"""Capability Manager (F1): catálogo y registro de capacidades.

El Self Model consumirá `available_capabilities` desde aquí (CapabilityRegistry
habilitado) en lugar de listas hardcodeadas. Ver `docs/AUTONOMY-V0.5-CAPABILITIES.md`.
"""

from alexis.capabilities.catalog import (
    ACTION_TO_CAPABILITY,
    CapabilityRegistry,
    CapabilitySpec,
    build_catalog,
)

__all__ = [
    "CapabilityRegistry",
    "CapabilitySpec",
    "build_catalog",
    "ACTION_TO_CAPABILITY",
]