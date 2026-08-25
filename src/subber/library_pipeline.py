"""Library pipeline orchestrator.

Takes scanned video files, categorizes their subtitle status, and executes
the appropriate action: skip, extract, translate, download, or sync.
All blocking operations use run_in_executor to keep the event loop free.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import library_db
from . import library_scanner
from . import config as subber_config
from .syncer import async_sync_apply, async_sync_preview
from .translator import Translator
from .providers import ProviderRegistry
from .identify import identify as identify_show
from .logsanitize import sanitize_log

logger = logging.getLogger("subber.library")

# Languages we consider "English" for subtitle status
ENGLISH_LANGS = {"en", "eng", "english"}

# ── Shared provider registry (memory-leak fix) ──
# Previously every file built a brand-new provider registry via
# build_provider_registry() — each provider owns an httpx.AsyncClient that
# was never closed, so ~21K files × 5 providers leaked sockets/connection
# pools/SSL contexts and RSS climbed to 2GB until the kernel OOM-killed us.
# Build ONE registry per scan and close it when the scan ends.
_LIBRARY_REGISTRY: ProviderRegistry | None = None
_LIBRARY_REGISTRY_LOCK = asyncio.Lock()

# ffsubsync decodes full audio tracks — running 8 syncs concurrently is the
# main PEAK-memory driver. Cap sync concurrency independently of the overall
# file concurrency so search stays fast but syncs are serialized to 2.
_SYNC_CONCURRENCY = int(os.environ.get("SUBBER_SYNC_CONCURRENCY", "2"))
_SYNC_SEMAPHORE = asyncio.Semaphore(_SYNC_CONCURRENCY)


async def _get_library_registry() -> ProviderRegistry:
    """Get the shared provider registry, building it lazily on first use."""
    global _LIBRARY_REGISTRY
    if _LIBRARY_REGISTRY is None:
        async with _LIBRARY_REGISTRY_LOCK:
            if _LIBRARY_REGISTRY is None:
                _LIBRARY_REGISTRY = await asyncio.to_thread(
                    subber_config.build_provider_registry
                )
    return _LIBRARY_REGISTRY


async def _close_library_registry() -> None:
    """Close all provider HTTP clients and drop the shared registry."""
    global _LIBRARY_REGISTRY
    registry = _LIBRARY_REGISTRY
    _LIBRARY_REGISTRY = None
    if registry is None:
        return
    try:
        await registry.close()
    except Exception as e:
        logger.warning("Error closing provider registry: %s", e)


# Languages we skip translation for (already English)
SKIP_LANGS = ENGLISH_LANGS | {"", "und", "unknown"}


# ── SMB/CIFS Mount Management ──

def _get_mounts() -> list[dict]:
    """Return the configured library mounts from config."""
    lib_cfg = subber_config.get_section("library") or {}
    return lib_cfg.get("mounts", [])


def _mount_shares(mounts: list[dict]) -> dict[str, str]:
    """Mount all enabled SMB shares. Returns {name: error} for failures."""
    errors = {}
    for m in mounts:
        if not m.get("enabled", True):
            continue
        name = m.get("name", "unnamed")
        mp = Path(m["mount_point"])
        mp.mkdir(parents=True, exist_ok=True)
        # Skip if already mounted
        if os.path.ismount(str(mp)):
            continue
        try:
            subprocess.run(
                ["mount", "-t", "cifs",
                 f"//{m['server']}/{m['share']}", str(mp),
                 "-o", f"username={m['username']},password={m['password']},rw,iocharset=utf8,vers=3.0"],
                capture_output=True, text=True, check=True, timeout=30,
            )
        except subprocess.CalledProcessError as e:
            # NEVER include str(e) — it contains the full command with
            # the password in plaintext.  Only return stderr output.
            errors[name] = e.stderr.strip() or f"mount failed (exit {e.returncode})"
        except Exception as e:
            errors[name] = "mount failed"
    return errors


def _unmount_shares(mounts: list[dict]) -> None:
    """Unmount all shares that were mounted by _mount_shares."""
    for m in mounts:
        mp = Path(m["mount_point"])
        if mp.exists() and os.path.ismount(str(mp)):
            subprocess.run(["umount", "-l", str(mp)], capture_output=True, timeout=10)


async def run_scan(
    scan_type: str = "full",
    paths: list[str] | None = None,
    scan_id: int | None = None,
    dry_run: bool = True,
    media_types: list[str] | None = None,
    max_concurrent: int = 5,
    drift_threshold_ms: int = 200,
    skip_walk: bool = False,
) -> int:
    """Run a library scan and process files.

    Args:
        scan_type: 'full', 'incremental', or 'manual'
        paths: Specific paths to scan (None = use config library.paths)
        dry_run: If True, only scan and categorize — no writes, no API calls
        media_types: Filter to ['tv'], ['movie'], or None for both
        max_concurrent: Max parallel file processing
        drift_threshold_ms: Sync only if drift exceeds this
        skip_walk: If True, skip the filesystem walk and load unprocessed files
            from the DB instead (used by resume — the walk already populated
            the table, so re-walking 24K files would waste 20-40 minutes).

    Returns scan_id from the DB.
    """
    if scan_id is None:
        scan_id = library_db.create_scan(scan_type)
    incremental = scan_type == "incremental"

    # Get library paths from config or args
    if not paths:
        lib_config = subber_config.get_section("library")
        paths = lib_config.get("paths", [])

    # Always include mount points as library paths
    mounts = _get_mounts()
    mount_paths = [m["mount_point"] for m in mounts if m.get("enabled", True) and m.get("mount_point")]
    for mp in mount_paths:
        if mp not in paths:
            paths.append(mp)
    if mount_paths:
        print(f"[LIBRARY] Mount paths added: {mount_paths} (total: {len(paths)})", flush=True)

    # Build mount_point → media_type map so directories tagged "TV Shows" or
    # "Movies" get an authoritative type override during classification.
    path_media_types: dict[str, str] = {}
    for m in mounts:
        mp = m.get("mount_point")
        mt = (m.get("media_type") or "").lower()
        if mp and mt in ("tv", "movie"):
            path_media_types[str(mp)] = mt
    if path_media_types:
        print(f"[LIBRARY] Path media-type overrides: {path_media_types}", flush=True)

    if not paths:
        library_db.update_scan(scan_id, status="failed", error_message="No library paths configured")
        return scan_id

    # Get existing hashes for incremental scan
    existing_hashes: set[str] = set()
    if incremental:
        # We'll check by file_hash to skip already-processed files
        existing_hashes = _get_all_hashes()

    # Mount SMB shares before scanning
    mount_errors = _mount_shares(mounts)
    if mount_errors:
        for name, err in mount_errors.items():
            print(f"[LIBRARY] Mount failed for {name}: {err}", flush=True)

        # If every configured, enabled mount failed, abort immediately — there
        # is no point burning through thousands of files that will all fail
        # with "share may be unmounted".  One clear error instead of a storm.
        configured = [m for m in mounts if m.get("enabled", True)]
        if configured:
            any_alive = any(
                m.get("mount_point") and os.path.ismount(m["mount_point"])
                for m in configured
            )
            if not any_alive:
                msg = (
                    "All configured mounts are down — check credentials in "
                    "Settings → Mounts"
                )
                for name, err in sorted(mount_errors.items()):
                    msg += f" | {name}: {err[:100]}"
                library_db.update_scan(scan_id, status="failed", error_message=msg)
                return scan_id

    # Scan filesystem (abort callback lets pause/cancel interrupt the walk)
    loop = asyncio.get_running_loop()
    if skip_walk:
        # Resume path — walk already populated the DB; process remaining files.
        # Reset any stale in_progress files (from a previous scan that died)
        # so they get re-processed instead of staying stuck forever.
        reset = library_db.mark_stale_in_progress()
        if reset:
            print(f"[LIBRARY] Reset {reset} stale in_progress files", flush=True)
        records = library_db.get_unprocessed_files()
        print(f"[LIBRARY] Skipping walk — {len(records)} unprocessed files from DB", flush=True)
    else:
        records = await loop.run_in_executor(
            None,
            lambda: library_scanner.scan_library(
                paths,
                incremental=incremental,
                existing_hashes=existing_hashes,
                should_abort=lambda: _is_cancelled(scan_id) or _is_paused(scan_id),
                path_media_types=path_media_types,
            ),
        )

    # If paused/cancelled during walk, stop here (don't mark completed)
    if _is_paused(scan_id) or _is_cancelled(scan_id):
        print(f"[LIBRARY] Scan {scan_id} stopped during walk (paused={_is_paused(scan_id)}, cancelled={_is_cancelled(scan_id)})", flush=True)
        return scan_id

    # Filter by media type if requested
    if media_types:
        records = [r for r in records if r["media_type"] in media_types]

    # files_processed is CUMULATIVE across resume runs (user preference):
    # it counts every completion attempt, matching how the stats panel counts
    # files. files_total must stay the FULL-library total — on resume (skip_walk)
    # `records` only holds the unprocessed remainder, so len(records) would be
    # wrong (that's what made progress show 326%: 19008/5823). Use the DB count
    # on resume; on a fresh walk, len(records) IS the full total.
    if skip_walk:
        library_db.update_scan(scan_id, files_total=library_db.get_total_file_count(media_types))
    else:
        library_db.update_scan(scan_id, files_total=len(records))

    if dry_run:
        # In dry-run mode, just upsert records with subtitle status but don't process
        for record in records:
            if _is_cancelled(scan_id):
                library_db.update_scan(scan_id, status='cancelled', error_message='Cancelled by user')
                print(f'[LIBRARY] Scan {scan_id} cancelled during dry-run', flush=True)
                return scan_id
            # Detect subtitle status without taking action
            file_path = Path(record["file_path"])
            sub_status, sub_langs = await _detect_subtitle_status(file_path)
            record["subtitle_status"] = sub_status
            record["subtitle_languages"] = json.dumps(sub_langs)
            record["action_taken"] = _planned_action(sub_status)
            record["status"] = "pending"
            library_db.upsert_file(record)

        library_db.update_scan(
            scan_id,
            files_processed=len(records),
            status="completed",
        )
        return scan_id

    # Mount SMB shares before processing
    mount_errors = _mount_shares(mounts)
    if mount_errors:
        for name, err in mount_errors.items():
            print(f"[LIBRARY] Mount failed for {name}: {err}", flush=True)

    mounts = _get_mounts()
    # Mount SMB shares before processing
    # Real processing — upsert all files first, then process concurrently.
    # Single bulk transaction: fast AND doesn't block API calls on the DB lock.
    library_db.bulk_upsert(records)

    semaphore = asyncio.Semaphore(max_concurrent)

    # Process in bounded batches instead of building all ~21K tasks at once.
    # Use running counters instead of accumulating a `results` list for every
    # file — 21K result dicts (plus exception tracebacks that pin large
    # locals) contributed to the memory climb. Close the shared provider
    # registry in finally so its httpx clients are released at scan end.
    processed = 0
    failed = 0
    total_cost = 0.0
    BATCH_SIZE = max(4, max_concurrent * 4)
    logger.info(
        "[scan %s] worker starting: %d files, batch=%d, concurrency=%d",
        scan_id, len(records), BATCH_SIZE, max_concurrent,
    )
    last_heartbeat = time.monotonic()
    try:
        for i in range(0, len(records), BATCH_SIZE):
            if _is_cancelled(scan_id):
                break
            chunk = records[i:i + BATCH_SIZE]
            tasks = [
                _process_file_with_semaphore(semaphore, record, scan_id, drift_threshold_ms)
                for record in chunk
            ]
            chunk_results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in chunk_results:
                if isinstance(r, Exception):
                    failed += 1
                else:
                    processed += 1
                    if isinstance(r, dict):
                        total_cost += float(r.get("cost", 0) or 0.0)
            # Heartbeat: log progress every 60s (or every batch if slow) so a
            # stall is distinguishable from healthy idling — and so we can see
            # WHERE it stopped (last heartbeat = last batch that completed).
            now = time.monotonic()
            if now - last_heartbeat >= 60:
                logger.info(
                    "[scan %s] heartbeat: %d/%d done, %d failed, cost=%.4f",
                    scan_id, processed + failed, len(records), failed, total_cost,
                )
                last_heartbeat = now
    finally:
        await _close_library_registry()

    logger.info(
        "[scan %s] worker finished: %d processed, %d failed, cost=%.4f",
        scan_id, processed, failed, total_cost,
    )
    # NOTE: do NOT set files_processed here. It is maintained CUMULATIVELY by
    # increment_scan_progress() (files_processed = files_processed + 1 per file),
    # so overwriting it with this run's local `processed` would clobber the
    # all-time total on resume (the same class of bug as the files_total 326%).
    library_db.update_scan(
        scan_id,
        files_failed=failed,
        translation_cost=total_cost,
        status="completed",
    )

    return scan_id


async def _process_file_with_semaphore(
    semaphore: asyncio.Semaphore,
    record: dict,
    scan_id: int,
    drift_threshold_ms: int,
) -> dict:
    """Process a single file with concurrency control."""
    async with semaphore:
        try:
            result = await _process_file(record, scan_id, drift_threshold_ms)
            # Increment scan progress after each file completes
            try:
                library_db.increment_scan_progress(scan_id)
            except Exception:
                pass
            return result
        except Exception as e:
            logger.error("Error processing %s: %s", sanitize_log(record["file_path"]), e)
            # Update DB with error
            file_id = library_db.get_file_by_path(record["file_path"])
            if file_id:
                library_db.update_file_status(
                    file_id["id"], status="failed", error_message=str(e)
                )
            # Still increment progress on failure
            try:
                library_db.increment_scan_progress(scan_id)
            except Exception:
                pass
            return {"cost": 0, "error": str(e)}


async def _process_file(record: dict, scan_id: int, drift_threshold_ms: int) -> dict:
    """Process a single video file through the full pipeline."""
    if _is_cancelled(scan_id):
        return {"success": False, "action": "cancelled", "cost": 0, "error": "Scan cancelled"}

    # Wait if scan is paused (polls every 2s until unpaused or cancelled)
    while _is_paused(scan_id):
        await asyncio.sleep(2)
        if _is_cancelled(scan_id):
            return {"success": False, "action": "cancelled", "cost": 0, "error": "Scan cancelled"}

    file_path = Path(record["file_path"])
    if not file_path.exists():
        # Video not visible — almost always a dead CIFS mount after a container
        # restart. Fail fast with a clear message instead of a deep ENOENT later.
        err = f"Video file not accessible (share may be unmounted): {file_path.name}"
        existing = library_db.get_file_by_path(record["file_path"])
        if existing:
            library_db.update_file_status(existing["id"], status="failed", error_message=err)
        return {"success": False, "action": "failed", "cost": 0, "error": err}
    # Records fetched for retry carry their stale error_message — strip it so
    # the upserts below can't resurrect an old error after we clear it.
    record.pop("error_message", None)
    file_id = library_db.upsert_file(record)
    # upsert_file returns lastrowid which is 0 for UPDATEs, so fetch the real ID
    actual_file = library_db.get_file_by_path(record["file_path"])
    if actual_file:
        file_id = actual_file["id"]
        # Skip already-completed files (safe to restart full scans)
        if actual_file.get("status") == "done":
            return {"success": True, "action": "skipped", "cost": 0}

    # Check for existing subtitle output (from a prior scan that was interrupted)
    # Avoids re-extraction/download when the work was already done.
    # Ignore EMPTY files — a killed ffmpeg can leave a 0-byte poison pill that
    # would block retries forever (and shows up blank in Plex).
    existing_subs = [s for s in file_path.parent.glob(file_path.stem + ".en.*")
                     if s.stat().st_size > 0]
    if existing_subs:
        # Valid subtitle on disk — record it properly. Without this, retry rows
        # keep whatever stale status they had (the upsert above can resurrect
        # 'failed' from the fetched record) and the UI shows ❌ with no error.
        library_db.update_file_status(
            file_id, status="done", subtitle_path=str(existing_subs[0]),
            error_message="",
        )
        return {"success": True, "action": "skipped", "cost": 0}

    # Mark as in progress
    # Clear any stale error message from a previous attempt before we start
    # (recovered files were keeping old 429/500 errors even after succeeding).
    library_db.update_file_status(file_id, status="in_progress", error_message="")

    # Detect subtitle status
    sub_status, sub_langs = await _detect_subtitle_status(file_path)

    # Update record with detected status (keep in_progress, don't reset to pending)
    update_record = dict(record)
    update_record["status"] = "in_progress"
    update_record["subtitle_status"] = sub_status
    update_record["subtitle_languages"] = json.dumps(sub_langs)
    library_db.upsert_file(update_record)

    cost = 0.0

    try:
        if sub_status == "external_en":
            # Already has English external subtitle — check sync
            action, drift = await _check_and_sync(file_path, sub_status, drift_threshold_ms)
            ext_en = _find_external_en(file_path)
            library_db.update_file_status(
                file_id, status="done", action_taken=action,
                sync_drift_ms=drift, subtitle_path=str(ext_en) if ext_en else None,
                subtitle_languages=["en"],
            )

        elif sub_status == "embedded_en":
            # Has English embedded track — extract and optionally sync.
            # First verify the track actually contains spoken dialogue: some
            # fansub releases embed a "signs/songs only" track (OP/ED lyrics,
            # on-screen text, spell effects) with zero dialogue. Those would
            # otherwise be extracted and marked done, leaving the user with a
            # subtitle that never matches the audio.
            extract_result = await _extract_embedded_sub(file_path, "en")
            if extract_result:
                sub_path = extract_result[0]
                if not _has_usable_dialogue(sub_path):
                    logger.warning(
                        "[scan %s] embedded_en track has no dialogue (signs/songs-only) for %s — falling back to providers",
                        scan_id, sanitize_log(file_path.name),
                    )
                    # Remove the useless extracted file so it doesn't block future runs.
                    try:
                        sub_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    provider_result = await _search_download_and_process(file_path, record, drift_threshold_ms)
                    if provider_result.get("success"):
                        cost = provider_result.get("cost", 0)
                        library_db.update_file_status(
                            file_id, status="done",
                            action_taken=provider_result.get("action", "downloaded"),
                            subtitle_path=provider_result.get("output_path"),
                            provider_used=provider_result.get("provider"),
                            model_used=provider_result.get("model_used"),
                            sync_drift_ms=provider_result.get("drift_ms"),
                            translation_cost=cost,
                            subtitle_languages=["en"],
                        )
                    else:
                        library_db.update_file_status(
                            file_id, status="failed",
                            error_message="Embedded English track has no dialogue and providers found nothing",
                        )
                else:
                    action, drift = await _check_and_sync(file_path, "embedded_en", drift_threshold_ms, sub_path)
                    library_db.update_file_status(
                        file_id, status="done", action_taken=action,
                        sync_drift_ms=drift, subtitle_path=str(sub_path),
                        provider_used="embedded",
                        subtitle_languages=["en"],
                    )
            else:
                library_db.update_file_status(
                    file_id, status="failed", error_message="Failed to extract embedded subtitle"
                )

        elif sub_status == "external_foreign":
            # Has non-English external subtitle — try providers first, then translate
            print(f"[LIBRARY] external_foreign: trying providers for {file_path.name}", flush=True)
            provider_result = await _search_download_and_process(file_path, record, drift_threshold_ms)
            if provider_result.get("success"):
                cost = provider_result.get("cost", 0)
                library_db.update_file_status(
                    file_id, status="done",
                    action_taken=provider_result.get("action", "downloaded"),
                    subtitle_path=provider_result.get("output_path"),
                    provider_used=provider_result.get("provider"),
                    model_used=provider_result.get("model_used"),
                    sync_drift_ms=provider_result.get("drift_ms"),
                    translation_cost=cost,
                    subtitle_languages=["en"],
                )
            else:
                # Provider search failed — translate existing foreign sub
                foreign_sub = _find_external_foreign(file_path)
                if foreign_sub:
                    result = await _translate_and_sync(file_path, foreign_sub, drift_threshold_ms)
                    cost = result["cost"]
                    library_db.update_file_status(
                        file_id, status="done", action_taken="translated",
                        subtitle_path=result["output_path"],
                        model_used=result["model_used"],
                        sync_drift_ms=result.get("drift_ms"),
                        translation_cost=cost,
                        subtitle_languages=["en"],
                    )
                else:
                    library_db.update_file_status(
                        file_id, status="failed", error_message="No foreign subtitle found to translate"
                    )

        elif sub_status == "embedded_foreign":
            # Has non-English embedded track — try providers first, then extract+translate
            print(f"[LIBRARY] embedded_foreign: trying providers for {file_path.name}", flush=True)
            provider_result = await _search_download_and_process(file_path, record, drift_threshold_ms)
            if provider_result.get("success"):
                cost = provider_result.get("cost", 0)
                library_db.update_file_status(
                    file_id, status="done",
                    action_taken=provider_result.get("action", "downloaded"),
                    subtitle_path=provider_result.get("output_path"),
                    provider_used=provider_result.get("provider"),
                    model_used=provider_result.get("model_used"),
                    sync_drift_ms=provider_result.get("drift_ms"),
                    translation_cost=cost,
                    subtitle_languages=["en"],
                )
            else:
                # Provider search failed — extract embedded foreign and translate
                foreign_lang = sub_langs[0] if sub_langs else "ja"
                print(f"[LIBRARY] No provider match, extracting embedded sub: lang={foreign_lang}", flush=True)
                extract_result = await _extract_embedded_sub(file_path, foreign_lang)
                if extract_result:
                    sub_path = extract_result[0]
                    result = await _translate_and_sync(file_path, sub_path, drift_threshold_ms)
                    cost = result["cost"]
                    print(f"[LIBRARY] Translation done: file_id={file_id} cost={cost} model={result.get('model_used')}", flush=True)
                    library_db.update_file_status(
                        file_id, status="done", action_taken="translated",
                        subtitle_path=result["output_path"],
                        model_used=result["model_used"],
                        provider_used="embedded",
                        sync_drift_ms=result.get("drift_ms"),
                        translation_cost=cost,
                        subtitle_languages=["en"],
                    )
                else:
                    library_db.update_file_status(
                        file_id, status="failed", error_message="Failed to extract embedded subtitle"
                    )

        elif sub_status == "none":
            # No subtitles at all — search providers, then try ASR fallback
            result = await _search_download_and_process(file_path, record, drift_threshold_ms)
            if not result.get("success"):
                asr_result = await _try_asr_fallback(file_path, drift_threshold_ms)
                if asr_result is not None:
                    result = asr_result
            cost = result.get("cost", 0)
            library_db.update_file_status(
                file_id, status="done" if result.get("success") else "failed",
                action_taken=result.get("action", "failed"),
                subtitle_path=result.get("output_path"),
                provider_used=result.get("provider"),
                model_used=result.get("model_used"),
                sync_drift_ms=result.get("drift_ms"),
                translation_cost=cost,
                error_message=result.get("error"),
                subtitle_languages=["en"] if result.get("success") else None,
            )
        else:
            library_db.update_file_status(
                file_id, status="skipped", action_taken="skipped"
            )

    except Exception as e:
        import traceback
        print(f"[LIBRARY] Pipeline error for {file_path}: {e}", flush=True)
        traceback.print_exc()
        logger.error("Pipeline error for %s: %s", sanitize_log(file_path), e)
        library_db.update_file_status(
            file_id, status="failed", error_message=str(e)
        )

    return {"cost": cost}


# ── Subtitle status detection ──

async def _detect_subtitle_status(file_path: Path) -> tuple[str, list[str]]:
    """Detect what subtitles exist for a video file.

    Returns (status, languages) where status is one of:
    - 'none', 'embedded_en', 'embedded_foreign', 'external_en', 'external_foreign', 'multiple'
    """
    loop = asyncio.get_running_loop()

    # Check external subs (fast, filesystem)
    external_subs = library_scanner.find_external_subs(file_path)

    # Check embedded tracks (slow, ffprobe)
    embedded_tracks = await loop.run_in_executor(
        None, lambda: library_scanner.probe_embedded_tracks(str(file_path))
    )

    has_external_en = any(s["language"] in ENGLISH_LANGS for s in external_subs)
    has_external_foreign = any(
        s["language"] not in SKIP_LANGS for s in external_subs
    )
    has_embedded_en = any(t["language"] in ENGLISH_LANGS for t in embedded_tracks)
    # Image-based subs (PGS/VobSub) carry no extractable text — separate them out
    IMAGE_CODECS = ("hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub")
    text_tracks = [t for t in embedded_tracks if t.get("codec", "") not in IMAGE_CODECS]
    eng_text = any(t["language"] in ENGLISH_LANGS for t in text_tracks)
    pgs_only = bool(embedded_tracks) and not text_tracks
    if pgs_only:
        # All embedded tracks are image-based — nothing to extract or translate.
        # Treat as 'none' so the provider download pipeline kicks in.
        print(f"[LIBRARY] Image-only subs (PGS/VobSub), treating as none: {file_path.name}", flush=True)
        has_embedded_en = False
        has_embedded_foreign = False
    else:
        if has_embedded_en and not eng_text:
            # English exists only as an image track — foreign TEXT tracks are usable
            has_embedded_en = False
        # Only text tracks count as translatable foreign subs (image tracks can't be)
        has_embedded_foreign = any(
            t["language"] not in SKIP_LANGS for t in text_tracks
        )

    # Collect all languages
    langs: list[str] = []
    for s in external_subs:
        if s["language"]:
            langs.append(s["language"])
    for t in embedded_tracks:
        if t["language"]:
            langs.append(t["language"])
    langs = list(set(langs))

    # Determine status (priority: external_en > embedded_en > external_foreign > embedded_foreign > none)
    if has_external_en:
        return "external_en", langs
    elif has_embedded_en:
        return "embedded_en", langs
    elif has_external_foreign:
        return "external_foreign", langs
    elif has_embedded_foreign:
        return "embedded_foreign", langs
    elif (external_subs or embedded_tracks) and not pgs_only:
        return "multiple", langs
    else:
        return "none", []


# ── Scan cancellation ──
_cancelled_scans: set[int] = set()

def cancel_scan(scan_id: int) -> bool:
    try:
        scan = library_db.get_scan(scan_id)
        if scan and scan.get("status") == "running":
            library_db.update_scan(scan_id, status="cancelled", error_message="Cancelled by user")
            return True
    except Exception:
        pass
    return False

def _is_cancelled(scan_id: int) -> bool:
    try:
        scan = library_db.get_scan(scan_id)
        return scan is not None and scan.get('status') == 'cancelled'
    except Exception:
        return False


# ── Scan pause / resume ──

_paused_scans: set[int] = set()

def pause_scan(scan_id: int) -> bool:
    """Pause a running scan. Returns True if the scan was paused."""
    try:
        scan = library_db.get_scan(scan_id)
        if scan and scan.get("status") == "running":
            library_db.update_scan(scan_id, status="paused")
            _paused_scans.add(scan_id)
            print(f"[LIBRARY] Scan {scan_id} paused", flush=True)
            return True
    except Exception:
        pass
    return False

def resume_scan(scan_id: int) -> bool:
    """Resume a paused scan. Returns True if the scan was resumed."""
    try:
        scan = library_db.get_scan(scan_id)
        if scan and scan.get("status") == "paused":
            library_db.update_scan(scan_id, status="running")
            _paused_scans.discard(scan_id)
            print(f"[LIBRARY] Scan {scan_id} resumed", flush=True)
            return True
    except Exception:
        pass
    return False

def _is_paused(scan_id: int) -> bool:
    """Check if a scan is paused (DB-backed, survives worker restarts)."""
    try:
        scan = library_db.get_scan(scan_id)
        return scan is not None and scan.get('status') == 'paused'
    except Exception:
        return scan_id in _paused_scans


def _planned_action(status: str) -> str:
    """Return the planned action for a subtitle status (for dry-run mode)."""
    actions = {
        "external_en": "skipped",
        "embedded_en": "skipped",
        "external_foreign": "translated",
        "embedded_foreign": "translated",
        "none": "downloaded",
        "multiple": "skipped",
    }
    return actions.get(status, "skipped")


# ── Extract embedded subtitle ──

async def _extract_embedded_sub(file_path: Path, language: str) -> tuple[Path, str] | None:
    """Extract the best embedded subtitle track using smart track selection.

    Uses EmbeddedProvider.get_embedded_result() which selects the best track
    by language priority (English first, then ja, ko, zh, etc.) — NOT just
    the first track.

    Returns (output_path, language_code) or None.
    """
    from .providers.embedded import EmbeddedProvider

    loop = asyncio.get_running_loop()

    embedded_prov = EmbeddedProvider()
    best_embedded, all_langs = await embedded_prov.get_embedded_result(file_path)

    if not best_embedded:
        return None

    # Build output path: same stem as video, with language code
    # e.g. Show.Name.S01E01.en.mkv -> Show.Name.S01E01.{lang}.srt
    # Keep the original format if it's ass/ssa, otherwise srt
    video_stem = file_path.stem
    # Remove any existing language suffix from the stem (e.g. .en at the end)
    stem_parts = video_stem.rsplit(".", 1)
    if len(stem_parts) == 2 and len(stem_parts[-1]) <= 5 and stem_parts[-1].lower() in {
        "en", "eng", "ja", "jpn", "zh", "chi", "ko", "kor", "de", "ger",
        "fr", "fre", "es", "spa", "it", "ita", "pt", "por", "ru", "rus",
    }:
        base_stem = stem_parts[0]
    else:
        base_stem = video_stem

    # Determine extension from codec
    from .providers.embedded import CODEC_EXTS
    codec = best_embedded.metadata.get("codec", "subrip")
    ext = CODEC_EXTS.get(codec, "srt")
    if ext not in ("srt", "ass", "ssa", "vtt"):
        ext = "srt"  # Convert non-text formats to srt

    out_path = file_path.parent / f"{base_stem}.{best_embedded.language}.{ext}"

    # Skip extraction if subtitle file already exists (from a previous run)
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path, best_embedded.language

    # Use EmbeddedProvider.download which uses the correct track index
    await embedded_prov.download(best_embedded, out_path)

    return out_path, best_embedded.language


# ── Sync check ──

def _replace_with_synced(sub_path: Path, synced_path: Path) -> Path:
    """Replace the pre-sync subtitle in-place with its synced version.

    Media servers (Plex/Jellyfin/Plexy) treat '<name>.en.srt' and
    '<name>.en.synced.srt' as the SAME English track, so keeping both
    leaves the user unable to tell which is which. The synced file is the
    final product: atomically overwrite the original and drop the
    intermediate. Falls back to keeping both files (old behavior) if the
    replace fails, so we never lose the synced result.
    """
    try:
        os.replace(synced_path, sub_path)
        return sub_path
    except OSError as e:
        logger.warning(
            "Could not replace %s with synced version (%s) — keeping .synced file",
            sanitize_log(sub_path), e,
        )
        return synced_path


async def _check_and_sync(
    video_path: Path,
    status: str,
    drift_threshold_ms: int,
    sub_path: Path | None = None,
) -> tuple[str, int | None]:
    """Check if a subtitle needs syncing and apply if drift exceeds threshold.

    Returns (action_taken, drift_ms).
    """
    if sub_path is None:
        sub_path = _find_external_en(video_path)

    if sub_path is None:
        return "skipped", None

    # Run ffsubsync preview to get drift
    from .syncer import async_sync_preview

    try:
        async with _SYNC_SEMAPHORE:
            preview = await async_sync_preview(video_path, sub_path)
            drift_ms = int(abs(preview.offset_seconds) * 1000)

            if drift_ms > drift_threshold_ms:
                # Apply sync and replace the original — never leave a parallel
                # .synced.srt (media servers can't tell the two apart).
                tmp_path = sub_path.with_suffix(".synced.srt")
                await async_sync_apply(video_path, sub_path, tmp_path)
                _replace_with_synced(sub_path, tmp_path)
                return "synced", drift_ms
            else:
                return "skipped", drift_ms
    except Exception as e:
        logger.warning("Sync check failed for %s: %s", sanitize_log(video_path), e)
        return "skipped", None


# ── Translation ──

async def _translate_and_sync(
    video_path: Path,
    sub_path: Path,
    drift_threshold_ms: int,
    source_lang: str | None = None,
) -> dict:
    """Translate a foreign subtitle to English and optionally sync.

    Returns {"output_path": str, "model_used": str, "cost": float, "drift_ms": int?}
    """
    loop = asyncio.get_running_loop()

    # Detect source language from filename or content, unless the caller already
    # knows it (the ASR path passes Whisper's detected language explicitly, since
    # the raw transcript carries a neutral filename that would otherwise read as
    # English via the ".en" suffix).
    if source_lang is None:
        source_lang = _detect_sub_language(sub_path)

    # Output path: same directory as the video, matching the episode name
    # e.g. Show.Name.S01E01.{source_lang}.srt -> Show.Name.S01E01.en.srt
    video_stem = video_path.stem
    # Remove any existing language suffix from the video stem
    stem_parts = video_stem.rsplit(".", 1)
    if len(stem_parts) == 2 and len(stem_parts[-1]) <= 5:
        base_stem = stem_parts[0]
    else:
        base_stem = video_stem
    # Keep the same format as input, or default to srt
    if sub_path.suffix in [".ass", ".ssa"]:
        output_path = video_path.parent / f"{base_stem}.en.ass"
    else:
        output_path = video_path.parent / f"{base_stem}.en.srt"

    # Get translator config
    backends = subber_config.translation_backends()
    if not backends:
        backends = [{
            "name": "default",
            "api_base": "http://localhost:11434/v1",
            "api_key": "ollama",
            "model": "translategemma:4b",
        }]

    ts = subber_config.translation_settings()
    chunk_size = ts.get("chunk_size", 20)
    max_retries = ts.get("max_retries", 3)

    # Try backends in priority order
    last_error = None
    for backend in backends:
        try:
            translator = Translator(
                api_base=backend["api_base"],
                api_key=backend.get("api_key", ""),
                model=backend["model"],
                chunk_size=chunk_size,
                max_retries=max_retries,
            )

            def _do_translate():
                translator.translate(
                    input_path=sub_path,
                    output_path=output_path,
                    source_lang=source_lang,
                    target_lang="en",
                )
                # Add attribution header
                _add_attribution(output_path, backend["model"])
                return backend["model"]

            model_used = await loop.run_in_executor(None, _do_translate)

            # Sync after translation
            drift_ms = None
            try:
                async with _SYNC_SEMAPHORE:
                    preview = await async_sync_preview(video_path, output_path)
                    drift_ms = int(abs(preview.offset_seconds) * 1000)
                    if drift_ms > drift_threshold_ms:
                        tmp_path = output_path.with_suffix(".synced.srt")
                        await async_sync_apply(video_path, output_path, tmp_path)
                        output_path = _replace_with_synced(output_path, tmp_path)
            except Exception as e:
                print(f"[LIBRARY] Sync failed: {e}", flush=True)

            return {
                "output_path": str(output_path),
                "model_used": model_used,
                "cost": _estimate_cost(sub_path, backend["model"]),
                "drift_ms": drift_ms,
            }

        except Exception as e:
            last_error = e
            logger.warning("Translation backend %s failed: %s", backend.get("name"), e)
            continue

    raise RuntimeError(f"All translation backends failed: {last_error}")


def _has_usable_dialogue(sub_path: Path) -> bool:
    """Return True if a subtitle file contains actual spoken dialogue.

    Some fansub releases embed a "signs/songs only" track — it translates
    on-screen text (signs, game UI, OP/ED lyrics, spell effects) but has ZERO
    spoken dialogue. ffprobe still reports it as an English stream, so Subber
    would extract it and mark the file done with no dialogue at all.

    Detection keys on the ABSENCE of dialogue, never the PRESENCE of positional
    text (legit full subs frequently include `\\pos` sign translations, which
    must NOT trigger a false positive).

    For .ass: count events per style. If dialogue-capable styles (main, italics,
    top, Default, dialogue, etc.) carry zero events while sign/song/effect styles
    carry everything, it's a signs/songs-only track → return False.
    For .srt/.vtt: no style info, so we can't distinguish — return True
    (don't flag; the known failure mode is .ass).

    Returns True unless we're confident there's no dialogue.
    """
    try:
        text = sub_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True  # unreadable → don't flag

    suffix = sub_path.suffix.lower()
    if suffix not in (".ass", ".ssa"):
        return True  # .srt/.vtt have no style info → assume dialogue

    # Styles whose exact (lowercased) name indicates spoken dialogue.
    dialogue_styles = {
        "main", "italics", "ital", "top", "default", "dialogue", "dial",
        "sub", "subtitle", "subtitles", "normal", "text", "speech", "talk",
        "caption", "dialogue2", "default2",
    }
    # Distinctive, specific substrings that mark a NON-dialogue style.
    # (Deliberately NOT "op"/"ed" here — those are too short and appear inside
    #  legitimate style names like "top".)
    non_dialogue_markers = (
        "sign", "song", "romaji", "karaoke", "title", "effect", "menu",
        "game", "logo", "credit", "magic", "insert", "lyric", "banner",
        "opening", "ending",
    )

    def _style_is_dialogue(style: str) -> bool:
        s = style.lower().strip()
        if s in dialogue_styles:
            return True
        for m in non_dialogue_markers:
            if m in s:
                return False
        # "op"/"ed" only as a leading word (opening/ending theme lyrics).
        for token in s.replace("_", " ").replace("-", " ").split():
            if token in ("op", "ed"):
                return False
        # Unknown style name → assume dialogue (avoid false positives).
        return True

    dialogue_events = 0
    non_dialogue_events = 0
    for line in text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        # Dialogue: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
        parts = line.split(",", 9)
        if len(parts) < 4:
            continue
        style = parts[3].strip()
        if not style:
            continue
        if _style_is_dialogue(style):
            dialogue_events += 1
        else:
            non_dialogue_events += 1

    # No events at all → unusable (empty file)
    if dialogue_events == 0 and non_dialogue_events == 0:
        return False
    # Signs/songs-only: zero dialogue-style events but non-zero sign/song events.
    if dialogue_events == 0 and non_dialogue_events > 0:
        return False
    return True


def _detect_sub_language(sub_path: Path) -> str:
    """Detect a subtitle's language — see parser.detect_subtitle_language."""
    from .parser import detect_subtitle_language
    return detect_subtitle_language(sub_path)


async def _confirm_language(sub_path: Path) -> str:
    """Confirm a subtitle's language via the LLM when filename + langdetect are
    inconclusive. Samples a few dialogue lines and asks the translation backend
    to identify the language. Returns an ISO 639-1 code or 'unknown'."""
    from .parser import read_raw_texts
    lines = [ln.strip() for ln in read_raw_texts(sub_path) if ln and ln.strip()]
    if not lines:
        return "unknown"
    sample = "\n".join(lines[:25])[:1500]

    backends = subber_config.translation_backends()
    if not backends:
        return "unknown"

    loop = asyncio.get_running_loop()
    for backend in backends:
        try:
            translator = Translator(
                api_base=backend["api_base"],
                api_key=backend.get("api_key", ""),
                model=backend["model"],
                max_tokens=32,
                max_retries=1,
                timeout=60.0,
            )
            raw = await loop.run_in_executor(None, translator.identify_language, sample)
            code = (raw or "").strip().lower()
            if len(code) == 2 and code.isalpha():
                return code
        except Exception as e:
            logger.warning(
                "Language confirmation backend %s failed: %s",
                backend.get("name"), e,
            )
            continue
    return "unknown"


async def _asr_transcribe(file_path: Path, drift_threshold_ms: int) -> dict:
    """Transcribe a video's audio via ASR (library fallback). Assumes the caller
    already confirmed ASR is enabled + configured. Returns a result dict like
    _search_download_and_process."""
    from .transcriber import transcribe_video
    asr_cfg = subber_config.asr_settings()
    # Write the raw transcript to a NEUTRAL name first. We don't know the audio's
    # language until Whisper reports it, and a premature ".en.srt" name would make
    # _translate_and_sync's filename-based language detection read the transcript
    # as English — so foreign audio (Japanese/Korean/…) would never get translated.
    raw_srt = file_path.with_suffix(".raw.srt")

    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, transcribe_video, file_path, raw_srt, asr_cfg)

    # Optional ad/credit removal on the transcript
    try:
        ad_cfg = subber_config.ad_removal_settings()
        if ad_cfg.get("mode", "off") != "off":
            from .ad_removal import remove_ads
            remove_ads(
                raw_srt,
                mode=ad_cfg.get("mode", "adverts"),
                window_seconds=int(ad_cfg.get("window_seconds", 60) or 60),
                extra_patterns=ad_cfg.get("patterns") or [],
            )
    except Exception:
        pass

    detected = (res.get("language") or "").lower()
    model_used = res.get("model") or asr_cfg.get("model", "large-v3-turbo")
    final_srt = file_path.with_suffix(".en.srt")

    # Whisper couldn't identify the language → don't silently assume English;
    # confirm from the transcript content before deciding.
    if not detected or detected in ("unknown", "und", "auto"):
        try:
            detected = await _confirm_language(raw_srt)
        except Exception:
            detected = ""

    # Non-English transcript → translate to English (passing Whisper's detected
    # language explicitly so the neutral filename can't short-circuit it).
    if detected and detected not in ("en", "eng", "english"):
        try:
            trans = await _translate_and_sync(
                file_path, raw_srt, drift_threshold_ms, source_lang=detected,
            )
            try:
                raw_srt.unlink(missing_ok=True)
            except OSError:
                pass
            return {
                "success": True,
                "action": "transcribed_and_translated",
                "output_path": trans["output_path"],
                "provider": "asr",
                "model_used": trans["model_used"],
                "cost": trans["cost"],
                "drift_ms": trans.get("drift_ms"),
            }
        except Exception as e:
            logger.warning(
                "[LIBRARY] ASR translation failed for %s, keeping raw transcript: %s",
                sanitize_log(file_path.name), e,
            )
            # Keep the raw transcript, labelled with its detected language so it
            # is never mistaken for English on a later scan.
            try:
                final_srt = file_path.with_suffix(f".{detected or 'raw'}.srt")
                raw_srt.replace(final_srt)
            except OSError:
                final_srt = raw_srt
            return {
                "success": True,
                "action": "transcribed",
                "output_path": str(final_srt),
                "provider": "asr",
                "model_used": model_used,
                "cost": 0.0,
                "drift_ms": None,
            }

    # English transcript → move into place as the English subtitle.
    try:
        raw_srt.replace(final_srt)
    except OSError:
        final_srt = raw_srt
    return {
        "success": True,
        "action": "transcribed",
        "output_path": str(final_srt),
        "provider": "asr",
        "model_used": model_used,
        "cost": 0.0,
        "drift_ms": None,
    }


