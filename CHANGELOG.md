# Changelog

All notable changes to Subber are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/), and this project versions with [SemVer](https://semver.org/).

## [0.8.0] - 2026-08-27

### Added
- **Character-name seeding** — the translator now fetches the show's canonical character names from AniList (native Japanese → romaji) and injects them into the translation prompt, so names in Whisper transcripts are romanized consistently (e.g. 瓜野 → "Urino" instead of "Umino"/"Urio"/"Uri-no"; 五日市 no longer becomes "5th of the month"). Applies to the library scan (external, embedded, and ASR translation paths) and the grab tab.

[0.8.0]: https://github.com/completeBeta/Subber/releases/tag/v0.8.0

## [0.7.1] - 2026-08-27

### Fixed
- ASR (Whisper) transcripts no longer include song/BGM vocalization — a single ending theme was surfacing as ~12,000 characters of "oooh"/"おおおお" (~60% of the file) and burning translation credits. Long runs of a single repeated syllable are dropped before the SRT is written.
- Translation no longer leaves untranslated source-language lines — when the LLM omits a line number (duplicate/hallucinated segments) or passes source text through (truncated response), those lines are retried once instead of leaking Japanese/Korean/Chinese into the final file.

[0.7.1]: https://github.com/completeBeta/Subber/releases/tag/v0.7.1

## [0.7.0] - 2026-08-27

### Added
- **Audio Transcription (ASR)** — self-hosted Whisper fallback: when a video has no embedded subtitle and no provider match, transcribe the audio track to a subtitle. Available as a grab-tab "Transcribe audio if no subtitles found" toggle and a library-scan `asr_fallback`. Works with any OpenAI-compatible `/v1/audio/transcriptions` server (faster-whisper-server, etc.).
- **Content-first language detection** — subtitle language is detected from the file's actual text rather than its filename (which anime releases routinely lie about), with LLM confirmation when a filename signals multiple/dual-audio languages.
- **Dialogue filter** — before translating a foreign fansub, ASS/SSA sign, song, karaoke, and OP/ED lines are dropped by `Style` so only real dialogue is translated (large fansubs translate in seconds instead of hanging).
- **Ad / credit removal** — strip advert, donation-request, and fansub-credit lines from the intro/outro of downloaded subtitles (opt-in, off by default).
- **Provider spam filter** — drop paid-subtitle scam listings (e.g. "Get A to Z … for ₹500") from provider results.
- **Configurable watchdog timeout** — mark a file failed if it's stuck in-progress this long (default 30 min, configurable 5–180 in a new Advanced settings section).
- **Advanced settings section** — collapsed tuning knobs for translation, scan, and show identification.

### Fixed
- English subtitles no longer misclassified as foreign and re-translated (content-first detection).
- Foreign-sub translation no longer hangs on large fansub files (dialogue filter + fail-fast watchdog).
- Provider enable/disable checkboxes in Settings now actually work — they read/write the `providers.enabled` list (previously they saved per-provider flags the registry ignored).
- `model_used` now persisted for provider-downloaded translations.
- Grab-tab wrong-show fuzzy matches rejected.
- Logs page auto-refresh persistence; log endpoints gated behind the API key.

### Security
- Raw-log endpoints redact API keys/passwords/tokens and warn when exposed without an API key.

### Changed
- `max_retries` is no longer silently forced to 1 on every settings save (default is 3).

[0.7.0]: https://github.com/completeBeta/Subber/releases/tag/v0.7.0

## [0.6.0] - 2026-08-19

### Added
- **Library tab** — full-library scanning with reports, DB backups, per-mount media type override, and auto-scan scheduling.
- **Grab tab** — one-click video → subtitle pipeline (embedded extraction, provider search, ffsubsync alignment, LLM translation).
- **Translate tab** — single and batch subtitle translation with a multi-backend LLM chain.
- **Gestdown provider** (replaces the dead Addic7ed scraper); Subscene removed.
- **Logs tab** — daily rotation, full-history export, and a redacted Diagnostics bundle for bug reports.
- Paid-subscription indicator + SubDL quota alert.
- `SUBBER_PORT` env var to configure the listen port.

### Fixed
- Translate tab returned "Not Found" (frontend called routes that never existed).
- Extensionless subtitle downloads from providers.
- Paid-subscription pill overlapping the header on mobile.
- Stale unit tests referencing removed providers.

### Changed
- `max_concurrent` default unified to 5.
- Version is now surfaced in the footer, `/api/health`, and the Diagnostics bundle.

[0.6.0]: https://github.com/completeBeta/Subber/releases/tag/v0.6.0
