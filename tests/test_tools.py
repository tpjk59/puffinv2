"""Tests for all 11 agent tools against an in-memory SQLite database."""

import json
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from db import crud
from agent import tools


# ---------------------------------------------------------------------------
# get_inventory
# ---------------------------------------------------------------------------


async def test_get_inventory_empty(db_session: AsyncSession) -> None:
    result = await tools.get_inventory(db_session)
    assert result["count"] == 0
    assert result["ingredients"] == []


async def test_get_inventory_by_location(db_session: AsyncSession) -> None:
    await crud.create_ingredient(
        db_session, name="spinach", quantity=150, unit="g",
        source_label="manual", location="fresh", arrived_date=date.today(),
    )
    await crud.create_ingredient(
        db_session, name="basmati rice", quantity=1, unit="kg",
        source_label="manual", location="pantry", arrived_date=date.today(),
    )
    fresh = await tools.get_inventory(db_session, location="fresh")
    assert fresh["count"] == 1
    assert fresh["ingredients"][0]["name"] == "spinach"


async def test_get_inventory_expiry_filter(db_session: AsyncSession) -> None:
    soon = date.today() + timedelta(days=1)
    later = date.today() + timedelta(days=30)
    await crud.create_ingredient(
        db_session, name="chicken thighs", quantity=500, unit="g",
        source_label="manual", location="fresh", arrived_date=date.today(), best_before=soon,
    )
    await crud.create_ingredient(
        db_session, name="mature cheddar", quantity=200, unit="g",
        source_label="manual", location="fresh", arrived_date=date.today(), best_before=later,
    )
    expiring = await tools.get_inventory(db_session, expiry_within_days=3)
    assert expiring["count"] == 1
    assert expiring["ingredients"][0]["name"] == "chicken thighs"


# ---------------------------------------------------------------------------
# update_inventory
# ---------------------------------------------------------------------------


async def test_update_inventory_add(db_session: AsyncSession) -> None:
    result = await tools.update_inventory(
        db_session, action="add", name="courgette", quantity=3, unit="whole", location="fresh"
    )
    assert "added" in result
    assert result["added"]["name"] == "courgette"
    assert result["added"]["quantity"] == 3.0


async def test_update_inventory_consume_partial(db_session: AsyncSession) -> None:
    ing = await crud.create_ingredient(
        db_session, name="red lentils", quantity=500, unit="g",
        source_label="manual", location="pantry", arrived_date=date.today(),
    )
    result = await tools.update_inventory(
        db_session, action="consume", ingredient_id=ing.id, quantity=200
    )
    assert "updated" in result
    assert result["updated"]["quantity"] == 300.0


async def test_update_inventory_consume_fully(db_session: AsyncSession) -> None:
    ing = await crud.create_ingredient(
        db_session, name="free-range eggs", quantity=2, unit="whole",
        source_label="manual", location="fresh", arrived_date=date.today(),
    )
    result = await tools.update_inventory(
        db_session, action="consume", ingredient_id=ing.id, quantity=2
    )
    assert result["status"] == "fully consumed"
    assert await crud.get_ingredient(db_session, ing.id) is None


async def test_update_inventory_expire(db_session: AsyncSession) -> None:
    ing = await crud.create_ingredient(
        db_session, name="old milk", quantity=500, unit="ml",
        source_label="manual", location="fresh", arrived_date=date.today(),
    )
    result = await tools.update_inventory(
        db_session, action="expire", ingredient_id=ing.id
    )
    assert result["status"] == "expired and removed"
    assert await crud.get_ingredient(db_session, ing.id) is None


async def test_update_inventory_missing_ingredient(db_session: AsyncSession) -> None:
    result = await tools.update_inventory(
        db_session, action="consume", ingredient_id=9999, quantity=1
    )
    assert "error" in result


async def test_update_inventory_unknown_action(db_session: AsyncSession) -> None:
    result = await tools.update_inventory(db_session, action="teleport")
    assert "error" in result


# ---------------------------------------------------------------------------
# log_meal_cooked
# ---------------------------------------------------------------------------


