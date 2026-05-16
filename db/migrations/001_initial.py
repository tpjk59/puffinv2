"""Migration 001 — create all tables from scratch.

Run with:  python -m db.migrations.001_initial
"""

import asyncio
import sys
from pathlib import Path

# Allow running as a script from the project root
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from db.database import create_all_tables, DATABASE_URL


async def main() -> None:
    print(f"Running migration 001 against: {DATABASE_URL}")
    await create_all_tables()
    print("Migration 001 complete: all tables created.")


if __name__ == "__main__":
    asyncio.run(main())
