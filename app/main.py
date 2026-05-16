"""FastAPI application — webhook, REST API, and web dashboard."""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from db.database import AsyncSessionLocal, create_all_tables

_STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_all_tables()
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if bot_token and chat_id:
        from app.scheduler import setup_scheduler
        setup_scheduler(bot_token, chat_id)
    yield


app = FastAPI(title="Meal Planner", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=_STATIC), name="static")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@app.get("/", response_class=FileResponse, include_in_schema=False)
async def dashboard():
    return FileResponse(_STATIC / "index.html")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# REST API (consumed by the web dashboard)
# ---------------------------------------------------------------------------


@app.get("/api/inventory")
async def api_inventory(location: Optional[str] = Query(None)):
    from agent.tools import get_inventory
    async with AsyncSessionLocal() as session:
        return await get_inventory(session, location=location)


@app.get("/api/meals")
async def api_meals(location: Optional[str] = Query(None)):
    from agent.tools import get_meal_history
    async with AsyncSessionLocal() as session:
        return await get_meal_history(session, location=location)


@app.get("/api/nutrition/summary")
async def api_nutrition_summary(period: str = Query("today")):
    from agent.tools import get_nutrition_summary
    async with AsyncSessionLocal() as session:
        return await get_nutrition_summary(session, period=period)


@app.get("/api/preferences")
async def api_preferences():
    from agent.tools import get_preferences
    async with AsyncSessionLocal() as session:
        return await get_preferences(session)


@app.get("/api/delivery-schedule")
async def api_delivery_schedule(source_label: Optional[str] = Query(None)):
    from agent.tools import get_delivery_schedule
    async with AsyncSessionLocal() as session:
        return await get_delivery_schedule(session, source_label=source_label)


# ---------------------------------------------------------------------------
# Telegram webhook
# ---------------------------------------------------------------------------


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request) -> Response:
    update = await request.json()
    from app.telegram import handle_update
    async with AsyncSessionLocal() as session:
        await handle_update(update, session)
    return Response(status_code=200)
