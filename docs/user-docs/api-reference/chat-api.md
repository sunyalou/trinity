# Chat API

API endpoints for agent chat, voice, streaming, and public chat access.

**Note:** All endpoints require JWT Bearer token unless noted. See [Authentication](authentication.md) for details. Full request/response schemas available at [Backend API Docs](http://localhost:8000/docs).

## Endpoints

### Authenticated Chat

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/chat` | POST | Send message (stream-json output) |
| `/api/agents/{name}/chat/sessions` | GET | List sessions |
| `/api/agents/{name}/chat/sessions/{id}` | GET | Session with messages |
| `/api/agents/{name}/chat/sessions/{id}/close` | POST | Close session |
| `/api/agents/{name}/chat/history/persistent` | GET | Persistent history |
| `/api/agents/{name}/chat/history` | DELETE | Reset session |
| `/api/agents/{name}/activity` | GET | Activity summary |

### Voice Chat

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/voice/start` | POST | Start voice session |
| `/api/agents/{name}/voice/stop` | POST | Stop session |
| `/api/agents/{name}/voice/status` | GET | Session status |
| `/ws/voice/{session_id}` | WS | Audio WebSocket bridge (URL returned by `voice/start`) |

### Public Chat (no auth)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/public/chat/{token}` | POST | Public chat |
| `/api/public/history/{token}` | GET | Public history |

### Paid Chat (x402)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/paid/{agent_name}/chat` | POST | Paid chat (402/200) |
| `/api/paid/{agent_name}/info` | GET | Payment requirements |

### Task Execution

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/task` | POST | Submit task |
| `/api/agents/{name}/executions` | GET | List executions |
| `/api/agents/{name}/executions/{id}` | GET | Execution details |

#### Deprecated: per-task `timeout_seconds`

The `timeout_seconds` field on the task request body is **deprecated** and will be removed in a future release. The agent's execution timeout (`GET/PUT /api/agents/{name}/timeout`) is authoritative.

Current behavior: the field is still honored, but values above the agent's timeout cap are clamped down to the cap (the server logs a deprecation warning). Omit the field — the task then uses the agent's configured timeout. To run longer tasks, raise the agent's timeout cap instead.

## Idempotency

Endpoints that trigger an execution accept an optional `Idempotency-Key` header so you can retry safely without creating duplicate executions. Pick any unique string per logical request (e.g., a UUID) and resend it on retry:

```bash
curl -X POST http://localhost:8000/api/agents/my-agent/task \
  -H "Authorization: Bearer <token>" \
  -H "Idempotency-Key: 7f3a2c1e-..." \
  -H "Content-Type: application/json" \
  -d '{"message": "Summarize the latest reports"}'
```

- The same key within 24 hours returns the original result with the header `X-Idempotent-Replay: true` — no second execution is created.
- A duplicate sent while the first request is still running returns **409** with the original `execution_id` to poll.
- If the first attempt was rejected before dispatch (e.g., at capacity), the key is released so the retry goes through.
- The header is optional and fail-open: omitting it preserves normal behavior, and a dedup-layer error never blocks a real request.

Wired boundaries: `/api/agents/{name}/chat`, `/api/agents/{name}/task`, `/api/agents/{name}/fan-out`, `/api/agents/{name}/voip/call`, [webhook triggers](webhook-triggers.md) (key auto-derived from token + body when the header is absent), and the MCP `chat_with_agent` / `fan_out` tools (deterministic key derived from the call arguments).

## See Also

- [Authentication](authentication.md) -- JWT token usage and login flow
- [Agent API](agent-api.md) -- Agent lifecycle and configuration endpoints
- [Backend API Docs](http://localhost:8000/docs) -- Interactive Swagger documentation
