"""Per-show glossary store: created and maintained by the app, never hand-edited."""
from types import SimpleNamespace

import pytest
import yaml

from subber import config, show_glossary


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBBER_GLOSSARY_DIR", str(tmp_path / "glossary"))
    yield tmp_path / "glossary"


def _identity(**kw):
    base = dict(
        anilist_id=None,
        tmdb_id=None,
        anilist_title_romaji=None,
        anilist_title_en=None,
        tmdb_title=None,
        parsed_title=None,
        characters=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_key_prefers_anilist_then_tmdb():
    assert show_glossary.key_for(_identity(anilist_id=123, tmdb_id=9)) == "anilist-123"
    assert show_glossary.key_for(_identity(tmdb_id=9)) == "tmdb-9"
    assert show_glossary.key_for(_identity()) is None
    assert show_glossary.key_for(None) is None


def test_creates_file_seeded_with_anilist_names(store):
    ident = _identity(
        anilist_id=42,
        anilist_title_romaji="Re:Zero kara Hajimeru Isekai Seikatsu",
        characters=[{"native": "リア", "full": "Lia"}, {"native": "ペテルギウス", "full": "Petelgeuse"}],
    )
    data = show_glossary.ensure_for_identity(ident)

    assert data["names"] == {"リア": "Lia", "ペテルギウス": "Petelgeuse"}
    assert data["title"].startswith("Re:Zero")
    assert data["terms"] == {}

    on_disk = yaml.safe_load((store / "anilist-42.yaml").read_text())
    assert on_disk["names"]["リア"] == "Lia"
    assert on_disk["source"] == "anilist"


def test_second_pass_is_idempotent_and_merges_new_names(store):
    ident = _identity(anilist_id=42, characters=[{"native": "リア", "full": "Lia"}])
    show_glossary.ensure_for_identity(ident)
    first = (store / "anilist-42.yaml").read_text()

    show_glossary.ensure_for_identity(ident)
    assert (store / "anilist-42.yaml").read_text() == first  # no rewrite when nothing changed

    ident2 = _identity(anilist_id=42, characters=[{"native": "リア", "full": "Lia"}, {"native": "ティビー", "full": "Tibby"}])
    data = show_glossary.ensure_for_identity(ident2)
    assert data["names"] == {"リア": "Lia", "ティビー": "Tibby"}


def test_terms_for_identified_show_excludes_global_list(store):
    ident = _identity(anilist_id=7, characters=[{"native": "リア", "full": "Lia"}])
    show_glossary.ensure_for_identity(ident)

    terms = show_glossary.terms_for(
        ident, "ja", global_terms=["魔女教 = Witch Cult"], have_names=True
    )
    # names arrive through the character map, so only show terms (none yet) appear
    assert terms == []


def test_terms_for_folds_names_when_no_character_map(store):
    ident = _identity(anilist_id=7, characters=[{"native": "リア", "full": "Lia"}])
    show_glossary.ensure_for_identity(ident)

    terms = show_glossary.terms_for(ident, "ja", global_terms=["魔女教 = Witch Cult"], have_names=False)
    assert terms == ["リア = Lia"]


def test_terms_for_unknown_show_falls_back_to_global(store):
    assert show_glossary.terms_for(None, "ja", global_terms=["魔女教 = Witch Cult"]) == [
        "魔女教 = Witch Cult"
    ]
    assert show_glossary.terms_for(_identity(parsed_title="Unmatched"), "ja", global_terms=["a = b"]) == ["a = b"]


def test_stored_terms_are_injected(store):
    ident = _identity(anilist_id=9)
    show_glossary.ensure_for_identity(ident)
    data = show_glossary.load("anilist-9")
    data["terms"] = {"ja": {"福音書": "Gospel"}}
    show_glossary.save("anilist-9", data)

    assert show_glossary.terms_for(ident, "ja", have_names=True) == ["福音書 = Gospel"]


def test_describe_summarises_for_logs(store):
    ident = _identity(anilist_id=5, anilist_title_en="Some Show", characters=[{"native": "a", "full": "A"}])
    show_glossary.ensure_for_identity(ident)
    text = show_glossary.describe("anilist-5")
    assert "anilist-5" in text and "Some Show" in text and "1 names" in text


def test_glossary_dir_follows_config_location(monkeypatch, tmp_path):
    monkeypatch.delenv("SUBBER_GLOSSARY_DIR", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("translation: {}\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg)
    assert show_glossary.glossary_dir() == tmp_path / "glossary"
