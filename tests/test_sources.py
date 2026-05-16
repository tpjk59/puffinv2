"""Tests for sources: protocol compliance, registry, and ManualSource parsing."""

import json
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from sources.base import FoodSource, IngredientArrival
from sources.manual import ManualSource
import sources.registry as registry


# ---------------------------------------------------------------------------
# FoodSource protocol
# ---------------------------------------------------------------------------


def test_manual_source_satisfies_food_source_protocol() -> None:
    source = ManualSource(client=MagicMock())
    assert isinstance(source, FoodSource)


def test_ingredient_arrival_fields() -> None:
    arrival = IngredientArrival(
        name="courgette",
        quantity=3.0,
        unit="whole",
        source_label="manual",
        arrived_date=date.today(),
        location="fridge",
    )
    assert arrival.name == "courgette"
    assert arrival.location == "fridge"
    assert arrival.best_before is None
    assert arrival.notes is None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_manual_source() -> None:
    sources = registry.list_all()
    assert "manual" in sources


def test_registry_get_known_label() -> None:
    source = registry.get("manual")
    assert source.source_label == "manual"


def test_registry_get_unknown_label_raises() -> None:
    with pytest.raises(KeyError, match="not_a_real_source"):
        registry.get("not_a_real_source")


def test_registry_register_and_retrieve() -> None:
    """Registering a new source makes it retrievable."""

    class DummySource:
        source_label = "_test_dummy"

        async def fetch(self, **kwargs):
            return []

        def describe(self):
            return "Dummy test source"

    dummy = DummySource()
    registry.register(dummy)
    assert registry.get("_test_dummy") is dummy

    # Clean up so other tests are unaffected
    registry._registry.pop("_test_dummy", None)


# ---------------------------------------------------------------------------
# ManualSource — LLM call mocked
# ---------------------------------------------------------------------------


def _make_llm_response(items: list[dict]) -> MagicMock:
    """Build a mock Anthropic message whose content[0].text is a JSON list."""
    msg = MagicMock()
    msg.content = [MagicMock()]
    msg.content[0].text = json.dumps(items)
    return msg


def _make_manual_source(items: list[dict]) -> ManualSource:
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=_make_llm_response(items))
    return ManualSource(client=mock_client)


async def test_manual_source_parses_single_ingredient() -> None:
    source = _make_manual_source([
        {"name": "chicken thighs", "quantity": 500, "unit": "g",
         "location": "fridge", "best_before": None, "notes": None},
    ])
    arrivals = await source.fetch(text="500g chicken thighs, fridge")

    assert len(arrivals) == 1
    a = arrivals[0]
    assert a.name == "chicken thighs"
    assert a.quantity == 500.0
    assert a.unit == "g"
    assert a.location == "fridge"
    assert a.source_label == "manual"
    assert a.best_before is None


async def test_manual_source_parses_multiple_ingredients() -> None:
    source = _make_manual_source([
        {"name": "courgette", "quantity": 2, "unit": "whole",
         "location": "fridge", "best_before": None, "notes": None},
        {"name": "red lentils", "quantity": 500, "unit": "g",
         "location": "pantry", "best_before": None, "notes": None},
    ])
    arrivals = await source.fetch(text="2 courgettes and 500g red lentils")

    assert len(arrivals) == 2
    names = {a.name for a in arrivals}
    assert "courgette" in names
    assert "red lentils" in names


async def test_manual_source_parses_best_before() -> None:
    best_before_str = "2026-05-20"
    source = _make_manual_source([
        {"name": "spinach", "quantity": 150, "unit": "g",
         "location": "fridge", "best_before": best_before_str, "notes": None},
    ])
    arrivals = await source.fetch(text="150g spinach, best before 20th May")

    assert len(arrivals) == 1
    assert arrivals[0].best_before == date(2026, 5, 20)


async def test_manual_source_returns_empty_for_blank_text() -> None:
    source = _make_manual_source([])
    arrivals = await source.fetch(text="")
    assert arrivals == []


async def test_manual_source_returns_empty_when_no_text_kwarg() -> None:
    source = _make_manual_source([])
    arrivals = await source.fetch()
    assert arrivals == []


async def test_manual_source_strips_markdown_fences() -> None:
    """Handles LLM responses that include ```json ... ``` fences."""
    items = [{"name": "aubergine", "quantity": 1, "unit": "whole",
              "location": "fridge", "best_before": None, "notes": None}]

    msg = MagicMock()
    msg.content = [MagicMock()]
    msg.content[0].text = "```json\n" + json.dumps(items) + "\n```"

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=msg)
    source = ManualSource(client=mock_client)

    arrivals = await source.fetch(text="1 aubergine")
    assert len(arrivals) == 1
    assert arrivals[0].name == "aubergine"


async def test_manual_source_raises_on_invalid_json() -> None:
    msg = MagicMock()
    msg.content = [MagicMock()]
    msg.content[0].text = "This is not JSON at all."

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=msg)
    source = ManualSource(client=mock_client)

    with pytest.raises(ValueError, match="non-JSON"):
        await source.fetch(text="some ingredients")


def test_manual_source_describe() -> None:
    source = ManualSource(client=MagicMock())
    desc = source.describe()
    assert isinstance(desc, str)
    assert len(desc) > 0
