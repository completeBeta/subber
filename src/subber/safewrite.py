"""Safe file write guards — ensures Subber only writes subtitle files, never videos.

All subtitle download/extraction/translation/sync paths must go through
safe_write_subtitle(). This is enforced at the application level; for
defense-in-depth, mount your media library read-only in docker-compose.
"""

from __future__ import annotations

import logging
from pathlib import Path
from .logsanitize import sanitize_log

_log = logging.getLogger("subber.safewrite")

# Allowed subtitle extensions (lowercase, with dot)
_ALLOWED_EXTENSIONS = frozenset({".srt", ".ass", ".ssa", ".vtt", ".sub", ".txt"})


class UnsafeWriteError(ValueError):
    """Raised when code attempts to write a non-subtitle file or overwrite an existing file."""


def is_safe_subtitle_path(path: Path) -> bool:
    """Check if a path targets a safe subtitle file."""
    return path.suffix.lower() in _ALLOWED_EXTENSIONS


def safe_write_subtitle(path: Path, content: str | bytes, allow_overwrite: bool = False) -> Path:
    """Write subtitle content to path. Refuses non-subtitle files or overwrites.

    Raises UnsafeWriteError if:
      - Extension is not .srt/.ass/.ssa/.vtt
      - Target file already exists and allow_overwrite is False

    allow_overwrite should only be used for files Subber itself created earlier
    in the same pipeline run (e.g. adding an attribution header).

    Returns the path on success.
    """
    if not is_safe_subtitle_path(path):
        raise UnsafeWriteError(
            f"Refusing to write non-subtitle file: {path} "
            f"(extension {path.suffix} not in {sorted(_ALLOWED_EXTENSIONS)})"
        )

    if path.exists() and not allow_overwrite:
        raise UnsafeWriteError(
            f"Refusing to overwrite existing file: {path} "
            f"({path.stat().st_size:,} bytes)"
        )

    path.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)

    _log.info("safe_write: %s (%s bytes)", sanitize_log(path.name), len(content))
    return path


def safe_write_subtitle_bytes(path: Path, data: bytes) -> Path:
    """Write raw bytes as subtitle. Same safety checks as safe_write_subtitle."""
    return safe_write_subtitle(path, data)
