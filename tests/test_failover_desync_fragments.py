"""Failover parity, one-cue desync detection, and glossary fragment pruning.

Three defects, one file, because each one is a case where the same input produced
two different behaviours depending on which code path handled it:

1. **Failover parity** — the interactive Translate tab went through
   `MultiBackendTranslator` (which logged the failover) while the library pipeline
   hand-rolled its own loop (which could not be cancelled mid-file and swallowed
   `TranslationCancelled` as a backend error). Both now use the same wrapper, and a
   total failure raises instead of returning the model name "none".

2. **One-cue desync** — a compensating merge (two cues translated as one line,
   another split, line count preserved) was detected via SDH speaker labels only.
   Files without labels had no defence; numbers, questions and glossary terms
   survive translation and now serve as anchors.

3. **Thai/Chinese fragment keys** — those scripts have no spaces, so the fragment
   check compared word counts (always 1 vs 1) and pruned nothing: "โรงเรีย" was
   stored alongside "โรงเรียน". Characters are compared now, and a stored key that
   only ever appears inside a longer stored key is removed.
"""
from pathlib import Path

import pytest

from subber import show_glossary, term_mining
from subber.translator import MultiBackendTranslator, Translator, TranslationCancelled


# ── failover parity ──────────────────────────────────────────────────────

def _backends():
    return [
        {"name": "A", "api_base": "http://x/v1", "api_key": "k", "model": "model-a"},
        {"name": "B", "api_base": "http://x/v1", "api_key": "k", "model": "model-b"},
    ]


def test_total_failure_raises_instead_of_returning_none(monkeypatch):
    tried = []

    def _boom(self, *a, **k):
        tried.append(self.model)
        raise RuntimeError("provider down")

    monkeypatch.setattr(Translator, "translate", _boom)
    multi = MultiBackendTranslator(_backends())

    with pytest.raises(RuntimeError) as err:
        multi.translate(Path("in.srt"), Path("out.srt"), "fr")

    assert tried == ["model-a", "model-b"]      # both were attempted
    assert "All 2 translation backends failed" in str(err.value)
    assert "A:" in str(err.value) and "B:" in str(err.value)
    assert len(multi.last_errors) == 2


def test_no_backends_is_an_error_not_a_silent_no_op():
    with pytest.raises(RuntimeError):
        MultiBackendTranslator([]).translate(Path("in.srt"), Path("out.srt"), "fr")


def test_cancel_is_never_treated_as_a_backend_failure(monkeypatch):
    tried = []

    def _cancel(self, *a, **k):
        tried.append(self.model)
        raise TranslationCancelled("Job cancelled by user")

    monkeypatch.setattr(Translator, "translate", _cancel)
    multi = MultiBackendTranslator(_backends())

    with pytest.raises(TranslationCancelled):
        multi.translate(Path("in.srt"), Path("out.srt"), "fr")

    assert tried == ["model-a"]   # the second backend must not be tried


def test_fallback_returns_the_backend_that_worked(monkeypatch):
    def _first_fails(self, *a, **k):
        if self.model == "model-a":
            raise RuntimeError("rate limited")
        return Path("out.srt")

    monkeypatch.setattr(Translator, "translate", _first_fails)
    multi = MultiBackendTranslator(_backends())

    assert multi.translate(Path("in.srt"), Path("out.srt"), "fr") == "model-b"


# ── one-cue desync detection ─────────────────────────────────────────────

_SOURCES = [
    "3人で行く？",        # number + question
    "7時から雨だ",        # number
    "本当に？",           # question
    "たぶんね",           # no anchor
    "12時に会おう",       # number
    "わかった",           # no anchor
]
_CORRECT = [
    "Are 3 of us going?",
    "It will rain from 7.",
    "Really?",
    "Probably.",
    "Let's meet at 12.",
    "Got it.",
]


def _entries():
    return [{"index": i, "text": t} for i, t in enumerate(_SOURCES)]


