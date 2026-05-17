"""Migration 007: add recipes table."""

import asyncio

from sqlalchemy import text

from db.database import engine


async def run() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS recipes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(255) NOT NULL,
                source_url VARCHAR(500),
                cuisine_tag VARCHAR(100),
                tags VARCHAR(500),
                notes TEXT,
                times_planned INTEGER NOT NULL DEFAULT 0,
                last_planned DATE,
                created_at DATE NOT NULL
            )
        """))
    print("Migration 007 complete: recipes table created.")


if __name__ == "__main__":
    asyncio.run(run())
