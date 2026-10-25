"""Term mining: learn a show's vocabulary from its own translated episodes."""
from unittest import mock

import pytest

from subber import show_glossary, term_mining


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBBER_GLOSSARY_DIR", str(tmp_path / "glossary"))
    return tmp_path / "glossary"


def _pairs(*rows):
    """rows of (japanese, english) -> (entries, translated)."""
    entries = [{"index": i, "text": ja} for i, (ja, _) in enumerate(rows)]
    translated = {i: en for i, (_, en) in enumerate(rows)}
    return entries, translated


def _filler(n):
    return [("これは普通の文です" if i % 2 else "まったく別の話をします", f"Just an ordinary line number {i} about something else") for i in range(n)]


# ── candidate extraction ────────────────────────────────────────────────

def test_candidates_pick_katakana_and_kanji():
    found = term_mining._candidates("（ミミ）リベンジだ！ 魔女教の連中め")
    assert "リベンジ" in found
    assert "ミミ" in found
    assert "魔女教" in found


def test_candidates_skip_common_words():
    assert term_mining._candidates("今日は本当に大丈夫です") == set()


# ── scoring ─────────────────────────────────────────────────────────────

def test_mines_a_consistently_translated_term():
    rows = _filler(20) + [
        ("リベンジするぞ", "I'll get my Revenger Blade."),
        ("リベンジだ", "This is Revenger Blade."),
        ("リベンジを見ろ", "Watch Revenger Blade."),
        ("リベンジ", "Revenger Blade!"),
    ]
    entries, translated = _pairs(*rows)
    mined = term_mining.mine_terms(entries, translated)

    assert "リベンジ" in mined
    assert mined["リベンジ"]["target"] == "Revenger Blade"
    assert mined["リベンジ"]["n"] == 4
    assert mined["リベンジ"]["score"] > 0.9


def test_rejects_a_word_rendered_inconsistently():
    rows = _filler(20) + [
        ("約束だ", "It's a promise."),
        ("約束を守れ", "Keep your word."),
        ("約束した", "You agreed."),
        ("約束", "A vow."),
    ]
    entries, translated = _pairs(*rows)
    mined = term_mining.mine_terms(entries, translated)
    assert "約束" not in mined


def test_lowercase_common_kanji_word_is_not_learned():
    """Ordinary words render lowercase; they are not terminology."""
    rows = [("約束の時間だ", "I promise today")] * 10
    rows += [(f"普通の{i}番", "nothing today here") for i in range(20)]
    entries, translated = _pairs(*rows)
    assert "約束" not in term_mining.mine_terms(entries, translated)


def test_possessive_is_folded_out_of_the_term():
    rows = _filler(20) + [
        ("白鯨だ", "It's the White Whale's roar."),
        ("白鯨が来る", "The White Whale's here."),
        ("白鯨を討て", "Hunt the White Whale."),
        ("白鯨", "White Whale!"),
    ]
    entries, translated = _pairs(*rows)
    mined = term_mining.mine_terms(entries, translated)
    assert mined["白鯨"]["target"] == "White Whale"


def test_kanji_term_prefers_the_full_phrase_over_the_fragment():
    """大罪司教 is "Sin Archbishop"; injecting "Sin" measurably hurts."""
    rows = _filler(20) + [
        ("大罪司教だ", "He is a Sin Archbishop."),
        ("大罪司教が来た", "A Sin Archbishop came."),
        ("大罪司教", "Sin Archbishop!"),
        ("大罪司教め", "That Sin fellow."),
        ("大罪司教の仕業", "The work of a Sin"),
    ]
    entries, translated = _pairs(*rows)
    mined = term_mining.mine_terms(entries, translated)
    assert mined["大罪司教"]["target"] == "Sin Archbishop"


def test_kanji_term_without_a_safe_phrase_is_skipped():
    """Only scattered fragments → learn nothing rather than a fragment."""
    rows = _filler(20) + [
        ("白鯨が来る", "The White is coming."),
        ("白鯨だ", "It's White."),
        ("白鯨", "White!"),
        ("白鯨を討て", "Hunt White."),
    ]
    entries, translated = _pairs(*rows)
    assert "白鯨" not in term_mining.mine_terms(entries, translated)


def test_prefers_the_full_phrase():
    """魔女教 → "Witch Cult", not just "Witch"."""
    rows = _filler(20) + [
        ("魔女教だ", "It's the Witch Cult."),
        ("魔女教の連中", "Those Witch Cult people."),
        ("魔女教が来た", "The Witch Cult came."),
        ("魔女教", "Witch Cult!"),
    ]
    entries, translated = _pairs(*rows)
    mined = term_mining.mine_terms(entries, translated)
    assert mined["魔女教"]["target"] == "Witch Cult"