async def test_log_meal_cooked_creates_meal_and_deducts(db_session: AsyncSession) -> None:
    ing = await crud.create_ingredient(
        db_session, name="red lentils", quantity=400, unit="g",
        source_label="manual", location="pantry", arrived_date=date.today(),
    )
    result = await tools.log_meal_cooked(
        db_session,
        name="Red Lentil Dal",
        cuisine_tag="south-asian",
        total_portions=4,
        ingredient_uses=[{"ingredient_id": ing.id, "quantity": 300, "unit": "g"}],
    )
    assert "meal" in result
    assert result["meal"]["name"] == "Red Lentil Dal"
    assert result["meal"]["portions_remaining"] == 4

    updated = await crud.get_ingredient(db_session, ing.id)
    assert updated.quantity == 100.0


async def test_log_meal_cooked_fully_uses_ingredient(db_session: AsyncSession) -> None:
    ing = await crud.create_ingredient(
        db_session, name="chicken thighs", quantity=600, unit="g",
        source_label="manual", location="fresh", arrived_date=date.today(),
    )
    await tools.log_meal_cooked(
        db_session,
        name="Chicken Curry",
        cuisine_tag="south-asian",
        total_portions=4,
        ingredient_uses=[{"ingredient_id": ing.id, "quantity": 600, "unit": "g"}],
    )
    assert await crud.get_ingredient(db_session, ing.id) is None


# ---------------------------------------------------------------------------
# log_meal_eaten
# ---------------------------------------------------------------------------


async def test_log_meal_eaten_decrements_portions(db_session: AsyncSession) -> None:
    meal = await crud.create_meal(
        db_session, name="Dal", cuisine_tag="south-asian",
        cooked_date=date.today(), total_portions=4, portions_remaining=4,
        location="freezer",
    )
    result = await tools.log_meal_eaten(
        db_session, meal_id=meal.id, portions=1, calories=450, protein_g=25, fibre_g=8
    )
    assert result["portions_remaining"] == 3
    updated = await crud.get_meal(db_session, meal.id)
    assert updated.portions_remaining == 3
    logs = await crud.list_nutrition_logs(db_session)
    assert len(logs) == 1
    assert logs[0].calories == 450.0


async def test_log_meal_eaten_insufficient_portions(db_session: AsyncSession) -> None:
    meal = await crud.create_meal(
        db_session, name="Soup", cuisine_tag="british",
        cooked_date=date.today(), total_portions=2, portions_remaining=1,
        location="fresh",
    )
    result = await tools.log_meal_eaten(
        db_session, meal_id=meal.id, portions=3, calories=300, protein_g=15, fibre_g=5
    )
    assert "error" in result


async def test_log_meal_eaten_meal_not_found(db_session: AsyncSession) -> None:
    result = await tools.log_meal_eaten(
        db_session, meal_id=9999, portions=1, calories=400, protein_g=20, fibre_g=6
    )
    assert "error" in result


# ---------------------------------------------------------------------------
# get_meal_history
# ---------------------------------------------------------------------------


async def test_get_meal_history(db_session: AsyncSession) -> None:
    await crud.create_meal(
        db_session, name="Dal", cuisine_tag="south-asian",
        cooked_date=date.today(), total_portions=4, portions_remaining=4, location="freezer",
    )
    await crud.create_meal(
        db_session, name="Pasta", cuisine_tag="italian",
        cooked_date=date.today(), total_portions=2, portions_remaining=2, location="fresh",
    )
    all_meals = await tools.get_meal_history(db_session)
    assert len(all_meals["meals"]) == 2

    freezer = await tools.get_meal_history(db_session, location="freezer")
    assert len(freezer["meals"]) == 1
    assert freezer["meals"][0]["name"] == "Dal"


# ---------------------------------------------------------------------------
# get_nutrition_summary
# ---------------------------------------------------------------------------


async def test_get_nutrition_summary_today(db_session: AsyncSession) -> None:
    await crud.create_nutrition_log(
        db_session, log_date=date.today(), calories=2000, protein_g=130, fibre_g=25
    )
    await crud.set_preference(db_session, "calorie_target", "2200")
    await crud.set_preference(db_session, "protein_target_g", "140")
    await crud.set_preference(db_session, "fibre_target_g", "30")

    result = await tools.get_nutrition_summary(db_session, period="today")
    assert result["totals"]["calories"] == 2000.0
    assert result["targets"]["calories"] == 2200.0
    assert result["period"] == "today"


async def test_get_nutrition_summary_week(db_session: AsyncSession) -> None:
    today = date.today()
    for i in range(3):
        await crud.create_nutrition_log(
            db_session,
            log_date=today - timedelta(days=i),
            calories=2100,
            protein_g=135,
            fibre_g=28,
        )
    result = await tools.get_nutrition_summary(db_session, period="week")
    assert result["totals"]["calories"] == 6300.0
    assert result["log_count"] == 3


