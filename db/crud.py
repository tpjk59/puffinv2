"""Async CRUD operations for every model. No business logic — pure DB operations."""

from datetime import UTC, date, datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    DeliverySchedule,
    Ingredient,
    Meal,
    MealIngredient,
    NutritionLog,
    Preference,
)


# ---------------------------------------------------------------------------
# Ingredients
# ---------------------------------------------------------------------------


async def create_ingredient(
    session: AsyncSession,
    name: str,
    quantity: float,
    unit: str,
    source_label: str,
    location: str,
    arrived_date: date,
    best_before: Optional[date] = None,
    subcategory: Optional[str] = None,
    usda_fdc_id: Optional[str] = None,
    open_food_facts_id: Optional[str] = None,
    calories_per_100g: Optional[float] = None,
    protein_per_100g: Optional[float] = None,
    fibre_per_100g: Optional[float] = None,
    notes: Optional[str] = None,
) -> Ingredient:
    ingredient = Ingredient(
        name=name,
        quantity=quantity,
        unit=unit,
        source_label=source_label,
        location=location,
        subcategory=subcategory,
        arrived_date=arrived_date,
        best_before=best_before,
        usda_fdc_id=usda_fdc_id,
        open_food_facts_id=open_food_facts_id,
        calories_per_100g=calories_per_100g,
        protein_per_100g=protein_per_100g,
        fibre_per_100g=fibre_per_100g,
        notes=notes,
    )
    session.add(ingredient)
    await session.commit()
    await session.refresh(ingredient)
    return ingredient


async def get_ingredient(session: AsyncSession, ingredient_id: int) -> Optional[Ingredient]:
    result = await session.execute(select(Ingredient).where(Ingredient.id == ingredient_id))
    return result.scalar_one_or_none()


async def list_ingredients(
    session: AsyncSession,
    location: Optional[str] = None,
    source_label: Optional[str] = None,
    expiry_before: Optional[date] = None,
) -> list[Ingredient]:
    query = select(Ingredient)
    if location is not None:
        query = query.where(Ingredient.location == location)
    if source_label is not None:
        query = query.where(Ingredient.source_label == source_label)
    if expiry_before is not None:
        query = query.where(
            Ingredient.best_before != None,  # noqa: E711
            Ingredient.best_before <= expiry_before,
        )
    result = await session.execute(query)
    return list(result.scalars().all())


async def update_ingredient(
    session: AsyncSession, ingredient_id: int, updates: dict[str, Any]
) -> Optional[Ingredient]:
    ingredient = await get_ingredient(session, ingredient_id)
    if ingredient is None:
        return None
    for key, value in updates.items():
        setattr(ingredient, key, value)
    await session.commit()
    await session.refresh(ingredient)
    return ingredient


async def delete_ingredient(session: AsyncSession, ingredient_id: int) -> bool:
    ingredient = await get_ingredient(session, ingredient_id)
    if ingredient is None:
        return False
    await session.delete(ingredient)
    await session.commit()
    return True


# ---------------------------------------------------------------------------
# Meals
# ---------------------------------------------------------------------------


async def create_meal(
    session: AsyncSession,
    name: str,
    cuisine_tag: str,
    cooked_date: date,
    total_portions: int,
    portions_remaining: int,
    location: str = "freezer",
    notes: Optional[str] = None,
) -> Meal:
    meal = Meal(
        name=name,
        cuisine_tag=cuisine_tag,
        cooked_date=cooked_date,
        total_portions=total_portions,
        portions_remaining=portions_remaining,
        location=location,
        notes=notes,
    )
    session.add(meal)
    await session.commit()
    await session.refresh(meal)
    return meal


async def get_meal(session: AsyncSession, meal_id: int) -> Optional[Meal]:
    result = await session.execute(select(Meal).where(Meal.id == meal_id))
    return result.scalar_one_or_none()


async def list_meals(
    session: AsyncSession,
    location: Optional[str] = None,
) -> list[Meal]:
    query = select(Meal)
    if location is not None:
        query = query.where(Meal.location == location)
    result = await session.execute(query)
    return list(result.scalars().all())


