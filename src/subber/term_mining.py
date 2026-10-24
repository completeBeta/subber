"""Learn a show's terminology from the episodes we already translated.

Phase 1 seeds a show glossary with AniList character names. That covers cast
members a database knows about, and nothing else: nicknames (リア for Emilia),
minor characters (ティビー), invented terms (魔女教, 福音書) stay unlisted and drift
between episodes.

Phase 2 fills that gap without asking anyone for input. After a file is translated
we hold sentence-aligned pairs — every source cue next to the English it produced —
which is exactly what bilingual lexicon induction needs. For each repeated Japanese
candidate (katakana run or kanji compound) we score how consistently one English
token shows up in the cues containing it, discounted by how common that token is
across the whole episode:

    consistency      = cues with term AND token / cues with term
    distinctiveness  = cues with term AND token / cues with token
    score            = consistency * distinctiveness

A real term scores high on both ("リベンジ" appears 6 times and almost always brings
"Revenger" with it, while "Revenger" is rare elsewhere). A common word scores low on
one axis or the other ("今日"/"today" is everywhere, so "today" isn't distinctive of
it; a word rendered five different ways isn't consistent). Only terms that clear
both bars are stored, which is why an uncertain file learns nothing instead of
poisoning the glossary — a partial or wrong term list measurably degrades
translation, so silence is the safe default.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

import yaml

from . import show_glossary

# Japanese candidate shapes: katakana runs (names, loanwords) and kanji compounds.
_KATAKANA = re.compile(r"[ァ-ヴヷ-ヺー・]{2,}")
_KANJI = re.compile(r"[一-龥々]{2,6}")

# English tokens carry the evidence; function words never do.
_EN_TOKEN = re.compile(r"[A-Za-z][A-Za-z'’\-]{2,}")
_EN_STOP = {
    "the", "and", "you", "your", "yours", "that", "this", "these", "those",
    "what", "with", "have", "has", "had", "was", "were", "are", "is", "am",
    "for", "not", "but", "all", "can", "could", "would", "should", "will",
    "just", "now", "then", "there", "here", "they", "them", "their", "she",
    "her", "him", "his", "how", "why", "who", "when", "where", "yes", "yeah",
    "know", "think", "want", "going", "get", "got", "let", "did", "does",
    "don", "don't", "it's", "i'm", "you're", "we're", "that's", "what's",
    "one", "two", "some", "any", "much", "very", "really", "well", "right",
    "good", "bad", "like", "make", "made", "say", "said", "see", "saw",
    # contractions and fillers carry no terminology
    "i'll", "i've", "i'd", "we'll", "we've", "you'll", "you've", "he'll",
    "she'll", "they'll", "it'll", "let's", "can't", "won't", "didn't",
    "isn't", "aren't", "wasn't", "weren't", "doesn't", "couldn't", "wouldn't",
    "shouldn't", "there's", "here's", "gonna", "wanna", "gotta", "okay", "sure",
    # prepositions/adverbs that glue phrases together but name nothing
    "between", "behind", "under", "over", "from", "into", "onto", "without",
    "inside", "outside", "after", "before", "while", "because", "though",
    "about", "around", "through", "against", "toward", "towards", "upon",
    "near", "off", "out", "down", "back", "away", "again", "still", "even",
    "only", "other", "another", "every", "each", "both", "more", "most",
}

# Super-common Japanese words that would otherwise be mined as "terms"; kept in
# code (not config) because they are language facts, not per-install data.
_JA_STOP = {
    "今日", "明日", "昨日", "本当", "自分", "大丈夫", "全部", "一緒", "最初",
    "最後", "今度", "時間", "問題", "人間", "世界", "意味", "気持", "気持ち",
    "方法", "場合", "場所", "相手", "全部", "無理", "当然", "普通", "絶対",
    "多分", "全然", "一番", "以外", "以上", "以下", "必要", "可能", "確認",
}

MIN_OCCURRENCES = 3
MIN_OCCURRENCES_KANJI = 4
MIN_CONSISTENCY = 0.5
MIN_CONSISTENCY_KANJI = 0.7
MIN_DISTINCTIVENESS = 0.3
MIN_SCORE = 0.2
MAX_TERMS = 40
MAX_NGRAM = 3
MIN_CUES_TO_LEARN = 20
MAX_FAILED_RATIO = 0.05
MIN_CASE_RATIO = 0.7
# A single-word match can't be told apart from ordinary vocabulary: "ギルド" → "guild",
# "ボス" → "boss", "ミス" → "mistakes" are lessons we do not want, while single-word
# character names already arrive from the AniList cast list. So only phrases
# ("Hellflame Dragon", "Clay Golem", "Witch Cult") are learned here.
MIN_TARGET_WORDS = 2


def _candidates(text: str) -> set[str]:
    """Japanese term candidates in one cue."""
    found: set[str] = set()
    for match in _KATAKANA.findall(text):
        token = match.strip("・ー")
        if len(token) >= 2 and token not in _JA_STOP:
            found.add(token)
    for match in _KANJI.findall(text):
        if match not in _JA_STOP:
            found.add(match)
    return found


def _tokens(text: str) -> list[tuple[str, str]]:
    """Content tokens of a translation as (original, lowercase) pairs.

    Possessives are folded ("Whale's" → "Whale") so a term isn't learned with a
    stray possessive attached — injecting `白鯨 = White Whale's` would make the
    model write "White Whale's" for every plain mention.
    """
    out: list[tuple[str, str]] = []
    for raw in _EN_TOKEN.findall(text):
        token = re.sub(r"[’']s$", "", raw)
        if len(token) < 3 or token.lower() in _EN_STOP:
            continue
        out.append((token, token.lower()))
    return out


def _ngrams(tokens: list[tuple[str, str]], size: int) -> list[str]:
    """Consecutive content-word phrases ("Witch Cult", "Sin Archbishop")."""
    return [
        " ".join(tokens[i + k][0] for k in range(size))
        for i in range(len(tokens) - size + 1)
    ]


def _is_katakana(term: str) -> bool:
    return bool(_KATAKANA.fullmatch(term))


def _case_ratio(target: str, all_text: str) -> float:
    """How often the target appears capitalised, i.e. as a name.

    Loanwords ("ギルド" → "guild", "スキル" → "skill") are rendered lowercase in
    running text; names and titles ("Laila", "Witch Cult") stay capitalised
    mid-sentence. This separates them without needing a dictionary.
    """
    pattern = re.escape(target)
    capitalised = len(re.findall(rf"(?<!\w){pattern}(?!\w)", all_text))
    any_case = len(re.findall(rf"(?<!\w){pattern}(?!\w)", all_text, re.I))
    return capitalised / any_case if any_case else 0.0


def mine_terms(entries: list[dict], translated: dict[int, str]) -> dict[str, dict]:
    """Return {japanese_term: {target, n, score, consistency}} for one file."""
    pairs: list[tuple[set[str], list[str]]] = []
    for entry in entries:
        text = (translated.get(entry["index"]) or "").strip()
        if not text:
            continue
        tokens = _tokens(text)
        if not tokens:
            continue
        phrases: list[str] = []
        for size in range(1, MAX_NGRAM + 1):
            phrases.extend(_ngrams(tokens, size))
        pairs.append((_candidates(entry["text"]), phrases))
    if len(pairs) < MIN_CUES_TO_LEARN:
        return {}
    all_text = " ".join((translated.get(e["index"]) or "") for e in entries)

    # Document frequency of every phrase across the file.
    df: Counter = Counter()
    for _, phrases in pairs:
        df.update({p.lower() for p in phrases})

    containing: dict[str, list[list[str]]] = defaultdict(list)
    for cands, phrases in pairs:
        for cand in cands:
            containing[cand].append(phrases)

    results: dict[str, dict] = {}
    for term, phrase_lists in containing.items():
        n = len(phrase_lists)
        if n < MIN_OCCURRENCES:
            continue
        katakana = _is_katakana(term)
        min_consistency = MIN_CONSISTENCY if katakana else MIN_CONSISTENCY_KANJI
        if not katakana and n < MIN_OCCURRENCES_KANJI:
            continue

        hits: dict[str, tuple[int, str]] = defaultdict(lambda: (0, ""))
        for phrases in phrase_lists:
            seen: dict[str, str] = {}
            for phrase in phrases:
                seen.setdefault(phrase.lower(), phrase)
            for low, original in seen.items():
                count, _ = hits[low]
                hits[low] = (count + 1, original)

        best = None
        for low, (count, original) in hits.items():
            consistency = count / n
            if consistency < min_consistency:
                continue
            distinctiveness = count / max(df.get(low, count), 1)
            if distinctiveness < MIN_DISTINCTIVENESS:
                continue
            # A kanji compound that keeps company with a lowercase word is usually
            # an ordinary word ("指先" → "fingertip"), not a name or a term. Katakana
            # candidates are names/loanwords and are allowed any casing.
            if not katakana and not original[:1].isupper():
                continue
            score = consistency * distinctiveness
            if score < MIN_SCORE:
                continue
            # Prefer the phrase: "Witch Cult" over "Witch", then the longer string.
            tiebreak = (round(score, 3), len(original.split()), len(original))
            if best is None or tiebreak > best["_tie"]:
                best = {
                    "target": original,
                    "n": n,
                    "score": round(score, 3),
                    "consistency": round(consistency, 3),
                    "_tie": tiebreak,
                }
        if best:
            best.pop("_tie", None)
            # A single kanji word is usually a fragment of a longer name ("大罪司教"
            # is "Sin Archbishop", not "Sin"; "白鯨" is "White Whale", not "White").
            # Injecting the fragment measurably degrades output, so try the phrase
            # it belongs to and otherwise learn nothing for this term.
            if not katakana and len(best["target"].split()) == 1:
                word = best["target"].lower()
                alt = None
                for low, (count, original) in hits.items():
                    if len(original.split()) < 2:
                        continue
                    # A term is a name-like phrase: it starts with a capital
                    # ("Sin Archbishop", not "between White").
                    if not original[:1].isupper():
                        continue
                    if not re.search(rf"(?<!\w){re.escape(word)}(?!\w)", low):
                        continue
                    consistency = count / n
                    if consistency < 0.5:
                        continue
                    distinctiveness = count / max(df.get(low, count), 1)
                    if distinctiveness < MIN_DISTINCTIVENESS:
                        continue
                    cand = (round(consistency * distinctiveness, 3), len(original.split()), len(original))
                    if alt is None or cand > alt[0]:
                        alt = (cand, {
                            "target": original,
                            "n": n,
                            "score": cand[0],
                            "consistency": round(consistency, 3),
                        })
                if alt is None:
                    continue  # no safe phrase form — learn nothing
                best = alt[1]
            # Only proper nouns: a word that mostly appears lowercase in running
            # text is vocabulary, not terminology ("guild", "skill", "relic").
            if _case_ratio(best["target"], all_text) < MIN_CASE_RATIO:
                continue
            if len(best["target"].split()) < MIN_TARGET_WORDS:
                continue  # single words are indistinguishable from vocabulary
            results[term] = best

    # Two candidates must not claim the same English phrase: 咆哮 ("roar") picks up
    # "Hellflame Dragon" simply because the two appear together. Keep the term with
    # the most evidence per target and drop the rest.
    strongest: dict[str, tuple[str, float]] = {}
    for term, info in results.items():
        key = info["target"].lower()
        weight = info["score"] * info["n"]
        if key not in strongest or weight > strongest[key][1]:
            strongest[key] = (term, weight)
    keep = {term for term, _ in strongest.values()}
    return {term: info for term, info in results.items() if term in keep}


def is_name_fragment(term: str, names: dict[str, str]) -> bool:
    """True when `term` is part of a known character's name (リア inside エミリア).

    Nicknames are the one thing the model has no evidence for, so learning
    "リア = Rem" would cement the mistake for every later episode.
    """
    return any(
        native != term and (term in native or native in term) for native in names
    )


def learn(
    key: str | None,
    entries: list[dict],
    translated: dict[int, str],
    qa: dict | None = None,
) -> dict:
    """Mine one finished episode into the show's glossary.

    Returns {"added": [...], "kept": [...], "conflicts": [...]} for logging; all
    lists hold "term = target" strings. Nothing is learned from a file whose
    translation failed in more than a few places, because bad pairs make bad terms.
    """
    empty = {"added": [], "kept": [], "conflicts": [], "skipped": []}
    if not key:
        return empty
    qa = qa or {}
    cues = qa.get("cues") or len(entries)
    failures = (qa.get("empty") or 0) + (qa.get("untranslated") or 0)
    if cues and failures / cues > MAX_FAILED_RATIO:
        return empty

    mined = mine_terms(entries, translated)
    if not mined:
        return empty

    data = show_glossary.load(key) or {}
    terms: dict[str, str] = dict(data.get("terms") or {})
    stats: dict[str, dict] = dict(data.get("term_stats") or {})
    names: dict[str, str] = dict(data.get("names") or {})

    def _is_name_fragment(term: str) -> bool:
        return is_name_fragment(term, names)

    added, kept, conflicts, skipped = [], [], [], []
    # Highest-evidence first so the cap keeps the strongest terms.
    ranked = sorted(mined.items(), key=lambda kv: (kv[1]["score"], kv[1]["n"]), reverse=True)
    for term, info in ranked:
        if term in terms:
            if terms[term].lower() != info["target"].lower():
                conflicts.append(f"{term} = {terms[term]} (model now says {info['target']})")
            stats[term] = {**stats.get(term, {}), "last": info["n"]}
            kept.append(f"{term} = {terms[term]}")
            continue
        if _is_name_fragment(term):
            skipped.append(f"{term} (fragment of a known character name)")
            continue
        if len(terms) >= MAX_TERMS:
            break
        terms[term] = info["target"]
        stats[term] = {"n": info["n"], "score": info["score"]}
        added.append(f"{term} = {info['target']}")

    if not added and not conflicts:
        return {"added": [], "kept": kept, "conflicts": [], "skipped": skipped}

    data["terms"] = terms
    data["term_stats"] = stats
    import datetime as _dt

    data["updated"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    show_glossary.save(key, data)
    return {"added": added, "kept": kept, "conflicts": conflicts, "skipped": skipped}
