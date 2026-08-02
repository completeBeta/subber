"""Show identification — AniList (anime) + TMDB (movies/TV).

Queries AniList GraphQL API and TMDB REST API to resolve show titles
from messy filenames into canonical metadata including IDs that
subtitle providers can use for high-accuracy searches.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
from .logsanitize import sanitize_log

_log = logging.getLogger("subber.identify")

# ── Rate limiter + cache ───────────────────────────────────

# AniList rate limit: 90 requests/min unauthenticated
# Safe default: 1 req per 670ms (89/min, well under limit)

class _RateLimiter:
    """Token-bucket rate limiter for API calls."""

    def __init__(self, interval: float = 0.67):
        self._interval = interval
        self._last: float = 0.0

    async def acquire(self):
        """Wait until a token is available."""
        now = time.monotonic()
        wait = self._last + self._interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last = time.monotonic()


_anilist_limiter = _RateLimiter(interval=0.67)
_tmdb_limiter = _RateLimiter(interval=0.25)  # TMDB: 40 req/10s

# In-memory cache: normalized_title → ShowIdentity
_cache: dict[str, ShowIdentity] = {}

# ── Types ──────────────────────────────────────────────

@dataclass
class ShowIdentity:
    """Canonical show metadata resolved from a filename."""
    # From AniList
    anilist_id: Optional[int] = None
    anilist_title_en: Optional[str] = None
    anilist_title_romaji: Optional[str] = None
    anilist_synonyms: list[str] = field(default_factory=list)

    # From TMDB
    tmdb_id: Optional[int] = None
    tmdb_title: Optional[str] = None
    tmdb_type: Optional[str] = None  # "tv" or "movie"

    # Parsed from filename
    parsed_title: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None

    # Source
    source: str = "unknown"  # "anilist", "tmdb", "filename"

    @property
    def best_title(self) -> str:
        """Best available English title."""
        return self.anilist_title_en or self.tmdb_title or self.parsed_title or "Unknown"

    @property
    def best_id(self) -> tuple[Optional[str], Optional[int]]:
        """Return (id_type, id_value) for provider search."""
        if self.anilist_id:
            return ("anilist", self.anilist_id)
        if self.tmdb_id:
            return (self.tmdb_type or "tmdb", self.tmdb_id)
        return (None, None)

    @property
    def search_terms(self) -> list[str]:
        """Priority-ordered search terms for name-based provider fallback."""
        terms = []
        if self.anilist_title_en:
            terms.append(self.anilist_title_en)
        if self.anilist_title_romaji:
            terms.append(self.anilist_title_romaji)
        for syn in self.anilist_synonyms:
            if syn not in terms:
                terms.append(syn)
        if self.tmdb_title and self.tmdb_title not in terms:
            terms.append(self.tmdb_title)
        if self.parsed_title and self.parsed_title not in terms:
            terms.append(self.parsed_title)
        return terms


# ── Filename parsing ───────────────────────────────────

# Common release group patterns to strip
_RELEASE_GROUP = re.compile(
    r'^\s*\[.*?\]\s*'           # [GroupName]
    r'|^\s*\(.*?\)\s*'          # (GroupName)
)

# Season/Episode patterns
_SE_PATTERNS = [
    re.compile(r'[Ss](\d{1,2})\s*[Ee](\d{1,3})'),   # S01E01
    re.compile(r'[Ss]eason\s*(\d{1,2}).*?[Ee]p?(?:isode)?\s*(\d{1,3})', re.I),
    re.compile(r'\b(\d{1,2})\s*[xX]\s*(\d{1,3})\b'),  # 1x01
    re.compile(r'[Ee]p?(?:isode)?\s*(\d{1,3})'),      # Ep01 / Episode 01
    re.compile(r'\b-\s*(\d{1,3})\b'),                  # - 01
]

# Resolution/year patterns to strip
_RES_PATTERNS = [
    re.compile(r'\b(?:720|1080|2160|480)[pi]\b', re.I),
    re.compile(r'\b\d{3,4}x\d{3,4}\b'),
    re.compile(r'\b(?:BDRip|BRRip|WEBRip|WEB-DL|BluRay|HDTV|DVDRip|HDRip)\b', re.I),
]

# Codec patterns to strip
_CODEC_PATTERNS = [
    re.compile(r'\b(?:x264|x265|HEVC|AVC|AV1|H264|H265)\b', re.I),
    re.compile(r'\b(?:10bit|8bit|10-bit|8-bit|Hi10p)\b', re.I),
    re.compile(r'\b(?:FLAC|AAC|AC3|DTS|Opus|MP3|EAC3|TrueHD)\b', re.I),
]

# CRC/hash patterns
_CRC_PATTERNS = [
    re.compile(r'\[[0-9A-Fa-f]{8}\]'),   # [ABCD1234]
    re.compile(r'\([0-9A-Fa-f]{8}\)'),   # (ABCD1234)
]

# Year pattern for movie detection
_YEAR_PATTERN = re.compile(r'\b(19\d{2}|20\d{2})\b')

# Dual audio / multi-audio markers
_AUDIO_MARKERS = re.compile(
    r'\b(?:Dual[-\s]?Audio|Multi[-\s]?Audio|Dual[-\s]?Lang|Multi[-\s]?Lang)\b',
    re.I,
)


def parse_filename(filepath: str | Path) -> dict:
    """Extract show title, season, and episode from a filename.

    Returns dict with keys: title, season, episode, year, is_movie
    """
    path = Path(filepath)
    name = path.stem  # Strip extension

    # Try parent folder as show title
    parent_title = path.parent.name if path.parent.name not in (
        "Season 01", "Season 02", "Season 1", "Season 2",
        "Specials", "Extras", "OVAs", "Movies",
    ) else None

    # Clean the filename
    cleaned = _clean_name(name)

    result = {"title": None, "season": None, "episode": None, "year": None, "is_movie": False}

    # Try to find season/episode
    for pattern in _SE_PATTERNS:
        m = pattern.search(cleaned)
        if m:
            groups = m.groups()
            if len(groups) == 2 and groups[0] and groups[1]:
                result["season"] = int(groups[0])
                result["episode"] = int(groups[1])
            elif len(groups) == 1 and groups[0]:
                result["episode"] = int(groups[0])
            # Remove the matched pattern from the name to get clean title
            cleaned = cleaned[:m.start()] + " " + cleaned[m.end():]
            break

    # Check for year (movie indicator)
    year_m = _YEAR_PATTERN.search(cleaned)
    if year_m:
        result["year"] = int(year_m.group(1))
        if result["season"] is None and result["episode"] is None:
            result["is_movie"] = True

    # Clean remaining cruft for title
    title = cleaned
    for pattern in _RES_PATTERNS + _CODEC_PATTERNS + _CRC_PATTERNS:
        title = pattern.sub(" ", title)
    title = _AUDIO_MARKERS.sub(" ", title)
    title = re.sub(r'[_\-.]+', ' ', title)
    title = re.sub(r'\s+', ' ', title).strip()

    if title and len(title) > 2:
        result["title"] = title
    elif parent_title:
        result["title"] = parent_title

    return result


def _clean_name(name: str) -> str:
    """Strip release group tags and normalize."""
    name = _RELEASE_GROUP.sub(" ", name).strip()
    return name


# ── AniList GraphQL ────────────────────────────────────

ANILIST_API = "https://graphql.anilist.co"

_ANIME_QUERY = """
query ($search: String) {
  Page(page: 1, perPage: 5) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      title { romaji english }
      synonyms
      format
      episodes
      season
      seasonYear
    }
  }
}
"""


async def _query_anilist(title: str) -> Optional[ShowIdentity]:
    """Query AniList for an anime show by title."""
    # Check cache first
    cache_key = title.lower().strip()
    if cache_key in _cache:
        _log.debug("AniList cache hit: %s", sanitize_log(title))
        return _cache[cache_key]

    # Rate limit
    await _anilist_limiter.acquire()

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                ANILIST_API,
                json={"query": _ANIME_QUERY, "variables": {"search": title}},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

            media_list = data.get("data", {}).get("Page", {}).get("media", [])
            if not media_list:
                return None

            best = media_list[0]
            identity = ShowIdentity(
                source="anilist",
                anilist_id=best["id"],
                anilist_title_en=best["title"].get("english"),
                anilist_title_romaji=best["title"].get("romaji"),
                anilist_synonyms=best.get("synonyms", []) or [],
            )

            # Cache the result
            _cache[cache_key] = identity

            _log.debug("AniList: %s -> %s (id=%s)", sanitize_log(title), identity.best_title, identity.anilist_id)
            return identity

    except Exception as e:
        _log.warning("AniList query failed for '%s': %s", sanitize_log(title), e)
        return None


# ── TMDB REST ──────────────────────────────────────────

TMDB_API = "https://api.themoviedb.org/3"


async def _query_tmdb(title: str, api_key: str) -> Optional[ShowIdentity]:
    """Query TMDB for a show by title."""
    if not api_key:
        return None

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Search both TV and movie
            for media_type, id_field in [("tv", "anilist"), ("movie", "tmdb")]:  # placeholder
                resp = await client.get(
                    f"{TMDB_API}/search/{media_type}",
                    params={
                        "api_key": api_key,
                        "query": title,
                        "language": "en-US",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results", [])
                if not results:
                    continue

                best = results[0]
                identity = ShowIdentity(
                    source="tmdb",
                    tmdb_id=best["id"],
                    tmdb_title=best.get("name") or best.get("title"),
                    tmdb_type=media_type,
                )
                _log.debug("TMDB: %s -> %s (id=%s)", sanitize_log(title), identity.best_title, identity.tmdb_id)
                return identity

        return None

    except Exception as e:
        _log.warning("TMDB query failed for '%s': %s", sanitize_log(title), e)
        return None


# ── Main identification API ────────────────────────────

async def identify(
    filepath: str | Path,
    tmdb_api_key: str = "",
    prefer: str = "anilist",
) -> ShowIdentity:
    """Identify a video file by querying AniList and/or TMDB.

    Args:
        filepath: Path to the video file.
        tmdb_api_key: TMDB API key (optional, v3 auth).
        prefer: Which service to try first ("anilist" or "tmdb").

    Returns:
        ShowIdentity with parsed metadata and API results populated.
    """
    # Step 1: Parse filename
    parsed = parse_filename(filepath)
    title = parsed.get("title")
    identity = ShowIdentity(
        parsed_title=title,
        season=parsed.get("season"),
        episode=parsed.get("episode"),
    )

    if not title:
        return identity

    # Step 2: Try primary source
    if prefer == "anilist":
        result = await _query_anilist(title)
        if result:
            identity.source = result.source
            identity.anilist_id = result.anilist_id
            identity.anilist_title_en = result.anilist_title_en
            identity.anilist_title_romaji = result.anilist_title_romaji
            identity.anilist_synonyms = result.anilist_synonyms
            return identity

        # Fallback to TMDB
        tmdb_result = await _query_tmdb(title, tmdb_api_key)
        if tmdb_result:
            identity.source = tmdb_result.source
            identity.tmdb_id = tmdb_result.tmdb_id
            identity.tmdb_title = tmdb_result.tmdb_title
            identity.tmdb_type = tmdb_result.tmdb_type
            return identity
    else:
        # Prefer TMDB
        result = await _query_tmdb(title, tmdb_api_key)
        if result:
            identity.source = result.source
            identity.tmdb_id = result.tmdb_id
            identity.tmdb_title = result.tmdb_title
            identity.tmdb_type = result.tmdb_type
            return identity

        # Fallback to AniList
        anilist_result = await _query_anilist(title)
        if anilist_result:
            identity.source = anilist_result.source
            identity.anilist_id = anilist_result.anilist_id
            identity.anilist_title_en = anilist_result.anilist_title_en
            identity.anilist_title_romaji = anilist_result.anilist_title_romaji
            identity.anilist_synonyms = anilist_result.anilist_synonyms
            return identity

    # Step 3: Only filename parsing available
    identity.source = "filename"
    return identity


async def identify_batch(
    filepaths: list[str | Path],
    tmdb_api_key: str = "",
    prefer: str = "anilist",
    concurrency: int = 1,  # Safe default: 1 at a time for AniList rate limits
) -> dict[str, ShowIdentity]:
    """Identify multiple files with title deduplication and rate limiting.

    Groups files by parsed title so we only hit AniList/TMDB once
    per unique show, not once per episode. Safe for 1000+ files.
    """
    # Phase 1: Parse all filenames (no API calls)
    parsed: list[tuple[str, dict]] = []
    for fp in filepaths:
        p = parse_filename(fp)
        title = p.get("title", "")
        if title:
            parsed.append((str(fp), p))

    # Phase 2: Group by normalized title
    from collections import defaultdict
    title_groups: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for fp, p in parsed:
        key = p["title"].lower().strip()
        title_groups[key].append((fp, p))

    # Phase 3: Identify each unique title (rate-limited)
    identities: dict[str, ShowIdentity] = {}
    for title_key, group in title_groups.items():
        original_title = group[0][1]["title"]
        identity = await identify(
            original_title, tmdb_api_key=tmdb_api_key, prefer=prefer
        )
        identities[title_key] = identity

    # Phase 4: Apply to all files
    result: dict[str, ShowIdentity] = {}
    for title_key, group in title_groups.items():
        base_identity = identities[title_key]
        for fp, p in group:
            # Copy base identity and add file-specific season/episode
            file_identity = ShowIdentity(
                source=base_identity.source,
                anilist_id=base_identity.anilist_id,
                anilist_title_en=base_identity.anilist_title_en,
                anilist_title_romaji=base_identity.anilist_title_romaji,
                anilist_synonyms=base_identity.anilist_synonyms[:],
                tmdb_id=base_identity.tmdb_id,
                tmdb_title=base_identity.tmdb_title,
                tmdb_type=base_identity.tmdb_type,
                parsed_title=p.get("title"),
                season=p.get("season"),
                episode=p.get("episode"),
            )
            result[fp] = file_identity

    return result
