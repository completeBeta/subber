"""Gestdown subtitle provider — Addic7ed proxy with a clean REST API.

Gestdown (https://www.gestdown.info) fronts Addic7ed + SuperSubtitles behind a
JSON API. No account/login required — which replaces the old Addic7ed scraper
that needed browser cookies and broke whenever Addic7ed's HTML changed.

TV shows only (same as Addic7ed). Endpoints (verified live):
  GET /shows/search/{query}                        → show UUIDs + seasons
  GET /subtitles/get/{showId}/{season}/{episode}/{lang} → matching subs
  GET /subtitles/download/{subtitleId}             → the .srt/.ass file

No published daily quota — rate-limits surface as HTTP 429.
"""

import logging
from pathlib import Path

import httpx

from ..safewrite import safe_write_subtitle_bytes
from ..types import SubtitleResult
from .base import ProviderCapabilities, SubtitleProvider

API_BASE = "https://api.gestdown.info"
_logger = logging.getLogger("subber.providers")

# Map our 2-letter language codes to Gestdown's expected codes (both are 2-letter
# ISO, so this is mostly passthrough — kept explicit for clarity/extensibility).
_LANG_MAP = {"en": "en", "ja": "ja", "ko": "ko", "zh": "zh", "fr": "fr",
             "de": "de", "es": "es", "it": "it", "pt": "pt", "ru": "ru",
             "ar": "ar", "pt-br": "pt", "zh-tw": "zh"}


class GestdownProvider(SubtitleProvider):
    """Addic7ed proxy — no auth required."""

    def __init__(self):
        super().__init__(ProviderCapabilities(
            name="Gestdown",
            free=True,
            requires_auth=False,
            supports_hash_search=False,
            supports_name_search=True,
            supports_season_episode=True,
            rate_limit_rps=0.5,  # polite pacing; no published daily quota
        ))
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            headers={"Accept": "application/json", "User-Agent": "Subber/1.0"},
            timeout=30,
            follow_redirects=True,
        )

    async def search(
        self, query: str, language: str = "en",
        season: int | None = None, episode: int | None = None,
    ) -> list[SubtitleResult]:
        lang = _LANG_MAP.get(language, language)

        # 1. Resolve show name → show UUID
        try:
            resp = await self._client.get("/shows/search/" + query)
        except httpx.HTTPError:
            return []
        if resp.status_code == 429:
            return []  # rate limited
        if resp.status_code != 200:
            return []
        shows = resp.json().get("shows", [])
        if not shows:
            return []

        results: list[SubtitleResult] = []
        # Without season/episode, search every returned show's subtitles is too
        # broad; but with S/E we can hit each candidate show. Cap at first 3
        # shows to stay polite.
        for show in shows[:3]:
            show_id = show.get("id")
            show_name = show.get("name", query)
            if not show_id:
                continue
            # If we don't have season/episode, we can't query episodes — skip.
            if season is None or episode is None:
                continue
            results.extend(await self._episode_results(show_id, show_name, season, episode, lang))
        return results

    async def _episode_results(
        self, show_id: str, show_name: str,
        season: int, episode: int, lang: str,
    ) -> list[SubtitleResult]:
        try:
            resp = await self._client.get(
                f"/subtitles/get/{show_id}/{season}/{episode}/{lang}"
            )
        except httpx.HTTPError:
            return []
        if resp.status_code == 429:
            return []
        if resp.status_code != 200:
            return []
        subs = resp.json().get("matchingSubtitles", [])
        out = []
        for s in subs:
            sid = s.get("subtitleId")
            if not sid:
                continue
            version = s.get("version") or ""
            # Build a meaningful filename — Gestdown doesn't return a filename,
            # only a version/release tag + source. Episode-based name keeps it
            # unique and sortable.
            base = f"{show_name}.S{season:02d}E{episode:02d}"
            if version:
                base += f".{version}"
            filename = f"{base}.{lang}.srt"
            out.append(SubtitleResult(
                id=f"gestdown_{sid}",
                filename=filename,
                language=s.get("language") or lang,
                provider="Gestdown",
                downloads=int(s.get("downloadCount", 0) or 0),
                rating=0.0,  # Gestdown doesn't expose a rating
                hearing_impaired=bool(s.get("hearingImpaired", False)),
                release_info=s.get("source") or None,  # e.g. "Addic7ed"
                metadata={"subtitle_id": sid, "download_uri": s.get("downloadUri")},
            ))
        return out

    async def search_by_hash(self, video_path: Path, language: str = "en") -> list[SubtitleResult]:
        return []  # Gestdown doesn't support hash search

    async def download(self, result: SubtitleResult, output_path: Path) -> Path:
        uri = result.metadata.get("download_uri") or f"/subtitles/download/{result.metadata['subtitle_id']}"
        resp = await self._client.get(uri)
        resp.raise_for_status()
        content = resp.content
        # Gestdown returns the file directly (no zip wrapping), but guard anyway.
        if content[:2] == b"PK":
            import io, zipfile
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                for name in zf.namelist():
                    if name.lower().endswith((".srt", ".ass", ".ssa")):
                        content = zf.read(name)
                        if output_path.suffix != Path(name).suffix:
                            output_path = output_path.with_suffix(Path(name).suffix)
                        break
        safe_write_subtitle_bytes(output_path, content)
        return output_path

    async def close(self) -> None:
        await self._client.aclose()