def test_rejects_single_occurrences():
    rows = _filler(20) + [("リベンジ", "Revenger")]
    entries, translated = _pairs(*rows)
    assert "リベンジ" not in term_mining.mine_terms(entries, translated)


def test_small_file_learns_nothing():
    entries, translated = _pairs(("リベンジ", "Revenger"), ("リベンジ", "Revenger"))
    assert term_mining.mine_terms(entries, translated) == {}


# ── persistence ─────────────────────────────────────────────────────────

def test_learn_writes_terms_into_the_show_glossary(store):
    ident = show_glossary.ensure_for_identity(_ident(11))
    assert ident  # file exists before learning
    rows = _filler(20) + [
        ("リベンジするぞ", "I'll get my Revenger Blade."),
        ("リベンジだ", "This is Revenger Blade."),
        ("リベンジを見ろ", "Watch Revenger Blade."),
        ("リベンジ", "Revenger Blade!"),
    ]
    entries, translated = _pairs(*rows)

    result = term_mining.learn("anilist-11", entries, translated, {"cues": len(rows)})
    assert any("リベンジ" in a for a in result["added"])

    data = show_glossary.load("anilist-11")
    assert data["terms"]["リベンジ"] == "Revenger Blade"
    assert data["term_stats"]["リベンジ"]["n"] == 4


def test_learn_keeps_existing_term_and_reports_conflict(store):
    show_glossary.ensure_for_identity(_ident(12))
    data = show_glossary.load("anilist-12")
    data["terms"] = {"魔女教": "Witch Cult"}
    show_glossary.save("anilist-12", data)

    rows = _filler(20) + [
        ("魔女教が来た", "The Witch Coven arrived."),
        ("魔女教だ", "It's the Witch Coven."),
        ("魔女教", "Witch Coven!"),
        ("魔女教を見ろ", "Watch the Witch Coven."),
    ]
    entries, translated = _pairs(*rows)
    result = term_mining.learn("anilist-12", entries, translated, {"cues": len(rows)})

    assert show_glossary.load("anilist-12")["terms"]["魔女教"] == "Witch Cult"  # never overwritten
    assert any("魔女教" in c for c in result["conflicts"])


def test_learn_refuses_a_file_that_translated_badly(store):
    show_glossary.ensure_for_identity(_ident(13))
    rows = _filler(20) + [
        ("リベンジ", "Revenger"), ("リベンジ", "Revenger"),
        ("リベンジ", "Revenger"), ("リベンジ", "Revenger"),
    ]
    entries, translated = _pairs(*rows)
    qa = {"cues": len(rows), "empty": 4, "untranslated": 2}  # 25% failures
    result = term_mining.learn("anilist-13", entries, translated, qa)
    assert result["added"] == []
    assert show_glossary.load("anilist-13").get("terms") == {}


def test_learn_caps_the_glossary(store):
    show_glossary.ensure_for_identity(_ident(14))
    data = show_glossary.load("anilist-14")
    data["terms"] = {f"既存{i}": f"Existing{i}" for i in range(term_mining.MAX_TERMS)}
    show_glossary.save("anilist-14", data)

    rows = _filler(20) + [
        ("リベンジするぞ", "I'll get my Revenger Blade."),
        ("リベンジだ", "This is Revenger Blade."),
        ("リベンジを見ろ", "Watch Revenger Blade."),
        ("リベンジ", "Revenger Blade!"),
    ]
    entries, translated = _pairs(*rows)
    result = term_mining.learn("anilist-14", entries, translated, {"cues": len(rows)})
    assert result["added"] == []
    assert len(show_glossary.load("anilist-14")["terms"]) == term_mining.MAX_TERMS


def test_nickname_fragment_of_a_known_name_is_not_learned(store):
    """リア is part of エミリア — learning the model's guess would cement it."""
    names = {"エミリア": "Emilia"}
    assert term_mining.is_name_fragment("リア", names) is True
    assert term_mining.is_name_fragment("ペテルギウス", {"ペテルギウス・ロマネコンティ": "Petelgeuse"}) is True
    assert term_mining.is_name_fragment("魔女教", names) is False

    show_glossary.ensure_for_identity(_ident(16))  # seeds エミリア: Emilia
    rows = _filler(20) + [
        ("リアが来た", "Rem Witch arrived."),
        ("リアだ", "It's Rem Witch."),
        ("リア", "Rem Witch!"),
        ("リアを見ろ", "Watch Rem Witch."),
    ]
    entries, translated = _pairs(*rows)
    result = term_mining.learn("anilist-16", entries, translated, {"cues": len(rows)})

    assert "リア" not in show_glossary.load("anilist-16")["terms"]
    assert any("リア" in s for s in result["skipped"])