# ---------------------------------------------------------------------------
# get_preferences / set_preference
# ---------------------------------------------------------------------------


async def test_get_preferences(db_session: AsyncSession) -> None:
    await crud.set_preference(db_session, "cultural_home", "british")
    await crud.set_preference(db_session, "cuisine_openness", "high")
    result = await tools.get_preferences(db_session)
    assert result["cultural_home"] == "british"
    assert result["cuisine_openness"] == "high"


async def test_set_preference(db_session: AsyncSession) -> None:
    result = await tools.set_preference(db_session, key="calorie_target", value="2400")
    assert result["value"] == "2400"
    pref = await crud.get_preference(db_session, "calorie_target")
    assert pref.value == "2400"


# ---------------------------------------------------------------------------
# list_sources
# ---------------------------------------------------------------------------


async def test_list_sources(db_session: AsyncSession) -> None:
    result = await tools.list_sources(db_session)
    labels = {s["label"] for s in result["sources"]}
    assert "manual" in labels
    assert "camera" in labels
    assert "veg_box" in labels
    assert "meat_box" in labels


# ---------------------------------------------------------------------------
# fetch_from_source (manual, with mocked LLM)
# ---------------------------------------------------------------------------


async def test_fetch_from_source_manual(db_session: AsyncSession) -> None:
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock()]
    mock_msg.content[0].text = json.dumps([
        {
            "name": "aubergine", "quantity": 1, "unit": "whole",
            "location": "fridge", "best_before": None, "notes": None,
        }
    ])
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_msg)

    import sources.registry as reg
    from sources.manual import ManualSource

    original = reg._registry["manual"]
    reg._registry["manual"] = ManualSource(client=mock_client)
    try:
        result = await tools.fetch_from_source(db_session, source_label="manual", text="1 aubergine")
        assert result["count"] == 1
        assert result["added"][0]["name"] == "aubergine"
        # Verify it was persisted
        inventory = await tools.get_inventory(db_session)
        assert inventory["count"] == 1
    finally:
        reg._registry["manual"] = original


# ---------------------------------------------------------------------------
# inventory_from_image (camera, with mocked LLM — does NOT save)
# ---------------------------------------------------------------------------


async def test_inventory_from_image_returns_candidates_only(db_session: AsyncSession) -> None:
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock()]
    mock_msg.content[0].text = json.dumps([
        {
            "name": "courgette", "quantity": 2, "unit": "whole",
            "location": "fridge", "best_before": None, "notes": "confidence:high",
        }
    ])
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_msg)

    import sources.registry as reg
    from sources.camera import CameraSource

    original = reg._registry.get("camera")
    reg._registry["camera"] = CameraSource(client=mock_client)
    try:
        result = await tools.inventory_from_image(db_session, image_b64="fakebase64==")
        assert result["count"] == 1
        assert result["candidates"][0]["name"] == "courgette"
        assert result["candidates"][0]["notes"] == "confidence:high"
        # Must NOT have saved anything
        inventory = await tools.get_inventory(db_session)
        assert inventory["count"] == 0
    finally:
        if original:
            reg._registry["camera"] = original
        else:
            reg._registry.pop("camera", None)


# ---------------------------------------------------------------------------
# dispatch_tool
# ---------------------------------------------------------------------------


async def test_dispatch_tool_unknown_name(db_session: AsyncSession) -> None:
    result = await tools.dispatch_tool("nonexistent_tool", {}, db_session)
    assert "error" in result


async def test_dispatch_tool_get_inventory(db_session: AsyncSession) -> None:
    result = await tools.dispatch_tool("get_inventory", {}, db_session)
    assert "ingredients" in result


# ---------------------------------------------------------------------------
# fetch_from_source records delivery schedule
# ---------------------------------------------------------------------------


async def test_fetch_from_source_records_delivery_schedule(db_session: AsyncSession) -> None:
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock()]
    mock_msg.content[0].text = json.dumps([
        {"name": "carrot", "quantity": 4, "unit": "whole",
         "location": "fridge", "best_before": None, "notes": None},
    ])
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_msg)

    import sources.registry as reg
    from sources.manual import ManualSource

    original = reg._registry["manual"]
    reg._registry["manual"] = ManualSource(client=mock_client)
    try:
        await tools.fetch_from_source(db_session, source_label="manual", text="4 carrots")
        schedules = await crud.list_delivery_schedules(db_session, source_label="manual")
        assert len(schedules) == 1
        assert schedules[0].source_label == "manual"
    finally:
        reg._registry["manual"] = original


