"""LLM-powered subtitle translation.

Talks to any OpenAI-compatible API (DeepSeek cloud, local llama.cpp server, etc.).
Uses httpx for HTTP calls — no vendor SDK dependency.
"""

import re
import time
from pathlib import Path
from typing import Callable

import httpx
import pysubs2


class TranslationCancelled(Exception):
    """Raised when a translation job is cancelled mid-flight."""
    pass


# Language code → full name for TranslateGemma prompt format
_LANG_NAMES = {
    "ja": "Japanese", "en": "English", "de": "German", "fr": "French",
    "es": "Spanish", "it": "Italian", "pt": "Portuguese", "ru": "Russian",
    "zh": "Chinese", "ko": "Korean", "ar": "Arabic", "hi": "Hindi",
    "th": "Thai", "vi": "Vietnamese", "nl": "Dutch", "pl": "Polish",
    "sv": "Swedish", "da": "Danish", "fi": "Finnish", "no": "Norwegian",
    "tr": "Turkish", "el": "Greek", "he": "Hebrew", "cs": "Czech",
    "ro": "Romanian", "hu": "Hungarian", "id": "Indonesian", "uk": "Ukrainian",
    "bg": "Bulgarian", "ca": "Catalan", "sk": "Slovak", "sl": "Slovenian",
}


# Characters of CJK scripts — used to detect source-language text that survived
# translation (the "leftover untranslated Japanese" leak).
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def _still_source_lang(text: str, source_lang: str) -> bool:
    """True when text still looks like it's in the source language's script.

    Only reliable for CJK sources (ja/zh/ko) — Latin-script languages share the
    alphabet, so those return False (best-effort only)."""
    if source_lang in ("ja", "zh", "ko"):
        return bool(_CJK_RE.search(text or ""))
    return False


def _format_character_map(character_map) -> str:
    """Build a prompt fragment listing canonical character names (native → romaji).

    The LLM romanizes Japanese character names from the source text; without a
    reference it guesses from sound (e.g. 瓜生 → "Umino"/"Urio"/"Uri-no"). Passing
    AniList's native→romaji mapping makes the spelling consistent.
    """
    if not character_map:
        return ""
    lines = []
    for c in character_map:
        if not isinstance(c, dict):
            continue
        native = (c.get("native") or "").strip()
        full = (c.get("full") or "").strip()
        if native and full:
            lines.append(f"- {native} = {full}")
    if not lines:
        return ""
    return (
        "Canonical character names for this show — romanize matching source "
        "names using these exact spellings (do not translate or alter them):\n"
        + "\n".join(lines)
    )


