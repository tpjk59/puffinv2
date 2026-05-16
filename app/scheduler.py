"""APScheduler setup — five scheduled agent jobs.

Jobs are added in setup_scheduler() which is called from app/main.py on startup
when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are configured.
"""

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agent.loop import run_agent
from db.database import AsyncSessionLocal

_scheduler = AsyncIOScheduler()


async def _run_and_send(prompt: str, bot_token: str, chat_id: str) -> None:
    async with AsyncSessionLocal() as session:
        text = await run_agent(prompt, session)
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=15.0) as client:
        await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})


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


async def _expiry_check(bot_token: str, chat_id: str) -> None:
    await _run_and_send(
        "Check for any ingredients expiring within the next 3 days. "
        "If anything is at risk, suggest a quick way to use it up. "
        "If nothing is expiring, say so briefly.",
        bot_token,
        chat_id,
    )


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
    # TODO: add veg_box (Monday) and meat_box (alternating Monday) scrape jobs
    # once VEG_BOX_URL and MEAT_BOX_URL are configured in .env
    _scheduler.start()
