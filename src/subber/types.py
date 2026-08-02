"""Shared types and data models."""

import uuid
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class SubFormat(str, Enum):
    SRT = "srt"
    ASS = "ass"
    SSA = "ssa"
    VTT = "vtt"


class SubStatus(str, Enum):
    FOUND = "found"
    DOWNLOADED = "downloaded"
    TRANSLATED = "translated"
    MISSING = "missing"
    SKIPPED = "skipped"


class JobStatus(str, Enum):
    PENDING = "pending"
    TRANSLATING = "translating"
    DONE = "done"
    FAILED = "failed"
    EXPIRED = "expired"


# ── File-oriented types (scanner / downloader) ──

@dataclass
class SubtitleFile:
    """A subtitle file found alongside media."""
    path: Path
    format: SubFormat
    language: str  # ISO 639-1


@dataclass
class MediaTarget:
    """A media file and its subtitle situation."""
    path: Path
    media_type: str = "video"
    existing_subs: list[SubtitleFile] = field(default_factory=list)
    status: SubStatus = SubStatus.MISSING

    @property
    def has_english(self) -> bool:
        return any(s.language == "en" for s in self.existing_subs)

    @property
    def translatable_subs(self) -> list[SubtitleFile]:
        return [s for s in self.existing_subs if s.language != "en"]


# ── Web / job types ──

@dataclass
class TranslationJob:
    """Tracks a translation request through the web UI."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    original_name: str = ""
    source_lang: str = "auto"
    target_lang: str = "en"
    status: JobStatus = JobStatus.PENDING
    input_path: str = ""
    output_path: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    total_chunks: int = 0
    chunks_done: int = 0

    @property
    def progress_pct(self) -> int:
        if self.total_chunks == 0:
            return 0
        return int(self.chunks_done / self.total_chunks * 100)

    @property
    def age_hours(self) -> float:
        return (time.time() - self.created_at) / 3600

    @property
    def is_expired(self) -> bool:
        return self.age_hours > 24

# ── Batch types ──

@dataclass
class BatchJob:
    """Groups multiple TranslationJobs (from a multi-file upload or zip)."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    job_ids: list[str] = field(default_factory=list)
    original_name: str = ""
    source_lang: str = "auto"
    target_lang: str = "en"
    created_at: float = field(default_factory=time.time)
    batch_zip_path: str = ""  # path to generated output zip


# ── Provider result types ──

@dataclass
class SubtitleResult:
    """A subtitle found by a provider."""

    id: str                          # unique ID (provider-prefixed, e.g. "subdl_3197651")
    filename: str                    # e.g. "Breaking.Bad.S01E01.720p.srt"
    language: str                    # ISO 639-1, e.g. "en"
    provider: str                    # provider name, e.g. "SubDL"
    downloads: int = 0
    rating: float = 0.0
    hearing_impaired: bool = False
    release_info: str = ""           # e.g. "BluRay", "WEBRip"
    metadata: dict = field(default_factory=dict)  # provider-specific data for download
