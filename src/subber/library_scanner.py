"""Filesystem scanner for the Library tab.

Walks library paths, classifies files as TV or movie, detects existing
subtitles (embedded + external), and computes fast dedup hashes.
"""

import hashlib
import logging
import os
import re
import subprocess
from pathlib import Path

from . import config as _cfg

logger = logging.getLogger("subber.library_scanner")


# ── Video file detection ──

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".flv"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}

# Patterns for TV show detection
TV_PATTERNS = [
    # S01E01, S01E01E02, s01e01
    re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})", re.IGNORECASE),
    # 1x01, 01x01
    re.compile(r"(\d{1,2})x(\d{1,3})", re.IGNORECASE),
    # Episode 01, Episode.01
    re.compile(r"[Ee]pisode[.\s_-]*(\d{1,3})", re.IGNORECASE),
    # Season 01 Episode 01
    re.compile(r"[Ss]eason[.\s_-]*(\d{1,2})", re.IGNORECASE),
]

# Movie year extraction: Movie.Name.(2024).mkv or Movie.Name.2024.mkv
MOVIE_YEAR_PATTERN = re.compile(r"[\(\.\s_](19\d{2}|20\d{2})[\)\.\s_]")


def is_video_file(path: Path) -> bool:
    """Check if a file is a video based on extension."""
    return path.suffix.lower() in VIDEO_EXTENSIONS


def is_subtitle_file(path: Path) -> bool:
    """Check if a file is a subtitle based on extension."""
    return path.suffix.lower() in SUBTITLE_EXTENSIONS


# ── Media type classification ──

def classify_media(file_path: Path, library_root: Path) -> tuple[str, dict]:
    """Classify a video file as 'tv' or 'movie'.

    Returns (media_type, metadata_dict).

    TV detection heuristics:
    - Path contains "Season XX" folder
    - Filename matches SxxExx pattern
    - Filename matches Episode.NNN pattern

    Movie detection:
    - Folder name contains a year in parentheses: Movie Name (2024)/
    - Filename contains a year: Movie.Name.2024.mkv
    - No TV patterns match
    """
    rel_path = file_path.relative_to(library_root)
    parts = rel_path.parts
    filename = file_path.stem
    full_path_str = str(rel_path)

    # Check for TV patterns in filename
    for pattern in TV_PATTERNS:
        match = pattern.search(filename)
        if match:
            # SxxExx or x pattern → TV
            if "season" in pattern.pattern.lower() or "s" in pattern.pattern.lower()[:3]:
                # Try to extract season + episode
                season_match = re.search(r"[Ss](\d{1,2})", filename, re.IGNORECASE)
                episode_match = re.search(r"[Ee](\d{1,3})", filename, re.IGNORECASE)
                season = int(season_match.group(1)) if season_match else 1
                episode = int(episode_match.group(1)) if episode_match else None
                show_title = _clean_show_title(filename)
                return "tv", {
                    "show_title": show_title,
                    "season": season,
                    "episode": episode,
                }

    # Check for "Season XX" in path
    for part in parts[:-1]:  # exclude filename
        season_match = re.search(r"[Ss]eason[.\s_-]*(\d{1,2})", part, re.IGNORECASE)
        if season_match:
            season = int(season_match.group(1))
            # Try to find episode number in filename
            # Strip CRC/hash patterns first (e.g. [3E5BF53D]) — they can contain
            # "E<digits>" that get mistaken for episode markers
            clean_name = re.sub(r'\[[0-9A-Fa-f]{8}\]', '', filename)
            clean_name = re.sub(r'\([0-9A-Fa-f]{8}\)', '', clean_name)
            ep_match = re.search(r"[Ee](\d{1,3})", clean_name, re.IGNORECASE)
            if not ep_match:
                ep_match = re.search(r"-\s*(\d{1,3})\b", clean_name)  # " - 02" format
            if not ep_match:
                # Last resort: last number in filename (avoids group tags like [Moozzi2])
                nums = re.findall(r"\b(\d{1,3})\b", clean_name)
                ep_match = re.match(r"(\d+)", str(nums[-1])) if nums else None
            episode = int(ep_match.group(1)) if ep_match else None
            # Show title is the parent folder before "Season XX"
            show_idx = parts.index(part)
            show_title = parts[show_idx - 1] if show_idx > 0 else _clean_show_title(filename)
            return "tv", {
                "show_title": _clean_show_title(str(show_title)),
                "season": season,
                "episode": episode,
            }

    # Check for Episode.NNN pattern
    ep_match = re.search(r"[Ee]pisode[.\s_-]*(\d{1,3})", filename, re.IGNORECASE)
    if ep_match:
        episode = int(ep_match.group(1))
        # Use parent folder name as show title, not the filename
        show_title = parts[-2] if len(parts) >= 2 else _clean_show_title(filename)
        return "tv", {
            "show_title": _clean_show_title(str(show_title)),
            "season": 1,
            "episode": episode,
        }

    # No TV patterns → check for movie (year in folder or filename)
    year = None
    movie_title = None

    # Check folder name for year
    if len(parts) > 1:
        folder = parts[-2]  # immediate parent folder
        year_match = MOVIE_YEAR_PATTERN.search(folder)
        if year_match:
            year = int(year_match.group(1))
            movie_title = _clean_movie_title(folder)

    # Check filename for year
    if year is None:
        year_match = MOVIE_YEAR_PATTERN.search(filename)
        if year_match:
            year = int(year_match.group(1))
            movie_title = _clean_movie_title(filename)

    # Default: treat as movie
    if movie_title is None:
        movie_title = _clean_movie_title(filename)

    return "movie", {
        "movie_title": movie_title,
        "movie_year": year,
    }


