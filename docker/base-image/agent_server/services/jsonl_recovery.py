"""JSONL fallback recovery for Claude Code stdout pipe races.

Extracted from `claude_code.py` per #122 (issue split). Provides authoritative
post-turn recovery from `~/.claude/projects/<dir>/<uuid>.jsonl` — Claude
Code's session record, written via a side channel independent of stdout.

Two recovery surfaces:

1. ``_recover_response_from_jsonl`` — when the stdout pipe race fires
   mid-tool-call and ``response_parts`` is empty, walk the JSONL forward
   from the most recent user-input boundary and reconstruct the assistant
   text emitted during this turn.

2. ``_extract_compact_events_from_jsonl`` — Claude Code's stdout
   ``compact_boundary`` event strips the ``compactMetadata`` envelope
   (we get the signal but no pre/post/duration detail). The JSONL has
   the canonical shape; extract events ≥ ``since_iso`` to scope to the
   just-completed turn.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import List, Optional

from ..models import CompactEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSONL fallback recovery (stdout pipe race — final safety net)
# ---------------------------------------------------------------------------
#
# Claude Code persists every turn to ~/.claude/projects/<dir>/<uuid>.jsonl
# via a side-channel that's INDEPENDENT of stdout. When a tool subprocess
# (or MCP grandchild) inherits claude's stdout fd and wedges the agent
# server's reader thread, the stream-json result event is lost — but the
# JSONL on disk usually contains the completed turn.
#
# The Phase 5.1 soft-recovery (response_parts != [] → synthesize success)
# only fires when stdout managed to deliver at least one assistant text
# block before the wedge. For races that fire mid-tool-call (zero text
# emitted), response_parts is empty and the soft-recovery falls through
# to a hard 502.
#
# This helper is the next layer down: when stdout failed AND
# response_parts is empty, read the JSONL and pull the assistant text
# emitted during the just-completed turn. The data is authoritative
# (Claude Code's own session record), so when the read succeeds we can
# synthesize a full soft-success response and surface
# `metadata.recovered_from_jsonl = True` for observability.
#
# Recovery is bounded: we only walk forward from the most recent user
# input message (string content, not a tool_result), so prior turns'
# text never leaks into this turn's response.

_JSONL_PROJECTS_DIR = "/home/developer/.claude/projects/-home-developer"
_MAX_JSONL_BYTES_FOR_RECOVERY = 10 * 1024 * 1024  # 10MB cap on read


def _recover_response_from_jsonl(session_id: Optional[str]) -> Optional[str]:
    """Try to recover an assistant text response from a Claude Code JSONL.

    Returns the concatenated text of all assistant.text blocks emitted
    after the most recent user-input message in the JSONL, or None when:
      - session_id is missing
      - the JSONL file doesn't exist or can't be read
      - no user-input boundary is found (shouldn't happen in practice)
      - no assistant text was emitted after the boundary (Claude died
        mid-tool-call before writing any text — genuinely incomplete).

    The boundary uses the shape difference between user inputs (string
    content) and tool_results (list-of-dicts content) — Claude Code
    records them with different types in the JSONL.
    """
    if not session_id:
        return None

    jsonl_path = Path(f"{_JSONL_PROJECTS_DIR}/{session_id}.jsonl")
    if not jsonl_path.exists():
        return None

    try:
        if jsonl_path.stat().st_size > _MAX_JSONL_BYTES_FOR_RECOVERY:
            # Cap read size — turns rarely produce more than a few hundred
            # KB; pathological JSONLs shouldn't hang recovery indefinitely.
            with jsonl_path.open("rb") as f:
                f.seek(-_MAX_JSONL_BYTES_FOR_RECOVERY, os.SEEK_END)
                # Skip the partial first line after seeking mid-file.
                f.readline()
                raw = f.read().decode("utf-8", errors="replace")
        else:
            raw = jsonl_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[JSONL Recovery] Failed to read {jsonl_path}: {e}"
        )
        return None

    lines = raw.strip().split("\n")

    # Walk backward to find the boundary: the most recent user-INPUT
    # message (content is a string, not a list). tool_result entries
    # also have type=user but their content is a list of dicts.
    boundary_idx = None
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict) or entry.get("type") != "user":
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            boundary_idx = i
            break

    if boundary_idx is None:
        return None

    # Collect assistant.text blocks emitted after the boundary. Skip
    # tool_use blocks (no user-facing text), thinking blocks (model's
    # internal reasoning, never shown), and any non-list content.
    text_parts: List[str] = []
    for line in lines[boundary_idx + 1:]:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text") or ""
                if text:
                    text_parts.append(text)

    if not text_parts:
        return None

    return "\n".join(text_parts)


def _extract_compact_events_from_jsonl(
    session_id: Optional[str], since_iso: Optional[str] = None
) -> List["CompactEvent"]:
    """Read compact_boundary records out of a Claude Code JSONL.

    Claude Code's `--output-format stream-json --verbose` emits
    `compact_boundary` events to stdout but strips the `compactMetadata`
    envelope (we get the event-fired signal but no pre/post/duration
    detail). The JSONL on disk has the canonical shape:

        {"type": "system", "subtype": "compact_boundary",
         "compactMetadata": {"trigger":"auto", "preTokens":175061,
                             "postTokens":5904, "durationMs":73651},
         "timestamp": "2026-05-04T13:01:56.959Z", ...}

    This helper is called AFTER a turn completes to populate
    `metadata.compact_events` with the real detail fields. ``since_iso``
    filters to compact records emitted at or after the given ISO
    timestamp — used to scope the result to the just-completed turn
    when the JSONL has compact records from prior turns.

    Returns an empty list when the session_id is missing, the file
    doesn't exist, or no compact records are present.
    """
    if not session_id:
        return []

    jsonl_path = Path(f"{_JSONL_PROJECTS_DIR}/{session_id}.jsonl")
    if not jsonl_path.exists():
        return []

    try:
        if jsonl_path.stat().st_size > _MAX_JSONL_BYTES_FOR_RECOVERY:
            with jsonl_path.open("rb") as f:
                f.seek(-_MAX_JSONL_BYTES_FOR_RECOVERY, os.SEEK_END)
                f.readline()
                raw = f.read().decode("utf-8", errors="replace")
        else:
            raw = jsonl_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[JSONL Compact Extract] Failed to read {jsonl_path}: {e}"
        )
        return []

    events: List["CompactEvent"] = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "system" or entry.get("subtype") != "compact_boundary":
            continue
        ts = entry.get("timestamp")
        if since_iso and isinstance(ts, str) and ts < since_iso:
            continue
        cm = entry.get("compactMetadata") or {}
        if not isinstance(cm, dict):
            cm = {}
        events.append(CompactEvent(
            trigger=cm.get("trigger"),
            pre_tokens=cm.get("preTokens"),
            post_tokens=cm.get("postTokens"),
            duration_ms=cm.get("durationMs"),
            timestamp=ts if isinstance(ts, str) else None,
        ))

    return events
