"""APScheduler setup — scheduled agent jobs and recurring delivery nudges.

Jobs are added in setup_scheduler() which is called from app/main.py on startup
when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are configured.
"""

import json
import logging
from datetime import date

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agent.loop import run_agent
from db.database import AsyncSessionLocal

_scheduler = AsyncIOScheduler()
_log = logging.getLogger(__name__)


async def _send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
        if r.status_code != 200:
            # Fall back to plain text if Markdown fails
            await client.post(url, json={"chat_id": chat_id, "text": text})


async def _run_and_send(prompt: str, bot_token: str, chat_id: str) -> None:
    async with AsyncSessionLocal() as session:
        text = await run_agent(prompt, session)
    await _send_telegram(bot_token, chat_id, text)


async def _packed_lunch_nudge(bot_token: str, chat_id: str) -> None:
    await _run_and_send(
        "It's a weekday morning. Suggest what to pack for lunch today — "
        "check the inventory and preferences first, prioritise anything close to expiry.",
        bot_token,
        chat_id,
    )


async def _batch_cook_suggestion(bot_token: str, chat_id: str) -> None:
    await _run_and_send(
        "It's Sunday afternoon, time to think about batch cooking. "
        "Check the inventory and preferences, then suggest a batch cook for the week. "
        "Keep it to 2–3 options.",
        bot_token,
        chat_id,
    )


async def _scrape_source(source_label: str, bot_token: str, chat_id: str) -> None:
    """Scrape a delivery source, persist arrivals, and notify via Telegram."""
    try:
        async with AsyncSessionLocal() as session:
            from agent.tools import fetch_from_source
            result = await fetch_from_source(session, source_label=source_label)
        count = result.get("count", 0)
        label = source_label.replace("_", " ").title()
        if count > 0:
            names = [i["name"] for i in result["added"][:5]]
            summary = ", ".join(names)
            if count > 5:
                summary += f" and {count - 5} more"
            text = f"{label} scraped: {count} item(s) added to inventory ({summary})."
        else:
            text = f"{label} scraped but no items found — check the URL or the page format."
        await _send_telegram(bot_token, chat_id, text)
    except ValueError:
        pass  # URL not configured — skip silently
    except Exception as exc:
        await _send_telegram(bot_token, chat_id, f"Error scraping {source_label}: {exc}")


async def _expiry_check(bot_token: str, chat_id: str) -> None:
    await _run_and_send(
        "Check for any ingredients expiring within the next 3 days. "
        "Also check for cooked meals stored fresh (location='fresh') that were cooked "
        "3 or more days ago — these should be eaten today or tomorrow. "
        "If anything is at risk, suggest a quick way to use it up. "
        "If nothing is expiring, say so briefly.",
        bot_token,
        chat_id,
    )


async def _recurring_delivery_check(bot_token: str, chat_id: str) -> None:
    """Send a confirmation nudge for any recurring delivery scheduled for today."""
    today = date.today()
    day_name = today.strftime("%A").lower()  # "monday", "thursday", etc.

    try:
        async with AsyncSessionLocal() as session:
            from db import crud
            deliveries = await crud.list_recurring_deliveries(session, active_only=True)
    except Exception as exc:
        _log.error("recurring_delivery_check: failed to query DB: %s", exc)
        return

    for rd in deliveries:
        if rd.paused_until and rd.paused_until >= today:
            continue
        delivery_days = [d.strip() for d in rd.days.split(",")]
        if day_name not in delivery_days:
            continue

        items = json.loads(rd.items_json)
        item_desc = ", ".join(
            f"{i['quantity']} {i['unit']} {i['name']}" for i in items
        )
        msg = (
            f"🚚 {rd.description}\n"
            f"{item_desc}\n"
            f"Did it arrive? Reply *yes* to add to inventory, or *skip* if not today."
        )
        await _send_telegram(bot_token, chat_id, msg)

        # Store in conversation history so the agent has context when the user replies
        from app.telegram import inject_assistant_message
        try:
            inject_assistant_message(int(chat_id), msg)
        except (ValueError, TypeError):
            pass


def setup_scheduler(bot_token: str, chat_id: str) -> None:
    """Register all scheduled jobs and start the scheduler."""
    _scheduler.add_job(
        _packed_lunch_nudge,
        CronTrigger(day_of_week="mon-fri", hour=7, minute=30),
        args=[bot_token, chat_id],
        id="packed_lunch_nudge",
        replace_existing=True,
    )
    _scheduler.add_job(
        _batch_cook_suggestion,
        CronTrigger(day_of_week="sun", hour=16, minute=0),
        args=[bot_token, chat_id],
        id="batch_cook_suggestion",
        replace_existing=True,
    )
    _scheduler.add_job(
        _expiry_check,
        CronTrigger(hour=8, minute=0),
        args=[bot_token, chat_id],
        id="expiry_check",
        replace_existing=True,
    )
    _scheduler.add_job(
        _recurring_delivery_check,
        CronTrigger(hour=7, minute=15),
        args=[bot_token, chat_id],
        id="recurring_delivery_check",
        replace_existing=True,
    )
    # Source scraping jobs — run only if the URL is configured in .env
    import os
    if os.getenv("VEG_BOX_URL"):
        _scheduler.add_job(
            _scrape_source,
            CronTrigger(day_of_week="mon", hour=7, minute=0),
            args=["veg_box", bot_token, chat_id],
            id="veg_box_scrape",
            replace_existing=True,
        )
    if os.getenv("MEAT_BOX_URL"):
        _scheduler.add_job(
            _scrape_source,
            CronTrigger(day_of_week="mon", hour=7, minute=5),
            args=["meat_box", bot_token, chat_id],
            id="meat_box_scrape",
            replace_existing=True,
        )
    _scheduler.start()
