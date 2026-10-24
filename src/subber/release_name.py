"""Turn provider release names into something a human can read.

Providers hand back raw scene filenames — `loop-7-kaime-no-akuyaku-reijou-wa-
moto-tekikoku-de-...`, `[Erai-raws]Loop7-...-05`, `Show.S01E02.WEB.H264-GROUP` —
and the Search tab used to print them verbatim, so every row looked the same and
nothing said which episode it was, or whether it was a whole-season pack.

This module derives, from a filename plus the provider's own `release_name`:
  show      — a readable show title
  season / episode / episodes — what the file actually covers
  is_pack   — True for season packs / batches / complete collections
  quality   — 1080p / WEB / BluRay … when present

It is deliberately conservative: an episode number is only accepted when it sits
where an episode number actually lives (after SxxExx, after "Episode/Ep/E", in
brackets at the end, or as the last dash-separated token). A bare number buried
inside a title (`loop-7-kaime`) is not an episode.
"""

from __future__ import annotations

import re

_PROVIDER_PREFIX = re.compile(
    r"^\s*(?:SUBDL|OS|OPENSUBTITLES|GESTDOWN|PODNAPISI|EMBEDDED)\s*::\s*", re.I
)
_EXT = re.compile(r"\.(?:srt|ass|ssa|vtt|zip|sub)$", re.I)
# S01E02, S01E02E03, S01.E02, S01 E02, S01E02-E04
_SXXEYY = re.compile(
    r"(?<![A-Za-z0-9])S(\d{1,2})[\s._-]?E(\d{1,3})(?:[\s._-]*(?:E|-)\s*(\d{1,3}))?(?![A-Za-z0-9])",
    re.I,
)
_SEASON_WORD = re.compile(r"\b(?:Season|Saison|Staffel|Temporada)\s*(\d{1,2})\b", re.I)
_SEASON_SHORT = re.compile(r"(?<![A-Za-z0-9])S(\d{1,2})(?![0-9A-Za-z])", re.I)
_PACK_WORD = re.compile(
    r"\b(?:Complete|Batch|Pack|Collection|Season\s+\d+|Vol(?:ume)?\.?\s*\d+|"
    r"全話|全集|一括)\b",
    re.I,
)
_EP_WORD = re.compile(r"(?<![A-Za-z0-9])(?:Episode|Ep|E)[\s._-]?(\d{1,3})(?![0-9])", re.I)
# "Episode 1 ~ 13", "Ep 1-12" — a range written after the word, which is a pack.
_EP_RANGE_WORD = re.compile(
    r"(?<![A-Za-z0-9])(?:Episode|Ep|E)[\s._-]?(\d{1,3})\s*[-~–]\s*(\d{1,3})(?![0-9])", re.I
)
_EP_BRACKET = re.compile(r"[\[(（]\s*(\d{1,3})(?:\s*[-~]\s*(\d{1,3}))?\s*(?:v\d)?\s*[\])）]\s*$")
_EP_LAST = re.compile(r"[-\s_](\d{1,3})(?:v\d)?\s*$")
_EP_RANGE = re.compile(r"(?<![A-Za-z0-9])(\d{1,3})\s*[-~]\s*(\d{1,3})(?![0-9])")
_QUALITY = re.compile(
    r"\b(2160p|1080p|720p|480p|WEB[-\s.]?DL|WEBRip|WEB|BluRay|BDRip|BRRip|HDTV|"
    r"DVDRip|x264|x265|H\.?264|H\.?265|HEVC|10bit|AAC|FLAC|DDP?5\.1)\b",
    re.I,
)
_YEAR = re.compile(r"(?<![0-9])(19|20)\d{2}(?![0-9])")
# A trailing "-GROUP" tag: short and attached to the end (…-SKYANiME, -Erai).
# Deliberately length-limited so a hyphenated title word ("…Reijouwa") survives.
_GROUP = re.compile(r"-(?=[A-Za-z0-9_]*[A-Z0-9])[A-Za-z0-9_]{2,15}$")
# Trailing bracket tags that are not episode numbers: "…-01[Crunchyroll]".
_TRAILING_TAG = re.compile(r"\s*[\[(][^\])]*[\])]\s*$")