def _shifted():
    """Translation lagging one cue: translated[i] holds cue i-1's text."""
    out = {0: ""}
    for i in range(1, len(_CORRECT)):
        out[i] = _CORRECT[i - 1]
    return out


def test_anchor_desync_detected_and_repaired_without_speaker_labels():
    t = Translator(api_key="x", model="m", chunk_size=50)
    translated = _shifted()

    def fake_chunk(chunk, *a, **k):
        i = chunk[0]["index"]
        assert len(chunk) == 1
        return f"[{i}] {_CORRECT[i]}"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(t, "_translate_chunk", fake_chunk)
        windows, fixed = t._repair_anchor_desync(_entries(), translated, "ja", "en")

    assert windows == 1
    assert fixed == len(_CORRECT)
    assert [translated[i] for i in range(len(_CORRECT))] == _CORRECT


def test_aligned_translation_is_left_alone():
    t = Translator(api_key="x", model="m", chunk_size=50)
    translated = dict(enumerate(_CORRECT))

    with pytest.MonkeyPatch.context() as mp:
        calls = []
        mp.setattr(t, "_translate_chunk", lambda *a, **k: calls.append(1) or "[0] x")
        assert t._repair_anchor_desync(_entries(), translated, "ja", "en") == (0, 0)

    assert calls == []   # detection costs nothing when nothing is violated


def test_a_paraphrased_line_is_not_mistaken_for_a_shift():
    """One cue legitimately drops its "?" — that alone must not trigger repairs."""
    t = Translator(api_key="x", model="m", chunk_size=50)
    translated = {
        0: "Are 3 of us going?",     # keeps both anchors
        1: "It will rain from 7.",
        2: "You're serious.",        # question mark legitimately gone
        3: "Probably.",
        4: "Let's meet at 12.",
        5: "Got it.",
    }

    with pytest.MonkeyPatch.context() as mp:
        calls = []
        mp.setattr(t, "_translate_chunk", lambda *a, **k: calls.append(1) or "[2] Really?")
        assert t._repair_anchor_desync(_entries(), translated, "ja", "en") == (0, 0)

    assert calls == []


def test_a_file_with_too_few_anchors_is_not_guessed_at():
    t = Translator(api_key="x", model="m", chunk_size=50)
    entries = [{"index": i, "text": f"ただの文{i}"} for i in range(10)]
    translated = {i: f"Just line {i}" for i in range(10)}

    with pytest.MonkeyPatch.context() as mp:
        calls = []
        mp.setattr(t, "_translate_chunk", lambda *a, **k: calls.append(1) or "[0] x")
        assert t._repair_anchor_desync(entries, translated, "ja", "en") == (0, 0)

    assert calls == []


def test_glossary_terms_are_used_as_anchors():
    """A listed term appearing in the wrong cue is a shift, with no digits in sight."""
    t = Translator(api_key="x", model="m", chunk_size=50,
                   glossary=["リア = Lia", "スバル = Subaru"])
    entries = [
        {"index": 0, "text": "リアはどこだ"},
        {"index": 1, "text": "知らない"},
        {"index": 2, "text": "スバルが呼んでる"},
        {"index": 3, "text": "すぐ行く"},
        {"index": 4, "text": "リアを探せ"},
        {"index": 5, "text": "スバルに言う"},
    ]
    correct = {
        0: "Where's Lia?",
        1: "No idea.",
        2: "Subaru is calling.",
        3: "Coming right away.",
        4: "Find Lia.",
        5: "Tell Subaru.",
    }
    # translated lags by one cue: each line sits one cue later than it belongs
    translated = {0: ""}
    for i in range(1, len(correct)):
        translated[i] = correct[i - 1]

    def fake_chunk(chunk, *a, **k):
        i = chunk[0]["index"]
        return f"[{i}] {correct[i]}"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(t, "_translate_chunk", fake_chunk)
        windows, fixed = t._repair_anchor_desync(entries, translated, "ja", "en")

    assert windows == 1
    assert fixed == len(correct)
    assert [translated[i] for i in range(len(correct))] == [correct[i] for i in range(len(correct))]


