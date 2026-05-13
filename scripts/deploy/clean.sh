#!/bin/bash
#
# Clean leftover Trinity state so a fresh install starts from zero.
#
# Issue #443: `stop.sh` runs `docker compose down` which only stops the
# platform services. Agent containers (created at runtime, named
# `agent-*`) and the `trinity-agent-network` they share are NOT part of
# the compose file, so they survive. On the next `start.sh` the first
# new agent collides on `AGENT_SSH_PORT_START` (2222), and old agents
# show up in `docker ps -a` as zombie state.
#
# What this script touches:
#   - stops + removes every `agent-*` container (incl. Exited zombies)
#   - removes the `trinity-agent-network` bridge (recreated on next up)
#
# What it does NOT touch:
#   - data volumes (`trinity-data`, `redis-data`, archives, agent
#     workspaces) — your trinity.db and agent files stay intact
#   - the .env file
#
# To also wipe data, append your own:
#   docker volume rm trinity_trinity-data trinity_redis-data ...
#
# Usage:
#   ./scripts/deploy/clean.sh
#
# Idempotent — safe to run when nothing is leftover.

set -e

cd "$(dirname "$0")/../.."

echo "====================================="
echo "Trinity Agent Platform - Clean"
echo "====================================="
echo ""

# 1. Bring the compose stack down (no-op if already stopped).
if docker compose ps --quiet 2>/dev/null | grep -q .; then
    echo "Stopping platform services..."
    docker compose down
fi

# 2. Remove agent-* containers. They're created outside compose so
# `down` doesn't reach them.
agent_ids=$(docker ps -a --filter "name=agent-" --format "{{.ID}}")
if [ -n "$agent_ids" ]; then
    echo "Removing $(echo "$agent_ids" | wc -l | tr -d ' ') leftover agent container(s)..."
    # shellcheck disable=SC2086  # word-split is intentional — docker rm
    # takes multiple IDs as separate args.
    docker rm -f $agent_ids >/dev/null
else
    echo "No leftover agent containers."
fi

# 3. Remove the agent bridge network. Compose recreates it on next up.
if docker network ls --format "{{.Name}}" | grep -qx "trinity-agent-network"; then
    echo "Removing trinity-agent-network..."
    docker network rm trinity-agent-network >/dev/null || true
fi

echo ""
echo "✅ Clean complete. Data volumes preserved."
echo "   Run ./scripts/deploy/start.sh to bring everything back up."
echo ""
