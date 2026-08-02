"""Subtitle provider abstraction layer."""
from .base import SubtitleProvider
from .registry import ProviderRegistry

__all__ = ["SubtitleProvider", "ProviderRegistry"]
