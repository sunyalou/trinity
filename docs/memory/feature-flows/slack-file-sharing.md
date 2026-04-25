# Feature: Slack Inbound File Sharing (SLACK-FILES)

## Overview

Slack users can upload files (images, text, CSV, JSON, etc.) alongside messages. Images are embedded as base64 for Claude vision. Text files are copied into the agent container for reading.

## User Story

As a Slack user chatting with a Trinity agent, I want to upload files so the agent can analyze images, read documents, and process data files.

## Entry Points

- **Slack**: User uploads file in DM, @mention, or thread reply
- **Transport**: Socket Mode or webhook delivers event with `files` array

## Flow

```
Slack event (message with files[])
  ↓
SlackAdapter.parse_message()
  → _extract_files(): id, name, mimetype, size, url
  → NormalizedMessage with files: [FileAttachment, ...]
  ↓
ChannelMessageRouter._handle_message_inner()
  ↓
Step 3b: File upload rate limit (5/min per user)
  ↓
Step 7b: _handle_file_uploads(adapter, message, agent_name, container, session_id)
  ↓
For each file:
  ├── Unsupported format? → skip with message
  ├── _sanitize_filename(): NFKC + basename + safe-chars + 200-char
  │     truncation + collision dedup (-1, -2, …) (#487)
  ├── adapter.download_file() → bytes (channel-agnostic)
  ├── Image? → base64 → "[File uploaded by {uploader}]: name (size) — image
  │     attached inline\n![name](data:mime;base64,…)"
  └── Text?  → tar archive → container_put_archive to uploads/{session_id}/
              → "[File uploaded by {uploader}]: name (size) saved to {path}"
  ↓
If every workspace write attempt failed → reply
  "Sorry, I couldn't save the file(s) you sent…" and abort execution (#487 AC6)
  ↓
Context prompt += per-file lines (no extra header — each line self-describes)
  ↓
Step 9: TaskExecutionService.execute_task()
  - Images: no extra tools needed (base64 in prompt)
  - Text files: Read added to allowed_tools
  ↓
Step 14: _cleanup_uploads() → rm -rf uploads/{session_id}/
```

## Frontend Layer

No frontend changes. File handling is entirely backend (Slack → adapter → router → agent).

## Backend Layer

### Models (`adapters/base.py`)
- `FileAttachment`: id, name, mimetype, size, url
- `NormalizedMessage.files`: List[FileAttachment] (default empty)
- `ChannelAdapter.download_file()`: abstract method for channel-specific downloads

### Slack Adapter (`adapters/slack_adapter.py`)
- `_extract_files(event)`: parses Slack `files` array into FileAttachment list
- `_parse_dm`, `_parse_mention`, `_parse_thread_reply`: all extract files, accept file-only messages with `"(file upload)"` placeholder
- `download_file()`: calls `slack_service.download_file()` with bot token

### Message Router (`adapters/message_router.py`)
- Step 3b: File upload rate limit (`_FILE_UPLOAD_RATE_LIMIT_MAX=5`, 60s window)
- Step 7b: `_handle_file_uploads(verified_email=...)` — download, route by type,
  copy/embed; returns `(descriptions, upload_dir, all_writes_failed)`
- Filename sanitization (`_sanitize_filename`, #487): NFKC unicode normalize
  → `os.path.basename` → safe-chars regex → `file_{id}` fallback (empty,
  dot-only, or hidden dotfiles like `.env`) → 200-char truncation preserving
  extension → collision dedup with `-1`, `-2`, … suffix
- Session ID sanitization: `re.sub(r"[^a-zA-Z0-9_-]", "_", session_id)` for shell safety
- Chat injection format: `[File uploaded by {uploader}]: {name} ({size}) saved
  to {path}` — `uploader` is the verified email (Issue #311) or
  `adapter.get_source_identifier(message)`
- Image budget: 5MB/image, 10MB total, excess skipped
- Unsupported MIME rejection: PDF, ZIP, tar, gzip, rar, video/*, audio/*
- Allowed tools: `Read` added only for non-image files
- All-writes-failed handling: when every write attempt fails, reply via
  channel with explicit error and skip agent execution (#487 AC6)
- Cleanup on all exit paths (success, task failure, exception, all-failed abort)

### Slack Service (`services/slack_service.py`)
- `download_file(bot_token, url, max_size)`: GET with Authorization header, follow redirects, 10MB cap
- OAuth scopes: `files:read` added (requires workspace reinstall)

### Docker Utils (`services/docker_utils.py`)
- `container_put_archive(container, path, data)`: async wrapper for Docker `put_archive`

## Side Effects

- Files temporarily written to agent container at `/home/developer/uploads/{session_id}/`
- Cleaned up after execution via `rm -rf`
- Rate limit buckets tracked in memory (pruned every 5 min)

## Error Handling

| Condition | Behavior |
|-----------|----------|
| No `files:read` scope | Download returns HTML instead of file → logged as download failure |
| File too large | Skipped with description in prompt |
| Unsupported format (PDF, etc.) | Skipped with user-friendly message |
| Download fails | Logged, description says "download failed" |
| Container copy fails | Logged, description prefixed `[File upload failed]:` |
| All container writes fail | Channel reply with explicit error, agent execution skipped (#487 AC6) |
| Image budget exceeded | Remaining images skipped with note |
| >10 files | Excess truncated with count message |
| Path traversal filename | Sanitized via NFKC + basename + safe-chars regex |
| Filename collision in batch | De-duped with `-1`, `-2`, … suffix |
| File upload rate limited | Slack user gets "uploading too quickly" message |

## Security Considerations

- **Path traversal**: `../../.env` → NFKC normalize (defeats fullwidth-encoded
  `．．／` variants) → `os.path.basename` → safe-chars regex → hidden-dotfile
  rejection (#487)
- **Filename length**: capped at 200 chars, extension preserved
- **Filename collision**: per-message dedup with `-1`, `-2`, … suffix (#487)
- **Shell injection**: Session ID sanitized before use in `rm -rf` / `mkdir -p` commands
- **Allowed tools escalation**: `Read` only added for non-image files. Images use base64 (no tool needed). Agent can still read `.env` with Read — accepted trade-off for now.
- **Rate limiting**: Separate file upload rate limit (5/min) in addition to message rate limit (30/min)
- **Size limits**: 5MB/image inline, 10MB/file container, 10MB total images, max 10 files
- **Audit trail**: Every successful upload logs `dest_path`, `storage`, and
  `uploader` (verified email or channel-native identity) via
  `platform_audit_service` (#487)

## Testing

### Unit Tests (`tests/unit/test_slack_file_uploads.py`)
36 tests covering:
- Filename sanitization (traversal, hidden files, special chars, empty)
- File type routing (image, text, unsupported)
- Slack event file extraction
- Size limits and file count caps
- Format file size helper
- Per-session directory naming

### Manual Tests
- [x] Image upload via Slack → agent describes image content
- [x] Text file via API → agent reads and summarizes
- [x] Text message without files → works unchanged
- [x] PDF upload → rejected with user message

**Last Tested**: 2026-03-31
**Status**: ✅ Working

## Related Flows

- [slack-channel-routing.md](slack-channel-routing.md) — Channel adapter abstraction (SLACK-002)
- [slack-integration.md](slack-integration.md) — Original Slack integration (SLACK-001)
