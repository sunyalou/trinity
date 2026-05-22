# Enterprise modules — local development

Clone-to-running guide for working on the closed-source enterprise
modules (`Abilityai/trinity-enterprise`) alongside the public Trinity
backend. Target: **15 minutes from `git clone` to a running
`/api/enterprise/sso/providers` endpoint.**

> **Access required**: read access to the private repo
> `Abilityai/trinity-enterprise`. Ask the project owner for an
> invite if you don't have it yet.

## TL;DR

```bash
git clone --recurse-submodules git@github.com:abilityai/trinity.git
cd trinity
./scripts/deploy/start.sh
curl http://localhost:8000/api/enterprise/sso/providers
# → []
```

That's it. The conditional loader in `src/backend/main.py` picks up
the submodule automatically.

## Step-by-step

### 1. Clone with submodules (1 min)

```bash
git clone --recurse-submodules git@github.com:abilityai/trinity.git
cd trinity
```

If you already cloned without `--recurse-submodules`, pull the
submodules in:

```bash
git submodule update --init --recursive
```

`.gitmodules` mounts three private submodules (the enterprise repo is
**dual-mounted** — same URL, two paths, so backend and frontend each
get a clean import surface):

| Submodule | Path | Consumed by | Subdir read |
|---|---|---|---|
| `.claude` | `.claude/` | Claude Code skills (`/sprint`, `/cso`, …) | (all) |
| `src/backend/enterprise` | `src/backend/enterprise/` | Python (`main.py`) | `backend/` |
| `src/frontend/src/enterprise` | `src/frontend/src/enterprise/` | Vite (`main.js`) | `frontend/` |

The two enterprise mounts clone the same repo, so disk usage is ~2×
the repo size. In exchange you get a single enterprise codebase to
version, and each consumer imports only the subdir it needs.

Both use SSH (`git@github.com:...`). If you only have HTTPS auth
configured, add an SSH override:

```bash
git config --global url."git@github.com:".insteadOf "https://github.com/"
git submodule sync --recursive
git submodule update --init --recursive
```

### 2. (Recommended) Auto-sync on branch switch (10 sec)

Without this, switching branches leaves the submodules stale and
imports fail silently:

```bash
git config submodule.recurse true
```

### 3. Start the stack (5 min, mostly Docker build)

```bash
./scripts/deploy/start.sh
```

This boots Redis, Vector, the backend (which conditionally loads the
enterprise submodule), MCP server, and the frontend.

### 4. Verify enterprise is wired (10 sec)

```bash
# Endpoint exists and responds
curl http://localhost:8000/api/enterprise/sso/providers
# → []

# Feature flags expose the entitled enterprise list
TOKEN=$(curl -s -X POST http://localhost:8000/api/token \
  -d 'username=admin&password='"$ADMIN_PASSWORD" | jq -r .access_token)
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/settings/feature-flags | jq .enterprise_features
# → ["sso", "scim", "siem"]
```

If you see those responses, the submodule is mounted, the conditional
import worked, and the entitlement seam reports all features enabled
(the Phase 0 stub).

## Working on the enterprise repo

The repo is dual-mounted. Pick **one** of the two mounts to make
changes in (typically `src/backend/enterprise/` since that's where
you most often start) — `git push` from there updates the upstream,
then the other mount can `git pull` to sync.

```bash
cd src/backend/enterprise
git checkout main                # submodules default to detached HEAD
# … make changes (in backend/ or frontend/ subdir) …
git commit -m "feat(sso): ..."
git push origin main

# Sync the other mount
cd ../../../src/frontend/src/enterprise
git checkout main
git pull origin main
```

Then, back in the public repo root, **bump both submodule pointers**:

```bash
cd <trinity-root>
git add src/backend/enterprise src/frontend/src/enterprise
git commit -m "chore: bump enterprise submodule"
git push
```

## Forcing OSS-only mode (testing the deny path)

```bash
echo "TRINITY_OSS_ONLY=1" >> .env
docker compose up -d --force-recreate backend
# Now enterprise features are gated (403) even though the submodule
# is mounted. Useful for testing the OSS UX without unmounting.

# Revert
sed -i '/^TRINITY_OSS_ONLY=/d' .env
docker compose up -d --force-recreate backend
```

## Running Trinity OSS-only (no submodule access)

Cloning without enterprise access is the **default** OSS experience:

```bash
git clone https://github.com/abilityai/trinity.git
cd trinity
git submodule update --init .claude    # explicit — skip enterprise
./scripts/deploy/start.sh
```

The conditional `try: from enterprise import register_enterprise` in
`main.py` raises `ImportError`, the `except` branch logs an
informational message, and Trinity runs as OSS. Confirm:

```bash
docker logs trinity-backend 2>&1 | grep -i enterprise
# → INFO  Trinity Enterprise submodule not present — OSS-only build
```

## CI: testing without the submodule

`.github/workflows/build-without-submodule.yml` boots the backend with
the submodule absent and asserts the conditional import doesn't break
the boot. Run it manually with:

```bash
gh workflow run build-without-submodule.yml
```

The job exists specifically so a forgotten access revocation
(submodule URL no longer reachable) is caught in CI rather than at a
customer's first OSS-only clone.

## Troubleshooting

**Submodule clone fails with "Permission denied (publickey)"** — your
GitHub SSH key isn't loaded. Run `ssh-add ~/.ssh/id_ed25519` or
configure the URL rewrite shown in Step 1.

**`curl /api/enterprise/sso/providers` returns 404** — the submodule
didn't init. Run `git submodule status` — the line for
`src/backend/enterprise` should show a real SHA, not `-XXXX`. If it
shows `-XXXX`, run `git submodule update --init --recursive`.

**Enterprise endpoint returns 403 "not licensed"** — `TRINITY_OSS_ONLY=1`
is set in your `.env`. Either remove it or change to `0`, then
`docker compose up -d --force-recreate backend`.

## See also

- `docs/planning/ENTERPRISE_ARCHITECTURE.md` — decision record
- `docs/planning/OSS_ENTERPRISE_SPLIT_RESEARCH.md` — full research
- `Abilityai/trinity-enterprise/README.md` — private repo overview
- Issue #847 — the spike that produced this scaffolding
