"""All 13 agent tool implementations and their Anthropic API definitions.

Each tool is an async function that takes an AsyncSession as its first argument.
TOOL_DEFINITIONS is the list passed directly to the Anthropic messages.create call.
dispatch_tool routes a tool_use block to the correct implementation.
"""

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

import sources.registry as registry
from db import crud
from nutrition.lookup import fetch_nutrition


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _ingredient_to_dict(ing) -> dict:
    return {
        "id": ing.id,
        "name": ing.name,
        "quantity": ing.quantity,
        "unit": ing.unit,
        "source_label": ing.source_label,
        "location": ing.location,
        "arrived_date": ing.arrived_date.isoformat(),
        "best_before": ing.best_before.isoformat() if ing.best_before else None,
        "calories_per_100g": ing.calories_per_100g,
        "protein_per_100g": ing.protein_per_100g,
        "fibre_per_100g": ing.fibre_per_100g,
        "notes": ing.notes,
    }


def _meal_to_dict(meal) -> dict:
    return {
        "id": meal.id,
        "name": meal.name,
        "cuisine_tag": meal.cuisine_tag,
        "cooked_date": meal.cooked_date.isoformat(),
        "total_portions": meal.total_portions,
        "portions_remaining": meal.portions_remaining,
        "location": meal.location,
        "notes": meal.notes,
    }


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def get_inventory(
    session: AsyncSession,
    location: Optional[str] = None,
    expiry_within_days: Optional[int] = None,
    source_label: Optional[str] = None,
) -> dict:
    expiry_before = None
    if expiry_within_days is not None:
        expiry_before = date.today() + timedelta(days=expiry_within_days)
    ingredients = await crud.list_ingredients(
        session, location=location, source_label=source_label, expiry_before=expiry_before
    )
    return {
        "ingredients": [_ingredient_to_dict(i) for i in ingredients],
        "count": len(ingredients),
    }


