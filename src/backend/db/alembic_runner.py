"""Alembic runner for the PostgreSQL backend (#1183).

Invoked from ``database.init_database()`` on the non-SQLite path instead of the
old ``init_schema_postgres`` fresh-build. SQLite keeps the legacy bespoke
``db/migrations.py`` runner — the two coexist during the Postgres transition.

Adoption (one-time) handling:
  - fresh PG DB (no tables)           -> ``upgrade head`` runs the baseline +
                                         any later revisions, building the
                                         full schema.
  - pre-Alembic PG DB (built by the
    old ``init_schema_postgres``, no
    ``alembic_version`` table)        -> ``stamp 0001_baseline`` (its schema IS
                                         the baseline), then ``upgrade head``
                                         applies anything added after baseline.
  - already-managed PG DB             -> ``upgrade head`` applies pending
                                         revisions.
"""
from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from db.engine import get_engine, resolve_database_url

logger = logging.getLogger(__name__)

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_BASELINE_REVISION = "0001_baseline"
# Any core OSS table that the baseline creates — used to detect a pre-Alembic
# database that already has the schema but no alembic_version table.
_CORE_TABLE = "users"


def _config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", resolve_database_url())
    return cfg


def upgrade_to_head() -> None:
    """Bring the active PostgreSQL schema to head via Alembic."""
    engine = get_engine()
    insp = inspect(engine)
    has_version = insp.has_table("alembic_version")
    has_core = insp.has_table(_CORE_TABLE)

    cfg = _config()
    if not has_version and has_core:
        logger.info(
            "Alembic: pre-Alembic PG schema detected (no alembic_version) — "
            "stamping %s before upgrade", _BASELINE_REVISION,
        )
        command.stamp(cfg, _BASELINE_REVISION)

    command.upgrade(cfg, "head")
    logger.info("Alembic: PostgreSQL schema at head")
