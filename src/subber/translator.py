"""LLM-powered subtitle translation.

Talks to any OpenAI-compatible API (DeepSeek cloud, local llama.cpp server, etc.).
Uses httpx for HTTP calls — no vendor SDK dependency.
"""

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

    def translate(
        self,
        input_path: Path,
        output_path: Path,
        source_lang: str,
        target_lang: str = "en",
        on_progress: Callable[[int, int], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
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

        Returns the output path on success.
        """
        subs = pysubs2.load(str(input_path), encoding="utf-8-sig")
        entries = [
            {"index": i, "text": event.plaintext, "start": event.start, "end": event.end}
            for i, event in enumerate(subs.events)
        ]

        if not entries:
            raise ValueError("No subtitle entries found in file")

        chunks = _chunk_entries(entries, self.chunk_size, self.chunk_overlap)
        translated: dict[int, str] = {}

        for ci, chunk in enumerate(chunks):
            if cancel_check and cancel_check():
                raise TranslationCancelled("Job cancelled by user")
            if on_progress:
                on_progress(ci + 1, len(chunks))

            chunk_text = self._translate_chunk(chunk, source_lang, target_lang)
            parsed = _parse_numbered_response(chunk_text)

            # Detect refusal: if model refused (e.g. safety filter), retry once with
            # a preamble clarifying these are fictional anime subtitles.
            if not parsed:
                chunk_text = self._translate_chunk(chunk, source_lang, target_lang, retry_refusal=True)
                parsed = _parse_numbered_response(chunk_text)

            for idx, text in parsed.items():
                translated[idx] = text

        # Apply translations back to the subtitle events
        for i, event in enumerate(subs.events):
            if i in translated:
                # Set event.text directly so ASS formatting tags and \N are preserved.
                # pysubs2 will handle format-specific newline encoding (literal \n for
                # SRT, \N escape for ASS) based on the output format.
                event.text = translated[i]

        subs.save(str(output_path))
        return output_path

    def _translate_chunk(
        self,
        chunk: list[dict],
        source_lang: str,
        target_lang: str,
        retry_refusal: bool = False,
    ) -> str:
        """Send one chunk to the LLM and return the raw response text."""
        numbered_lines = "\n".join(
            f"[{e['index']}] {e['text']}" for e in chunk
        )

        preamble = "These are fictional anime subtitles. " if retry_refusal else ""
        lang_name = _LANG_NAMES.get(source_lang, source_lang.upper())
        target_name = _LANG_NAMES.get(target_lang, target_lang.upper())
        system_prompt = (
            f"{preamble}"
            f"You are a professional {lang_name} ({source_lang}) to {target_name} ({target_lang}) translator. "
            f"Your goal is to accurately convey the meaning and nuances of the original {lang_name} text "
            f"while adhering to {target_name} grammar, vocabulary, and cultural sensitivities.\n\n"
            f"Translate these numbered subtitle lines from {source_lang} to {target_lang}.\n\n"
            f"Rules:\n"
            f"- Preserve meaning, tone, and character names\n"
            f"- Keep lines concise (subtitle-friendly length)\n"
            f"- Preserve any line break markers (\\\\N) at natural positions\n"
            f"- Preserve the original line numbers exactly as shown\n"
            f"- Return ONLY the translated lines with their numbers\n"
            f"- Format: [N] translated text (keep original numbers)\n"
            f"- Produce only the translation, without any additional explanations"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": numbered_lines},
        ]

        return self._call_api(messages)

    def _call_api(self, messages: list[dict]) -> str:
        """Call the LLM API with retry logic."""
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
                "temperature": self.temperature,
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
                "temperature": self.temperature,
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
                content = choice.get("text") or choice.get("message", {}).get("content", "")
                return content.strip()
            except (httpx.HTTPError, KeyError, IndexError) as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    delay = 2 ** attempt  # 1s, 2s, 4s
                    time.sleep(delay + (time.monotonic() % 1))  # + jitter

        raise RuntimeError(
            f"Translation API call failed after {self.max_retries} attempts"
        ) from last_error




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
                )
                _log.info("  Backend succeeded: %s (%s)", name, model)
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
    **kwargs,
) -> str:
    """One-shot subtitle translation with multi-backend failover.
    
    Returns the model name that was used for translation (or 'none' if skipped).
    """
    if not backends:
        translator = Translator(**kwargs)
        translator.translate(input_path, output_path, source_lang, target_lang, on_progress)
        return translator.model
    
    multi = MultiBackendTranslator(backends, **kwargs)
    return multi.translate(input_path, output_path, source_lang, target_lang, on_progress)
def _chunk_entries(
    entries: list[dict],
    chunk_size: int,
    overlap: int,
) -> list[list[dict]]:
    """Split entries into overlapping chunks for translation."""
    if len(entries) <= chunk_size:
        return [entries]

    chunks = []
    i = 0
    while i < len(entries):
        # Take chunk_size entries, but include 'overlap' from previous chunk for context
        start = max(0, i - overlap) if i > 0 else i
        chunk = entries[start : i + chunk_size]
        # Mark which entries are new in this chunk (not context-only)
        for e in chunk:
            e["_translate"] = e["index"] >= i
        chunks.append(chunk)
        i += chunk_size

    return chunks


def _parse_numbered_response(text: str) -> dict[int, str]:
    """Parse LLM response like '[0] Hello\n[1] World' into a dict."""
    import re

    result: dict[int, str] = {}
    pattern = re.compile(r"\[(\d+)\]\s*(.*)")

    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        match = pattern.match(line)
        if match:
            idx = int(match.group(1))
            translated = match.group(2)
            # Strip any trailing quotes or formatting the LLM might add
            translated = translated.strip('"').strip("'")
            result[idx] = translated

    return result


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
