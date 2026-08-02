from ..safewrite import safe_write_subtitle_bytes
"""SubDL subtitle provider — free REST API (subdl.com)."""

import os
from pathlib import Path

import httpx

from ..types import SubtitleResult
from .base import ProviderCapabilities, SubtitleProvider

API_BASE = "https://api.subdl.com/api/v1"
DL_BASE = "https://dl.subdl.com"


class SubDLProvider(SubtitleProvider):
    """Provider for SubDL's free subtitle API."""

    def __init__(self, api_key: str = "", pro_mode: bool = False):
        # PRO: 30000 req/day, Normal: 2000 req/day
        rate_limit = 1.0 if pro_mode else 0.33
        super().__init__(ProviderCapabilities(
            name="SubDL",
            free=not pro_mode,
            requires_auth=True,
            supports_hash_search=False,
            supports_name_search=True,
            supports_season_episode=True,
            rate_limit_rps=rate_limit,
        ))
        self._api_key = api_key or os.environ.get("SUBDL_API_KEY", "")
        self._pro_mode = pro_mode
        self._client = httpx.AsyncClient(timeout=30)

    async def search(
        self, query: str, language: str = "en",
        season: int | None = None, episode: int | None = None,
    ) -> list[SubtitleResult]:
        params: dict = {
            "api_key": self._api_key,
            "film_name": query,
            "languages": language,
            "type": "tv",
        }
        if season is not None:
            params["season_number"] = season
        if episode is not None:
            params["episode_number"] = episode

        resp = await self._client.get(f"{API_BASE}/subtitles", params=params)
        if resp.status_code == 429:
            return []  # rate limited
        resp.raise_for_status()
        data = resp.json()

        if not data.get("status"):
            return []

        return self._format_results(data.get("subtitles", []))

    async def search_by_hash(
        self, video_path: Path, language: str = "en",
    ) -> list[SubtitleResult]:
        """SubDL doesn't support hash search."""
        return []

    async def download(self, result: SubtitleResult, output_path: Path) -> Path:
        dl_url = result.metadata.get("dl_url")
        if not dl_url:
            sd_id = result.metadata.get("sd_id")
            if not sd_id:
                raise ValueError("Missing sd_id/dl_url in SubDL result metadata")
            # Fallback: resolve via API
            info_resp = await self._client.get(f"{API_BASE}/subtitles", params={
                "api_key": self._api_key,
                "subtitle_id": sd_id,
            })
            info_resp.raise_for_status()
            info = info_resp.json()
            subtitles = info.get("subtitles", [])
            if not subtitles:
                raise ValueError(f"SubDL subtitle {sd_id} not found")
            dl_url = subtitles[0].get("url")
            if not dl_url:
                raise ValueError(f"No download URL for SubDL subtitle {sd_id}")

        # Download the actual file
        if not dl_url.startswith("http"): dl_url = f"{DL_BASE}{dl_url}"
        dl_resp = await self._client.get(dl_url)
        dl_resp.raise_for_status()

        # May be a zip — detect via magic bytes (robust to query strings in URL)
        content = dl_resp.content
        if content[:2] == b"PK":
            import io, zipfile
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                # Find first .srt/.ass file
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

    def _format_results(self, raw: list[dict]) -> list[SubtitleResult]:
        results = []
        for r in raw:
            filename = r.get("name", "unknown")
            sd_id = r.get("sd_id") or r.get("subtitle_id")
            dl_url = r.get("url")
            results.append(SubtitleResult(
                id=f"subdl_{sd_id}",
                filename=filename if filename.endswith((".srt", ".ass")) else f"{filename}.srt",
                language=r.get("language", "?"),
                provider="SubDL",
                downloads=int(r.get("downloads", 0)),
                rating=float(r.get("rating", 0)),
                hearing_impaired=r.get("hi", False),
                release_info=r.get("release_name", ""),
                metadata={"sd_id": sd_id, "dl_url": r.get("url")},
            ))
        return results
