I am building a personal agentic meal planning application. The full project
brief is in CLAUDE.md — read that carefully before writing any code, and
refer back to it throughout this session. Think hard about the overall design
before starting.

Work through the build in phases. For each phase: briefly state your plan,
then implement it fully with tests before moving on. Pause after each phase
and summarise what was built.

**Phase 1 — Foundation: sources protocol + database**

1. Implement the FoodSource protocol and IngredientArrival dataclass exactly
   as specified in CLAUDE.md sources/base.py. Include docstrings.

2. Implement sources/registry.py with functions to register, retrieve, and
   list sources. Sources should be registerable by label string.

3. Implement sources/manual.py — a source that parses a plain text
   description ("500g chicken thighs, fridge, best before Friday") into
   IngredientArrival objects. Use the LLM to do the parsing (a simple
   single-turn call, not the full agent loop) so it handles natural language
   gracefully.

4. Implement all SQLAlchemy models in db/models.py as specified in CLAUDE.md.
   Use async SQLAlchemy. Include db/database.py with engine and session factory.

5. Write db/crud.py with async CRUD functions for every model. No business
   logic here — pure database operations.

6. Write a db/migrations/ script that creates all tables from scratch.

7. Write cli.py with:
   - `seed` command: populates all preference keys from CLAUDE.md with their
     seed values, plus 10 realistic British-kitchen ingredients (use British
     names: courgette, aubergine, etc.), 2 batch-cooked meals with portions
     in the freezer, and 3 days of nutrition log entries
   - `list-sources` command: prints registered sources

8. Write tests covering:
   - The FoodSource protocol (verify manual.py satisfies it)
   - All crud functions (use in-memory SQLite)
   - Manual source parsing (mock the LLM call)

Once Phase 1 tests pass, pause and summarise before moving to Phase 2
(the agent loop, remaining sources, and Telegram interface).

Constraints throughout:
- British ingredient names and metric units everywhere, including seed data
- Follow the project structure in CLAUDE.md precisely
- All prompt strings go in agent/prompts.py, even if the module is mostly
  empty in Phase 1
- Ask before making architectural decisions not covered in CLAUDE.md
- Leave # TODO comments at decision points with your reasoning
