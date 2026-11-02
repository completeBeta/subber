"""Multi-script typeset fixtures: the filter must not depend on style language.

The Rosario bug was caused by matching *English* style names. These fixtures build
the same shape of file in scripts/languages we have never benchmarked (Japanese,
Chinese, Thai, Arabic, Vietnamese, French, Portuguese, Spanish) and run the real
`translate()` end to end with a fake API, asserting that:

  * vector drawings and karaoke syllables never reach the model,
  * per-frame repeats collapse into one request per distinct string,
  * the chunk count stays tiny instead of exploding, and
  * every dialogue cue actually comes back translated (nothing silently
    passed through because its line was a single CJK character).
"""

import re

import pysubs2

from subber.library_pipeline import _filter_to_dialogue
from subber.translator import Translator, _plan_translation_units

ASS_HEADER = """[Script Info]
Title: fixture
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize
Style: Default,Arial,20
Style: {sign_style},Arial,20
Style: OP,Arial,20

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

DRAWING = "{\\p1\\pos(10,20)}m 0 0 l 0 1 40 1 40 0"

# (language, sign style named in that language, dialogue lines, sign text)
FIXTURES = [
    ("es", "Letreros", ["Hola, ¿qué tal?", "¡No!", "Nos vemos mañana."], "Instituto Youkai"),
    ("pt", "Letreiros", ["Olá, tudo bem?", "Não!", "Até amanhã."], "Instituto Youkai"),
    ("fr", "Panneaux", ["Bonjour, ça va ?", "Non !", "À demain."], "Institut Youkai"),
    ("vi", "Bảng hiệu", ["Chào, khỏe không?", "Không!", "Mai gặp lại."], "Học viện Youkai"),
    ("th", "ป้าย", ["สวัสดี เป็นไงบ้าง", "ไม่!", "เจอกันพรุ่งนี้"], "สถาบันโยไก"),
    ("ar", "لافتات", ["مرحبا، كيف حالك؟", "لا!", "أراك غدا."], "معهد يوكاي"),
    ("zh", "字幕", ["你好，最近怎么样？", "嗯", "明天见。"], "妖怪学园"),
    ("ja", "字幕", ["やあ、元気？", "え？", "また明日。"], "妖怪学園"),
]


def _build(tmp_path, lang, sign_style, lines, sign_text):
    body = []
    for i, text in enumerate(lines):
        body.append("Dialogue: 0,0:00:%02d.00,0:00:%02d.00,Default,,0,0,0,,%s"
                    % (i + 1, i + 2, text))
    # Sign animation: same string 40 times, plus 120 frames of pure vector drawing.
    for _ in range(40):
        body.append("Dialogue: 1,0:00:30.00,0:00:30.04,%s,,0,0,0,,%s" % (sign_style, sign_text))
    for _ in range(120):
        body.append("Dialogue: 1,0:00:31.00,0:00:31.04,%s,,0,0,0,,%s" % (sign_style, DRAWING))
    # Karaoke syllables in a recognised song style (must be dropped as a whole).
    for ch in "abcdefghij":
        body.append("Dialogue: 2,0:00:40.00,0:00:40.20,OP,,0,0,0,,%s" % ch)
    path = tmp_path / ("Show.%s.ass" % lang)
    path.write_text(ASS_HEADER.format(sign_style=sign_style) + "\n".join(body) + "\n",
                    encoding="utf-8")
    return path


class FakeAPI(Translator):
    """Translator whose API echoes '[N] EN:<source>' — no network, no cost."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = 0

    def _call_api(self, messages, temperature=None):
        self.calls += 1
        user = messages[-1]["content"]
        out = []
        for line in user.splitlines():
            m = re.match(r"^\[(\d+)\]\s*(.*)$", line)
            if m:
                # ASCII on purpose: a fake that echoed the source would trip the
                # "still in source language" leftover retry for zh/ja and inflate
                # the call count for reasons that have nothing to do with bloat.
                out.append("[%s] EN<line %s>" % (m.group(1), m.group(1)))
        return "\n".join(out)


