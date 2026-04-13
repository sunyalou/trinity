# Feature: Telegram Bot Integration (TELEGRAM-001, TGRAM-GROUP)

## Overview
Per-agent Telegram bots with 1:1 agent-to-bot mapping, enabling users to chat with Trinity agents via Telegram DMs and group chats. Agents are shareable via `t.me/BotUsername` links. In groups, agents respond to @mentions and direct replies.

## User Story
As an agent owner, I want to connect a Telegram bot to my agent so that external users can interact with it via Telegram without needing a Trinity account.

## Entry Points
- **API (configure)**: `PUT /api/agents/{name}/telegram` — bind a bot token to an agent
- **API (webhook)**: `POST /api/telegram/webhook/{webhook_secret}` — receive Telegram updates (public, no JWT)
- **API (status)**: `GET /api/agents/{name}/telegram` — check binding status (includes group_count)
- **API (delete)**: `DELETE /api/agents/{name}/telegram` — remove bot binding
- **API (test)**: `POST /api/agents/{name}/telegram/test` — send test message or verify bot
- **API (groups)**: `GET /api/agents/{name}/telegram/groups` — list group configs (TGRAM-GROUP)
- **API (group update)**: `PUT /api/agents/{name}/telegram/groups/{id}` — update trigger mode / welcome settings
- **API (group delete)**: `DELETE /api/agents/{name}/telegram/groups/{id}` — deactivate group config

## Architecture

### Phase 0: Adapter Generalization

Before Telegram was added, the message router had 6 Slack-specific hardcodings. These were replaced with abstract adapter methods on the `ChannelAdapter` base class:

- `src/backend/adapters/base.py` — Added abstract methods:
  - `channel_type` (property) — `"slack"` or `"telegram"`
  - `get_rate_key(message)` — rate-limit key per sender
  - `get_session_identifier(message)` — session key for conversation persistence
  - `get_source_identifier(message)` — audit trail identifier
  - `get_bot_token(message)` — resolve the bot/app token for responses
- `src/backend/adapters/message_router.py` — All 6 hardcodings replaced with `adapter.channel_type`, `adapter.get_rate_key()`, etc.
- `src/backend/adapters/slack_adapter.py` — Added backward-compatible overrides of the new methods

### Layer Diagram

```
Telegram Bot API
    |
    v (HTTP POST)
FastAPI: POST /api/telegram/webhook/{webhook_secret}
    |
    v
routers/telegram.py:55 → handle_telegram_webhook()
    |
    v
TelegramWebhookTransport.handle_webhook()
    |  - Resolve binding by webhook_secret
    |  - Validate X-Telegram-Bot-Api-Secret-Token header
    |  - Dedup by update_id
    |  - Inject _bot_id + _agent_name into update dict
    v (asyncio.create_task — returns 200 immediately)
TelegramWebhookTransport._process_update()
    |  - Commands (/start, /help, /reset) → direct response
    |  - Regular messages → on_event(update)
    v
ChannelTransport.on_event() (base.py:52)
    |
    v
TelegramAdapter.parse_message(update)
    |  - Extract text, media context, user/chat IDs
    |  - Build NormalizedMessage with metadata
    v
ChannelMessageRouter.handle_message(adapter, message)
    |  - Resolve agent, bot token, rate limit
    |  - Check container status
    |  - Get/create session
    |  - Build context prompt
    |  - Send typing indicator
    |  - Execute via TaskExecutionService
    |  - Persist messages
    v
TelegramAdapter.send_response()
    |  - Convert markdown → Telegram HTML
    |  - Split at 4096 char limit
    |  - POST sendMessage to Telegram Bot API
    v
User receives response in Telegram
```

## Backend Layer

### Router: `src/backend/routers/telegram.py`

Two routers are registered in `main.py:523-524`:

**Public router** (`/api/telegram`):
- `POST /webhook/{webhook_secret}` (line 54) — Receives Telegram updates. No JWT auth; validated by webhook secret in URL + `X-Telegram-Bot-Api-Secret-Token` header. Always returns 200 to prevent Telegram retries.

