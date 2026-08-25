"""Tests for the library-tab ASR fallback: foreign audio -> transcribe -> translate.

The key regression this pins: Whisper's detected language must be passed to the
translator EXPLICITLY. The old code wrote the raw transcript to a ".en.srt" name,
then re-detected language from that filename — which read "en" and skipped
translation entirely, leaving Japanese/Korean text in an English-labelled file.
"""

import asyncio
from unittest.mock import patch

from subber import library_pipeline as lp


def _run(coro):
    return asyncio.run(coro)


def _fake_transcribe(text: str, lang: str):
    def _fake(video_path, out_srt, asr_cfg):
        out_srt.write_text(
            f"1\n00:00:00,000 --> 00:00:02,000\n{text}\n",
            encoding="utf-8",
        )
        return {"language": lang, "segments": 1, "model": "large-v3-turbo"}
    return _fake


def test_foreign_audio_is_translated_with_whisper_language(tmp_path, monkeypatch):
    """Whisper detects 'ja' -> must call _translate_and_sync with source_lang='ja'."""
    video = tmp_path / "Show.S01E01.mkv"
    video.write_bytes(b"x")

    captured = {}

    async def fake_translate_and_sync(video_path, sub_path, drift_threshold_ms, source_lang=None):
        captured["source_lang"] = source_lang
        captured["sub_path"] = str(sub_path)
        out = video_path.with_suffix(".en.srt")
        out.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
        return {"output_path": str(out), "model_used": "deepseek", "cost": 0.01, "drift_ms": None}

    monkeypatch.setattr(lp.subber_config, "asr_settings", lambda: {"mode": "auto", "backends": [{"url": "http://x"}]})
    monkeypatch.setattr(lp.subber_config, "ad_removal_settings", lambda: {"mode": "off"})

    with patch("subber.transcriber.transcribe_video",
               side_effect=_fake_transcribe("こんにちは", "ja")), \
         patch.object(lp, "_translate_and_sync", side_effect=fake_translate_and_sync):
        result = _run(lp._asr_transcribe(video, 200))

    assert captured["source_lang"] == "ja", "Whisper language must be passed explicitly"
    assert result["action"] == "transcribed_and_translated"
    assert result["output_path"].endswith(".en.srt")
    assert not video.with_suffix(".raw.srt").exists(), "raw transcript must be cleaned up"


def test_korean_audio_is_translated(tmp_path, monkeypatch):
    """Whisper detects 'ko' -> source_lang='ko', same as Japanese."""
    video = tmp_path / "Show.S01E01.mkv"
    video.write_bytes(b"x")
    captured = {}

    async def fake_translate_and_sync(video_path, sub_path, drift_threshold_ms, source_lang=None):
        captured["source_lang"] = source_lang
        out = video_path.with_suffix(".en.srt")
        out.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
        return {"output_path": str(out), "model_used": "deepseek", "cost": 0.01, "drift_ms": None}

    monkeypatch.setattr(lp.subber_config, "asr_settings", lambda: {"mode": "auto", "backends": [{"url": "http://x"}]})
    monkeypatch.setattr(lp.subber_config, "ad_removal_settings", lambda: {"mode": "off"})

    with patch("subber.transcriber.transcribe_video",
               side_effect=_fake_transcribe("안녕하세요", "ko")), \
         patch.object(lp, "_translate_and_sync", side_effect=fake_translate_and_sync):
        result = _run(lp._asr_transcribe(video, 200))

    assert captured["source_lang"] == "ko"
    assert result["action"] == "transcribed_and_translated"


def test_english_audio_is_not_translated(tmp_path, monkeypatch):
    """Whisper detects 'en' -> no translation, raw transcript becomes .en.srt."""
    video = tmp_path / "Show.S01E01.mkv"
    video.write_bytes(b"x")
    monkeypatch.setattr(lp.subber_config, "asr_settings", lambda: {"mode": "auto", "backends": [{"url": "http://x"}]})
    monkeypatch.setattr(lp.subber_config, "ad_removal_settings", lambda: {"mode": "off"})

    with patch("subber.transcriber.transcribe_video",
               side_effect=_fake_transcribe("Hello there", "en")), \
         patch.object(lp, "_translate_and_sync") as mock_translate:
        result = _run(lp._asr_transcribe(video, 200))

    mock_translate.assert_not_called()
    assert result["action"] == "transcribed"
    assert result["output_path"].endswith(".en.srt")
    assert not video.with_suffix(".raw.srt").exists()
