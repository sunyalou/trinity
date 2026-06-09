"""
Backend-agnostic database test harness (#300).

Replaces the per-file bespoke pattern (a hand-rolled ``tmp_db`` fixture +
``_make_db_schema`` partial DDL + raw ``sqlite3.connect`` seed helpers) that
hardwired unit tests to SQLite. Tests built on this harness run on **either**
backend: SQLite always, and PostgreSQL when ``TEST_POSTGRES_URL`` is set.

Why this exists
---------------
The old fixtures wrote seed rows with raw ``sqlite3`` and built a partial
schema by hand. That (a) could not target PostgreSQL and (b) silently drifted
from the real schema. This harness instead:

* builds the schema from the canonical Core table metadata
  (``db.tables.metadata.create_all``) — one source of truth, dialect-correct
  on both backends, and crucially WITHOUT importing ``database`` /
  ``services`` / ``config`` (so lightweight unit tests keep their minimal
  import surface and don't acquire a ``REDIS_URL`` requirement);
* seeds via the active SQLAlchemy engine, so writes land in whatever backend
  ``db_backend`` selected;
* gives each test a clean database (a fresh temp file on SQLite; a
  drop-and-recreate of the ``public`` schema on PostgreSQL).

Usage
-----
    from db_harness import db_backend, seed_schedule, seed_execution

    def test_something(db_backend):           # runs once per available backend
        seed_schedule("sid-1")
        seed_execution("sid-1", duration_ms=100)
        ...

Backends that legitimately only make sense on SQLite (e.g. tests of the
SQLite migration runner itself) should depend on ``sqlite_only_backend``
instead of ``db_backend``.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

SQLITE = "sqlite"
POSTGRES = "postgres"


def available_backends() -> list[str]:
    """SQLite always; PostgreSQL only when TEST_POSTGRES_URL is set."""
    backends = [SQLITE]
    if os.getenv("TEST_POSTGRES_URL"):
        backends.append(POSTGRES)
    return backends


def _reset_postgres() -> None:
    """Drop + recreate the public schema for a clean per-test slate."""
    from db.engine import get_engine

    with get_engine().begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))


def bootstrap_schema() -> None:
    """Build the full production schema on the active engine.

    Uses the real schema builders — ``db.schema.init_schema`` (cursor-based
    raw DDL) on SQLite and ``init_schema_postgres`` on PostgreSQL — so the
    test schema is a faithful reproduction of production, INCLUDING UNIQUE
    constraints and append-only triggers. (``metadata.create_all`` was an
    earlier approach but dropped UNIQUE constraints that ``ON CONFLICT`` and
    production upserts rely on.) Still import-light: ``db.schema`` pulls in
    no ``services``/``config`` chain.
    """
    from db.engine import get_engine, is_sqlite

    engine = get_engine()
    if is_sqlite():
        from db import schema as sch

        raw = engine.raw_connection()
        try:
            cur = raw.cursor()
            sch.init_schema(cur, raw)
            raw.commit()
        finally:
            raw.close()
    else:
        from db.schema import init_schema_postgres

        init_schema_postgres(engine)


def _activate_backend(backend: str, tmp_path, monkeypatch) -> None:
    """Point the engine at `backend` and build a fresh schema."""
    from db.engine import dispose_engines

    if backend == SQLITE:
        db_path = tmp_path / "trinity.db"
        monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))
        monkeypatch.delenv("DATABASE_URL", raising=False)
        dispose_engines()
        bootstrap_schema()
    elif backend == POSTGRES:
        monkeypatch.setenv("DATABASE_URL", os.environ["TEST_POSTGRES_URL"])
        dispose_engines()
        _reset_postgres()
        bootstrap_schema()
    else:  # pragma: no cover - guard
        raise ValueError(f"unknown backend {backend!r}")


@pytest.fixture(params=available_backends())
def db_backend(request, tmp_path, monkeypatch):
    """Parametrized DB backend.

    Yields the backend name (``"sqlite"`` / ``"postgres"``) after pointing the
    engine at it and building a fresh schema. Disposes cached engines on
    teardown so the next test/param starts clean.
    """
    from db.engine import dispose_engines

    _activate_backend(request.param, tmp_path, monkeypatch)
    try:
        yield request.param
    finally:
        dispose_engines()


@pytest.fixture
def sqlite_only_backend(tmp_path, monkeypatch):
    """SQLite-only variant for tests that are inherently SQLite-specific
    (e.g. the SQLite migration runner). Mirrors ``db_backend`` but never
    parametrizes onto PostgreSQL."""
    from db.engine import dispose_engines

    _activate_backend(SQLITE, tmp_path, monkeypatch)
    try:
        yield SQLITE
    finally:
        dispose_engines()


# ----------------------------------------------------------------------
# Engine-based seed helpers (write to the active backend, not raw sqlite3).
# Cover the common scheduling/ownership tables; tests needing other tables
# seed them the same way via ``get_engine()``.
# ----------------------------------------------------------------------

def _engine():
    from db.engine import get_engine

    return get_engine()


def run(sql: str, **binds):
    """Execute a write statement on the active engine (named :binds)."""
    with _engine().begin() as conn:
        conn.execute(text(sql), binds)


def scalar(sql: str, **binds):
    """Return the first column of the first row (or None) — named :binds."""
    with _engine().connect() as conn:
        return conn.execute(text(sql), binds).scalar()


def count(table: str, where: str = "1=1", **binds) -> int:
    """COUNT(*) helper that works on both backends (named :binds in `where`)."""
    return scalar(f"SELECT COUNT(*) FROM {table} WHERE {where}", **binds) or 0


def seed_user(user_id: int = 1, username: str = "owner", role: str = "user") -> int:
    """Insert a users row (idempotent on username). Returns the id."""
    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, username, role, created_at, updated_at) "
                "VALUES (:id, :u, :r, :n, :n)"
            ),
            {"id": user_id, "u": username, "r": role, "n": "2026-01-01T00:00:00Z"},
        )
    return user_id


def seed_agent(agent_name: str = "agent-1", owner_id: int = 1) -> None:
    """Insert an agent_ownership row."""
    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
                "VALUES (:a, :o, :n)"
            ),
            {"a": agent_name, "o": owner_id, "n": "2026-01-01T00:00:00Z"},
        )


def seed_schedule(
    sid: str,
    agent_name: str = "agent-1",
    owner_id: int = 1,
    deleted_at: str | None = None,
) -> None:
    """Insert an agent_schedules row via the active engine."""
    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_schedules "
                "(id, agent_name, name, cron_expression, message, enabled, "
                " timezone, owner_id, created_at, updated_at, deleted_at) "
                "VALUES (:id, :a, 'sched', '0 0 * * *', 'hi', 1, 'UTC', "
                " :o, :n, :n, :del)"
            ),
            {"id": sid, "a": agent_name, "o": owner_id,
             "n": "2026-01-01T00:00:00Z", "del": deleted_at},
        )


def seed_execution(
    sid: str,
    agent_name: str = "agent-1",
    *,
    exec_id: str | None = None,
    started_at: str = "2026-01-01T00:00:00.000000Z",
    status: str = "success",
    duration_ms: int | None = 1000,
    cost: float | None = 0.01,
    tool_calls=None,
    triggered_by: str = "schedule",
) -> str:
    """Insert a schedule_executions row via the active engine. Returns its id."""
    import json

    if exec_id is None:
        exec_id = f"e-{sid}-{started_at[-10:]}-{status}-{duration_ms}"
    if isinstance(tool_calls, list):
        tool_calls = json.dumps(tool_calls)
    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO schedule_executions "
                "(id, schedule_id, agent_name, status, started_at, duration_ms, "
                " cost, tool_calls, triggered_by, message) "
                "VALUES (:id, :sid, :a, :st, :sa, :du, :co, :tc, :tb, '')"
            ),
            {"id": exec_id, "sid": sid, "a": agent_name, "st": status,
             "sa": started_at, "du": duration_ms, "co": cost,
             "tc": tool_calls, "tb": triggered_by},
        )
    return exec_id
