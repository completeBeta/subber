"""Tests for the translation-quality fixes: response parsing, chunk validation,
glossary injection and subtitle line wrapping.

Each of these pins a defect that shipped silently in v0.10.0:
- a speaker label on its own response line deleted the dialogue;
- a chunk whose line count didn't match the cues sent shifted every later cue;
- nothing let a show's names be fixed without editing the prompt by hand;
- translated lines were never wrapped, so cues ran off screen.
"""
from unittest import mock

import pysubs2
import pytest

from subber import config
from subber.translator import (
    Translator,
    _chunk_entries,
    _format_glossary,
    _labels_mismatch,
    _parse_numbered_response,
    _wrap_subtitle,
)


def _write_srt(path, texts):
    cues = []
    for i, txt in enumerate(texts, start=1):
        cues.append(f"{i}\n00:00:{i-1:02d},000 --> 00:00:{i:02d},000\n{txt}")
    path.write_text("\n\n".join(cues) + "\n", encoding="utf-8")


def _texts(path):
    return [e.plaintext for e in pysubs2.load(str(path), encoding="utf-8-sig").events]


# ── parsing ──────────────────────────────────────────────────────────────

def test_parse_joins_speaker_label_continuation():
    raw = "[0] (Felix Argyle)\nShowing up late to join us now—\n[1] Julius sure is living it up."
    parsed = _parse_numbered_response(raw)
    assert parsed[0] == "(Felix Argyle) Showing up late to join us now—"
    assert parsed[1] == "Julius sure is living it up."


def test_parse_ignores_meta_footer():
    parsed = _parse_numbered_response("[0] Hello\nNote: translated from Japanese")
    assert parsed[0] == "Hello"


def test_parse_drops_empty_entries():
    assert _parse_numbered_response("[0] \n[1] Hi") == {1: "Hi"}


def test_translate_keeps_dialogue_after_label(tmp_path):
    src = tmp_path / "in.srt"
    _write_srt(src, ["（フェリックス） 今頃 遅れて合流なんて―"])
    out = tmp_path / "out.srt"

    t = Translator(api_key="x", model="m", chunk_size=50)
    with mock.patch.object(
        t, "_call_api",
        side_effect=lambda messages, *a, **k: "[0] (Felix)\nShowing up late to join us now—",
    ):
        t.translate(src, out, "ja", "en")

    assert _texts(out) == ["(Felix) Showing up late to join us now—"]


# ── chunk validation ─────────────────────────────────────────────────────

def test_chunk_retried_when_line_count_mismatches():
    t = Translator(api_key="x", model="m", chunk_size=50)
    chunk = [{"index": 0, "text": "a"}, {"index": 1, "text": "b"}, {"index": 2, "text": "c"}]
    responses = iter([
        "[0] A\n[2] C",          # merged/dropped [1] -> desync if accepted
        "[0] A\n[1] B\n[2] C",   # retry is complete
    ])
    with mock.patch.object(t, "_translate_chunk", side_effect=lambda *a, **k: next(responses)):
        parsed, repaired = t._translate_chunk_verified(chunk, "ja", "en")

    assert repaired is True
    assert parsed == {0: "A", 1: "B", 2: "C"}


def test_chunk_not_retried_when_complete():
    t = Translator(api_key="x", model="m", chunk_size=50)
    chunk = [{"index": 0, "text": "a"}, {"index": 1, "text": "b"}]
    with mock.patch.object(t, "_translate_chunk", return_value="[0] A\n[1] B") as m:
        parsed, repaired = t._translate_chunk_verified(chunk, "ja", "en")

    assert repaired is False
    assert parsed == {0: "A", 1: "B"}
    assert m.call_count == 1


def test_chunk_falls_back_to_per_line_translation():
    t = Translator(api_key="x", model="m", chunk_size=50)
    chunk = [{"index": 0, "text": "a"}, {"index": 1, "text": "b"}]
    # Both whole-chunk attempts drop [1]; the single-line pass rescues it.
    responses = iter(["[0] A", "[0] A", "[1] B"])
    with mock.patch.object(t, "_translate_chunk", side_effect=lambda *a, **k: next(responses)):
        parsed, repaired = t._translate_chunk_verified(chunk, "ja", "en")

    assert repaired is True
    assert parsed == {0: "A", 1: "B"}