async def update_inventory(
    session: AsyncSession,
    action: str,
    ingredient_id: Optional[int] = None,
    name: Optional[str] = None,
    quantity: Optional[float] = None,
    unit: Optional[str] = None,
    location: Optional[str] = None,
    arrived_date: Optional[str] = None,
    best_before: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    if action == "add":
        ing = await crud.create_ingredient(
            session,
            name=name,
            quantity=quantity,
            unit=unit,
            source_label="manual",
            location=location or "fridge",
            arrived_date=date.fromisoformat(arrived_date) if arrived_date else date.today(),
            best_before=date.fromisoformat(best_before) if best_before else None,
            notes=notes,
        )
        return {"added": _ingredient_to_dict(ing)}

    if ingredient_id is None:
        return {"error": f"ingredient_id is required for action '{action}'"}

    ing = await crud.get_ingredient(session, ingredient_id)
    if ing is None:
        return {"error": f"Ingredient {ingredient_id} not found"}

    if action == "consume":
        qty_consumed = quantity if quantity is not None else ing.quantity
        new_qty = ing.quantity - qty_consumed
        if new_qty <= 0:
            await crud.delete_ingredient(session, ingredient_id)
            return {"status": "fully consumed", "ingredient_id": ingredient_id}
        updated = await crud.update_ingredient(session, ingredient_id, {"quantity": new_qty})
        return {"updated": _ingredient_to_dict(updated)}

    if action == "expire":
        await crud.delete_ingredient(session, ingredient_id)
        return {"status": "expired and removed", "ingredient_id": ingredient_id}

    return {"error": f"Unknown action '{action}'. Must be add, consume, or expire."}


async def log_meal_cooked(
    session: AsyncSession,
    name: str,
    cuisine_tag: str,
    total_portions: int,
    ingredient_uses: list[dict],
    location: str = "freezer",
    notes: Optional[str] = None,
) -> dict:
    meal = await crud.create_meal(
        session,
        name=name,
        cuisine_tag=cuisine_tag,
        cooked_date=date.today(),
        total_portions=total_portions,
        portions_remaining=total_portions,
        location=location,
        notes=notes,
    )
    for use in ingredient_uses:
        ing = await crud.get_ingredient(session, use["ingredient_id"])
        if ing is None:
            continue
        await crud.add_meal_ingredient(
            session, meal.id, use["ingredient_id"], use["quantity"], use["unit"]
        )
        new_qty = ing.quantity - use["quantity"]
        if new_qty <= 0:
            await crud.delete_ingredient(session, ing.id)
        else:
            await crud.update_ingredient(session, ing.id, {"quantity": new_qty})

    return {"meal": _meal_to_dict(meal)}


async def log_meal_eaten(
    session: AsyncSession,
    meal_id: int,
    calories: float,
    protein_g: float,
    fibre_g: float,
    portions: int = 1,
) -> dict:
    meal = await crud.get_meal(session, meal_id)
    if meal is None:
        return {"error": f"Meal {meal_id} not found"}
    if meal.portions_remaining < portions:
        return {
            "error": f"Only {meal.portions_remaining} portion(s) remaining, cannot eat {portions}"
        }
    new_portions = meal.portions_remaining - portions
    await crud.update_meal(session, meal_id, {"portions_remaining": new_portions})
    log = await crud.create_nutrition_log(
        session,
        log_date=date.today(),
        calories=calories,
        protein_g=protein_g,
        fibre_g=fibre_g,
        source_meal_id=meal_id,
    )
    return {"portions_remaining": new_portions, "nutrition_log_id": log.id}


async def get_meal_history(
    session: AsyncSession,
    location: Optional[str] = None,
    limit: int = 20,
) -> dict:
    meals = await crud.list_meals(session, location=location)
    return {"meals": [_meal_to_dict(m) for m in meals[:limit]]}


async def get_nutrition_summary(
    session: AsyncSession,
    period: str = "today",
) -> dict:
    today = date.today()
    start = today if period == "today" else today - timedelta(days=6)
    logs = await crud.list_nutrition_logs(session, start_date=start, end_date=today)
    prefs = await crud.get_all_preferences(session)

    totals = {
        "calories": sum(l.calories for l in logs),
        "protein_g": sum(l.protein_g for l in logs),
        "fibre_g": sum(l.fibre_g for l in logs),
    }
    def _pref_float(key: str, default: float) -> float:
        try:
            return float(prefs.get(key, default))
        except (ValueError, TypeError):
            return default

    targets = {
        "calories": _pref_float("calorie_target", 2200),
        "protein_g": _pref_float("protein_target_g", 140),
        "fibre_g": _pref_float("fibre_target_g", 30),
    }
    return {"period": period, "totals": totals, "targets": targets, "log_count": len(logs)}


async def get_preferences(session: AsyncSession) -> dict:
    return await crud.get_all_preferences(session)


async def set_preference(session: AsyncSession, key: str, value: str) -> dict:
    pref = await crud.set_preference(session, key, value)
    return {"key": pref.key, "value": pref.value}


async def fetch_from_source(
    session: AsyncSession,
    source_label: str,
    text: Optional[str] = None,
) -> dict:
    source = registry.get(source_label)
    kwargs: dict[str, Any] = {}
    if text is not None:
        kwargs["text"] = text
    arrivals = await source.fetch(**kwargs)
    created = []
    for arrival in arrivals:
        ing = await crud.create_ingredient(
            session,
            name=arrival.name,
            quantity=arrival.quantity,
            unit=arrival.unit,
            source_label=arrival.source_label,
            location=arrival.location,
            arrived_date=arrival.arrived_date,
            best_before=arrival.best_before,
            notes=arrival.notes,
        )
        created.append(_ingredient_to_dict(ing))

    # Record the scrape in delivery_schedule for history / audit
    await crud.create_delivery_schedule(
        session,
        source_label=source_label,
        expected_date=date.today(),
        scraped_at=datetime.now(UTC),
        raw_json=json.dumps(created),
    )

    return {"added": created, "count": len(created)}


async def inventory_from_image(
    session: AsyncSession,
    image_b64: str,
    media_type: str = "image/jpeg",
) -> dict:
    """Identify ingredients in a photo. Does NOT save to inventory.

    Present the returned candidates to the user, then call update_inventory
    (action='add') for each confirmed item.
    """
    source = registry.get("camera")
    arrivals = await source.fetch(image_b64=image_b64, media_type=media_type)
    return {
        "candidates": [
            {
                "name": a.name,
                "quantity": a.quantity,
                "unit": a.unit,
                "location": a.location,
                "best_before": a.best_before.isoformat() if a.best_before else None,
                "notes": a.notes,
            }
            for a in arrivals
        ],
        "count": len(arrivals),
        "message": (
            "Review these candidates. Call update_inventory(action='add') "
            "for each confirmed item. Flag low-confidence items to the user."
        ),
    }


async def list_sources(session: AsyncSession) -> dict:
    sources = registry.list_all()
    return {
        "sources": [
            {"label": label, "description": source.describe()}
            for label, source in sorted(sources.items())
        ]
    }


async def lookup_nutrition(
    session: AsyncSession,
    ingredient_name: str,
    ingredient_id: Optional[int] = None,
) -> dict:
    """Look up per-100g nutrition from USDA / Open Food Facts.

    If ingredient_id is given, persists the result to that ingredient record.
    """
    data = await fetch_nutrition(ingredient_name)
    if data is None:
        return {"error": f"No nutrition data found for '{ingredient_name}'"}
    if ingredient_id is not None:
        await crud.update_ingredient(
            session,
            ingredient_id,
            {
                "calories_per_100g": data["calories_per_100g"],
                "protein_per_100g": data["protein_per_100g"],
                "fibre_per_100g": data["fibre_per_100g"],
            },
        )
        data["saved_to_ingredient_id"] = ingredient_id
    return data


async def get_delivery_schedule(
    session: AsyncSession,
    source_label: Optional[str] = None,
    limit: int = 10,
) -> dict:
    """Return recent delivery scrape history."""
    schedules = await crud.list_delivery_schedules(session, source_label=source_label)
    return {
        "schedules": [
            {
                "id": s.id,
                "source_label": s.source_label,
                "expected_date": s.expected_date.isoformat(),
                "scraped_at": s.scraped_at.isoformat() if s.scraped_at else None,
                "item_count": len(json.loads(s.raw_json)) if s.raw_json else 0,
            }
            for s in schedules[:limit]
        ]
    }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_HANDLERS = {
    "get_inventory": get_inventory,
    "update_inventory": update_inventory,
    "log_meal_cooked": log_meal_cooked,
    "log_meal_eaten": log_meal_eaten,
    "get_meal_history": get_meal_history,
    "get_nutrition_summary": get_nutrition_summary,
    "get_preferences": get_preferences,
    "set_preference": set_preference,
    "fetch_from_source": fetch_from_source,
    "inventory_from_image": inventory_from_image,
    "list_sources": list_sources,
    "lookup_nutrition": lookup_nutrition,
    "get_delivery_schedule": get_delivery_schedule,
}


async def dispatch_tool(name: str, tool_input: dict, session: AsyncSession) -> dict:
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"error": f"Unknown tool: {name}"}
    return await handler(session=session, **tool_input)


