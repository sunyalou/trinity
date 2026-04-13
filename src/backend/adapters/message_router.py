"""
Channel-agnostic message router.

Receives NormalizedMessage from any adapter, resolves the agent,
builds context, dispatches to the agent via TaskExecutionService,
persists messages, and returns the response through the adapter.

Uses the same execution path as web public chat (EXEC-024) for:
- Execution records and audit trail
- Activity tracking (Dashboard timeline)
- Slot management (capacity limits)
- Credential sanitization
"""

import io
import logging
import re
import tarfile
import time
from typing import List, Optional, Tuple
from collections import defaultdict

from database import db
from services.docker_service import get_agent_container
from services.settings_service import settings_service
from services.task_execution_service import get_task_execution_service
from services.docker_utils import container_put_archive, container_exec_run
from adapters.base import ChannelAdapter, ChannelResponse, FileAttachment, NormalizedMessage, OutboundFile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configurable defaults (overridable via settings_service)
# ---------------------------------------------------------------------------

_DEFAULT_RATE_LIMIT_MAX = 30        # messages per window
_DEFAULT_RATE_LIMIT_WINDOW = 60     # seconds
_DEFAULT_CHANNEL_TIMEOUT = 120      # seconds
_DEFAULT_CHANNEL_ALLOWED_TOOLS = "WebSearch,WebFetch"
_FILE_UPLOAD_RATE_LIMIT_MAX = 5     # file uploads per window
_FILE_UPLOAD_RATE_LIMIT_WINDOW = 60 # seconds

# Outbound file extraction from agent responses
_OUTBOUND_MIN_BLOCK_CHARS = 100
_OUTBOUND_MAX_FILES = 5
_OUTBOUND_MAX_FILE_BYTES = 500 * 1024       # 500 KB per block
_OUTBOUND_MAX_TOTAL_BYTES = 2 * 1024 * 1024 # 2 MB total

_OUTBOUND_LANG_MAP = {
    "csv": "csv", "json": "json", "html": "html", "xml": "xml",
    "yaml": "yaml", "yml": "yaml", "sql": "sql",
    "python": "py", "py": "py",
    "javascript": "js", "js": "js", "typescript": "ts", "ts": "ts",
    "txt": "txt", "text": "txt",
}

# Regex: fenced code block at start of line with language hint
_CODE_BLOCK_RE = re.compile(r'^```(\w+)\s*\n(.*?)^```', re.DOTALL | re.MULTILINE)


def _get_rate_limit_max() -> int:
    return int(settings_service.get_setting("channel_rate_limit_max", str(_DEFAULT_RATE_LIMIT_MAX)))


def _get_rate_limit_window() -> int:
    return int(settings_service.get_setting("channel_rate_limit_window", str(_DEFAULT_RATE_LIMIT_WINDOW)))


def _get_channel_timeout() -> int:
    return int(settings_service.get_setting("channel_timeout_seconds", str(_DEFAULT_CHANNEL_TIMEOUT)))


def _get_channel_allowed_tools() -> List[str]:
    raw = settings_service.get_setting("channel_allowed_tools", _DEFAULT_CHANNEL_ALLOWED_TOOLS)
    return [t.strip() for t in raw.split(",") if t.strip()]


# ---------------------------------------------------------------------------
# Simple in-memory rate limiter with periodic pruning
# ---------------------------------------------------------------------------

_rate_limit_buckets: dict = defaultdict(list)  # key → list of timestamps
_PRUNE_INTERVAL = 300  # prune stale buckets every 5 minutes
_last_prune_time: float = 0.0


def _prune_stale_buckets() -> None:
    """Remove empty or fully-expired buckets to prevent memory leaks."""
    global _last_prune_time
    now = time.time()
    if now - _last_prune_time < _PRUNE_INTERVAL:
        return
    _last_prune_time = now
    window = _get_rate_limit_window()
    stale_keys = [k for k, v in _rate_limit_buckets.items() if not v or v[-1] < now - window]
    for k in stale_keys:
        del _rate_limit_buckets[k]
    if stale_keys:
        logger.debug(f"[ROUTER] Pruned {len(stale_keys)} stale rate-limit buckets")


