"""FoodSource protocol and IngredientArrival dataclass.

All food sources must implement FoodSource. A source produces a list of
IngredientArrival objects which the agent loop persists to the database.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable


@dataclass
class IngredientArrival:
    """A single ingredient arriving into inventory from any source.

    Quantities use metric units (g, kg, ml, l) or count-based units
    (whole, bunch, tin, etc.). Location must be one of: fresh, freezer, pantry.
    """

    name: str
    quantity: float
    unit: str
    source_label: str
    arrived_date: date
    best_before: date | None = None
    location: str = "fresh"
    notes: str | None = None


@runtime_checkable
class FoodSource(Protocol):
    """Protocol that every food source must satisfy.

    Register implementations in sources/registry.py — nowhere else.
    The agent loop and scheduler discover sources via the registry.
    """

    source_label: str  # unique identifier, used in DB records

    async def fetch(self, **kwargs) -> list[IngredientArrival]:
        """Fetch or parse arrivals from this source.

        kwargs are source-specific (e.g. text= for ManualSource,
        image_b64= for CameraSource). Returns an empty list if there
        is nothing to process.
        """
        ...

    def describe(self) -> str:
        """Human-readable description of this source for the agent."""
        ...
