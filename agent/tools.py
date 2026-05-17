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
from nutrition.lookup import fetch_nutrition

_anthropic_client = anthropic.AsyncAnthropic()
_RECIPE_PARSE_MODEL = "claude-haiku-4-5-20251001"


# Normalise inventory quantities to a base unit so kg/g, l/ml, pint/ml comparisons work.
_UNIT_TO_BASE: dict[str, tuple[str, float]] = {
    "kg": ("g", 1000.0),
    "l": ("ml", 1000.0),
    "pint": ("ml", 568.0),   # UK pint
    "pints": ("ml", 568.0),
}

# Ingredient-specific conversions where the units are otherwise incompatible.
_INGREDIENT_UNIT_TO_BASE: dict[tuple[str, str], tuple[str, float]] = {
    ("garlic", "bulb"):  ("cloves", 12),
    ("garlic", "bulbs"): ("cloves", 12),
}

# If an ingredient is stocked in one of these "container" units but a recipe
# measures it in a specific amount, treat it as available (container = "I have some").
_CONTAINER_UNITS = frozenset({"whole", "jar", "bottle", "bag", "box", "packet"})

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
) -> tuple[bool, float]:
    """Return (in_stock, stock_qty).

    Staples (herb_spice / condiment subcategory): any stock = available,
    quantity is ignored — they only appear on the shopping list when fully gone.

    Otherwise falls back to a container-unit check: if stocked as a whole container
    (jar, bag, etc.) but the recipe measures in tsp/g/ml, treat as available.
    """
    if name in staple_names:
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
    log = await crud.create_nutrition_log(
        session,
        log_date=date.today(),
        calories=calories,
        protein_g=protein_g,
        fibre_g=fibre_g,
        source_meal_id=meal_id,
    )
    if new_portions <= 0:
        await crud.delete_meal(session, meal_id)
        return {"status": "fully eaten and removed", "nutrition_log_id": log.id}
    await crud.update_meal(session, meal_id, {"portions_remaining": new_portions})
    return {"portions_remaining": new_portions, "nutrition_log_id": log.id}


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
) -> dict:
    plans = await crud.list_meal_plans(
        session,
        from_date=date.fromisoformat(from_date) if from_date else None,
        to_date=date.fromisoformat(to_date) if to_date else None,
        status=status,
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

    result = []
    for plan in plans:
        ings = await crud.list_meal_plan_ingredients(session, plan.id)
        ing_list = []
        for pi in ings:
            needed_qty, needed_unit = _normalise(pi.quantity, pi.unit, pi.name)
            in_stock, have = _check_stock(stock, pi.name.lower(), needed_qty, needed_unit, staple_names)
            ing_list.append({
                "id": pi.id,
                "name": pi.name,
                "quantity": pi.quantity,
                "unit": pi.unit,
                "notes": pi.notes,
                "in_stock": in_stock,
                "stock_qty": round(have, 2),
            })
        entry = _meal_plan_to_dict(plan)
        entry["ingredients"] = ing_list
        entry["all_in_stock"] = all(i["in_stock"] for i in ing_list) if ing_list else True
        result.append(entry)
    return {"plans": result}


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

    plans = await crud.list_meal_plans(session, from_date=ws, to_date=we)

    all_inventory = await crud.list_ingredients(session)
    stock: dict[tuple, float] = {}
    staple_names: frozenset[str] = frozenset(
        ing.name.lower() for ing in all_inventory if ing.subcategory in _STAPLE_SUBCATEGORIES
    )
    for ing in all_inventory:
        qty, unit = _normalise(ing.quantity, ing.unit, ing.name)
        key = (ing.name.lower(), unit)
        stock[key] = stock.get(key, 0) + qty

    days = []
    for i in range(7):
        day = ws + timedelta(days=i)
        day_plans = [p for p in plans if p.planned_date == day]
        slots: dict[str, dict] = {}
        for plan in day_plans:
            mt = plan.meal_type or "dinner"
            ings = await crud.list_meal_plan_ingredients(session, plan.id)
            ing_list = []
            for pi in ings:
                nq, nu = _normalise(pi.quantity, pi.unit, pi.name)
                ok, have = _check_stock(stock, pi.name.lower(), nq, nu, staple_names)
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
        plans = await crud.list_meal_plans(session, from_date=ws, to_date=we, status="planned")
    else:
        plans = await crud.list_meal_plans(session, from_date=today, status="planned")

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

    shopping_list = []
    for key, item in needed.items():
        name, unit = key
        in_stock, have = _check_stock(stock, name, item["quantity_needed"], unit, staple_names)
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
    "get_nutrition_summary": get_nutrition_summary,
    "get_preferences": get_preferences,
    "set_preference": set_preference,
    "fetch_from_source": fetch_from_source,
    "inventory_from_image": inventory_from_image,
    "list_sources": list_sources,
    "lookup_nutrition": lookup_nutrition,
    "get_delivery_schedule": get_delivery_schedule,
    "plan_meal": plan_meal,
    "get_meal_plan": get_meal_plan,
    "get_week_plan": get_week_plan,
    "update_meal_plan": update_meal_plan,
    "remove_from_meal_plan": remove_from_meal_plan,
    "get_shopping_list": get_shopping_list,
    "parse_recipe_from_url": parse_recipe_from_url,
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
                    "enum": ["lunch", "brunch", "dinner"],
                    "description": (
                        "Slot in the day. Use 'brunch' for weekend/holiday midday meals "
                        "that replace both breakfast and lunch. Defaults to 'dinner'."
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
                "meal_type": {"type": "string", "enum": ["lunch", "brunch", "dinner"]},
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