# ---------------------------------------------------------------------------
# lookup_nutrition
# ---------------------------------------------------------------------------


async def test_lookup_nutrition_returns_data(db_session: AsyncSession) -> None:
    from unittest.mock import patch, AsyncMock as AM
    nutrition_data = {
        "calories_per_100g": 17.0, "protein_per_100g": 1.2,
        "fibre_per_100g": 1.1, "source": "usda", "food_name": "Courgette",
    }
    with patch("agent.tools.fetch_nutrition", AM(return_value=nutrition_data)):
        result = await tools.lookup_nutrition(db_session, ingredient_name="courgette")
    assert result["calories_per_100g"] == 17.0
    assert result["source"] == "usda"


async def test_lookup_nutrition_saves_to_ingredient(db_session: AsyncSession) -> None:
    from unittest.mock import patch, AsyncMock as AM
    ing = await crud.create_ingredient(
        db_session, name="courgette", quantity=3, unit="whole",
        source_label="manual", location="fresh", arrived_date=date.today(),
    )
    assert ing.calories_per_100g is None

    nutrition_data = {
        "calories_per_100g": 17.0, "protein_per_100g": 1.2,
        "fibre_per_100g": 1.1, "source": "usda", "food_name": "Courgette",
    }
    with patch("agent.tools.fetch_nutrition", AM(return_value=nutrition_data)):
        result = await tools.lookup_nutrition(
            db_session, ingredient_name="courgette", ingredient_id=ing.id
        )
    assert result["saved_to_ingredient_id"] == ing.id

    updated = await crud.get_ingredient(db_session, ing.id)
    assert updated.calories_per_100g == 17.0
    assert updated.protein_per_100g == 1.2


async def test_lookup_nutrition_not_found(db_session: AsyncSession) -> None:
    from unittest.mock import patch, AsyncMock as AM
    with patch("agent.tools.fetch_nutrition", AM(return_value=None)):
        result = await tools.lookup_nutrition(db_session, ingredient_name="xyzzy123")
    assert "error" in result


# ---------------------------------------------------------------------------
# get_delivery_schedule
# ---------------------------------------------------------------------------


async def test_get_delivery_schedule_empty(db_session: AsyncSession) -> None:
    result = await tools.get_delivery_schedule(db_session)
    assert result["schedules"] == []


async def test_get_delivery_schedule_with_data(db_session: AsyncSession) -> None:
    from datetime import UTC, datetime
    await crud.create_delivery_schedule(
        db_session,
        source_label="veg_box",
        expected_date=date.today(),
        scraped_at=datetime.now(UTC),
        raw_json='[{"name":"courgette"},{"name":"kale"}]',
    )
    result = await tools.get_delivery_schedule(db_session)
    assert len(result["schedules"]) == 1
    assert result["schedules"][0]["source_label"] == "veg_box"
    assert result["schedules"][0]["item_count"] == 2


# ---------------------------------------------------------------------------
# Meal plan tools
# ---------------------------------------------------------------------------


async def test_plan_meal_creates_entry(db_session: AsyncSession) -> None:
    result = await tools.plan_meal(
        db_session,
        name="Spaghetti Bolognese",
        planned_date="2026-05-20",
        ingredients=[
            {"name": "beef mince", "quantity": 400, "unit": "g"},
            {"name": "spaghetti", "quantity": 200, "unit": "g"},
        ],
        servings=2,
        cuisine_tag="italian",
    )
    assert result["name"] == "Spaghetti Bolognese"
    assert result["planned_date"] == "2026-05-20"
    assert len(result["ingredients"]) == 2
    assert result["status"] == "planned"


async def test_get_meal_plan_availability(db_session: AsyncSession) -> None:
    await crud.create_ingredient(
        db_session, name="beef mince", quantity=400, unit="g",
        source_label="manual", location="fresh", arrived_date=date.today(),
    )
    plan = await tools.plan_meal(
        db_session,
        name="Bolognese",
        planned_date="2026-05-20",
        ingredients=[
            {"name": "beef mince", "quantity": 400, "unit": "g"},
            {"name": "spaghetti", "quantity": 200, "unit": "g"},
        ],
    )
    result = await tools.get_meal_plan(db_session)
    assert len(result["plans"]) == 1
    ings = {i["name"]: i for i in result["plans"][0]["ingredients"]}
    assert ings["beef mince"]["in_stock"] is True
    assert ings["spaghetti"]["in_stock"] is False
    assert result["plans"][0]["all_in_stock"] is False


