"""Tests for ad/credit removal from subtitles."""

import tempfile
from pathlib import Path

from subber.ad_removal import remove_ads

# A short SRT with an ad line in the intro, dialogue, and an ad line in the outro.
SRT = """1
00:00:01,000 --> 00:00:03,000
Support us on Patreon: patreon.com/example

2
00:00:05,000 --> 00:00:07,000
Hello, how are you?

3
00:00:10,000 --> 00:00:12,000
Don't forget to subscribe!

4
00:05:00,000 --> 00:05:02,000
subscribe to my newsletter

5
00:10:00,000 --> 00:10:02,000
Thanks for watching! Subscribe for more.
"""


def _write(srt: str) -> Path:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".srt", delete=False, encoding="utf-8")
    f.write(srt)
    f.close()
    return Path(f.name)


def test_adverts_mode_strips_intro_outro_ads():
    p = _write(SRT)
    res = remove_ads(p, mode="adverts", window_seconds=60)
    assert res["removed"] == 3, f"expected 3 removed, got {res}"

    # Reload and confirm dialogue + the mid-file "subscribe" (out of window) survive.
    import pysubs2
    subs = pysubs2.load(str(p), encoding="utf-8-sig")
    texts = [e.plaintext for e in subs.events]
    assert "Hello, how are you?" in texts
    assert "subscribe to my newsletter" in texts  # mid-file, outside intro/outro window
    assert "patreon.com/example" not in " ".join(texts)
    assert "Subscribe for more" not in " ".join(texts)


def test_off_mode_is_noop():
    p = _write(SRT)
    res = remove_ads(p, mode="off", window_seconds=60)
    assert res["removed"] == 0
    import pysubs2
    subs = pysubs2.load(str(p), encoding="utf-8-sig")
    assert len(subs.events) == 5


def test_credits_mode_strips_group_credits_not_cues():
    srt = """1
00:00:01,000 --> 00:00:03,000
[HorribleSubs]

2
00:00:04,000 --> 00:00:06,000
Translation by Alice

3
00:00:07,000 --> 00:00:09,000
[He laughs]

4
00:00:10,000 --> 00:00:12,000
I'm going now.
"""
    p = _write(srt)
    res = remove_ads(p, mode="adverts_and_credits", window_seconds=60)
    assert res["removed"] == 2, f"expected 2 removed, got {res}"

    import pysubs2
    subs = pysubs2.load(str(p), encoding="utf-8-sig")
    texts = [e.plaintext for e in subs.events]
    assert "[He laughs]" in texts          # cue (space) must NOT be stripped
    assert "I'm going now." in texts
    assert "[HorribleSubs]" not in texts
    assert "Translation by Alice" not in texts


def test_adverts_mode_does_not_strip_credits():
    srt = """1
00:00:01,000 --> 00:00:03,000
Translation by Alice
"""
    p = _write(srt)
    res = remove_ads(p, mode="adverts", window_seconds=60)
    assert res["removed"] == 0  # credits only stripped in adverts_and_credits mode
