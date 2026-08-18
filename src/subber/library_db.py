"""SQLite database for the Library tab — schema, migrations, and CRUD operations."""

import json
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


DB_PATH = Path("/app/data/library.db")
BACKUP_DIR = Path("/app/data/backups")
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    """Get a SQLite connection with row factory enabled."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create tables and indexes if they don't exist."""
    with _lock:
        conn = _connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS library_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT UNIQUE NOT NULL,
                    file_hash TEXT,
                    file_size INTEGER,
                    media_type TEXT NOT NULL DEFAULT 'tv',
                    show_title TEXT,
                    season INTEGER,
                    episode INTEGER,
                    episode_title TEXT,
                    movie_title TEXT,
                    movie_year INTEGER,
                    subtitle_status TEXT NOT NULL DEFAULT 'unknown',
                    subtitle_languages TEXT,
                    action_taken TEXT,
                    subtitle_path TEXT,
                    provider_used TEXT,
                    model_used TEXT,
                    sync_drift_ms INTEGER,
                    translation_cost REAL DEFAULT 0.0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error_message TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                );

                CREATE INDEX IF NOT EXISTS idx_status ON library_files(status);
                CREATE INDEX IF NOT EXISTS idx_media_type ON library_files(media_type);
                CREATE INDEX IF NOT EXISTS idx_show_season_ep ON library_files(show_title, season, episode);
                CREATE INDEX IF NOT EXISTS idx_updated ON library_files(updated_at);
                CREATE INDEX IF NOT EXISTS idx_file_path ON library_files(file_path);

                CREATE TABLE IF NOT EXISTS scan_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_type TEXT NOT NULL,
                    files_total INTEGER,
                    files_processed INTEGER DEFAULT 0,
                    files_skipped INTEGER DEFAULT 0,
                    files_failed INTEGER DEFAULT 0,
                    translation_cost REAL DEFAULT 0.0,
                    status TEXT NOT NULL DEFAULT 'running',
                    started_at TEXT DEFAULT (datetime('now')),
                    completed_at TEXT,
                    error_message TEXT
                );
            """)
            conn.commit()
        finally:
            conn.close()


# ── File CRUD ──

def upsert_file(record: dict) -> int:
    """Insert or update a library file record. Returns the row ID."""
    with _lock:
        conn = _connect()
        try:
            cols = [
                "file_path", "file_hash", "file_size", "media_type",
                "show_title", "season", "episode", "episode_title",
                "movie_title", "movie_year",
                "subtitle_status", "subtitle_languages",
                "action_taken", "subtitle_path", "provider_used", "model_used",
                "sync_drift_ms", "translation_cost", "status", "error_message",
            ]
            values = [record.get(c) for c in cols]
            placeholders = ",".join(["?"] * len(cols))
            col_list = ",".join(cols)
            update_set = ",".join([f"{c}=excluded.{c}" for c in cols if c != "file_path"])
            update_set += ",updated_at=datetime('now')"

            cursor = conn.execute(
                f"""INSERT INTO library_files ({col_list})
                    VALUES ({placeholders})
                    ON CONFLICT(file_path) DO UPDATE SET {update_set}""",
                values,
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()


def bulk_upsert(records: list[dict]) -> int:
    """Insert/update many file records in ONE transaction (fast bulk path).

    Used for the initial upsert-all-files phase of a scan — 22K individual
    commits would hold the DB lock for minutes and block API calls.
    Returns number of records written.
    """
    if not records:
        return 0
    with _lock:
        conn = _connect()
        try:
            cols = [
                "file_path", "file_hash", "file_size", "media_type",
                "show_title", "season", "episode", "episode_title",
                "movie_title", "movie_year",
                "subtitle_status", "subtitle_languages",
                "action_taken", "subtitle_path", "provider_used", "model_used",
                "sync_drift_ms", "translation_cost", "status", "error_message",
            ]
            placeholders = ",".join(["?"] * len(cols))
            col_list = ",".join(cols)
            update_set = ",".join([f"{c}=excluded.{c}" for c in cols if c != "file_path"])
            update_set += ",updated_at=datetime('now')"
            rows = [[record.get(c) for c in cols] for record in records]
            conn.executemany(
                f"""INSERT INTO library_files ({col_list})
                    VALUES ({placeholders})
                    ON CONFLICT(file_path) DO UPDATE SET {update_set}""",
                rows,
            )
            conn.commit()
            return len(records)
        finally:
            conn.close()


def get_file(file_id: int) -> dict | None:
    """Get a single file by ID."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM library_files WHERE id = ?", (file_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def get_file_by_path(file_path: str) -> dict | None:
    """Get a single file by path."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM library_files WHERE file_path = ?", (file_path,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def update_file_status(
    file_id: int,
    status: str,
    action_taken: str | None = None,
    subtitle_path: str | None = None,
    provider_used: str | None = None,
    model_used: str | None = None,
    sync_drift_ms: int | None = None,
    translation_cost: float | None = None,
    error_message: str | None = None,
    subtitle_languages: list | None = None,
) -> None:
    """Update a file's processing status and results.

    subtitle_languages: final subtitle language list once known (e.g. ["en"]).
    Detection runs BEFORE download (often '[]' for no-subs files), so the
    final language is written here when the subtitle actually lands.
    """
    updates = ["status = ?", "updated_at = datetime('now')"]
    params: list[Any] = [status]

    if action_taken is not None:
        updates.append("action_taken = ?")
        params.append(action_taken)
    if subtitle_path is not None:
        updates.append("subtitle_path = ?")
        params.append(subtitle_path)
    if provider_used is not None:
        updates.append("provider_used = ?")
        params.append(provider_used)
    if model_used is not None:
        updates.append("model_used = ?")
        params.append(model_used)
    if sync_drift_ms is not None:
        updates.append("sync_drift_ms = ?")
        params.append(sync_drift_ms)
    if translation_cost is not None:
        updates.append("translation_cost = ?")
        params.append(translation_cost)
    if error_message is not None:
        updates.append("error_message = ?")
        params.append(error_message)
    if subtitle_languages is not None:
        import json as _json
        updates.append("subtitle_languages = ?")
        params.append(_json.dumps(subtitle_languages))

    params.append(file_id)
    set_clause = ", ".join(updates)

    with _lock:
        conn = _connect()
        try:
            conn.execute(
                f"UPDATE library_files SET {set_clause} WHERE id = ?", params
            )
            conn.commit()
        finally:
            conn.close()


def query_files(
    status: str | None = None,
    media_type: str | None = None,
    page: int = 1,
    limit: int = 50,
    sort: str = "updated_at",
    order: str = "desc",
    search: str | None = None,
    action: str | None = None,
) -> dict:
    """Query library files with filtering, sorting, and pagination.

    Returns {"files": [...], "total": N, "page": P, "limit": L}
    """
    # Validate sort column
    allowed_sort = {
        "updated_at", "created_at", "file_path", "show_title",
        "movie_title", "status", "media_type", "season", "episode",
    }
    if sort not in allowed_sort:
        sort = "updated_at"
    order = "ASC" if order.lower() == "asc" else "DESC"

    where_clauses: list[str] = []
    params: list[Any] = []

    if status and status != "all":
        if status == "failed":
            # "failed" view = files needing attention: genuinely failed AND
            # those currently queued for retry. Folding `retrying` in here keeps
            # retried items visible in the failed filter instead of vanishing
            # the instant a retry resets them to a non-failed status.
            where_clauses.append("status IN ('failed', 'retrying')")
        else:
            where_clauses.append("status = ?")
            params.append(status)
    if media_type and media_type != "all":
        where_clauses.append("media_type = ?")
        params.append(media_type)
    if action and action != "all":
        # Accept comma-separated values so UI pills can map one click to
        # multiple action_taken values (e.g. Translated -> translated +
        # downloaded_and_translated).
        action_list = [a.strip() for a in action.split(",") if a.strip()]
        if len(action_list) == 1:
            where_clauses.append("action_taken = ?")
            params.append(action_list[0])
        elif action_list:
            where_clauses.append(f"action_taken IN ({','.join('?' * len(action_list))})")
            params.extend(action_list)
    if search:
        where_clauses.append("(show_title LIKE ? OR movie_title LIKE ? OR file_path LIKE ?)")
        params.extend([f"%{search}%"] * 3)

    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    offset = (page - 1) * limit

    with _lock:
        conn = _connect()
        try:
            # Count
            total = conn.execute(
                f"SELECT COUNT(*) FROM library_files{where_sql}", params
            ).fetchone()[0]

            # Query
            rows = conn.execute(
                f"""SELECT * FROM library_files{where_sql}
                    ORDER BY {sort} {order}
                    LIMIT ? OFFSET ?""",
                params + [limit, offset],
            ).fetchall()

            files = []
            for row in rows:
                d = dict(row)
                if d.get("subtitle_languages"):
                    try:
                        d["subtitle_languages"] = json.loads(d["subtitle_languages"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                files.append(d)

            return {"files": files, "total": total, "page": page, "limit": limit}
        finally:
            conn.close()


def get_unprocessed_files() -> list[dict]:
    """Return all files that still need processing (not done/skipped).

    Used by resume to skip the filesystem walk and process straight from the
    DB — the walk already populated the table, so re-walking 24K files over
    CIFS would just waste 20-40 minutes.
    """
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM library_files WHERE status NOT IN ('done', 'skipped')"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def get_all_pending() -> list[dict]:
    """Get all files with status 'pending'."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM library_files WHERE status = 'pending' ORDER BY file_path"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def mark_stale_in_progress(minutes: int | None = None) -> int:
    """Reset 'in_progress' files back to 'pending'.

    Args:
        minutes: if set, only reset rows whose updated_at is OLDER than this
            many minutes (a watchdog/heartbeat use — a file legitimately
            processing gets updated frequently, so old rows are corpses from
            crashes, hangs, or killed containers). If None, resets ALL
            in_progress rows (crash-recovery on resume).

    Returns count of reset files.
    """
    with _lock:
        conn = _connect()
        try:
            if minutes is None:
                cursor = conn.execute(
                    """UPDATE library_files
                       SET status = 'pending', updated_at = datetime('now')
                       WHERE status = 'in_progress'"""
                )
            else:
                cursor = conn.execute(
                    """UPDATE library_files
                       SET status = 'pending', updated_at = datetime('now')
                       WHERE status = 'in_progress'
                         AND updated_at < datetime('now', ?)""",
                    (f"-{minutes} minutes",),
                )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()


def get_stale_in_progress(minutes: int = 30) -> list[dict]:
    """Return rows stuck 'in_progress' for longer than `minutes`.

    Used by the watchdog to log WHICH files hung and for how long before
    resetting them — the previous code only returned a count, so a stall
    gave no clue about what file (or provider) was responsible.
    """
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT id, file_path, status, updated_at,
                          CAST((julianday('now') - julianday(updated_at)) * 1440 AS INTEGER) AS minutes_stale
                   FROM library_files
                   WHERE status = 'in_progress'
                     AND updated_at < datetime('now', ?)
                   ORDER BY updated_at ASC""",
                (f"-{minutes} minutes",),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def get_total_file_count(media_types: list[str] | None = None) -> int:
    """Return the total number of files in the library (optionally filtered).

    Used to set scan_history.files_total correctly on resume — the resume path
    only loads UNPROCESSED files, so len(records) would be the *remaining*
    count, not the full-scan total. That mismatch made progress show 326%.
    """
    with _lock:
        conn = _connect()
        try:
            if media_types:
                placeholders = ",".join("?" * len(media_types))
                row = conn.execute(
                    f"SELECT COUNT(*) FROM library_files WHERE media_type IN ({placeholders})",
                    list(media_types),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM library_files").fetchone()
            return row[0] if row else 0
        finally:
            conn.close()


# ── Stats ──

def get_stats() -> dict:
    """Get aggregate statistics for the library."""
    with _lock:
        conn = _connect()
        try:
            # Total counts by status
            status_rows = conn.execute(
                """SELECT media_type, status, COUNT(*) as count
                   FROM library_files GROUP BY media_type, status"""
            ).fetchall()

            tv_stats = {"total": 0, "done": 0, "failed": 0, "pending": 0, "in_progress": 0, "skipped": 0, "retrying": 0}
            movie_stats = {"total": 0, "done": 0, "failed": 0, "pending": 0, "in_progress": 0, "skipped": 0, "retrying": 0}

            for row in status_rows:
                mt = row["media_type"]
                stats = tv_stats if mt == "tv" else movie_stats
                stats["total"] += row["count"]
                if row["status"] in stats:
                    stats[row["status"]] += row["count"]

            # Action breakdown
            action_rows = conn.execute(
                """SELECT action_taken, COUNT(*) as count
                   FROM library_files WHERE action_taken IS NOT NULL
                   GROUP BY action_taken"""
            ).fetchall()
            actions = {row["action_taken"]: row["count"] for row in action_rows}

            # Total cost
            cost_row = conn.execute(
                "SELECT COALESCE(SUM(translation_cost), 0) as total FROM library_files"
            ).fetchone()
            total_cost = cost_row["total"]

            # Last scan
            last_scan = conn.execute(
                """SELECT * FROM scan_history ORDER BY started_at DESC LIMIT 1"""
            ).fetchone()

            return {
                "tv": tv_stats,
                "movies": movie_stats,
                "actions": actions,
                "total_cost": round(total_cost, 4),
                "last_scan": dict(last_scan) if last_scan else None,
            }
        finally:
            conn.close()


# ── Scan History ──

def create_scan(scan_type: str) -> int:
    """Create a new scan history record. Returns scan ID."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                """INSERT INTO scan_history (scan_type, status, started_at)
                   VALUES (?, 'running', datetime('now'))""",
                (scan_type,),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()


def update_scan(
    scan_id: int,
    files_total: int | None = None,
    files_processed: int | None = None,
    files_skipped: int | None = None,
    files_failed: int | None = None,
    translation_cost: float | None = None,
    status: str | None = None,
    error_message: str | None = None,
) -> None:
    """Update a scan history record."""
    updates: list[str] = []
    params: list[Any] = []

    if files_total is not None:
        updates.append("files_total = ?")
        params.append(files_total)
    if files_processed is not None:
        updates.append("files_processed = ?")
        params.append(files_processed)
    if files_skipped is not None:
        updates.append("files_skipped = ?")
        params.append(files_skipped)
    if files_failed is not None:
        updates.append("files_failed = ?")
        params.append(files_failed)
    if translation_cost is not None:
        updates.append("translation_cost = ?")
        params.append(translation_cost)
    if status is not None:
        updates.append("status = ?")
        params.append(status)
        if status in ("completed", "failed"):
            updates.append("completed_at = datetime('now')")
    if error_message is not None:
        updates.append("error_message = ?")
        params.append(error_message)

    if not updates:
        return

    params.append(scan_id)
    set_clause = ", ".join(updates)

    with _lock:
        conn = _connect()
        try:
            conn.execute(
                f"UPDATE scan_history SET {set_clause} WHERE id = ?", params
            )
            conn.commit()
        finally:
            conn.close()


def increment_scan_progress(scan_id: int) -> None:
    """Increment files_processed counter for a scan by 1."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE scan_history SET files_processed = files_processed + 1 WHERE id = ?",
                (scan_id,),
            )
            conn.commit()
        finally:
            conn.close()

