"""Tests for subtitle parser."""

import tempfile
from pathlib import Path

from subber.parser import detect_format, parse
from subber.types import SubFormat


def test_detect_format_srt():
    path = Path("test.en.srt")
    assert detect_format(path) == SubFormat.SRT


def test_detect_format_ass():
    path = Path("test.de.ass")
    assert detect_format(path) == SubFormat.ASS


def test_parse_srt():
    content = (
        "1\n"
        "00:00:01,000 --> 00:00:02,500\n"
        "Hello world\n"
        "\n"
        "2\n"
        "00:00:03,000 --> 00:00:05,000\n"
        "Goodbye\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".srt", delete=False) as f:
        f.write(content)
        f.flush()
        entries = parse(Path(f.name))
    
    assert len(entries) == 2
    assert entries[0]["text"] == "Hello world"
    assert entries[1]["text"] == "Goodbye"
    assert entries[0]["start"] == 1.0
    assert entries[0]["end"] == 2.5