class Translator:
    """Translate subtitle files via an OpenAI-compatible LLM API."""

    def __init__(
        self,
        api_base: str = "https://api.deepseek.com/v1",
        api_key: str = "",
        model: str = "deepseek-chat",
        chunk_size: int = 50,
        chunk_overlap: int = 3,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        max_retries: int = 3,
        timeout: float = 120.0,
        glossary: list | None = None,
        wrap_chars: int = 42,
        glossary_key: str | None = None,
    ):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.timeout = timeout
        # Terminology table (["source = target", ...]) injected into the prompt.
        self.glossary = glossary or []
        # Subtitle line wrapping applied when the translation is written back.
        self.wrap_chars = int(wrap_chars or 0)
        # Filled in by translate(): per-file quality counters (see translation_qa).
        self.last_qa: dict = {}
        # Show this file belongs to (anilist-<id> / tmdb-<id>). When set, the
        # finished translation teaches that show's glossary new terms.
        self.glossary_key = glossary_key

    def translate(
        self,
        input_path: Path,
        output_path: Path,
        source_lang: str,
        target_lang: str = "en",
        on_progress: Callable[[int, int], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        character_map: list[dict] | None = None,
    ) -> Path:
        """
        Translate a subtitle file.

        Args:
            input_path: Path to .srt/.ass/.vtt subtitle file
            output_path: Where to write the translated file
            source_lang: Source language code (e.g. 'de', 'ja')
            target_lang: Target language code (default 'en')
            on_progress: Optional callback(chunk_index, total_chunks)
            cancel_check: Optional callback() → True if job was cancelled
            character_map: Optional list of {"native": ..., "full": ...} name
                mappings (from show identification) injected into the prompt so
                character names are romanized consistently.

        Returns the output path on success.
        """
        self._character_context = _format_character_map(character_map)
        self._glossary_context = _format_glossary(self.glossary)
        subs = pysubs2.load(str(input_path), encoding="utf-8-sig")
        entries = [
            {"index": i, "text": event.plaintext, "start": event.start, "end": event.end}
            for i, event in enumerate(subs.events)
        ]

        if not entries:
            raise ValueError("No subtitle entries found in file")

        chunks = _chunk_entries(entries, self.chunk_size, self.chunk_overlap)
        translated: dict[int, str] = {}
        repairs = 0

        for ci, chunk in enumerate(chunks):
            if cancel_check and cancel_check():
                raise TranslationCancelled("Job cancelled by user")
            if on_progress:
                on_progress(ci + 1, len(chunks))

            parsed, repaired = self._translate_chunk_verified(chunk, source_lang, target_lang)
            if repaired:
                repairs += 1
                import logging
                logging.getLogger("subber").warning(
                    "  Chunk %d/%d repaired: model returned a line count that did not "
                    "match %d sent cues (merged/dropped/duplicated lines)",
                    ci + 1, len(chunks), len(chunk),
                )

            for idx, text in parsed.items():
                translated[idx] = text

        # Last line of defence against a merge that preserved the line count: the
        # speaker labels tell us where the content really belongs, so re-translate
        # the cues around a label drift one line at a time (a single-line request
        # has nothing to merge with).
        repaired_cues = self._repair_label_desync(
            entries, translated, source_lang, target_lang
        )
        if repaired_cues:
            import logging
            logging.getLogger("subber").warning(
                "  Repaired %d cue(s) whose speaker labels did not line up with the "
                "source (model merged two cues and shifted the rest)",
                repaired_cues,
            )

        # A cue that came back as nothing but its speaker label lost its dialogue —
        # ask for that single line again rather than shipping a bare "(Subaru)".
        dialogue_fixed = self._repair_missing_dialogue(
            entries, translated, source_lang, target_lang
        )
        if dialogue_fixed:
            import logging
            logging.getLogger("subber").warning(
                "  Repaired %d cue(s) that came back as a speaker label with no dialogue",
                dialogue_fixed,
            )

        # Retry any entries still in the source language — the LLM can omit line
        # numbers (duplicate/hallucinated segments) or pass source text straight
        # through (e.g. a truncated response on a huge vocalization chunk),
        # leaving those lines untranslated in the final file. Re-send them once
        # so a mixed chunk can't leak source-language text.
        if source_lang not in ("en", "eng", "english"):
            leftover = [
                e for e in entries
                if e["index"] not in translated
                or _still_source_lang(translated[e["index"]], source_lang)
            ]
            if leftover:
                try:
                    retry_text = self._translate_chunk(leftover, source_lang, target_lang)
                    for idx, text in _parse_numbered_response(retry_text).items():
                        if not _still_source_lang(text, source_lang):
                            translated[idx] = text
                except Exception:
                    pass  # retry is best-effort — keep the main-pass results

        # Summarise the result so each file carries a machine-checkable verdict
        # instead of having to be read to be trusted.
        self.last_qa = translation_qa(entries, translated, source_lang, self.wrap_chars)
        self.last_qa.update({
            "chunks_repaired": repairs,
            "label_desyncs": repaired_cues,
            "dialogue_repaired": dialogue_fixed,
            "chunks": len(chunks),
        })
        import logging
        logging.getLogger("subber").info(
            "  QA: %d cues · %d empty · %d label-only · %d sound-cue-only · "
            "%d untranslated · longest %d chars · %d/%d chunks repaired · "
            "%d label-drift · %d dialogue",
            self.last_qa["cues"], self.last_qa["empty"], self.last_qa["label_only"],
            self.last_qa["sound_cues"], self.last_qa["untranslated"],
            self.last_qa["longest_line"], repairs, len(chunks), repaired_cues,
            dialogue_fixed,
        )

        # Learn this show's terminology from the pairs we just produced, so the
        # next episode renders リア, ティビー, 魔女教 the same way this one did.
        if self.glossary_key:
            try:
                from .show_glossary import is_japanese_source
                if not is_japanese_source(source_lang):
                    self.last_qa["terms_learned"] = 0
                else:
                    from .term_mining import learn
                    learned = learn(self.glossary_key, entries, translated, self.last_qa)
                    self.last_qa["terms_learned"] = len(learned.get("added") or [])
                    if learned.get("added"):
                        import logging
                        logging.getLogger("subber").info(
                            "  Learned %d term(s) for %s: %s",
                            len(learned["added"]), self.glossary_key,
                            ", ".join(learned["added"][:8]),
                        )
                    if learned.get("conflicts"):
                        import logging
                        logging.getLogger("subber").warning(
                            "  Glossary conflict(s) kept as-is: %s",
                            "; ".join(learned["conflicts"][:5]),
                        )
            except Exception as exc:  # never fail a translation over learning
                import logging
                logging.getLogger("subber").warning("  Term learning skipped: %s", exc)

        # Apply translations back to the subtitle events
        for i, event in enumerate(subs.events):
            if i in translated:
                # Set event.text directly so ASS formatting tags and \N are preserved.
                # pysubs2 will handle format-specific newline encoding (literal \n for
                # SRT, \N escape for ASS) based on the output format.
                event.text = _wrap_subtitle(translated[i], self.wrap_chars)

        subs.save(str(output_path))
        return output_path

    def _repair_missing_dialogue(
        self,
        entries: list[dict],
        translated: dict[int, str],
        source_lang: str,
        target_lang: str,
    ) -> int:
        """Re-request cues that came back as nothing but a speaker label.

        A model occasionally answers `[7] (Subaru)` for a line that has dialogue in
        the source — the label is there, the words are not, so nothing upstream
        notices and the viewer gets an empty subtitle. Each suspect line is asked
        for on its own, which is the one case a model can't silently merge away.

        Returns the number of cues replaced.
        """
        suspects = []
        for e in entries:
            out = (translated.get(e["index"]) or "").strip()
            if not out or not _EN_LABEL_RE.fullmatch(out):
                continue
            source = e["text"].strip()
            if not _JP_LABEL_RE.match(source):
                continue
            remainder = _JP_LABEL_RE.sub("", source, count=1).strip()
            if len(remainder) >= 2:  # the source really does have words after the label
                suspects.append(e)

        fixed = 0
        for e in suspects:
            try:
                raw = self._translate_chunk(
                    [e], source_lang, target_lang, temperature=0.0
                )
                got = _parse_numbered_response(raw)
                text = (got.get(e["index"]) or next(iter(got.values()), "")).strip()
            except Exception:
                continue
            if text and not _EN_LABEL_RE.fullmatch(text):
                translated[e["index"]] = text
                fixed += 1
        return fixed

    def _repair_label_desync(
        self,
        entries: list[dict],
        translated: dict[int, str],
        source_lang: str,
        target_lang: str,
    ) -> int:
        """Re-translate cues whose speaker label ended up on the wrong line.

        Models occasionally translate a sentence that spans two cues as one line and
        then duplicate or split another line so the response length still matches —
        the chunk passes every count check while the content is one cue out of step
        from the merge point onwards. Speaker labels are the only anchors available
        without a reference translation, so any cue whose label presence disagrees
        with the source marks a window that is re-translated one line at a time.

        Returns the number of cues replaced.
        """
        has_labels = any(_JP_LABEL_RE.match(e["text"].strip()) for e in entries)
        if not has_labels:
            return 0

        bad = [
            e["index"]
            for e in entries
            if e["index"] in translated
            and bool(_JP_LABEL_RE.match(e["text"].strip()))
            != bool(_EN_LABEL_RE.match((translated[e["index"]] or "").strip()))
        ]
        if not bad:
            return 0

        windows: list[list[int]] = []
        cur = [bad[0]]
        for i in bad[1:]:
            if i - cur[-1] <= 3:
                cur.append(i)
            else:
                windows.append(cur)
                cur = [i]
        windows.append(cur)

        by_index = {e["index"]: e for e in entries}
        fixed = 0
        for window in windows:
            lo = max(0, window[0] - 2)
            hi = min(len(entries) - 1, window[-1] + 2)
            for i in range(lo, hi + 1):
                e = by_index.get(i)
                if e is None:
                    continue
                try:
                    raw = self._translate_chunk(
                        [e], source_lang, target_lang, temperature=0.0
                    )
                    got = _parse_numbered_response(raw)
                    text = (got.get(i) or next(iter(got.values()), "")).strip()
                except Exception:
                    continue
                if not text:
                    continue
                # Only keep it when the label situation now matches the source.
                if bool(_JP_LABEL_RE.match(e["text"].strip())) == bool(
                    _EN_LABEL_RE.match(text)
                ):
                    translated[i] = text
                    fixed += 1

        return fixed

    def _translate_chunk(
        self,
        chunk: list[dict],
        source_lang: str,
        target_lang: str,
        retry_refusal: bool = False,
        temperature: float | None = None,
    ) -> str:
        """Send one chunk to the LLM and return the raw response text."""
        numbered_lines = "\n".join(
            f"[{e['index']}] {e['text']}" for e in chunk
        )

        preamble = "These are fictional anime subtitles. " if retry_refusal else ""
        lang_name = _LANG_NAMES.get(source_lang, source_lang.upper())
        target_name = _LANG_NAMES.get(target_lang, target_lang.upper())
        char_ctx = getattr(self, "_character_context", "") or ""
        char_block = f"{char_ctx}\n" if char_ctx else ""
        glossary_ctx = getattr(self, "_glossary_context", "") or ""
        glossary_block = f"{glossary_ctx}\n\n" if glossary_ctx else ""
        system_prompt = (
            f"{preamble}"
            f"You are a professional {lang_name} ({source_lang}) to {target_name} ({target_lang}) translator. "
            f"Your goal is to accurately convey the meaning and nuances of the original {lang_name} text "
            f"while adhering to {target_name} grammar, vocabulary, and cultural sensitivities.\n\n"
            f"{char_block}"
            f"{glossary_block}"
            f"Translate these numbered subtitle lines from {source_lang} to {target_lang}.\n\n"
            f"Rules:\n"
            f"- Preserve meaning, tone, and character names\n"
            f"- Keep lines concise (subtitle-friendly length)\n"
            f"- Preserve any line break markers (\\\\N) at natural positions\n"
            f"- Preserve the original line numbers exactly as shown\n"
            f"- Return exactly one line per number, never merge or skip a number\n"
            f"- Return ONLY the translated lines with their numbers\n"
            f"- Format: [N] translated text (keep original numbers)\n"
            f"- Produce only the translation, without any additional explanations"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": numbered_lines},
        ]

        return self._call_api(messages, temperature)

    def _translate_chunk_verified(
        self,
        chunk: list[dict],
        source_lang: str,
        target_lang: str,
    ) -> tuple[dict[int, str], bool]:
        """Translate one chunk and guarantee a line for every cue index it sent.

        A model that merges two cues into one line, drops a line or repeats a
        number collapses the 1:1 index mapping, and every later cue in that chunk
        is then shifted (observed as 1-15 cue desyncs that survive into the final
        file). Any chunk whose response does not carry exactly the indices we
        sent is retried once at temperature 0, then the stragglers are translated
        one line at a time — a single-line request cannot desync.

        Returns (translations, repaired) where `repaired` is True when the first
        response was unusable and a retry / per-line pass was needed.
        """
        # Overlap cues were already translated as part of the previous chunk; they
        # are sent as context only. Letting the later chunk write them back would
        # overwrite a good line with one translated out of context.
        wanted = [e for e in chunk if e.get("_translate", True)]
        sent = {e["index"] for e in chunk}
        expected = [e["index"] for e in wanted]

        parsed: dict[int, str] = {}
        repaired = False
        for attempt, temp in enumerate((None, 0.0)):
            raw = self._translate_chunk(
                chunk, source_lang, target_lang,
                retry_refusal=attempt > 0,
                temperature=temp,
            )
            got = _parse_numbered_response(raw)
            parsed = {i: t for i, t in got.items() if i in set(expected) and t.strip()}
            missing = [i for i in expected if i not in parsed]
            extra = set(got) - sent
            if not missing and not extra and _labels_mismatch(wanted, parsed) == 0:
                return parsed, repaired
            repaired = True

        for e in wanted:
            if e["index"] in parsed:
                continue
            try:
                raw = self._translate_chunk(
                    [e], source_lang, target_lang, temperature=0.0
                )
                got = _parse_numbered_response(raw)
                text = got.get(e["index"]) or next(iter(got.values()), "")
                if text and text.strip():
                    parsed[e["index"]] = text.strip()
            except Exception:
                continue  # keep whatever the chunk pass produced

        return parsed, repaired

    def _call_api(self, messages: list[dict], temperature: float | None = None) -> str:
        """Call the LLM API with retry logic."""
        temp = self.temperature if temperature is None else temperature
        # Detect translategemma models — they need the completion API, not chat
        is_translategemma = "translategemma" in self.model.lower()
        if is_translategemma:
            # Build a single prompt string from the messages
            system = messages[0]["content"] if messages[0]["role"] == "system" else ""
            user = messages[-1]["content"] if messages[-1]["role"] == "user" else ""
            prompt = f"{system}\n\n{user}" if system else user
            url = f"{self.api_base}/completions"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.model,
                "prompt": prompt,
                "temperature": temp,
                "max_tokens": self.max_tokens,
            }
        else:
            url = f"{self.api_base}/chat/completions"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": temp,
                "max_tokens": self.max_tokens,
            }

        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            try:
                resp = httpx.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                # Completion API returns choices[0]["text"], chat API returns choices[0]["message"]["content"]
                choice = data["choices"][0]
                # `or ""` matters: OpenAI-compatible servers (OpenRouter included)
                # answer {"message": {"content": null}} for an empty or filtered
                # completion, and .get("content", "") does NOT cover an explicit
                # null — that used to raise "'NoneType' object has no attribute
                # 'strip'" and abort the whole backend failover chain.
                content = (
                    choice.get("text")
                    or (choice.get("message") or {}).get("content")
                    or ""
                )
                if not isinstance(content, str):
                    content = ""
                return content.strip()
            except (httpx.HTTPError, KeyError, IndexError, AttributeError, ValueError) as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    delay = 2 ** attempt  # 1s, 2s, 4s
                    time.sleep(delay + (time.monotonic() % 1))  # + jitter

        raise RuntimeError(
            f"Translation API call failed after {self.max_retries} attempts"
            + (f": {last_error}" if last_error else "")
        ) from last_error

    def identify_language(self, text: str) -> str:
        """Identify the language of a subtitle text sample via the LLM.

        Used as a last-resort confirmation when neither the filename nor
        langdetect could determine the language. Returns a 2-letter ISO 639-1
        code (e.g. 'en', 'ja') or 'unknown'.
        """
        system_prompt = (
            "You are a language identification expert. Identify the language of "
            "the subtitle lines below. Reply with ONLY the 2-letter ISO 639-1 "
            "language code (for example: en, ja, fr, de, es, it, pt, zh, ko, ru, "
            "ar). Output nothing else."
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ]
        raw = self._call_api(messages)
        text = raw.strip().lower()
        # Return the first 2-letter token that is a known language code, so a
        # verbose reply ("the language is English") can't return a filler word
        # like "is"/"to" instead of the actual code.
        for tok in re.findall(r"\b[a-z]{2}\b", text):
            if tok in _LANG_NAMES:
                return tok
        return "unknown"




