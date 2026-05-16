"""Telegram webhook handler — parses incoming updates and drives the agent loop."""

import base64
import os

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from agent.loop import run_agent

_TELEGRAM_API = "https://api.telegram.org"

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


async def send_message(bot_token: str, chat_id: int | str, text: str) -> None:
    """Send a text message to a Telegram chat."""
    url = f"{_TELEGRAM_API}/bot{bot_token}/sendMessage"
    # Telegram has a 4096-char limit per message
    for chunk in _chunk(text, 4096):
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(
                url,
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"},
            )


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

    try:
        if "photo" in message:
            file_id = message["photo"][-1]["file_id"]
            image_b64, media_type = await _download_file_b64(bot_token, file_id)
            user_text = (
                message.get("caption")
                or "Here's a photo of some food. Please identify the ingredients."
            )
            response_text = await run_agent(
                user_text, session, image_b64=image_b64, media_type=media_type,
                history=history,
            )
        elif "text" in message:
            user_text = message["text"]
            response_text = await run_agent(user_text, session, history=history)
        else:
            return
    except Exception as exc:  # noqa: BLE001
        response_text = f"Sorry, something went wrong: {exc}"
    else:
        # Append this exchange to history and trim to the cap
        chat_history = _history.setdefault(chat_id, [])
        chat_history.append({"role": "user", "content": user_text})
        chat_history.append({"role": "assistant", "content": response_text})
        if len(chat_history) > _MAX_HISTORY_MESSAGES:
            _history[chat_id] = chat_history[-_MAX_HISTORY_MESSAGES:]

    await send_message(bot_token, chat_id, response_text)
