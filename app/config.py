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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        connection_string=os.getenv("CB_CONNECTION_STRING", ""),
        username=os.getenv("CB_USERNAME", ""),
        password=os.getenv("CB_PASSWORD", ""),
        bucket=os.getenv("CB_BUCKET", "vitrina"),
        scope=os.getenv("CB_SCOPE", "_default"),
        collection=os.getenv("CB_COLLECTION", "productos"),
        gh_repo=os.getenv("GH_REPO", "FeRnAn101002/boveda"),
    )
