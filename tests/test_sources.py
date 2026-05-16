"""Tests for sources: protocol compliance, registry, ManualSource, CameraSource, WebScraper."""

import json
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from sources.base import FoodSource, IngredientArrival
from sources.camera import CameraSource
from sources.manual import ManualSource
from sources.web_scraper import WebScraper
from sources.veg_box import VegBoxSource
from sources.meat_box import MeatBoxSource
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


# ---------------------------------------------------------------------------
# CameraSource
# ---------------------------------------------------------------------------


def _make_camera_source(items: list[dict]) -> CameraSource:
    msg = MagicMock()
    msg.content = [MagicMock()]
    msg.content[0].text = json.dumps(items)
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=msg)
    return CameraSource(client=mock_client)


def test_camera_source_satisfies_protocol() -> None:
    assert isinstance(CameraSource(client=MagicMock()), FoodSource)


async def test_camera_source_returns_empty_without_image() -> None:
    source = CameraSource(client=MagicMock())
    assert await source.fetch() == []


async def test_camera_source_parses_ingredients() -> None:
    source = _make_camera_source([
        {
            "name": "aubergine", "quantity": 1, "unit": "whole",
            "location": "fridge", "best_before": None, "notes": "confidence:high",
        }
    ])
    arrivals = await source.fetch(image_b64="fakebase64==")
    assert len(arrivals) == 1
    assert arrivals[0].name == "aubergine"
    assert arrivals[0].notes == "confidence:high"
    assert arrivals[0].source_label == "camera"


async def test_camera_source_parses_best_before() -> None:
    source = _make_camera_source([
        {
            "name": "milk", "quantity": 2, "unit": "l",
            "location": "fridge", "best_before": "2026-05-20", "notes": "confidence:high",
        }
    ])
    arrivals = await source.fetch(image_b64="fakebase64==")
    assert arrivals[0].best_before == date(2026, 5, 20)


async def test_camera_source_strips_markdown_fences() -> None:
    items = [{"name": "courgette", "quantity": 2, "unit": "whole",
              "location": "fridge", "best_before": None, "notes": "confidence:medium"}]
    msg = MagicMock()
    msg.content = [MagicMock()]
    msg.content[0].text = "```json\n" + json.dumps(items) + "\n```"
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=msg)
    source = CameraSource(client=mock_client)
    arrivals = await source.fetch(image_b64="fakebase64==")
    assert len(arrivals) == 1


async def test_camera_source_raises_on_invalid_json() -> None:
    msg = MagicMock()
    msg.content = [MagicMock()]
    msg.content[0].text = "I see some vegetables but cannot parse them."
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=msg)
    source = CameraSource(client=mock_client)
    with pytest.raises(ValueError, match="non-JSON"):
        await source.fetch(image_b64="fakebase64==")


def test_camera_source_describe() -> None:
    desc = CameraSource(client=MagicMock()).describe()
    assert isinstance(desc, str) and len(desc) > 0


# ---------------------------------------------------------------------------
# WebScraper (base) + VegBoxSource + MeatBoxSource
# ---------------------------------------------------------------------------


def _make_web_scraper(items: list[dict], url: str = "http://example.com") -> WebScraper:
    """Build a WebScraper with mocked HTTP and LLM clients."""
    # Mock HTTP response
    mock_http_response = MagicMock()
    mock_http_response.raise_for_status = MagicMock()
    mock_http_response.text = "<html><body>Courgette 2, Aubergine 1</body></html>"

    mock_http = MagicMock()
    mock_http.get = AsyncMock(return_value=mock_http_response)

    # Mock LLM response
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock()]
    mock_msg.content[0].text = json.dumps(items)
    mock_llm = MagicMock()
    mock_llm.messages.create = AsyncMock(return_value=mock_msg)

    scraper = WebScraper(url=url, client=mock_llm, http_client=mock_http)
    scraper.source_label = "_test_scraper"
    return scraper


async def test_web_scraper_fetches_and_parses() -> None:
    items = [
        {"name": "courgette", "quantity": 2, "unit": "whole",
         "location": "fridge", "best_before": None, "notes": None},
    ]
    scraper = _make_web_scraper(items)
    arrivals = await scraper.fetch()
    assert len(arrivals) == 1
    assert arrivals[0].name == "courgette"
    assert arrivals[0].source_label == "_test_scraper"


async def test_web_scraper_no_url_raises() -> None:
    scraper = WebScraper(url=None, client=MagicMock())
    scraper.source_label = "_test"
    with pytest.raises(ValueError, match="no URL configured"):
        await scraper.fetch()


async def test_web_scraper_url_override() -> None:
    items = [{"name": "spinach", "quantity": 100, "unit": "g",
              "location": "fridge", "best_before": None, "notes": None}]
    scraper = _make_web_scraper(items, url=None)
    arrivals = await scraper.fetch(url="http://override.example.com")
    assert len(arrivals) == 1


def test_veg_box_source_satisfies_protocol() -> None:
    assert isinstance(VegBoxSource(url="http://example.com", client=MagicMock()), FoodSource)


def test_veg_box_source_label() -> None:
    assert VegBoxSource().source_label == "veg_box"


def test_meat_box_source_label() -> None:
    assert MeatBoxSource().source_label == "meat_box"


def test_veg_box_describe_unconfigured() -> None:
    desc = VegBoxSource().describe()
    assert "not yet configured" in desc


def test_veg_box_describe_configured() -> None:
    desc = VegBoxSource(url="http://myvegbox.example.com").describe()
    assert "configured" in desc and "not yet" not in desc
