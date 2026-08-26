"""Tests for subtitle language detection (parser.detect_subtitle_language & helpers).

Regression coverage for the ".zip"-as-language bug: SubDL names English subs
like "…English.EN.zip.ass", and the old detector read the last token ("zip")
as a language code, translating thousands of English subs pointlessly.
"""

from subber.parser import (
    detect_subtitle_language,
    filename_signals_multiple_langs,
    lang_from_filename,
)
from subber.translator import Translator


# ── filename detection (pure, no I/O) ──

def test_filename_english_variants():
    for name in [
        "Show.S01E01.en.srt",
        "Show.S01E01.eng.srt",
        "Show.S01E01.English.srt",
        "Show.S01E01.EN.srt",
        "Show.S01E01.english.srt",
    ]:
        assert lang_from_filename(name) == "en", name


def test_filename_zip_is_not_a_language():
    # The exact SubDL naming that caused the bug.
    assert lang_from_filename(
        "I.May.Be.A.Guild.Receptionist.But.Ill.Solo.Any.Boss.To.Clock.Out.On.Time."
        "S01E11.CR.WEB-DL.English.EN.zip.ass"
    ) == "en"
    assert lang_from_filename(
        "mahou-shoujo-lyrical-nanoha-vivid_english-1522947.zip.ass"
    ) == "en"
    assert lang_from_filename(
        "Tojima.Wants.To.Be.A.Kamen.Rider.S01E13.CR.WEB-DL.English.EN.zip.ass"
    ) == "en"


def test_filename_no_language_marker_returns_none():
    # "zip", "san1317853", "season557025", quality tags — none are languages.
    assert lang_from_filename("SUBDL.com::osomatsu.san1317853.zip.ass") is None
    assert lang_from_filename("claymore.kureimoa..first.season557025.zip.srt") is None
    assert lang_from_filename("Show.720p.x264.HDTV.srt") is None
    assert lang_from_filename("Show.S01E01.srt") is None


def test_filename_quality_tags_ignored():
    assert lang_from_filename("Show.1080p.BluRay.x264-AAC.srt") is None
    assert lang_from_filename("Show.WEB-DL.CR.srt") is None


def test_filename_foreign_languages():
    assert lang_from_filename("Show.S01E01.ja.srt") == "ja"
    assert lang_from_filename("Show.S01E01.japanese.srt") == "ja"
    assert lang_from_filename("Show.S01E01.fr.srt") == "fr"
    assert lang_from_filename("Show.S01E01.spanish.srt") == "es"
    assert lang_from_filename("Show.S01E01.zh-cn.srt") == "zh"


# ── content detection (uses langdetect) ──

EN_SRT = """1
00:00:01,000 --> 00:00:03,000
Hello, how are you doing today?

2
00:00:03,000 --> 00:00:05,000
I am going to the store to buy some groceries.

3
00:00:05,000 --> 00:00:07,000
Would you like to come with me?
"""

JA_SRT = """1
00:00:01,000 --> 00:00:03,000
こんにちは、お元気ですか。

2
00:00:03,000 --> 00:00:05,000
私は今から店に行って買い物をします。

3
00:00:05,000 --> 00:00:07,000
一緒に来ませんか。
"""


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_detect_subtitle_language_content_english(tmp_path):
    # No language marker in filename → falls back to content sampling.
    p = _write(tmp_path, "Show.S01E01.srt", EN_SRT)
    assert detect_subtitle_language(p) == "en"


def test_detect_subtitle_language_content_japanese(tmp_path):
    p = _write(tmp_path, "Show.S01E01.srt", JA_SRT)
    assert detect_subtitle_language(p) == "ja"


def test_detect_subtitle_language_empty_file_is_unknown(tmp_path):
    p = _write(tmp_path, "Show.S01E01.srt", "")
    assert detect_subtitle_language(p) == "unknown"


def test_detect_subtitle_language_jap_filename_english_content(tmp_path):
    # Anime release: filename carries the AUDIO language (JAP), but the sub
    # text is English. Content must win over the misleading filename marker.
    p = _write(tmp_path, "Show.S01E01.JAP.720p.srt", EN_SRT)
    assert detect_subtitle_language(p) == "en"


def test_detect_subtitle_language_jap_filename_japanese_content(tmp_path):
    # Same filename, but genuinely Japanese text — still detected via content.
    p = _write(tmp_path, "Show.S01E01.JAP.720p.srt", JA_SRT)
    assert detect_subtitle_language(p) == "ja"


# ── LLM language confirmation (response parsing, no network) ──

def test_identify_language_parses_clean_code(monkeypatch):
    t = Translator(api_base="http://fake", api_key="x", model="test")
    monkeypatch.setattr(t, "_call_api", lambda messages: "ja")
    assert t.identify_language("こんにちは") == "ja"


def test_identify_language_skips_filler_words(monkeypatch):
    # Verbose reply — must NOT return "is"/"to"/"the", only a real code.
    t = Translator(api_base="http://fake", api_key="x", model="test")
    monkeypatch.setattr(t, "_call_api", lambda messages: "The language is English (en).")
    assert t.identify_language("hello world") == "en"


def test_identify_language_empty_response_is_unknown(monkeypatch):
    t = Translator(api_base="http://fake", api_key="x", model="test")
    monkeypatch.setattr(t, "_call_api", lambda messages: "")
    assert t.identify_language("...") == "unknown"


# ── multi-language filename signals (trigger LLM confirmation) ──

def test_multilang_dual_audio():
    assert filename_signals_multiple_langs("Show.S01E01.Dual.Audio.720p.srt")
    assert filename_signals_multiple_langs("Show.S01E01.Dual-Audio.srt")
    assert filename_signals_multiple_langs("Show.S01E01.DualAudio.srt")


def test_multilang_multi_tag():
    assert filename_signals_multiple_langs("[Aoi] Show - 04 MULTI [BD 1080p].srt")
    assert filename_signals_multiple_langs("Show.S01E01.Multi-Audio.srt")


def test_multilang_two_language_tokens():
    assert filename_signals_multiple_langs("Show.S01E01.JAP+ENG.srt")
    assert filename_signals_multiple_langs("Show.japanese.and.english.srt")


def test_multilang_single_language_is_not_multilang():
    # A single audio-language token (the original bug) is NOT multi-language.
    assert not filename_signals_multiple_langs("Show.S01E01.JAP.720p.srt")
    assert not filename_signals_multiple_langs("Show.S01E01.english.srt")


def test_multilang_plain_name_is_not_multilang():
    assert not filename_signals_multiple_langs("Show.S01E01.1080p.BluRay.srt")
    assert not filename_signals_multiple_langs("Show.S01E01.WEB-DL.CR.srt")
