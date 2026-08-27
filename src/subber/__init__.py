"""Subber — subtitle grabber and translator powered by LLMs."""

import os

__version__ = "0.8.0"


def version_string() -> str:
    """Human-readable version (v0.6.0), with git SHA when baked in at build time."""
    sha = os.environ.get("SUBBER_GIT_SHA", "").strip()
    if sha and sha != "unknown":
        return f"v{__version__} ({sha})"
    return f"v{__version__}"
