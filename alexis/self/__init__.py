"""Self Model de ALEXIS: autoconocimiento operacional y presencia.

No es conciencia subjetiva: es un modelo activo y persistente de los propios
estados, capacidades, permisos, límites, decisiones, incertidumbres, confianza,
resultados y lecciones de ALEXIS, actualizado por los eventos reales del runtime.
Ver `docs/SELF-MODEL.md`.
"""

from alexis.self.model import SelfModel
from alexis.self.presence import PRESENCE_ORDER, derive_presence
from alexis.self.sync import SelfModelSync

__all__ = ["SelfModel", "SelfModelSync", "PRESENCE_ORDER", "derive_presence"]