def _clean_title(text: str) -> str:
    """Readable form of a scene title: separators to spaces, junk trimmed."""
    t = text
    t = re.sub(r"[._]+", " ", t)
    t = t.replace("[", " ").replace("]", " ").replace("(", " ").replace(")", " ")
    t = re.sub(r"\s+", " ", t)
    t = t.strip(" -–—:·")
    # Trailing provider boilerplate ("… S3 E1 English", "… Subtitles", "… SDH").
    while True:
        new = re.sub(r"[\s-]+(?:English|Eng|Subtitles?|Subs?|SDH|Dub(?:bed)?|Multi)\s*$",
                     "", t, flags=re.I)
        if new == t:
            break
        t = new.strip(" -–—:·")
    # All-lowercase titles are provider sloppiness; make them presentable.
    if t and t == t.lower() and len(t.split()) > 1:
        t = " ".join(w[:1].upper() + w[1:] for w in t.split())
    return t


def _strip_noise(text: str) -> str:
    """Remove quality/group/year tokens so they don't end up in the title."""
    t = _QUALITY.sub(" ", text)
    t = _YEAR.sub(" ", t)
    t = re.sub(r"\b(?:WEB|BD|DVD)(?:[-\s]?DL)?\b", " ", t, flags=re.I)
    t = re.sub(r"\s+", " ", t)
    return t.strip(" -–—:·")


def _parse_one(raw: str) -> dict:
    """Parse a single candidate string. Returns {} when nothing is recognisable."""
    if not raw:
        return {}
    text = _PROVIDER_PREFIX.sub("", raw.strip())
    text = _EXT.sub("", text).strip()
    # Drop leading release-group tags ("[Erai-raws]...", "(Group) ...") but never a
    # leading episode bracket, and never a trailing one — "[05]" is an episode.
    text = re.sub(r"^\s*(?:\[[^\]]*\]|\([^)]*\))\s*", "", text).strip()
    # A trailing non-numeric tag ("…-01[Crunchyroll]", "…[1080p]") hides the episode
    # marker underneath it, so peel those off first; "[05]" stays because it is numeric.
    while True:
        m = _TRAILING_TAG.search(text)
        if not m or re.fullmatch(r"[\[(]\s*\d{1,3}(?:\s*[-~]\s*\d{1,3})?\s*[\])]",
                                 text[m.start():].strip()):
            break
        text = text[: m.start()].strip()
    if not text:
        return {}

    season = episode = None
    episodes: list[int] = []
    is_pack = False
    cut = len(text)  # where the title ends

    m = _SXXEYY.search(text)
    if m:
        season = int(m.group(1))
        episode = int(m.group(2))
        episodes = [episode]
        if m.group(3):
            end = int(m.group(3))
            episodes = list(range(episode, end + 1)) if end >= episode else [episode]
        else:
            # S01E02E03 / S01E02-04 style continuation
            tail = text[m.end():]
            extra = re.match(r"[\s._-]*E(\d{1,3})", tail, re.I)
            if extra:
                episodes.append(int(extra.group(1)))
        cut = m.start()
    else:
        m = _SEASON_WORD.search(text)
        if m:
            season = int(m.group(1))
            is_pack = True
            cut = m.start()
        else:
            m = _SEASON_SHORT.search(text)
            if m:
                season = int(m.group(1))
                is_pack = True
                cut = m.start()

    if episode is None:
        # bracket / "Episode 1-13" / trailing-number episode markers
        m = _EP_RANGE_WORD.search(text)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            episodes = list(range(start, end + 1)) if end >= start else [start]
            episode = start
            is_pack = end > start
            cut = min(cut, m.start())
        else:
            m = _EP_WORD.search(text)
            if m:
                episode = int(m.group(1))
                episodes = [episode]
                cut = min(cut, m.start())
            else:
                m = _EP_BRACKET.search(text)
                if m:
                    episode = int(m.group(1))
                    episodes = [episode]
                    if m.group(2):
                        end = int(m.group(2))
                        is_pack = end > episode
                        episodes = list(range(episode, end + 1)) if end >= episode else [episode]
                    cut = min(cut, m.start())
                else:
                    m = _EP_LAST.search(text)
                    if m:
                        episode = int(m.group(1))
                        episodes = [episode]
                        cut = min(cut, m.start())
                    else:
                        m = _EP_RANGE.search(text)
                        if m:
                            start, end = int(m.group(1)), int(m.group(2))
                            if end > start:
                                episodes = list(range(start, end + 1))
                                is_pack = True
                                cut = min(cut, m.start())

    if _PACK_WORD.search(text) and not episodes:
        is_pack = True
    if len(episodes) > 1:
        is_pack = len(episodes) > 1 and (episodes[-1] - episodes[0] + 1) > 1

    title_raw = text[:cut] if cut > 0 else text
    # Folder-style names ("…Episode 1 ~ 13/Show Name") should read as the show:
    # when the string is a path, the last segment is the title, not the folder.
    if "/" in text:
        segments = [seg for seg in text.split("/") if seg.strip()]
        if segments:
            title_raw = segments[-1]
    title = _strip_noise(_GROUP.sub("", _clean_title(title_raw)))
    if not title:
        title = _strip_noise(_clean_title(text))

    quality = ""
    qm = _QUALITY.search(raw) or _QUALITY.search(text)
    if qm:
        quality = qm.group(1).upper().replace("H.264", "H.264")

    if not title:
        return {}
    return {
        "show": title,
        "season": season,
        "episode": episode,
        "episodes": sorted(set(episodes)),
        "is_pack": bool(is_pack),
        "quality": quality,
        # A whole film / OVA tagged FULL or Movie is neither an episode nor a pack.
        "is_feature": bool(
            not episodes and not is_pack
            and re.search(r"\b(?:FULL|Movie|Gekijouban|The Movie)\b", raw, re.I)
        ),
    }