def _check_rate_limit(key: str, max_msgs: Optional[int] = None, window: Optional[int] = None) -> bool:
    """Returns True if allowed, False if rate limited."""
    _prune_stale_buckets()
    now = time.time()
    window = window or _get_rate_limit_window()
    max_msgs = max_msgs or _get_rate_limit_max()
    bucket = _rate_limit_buckets[key]
    # Remove expired entries
    _rate_limit_buckets[key] = [t for t in bucket if now - t < window]
    if len(_rate_limit_buckets[key]) >= max_msgs:
        return False
    _rate_limit_buckets[key].append(now)
    return True


def _format_file_size(size_bytes: int) -> str:
    """Format bytes as human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


class ChannelMessageRouter:
    """Channel-agnostic message dispatcher."""

    async def handle_message(self, adapter: ChannelAdapter, message: NormalizedMessage) -> None:
        """Process an incoming message through the full pipeline."""
        try:
            await self._handle_message_inner(adapter, message)
        except Exception as e:
            logger.error(f"[ROUTER] Unhandled error in handle_message: {e}", exc_info=True)

    async def _handle_message_inner(self, adapter: ChannelAdapter, message: NormalizedMessage) -> None:
        channel = adapter.channel_type
        logger.info(f"[ROUTER:{channel}] START: sender={message.sender_id}, channel={message.channel_id}")

        # 1. Resolve agent
        agent_name = await adapter.get_agent_name(message)
        logger.debug(f"[ROUTER:{channel}] Step 1 - resolved agent: {agent_name}")
        if not agent_name:
            logger.warning(f"[ROUTER:{channel}] No agent found for channel {message.channel_id}")
            return

        # 2. Resolve bot token (needed for all responses)
        bot_token = adapter.get_bot_token(message)
        logger.debug(f"[ROUTER:{channel}] Step 2 - bot_token: {'yes' if bot_token else 'NO'}")
        if not bot_token:
            logger.error(f"[ROUTER:{channel}] No bot token for message in {message.channel_id}")
            return

        # 3. Rate limiting per channel user
        rate_key = adapter.get_rate_key(message)
        is_group = message.metadata.get("is_group", False)
        if not _check_rate_limit(rate_key):
            logger.warning(f"[ROUTER:{channel}] Rate limited: {rate_key}")
            # In groups, silently drop to avoid spamming the group with error messages
            if is_group:
                return
            await adapter.send_response(
                message.channel_id,
                ChannelResponse(
                    text="You're sending messages too quickly. Please wait a moment.",
                    metadata={"bot_token": bot_token, "agent_name": agent_name}
                ),
                thread_id=message.thread_id,
            )
            return

        # 3b. File upload rate limiting (stricter than message rate limit)
        if message.files:
            file_rate_key = f"{channel}-files:{message.channel_id}:{message.sender_id}"
            if not _check_rate_limit(file_rate_key, max_msgs=_FILE_UPLOAD_RATE_LIMIT_MAX, window=_FILE_UPLOAD_RATE_LIMIT_WINDOW):
                logger.warning(f"[ROUTER] File upload rate limited: {file_rate_key}")
                await adapter.send_response(
                    message.channel_id,
                    ChannelResponse(
                        text="You're uploading files too quickly. Please wait a moment.",
                        metadata={"bot_token": bot_token, "agent_name": agent_name}
                    ),
                    thread_id=message.thread_id,
                )
                return

        # 4. Check agent availability
        container = get_agent_container(agent_name)
        container_status = container.status if container else "not_found"
        logger.debug(f"[ROUTER:{channel}] Step 4 - container: {container_status}")
        if not container or container.status != "running":
            await adapter.send_response(
                message.channel_id,
                ChannelResponse(
                    text="Sorry, I'm not available right now. Please try again later.",
                    metadata={"bot_token": bot_token, "agent_name": agent_name}
                ),
            )
            return

        # 5. Handle verification (base class default: always verified)
        logger.debug(f"[ROUTER:{channel}] Step 5 - running verification")
        verified = await adapter.handle_verification(message)
        logger.debug(f"[ROUTER:{channel}] Step 5 - verified: {verified}")
        if not verified:
            return

        # 5b. Unified cross-channel access gate (Issue #311).
        # Resolve a verified email via the adapter, then apply the agent's
        # access policy. Group chats bypass — group context is gated by the
        # bot being added to the group, not by per-user email.
        verified_email: Optional[str] = None
        if not is_group:
            try:
                verified_email = await adapter.resolve_verified_email(message)
            except Exception as e:
                logger.warning(f"[ROUTER:{channel}] resolve_verified_email error: {e}")
                verified_email = None

            policy = db.get_access_policy(agent_name)
            require_email = policy.get("require_email", False)
            open_access = policy.get("open_access", False)

            if require_email and not verified_email:
                logger.info(
                    f"[ROUTER:{channel}] Access denied: agent={agent_name} requires email "
                    f"and sender={message.sender_id} not verified"
                )
                await adapter.prompt_auth(message, agent_name, bot_token)
                return

            if verified_email and db.email_has_agent_access(agent_name, verified_email):
                logger.debug(f"[ROUTER:{channel}] Access granted via owner/admin/sharing: {verified_email}")
            elif open_access:
                logger.debug(
                    f"[ROUTER:{channel}] Access granted via open_access "
                    f"(email={verified_email or 'none'})"
                )
            elif verified_email:
                # Verified email + restrictive policy → record access request
                try:
                    db.upsert_access_request(agent_name, verified_email, channel)
                except Exception as e:
                    logger.error(f"[ROUTER:{channel}] Failed to upsert access_request: {e}")
                logger.info(
                    f"[ROUTER:{channel}] Pending access request: "
                    f"agent={agent_name}, email={verified_email}"
                )
                await adapter.send_response(
                    message.channel_id,
                    ChannelResponse(
                        text=(
                            "🔒 Your access request is pending approval. "
                            "I'll let you know once the agent owner responds."
                        ),
                        metadata={"bot_token": bot_token, "agent_name": agent_name},
                    ),
                    thread_id=message.thread_id,
                )
                return
            # else: no verified email and policy not set → legacy permissive
            # (preserves backward compat for agents that haven't opted in).

        # 6. Get/create session
        logger.debug(f"[ROUTER:{channel}] Step 6 - creating session")
        session_identifier = adapter.get_session_identifier(message)
        session = db.get_or_create_public_chat_session(
            agent_name, session_identifier, channel
        )
        session_id = session.id if hasattr(session, 'id') else session["id"]
        logger.debug(f"[ROUTER:{channel}] Step 6 - session_id: {session_id}")

        # 7. Build context prompt (same as web public chat)
        # In group chats, use fresh context (no prior history) to prevent
        # leaking prior private conversation context into public group replies.
        if is_group:
            context_prompt = message.text
        else:
            context_prompt = db.build_public_chat_context(session_id, message.text)
        logger.debug(f"[ROUTER:{channel}] Step 7 - context built ({len(context_prompt)} chars, group={is_group})")

        # 7b. Handle file uploads — download from Slack, copy into agent container
        upload_dir = None  # Track for cleanup
        if message.files:
            file_descriptions, upload_dir = await self._handle_file_uploads(
                adapter, message, agent_name, container, session_id
            )
            if file_descriptions:
                file_block = "\n".join(file_descriptions)
                context_prompt = f"{context_prompt}\n\n[Uploaded files]\n{file_block}"
                logger.info(f"[ROUTER] Step 7b - {len(file_descriptions)} file(s) copied to agent")

        # 8. Show processing indicator (⏳ on Slack, typing on Telegram, etc.)
        await adapter.indicate_processing(message)

        # 9. Execute via TaskExecutionService (same path as web public chat)
        logger.debug(f"[ROUTER:{channel}] Step 9 - executing via TaskExecutionService")
        # Prefer the verified email (Issue #311) so MEM-001 keys cross-channel
        # off the same identity. Fall back to the channel-native source id.
        source_email = verified_email or adapter.get_source_identifier(message)

        # Security: restrict tools for public channel users
        # No file access (Read exposes .env/credentials), no Bash, no Write/Edit
        # Configurable via settings_service (default: WebSearch, WebFetch)
        public_allowed_tools = _get_channel_allowed_tools()

        # If user uploaded non-image files, agent needs Read to access them.
        # Images are excluded: Claude Code crashes when reading PNGs with
        # --allowedTools (API returns 400 "Could not process image" and the
        # process exits without flushing stdout, hanging the pipe reader).
        _IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"}
        has_readable_files = message.files and any(
            f.mimetype not in _IMAGE_MIMES for f in message.files
        )
        if has_readable_files and "Read" not in public_allowed_tools:
            public_allowed_tools = public_allowed_tools + ["Read"]

        try:
            task_execution_service = get_task_execution_service()

            result = await task_execution_service.execute_task(
                agent_name=agent_name,
                message=context_prompt,
                triggered_by=channel,
                source_user_email=source_email,
                timeout_seconds=None,  # Uses agent's configured timeout (TIMEOUT-001)
                allowed_tools=public_allowed_tools,
            )

            if result.status == "failed":
                error_msg = result.error or "Unknown error"
                logger.error(f"[ROUTER:{channel}] Step 9 - task failed: {error_msg}")
                await adapter.indicate_done(message)

                # Reply with the actual error if available, otherwise generic message
                if error_msg and error_msg != "Unknown error":
                    response_text = error_msg
                else:
                    response_text = "Sorry, I encountered an error processing your message."

                await adapter.send_response(
                    message.channel_id,
                    ChannelResponse(text=response_text, metadata={"bot_token": bot_token, "agent_name": agent_name}),
                    thread_id=message.thread_id,
                )
                await self._cleanup_uploads(container, upload_dir)
                return

            response_text = result.response or ""
            logger.debug(f"[ROUTER:{channel}] Step 9 - agent responded ({len(response_text)} chars, cost=${result.cost or 0:.4f})")

        except Exception as e:
            logger.error(f"[ROUTER:{channel}] Step 9 - execution error: {e}", exc_info=True)
            await adapter.indicate_done(message)
            await adapter.send_response(
                message.channel_id,
                ChannelResponse(
                    text="Sorry, I encountered an error processing your message. Please try again.",
                    metadata={"bot_token": bot_token, "agent_name": agent_name}
                ),
                thread_id=message.thread_id,
            )
            await self._cleanup_uploads(container, upload_dir)
            return

        # 10. Done processing — show completion indicator
        await adapter.indicate_done(message)

        # 11. Persist messages in session
        logger.debug(f"[ROUTER:{channel}] Step 11 - persisting messages")
        db.add_public_chat_message(session_id, "user", message.text)
        db.add_public_chat_message(session_id, "assistant", response_text, cost=result.cost)

        # 11b. Extract code blocks as outbound files (after persisting, before sending)
        outbound_files = []
        send_text = response_text
        try:
            send_text, outbound_files = self._extract_outbound_files(response_text)
            if outbound_files:
                logger.info(
                    f"[ROUTER:{channel}] Extracted {len(outbound_files)} outbound file(s) "
                    f"({sum(len(f.content) for f in outbound_files)} bytes total)"
                )
        except Exception as e:
            logger.error(f"[ROUTER:{channel}] Outbound file extraction failed: {e}", exc_info=True)

        # 12. Send response to channel
        logger.debug(f"[ROUTER:{channel}] Step 12 - sending response")
        response_metadata = {"bot_token": bot_token, "agent_name": agent_name}
        if is_group:
            response_metadata["is_group"] = True
        await adapter.send_response(
            message.channel_id,
            ChannelResponse(text=send_text, files=outbound_files, metadata=response_metadata),
            thread_id=message.thread_id,
        )

        # 13. Post-response hook (thread tracking, etc.)
        await adapter.on_response_sent(message, agent_name)

        # 14. Clean up uploaded files (per-session directory)
        await self._cleanup_uploads(container, upload_dir)

        logger.info(f"[ROUTER:{channel}] DONE: {agent_name}, execution_id={result.execution_id}")

    # =========================================================================
    # Private helpers
    # =========================================================================

    @staticmethod
    def _extract_outbound_files(response_text: str) -> Tuple[str, List[OutboundFile]]:
        """
        Extract large fenced code blocks from agent response text.

        Scans for ```lang ... ``` blocks with recognized language hints.
        Blocks exceeding the minimum size threshold are extracted as
        OutboundFile objects and replaced with a placeholder in the text.

        Returns (cleaned_text, files).
        """
        files: List[OutboundFile] = []
        total_bytes = 0
        file_counter = 0

        def replace_block(match: re.Match) -> str:
            nonlocal total_bytes, file_counter

            lang_hint = match.group(1).lower()
            content = match.group(2)

            # Skip unrecognized languages
            ext = _OUTBOUND_LANG_MAP.get(lang_hint)
            if not ext:
                return match.group(0)

            # Skip small blocks
            if len(content) < _OUTBOUND_MIN_BLOCK_CHARS:
                return match.group(0)

            # Enforce per-file size limit
            content_bytes = content.encode("utf-8")
            if len(content_bytes) > _OUTBOUND_MAX_FILE_BYTES:
                return match.group(0)

            # Enforce total size limit
            if total_bytes + len(content_bytes) > _OUTBOUND_MAX_TOTAL_BYTES:
                return match.group(0)

            # Enforce max file count
            if file_counter >= _OUTBOUND_MAX_FILES:
                return match.group(0)

            file_counter += 1
            total_bytes += len(content_bytes)
            filename = f"response_{file_counter}.{ext}"

            files.append(OutboundFile(
                filename=filename,
                content=content_bytes,
                language=lang_hint,
            ))

            return f"(see attached: {filename})"

        cleaned_text = _CODE_BLOCK_RE.sub(replace_block, response_text)
        return cleaned_text, files

    @staticmethod
    async def _cleanup_uploads(container, upload_dir: Optional[str]) -> None:
        """Remove per-session upload directory from agent container."""
        if not upload_dir:
            return
        try:
            await container_exec_run(container, f"rm -rf {upload_dir}", user="developer")
            logger.debug(f"[ROUTER] Cleaned up {upload_dir}")
        except Exception as e:
            logger.warning(f"[ROUTER] Upload cleanup failed: {e}")

    async def _handle_file_uploads(
        self,
        adapter: ChannelAdapter,
        message: NormalizedMessage,
        agent_name: str,
        container,
        session_id: str,
    ) -> tuple:
        """
        Download files via adapter and either:
        - Images: embed as base64 data URI in the prompt (Claude vision)
        - Other files: copy into per-session dir in agent container

        Returns (descriptions, upload_dir):
        - descriptions: list of context strings for prompt injection
        - upload_dir: container path to clean up after execution, or None
        """
        import base64
        import os
        import re

        MAX_FILE_SIZE = 10 * 1024 * 1024       # 10 MB per file
        MAX_IMAGE_SIZE = 5 * 1024 * 1024        # 5 MB per image for inline base64
        MAX_TOTAL_IMAGE_SIZE = 10 * 1024 * 1024 # 10 MB total across all images
        MAX_FILES = 10                          # Max files per message
        UNSUPPORTED_MIMES = {"application/pdf", "application/zip", "application/x-tar",
                             "application/gzip", "application/x-rar-compressed",
                             "video/", "audio/"}

        UPLOAD_BASE = "/home/developer/uploads"
        safe_session_id = re.sub(r"[^a-zA-Z0-9_-]", "_", session_id)
        upload_dir = f"{UPLOAD_BASE}/{safe_session_id}"
        descriptions = []
        dir_created = False
        total_image_bytes = 0

        files = message.files
        for f in files[:MAX_FILES]:
            is_image = f.mimetype.startswith("image/")

            # Reject unsupported binary formats (PDF, archives, video, audio)
            if any(f.mimetype.startswith(m) if m.endswith("/") else f.mimetype == m
                   for m in UNSUPPORTED_MIMES):
                descriptions.append(f"{f.name} — unsupported format ({f.mimetype}). Text, CSV, JSON, and image files are supported.")
                continue

            # Sanitize filename: basename only, strip path traversal
            safe_name = os.path.basename(f.name)
            safe_name = re.sub(r'[^\w\s.\-()]', '_', safe_name)  # keep alphanumeric, dots, hyphens, parens
            if not safe_name or safe_name.startswith('.'):
                safe_name = f"file_{f.id}"

            # Size checks
            size_limit = MAX_IMAGE_SIZE if is_image else MAX_FILE_SIZE
            if f.size > size_limit:
                logger.warning(f"[ROUTER] Skipping {safe_name}: too large ({f.size} bytes)")
                descriptions.append(f"{safe_name} — skipped (exceeds {_format_file_size(size_limit)} limit)")
                continue

            # Download via adapter (channel-agnostic)
            data = await adapter.download_file(f, message)
            if not data:
                logger.warning(f"[ROUTER] Failed to download {safe_name} from Slack")
                descriptions.append(f"{safe_name} — download failed")
                continue

            size_str = _format_file_size(len(data))

            if is_image:
                # Check total inline image budget
                if total_image_bytes + len(data) > MAX_TOTAL_IMAGE_SIZE:
                    logger.warning(f"[ROUTER] Skipping {safe_name}: total image budget exceeded")
                    descriptions.append(f"{safe_name} ({size_str}) — skipped (total image size limit reached)")
                    continue

                total_image_bytes += len(data)
                b64 = base64.b64encode(data).decode()
                descriptions.append(f"![{safe_name}](data:{f.mimetype};base64,{b64})")
                logger.info(f"[ROUTER] Embedded {safe_name} ({size_str}) as base64 for {agent_name}")
            else:
                # Create per-session upload directory on first non-image file
                if not dir_created:
                    try:
                        await container_exec_run(container, f"mkdir -p {upload_dir}", user="developer")
                        dir_created = True
                    except Exception as e:
                        logger.error(f"[ROUTER] Failed to create {upload_dir} in {agent_name}: {e}")
                        descriptions.append(f"{safe_name} — copy to agent failed")
                        continue

                try:
                    tar_buf = io.BytesIO()
                    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
                        info = tarfile.TarInfo(name=safe_name)
                        info.size = len(data)
                        info.uid = 1000  # developer user
                        info.gid = 1000
                        info.mode = 0o644
                        tar.addfile(info, io.BytesIO(data))
                    tar_buf.seek(0)

                    success = await container_put_archive(container, upload_dir, tar_buf.read())
                    if not success:
                        logger.error(f"[ROUTER] Failed to copy {safe_name} into {agent_name}")
                        descriptions.append(f"{safe_name} — copy to agent failed")
                        continue

                    dest_path = f"{upload_dir}/{safe_name}"
                    descriptions.append(f"{safe_name} ({size_str}, {f.mimetype}) → {dest_path}")
                    logger.info(f"[ROUTER] Copied {safe_name} ({size_str}) to {agent_name}:{dest_path}")

                except Exception as e:
                    logger.error(f"[ROUTER] Error copying {safe_name} to {agent_name}: {e}")
                    descriptions.append(f"{safe_name} — copy error")

        if len(files) > MAX_FILES:
            descriptions.append(f"({len(files) - MAX_FILES} more file(s) skipped — max {MAX_FILES} per message)")

        return descriptions, upload_dir if dir_created else None


# Singleton instance
message_router = ChannelMessageRouter()