async def _try_asr_fallback(file_path: Path, drift_threshold_ms: int) -> dict | None:
    """Transcribe audio via ASR when no subtitle exists. Returns a result dict,
    or None when ASR is disabled (library.asr_fallback off) or unconfigured."""
    lib_cfg = subber_config.get_section("library") or {}
    if not lib_cfg.get("asr_fallback", False):
        return None
    asr_cfg = subber_config.asr_settings()
    if asr_cfg.get("mode", "off") != "auto" or not asr_cfg.get("backends"):
        return None
    try:
        return await _asr_transcribe(file_path, drift_threshold_ms)
    except Exception as e:
        logger.warning(
            "[LIBRARY] ASR fallback failed for %s: %s",
            sanitize_log(file_path.name), e,
        )
        return {"success": False, "action": "failed", "error": f"ASR failed: {e}"}


def _estimate_cost(sub_path: Path, model: str) -> float:
    """Estimate translation cost from configurable pricing.

    Reads from config section 'cost':
      - input_cost_per_million / output_cost_per_million
      - chars_per_token
      - peak_enabled, peak_multiplier, peak_hours_utc, timezone
    """
    try:
        cost_cfg = subber_config.get_section("cost") or {}
        chars_per_token = float(cost_cfg.get("chars_per_token", 2.5))
        input_rate = float(cost_cfg.get("input_cost_per_million", 0.14))
        output_rate = float(cost_cfg.get("output_cost_per_million", 0.28))

        # Optional per-model overrides: cost.models.<model>.{input,output}_cost_per_million
        model_ovr = (cost_cfg.get("models") or {}).get(model) or {}
        if model_ovr:
            input_rate = float(model_ovr.get("input_cost_per_million", input_rate))
            output_rate = float(model_ovr.get("output_cost_per_million", output_rate))
        
        # Peak pricing check
        total_rate = input_rate + output_rate
        if cost_cfg.get("peak_enabled", False):
            from datetime import datetime, timezone as tz
            multiplier = float(cost_cfg.get("peak_multiplier", 2.0))
            peak_hours = cost_cfg.get("peak_hours_utc", [[1,4],[6,10]])
            tz_name = cost_cfg.get("timezone", "UTC")
            try:
                import zoneinfo
                user_tz = zoneinfo.ZoneInfo(tz_name)
            except Exception:
                user_tz = tz.utc
            now = datetime.now(user_tz)
            hour = now.hour
            for start, end in peak_hours:
                if start <= hour < end:
                    total_rate *= multiplier
                    break
        
        size = sub_path.stat().st_size
        estimated_tokens = size / chars_per_token
        return round(estimated_tokens * total_rate / 1_000_000, 4)
    except OSError:
        return 0.0


