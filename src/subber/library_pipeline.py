"""Library pipeline orchestrator.

Takes scanned video files, categorizes their subtitle status, and executes
the appropriate action: skip, extract, translate, download, or sync.
All blocking operations use run_in_executor to keep the event loop free.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
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
                capture_output=True, text=True, check=True, timeout=15,
            )
        except subprocess.CalledProcessError as e:
            errors[name] = e.stderr.strip() or str(e)
        except Exception as e:
            errors[name] = str(e)
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
    max_concurrent: int = 2,
    drift_threshold_ms: int = 200,
) -> int:
    """Run a library scan and process files.

    Args:
        scan_type: 'full', 'incremental', or 'manual'
        paths: Specific paths to scan (None = use config library.paths)
        dry_run: If True, only scan and categorize — no writes, no API calls
        media_types: Filter to ['tv'], ['movie'], or None for both
        max_concurrent: Max parallel file processing
        drift_threshold_ms: Sync only if drift exceeds this

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

    # Scan filesystem
    loop = asyncio.get_running_loop()
    records = await loop.run_in_executor(
        None,
        lambda: library_scanner.scan_library(paths, incremental=incremental, existing_hashes=existing_hashes),
    )

    # Filter by media type if requested
    if media_types:
        records = [r for r in records if r["media_type"] in media_types]

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
    # Real processing — upsert all files first, then process concurrently
    for record in records:
        library_db.upsert_file(record)

    semaphore = asyncio.Semaphore(max_concurrent)
    tasks = [
        _process_file_with_semaphore(semaphore, record, scan_id, drift_threshold_ms)
        for record in records
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    processed = sum(1 for r in results if not isinstance(r, Exception))
    failed = sum(1 for r in results if isinstance(r, Exception))
    total_cost = sum(
        r.get("cost", 0) for r in results
        if isinstance(r, dict)
    )

    library_db.update_scan(
        scan_id,
        files_processed=processed,
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

    file_path = Path(record["file_path"])
    file_id = library_db.upsert_file(record)
    # upsert_file returns lastrowid which is 0 for UPDATEs, so fetch the real ID
    actual_file = library_db.get_file_by_path(record["file_path"])
    if actual_file:
        file_id = actual_file["id"]

    # Mark as in progress
    library_db.update_file_status(file_id, status="in_progress")

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
            )

        elif sub_status == "embedded_en":
            # Has English embedded track — extract and optionally sync
            extract_result = await _extract_embedded_sub(file_path, "en")
            if extract_result:
                sub_path = extract_result[0]
                action, drift = await _check_and_sync(file_path, "embedded_en", drift_threshold_ms, sub_path)
                library_db.update_file_status(
                    file_id, status="done", action_taken=action,
                    sync_drift_ms=drift, subtitle_path=str(sub_path),
                    provider_used="embedded",
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
                    sync_drift_ms=provider_result.get("drift_ms"),
                    translation_cost=cost,
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
                    sync_drift_ms=provider_result.get("drift_ms"),
                    translation_cost=cost,
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
                    )
                else:
                    library_db.update_file_status(
                        file_id, status="failed", error_message="Failed to extract embedded subtitle"
                    )

        elif sub_status == "none":
            # No subtitles at all — search providers
            result = await _search_download_and_process(file_path, record, drift_threshold_ms)
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

    # Use EmbeddedProvider.download which uses the correct track index
    await embedded_prov.download(best_embedded, out_path)

    return out_path, best_embedded.language


# ── Sync check ──

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
        preview = await async_sync_preview(video_path, sub_path)
        drift_ms = int(abs(preview.offset_seconds) * 1000)

        if drift_ms > drift_threshold_ms:
            # Apply sync
            output_path = sub_path.with_suffix(".synced.srt")
            await async_sync_apply(video_path, sub_path, output_path)
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
) -> dict:
    """Translate a foreign subtitle to English and optionally sync.

    Returns {"output_path": str, "model_used": str, "cost": float, "drift_ms": int?}
    """
    loop = asyncio.get_running_loop()

    # Detect source language from filename or content
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
                preview = await async_sync_preview(video_path, output_path)
                drift_ms = int(abs(preview.offset_seconds) * 1000)
                if drift_ms > drift_threshold_ms:
                    synced_path = output_path.with_suffix(".synced.srt")
                    await async_sync_apply(video_path, output_path, synced_path)
                    output_path = synced_path
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


def _detect_sub_language(sub_path: Path) -> str:
    """Detect the language of a subtitle file from filename or content."""
    # Check filename for language code
    stem = sub_path.stem
    parts = stem.split(".")
    if len(parts) >= 2:
        potential_lang = parts[-1].lower()
        if len(potential_lang) <= 5 and potential_lang not in {"en", "eng"}:
            return potential_lang

    # Fallback: use langdetect on content
    try:
        from .parser import detect_language
        lang = detect_language(sub_path)
        return lang or "ja"
    except Exception:
        return "ja"


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

    # Build provider registry
    try:
        registry = await loop.run_in_executor(None, subber_config.build_provider_registry)
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
    # multiple episodes share the same provider result filename)
    best = results[0]
    try:
        import uuid, tempfile
        tmp_dir = Path(tempfile.mkdtemp(prefix="subber_dl_"))
        downloaded_path = await asyncio.wait_for(
            registry.download(best, tmp_dir),
            timeout=30,
        )
        # Move to target dir with unique name
        tmp_name = f".subber_{uuid.uuid4().hex[:8]}_{best.filename}"
        tmp_dest = video_path.parent / tmp_name
        shutil.move(str(downloaded_path), str(tmp_dest))
        shutil.rmtree(tmp_dir, ignore_errors=True)
        downloaded_path = tmp_dest
    except asyncio.TimeoutError:
        return {"success": False, "error": "Download timed out"}
    except Exception as e:
        return {"success": False, "error": f"Download failed: {e}"}

    downloaded_path = Path(downloaded_path)
    provider_name = best.provider if hasattr(best, "provider") else "unknown"

    # Check if downloaded sub is English
    sub_lang = _detect_sub_language(downloaded_path)
    if sub_lang in ENGLISH_LANGS:
        # English sub — sync and write
        output_path = video_path.with_suffix(".en.srt")
        shutil.copy2(downloaded_path, output_path)

        drift_ms = None
        try:
            preview = await async_sync_preview(video_path, output_path)
            drift_ms = int(abs(preview.offset_seconds) * 1000)
            if drift_ms > drift_threshold_ms:
                synced_path = output_path.with_suffix(".synced.srt")
                await async_sync_apply(video_path, output_path, synced_path)
                output_path = synced_path
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
