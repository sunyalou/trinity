# Enterprise Architecture Decision

> **Status**: Decided 2026-05-21. PoC implemented (issue #847).
> Decision record condensed from
> `docs/planning/OSS_ENTERPRISE_SPLIT_RESEARCH.md` (521 lines, the
> full investigation and tradeoffs).

## Decision

Trinity adopts an **open-core** model: one public codebase
(`abilityai/trinity`), with closed-source compliance modules in a
**private git submodule** at `src/backend/enterprise/` pointing to
`Abilityai/trinity-enterprise`. The public backend loads the submodule
via a conditional import; clones without enterprise access boot as
OSS-only with no code changes.

### Six load-bearing decisions

| # | Decision | One-line why |
|---|---|---|
| 1 | **Open-core via private submodule** at `src/backend/enterprise/` | Reuses the trusted `.claude` submodule pattern. One repo per edition diverges; one repo + submodule converges. |
| 2 | **One `EntitlementService` singleton** in `services/entitlement_service.py` | Centralises feature-gate logic. Routers call `requires_entitlement(feature_id)` instead of scattered `if`. |
| 3 | **Offline Ed25519-signed license** (Phase 1, not in this PR) | "Sovereign infra on your own hardware" pitch breaks if Trinity has to phone home; air-gapped buyers refuse phone-home. |
| 4 | **Tier as Community / Team / Enterprise** | Charge for governance / scale / compliance — what an *org* pays for. Don't paywall the adoption funnel. |
| 5 | **Accept soft enforcement** | The moat is signed updates + support + the legal license, not obfuscation of code. |
| 6 | **Fix the repo's license-of-record** before Phase 1 | Repo currently shows `license: NOASSERTION`. Open-core is a *legal* structure before a code one. |

### What lives where

```
abilityai/trinity                   (public, MIT/Apache after decision #6)
├── src/backend/
│   ├── main.py                     conditional import + register_enterprise(app)
│   ├── dependencies.py             requires_entitlement(feature_id) — Phase 0 seam
│   ├── services/
│   │   └── entitlement_service.py  EntitlementService (Phase 0 stub; Phase 1 license)
│   └── enterprise/                 ← submodule mount point
│       └── (populated by submodule init — see below)
├── docs/planning/
│   ├── ENTERPRISE_ARCHITECTURE.md  this file
│   └── OSS_ENTERPRISE_SPLIT_RESEARCH.md  long-form research
└── docs/dev/
    └── ENTERPRISE_LOCAL_DEV.md     15-min onboarding guide

Abilityai/trinity-enterprise        (private, proprietary)
├── __init__.py                     register_enterprise(app) entry point
├── sso/                            #847 PoC (router + provider ABC + stubs)
├── scim/                           planned (#???)
├── siem/                           planned (#???)
└── LICENSE                         commercial / proprietary
```

## Why a submodule, not a Python package

Three options compared in the research:

| Option | Local DX | CI complexity | Distribution | Verdict |
|---|---|---|---|---|
| **Git submodule** | clone-with-submodule, no extra step | OSS CI works with or without submodule | URL access controls who gets it | ✅ chosen — same pattern as `.claude` |
| Private PyPI / GitHub Packages | `pip install trinity-enterprise` extra | needs Packages auth on every CI run | requires customer to know about pkg index | ✗ added DX friction for the dev-loop |
| Plugin hook (runtime download) | needs a registration script | brittle in air-gapped deployments | hardest to gate | ✗ doesn't match "sovereign infra" pitch |

The submodule approach lets a developer with org access work on the
enterprise code in the same checkout as Trinity — no `pip install -e`
dance, no symlink hacks. Compare CI cost: the `build-without-submodule`
job below proves the conditional import works.

## The seam (what landed in this PR — #847 Phase 0)

**`src/backend/services/entitlement_service.py`**
- `EntitlementService.is_entitled(feature_id) -> bool` — returns True
  by default (Phase 0 stub). `TRINITY_OSS_ONLY=1` env flips every check
  to False for compliance lockdown or testing the deny path.
- `EntitlementService.list_entitled_features() -> list[str]` — drives
  UI tab visibility via `/api/settings/feature-flags`.
- Module-level singleton `entitlement_service`; `_set_for_testing` test seam.

**`src/backend/dependencies.py:requires_entitlement(feature_id)`**
- Dependency factory mirroring `require_role`. Raises HTTP 403 with a
  message naming the missing feature, so the UI can surface a "license
  required" toast and the operator can correlate with `system_settings`.

**`src/backend/main.py`**
- Conditional `try: from enterprise import register_enterprise; register_enterprise(app) except ImportError`.
- Logs which mode it's in (registered / OSS-only) for ops visibility.

**`src/backend/routers/settings.py:get_public_feature_flags`**
- Adds `enterprise_features: list[str]` to the response. Empty list ==
  OSS-only (UI hides enterprise tabs).

**`.gitmodules`**
- New `src/backend/enterprise` submodule entry pointing at the private repo via SSH.

**`docker-compose.yml`**
- Pass-through for `TRINITY_OSS_ONLY` env var.

## What the private repo holds (`Abilityai/trinity-enterprise`)

PoC scope (this PR):

| File | Purpose |
|---|---|
| `__init__.py` | `register_enterprise(app)` — single integration entry |
| `sso/router.py` | `/api/enterprise/sso/{providers,login/{id}}` — stubs, gated by `requires_entitlement("sso")` |
| `sso/providers.py` | `SSOProvider` ABC + `StubProvider` for the PoC registry |
| `pyproject.toml` | Metadata (no pip-install mode yet — submodule mount only) |
| `LICENSE` | Proprietary |

## How CI handles "build without submodule"

Workflow `.github/workflows/build-without-submodule.yml` boots the
backend image with the submodule **absent** and asserts:

1. Container starts cleanly
2. `GET /api/settings/feature-flags` returns `enterprise_features: []`
3. `GET /api/enterprise/sso/providers` returns 404 (router not mounted)

This proves the conditional import doesn't break OSS-only deployments
when the submodule URL access is revoked or the submodule is unchecked.

## What's NOT in this PR (open follow-ups)

1. **Phase 1 — License mechanism**: Ed25519 signing CLI, license verify
   path, admin License UI, grace + clock-tamper handling. The
   `EntitlementService` stub is the seam for this.
2. **Phase 2 — Extract a clean leaf**: move audit log into the
   enterprise submodule (highest enterprise value, cleanest boundary).
3. **Phase 3 — Prove the "core-primitive + enterprise-knob" pattern**
   via #834 (soft-delete OSS core + license-capped retention as
   enterprise knob).
4. **Phase 4 — Build SSO/SAML** for real (replaces the PoC stubs).
5. **MCP entitlement edge**: `GET /api/internal/entitlements` polled by
   the TypeScript MCP server so MCP-tool-layer gates also see the
   license state.
6. **License-of-record fix**: `LICENSE` file at repo root replacing
   `NOASSERTION`. Owner decision, not engineering.

## Risks accepted

- **Forks of the OSS repo can replace `EntitlementService` with an
  always-True implementation.** The defence is licensing law (signed
  contract + commercial license terms), signed releases, and access to
  support — not obfuscation. Documented in research §7.
- **Per-process semaphore-style state lives in the singleton** —
  acceptable for a single-host backend; horizontal scaling needs a
  rethink (none currently planned).
- **Drift risk between `AUTH_INDICATORS`-style duplicated lists in the
  scheduler container** — already exists for `is_auth_failure` (#904);
  the entitlement service is read once at boot so it doesn't add new
  drift surface.

---

For the full investigation, alternatives considered, gating-mechanism
options, stress test against 8 other open issues, and the
four-patterns refinement (clean leaf / core-primitive + knob / data
capture / cross-cutting integration), see
`docs/planning/OSS_ENTERPRISE_SPLIT_RESEARCH.md`.
