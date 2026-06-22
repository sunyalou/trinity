"""
Configuration constants for the agent server.
"""
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading

# CORS settings
CORS_ORIGINS = ["*"]

# Thread pool for non-blocking subprocess execution
EXECUTOR = ThreadPoolExecutor(max_workers=4)

# Lock for thread-safe state updates
STATE_LOCK = threading.Lock()

# Workspace paths
WORKSPACE_DIR = Path("/home/developer")
PLANS_ACTIVE_DIR = WORKSPACE_DIR / "plans" / "active"
PLANS_ARCHIVE_DIR = WORKSPACE_DIR / "plans" / "archive"
TRINITY_DIR = WORKSPACE_DIR / ".trinity"
CLAUDE_COMMANDS_DIR = WORKSPACE_DIR / ".claude" / "commands" / "trinity"

# File size limits
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100MB

# Runtime configuration
# Runtime selection: "claude-code", "gemini-cli", or "opencode"
AGENT_RUNTIME = os.getenv("AGENT_RUNTIME", "claude-code")
AGENT_RUNTIME_MODEL = os.getenv("AGENT_RUNTIME_MODEL", None)  # Optional model override
OPENCODE_DEFAULT_MODEL = os.getenv("OPENCODE_DEFAULT_MODEL", "anthropic/claude-sonnet-4-5")

# Claude Code defaults
DEFAULT_CONTEXT_WINDOW = 200000

# Gemini CLI defaults
GEMINI_CONTEXT_WINDOW = 1000000  # 1M tokens

# Git configuration
GIT_TIMEOUT_SECONDS = 60
