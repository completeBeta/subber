# Subber

**Subtitle grabber, sync, and translator — Docker + Web UI.**

Upload a video, Subber finds subtitles across 6 providers, syncs them with ffsubsync, and translates non-English subs using **OpenRouter Llama 3.1 8B** (recommended — fast, cheap, accurate) with DeepSeek or Ollama as fallbacks. Or scan your entire media library and Subber handles everything automatically.

## ⚙️ How It Works

A library scan follows a deterministic pipeline designed to be **safe to run repeatedly** on a live media collection:

1. **Mounts SMB/CIFS shares** — auto-mounts configured shares at scan start (30s timeout, dead mounts abort cleanly instead of failing every file)
2. **Walks the filesystem** — discovers new, changed, and unprocessed files; skips anything already marked `done` in the DB
3. **Checks for existing subtitles** — if a non-empty `.en.srt`/`.en.ass` already sits next to the video, it's skipped immediately (no re-download, no re-sync)
4. **Extracts embedded subtitles** — ffmpeg pulls subtitle tracks from video files (configurable timeout, partial output cleaned on failure)
5. **Searches external providers** — SubDL → Addic7ed → Podnapisi → Subscene, with OpenSubtitles as a rate-limited fallback; episode-matching guard prevents wrong-episode downloads; zip/gzip packs are unpacked and matched
6. **Syncs with ffsubsync** — audio-alignment with drift threshold; skips re-sync if drift is below threshold
7. **Translates if needed** — non-English subs go through the LLM backend chain (OpenRouter → DeepSeek → Ollama) with automatic fallback
8. **Marks complete** — writes result to SQLite; scan progress, costs, and timing are all persisted

**Crash recovery:** if the container dies mid-scan, Resume picks up exactly where it left off — done files are skipped, in-progress files are re-processed. An automatic DB backup runs before every scan (newest 5 kept, oldest cycled out).

## 🚀 Quick Start (Docker)

### 1. Clone and configure

```bash
git clone https://github.com/completeBeta/Subber.git
cd Subber

# Copy the example files to create your own config
cp config.example.yaml config/config.yaml
cp docker-compose.example.yml docker-compose.yml
```

### 2. Edit your config

