"""Tests for #1153 — the orphan sweeper must spare live `docker exec`
sessions (PPID 0), consistent with the SSH-session protection, while still
reaping genuinely-leaked orphans reparented to PID 1.

`_docker_exec_session_pids()` allowlists every PPID-0 process (except PID 1)
plus its descendant tree. PID 1 also has PPID 0 but is hard-protected
separately and must NOT pull "all children of PID 1" into the allowlist.

The module reads real `/proc`; here we monkeypatch its `os.listdir` and the
`_read_ppid` / `_read_comm` / `_read_cmdline` helpers to drive a synthetic
process tree.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BASE_IMAGE = _REPO / "docker" / "base-image"
if str(_BASE_IMAGE) not in sys.path:
    sys.path.insert(0, str(_BASE_IMAGE))

from agent_server.utils import orphan_allowlist as oa  # noqa: E402


# Synthetic container PID namespace:
#   1   ppid 0   init (startup.sh)          — PPID 0 but excluded (hard-protected)
#   39  ppid 1   sudo                       — child of init (NOT auto-protected)
#   100 ppid 0   sh   (docker exec entry)   — PROTECT + descendants
#   101 ppid 100 indexer (exec child)       — protected (descendant)
#   102 ppid 101 python (grandchild)        — protected (descendant)
#   200 ppid 1   leaked orphan              — NOT protected (reparented to 1)
#   300 ppid 0   sh   (second exec session) — PROTECT
_TREE_PPID = {1: 0, 39: 1, 100: 0, 101: 100, 102: 101, 200: 1, 300: 0}
_TREE_COMM = {1: "startup.sh", 39: "sudo", 100: "sh", 101: "indexer",
              102: "python3", 200: "sh", 300: "sh"}
_TREE_CMD = {
    1: "/app/startup.sh", 39: "sudo tail", 100: "sh",
    101: "python3 build_index.py", 102: "python3 -c faiss",
    200: "sh -c leaked", 300: "sh",
}


@pytest.fixture
def fake_proc(monkeypatch):
    monkeypatch.setattr(oa.os, "listdir",
                        lambda p: [str(pid) for pid in _TREE_PPID])
    monkeypatch.setattr(oa, "_read_ppid", lambda pid: _TREE_PPID.get(pid))
    monkeypatch.setattr(oa, "_read_comm", lambda pid: _TREE_COMM.get(pid))
    monkeypatch.setattr(oa, "_read_cmdline", lambda pid: _TREE_CMD.get(pid))
    return monkeypatch


def test_docker_exec_entry_and_descendants_protected(fake_proc):
    got = oa._docker_exec_session_pids()
    # Both PPID-0 exec entries (100, 300) + the 100-rooted subtree (101, 102).
    assert got == {100, 101, 102, 300}


def test_pid1_excluded_from_exec_protection(fake_proc):
    # PID 1 has PPID 0 but must not be returned here (and its child 39 must
    # not be dragged in as a "descendant of an exec session").
    got = oa._docker_exec_session_pids()
    assert 1 not in got
    assert 39 not in got


def test_reparented_orphan_not_protected(fake_proc):
    # The leaked orphan (200, ppid 1) is exactly what the sweep must still
    # kill — it is not a PPID-0 session.
    assert 200 not in oa._docker_exec_session_pids()


def test_resolve_allowlist_includes_exec_session_excludes_orphan(fake_proc):
    # sweep_pid = the indexer's... no — sweep runs from agent-server. Use a
    # pid not in the exec tree so the only thing keeping 100/101/102 alive is
    # the new PPID-0 rule. 39 (sudo, child of init) stands in for agent-server.
    allow = oa.resolve_allowlist(39)
    assert {100, 101, 102, 300}.issubset(allow)  # exec sessions spared
    assert 1 in allow                              # init hard-protected
    assert 200 not in allow                        # leaked orphan still killable


def test_exec_child_reparented_after_session_exit_is_reaped(monkeypatch):
    # Session exited: pid 100 gone, its child 101 reparented to PID 1. With
    # no PPID-0 ancestor, 101 falls out of the exec allowlist → reaped.
    tree = {1: 0, 101: 1}
    monkeypatch.setattr(oa.os, "listdir", lambda p: [str(p) for p in tree])
    monkeypatch.setattr(oa, "_read_ppid", lambda pid: tree.get(pid))
    monkeypatch.setattr(oa, "_read_comm", lambda pid: None)
    monkeypatch.setattr(oa, "_read_cmdline", lambda pid: None)
    assert oa._docker_exec_session_pids() == set()
