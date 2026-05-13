"""
Linux capability sets for agent containers (Issue #602 — Phase 3c).

This module is intentionally stdlib-only and import-light so it can be
exercised by `tests/unit/test_capability_set.py` without dragging the
docker / fastapi / database transitive imports of `lifecycle.py` into
the test runner. `lifecycle.py` re-exports these names for callers.

Trinity always launches agent containers with `cap_drop=ALL` and then
re-adds one of these sets. The pattern (defense in depth):

    cap_drop = ['ALL']
    cap_add  = FULL_CAPABILITIES if full_capabilities else RESTRICTED_CAPABILITIES
"""

from __future__ import annotations


# Restricted mode capabilities - minimum for agent operation (default)
RESTRICTED_CAPABILITIES: list[str] = [
    'NET_BIND_SERVICE',  # Bind to ports < 1024
    'SETGID', 'SETUID',  # Change user/group (for su/sudo)
    'CHOWN',             # Change file ownership
    'SYS_CHROOT',        # Use chroot
    'AUDIT_WRITE',       # Write to audit log
]

# Full capabilities mode - adds package installation support.
# Used when agents need apt-get, pip install, etc.
#
# Issue #602 / Phase 3c (cap tightening): four caps removed from this
# set after AISEC-C2 review. The remaining set is the minimum that keeps
# `sudo apt install` working inside an agent container.
#
# Dropped (no defensible agent use case — documented here so a future
# PR doesn't silently re-add them):
#   SYS_PTRACE  Lets a process read another process's memory. A malicious
#               MCP server could read Claude Code's heap and exfil the
#               OAuth token even if the token isn't in env. This is the
#               direct AISEC-C2 escalation path; removing it closes it
#               without waiting for Layer 3b (bubblewrap sandbox).
#   MKNOD       Creates device nodes under /dev. Agents have no use case
#               for /dev/* manipulation; primarily a container-escape
#               primitive (e.g. creating a writable raw disk device).
#   NET_RAW     Raw / ICMP sockets. Trinity's "ping another agent" UX is
#               HTTP-level, not ICMP. Removing this prevents raw-packet
#               crafting (TCP RST injection, ARP spoofing on the docker
#               bridge, etc.).
#   FSETID      Lets a process keep setuid/setgid bits on chmod after a
#               non-owner write — used to plant a setuid binary the next
#               privileged path can run. No agent workflow needs it.
FULL_CAPABILITIES: list[str] = RESTRICTED_CAPABILITIES + [
    'DAC_OVERRIDE',      # Bypass file permission checks (needed for sudo apt)
    'FOWNER',            # Bypass permission checks on file owner
    'KILL',              # Send signals to processes
]

# These capabilities are NEVER granted - they pose significant security risks.
# Listed for documentation; we achieve this by always using cap_drop=['ALL'].
PROHIBITED_CAPABILITIES: list[str] = [
    'SYS_ADMIN',         # Mount filesystems, configure namespace - too powerful
    'NET_ADMIN',         # Network administration - could escape container
    'SYS_RAWIO',         # Raw I/O access - direct hardware access
    'SYS_MODULE',        # Load kernel modules - kernel compromise
    'SYS_BOOT',          # Reboot system
]