**`config/config.yaml`** — add your API keys (all optional, but recommended):
- `translation.backends[0].api_key` — OpenRouter API key (for translation, recommend Llama 3.1 8B ~$1 to translate hundreds of episodes)
- `translation.backends[1+]` — DeepSeek, Ollama, or any OpenAI-compatible fallback
- `providers.subdl.api_key` — free from [subdl.com](https://subdl.com)
- `providers.opensubtitles` — see [OpenSubtitles Setup](#-opensubtitles-setup) below

**`docker-compose.yml`** — uncomment and set your media paths:
```yaml
volumes:
  # Uncomment and set your media paths:
  - /path/to/your/media:/mnt/library:rw
```

### 3. Start

```bash
docker compose up -d --build
# Open http://localhost:8676
```

> **All config can also be set via the Web UI** at `/settings` after first start. The config files just save you from re-entering keys on every rebuild.

> **Minimal setup:** If you don't need translation or provider subtitles, just run step 3. Embedded subtitle extraction works with zero config.

## 📑 Tabs

### 🎬 Grab
Drop a video file — Subber probes for embedded subtitles, searches providers (SubDL, Addic7ed, Podnapisi, Subscene, OpenSubtitles, Embedded), downloads the best match, syncs it with ffsubsync, and translates if needed.

- **Multi-file concurrent uploads** with XHR progress
- **Batch zip processing** — drop a zip of videos, processes all in parallel
- **History persistence** — results survive page refreshes and container restarts
- **Clear History** button for cleanup

### 🎯 Sync Video + Subtitle (Grab tab)
Drop a video + subtitle pair — ffsubsync aligns them automatically. Shows the computed offset (e.g., `-11.18s / 11180ms`). Manual offset input for files where auto-detection fails.

### 📚 Library
Scan your media library — Subber walks directories, classifies files as TV/movie, detects subtitle status, and processes everything automatically.

- **Show Identification** via AniList (free, no API key) — resolves messy filenames to canonical titles
- **TMDB fallback** (optional API key) for movies and western TV
- **Rate-limited with caching** — safe for 1000+ shows
- **PGS distrust** — image-based subtitle tracks with wrong language metadata are flagged
- **Content-based language verification** — reads subtitle content to verify ffprobe language tags
- **Provider search with identified titles** — uses canonical English titles for higher match rates
- **Embedded subtitle extraction** with smart track selection (prefers dialogue tracks)
- **OpenRouter Llama 3.1 8B** translation (recommended — fast, cheap, accurate) with DeepSeek/Ollama fallback
- **ffsubsync audio alignment** for all subtitles
- **Zip/gzip subtitle pack unpacking** — zip/gzip downloads are extracted and episode-matched before sync
- **Episode guard** — prevents wrong-episode downloads from fallback provider search
- **CIFS robustness** — move-to-share retries with backoff; stale/inaccessible files handled gracefully
- **Mount dead-man switch** — scan aborts cleanly when all SMB mounts are down (instead of failing thousands of files)
- **Configurable extraction timeout** — ffmpeg timeout tunable via env var (default 900s) with concurrency cap
- **SQLite persistence** — scan state, file metadata, costs, and timing all stored
- **Smart rescan** — "Scan New Only" picks up genuinely new files; "Scan Library (Full)" rescans everything
- **📄 Reports** — click ☰ Reports to generate/save/view Markdown reports with success/fail/pending breakdowns and action items
- **Live auto-update** — file list refreshes during scans, expanded detail rows persist across updates
- **Clickable status pills** — click Total/Done/Pending/Failed/Skipped to filter instantly
- **Progress tracking** — per-file progress bar increments in real time during scans
- **Bulk retry** — one-click "Retry All Failed" banner when viewing failed files; retries auto-remount shares
- **💾 DB Backups** — auto backup before every scan + manual Backup Now; import/export/restore with safety snapshots; 5-backup rotation
- **Cost tracking** — per-file translation cost with accurate DeepSeek pricing (input+output tokens)
- **Smart fansub stripping** — removes [GroupName] tags and hex hashes from show titles
- **OpenSubtitles fallback** — only searched when primary providers find nothing, saving quota
- **ConvertX integration** — deploy ConvertX alongside Subber for video conversion

### 🌍 Translate
Upload `.srt`, `.ass`, `.vtt`, or `.zip` files for translation. Multi-backend support with automatic fallback.

- **History persistence** with progress bars for in-progress jobs
- **Auto-polling** while jobs are active
- **Clear History** button

### 🔍 Search
Search all enabled providers by show name, season, and episode. Returns results with download links.

### 📋 Logs
Real-time log viewer with search, level filter, auto-refresh, **full-history export**, and **redacted diagnostics bundle** (safe for bug reports). Shows live **API call stats** per provider (searches/downloads today). Daily log rotation with **45-day retention**.

### ⚙️ Settings
Full configuration UI with live save:

- **AI Backends** — multiple translation backends with priority ordering (OpenRouter, DeepSeek, Ollama, OpenAI-compatible)
- **Subtitle Providers** — toggle and configure SubDL (with PRO mode), Addic7ed, Podnapisi, Subscene, OpenSubtitles (.org VIP 1,000/day or [.com](http://opensubtitles.com) API packages), Embedded
- **Cost Estimation** — per-token input/output pricing with peak hour multipliers and per-model overrides
- **🔒 Security** — optional API key for write protection (disabled by default)
- **💾 DB Backups** — automatic scan-start backup + manual Backup Now; import/export/restore with safety snapshots
- **✅ Save Validation** — red outlines + clickable error summary (e.g. missing VIP credentials) prevents saving broken config
- **Show Identification** — AniList toggle, TMDB API key, preferred source selector
- **Library Settings** — scan paths, concurrency, sync threshold, auto-scan interval
- **SMB/CIFS Library Mounts** — configure and test shares with password-guarded save (never wipes credentials on edit)

## 🌍 Subtitle Providers

Providers are searched in priority order. **OpenSubtitles is a fallback** — only searched if primary providers (SubDL, Addic7ed, Podnapisi, Subscene) return nothing. This saves your daily quota for when nothing else works.

| Provider | Type | Auth | Rate Limit | Fallback? |
|---|---|---|---|---|
| 🎬 **Embedded** | ffmpeg extraction | None | Unlimited | No |
| 🔑 **SubDL** | REST API | API key | 2,000/day (30,000 PRO) | No |
| 📺 **Addic7ed** | Web scraping | Cookies | Fair use | No |
| 🌍 **Podnapisi** | Web scraping | None | Fair use | No |
| 🎬 **Subscene** | Web scraping | None | Fair use | No |
| 🌐 **OpenSubtitles** | REST API | .org user/pass or .com API key | 20/day Lite, 40/day Developer, 1,000/day .org VIP, configurable .com | **Yes** |

### 🔑 OpenSubtitles Setup

OpenSubtitles supports two auth methods — use **one or both**. Each mode has its **own API key** so switching never clears the other mode's credentials:

#### opensubtitles.org VIP (1,000 downloads/day)
If you have a VIP subscription at [opensubtitles.org](https://www.opensubtitles.org):
1. Create a **free API consumer key** at [opensubtitles.com/consumers](https://www.opensubtitles.com/en/consumers) (takes 10 seconds)
2. In Subber Settings → OpenSubtitles, set Mode to **VIP — .org auth (1,000/day)**:
   - **VIP API Key** — paste your consumer key in the dedicated VIP key field
   - **Username** — your opensubtitles.org username
   - **Password** — your opensubtitles.org password
3. The VIP key is **separate from** the `.com` API key — switching modes never blanks the other.

> **Why the API key?** The `.org` and `.com` share the same REST API. The consumer key authorizes API access; your username+password authenticates your VIP account.

#### opensubtitles.com API Consumer (configurable limit)
If you purchased an API package at [opensubtitles.com](https://www.opensubtitles.com):
1. Get your API key from [opensubtitles.com/consumers](https://www.opensubtitles.com/en/consumers)
2. In Subber Settings → OpenSubtitles → `.com API Consumer Key` section, paste your key
3. Select your tier: Free (5/day), Premium (5,000/day), or set a custom daily limit

> **Daily limit override:** Set a custom number in the "Custom daily limit" field to override the tier default. Useful for enterprise packages (up to 50,000/day).

## 🤖 LLM Backends

Subber uses a multi-backend translator with automatic fallback. **Recommended setup:**

```yaml
backends:
  - name: OpenRouter Llama 3.1 8B
    api_base: https://openrouter.ai/api/v1
    model: meta-llama/llama-3.1-8b-instruct
  - name: DeepSeek V4 Flash (fallback)
    api_base: https://api.deepseek.com/v1
    model: deepseek-v4-flash
  - name: Ollama (local)
    api_base: http://localhost:11434/v1
    model: llama3.2:3b
```

OpenRouter Llama 3.1 8B costs ~$1 to translate **hundreds** of episodes — far cheaper than DeepSeek and better at subtitle-format adherence. Works with any OpenAI-compatible API — cloud or local.

## 📺 Show Identification

Subber resolves messy filenames into canonical metadata:

| File | → | Identified As |
|---|---|---|
| `Busamen Gachi Fighter 01.mkv` | → | **Uglymug, Epicfighter** (AniList #184575) |
| `7th.Time.Loop.S01E01.mkv` | → | **7th Time Loop: The Villainess Enjoys...** (AniList #168374) |

- **AniList** (free, no key) — covers anime
- **TMDB** (optional free API key) — covers movies/TV
- **Rate-limited**: 1 req/670ms (safe under 90/min limit)
- **Cached**: second query is instant
- **Batch deduplication**: 1000 files across 4 shows = 4 API calls

## 🔒 Security

An optional **API key** protects write operations (scan, translate, grab, settings changes). Disabled by default — set it in Settings → Security if you expose Subber beyond your LAN.

```
# Without API key (default): everything works
curl -X POST http://localhost:8676/api/library/scan ...

# With API key set: all writes require the header
curl -X POST http://localhost:8676/api/library/scan \
  -H "X-API-Key: your-key-here" ...
```

GET endpoints (read-only) are always open. Write endpoints (scan, translate, grab, settings changes) require the `X-API-Key` header **only when a key is configured**. When no key is set, writes are open — so you can always set your first key via the Settings UI.

For SSL, use Cloudflare Tunnel or a reverse proxy (nginx/Caddy). Subber itself runs plain HTTP on port 8676.

## 🔧 Architecture

```
Video file → ffprobe (subtitle tracks)
           → identify.py (AniList/TMDB → canonical titles)
           → ProviderRegistry.search_all() (primary providers first, fallback last)
           → Download best match
           → ffsubsync (audio alignment)
           → DeepSeek/Ollama (translation if needed)
           → Save to library / return to user
           → Cost estimator: accurate DeepSeek input+output pricing
           → Cancel scan: DELETE /api/library/scan/{id}
           → Incremental progress: per-file scan counter updates
```

### File Structure

```
src/subber/
├── web.py              # FastAPI app, all routes + auth
├── config.py           # YAML config with deep merge
├── identify.py         # AniList + TMDB show identification
├── translator.py       # Multi-backend LLM translator
├── syncer.py           # ffsubsync wrapper (0.5.0 compat)
├── safewrite.py        # Safe subtitle file writes
├── providers/          # Subtitle provider implementations
│   ├── subdl.py        # SubDL REST API
│   ├── addic7ed.py     # Addic7ed scraper
│   ├── podnapisi.py    # Podnapisi scraper
│   ├── subscene.py     # Subscene scraper
│   ├── opensubtitles.py # OpenSubtitles REST API (rate-limited)
│   ├── embedded.py     # ffmpeg subtitle extraction
│   ├── registry.py     # Parallel provider search (primary-first, fallback-last)
│   ├── provider_stats.py # API call tracking per provider
│   └── base.py         # Abstract provider + capabilities
├── library_db.py       # SQLite schema + CRUD + report generation
├── library_scanner.py  # Filesystem walker + classifier
└── library_pipeline.py # Orchestrator (detect → extract → translate → sync)

templates/
├── grab.html           # Grab tab
├── index.html          # Translate tab
├── library.html        # Library tab (+ reports panel)
├── logs.html           # Log viewer (+ API stats)
├── settings.html       # Settings tab (+ security)
├── search.html         # Search tab
└── base.html           # Shared layout + nav

static/
├── style.css           # Global styles
├── library.css         # Library-specific styles
└── app.js              # Translate tab JS
```

## 🐳 Docker

```bash
docker compose up -d --build
```

- Runs on port `8676`
- Volume mounts: `./uploads`, `./config`, `./data` (SQLite + logs + reports + stats)
- Library mount: `/mnt/test_library` → your media directory
- Host networking mode (required for provider API access)
- Auto-cleanup: temporary files removed after 24 hours
- Logs: daily rotation with 45-day retention at `/app/data/subber.log`
- Full-history log export + redacted diagnostics bundle via Logs tab
- Provider stats: daily API call counts at `/app/data/provider_stats.json`
- Reports: Markdown reports at `/app/data/reports/`

## 📦 Installation (without Docker)

```bash
pip install git+https://github.com/completeBeta/Subber.git

# Or for local development:
git clone https://github.com/completeBeta/Subber.git
cd Subber
pip install -e ".[dev]"
```

## 📋 Requirements

- **Docker** with Docker Compose
- **SMB/CIFS mounts** (optional): container needs `cap_add: [SYS_ADMIN, DAC_READ_SEARCH]` in `docker-compose.yml`
- **cifs-utils**: installed automatically in the Docker image (for SMB mount support)
- **Translation**: DeepSeek API key (or Ollama for local)
- **Subtitles**: free API keys from SubDL and OpenSubtitles (optional but recommended)

## 🔑 Configuration

All configurable via the Web UI at `/settings`. Config file at `config/config.yaml` (Docker) or `~/.config/subber/config.yaml` (standalone).

Key sections: `translation.backends`, `providers`, `library`, `sync`, `cost`, `limits`, `ui` (API key).

## 📜 License

MIT — see [LICENSE](LICENSE).
