"""Meat box source — scrapes a bi-weekly meat box delivery page."""

import os

from sources.web_scraper import WebScraper


class MeatBoxSource(WebScraper):
    """Bi-weekly meat box. URL configured via MEAT_BOX_URL in .env."""

    source_label = "meat_box"

    def __init__(self, **kwargs) -> None:
        url = kwargs.pop("url", None) or os.getenv("MEAT_BOX_URL")
        super().__init__(url=url, **kwargs)

    def describe(self) -> str:
        configured = "configured" if self._url else "not yet configured — set MEAT_BOX_URL in .env"
        return f"Bi-weekly meat box: scrapes your delivery page to log what arrived ({configured})."