# ── Thai / Chinese fragment keys ─────────────────────────────────────────

_THAI_SCHOOL = "โรงเรียน"      # "school" — no spaces, so it is a run of characters
_THAI_FRAGMENT = "โรงเรีย"     # a 7-character slice of it: never a word on its own
_THAI_OTHER = "วันนี้อากาศดีมาก"


def _thai_rows(extra=None):
    rows = []
    for i in range(20):
        rows.append((_THAI_OTHER, f"The weather is nice today number {i}"))
    for _ in range(8):
        rows.append(("ไป" + _THAI_SCHOOL + "กันเถอะ", "Let's go to school."))
    if extra:
        rows.extend(extra)
    return rows


def _thai_pairs(rows):
    entries = [{"index": i, "text": th} for i, (th, _) in enumerate(rows)]
    translated = {i: en for i, (_, en) in enumerate(rows)}
    return entries, translated


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBBER_GLOSSARY_DIR", str(tmp_path / "glossary"))
    return tmp_path / "glossary"


def _seed(key, terms):
    show_glossary.ensure_for_identity(type("I", (), {
        "source": "anilist", "show_id": int(key.split("-")[1]), "title": "x",
        "characters": [], "episodes": None,
    })())
    data = show_glossary.load(key)
    data["terms"] = terms
    show_glossary.save(key, data)


def test_stored_fragment_key_is_pruned_when_it_never_appears_alone(store):
    _seed("anilist-21", {"th": {_THAI_FRAGMENT: "School", _THAI_SCHOOL: "School"}})
    entries, translated = _thai_pairs(_thai_rows())

    result = term_mining.learn("anilist-21", entries, translated, {"cues": len(entries)}, "th")

    terms = show_glossary.load("anilist-21")["terms"]["th"]
    assert _THAI_FRAGMENT not in terms
    assert terms[_THAI_SCHOOL] == "School"
    assert _THAI_FRAGMENT in result["pruned"]


def test_a_key_that_also_appears_alone_is_kept(store):
    _seed("anilist-22", {"th": {_THAI_FRAGMENT: "School", _THAI_SCHOOL: "School"}})
    # one cue uses the short form on its own — it is a word here, not a fragment
    entries, translated = _thai_pairs(_thai_rows(extra=[(_THAI_FRAGMENT, "School!")]))

    result = term_mining.learn("anilist-22", entries, translated, {"cues": len(entries)}, "th")

    terms = show_glossary.load("anilist-22")["terms"]["th"]
    assert _THAI_FRAGMENT in terms
    assert result["pruned"] == []


def test_a_shared_prefix_with_a_different_meaning_is_not_pruned(store):
    _seed("anilist-23", {"th": {_THAI_FRAGMENT: "Sick", _THAI_SCHOOL: "School"}})
    entries, translated = _thai_pairs(_thai_rows())

    term_mining.learn("anilist-23", entries, translated, {"cues": len(entries)}, "th")

    terms = show_glossary.load("anilist-23")["terms"]["th"]
    assert _THAI_FRAGMENT in terms      # different rendering → different term


def test_miner_never_returns_a_key_contained_only_in_a_longer_key():
    rows = _thai_rows()
    entries, translated = _thai_pairs(rows)
    mined = term_mining.mine_terms(entries, translated, "th")

    for short in mined:
        for long in mined:
            if short == long or short not in long:
                continue
            cues = [e["text"] for e in entries]
            n_short = sum(1 for t in cues if short in t)
            n_both = sum(1 for t in cues if short in t and long in t)
            if term_mining._related_targets(mined[short]["target"], mined[long]["target"]):
                assert not (n_short and n_both == n_short), (
                    f"{short!r} only ever appears inside {long!r}"
                )
