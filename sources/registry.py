"""Source registry — the single place where food sources are registered.

Add new sources here and nowhere else. The agent loop, scheduler, and CLI
all discover sources through this registry.
"""

from sources.base import FoodSource

_registry: dict[str, FoodSource] = {}


def register(source: FoodSource) -> None:
    """Register a source by its source_label. Overwrites any existing entry."""
    _registry[source.source_label] = source


def get(label: str) -> FoodSource:
    """Return the source registered under label, or raise KeyError."""
    if label not in _registry:
        raise KeyError(f"No source registered with label '{label}'")
    return _registry[label]


def list_all() -> dict[str, FoodSource]:
    """Return a snapshot of all registered sources keyed by label."""
    return dict(_registry)


# --- built-in source registrations ---
# Import order matters: each import triggers module-level side-effects;
# keep sources that have no inter-source dependencies at the top.

from sources.manual import ManualSource      # noqa: E402
from sources.camera import CameraSource      # noqa: E402
from sources.veg_box import VegBoxSource     # noqa: E402
from sources.meat_box import MeatBoxSource   # noqa: E402

register(ManualSource())
register(CameraSource())
register(VegBoxSource())
register(MeatBoxSource())