async def test_get_meal_plan_all_in_stock(db_session: AsyncSession) -> None:
    await crud.create_ingredient(
        db_session, name="pasta", quantity=500, unit="g",
        source_label="manual", location="pantry", arrived_date=date.today(),
    )
    await tools.plan_meal(
        db_session,
        name="Simple Pasta",
        planned_date="2026-05-21",
        ingredients=[{"name": "pasta", "quantity": 200, "unit": "g"}],
    )
    result = await tools.get_meal_plan(db_session)
    assert result["plans"][0]["all_in_stock"] is True


async def test_update_meal_plan_changes_date(db_session: AsyncSession) -> None:
    plan = await tools.plan_meal(
        db_session, name="Curry", planned_date="2026-05-20",
        ingredients=[{"name": "chicken", "quantity": 500, "unit": "g"}],
    )
    result = await tools.update_meal_plan(
        db_session, plan_id=plan["id"], planned_date="2026-05-22"
    )
    assert result["planned_date"] == "2026-05-22"


async def test_update_meal_plan_replaces_ingredients(db_session: AsyncSession) -> None:
    plan = await tools.plan_meal(
        db_session, name="Curry", planned_date="2026-05-20",
        ingredients=[{"name": "chicken", "quantity": 500, "unit": "g"}],
    )
    await tools.update_meal_plan(
        db_session, plan_id=plan["id"],
        ingredients=[
            {"name": "tofu", "quantity": 400, "unit": "g"},
            {"name": "peppers", "quantity": 2, "unit": "whole"},
        ],
    )
    # Fetch via get_meal_plan to see updated ingredients
    plans = await tools.get_meal_plan(db_session)
    ings = [i["name"] for i in plans["plans"][0]["ingredients"]]
    assert "tofu" in ings
    assert "chicken" not in ings


async def test_update_meal_plan_not_found(db_session: AsyncSession) -> None:
    result = await tools.update_meal_plan(db_session, plan_id=9999, name="Ghost")
    assert "error" in result


async def test_remove_from_meal_plan(db_session: AsyncSession) -> None:
    plan = await tools.plan_meal(
        db_session, name="Risotto", planned_date="2026-05-23",
        ingredients=[{"name": "arborio rice", "quantity": 300, "unit": "g"}],
    )
    result = await tools.remove_from_meal_plan(db_session, plan_id=plan["id"])
    assert result["status"] == "removed"
    remaining = await tools.get_meal_plan(db_session)
    assert remaining["plans"] == []


async def test_remove_from_meal_plan_not_found(db_session: AsyncSession) -> None:
    result = await tools.remove_from_meal_plan(db_session, plan_id=9999)
    assert "error" in result


async def test_get_shopping_list_with_gap(db_session: AsyncSession) -> None:
    await crud.create_ingredient(
        db_session, name="chicken thighs", quantity=200, unit="g",
        source_label="manual", location="fresh", arrived_date=date.today(),
    )
    await tools.plan_meal(
        db_session, name="Chicken Curry",
        planned_date=date.today().isoformat(),
        ingredients=[{"name": "chicken thighs", "quantity": 600, "unit": "g"}],
    )
    result = await tools.get_shopping_list(db_session)
    assert result["count"] == 1
    item = result["shopping_list"][0]
    assert item["name"] == "chicken thighs"
    assert item["quantity_to_buy"] == 400.0
    assert item["quantity_in_stock"] == 200.0


async def test_get_shopping_list_unit_normalisation(db_session: AsyncSession) -> None:
    # Pantry has 1 kg plain flour; recipe needs 200 g — should show as in-stock
    await crud.create_ingredient(
        db_session, name="plain flour", quantity=1, unit="kg",
        source_label="manual", location="pantry", arrived_date=date.today(),
    )
    await tools.plan_meal(
        db_session, name="Crumble",
        planned_date=date.today().isoformat(),
        ingredients=[{"name": "plain flour", "quantity": 200, "unit": "g"}],
    )
    result = await tools.get_shopping_list(db_session)
    assert result["count"] == 0, f"plain flour should be in stock but got: {result}"


