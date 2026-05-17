"""FastAPI application — webhook, REST API, and web dashboard."""

import base64
import logging
import os
import secrets
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO)
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse
from starlette.types import ASGIApp

from db.database import AsyncSessionLocal, create_all_tables

_STATIC = Path(__file__).parent / "static"


class _BasicAuthMiddleware(BaseHTTPMiddleware):
    """Require HTTP Basic Auth for all routes except /health and /webhook/*."""

    def __init__(self, app: ASGIApp, password: str) -> None:
        super().__init__(app)
        self._password = password

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/health" or path.startswith("/webhook/"):
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                _, _, pwd = decoded.partition(":")
                if secrets.compare_digest(pwd, self._password):
                    return await call_next(request)
            except Exception:
                pass
        return StarletteResponse(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Puffin"'},
        )


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

_web_pass = os.getenv("WEB_PASSWORD", "")
if _web_pass:
    app.add_middleware(_BasicAuthMiddleware, password=_web_pass)

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


@app.get("/api/week-plan")
async def api_week_plan(week_start: Optional[str] = Query(None)):
    from agent.tools import get_week_plan
    async with AsyncSessionLocal() as session:
        return await get_week_plan(session, week_start=week_start)


@app.get("/api/meal-plan")
async def api_meal_plan(
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
):
    from datetime import date
    from agent.tools import get_meal_plan
    if from_date is None:
        from_date = date.today().isoformat()
    async with AsyncSessionLocal() as session:
        return await get_meal_plan(session, from_date=from_date, to_date=to_date)


# ---------------------------------------------------------------------------
# Telegram webhook
# ---------------------------------------------------------------------------


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request) -> Response:
    webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if webhook_secret:
        provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secrets.compare_digest(provided, webhook_secret):
            return Response(status_code=403)

    update = await request.json()
    # Respond immediately so Telegram doesn't retry on slow agent responses
    import asyncio
    from app.telegram import handle_update
    async def _process():
        async with AsyncSessionLocal() as session:
            await handle_update(update, session)
    asyncio.create_task(_process())
    return Response(status_code=200)
