"""Migration 002 — add notes column to ingredients table.

Run with:  python -m db.migrations.002_add_ingredient_notes
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text
from db.database import engine


async def main() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE ingredients ADD COLUMN notes TEXT"))
    print("Migration 002 complete: notes column added to ingredients.")


if __name__ == "__main__":
    asyncio.run(main())