class MultiBackendTranslator:
    """Wraps multiple Translator instances, tries them in priority order."""

    def __init__(self, backends: list[dict], **shared_kwargs):
        """
        Args:
            backends: list of {name, api_base, api_key, model, priority} dicts
            shared_kwargs: chunk_size, temperature, max_tokens, max_retries, timeout
                applied to all backends (individual backends can override)
        """
        self.backends = backends
        self.shared_kwargs = shared_kwargs
        self._translators: dict[str, Translator] = {}
        self.last_qa: dict = {}

    @property
    def chunk_size(self) -> int:
        return self.shared_kwargs.get("chunk_size", 50)

    @property
    def temperature(self) -> float:
        return self.shared_kwargs.get("temperature", 0.1)

    @property
    def max_tokens(self) -> int:
        return self.shared_kwargs.get("max_tokens", 4096)

    def _get_translator(self, backend: dict) -> Translator:
        """Lazy-create a Translator for a backend."""
        name = backend["name"]
        if name not in self._translators:
            kwargs = dict(self.shared_kwargs)
            for k in ("temperature", "max_tokens", "max_retries", "timeout", "chunk_size"):
                if k in backend:
                    kwargs[k] = backend[k]
            self._translators[name] = Translator(
                api_base=backend["api_base"],
                api_key=backend.get("api_key", "ollama"),
                model=backend["model"],
                **kwargs,
            )
        return self._translators[name]

    def translate(
        self,
        input_path: Path,
        output_path: Path,
        source_lang: str,
        target_lang: str = "en",
        on_progress: Callable[[int, int], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        character_map: list[dict] | None = None,
    ) -> str:
        """Translate using the first backend that succeeds. Returns model name."""
        import logging
        _log = logging.getLogger("subber")
        errors = []
        for i, backend in enumerate(self.backends):
            name = backend["name"]
            model = backend.get("model", "?")
            _log.info("  Trying backend: %s (%s)", name, model)
            translator = self._get_translator(backend)
            try:
                result = translator.translate(
                    input_path, output_path, source_lang, target_lang,
                    on_progress=on_progress, cancel_check=cancel_check,
                    character_map=character_map,
                )
                _log.info("  Backend succeeded: %s (%s)", name, model)
                self.last_qa = getattr(translator, "last_qa", {}) or {}
                if i > 0:
                    _log.info(
                        "Translation succeeded via fallback backend %s after %d failures",
                        name, i,
                    )
                return model
            except Exception as e:
                _log.warning(
                    "  Backend %s (%s) failed: %s", name, model, e,
                )
                errors.append(f"{name}: {e}")
                if isinstance(e, TranslationCancelled):
                    raise
                continue

        return "none"
        raise RuntimeError(
            f"All {len(self.backends)} translation backends failed:\n" +
            "\n".join(errors)
        )


def translate_subtitles_multi(
    input_path: Path,
    output_path: Path,
    source_lang: str,
    target_lang: str = "en",
    backends: list[dict] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    character_map: list[dict] | None = None,
    **kwargs,
) -> str:
    """One-shot subtitle translation with multi-backend failover.
    
    Returns the model name that was used for translation (or 'none' if skipped).
    """
    if not backends:
        translator = Translator(**kwargs)
        translator.translate(
            input_path, output_path, source_lang, target_lang, on_progress,
            character_map=character_map,
        )
        return translator.model
    
    multi = MultiBackendTranslator(backends, **kwargs)
    return multi.translate(
        input_path, output_path, source_lang, target_lang, on_progress,
        character_map=character_map,
    )
def _chunk_entries(
    entries: list[dict],
    chunk_size: int,
    overlap: int,
) -> list[list[dict]]:
    """Split entries into overlapping chunks for translation."""
    if len(entries) <= chunk_size:
        return [[dict(e, _translate=True) for e in entries]]

    chunks = []
    i = 0
    while i < len(entries):
        # Take chunk_size entries, but include 'overlap' from previous chunk for context
        start = max(0, i - overlap) if i > 0 else i
        # Copy each entry: the same cue is context-only in one chunk and new in the
        # previous one, so the flag has to live on the chunk's copy, not the cue.
        chunk = [
            dict(e, _translate=e["index"] >= i)
            for e in entries[start : i + chunk_size]
        ]
        chunks.append(chunk)
        i += chunk_size

    return chunks


_NUMBERED_LINE_RE = re.compile(r"^\[(\d+)\]\s*(.*)")
# Lines a model sometimes appends after the translation ("Note: ...", "Here is ...").
_META_TAIL_RE = re.compile(r"^(note|notes?|explanation|here (?:is|are)|translation|output)\b", re.I)


# Speaker labels in the source (SDH cues: "（スバル） ...") and in the response.
_JP_LABEL_RE = re.compile(r"^[（(]([^）)]{1,16})[）)]")
_EN_LABEL_RE = re.compile(r"^\(([^)]{1,40})\)")


def _labels_mismatch(chunk: list[dict], parsed: dict[int, str]) -> int:
    """Count cues where the speaker-label presence disagrees with the source.

    A compensating merge — two cues translated as one line, then another line
    duplicated to keep the count — leaves the response length correct while the
    content is one cue out of step. Text similarity can't separate that from two
    genuinely similar lines, but the speaker labels can: in an SDH file every
    labelled source cue must come back with a label at the same index, and an
    unlabelled one must not sprout one. A shift breaks that on either side of the
    merge point.
    """
    bad = 0
    for e in chunk:
        if not e.get("_translate", True):
            continue
        text = parsed.get(e["index"])
        if text is None:
            continue
        if bool(_JP_LABEL_RE.match(e["text"].strip())) != bool(_EN_LABEL_RE.match(text.strip())):
            bad += 1
    return bad
# Accepted separators in a glossary entry: "リア = Lia", "リア->Lia", "リア\tLia".
_GLOSSARY_SEPARATORS = (" = ", "=", " -> ", "->", "\t")


def _format_glossary(glossary) -> str:
    """Build the terminology block injected into the translation prompt.

    Entries are "source = target" strings or {"source", "target"} dicts. The block
    is applied verbatim by the model — Hy-MT2 in particular follows it closely and
    then degrades anything *not* listed, so entries are emitted only when present.
    """
    if not glossary:
        return ""
    lines: list[str] = []
    for entry in glossary:
        if isinstance(entry, dict):
            src = str(entry.get("source") or entry.get("term") or "").strip()
            dst = str(entry.get("target") or entry.get("translation") or "").strip()
        else:
            raw = str(entry)
            src = dst = ""
            for sep in _GLOSSARY_SEPARATORS:
                if sep in raw:
                    left, _, right = raw.partition(sep)
                    src, dst = left.strip(), right.strip()
                    break
        if src and dst:
            lines.append(f"- {src} = {dst}")
    if not lines:
        return ""
    return (
        "Terminology — use these exact renderings; do not translate or alter the "
        "target spelling:\n" + "\n".join(lines)
    )


def _wrap_line(text: str, width: int) -> list[str]:
    """Greedy word wrap of one line to `width` characters."""
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    cur = ""
    for word in words:
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= width:
            cur = f"{cur} {word}"
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _wrap_subtitle(text: str, width: int = 42) -> str:
    """Wrap translated subtitle text so no line runs off the screen.

    Existing hard breaks (SRT newline or ASS \\N) are respected — each segment is
    wrapped on its own. Text that needs three lines gets three lines rather than a
    merged overlong one or a truncated one. width < 10 disables wrapping (0 = off).
    """
    if not text or not width or width < 10:
        return text
    out: list[str] = []
    for segment in re.split(r"\\N|\n", text):
        out.extend(_wrap_line(segment, width))
    return "\n".join(out)


def translation_qa(
    entries: list[dict],
    translated: dict[int, str],
    source_lang: str,
    wrap_chars: int = 0,
) -> dict:
    """Per-file quality counters, measured on the finished translations.

    These are the defects that are visible in a shipped subtitle: a cue with no
    text, a cue that is only a speaker label, a line left in the source language,
    and a line too long for the screen. Counting them costs nothing and turns
    "looks fine" into a number.
    """
    empty = label_only = untranslated = sound_cues = 0
    longest = 0
    for e in entries:
        text = (translated.get(e["index"]) or "").strip()
        if not text:
            empty += 1
            continue
        if _EN_LABEL_RE.fullmatch(text):
            source = e["text"].strip()
            if _JP_LABEL_RE.match(source):
                remainder = _JP_LABEL_RE.sub("", source, count=1).strip()
                # "（扉の開閉音）" → "(Sound of a door)" is a complete translation of a
                # sound cue, not a dropped line.
                if len(remainder) >= 2:
                    label_only += 1
                else:
                    sound_cues += 1
            else:
                label_only += 1
        if _still_source_lang(text, source_lang):
            untranslated += 1
        for line in text.split("\n"):
            longest = max(longest, len(line))
    return {
        "cues": len(entries),
        "empty": empty,
        "label_only": label_only,
        "sound_cues": sound_cues,
        "untranslated": untranslated,
        "longest_line": longest,
        "wrap_chars": wrap_chars,
        "clean": empty == 0 and label_only == 0 and untranslated == 0,
    }


def _parse_numbered_response(text: str) -> dict[int, str]:
    """Parse an LLM response like '[0] Hello\\n[1] World' into a dict.

    Models do not reliably keep one cue on one line. The common failure is an SDH
    speaker label alone on a line with the dialogue on the next:

        [0] (Felix Argyle)
        Showing up late to join us now—

    A strict '[N]' matcher discards that dialogue, which is how a finished file
    ends up carrying a bare "(Felix Argyle)" with no text. Un-numbered lines are
    therefore appended to the previous cue. Lines that look like model commentary
    ("Note: ...") are skipped instead of being glued on.
    """
    result: dict[int, str] = {}
    current: int | None = None

    for raw_line in text.strip().split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("```"):
            continue
        match = _NUMBERED_LINE_RE.match(line)
        if match:
            current = int(match.group(1))
            result[current] = match.group(2).strip().strip('"').strip("'")
            continue
        if current is None or _META_TAIL_RE.match(line):
            continue
        result[current] = f"{result[current]} {line}".strip()

    return {i: t for i, t in result.items() if t}


# Convenience function for direct use
def translate_subtitles(
    input_path: Path,
    output_path: Path,
    source_lang: str,
    target_lang: str = "en",
    api_base: str = "https://api.deepseek.com/v1",
    api_key: str = "",
    model: str = "deepseek-chat",
    on_progress: Callable[[int, int], None] | None = None,
) -> Path:
    """One-shot subtitle translation. See Translator class for details."""
    translator = Translator(
        api_base=api_base,
        api_key=api_key,
        model=model,
    )
    return translator.translate(
        input_path, output_path, source_lang, target_lang, on_progress
    )
