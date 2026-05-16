"""Migration 003 — make meal_ingredients.ingredient_id nullable (ON DELETE SET NULL).

SQLite does not support ALTER COLUMN, so this migration recreates the table.

Run with:  python -m db.migrations.003_meal_ingredient_nullable_fk
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text
from db.database import engine


_RECREATE = """
CREATE TABLE meal_ingredients_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_id         INTEGER NOT NULL REFERENCES meals(id),
    ingredient_id   INTEGER REFERENCES ingredients(id) ON DELETE SET NULL,
    quantity_used   REAL    NOT NULL,
    unit            VARCHAR(50) NOT NULL
);
INSERT INTO meal_ingredients_new SELECT id, meal_id, ingredient_id, quantity_used, unit
  FROM meal_ingredients;
DROP TABLE meal_ingredients;
ALTER TABLE meal_ingredients_new RENAME TO meal_ingredients;
"""


async def main() -> None:
    async with engine.begin() as conn:
        for stmt in _RECREATE.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                await conn.execute(text(stmt))
    print("Migration 003 complete: meal_ingredients.ingredient_id is now nullable.")


if __name__ == "__main__":
    asyncio.run(main())
