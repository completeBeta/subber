from ..safewrite import safe_write_subtitle_bytes
"""Subscene subtitle provider — web scraping (no auth needed)."""

import re
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from ..types import SubtitleResult
from .base import ProviderCapabilities, SubtitleProvider

BASE_URL = "https://subscene.com"


class SubsceneProvider(SubtitleProvider):
    """Scrapes Subscene.com for subtitles. Free, no auth."""

    def __init__(self):
        super().__init__(ProviderCapabilities(
            name="Subscene",
            free=True,
            requires_auth=False,
            supports_hash_search=False,
            supports_name_search=True,
            supports_season_episode=False,
            rate_limit_rps=1.0,
        ))
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
            timeout=30,
        )

    async def search(
        self, query: str, language: str = "en",
        season: int | None = None, episode: int | None = None,
    ) -> list[SubtitleResult]:
        resp = await self._client.get(
            f"{BASE_URL}/subtitles/search",
            params={"query": query},
        )
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "lxml")

        search_results = soup.select("div.search-result a")
        if not search_results:
            return []

        first_link = f"{BASE_URL}{search_results[0]['href']}"

        title_resp = await self._client.get(first_link)
        title_soup = BeautifulSoup(title_resp.text, "lxml")

        return self._parse_title_page(title_soup, language)

    async def search_by_hash(
        self, video_path: Path, language: str = "en",
    ) -> list[SubtitleResult]:
        return []

    async def download(self, result: SubtitleResult, output_path: Path) -> Path:
        url = result.metadata.get("download_url")
        if not url:
            raise ValueError("Missing download_url in Subscene result")

        resp = await self._client.get(f"{BASE_URL}{url}")
        resp.raise_for_status()
        safe_write_subtitle_bytes(output_path, resp.content)
        return output_path

    async def close(self) -> None:
        await self._client.aclose()

    def _parse_title_page(self, soup: BeautifulSoup, language: str) -> list[SubtitleResult]:
        results = []
        for row in soup.select("tbody tr"):
            lang_cell = row.select_one("td.a1 span")
            if not lang_cell:
                continue
            if language != "en" and language.lower() not in lang_cell.text.strip().lower():
                continue

            dl_link = row.select_one("td.a1 a[href*='subtitles']")
            if not dl_link:
                continue

            filename = dl_link.text.strip()
            if not filename:
                continue

            filename = re.sub(r"\s*\(.*\)\s*", "", filename).strip()
            if not filename.endswith((".srt", ".ass")):
                filename += ".srt"

            results.append(SubtitleResult(
                id=f"subscene_{hash(dl_link['href'])}",
                filename=filename,
                language=language,
                provider="Subscene",
                downloads=0,
                rating=0.0,
                hearing_impaired="hi" in filename.lower(),
                metadata={"download_url": dl_link["href"]},
            ))

        return results
