# Changelog

All notable changes to Subber are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/), and this project versions with [SemVer](https://semver.org/).

## [0.11.3] - 2026-10-24

### Added
- **Per-show glossaries, created automatically.** Terminology is now keyed to the
  show instead of being one global list. The first time a show is translated, the
  app writes `config/glossary/anilist-<id>.yaml` (or `tmdb-<id>` for live action)
  and seeds it with the AniList character list — native → romaji — so names stay
  consistent across episodes with no input from you. Library scans already resolve
  each file's show, so the right glossary is chosen per file; Translate-tab uploads
  are identified from their filename (time-boxed, best effort).

### Changed
- `translation.glossary` is now a **fallback** list, used only when a file's show
  cannot be identified. An identified show uses its own glossary exclusively, so
  one series' names can no longer leak into another's episodes.

## [0.11.2] - 2026-10-24

### Added
- **Per-file translation QA summary.** Every finished translation now logs one line
  — `QA: 388 cues · 0 empty · 0 label-only · 0 untranslated · longest 41 chars ·
  2/20 chunks repaired · 3 label-drift · 1 dialogue` — and the same counters travel
  in the job API (`qa`) and appear under the file in Recent Translations as
  `✓ clean` or `⚠ 2 chunk repairs · 3 drift repairs`. You can tell a good output from
  a bad one without opening the subtitle.

### Fixed
- **A cue that comes back as only a speaker label no longer ships empty.** When the
  source has dialogue but the model answers `[7] (Subaru)`, that single line is
  requested again on its own and the text is restored.
- **Cost estimates follow the model that actually ran.** Built-in per-model prices
  (Hy-MT2 30B-A3B, Hy-MT2 7B/1.8B, Llama 3.1 8B, local Ollama = free) are applied
  under `cost.models`, so switching to Hy-MT2 no longer keeps reporting Llama's
  price — which under-reported the real cost by roughly 4x. User entries in
  `cost.models` still win over the built-ins.

## [0.11.1] - 2026-10-24

### Added
- **Search results say what they are.** Each row now shows a readable show name
  plus a chip for what the file covers — `S01E04`, `Season 1 pack`,
  `S03 pack · E01–E13`, `Special 5`, `Full feature` — derived from the provider's
  filename and release name. Rows are split into **Single episodes** and
  **Season packs** when a search returns both, episodes sorted by season/episode.

### Fixed
- **Search result rows no longer overlap.** The raw release name is clamped to two
  lines with break-anywhere wrapping, the text column is allowed to shrink
  (`min-width: 0`), and the Download button keeps its own space — verified at
  1200 px and 390 px with no overlapping or overflowing boxes.
- Release-name parsing: a trailing tag after the episode (`…-01[Crunchyroll]`) no
  longer hides the episode, a hyphenated title word is no longer eaten as a
  release group, `Episode 1 ~ 13` ranges are recognised as packs, and
  folder-style names fall back to the show name rather than the folder path.

## [0.11.0] - 2026-10-24

### Added
- **Per-show terminology (glossary)** — `translation.glossary`, a list of
  `"source = target"` entries injected into the translation prompt, so names and
  terms come out the same way in every episode (`リア = Lia`,
  `ペテルギウス・ロマネコンティ = Petelgeuse Romanée-Conti`). Verified against
  Hy-MT2, which applies a listed term verbatim; anything left off the list is still
  guessed by the model, so keep the list complete.
- **Subtitle line wrapping** — `translation.wrap_chars` (default 42, `0` disables):
  translated cues are word-wrapped when written, instead of being emitted as one
  overlong line. Existing hard breaks (`\N`) are preserved.
- **The effective backend chain is logged on every translation**
  (`Backend chain: Name (model) -> ...`), plus the glossary size when configured, so
  "which model actually ran" is answerable from the log alone.

### Fixed
- **A model/backend switch written to `config.yaml` now takes effect without a
  restart.** `config.get()` cached the file for the lifetime of the process and only
  a save through the UI refreshed it, so editing the YAML directly kept running the
  previous backend — a two-model comparison could silently be the same model twice.
- **Dialogue no longer disappears when a model puts the speaker label on its own
  response line.** `[0] (Felix Argyle)` followed by the translation on the next line
  used to keep only the label; un-numbered continuation lines are now appended to
  the previous cue (4-7 cues per episode depending on the model).
- **A chunk whose response doesn't carry exactly the cues that were sent is now
  retried** — once at temperature 0, then the remaining lines one at a time.
  Previously a merged or dropped line shifted every following cue in the file
  (observed 1-15 cue desyncs, with dialogue landing on the wrong speaker). A chunk
  whose speaker labels no longer line up with the source — the tell-tale of a
  merge-plus-duplicate that keeps the line count intact — is retried the same way,
  and any window that still comes back drifted is re-translated one line at a time
  (a single-line request cannot merge), then written only if its label now matches.
  The 3 overlap cues each chunk re-sends for context are now context only: the later
  chunk no longer writes its out-of-context version over the line the previous
  chunk already translated.
- **The Translate tab no longer freezes the app while it works.** The synchronous
  translation ran inside the request's event loop, so every other endpoint
  (`/api/health`, job polling for the progress bar, Cancel) was blocked until the
  last chunk finished — a 388-cue episode made the UI unresponsive for ~60 s and the
  upload request itself only returned at the end. Translation now runs in a worker
  thread, so progress and Cancel behave live.

## [0.10.0] - 2026-09-25

### Added
- **Auto-scan type** (Settings → Advanced tuning, next to Auto-scan interval): the
  recurring auto-scan can now run "New files only (incremental)" — the default —
  instead of always re-checking the whole library (`library.auto_scan_type`).
  "Full library scan" is still one dropdown click away. Previously the scheduler
  hardcoded a full scan, so every interval re-walked every file on the media disks.
- **Startup reconciliation of orphaned scans**: a scan left `running` by a hard kill
  (SIGKILL / OOM / VM reset — the app only records a `shutdown` lifecycle event on a
  graceful exit) is now marked `paused` at startup with an explanation, and its
  orphaned `in_progress` files are returned to `pending`. Before this, a 12-day-old
  scan row kept the Library page on a phantom "Scanning…" bar at its last percentage,
  left both scan buttons disabled, answered `409 already running` to every new scan,
  and let the 30-minute stale-progress watchdog burn the in-flight files to `failed`
  ("Hung >30 min during processing (watchdog)"). Resume reuses the populated file
  list (`skip_walk`), so continuing does not repeat the filesystem walk.

### Fixed
- Translation failover no longer aborts with `'NoneType' object has no attribute
  'strip'` when an OpenAI-compatible backend answers `{"message": {"content": null}}`
  (empty or filtered completion); the retry loop now also covers
  AttributeError/ValueError, and the underlying cause is appended to
  "Translation API call failed after N attempts" instead of being discarded.
- ASR: an unreadable backend body (non-JSON / invalid UTF-8, e.g. a proxy error page)
  is reported with its HTTP status and a body snippet and fails over to the next
  backend, instead of surfacing a raw codec error and skipping the remaining backends.
- `DELETE /api/library/scan/{id}` (Cancel) now also cancels a **paused** scan, not just
  a running one, so an orphaned or operator-paused scan can no longer deadlock the page.
- `/api/logs/diagnostics` no longer raises `NameError: name 'library_db' is not defined`
  — `last_scan` and `hung_files` now populate (using the real DB column names).

[0.10.0]: https://github.com/completeBeta/subber/releases/tag/v0.10.0

## [0.9.3] - 2026-08-28

### Changed
- README accuracy pass — tab and Settings descriptions now match the actual UI (Default Languages, Subtitle Sync, Subtitle Providers, Library Settings), added missing controls (Search Season/Episode, Grab ASR checkbox, Database Backups Import DB), documented `scanner.py`, removed the now-shipped "mobile responsiveness" feature request, and fixed the security curl example.

[0.9.3]: https://github.com/completeBeta/Subber/releases/tag/v0.9.3

## [0.9.2] - 2026-08-28

### Fixed
- Library page no longer cramps its 10-column table into a 600px-wide container — it now uses the intended 1000px layout, so the table and empty-state message render without horizontal clipping on desktop.

### Changed
- README screenshots refreshed (clean empty-state captures).

[0.9.2]: https://github.com/completeBeta/Subber/releases/tag/v0.9.2

## [0.9.1] - 2026-08-28

### Fixed
- Library pagination (First/Prev/Next/Last) no longer overflows both screen edges on phones — the controls wrap and get larger touch targets.

[0.9.1]: https://github.com/completeBeta/Subber/releases/tag/v0.9.1

## [0.9.0] - 2026-08-28

### Added
- **Mobile responsiveness** — the whole UI now adapts to phones and tablets:
  - The 6-tab nav collapses into a 3×2 grid on narrow screens so every tab stays visible and tappable (previously the last tabs were silently clipped off-screen).
  - Library filter controls wrap and the search box takes a full row (was causing horizontal page overflow).
  - Settings' OpenSubtitles auth-mode radios wrap instead of overflowing.
  - Wide tables (Library, Sync preview) scroll horizontally inside their own containers.
  - Tightened card padding, enlarged touch targets, and ≥16px inputs (prevents iOS auto-zoom on focus); the Translate From/To row stacks on very small screens.

[0.9.0]: https://github.com/completeBeta/Subber/releases/tag/v0.9.0

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
