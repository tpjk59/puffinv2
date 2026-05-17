"""Migration 008: add recurring_deliveries table."""

import asyncio

from sqlalchemy import text

from db.database import engine


async def run() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS recurring_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label VARCHAR(100) NOT NULL UNIQUE,
                description VARCHAR(255) NOT NULL,
                items_json TEXT NOT NULL,
                days VARCHAR(100) NOT NULL,
                send_time VARCHAR(5) NOT NULL DEFAULT '07:00',
                active INTEGER NOT NULL DEFAULT 1,
                paused_until DATE
            )
        """))
    print("Migration 008 complete: recurring_deliveries table created.")


if __name__ == "__main__":
    asyncio.run(run())
