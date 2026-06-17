"""
Unit tests for FileSharingMixin (amazing-file-outbound Step 2).

Covers the DB-side toggle + static helpers:
- Default value is False when no row exists
- Default value is False when column is NULL
- set → get round trip
- Flip on, then off — idempotent rewrites
- Volume-name and mount-path conventions
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# The mixin under test was migrated off the raw-sqlite `get_db_connection`
# seam to the SQLAlchemy engine seam (#300): it now uses `get_engine()`,
# which reads `DATABASE_URL`. We import it through the real `db` package and
# route both the seeding sqlite3 connection AND the engine at the same temp
# file via `DATABASE_URL` + `dispose_engines()` (engine cache is keyed by URL).
_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


@pytest.fixture
def tmp_db_conn(tmp_path, monkeypatch):
    """Provision an empty DB with just agent_ownership and the new column.

    Returns a sqlite3 connection the test can seed with rows. The same file
    is wired to the SQLAlchemy engine via DATABASE_URL so the mixin's
    `get_engine()` ops hit this exact database.
    """
    db_path = tmp_path / "trinity.db"
    # Legacy seam (harmless; not-yet-converted modules still read it) +
    # the engine seam (#300). dispose_engines after setting DATABASE_URL so
    # the temp file's engine is the one the cache returns.
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE agent_ownership (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT UNIQUE NOT NULL,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            file_sharing_enabled INTEGER DEFAULT 0,
            deleted_at TEXT  -- #834: read paths filter `WHERE deleted_at IS NULL`
        )
        """
    )
    conn.commit()

    import db.engine as engine_mod
    engine_mod.dispose_engines()
    yield conn
    engine_mod.dispose_engines()
    conn.close()


@pytest.fixture
def mixin(tmp_db_conn):
    """The real FileSharingMixin, routed to the tmp DB via DATABASE_URL.

    Skips if the backend package can't be imported (no venv).
    """
    try:
        from db.agent_settings.file_sharing import FileSharingMixin
    except ImportError:
        pytest.skip("backend venv required (no `db.agent_settings` import)")

    class _Wrapper(FileSharingMixin):
        pass

    return _Wrapper()


def _insert_agent(conn, name, enabled=None):
    if enabled is None:
        conn.execute(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
            "VALUES (?, 1, 'now')",
            (name,),
        )
    else:
        conn.execute(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at, file_sharing_enabled) "
            "VALUES (?, 1, 'now', ?)",
            (name, 1 if enabled else 0),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Getter — defaults
# ---------------------------------------------------------------------------


def test_default_false_when_no_row(mixin):
    assert mixin.get_file_sharing_enabled("ghost-agent") is False


def test_default_false_when_column_unset(mixin, tmp_db_conn):
    _insert_agent(tmp_db_conn, "a1")
    assert mixin.get_file_sharing_enabled("a1") is False


def test_false_when_column_is_zero(mixin, tmp_db_conn):
    _insert_agent(tmp_db_conn, "a1", enabled=False)
    assert mixin.get_file_sharing_enabled("a1") is False


def test_true_when_column_is_one(mixin, tmp_db_conn):
    _insert_agent(tmp_db_conn, "a1", enabled=True)
    assert mixin.get_file_sharing_enabled("a1") is True


# ---------------------------------------------------------------------------
# Setter — round-trip
# ---------------------------------------------------------------------------


def test_set_true_then_get(mixin, tmp_db_conn):
    _insert_agent(tmp_db_conn, "a1")
    assert mixin.set_file_sharing_enabled("a1", True) is True
    assert mixin.get_file_sharing_enabled("a1") is True


def test_set_false_then_get(mixin, tmp_db_conn):
    _insert_agent(tmp_db_conn, "a1", enabled=True)
    assert mixin.set_file_sharing_enabled("a1", False) is True
    assert mixin.get_file_sharing_enabled("a1") is False


def test_set_is_idempotent(mixin, tmp_db_conn):
    _insert_agent(tmp_db_conn, "a1", enabled=True)
    assert mixin.set_file_sharing_enabled("a1", True) is True
    assert mixin.set_file_sharing_enabled("a1", True) is True
    assert mixin.get_file_sharing_enabled("a1") is True


def test_set_returns_false_when_agent_missing(mixin):
    """No-op UPDATE should report 0 rowcount back to the caller."""
    assert mixin.set_file_sharing_enabled("ghost", True) is False


def test_set_isolated_between_agents(mixin, tmp_db_conn):
    _insert_agent(tmp_db_conn, "a1")
    _insert_agent(tmp_db_conn, "a2")
    mixin.set_file_sharing_enabled("a1", True)
    assert mixin.get_file_sharing_enabled("a1") is True
    assert mixin.get_file_sharing_enabled("a2") is False


# ---------------------------------------------------------------------------
# Static helpers — volume name + mount path conventions
# ---------------------------------------------------------------------------


def test_volume_name_convention(mixin):
    assert mixin.get_public_volume_name("alpha") == "agent-alpha-public"
    assert mixin.get_public_volume_name("xyz-1") == "agent-xyz-1-public"


def test_mount_path_is_constant(mixin):
    assert mixin.get_public_mount_path() == "/home/developer/public"


def test_mount_path_does_not_depend_on_agent(mixin):
    """Different agents share the same in-container mount point."""
    # Method doesn't take agent_name today — pin the contract so a
    # future change that introduces per-agent paths has to explicitly
    # break this test and justify it.
    import inspect
    sig = inspect.signature(mixin.get_public_mount_path)
    assert list(sig.parameters) == []
