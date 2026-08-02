"""Media scanner — finds video files and their subtitle companions."""

import os
from pathlib import Path

from .types import MediaTarget, SubFormat, SubStatus, SubtitleFile

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm",
    ".wmv", ".flv", ".ts", ".m2ts",
}

SUB_EXTENSIONS = {
    ".srt", ".ass", ".ssa", ".vtt", ".sub", ".sbv",
}

# Common subtitle language markers in filenames
LANG_MARKERS = {
    "en": [".en.", ".eng.", ".english.", "_en.", "_eng."],
    "de": [".de.", ".ger.", ".german.", ".deu.", "_de.", "_ger."],
    "ja": [".ja.", ".jpn.", ".japanese.", "_ja.", "_jpn."],
    "fr": [".fr.", ".fra.", ".french.", "_fr."],
    "es": [".es.", ".spa.", ".spanish.", "_es."],
    "pt": [".pt.", ".por.", ".portuguese.", ".pt-br.", "_pt."],
    "it": [".it.", ".ita.", ".italian.", "_it."],
    "ru": [".ru.", ".rus.", ".russian.", "_ru."],
    "zh": [".zh.", ".chi.", ".chinese.", ".cn.", "_zh.", ".zh-cn."],
    "ar": [".ar.", ".ara.", ".arabic.", "_ar."],
}


def scan_directory(root: Path, recursive: bool = True) -> list[MediaTarget]:
    """
    Scan a directory for video files and their subtitle companions.
    
    Returns a list of MediaTarget objects, one per video file found.
    """
    targets: list[MediaTarget] = []
    videos = _find_videos(root, recursive)

    for video in videos:
        subs = find_subs(video)
        target = MediaTarget(
            path=video,
            media_type="video",
            existing_subs=subs,
        )
        target.status = _classify(target)
        targets.append(target)

    return targets


def _find_videos(root: Path, recursive: bool) -> list[Path]:
    """Find all video files under a directory."""
    videos: list[Path] = []
    iterator = root.rglob("*") if recursive else root.glob("*")
    for f in iterator:
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
            # Skip sample files
            if "sample" in f.stem.lower():
                continue
            videos.append(f)
    return sorted(videos)


def find_subs(video: Path) -> list[SubtitleFile]:
    """Find subtitle files associated with a video file."""
    subs: list[SubtitleFile] = []
    video_dir = video.parent
    video_stem = video.stem.lower()

    for sub_path in video_dir.iterdir():
        if not sub_path.is_file():
            continue
        if sub_path.suffix.lower() not in SUB_EXTENSIONS:
            continue

        sub_stem = sub_path.stem.lower()
        # Match if the sub file starts with the video name
        if sub_stem.startswith(video_stem) or sub_stem == video_stem:
            lang = _detect_sub_language(sub_path)
            try:
                fmt = SubFormat(sub_path.suffix.lower().lstrip("."))
            except ValueError:
                continue
            subs.append(SubtitleFile(path=sub_path, format=fmt, language=lang))

    return subs


def _detect_sub_language(path: Path) -> str:
    """Detect language from filename markers or fall back to content analysis."""
    name_lower = path.stem.lower()

    for lang, markers in LANG_MARKERS.items():
        for marker in markers:
            if marker in name_lower:
                return lang

    # Default: assume English if no marker found
    # (full content-based detection is done during translation)
    return "en"


def _classify(target: MediaTarget) -> SubStatus:
    """Determine the subtitle status for a media target."""
    if target.has_english:
        return SubStatus.FOUND
    if target.translatable_subs:
        return SubStatus.MISSING  # Has subs, just not English — needs translation
    return SubStatus.MISSING


def find_missing(targets: list[MediaTarget]) -> list[MediaTarget]:
    """Filter targets to only those missing English subtitles."""
    return [t for t in targets if not t.has_english]
