"""
PostgreSQL schema bootstrap test (#300, experimental backend).

Proves `db/schema.py:init_schema_postgres` builds the entire Trinity schema on
a real PostgreSQL engine from the same `TABLES`/`INDEXES` strings used for
SQLite, and that the audit_log append-only triggers are translated to PL/pgSQL
and actually fire.

Runs only when a PostgreSQL server is reachable via `TEST_POSTGRES_URL`
(default `postgresql://trinity:trinity@localhost:5432/trinity`); otherwise the
whole module is skipped, so CI without a postgres service stays green.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

sqlalchemy = pytest.importorskip("sqlalchemy")
from sqlalchemy import create_engine, text  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
DEFAULT_PG_URL = os.getenv(
    "TEST_POSTGRES_URL", "postgresql://trinity:trinity@localhost:5432/trinity"
)


def _pg_reachable(url: str) -> bool:
    try:
        eng = create_engine(url)
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


if not _pg_reachable(DEFAULT_PG_URL):
    pytest.skip(
        f"no PostgreSQL reachable at {DEFAULT_PG_URL} "
        "(set TEST_POSTGRES_URL to run)",
        allow_module_level=True,
    )


def _load_schema():
    spec = importlib.util.spec_from_file_location("_schema_pg_test", _BACKEND / "db/schema.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def pg_engine():
    """A clean public schema on the test PostgreSQL, torn down after."""
    admin = create_engine(DEFAULT_PG_URL)
    with admin.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    admin.dispose()
    eng = create_engine(DEFAULT_PG_URL)
    yield eng
    eng.dispose()


def test_all_tables_created(pg_engine):
    schema = _load_schema()
    schema.init_schema_postgres(pg_engine)
    schema.init_schema_postgres(pg_engine)  # idempotent

    with pg_engine.connect() as c:
        tables = {
            r[0]
            for r in c.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public'"
                )
            )
        }
    missing = set(schema.TABLES.keys()) - tables
    assert not missing, f"tables missing on PostgreSQL: {sorted(missing)}"


def test_serial_primary_key(pg_engine):
    """INTEGER PRIMARY KEY AUTOINCREMENT → SERIAL (auto-incrementing identity)."""
    schema = _load_schema()
    schema.init_schema_postgres(pg_engine)
    with pg_engine.connect() as c:
        default = c.execute(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name='users' AND column_name='id'"
            )
        ).scalar()
    assert default and "nextval" in default  # SERIAL


def test_audit_log_append_only_triggers_fire(pg_engine):
    schema = _load_schema()
    schema.init_schema_postgres(pg_engine)

    iso_now = "to_char((now() at time zone 'utc'),'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"
    with pg_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO audit_log (event_id,event_type,event_action,actor_type,timestamp,source) "
                f"VALUES ('e1','t','a','user',{iso_now},'api')"
            )
        )

    with pytest.raises(Exception) as ui:
        with pg_engine.begin() as c:
            c.execute(text("UPDATE audit_log SET actor_type='x' WHERE event_id='e1'"))
    assert "cannot be modified" in str(ui.value)

    with pytest.raises(Exception) as di:
        with pg_engine.begin() as c:
            c.execute(text("DELETE FROM audit_log WHERE event_id='e1'"))
    assert "retention period" in str(di.value)
