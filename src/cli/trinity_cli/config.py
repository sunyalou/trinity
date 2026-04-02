"""Configuration management for Trinity CLI.

Stores instance URL, auth token, and user info in ~/.trinity/config.json.
"""

import json
import os
import stat
from pathlib import Path
from typing import Optional


CONFIG_DIR = Path.home() / ".trinity"
CONFIG_FILE = CONFIG_DIR / "config.json"


def _ensure_config_dir():
    CONFIG_DIR.mkdir(mode=0o700, exist_ok=True)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    return json.loads(CONFIG_FILE.read_text())


def save_config(config: dict):
    _ensure_config_dir()
    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
    os.chmod(CONFIG_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 0600


def get_instance_url() -> Optional[str]:
    """Get configured instance URL, falling back to env var."""
    url = os.environ.get("TRINITY_URL")
    if url:
        return url.rstrip("/")
    config = load_config()
    url = config.get("instance_url")
    return url.rstrip("/") if url else None


def get_api_key() -> Optional[str]:
    """Get API key/token, falling back to env var."""
    key = os.environ.get("TRINITY_API_KEY")
    if key:
        return key
    config = load_config()
    return config.get("token")


def set_auth(instance_url: str, token: str, user: Optional[dict] = None):
    config = load_config()
    config["instance_url"] = instance_url.rstrip("/")
    config["token"] = token
    if user:
        config["user"] = user
    save_config(config)


def clear_auth():
    config = load_config()
    config.pop("token", None)
    config.pop("user", None)
    save_config(config)


def get_user() -> Optional[dict]:
    config = load_config()
    return config.get("user")