def _add_attribution(sub_path: Path, model: str) -> None:
    """Add model attribution header to subtitle file."""
    try:
        with open(sub_path, "r", encoding="utf-8-sig") as f:
            content = f.read()
        header = f"; Subber Library — translated by {model}\n; https://github.com/completeBeta/Subber\n"
        if not content.startswith(";"):
            content = header + content
            # allow_overwrite: we created this file earlier in the pipeline
            safe_write_subtitle(Path(sub_path), content, allow_overwrite=True)
    except Exception:
        pass


# ── Provider search and download ──

async def _search_download_and_process(
    video_path: Path,
    record: dict,
    drift_threshold_ms: int,
) -> dict:
    """Search providers for subtitles, download, translate if needed, sync.

    Returns dict with success, action, output_path, provider, model_used, cost, drift_ms, error.
    """
    loop = asyncio.get_running_loop()

    # Use the shared provider registry (one per scan). Building a fresh
    # registry per file leaked httpx clients and caused the 2GB OOM kills.
    try:
        registry = await _get_library_registry()
    except Exception as e:
        return {"success": False, "error": f"Failed to build providers: {e}"}

    # Determine search params based on media type
    if record["media_type"] == "tv":
        query = record.get("show_title") or video_path.stem
        season = record.get("season")
        episode = record.get("episode")
    else:
        query = record.get("movie_title") or video_path.stem
        season = None
        episode = None

    # Show identification — resolve canonical title via AniList/TMDB
    search_queries = [query]  # fallback: use original query
    if subber_config.library_settings().get("use_identification", True):
        try:
            tmdb_key = subber_config.library_settings().get("tmdb_api_key", "")
            prefer = subber_config.library_settings().get("identify_prefer", "anilist")
            identity = await identify_show(query, tmdb_api_key=tmdb_key, prefer=prefer)
            if identity.anilist_title_en:
                search_queries.insert(0, identity.anilist_title_en)
            for term in identity.search_terms:
                if term not in search_queries:
                    search_queries.append(term)
            logger.info("Identified '%s' → %s (anilist=%s)", sanitize_log(query), identity.best_title, identity.anilist_id)
        except Exception as e:
            logger.warning("Show identification failed for '%s': %s", sanitize_log(query), e)

    # Search all providers with timeout, trying each query candidate
    results = []
    for q in search_queries:
        if results:
            break
        try:
            results = await asyncio.wait_for(
                registry.search_all(
                    query=q,
                    language="en",
                    season=season,
                    episode=episode,
                    video_path=video_path,
                ),
                timeout=60,
            )
        except asyncio.TimeoutError:
            continue
        except Exception:
            continue

    # Fallback: retry without season/episode (some shows have different numbering)
    if not results and season is not None:
        for q in search_queries:
            if results:
                break
            try:
                results = await asyncio.wait_for(
                    registry.search_all(query=q, language="en", video_path=video_path),
                    timeout=30,
                )
            except asyncio.TimeoutError:
                continue
            except Exception:
                continue

    if not results:
        return {"success": False, "error": "No subtitles found from any provider"}

    # Download best match to temp dir (avoids overwrite conflicts when
    # multiple episodes share the same provider result filename).
    # Try each result in order: providers can return results with stale or
    # invalid file_ids (e.g. OpenSubtitles "Invalid file_id"), so fall through
    # to the next candidate rather than failing on the first.
    #
    # Episode guard: the no-S/E fallback search can return packs for OTHER
    # episodes (matched S03E01 for an S03E11 request once). Reject candidates
    # whose filename clearly identifies a different episode.
    if season is not None and episode is not None and results:
        before = len(results)
        guarded = [r for r in results
                   if _candidate_matches_episode(r.filename or "", season, episode)]
        dropped = before - len(guarded)
        if dropped:
            print(f"[LIBRARY] Episode guard: dropped {dropped} candidate(s) for "
                  f"wrong episode (wanted S{season:02d}E{episode:02d})", flush=True)
        if not guarded:
            return {"success": False,
                    "error": f"No subtitles found for S{season:02d}E{episode:02d} "
                             f"({before} candidate(s) were for other episodes)"}
        results = guarded

    import uuid, tempfile
    best = None
    downloaded_path = None
    last_err = None
    for candidate in results[:5]:
        tmp_dir = Path(tempfile.mkdtemp(prefix="subber_dl_"))
        try:
            downloaded_path = await asyncio.wait_for(
                registry.download(candidate, tmp_dir),
                timeout=30,
            )
            best = candidate
            break
        except asyncio.TimeoutError:
            last_err = "Download timed out"
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception as e:
            last_err = str(e)
            shutil.rmtree(tmp_dir, ignore_errors=True)

    if best is None or downloaded_path is None:
        return {"success": False, "error": f"Download failed: {last_err}"}

    # Move to target dir with unique name (retried — CIFS shares flap briefly
    # under load and a single ENOENT should not kill a download we paid for).
    tmp_name = f".subber_{uuid.uuid4().hex[:8]}_{best.filename}"
    tmp_dest = video_path.parent / tmp_name
    await _robust_move(downloaded_path, tmp_dest)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    downloaded_path = tmp_dest

    downloaded_path = Path(downloaded_path)
    provider_name = best.provider if hasattr(best, "provider") else "unknown"

    # Normalize the downloaded file: some providers serve zip/gzip archives
    # (multi-episode packs). Unpack them and pick the member that matches the
    # target episode — grabbing the first member can yield the WRONG episode.
    try:
        downloaded_path = _unpack_archive_subtitle(
            downloaded_path, season, episode,
        )
    except ValueError as e:
        downloaded_path.unlink(missing_ok=True)
        return {"success": False, "error": str(e)}

    # Ad/credit removal (opt-in) — strip advert & fansub-credit lines from the
    # intro/outro before language detection / translation.
    try:
        ad_cfg = subber_config.ad_removal_settings()
        if ad_cfg.get("mode", "off") != "off":
            from .ad_removal import remove_ads
            res = remove_ads(
                downloaded_path,
                mode=ad_cfg.get("mode", "adverts"),
                window_seconds=int(ad_cfg.get("window_seconds", 60) or 60),
                extra_patterns=ad_cfg.get("patterns") or [],
            )
            if res["removed"]:
                logger.info("Ad removal: stripped %d line(s) from %s", res["removed"], sanitize_log(downloaded_path.name))
    except Exception as e:
        logger.warning("Ad removal failed for %s: %s", sanitize_log(downloaded_path.name), e)

    # Decide whether the downloaded sub needs translation. Confirm the language
    # rather than assuming it: filename marker first, then langdetect, then the
    # LLM. Translate only when a non-English language is positively identified.
    sub_lang = _detect_sub_language(downloaded_path)
    if sub_lang == "unknown":
        sub_lang = await _confirm_language(downloaded_path)
    if sub_lang == "unknown":
        logger.warning(
            "Language unconfirmed for %s (filename + langdetect + LLM all "
            "inconclusive) — saving as-is without translation",
            sanitize_log(downloaded_path.name),
        )
    if sub_lang in ENGLISH_LANGS or sub_lang == "unknown":
        # English (or truly unconfirmable) — sync and write as-is
        output_path = video_path.with_suffix(".en.srt")
        shutil.copy2(downloaded_path, output_path)

        drift_ms = None
        try:
            async with _SYNC_SEMAPHORE:
                preview = await async_sync_preview(video_path, output_path)
                drift_ms = int(abs(preview.offset_seconds) * 1000)
                if drift_ms > drift_threshold_ms:
                    tmp_path = output_path.with_suffix(".synced.srt")
                    await async_sync_apply(video_path, output_path, tmp_path)
                    output_path = _replace_with_synced(output_path, tmp_path)
        except Exception:
            pass

        # Clean up downloaded file if different from output
        if downloaded_path != output_path:
            try:
                downloaded_path.unlink(missing_ok=True)
            except Exception:
                pass

        return {
            "success": True,
            "action": "downloaded",
            "output_path": str(output_path),
            "provider": provider_name,
            "drift_ms": drift_ms,
        }
    else:
        # Non-English sub — translate then sync
        result = await _translate_and_sync(video_path, downloaded_path, drift_threshold_ms)
        # Clean up downloaded file
        try:
            downloaded_path.unlink(missing_ok=True)
        except Exception:
            pass
        return {
            "success": True,
            "action": "downloaded_and_translated",
            "output_path": result["output_path"],
            "provider": provider_name,
            "model_used": result["model_used"],
            "cost": result["cost"],
            "drift_ms": result.get("drift_ms"),
        }


