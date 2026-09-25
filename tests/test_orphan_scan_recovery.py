"""Startup reconciliation of a scan orphaned by a hard kill (SIGKILL/OOM/reset).

Production symptom (2026-09-13, scan 34): the container was killed mid-scan, so
the scan row stayed 'running' forever. The Library page then showed a phantom
"Scanning..." bar at 18%, disabled both scan buttons, answered 409 to every new
scan, and the 30-minute stale-progress watchdog burned the eight in-flight files
to 'failed' ("Hung >30 min during processing (watchdog)"). At startup there is by
definition no live scan task, so the row belongs in 'paused' (Resume re-uses the
populated file list) and orphaned file rows belong back in 'pending'.
"""
import sqlite3

from subber import library_db, web


def _seed(tmp_path, monkeypatch, *, scan_status="running", file_status="in_progress",
          stale_minutes=120):
    """Point library_db at a throwaway DB and seed one scan + one file row."""
    monkeypatch.setattr(library_db, "DB_PATH", tmp_path / "library.db")
    monkeypatch.setattr(library_db, "BACKUP_DIR", tmp_path / "backups")
    library_db.init_db()

    scan_id = library_db.create_scan("full")
    if scan_status is not None:
        library_db.update_scan(scan_id, status=scan_status)

    file_id = library_db.upsert_file({
        "file_path": "/mnt/anime-shows/Show/Season 1/Show - S01E01.mkv",
        "media_type": "tv",
        "subtitle_status": "embedded_en",
        "status": file_status,
    })
    if stale_minutes:
        conn = sqlite3.connect(str(library_db.DB_PATH))
        try:
            conn.execute(
                "UPDATE library_files SET updated_at = datetime('now', ?) WHERE id = ?",
                (f"-{stale_minutes} minutes", file_id),
            )
            conn.commit()
        finally:
            conn.close()
    return scan_id, file_id


def test_orphaned_running_scan_is_paused_and_files_requeued(tmp_path, monkeypatch):
    scan_id, file_id = _seed(tmp_path, monkeypatch)

    web._reconcile_orphaned_scans()

    scan = library_db.get_scan(scan_id)
    assert scan["status"] == "paused"
    assert "Interrupted by a restart" in (scan["error_message"] or "")
    # still resumable: not completed/failed
    assert library_db.get_active_scan()["id"] == scan_id
    assert library_db.get_file(file_id)["status"] == "pending"


def test_completed_scan_is_untouched(tmp_path, monkeypatch):
    scan_id, file_id = _seed(tmp_path, monkeypatch, scan_status="completed",
                             file_status="done")

    web._reconcile_orphaned_scans()

    assert library_db.get_scan(scan_id)["status"] == "completed"
    assert library_db.get_file(file_id)["status"] == "done"
    assert library_db.get_active_scan() is None


def test_paused_scan_keeps_its_status(tmp_path, monkeypatch):
    scan_id, _ = _seed(tmp_path, monkeypatch, scan_status="paused",
                       file_status="pending")

    web._reconcile_orphaned_scans()

    assert library_db.get_scan(scan_id)["status"] == "paused"


def test_no_scan_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(library_db, "DB_PATH", tmp_path / "library.db")
    library_db.init_db()
    web._reconcile_orphaned_scans()  # must not raise
    assert library_db.get_active_scan() is None
