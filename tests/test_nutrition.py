"""Tests for nutrition/lookup.py — all HTTP calls mocked."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nutrition.lookup import fetch_nutrition, _fetch_usda, _fetch_off


def _mock_response(status: int, json_data: dict) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data
    return r


# ---------------------------------------------------------------------------
# USDA
# ---------------------------------------------------------------------------


async def test_usda_returns_nutrition_for_known_ingredient() -> None:
    payload = {
        "foods": [
            {
                "description": "Zucchini, raw",
                "foodNutrients": [
                    {"nutrientId": 1008, "value": 17.0},
                    {"nutrientId": 1003, "value": 1.2},
                    {"nutrientId": 1079, "value": 1.1},
                ],
            }
        ]
    }
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=_mock_response(200, payload))

    with patch("nutrition.lookup.httpx.AsyncClient", return_value=mock_client):
        result = await _fetch_usda("courgette")

    assert result is not None
    assert result["calories_per_100g"] == 17.0
    assert result["protein_per_100g"] == 1.2
    assert result["fibre_per_100g"] == 1.1
    assert result["source"] == "usda"


async def test_usda_returns_none_when_no_foods() -> None:
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=_mock_response(200, {"foods": []}))

    with patch("nutrition.lookup.httpx.AsyncClient", return_value=mock_client):
        result = await _fetch_usda("xyzzy")

    assert result is None


async def test_usda_returns_none_on_http_error() -> None:
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=_mock_response(500, {}))

    with patch("nutrition.lookup.httpx.AsyncClient", return_value=mock_client):
        result = await _fetch_usda("chicken")

    assert result is None


async def test_usda_uses_sr_legacy_nutrient_ids() -> None:
    """Falls back to SR Legacy nutrient IDs (208, 203, 291)."""
    payload = {
        "foods": [
            {
                "description": "Chicken, raw",
                "foodNutrients": [
                    {"nutrientId": 208, "value": 165.0},
                    {"nutrientId": 203, "value": 31.0},
                    {"nutrientId": 291, "value": 0.0},
                ],
            }
        ]
    }
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=_mock_response(200, payload))

    with patch("nutrition.lookup.httpx.AsyncClient", return_value=mock_client):
        result = await _fetch_usda("chicken thigh")

    assert result["calories_per_100g"] == 165.0
    assert result["protein_per_100g"] == 31.0


# ---------------------------------------------------------------------------
# Open Food Facts
# ---------------------------------------------------------------------------


async def test_off_returns_nutrition_for_known_product() -> None:
    payload = {
        "products": [
            {
                "product_name": "Tinned tomatoes",
                "nutriments": {
                    "energy-kcal_100g": 24.0,
                    "proteins_100g": 1.1,
                    "fiber_100g": 0.9,
                },
            }
        ]
    }
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=_mock_response(200, payload))

    with patch("nutrition.lookup.httpx.AsyncClient", return_value=mock_client):
        result = await _fetch_off("tinned tomatoes")

    assert result is not None
    assert result["calories_per_100g"] == 24.0
    assert result["source"] == "open_food_facts"


async def test_off_returns_none_when_no_products() -> None:
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=_mock_response(200, {"products": []}))

    with patch("nutrition.lookup.httpx.AsyncClient", return_value=mock_client):
        result = await _fetch_off("xyzzy")

    assert result is None


# ---------------------------------------------------------------------------
# fetch_nutrition — USDA first, OFF fallback
# ---------------------------------------------------------------------------


async def test_fetch_nutrition_uses_usda_first() -> None:
    usda_data = {
        "calories_per_100g": 17.0, "protein_per_100g": 1.2,
        "fibre_per_100g": 1.1, "source": "usda", "food_name": "Courgette",
    }
    with patch("nutrition.lookup._fetch_usda", AsyncMock(return_value=usda_data)), \
         patch("nutrition.lookup._fetch_off", AsyncMock(return_value=None)) as mock_off:
        result = await fetch_nutrition("courgette")

    assert result["source"] == "usda"
    mock_off.assert_not_called()


async def test_fetch_nutrition_falls_back_to_off() -> None:
    off_data = {
        "calories_per_100g": 55.0, "protein_per_100g": 2.0,
        "fibre_per_100g": 3.5, "source": "open_food_facts", "food_name": "Marmite",
    }
    with patch("nutrition.lookup._fetch_usda", AsyncMock(return_value=None)), \
         patch("nutrition.lookup._fetch_off", AsyncMock(return_value=off_data)):
        result = await fetch_nutrition("marmite")

    assert result["source"] == "open_food_facts"


async def test_fetch_nutrition_returns_none_when_both_fail() -> None:
    with patch("nutrition.lookup._fetch_usda", AsyncMock(return_value=None)), \
         patch("nutrition.lookup._fetch_off", AsyncMock(return_value=None)):
        result = await fetch_nutrition("xyzzy123")

    assert result is None
