"""Provider registry — searches all enabled providers in parallel, fallback-last."""

import asyncio
import logging
import re
from pathlib import Path

from ..types import SubtitleResult
from .base import SubtitleProvider
from . import provider_stats

_log = logging.getLogger("subber.providers")

_VALID_SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt"}


def _ensure_subtitle_extension(path: Path) -> Path:
    """Return the path with a correct subtitle extension, sniffing the file
    content when the current extension is missing or wrong.

    Provider `filename` fields are often release names with no extension
    (e.g. OpenSubtitles "breaking.bad.s01e01.dvdrip.xvid-orpheus"), which
    breaks Path.suffix and produces extensionless downloads.
    """
    if path.suffix.lower() in _VALID_SUB_EXTS:
        return path
    try:
        from ..parser import detect_format
        ext = f".{detect_format(path).value}"
    except Exception:
        return path
    new_path = path.with_name(path.name + ext)
    try:
        path.rename(new_path)
        return new_path
    except OSError:
        return path


class ProviderRegistry:
    """Manages multiple subtitle providers and searches them in parallel.

    Providers marked as 'fallback' are only searched if primary providers
    return no results. This keeps expensive/rate-limited providers (like
    OpenSubtitles) as a last resort.
    """

    def __init__(self, providers: list[SubtitleProvider] | None = None):
        self._providers: dict[str, SubtitleProvider] = {}
        if providers:
            for p in providers:
                self.add(p)

    def add(self, provider: SubtitleProvider) -> None:
        """Register a provider."""
        self._providers[provider.name] = provider

    def remove(self, name: str) -> None:
        """Remove a provider by name."""
        self._providers.pop(name, None)

    def get(self, name: str) -> SubtitleProvider | None:
        """Get a provider by name."""
        return self._providers.get(name)

    @property
    def names(self) -> list[str]:
        return list(self._providers.keys())

    @property
    def count(self) -> int:
        return len(self._providers)

    def _split_providers(self) -> tuple[list[SubtitleProvider], list[SubtitleProvider]]:
        """Split providers into primary and fallback groups."""
        primary = []
        fallback = []
        for p in self._providers.values():
            if p.capabilities.fallback:
                fallback.append(p)
            else:
                primary.append(p)
        return primary, fallback

    async def search_all(
        self, query: str, language: str = "en",
        season: int | None = None, episode: int | None = None,
        video_path: Path | None = None,
    ) -> list[SubtitleResult]:
        """Search primary providers in parallel. Only search fallback if nothing found."""
        primary, fallback = self._split_providers()

        # Phase 1: search primary providers
        results = await self._search_providers(
            primary, query, language, season, episode, video_path
        )

        if results:
            return results

        # Phase 2: nothing from primary — try fallback providers
        if fallback:
            results = await self._search_providers(
                fallback, query, language, season, episode, video_path
            )

        return results

    async def _search_providers(
        self, providers: list[SubtitleProvider],
        query: str, language: str,
        season: int | None, episode: int | None,
        video_path: Path | None,
    ) -> list[SubtitleResult]:
        """Search a list of providers in parallel. Returns merged + deduped results."""
        hash_results_all: list[SubtitleResult] = []
        name_results_all: list[SubtitleResult] = []

        # Hash search first (faster, more accurate)
        hash_tasks = []
        if video_path and video_path.is_file():
            for p in providers:
                if p.capabilities.supports_hash_search:
                    hash_tasks.append(p.search_by_hash(video_path, language))
                    provider_stats.record_search(p.name)
            if hash_tasks:
                hash_results = await asyncio.gather(*hash_tasks, return_exceptions=True)
                for res in hash_results:
                    if isinstance(res, list):
                        hash_results_all.extend(res)

        # Name search
        name_tasks = []
        for p in providers:
            if p.capabilities.supports_name_search:
                name_tasks.append(p.search(query, language, season, episode))
                provider_stats.record_search(p.name)

        if name_tasks:
            name_results = await asyncio.gather(*name_tasks, return_exceptions=True)
            for res in name_results:
                if isinstance(res, list):
                    name_results_all.extend(res)

        # Relevance filter on NAME-search results: drop clearly-unrelated fuzzy
        # matches (e.g. "Shoushimin Series" returning "A Series of Unfortunate
        # Events"). Hash-search results are exact matches by definition and are
        # always kept. This must run regardless of whether a video file is
        # present — previously it was gated on "no video file", which disabled
        # it entirely for grab-tab uploads (where a file is always present).
        if query:
            name_results_all = _filter_relevance(query, name_results_all)

        all_results = _merge_results(hash_results_all + name_results_all)
        return _filter_spam(all_results)

    async def download(
        self, result: SubtitleResult, output_dir: Path,
    ) -> Path:
        """Download a subtitle using the provider that found it."""
        provider = self._providers.get(result.provider)
        if not provider:
            raise ValueError(f"Provider '{result.provider}' not registered")

        filename = result.filename or f"{result.id}.srt"
        output_path = output_dir / filename
        result_path = await provider.download(result, output_path)
        provider_stats.record_download(result.provider)
        return _ensure_subtitle_extension(result_path)

    async def close(self) -> None:
        """Close all provider connections."""
        for p in self._providers.values():
            await p.close()