def _clean_show_title(filename: str) -> str:
    """Extract a clean show title from a filename."""
    # Remove extension, SxxExx patterns, episode numbers, quality tags
    title = re.sub(r"\.[a-z0-9]{2,4}$", "", filename)  # extension
    title = re.sub(r"[._]", " ", title)  # dots/underscores → spaces
    # Strip leading fansub group tags: [Judas], [Reinforce], [LoliHouse], etc.
    title = re.sub(r"^\s*\[[^\]]+\]\s*", "", title)
    # Strip trailing hex hash fragments: [130624A6], [F95A12A5], etc.
    title = re.sub(r"\s*\[[0-9A-Fa-f]{6,12}\]\s*$", "", title)
    title = re.sub(r"\s*\[[0-9A-Fa-f]{6,12}\.[0-9A-Fa-f]{6,12}\]\s*$", "", title)
    title = re.sub(r"[Ss]\d{1,2}[Ee]\d{1,3}.*$", "", title)  # SxxExx onwards
    title = re.sub(r"\d{1,2}x\d{1,3}.*$", "", title)  # 1x01 onwards
    title = re.sub(r"[Ee]pisode[.\s_-]*\d{1,3}.*$", "", title)
    title = re.sub(r"\b(720p|1080p|2160p|480p|SD|HD|UHD|BluRay|BDRip|DVDRip|WEBRip|x264|x265|HEVC|AAC|AC3|FLAC)\b.*$", "", title, flags=re.IGNORECASE)
    title = title.strip(" .-_")
    return title if title else filename


def _clean_movie_title(text: str) -> str:
    """Extract a clean movie title from a filename or folder name."""
    title = re.sub(r"\.[a-z0-9]{2,4}$", "", text)  # extension
    title = re.sub(r"[\(\[\{].*?[\)\]\}]", "", title)  # remove parenthetical groups
    title = re.sub(r"[._]", " ", title)  # dots/underscores → spaces
    title = re.sub(r"\b(19\d{2}|20\d{2})\b.*$", "", title)  # year onwards
    title = re.sub(r"\b(720p|1080p|2160p|480p|SD|HD|UHD|BluRay|BDRip|DVDRip|WEBRip|x264|x265|HEVC|AAC|AC3|FLAC)\b.*$", "", title, flags=re.IGNORECASE)
    title = title.strip(" .-_")
    return title if title else text