# ── speaker-label shift detection ───────────────────────────────────────

def test_labels_mismatch_accepts_aligned_chunk():
    chunk = [
        {"index": 0, "text": "（スバル） 何だよ", "_translate": True},
        {"index": 1, "text": "まあ 一応回収しとくか", "_translate": True},
    ]
    parsed = {0: "(Subaru) What the hell.", 1: "Well, I'll take it anyway."}
    assert _labels_mismatch(chunk, parsed) == 0


def test_labels_mismatch_detects_one_cue_shift():
    chunk = [
        {"index": 0, "text": "できあがりーってお話にゃの", "_translate": True},
        {"index": 1, "text": "（スバル） 何だよ そのうさん臭い話", "_translate": True},
    ]
    # content shifted by one: cue 1's label landed on cue 0
    parsed = {0: "(Subaru) What a sketchy story.", 1: "Well, I guess I'll take it."}
    assert _labels_mismatch(chunk, parsed) == 2


def test_chunk_retried_when_labels_shift():
    t = Translator(api_key="x", model="m", chunk_size=50)
    chunk = [
        {"index": 0, "text": "できあがりーってお話にゃの"},
        {"index": 1, "text": "（スバル） 何だよ そのうさん臭い話"},
    ]
    responses = iter([
        "[0] (Subaru) What a sketchy story.\n[1] Well, I'll take it anyway.",   # shifted
        "[0] And that's how you get a devout cultist.\n[1] (Subaru) What a sketchy story.",  # clean
    ])
    with mock.patch.object(t, "_translate_chunk", side_effect=lambda *a, **k: next(responses)):
        parsed, repaired = t._translate_chunk_verified(chunk, "ja", "en")

    assert repaired is True
    assert parsed[0] == "And that's how you get a devout cultist."


def test_repair_label_desync_replaces_shifted_cues():
    t = Translator(api_key="x", model="m", chunk_size=50)
    entries = [
        {"index": 0, "text": "できあがりーってお話にゃの"},
        {"index": 1, "text": "（スバル） 何だよ そのうさん臭い話"},
        {"index": 2, "text": "まあ 一応回収しとくか"},
    ]
    translated = {
        0: "(Subaru) What a shady story!",   # label landed one cue early
        1: "Well, I guess I'll pick it up.",
        2: "If I can decipher it, maybe...",
    }
    # Single-line passes come back matching the source label state.
    def fake_chunk(chunk, *a, **k):
        i = chunk[0]["index"]
        assert len(chunk) == 1
        return {
            0: "[0] And that makes a devout cultist.",
            1: "[1] (Subaru) What a shady story!",
            2: "[2] Well, I'll pick it up for now.",
        }[i]

    with mock.patch.object(t, "_translate_chunk", side_effect=fake_chunk):
        fixed = t._repair_label_desync(entries, translated, "ja", "en")

    assert fixed == 3
    assert translated[1].startswith("(Subaru)")
    assert not translated[0].startswith("(")


def test_repair_label_desync_noop_without_labels():
    t = Translator(api_key="x", model="m", chunk_size=50)
    entries = [{"index": 0, "text": "こんにちは"}, {"index": 1, "text": "さようなら"}]
    translated = {0: "Hello", 1: "Goodbye"}
    with mock.patch.object(t, "_translate_chunk") as m:
        assert t._repair_label_desync(entries, translated, "ja", "en") == 0
    m.assert_not_called()


# ── overlap handling ─────────────────────────────────────────────────────

def test_chunk_entries_flags_overlap_without_mutating_source():
    entries = [{"index": i, "text": f"t{i}"} for i in range(5)]
    chunks = _chunk_entries(entries, chunk_size=3, overlap=1)

    assert [e["index"] for e in chunks[0]] == [0, 1, 2]
    assert [e["index"] for e in chunks[1]] == [2, 3, 4]
    assert chunks[0][2]["_translate"] is True   # cue 2 is new in chunk 0
    assert chunks[1][0]["_translate"] is False  # and context-only in chunk 1
    assert "_translate" not in entries[2]       # shared cue dicts stay untouched


