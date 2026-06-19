# In-App Bug Reporting (#1116)

> Users report bugs from the floating **Help widget** without the deployed
> instance holding any GitHub credentials. The report is routed through a
> hosted, credential-holding intake service that files it as a GitHub issue in
> `abilityai/trinity`.

## Why a hosted intake

Trinity is open-source and self-hosted, so a deployed instance has no token to
file issues against `abilityai/trinity`. Reports POST to an **Ability.ai-operated
intake endpoint** that holds the GitHub token server-side — the same hosting
pattern as the `ask-trinity` Q&A function the widget already uses.

**Permanent, replaceable URL.** The widget URL is compiled into every frontend
bundle and can't be updated on already-deployed instances, so the intake is
fronted by a stable Cloudflare-managed vanity domain —
`https://intake.abilityai.dev/v1/report-bug`. The `/v1/` prefix versions the
contract; the backing service can be redeployed/replaced behind the domain
forever. (The intake service itself lives **outside this repo** in
`trinity-ops-agent/bug-intake/` — a Cloudflare Worker.)

## Flow

```
HelpChatWidget "Report a bug" tab
  → form (title + description, required, char-capped)
  → client builds diagnostics + CLIENT-SIDE SCRUB (utils/scrub.js)
  → REVIEW screen (see-before-send): scrubbed preview + public-repo warning
  → POST {title, description, install_id, diagnostics} → intake Worker
       Worker: guards → SERVER-SIDE re-scrub → soft per-install cap → dedupe
               → create GitHub issue (labels type-bug, source:in-app)
       ← { ok, issue_url, issue_number, deduped }
  → success state with issue link
```

## Frontend (in this repo)

- `src/components/HelpChatWidget.vue` — adds a **"Report a bug"** tab alongside
  the existing Q&A (no second floating button). Stages: `form → review →
  success`. The review stage is the explicit opt-in confirm and renders exactly
  what will be sent. Returned `issue_url` is link-rendered only if it matches
  `https://github.com/` (href-injection guard). Gated by `bugReportingEnabled`.
- `src/utils/consoleBuffer.js` — capped ring buffer (60 entries) of recent
  `console.error`/`warn` + `window.error`/`unhandledrejection`, installed in
  `src/main.js` **before app mount** so early errors are captured.
- `src/utils/scrub.js` — client mirror of the Worker's scrubber; scrubs title,
  description, route/url, and console lines so the review screen shows the
  already-scrubbed payload.
- **Config knobs** (build-time): `VITE_BUG_INTAKE_URL` (default the stable
  domain — operators can repoint) and `VITE_BUG_REPORTING_ENABLED=false` to hide
  the tab entirely.
- **CSP**: `https://intake.abilityai.dev` added to `connect-src` in **both**
  `vite.config.js` (dev) and `security-headers.conf` (prod nginx) — without this
  the browser blocks the POST (the April 2026 `ask-trinity` CSP bug class).
- Build info (`app_version`, `git_commit`) sourced via the existing
  `useBuildInfo()` composable (`GET /api/version`, Invariant #7).

## Diagnostics captured (scrubbed before the user reviews)

App version + git commit, current route/URL, user agent, viewport, OS, and the
last N console errors/warnings. Optional screenshot is **deferred** to a
follow-up (image hosting + visual-leak surface).

## Intake service (`trinity-ops-agent/bug-intake/`, out-of-repo)

Cloudflare Worker. `GITHUB_TOKEN` is a Worker secret (least-privilege
fine-grained PAT, `Issues: write` on `abilityai/trinity` only) — never client-exposed.

Defense-in-depth: Cloudflare edge DDoS (native) + a per-IP Rate Limiting Rule;
Worker method/`Content-Type`/256 KB body guards; KV soft per-`install_id` daily
cap; content-hash dedupe (a flapping client gets the existing issue URL back, no
duplicates); **second server-side scrub** (never trusts the client); optional
Turnstile. Scrubber has 14 unit tests; verified live end-to-end against
`abilityai/trinity` (filing, all secret classes scrubbed, dedupe, 400 guard).

## Security notes (public repo)

Reports land in a PUBLIC, indexed repo. Tokens (`Bearer`, `trinity_mcp_*`,
`sk-*`, `gh*_`, `xox*`, `AIza*`, `AKIA*`, JWTs), URL credentials/signed tokens,
`key=value` secrets, emails, and RFC-1918 IPs are scrubbed **on both the client
and the server**. The screenshot path is deferred precisely because an image
can leak secrets visually that regex scrubbing can't catch.
