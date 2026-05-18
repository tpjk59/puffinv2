"""All 13 agent tool implementations and their Anthropic API definitions.

Each tool is an async function that takes an AsyncSession as its first argument.
TOOL_DEFINITIONS is the list passed directly to the Anthropic messages.create call.
dispatch_tool routes a tool_use block to the correct implementation.
"""

import json
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any, Optional

import anthropic
import httpx
from sqlalchemy.ext.asyncio import AsyncSession

import sources.registry as registry
from agent.prompts import RECIPE_PARSE_PROMPT
from db import crud
from db import crud as db_crud

_anthropic_client = anthropic.AsyncAnthropic()
_RECIPE_PARSE_MODEL = "claude-haiku-4-5-20251001"


# Normalise inventory quantities to a base unit so kg/g, l/ml, pint/ml comparisons work.
_UNIT_TO_BASE: dict[str, tuple[str, float]] = {
    "kg": ("g", 1000.0),
    "l": ("ml", 1000.0),
    "pint": ("ml", 568.0),   # UK pint
    "pints": ("ml", 568.0),
    "sticks": ("stalk", 1.0),  # celery sticks = stalks
    "stick": ("stalk", 1.0),
    "portions": ("portion", 1.0),
    "serving": ("portion", 1.0),
    "servings": ("portion", 1.0),
}

# Ingredient-specific conversions where the units are otherwise incompatible.
_INGREDIENT_UNIT_TO_BASE: dict[tuple[str, str], tuple[str, float]] = {
    ("garlic", "bulb"):  ("cloves", 12),
    ("garlic", "bulbs"): ("cloves", 12),
}

# If an ingredient is stocked in one of these "container" units but a recipe
# measures it in a specific amount, treat it as available (container = "I have some").
_CONTAINER_UNITS = frozenset({
    "whole", "jar", "bottle", "bag", "box", "packet", "pot", "tin", "pack",
    "bundle", "bunch", "head",
})

# Informal recipe measures that can't be compared numerically to stock quantities.
# If ANY stock exists for the named ingredient, it's considered available.
_VAGUE_UNITS = frozenset({
    "drizzle", "splash", "glug", "knob", "thumb", "handful",
    "pinch", "dash", "sprig", "clove", "tbsp", "tsp", "teaspoon", "tablespoon",
})

# Ingredients in these subcategories are tracked by presence only — any stock means
# available, regardless of quantity. They appear on the shopping list only when fully
# out of stock (deleted from inventory).
_STAPLE_SUBCATEGORIES = frozenset({"herb_spice", "condiment"})


def _check_stock(
    stock: dict[tuple, float],
    name: str,
    needed_qty: float,
    needed_unit: str,
    staple_names: frozenset[str] = frozenset(),
    stock_names: frozenset[str] = frozenset(),
) -> tuple[bool, float]:
    """Return (in_stock, stock_qty).

    Staples (herb_spice / condiment subcategory): any stock = available,
    quantity is ignored — they only appear on the shopping list when fully gone.

    Vague/informal measures (tbsp, drizzle, thumb, etc.): if ANY stock exists
    for the ingredient, treat as available — we can't numerically compare them.

    Otherwise falls back to a container-unit check: if stocked as a whole container
    (jar, bag, etc.) but the recipe measures in a specific amount, treat as available.
    """
    if name in staple_names:
        return True, needed_qty
    if needed_unit in _VAGUE_UNITS and name in stock_names:
        return True, needed_qty
    have = stock.get((name, needed_unit), 0.0)
    if have >= needed_qty:
        return True, have
    for cu in _CONTAINER_UNITS:
        if stock.get((name, cu), 0) > 0:
            return True, needed_qty
    return False, have


def _normalise(quantity: float, unit: str, name: str = "") -> tuple[float, str]:
    """Return (quantity, base_unit) with unit normalisation.

    Checks ingredient-specific conversions first (e.g. garlic bulb→cloves),
    then generic metric ones (kg→g, l→ml, UK pint→ml).
    """
    ing_key = (name.lower(), unit.lower())
    if ing_key in _INGREDIENT_UNIT_TO_BASE:
        base_unit, factor = _INGREDIENT_UNIT_TO_BASE[ing_key]
        return quantity * factor, base_unit
    conv = _UNIT_TO_BASE.get(unit.lower())
    if conv is None:
        return quantity, unit.lower()
    base_unit, factor = conv
    return quantity * factor, base_unit


def _strip_html(html: str) -> str:
    html = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<[^>]+>', ' ', html)
    return re.sub(r'\s+', ' ', html).strip()[:8000]


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
        "subcategory": ing.subcategory,
        "calories_per_100g": ing.calories_per_100g,
        "protein_per_100g": ing.protein_per_100g,
        "fibre_per_100g": ing.fibre_per_100g,
        "notes": ing.notes,
    }


def _meal_plan_to_dict(plan) -> dict:
    return {
        "id": plan.id,
        "name": plan.name,
        "meal_type": plan.meal_type,
        "cuisine_tag": plan.cuisine_tag,
        "planned_date": plan.planned_date.isoformat(),
        "servings": plan.servings,
        "status": plan.status,
        "source_url": plan.source_url,
        "notes": plan.notes,
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
    subcategory: Optional[str] = None,
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
            location=location or "fresh",
            subcategory=subcategory,
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
    portions: int = 1,
) -> dict:
    """Decrement portions for a batch-cooked meal.

    After calling this, also log nutrition to MacroFactor using mf_log_food
    (search the database) or mf_log_manual_food (if you know the macros directly).
    """
    meal = await crud.get_meal(session, meal_id)
    if meal is None:
        return {"error": f"Meal {meal_id} not found"}
    if meal.portions_remaining < portions:
        return {
            "error": f"Only {meal.portions_remaining} portion(s) remaining, cannot eat {portions}"
        }
    new_portions = meal.portions_remaining - portions
    if new_portions <= 0:
        await crud.delete_meal(session, meal_id)
        return {"status": "fully eaten and removed", "meal_name": meal.name}
    await crud.update_meal(session, meal_id, {"portions_remaining": new_portions})
    return {"portions_remaining": new_portions, "meal_name": meal.name}


async def get_meal_history(
    session: AsyncSession,
    location: Optional[str] = None,
    limit: int = 20,
) -> dict:
    meals = await crud.list_meals(session, location=location)
    active = [m for m in meals if m.portions_remaining > 0]
    return {"meals": [_meal_to_dict(m) for m in active[:limit]]}


