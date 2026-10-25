"""Learn a show's terminology from the episodes we already translated.

Subber rarely gets Japanese subtitles — most sources are French, Chinese, Vietnamese,
Spanish. So the learner works on whatever language the source is in. It scores each
repeated *source* candidate against the English the run produced:

    consistency      = cues with term AND token / cues with term
    distinctiveness  = cues with term AND token / cues with token
    score            = consistency * distinctiveness

A real name or term scores high on both ("Lufas" appears 40 times and almost always
brings "Lufas" with it, while that word is rare elsewhere). A common word loses on one
axis or the other ("aujourd'hui"/"today" is everywhere, so "today" isn't distinctive of
it; a word rendered five different ways isn't consistent). Only terms that clear both
bars are stored, because a partial or wrong term list measurably degrades translation —
silence is the safe default.

What counts as a candidate depends on the script:

* Japanese — katakana runs and kanji compounds (AniList already supplies romaji names,
  so a learned term has to be a *phrase*: 魔女教 → "Witch Cult")
* Chinese  — repeated Han compounds
* everything else (Latin script: French, Vietnamese, Spanish, …) — capitalised words
  and phrases that appear mid-sentence, i.e. proper nouns rather than sentence starts
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

import yaml

from . import show_glossary

# Candidate shapes.
_KATAKANA = re.compile(r"[ァ-ヴヷ-ヺー・]{2,}")
_KANJI = re.compile(r"[一-龥々]{2,6}")
_HAN_RUN = re.compile(r"[一-龥々]{2,}")
# Any Latin-script word; capitalisation is judged with str.isupper() because
# Unicode ranges like Ā-ſ contain lowercase letters too (đ, ơ, …).
_LATIN_WORD = re.compile(r"[^\W\d_][\w'’\-]{1,}", re.UNICODE)
_HAS_KANA = re.compile(r"[ぁ-んァ-ヶ]")
_HAS_HAN = re.compile(r"[一-龥]")

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

# Words that are capitalised for grammar in Latin-script sources (sentence starts,
# pronouns) rather than being names. Lowercased when compared.
_LATIN_STOP = {
    "the", "a", "an", "and", "or", "but", "if", "so", "then", "when", "while",
    "because", "that", "this", "these", "those", "what", "who", "how", "why",
    "where", "here", "there", "yes", "no", "not", "you", "your", "she", "her",
    "he", "his", "they", "them", "their", "we", "our", "it", "its", "i", "me",
    "my", "mine", "one", "all", "some", "any", "please", "thanks", "thank",
    "sorry", "okay", "oh", "ah", "hey", "well", "now", "just", "very", "really",
    # French / Vietnamese / Spanish / Portuguese / German / Italian function words
    "je", "tu", "il", "elle", "on", "nous", "vous", "ils", "elles", "le", "la",
    "les", "un", "une", "des", "du", "de", "et", "ou", "mais", "que", "qui",
    "quoi", "dans", "pour", "avec", "sur", "pas", "ne", "se", "ce", "cette",
    "mon", "ton", "son", "est", "sont", "ai", "as", "a", "ont", "être", "avoir",
    "tôi", "bạn", "anh", "chị", "em", "hắn", "cô", "ông", "bà", "chúng", "nó",
    "và", "nhưng", "của", "là", "có", "không", "được", "cho", "với", "trong",
    "el", "los", "las", "una", "y", "pero", "porque", "para", "con", "por",
    "der", "die", "das", "und", "aber", "nicht", "ist", "sind", "ich", "du",
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
MIN_CASE_RATIO = 0.7        # Japanese targets
MIN_CASE_RATIO_STRICT = 0.9  # other languages: the target has to behave like a name
MIN_SOURCE_CASE_RATIO = 0.75  # Latin-script candidates: capitalised nearly always
MIN_MID_SENTENCE = 1          # …and seen at least once away from a line start
MIN_TARGET_WORDS_JA = 2


def script_of(text: str, source_lang: str | None = None) -> str:
    """'ja', 'zh' or 'latin' — which candidate rules apply."""
    lang = show_glossary.normalize_lang(source_lang)
    if lang == "ja" or _HAS_KANA.search(text):
        return "ja"
    if lang == "zh" or _HAS_HAN.search(text):
        return "zh"
    return "latin"


def _han_candidates(text: str) -> set[str]:
    """Every 2–4 character substring of each Han run.

    Matching only the maximal run ("鲁法斯来了" as one blob) never repeats across
    cues, so a name is never seen twice. All sub-runs are candidates and the
    frequency/consistency scoring picks the one that recurs.
    """
    found: set[str] = set()
    for run in _HAN_RUN.findall(text):
        for size in range(2, min(4, len(run)) + 1):
            for i in range(len(run) - size + 1):
                found.add(run[i : i + size])
    return found


def _latin_candidates(text: str) -> set[str]:
    """Capitalised words and runs of adjacent capitalised words.

    "Lufas" is a name; "Ma Vương" is one (two words, both capitalised) and has to
    beat its own fragments "Ma" and "Vương". All-caps tokens are titles/signs
    ("FINAL BOSS APPEARS"), not names, and are skipped.
    """
    found: set[str] = set()
    run: list[str] = []
    last_end = -10

    def flush() -> None:
        for size in range(1, min(3, len(run)) + 1):
            for i in range(len(run) - size + 1):
                candidate = " ".join(run[i : i + size])
                if candidate.lower() not in _LATIN_STOP:
                    found.add(candidate)
        run.clear()

    for match in _LATIN_WORD.finditer(text):
        token = match.group(0).strip("'’-")
        gap = text[last_end : match.start()] if last_end > 0 else ""
        adjacent = last_end > 0 and gap.strip() in ("", "-", "’", "'")
        keep = (
            len(token) >= 2
            and token[:1].isupper()
            and not token.isupper()
            and token.lower() not in _LATIN_STOP
        )
        if keep:
            if not adjacent:
                flush()
            run.append(token)
        else:
            flush()
        last_end = match.end()
    flush()
    return found


def _candidates(text: str, script: str) -> set[str]:
    """Term candidates in one source cue."""
    found: set[str] = set()
    if script == "ja":
        for match in _KATAKANA.findall(text):
            token = match.strip("・ー")
            if len(token) >= 2 and token not in _JA_STOP:
                found.add(token)
        stop_here = {w for w in _JA_STOP if w in text}
        found |= {
            c for c in _han_candidates(text)
            if c not in _JA_STOP and not any(c in w for w in stop_here)
        }
    elif script == "zh":
        found |= _han_candidates(text)
    else:
        found |= _latin_candidates(text)
    return found


def _tokens(text: str) -> list[tuple[str, str]]:
    """Content tokens of a translation as (original, lowercase) pairs.

    Possessives are folded ("Whale's" → "Whale") so a term isn't learned with a
    stray possessive attached.
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


