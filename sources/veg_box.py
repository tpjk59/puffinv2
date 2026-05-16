"""Veg box source — scrapes a weekly veg box delivery page."""

import os

from sources.web_scraper import WebScraper


class VegBoxSource(WebScraper):
    """Weekly veg box. URL configured via VEG_BOX_URL in .env."""

    source_label = "veg_box"

    def __init__(self, **kwargs) -> None:
        url = kwargs.pop("url", None) or os.getenv("VEG_BOX_URL")
        super().__init__(url=url, **kwargs)

    def describe(self) -> str:
        configured = "configured" if self._url else "not yet configured — set VEG_BOX_URL in .env"
        return f"Weekly veg box: scrapes your delivery page to log what arrived ({configured})."