def describe_release(filename: str, release_info: str = "") -> dict:
    """Best readable description of a provider result.

    Prefers whichever string carries an episode/season marker (providers often
    return a romaji name with no numbering plus a scene release name that has it),
    and falls back to the filename for the title.
    """
    primary = _parse_one(release_info)
    secondary = _parse_one(filename)

    def score(d: dict) -> int:
        if not d:
            return -1
        s = 0
        if d.get("season") is not None:
            s += 2
        if d.get("episodes"):
            s += 3
        if d.get("is_pack"):
            s += 1
        if d.get("quality"):
            s += 1
        return s

    chosen = primary if score(primary) >= score(secondary) else secondary
    fallback = secondary or primary
    if not chosen:
        return {
            "show": _strip_noise(_clean_title(_EXT.sub("", filename or ""))) or "Unknown",
            "season": None,
            "episode": None,
            "episodes": [],
            "is_pack": False,
            "is_feature": False,
            "quality": "",
        }
    # A title from the better-parsed candidate, but never an empty one.
    if not chosen.get("show") and fallback:
        chosen["show"] = fallback.get("show", "")
    return chosen


def short_label(info: dict) -> str:
    """`S01E04` / `S01E01–E12` / `Season 1 pack` — the badge next to a result."""
    season = info.get("season")
    eps = info.get("episodes") or []
    if info.get("is_feature"):
        return "Full feature"
    if season == 0 and eps and not info.get("is_pack"):
        return f"Special {eps[0]}"
    if info.get("is_pack"):
        if season is not None and len(eps) > 1:
            return f"S{season:02d} pack · E{eps[0]:02d}–E{eps[-1]:02d}"
        if season is not None:
            return f"Season {season} pack"
        if len(eps) > 1:
            return f"E{eps[0]:02d}–E{eps[-1]:02d} pack"
        return "Season pack"
    if season is not None and eps:
        return f"S{season:02d}E{eps[0]:02d}"
    if eps:
        return f"Episode {eps[0]}"
    if season is not None:
        return f"Season {season}"
    return "Unknown episode"
