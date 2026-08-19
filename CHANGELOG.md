# Changelog

All notable changes to Subber are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/), and this project versions with [SemVer](https://semver.org/).

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
