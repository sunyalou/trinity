---
name: validate-architecture
description: Detect drift between architecture.md and the actual code. Validates 15 architectural invariants and flags stale doc claims with suggested edits.
allowed-tools: [Read, Grep, Glob, Bash, Agent]
user-invocable: true
---

# Validate Architecture

## Purpose

Check the codebase against the 15 Architectural Invariants in @docs/memory/architecture.md, and detect drift between the doc's quantitative and scope claims and the actual code. Output: invariant violations plus suggested architecture.md edits. No files are modified — read-only analysis.

The architecture doc is a living artifact. When counts or scope claims drift, this skill proposes a concrete edit (`architecture.md:L<N> — update "X" → "Y"`) rather than just marking FAIL.

## Process

### Step 1: Load Invariants

Read the "Architectural Invariants" section from `docs/memory/architecture.md` to get the current list. Also parse its "Component Details", "Backend", and per-router sections for quantitative claims (line counts, router counts, tool counts, endpoint counts) — these feed the drift checks in Step 2b.

### Step 2a: Validate Each Invariant

Run the checks below. For each invariant, record PASS or FAIL with evidence.

**1. Three-Layer Backend: Router → Service → DB**
- Grep `routers/*.py` for raw SQL (`execute(`, `cursor`, `SELECT`, `INSERT`, `UPDATE`, `DELETE`) — routers must not contain SQL
- Grep `routers/*.py` for business logic patterns (complex conditionals, loops over data) beyond simple request/response handling
- Grep `db/*.py` for HTTP-specific imports (`fastapi`, `Request`, `Response`) — db layer must not know about HTTP

**2. DB Layer: Class-per-domain with Mixin Composition**
- Glob `src/backend/db/*.py` and verify each defines an `*Operations` class
- Glob `src/backend/db/agent_settings/*.py` and verify each defines a `*Mixin` class
- Check that `AgentOperations` in `db/agents.py` uses mixin inheritance

**3. Schema in `db/schema.py`, Migrations in `db/migrations.py`**
- Grep all `src/backend/` files (excluding `schema.py` and `migrations.py`) for `CREATE TABLE` — should find none
- Verify `db/schema.py` and `db/migrations.py` both exist

**4. Router Registration Order**
- Read `src/backend/main.py` and find the `include_router` block
- Check that static agent routes (`context-stats`, `autonomy-status`) are registered before the main `agents` router with `/{name}` params

**5. Agent Server Mirrors Backend (Subset)**
- Glob `docker/base-image/agent_server/routers/*.py` and list them
- For each agent-server router, verify a corresponding backend router exists in `src/backend/routers/`
- Flag any agent-server router that has no backend counterpart

**6. Frontend: Store = Domain, View = Page**
- Grep `src/frontend/src/views/*.vue` for direct `api.get(`, `api.post(`, `api.put(`, `api.delete(` calls — views should go through stores
- Grep `src/frontend/src/views/*.vue` for `import api` or `import { api` — views should not import the API client directly

**7. Single API Client (`api.js`)**
- Grep `src/frontend/src/` for `new axios` or `axios.create` — should only be in `api.js`
- Grep `src/frontend/src/` for raw `fetch(` calls — should find none (except in non-API contexts like file downloads)

**8. Auth Pattern: `Depends(get_current_user)` + `AuthorizedAgent`**
- Grep `src/backend/routers/*.py` for route handlers (decorated with `@router.get`, `@router.post`, etc.)
- For each router file (except `internal.py`, `setup.py`, `auth.py`, `public.py`), verify at least one endpoint uses `get_current_user` or `AuthorizedAgent` or `OwnedAgentByName`
- Check that `internal.py` does NOT use `get_current_user`
- **Inline authorization sprawl** — grep router endpoint bodies for permission-check patterns that should be dependencies: `db.can_user_`, `db.is_system_agent`, `current_user.username !=`, `if not ... owner`, and hand-rolled `raise HTTPException(status_code=403` blocks. Permission logic should live in a `Depends()` dependency, not inline in each endpoint. List every occurrence (file:line) and FAIL if more than 5 distinct sites.

**9. Channel Adapter ABC**
- Verify `src/backend/adapters/base.py` exists and defines `ChannelAdapter` class
- Check that adapter implementations (`slack_adapter.py`, etc.) inherit from `ChannelAdapter`

**10. WebSocket Events for Real-Time**
- Grep `src/frontend/src/` for `setInterval` or `setTimeout` patterns that poll API endpoints — flag as potential violations (should use WebSocket instead)
- Verify `src/frontend/src/utils/websocket.js` exists

**11. Docker as Source of Truth**
- Grep `src/backend/` for container state stored in global variables or module-level dicts (e.g., `running_agents = {}`, `container_cache = {}`) — should not exist
- Verify `docker_service.py` exists as the Docker interaction point

**12. Credentials: File Injection, Never Stored in DB**
- Grep `db/schema.py` for any table that stores credential values (not references/metadata) — should find none
- Grep `src/backend/` for patterns that write credential values to SQLite

**13. MCP Server = Third Surface in Sync** (enforced)
- Glob `src/mcp-server/src/tools/*.ts` and build the MCP tool-module list
- Glob `src/backend/routers/*.py` and build the backend-domain list, excluding `internal.py`, `setup.py`, `auth.py`, `public.py`, `paid.py` (these are not externally accessible via MCP by design)
- For each remaining backend domain, require one of:
  - a corresponding tool module in `src/mcp-server/src/tools/`, OR
  - an explicit `# mcp: none` comment at the top of the router declaring intentional exclusion (with a one-line reason)
