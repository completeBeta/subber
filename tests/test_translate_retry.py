"""Tests for the leftover-untranslated retry in Translator.translate().

When the LLM omits a line number (duplicate/hallucinated segments) or passes
source text straight through (truncated response), those lines would otherwise
stay in the source language in the final file. These tests pin the retry.
"""
from unittest import mock

import pysubs2

from subber.translator import Translator, _still_source_lang


def test_still_source_lang():
    assert _still_source_lang("こんにちは", "ja") is True
    assert _still_source_lang("안녕하세요", "ko") is True
    assert _still_source_lang("你好", "zh") is True
    assert _still_source_lang("Hello", "ja") is False
    assert _still_source_lang("", "ja") is False
    assert _still_source_lang("bonjour", "fr") is False  # non-CJK -> best-effort False


def _write_srt(path, texts):
    cues = []
    for i, txt in enumerate(texts, start=1):
        cues.append(f"{i}\n00:00:{i-1:02d},000 --> 00:00:{i:02d},000\n{txt}")
    path.write_text("\n\n".join(cues) + "\n", encoding="utf-8")


def _texts(path):
    return [e.plaintext for e in pysubs2.load(str(path), encoding="utf-8-sig").events]


def test_translate_retries_omitted_lines(tmp_path):
    src = tmp_path / "in.srt"
    _write_srt(src, ["こんにちは", "さようなら", "ありがとう"])
    out = tmp_path / "out.srt"

    t = Translator(api_key="x", model="m", chunk_size=50)
    responses = iter([
        "[0] Hello\n[2] Thank you",   # main pass omits [1]
        "[1] Goodbye",                # retry pass
    ])
    with mock.patch.object(t, "_translate_chunk", side_effect=lambda *a, **k: next(responses)):
        t.translate(src, out, "ja", "en")

    assert _texts(out) == ["Hello", "Goodbye", "Thank you"]


def test_translate_retries_passthrough_cjk(tmp_path):
    src = tmp_path / "in.srt"
    _write_srt(src, ["こんにちは", "さようなら", "ありがとう"])
    out = tmp_path / "out.srt"

    t = Translator(api_key="x", model="m", chunk_size=50)
    responses = iter([
        "[0] Hello\n[1] さようなら\n[2] Thank you",  # [1] passed through as CJK
        "[1] Goodbye",                                # retry pass
    ])
    with mock.patch.object(t, "_translate_chunk", side_effect=lambda *a, **k: next(responses)):
        t.translate(src, out, "ja", "en")

    assert _texts(out) == ["Hello", "Goodbye", "Thank you"]