def test_overlap_cue_is_not_overwritten_by_later_chunk():
    entries = [{"index": i, "text": f"t{i}"} for i in range(5)]
    chunks = _chunk_entries(entries, chunk_size=3, overlap=1)
    t = Translator(api_key="x", model="m", chunk_size=3, chunk_overlap=1)
    responses = iter([
        "[0] A\n[1] B\n[2] C",
        "[2] WRONG\n[3] D\n[4] E",  # overlap line re-translated out of context
    ])
    with mock.patch.object(t, "_translate_chunk", side_effect=lambda *a, **k: next(responses)):
        first, _ = t._translate_chunk_verified(chunks[0], "ja", "en")
        second, _ = t._translate_chunk_verified(chunks[1], "ja", "en")

    assert first[2] == "C"
    assert 2 not in second  # context-only line is discarded, not written back
    assert second == {3: "D", 4: "E"}


# ── glossary ─────────────────────────────────────────────────────────────

def test_format_glossary_accepts_string_and_dict_entries():
    block = _format_glossary(["リア = Lia", {"source": "ペテルギウス", "target": "Petelgeuse"}])
    assert "- リア = Lia" in block
    assert "- ペテルギウス = Petelgeuse" in block
    assert _format_glossary([]) == ""
    assert _format_glossary(["nonsense"]) == ""


def test_glossary_reaches_the_prompt(tmp_path):
    src = tmp_path / "in.srt"
    _write_srt(src, ["こんにちは"])
    out = tmp_path / "out.srt"

    t = Translator(api_key="x", model="m", chunk_size=50, glossary=["リア = Lia"])
    captured = {}

    def fake(messages, *a, **k):
        captured["system"] = messages[0]["content"]
        return "[0] Hello"

    with mock.patch.object(t, "_call_api", side_effect=fake):
        t.translate(src, out, "ja", "en")

    assert "リア = Lia" in captured["system"]


# ── wrapping ─────────────────────────────────────────────────────────────

def test_wrap_subtitle_wraps_long_line():
    wrapped = _wrap_subtitle(
        "You must be tired, Lia. You should get some sleep and rest a while.", 30
    )
    lines = wrapped.split("\n")
    assert len(lines) >= 2
    assert all(len(ln) <= 30 for ln in lines)


def test_wrap_subtitle_disabled_and_short_text():
    assert _wrap_subtitle("Short line", 0) == "Short line"
    assert _wrap_subtitle("Short line", 42) == "Short line"


def test_wrap_subtitle_preserves_existing_break():
    wrapped = _wrap_subtitle("First part\\NSecond part", 42)
    assert wrapped == "First part\nSecond part"


def test_wrap_applied_on_write(tmp_path):
    src = tmp_path / "in.srt"
    _write_srt(src, ["こんにちは"])
    out = tmp_path / "out.srt"

    t = Translator(api_key="x", model="m", chunk_size=50, wrap_chars=20)
    long_line = "This is a fairly long translated subtitle line that needs wrapping."
    with mock.patch.object(t, "_call_api", side_effect=lambda m, *a, **k: f"[0] {long_line}"):
        t.translate(src, out, "ja", "en")

    text = _texts(out)[0]
    assert "\n" in text
    assert all(len(ln) <= 20 for ln in text.split("\n"))


# ── config hot reload ────────────────────────────────────────────────────

def test_config_reloads_when_file_changes(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("translation:\n  chunk_size: 20\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    config.reload()

    assert config.translation_settings()["chunk_size"] == 20

    # Operator edits the file directly (no POST /api/config) — must be picked up.
    cfg_path.write_text("translation:\n  chunk_size: 55\n", encoding="utf-8")
    assert config.translation_settings()["chunk_size"] == 55

    config.reload()
