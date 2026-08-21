"""Ad/credit removal for subtitles.

Strips advertising, donation-request, and fansub group-credit lines from the
intro and outro of a subtitle file (opt-in; off by default). Detection is a
time-window + keyword-pattern filter — deliberately conservative, since it
mutates subtitle content.

Modes:
  - "off"                -> no-op
  - "adverts"            -> strip adverts/donations/engagement-bait
  - "adverts_and_credits" -> also strip fansub group-credit lines
"""

from __future__ import annotations

import re
from pathlib import Path

import pysubs2

MODE_ADVERTS = "adverts"
MODE_ADVERTS_AND_CREDITS = "adverts_and_credits"
_VALID_MODES = (MODE_ADVERTS, MODE_ADVERTS_AND_CREDITS)

# ── Patterns (all matched case-insensitively) ───────────────────────────────
_URL_RE = re.compile(r"https?://|www\.|\b\w+\.(com|net|org|io|tv|me|gg)\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

_ADVERT_PATTERNS: list[str] = [
    r"patreon",
    r"ko-?fi",
    r"buy me a coffee",
    r"donat(e|ing|ion)",
    r"subscrib(e|ed|ing)",
    r"like,? (and )?(share|comment|subscribe|follow)",
    r"support (us|me|our|the (channel|show|team|server))",
    r"thanks for (your )?support",
    r"follow (me|us)( on| at)?",
    r"advertis(e|ement|ing)",
    r"sponsor(ed|s)? (by|:)",
    r"brought to you by",
    r"promo ?code",
    r"discord(\.gg)?",
    r"join (our|the) (discord|server)",
    r"giveaway",
    r"merch(andise)?\b",
    r"check out (my|our)",
]

# Fansub group-credit lines ("Translation by X", "Timing: Y", "[GroupName]").
# Bracket tags require a space-free token so "[HorribleSubs]" matches but
# sound/action cues like "[He laughs]" do not.
_CREDIT_PATTERNS: list[str] = [
    r"\b(subbed|subtitle[sd]?|translat(ed?|ion)|tim(e|ing|ed)|encod(e|ing|ed)"
    r"|typeset(ting)?|edit(ed|ing)?|qc|quality ?check|karaoke"
    r"|song (styling|translation)|styl(e|ing|ed)|check(ed)?) (by|:)\b",
    r"\[[A-Za-z0-9][A-Za-z0-9._-]*\]",
]


def _patterns_for_mode(mode: str) -> list[str]:
    if mode == MODE_ADVERTS_AND_CREDITS:
        return _ADVERT_PATTERNS + _CREDIT_PATTERNS
    return _ADVERT_PATTERNS


def _is_ad(text: str, patterns: list[str]) -> bool:
    """Return True if a subtitle line looks like an advert/credit line."""
    t = text.strip().lower()
    if not t:
        return False
    if _URL_RE.search(t) or _EMAIL_RE.search(t):
        return True
    return any(re.search(p, t) for p in patterns)


def remove_ads(
    input_path: Path,
    output_path: Path | None = None,
    mode: str = MODE_ADVERTS,
    window_seconds: int = 60,
    extra_patterns: list[str] | None = None,
) -> dict:
    """Strip advert/credit lines from the intro & outro of a subtitle file.

    Only lines within the first/last `window_seconds` of the file are
    considered, AND only if they match an advert/credit pattern.

    Returns ``{"removed": int, "kept": int, "lines": [removed text...]}``.
    Writes the cleaned file to `output_path` (or in place when None).
    """
    if mode not in _VALID_MODES:
        return {"removed": 0, "kept": 0, "lines": []}

    try:
        subs = pysubs2.load(str(input_path), encoding="utf-8-sig")
    except Exception:
        return {"removed": 0, "kept": 0, "lines": []}

    events = list(subs.events)
    if not events:
        return {"removed": 0, "kept": 0, "lines": []}

    window_ms = max(0, int(window_seconds)) * 1000
    total_end_ms = max(ev.end for ev in events)  # approximate duration

    patterns = _patterns_for_mode(mode) + list(extra_patterns or [])

    kept: list = []
    removed_lines: list[str] = []
    for ev in events:
        text = ev.plaintext or ""
        in_intro = ev.start < window_ms
        in_outro = ev.end > total_end_ms - window_ms
        if (in_intro or in_outro) and _is_ad(text, patterns):
            removed_lines.append(text)
            continue
        kept.append(ev)

    subs.events = kept
    out = output_path or input_path
    try:
        subs.save(str(out), encoding="utf-8")
    except Exception:
        return {"removed": 0, "kept": len(events), "lines": []}

    return {"removed": len(removed_lines), "kept": len(kept), "lines": removed_lines}
