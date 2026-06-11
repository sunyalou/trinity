# In-App Bug Report Intake Contract (#1116)

Trinity's Help widget (`HelpChatWidget.vue`) has a **Report a bug** tab. Because
Trinity is self-hosted and a deployed instance holds **no** GitHub credentials
of its own, reports are POSTed to a **hosted Ability.ai intake service** — the
same hosting pattern as the `ask-trinity` Q&A Cloud Function — which holds a
**server-side** GitHub token and files the issue into `abilityai/trinity`.

This document is the **client↔intake contract**. The intake service lives
outside this repo (an Ability.ai-operated Cloud Function, sibling to
`ask-trinity`); this file is the spec it must satisfy. The client half ships in
this repo.

## Client config

| Env var | Default | Effect |
|---------|---------|--------|
| `VITE_BUG_REPORT_ENDPOINT` | `https://us-central1-mcp-server-project-455215.cloudfunctions.net/report-bug` | Intake URL the widget POSTs to. |
| `VITE_BUG_REPORT_ENABLED` | `true` | `false` hides the "Report a bug" tab entirely (Q&A still works). |

## Request

`POST <VITE_BUG_REPORT_ENDPOINT>`  ·  `Content-Type: application/json`

```jsonc
{
  "title": "string (8–120 chars, required)",
  "description": "string (20–4000 chars, required)",
  "source": "in-app",
  "screenshot": "data:image/png;base64,... | null",   // optional, user-confirmed
  "diagnostics": {
    "app":      { "version", "git_commit", "git_branch", "build_date" },
    "location": { "route", "route_name", "href" },     // scrubbed client-side
    "browser":  { "user_agent", "language", "platform",
                  "viewport": { "width", "height", "dpr" } },
    "console_logs": [ { "level": "error|warn", "ts": "ISO", "text": "scrubbed" } ],
    "captured_at": "ISO"
  }
}
```

The client **scrubs** `console_logs` and `location` for tokens
(`Bearer`, `trinity_mcp_*`, `sk-`, `gh*_`, `github_pat_`, `xox*-`, `AIza`, JWTs),
emails, private IPs, and `key=value` secrets before sending
(`src/frontend/src/utils/scrub.js`). User-typed `title`/`description` are sent
verbatim (the user reviews them; the public-issue notice + explicit confirm are
the safeguard).

## Response

Success (`200`):

```jsonc
{ "issue_url": "https://github.com/abilityai/trinity/issues/NNNN" }
// client also accepts "html_url" or "url"; or "reference"/"id" as a fallback tracking ref
```

Error (`4xx`/`5xx`): optional `{ "error": "human-readable message" }`. The widget
shows a retryable error state.

## Intake service responsibilities (server-side, out of this repo)

These are **required** of the hosted service — the client cannot enforce them:

- **Server-side GitHub token** — never exposed to the client.
- **Secondary scrub** (defense-in-depth) of `title`, `description`,
  `console_logs`, and a best-effort scan of the screenshot's surrounding
  metadata. Do **not** trust the client scrub alone — issues are public + indexed.
- **Labels** on the filed issue: `type-bug` + a `source:in-app` marker.
- **Structured body**: render the diagnostics into a readable issue body;
  attach the screenshot (e.g. upload to an image host or a gist and embed).
- **Rate limiting + abuse/spam protection** on the public surface.
- **Dedupe/throttle** so a flapping client can't open dozens of identical
  issues (e.g. hash `title`+`route`+`git_commit` over a short window).

## Out of scope (follow-ups)

Authenticated user attribution, multi-file attachments, and per-operator
configurable destinations beyond the `VITE_*` knobs above.
