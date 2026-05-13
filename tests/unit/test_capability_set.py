"""
Pin the FULL_CAPABILITIES set so future PRs can't silently re-add the
caps that Issue #602 / Phase 3c removed.

Each removed cap is a known security primitive:
- SYS_PTRACE  — read other process memory (AISEC-C2 token exfil path)
- MKNOD       — create /dev nodes (container-escape primitive)
- NET_RAW     — raw / ICMP sockets (packet crafting, ARP spoof)
- FSETID      — preserve setuid bits on chmod (priv-escalation primitive)

If a future caller has a genuine need for one of these, the right path
is: document the use case here, then remove the entry from the
forbidden list with a justification — not silently revert
lifecycle.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


REMOVED_BY_ISSUE_602 = {
    "SYS_PTRACE",
    "MKNOD",
    "NET_RAW",
    "FSETID",
}


def test_restricted_set_minimal():
    """Restricted (default) set must stay tight — no debugger/raw-socket
    caps even at the baseline."""
    from services.agent_service.lifecycle import RESTRICTED_CAPABILITIES

    leaked = REMOVED_BY_ISSUE_602 & set(RESTRICTED_CAPABILITIES)
    assert not leaked, (
        f"RESTRICTED_CAPABILITIES regained Issue #602 forbidden caps: {leaked}. "
        "These are security primitives — see lifecycle.py comments before re-adding."
    )


def test_full_set_excludes_issue_602_removals():
    """FULL_CAPABILITIES (apt-install mode) must not regain the caps
    Issue #602 / Phase 3c removed."""
    from services.agent_service.lifecycle import FULL_CAPABILITIES

    leaked = REMOVED_BY_ISSUE_602 & set(FULL_CAPABILITIES)
    assert not leaked, (
        f"FULL_CAPABILITIES regained Issue #602 forbidden caps: {leaked}. "
        "Each entry in REMOVED_BY_ISSUE_602 is a documented security "
        "primitive — see lifecycle.py FULL_CAPABILITIES comment block "
        "before re-adding."
    )


def test_full_set_remains_a_superset_of_restricted():
    """FULL must always include everything in RESTRICTED — tightening
    the FULL set should not accidentally drop a baseline cap."""
    from services.agent_service.lifecycle import (
        FULL_CAPABILITIES,
        RESTRICTED_CAPABILITIES,
    )

    missing = set(RESTRICTED_CAPABILITIES) - set(FULL_CAPABILITIES)
    assert not missing, (
        f"FULL_CAPABILITIES dropped baseline caps: {missing}. "
        "FULL must remain a superset of RESTRICTED."
    )


def test_prohibited_caps_never_appear_in_either_set():
    """SYS_ADMIN-class caps must never leak into RESTRICTED or FULL.
    PROHIBITED_CAPABILITIES is the documented blocklist."""
    from services.agent_service.lifecycle import (
        FULL_CAPABILITIES,
        PROHIBITED_CAPABILITIES,
        RESTRICTED_CAPABILITIES,
    )

    leaks_full = set(PROHIBITED_CAPABILITIES) & set(FULL_CAPABILITIES)
    leaks_restricted = set(PROHIBITED_CAPABILITIES) & set(RESTRICTED_CAPABILITIES)
    assert not leaks_full, f"PROHIBITED caps in FULL set: {leaks_full}"
    assert not leaks_restricted, f"PROHIBITED caps in RESTRICTED set: {leaks_restricted}"
