"""User Model — lo que ALEXIS sabe de quien lo usa.

Falta a propósito una cosa: un `UserId`. Este módulo guarda preferencias de un único
usuario, el del proceso. Añadir el identificador después obligaría a recorrer cada
consulta y cada clave primaria; añadirlo ahora sería)}-estructura que hoy nadie consulta.
"""

from alexis.user.model import UserContext, UserPreferences
from alexis.user.repository import UserRepository

__all__ = ["UserContext", "UserPreferences", "UserRepository"]