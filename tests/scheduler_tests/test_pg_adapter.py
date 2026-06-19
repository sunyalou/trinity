"""
Unit tests for the scheduler's PostgreSQL paramstyle adapter (#300).

The scheduler's query methods are written for sqlite3 (qmark ``?`` params,
``row["col"]`` access). When DATABASE_URL points at PostgreSQL, get_connection
yields a _PgConn whose cursor adapts those calls for psycopg2: ``?``→``%s`` and
Python bool→int (INTEGER columns). These tests verify that translation against
a fake cursor — no database required.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

# config.py requires REDIS_URL with creds at import time (#589).
import os
os.environ.setdefault("REDIS_URL", "redis://test:test@localhost:6379")

from scheduler.database import _PgCursor, _PgConn, _scheduler_pg_url  # noqa: E402

pytestmark = pytest.mark.unit


class _FakeCursor:
    def __init__(self):
        self.calls = []
        self.rowcount = 0

    def execute(self, sql, params=()):
        self.calls.append((sql, params))

    def fetchone(self):
        return {"id": 1}

    def fetchall(self):
        return [{"id": 1}]

    def close(self):
        pass


def test_qmark_translated_to_pyformat():
    fake = _FakeCursor()
    _PgCursor(fake).execute("SELECT * FROM t WHERE a = ? AND b = ?", ("x", "y"))
    sql, params = fake.calls[0]
    assert sql == "SELECT * FROM t WHERE a = %s AND b = %s"
    assert params == ("x", "y")


def test_bool_params_coerced_to_int():
    fake = _FakeCursor()
    _PgCursor(fake).execute("INSERT INTO t(enabled, n) VALUES (?, ?)", (True, False))
    _, params = fake.calls[0]
    assert params == (1, 0)
    assert all(isinstance(p, int) and not isinstance(p, bool) for p in params)


def test_no_params_passthrough():
    fake = _FakeCursor()
    _PgCursor(fake).execute("SELECT 1")
    sql, params = fake.calls[0]
    assert sql == "SELECT 1"
    assert params == ()


def test_execute_returns_self_for_chaining():
    """Some call sites do cursor.execute(...).fetchone()."""
    cur = _PgCursor(_FakeCursor())
    assert cur.execute("SELECT 1").fetchone() == {"id": 1}


def test_pgconn_cursor_wraps():
    class _FakeConn:
        def cursor(self):
            return _FakeCursor()
        def commit(self): pass
        def rollback(self): pass
        def close(self): pass

    conn = _PgConn(_FakeConn())
    assert isinstance(conn.cursor(), _PgCursor)


@pytest.mark.parametrize("url,is_pg", [
    ("", False),
    ("sqlite:////data/trinity.db", False),
    ("postgresql://u:p@h:5432/db", True),
])
def test_pg_url_detection(monkeypatch, url, is_pg):
    if url:
        monkeypatch.setenv("DATABASE_URL", url)
    else:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    assert (_scheduler_pg_url() is not None) == is_pg
