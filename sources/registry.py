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

from sources.manual import ManualSource  # noqa: E402  (import after functions to avoid circularity)

register(ManualSource())

# TODO: register CameraSource, VegBoxSource, MeatBoxSource here once implemented in Phase 2
