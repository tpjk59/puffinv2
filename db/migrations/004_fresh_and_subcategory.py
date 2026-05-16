"""Migration 004: rename location 'fridge' to 'fresh'; add subcategory column."""

import asyncio

from sqlalchemy import text

from db.database import engine


async def run() -> None:
    async with engine.begin() as conn:
        await conn.execute(text(
            "ALTER TABLE ingredients ADD COLUMN subcategory VARCHAR(50)"
        ))
        await conn.execute(text(
            "UPDATE ingredients SET location = 'fresh' WHERE location = 'fridge'"
        ))
    print("Migration 004 complete: location 'fridge' → 'fresh', subcategory column added.")


if __name__ == "__main__":
    asyncio.run(run())
