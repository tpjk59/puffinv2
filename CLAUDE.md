# Meal Planner Agent — Project Brief

## What this is
A personal agentic meal planning application for a British user. The agent tracks
food inventory from multiple pluggable sources, logs meals (batch-cooked and
frozen), suggests dinners and packed lunches, flags expiry risk, and tracks
nutrition against daily targets (calories, protein, fibre).

The agent's suggestions should feel culturally grounded — see the Cultural
Alignment section below.

## Architecture
- **Backend**: Python, FastAPI, SQLite via SQLAlchemy (async), APScheduler for cron
- **Agent loop**: Anthropic Python SDK, tool-use pattern (claude-sonnet-4-6)
- **Food sources**: Pluggable via the FoodSource protocol (see below)
- **Nutrition data**: USDA FoodData Central REST API (free, no auth required)
  — supplement with Open Food Facts for British-specific products
- **CLI**: Typer for admin commands (seed data, run agent manually, etc.)
- **Interface**: Telegram bot (primary); FastAPI-served web UI (secondary)
- **Frontend**: Minimal — plain HTML/JS served by FastAPI; no heavy frameworks

## Pluggable Food Source Architecture

All food sources implement the `FoodSource` protocol defined in
`sources/base.py`. A source is responsible for producing a list of
`IngredientArrival` objects. The agent loop and database are source-agnostic.

```python
# sources/base.py
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from datetime import date

@dataclass
class IngredientArrival:
    name: str                        # canonical ingredient name
    quantity: float
    unit: str                        # g, kg, ml, l, whole, bunch, etc.
    source_label: str                # e.g. "riverford_veg_box", "camera", "shop"
    arrived_date: date
    best_before: date | None = None
    location: str = "fridge"         # fridge | freezer | pantry
    notes: str | None = None         # e.g. "slightly soft" from camera source

@runtime_checkable
class FoodSource(Protocol):
    source_label: str                # unique identifier, used in DB records

    async def fetch(self, **kwargs) -> list[IngredientArrival]:
        """Fetch/parse arrivals from this source. kwargs are source-specific."""
        ...

    def describe(self) -> str:
        """Human-readable description of this source for the agent."""
        ...
```

### Source registry
`sources/registry.py` maintains a dict of registered sources. New sources are
added here and nowhere else — the agent loop, scheduler, and CLI all discover
sources through the registry.

### Built-in sources (implement these first)
- `sources/web_scraper.py` — base class for URL-based scrapers; subclassed by:
  - `sources/veg_box.py` — weekly veg box (configurable URL in .env)
  - `sources/meat_box.py` — bi-weekly meat box (configurable URL in .env)
- `sources/camera.py` — accepts a base64 image, calls Claude vision to identify
  and quantity ingredients; returns IngredientArrival list with confidence scores
  in the notes field
