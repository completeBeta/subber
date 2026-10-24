"""Identification must not give up on a long official title.

A library filename carries the whole English title, which AniList's search does not
match; the first few words do. Without the retry the show is silently unidentified
and never gets its per-show glossary.
"""
import asyncio

from subber import identify as ident_mod
from subber.identify import ShowIdentity, _title_variants


def test_title_variants_shorten_progressively():
    variants = _title_variants(
        "I May Be a Guild Receptionist But Ill Solo Any Boss to Clock Out on Time"
    )
    assert variants[0] == "I May Be a Guild Receptionist But Ill"
    assert all(len(v.split()) < 10 for v in variants)


def test_title_variants_empty_for_short_titles():
    assert _title_variants("Re Zero") == []


def test_identify_retries_with_shorter_title(monkeypatch):
    calls = []

    async def fake_anilist(title):
        calls.append(title)
        if len(title.split()) <= 8:
            return ShowIdentity(anilist_id=42, anilist_title_en="Matched")
        return None

    async def fake_tmdb(*a, **k):
        return None

    monkeypatch.setattr(ident_mod, "_query_anilist", fake_anilist)
    monkeypatch.setattr(ident_mod, "_query_tmdb", fake_tmdb)

    result = asyncio.run(
        ident_mod.identify(
            "Some.Very.Long.Official.English.Title.That.AniList.Will.Not.Match.S01E01.srt"
        )
    )
    assert result.anilist_id == 42
    assert len(calls) >= 2  # the full title, then a shortened one
