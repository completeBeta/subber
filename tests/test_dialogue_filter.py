"""Tests for _filter_to_dialogue: strip sign/song/effect lines before
translating a foreign fansub, so only spoken dialogue is translated.

Pins the fix for the money-waste loop: French-only fansubs with thousands of
sign/karaoke lines were translated wholesale (30+ min of LLM calls), hit the
watchdog, and got re-queued. The filter keeps only dialogue-style events.
"""

from subber.library_pipeline import _filter_to_dialogue


ASS_HEADER = """[Script Info]
Title: test
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize
Style: Default,Arial,20
Style: Sign,Arial,20
Style: OP,Arial,20

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_keeps_dialogue_drops_signs(tmp_path):
    ass = _write(tmp_path / "Show.fr.ass", ASS_HEADER + (
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Bonjour\n"
        "Dialogue: 0,0:00:02.00,0:00:03.00,Sign,,0,0,0,,ON-SCREEN TEXT\n"
        "Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,Comment ca va?\n"
        "Dialogue: 0,0:00:04.00,0:00:05.00,OP,,0,0,0,,LA LA LA\n"
    ))
    filtered = _filter_to_dialogue(ass)
    assert filtered is not None
    assert filtered != ass
    content = filtered.read_text(encoding="utf-8")
    assert "Bonjour" in content
    assert "Comment ca va?" in content
    assert "ON-SCREEN TEXT" not in content
    assert "LA LA LA" not in content


def test_signs_only_returns_none(tmp_path):
    ass = _write(tmp_path / "Show.fr.ass", ASS_HEADER + (
        "Dialogue: 0,0:00:01.00,0:00:02.00,Sign,,0,0,0,,TEXT ONE\n"
        "Dialogue: 0,0:00:02.00,0:00:03.00,OP,,0,0,0,,LA LA LA\n"
    ))
    assert _filter_to_dialogue(ass) is None


def test_srt_returns_original(tmp_path):
    srt = _write(tmp_path / "Show.fr.srt",
                 "1\n00:00:01,000 --> 00:00:02,000\nBonjour\n")
    # .srt has no style info — nothing to filter, return the original path.
    assert _filter_to_dialogue(srt) == srt


def test_no_events_returns_none(tmp_path):
    ass = _write(tmp_path / "Show.fr.ass", ASS_HEADER)
    assert _filter_to_dialogue(ass) is None