- `sources/manual.py` — simple structured text input ("500g chicken thighs,
  fridge"); used by the agent when the user describes purchases in chat

### Adding a new source (the contract)
1. Create `sources/your_source.py` implementing `FoodSource`
2. Register it in `sources/registry.py`
3. If it needs credentials, add them to `.env.example`
4. If it needs scheduling, add the schedule to `app/scheduler.py`
No other files need changing.

### Camera source detail
`sources/camera.py` uses the Anthropic API directly (vision capability) with a
structured prompt that asks for a JSON list of identified ingredients with
quantities and confidence. The tool `inventory_from_image` in `agent/tools.py`
wraps this source and is callable by the agent when the user sends a photo.
The agent should present low-confidence items back to the user for confirmation
before committing to the database.

## Database — core tables
- `ingredients` (id, name, quantity, unit, source_label, location
  [fridge|freezer|pantry], arrived_date, best_before, usda_fdc_id,
  open_food_facts_id, calories_per_100g, protein_per_100g, fibre_per_100g)
- `meals` (id, name, cuisine_tag, cooked_date, total_portions,
  portions_remaining, location [fridge|freezer], notes)
- `meal_ingredients` (meal_id, ingredient_id, quantity_used, unit)
- `nutrition_log` (id, date, source_meal_id, calories, protein_g, fibre_g, notes)
- `preferences` (key, value, updated_at) — key-value store, queried by agent
- `delivery_schedule` (id, source_label, expected_date, scraped_at, raw_json)

Note: `cuisine_tag` on meals is free text but the agent should use consistent
tags. Seed data should include tags like: british, south-asian, middle-eastern,
italian, east-asian, french, west-african, american, other.

## Agent tools
1. `get_inventory` — list current ingredients, filter by location/expiry/source
2. `update_inventory` — add, consume, or expire ingredients
3. `log_meal_cooked` — record a batch cook; deducts ingredients, creates meal
4. `log_meal_eaten` — mark portions consumed; updates nutrition_log
5. `get_meal_history` — recent meals, freezer contents, portions remaining
6. `get_nutrition_summary` — today's/this week's totals vs targets
7. `get_preferences` — retrieve user preferences (agent reads these before
   making suggestions; do not rely solely on the system prompt for preferences)
8. `set_preference` — update a preference key-value pair
9. `fetch_from_source` — trigger a named source from the registry to fetch
   arrivals; agent calls this when user says "my veg box arrived"
10. `inventory_from_image` — wraps the camera source; agent calls this when
    user sends a photo; returns candidate list for user confirmation
11. `list_sources` — return registered sources and their descriptions; useful
    when user asks "how do I add food?"

## Scheduled jobs (APScheduler)
- Monday–Friday 07:30 — packed lunch nudge: "here's what to take today"
- Sunday 16:00 — batch cook suggestion based on current inventory
- Daily 08:00 — expiry check; alert if anything expires within 3 days
- Source-specific schedules configured per source in the registry

## Cultural Alignment

### Why this matters
The user is British. Their culinary home is British home cooking. Suggestions
that default to American portion sizes, ingredients, or idioms create friction
even when the food itself is fine. The agent should feel like it was built for
a British kitchen, not localised from an American one.

### What this means concretely
- **Default flavour profile**: British home cooking — roasts, stews, pies,
  gratins, stir-fries (a staple of British weeknight cooking), curries (as
  integral to British food culture as fish and chips), pasta dishes, salads
- **Ingredient naming**: courgette not zucchini, aubergine not eggplant,
  coriander not cilantro, spring onion not scallion, rocket not arugula,
  mince not ground beef, prawns not shrimp, grill not broil, tin not can
- **Measurements**: metric first (grams, millilitres, °C); never Fahrenheit
  or cups as primary units
- **Portion framing**: British meal sizes, not American restaurant sizes
- **Cuisine openness**: The user enjoys experimenting. South Asian, East Asian,
  Middle Eastern, West African, and Mediterranean cuisines are all welcome —
  frame them naturally as things a British home cook would make, not as
  "exotic" departures
- **Avoid**: Framing any dish as "British-ified", unnecessary fusion labelling,
  American fast-food references, and any suggestion that British food is bland
  (it isn't)

### Preferences stored in DB (seed these)
The following preference keys must be seeded and the agent must read them via
`get_preferences` before making any meal suggestion:

| key | seed value | notes |
|-----|-----------|-------|
| `cultural_home` | `british` | Primary food culture |
| `cuisine_openness` | `high` | Willing to try most things |
| `weekday_max_cook_minutes` | `30` | Time constraint Mon–Fri |
| `weekend_max_cook_minutes` | `120` | Batch cooking window |
| `calorie_target` | `2200` | Daily kcal target |
| `protein_target_g` | `140` | Daily protein target |
| `fibre_target_g` | `30` | Daily fibre target |
| `dislikes` | `offal,blue_cheese` | Comma-separated |
| `batch_cook_portions_target` | `4` | Preferred batch size |
| `freezer_first` | `true` | Prefer using freezer stock |

## Agent system prompt (in agent/prompts.py)
The system prompt must open with this framing and be iterated upon:

> You are a practical, knowledgeable meal planning assistant for a British home
> cook. You have access to their live food inventory, meal history, nutritional
> targets, and personal preferences — always read preferences before making
> suggestions. Your suggestions should feel natural for a British kitchen:
> use British ingredient names and metric measurements throughout. The user
> enjoys cooking and is open to cuisines from around the world, but their
> culinary home is British — your default register should reflect that.
>
> You are pragmatic: you prioritise using what's already in the fridge and
> freezer, especially anything approaching its best-before date. You are not
> a food snob. A good weeknight dinner that takes 25 minutes is as valuable
> as an ambitious weekend dish.
>
> Always give the user options, not a single prescription — their mood varies.
> When suggesting meals, briefly explain why each makes sense given their
> current inventory and nutritional state. Keep responses concise; the user
> is often reading on their phone.

## Coding conventions
- Type hints everywhere; Pydantic models for all API request/response bodies
- Async throughout (asyncio, async SQLAlchemy, httpx)
- Pytest for tests; unit tests on all tool functions and source implementations
- Environment variables via python-dotenv (.env, never commit secrets)
- Keep all prompt strings in `agent/prompts.py` — no scattered f-strings
- One migration script per schema change in `db/migrations/`
- Readable over clever; this is a personal tool

## Project structure
meal-planner/
├── CLAUDE.md
├── .env.example
├── pyproject.toml
├── requirements.txt
├── agent/
│   ├── loop.py          # agentic loop: LLM + tool dispatch
│   ├── tools.py         # all tool implementations
│   └── prompts.py       # all system and task prompts
├── sources/
│   ├── base.py          # FoodSource protocol + IngredientArrival dataclass
│   ├── registry.py      # source registration and lookup
│   ├── web_scraper.py   # base web scraper class
│   ├── veg_box.py       # veg box source
│   ├── meat_box.py      # meat box source
│   ├── camera.py        # vision-based camera source
│   └── manual.py        # manual text input source
├── db/
│   ├── database.py      # engine, session factory
│   ├── models.py        # SQLAlchemy models
│   ├── crud.py          # async CRUD operations
│   └── migrations/      # schema migration scripts
├── app/
│   ├── main.py          # FastAPI app + routes
│   ├── scheduler.py     # APScheduler setup
│   └── telegram.py      # Telegram webhook handler
├── cli.py               # Typer CLI (seed, run-agent, etc.)
└── tests/
├── test_crud.py
├── test_tools.py
└── test_sources.py

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