def test_two_candidates_cannot_claim_the_same_phrase():
    """咆哮 ("roar") rides on "Hellflame Dragon" co-occurrence; drop the weaker one."""
    rows = _filler(20) + [
        ("ヘルフレイムドラゴンの咆哮", "The Hellflame Dragon's roar."),
        ("ヘルフレイムドラゴンだ", "It's the Hellflame Dragon."),
        ("ヘルフレイムドラゴンが来る", "The Hellflame Dragon comes."),
        ("咆哮", "A Hellflame Dragon roar."),
    ]
    entries, translated = _pairs(*rows)
    mined = term_mining.mine_terms(entries, translated)
    targets = [v["target"].lower() for v in mined.values()]
    assert len(targets) == len(set(targets))
    assert mined.get("ヘルフレイムドラゴン", {}).get("target") == "Hellflame Dragon"


def test_learn_without_key_is_a_noop(store):
    entries, translated = _pairs(("リベンジ", "Revenger"))
    assert term_mining.learn(None, entries, translated)["added"] == []


# ── end to end through the translator ───────────────────────────────────

def test_is_japanese_source_flags_only_japanese():
    assert show_glossary.is_japanese_source("ja")
    assert show_glossary.is_japanese_source("JPN")
    assert show_glossary.is_japanese_source("japanese")
    assert not show_glossary.is_japanese_source("vi")
    assert not show_glossary.is_japanese_source("en")
    assert not show_glossary.is_japanese_source(None)


def test_learning_is_skipped_for_a_non_japanese_source(store, tmp_path):
    """A Vietnamese file must not teach a Japanese glossary anything."""
    show_glossary.ensure_for_identity(_ident(21))
    lines = ["Tôi sẽ trở nên mạnh mẽ"] * 24
    srt = tmp_path / "in.srt"
    srt.write_text(
        "".join(
            f"{i+1}\n00:00:{i:02d},000 --> 00:00:{i:02d},800\n{text}\n\n"
            for i, text in enumerate(lines)
        ),
        encoding="utf-8",
    )

    from subber.translator import Translator

    translator = Translator(api_key="x", model="m", chunk_size=50, glossary_key="anilist-21")

    def fake_api(messages, *a, **k):
        body = messages[-1]["content"]
        out = []
        for block in body.split("[")[1:]:
            num, _, _rest = block.partition("]")
            if num.isdigit():
                out.append(f"[{num}] I will become strong Magical Girl")
        return "\n".join(out)

    with mock.patch.object(translator, "_call_api", side_effect=fake_api):
        translator.translate(srt, tmp_path / "out.srt", "vi", "en")

    assert show_glossary.load("anilist-21").get("terms") == {}


def test_translation_teaches_the_show_glossary(store, tmp_path):
    show_glossary.ensure_for_identity(_ident(15))
    lines = ["リベンジするぞ"] * 4 + ["これは普通の文です"] * 20
    srt = tmp_path / "in.srt"
    srt.write_text(
        "".join(
            f"{i+1}\n00:00:{i:02d},000 --> 00:00:{i:02d},800\n{text}\n\n"
            for i, text in enumerate(lines)
        ),
        encoding="utf-8",
    )

    t = term_mining  # keep import used
    translator = __import__("subber.translator", fromlist=["Translator"]).Translator(
        api_key="x", model="m", chunk_size=50, glossary_key="anilist-15"
    )

    def fake_api(messages, *a, **k):
        body = messages[-1]["content"]
        out = []
        for block in body.split("[")[1:]:
            num, _, rest = block.partition("]")
            if num.isdigit():
                en = "I'll get my Revenger Blade." if "リベンジ" in rest else "Just an ordinary line here"
                out.append(f"[{num}] {en}")
        return "\n".join(out)

    with mock.patch.object(translator, "_call_api", side_effect=fake_api):
        translator.translate(srt, tmp_path / "out.srt", "ja", "en")

    data = show_glossary.load("anilist-15")
    assert data["terms"].get("リベンジ") == "Revenger Blade"
    assert translator.last_qa.get("terms_learned", 0) >= 1


def _ident(anilist_id):
    from types import SimpleNamespace

    return SimpleNamespace(
        anilist_id=anilist_id,
        tmdb_id=None,
        anilist_title_romaji=f"Show {anilist_id}",
        anilist_title_en=None,
        tmdb_title=None,
        parsed_title=None,
        characters=[{"native": "エミリア", "full": "Emilia"}],
    )
