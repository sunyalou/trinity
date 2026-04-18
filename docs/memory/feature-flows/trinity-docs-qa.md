# Trinity Docs Q&A

**ID**: DOCS-QA-001  
**Status**: Implemented  
**Added**: 2026-04-18

## Overview

Public conversational Q&A system for Trinity documentation, powered by Vertex AI Search with Gemini LLM. Users can ask questions about Trinity and receive grounded answers with citations from the onboarding documentation.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         Trinity Docs Q&A                                 │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  docs/onboarding/*.md                                                    │
│         │                                                                │
│         │ GitHub Action (on push)                                        │
│         ▼                                                                │
│  ┌─────────────────────┐    ┌─────────────────────┐                     │
│  │   GCS Bucket        │───▶│ Vertex AI Search    │                     │
│  │   (txt conversion)  │    │ Data Store          │                     │
│  └─────────────────────┘    └──────────┬──────────┘                     │
│                                        │                                 │
│                              ┌─────────┴──────────┐                     │
│                              │  Search Engine     │                     │
│                              │  (Gemini LLM)      │                     │
│                              └─────────┬──────────┘                     │
│                                        │                                 │
│                              ┌─────────┴──────────┐                     │
│                              │  Cloud Function    │                     │
│                              │  (public endpoint) │                     │
│                              └─────────┬──────────┘                     │
│                                        │                                 │
│                    ┌───────────────────┼───────────────────┐            │
│                    ▼                   ▼                   ▼            │
│              ask-trinity.sh       curl/REST           Future UI         │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

## Components

### GCP Resources

| Resource | ID | Description |
|----------|-----|-------------|
| Project | `mcp-server-project-455215` | GCP project |
| GCS Bucket | `trinity-docs-rag-mcp-server-project-455215` | Document storage |
| Data Store | `trinity-docs` | Vertex AI Search data store |
| Search Engine | `trinity-search` | Search engine with LLM add-on |
| Cloud Function | `ask-trinity` | Public HTTP endpoint |
| Workload Identity Pool | `github-actions` | GitHub Actions auth |
| Service Account | `trinity-docs-sync` | GCS + Discovery Engine access |

### Files

| File | Purpose |
|------|---------|
| `.github/workflows/sync-docs-to-vertex.yml` | Auto-sync docs to GCS on push |
| `scripts/ask-trinity.sh` | CLI tool for querying |
| `docs/onboarding/*.md` | Source documentation |

## Data Flow

### Document Sync (GitHub Action)

1. Push to `docs/onboarding/*.md` triggers workflow
2. Workflow authenticates via Workload Identity Federation
3. Markdown files converted to `.txt` (Vertex AI requirement)
4. Files synced to `gs://trinity-docs-rag-*/txt/`
5. Document re-import triggered via Discovery Engine API
6. Vertex AI indexes and chunks documents

### Query Flow

1. User sends question via `ask-trinity.sh` or direct curl
2. Cloud Function receives request (no auth required)
3. Function calls Vertex AI Search Answer API
4. Gemini 2.0 Flash generates answer from indexed docs
5. Response includes answer text and citation references

## API

### Public Endpoint

```
POST https://us-central1-mcp-server-project-455215.cloudfunctions.net/ask-trinity
Content-Type: application/json

{
  "question": "How do I create an agent?",
  "session_id": "optional-for-multi-turn"
}
```

**Response:**
```json
{
  "answer": "To create an agent in Trinity...",
  "state": "SUCCEEDED",
  "session_id": "7547107641198884380"
}
```

### Multi-Turn Chat

The endpoint supports conversational sessions with context memory:

```javascript
// First message - creates new session
const res1 = await fetch(ENDPOINT, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ question: "What are agents?" })
});
const { answer, session_id } = await res1.json();

// Follow-up - continues conversation
const res2 = await fetch(ENDPOINT, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ 
    question: "How do I create one?",  // "one" resolves to "agent"
    session_id: session_id
  })
});
```

Sessions persist ~30 minutes of inactivity. Context carries across turns.

### CLI Usage

```bash
./scripts/ask-trinity.sh "How do I add credentials to an agent?"
```

## Tone & Personality

The assistant has a baked-in personality via system prompt:

- **Markdown formatted** — headers, bullets, code blocks
- **Friendly & witty** — casual language, emojis, personality
- **Simple explanations** — plain language, no jargon overload
- **Concise** — get to the point without being robotic

## Configuration

### GitHub Secrets

| Secret | Value |
|--------|-------|
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/667627606781/locations/global/workloadIdentityPools/github-actions/providers/github` |
| `GCP_SERVICE_ACCOUNT` | `trinity-docs-sync@mcp-server-project-455215.iam.gserviceaccount.com` |

### Service Account Roles

- `roles/storage.objectAdmin` — GCS bucket access
- `roles/discoveryengine.editor` — Document import

## Indexed Documents

| Document | Content |
|----------|---------|
| `00-welcome.txt` | Introduction to Trinity |
| `01-getting-started.txt` | Installation and first agent |
| `02-use-case-scenarios.txt` | Real-world usage examples |
| `03-common-workflows.txt` | Day-to-day operations |
| `04-troubleshooting.txt` | Problem diagnosis and fixes |
| `README.txt` | Documentation index |

## Limitations

- **Markdown not supported**: Vertex AI Search requires `text/plain`, so `.md` files are converted to `.txt`
- **No real-time indexing**: Document changes require ~30s for re-indexing
- **Context window**: Large questions may be truncated
- **Session timeout**: Sessions expire after ~30 min of inactivity

## In-App Help Widget (#391)

A floating help chat widget provides instant access to Trinity documentation from within the UI.

### Components

| File | Purpose |
|------|---------|
| `src/frontend/src/components/HelpChatWidget.vue` | Floating button + expandable chat panel |
| `src/frontend/src/App.vue` | Mounts widget for authenticated users |

### Features

- Floating button in bottom-right corner (collapsible)
- Chat panel with message history
- Multi-turn conversations via session persistence (localStorage)
- Markdown rendering with DOMPurify sanitization
- Loading indicator while waiting for response
- Error handling with retry button
- Keyboard navigation and focus trap
- ARIA labels for accessibility
- "New conversation" button to reset session

### User Flow

```
User clicks help button → Panel opens → Types question → Enter to send
    → Loading indicator → Response renders with markdown
    → Continue conversation or start new one
```

### Session Persistence

- Session ID stored in `localStorage` key `trinity_help_session_id`
- Sessions persist ~30 min server-side (Vertex AI Search limit)
- Conversation history shown in-panel during session
- "New conversation" clears local messages and session ID

## Future Enhancements

- [ ] Add more docs (architecture, API reference)
- [x] ~~Integrate into Trinity UI as help widget~~ (done: #391)
- [x] ~~Add conversation memory for follow-up questions~~ (done: session support)
- [ ] Support for code snippets with syntax highlighting
- [ ] Usage analytics/telemetry

## Related

- [Vertex AI Search Console](https://console.cloud.google.com/gen-app-builder/engines?project=mcp-server-project-455215)
- [Cloud Function](https://console.cloud.google.com/functions/details/us-central1/ask-trinity?project=mcp-server-project-455215)
- [GCS Bucket](https://console.cloud.google.com/storage/browser/trinity-docs-rag-mcp-server-project-455215)