def _case_ratio(target: str, all_text: str) -> float:
    """How often the target appears capitalised, i.e. as a name."""
    pattern = re.escape(target)
    capitalised = len(re.findall(rf"(?<!\w){pattern}(?!\w)", all_text))
    any_case = len(re.findall(rf"(?<!\w){pattern}(?!\w)", all_text, re.I))
    return capitalised / any_case if any_case else 0.0


def mine_terms(
    entries: list[dict],
    translated: dict[int, str],
    source_lang: str | None = None,
) -> dict[str, dict]:
    """Return {source_term: {target, n, score, consistency}} for one file."""
    source_text = " ".join(e["text"] for e in entries)
    script = script_of(source_text, source_lang)
    all_text = " ".join((translated.get(e["index"]) or "") for e in entries)

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
        pairs.append((_candidates(entry["text"], script), phrases))
    if len(pairs) < MIN_CUES_TO_LEARN:
        return {}

    df: Counter = Counter()
    for _, phrases in pairs:
        df.update({p.lower() for p in phrases})

    containing: dict[str, list[list[str]]] = defaultdict(list)
    for cands, phrases in pairs:
        for cand in cands:
            containing[cand].append(phrases)

    # Candidates that are capitalised in the source only because they start a line
    # are grammar, not names: require most occurrences to be capitalised, and at
    # least two occurrences mid-sentence ("Ngài Lufas" — not "Đây là …").
    source_case: dict[str, float] = {}
    mid_sentence: Counter = Counter()
    if script == "latin":
        for cand in containing:
            source_case[cand] = _case_ratio(cand, source_text)
        for entry in entries:
            text = entry["text"]
            for cand in _latin_candidates(text):
                for match in re.finditer(rf"(?<![\w'’\-]){re.escape(cand)}(?![\w'’\-])", text):
                    before = text[: match.start()].rstrip()
                    if before and before[-1] not in ".!?…。！？\"'":
                        mid_sentence[cand] += 1

    results: dict[str, dict] = {}
    for term, phrase_lists in containing.items():
        n = len(phrase_lists)
        if n < MIN_OCCURRENCES:
            continue
        katakana = bool(_KATAKANA.fullmatch(term))
        han = bool(_KANJI.fullmatch(term))
        if script == "ja":
            min_consistency = MIN_CONSISTENCY if katakana else MIN_CONSISTENCY_KANJI
            if not katakana and n < MIN_OCCURRENCES_KANJI:
                continue
        else:
            min_consistency = MIN_CONSISTENCY
        if script == "latin" and source_case.get(term, 0.0) < MIN_SOURCE_CASE_RATIO:
            continue
        if script == "latin" and mid_sentence.get(term, 0) < MIN_MID_SENTENCE:
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
            # A Han compound or Latin-script name that keeps company with a
            # lowercase word is usually ordinary vocabulary, not a name.
            if script == "ja" and han and not original[:1].isupper():
                continue
            score = consistency * distinctiveness
            if score < MIN_SCORE:
                continue
            tiebreak = (round(score, 3), len(original.split()), len(original))
            if best is None or tiebreak > best["_tie"]:
                best = {
                    "target": original,
                    "n": n,
                    "score": round(score, 3),
                    "consistency": round(consistency, 3),
                    "_tie": tiebreak,
                }
        if not best:
            continue
        best.pop("_tie", None)

        # Japanese single words are indistinguishable from vocabulary ("ギルド" →
        # "guild"), and names in Japanese already come from the AniList cast list, so
        # only phrases are learned. In other scripts a consistently capitalised single
        # word is a name ("Lufas") and is kept.
        if script == "ja" and len(best["target"].split()) == 1:
            word = best["target"].lower()
            alt = None
            for low, (count, original) in hits.items():
                if len(original.split()) < 2 or not original[:1].isupper():
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

        # Only proper nouns: a target that mostly appears lowercase in running text
        # is vocabulary, not terminology.
        required = MIN_CASE_RATIO if script == "ja" else MIN_CASE_RATIO_STRICT
        if _case_ratio(best["target"], all_text) < required:
            continue
        if script == "ja" and len(best["target"].split()) < MIN_TARGET_WORDS_JA:
            continue
        results[term] = best

    # Two candidates must not claim the same English phrase (咆哮 "roar" picks up
    # "Hellflame Dragon" purely by co-occurrence). Keep the strongest per target.
    strongest: dict[str, tuple[str, tuple]] = {}
    for term, info in results.items():
        key = info["target"].lower()
        weight = (info["n"], len(term), info["score"])
        if key not in strongest or weight > strongest[key][1]:
            strongest[key] = (term, weight)
    keep = {term for term, _ in strongest.values()}

    # Drop fragments: "Ma" and "Vương" are pieces of "Ma Thần Vương", and a piece is
    # never the term. A shorter candidate is dropped when a longer, nearly-as-frequent
    # candidate contains it as whole words.
    freq = {c: len(v) for c, v in containing.items()}
    ordered = sorted(freq, key=lambda t: len(t.split()), reverse=True)
    drop: set[str] = set()
    for i, longer in enumerate(ordered):
        for shorter in ordered[i + 1 :]:
            if shorter in drop or len(shorter.split()) >= len(longer.split()):
                continue
            if not re.search(rf"(?<!\w){re.escape(shorter)}(?!\w)", longer):
                continue
            if freq[longer] >= 0.5 * freq[shorter]:
                drop.add(shorter)
    return {
        term: info
        for term, info in results.items()
        if term in keep and term not in drop
    }


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
    source_lang: str | None = None,
) -> dict:
    """Mine one finished episode into the show's glossary, for its language.

    Returns {"added": [...], "kept": [...], "conflicts": [...], "skipped": [...]}
    of "source = target" strings. Nothing is learned from a file whose translation
    failed in more than a few places, because bad pairs make bad terms.
    """
    empty = {"added": [], "kept": [], "conflicts": [], "skipped": []}
    if not key:
        return empty
    qa = qa or {}
    cues = qa.get("cues") or len(entries)
    failures = (qa.get("empty") or 0) + (qa.get("untranslated") or 0)
    if cues and failures / cues > MAX_FAILED_RATIO:
        return empty

    lang = show_glossary.normalize_lang(source_lang)
    if lang == "en":
        return empty  # nothing to learn from translating English into English

    mined = mine_terms(entries, translated, source_lang)
    if not mined:
        return empty

    data = show_glossary.load(key) or {}
    show_glossary._migrate_terms(data)
    by_lang: dict[str, dict[str, str]] = {
        str(k): dict(v) for k, v in (data.get("terms") or {}).items() if isinstance(v, dict)
    }
    terms: dict[str, str] = by_lang.setdefault(lang, {})
    stats: dict[str, dict] = dict(data.get("term_stats") or {})
    lang_stats: dict[str, dict] = stats.setdefault(lang, {})
    names: dict[str, str] = dict(data.get("names") or {})

    def _is_name_fragment(term: str) -> bool:
        # AniList names are Japanese; the guard only makes sense there.
        return lang == "ja" and is_name_fragment(term, names)

    added, kept, conflicts, skipped = [], [], [], []
    ranked = sorted(mined.items(), key=lambda kv: (kv[1]["score"], kv[1]["n"]), reverse=True)
    for term, info in ranked:
        if term in terms:
            if terms[term].lower() != info["target"].lower():
                conflicts.append(f"{term} = {terms[term]} (model now says {info['target']})")
            lang_stats[term] = {**lang_stats.get(term, {}), "last": info["n"]}
            kept.append(f"{term} = {terms[term]}")
            continue
        if _is_name_fragment(term):
            skipped.append(f"{term} (fragment of a known character name)")
            continue
        if len(terms) >= MAX_TERMS:
            break
        terms[term] = info["target"]
        lang_stats[term] = {"n": info["n"], "score": info["score"]}
        added.append(f"{term} = {info['target']}")

    if not added and not conflicts:
        return {"added": [], "kept": kept, "conflicts": [], "skipped": skipped}

    by_lang[lang] = terms
    data["terms"] = by_lang
    data["term_stats"] = stats
    import datetime as _dt

    data["updated"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    show_glossary.save(key, data)
    return {"added": added, "kept": kept, "conflicts": conflicts, "skipped": skipped}
