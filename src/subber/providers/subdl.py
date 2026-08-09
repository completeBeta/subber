"""SubDL subtitle provider — free REST API (subdl.com)."""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..safewrite import safe_write_subtitle_bytes
from ..types import SubtitleResult
from .base import ProviderCapabilities, SubtitleProvider

API_BASE = "https://api.subdl.com/api/v1"
DL_BASE = "https://dl.subdl.com"

# Local cache of SubDL's authoritative download quota, synced from the
# /api/v2/me endpoint (that endpoint does not count against search quota).
USAGE_FILE = Path("/app/data/subdl_usage.json")
_logger = logging.getLogger("subber.providers")

# Tier → daily DOWNLOAD limits (per subdl.com/developers):
#   Free: 50 downloads/day (2,000 searches/day)
#   PRO:  2,000 downloads/day (30,000 searches/day)
DOWNLOAD_LIMITS = {"free": 50, "pro": 2000}


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
        self._download_limit = DOWNLOAD_LIMITS["pro" if pro_mode else "free"]

    # ── Download-quota tracking (authoritative values from SubDL) ──

    async def sync_usage(self) -> dict | None:
        """Fetch authoritative usage from SubDL's /api/v2/me and cache it.

        Returns the downloads entry {"used", "limit", "remaining"} or None if
        the endpoint is unavailable (local cap still applies).
        """
        try:
            resp = await self._client.get(
                "https://api.subdl.com/api/v2/me",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            plan = data.get("plan", {})
            dl = data.get("usage", {}).get("downloads", {})
            used = int(dl.get("used", 0))
            limit = int(dl.get("limit", self._download_limit))
            remaining = int(dl.get("remaining", max(0, limit - used)))
            entry = {
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "used": used,
                "limit": limit,
                "remaining": remaining,
                "is_pro": bool(plan.get("is_pro")),
                "synced_at": datetime.now(timezone.utc).isoformat(),
            }
            self._download_limit = limit
            try:
                USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
                USAGE_FILE.write_text(json.dumps(entry))
            except Exception:
                pass
            return entry
        except Exception as e:
            _logger.debug("SubDL usage sync failed: %s", e)
            return None

    def _cached_usage(self) -> dict | None:
        try:
            entry = json.loads(USAGE_FILE.read_text())
            if entry.get("date") == datetime.now(timezone.utc).strftime("%Y-%m-%d"):
                return entry
        except Exception:
            pass
        return None

    def downloads_remaining(self) -> int:
        """Best-effort remaining downloads today (synced value if fresh)."""
        entry = self._cached_usage()
        if entry and "remaining" in entry:
            return max(0, int(entry["remaining"]))
        # No sync today — assume full limit minus nothing tracked locally
        return self._download_limit

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
        # Respect the daily DOWNLOAD quota — stop at the cap instead of
        # hammering the API (previously only searches were rate-limited).
        if self.downloads_remaining() <= 0:
            raise RuntimeError(
                f"SubDL daily download quota reached ({self._download_limit}/day)"
            )
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
        # Refresh the quota cache after each successful download (best effort).
        try:
            await self.sync_usage()
        except Exception:
            pass
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