# ---------------------------------------------------------------------------
# Tool definitions (Anthropic API schema)
# cache_control on the last entry caches the full list as a prompt prefix.
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "get_inventory",
        "description": (
            "List current ingredients in the inventory. "
            "Filter by location, source, or upcoming expiry."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "enum": ["fridge", "freezer", "pantry"],
                    "description": "Filter by storage location.",
                },
                "expiry_within_days": {
                    "type": "integer",
                    "description": "Only return items expiring within this many days.",
                },
                "source_label": {
                    "type": "string",
                    "description": "Filter by source, e.g. 'veg_box' or 'manual'.",
                },
            },
        },
    },
    {
        "name": "update_inventory",
        "description": (
            "Add, consume, or expire ingredients. "
            "action='add': add new stock. "
            "action='consume': reduce quantity after use; removes the item if fully consumed. "
            "action='expire': remove an expired item."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "consume", "expire"],
                },
                "ingredient_id": {
                    "type": "integer",
                    "description": "Required for consume and expire.",
                },
                "name": {"type": "string", "description": "Ingredient name — use British terms. Required for add."},
                "quantity": {"type": "number", "description": "Amount to add or consume."},
                "unit": {"type": "string", "description": "Unit of measure, e.g. g, kg, whole. Required for add."},
                "location": {
                    "type": "string",
                    "enum": ["fridge", "freezer", "pantry"],
                    "description": "Storage location. Required for add.",
                },
                "arrived_date": {
                    "type": "string",
                    "description": "ISO date YYYY-MM-DD. Defaults to today. (add only)",
                },
                "best_before": {
                    "type": "string",
                    "description": "ISO date YYYY-MM-DD. (add only, optional)",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional notes, e.g. quality observations. (add only)",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "log_meal_cooked",
        "description": (
            "Record a batch-cooked meal. Deducts used ingredients from inventory "
            "and creates a meal record with the given number of portions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "cuisine_tag": {
                    "type": "string",
                    "description": "E.g. british, south-asian, italian, east-asian, middle-eastern, west-african, french, american, other.",
                },
                "total_portions": {"type": "integer"},
                "ingredient_uses": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ingredient_id": {"type": "integer"},
                            "quantity": {"type": "number"},
                            "unit": {"type": "string"},
                        },
                        "required": ["ingredient_id", "quantity", "unit"],
                    },
                },
                "location": {
                    "type": "string",
                    "enum": ["fridge", "freezer"],
                    "description": "Where to store the cooked meal. Defaults to freezer.",
                },
                "notes": {"type": "string"},
            },
            "required": ["name", "cuisine_tag", "total_portions", "ingredient_uses"],
        },
    },
    {
        "name": "log_meal_eaten",
        "description": "Mark portions of a meal as eaten and log the nutrition.",
        "input_schema": {
            "type": "object",
            "properties": {
                "meal_id": {"type": "integer"},
                "portions": {"type": "integer", "description": "Defaults to 1."},
                "calories": {"type": "number"},
                "protein_g": {"type": "number"},
                "fibre_g": {"type": "number"},
            },
            "required": ["meal_id", "calories", "protein_g", "fibre_g"],
        },
    },
    {
        "name": "get_meal_history",
        "description": "Get meals with portions remaining. Use location='freezer' to see freezer stock.",
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "enum": ["fridge", "freezer"]},
                "limit": {"type": "integer", "description": "Defaults to 20."},
            },
        },
    },
    {
        "name": "get_nutrition_summary",
        "description": "Total calories, protein, and fibre for today or this week vs. targets.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": ["today", "week"],
                    "description": "Defaults to 'today'.",
                },
            },
        },
    },
    {
        "name": "get_preferences",
        "description": "Retrieve all user preferences. Always call this before making meal suggestions.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_preference",
        "description": (
            "Update a user preference. "
            "Numeric preferences (calorie_target, protein_target_g, fibre_target_g, "
            "weekday_max_cook_minutes, weekend_max_cook_minutes, batch_cook_portions_target) "
            "must be stored as a single number, not a range. "
            "If the user gives a range (e.g. '1400-1600 kcal'), use the midpoint (1500)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "fetch_from_source",
        "description": (
            "Trigger a registered food source to fetch arrivals and save them to inventory. "
            "Use when the user says their veg box or meat box has arrived. "
            "For the manual source, pass text= describing what arrived."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_label": {
                    "type": "string",
                    "description": "E.g. 'veg_box', 'meat_box', 'manual'.",
                },
                "text": {
                    "type": "string",
                    "description": "Natural language ingredient description — manual source only.",
                },
            },
            "required": ["source_label"],
        },
    },
    {
        "name": "inventory_from_image",
        "description": (
            "Identify ingredients in a photo. Returns candidates — does NOT save to inventory. "
            "Present candidates to the user, then call update_inventory for confirmed items."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "image_b64": {"type": "string", "description": "Base64-encoded image data."},
                "media_type": {
                    "type": "string",
                    "description": "MIME type, e.g. image/jpeg. Defaults to image/jpeg.",
                },
            },
            "required": ["image_b64"],
        },
    },
    {
        "name": "list_sources",
        "description": "List all registered food sources and their descriptions.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "lookup_nutrition",
        "description": (
            "Look up per-100g calories, protein, and fibre for an ingredient "
            "from USDA FoodData Central (falls back to Open Food Facts for British packaged products). "
            "Pass ingredient_id to save the result to that inventory record."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ingredient_name": {
                    "type": "string",
                    "description": "Name to search for, e.g. 'courgette', 'chicken thigh'.",
                },
                "ingredient_id": {
                    "type": "integer",
                    "description": "If provided, saves the looked-up data to this ingredient.",
                },
            },
            "required": ["ingredient_name"],
        },
    },
    {
        "name": "get_delivery_schedule",
        "description": "Return recent delivery scrape history — when each source was last scraped and how many items were found.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_label": {
                    "type": "string",
                    "description": "Filter to a specific source, e.g. 'veg_box'.",
                },
                "limit": {"type": "integer", "description": "Defaults to 10."},
            },
        },
        "cache_control": {"type": "ephemeral"},  # caches entire tool list as a prefix
    },
]
