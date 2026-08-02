"""Subtitle synchronization — ffsubsync + manual offset fallback."""
import argparse
import asyncio
from pathlib import Path
from dataclasses import dataclass, field

import pysubs2


@dataclass
class SyncPreview:
    """Preview of sync changes before applying."""
    offset_seconds: float
    sample_lines: list = field(default_factory=list)
    engine: str = "ffsubsync"


def sync_preview(
    video_path: Path,
    sub_path: Path,
    engine: str = "ffsubsync",
    offset: float = 0.0,
) -> SyncPreview:
    """
    Analyze sync between video and subtitle, return a preview.

    Does NOT modify the subtitle file.
    """
    if engine == "ffsubsync":
        try:
            return _ffsubsync_preview(video_path, sub_path)
        except Exception:
            # Fallback to offset mode on failure
            return _offset_preview(sub_path, offset)
    else:
        return _offset_preview(sub_path, offset)


def sync_apply(
    video_path: Path,
    sub_path: Path,
    output_path: Path,
    engine: str = "ffsubsync",
    offset: float = 0.0,
) -> Path:
    """
    Apply sync correction to subtitle and save.

    Returns output_path on success.
    """
    if engine == "ffsubsync":
        try:
            return _ffsubsync_apply(video_path, sub_path, output_path)
        except Exception:
            return _offset_apply(sub_path, output_path, offset)
    else:
        return _offset_apply(sub_path, output_path, offset)


def _ffsubsync_preview(video_path: Path, sub_path: Path) -> SyncPreview:
    from ffsubsync.ffsubsync import run
    synced_sub_path = sub_path.with_stem(f"{sub_path.stem}.synced")
    args = _make_ffsubsync_args(video_path, sub_path, synced_sub_path)
    try:
        result = run(args)
        offset = result.get("offset_seconds", 0.0) or 0.0
    except Exception:
        offset = 0.0
    try:
        synced_sub_path.unlink(missing_ok=True)
    except Exception:
        pass
    sample = _build_sample(sub_path, offset)
    return SyncPreview(offset_seconds=offset, sample_lines=sample, engine="ffsubsync")


def _ffsubsync_apply(video_path: Path, sub_path: Path, output_path: Path) -> Path:
    from ffsubsync.ffsubsync import run
    args = _make_ffsubsync_args(video_path, sub_path, output_path)
    run(args)
    return output_path


def _offset_preview(sub_path: Path, offset: float) -> SyncPreview:
    """Preview what an offset shift would look like."""
    sample = _build_sample(sub_path, offset)
    return SyncPreview(offset_seconds=offset, sample_lines=sample, engine="offset")


def _offset_apply(sub_path: Path, output_path: Path, offset: float) -> Path:
    """Apply a time offset to all subtitle events."""
    subs = pysubs2.load(str(sub_path), encoding="utf-8-sig")
    offset_ms = int(offset * 1000)
    for event in subs.events:
        event.start += offset_ms
        event.end += offset_ms
        if event.start < 0:
            event.start = 0
        if event.end < 0:
            event.end = 0
    subs.save(str(output_path))
    return output_path


def _build_sample(sub_path: Path, offset: float) -> list[dict]:
    """Build ~8 sample lines showing original vs synced timestamps."""
    subs = pysubs2.load(str(sub_path), encoding="utf-8-sig")
    sample = []
    total = len(subs.events)
    if total == 0:
        return sample
    step = max(1, total // 8)
    for i in range(0, total, step):
        event = subs.events[i]
        sample.append({
            "index": i,
            "original_start": round(event.start / 1000, 3),
            "synced_start": round((event.start / 1000) + offset, 3),
            "text": event.plaintext[:80],
        })
    return sample




def _make_ffsubsync_args(video_path, sub_path, output_path):
    from ffsubsync.ffsubsync import make_parser
    parser = make_parser()
    args = parser.parse_args([])  # get all defaults
    args.reference = str(video_path)
    args.srtin = [str(sub_path)]
    args.srtout = str(output_path)
    args.gss = True
    args.max_offset_seconds = 60
    return args
async def async_sync_preview(
    video_path: Path,
    sub_path: Path,
    engine: str = "ffsubsync",
    offset: float = 0.0,
) -> SyncPreview:
    """Non-blocking wrapper for sync_preview — runs in a thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: sync_preview(video_path, sub_path, engine=engine, offset=offset),
    )


async def async_sync_apply(
    video_path: Path,
    sub_path: Path,
    output_path: Path,
    engine: str = "ffsubsync",
    offset: float = 0.0,
) -> Path:
    """Non-blocking wrapper for sync_apply — runs in a thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: sync_apply(video_path, sub_path, output_path, engine=engine, offset=offset),
    )