**Authenticated router** (`/api/agents`):
- `GET /{agent_name}/telegram` (line 95) — Returns binding status (bot_username, bot_id, webhook_url, bot_link, configured flag). No token decryption.
- `PUT /{agent_name}/telegram` (line 116) — Configure bot. Validates token format (`id:secret`), calls `getMe` API, checks bot_id uniqueness across agents, creates encrypted binding, registers webhook if `public_chat_url` is set. If `public_chat_url` is unset, the binding is still created (the UI "Connected (no webhook)" state handles this), and the response includes a `warning` field explaining that delivery will start automatically once the URL is saved. Back-fill is triggered by saving the setting — see "Back-fill on setting save" below.
- `DELETE /{agent_name}/telegram` (line 190) — Calls Telegram `deleteWebhook`, then deletes binding + chat links from DB.
- `POST /{agent_name}/telegram/test` (line 211) — Without `chat_id`: verifies bot via `getMe`. With `chat_id`: sends test message.

### Transport: `src/backend/adapters/transports/telegram_webhook.py`

`TelegramWebhookTransport` extends `ChannelTransport`:
- `start()` / `stop()` (lines 25-32) — No-ops; transport is passive (FastAPI endpoint handles requests).
- `handle_webhook(request, webhook_secret)` (line 34) — Core inbound handler:
  1. Resolve binding by `webhook_secret` via DB
  2. Validate `X-Telegram-Bot-Api-Secret-Token` header
  3. Parse JSON body
  4. Dedup by `update_id` (skip if <= `last_update_id`)
  5. Update `last_update_id` in DB
  6. Inject `_bot_id` and `_agent_name` into update dict
  7. `asyncio.create_task(_process_update(...))` — returns 200 immediately
- `_process_update(update, binding)` (line 79) — Commands are handled directly; regular messages go through `on_event()` → adapter → router pipeline.

**Webhook lifecycle functions** (module-level):
- `register_webhook(agent_name, public_url)` — Calls Telegram `setWebhook` with URL + secret_token + `allowed_updates: ["message", "my_chat_member", "chat_member"]`. Updates `webhook_url` in DB on success.
- `delete_webhook(agent_name)` — Calls Telegram `deleteWebhook`.

### Adapter: `src/backend/adapters/telegram_adapter.py`

`TelegramAdapter` implements `ChannelAdapter`:

**Identity and routing** (used by message_router):
- `channel_type` → `"telegram"`
- `get_rate_key()` → `telegram:{bot_id}:{sender_id}`
- `get_session_identifier()` → `{bot_id}:{sender_id}:{chat_id}`
- `get_source_identifier()` → `telegram:{bot_id}:{sender_id}`
- `get_bot_token()` → decrypts from DB via `db.get_telegram_bot_token(agent_name)`

**Message processing**:
- `parse_message(raw_event)` (line 66) — Extracts text + media context from Telegram Update. Skips bot messages. Returns `NormalizedMessage` with metadata including `bot_id`, `agent_name`, `username`, media flags.
- `send_response(channel_id, response, thread_id)` (line 123) — Converts markdown to HTML, splits at 4096 chars, sends via `sendMessage`. Falls back to plain text if HTML parsing fails (line 247).
- `indicate_processing(message)` (line 158) — Sends `typing` chat action.

**Bot commands** (line 168):
- `/start` — Welcome message with agent name
- `/help` — Capabilities list (text, photos, documents)
- `/reset` — Clears conversation session via `db.clear_public_chat_session()`

**Message formatting**:
- `_markdown_to_html()` (line 274) — Converts `**bold**`, `*italic*`, `` `code` ``, `~~strike~~` to Telegram HTML tags
- `_split_message()` (line 298) — Splits at paragraph/sentence/word boundaries within 4096 char limit
- `_extract_media_context()` (line 327) — Generates descriptive context for photos, documents, stickers, locations, voice, video