def _run(tmp_path, case):
    lang, sign_style, lines, sign_text = case
    src = _build(tmp_path, lang, sign_style, lines, sign_text)
    filtered = _filter_to_dialogue(src)
    assert filtered is not None, "%s: filter found nothing to translate" % lang

    kept = [ln for ln in filtered.read_text(encoding="utf-8").splitlines()
            if ln.startswith("Dialogue:")]
    assert not any(DRAWING.split("}")[-1] in ln for ln in kept), \
        "%s: vector drawings leaked into the translation input" % lang
    assert not any(ln.endswith(",OP,,0,0,0,,a") for ln in kept), \
        "%s: karaoke syllables leaked into the translation input" % lang

    subs = pysubs2.load(str(filtered), encoding="utf-8-sig")
    entries = [{"index": i, "text": e.plaintext} for i, e in enumerate(subs.events)]
    translatable, duplicates, passthrough = _plan_translation_units(entries)
    # 3 dialogue lines + 1 distinct sign string; 40 sign frames collapse. The 120
    # drawings were already dropped by the filter, so nothing reaches passthrough.
    assert len(translatable) == 4, "%s: planned %d requests (%s)" % (
        lang, len(translatable), [e["text"] for e in translatable])
    assert len(duplicates) == 39, "%s: %d repeats collapsed" % (lang, len(duplicates))
    assert len(passthrough) == 0, "%s: %d drawings survived the filter" % (lang, len(passthrough))

    t = FakeAPI(api_key="x", model="fake", chunk_size=20)
    out = tmp_path / ("Show.%s.en.ass" % lang)
    t.translate(filtered, out, lang, "en")
    assert t.calls <= 2, "%s: %d API calls for 4 cues" % (lang, t.calls)

    produced = pysubs2.load(str(out), encoding="utf-8-sig")
    texts = [e.plaintext for e in produced.events]
    for i, line in enumerate(lines):
        assert texts[i].startswith("EN<"), "%s: cue %d not translated (%r)" % (
            lang, i, texts[i])
    assert produced.events[3].plaintext.startswith("EN<"), "%s: sign not translated" % lang
    return len(translatable), t.calls


def test_multiscript_typesets_stay_small(tmp_path):
    results = {}
    for case in FIXTURES:
        sub = tmp_path / case[0]
        sub.mkdir()
        results[case[0]] = _run(sub, case)
    # Every script plans exactly 4 cues (3 dialogue + 1 sign) and needs <= 2 calls.
    assert all(plan == 4 for plan, _calls in results.values()), results
    assert all(calls <= 2 for _plan, calls in results.values()), results


def test_translator_passthrough_guard_catches_drawings_in_dialogue_style(tmp_path):
    """A file whose typeset lives in "Default" can't be filtered by style name —
    the translator itself must refuse to send drawings to the model."""
    path = tmp_path / "Show.es.ass"
    path.write_text(ASS_HEADER.format(sign_style="Letreros") + "\n".join([
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hola",
        "Dialogue: 0,0:00:02.00,0:00:02.04,Default,,0,0,0,," + DRAWING,
        "Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,Adios",
    ]) + "\n", encoding="utf-8")

    subs = pysubs2.load(str(path), encoding="utf-8-sig")
    entries = [{"index": i, "text": e.plaintext} for i, e in enumerate(subs.events)]
    translatable, duplicates, passthrough = _plan_translation_units(entries)
    assert [e["text"] for e in translatable] == ["Hola", "Adios"]
    assert passthrough == {1}
    assert duplicates == {}

    t = FakeAPI(api_key="x", model="fake", chunk_size=20)
    out = tmp_path / "Show.es.en.ass"
    t.translate(path, out, "es", "en")
    assert t.calls == 1
    produced = pysubs2.load(str(out), encoding="utf-8-sig")
    # The drawing is copied back untouched so the typesetting still renders.
    assert produced.events[1].plaintext == DRAWING.split("}")[-1]
    assert produced.events[0].plaintext.startswith("EN<")
