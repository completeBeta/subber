"""Backward-compatible re-export. New code should use providers package directly."""

from .providers.opensubtitles import OpenSubtitlesProvider as OpenSubtitlesClient

__all__ = ["OpenSubtitlesClient"]
