"""Migration 005: add meal_plan and meal_plan_ingredients tables."""

import asyncio

from sqlalchemy import text

from db.database import engine


async def run() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS meal_plan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(255) NOT NULL,
                cuisine_tag VARCHAR(100),
                planned_date DATE NOT NULL,
                servings INTEGER NOT NULL DEFAULT 2,
                status VARCHAR(20) NOT NULL DEFAULT 'planned',
                source_url VARCHAR(500),
                notes TEXT
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS meal_plan_ingredients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL REFERENCES meal_plan(id) ON DELETE CASCADE,
                name VARCHAR(255) NOT NULL,
                quantity FLOAT NOT NULL,
                unit VARCHAR(50) NOT NULL,
                notes TEXT
            )
        """))
    print("Migration 005 complete: meal_plan and meal_plan_ingredients tables created.")


if __name__ == "__main__":
    asyncio.run(run())
