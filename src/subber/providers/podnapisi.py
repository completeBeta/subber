from ..safewrite import safe_write_subtitle_bytes
"""Podnapisi subtitle provider — web scraping (no auth needed)."""

import re
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from ..types import SubtitleResult
from .base import ProviderCapabilities, SubtitleProvider

BASE_URL = "https://www.podnapisi.net"


class PodnapisiProvider(SubtitleProvider):
    """Scrapes Podnapisi.net for subtitles. Free, no auth."""

    def __init__(self):
        super().__init__(ProviderCapabilities(
            name="Podnapisi",
            free=True,
            requires_auth=False,
            supports_hash_search=False,
            supports_name_search=True,
            supports_season_episode=True,
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
        search_query = query
        if season is not None and episode is not None:
            search_query = f"{query} S{season:02d}E{episode:02d}"

        resp = await self._client.get(
            f"{BASE_URL}/subtitles/search/old",
            params={"keywords": search_query, "language": language},
        )
        if resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        return self._parse_results(soup, language)

    async def search_by_hash(
        self, video_path: Path, language: str = "en",
    ) -> list[SubtitleResult]:
        return []

    async def download(self, result: SubtitleResult, output_path: Path) -> Path:
        url = result.metadata.get("download_url")
        if not url:
            raise ValueError("Missing download_url in Podnapisi result")

        resp = await self._client.get(url)
        resp.raise_for_status()
        safe_write_subtitle_bytes(output_path, resp.content)
        return output_path

    async def close(self) -> None:
        await self._client.aclose()

    def _parse_results(self, soup: BeautifulSoup, language: str) -> list[SubtitleResult]:
        results = []
        for row in soup.select("tr.subtitle-entry"):
            dl_link = row.select_one("a[href*='download']")
            if not dl_link:
                continue

            filename = dl_link.text.strip() or "unknown.srt"
            if not filename.endswith((".srt", ".ass")):
                filename += ".srt"

            dls_text = ""
            dls_elem = row.select_one(".downloads")
            if dls_elem:
                dls_text = dls_elem.text.strip()

            try:
                downloads = int(re.sub(r"[^0-9]", "", dls_text) or "0")
            except ValueError:
                downloads = 0

            results.append(SubtitleResult(
                id=f"podnapisi_{hash(dl_link['href'])}",
                filename=filename,
                language=language,
                provider="Podnapisi",
                downloads=downloads,
                rating=0.0,
                hearing_impaired=False,
                metadata={"download_url": dl_link["href"]},
            ))

        return results
