"""OpenSubtitles subtitle provider.

Two auth modes:
  - .org VIP auth: username+password, 1,000 downloads/day
  - .com API key: API consumer key, configurable daily limit (free=5, packages up to 40,000/day)
"""

import hashlib
import json
import os
import struct
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..types import SubtitleResult
from .base import ProviderCapabilities, SubtitleProvider

API_BASE = "https://api.opensubtitles.com/api/v1"
USER_AGENT = "Subber/0.4.0"
USAGE_FILE = Path("/app/data/opensubtitles_usage.json")


class RateLimitError(Exception):
    """Raised when daily download limit is exceeded."""
    pass


class OpenSubtitlesProvider(SubtitleProvider):
    """Provider for OpenSubtitles REST API (works with both .org and .com accounts).

    Auth modes:
      - org VIP: username+password login, 1,000 downloads/day
      - com API: consumer API key, configurable limit (free=5, paid packages available)

    The .org and .com share the same REST API at api.opensubtitles.com.
    VIP members from .org authenticate with username/password.
    API consumers from .com use API keys with purchased packages.
    """

    def __init__(self, api_key: str = "", username: str = "", password: str = "",
                 tier: str = "free", daily_limit: int = 0):
        super().__init__(ProviderCapabilities(
            name="OpenSubtitles",
            free=(tier == "free"),
            requires_auth=True,
            supports_hash_search=True,
            supports_name_search=True,
            supports_season_episode=True,
            rate_limit_rps=1.0,
            fallback=True,  # only searched if primary providers find nothing
        ))
        self.api_key = api_key or os.environ.get("OPENSUBTITLES_API_KEY", "")
        self.username = username or os.environ.get("OPENSUBTITLES_USER", "")
        self.password = password or os.environ.get("OPENSUBTITLES_PASS", "")
        self.tier = tier
        # daily_limit: explicit override takes priority, else tier-based default
        if daily_limit > 0:
            self._daily_limit = daily_limit
        elif tier == "lite":
            self._daily_limit = 2000   # Light plan = 2,000/day
        elif tier == "startup":
            self._daily_limit = 5000   # Startup plan = 5,000/day
        elif tier == "basic":
            self._daily_limit = 15000  # Basic plan = 15,000/day
        elif tier == "premium":
            self._daily_limit = 50000  # Premium plan = 50,000/day
        elif tier == "pro":
            self._daily_limit = 100000 # Pro plan = 100,000/day
        else:
            self._daily_limit = 5      # free tier
        self._token: str | None = None
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            headers={"User-Agent": USER_AGENT, "Api-Key": self.api_key, "Accept": "application/json"},
            timeout=30, follow_redirects=True,
        )

    # ── Rate limiting ──

    def _load_usage(self) -> dict:
        """Load daily usage counters. Returns {date_str: count}."""
        try:
            if USAGE_FILE.exists():
                return json.loads(USAGE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def _save_usage(self, usage: dict) -> None:
        """Save daily usage counters, pruning stale dates."""
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Keep only last 7 days
        usage = {k: v for k, v in usage.items() if k >= _days_ago(7)}
        USAGE_FILE.write_text(json.dumps(usage, indent=2))

    def _check_rate_limit(self):
        """Raise RateLimitError if daily limit exceeded."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        usage = self._load_usage()
        count = usage.get(today, 0)
        if count >= self._daily_limit:
            raise RateLimitError(
                f"OpenSubtitles {self.tier} tier daily limit reached "
                f"({count}/{self._daily_limit}). "
                f"Upgrade to VIP (1,000/day) or purchase an API package at opensubtitles.com."
            )

    def _record_download(self):
        """Increment today's usage counter."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        usage = self._load_usage()
        usage[today] = usage.get(today, 0) + 1
        self._save_usage(usage)

    def get_usage(self) -> dict:
        """Return current usage stats for the UI."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        usage = self._load_usage()
        return {
            "tier": self.tier,
            "daily_limit": self._daily_limit,
            "used_today": usage.get(today, 0),
            "remaining": max(0, self._daily_limit - usage.get(today, 0)),
        }

    # ── Auth ──

    async def _maybe_login(self) -> None:
        if not self.username or not self.password or self._token:
            return
        resp = await self._client.post("/login", json={
            "username": self.username,
            "password": self.password,
        })
        resp.raise_for_status()
        self._token = resp.json()["token"]

    # ── Search ──

    def _rate_limited(self) -> bool:
        """Return True if daily limit is already exceeded (skip API call)."""
        try:
            self._check_rate_limit()
            return False
        except RateLimitError:
            return True

    async def search(
        self, query: str, language: str = "en",
        season: int | None = None, episode: int | None = None,
    ) -> list[SubtitleResult]:
        # Don't waste an API call if we're already over the daily limit
        if self._rate_limited():
            return []
        await self._maybe_login()
        params: dict = {"query": query, "languages": language}
        if season is not None:
            params["season_number"] = season
        if episode is not None:
            params["episode_number"] = episode

        resp = await self._client.get("/subtitles", params=params)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                body = resp.json()
                detail = body.get("message", "") or json.dumps(body)
            except Exception:
                detail = resp.text[:300]
            raise httpx.HTTPStatusError(
                f"{e}\nOpenSubtitles API response: {detail}",
                request=e.request, response=e.response,
            ) from e
        return self._format_results(resp.json().get("data", []))

    async def search_by_hash(
        self, video_path: Path, language: str = "en",
    ) -> list[SubtitleResult]:
        if self._rate_limited():
            return []
        await self._maybe_login()
        file_hash = _compute_moviehash(video_path)
        file_size = video_path.stat().st_size

        resp = await self._client.get("/subtitles", params={
            "moviehash": file_hash,
            "moviebytesize": file_size,
            "languages": language,
        })
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                body = resp.json()
                detail = body.get("message", "") or json.dumps(body)
            except Exception:
                detail = resp.text[:300]
            raise httpx.HTTPStatusError(
                f"{e}\nOpenSubtitles API response: {detail}",
                request=e.request, response=e.response,
            ) from e
        return self._format_results(resp.json().get("data", []))

    # ── Download ──

    async def download(self, result: SubtitleResult, output_path: Path) -> Path:
        self._check_rate_limit()
        await self._maybe_login()

        file_id = int(result.metadata["file_id"])
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        resp = await self._client.post("/download", json={"file_id": file_id}, headers=headers)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            # Surface the API's actual error body — OpenSubtitles hides the real
            # reason (e.g. quota exceeded) behind generic 406/404 status codes
            detail = ""
            try:
                body = resp.json()
                detail = body.get("message", "")
                if not detail:
                    detail = json.dumps(body)
            except Exception:
                detail = resp.text[:300]
            raise httpx.HTTPStatusError(
                f"{e}\nOpenSubtitles API response: {detail}",
                request=e.request, response=e.response,
            ) from e
        data = resp.json()

        download_url = data.get("link") or data.get("file_name")
        if download_url and download_url.startswith("http"):
            dl_resp = await self._client.get(download_url)
            dl_resp.raise_for_status()
            output_path.write_bytes(dl_resp.content)
        else:
            import base64
            content = base64.b64decode(data.get("file", ""))
            output_path.write_bytes(content)

        self._record_download()
        return output_path

    async def close(self) -> None:
        await self._client.aclose()

    # ── Formatting ──

    def _format_results(self, raw: list[dict]) -> list[SubtitleResult]:
        results = []
        for r in raw:
            attrs = r.get("attributes", {})
            results.append(SubtitleResult(
                id=f"opensubtitles_{r['id']}",
                filename=attrs.get("filename", "unknown"),
                language=attrs.get("language", "?"),
                provider="OpenSubtitles",
                downloads=attrs.get("download_count", 0),
                rating=float(attrs.get("rating", 0)),
                hearing_impaired=attrs.get("hearing_impaired", False),
                release_info=attrs.get("release", ""),
                metadata={"file_id": r["id"]},
            ))
        return results


# ── Helpers ──

def _days_ago(n: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def _compute_moviehash(path: Path) -> str:
    file_size = path.stat().st_size
    hash_size = 65536

    with open(path, "rb") as f:
        head = f.read(hash_size)
        if file_size > hash_size:
            f.seek(-hash_size, os.SEEK_END)
        tail = f.read(hash_size)

    combined = head + tail
    long_long_size = struct.unpack("Q", struct.pack("q", file_size))[0]
    return f"{hashlib.md5(combined).hexdigest()}{long_long_size:x}"
