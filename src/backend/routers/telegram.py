"""
Telegram bot integration router (TELEGRAM-001).

Thin HTTP layer that delegates to the channel adapter abstraction.

Public Endpoints (no auth — validated by webhook secret + header token):
- POST /api/telegram/webhook/{webhook_secret} — Receive Telegram updates

Authenticated Endpoints:
- GET    /api/agents/{name}/telegram       — Bot binding status
- PUT    /api/agents/{name}/telegram       — Configure bot token
- DELETE /api/agents/{name}/telegram       — Remove bot binding
- POST   /api/agents/{name}/telegram/test  — Send test message
"""

import logging
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel

from database import db
from dependencies import get_current_user, OwnedAgentByName
from models import User

logger = logging.getLogger(__name__)


# =========================================================================
# Transport reference — set by startup hook in main.py
# =========================================================================

_webhook_transport = None


def set_webhook_transport(transport):
    """Set the webhook transport instance (called from main.py startup)."""
    global _webhook_transport
    _webhook_transport = transport


# =========================================================================
# Public Router (webhook receiver — no JWT auth, validated by secret token)
# =========================================================================

public_router = APIRouter(prefix="/api/telegram", tags=["telegram-public"])


class TelegramWebhookResponse(BaseModel):
    ok: bool = True


@public_router.post("/webhook/{webhook_secret}", response_model=TelegramWebhookResponse)
async def handle_telegram_webhook(webhook_secret: str, request: Request):
    """
    Receive Telegram Bot API updates.

    Authentication: webhook_secret in URL for routing + X-Telegram-Bot-Api-Secret-Token header.
    Always returns 200 to prevent Telegram retries.
    """
    if not _webhook_transport:
        logger.warning("Telegram webhook received but transport not initialized")
        return TelegramWebhookResponse(ok=True)

    result = await _webhook_transport.handle_webhook(request, webhook_secret)
    return TelegramWebhookResponse(ok=result.get("ok", True))


# =========================================================================
# Authenticated Router (bot configuration)
# =========================================================================

auth_router = APIRouter(prefix="/api/agents", tags=["telegram"])


class TelegramBindingResponse(BaseModel):
    agent_name: str
    bot_username: Optional[str] = None
    bot_id: Optional[str] = None
    webhook_url: Optional[str] = None
    bot_link: Optional[str] = None
    configured: bool = False


class TelegramConfigureRequest(BaseModel):
    bot_token: str


class TelegramTestRequest(BaseModel):
    chat_id: Optional[str] = None
    message: str = "Hello from Trinity! Your Telegram bot is configured correctly."


@auth_router.get("/{agent_name}/telegram", response_model=TelegramBindingResponse)
async def get_telegram_binding(
    agent_name: str,
    current_user: User = Depends(get_current_user)
):
    """Get Telegram bot binding status for an agent."""
    binding = db.get_telegram_binding(agent_name)
    if not binding:
        return TelegramBindingResponse(agent_name=agent_name, configured=False)

    bot_username = binding.get("bot_username")
    return TelegramBindingResponse(
        agent_name=agent_name,
        bot_username=bot_username,
        bot_id=binding.get("bot_id"),
        webhook_url=binding.get("webhook_url"),
        bot_link=f"https://t.me/{bot_username}" if bot_username else None,
        configured=True,
    )


@auth_router.put("/{agent_name}/telegram", response_model=TelegramBindingResponse)
async def configure_telegram_bot(
    agent_name: OwnedAgentByName,
    config: TelegramConfigureRequest,
):
    """
    Configure a Telegram bot for an agent.

    Validates the bot token via getMe API, stores encrypted,
    and registers the webhook if a public URL is available.
    """
    bot_token = config.bot_token.strip()

    # Validate token format: {bot_id}:{secret}
    if ":" not in bot_token:
        raise HTTPException(status_code=400, detail="Invalid bot token format. Expected format: 123456:ABC-DEF")

    # Validate token via getMe
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"https://api.telegram.org/bot{bot_token}/getMe")
            result = resp.json()

            if not result.get("ok"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid bot token: {result.get('description', 'Unknown error')}"
                )

            bot_info = result["result"]
            bot_username = bot_info.get("username")
            bot_id = str(bot_info.get("id"))

    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Telegram API: {e}")

    # Check bot_id isn't already bound to another agent
    existing = db.get_telegram_binding_by_bot_id(bot_id)
    if existing and existing["agent_name"] != agent_name:
        raise HTTPException(
            status_code=409,
            detail=f"This bot is already bound to agent '{existing['agent_name']}'"
        )

    # Create binding (encrypted token)
    binding = db.create_telegram_binding(
        agent_name=agent_name,
        bot_token=bot_token,
        bot_username=bot_username,
        bot_id=bot_id,
    )

    # Register webhook if public URL is available
    from services.settings_service import settings_service
    public_url = settings_service.get_setting("public_chat_url", "")
    if public_url:
        from adapters.transports.telegram_webhook import register_webhook
        await register_webhook(agent_name, public_url)
        # Refresh binding to get updated webhook_url
        binding = db.get_telegram_binding(agent_name)

    logger.info(f"Telegram bot configured for agent={agent_name} bot=@{bot_username}")

    return TelegramBindingResponse(
        agent_name=agent_name,
        bot_username=bot_username,
        bot_id=bot_id,
        webhook_url=binding.get("webhook_url") if binding else None,
        bot_link=f"https://t.me/{bot_username}" if bot_username else None,
        configured=True,
    )


@auth_router.delete("/{agent_name}/telegram")
async def delete_telegram_binding(
    agent_name: OwnedAgentByName,
):
    """Remove Telegram bot binding from an agent."""
    binding = db.get_telegram_binding(agent_name)
    if not binding:
        raise HTTPException(status_code=404, detail="No Telegram binding found")

    # Remove webhook from Telegram
    from adapters.transports.telegram_webhook import delete_webhook
    await delete_webhook(agent_name)

    # Delete from DB
    db.delete_telegram_binding(agent_name)

    logger.info(f"Telegram bot removed for agent={agent_name}")
    return {"ok": True, "message": f"Telegram bot removed from {agent_name}"}


@auth_router.post("/{agent_name}/telegram/test")
async def test_telegram_bot(
    agent_name: OwnedAgentByName,
    test: TelegramTestRequest,
):
    """Send a test message via the agent's Telegram bot."""
    bot_token = db.get_telegram_bot_token(agent_name)
    if not bot_token:
        raise HTTPException(status_code=404, detail="No Telegram binding found or token decryption failed")

    # If no chat_id provided, just verify the bot can make API calls
    if not test.chat_id:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"https://api.telegram.org/bot{bot_token}/getMe")
                result = resp.json()
                if result.get("ok"):
                    bot_info = result["result"]
                    return {
                        "ok": True,
                        "message": f"Bot @{bot_info.get('username')} is operational",
                        "bot_info": bot_info,
                    }
                else:
                    return {"ok": False, "message": result.get("description", "Unknown error")}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    # Send test message to specific chat
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": test.chat_id,
                    "text": test.message,
                    "parse_mode": "HTML",
                }
            )
            result = resp.json()
            if result.get("ok"):
                return {"ok": True, "message": "Test message sent successfully"}
            else:
                return {"ok": False, "message": result.get("description", "Failed to send")}
    except Exception as e:
        return {"ok": False, "message": str(e)}
