"""Provider registry — searches all enabled providers in parallel, fallback-last."""

import asyncio
from pathlib import Path

from ..types import SubtitleResult
from .base import SubtitleProvider
from . import provider_stats


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
        all_results: list[SubtitleResult] = []

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
                        all_results.extend(res)

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
                    all_results.extend(res)

        return _merge_results(all_results)

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
        return result_path

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
