from ..safewrite import safe_write_subtitle_bytes
"""Addic7ed subtitle provider — web scraping (requires free account cookies)."""

import os
import re
import urllib.parse
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from ..types import SubtitleResult
from .base import ProviderCapabilities, SubtitleProvider

BASE_URL = "https://www.addic7ed.com"


class Addic7edProvider(SubtitleProvider):
    """Scrapes Addic7ed.com for TV show subtitles. Best for TV."""

    def __init__(self, username: str = "", password: str = "", cookies: str = ""):
        super().__init__(ProviderCapabilities(
            name="Addic7ed",
            free=True,
            requires_auth=True,
            supports_hash_search=False,
            supports_name_search=True,
            supports_season_episode=True,
            rate_limit_rps=0.5,
        ))
        self._username = username or os.environ.get("ADDIC7ED_USER", "")
        self._password = password or os.environ.get("ADDIC7ED_PASS", "")
        self._cookies_str = cookies or os.environ.get("ADDIC7ED_COOKIES", "")
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
                "Referer": "https://www.addic7ed.com/",
            },
            timeout=30,
            follow_redirects=True,
        )
        self._authenticated = False

    async def _ensure_auth(self) -> None:
        if self._authenticated:
            return

        if self._cookies_str:
            for pair in self._cookies_str.split(";"):
                if "=" in pair:
                    key, val = pair.strip().split("=", 1)
                    self._client.cookies.set(key, val)
            self._authenticated = True
            return

        if self._username and self._password:
            resp = await self._client.post("/dologin.php", data={
                "username": self._username,
                "password": self._password,
                "Submit": "Log+in",
            })
            if "logout" in resp.text.lower():
                self._authenticated = True
                return

        self._authenticated = True

    async def search(
        self, query: str, language: str = "en",
        season: int | None = None, episode: int | None = None,
    ) -> list[SubtitleResult]:
        await self._ensure_auth()

        search_resp = await self._client.get("/search.php", params={
            "search": query,
            "Submit": "Search",
        })
        soup = BeautifulSoup(search_resp.text, "lxml")

        show_link = None
        for a in soup.select("a[href]"):
            if f"/show/" in a.get("href", "") and query.lower() in a.text.lower():
                show_link = a["href"]
                break

        if not show_link:
            return []

        season_resp = await self._client.get(show_link)
        season_soup = BeautifulSoup(season_resp.text, "lxml")

        if season and episode:
            pattern = re.compile(rf"S0*{season}E0*{episode}", re.IGNORECASE)
            ep_link = None
            for a in season_soup.select("a[href]"):
                if pattern.search(a.text) or pattern.search(a.get("href", "")):
                    ep_link = a["href"]
                    break

            if not ep_link:
                return []

            ep_resp = await self._client.get(ep_link)
            ep_soup = BeautifulSoup(ep_resp.text, "lxml")
            return self._parse_episode_page(ep_soup, language)

        return []

    async def search_by_hash(
        self, video_path: Path, language: str = "en",
    ) -> list[SubtitleResult]:
        return []

    async def download(self, result: SubtitleResult, output_path: Path) -> Path:
        url = result.metadata.get("download_url")
        if not url:
            raise ValueError("Missing download_url in Addic7ed result")

        full_url = urllib.parse.urljoin(BASE_URL, url)
        resp = await self._client.get(full_url)
        resp.raise_for_status()

        if "search.php" in str(resp.url) or len(resp.content) < 100:
            raise RuntimeError("Addic7ed download failed — auth expired? Refresh cookies.")

        safe_write_subtitle_bytes(output_path, resp.content)
        return output_path

    async def close(self) -> None:
        await self._client.aclose()

    def _parse_episode_page(self, soup: BeautifulSoup, language: str) -> list[SubtitleResult]:
        results = []
        for row in soup.select("tr"):
            lang_cell = row.select_one("td.language")
            if not lang_cell:
                continue
            if language != "en" and language.lower() not in lang_cell.text.lower():
                continue

            dl_link = row.select_one("a[href*='download']")
            if not dl_link:
                continue

            filename_cell = row.select_one("td.NewsTitle")
            filename = filename_cell.text.strip() if filename_cell else "Unknown"

            downloads_text = ""
            dls_cell = row.select_one("td:last-child")
            if dls_cell:
                downloads_text = dls_cell.text.strip()

            try:
                downloads = int(re.sub(r"[^0-9]", "", downloads_text) or "0")
            except ValueError:
                downloads = 0

            hi = "hearing impaired" in filename.lower() or "hi" in filename.lower()
            lang_text = lang_cell.text.strip().lower()
            if "english" in lang_text:
                lang_code = "en"
            else:
                lang_code = lang_text[:2]

            results.append(SubtitleResult(
                id=f"addic7ed_{hash(dl_link['href'])}",
                filename=filename if "." in filename else f"{filename}.srt",
                language=lang_code,
                provider="Addic7ed",
                downloads=downloads,
                rating=0.0,
                hearing_impaired=hi,
                release_info="",
                metadata={"download_url": dl_link["href"]},
            ))

        return results