- FAIL for any domain without either. Enforced, not advisory.

**14. Pydantic Models Centralized in `models.py`**
- Grep `src/backend/routers/*.py` for `class.*BaseModel` or `class.*Model(` definitions — models should be in `models.py`, not routers
- Count models in `models.py` vs scattered across other files

**15. API URL Nesting Convention**
- Grep `src/backend/routers/*.py` for `APIRouter(prefix=` and list all prefixes
- Flag any agent-scoped resource that doesn't nest under `/api/agents/{name}/`
- Flag any platform-wide resource that incorrectly nests under `/api/agents/`

### Step 2b: Detect Doc Drift

These checks compare `architecture.md` claims against repo reality and propose concrete doc edits, not pass/fail alone.

**D1. Quantitative count alignment**

For each claim in `architecture.md`, compute the actual value and compare:

| Claim in arch.md | Actual value (compute) |
|------------------|------------------------|
| `main.py` line count | `wc -l src/backend/main.py` |
| Router count / list of routers | `ls src/backend/routers/*.py` (exclude `__init__.py`) |
| Service module count | `ls src/backend/services/*.py` (exclude `__init__.py`) |
| MCP tool count (modules + total tools) | `ls src/mcp-server/src/tools/*.ts` plus count of exported tools |
| Per-router endpoint counts | `grep -E "@router\.(get\|post\|put\|delete)" src/backend/routers/<name>.py` |

For each divergence >10%, emit a suggested doc edit:

```
architecture.md:L<N> — claim "main.py ... 182 lines" does not match actual (860 lines).
  Suggested edit: update to "860 lines" (or drop the parenthetical).
```

**D2. Scope-coherence check**

Grep `architecture.md` for markers: `OUT OF SCOPE`, `dormant`, `not currently being developed`, `deprecated`. For each match, extract the named module path (e.g., `src/backend/services/process_engine/`).

Then:
- Grep `src/backend/main.py` for imports of that module or for routers in that area
- Grep `src/backend/routers/*.py` for imports from that module
- If the supposedly-dormant module is actively imported, or its routers are registered in `main.py`, emit a suggested doc edit:

```
architecture.md:L<N> — Process Engine marked "OUT OF SCOPE" but routers/processes.py, routers/approvals.py, routers/triggers.py are registered in main.py.
  Suggested edit: either remove the OUT OF SCOPE tag (if the module is in fact live), or remove the routers (if it is truly dormant).
```

### Step 3: Generate Report

Output two sections:

```
## Architecture Validation Report

### Invariant Compliance

| # | Invariant | Status | Details |
|---|-----------|--------|---------|
| 1 | Three-Layer Backend | PASS/FAIL | ... |
...

**Result: X/15 PASS, Y/15 FAIL**

#### Violations

##### [Invariant Name]
- **File**: path/to/file.py:line
- **Issue**: Description
- **Fix**: Suggested remediation

### Doc Drift — Suggested architecture.md Edits

#### D1. Count mismatches
- architecture.md:L<N> — "<claim>" vs actual "<value>". Suggested edit: "<new text>".

#### D2. Scope contradictions
- architecture.md:L<N> — "<section>" marked out-of-scope but <evidence of activity>. Suggested edit: <resolution>.
```

### Step 4: Create Issue if Critical

Create a GitHub issue when any of these fire:

**P0-P1 invariants** (critical — break runtime or security):
- #1 Three-Layer Backend (layer violations cause maintenance debt)
- #8 Auth Pattern (missing auth = security hole; inline authorization sprawl = scattered security logic)
- #12 Credentials Never in DB (credential exposure)
- #3 Schema in schema.py (ad-hoc tables break migrations)

**P1 drift** (misleading docs cause cascading downstream errors):
- D1 count mismatches with >25% divergence
- D2 any scope contradiction (dormant-but-live modules)

If any fire, create or update an issue:

```bash
# Check for existing open architecture validation issue
EXISTING=$(gh issue list --repo abilityai/trinity --state open --limit 50 --json number,title --jq '.[] | select(.title | startswith("Architecture Validation:")) | .number' | head -1)

TITLE="Architecture Validation: [N] critical violations ([invariant names])"
BODY="## Automated Architecture Validation Report

**Date**: $(date -u +%Y-%m-%d)

### Critical Invariant Violations (P0-P1)

[List each P0-P1 violation with invariant number, file:line, description]

### Doc Drift — Suggested architecture.md Edits

[List each suggested edit with line number and replacement text]

### Recommended Actions

1. [Prioritized fix for each finding]

---
*Generated by scheduled /validate-architecture run*"

if [ -n "$EXISTING" ]; then
  # Update existing issue with latest findings
  gh issue edit $EXISTING --repo abilityai/trinity --title "$TITLE" --body "$BODY"
  echo "Updated existing issue #$EXISTING"
else
  # Create new issue
  gh issue create --repo abilityai/trinity --title "$TITLE" --body "$BODY" \
    --label "type-bug,priority-p1,automated"
fi
```

If nothing critical fires, skip issue creation — report only logged to execution history.

## Outputs

- Markdown report (invariant compliance + doc drift suggestions) printed to conversation
- GitHub issue created if P0-P1 invariant violations OR P1 drift findings exist (labeled `automated`, `priority-p1`)
- No files modified