# ── Helper functions ──

# Episode markers like S03E11, S3E5, S03 E11 in provider filenames
_SE_MARKER_RE = re.compile(r"S(\d{1,2})[ ._-]*E(\d{1,3})", re.IGNORECASE)


def _candidate_matches_episode(filename: str, season, episode) -> bool:
    """Return False only when the filename clearly identifies a DIFFERENT episode.

    Conservative on purpose: filenames without episode markers pass through
    (many providers use opaque names); only explicit SxxEyy markers that all
    disagree with the target cause rejection.
    """
    if season is None or episode is None:
        return True
    markers = _SE_MARKER_RE.findall(filename)
    if not markers:
        return True
    for s, e in markers:
        if int(s) == int(season) and int(e) == int(episode):
            return True
    return False


async def _robust_move(src: Path, dst: Path, attempts: int = 3) -> None:
    """Move a file onto the share with retries.

    CIFS mounts flap momentarily under concurrent load; shutil.move raises
    FileNotFoundError on the destination when that happens. Back off and retry
    rather than losing an already-downloaded subtitle.
    """
    loop = asyncio.get_running_loop()
    last_err = None
    for attempt in range(attempts):
        try:
            await loop.run_in_executor(None, lambda: shutil.move(str(src), str(dst)))
            return
        except OSError as e:
            last_err = e
            if attempt < attempts - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
    raise last_err


