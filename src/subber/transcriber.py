"""Audio-to-subtitle transcription (ASR) fallback.

Extracts the audio track from a video, transcribes it via a configurable
OpenAI-compatible ``/v1/audio/transcriptions`` endpoint (faster-whisper-server /
speaches / Groq / OpenAI, etc.), and writes an SRT subtitle file.

This is the last-resort fallback used when neither embedded subtitles nor any
provider can supply a subtitle. Opt-in: ``asr.mode`` must be ``"auto"`` (default
``"off"``) and the caller must explicitly request transcription.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

_log = logging.getLogger("subber")

_AUDIO_HZ = 16000  # mono 16 kHz — Whisper's preferred input


def extract_audio(video_path: Path, wav_path: Path) -> None:
    """Extract a mono 16 kHz WAV from a video via ffmpeg (already in the image)."""
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", str(_AUDIO_HZ),
        "-f", "wav", str(wav_path),
    ]
    _log.info("[ASR] Extracting audio (ffmpeg -i … -vn -ac 1 -ar %d)", _AUDIO_HZ)
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or "")[-400:]
        _log.error("[ASR] ffmpeg failed (rc=%d): %s", proc.returncode, err)
        raise RuntimeError(f"audio extraction failed (ffmpeg rc={proc.returncode})")
    _log.info(
        "[ASR] Extracted audio: %s bytes in %.1fs",
        wav_path.stat().st_size, time.time() - t0,
    )


def _audio_duration_seconds(wav_path: Path) -> float:
    """Return audio duration in seconds via ffprobe (ships with ffmpeg)."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(wav_path)],
        capture_output=True, text=True,
    )
    try:
        return float((proc.stdout or "").strip())
    except ValueError:
        return 0.0


def transcribe_file(
    audio_path: Path,
    backends: list[dict],
    model: str,
    language: str,
    timeout: int,
) -> dict:
    """POST audio to the first working backend; return verbose_json (segments).

    Failover: try each backend in order until one returns HTTP 200.
    """
    errors: list[str] = []
    for b in backends:
        name = b.get("name") or b.get("url") or "unknown"
        url = (b.get("url") or "").rstrip("/")
        api_key = b.get("api_key") or ""
        bmodel = b.get("model") or model
        if not url:
            errors.append(f"{name}: no url configured")
            continue
        try:
            _log.info("[ASR] Transcribing via backend '%s' (model=%s)", name, bmodel)
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            data = {"model": bmodel, "response_format": "verbose_json"}
            if language and language != "auto":
                data["language"] = language
            with open(audio_path, "rb") as f:
                resp = httpx.post(
                    f"{url}/v1/audio/transcriptions",
                    headers=headers,
                    files={"file": (audio_path.name, f, "audio/wav")},
                    data=data,
                    timeout=timeout,
                )
            if resp.status_code != 200:
                errors.append(f"{name}: HTTP {resp.status_code}")
                _log.warning(
                    "[ASR] backend '%s' returned %d: %s",
                    name, resp.status_code, (resp.text or "")[:200],
                )
                continue
            payload = resp.json()
            _log.info(
                "[ASR] backend '%s' OK: %d segment(s)",
                name, len(payload.get("segments") or []),
            )
            return payload
        except Exception as e:  # noqa: BLE001 — failover must swallow and try next
            _log.warning("[ASR] backend '%s' failed: %s", name, e)
            errors.append(f"{name}: {e}")
            continue
    raise RuntimeError("all ASR backends failed: " + "; ".join(errors))


def _fmt_ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segments_to_srt(segments: list[dict]) -> str:
    """Convert Whisper verbose_json segments into SRT text."""
    out: list[str] = []
    idx = 1
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        out.append(str(idx))
        out.append(f"{_fmt_ts(seg.get('start', 0.0))} --> {_fmt_ts(seg.get('end', 0.0))}")
        out.append(text)
        out.append("")
        idx += 1
    return "\n".join(out)


def transcribe_video(video_path: Path, out_srt: Path, asr_cfg: dict) -> dict:
    """Full pipeline: extract audio -> transcribe -> write SRT.

    Returns a result dict for logging / pipeline bookkeeping.
    """
    backends = asr_cfg.get("backends") or []
    if not backends:
        raise RuntimeError("no ASR backends configured (set asr.backends in config)")
    model = asr_cfg.get("model", "large-v3-turbo")
    language = asr_cfg.get("language", "auto")
    timeout = int(asr_cfg.get("timeout", 600))
    max_seconds = int(asr_cfg.get("max_audio_seconds", 3600))

    with tempfile.TemporaryDirectory(prefix="subber_asr_") as td:
        wav = Path(td) / "audio.wav"
        extract_audio(video_path, wav)
        duration = _audio_duration_seconds(wav)
        if duration > max_seconds:
            raise RuntimeError(
                f"audio duration {duration:.0f}s exceeds asr.max_audio_seconds ({max_seconds})"
            )
        payload = transcribe_file(wav, backends, model, language, timeout)

    segments = payload.get("segments") or []
    text = (payload.get("text") or "").strip()
    if not segments and text:
        segments = [{"start": 0.0, "end": duration, "text": text}]
    if not segments:
        raise RuntimeError("transcription returned no text")

    out_srt.write_text(segments_to_srt(segments), encoding="utf-8")
    _log.info("[ASR] Wrote SRT with %d segment(s) to %s", len(segments), out_srt.name)
    return {
        "segments": len(segments),
        "duration_s": duration,
        "model": payload.get("model") or model,
    }
