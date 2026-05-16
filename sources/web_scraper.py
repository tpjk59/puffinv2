"""Base class for URL-based food delivery scrapers.

Subclasses (VegBoxSource, MeatBoxSource) inherit fetch() and only need to
set source_label, provide a URL, and implement describe().
"""

import json
from datetime import date
from typing import Any

import anthropic
import httpx

from agent.prompts import WEB_SCRAPER_PARSE_PROMPT
from sources.base import FoodSource, IngredientArrival

_PARSE_MODEL = "claude-haiku-4-5-20251001"
_MAX_CONTENT_CHARS = 8_000  # keep page content under LLM context budget


class WebScraper:
    """Fetches a URL and uses Claude to extract IngredientArrival objects.

    Subclasses must set source_label and implement describe().
    The URL can be passed at construction time or in fetch(url=...).
    """

    source_label: str  # set by subclass

    def __init__(
        self,
        url: str | None = None,
        client: anthropic.AsyncAnthropic | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url
        self._client = client or anthropic.AsyncAnthropic()
        self._http_client = http_client  # injected in tests to avoid real HTTP

    async def _fetch_page(self, url: str) -> str:
        """Fetch page content. Uses injected client in tests, real httpx otherwise."""
        if self._http_client:
            response = await self._http_client.get(url, follow_redirects=True)
        else:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, follow_redirects=True)
        response.raise_for_status()
        # Strip HTML tags crudely — keeps text for the LLM without full BS4 dependency
        text = response.text
        # Remove script/style blocks
        import re
        text = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", " ", text, flags=re.DOTALL | re.IGNORECASE)
        # Strip remaining tags
        text = re.sub(r"<[^>]+>", " ", text)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        return text[:_MAX_CONTENT_CHARS]

    async def fetch(self, **kwargs) -> list[IngredientArrival]:
        """Fetch the configured URL and parse arrivals.

        kwargs:
            url (str): Override the configured URL for this call.

        Raises ValueError if no URL is available.
        Raises ValueError if the LLM response cannot be parsed.
        """
        url = kwargs.get("url") or self._url
        if not url:
            raise ValueError(f"{self.source_label}: no URL configured (set in .env or pass url=)")

        content = await self._fetch_page(url)
        today = date.today().isoformat()
        prompt = WEB_SCRAPER_PARSE_PROMPT.format(
            today=today,
            source_label=self.source_label,
            content=content,
        )

        message = await self._client.messages.create(
            model=_PARSE_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

        try:
            items: list[dict] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{self.source_label}: LLM returned non-JSON: {raw!r}") from exc

        today_date = date.today()
        arrivals: list[IngredientArrival] = []
        for item in items:
            best_before: date | None = None
            if item.get("best_before"):
                best_before = date.fromisoformat(item["best_before"])
            arrivals.append(
                IngredientArrival(
                    name=item["name"],
                    quantity=float(item["quantity"]),
                    unit=item["unit"],
                    source_label=self.source_label,
                    arrived_date=today_date,
                    location=item.get("location", "fridge"),
                    best_before=best_before,
                    notes=item.get("notes"),
                )
            )

        return arrivals

    def describe(self) -> str:
        raise NotImplementedError(f"{self.__class__.__name__} must implement describe()")
