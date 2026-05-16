"""Nutrition data lookup — USDA FoodData Central (primary) with Open Food Facts fallback.

Both APIs are free. USDA is better for raw ingredients; OFF covers packaged British products.
Set USDA_API_KEY in .env (defaults to DEMO_KEY which allows ~30 req/hour).
"""

import os
from typing import Optional

import httpx

_USDA_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
_OFF_URL = "https://world.openfoodfacts.org/cgi/search.pl"

# USDA nutrient IDs — Foundation/Survey foods use 1xxx, SR Legacy uses 2xx
_ENERGY_IDS = {1008, 208}
_PROTEIN_IDS = {1003, 203}
_FIBRE_IDS = {1079, 291}


async def fetch_nutrition(ingredient_name: str) -> Optional[dict]:
    """Return per-100g nutrition dict or None if nothing useful found.

    Tries USDA first; falls back to Open Food Facts.
    Dict keys: calories_per_100g, protein_per_100g, fibre_per_100g, source, food_name.
    """
    result = await _fetch_usda(ingredient_name)
    if result:
        return result
    return await _fetch_off(ingredient_name)


async def _fetch_usda(name: str) -> Optional[dict]:
    api_key = os.getenv("USDA_API_KEY", "DEMO_KEY")
    params = {
        "query": name,
        "api_key": api_key,
        "dataType": "Foundation,SR Legacy",
        "pageSize": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(_USDA_URL, params=params)
        if r.status_code != 200:
            return None
        foods = r.json().get("foods", [])
    except Exception:
        return None

    if not foods:
        return None

    nutrients = {n["nutrientId"]: n.get("value") for n in foods[0].get("foodNutrients", [])}

    calories = next((nutrients[k] for k in _ENERGY_IDS if k in nutrients), None)
    if not calories:
        return None

    protein = next((nutrients[k] for k in _PROTEIN_IDS if k in nutrients), None)
    fibre = next((nutrients[k] for k in _FIBRE_IDS if k in nutrients), None)

    return {
        "calories_per_100g": round(float(calories), 1),
        "protein_per_100g": round(float(protein), 1) if protein is not None else None,
        "fibre_per_100g": round(float(fibre), 1) if fibre is not None else None,
        "source": "usda",
        "food_name": foods[0].get("description", name),
    }


async def _fetch_off(name: str) -> Optional[dict]:
    params = {
        "search_terms": name,
        "json": 1,
        "page_size": 1,
        "action": "process",
        "fields": "product_name,nutriments",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(_OFF_URL, params=params)
        if r.status_code != 200:
            return None
        products = r.json().get("products", [])
    except Exception:
        return None

    if not products:
        return None

    n = products[0].get("nutriments", {})
    calories = n.get("energy-kcal_100g") or n.get("energy_100g")
    if not calories:
        return None

    protein = n.get("proteins_100g")
    fibre = n.get("fiber_100g") or n.get("fibre_100g")

    return {
        "calories_per_100g": round(float(calories), 1),
        "protein_per_100g": round(float(protein), 1) if protein is not None else None,
        "fibre_per_100g": round(float(fibre), 1) if fibre is not None else None,
        "source": "open_food_facts",
        "food_name": products[0].get("product_name", name),
    }