# ── File hashing ──

def compute_file_hash(file_path: Path, chunk_size: int = 65536) -> str:
    """Compute SHA256 of the first 64KB of a file for fast dedup."""
    hasher = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            hasher.update(f.read(chunk_size))
        return hasher.hexdigest()
    except (OSError, PermissionError):
        return ""


def get_file_size(file_path: Path) -> int:
    """Get file size in bytes."""
    try:
        return file_path.stat().st_size
    except (OSError, PermissionError):
        return 0


# ── Subtitle detection ──

def find_external_subs(video_path: Path) -> list[dict]:
    """Find external subtitle files next to a video.

    Returns list of {"path": Path, "language": str, "format": str}.
    Language is detected from filename: Show.Name.en.srt → "en",
    Show.Name.srt → "" (unknown).
    """
    stem = video_path.stem
    parent = video_path.parent

    subs = []
    for ext in SUBTITLE_EXTENSIONS:
        # Pattern: filename.lang.ext (e.g. Show.Name.en.srt)
        for sub_path in parent.glob(f"{stem}*{ext}"):
            if sub_path == video_path:
                continue

            # Extract language from filename
            # Show.Name.en.srt → "en", Show.Name.ja.srt → "ja"
            # Show.Name.srt → "" (no lang code)
            sub_stem = sub_path.stem  # "Show.Name.en"
            lang = ""

            # Check if there's a language code between stem and extension
            if sub_stem.startswith(stem):
                suffix = sub_stem[len(stem):].lstrip(".")
                if suffix and len(suffix) <= 5:
                    lang = suffix.lower()
                elif suffix and "." in suffix:
                    # Could be "en.forced" or similar
                    parts = suffix.split(".")
                    if parts[0] and len(parts[0]) <= 5:
                        lang = parts[0].lower()

            subs.append({
                "path": sub_path,
                "language": lang,
                "format": ext.lstrip("."),
            })

    return subs


