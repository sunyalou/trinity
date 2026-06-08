"""
SQLAlchemy Core table definitions (#300 Phase 1).

A dialect-agnostic schema registry: ``metadata.create_all(engine)`` emits
SQLite or PostgreSQL DDL as appropriate (``INTEGER PRIMARY KEY AUTOINCREMENT``
vs ``SERIAL``, etc.). This is what lets one codebase target both backends.

Phase 1 defines only the ``users`` table — the ``db/users.py`` pilot. The rest
are added one at a time as each ``db/*.py`` module is migrated off raw SQL
(#300 Phase 2). Until a table appears here, the legacy ``db/schema.py`` DDL
remains its authoritative definition.

Timestamp columns stay ``Text`` (ISO-8601 strings via ``utc_now_iso()``),
consistent with Architectural Invariant #16 — converting them to native
``DateTime`` is deferred to a later phase to keep Phase 1 behavior-neutral.
"""

from sqlalchemy import Column, Integer, MetaData, Table, Text

metadata = MetaData()

# Mirrors the ``users`` DDL in db/schema.py. autoincrement integer PK →
# AUTOINCREMENT on SQLite, SERIAL on PostgreSQL.
users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("username", Text, nullable=False, unique=True),
    Column("password_hash", Text),
    Column("role", Text, nullable=False, server_default="user"),
    Column("auth0_sub", Text, unique=True),
    Column("name", Text),
    Column("picture", Text),
    Column("email", Text),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    Column("last_login", Text),
    Column("suspended_at", Text),  # #995 — NULL = active; set = deactivated
)
