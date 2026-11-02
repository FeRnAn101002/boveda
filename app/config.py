"""Configuracion leida del entorno."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    connection_string: str
    username: str
    password: str
    bucket: str
    scope: str
    collection: str
    gh_repo: str

    @property
    def configured(self) -> bool:
        return all([self.connection_string, self.username, self.password])

    @property
    def fuente(self) -> str:
        return f"`{self.bucket}`.`{self.scope}`.`{self.collection}`"


def _var(nombre: str, por_defecto: str = "") -> str:
    """Lee una variable de entorno sin los espacios de los extremos.

    Estos valores se pegan a mano en formularios web (Render, GitHub
    Secrets) y es facil que arrastren un salto de linea. Una contrasena con
    un '\\n' al final falla la autenticacion con un error que no se parece
    en nada a la causa real.
    """
    return os.getenv(nombre, por_defecto).strip()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        connection_string=_var("CB_CONNECTION_STRING"),
        username=_var("CB_USERNAME"),
        password=_var("CB_PASSWORD"),
        bucket=_var("CB_BUCKET", "vitrina"),
        scope=_var("CB_SCOPE", "_default"),
        collection=_var("CB_COLLECTION", "productos"),
        gh_repo=_var("GH_REPO", "FeRnAn101002/boveda"),
    )
