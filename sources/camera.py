"""Camera source — identifies ingredients from a base64-encoded image via Claude vision."""

import json
from datetime import date

import anthropic

from agent.prompts import CAMERA_SOURCE_PROMPT
from sources.base import FoodSource, IngredientArrival

_VISION_MODEL = "claude-sonnet-4-6"


class CameraSource:
    """Identifies food in a photo and returns IngredientArrival candidates.

    Confidence scores are stored in the notes field as "confidence:high",
    "confidence:medium", or "confidence:low". Low-confidence items should
    be presented to the user for confirmation before being saved.

    Pass image_b64= (and optionally media_type=) to fetch():
        arrivals = await source.fetch(image_b64="...", media_type="image/jpeg")
    """

    source_label = "camera"

    def __init__(self, client: anthropic.AsyncAnthropic | None = None) -> None:
        self._client = client or anthropic.AsyncAnthropic()

    async def fetch(self, **kwargs) -> list[IngredientArrival]:
        """Identify ingredients in a base64 image.

        Returns an empty list if image_b64 is absent.
        Raises ValueError if the response cannot be parsed as JSON.
        """
        image_b64: str = kwargs.get("image_b64", "")
        if not image_b64:
            return []
        media_type: str = kwargs.get("media_type", "image/jpeg")

        today = date.today().isoformat()
        prompt = CAMERA_SOURCE_PROMPT.format(today=today)

        message = await self._client.messages.create(
            model=_VISION_MODEL,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )

        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

        try:
            items: list[dict] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"CameraSource: LLM returned non-JSON: {raw!r}") from exc

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
        return (
            "Camera: send a photo of your fridge or shopping and Claude will "
            "identify the ingredients for you to confirm."
        )
