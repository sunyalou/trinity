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
  ├── Sanitize filename (basename, regex, hidden file check)
  ├── adapter.download_file() → bytes (channel-agnostic)
  ├── Image? → base64 encode → ![name](data:mime;base64,...)
  └── Text?  → tar archive → container_put_archive to uploads/{session_id}/
  ↓
Context prompt += [Uploaded files] block
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
- Step 7b: `_handle_file_uploads()` — download, route by type, copy/embed
- Filename sanitization: `os.path.basename` + `re.sub(r'[^\w\s.\-()]', '_', ...)` + hidden file fallback
- Session ID sanitization: `re.sub(r"[^a-zA-Z0-9_-]", "_", session_id)` for shell safety
- Image budget: 5MB/image, 10MB total, excess skipped
- Unsupported MIME rejection: PDF, ZIP, tar, gzip, rar, video/*, audio/*
- Allowed tools: `Read` added only for non-image files
- Cleanup on all exit paths (success, task failure, exception)

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
| Container copy fails | Logged, description says "copy to agent failed" |
| Image budget exceeded | Remaining images skipped with note |
| >10 files | Excess truncated with count message |
| Path traversal filename | Sanitized to safe basename or `file_{id}` fallback |
| File upload rate limited | Slack user gets "uploading too quickly" message |

## Security Considerations

- **Path traversal**: `../../.env` → sanitized via `os.path.basename` + regex + hidden file rejection
- **Shell injection**: Session ID sanitized before use in `rm -rf` / `mkdir -p` commands
- **Allowed tools escalation**: `Read` only added for non-image files. Images use base64 (no tool needed). Agent can still read `.env` with Read — accepted trade-off for now.
- **Rate limiting**: Separate file upload rate limit (5/min) in addition to message rate limit (30/min)
- **Size limits**: 5MB/image inline, 10MB/file container, 10MB total images, max 10 files

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
