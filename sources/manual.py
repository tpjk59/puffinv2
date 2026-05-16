"""Manual text input source.

Parses a natural language description of ingredients (e.g.
"500g chicken thighs, fridge, best before Friday") into IngredientArrival
objects using a single-turn Claude call.
"""

import json
from datetime import date

import anthropic

from agent.prompts import MANUAL_SOURCE_PARSE_PROMPT
from sources.base import FoodSource, IngredientArrival

# The model used for manual parsing — lightweight single-turn call
_PARSE_MODEL = "claude-haiku-4-5-20251001"


class ManualSource:
    """Parses natural language ingredient descriptions via the Anthropic API.

    Pass text= to fetch():
        arrivals = await source.fetch(text="2 courgettes, fridge")
    """

    source_label = "manual"

    def __init__(self, client: anthropic.AsyncAnthropic | None = None) -> None:
        # Allow injection of a mock client in tests
        self._client = client or anthropic.AsyncAnthropic()

    async def fetch(self, **kwargs) -> list[IngredientArrival]:
        """Parse kwargs['text'] into a list of IngredientArrival objects.

        Returns an empty list if text is absent or blank.
        Raises ValueError if the LLM response cannot be parsed as JSON.
        """
        text: str = kwargs.get("text", "").strip()
        if not text:
            return []

        today = date.today().isoformat()
        prompt = MANUAL_SOURCE_PARSE_PROMPT.format(today=today, text=text)

        message = await self._client.messages.create(
            model=_PARSE_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = message.content[0].text.strip()

        # Strip markdown code fences if the model adds them despite instructions
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

        try:
            items: list[dict] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ManualSource: LLM returned non-JSON: {raw!r}") from exc

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
                    location=item.get("location", "fresh"),
                    subcategory=item.get("subcategory"),
                    best_before=best_before,
                    notes=item.get("notes"),
                )
            )

        return arrivals

    def describe(self) -> str:
        return (
            "Manual text input: describe purchases in plain English and "
            "Claude will parse them into your inventory."
        )