### Media Service: `src/backend/services/telegram_media.py`

- `download_telegram_file(bot_token, file_id)` (line 29) — Two-step download: `getFile` → download from file path. **SSRF prevention**: hostname must be `api.telegram.org` (line 71). **Size limit**: 20MB (line 58).
- `process_photo(bot_token, photo_sizes)` (line 93) — Downloads largest photo, saves to temp file, returns size description. Temp file always cleaned up.
- `process_document(bot_token, document)` (line 127) — Downloads and extracts text from plain text files (.txt, .md, .csv, .json, .py, etc.). Truncates at 10,000 chars. Non-text files get metadata-only description.

### Message Router: `src/backend/adapters/message_router.py`

The `ChannelMessageRouter` is channel-agnostic. For Telegram messages it follows the same 13-step pipeline as Slack:

1. Resolve agent via `adapter.get_agent_name()`
2. Resolve bot token via `adapter.get_bot_token()`
3. Rate limit via `adapter.get_rate_key()` (30 msgs / 60s default)
4. Check agent container status
5. Handle verification (default: always verified for Telegram)
6. Get/create session via `adapter.get_session_identifier()`
7. Build context prompt from session history
8. Show typing indicator via `adapter.indicate_processing()`
9. Execute via `TaskExecutionService` with restricted tools (default: `WebSearch,WebFetch` only)
10. Show completion indicator
11. Persist user + assistant messages in session
12. Send response via `adapter.send_response()`
13. Post-response hook via `adapter.on_response_sent()`

### Startup: `src/backend/main.py:355-385`

On backend startup:
1. Create `TelegramAdapter` and `TelegramWebhookTransport` instances
2. Call `transport.start()` (no-op for webhook transport)
3. Set transport reference on router module via `set_webhook_transport()`
4. Store transport on `app.state.telegram_transport`
5. **Webhook reconciliation**: If `public_chat_url` is set, iterate all bindings and call `register_webhook()` for each. This ensures webhooks are re-registered after backend restarts or URL changes.

On shutdown (`main.py:432-437`): calls `transport.stop()`.

### Back-fill on `public_chat_url` save (`routers/settings.py`)

