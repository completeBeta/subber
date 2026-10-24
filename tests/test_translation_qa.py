"""Tests for the per-file translation QA counters and the missing-dialogue repair."""
from unittest import mock

import pysubs2

from subber.translator import Translator, translation_qa


def _entries(*texts):
    return [{"index": i, "text": t} for i, t in enumerate(texts)]


def test_qa_clean_file():
    entries = _entries("こんにちは", "さようなら")
    qa = translation_qa(entries, {0: "Hello", 1: "Goodbye"}, "ja")
    assert qa["clean"] is True
    assert (qa["empty"], qa["label_only"], qa["untranslated"]) == (0, 0, 0)
    assert qa["cues"] == 2
    assert qa["longest_line"] == len("Goodbye")


def test_qa_counts_empty_label_only_and_untranslated():
    entries = _entries("（スバル） 何だよ", "まあ いいか", "ありがとう", "おはよう")
    translated = {
        0: "(Subaru)",       # label only
        1: "",               # empty
        2: "ありがとう",      # untranslated (still CJK)
        3: "Morning",
    }
    qa = translation_qa(entries, translated, "ja")
    assert qa["label_only"] == 1
    assert qa["empty"] == 1
    assert qa["untranslated"] == 1
    assert qa["clean"] is False


def test_qa_sound_cue_is_not_a_defect():
    """（扉の開閉音）→ "(Sound of a door)" is complete, not a dropped line."""
    entries = _entries("（扉の開閉音）", "（ミミの笑い声）")
    qa = translation_qa(entries, {0: "(Sound of a door)", 1: "(Mimi's laughter)"}, "ja")
    assert qa["sound_cues"] == 2
    assert qa["label_only"] == 0
    assert qa["clean"] is True


def test_qa_longest_line_uses_wrapped_lines():
    entries = _entries("こんにちは")
    qa = translation_qa(entries, {0: "short\nlonger line here"}, "ja")
    assert qa["longest_line"] == len("longer line here")


def test_missing_dialogue_repair_replaces_label_only_cue(tmp_path):
    src = tmp_path / "in.srt"
    src.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n（スバル） 何だよ その話\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.srt"

    t = Translator(api_key="x", model="m", chunk_size=50)
    calls = []

    def fake_chunk(chunk, *a, **k):
        calls.append(chunk[0]["index"])
        # first pass says only the label; the single-line retry has the words
        return "[0] (Subaru)" if len(calls) == 1 else "[0] (Subaru) What the hell is that?"

    with mock.patch.object(t, "_translate_chunk", side_effect=fake_chunk):
        t.translate(src, out, "ja", "en")

    text = pysubs2.load(str(out), encoding="utf-8-sig").events[0].plaintext
    assert text == "(Subaru) What the hell is that?"
    assert t.last_qa["label_only"] == 0
    assert t.last_qa["dialogue_repaired"] == 1


def test_missing_dialogue_repair_leaves_pure_sound_cues_alone(tmp_path):
    """A cue that is genuinely only a label must not be thrashed."""
    src = tmp_path / "in.srt"
    src.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n（扉の開閉音）\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.srt"

    t = Translator(api_key="x", model="m", chunk_size=50)
    with mock.patch.object(t, "_translate_chunk", return_value="[0] (Door opening/closing)"):
        t.translate(src, out, "ja", "en")

    text = pysubs2.load(str(out), encoding="utf-8-sig").events[0].plaintext
    assert "Door" in text
    assert t.last_qa["dialogue_repaired"] == 0


def test_last_qa_present_after_translate(tmp_path):
    src = tmp_path / "in.srt"
    src.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n",
        encoding="utf-8",
    )
    t = Translator(api_key="x", model="m", chunk_size=50)
    with mock.patch.object(t, "_call_api", side_effect=lambda m, *a, **k: "[0] Hello"):
        t.translate(src, tmp_path / "out.srt", "ja", "en")

    qa = t.last_qa
    assert qa["cues"] == 1
    assert qa["chunks"] == 1
    assert qa["clean"] is True
