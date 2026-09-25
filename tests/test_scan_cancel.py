"""A paused scan must be cancellable.

Startup reconciliation parks an orphaned scan in 'paused' (see
test_orphan_scan_recovery.py). While any active scan row exists the Library page
keeps both scan buttons disabled, so if cancel refused a paused row the operator
would be permanently stuck — which is why cancel_scan accepts 'running' or
'paused' but still refuses finished scans.
"""
from subber import library_db, library_pipeline


def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(library_db, "DB_PATH", tmp_path / "library.db")
    monkeypatch.setattr(library_db, "BACKUP_DIR", tmp_path / "backups")
    library_db.init_db()


def test_cancel_paused_scan(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    sid = library_db.create_scan("full")
    library_db.update_scan(sid, status="paused", error_message="Interrupted by a restart")

    assert library_pipeline.cancel_scan(sid) is True
    assert library_db.get_scan(sid)["status"] == "cancelled"
    assert library_db.get_active_scan() is None


def test_cancel_running_scan(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    sid = library_db.create_scan("incremental")
    assert library_pipeline.cancel_scan(sid) is True
    assert library_db.get_scan(sid)["status"] == "cancelled"


def test_cannot_cancel_finished_scan(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    sid = library_db.create_scan("full")
    library_db.update_scan(sid, status="completed")
    assert library_pipeline.cancel_scan(sid) is False
    assert library_db.get_scan(sid)["status"] == "completed"
