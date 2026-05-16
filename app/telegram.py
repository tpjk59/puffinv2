"""Telegram webhook handler — parses incoming updates and drives the agent loop."""

import asyncio
import base64
import logging
import os
import traceback

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from agent.loop import run_agent

_TELEGRAM_API = "https://api.telegram.org"
_log = logging.getLogger(__name__)

# In-memory conversation history keyed by chat_id.
# Survives between messages; cleared on server restart (deploys).
_history: dict[int, list[dict]] = {}
_MAX_HISTORY_MESSAGES = 20  # 10 exchanges


async def _download_file_b64(bot_token: str, file_id: str) -> tuple[str, str]:
    """Download a Telegram file by file_id and return (base64_data, media_type)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            f"{_TELEGRAM_API}/bot{bot_token}/getFile",
            params={"file_id": file_id},
        )
        r.raise_for_status()
        file_path: str = r.json()["result"]["file_path"]

        r2 = await client.get(f"{_TELEGRAM_API}/file/bot{bot_token}/{file_path}")
        r2.raise_for_status()
        data = base64.b64encode(r2.content).decode()

    ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else "jpg"
    media_type = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
    return data, media_type


async def _keep_typing(bot_token: str, chat_id: int) -> None:
    """Send typing action every 4 seconds until cancelled."""
    url = f"{_TELEGRAM_API}/bot{bot_token}/sendChatAction"
    while True:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(url, json={"chat_id": chat_id, "action": "typing"})
        except Exception:
            pass
        await asyncio.sleep(4)


async def send_message(bot_token: str, chat_id: int | str, text: str) -> None:
    """Send a text message to a Telegram chat.

    Tries Markdown first; falls back to plain text if Telegram rejects it.
    """
    url = f"{_TELEGRAM_API}/bot{bot_token}/sendMessage"
    for chunk in _chunk(text, 4096):
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                url,
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"},
            )
            if r.status_code != 200:
                _log.warning("Markdown send failed (%s): %s — retrying as plain text", r.status_code, r.text)
                await client.post(url, json={"chat_id": chat_id, "text": chunk})


def _chunk(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


async def handle_update(update: dict, session: AsyncSession) -> None:
    """Entry point called by the FastAPI webhook route for every Telegram update."""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat_id: int = message["chat"]["id"]
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        return

    history = _history.get(chat_id, [])
    typing_task = asyncio.create_task(_keep_typing(bot_token, chat_id))

    user_text = ""
    response_text = ""
    try:
        if "photo" in message:
            file_id = message["photo"][-1]["file_id"]
            image_b64, media_type = await _download_file_b64(bot_token, file_id)
            user_text = (
                message.get("caption")
                or "Here's a photo of some food. Please identify the ingredients."
            )
            _log.info("photo message from chat %s (caption: %s)", chat_id, user_text[:80])
            response_text = await run_agent(
                user_text, session, image_b64=image_b64, media_type=media_type,
                history=history,
            )
        elif "text" in message:
            user_text = message["text"]
            _log.info("text message from chat %s: %s", chat_id, user_text[:120])
            response_text = await run_agent(user_text, session, history=history)
        else:
            typing_task.cancel()
            return
    except Exception as exc:  # noqa: BLE001
        _log.error("agent error for chat %s:\n%s", chat_id, traceback.format_exc())
        response_text = f"Sorry, something went wrong: {exc}"
    finally:
        typing_task.cancel()

    _log.info("agent response for chat %s: %s", chat_id, response_text[:120])

    if user_text and response_text:
        chat_history = _history.setdefault(chat_id, [])
        chat_history.append({"role": "user", "content": user_text})
        chat_history.append({"role": "assistant", "content": response_text})
        if len(chat_history) > _MAX_HISTORY_MESSAGES:
            _history[chat_id] = chat_history[-_MAX_HISTORY_MESSAGES:]

    if response_text:
        await send_message(bot_token, chat_id, response_text)
