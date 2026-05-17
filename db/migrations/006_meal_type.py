"""Migration 006: add meal_type column to meal_plan."""

import asyncio

from sqlalchemy import text

from db.database import engine


async def run() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("""
            ALTER TABLE meal_plan ADD COLUMN meal_type VARCHAR(20)
        """))
    print("Migration 006 complete: meal_type column added to meal_plan.")


if __name__ == "__main__":
    asyncio.run(run())
