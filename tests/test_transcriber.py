"""Tests for the ASR transcriber module (segments->SRT, failover, extraction)."""
import json
import shutil

import pytest

from subber.transcriber import (
    _fmt_ts,
    extract_audio,
    segments_to_srt,
    transcribe_file,
)


def test_fmt_ts():
    assert _fmt_ts(0.0) == "00:00:00,000"
    assert _fmt_ts(1.5) == "00:00:01,500"
    assert _fmt_ts(61.234) == "00:01:01,234"
    assert _fmt_ts(3600.0) == "01:00:00,000"
    assert _fmt_ts(3661.999) == "01:01:01,999"


def test_segments_to_srt():
    segs = [
        {"start": 0.0, "end": 2.0, "text": "Hello there"},
        {"start": 2.0, "end": 4.5, "text": "General Kenobi"},
        {"start": 4.5, "end": 5.0, "text": "  "},  # blank -> skipped
    ]
    srt = segments_to_srt(segs)
    assert "1\n00:00:00,000 --> 00:00:02,000\nHello there" in srt
    assert "2\n00:00:02,000 --> 00:00:04,500\nGeneral Kenobi" in srt
    assert srt.strip().endswith("General Kenobi")  # blank segment not emitted


def test_transcribe_file_failover(monkeypatch, tmp_path):
    import subber.transcriber as tr

    calls = []

    class FakeResp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
            self.text = payload if isinstance(payload, str) else json.dumps(payload)

        def json(self):
            return self._payload

    def fake_post(url, **kwargs):
        calls.append(url)
        if "backend-a" in url:
            return FakeResp(500, "internal error")
        return FakeResp(200, {
            "text": "hello world",
            "segments": [{"start": 0.0, "end": 1.2, "text": "hello world"}],
        })

    monkeypatch.setattr(tr.httpx, "post", fake_post)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"\x00" * 8)

    backends = [
        {"name": "a", "url": "http://backend-a:8000", "api_key": ""},
        {"name": "b", "url": "http://backend-b:8000", "api_key": ""},
    ]
    result = tr.transcribe_file(audio, backends, "large-v3-turbo", "auto", 10)
    assert len(calls) == 2  # first failed, second used
    assert result["segments"][0]["text"] == "hello world"


def test_transcribe_file_all_fail(monkeypatch, tmp_path):
    import subber.transcriber as tr

    class FakeResp:
        def __init__(self, status):
            self.status_code = status
            self.text = "nope"

    def fake_post(url, **kwargs):
        return FakeResp(503)

    monkeypatch.setattr(tr.httpx, "post", fake_post)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"\x00" * 8)
    backends = [{"name": "a", "url": "http://backend-a:8000", "api_key": ""}]
    with pytest.raises(RuntimeError, match="all ASR backends failed"):
        tr.transcribe_file(audio, backends, "large-v3-turbo", "auto", 10)


def test_extract_audio(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    # Generate 1s of 440 Hz sine audio, mux into an mp4 "video"
    mp4 = tmp_path / "clip.mp4"
    import subprocess
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1",
         "-shortest", "-c:v", "libx264", "-c:a", "aac", str(mp4)],
        check=True, capture_output=True,
    )
    wav = tmp_path / "out.wav"
    extract_audio(mp4, wav)
    assert wav.exists() and wav.stat().st_size > 0
    # ffprobe should report ~1s duration, mono 16k
    import subprocess as sp
    out = sp.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate,channels",
         "-of", "default=noprint_wrappers=1", str(wav)],
        capture_output=True, text=True,
    ).stdout
    assert "sample_rate=16000" in out
    assert "channels=1" in out
