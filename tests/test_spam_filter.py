"""Tests for the provider spam/advert filter (paid-subtitle listings)."""

from subber.providers.registry import _filter_spam
from subber.types import SubtitleResult


def _r(filename, release_info=""):
    return SubtitleResult(
        id="test", filename=filename, language="en",
        provider="SubDL", release_info=release_info,
    )


SCAM = [
    # The observed "Get A to Z … for ₹500 only" listing.
    "Black Summoner - S01 English[CC] (Get A to Z English Dubbed Anime English CC Subtitles for ₹500 only).zip.srt",
    "Naruto S01 HULU English[CC] (English CC Subtitles for ₹500 only).zip.srt",
    # Currency amounts in any form.
    "Show S01 $5 English.srt",
    "Show S01 €5.srt",
    "Show S01 USD 500.srt",
    # Known scam brand alone.
    "Show S01 (Get A to Z).srt",
    # Contact solicitation.
    "Show S01 contact me @gmail.com.srt",
    "Show S01 WhatsApp +91 12345.srt",
    "Show S01 join telegram for full file.srt",
]


LEGIT = [
    "Naruto S01E01 English.srt",
    "Breaking.Bad.S01E01.720p.BluRay.x264-ORPHEUS.srt",
    "Shoshimin.How.To.Become.Ordinary.S02E01.Arabic.zip.srt",
    "[Judas] Bungo Stray Dogs - S01E01 [1080p].srt",
    # Regression guards: show titles containing spam-adjacent words.
    "Selling Sunset S01E01 English.srt",
    "The Price Is Right S01E01.srt",
]


def test_scam_results_are_dropped():
    for fn in SCAM:
        assert _filter_spam([_r(fn)]) == [], f"scam not dropped: {fn}"


def test_legit_results_are_kept():
    for fn in LEGIT:
        out = _filter_spam([_r(fn)])
        assert len(out) == 1, f"legit dropped: {fn}"


def test_spam_marker_in_release_info_also_drops():
    # Marker lives in release_info rather than filename.
    r = _r("Show S01 English.srt", release_info="Get A to Z English Dubbed Anime for ₹500")
    assert _filter_spam([r]) == []


def test_mixed_list_keeps_only_legit():
    legit = _r("Naruto S01E01 English.srt")
    scam = _r("Naruto S01 English[CC] (for ₹500 only).zip.srt")
    out = _filter_spam([legit, scam])
    assert out == [legit]
