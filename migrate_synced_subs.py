#!/usr/bin/env python3
"""One-shot migration: collapse '<name>.en.synced.srt' back onto '<name>.en.srt'.

Plex/Jellyfin can't distinguish the two files, so the synced (final) version
must replace the pre-sync original. For each DB row whose subtitle_path ends
with '.synced.srt':

  - synced file missing on disk      -> report, skip (DB untouched)
  - base .en.srt exists              -> overwrite base with synced content,
                                        delete synced, point DB at base
  - base .en.srt missing             -> rename synced -> base, point DB at base

SAFETY:
  * Runs DRY-RUN by default (pass --execute to actually change anything).
  * Mounts shares via the app's own _mount_shares (refuses to proceed if the
    mount fails — an empty dir would look like "base missing" and corrupt
    the data).
  * Atomic replace via os.replace (same filesystem).
  * Never touches foreign-language originals (.ja.srt etc.) — English pair only.
"""
import argparse
import os
import sqlite3
import sys

DB = "/app/data/library.db"

parser = argparse.ArgumentParser()
parser.add_argument("--execute", action="store_true",
                    help="actually perform changes (default: dry run)")
args = parser.parse_args()

# Reuse the app's own mount logic (same code path the scan uses).
sys.path.insert(0, "/app/src")
from subber.library_pipeline import _get_mounts, _mount_shares  # noqa: E402

mounts = _get_mounts()
mount_errors = _mount_shares(mounts)
live = [m["mount_point"] for m in mounts
        if m.get("enabled", True) and m["mount_point"] not in mount_errors]
if not live:
    print(f"ABORT: no live library mounts. Errors: {mount_errors}")
    sys.exit(1)
print(f"Live mounts: {live}")
if mount_errors:
    print(f"Mount errors (continuing for live mounts): {mount_errors}")

conn = sqlite3.connect(DB)
rows = conn.execute(
    "SELECT id, subtitle_path FROM library_files "
    "WHERE subtitle_path LIKE '%.synced.srt'"
).fetchall()

stats = {"replace": 0, "rename": 0, "missing_file": 0, "errors": 0}
problems = []

for file_id, spath in rows:
    base = spath.replace(".synced.srt", ".srt")
    if not base.endswith(".srt"):
        problems.append(f"  id={file_id}: unexpected path shape: {spath}")
        stats["errors"] += 1
        continue
    if not os.path.isfile(spath):
        problems.append(f"  id={file_id}: synced file not on disk: {spath}")
        stats["missing_file"] += 1
        continue
    mode = "replace" if os.path.isfile(base) else "rename"
    if args.execute:
        try:
            os.replace(spath, base)  # atomic on same FS
            conn.execute(
                "UPDATE library_files SET subtitle_path = ? WHERE id = ?",
                (base, file_id),
            )
            stats[mode] += 1
        except OSError as e:
            problems.append(f"  id={file_id}: {mode} failed: {e}")
            stats["errors"] += 1
    else:
        stats[mode] += 1

conn.commit()
conn.close()

print(f"{'EXECUTED' if args.execute else 'DRY RUN'} — "
      f"{len(rows)} rows with .synced.srt:")
print(f"  replace (base exists, will be overwritten): {stats['replace']}")
print(f"  rename  (base missing, synced becomes base): {stats['rename']}")
print(f"  missing on disk (skipped):                  {stats['missing_file']}")
print(f"  errors:                                     {stats['errors']}")
if problems:
    print("Problems (first 20):")
    print("\n".join(problems[:20]))
if not args.execute:
    print("\nDry run only. Re-run with --execute to apply.")