def get_scan(scan_id: int) -> dict | None:
    """Get a scan history record."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM scan_history WHERE id = ?", (scan_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def get_active_scan() -> dict | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM scan_history WHERE status IN ('running', 'paused') ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def get_scan_history(limit: int = 20) -> list[dict]:
    """Get recent scan history."""
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM scan_history ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


# ── Cost breakdown ──

def get_cost_breakdown() -> dict:
    """Get cost breakdown by show and by month."""
    with _lock:
        conn = _connect()
        try:
            # By show
            show_rows = conn.execute(
                """SELECT COALESCE(show_title, movie_title, 'Unknown') as title,
                          SUM(translation_cost) as cost,
                          COUNT(*) as count
                   FROM library_files WHERE translation_cost > 0
                   GROUP BY title ORDER BY cost DESC LIMIT 50"""
            ).fetchall()

            # By month
            month_rows = conn.execute(
                """SELECT strftime('%Y-%m', updated_at) as month,
                          SUM(translation_cost) as cost,
                          COUNT(*) as count
                   FROM library_files WHERE translation_cost > 0
                   GROUP BY month ORDER BY month DESC"""
            ).fetchall()

            total = conn.execute(
                "SELECT COALESCE(SUM(translation_cost), 0) as total FROM library_files"
            ).fetchone()["total"]

            return {
                "by_show": [dict(r) for r in show_rows],
                "by_month": [dict(r) for r in month_rows],
                "total": round(total, 4),
            }
        finally:
            conn.close()


# ── Report generation ──

def generate_report() -> str:
    """Generate a comprehensive Markdown report of the library.

    Sections: summary, succeeded, failed (action items), pending, skipped.

    Returns markdown string ready for saving or display.
    """
    stats = get_stats()
    all_files = query_files(status="all", limit=100000, sort="file_path", order="asc")
    files = all_files["files"]

    succeeded = [f for f in files if f["status"] == "done"]
    failed = [f for f in files if f["status"] == "failed"]
    pending = [f for f in files if f["status"] == "pending"]
    skipped = [f for f in files if f["status"] == "skipped"]
    in_progress = [f for f in files if f["status"] == "in_progress"]

    total = len(files)
    done_pct = round(len(succeeded) / total * 100, 1) if total else 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    md = f"""# 📊 Subber Library Report

