"""Tests for character-name seeding (show identification → translator prompt).

The translator romanizes Japanese names from audio ("瓜生" → "Umino"/"Urio"/
"Uri-no"); seeding AniList's native→romaji mapping makes it consistent.
"""
import asyncio
from unittest import mock

from subber.translator import Translator, _format_character_map


def test_format_character_map():
    cm = [
        {"native": "小鳩常悟朗", "full": "Jougorou Kobato"},
        {"native": "小佐内ゆき", "full": "Yuki Osanai"},
    ]
    out = _format_character_map(cm)
    assert "小鳩常悟朗 = Jougorou Kobato" in out
    assert "小佐内ゆき = Yuki Osanai" in out
    assert "canonical" in out.lower()


def test_format_character_map_empty():
    assert _format_character_map(None) == ""
    assert _format_character_map([]) == ""
    assert _format_character_map([{"native": "", "full": ""}]) == ""
    assert _format_character_map([{"native": "小鳩", "full": ""}]) == ""


def test_translate_injects_character_names(tmp_path):
    src = tmp_path / "in.srt"
    src.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nこんにちは、小鳩さん\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.srt"

    t = Translator(api_key="x", model="m", chunk_size=50)
    captured = {}

    def fake_call_api(messages):
        captured["system"] = messages[0]["content"]
        return "[0] Hello, Kobato"

    with mock.patch.object(t, "_call_api", side_effect=fake_call_api):
        t.translate(
            src, out, "ja", "en",
            character_map=[{"native": "小鳩", "full": "Kobato"}],
        )

    assert "小鳩 = Kobato" in captured["system"]


def test_translate_no_character_map_no_injection(tmp_path):
    src = tmp_path / "in.srt"
    src.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.srt"

    t = Translator(api_key="x", model="m", chunk_size=50)
    captured = {}

    def fake_call_api(messages):
        captured["system"] = messages[0]["content"]
        return "[0] Hello"

    with mock.patch.object(t, "_call_api", side_effect=fake_call_api):
        t.translate(src, out, "ja", "en")

    assert "Canonical character names" not in captured["system"]


def test_identify_returns_characters(monkeypatch):
    from subber import identify

    identify._cache.clear()

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"Page": {"media": [{
                "id": 12345,
                "title": {"romaji": "Unique Test Anime", "english": "Unique Test Anime"},
                "synonyms": [],
                "characters": {"nodes": [
                    {"name": {"full": "Jougorou Kobato", "native": "小鳩常悟朗"}},
                    {"name": {"full": "Yuki Osanai", "native": "小佐内ゆき"}},
                ]},
            }]}}}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(identify.httpx, "AsyncClient", FakeClient)
    ident = asyncio.run(identify.identify("Unique.Test.Anime.S01E01.mkv"))

    assert len(ident.characters) == 2
    assert ident.characters[0]["native"] == "小鳩常悟朗"
    assert ident.characters[0]["full"] == "Jougorou Kobato"
