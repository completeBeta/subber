"""Typeset tracks: foreign-named sign styles and per-frame repeats.

Pins the fix for the Rosario to Vampire E03 blow-up. That `.es.ass` carries 44,472
events of which 44,087 are style "Letreros" (Spanish for *signs*): 31,414 pure
vector drawings ({\\p1}m 0 0 l ...) plus 12,673 per-frame repeats of 11 distinct
strings. The style-name filter only knew English sign words, so every one of them
counted as dialogue → 2,224 chunks instead of 20, and each chunk of drawings cost
a retry plus a per-line repair (16,823 OpenRouter calls in 24 h).
"""

from subber.library_pipeline import _filter_to_dialogue
from subber.translator import _plan_translation_units, _translation_key


ASS_HEADER = """[Script Info]
Title: test
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize
Style: Default,Arial,20
Style: Letreros,Arial,20
Style: Sign,Arial,20

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

DRAWING = "{\\p1\\pos(438.84,489.36)}m 0 0 l 0 1 307 1 307 0"


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_unknown_style_text_kept_drawings_dropped(tmp_path):
    ass = _write(tmp_path / "Show.es.ass", ASS_HEADER + (
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hola\n"
        f"Dialogue: 0,0:01:00.00,0:01:00.04,Letreros,,0,0,0,,{DRAWING}\n"
        "Dialogue: 0,0:01:00.04,0:01:00.08,Letreros,,0,0,0,,Instituto Youkai\n"
    ))
    filtered = _filter_to_dialogue(ass)
    assert filtered is not None
    content = filtered.read_text(encoding="utf-8")
    assert "Hola" in content
    assert "Instituto Youkai" in content          # unknown style, real wording → kept
    assert "m 0 0 l 0 1 307 1" not in content      # vector drawing → dropped


def test_known_english_sign_style_still_dropped(tmp_path):
    # A recognised sign style stays dropped even when it carries words.
    ass = _write(tmp_path / "Show.fr.ass", ASS_HEADER + (
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Bonjour\n"
        "Dialogue: 0,0:00:02.00,0:00:03.00,Sign,,0,0,0,,ON-SCREEN TEXT\n"
    ))
    filtered = _filter_to_dialogue(ass)
    assert filtered is not None
    content = filtered.read_text(encoding="utf-8")
    assert "Bonjour" in content
    assert "ON-SCREEN TEXT" not in content


def test_drawings_only_returns_none(tmp_path):
    # Only animations and no wording → nothing worth translating at all.
    ass = _write(tmp_path / "Show.es.ass", ASS_HEADER + (
        f"Dialogue: 0,0:01:00.00,0:01:00.04,Letreros,,0,0,0,,{DRAWING}\n"
        "Dialogue: 0,0:01:00.04,0:01:00.08,Letreros,,0,0,0,,♪\n"
    ))
    assert _filter_to_dialogue(ass) is None


def test_translation_key_rejects_non_language():
    assert _translation_key("m 2.4 68 l 16.8 30.4 81.6 24") is None
    assert _translation_key("{\\p1}m 0 0 l 0 1 307 1") is None
    assert _translation_key("♪") is None
    assert _translation_key("   ") is None
    assert _translation_key("¡No!") == "¡no!"


def test_plan_collapses_repeats_and_passthrough():
    entries = []
    for i in range(40):
        entries.append({"index": i, "text": "Instituto Youkai" if i % 2 else DRAWING})
    entries.append({"index": 40, "text": "¡Tsukune!"})
    translatable, duplicates, passthrough = _plan_translation_units(entries)

    assert [e["index"] for e in translatable] == [1, 40]   # one rep per distinct text
    assert len(duplicates) == 19                            # the other word-bearing repeats
    assert len(passthrough) == 20                           # the drawings
    assert duplicates[3] == 1                               # fan-out target is the first copy


def test_plan_keeps_every_cue_of_a_normal_file():
    entries = [{"index": i, "text": f"Line {i}"} for i in range(5)]
    translatable, duplicates, passthrough = _plan_translation_units(entries)
    assert len(translatable) == 5
    assert duplicates == {}
    assert passthrough == set()