When an admin saves `public_chat_url` via `PUT /api/settings/{key}`, the handler calls `_backfill_telegram_webhooks(new_url)` after the setting write succeeds. The helper iterates `db.get_all_telegram_bindings()` and invokes `register_webhook(agent_name, new_url)` for each. This is idempotent (Telegram's `setWebhook` replaces any existing registration) and self-healing — bindings created before a public URL was configured automatically start receiving messages as soon as the URL is saved, without requiring the admin to remove and re-add the bot.

Failures during back-fill are logged (`logger.warning`) but not raised: the setting write has already succeeded, and a single bad binding (network blip, expired token) must not block others or the API response. The reconciliation loop in `main.py` startup re-runs on the next backend restart and catches any stragglers.

## Data Layer

### Database Tables

Created by migration `telegram_bindings` in `src/backend/db/migrations.py:913-958`:

**`telegram_bindings`** — one bot per agent (1:1 mapping):
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| agent_name | TEXT UNIQUE | Links to agent |
| bot_token_encrypted | TEXT | AES-256-GCM via `CredentialEncryptionService` |
| bot_username | TEXT | e.g. `MyAgentBot` |
| bot_id | TEXT UNIQUE | Telegram bot user ID |
| webhook_secret | TEXT | `secrets.token_urlsafe(32)` — URL path component |
| webhook_url | TEXT | Full registered webhook URL |
| telegram_secret_token | TEXT | `secrets.token_urlsafe(32)` — header validation |
| last_update_id | INTEGER | For dedup (skip updates <= this) |
| created_at | TEXT | ISO timestamp |
| updated_at | TEXT | ISO timestamp |

Indexes: `agent_name`, `bot_id`, `webhook_secret`

**`telegram_chat_links`** — tracks Telegram users per bot:
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| binding_id | INTEGER FK | References `telegram_bindings(id)` |
| telegram_user_id | TEXT | Telegram user ID |
| telegram_username | TEXT | Optional @username |
| session_id | TEXT | Public chat session reference |
| message_count | INTEGER | Incremented per message |
| created_at | TEXT | ISO timestamp |
| last_active | TEXT | ISO timestamp |

Unique constraint: `(binding_id, telegram_user_id)`

### Database Operations: `src/backend/db/telegram_channels.py`

`TelegramChannelOperations` class with methods:
- `create_binding()` — UPSERT with encrypted token, generates webhook_secret + telegram_secret_token
- `get_binding_by_agent()` / `get_binding_by_bot_id()` / `get_binding_by_webhook_secret()` — Lookups (token NOT decrypted)
- `get_decrypted_bot_token()` — Decrypts via `CredentialEncryptionService`
- `get_all_bindings()` — For webhook reconciliation on startup
- `update_webhook_url()` / `update_last_update_id()` — Field updates
- `delete_binding()` — Cascading delete of chat links + binding
- `get_or_create_chat_link()` — Upsert for Telegram user tracking
- `increment_message_count()` — Stats tracking

### Delegation: `src/backend/database.py:1296-1330`

The `Database` singleton delegates all Telegram operations to `TelegramChannelOperations`:
- `db.create_telegram_binding()` → `_telegram_channel_ops.create_binding()`
- `db.get_telegram_binding()` → `_telegram_channel_ops.get_binding_by_agent()`
- `db.get_telegram_binding_by_bot_id()` → `_telegram_channel_ops.get_binding_by_bot_id()`
- `db.get_telegram_binding_by_webhook_secret()` → `_telegram_channel_ops.get_binding_by_webhook_secret()`
- `db.get_telegram_bot_token()` → `_telegram_channel_ops.get_decrypted_bot_token()`
- `db.get_all_telegram_bindings()` → `_telegram_channel_ops.get_all_bindings()`
- `db.update_telegram_webhook_url()` → `_telegram_channel_ops.update_webhook_url()`
- `db.update_telegram_last_update_id()` → `_telegram_channel_ops.update_last_update_id()`
- `db.delete_telegram_binding()` → `_telegram_channel_ops.delete_binding()`
- `db.get_or_create_telegram_chat_link()` → `_telegram_channel_ops.get_or_create_chat_link()`
- `db.increment_telegram_message_count()` → `_telegram_channel_ops.increment_message_count()`

## Security

### Token Encryption
Bot tokens are encrypted at rest using AES-256-GCM via `CredentialEncryptionService` (same pattern as Slack tokens). The `_encrypt_token()` / `_decrypt_token()` methods in `TelegramChannelOperations` wrap the token in `{"bot_token": token}` before encryption.

### Webhook Authentication (dual layer)
1. **URL secret**: `webhook_secret` (32-byte URL-safe token) in the webhook URL path prevents enumeration
2. **Header secret**: `X-Telegram-Bot-Api-Secret-Token` header (separate 32-byte token) — Telegram sends this header with every update; transport validates it matches the stored `telegram_secret_token`

### SSRF Prevention
`telegram_media.py:71` — File download URLs are validated: `parsed.hostname` must equal `api.telegram.org`. This prevents a malicious file_path from redirecting downloads to internal services.

### Tool Restrictions
The message router restricts public channel users to `WebSearch,WebFetch` by default (configurable via `channel_allowed_tools` setting). No `Read`, `Bash`, `Write`, or `Edit` tools — prevents credential exfiltration from agent containers.

### Update Deduplication
`last_update_id` column prevents replay attacks. Updates with `update_id <= last_update_id` are silently dropped (`telegram_webhook.py:62-65`).

### Bot-Agent Uniqueness
- `agent_name` is UNIQUE in `telegram_bindings` — one bot per agent
- `bot_id` is UNIQUE — one agent per bot (checked at `routers/telegram.py:154`)

## Error Handling

| Error Case | HTTP Status | Message | Location |
|------------|-------------|---------|----------|
| Invalid token format | 400 | Invalid bot token format | `telegram.py:132` |
| Token fails getMe | 400 | Invalid bot token: {description} | `telegram.py:142` |
| Telegram API unreachable | 502 | Could not reach Telegram API | `telegram.py:151` |
| Bot bound to other agent | 409 | This bot is already bound to agent '{name}' | `telegram.py:157` |
| No binding found (DELETE) | 404 | No Telegram binding found | `telegram.py:198` |
| No binding found (test) | 404 | No Telegram binding found or token decryption failed | `telegram.py:220` |
| Unknown webhook_secret | 200* | `{"ok": false}` | `telegram_webhook.py:43` |
| Invalid header token | 200* | `{"ok": false}` | `telegram_webhook.py:49` |
| Rate limited by Telegram | Retry | Waits `retry_after` seconds (max 30s) | `telegram_adapter.py:236` |
| HTML parse failure | Retry | Falls back to plain text | `telegram_adapter.py:247` |
| Agent container not running | 200 | "I'm not available right now" | `message_router.py:143` |
| Task execution timeout | 200 | "That took too long" | `message_router.py:201` |

*Webhook always returns 200 to Telegram to prevent automatic retries.

## Testing

### Prerequisites
- Backend running (`./scripts/deploy/start.sh`)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- `public_chat_url` setting configured (Settings page → Platform section, or `PUT /api/settings/public_chat_url`)
- At least one agent running

### Test Steps

1. **Configure bot**
   **Action**: `PUT /api/agents/{agent}/telegram` with `{"bot_token": "123:ABC"}`
   **Expected**: 200 with `configured: true`, `bot_link: "https://t.me/..."`, webhook registered
   **Verify**: `GET /api/agents/{agent}/telegram` returns binding info

2. **Test bot connectivity**
   **Action**: `POST /api/agents/{agent}/telegram/test` with `{}`
   **Expected**: `{"ok": true, "bot_info": {...}}`

3. **Send message via Telegram**
   **Action**: Open `t.me/BotUsername`, send a text message
   **Expected**: Bot shows typing indicator, then responds with agent output
   **Verify**: Check backend logs for `[ROUTER:telegram]` entries

4. **Bot commands**
   **Action**: Send `/start`, `/help`, `/reset` in Telegram
   **Expected**: Each returns its formatted HTML response

5. **Duplicate bot prevention**
   **Action**: Try to bind the same bot to a different agent
   **Expected**: 409 Conflict

6. **Remove bot**
   **Action**: `DELETE /api/agents/{agent}/telegram`
   **Expected**: Webhook deleted from Telegram, binding removed from DB
   **Verify**: `GET /api/agents/{agent}/telegram` returns `configured: false`

## Frontend Layer

### Component: `src/frontend/src/components/TelegramChannelPanel.vue`

Self-contained panel rendered in the Agent Detail → Sharing tab via `SharingPanel.vue`. Mirrors the `SlackChannelPanel.vue` pattern.

**Props**: `agentName` (String, required)

**States**: `loading`, `connecting`, `disconnecting`, `verifying`, `accessDenied`

**Data model**:
```javascript
binding = {
  configured: boolean,
  bot_username: string,
  bot_id: string,
  webhook_url: string | null,
  bot_link: string | null
}
```

**UI States**:

| State | Display |
|-------|---------|
| Loading | Spinner |
| Access denied (403) | "Only the agent owner can manage..." |
| Disconnected | Token input (`type="password"`) + "Connect Bot" button + BotFather link |
| Connected | Green dot, `@bot_username`, t.me link, Verify + Disconnect buttons |
| Connected (no webhook) | Yellow warning: "Bot connected but webhook not registered..." with `router-link` to `/settings` |

**API calls** (via `api.js` — Invariant #7):
- `GET /api/agents/{name}/telegram` → load binding status
- `PUT /api/agents/{name}/telegram` → connect bot (sends `{ bot_token }`)
- `DELETE /api/agents/{name}/telegram` → disconnect bot
- `POST /api/agents/{name}/telegram/test` → verify bot (calls getMe)

**Security**:
- Token input is `type="password"` (masked in DOM)
- `botToken` ref cleared to `''` immediately on successful connect
- No `console.error(e)` that could leak token via axios error object
- Backend never returns token in GET response

**Error handling**:
- 409 → "This bot is already bound to agent '{name}'" (surfaces agent name from backend)
- 400 → Invalid token format or failed getMe validation
- 502 → Telegram API unreachable
- Generic fallback for unexpected errors

### Integration: `src/frontend/src/components/SharingPanel.vue`

TelegramChannelPanel is imported and rendered between the Slack and Public Links sections:

```vue
<SlackChannelPanel :agent-name="agentName" />
<div class="border-t ..."></div>
<TelegramChannelPanel :agent-name="agentName" />
<div class="border-t ..."></div>
<PublicLinksPanel :agent-name="agentName" />
```

## Group Chat Support (TGRAM-GROUP)

### Overview

Agents can participate in Telegram group chats. Bots respond to @mentions and direct replies in groups. Per-group configuration controls trigger mode and welcome messages. Added 2026-04-11.

### Group Message Flow

```
Telegram Group Chat
    |
    v (HTTP POST — message update)
TelegramWebhookTransport.handle_webhook()
    |
    v (inject _bot_id, _bot_username, _agent_name)
TelegramWebhookTransport._process_update()
    |  - Member events (my_chat_member, chat_member) → adapter.handle_member_event()
    |  - Commands with @botname suffix → adapter.handle_command()
    |  - Regular messages → on_event(update)
    v
TelegramAdapter.parse_message(update)
    |  - Detect group vs private (chat.type in {"group", "supergroup"})
    |  - Check @mention in entities → _is_bot_mentioned()
    |  - Check reply_to_message → _is_reply_to_bot()
    |  - If neither and trigger_mode != "all" → return None (skip)
    |  - Strip @mention from text for cleaner agent input
    v
ChannelMessageRouter.handle_message()
    |  - Rate limit with per-group key (telegram:{bot_id}:group:{chat_id})
    |  - Silent drop on rate limit (no public error message in group)
    |  - Fresh context per message (no prior session history — prevents context bleed)
    |  - Execute via TaskExecutionService
    v
TelegramAdapter.send_response()
    |  - Reply to triggering message via reply_parameters (threaded in group)
    v
User sees response as reply in group
```

### Trigger Rules

| Condition | trigger_mode=mention | trigger_mode=all |
|-----------|---------------------|-----------------|
| @mention in entities | ✅ Process | ✅ Process |
| Reply to bot's message | ✅ Process | ✅ Process |
| Regular message (no mention) | ❌ Skip | ✅ Process |

### Member Events

**`my_chat_member`** — Bot's own status changes (no admin required):
- Bot added to group (`left/kicked → member/administrator`) → `get_or_create_group_config()` with auto-activate
- Bot removed from group (`member/administrator → left/kicked`) → `deactivate_group_config()`

**`chat_member`** — Other users' status changes (requires bot admin):
- User joins group → send welcome message if `welcome_enabled` and `welcome_text` set
- Welcome text supports `{name}` placeholder for user's first name

### Database: `telegram_group_configs`

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| binding_id | INTEGER FK | References `telegram_bindings(id)` |
| chat_id | TEXT | Telegram chat ID (negative for groups) |
| chat_title | TEXT | Group name (updated on each interaction) |
| chat_type | TEXT | `"group"` or `"supergroup"` |
| trigger_mode | TEXT | `"mention"` (default) or `"all"` |
| welcome_enabled | INTEGER | 0 or 1 |
| welcome_text | TEXT | Welcome message template |
| is_active | INTEGER | 1=active, 0=deactivated (bot removed) |
| created_at | TEXT | ISO timestamp |
| updated_at | TEXT | ISO timestamp |

Unique constraint: `(binding_id, chat_id)`

### Group Config API

All endpoints require JWT + `OwnedAgentByName` (agent owner only):

- `GET /api/agents/{name}/telegram/groups` — List active group configs
- `PUT /api/agents/{name}/telegram/groups/{id}` — Update trigger_mode, welcome_enabled, welcome_text (ownership verified)
- `DELETE /api/agents/{name}/telegram/groups/{id}` — Deactivate group config

### Frontend: Group Config UI

The `TelegramChannelPanel.vue` component shows group configurations when the bot is connected:

- Group list with chat title and type badge
- Trigger mode radio buttons (mention-only / all messages)
- Welcome message toggle with text input (`{name}` placeholder)
- Remove button per group (deactivates, doesn't delete)

### Security Notes

- **No new attack surface**: Group messages use the same webhook endpoint with the same dual-layer auth
- **IDOR prevention**: Group config update verifies the config ID belongs to the requesting agent's binding
- **Context bleed prevention**: Group messages get fresh context (no prior session history) — agent replies in a public group cannot leak prior DM conversation context
- **Bot loop prevention**: Inherited from DM support — `parse_message()` skips messages from bots (`is_bot` check)
- **Silent rate limiting**: Rate limit errors are not sent to groups (would be visible to all members)
- **Membership not verified per message**: Telegram doesn't provide a cheap per-message membership check; this is standard bot behavior and is documented

### Known Limitations

- **"All messages" trigger mode requires BotFather privacy mode change**: By default, Telegram privacy mode is ON and bots only receive @mentions/replies. The "all messages" mode works if the bot admin disables privacy mode via BotFather (`/setprivacy` → Disable). This is a manual step not automatable via the Bot API.
- **`chat_member` events require bot admin**: Welcome messages for user joins only work if the bot has admin rights in the group AND `chat_member` is in `allowed_updates`. If the bot isn't admin, events simply don't arrive — graceful degradation.
- **No `edited_message` handling**: Edited messages in groups are not processed. In mention-only mode this is rare.

## Access Control & `/login` Email Verification (#311)

Part of the unified cross-channel access control primitive. Full design (policy columns on `agent_ownership`, router gate logic, access-request inbox, Slack/public parity) lives in [unified-channel-access-control.md](unified-channel-access-control.md) — this section documents the Telegram-specific pieces only.

### Schema additions

Migration `access_control` adds two columns to `telegram_chat_links`:

| Column | Type | Notes |
|--------|------|-------|
| verified_email | TEXT | The email bound to this Telegram user within this bot, or NULL if unverified |
| verified_at | TEXT | ISO timestamp set when `set_verified_email` runs |

The unique index remains `UNIQUE(binding_id, telegram_user_id)`.

### New DB methods (`src/backend/db/telegram_channels.py`)

Added to `TelegramChannelOperations`:

- `get_chat_link(binding_id, telegram_user_id)` — lookup without auto-create (the existing `get_or_create_chat_link` upserts, which is wrong for a pure "is this user verified?" read)
- `get_verified_email(binding_id, telegram_user_id) -> str | None`
- `set_verified_email(binding_id, telegram_user_id, email)` — `INSERT OR IGNORE` to ensure the row exists, then `UPDATE` to set `verified_email` + `verified_at`
- `clear_verified_email(binding_id, telegram_user_id)` — nulls both columns

Exposed on the `Database` facade (`src/backend/database.py`):
- `db.get_telegram_verified_email(...)`
- `db.set_telegram_verified_email(...)`
- `db.clear_telegram_verified_email(...)`

### Adapter additions (`src/backend/adapters/telegram_adapter.py`)

**`async resolve_verified_email(message) -> str | None`**
Reads `db.get_telegram_verified_email(binding_id, telegram_user_id)` from the normalized message metadata. Used by the router gate before dispatching to an agent.

**`async prompt_auth(message, agent_name, bot_token=None)`**
Sends a Telegram-native HTML prompt instructing the user to verify with `/login your@email.com`. Called by the router gate when `require_email=True` and no verified email is bound.

**`handle_command` — new commands**

| Command | Behavior |
|---------|----------|
| `/login` (no args) | Sends usage instructions |
| `/login <email>` | `db.create_login_code(email, 10)` → `EmailService.send_verification_code(email, code)` → stores `_PENDING_LOGINS[(binding_id, telegram_user_id)] = email` → replies "📧 Sent a 6-digit code" |
| `/login <6-digit-code>` | Looks up pending email → `db.verify_login_code(email, code)` → on success `db.set_telegram_verified_email(binding_id, telegram_user_id, email)` + `_PENDING_LOGINS.pop(...)` → replies "✅ Verified" |
| `/logout` | `db.clear_telegram_verified_email(...)` + `_PENDING_LOGINS.pop(...)` |
| `/whoami` | Displays current verified email (or "not verified") |

**`_PENDING_LOGINS`** is a module-level in-memory `dict[(binding_id, telegram_user_id) -> email]`. Login codes have a 10-minute TTL in `email_login_codes` (same table used by email authentication), so a backend restart mid-verification simply forces the user to re-issue `/login <email>` — no migration required, no persistent state to corrupt.

### Router gate integration

The unified gate lives in `adapters/message_router.py` at step 5b of `_handle_message_inner` (see [unified-channel-access-control.md](unified-channel-access-control.md) for the full policy matrix). For Telegram:

- **1:1 DMs**: Gate calls `adapter.resolve_verified_email(message)` before step 9 (execution). If `None` and policy `require_email=True`, it invokes `adapter.prompt_auth(...)` and short-circuits — the message is not sent to the agent.
- **Group chats**: The gate is **bypassed** for group messages. Groups are gated by bot membership (the group admin who added the bot is trusted), not per-user email verification. Trying to verify every group member via DM would be a poor UX and, for large groups, impractical.

### `/login` state machine (DM)

```
stranger sends "hi"
    |
    v
router step 5b: resolve_verified_email() → None
    |
    v
policy.require_email=True → adapter.prompt_auth()
    |
    v
user sends "/login user@example.com"
    |
    v
handle_command:
    db.create_login_code(email, ttl=10min)
    EmailService.send_verification_code(email, code)
    _PENDING_LOGINS[(binding_id, user_id)] = email
    reply: "📧 Sent a 6-digit code to user@example.com"
    |
    v
user sends "/login 123456"
    |
    v
handle_command:
    email = _PENDING_LOGINS[(binding_id, user_id)]
    db.verify_login_code(email, "123456") → OK
    db.set_telegram_verified_email(binding_id, user_id, email)
    _PENDING_LOGINS.pop(...)
    reply: "✅ Verified as user@example.com"
    |
    v
user sends "what's the weather?"
    |
    v
router step 5b: resolve_verified_email() → "user@example.com"
    |
    v
gate admits (or issues access-request, per policy) → agent executes
```

### Transport

No changes to `src/backend/adapters/transports/telegram_webhook.py`. The transport already dispatches `/`-prefixed messages to `adapter.handle_command` before the router pipeline, so `/login`, `/logout`, and `/whoami` are picked up for free.

## Related Flows
- [unified-channel-access-control.md](unified-channel-access-control.md) — Cross-channel access primitive (policy, router gate, access requests) (#311)
- [agent-sharing.md](agent-sharing.md) — Allow-list / ownership model the gate consults
- [email-authentication.md](email-authentication.md) — Shared `email_login_codes` infrastructure and `EmailService.send_verification_code`
- [slack-integration.md](slack-integration.md) — Slack equivalent (SLACK-001)
- [slack-channel-routing.md](slack-channel-routing.md) — Channel adapter abstraction (SLACK-002)
- [public-agent-links.md](public-agent-links.md) — Web-based public chat (shares session/execution infrastructure)
- [task-execution-service.md](task-execution-service.md) — Unified execution path (EXEC-024)
