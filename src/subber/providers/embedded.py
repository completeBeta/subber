"""Embedded subtitle provider — extracts subtitles already inside video files."""

import asyncio
import json
import subprocess
from pathlib import Path

from ..types import SubtitleResult
from .base import ProviderCapabilities, SubtitleProvider

CODEC_EXTS = {
    "subrip": "srt",
    "ass": "ass",
    "ssa": "ass",
    "webvtt": "vtt",
    "mov_text": "srt",
    "dvd_subtitle": "vobsub",
    "hdmv_pgs_subtitle": "sup",
}

ISO639_3_TO_1 = {
    "eng": "en", "jpn": "ja", "spa": "es", "fre": "fr", "fra": "fr",
    "deu": "de", "ger": "de", "ita": "it", "por": "pt", "rus": "ru",
    "ara": "ar", "zho": "zh", "chi": "zh", "kor": "ko", "vie": "vi",
    "tha": "th", "ind": "id", "tur": "tr", "pol": "pl", "nld": "nl",
    "dut": "nl", "swe": "sv", "nor": "no", "dan": "da", "fin": "fi",
    "hun": "hu", "ces": "cs", "cze": "cs", "ron": "ro", "rum": "ro",
    "bul": "bg", "hrv": "hr", "srp": "sr", "ukr": "uk", "heb": "he",
    "ell": "el", "gre": "el", "cat": "ca", "eus": "eu", "baq": "eu",
    "glg": "gl", "slv": "sl", "slk": "sk", "slo": "sk",
}


class EmbeddedProvider(SubtitleProvider):
    """Extract subtitles already embedded inside video files."""

    def __init__(self):
        super().__init__(ProviderCapabilities(
            name="Embedded",
            free=True,
            requires_auth=False,
            supports_hash_search=False,
            supports_name_search=False,
            supports_season_episode=False,
            rate_limit_rps=10.0,
        ))

    async def search(
        self, query: str = "", language: str = "en",
        season: int | None = None, episode: int | None = None,
    ) -> list[SubtitleResult]:
        return []

    async def search_by_hash(
        self, video_path: Path, language: str = "en",
    ) -> list[SubtitleResult]:
        if not video_path.is_file():
            return []

        tracks = await self._probe_tracks(video_path)
        results = []
        for i, track in enumerate(tracks):
            lang = track.get("lang", "unknown")
            if language != "en" and lang != language:
                continue

            codec = track.get("codec", "unknown")
            ext = CODEC_EXTS.get(codec, "srt")
            results.append(SubtitleResult(
                id=f"embedded_{hash(video_path)}_{i}",
                filename=f"{video_path.stem}.{lang}.{ext}",
                language=lang,
                provider="Embedded",
                downloads=0,
                rating=10.0,
                hearing_impaired=False,
                release_info=f"Embedded ({codec})",
                metadata={
                    "video_path": str(video_path),
                    "track_index": i,
                    "codec": codec,
                },
            ))
        return results

    async def download(self, result: SubtitleResult, output_path: Path) -> Path:
        video_path = result.metadata["video_path"]
        track_index = result.metadata["track_index"]
        codec = result.metadata.get("codec", "subrip")

        args = [
            "ffmpeg", "-y", "-nostdin",
            "-i", video_path,
            "-map", f"0:s:{track_index}",
        ]

        # Always copy codec, but match output extension to codec
        # (ASS data in .srt container = ffmpeg error)
        args.extend(["-c:s", "copy"])
        ext = CODEC_EXTS.get(codec, "srt")
        output_path = output_path.with_suffix(f".{ext}")

        args.append(str(output_path))

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: subprocess.run(
                args, capture_output=True, text=True, timeout=300,
            )
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg extraction failed (track {track_index}): {result.stderr[:500]}"
            )

        return output_path

    async def search_all_tracks(
        self, video_path: Path,
    ) -> list[SubtitleResult]:
        tracks = await self._probe_tracks(video_path)
        results = []
        for i, track in enumerate(tracks):
            lang = track.get("lang", "unknown")
            codec = track.get("codec", "unknown")
            ext = CODEC_EXTS.get(codec, "srt")
            results.append(SubtitleResult(
                id=f"embedded_{hash(video_path)}_{i}",
                filename=f"{video_path.stem}.{lang}.{ext}",
                language=lang,
                provider="Embedded",
                downloads=0,
                rating=10.0,
                hearing_impaired=False,
                release_info=f"Embedded ({codec})",
                metadata={
                    "video_path": str(video_path),
                    "track_index": i,
                    "codec": codec,
                },
            ))
        return results

    async def get_embedded_result(
        self, video_path: Path,
    ) -> tuple[SubtitleResult | None, list[str]]:
        """Return best embedded track + list of all available languages.

        Uses language_priority from selection settings to pick the best track.
        English is always preferred; falls back to the highest-priority non-English track.
        """
        from ..config import selection_settings

        tracks = await self._probe_tracks(video_path)
        if not tracks:
            return None, []

        settings = selection_settings()
        lang_priority = settings.get("language_priority", ["en"])

        all_langs = list({t.get("lang", "unknown") for t in tracks})

        # Build results for all tracks
        results: list[SubtitleResult] = []
        for i, track in enumerate(tracks):
            lang = track.get("lang", "unknown")
            codec = track.get("codec", "unknown")
            ext = CODEC_EXTS.get(codec, "srt")
            results.append(SubtitleResult(
                id=f"embedded_{hash(video_path)}_{i}",
                filename=f"{video_path.stem}.{lang}.{ext}",
                language=lang,
                provider="Embedded",
                downloads=0,
                rating=10.0,
                hearing_impaired=False,
                release_info=f"Embedded ({codec})",
                metadata={
                    "video_path": str(video_path),
                    "track_index": i,
                    "codec": codec,
                },
            ))

        # Prefer English, then fall back through language priority
        en_results = [r for r in results if r.language == "en"]
        if en_results:
            return en_results[0], all_langs

        # Walk priority list
        for lang in lang_priority:
            if lang == "en":
                continue  # already checked
            matches = [r for r in results if r.language == lang]
            if matches:
                return matches[0], all_langs

        # Fallback: return first available
        return results[0], all_langs

    async def _probe_tracks(self, video_path: Path) -> list[dict]:
        args = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-select_streams", "s",
            str(video_path),
        ]
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: subprocess.run(
                args, capture_output=True, text=True, timeout=300,
            )
        )
        if result.returncode != 0:
            return []

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []

        tracks = []
        for stream in data.get("streams", []):
            if stream.get("codec_type") != "subtitle":
                continue
            tags = stream.get("tags", {})
            lang_raw = tags.get("language", "und")
            lang = ISO639_3_TO_1.get(lang_raw, lang_raw[:2] if len(lang_raw) > 2 else lang_raw)
            tracks.append({
                "lang": lang,
                "codec": stream.get("codec_name", "unknown"),
                "title": tags.get("title", ""),
            })
        return tracks
