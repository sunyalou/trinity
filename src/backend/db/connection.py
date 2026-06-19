"""
Database connection utilities.

Provides the shared connection context manager and database path configuration.
"""

import os
import sqlite3
from contextlib import contextmanager


def _resolve_sqlite_path() -> str:
    """Resolve the on-disk SQLite path, honoring a SQLite ``DATABASE_URL`` (#300).

    When ``DATABASE_URL`` names a SQLite backend its path wins, so the legacy
    raw-sqlite3 modules and the new SQLAlchemy engine agree on one file. A
    non-SQLite ``DATABASE_URL`` (e.g. PostgreSQL) is ignored here: modules not
    yet migrated to SQLAlchemy Core (#300 Phase 2 pending) still require a
    SQLite file, so we fall back to ``TRINITY_DB_PATH``.
    """
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        try:
            from sqlalchemy.engine import make_url

            parsed = make_url(url)
            if parsed.get_backend_name() == "sqlite" and parsed.database:
                return parsed.database
        except Exception:
            pass  # malformed URL or sqlalchemy unavailable → fall through
    return os.getenv("TRINITY_DB_PATH", "/data/trinity.db")


# Database path - stored in trinity-data volume. Kept as a module-level
# attribute because tests monkeypatch it directly (db.connection.DB_PATH).
DB_PATH = _resolve_sqlite_path()


@contextmanager
def get_db_connection():
    """Context manager for database connections with proper transaction handling."""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