def probe_embedded_tracks(video_path: str) -> list[dict]:
    """Use ffprobe to detect embedded subtitle tracks.

    Returns list of {"index": int, "language": str, "codec": str, "title": str}.

    Runs synchronously — callers in async context MUST use run_in_executor().
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", "-select_streams", "s",
                video_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return []

        import json
        data = json.loads(result.stdout)
        tracks = []
        for stream in data.get("streams", []):
            tracks.append({
                "index": stream.get("index", 0),
                "language": (stream.get("tags", {}).get("language") or "").lower(),
                "codec": stream.get("codec_name", ""),
                "title": stream.get("tags", {}).get("title", ""),
            })
        return tracks
    except (subprocess.TimeoutExpired, Exception):
        return []


# ── Path traversal protection ──

def validate_library_paths(requested_paths: list[str]) -> list[str]:
    """Filter requested paths to only those within configured library roots.

    Resolves all paths to their canonical form (eliminating symlinks and '..'
    components), then checks each requested path is a subdirectory of at least
    one configured library root.  Paths outside the library roots are silently
    dropped and a warning is logged.

    When no library roots are configured, returns an empty list (deny-by-default).
    """
    library_roots = _cfg.get_section("library").get("paths", [])
    if not library_roots:
        logger.warning("validate_library_paths: no library roots configured - rejecting all paths")
        return []

    resolved_roots = []
    for r in library_roots:
        try:
            resolved_roots.append(Path(r).resolve(strict=False))
        except Exception:
            logger.warning("validate_library_paths: could not resolve root %r", r)

    if not resolved_roots:
        logger.warning("validate_library_paths: no resolved roots available - rejecting all paths")
        return []

    valid = []
    for p in requested_paths:
        try:
            resolved = Path(p).resolve(strict=False)
            inside = False
            for root in resolved_roots:
                try:
                    resolved.relative_to(root)
                    inside = True
                    break
                except ValueError:
                    continue
            if inside:
                valid.append(str(resolved))
            else:
                logger.warning(
                    "validate_library_paths: rejected path %r (resolved: %r) - not within any library root",
                    p, str(resolved),
                )
        except Exception as exc:
            logger.warning("validate_library_paths: rejected path %r - %s", p, exc)

    return valid


# ── Library walker ──

_VID_EXTS = frozenset({".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".webm", ".ts", ".m2ts"})

def _process_one(fp, root, seen_paths, records, incremental, existing_hashes):
    """Process one file: validate, dedup, classify."""
    fps = str(fp)
    if fps in seen_paths:
        return
    seen_paths.add(fps)
    try:
        rp = fp.resolve(strict=False)
        rp.relative_to(root.resolve(strict=False))
    except (ValueError, OSError):
        return
    # Always hash (first 64KB) so the hash persists to the DB for future
    # incremental scans. Without this, "Scan New Only" can never skip files.
    file_hash = None
    try:
        file_hash = compute_file_hash(fp)
    except OSError:
        pass
    if incremental and existing_hashes and file_hash and file_hash in existing_hashes:
        return
    media_type, metadata = classify_media(fp, root)
    record = {
        'file_path': str(fp),
        'file_hash': file_hash,
        'media_type': media_type,
        'file_size': fp.stat().st_size if fp.exists() else 0,
        **metadata,
        'subtitle_status': 'unknown',
        'status': 'pending',
    }
    records.append(record)


def scan_library(
    library_paths: list[str],
    incremental: bool = False,
    existing_hashes: set[str] | None = None,
    should_abort=None,
) -> list[dict]:
    """Walk library paths and return a list of video file records.

    Each record contains: file_path, file_hash, file_size, media_type,
    and TV/movie metadata.

    Args:
        library_paths: List of root directories to scan.
        incremental: If True, skip files whose hash is in existing_hashes.
        existing_hashes: Set of file hashes already in the DB (for incremental scans).
        should_abort: Optional callback; when it returns True the walk stops
            early and returns whatever has been collected so far. Used to make
            pause/cancel work during the (otherwise silent) filesystem walk.

    Returns list of file record dicts.
    """
    if existing_hashes is None:
        existing_hashes = set()

    # Validate paths against configured library roots (path-traversal protection)
    library_paths = validate_library_paths(library_paths)
    if not library_paths:
        logger.warning("scan_library: no valid paths remaining after validation")

    records = []
    seen_paths = set()

    def _abort():
        return should_abort is not None and should_abort()

    for root_path_str in library_paths:
        if _abort():
            break
        root = Path(root_path_str)
        if not root.exists():
            continue

        # Use os.scandir for CIFS performance (avoids stat overhead of glob/rglob)
        _WALKED = 0
        for show_entry in os.scandir(root):
            if _abort():
                return records
            # Video files directly in the library root (e.g. loose movies)
            if show_entry.is_file() and Path(show_entry.path).suffix.lower() in _VID_EXTS:
                _WALKED += 1
                _process_one(Path(show_entry.path), root, seen_paths, records, incremental, existing_hashes)
                continue
            if not show_entry.is_dir():
                continue
            show_path = Path(show_entry.path)
            for season_entry in os.scandir(show_path):
                if _abort():
                    return records
                if season_entry.is_dir():
                    for file_entry in os.scandir(Path(season_entry.path)):
                        if _abort():
                            return records
                        if file_entry.is_file() and Path(file_entry.path).suffix.lower() in _VID_EXTS:
                            _WALKED += 1
                            _process_one(Path(file_entry.path), root, seen_paths, records, incremental, existing_hashes)
                elif season_entry.is_file() and Path(season_entry.path).suffix.lower() in _VID_EXTS:
                    _WALKED += 1
                    _process_one(Path(season_entry.path), root, seen_paths, records, incremental, existing_hashes)

    return records