**Generated:** {now}  
**Total files:** {total} | ✅ {len(succeeded)} done ({done_pct}%) | ❌ {len(failed)} failed | ⏳ {len(pending)} pending | 🔄 {len(in_progress)} in progress | ⏭️ {len(skipped)} skipped

---

## 📈 Summary

| Category | Count |
|----------|-------|
| ✅ Done | {len(succeeded)} |
| ❌ Failed | {len(failed)} |
| ⏳ Pending | {len(pending)} |
| 🔄 In Progress | {len(in_progress)} |
| ⏭️ Skipped | {len(skipped)} |
| **Total** | **{total}** |

**Actions breakdown:** {json.dumps(stats.get("actions", {}))}  
**Total translation cost:** ${(stats.get("total_cost") or 0):.4f}

---

"""

    # ── Action Items (failed files) — most important section ──
    if failed:
        md += "## 🚨 Action Items — Failed Files\n\n"
        md += "These files need attention. Check error messages for root causes. "
        md += "Use the Library tab **Retry** button or fix the underlying issue.\n\n"
        md += "| # | Show / Movie | Episode | Error |\n"
        md += "|---|-------------|---------|-------|\n"
        for i, f in enumerate(failed, 1):
            title = _file_display_title(f)
            episode = _file_episode_label(f)
            error = (f.get("error_message") or "Unknown error")[:80]
            md += f"| {i} | {title} | {episode} | {error} |\n"
        md += "\n---\n\n"

    # ── Pending files ──
    if pending:
        md += "## ⏳ Pending Files\n\n"
        md += "Files not yet processed. Run a scan in **Apply** mode to process them.\n\n"
        md += "| # | Show / Movie | Episode | Type | Path |\n"
        md += "|---|-------------|---------|------|------|\n"
        for i, f in enumerate(pending[:50], 1):
            title = _file_display_title(f)
            episode = _file_episode_label(f)
            path = _short_path(f.get("file_path", ""), 60)
            md += f"| {i} | {title} | {episode} | {f.get('media_type', '?')} | {path} |\n"
        if len(pending) > 50:
            md += f"\n*...and {len(pending) - 50} more pending files*\n"
        md += "\n---\n\n"

    # ── Succeeded files ──
    if succeeded:
        md += "## ✅ Successfully Processed\n\n"
        md += "| # | Show / Movie | Episode | Action | Provider | Drift | Cost |\n"
        md += "|---|-------------|---------|--------|----------|-------|------|\n"
        for i, f in enumerate(succeeded[:100], 1):
            title = _file_display_title(f)
            episode = _file_episode_label(f)
            action = f.get("action_taken", "—")
            provider = f.get("provider_used", "—")
            drift = f"{f.get('sync_drift_ms', '—')}ms" if f.get("sync_drift_ms") else "—"
            cost = f"${(f.get('translation_cost') or 0):.4f}"
            md += f"| {i} | {title} | {episode} | {action} | {provider} | {drift} | {cost} |\n"
        if len(succeeded) > 100:
            md += f"\n*...and {len(succeeded) - 100} more succeeded files*\n"
        md += "\n---\n\n"

    # ── Skipped files ──
    if skipped:
        md += "## ⏭️ Skipped Files\n\n"
        md += f"{len(skipped)} files were skipped (already had English subtitles).\n\n"

    # ── Footer ──
    md += "---\n"
    md += f"*Report generated by [Subber](https://github.com/completeBeta/Subber) on {now}*\n"

    return md


def _file_display_title(f: dict) -> str:
    """Get human-readable title for a file record."""
    if f.get("media_type") == "tv":
        return f.get("show_title") or "Unknown Show"
    return f.get("movie_title") or "Unknown Movie"


def _file_episode_label(f: dict) -> str:
    """Get episode label for a file record."""
    if f.get("media_type") == "tv":
        s = str(f.get("season", 0)).zfill(2)
        e = str(f.get("episode", 0)).zfill(2)
        return f"S{s}E{e}"
    if f.get("movie_year"):
        return f"({f['movie_year']})"
    return "—"


def _short_path(path: str, max_len: int = 60) -> str:
    """Truncate a path for display, keeping the tail."""
    if len(path) <= max_len:
        return path
    return "…" + path[-(max_len - 1):]


# ═══════════════════════════════════════════════
# Database backups — create, list, restore, rotate
# ═══════════════════════════════════════════════

# Keep the N most recent auto/manual backups; the newest pre_restore snapshot
# (taken automatically before every restore) is kept separately so a bad
# restore can always be undone.
BACKUP_KEEP = 5
_BACKUP_NAME_RE = re.compile(
    r"^library_(\d{8}_\d{6})_(auto|manual|pre_restore|imported)\.db$"
)


def create_backup(kind: str = "manual") -> dict:
    """Snapshot the live DB using SQLite's online backup API (safe while a
    scan is writing — WAL-consistent, no file copy races).

    kind: 'auto' (scan start), 'manual' (user), 'pre_restore' (safety net).
    Returns {"name", "path", "size"} and rotates old backups.
    """
    if kind not in ("auto", "manual", "pre_restore", "imported"):
        kind = "manual"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"library_{ts}_{kind}.db"

    src = sqlite3.connect(str(DB_PATH), timeout=30)
    try:
        dst = sqlite3.connect(str(dest), timeout=30)
        try:
            with _lock:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    _rotate_backups()
    return {"name": dest.name, "path": str(dest), "size": dest.stat().st_size}


def _rotate_backups() -> None:
    """Keep the newest BACKUP_KEEP auto/manual backups (by age) and the newest
    pre_restore snapshot. Delete the rest."""
    try:
        backups = _scan_backup_dir()
    except OSError:
        return

    regular = sorted(
        (b for b in backups if b["kind"] in ("auto", "manual")),
        key=lambda b: b["created"], reverse=True,
    )
    pre_restore = sorted(
        (b for b in backups if b["kind"] == "pre_restore"),
        key=lambda b: b["created"], reverse=True,
    )
    doomed = regular[BACKUP_KEEP:] + pre_restore[1:]
    for b in doomed:
        try:
            Path(b["path"]).unlink(missing_ok=True)
        except OSError:
            pass


def _scan_backup_dir() -> list[dict]:
    """Enumerate valid backup files with metadata."""
    if not BACKUP_DIR.exists():
        return []
    out = []
    for p in BACKUP_DIR.iterdir():
        m = _BACKUP_NAME_RE.match(p.name)
        if not m or not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        # Row count gives the user a sense of what each backup contains
        rows = None
        try:
            conn = sqlite3.connect(str(p), timeout=5)
            try:
                rows = conn.execute(
                    "SELECT COUNT(*) FROM library_files"
                ).fetchone()[0]
            finally:
                conn.close()
        except Exception:
            pass
        out.append({
            "name": p.name,
            "kind": m.group(2),
            "created": m.group(1),
            "created_display": (
                f"{m.group(1)[0:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]} "
                f"{m.group(1)[9:11]}:{m.group(1)[11:13]}:{m.group(1)[13:15]}"
            ),
            "size": st.st_size,
            "file_count": rows,
            "path": str(p),
        })
    out.sort(key=lambda b: b["created"], reverse=True)
    return out


def list_backups() -> list[dict]:
    return _scan_backup_dir()


def validate_backup_file(path: Path) -> tuple[bool, str]:
    """Check that a file is a SQLite DB containing our library_files table."""
    try:
        conn = sqlite3.connect(str(path), timeout=10)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if "library_files" not in tables:
                return False, "Not a Subber library DB (missing library_files table)"
            # Integrity check catches truncated/corrupt uploads before restore
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if ok != "ok":
                return False, f"Backup failed integrity check: {ok}"
            return True, "ok"
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        return False, f"Not a valid SQLite database: {e}"
    except OSError as e:
        return False, str(e)


def restore_backup(name: str) -> dict:
    """Restore a backup over the live DB.

    Safety: 1) snapshot the CURRENT db first (pre_restore), 2) validate the
    backup, 3) restore via the online backup API (atomic, no file juggling).
    """
    src_path = BACKUP_DIR / name
    if not src_path.is_file() or not _BACKUP_NAME_RE.match(name):
        raise ValueError(f"Unknown backup: {name}")

    ok, msg = validate_backup_file(src_path)
    if not ok:
        raise ValueError(msg)

    # Safety net — never a point of no return
    snapshot = create_backup(kind="pre_restore")

    src = sqlite3.connect(str(src_path), timeout=30)
    try:
        dst = sqlite3.connect(str(DB_PATH), timeout=30)
        try:
            with _lock:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM library_files").fetchone()[0]
    finally:
        conn.close()

    return {"restored": name, "safety_snapshot": snapshot["name"], "file_count": rows}
