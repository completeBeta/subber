"""Per-show terminology, built automatically — no hand-editing.

Why this exists
---------------
`translation.glossary` is one global list, so any term placed there is injected into
every show that gets translated. That is fine for a single series and actively
harmful across a library: the model follows a terminology block closely and
degrades anything missing from a partial list, so Re:Zero's names would leak into
unrelated anime.

This module keys terminology by *show* instead. The identity is the one the scan
already resolves (AniList id, or TMDB id for live action), so the file is created
and maintained by the app:

    <config dir>/glossary/anilist-12345.yaml
        title: Re:Zero kara Hajimeru Isekai Seikatsu
        source: anilist
        names:            # seeded from AniList's character list (native → romaji)
          リア: Lia
          ペテルギウス: Petelgeuse
        terms: {}         # reserved for learned terms (phase 2)
        updated: 2026-10-24T12:00:00+00:00

Resolution rules
----------------
* Show identified  → that show's glossary. The global list is *not* merged in.
* Show unknown     → the global list, as a fallback (previous behaviour).
* Names are only folded into the injected terminology when the caller has no
  character map of its own, so the same name is never rendered twice in a prompt.

Nothing in here asks the operator for input: a missing file is created from the
identity, and an empty file simply injects nothing.
"""

from __future__ import annotations

import datetime as _dt
import os
import threading
from pathlib import Path

import yaml

_LOCK = threading.Lock()


def glossary_dir() -> Path:
    """Where show glossaries live (next to config.yaml, so it rides the volume)."""
    override = os.environ.get("SUBBER_GLOSSARY_DIR")
    if override:
        return Path(override)
    from . import config

    return Path(config.CONFIG_PATH).parent / "glossary"


_LANG_ALIASES = {
    "ja": "ja", "jp": "ja", "jpn": "ja", "japanese": "ja", "日本語": "ja",
    "fr": "fr", "fra": "fr", "fre": "fr", "french": "fr", "français": "fr",
    "zh": "zh", "chi": "zh", "zho": "zh", "cmn": "zh", "chinese": "zh",
    "vi": "vi", "vie": "vi", "vietnamese": "vi",
    "es": "es", "spa": "es", "spanish": "es",
    "pt": "pt", "por": "pt", "portuguese": "pt",
    "de": "de", "deu": "de", "ger": "de", "german": "de",
    "it": "it", "ita": "it", "italian": "it",
    "ko": "ko", "kor": "ko", "korean": "ko",
    "en": "en", "eng": "en", "english": "en",
}


def normalize_lang(source_lang: str | None) -> str:
    """Two-letter code for a source language ('fra'/'French' → 'fr')."""
    raw = (source_lang or "").strip().lower()
    return _LANG_ALIASES.get(raw, raw[:2] or "unknown")


def is_japanese_source(source_lang: str | None) -> bool:
    """True when the source language is Japanese.

    Japanese is the only language whose *names* we can seed from AniList (it lists
    native Japanese names); every other language has to be learned from the text.
    """
    return normalize_lang(source_lang) == "ja"


def _migrate_terms(data: dict) -> bool:
    """Older files stored `terms: {source: target}`; newer ones are per-language."""
    terms = data.get("terms")
    if isinstance(terms, dict) and terms and all(isinstance(v, str) for v in terms.values()):
        data["terms"] = {"ja": dict(terms)}
        return True
    if terms is None:
        data["terms"] = {}
        return True
    return False


def terms_for(
    identity,
    source_lang: str | None = None,
    global_terms=None,
    have_names: bool = False,
) -> list[str]:
    """Terminology lines to inject for this show **in this source language**.

    Terms are stored per language (`terms: {fr: {...}, vi: {...}}`) so a Vietnamese
    run never receives French spellings. AniList names are Japanese-only and are
    folded in only for Japanese sources, and only when the caller has no character
    map of its own (otherwise they arrive through that block).
    """
    data = ensure_for_identity(identity) if identity is not None else {}
    if not data:
        return list(global_terms or [])

    lang = normalize_lang(source_lang)
    merged: dict[str, str] = {}
    for source, target in ((data.get("terms") or {}).get(lang) or {}).items():
        if source and target:
            merged[str(source)] = str(target)
    if lang == "ja" and not have_names:
        for native, target in (data.get("names") or {}).items():
            if native and target:
                merged.setdefault(str(native), str(target))
    return [f"{source} = {target}" for source, target in merged.items()]


def key_for(identity) -> str | None:
    """Stable key for a show: anilist-<id> / tmdb-<id>, or None when unidentified."""
    if identity is None:
        return None
    anilist_id = getattr(identity, "anilist_id", None)
    if anilist_id:
        return f"anilist-{int(anilist_id)}"
    tmdb_id = getattr(identity, "tmdb_id", None)
    if tmdb_id:
        return f"tmdb-{int(tmdb_id)}"
    return None


def title_for(identity) -> str:
    for attr in ("anilist_title_romaji", "anilist_title_en", "tmdb_title", "parsed_title"):
        value = getattr(identity, attr, None)
        if value:
            return str(value)
    return ""


def _path(key: str) -> Path:
    return glossary_dir() / f"{key}.yaml"


def load(key: str) -> dict:
    try:
        with open(_path(key), "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def save(key: str, data: dict) -> None:
    path = _path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)
    tmp.replace(path)


def _names_from(identity) -> dict[str, str]:
    names: dict[str, str] = {}
    for entry in getattr(identity, "characters", None) or []:
        if not isinstance(entry, dict):
            continue
        native = str(entry.get("native") or "").strip()
        full = str(entry.get("full") or "").strip()
        if native and full:
            names[native] = full
    return names


def ensure_for_identity(identity) -> dict:
    """Create the show's glossary on first sight and keep its name list current.

    Returns the on-disk record (empty dict when the show has no stable id).
    """
    key = key_for(identity)
    if not key:
        return {}

    with _LOCK:
        data = load(key)
        changed = False
        if not data:
            data = {
                "title": title_for(identity),
                "source": key.split("-", 1)[0],
                "names": {},
                "terms": {},
            }
            changed = True
        if not data.get("title"):
            data["title"] = title_for(identity)
            changed = True
        names = data.setdefault("names", {})
        for native, full in _names_from(identity).items():
            if names.get(native) != full:
                names[native] = full
                changed = True
        data.setdefault("terms", {})
        if _migrate_terms(data):
            changed = True
        if changed:
            data["updated"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            save(key, data)
        return data


def describe(key: str | None) -> str:
    """One-line summary for logs: 'anilist-1234 Re:Zero (12 names, 0 terms)'."""
    if not key:
        return "none"
    data = load(key)
    if not data:
        return key
    return (
        f"{key} {data.get('title', '')}".strip()
        + f" ({len(data.get('names') or {})} names, {len(data.get('terms') or {})} terms)"
    )
