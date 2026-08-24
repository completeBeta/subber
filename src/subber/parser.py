"""Subtitle format parser — reads SRT, ASS, VTT into structured data."""

import re
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


# Token → ISO 639-1 code for the language markers providers put in filenames.
# The download path uses this (plus a content sample) instead of guessing from
# the last filename token, because SubDL names subs like "…English.EN.zip.ass" —
# the last token ("zip") is a packaging/quality tag, not a language.
LANG_TOKEN_TO_CODE = {
    "en": "en", "eng": "en", "english": "en", "englishcc": "en",
    "ja": "ja", "jap": "ja", "jpn": "ja", "japanese": "ja",
    "fr": "fr", "fra": "fr", "fre": "fr", "french": "fr",
    "de": "de", "ger": "de", "deu": "de", "german": "de",
    "es": "es", "spa": "es", "spanish": "es",
    "it": "it", "ita": "it", "italian": "it",
    "pt": "pt", "por": "pt", "portuguese": "pt", "pt-br": "pt", "ptbr": "pt",
    "zh": "zh", "chi": "zh", "zho": "zh", "chinese": "zh", "cn": "zh",
    "zh-cn": "zh", "zhcn": "zh", "zh-tw": "zh", "zhtw": "zh",
    "ko": "ko", "kor": "ko", "korean": "ko",
    "ru": "ru", "rus": "ru", "russian": "ru",
    "ar": "ar", "ara": "ar", "arabic": "ar",
    "nl": "nl", "dut": "nl", "nld": "nl", "dutch": "nl",
    "pl": "pl", "pol": "pl", "polish": "pl",
    "tr": "tr", "tur": "tr", "turkish": "tr",
    "sv": "sv", "swe": "sv", "swedish": "sv",
    "no": "no", "nor": "no", "norwegian": "no",
    "da": "da", "dan": "da", "danish": "da",
    "fi": "fi", "fin": "fi", "finnish": "fi",
    "el": "el", "gre": "el", "ell": "el", "greek": "el",
    "he": "he", "heb": "he", "hebrew": "he",
    "hi": "hi", "hin": "hi", "hindi": "hi",
    "th": "th", "tha": "th", "thai": "th",
    "vi": "vi", "vie": "vi", "vietnamese": "vi",
    "id": "id", "ind": "id", "indonesian": "id",
    "ms": "ms", "msa": "ms", "malay": "ms",
    "cs": "cs", "cze": "cs", "ces": "cs", "czech": "cs",
    "hu": "hu", "hun": "hu", "hungarian": "hu",
    "ro": "ro", "rum": "ro", "ron": "ro", "romanian": "ro",
    "uk": "uk", "ukr": "uk", "ukrainian": "uk",
    "bg": "bg", "bul": "bg", "bulgarian": "bg",
    "ca": "ca", "cat": "ca", "catalan": "ca",
    "fa": "fa", "per": "fa", "fas": "fa", "persian": "fa", "farsi": "fa",
    "tl": "tl", "fil": "tl", "tagalog": "tl", "filipino": "tl",
    "sr": "sr", "srp": "sr", "serbian": "sr",
    "hr": "hr", "hrv": "hr", "croatian": "hr",
    "sk": "sk", "slk": "sk", "slovak": "sk",
    "sl": "sl", "slv": "sl", "slovenian": "sl",
}


def lang_from_filename(name: str) -> str | None:
    """Extract a language code from a filename's tokens, ignoring packaging /
    quality tags ('zip', '720p', 'x264', …) that are not languages."""
    for token in re.split(r"[^a-zA-Z0-9]+", name.lower()):
        if token in LANG_TOKEN_TO_CODE:
            return LANG_TOKEN_TO_CODE[token]
    return None


def lang_from_content(path: Path, max_lines: int = 25) -> str | None:
    """Sample a few dialogue lines and detect the language. Used only when the
    filename carries no language marker, to confirm the language from the
    actual speech before deciding to translate."""
    lines = [ln.strip() for ln in read_raw_texts(path) if ln and ln.strip()]
    if not lines:
        return None
    sample = "\n".join(lines[:max_lines])[:2000]
    if len(sample) < 20:
        return None
    code = (detect_language(sample) or "").lower()
    return code if code and code not in {"unknown", "und"} else None


def detect_subtitle_language(path: Path) -> str:
    """Detect a subtitle's language: filename marker first, then a content
    sample of a few dialogue lines. Returns an ISO 639-1 code, or 'unknown'
    when neither source is conclusive.

    Never trusts a bare trailing token — SubDL and friends ship names like
    '…English.EN.zip.ass', where 'zip' is packaging, not a language.
    """
    lang = lang_from_filename(path.name)
    if lang:
        return lang
    return lang_from_content(path) or "unknown"
