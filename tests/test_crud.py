"""Tests for db/crud.py — all run against an in-memory SQLite database."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from db import crud


# ---------------------------------------------------------------------------
# Ingredients
# ---------------------------------------------------------------------------


async def test_create_and_get_ingredient(db_session: AsyncSession) -> None:
    ing = await crud.create_ingredient(
        db_session,
        name="courgette",
        quantity=3.0,
        unit="whole",
        source_label="manual",
        location="fresh",
        arrived_date=date.today(),
    )
    assert ing.id is not None
    assert ing.name == "courgette"

    fetched = await crud.get_ingredient(db_session, ing.id)
    assert fetched is not None
    assert fetched.name == "courgette"
    assert fetched.quantity == 3.0


async def test_get_ingredient_missing(db_session: AsyncSession) -> None:
    result = await crud.get_ingredient(db_session, 9999)
    assert result is None


async def test_list_ingredients_by_location(db_session: AsyncSession) -> None:
    await crud.create_ingredient(
        db_session, name="spinach", quantity=150.0, unit="g",
        source_label="manual", location="fresh", arrived_date=date.today(),
    )
    await crud.create_ingredient(
        db_session, name="basmati rice", quantity=1.0, unit="kg",
        source_label="manual", location="pantry", arrived_date=date.today(),
    )

    fresh = await crud.list_ingredients(db_session, location="fresh")
    pantry = await crud.list_ingredients(db_session, location="pantry")

    assert len(fresh) == 1
    assert fresh[0].name == "spinach"
    assert len(pantry) == 1
    assert pantry[0].name == "basmati rice"


async def test_list_ingredients_by_expiry(db_session: AsyncSession) -> None:
    soon = date.today() + timedelta(days=1)
    later = date.today() + timedelta(days=30)

    await crud.create_ingredient(
        db_session, name="chicken thighs", quantity=500.0, unit="g",
        source_label="manual", location="fresh", arrived_date=date.today(),
        best_before=soon,
    )
    await crud.create_ingredient(
        db_session, name="mature cheddar", quantity=200.0, unit="g",
        source_label="manual", location="fresh", arrived_date=date.today(),
        best_before=later,
    )

    expiring = await crud.list_ingredients(
        db_session, expiry_before=date.today() + timedelta(days=3)
    )
    assert len(expiring) == 1
    assert expiring[0].name == "chicken thighs"


async def test_update_ingredient(db_session: AsyncSession) -> None:
    ing = await crud.create_ingredient(
        db_session, name="aubergine", quantity=2.0, unit="whole",
        source_label="manual", location="fresh", arrived_date=date.today(),
    )
    updated = await crud.update_ingredient(db_session, ing.id, {"quantity": 1.0})
    assert updated is not None
    assert updated.quantity == 1.0


async def test_delete_ingredient(db_session: AsyncSession) -> None:
    ing = await crud.create_ingredient(
        db_session, name="rocket", quantity=80.0, unit="g",
        source_label="manual", location="fresh", arrived_date=date.today(),
    )
    deleted = await crud.delete_ingredient(db_session, ing.id)
    assert deleted is True
    assert await crud.get_ingredient(db_session, ing.id) is None


async def test_delete_ingredient_missing(db_session: AsyncSession) -> None:
    assert await crud.delete_ingredient(db_session, 9999) is False


# ---------------------------------------------------------------------------
# Meals
# ---------------------------------------------------------------------------


async def test_create_and_get_meal(db_session: AsyncSession) -> None:
    meal = await crud.create_meal(
        db_session,
        name="Chicken and Lentil Dal",
        cuisine_tag="south-asian",
        cooked_date=date.today(),
        total_portions=4,
        portions_remaining=4,
        location="freezer",
    )
    assert meal.id is not None

    fetched = await crud.get_meal(db_session, meal.id)
    assert fetched is not None
    assert fetched.name == "Chicken and Lentil Dal"
    assert fetched.cuisine_tag == "south-asian"
    assert fetched.portions_remaining == 4


async def test_list_meals_by_location(db_session: AsyncSession) -> None:
    await crud.create_meal(
        db_session, name="Dal", cuisine_tag="south-asian",
        cooked_date=date.today(), total_portions=4, portions_remaining=4,
        location="freezer",
    )
    await crud.create_meal(
        db_session, name="Pasta", cuisine_tag="italian",
        cooked_date=date.today(), total_portions=2, portions_remaining=1,
        location="fresh",
    )

    freezer = await crud.list_meals(db_session, location="freezer")
    fresh = await crud.list_meals(db_session, location="fresh")

    assert len(freezer) == 1
    assert len(fresh) == 1


async def test_update_meal(db_session: AsyncSession) -> None:
    meal = await crud.create_meal(
        db_session, name="Ragu", cuisine_tag="italian",
        cooked_date=date.today(), total_portions=4, portions_remaining=4,
        location="freezer",
    )
    updated = await crud.update_meal(db_session, meal.id, {"portions_remaining": 3})
    assert updated is not None
    assert updated.portions_remaining == 3


async def test_delete_meal(db_session: AsyncSession) -> None:
    meal = await crud.create_meal(
        db_session, name="Soup", cuisine_tag="british",
        cooked_date=date.today(), total_portions=3, portions_remaining=3,
    )
    assert await crud.delete_meal(db_session, meal.id) is True
    assert await crud.get_meal(db_session, meal.id) is None


# ---------------------------------------------------------------------------
# MealIngredients
# ---------------------------------------------------------------------------


async def test_add_and_list_meal_ingredients(db_session: AsyncSession) -> None:
    meal = await crud.create_meal(
        db_session, name="Dal", cuisine_tag="south-asian",
        cooked_date=date.today(), total_portions=4, portions_remaining=4,
    )
    ing = await crud.create_ingredient(
        db_session, name="red lentils", quantity=400.0, unit="g",
        source_label="manual", location="pantry", arrived_date=date.today(),
    )

    mi = await crud.add_meal_ingredient(
        db_session, meal_id=meal.id, ingredient_id=ing.id,
        quantity_used=400.0, unit="g",
    )
    assert mi.id is not None
    assert mi.meal_id == meal.id

    items = await crud.list_meal_ingredients(db_session, meal.id)
    assert len(items) == 1
    assert items[0].quantity_used == 400.0


# ---------------------------------------------------------------------------
# NutritionLog
# ---------------------------------------------------------------------------


async def test_create_and_get_nutrition_log(db_session: AsyncSession) -> None:
    log = await crud.create_nutrition_log(
        db_session,
        log_date=date.today(),
        calories=2100.0,
        protein_g=135.0,
        fibre_g=28.0,
    )
    assert log.id is not None

    fetched = await crud.get_nutrition_log(db_session, log.id)
    assert fetched is not None
    assert fetched.calories == 2100.0


async def test_list_nutrition_logs_date_filter(db_session: AsyncSession) -> None:
    today = date.today()
    await crud.create_nutrition_log(
        db_session, log_date=today - timedelta(days=2), calories=2000.0, protein_g=120.0, fibre_g=25.0
    )
    await crud.create_nutrition_log(
        db_session, log_date=today - timedelta(days=1), calories=2200.0, protein_g=140.0, fibre_g=30.0
    )
    await crud.create_nutrition_log(
        db_session, log_date=today, calories=800.0, protein_g=50.0, fibre_g=12.0
    )

    recent = await crud.list_nutrition_logs(
        db_session, start_date=today - timedelta(days=1), end_date=today
    )
    assert len(recent) == 2

    all_logs = await crud.list_nutrition_logs(db_session)
    assert len(all_logs) == 3


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


async def test_set_and_get_preference(db_session: AsyncSession) -> None:
    await crud.set_preference(db_session, "cultural_home", "british")
    pref = await crud.get_preference(db_session, "cultural_home")
    assert pref is not None
    assert pref.value == "british"


async def test_set_preference_upserts(db_session: AsyncSession) -> None:
    await crud.set_preference(db_session, "calorie_target", "2000")
    await crud.set_preference(db_session, "calorie_target", "2200")

    pref = await crud.get_preference(db_session, "calorie_target")
    assert pref is not None
    assert pref.value == "2200"


async def test_get_all_preferences(db_session: AsyncSession) -> None:
    await crud.set_preference(db_session, "cultural_home", "british")
    await crud.set_preference(db_session, "cuisine_openness", "high")

    prefs = await crud.get_all_preferences(db_session)
    assert prefs["cultural_home"] == "british"
    assert prefs["cuisine_openness"] == "high"


async def test_get_preference_missing(db_session: AsyncSession) -> None:
    result = await crud.get_preference(db_session, "nonexistent_key")
    assert result is None


# ---------------------------------------------------------------------------
# DeliverySchedule
# ---------------------------------------------------------------------------


async def test_create_and_list_delivery_schedule(db_session: AsyncSession) -> None:
    ds = await crud.create_delivery_schedule(
        db_session,
        source_label="veg_box",
        expected_date=date.today() + timedelta(days=3),
        scraped_at=datetime.now(UTC),
        raw_json='{"items": ["courgette", "aubergine"]}',
    )
    assert ds.id is not None

    all_ds = await crud.list_delivery_schedules(db_session)
    assert len(all_ds) == 1

    filtered = await crud.list_delivery_schedules(db_session, source_label="veg_box")
    assert len(filtered) == 1

    empty = await crud.list_delivery_schedules(db_session, source_label="meat_box")
    assert len(empty) == 0