def _merge_results(results: list[SubtitleResult]) -> list[SubtitleResult]:
    """Merge results, removing near-duplicates by filename similarity."""
    if len(results) <= 1:
        return results

    results.sort(key=lambda r: r.downloads, reverse=True)

    seen: set[str] = set()
    merged: list[SubtitleResult] = []

    for r in results:
        key = _normalize(r.filename)
        if key not in seen:
            seen.add(key)
            merged.append(r)

    return merged


def _normalize(filename: str) -> str:
    """Normalize filename for deduplication."""
    import re
    base = filename.rsplit(".", 1)[0].lower() if "." in filename else filename.lower()
    base = re.sub(r"[._\- ]+", " ", base).strip()
    return base


# Patterns that mark a provider result as a paid-subtitle scam/advert.
# A real subtitle release name never carries a price, contact detail, or
# these known scam brands. Compiled once at import.
_SPAM_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"₹"), "rupee/price symbol"),
    (re.compile(r"[$€£]\s?\d"), "currency symbol + amount"),
    (re.compile(r"\b(?:usd|inr|eur|gbp)\s*\d", re.I), "currency code + amount"),
    (re.compile(r"@(?:gmail|yahoo|outlook|hotmail|protonmail|icloud)\.com", re.I), "contact email"),
    (re.compile(r"\b(?:whatsapp|telegram|paytm|phonepe|gpay|venmo|cashapp)\b", re.I), "payment/contact app"),
    (re.compile(r"\bget a to z\b", re.I), "known scam brand 'Get A to Z'"),
]


def _filter_spam(results: list[SubtitleResult]) -> list[SubtitleResult]:
    """Drop paid-subtitle scam/advert listings (e.g. 'Get A to Z … for ₹500 only').

    Some providers surface "releases" whose real content is a "contact me to
    buy the full file" note or a gated sample. They accumulate fake download
    counts and outrank genuine subtitles. We scan each result's filename and
    release_info for known spam markers and drop matches, logging each rejection
    so we can diagnose exactly what was filtered.
    """
    kept: list[SubtitleResult] = []
    for r in results:
        haystack = f"{r.filename} {r.release_info or ''}"
        reason = next(
            (why for pat, why in _SPAM_PATTERNS if pat.search(haystack)),
            None,
        )
        if reason:
            _log.warning(
                "Spam filter dropped %r from %s (%s)",
                r.filename, r.provider, reason,
            )
        else:
            kept.append(r)
    return kept


def _filter_relevance(query: str, results: list[SubtitleResult]) -> list[SubtitleResult]:
    """Drop clearly-unrelated name-search results.

    A fuzzy provider match can return something completely unrelated to the
    query (e.g. "Shoushimin Series" → "A Series of Unfortunate Events"). We
    tokenize the query and each result's filename/release and keep only results
    that share a DISTINCTIVE token (length >= 3, not a stopword) with the query.
    If the query has distinctive tokens but NO result shares any, every result
    is unrelated — drop them all rather than return a wrong show's subtitles.
    Hash-search results are handled separately (never filtered) by the caller.
    """
    import re

    _STOP = {
        "the", "and", "for", "not", "you", "are", "with", "that", "this",
        "from", "have", "was", "were", "will", "can", "get", "got",
        "all", "but", "out", "our", "who", "what", "when", "how", "way",
        "one", "two", "episode", "season", "movie", "series", "show",
        "become", "into", "1080p", "720p", "2160p", "480p", "x264", "x265",
        "hevc", "web", "dl", "bluray", "hdtv", "webrip", "bdrip", "dvdrip",
    }

    def tokens(s: str) -> set[str]:
        return {
            t for t in re.split(r"[^a-z0-9]+", s.lower())
            if len(t) >= 3 and t not in _STOP
        }

    q_tokens = tokens(query)
    if not q_tokens:
        return results

    # Relevance = how many query tokens appear in the result.
    def score(r: SubtitleResult) -> int:
        r_tokens = tokens(r.filename) | tokens(r.release_info or "")
        return len(q_tokens & r_tokens)

    scored = [(score(r), r) for r in results]
    if not any(s > 0 for s, _ in scored):
        return []  # nothing shares a distinctive token — all unrelated

    return [r for s, r in scored if s > 0]
