"""Programmatic migration runner used by the CLI and tests.

It needs no alembic.ini: the migrations ship inside the `opa` package, so an installed wheel (for
example in the Docker image) can migrate from any working directory. alembic.ini stays in the
source tree only for developers who run the `alembic` command directly.
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def alembic_config(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("path_separator", "os")
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def upgrade_head(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), "head")


def current_head() -> str:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config("")).get_current_head()