async def test_get_meal_plan_garlic_bulb_to_cloves(db_session: AsyncSession) -> None:
    # 1 garlic bulb = 12 cloves; recipe needs 3 cloves — should show in-stock
    await crud.create_ingredient(
        db_session, name="garlic", quantity=1, unit="bulb",
        source_label="manual", location="fresh", arrived_date=date.today(),
    )
    await tools.plan_meal(
        db_session, name="Aloo Gobi",
        planned_date=date.today().isoformat(),
        ingredients=[{"name": "garlic", "quantity": 3, "unit": "cloves"}],
    )
    result = await tools.get_meal_plan(db_session)
    ing = result["plans"][0]["ingredients"][0]
    assert ing["in_stock"] is True, f"1 bulb should cover 3 cloves but got: {ing}"


async def test_get_meal_plan_unit_normalisation(db_session: AsyncSession) -> None:
    # Milk stored in pints; recipe needs ml
    await crud.create_ingredient(
        db_session, name="whole milk", quantity=2, unit="pint",
        source_label="manual", location="fresh", arrived_date=date.today(),
    )
    await tools.plan_meal(
        db_session, name="Pancakes",
        planned_date=date.today().isoformat(),
        ingredients=[{"name": "whole milk", "quantity": 300, "unit": "ml"}],
    )
    result = await tools.get_meal_plan(db_session)
    ing = result["plans"][0]["ingredients"][0]
    # 2 UK pints = 1136 ml >= 300 ml
    assert ing["in_stock"] is True


async def test_get_shopping_list_all_in_stock(db_session: AsyncSession) -> None:
    await crud.create_ingredient(
        db_session, name="red lentils", quantity=500, unit="g",
        source_label="manual", location="pantry", arrived_date=date.today(),
    )
    await tools.plan_meal(
        db_session, name="Dal",
        planned_date=date.today().isoformat(),
        ingredients=[{"name": "red lentils", "quantity": 300, "unit": "g"}],
    )
    result = await tools.get_shopping_list(db_session)
    assert result["count"] == 0
    assert result["shopping_list"] == []


async def test_parse_recipe_from_url_success(db_session: AsyncSession) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_response = MagicMock()
    mock_response.text = "<html><body><h1>Lentil Soup</h1><p>Serves 4. Use 300g red lentils.</p></body></html>"
    mock_response.raise_for_status = MagicMock()

    recipe_data = {
        "name": "Lentil Soup", "servings": 4, "cuisine_tag": "british",
        "ingredients": [{"name": "red lentils", "quantity": 300, "unit": "g"}],
    }
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock()]
    mock_msg.content[0].text = json.dumps(recipe_data)

    mock_client_inst = MagicMock()
    mock_client_inst.messages.create = AsyncMock(return_value=mock_msg)

    mock_http_client = AsyncMock()
    mock_http_client.__aenter__ = AsyncMock(return_value=MagicMock(
        get=AsyncMock(return_value=mock_response)
    ))
    mock_http_client.__aexit__ = AsyncMock(return_value=None)

    with patch("agent.tools._anthropic_client", mock_client_inst), \
         patch("httpx.AsyncClient", return_value=mock_http_client):
        result = await tools.parse_recipe_from_url(db_session, url="https://example.com/recipe")

    assert "recipe" in result
    assert result["recipe"]["name"] == "Lentil Soup"
    assert result["source_url"] == "https://example.com/recipe"


async def test_parse_recipe_from_url_fetch_error(db_session: AsyncSession) -> None:
    from unittest.mock import patch, AsyncMock

    mock_http_client = AsyncMock()
    mock_http_client.__aenter__ = AsyncMock(side_effect=Exception("connection refused"))
    mock_http_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_http_client):
        result = await tools.parse_recipe_from_url(db_session, url="https://bad.example.com")

    assert "error" in result


async def test_get_delivery_schedule_filter_by_source(db_session: AsyncSession) -> None:
    from datetime import UTC, datetime
    now = datetime.now(UTC)
    await crud.create_delivery_schedule(
        db_session, source_label="veg_box", expected_date=date.today(),
        scraped_at=now, raw_json="[]",
    )
    await crud.create_delivery_schedule(
        db_session, source_label="meat_box", expected_date=date.today(),
        scraped_at=now, raw_json="[]",
    )
    result = await tools.get_delivery_schedule(db_session, source_label="veg_box")
    assert len(result["schedules"]) == 1
    assert result["schedules"][0]["source_label"] == "veg_box"
