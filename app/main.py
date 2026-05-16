"""FastAPI application — webhook endpoint and health check."""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from db.database import AsyncSessionLocal, create_all_tables


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


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request) -> Response:
    update = await request.json()
    from app.telegram import handle_update
    async with AsyncSessionLocal() as session:
        await handle_update(update, session)
    return Response(status_code=200)
