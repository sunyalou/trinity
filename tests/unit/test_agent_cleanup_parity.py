"""
Parity test (Issue #816): every agent-referencing column declared in
`src/backend/db/schema.py` must have an entry in
`db.agent_cleanup.AGENT_REFS`.

This is the gate that keeps the cleanup registry honest. When a new
table adds an `agent_name` (or non-canonical `source_agent` /
`target_agent` / `subscriber_agent`) column without the author also
declaring a CASCADE/KEEP policy, this test fails — forcing the decision
at PR time instead of letting it drift silently into production as an
orphan-row leak.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

# Column names that identify an agent. Add new ones here if a future
# table introduces yet another spelling (e.g. "owner_agent").
AGENT_COLUMN_PATTERNS = re.compile(
    r"^(agent_name|source_agent|target_agent|subscriber_agent)$"
)

# Tables that are intentionally EXEMPT from the registry. Each exemption
# needs a reason that survives PR review.
EXEMPT_TABLES = {
    # agent_ownership IS the parent row — the caller manages its delete
    # / rename directly (cascade_delete returns and then the caller does
    # delete_agent_ownership; rename_agent updates it before calling
    # cascade_rename).
    "agent_ownership",
}


def _parse_schema() -> dict:
    """Return {table_name: [columns_matching_AGENT_COLUMN_PATTERNS]}."""
    schema_path = _BACKEND / "db" / "schema.py"
    text = schema_path.read_text(encoding="utf-8")

    # Match each `CREATE TABLE [IF NOT EXISTS] <name> ( ... )` block. The
    # body is delimited by the closing `)` of the CREATE TABLE statement;
    # we stop at the next standalone `)` line (the standard pattern in
    # schema.py).
    pattern = re.compile(
        r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?(\w+)\s*\((.*?)^\s*\)\s*",
        re.DOTALL | re.MULTILINE,
    )

    result: dict = {}
    for table_name, body in pattern.findall(text):
        cols = []
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("--"):
                continue
            # Skip FOREIGN KEY / UNIQUE / PRIMARY KEY constraint lines.
            if line.upper().startswith(("FOREIGN KEY", "UNIQUE", "PRIMARY KEY", "CHECK")):
                continue
            # Column declarations start with: <name> <TYPE> ...
            match = re.match(r"^([a-z_][a-z0-9_]*)\s+(?:TEXT|INTEGER|REAL|BLOB)", line)
            if not match:
                continue
            col_name = match.group(1)
            if AGENT_COLUMN_PATTERNS.match(col_name):
                cols.append(col_name)
        if cols:
            result[table_name] = cols
    return result


def test_every_agent_column_in_schema_has_registry_entry():
    """The forcing function: new agent-referencing columns must be
    classified by the AGENT_REFS registry before they merge."""
    from db.agent_cleanup import AGENT_REFS

    schema_refs = _parse_schema()
    registered = {(r.table, r.column) for r in AGENT_REFS}

    missing = []
    for table, cols in schema_refs.items():
        if table in EXEMPT_TABLES:
            continue
        for col in cols:
            if (table, col) not in registered:
                missing.append(f"  - {table}.{col}")

    assert not missing, (
        "Tables in db/schema.py reference an agent but have no entry in "
        "db.agent_cleanup.AGENT_REFS. Either add an AgentRef entry with "
        "Policy.CASCADE (delete on agent delete) or Policy.KEEP (preserve "
        "for rolling-window billing / forever record), or add the table "
        "to EXEMPT_TABLES in this test file with a justification.\n\n"
        "Missing entries:\n" + "\n".join(missing)
    )


def test_no_registry_entries_for_nonexistent_tables():
    """Inverse parity: the registry must not reference tables that no
    longer exist in schema.py (catches deletion drift)."""
    from db.agent_cleanup import AGENT_REFS

    schema_path = _BACKEND / "db" / "schema.py"
    text = schema_path.read_text(encoding="utf-8")
    existing_tables = set(
        re.findall(r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(\w+)\s*\(", text)
    )

    stale = [
        f"  - {r.table}.{r.column}"
        for r in AGENT_REFS
        if r.table not in existing_tables
    ]

    assert not stale, (
        "AGENT_REFS references tables not present in db/schema.py. "
        "Remove the entries or restore the tables.\n\n"
        "Stale entries:\n" + "\n".join(stale)
    )


def test_registry_has_no_duplicate_entries():
    """Defensive: each (table, column) pair must be unique."""
    from db.agent_cleanup import AGENT_REFS

    pairs = [(r.table, r.column) for r in AGENT_REFS]
    duplicates = [p for p in pairs if pairs.count(p) > 1]
    assert not duplicates, f"Duplicate AGENT_REFS entries: {set(duplicates)}"


def test_policy_enum_values():
    """Defensive: every entry has a valid Policy value."""
    from db.agent_cleanup import AGENT_REFS, Policy

    for r in AGENT_REFS:
        assert isinstance(r.policy, Policy), (
            f"AgentRef({r.table}, {r.column}) has non-Policy value: {r.policy!r}"
        )