async def delete_meal(session: AsyncSession, meal_id: int) -> dict:
    deleted = await crud.delete_meal(session, meal_id)
    if not deleted:
        return {"error": f"Meal {meal_id} not found"}
    return {"status": "deleted", "meal_id": meal_id}


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
            subcategory=arrival.subcategory,
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
# Meal plan tools
# ---------------------------------------------------------------------------


async def plan_meal(
    session: AsyncSession,
    name: str,
    planned_date: str,
    meal_type: str = "dinner",
    ingredients: Optional[list[dict]] = None,
    servings: int = 2,
    cuisine_tag: Optional[str] = None,
    source_url: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    plan = await crud.create_meal_plan(
        session,
        name=name,
        meal_type=meal_type,
        planned_date=date.fromisoformat(planned_date),
        servings=servings,
        cuisine_tag=cuisine_tag,
        source_url=source_url,
        notes=notes,
    )
    for item in (ingredients or []):
        await crud.add_meal_plan_ingredient(
            session, plan.id,
            item["name"], float(item["quantity"]), item["unit"],
            item.get("notes"),
        )
    ing_list = await crud.list_meal_plan_ingredients(session, plan.id)
    result = _meal_plan_to_dict(plan)
    result["ingredients"] = [
        {"id": i.id, "name": i.name, "quantity": i.quantity, "unit": i.unit, "notes": i.notes}
        for i in ing_list
    ]
    return result


async def get_meal_plan(
    session: AsyncSession,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    status: Optional[str] = None,
    meal_type: Optional[str] = None,
) -> dict:
    plans = await crud.list_meal_plans(
        session,
        from_date=date.fromisoformat(from_date) if from_date else None,
        to_date=date.fromisoformat(to_date) if to_date else None,
        status=status,
        meal_type=meal_type,
    )
    all_inventory = await crud.list_ingredients(session)
    stock: dict[tuple, float] = {}
    staple_names: frozenset[str] = frozenset(
        ing.name.lower() for ing in all_inventory if ing.subcategory in _STAPLE_SUBCATEGORIES
    )
    for ing in all_inventory:
        qty, unit = _normalise(ing.quantity, ing.unit, ing.name)
        key = (ing.name.lower(), unit)
        stock[key] = stock.get(key, 0) + qty

    # Add frozen/fridge meal portions so "Carrot Pasta Sauce 2 portions" can match
    all_meals = await crud.list_meals(session)
    for meal in all_meals:
        key = (meal.name.lower(), "portion")
        stock[key] = meal.portions_remaining

    stock_names: frozenset[str] = frozenset(k[0] for k in stock.keys())

    result = []
    # agg_by_date: date_str -> {(name_lower, unit): {name, needed, unit}}
    agg_by_date: dict[str, dict[tuple, dict]] = {}

    for plan in plans:
        ings = await crud.list_meal_plan_ingredients(session, plan.id)
        date_str = plan.planned_date.isoformat()
        if date_str not in agg_by_date:
            agg_by_date[date_str] = {}

        ing_list = []
        for pi in ings:
            needed_qty, needed_unit = _normalise(pi.quantity, pi.unit, pi.name)
            resolved = _fuzzy_resolve_name(pi.name.lower(), stock_names)
            in_stock, have = _check_stock(stock, resolved, needed_qty, needed_unit, staple_names, stock_names)
            ing_list.append({
                "id": pi.id,
                "name": pi.name,
                "quantity": pi.quantity,
                "unit": pi.unit,
                "notes": pi.notes,
                "in_stock": in_stock,
                "stock_qty": round(have, 2),
            })
            # Only uncooked plans contribute to the shopping aggregate
            if plan.status != "cooked":
                key = (pi.name.lower(), needed_unit)
                if key not in agg_by_date[date_str]:
                    agg_by_date[date_str][key] = {"name": pi.name, "needed": 0.0, "unit": needed_unit}
                agg_by_date[date_str][key]["needed"] += needed_qty

        entry = _meal_plan_to_dict(plan)
        entry["ingredients"] = ing_list
        entry["all_in_stock"] = all(i["in_stock"] for i in ing_list) if ing_list else True
        result.append(entry)

    # Resolve aggregate stock checks (total needed vs total available)
    aggregate_by_date: dict[str, list] = {}
    for date_str, agg in agg_by_date.items():
        agg_list = []
        for (name_lower, unit), item in agg.items():
            resolved = _fuzzy_resolve_name(name_lower, stock_names)
            in_stock, have = _check_stock(stock, resolved, item["needed"], unit, staple_names, stock_names)
            shortfall = round(max(0.0, item["needed"] - have), 2) if not in_stock else 0.0
            agg_list.append({
                "name": item["name"],
                "needed": round(item["needed"], 2),
                "unit": unit,
                "in_stock": in_stock,
                "have": round(have, 2),
                "shortfall": shortfall,
            })
        agg_list.sort(key=lambda x: (x["in_stock"], x["name"]))
        aggregate_by_date[date_str] = agg_list

    return {"plans": result, "aggregate_by_date": aggregate_by_date}


async def update_meal_plan(
    session: AsyncSession,
    plan_id: int,
    name: Optional[str] = None,
    meal_type: Optional[str] = None,
    planned_date: Optional[str] = None,
    servings: Optional[int] = None,
    cuisine_tag: Optional[str] = None,
    status: Optional[str] = None,
    source_url: Optional[str] = None,
    notes: Optional[str] = None,
    ingredients: Optional[list[dict]] = None,
) -> dict:
    updates: dict[str, Any] = {}
    if name is not None:
        updates["name"] = name
    if meal_type is not None:
        updates["meal_type"] = meal_type
    if planned_date is not None:
        updates["planned_date"] = date.fromisoformat(planned_date)
    if servings is not None:
        updates["servings"] = servings
    if cuisine_tag is not None:
        updates["cuisine_tag"] = cuisine_tag
    if status is not None:
        updates["status"] = status
    if source_url is not None:
        updates["source_url"] = source_url
    if notes is not None:
        updates["notes"] = notes

    plan = await crud.update_meal_plan(session, plan_id, updates)
    if plan is None:
        return {"error": f"Meal plan {plan_id} not found"}

    if ingredients is not None:
        await crud.replace_meal_plan_ingredients(
            session, plan_id,
            [{"name": i["name"], "quantity": float(i["quantity"]), "unit": i["unit"],
              "notes": i.get("notes")} for i in ingredients],
        )

    return _meal_plan_to_dict(plan)


async def remove_from_meal_plan(session: AsyncSession, plan_id: int) -> dict:
    deleted = await crud.delete_meal_plan(session, plan_id)
    if not deleted:
        return {"error": f"Meal plan {plan_id} not found"}
    return {"status": "removed", "plan_id": plan_id}


async def get_week_plan(
    session: AsyncSession,
    week_start: Optional[str] = None,
) -> dict:
    """Return a Mon–Sun weekly grid with lunch/brunch and dinner slots per day."""
    today = date.today()
    if week_start:
        ws = date.fromisoformat(week_start)
    elif today.weekday() >= 5:  # Sat or Sun → upcoming Monday
        ws = today + timedelta(days=(7 - today.weekday()) % 7)
    else:  # weekday → current Monday
        ws = today - timedelta(days=today.weekday())
    we = ws + timedelta(days=6)

    all_plans = await crud.list_meal_plans(session, from_date=ws, to_date=we)
    plans = [p for p in all_plans if p.meal_type != "batch_cook"]

    all_inventory = await crud.list_ingredients(session)
    stock: dict[tuple, float] = {}
    staple_names: frozenset[str] = frozenset(
        ing.name.lower() for ing in all_inventory if ing.subcategory in _STAPLE_SUBCATEGORIES
    )
    for ing in all_inventory:
        qty, unit = _normalise(ing.quantity, ing.unit, ing.name)
        key = (ing.name.lower(), unit)
        stock[key] = stock.get(key, 0) + qty
    for meal in await crud.list_meals(session):
        stock[(meal.name.lower(), "portion")] = meal.portions_remaining
    stock_names_wk: frozenset[str] = frozenset(k[0] for k in stock.keys())

    days = []
    for i in range(7):
        day = ws + timedelta(days=i)
        day_plans = [p for p in plans if p.planned_date == day]
        slots: dict[str, dict] = {}
        for plan in day_plans:
            mt = plan.meal_type or "unplanned"
            if mt not in ("lunch", "dinner", "brunch"):
                continue
            ings = await crud.list_meal_plan_ingredients(session, plan.id)
            ing_list = []
            for pi in ings:
                nq, nu = _normalise(pi.quantity, pi.unit, pi.name)
                resolved_wk = _fuzzy_resolve_name(pi.name.lower(), stock_names_wk)
                ok, have = _check_stock(stock, resolved_wk, nq, nu, staple_names, stock_names_wk)
                ing_list.append({
                    "id": pi.id, "name": pi.name,
                    "quantity": pi.quantity, "unit": pi.unit,
                    "in_stock": ok,
                })
            entry = _meal_plan_to_dict(plan)
            entry["ingredients"] = ing_list
            entry["all_in_stock"] = all(x["in_stock"] for x in ing_list) if ing_list else True
            slots[mt] = entry
        days.append({
            "date": day.isoformat(),
            "weekday": day.strftime("%A"),
            "is_weekend": day.weekday() >= 5,
            "slots": slots,
        })

    return {"week_start": ws.isoformat(), "week_end": we.isoformat(), "days": days}


async def get_shopping_list(
    session: AsyncSession,
    week_start: Optional[str] = None,
) -> dict:
    today = date.today()
    if week_start:
        ws = date.fromisoformat(week_start)
        we = ws + timedelta(days=6)
        # Meal plan: the specified week; cook sessions: next 13 days from ws
        meal_plans = await crud.list_meal_plans(
            session, from_date=ws, to_date=we, status="planned", meal_type=None
        )
        cook_plans = await crud.list_meal_plans(
            session, from_date=ws, to_date=ws + timedelta(days=13),
            status="planned", meal_type="batch_cook",
        )
        # Deduplicate (cook sessions within the week are already in meal_plans)
        seen_ids = {p.id for p in meal_plans}
        plans = meal_plans + [p for p in cook_plans if p.id not in seen_ids]
    else:
        # Default: all meal types from today; batch cook looks further ahead (13 days)
        meal_plans = await crud.list_meal_plans(
            session, from_date=today, to_date=today + timedelta(days=6), status="planned"
        )
        cook_plans = await crud.list_meal_plans(
            session, from_date=today, to_date=today + timedelta(days=13),
            status="planned", meal_type="batch_cook",
        )
        seen_ids = {p.id for p in meal_plans}
        plans = meal_plans + [p for p in cook_plans if p.id not in seen_ids]

    # Aggregate needed quantities in normalised units so kg/g etc. are comparable
    needed: dict[tuple, dict] = {}
    for plan in plans:
        ings = await crud.list_meal_plan_ingredients(session, plan.id)
        for pi in ings:
            norm_qty, norm_unit = _normalise(pi.quantity, pi.unit, pi.name)
            key = (pi.name.lower(), norm_unit)
            if key not in needed:
                needed[key] = {
                    "name": pi.name, "unit": norm_unit,
                    "quantity_needed": 0.0, "from_plans": [],
                }
            needed[key]["quantity_needed"] += norm_qty
            if plan.name not in needed[key]["from_plans"]:
                needed[key]["from_plans"].append(plan.name)

    if not needed:
        return {"shopping_list": [], "message": "No upcoming planned meals."}

    all_inventory = await crud.list_ingredients(session)
    stock: dict[tuple, float] = {}
    staple_names: frozenset[str] = frozenset(
        ing.name.lower() for ing in all_inventory if ing.subcategory in _STAPLE_SUBCATEGORIES
    )
    for ing in all_inventory:
        qty, unit = _normalise(ing.quantity, ing.unit, ing.name)
        k = (ing.name.lower(), unit)
        stock[k] = stock.get(k, 0) + qty
    for meal in await crud.list_meals(session):
        stock[(meal.name.lower(), "portion")] = meal.portions_remaining
    stock_names_sl: frozenset[str] = frozenset(k[0] for k in stock.keys())

    shopping_list = []
    for key, item in needed.items():
        name, unit = key
        resolved_sl = _fuzzy_resolve_name(name, stock_names_sl)
        in_stock, have = _check_stock(stock, resolved_sl, item["quantity_needed"], unit, staple_names, stock_names_sl)
        shortfall = 0.0 if in_stock else item["quantity_needed"] - have
        if shortfall > 0:
            shopping_list.append({
                "name": item["name"],
                "quantity_needed": item["quantity_needed"],
                "quantity_in_stock": round(have, 2),
                "quantity_to_buy": round(shortfall, 2),
                "unit": item["unit"],
                "from_plans": item["from_plans"],
            })

    return {"shopping_list": shopping_list, "count": len(shopping_list)}


async def create_basket(
    session: AsyncSession,
    name: str,
) -> dict:
    b = await db_crud.create_basket(session, name)
    return {"basket": {"id": b.id, "name": b.name}}


async def list_baskets(session: AsyncSession) -> dict:
    bs = await db_crud.list_baskets(session)
    return {"baskets": [{"id": b.id, "name": b.name} for b in bs]}


async def get_basket_items(session: AsyncSession, basket_id: int) -> dict:
    items = await db_crud.list_basket_items(session, basket_id)
    return {"items": [
        {"id": i.id, "name": i.name, "quantity": i.quantity, "unit": i.unit, "notes": i.notes}
        for i in items
    ]}


async def add_basket_item(
    session: AsyncSession,
    basket_id: int,
    name: str,
    quantity: float,
    unit: str = "unit",
    notes: Optional[str] = None,
) -> dict:
    item = await db_crud.add_basket_item(session, basket_id, name, quantity, unit, notes)
    return {"item": {"id": item.id, "name": item.name, "quantity": item.quantity, "unit": item.unit}}


async def update_basket_item_tool(
    session: AsyncSession,
    item_id: int,
    quantity: Optional[float] = None,
    unit: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    updates: dict[str, Any] = {}
    if quantity is not None:
        updates["quantity"] = quantity
    if unit is not None:
        updates["unit"] = unit
    if notes is not None:
        updates["notes"] = notes
    item = await db_crud.update_basket_item(session, item_id, updates)
    if item is None:
        return {"error": "item not found"}
    return {"item": {"id": item.id, "name": item.name, "quantity": item.quantity, "unit": item.unit}}


async def remove_basket_item_tool(session: AsyncSession, item_id: int) -> dict:
    ok = await db_crud.remove_basket_item(session, item_id)
    return {"removed": item_id} if ok else {"error": "item not found"}


async def basket_required_ingredients(session: AsyncSession, basket_id: int) -> dict:
    """Return which ingredients from the basket are needed against current meal plans/shopping list.

    This computes the shopping list for planned meals and then intersects with basket items by name.
    """
    shop = await get_shopping_list(session)
    basket_items = await db_crud.list_basket_items(session, basket_id)
    needed = {i["name"].lower(): i for i in shop.get("shopping_list", [])}
    result = []
    for bi in basket_items:
        name = bi.name.lower()
        if name in needed:
            entry = needed[name].copy()
            entry["basket_quantity"] = bi.quantity
            entry["basket_unit"] = bi.unit
            result.append(entry)
    return {"matches": result}


async def parse_recipe_from_url(session: AsyncSession, url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; PuffinBot/1.0)"})
            r.raise_for_status()
        page_text = _strip_html(r.text)
    except Exception as exc:
        return {"error": f"Could not fetch URL: {exc}"}

    today = date.today().isoformat()
    prompt = RECIPE_PARSE_PROMPT.format(today=today, content=page_text)

    msg = await _anthropic_client.messages.create(
        model=_RECIPE_PARSE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

    try:
        recipe = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "Could not parse recipe from page", "preview": raw[:200]}

    return {"recipe": recipe, "source_url": url}


def _recipe_to_dict(r) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "source_url": r.source_url,
        "cuisine_tag": r.cuisine_tag,
        "tags": r.tags,
        "notes": r.notes,
        "times_planned": r.times_planned,
        "last_planned": r.last_planned.isoformat() if r.last_planned else None,
        "created_at": r.created_at.isoformat(),
    }


async def save_recipe(
    session: AsyncSession,
    name: str,
    source_url: Optional[str] = None,
    cuisine_tag: Optional[str] = None,
    tags: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """Save a recipe to the bank. Deduplicates by URL if provided."""
    if source_url:
        existing = await crud.get_recipe_by_url(session, source_url)
        if existing is not None:
            # Update metadata if provided, return existing
            updates: dict[str, Any] = {}
            if name:
                updates["name"] = name
            if cuisine_tag is not None:
                updates["cuisine_tag"] = cuisine_tag
            if tags is not None:
                updates["tags"] = tags
            if notes is not None:
                updates["notes"] = notes
            if updates:
                existing = await crud.update_recipe(session, existing.id, updates)
            return {"recipe": _recipe_to_dict(existing), "status": "updated_existing"}

    recipe = await crud.create_recipe(
        session,
        name=name,
        created_at=date.today(),
        source_url=source_url,
        cuisine_tag=cuisine_tag,
        tags=tags,
        notes=notes,
    )
    return {"recipe": _recipe_to_dict(recipe), "status": "saved"}


async def get_recipes(
    session: AsyncSession,
    search: Optional[str] = None,
    cuisine_tag: Optional[str] = None,
    tag: Optional[str] = None,
    limit: int = 20,
) -> dict:
    recipes = await crud.list_recipes(session, cuisine_tag=cuisine_tag, tag=tag, search=search)
    return {
        "recipes": [_recipe_to_dict(r) for r in recipes[:limit]],
        "count": len(recipes),
    }


async def delete_recipe_from_bank(session: AsyncSession, recipe_id: int) -> dict:
    deleted = await crud.delete_recipe(session, recipe_id)
    if not deleted:
        return {"error": f"Recipe {recipe_id} not found"}
    return {"status": "deleted", "recipe_id": recipe_id}


def _recurring_delivery_to_dict(rd) -> dict:
    return {
        "id": rd.id,
        "label": rd.label,
        "description": rd.description,
        "items": json.loads(rd.items_json),
        "days": rd.days,
        "send_time": rd.send_time,
        "active": rd.active,
        "paused_until": rd.paused_until.isoformat() if rd.paused_until else None,
    }


async def list_recurring_deliveries(session: AsyncSession) -> dict:
    deliveries = await crud.list_recurring_deliveries(session)
    return {
        "deliveries": [_recurring_delivery_to_dict(d) for d in deliveries],
        "count": len(deliveries),
    }


async def add_recurring_delivery(
    session: AsyncSession,
    label: str,
    description: str,
    items: list[dict],
    days: str,
    send_time: str = "07:00",
) -> dict:
    """Create a recurring delivery. items is a list of {name, quantity, unit, location, subcategory}."""
    existing = await crud.get_recurring_delivery(session, label)
    if existing is not None:
        return {"error": f"Recurring delivery '{label}' already exists. Use update_recurring_delivery to modify it."}
    rd = await crud.create_recurring_delivery(
        session,
        label=label,
        description=description,
        items_json=json.dumps(items),
        days=days,
        send_time=send_time,
    )
    return {"delivery": _recurring_delivery_to_dict(rd), "status": "created"}


async def update_recurring_delivery(
    session: AsyncSession,
    label: str,
    description: Optional[str] = None,
    items: Optional[list[dict]] = None,
    days: Optional[str] = None,
    send_time: Optional[str] = None,
    active: Optional[bool] = None,
    paused_until: Optional[str] = None,
) -> dict:
    updates: dict[str, Any] = {}
    if description is not None:
        updates["description"] = description
    if items is not None:
        updates["items_json"] = json.dumps(items)
    if days is not None:
        updates["days"] = days
    if send_time is not None:
        updates["send_time"] = send_time
    if active is not None:
        updates["active"] = active
    if paused_until is not None:
        updates["paused_until"] = date.fromisoformat(paused_until) if paused_until else None
    rd = await crud.update_recurring_delivery(session, label, updates)
    if rd is None:
        return {"error": f"Recurring delivery '{label}' not found"}
    return {"delivery": _recurring_delivery_to_dict(rd), "status": "updated"}


async def confirm_recurring_delivery(session: AsyncSession, label: str) -> dict:
    """Add the delivery's items to inventory. Call when the user confirms a delivery arrived."""
    rd = await crud.get_recurring_delivery(session, label)
    if rd is None:
        return {"error": f"Recurring delivery '{label}' not found"}
    items = json.loads(rd.items_json)
    today = date.today()
    added = []
    for item in items:
        ing = await crud.create_ingredient(
            session,
            name=item["name"],
            quantity=float(item["quantity"]),
            unit=item["unit"],
            source_label=f"recurring_{label}",
            location=item.get("location", "fresh"),
            subcategory=item.get("subcategory"),
            arrived_date=today,
        )
        added.append({"name": ing.name, "quantity": ing.quantity, "unit": ing.unit})
    return {"added": added, "count": len(added), "label": label}


async def mark_recipe_planned(
    session: AsyncSession,
    recipe_id: int,
) -> dict:
    """Increment times_planned and set last_planned to today. Call when planning a meal from the bank."""
    updates = {"times_planned": None, "last_planned": date.today()}
    recipe = await crud.get_recipe(session, recipe_id)
    if recipe is None:
        return {"error": f"Recipe {recipe_id} not found"}
    updates["times_planned"] = recipe.times_planned + 1
    updated = await crud.update_recipe(session, recipe_id, updates)
    return {"recipe": _recipe_to_dict(updated)}


def _match_inventory(
    recipe_name: str,
    inventory: list,
) -> list:
    """Return inventory items whose name plausibly matches a recipe ingredient name.

    Tries exact lowercase match, then substring containment in either direction.
    Returns all candidates — the agent/user picks the right one if there are multiple.
    """
    needle = recipe_name.strip().lower()
    candidates = []
    for ing in inventory:
        hay = ing.name.strip().lower()
        if hay == needle or needle in hay or hay in needle:
            candidates.append(ing)
    return candidates


def _fuzzy_resolve_name(name: str, stock_names: frozenset[str]) -> str:
    """Map a recipe ingredient name to the best matching name in the stock dict.

    Tries exact match first, then substring containment in either direction
    (e.g. "lemon" → "lemons", "fresh chives" → "chives", "tofu" → "silken tofu").
    Returns the original name unchanged if no match is found.
    """
    if name in stock_names:
        return name
    for sn in stock_names:
        if name in sn or sn in name:
            return sn
    return name


async def preview_cook(session: AsyncSession, plan_id: int) -> dict:
    """Show what a cook session would change before committing anything.

    Returns matched inventory deductions and unmatched recipe ingredients so the
    agent can present a clear summary and ask the user where outputs should go
    (fridge / freezer / back into ingredients).
    """
    plan = await crud.get_meal_plan_entry(session, plan_id)
    if plan is None:
        return {"error": f"Meal plan entry {plan_id} not found"}

    plan_ings = await crud.list_meal_plan_ingredients(session, plan_id)
    all_inventory = await crud.list_ingredients(session)

    deductions = []
    unmatched = []

    for pi in plan_ings:
        candidates = _match_inventory(pi.name, all_inventory)
        if not candidates:
            unmatched.append({"name": pi.name, "quantity": pi.quantity, "unit": pi.unit, "notes": pi.notes})
            continue

        # Normalise recipe qty to the same base unit for the "will_remain" calc.
        norm_need, norm_unit = _normalise(pi.quantity, pi.unit, pi.name)

        matches = []
        for ing in candidates:
            norm_have, _ = _normalise(ing.quantity, ing.unit, ing.name)
            after = norm_have - norm_need if norm_unit == _ else None
            matches.append({
                "ingredient_id": ing.id,
                "name": ing.name,
                "current_quantity": ing.quantity,
                "current_unit": ing.unit,
                "location": ing.location,
                "will_remain": round(after, 3) if after is not None else None,
                "will_remain_unit": norm_unit if after is not None else None,
                "fully_consumed": (after is not None and after <= 0),
            })

        deductions.append({
            "recipe_ingredient": pi.name,
            "recipe_quantity": pi.quantity,
            "recipe_unit": pi.unit,
            "notes": pi.notes,
            "inventory_matches": matches,
            # Convenience: pre-select the best (exact or single) match for the agent.
            "suggested_ingredient_id": matches[0]["ingredient_id"] if len(matches) == 1 else None,
        })

    return {
        "plan": _meal_plan_to_dict(plan),
        "deductions": deductions,
        "unmatched": unmatched,
        "instructions": (
            "Present this to the user. Ask: (1) Is each deduction correct? "
            "(2) Where does the cooked output go — fridge, freezer, or split? "
            "(3) How many portions? "
            "(4) Is any output going back as a standalone ingredient "
            "(e.g. a condiment, batch stock) rather than a portioned meal? "
            "Then call confirm_cook with their answers."
        ),
    }


async def confirm_cook(
    session: AsyncSession,
    plan_id: int,
    deductions: list[dict],
    outputs: list[dict],
) -> dict:
    """Commit a completed cook session.

    deductions: [{ingredient_id, quantity, unit}] — ingredients to subtract.
    outputs: list where each item is one of:
      {type: "meal",       name, cuisine_tag, portions, location, notes?}
      {type: "ingredient", name, quantity, unit, location, subcategory?, notes?}

    Marks the meal plan entry as status="cooked".
    """
    plan = await crud.get_meal_plan_entry(session, plan_id)
    if plan is None:
        return {"error": f"Meal plan entry {plan_id} not found"}

    # Apply ingredient deductions.
    deducted = []
    errors = []
    for d in deductions:
        ing = await crud.get_ingredient(session, d["ingredient_id"])
        if ing is None:
            errors.append(f"Ingredient {d['ingredient_id']} not found — skipped")
            continue
        norm_consume, norm_unit = _normalise(float(d["quantity"]), d["unit"], ing.name)
        norm_have, norm_have_unit = _normalise(ing.quantity, ing.unit, ing.name)
        if norm_unit == norm_have_unit:
            new_norm = norm_have - norm_consume
            # Convert back to the ingredient's original unit.
            inv_factor = norm_have / ing.quantity if ing.quantity else 1.0
            new_qty = new_norm / inv_factor if inv_factor else new_norm
        else:
            # Units don't reduce — just subtract raw quantity as given.
            new_qty = ing.quantity - float(d["quantity"])
        if new_qty <= 0:
            await crud.delete_ingredient(session, ing.id)
            deducted.append({"ingredient_id": ing.id, "name": ing.name, "status": "fully consumed"})
        else:
            await crud.update_ingredient(session, ing.id, {"quantity": round(new_qty, 3)})
            deducted.append({"ingredient_id": ing.id, "name": ing.name, "remaining": round(new_qty, 3), "unit": ing.unit})

    # Create outputs.
    created_meals = []
    created_ingredients = []
    for out in outputs:
        if out["type"] == "meal":
            meal = await crud.create_meal(
                session,
                name=out["name"],
                cuisine_tag=out.get("cuisine_tag", plan.cuisine_tag or "other"),
                cooked_date=date.today(),
                total_portions=int(out["portions"]),
                portions_remaining=int(out["portions"]),
                location=out["location"],
                notes=out.get("notes"),
            )
            created_meals.append(_meal_to_dict(meal))
        elif out["type"] == "ingredient":
            ing = await crud.create_ingredient(
                session,
                name=out["name"],
                quantity=float(out["quantity"]),
                unit=out["unit"],
                source_label="cooked",
                location=out["location"],
                subcategory=out.get("subcategory"),
                arrived_date=date.today(),
                notes=out.get("notes"),
            )
            created_ingredients.append(_ingredient_to_dict(ing))

    # Mark the plan entry as cooked.
    await crud.update_meal_plan(session, plan_id, {"status": "cooked"})

    result: dict = {"plan_id": plan_id, "status": "cooked", "deducted": deducted}
    if errors:
        result["warnings"] = errors
    if created_meals:
        result["meals_created"] = created_meals
    if created_ingredients:
        result["ingredients_created"] = created_ingredients
    return result


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_HANDLERS = {
    "get_inventory": get_inventory,
    "update_inventory": update_inventory,
    "log_meal_cooked": log_meal_cooked,
    "log_meal_eaten": log_meal_eaten,
    "delete_meal": delete_meal,
    "get_meal_history": get_meal_history,
    "get_preferences": get_preferences,
    "set_preference": set_preference,
    "fetch_from_source": fetch_from_source,
    "inventory_from_image": inventory_from_image,
    "list_sources": list_sources,
    "get_delivery_schedule": get_delivery_schedule,
    "plan_meal": plan_meal,
    "get_meal_plan": get_meal_plan,
    "get_week_plan": get_week_plan,
    "update_meal_plan": update_meal_plan,
    "remove_from_meal_plan": remove_from_meal_plan,
    "get_shopping_list": get_shopping_list,
    "parse_recipe_from_url": parse_recipe_from_url,
    "save_recipe": save_recipe,
    "get_recipes": get_recipes,
    "delete_recipe_from_bank": delete_recipe_from_bank,
    "mark_recipe_planned": mark_recipe_planned,
    "preview_cook": preview_cook,
    "confirm_cook": confirm_cook,
    "list_recurring_deliveries": list_recurring_deliveries,
    "add_recurring_delivery": add_recurring_delivery,
    "update_recurring_delivery": update_recurring_delivery,
    "confirm_recurring_delivery": confirm_recurring_delivery,
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
                    "enum": ["fresh", "freezer", "pantry"],
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
            "Add, consume, or expire a single ingredient. "
            "action='add': add one item — use this for individual additions only. "
            "For adding multiple ingredients at once, use fetch_from_source instead. "
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
                    "enum": ["fresh", "freezer", "pantry"],
                    "description": "Storage location. Use 'fresh' for anything fridge-kept or short-lived. Required for add.",
                },
                "subcategory": {
                    "type": "string",
                    "enum": ["meat", "fish", "dairy", "eggs", "fruit", "veg", "grain", "legume", "bakery", "condiment", "herb_spice", "other"],
                    "description": "Ingredient subcategory. Infer from the ingredient name if not specified.",
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
            "Record a batch-cooked meal directly, without a preview step. "
            "Use this only when you already know all ingredient IDs and quantities "
            "(e.g. programmatic logging). "
            "For the interactive cook-completion flow — where the user says 'I've made X' "
            "— use preview_cook first, then confirm_cook."
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
                    "enum": ["fresh", "freezer"],
                    "description": "Where to store the cooked meal. Defaults to freezer.",
                },
                "notes": {"type": "string"},
            },
            "required": ["name", "cuisine_tag", "total_portions", "ingredient_uses"],
        },
    },
    {
        "name": "log_meal_eaten",
        "description": (
            "Mark portions of a batch-cooked meal as eaten (decrements Puffin's portion counter). "
            "After calling this, log nutrition to MacroFactor using mf_log_food or mf_log_manual_food."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "meal_id": {"type": "integer"},
                "portions": {"type": "integer", "description": "Number of portions eaten. Defaults to 1."},
            },
            "required": ["meal_id"],
        },
    },
    {
        "name": "delete_meal",
        "description": "Permanently delete a meal record. Use when the user wants to remove a meal (e.g. thrown away, added by mistake).",
        "input_schema": {
            "type": "object",
            "properties": {
                "meal_id": {"type": "integer"},
            },
            "required": ["meal_id"],
        },
    },
    {
        "name": "get_meal_history",
        "description": "Get meals with portions remaining. Use location='freezer' to see freezer stock.",
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "enum": ["fresh", "freezer"]},
                "limit": {"type": "integer", "description": "Defaults to 20."},
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
            "Numeric preferences (weekday_max_cook_minutes, weekend_max_cook_minutes, "
            "batch_cook_portions_target) must be stored as a single number, not a range."
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
            "Also use with source_label='manual' whenever the user provides a list of "
            "ingredients to add — pass the entire list as text= in one call rather than "
            "calling update_inventory repeatedly."
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
            "Show the candidate list to the user. Once confirmed, save everything in one call "
            "using fetch_from_source(source_label='manual', text=<comma-separated list>) "
            "rather than calling update_inventory per item."
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
    },
    {
        "name": "plan_meal",
        "description": (
            "Add a meal to the plan for a specific date. "
            "Always fetch the recipe (parse_recipe_from_url) before calling this so ingredients "
            "can be populated and availability checking works from the start. "
            "Does NOT touch inventory — planning is separate from cooking. "
            "For 'eating out', set name='Eating out' with no ingredients."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "planned_date": {"type": "string", "description": "ISO date YYYY-MM-DD."},
                "meal_type": {
                    "type": "string",
                    "enum": ["lunch", "brunch", "dinner", "batch_cook"],
                    "description": (
                        "Use 'batch_cook' for planned cooking/prep sessions (batch cooking, "
                        "baking, making ahead to freeze). Use 'lunch', 'dinner', or 'brunch' "
                        "for meals planned to eat. batch_cook entries appear in the Cook Plan, "
                        "not the Meal Plan eating grid."
                    ),
                },
                "ingredients": {
                    "type": "array",
                    "description": "Ingredient list for this meal.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "quantity": {"type": "number"},
                            "unit": {"type": "string"},
                            "notes": {"type": "string"},
                        },
                        "required": ["name", "quantity", "unit"],
                    },
                },
                "servings": {"type": "integer", "description": "Defaults to 2."},
                "cuisine_tag": {"type": "string"},
                "source_url": {"type": "string", "description": "Recipe URL if applicable."},
                "notes": {"type": "string"},
            },
            "required": ["name", "planned_date", "meal_type"],
        },
    },
    {
        "name": "get_meal_plan",
        "description": (
            "View upcoming planned meals with per-ingredient availability vs current inventory. "
            "Each ingredient has in_stock (bool) and stock_qty. "
            "Each plan has all_in_stock (bool). "
            "Staple ingredients (subcategory herb_spice or condiment) are checked by presence "
            "only — any stock = available regardless of recipe quantity. "
            "Tell the user to say 'I've used the last of X' when a staple runs out."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string", "description": "ISO date. Omit to get all."},
                "to_date": {"type": "string", "description": "ISO date."},
                "status": {
                    "type": "string",
                    "enum": ["planned", "cooked", "skipped"],
                    "description": "Filter by status. Omit to get all.",
                },
            },
        },
    },
    {
        "name": "update_meal_plan",
        "description": "Update a meal plan entry — change date, meal_type, name, servings, status, or replace the ingredient list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "integer"},
                "name": {"type": "string"},
                "meal_type": {"type": "string", "enum": ["lunch", "brunch", "dinner", "batch_cook"]},
                "planned_date": {"type": "string", "description": "ISO date YYYY-MM-DD."},
                "servings": {"type": "integer"},
                "cuisine_tag": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["planned", "cooked", "skipped"],
                    "description": "Use 'skipped' when a planned meal wasn't made.",
                },
                "source_url": {"type": "string"},
                "notes": {"type": "string"},
                "ingredients": {
                    "type": "array",
                    "description": "If provided, replaces the entire ingredient list.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "quantity": {"type": "number"},
                            "unit": {"type": "string"},
                            "notes": {"type": "string"},
                        },
                        "required": ["name", "quantity", "unit"],
                    },
                },
            },
            "required": ["plan_id"],
        },
    },
    {
        "name": "remove_from_meal_plan",
        "description": "Remove a meal from the plan. Does not affect inventory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "integer"},
            },
            "required": ["plan_id"],
        },
    },
    {
        "name": "get_week_plan",
        "description": (
            "Return the Mon–Sun meal plan grid for a given week. "
            "Each day has a 'slots' dict keyed by meal_type (lunch, brunch, dinner) with availability info. "
            "Use this at the start of a planning session — call it first to show what's already planned "
            "and what slots are empty. "
            "Defaults to the upcoming Monday if today is Sat/Sun, otherwise the current Monday."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "week_start": {
                    "type": "string",
                    "description": "ISO date of the Monday to view. Omit for the default week.",
                },
            },
        },
    },
    {
        "name": "get_shopping_list",
        "description": (
            "Generate a shopping list: ingredients needed for planned meals that are not sufficiently "
            "in stock. Returns items with quantity_to_buy and which plans need them. "
            "Staples (herb_spice / condiment subcategory) only appear here if completely out of stock. "
            "Optionally scope to a single week with week_start."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "week_start": {
                    "type": "string",
                    "description": "ISO date of Monday. If provided, only includes plans for that week.",
                },
            },
        },
    },
    {
        "name": "save_recipe",
        "description": (
            "Save a recipe to the recipe bank. "
            "Deduplicates by URL — if the URL already exists, updates metadata instead. "
            "Call after parse_recipe_from_url when the user wants to keep a recipe, "
            "or when they say 'save this recipe' / 'add to my recipe bank'. "
            "Also call when saving a recipe that was just added to the meal plan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "source_url": {"type": "string", "description": "Recipe URL."},
                "cuisine_tag": {"type": "string"},
                "tags": {
                    "type": "string",
                    "description": (
                        "Comma-separated tags. Valid values: quick, batch_cook, vegetarian, "
                        "vegan, weekend, light, freezer_friendly, favourite."
                    ),
                },
                "notes": {"type": "string", "description": "Personal notes, e.g. 'great with rice'."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_recipes",
        "description": (
            "Search the recipe bank. Results are sorted by times_planned descending so "
            "frequently used recipes surface first. "
            "Use during planning sessions to find relevant recipes — cross-reference with "
            "get_inventory to surface options that use what's already in stock. "
            "Read preferred_recipe_domains from preferences and favour those sources when suggesting."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Partial name match."},
                "cuisine_tag": {"type": "string", "description": "Filter by cuisine."},
                "tag": {
                    "type": "string",
                    "description": "Filter by single tag, e.g. 'quick' or 'batch_cook'.",
                },
                "limit": {"type": "integer", "description": "Max results. Defaults to 20."},
            },
        },
    },
    {
        "name": "delete_recipe_from_bank",
        "description": "Remove a recipe from the recipe bank. Does not affect the meal plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "recipe_id": {"type": "integer"},
            },
            "required": ["recipe_id"],
        },
    },
    {
        "name": "mark_recipe_planned",
        "description": (
            "Increment times_planned and set last_planned to today for a recipe in the bank. "
            "Call this whenever a recipe from the bank is added to the meal plan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recipe_id": {"type": "integer"},
            },
            "required": ["recipe_id"],
        },
    },
    {
        "name": "list_recurring_deliveries",
        "description": "List all configured recurring deliveries (e.g. milkman) with their schedules and pause status.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_recurring_delivery",
        "description": (
            "Configure a new recurring delivery — e.g. a milkman, a weekly bread order. "
            "The scheduler will send a Telegram nudge on each delivery day asking for confirmation. "
            "days format: comma-separated lowercase day names, e.g. 'monday,thursday'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Short unique identifier, e.g. 'milkman'."},
                "description": {"type": "string", "description": "Human-readable label, e.g. 'Milkman — 2 pints whole milk'."},
                "items": {
                    "type": "array",
                    "description": "Fixed items added on each delivery.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "quantity": {"type": "number"},
                            "unit": {"type": "string"},
                            "location": {"type": "string", "enum": ["fresh", "freezer", "pantry"]},
                            "subcategory": {"type": "string"},
                        },
                        "required": ["name", "quantity", "unit", "location"],
                    },
                },
                "days": {"type": "string", "description": "Comma-separated delivery days, e.g. 'monday,thursday'."},
                "send_time": {"type": "string", "description": "HH:MM nudge time. Defaults to '07:00'."},
            },
            "required": ["label", "description", "items", "days"],
        },
    },
    {
        "name": "update_recurring_delivery",
        "description": (
            "Update a recurring delivery config. "
            "To pause for a holiday, set paused_until to the return date (ISO date). "
            "To permanently disable, set active=false. "
            "To re-enable after a pause, set active=true and paused_until=null."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "description": {"type": "string"},
                "items": {"type": "array", "items": {"type": "object"}},
                "days": {"type": "string"},
                "send_time": {"type": "string"},
                "active": {"type": "boolean"},
                "paused_until": {
                    "type": "string",
                    "description": "ISO date YYYY-MM-DD to pause until (inclusive). Pass null to clear.",
                },
            },
            "required": ["label"],
        },
    },
    {
        "name": "confirm_recurring_delivery",
        "description": (
            "Add a recurring delivery's items to inventory. "
            "Call this when the user confirms a delivery arrived "
            "(e.g. replies 'yes' to the morning nudge)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "The delivery label, e.g. 'milkman'."},
            },
            "required": ["label"],
        },
    },
    {
        "name": "preview_cook",
        "description": (
            "Preview what completing a batch cook session would change, without committing anything. "
            "Call this when the user says they've made / finished cooking something from the cook plan. "
            "Matches the plan's ingredient list against current inventory and returns proposed deductions. "
            "Present the summary to the user and ask: where does the output go (fridge/freezer/split)? "
            "How many portions? Is any output an ingredient rather than a portioned meal (e.g. a condiment, "
            "batch stock)? Then call confirm_cook with their answers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_id": {
                    "type": "integer",
                    "description": "ID of the batch_cook meal plan entry.",
                },
            },
            "required": ["plan_id"],
        },
    },
    {
        "name": "confirm_cook",
        "description": (
            "Commit a completed cook session. Call after preview_cook once the user has confirmed "
            "the deductions and told you where each output should go. "
            "Marks the meal plan entry as cooked, deducts ingredients, and creates output records."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_id": {
                    "type": "integer",
                    "description": "ID of the batch_cook meal plan entry being completed.",
                },
                "deductions": {
                    "type": "array",
                    "description": "Ingredients to subtract from inventory. Use the ingredient_ids from preview_cook.",
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
                "outputs": {
                    "type": "array",
                    "description": (
                        "What the cook session produced. Each item is either a portioned meal or "
                        "an ingredient (e.g. a condiment or batch stock). "
                        "Never assume location — always ask the user first."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["meal", "ingredient"],
                                "description": "'meal' for portioned dishes; 'ingredient' for condiments, stock, etc.",
                            },
                            "name": {"type": "string"},
                            "cuisine_tag": {
                                "type": "string",
                                "description": "Required for type='meal'. E.g. british, south-asian, italian.",
                            },
                            "portions": {
                                "type": "integer",
                                "description": "Required for type='meal'.",
                            },
                            "quantity": {
                                "type": "number",
                                "description": "Required for type='ingredient'.",
                            },
                            "unit": {
                                "type": "string",
                                "description": "Required for type='ingredient'.",
                            },
                            "location": {
                                "type": "string",
                                "enum": ["fresh", "freezer", "pantry"],
                                "description": "Where to store the output. Always confirm with user — do not assume.",
                            },
                            "subcategory": {
                                "type": "string",
                                "description": "For type='ingredient' only.",
                            },
                            "notes": {"type": "string"},
                        },
                        "required": ["type", "name", "location"],
                    },
                },
            },
            "required": ["plan_id", "deductions", "outputs"],
        },
    },
    {
        "name": "parse_recipe_from_url",
        "description": (
            "Fetch a recipe URL and extract name, servings, cuisine_tag, and ingredient list. "
            "Returns structured data — does NOT add to the meal plan. "
            "After reviewing with the user, call plan_meal to schedule it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL of the recipe page."},
            },
            "required": ["url"],
        },
        "cache_control": {"type": "ephemeral"},  # caches entire tool list as a prefix
    },
]
