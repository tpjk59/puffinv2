"""Tests for the FastAPI REST endpoints."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client():
    """Async test client with tables created."""
    from db.database import create_all_tables
    await create_all_tables()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_health(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_api_inventory_returns_list(client: AsyncClient) -> None:
    r = await client.get("/api/inventory")
    assert r.status_code == 200
    data = r.json()
    assert "ingredients" in data
    assert "count" in data
    assert isinstance(data["ingredients"], list)


async def test_api_inventory_location_filter(client: AsyncClient) -> None:
    r = await client.get("/api/inventory?location=fridge")
    assert r.status_code == 200
    data = r.json()
    for ing in data["ingredients"]:
        assert ing["location"] == "fridge"


async def test_api_meals_returns_list(client: AsyncClient) -> None:
    r = await client.get("/api/meals")
    assert r.status_code == 200
    data = r.json()
    assert "meals" in data
    assert isinstance(data["meals"], list)


async def test_api_nutrition_summary_today(client: AsyncClient) -> None:
    r = await client.get("/api/nutrition/summary?period=today")
    assert r.status_code == 200
    data = r.json()
    assert "totals" in data
    assert "targets" in data
    assert data["period"] == "today"


async def test_api_nutrition_summary_week(client: AsyncClient) -> None:
    r = await client.get("/api/nutrition/summary?period=week")
    assert r.status_code == 200
    assert r.json()["period"] == "week"


async def test_api_preferences_returns_dict(client: AsyncClient) -> None:
    r = await client.get("/api/preferences")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


async def test_api_delivery_schedule_returns_list(client: AsyncClient) -> None:
    r = await client.get("/api/delivery-schedule")
    assert r.status_code == 200
    assert "schedules" in r.json()


async def test_dashboard_serves_html(client: AsyncClient) -> None:
    r = await client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Puffin" in r.text
