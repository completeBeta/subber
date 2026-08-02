"""Abstract base class for subtitle providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..types import SubtitleResult


@dataclass
class ProviderCapabilities:
    """What a provider can and cannot do."""
    name: str
    free: bool = True
    requires_auth: bool = False
    supports_hash_search: bool = False
    supports_name_search: bool = True
    supports_season_episode: bool = False
    rate_limit_rps: float = 1.0  # requests per second
    fallback: bool = False       # only searched if primary providers find nothing


class SubtitleProvider(ABC):
    """Abstract base for all subtitle providers."""

    def __init__(self, caps: ProviderCapabilities):
        self._caps = caps

    @property
    def name(self) -> str:
        return self._caps.name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._caps

    @abstractmethod
    async def search(
        self, query: str, language: str = "en",
        season: int | None = None, episode: int | None = None,
    ) -> list[SubtitleResult]:
        """Search for subtitles by name/title."""
        ...

    @abstractmethod
    async def search_by_hash(
        self, video_path: Path, language: str = "en",
    ) -> list[SubtitleResult]:
        """Search for subtitles by video file hash. Return empty list if unsupported."""
        ...

    @abstractmethod
    async def download(
        self, result: SubtitleResult, output_path: Path,
    ) -> Path:
        """Download a subtitle file. Returns the path to the saved file."""
        ...

    async def close(self) -> None:
        """Clean up any open connections."""
        pass