async def update_meal(
    session: AsyncSession, meal_id: int, updates: dict[str, Any]
) -> Optional[Meal]:
    meal = await get_meal(session, meal_id)
    if meal is None:
        return None
    for key, value in updates.items():
        setattr(meal, key, value)
    await session.commit()
    await session.refresh(meal)
    return meal


async def delete_meal(session: AsyncSession, meal_id: int) -> bool:
    meal = await get_meal(session, meal_id)
    if meal is None:
        return False
    await session.delete(meal)
    await session.commit()
    return True


# ---------------------------------------------------------------------------
# MealIngredients
# ---------------------------------------------------------------------------


async def add_meal_ingredient(
    session: AsyncSession,
    meal_id: int,
    ingredient_id: int,
    quantity_used: float,
    unit: str,
) -> MealIngredient:
    mi = MealIngredient(
        meal_id=meal_id,
        ingredient_id=ingredient_id,
        quantity_used=quantity_used,
        unit=unit,
    )
    session.add(mi)
    await session.commit()
    await session.refresh(mi)
    return mi


async def list_meal_ingredients(
    session: AsyncSession, meal_id: int
) -> list[MealIngredient]:
    result = await session.execute(
        select(MealIngredient).where(MealIngredient.meal_id == meal_id)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# NutritionLog
# ---------------------------------------------------------------------------


async def create_nutrition_log(
    session: AsyncSession,
    log_date: date,
    calories: float,
    protein_g: float,
    fibre_g: float,
    source_meal_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> NutritionLog:
    log = NutritionLog(
        date=log_date,
        source_meal_id=source_meal_id,
        calories=calories,
        protein_g=protein_g,
        fibre_g=fibre_g,
        notes=notes,
    )
    session.add(log)
    await session.commit()
    await session.refresh(log)
    return log


async def get_nutrition_log(session: AsyncSession, log_id: int) -> Optional[NutritionLog]:
    result = await session.execute(select(NutritionLog).where(NutritionLog.id == log_id))
    return result.scalar_one_or_none()


async def list_nutrition_logs(
    session: AsyncSession,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> list[NutritionLog]:
    query = select(NutritionLog)
    if start_date is not None:
        query = query.where(NutritionLog.date >= start_date)
    if end_date is not None:
        query = query.where(NutritionLog.date <= end_date)
    result = await session.execute(query.order_by(NutritionLog.date))
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


async def set_preference(session: AsyncSession, key: str, value: str) -> Preference:
    existing = await get_preference(session, key)
    if existing is not None:
        existing.value = value
        existing.updated_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(existing)
        return existing
    pref = Preference(key=key, value=value, updated_at=datetime.now(UTC))
    session.add(pref)
    await session.commit()
    await session.refresh(pref)
    return pref


async def get_preference(session: AsyncSession, key: str) -> Optional[Preference]:
    result = await session.execute(select(Preference).where(Preference.key == key))
    return result.scalar_one_or_none()


async def get_all_preferences(session: AsyncSession) -> dict[str, str]:
    result = await session.execute(select(Preference))
    return {p.key: p.value for p in result.scalars().all()}


# ---------------------------------------------------------------------------
# DeliverySchedule
# ---------------------------------------------------------------------------


async def create_delivery_schedule(
    session: AsyncSession,
    source_label: str,
    expected_date: date,
    scraped_at: datetime,
    raw_json: Optional[str] = None,
) -> DeliverySchedule:
    ds = DeliverySchedule(
        source_label=source_label,
        expected_date=expected_date,
        scraped_at=scraped_at,
        raw_json=raw_json,
    )
    session.add(ds)
    await session.commit()
    await session.refresh(ds)
    return ds


async def list_delivery_schedules(
    session: AsyncSession,
    source_label: Optional[str] = None,
) -> list[DeliverySchedule]:
    query = select(DeliverySchedule)
    if source_label is not None:
        query = query.where(DeliverySchedule.source_label == source_label)
    result = await session.execute(query.order_by(DeliverySchedule.expected_date))
    return list(result.scalars().all())
