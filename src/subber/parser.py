"""Subtitle format parser — reads SRT, ASS, VTT into structured data."""

from pathlib import Path

import pysubs2

from .types import SubFormat


def detect_format(path: Path) -> SubFormat:
    """Detect subtitle format from file extension."""
    suffix = path.suffix.lower().lstrip(".")
    try:
        return SubFormat(suffix)
    except ValueError:
        # Fallback: try to sniff content
        return _sniff_format(path)


def _sniff_format(path: Path) -> SubFormat:
    """Sniff subtitle format from file contents."""
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        head = f.read(512)
    if "[Script Info]" in head or "[V4" in head:
        return SubFormat.ASS
    if "WEBVTT" in head:
        return SubFormat.VTT
    # Default to SRT
    return SubFormat.SRT


def parse(path: Path) -> list[dict]:
    """
    Parse a subtitle file into a list of entries.
    
    Returns list of dicts with keys: start, end, text, (optional) style, speaker
    """
    fmt = detect_format(path)
    subs = pysubs2.load(str(path), encoding="utf-8-sig")

    entries = []
    for event in subs.events:
        entries.append({
            "start": event.start / 1000,  # ms → seconds
            "end": event.end / 1000,
            "text": event.plaintext,
            "style": event.style,
            "speaker": getattr(event, "name", ""),
        })
    return entries


def detect_language(text: str) -> str:
    """Detect language of a subtitle text sample."""
    from langdetect import detect
    try:
        return detect(text)
    except Exception:
        return "unknown"


def read_raw_texts(path: Path) -> list[str]:
    """Read a subtitle file and return raw text lines (with \\N for line breaks in ASS).

    Falls back to a naive text read on parse failure so a malformed or
    unrecognised subtitle file doesn't 500 the whole upload during
    language auto-detection.
    """
    try:
        subs = pysubs2.load(str(path), encoding="utf-8-sig")
        return [event.plaintext for event in subs.events]
    except Exception:
        try:
            raw = path.read_text(encoding="utf-8-sig", errors="replace")
        except Exception:
            return []
        return [
            ln for ln in (l.strip() for l in raw.splitlines())
            if ln
            and not ln.startswith(("[", ";", "!"))
            and "-->" not in ln
            and not ln.isdigit()
        ]