_SUBTITLE_SUFFIXES = (".srt", ".ass", ".ssa", ".sub", ".vtt")


def _unpack_archive_subtitle(path: Path, season=None, episode=None) -> Path:
    """Ensure the downloaded file is a usable subtitle, unpacking archives.

    Providers (SubDL, OpenSubtitles) sometimes serve zip/gzip archives —
    including multi-episode season packs. Detect archives by MAGIC BYTES
    (extensions lie: a zip can arrive named `...zip.srt`), unpack, and pick the
    member for the target episode.

    Raises ValueError when the archive holds no usable subtitle for the target
    episode (better to fail cleanly than write the WRONG episode's text).
    """
    import gzip
    import io
    import zipfile

    head = path.read_bytes()[:4]

    # Plain subtitle — nothing to unpack
    if head[:2] != b"PK" and head[:2] != b"\x1f\x8b":
        return path

    if head[:2] == b"\x1f\x8b":  # gzip: single file inside
        raw = gzip.decompress(path.read_bytes())
        name = path.name
        if name.lower().endswith(".gz"):
            out_name = name[:-3]
        else:
            out_name = path.stem
        if not out_name.lower().endswith(_SUBTITLE_SUFFIXES):
            out_name += ".srt"
        out = path.parent / out_name
        out.write_bytes(raw)
        if out != path:
            path.unlink(missing_ok=True)
        return out

    # ZIP archive — collect subtitle members
    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as zf:
        members = [n for n in zf.namelist()
                   if not n.endswith("/")
                   and n.lower().endswith(_SUBTITLE_SUFFIXES)]
        if not members:
            raise ValueError(
                f"Archive contained no subtitle files ({len(zf.namelist())} entries)"
            )

        # Prefer the member that matches the target episode
        chosen = None
        if season is not None and episode is not None:
            for name in members:
                base = Path(name).name
                if _candidate_matches_episode(base, season, episode):
                    chosen = name
                    break
        if chosen is None and len(members) == 1:
            chosen = members[0]
        if chosen is None and season is not None and episode is not None:
            raise ValueError(
                f"Archive holds {len(members)} subtitles but none matches "
                f"S{season:02d}E{episode:02d}"
            )
        if chosen is None:
            # No episode target (movies) — keep historic behavior, take first
            chosen = members[0]

        content = zf.read(chosen)
        ext = Path(chosen).suffix.lower() or ".srt"

    # Write the chosen subtitle next to the archive, then remove the archive.
    # Guard: archives often have LYING extensions (pack.zip.srt) where
    # with_suffix returns the same path — never unlink what we just wrote.
    out = path.with_suffix(ext)
    out.write_bytes(content)
    if out != path:
        path.unlink(missing_ok=True)
    return out


def _find_external_en(video_path: Path) -> Path | None:
    """Find an English external subtitle next to a video."""
    subs = library_scanner.find_external_subs(video_path)
    for s in subs:
        if s["language"] in ENGLISH_LANGS:
            return s["path"]
    return None


def _find_external_foreign(video_path: Path) -> Path | None:
    """Find a non-English external subtitle next to a video."""
    subs = library_scanner.find_external_subs(video_path)
    for s in subs:
        if s["language"] not in SKIP_LANGS:
            return s["path"]
    # Fallback: return any external sub
    for s in subs:
        return s["path"]
    return None


def _get_all_hashes() -> set[str]:
    """Get all file hashes from the DB for incremental scan dedup."""
    # This is a simple implementation — could be optimized
    with library_db._connect() as conn:
        rows = conn.execute(
            "SELECT file_hash FROM library_files WHERE file_hash IS NOT NULL AND file_hash != ''"
        ).fetchall()
        return {r["file_hash"] for r in rows}
