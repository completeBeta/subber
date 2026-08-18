"""Subber web application — FastAPI + Jinja2."""

import asyncio
import json
import os
import subprocess
import shutil
import tempfile
import time
from copy import deepcopy
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile, Header, HTTPException, Depends
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

from . import config
from .parser import detect_language, read_raw_texts
from .translator import TranslationCancelled, Translator
import logging
import logging.handlers
_log = logging.getLogger("subber")
from .rate_limit import RateLimitMiddleware
from .logsanitize import sanitize_log
from .types import BatchJob, JobStatus, TranslationJob

# ── App setup ──
BASE_DIR = Path(__file__).resolve().parent.parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
_LOG_FILE = Path("/app/data/subber.log")
_LIFECYCLE_FILE = Path("/app/data/lifecycle.jsonl")
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = Path(os.environ.get("SUBBER_UPLOAD_DIR", BASE_DIR / "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Redirect temp files to uploads volume (host mount with lots of space)
import tempfile as _tempfile
_uploads_tmp = UPLOAD_DIR / "tmp"
_uploads_tmp.mkdir(parents=True, exist_ok=True)
_tempfile.tempdir = str(_uploads_tmp)



app = FastAPI(title="Subber", version="0.3.0")
app.add_middleware(RateLimitMiddleware)

# Custom exception handlers - convert detail to error for frontend compatibility
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi import Request as _Request

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: _Request, exc: RequestValidationError):
    msg = exc.errors()[0].get("msg", "Validation error") if exc.errors() else "Validation error"
    return JSONResponse(content={"error": msg}, status_code=422)

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: _Request, exc: StarletteHTTPException):
    return JSONResponse(content={"error": str(exc.detail)}, status_code=exc.status_code)

jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── API key auth (disabled by default — empty key = no auth) ──

async def _require_write_auth(x_api_key: str = Header(None)):
    """FastAPI dependency: require API key for write endpoints.

    If api_key is empty (default), all requests pass through.
    If set, requests must include matching X-API-Key header.
    """
    cfg_key = (config.get().get("ui", {}) or {}).get("api_key", "")
    if not cfg_key:
        return  # disabled — allow all
    if not x_api_key or x_api_key != cfg_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ── In-memory job store ──
_jobs: dict[str, TranslationJob] = {}
_batches: dict[str, BatchJob] = {}

# ── Grab job store (for async pipeline with live status) ──
_grab_jobs: dict[str, dict] = {}
_step_timers: dict[str, float] = {}  # job_id → last step timestamp
_grab_batches: dict[str, dict] = {}  # batch_id → {job_ids, filenames, status}

_GRAB_STATE_FILE = UPLOAD_DIR / ".grab_state.json"

def _save_grab_state():
    """Persist grab state to disk so it survives container restarts."""
    try:
        state = {
            "jobs": _grab_jobs,
            "batches": _grab_batches,
            "timers": _step_timers,
        }
        with open(_GRAB_STATE_FILE, "w") as f:
            json.dump(state, f, default=str)
    except Exception:
        pass

def _load_grab_state():
    """Restore grab state from disk on startup."""
    global _grab_jobs, _grab_batches, _step_timers
    try:
        if _GRAB_STATE_FILE.exists():
            with open(_GRAB_STATE_FILE) as f:
                state = json.load(f)
            _grab_jobs = state.get("jobs", {})
            _grab_batches = state.get("batches", {})
            _step_timers = state.get("timers", {})
            # Mark all previously active jobs as interrupted
            now = time.time()
            for jid, job in _grab_jobs.items():
                if job.get("status") not in ("done", "failed"):
                    job["status"] = "interrupted"
                    job["error"] = "Server restarted — job was interrupted"
                    job["finished_at"] = now
            _log.info("Restored %d grab jobs from disk", len(_grab_jobs))
            _save_grab_state()
    except Exception:
        pass

# ── Sync concurrency lock ──
_SYNC_LOCK = asyncio.Lock()


# ═══════════════════════════════════════════════
# Disk safety
# ═══════════════════════════════════════════════

def _check_disk(min_free_mb: int | None = None) -> None:
    """Raise HTTPException if free disk space is below threshold."""
    if min_free_mb is None:
        min_free_mb = config.limits_settings().get("min_free_disk_mb", 1024)
    usage = shutil.disk_usage(UPLOAD_DIR)
    free_mb = usage.free / (1024 * 1024)
    if free_mb < min_free_mb:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=413,
            detail=f"Insufficient disk space: {free_mb:.0f}MB free, {min_free_mb}MB required",
        )


def _check_upload_size(size: int, max_mb: int | None = None) -> None:
    """Raise HTTPException if upload exceeds max size."""
    if max_mb is None:
        max_mb = config.limits_settings().get("max_upload_mb", 2048)
    if size > max_mb * 1024 * 1024:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=413,
            detail=f"Upload too large: {size / (1024 * 1024):.0f}MB exceeds {max_mb}MB limit",
        )


# ── Translator config ──
def _get_translator():
    from .translator import MultiBackendTranslator
    ts = config.translation_settings()
    backends = config.translation_backends()
    return MultiBackendTranslator(
        backends=backends,
        temperature=ts.get("temperature", 0.1),
        max_tokens=ts.get("max_tokens", 4096),
        chunk_size=ts.get("chunk_size", 50),
        max_retries=ts.get("max_retries", 3),
        timeout=ts.get("timeout", 120),
    )


# ═══════════════════════════════════════════════
# Page routes
# ═══════════════════════════════════════════════

_NO_CACHE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    template = jinja_env.get_template("index.html")
    return HTMLResponse(template.render(request=request), headers=_NO_CACHE_HEADERS)


@app.get("/sync", response_class=HTMLResponse)
async def sync_page(request: Request):
    template = jinja_env.get_template("sync.html")
    return HTMLResponse(template.render(request=request), headers=_NO_CACHE_HEADERS)


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request):
    template = jinja_env.get_template("search.html")
    api_key = (config.get().get("ui", {}) or {}).get("api_key", "")
    return HTMLResponse(template.render(request=request, api_key=api_key), headers=_NO_CACHE_HEADERS)


@app.get("/grab", response_class=HTMLResponse)
async def grab_page(request: Request):
    template = jinja_env.get_template("grab.html")
    return HTMLResponse(template.render(request=request), headers=_NO_CACHE_HEADERS)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    template = jinja_env.get_template("settings.html")
    api_key = (config.get().get("ui", {}) or {}).get("api_key", "")
    return HTMLResponse(
        template.render(request=request, api_key=api_key),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


# ═══════════════════════════════════════════════
# Config API
# ═══════════════════════════════════════════════

def _mask(value: str) -> str:
    """<8 chars → '****', else prefix[2]+****+suffix[2]."""
    if not value:
        return value
    if len(value) < 8:
        return "****"
    return value[:2] + "****" + value[-2:]


@app.get("/api/config")
async def api_get_config():
    """Return the full current config with masked secrets.

    Reloads from disk first — multi-worker uvicorn keeps per-process config
    caches, so without this a GET on worker B shows stale values after a
    POST on worker A updated them.

    Uses a deep copy so masking NEVER mutates the live config cache.
    Masked values are safe for the settings UI to display; the POST handler
    refuses to persist any value containing '****' (see _restore_masked).
    """
    config.reload()
    display = deepcopy(config.get())
    # Translation: top-level key + every backend's key
    tr = display.get("translation")
    if isinstance(tr, dict):
        key = tr.get("api_key")
        if key and key != "ollama":
            tr["api_key"] = _mask(str(key))
        backends = tr.get("backends")
        if isinstance(backends, list):
            for b in backends:
                if isinstance(b, dict) and b.get("api_key"):
                    b["api_key"] = _mask(str(b["api_key"]))

    # Providers: credentials + cookies on every provider
    provs = display.get("providers")
    if isinstance(provs, dict):
        for p in provs.values():
            if not isinstance(p, dict):
                continue
            for field in ("api_key", "vip_api_key", "username", "password"):
                if p.get(field):
                    p[field] = _mask(str(p[field]))
            cookies = p.get("cookies")
            if isinstance(cookies, dict):
                for ck in list(cookies.keys()):
                    if cookies[ck]:
                        cookies[ck] = _mask(str(cookies[ck]))
            elif isinstance(cookies, str) and cookies:
                p["cookies"] = _mask(cookies)

    # Library: TMDB key + SMB mount credentials
    lib = display.get("library")
    if isinstance(lib, dict):
        if lib.get("tmdb_api_key"):
            lib["tmdb_api_key"] = _mask(str(lib["tmdb_api_key"]))
        mounts = lib.get("mounts")
        if isinstance(mounts, list):
            for m in mounts:
                if isinstance(m, dict):
                    for field in ("username", "password"):
                        if m.get(field):
                            m[field] = _mask(str(m[field]))

    # UI: the API key itself (frontend gets it via template render, not here)
    ui = display.get("ui")
    if isinstance(ui, dict) and ui.get("api_key"):
        ui["api_key"] = _mask(str(ui["api_key"]))

    return display


def _restore_masked(values, existing):
    """Replace any masked ('****') values in an incoming update with the real
    values from the current config. Prevents the settings UI round-trip from
    overwriting real credentials with their masked display versions.

    Lists of dicts are matched by 'name' when present, else by index.
    """
    if not isinstance(values, dict) or not isinstance(existing, dict):
        return values
    for k, v in values.items():
        ev = existing.get(k)
        if isinstance(v, dict) and isinstance(ev, dict):
            _restore_masked(v, ev)
        elif isinstance(v, list) and isinstance(ev, list):
            for item in v:
                if not isinstance(item, dict):
                    continue
                match = None
                if item.get("name"):
                    match = next((e for e in ev if isinstance(e, dict) and e.get("name") == item["name"]), None)
                if match is None:
                    idx = v.index(item)
                    match = ev[idx] if idx < len(ev) and isinstance(ev[idx], dict) else None
                if match is not None:
                    _restore_masked(item, match)
        elif isinstance(v, str) and "****" in v:
            values[k] = ev
    return values


@app.post("/api/config")
async def api_update_config(request: Request, _=Depends(_require_write_auth)):
    """Update one or more config sections. Masked ('****') credential values
    are never persisted — the existing real value is kept instead."""
    body = await request.json()
    section = body.get("section")
    values = body.get("values", {})
    if not section:
        return JSONResponse(content={"error": "Missing 'section' field"}, status_code=400)
    try:
        current = config.get().get(section)
        if isinstance(current, dict) and isinstance(values, dict):
            values = _restore_masked(values, current)
            # Defense: strip any nested request-wrapper keys that a malformed
            # client could inject (e.g. {"section": ..., "values": ...} saved
            # as if they were config keys). These corrupt the stored config.
            for junk_key in ("section", "values"):
                values.pop(junk_key, None)
        updated = config.update(section, values)
        return {"ok": True, "section": section, "values": updated}
    except KeyError:
        return JSONResponse(content={"error": f"Unknown config section: {section}"}, status_code=400)


# ═══════════════════════════════════════════════
# Health
# ═══════════════════════════════════════════════

@app.get("/api/health")
async def api_health():
    usage = shutil.disk_usage(UPLOAD_DIR)
    return {
        "status": "ok",
        "free_disk_mb": round(usage.free / (1024 * 1024), 1),
        "total_disk_mb": round(usage.total / (1024 * 1024), 1),
        "active_jobs": len([j for j in _jobs.values() if j.status == JobStatus.TRANSLATING]),
        "sync_busy": _SYNC_LOCK.locked(),
    }


# ═══════════════════════════════════════════════
# Upload + Translation (existing, minimal changes)
# ═══════════════════════════════════════════════

@app.post("/api/upload")
async def api_upload(_=Depends(_require_write_auth),
    file: UploadFile = File(...),
    source_lang: str = Form("auto"),
    target_lang: str = Form("en"),
):
    import zipfile

    if not file.filename:
        return JSONResponse(content={"error": "No file provided"}, status_code=400)

    # Read content for size check
    content = await file.read()
    _check_disk()
    _check_upload_size(len(content))

    suffix = Path(file.filename).suffix.lower()
    if suffix not in (".srt", ".ass", ".ssa", ".vtt", ".zip"):
        return JSONResponse(content={"error": f"Unsupported format: {suffix}. Use .srt, .ass, .ssa, .vtt, or .zip"}, status_code=400)

    job = TranslationJob(
        original_name=file.filename,
        source_lang=source_lang,
        target_lang=target_lang,
        status=JobStatus.PENDING,
    )

    raw_path = UPLOAD_DIR / f"{job.id}_raw{suffix}"
    raw_path.write_bytes(content)

    if suffix == ".zip":
        try:
            with zipfile.ZipFile(raw_path) as zf:
                sub_names = sorted(
                    n for n in zf.namelist()
                    if Path(n).suffix.lower() in (".srt", ".ass", ".ssa", ".vtt")
                    and not n.startswith("__MACOSX")
                )
                if not sub_names:
                    return JSONResponse(content={"error": "No subtitle files found in zip"}, status_code=400)

                if len(sub_names) == 1:
                    sub_name = sub_names[0]
                    sub_suffix = Path(sub_name).suffix.lower()
                    input_path = UPLOAD_DIR / f"{job.id}_in{sub_suffix}"
                    with zf.open(sub_name) as src, open(input_path, "wb") as dst:
                        dst.write(src.read())
                    job.original_name = Path(sub_name).name
                    job.input_path = str(input_path)
                else:
                    return await _handle_multi_upload(
                        zf, sub_names, raw_path, file.filename,
                        source_lang, target_lang
                    )
        except zipfile.BadZipFile:
            return JSONResponse(content={"error": "Invalid or corrupted zip file"}, status_code=400)
    else:
        input_path = UPLOAD_DIR / f"{job.id}_in{suffix}"
        raw_path.rename(input_path)
        job.input_path = str(input_path)

    if source_lang == "auto":
        texts = read_raw_texts(input_path)
        sample = " ".join(texts[:30])
        job.source_lang = detect_language(sample)

    _jobs[job.id] = job

    if job.source_lang == job.target_lang:
        job.status = JobStatus.DONE
        job.output_path = str(input_path)
        job.total_chunks = 0
        return {
            "job_id": job.id,
            "source_lang": job.source_lang,
            "target_lang": job.target_lang,
            "skipped": True,
            "reason": f"File is already in {job.target_lang}",
        }

    asyncio.create_task(_run_translation(job.id))
    return {
        "job_id": job.id,
        "source_lang": job.source_lang,
        "target_lang": job.target_lang,
    }


@app.get("/api/jobs/{job_id}")
async def api_job_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse(content={"error": "Job not found"}, status_code=404)
    return {
        "id": job.id,
        "status": job.status.value,
        "original_name": job.original_name,
        "source_lang": job.source_lang,
        "target_lang": job.target_lang,
        "progress_pct": job.progress_pct,
        "chunks_done": job.chunks_done,
        "total_chunks": job.total_chunks,
        "error": job.error,
        "age_hours": round(job.age_hours, 1),
    }


@app.delete("/api/jobs/{job_id}")
async def api_cancel_job(job_id: str, _=Depends(_require_write_auth)):
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse(content={"error": "Job not found"}, status_code=404)
    if job.status not in (JobStatus.PENDING, JobStatus.TRANSLATING):
        return JSONResponse(content={"error": f"Job already {job.status.value}"}, status_code=409)
    job.status = JobStatus.FAILED
    job.error = "Cancelled by user"
    return {"id": job_id, "status": "cancelled"}


@app.get("/api/jobs")
async def api_jobs_list():
    batch_job_ids = set()
    for b in _batches.values():
        batch_job_ids.update(b.job_ids)
    recent_jobs = sorted(
        [j for j in _jobs.values() if j.id not in batch_job_ids],
        key=lambda j: j.created_at, reverse=True,
    )[:20]
    recent_batches = sorted(
        _batches.values(), key=lambda b: b.created_at, reverse=True,
    )[:10]
    return {
        "jobs": [
            {
                "id": j.id, "status": j.status.value,
                "original_name": j.original_name,
                "source_lang": j.source_lang, "target_lang": j.target_lang,
                "age_hours": round(j.age_hours, 1),
            }
            for j in recent_jobs
        ],
        "batches": [
            {
                "id": b.id, "original_name": b.original_name,
                "file_count": len(b.job_ids),
                "source_lang": b.source_lang, "target_lang": b.target_lang,
                "age_hours": round((time.time() - b.created_at) / 3600, 1),
            }
            for b in recent_batches
        ],
    }


@app.get("/download/{job_id}")
async def download(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return HTMLResponse("Job not found.", status_code=404)
    if job.status != JobStatus.DONE or not job.output_path:
        return HTMLResponse("Translation not ready yet.", status_code=404)
    if not Path(job.output_path).exists():
        return HTMLResponse("File expired or was removed.", status_code=410)
    stem = Path(job.original_name).stem
    download_name = f"{stem}.{job.target_lang}{Path(job.output_path).suffix}"
    return FileResponse(
        job.output_path, filename=download_name, media_type="application/octet-stream",
    )


# ═══════════════════════════════════════════════
# Batch endpoints (unchanged)
# ═══════════════════════════════════════════════

async def _handle_multi_upload(
    zf, sub_names: list, raw_path, zip_name: str, source_lang: str, target_lang: str,
):
    batch = BatchJob(original_name=zip_name, source_lang=source_lang, target_lang=target_lang)
    job_ids = []
    for sub_name in sub_names:
        sub_suffix = Path(sub_name).suffix.lower()
        job = TranslationJob(
            original_name=Path(sub_name).name,
            source_lang=source_lang, target_lang=target_lang,
            status=JobStatus.PENDING,
        )
        input_path = UPLOAD_DIR / f"{job.id}_in{sub_suffix}"
        with zf.open(sub_name) as src, open(input_path, "wb") as dst:
            dst.write(src.read())
        job.input_path = str(input_path)
        if source_lang == "auto":
            texts = read_raw_texts(input_path)
            sample = " ".join(texts[:30])
            job.source_lang = detect_language(sample)
        _jobs[job.id] = job
        job_ids.append(job.id)
        if job.source_lang == job.target_lang:
            job.status = JobStatus.DONE
            job.output_path = str(input_path)
            job.total_chunks = 0
        else:
            asyncio.create_task(_run_translation(job.id))
    batch.job_ids = job_ids
    _batches[batch.id] = batch
    return {
        "batch_id": batch.id, "job_ids": job_ids,
        "file_count": len(job_ids),
        "source_lang": source_lang, "target_lang": target_lang,
    }


@app.get("/api/batch/{batch_id}")
async def api_batch_status(batch_id: str):
    batch = _batches.get(batch_id)
    if not batch:
        return JSONResponse(content={"error": "Batch not found"}, status_code=404)
    jobs = []
    done = 0
    failed = 0
    for jid in batch.job_ids:
        job = _jobs.get(jid)
        if not job:
            continue
        jobs.append({
            "id": job.id, "status": job.status.value,
            "original_name": job.original_name, "error": job.error,
            "progress_pct": job.progress_pct,
        })
        if job.status == JobStatus.DONE:
            done += 1
        elif job.status == JobStatus.FAILED:
            failed += 1
    total = len(batch.job_ids)
    if done == total or done + failed == total:
        overall = "done"
    elif done > 0:
        overall = "translating"
    else:
        overall = "pending"
    return {
        "batch_id": batch.id, "status": overall,
        "original_name": batch.original_name,
        "source_lang": batch.source_lang, "target_lang": batch.target_lang,
        "file_count": total, "done": done, "failed": failed,
        "progress_pct": int((done + failed) / total * 100) if total else 0,
        "jobs": jobs,
    }


@app.get("/download/batch/{batch_id}")
async def download_batch(batch_id: str):
    import io
    import zipfile as zf_mod

    batch = _batches.get(batch_id)
    if not batch:
        return HTMLResponse("Batch not found.", status_code=404)
    zip_buffer = io.BytesIO()
    with zf_mod.ZipFile(zip_buffer, "w", zf_mod.ZIP_DEFLATED) as out_zip:
        for jid in batch.job_ids:
            job = _jobs.get(jid)
            if not job or job.status != JobStatus.DONE or not job.output_path:
                continue
            out_path = Path(job.output_path)
            if not out_path.exists():
                continue
            stem = Path(job.original_name).stem
            arcname = f"{stem}.{job.target_lang}{out_path.suffix}"
            out_zip.write(out_path, arcname)
    zip_buffer.seek(0)
    stem = Path(batch.original_name).stem
    download_name = f"{stem}.{batch.target_lang}.zip"
    from fastapi.responses import Response
    return Response(
        content=zip_buffer.getvalue(), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )


# ═══════════════════════════════════════════════
# Sync API
# ═══════════════════════════════════════════════

@app.post("/api/sync/preview")
async def api_sync_preview(_=Depends(_require_write_auth),
    video_path: str = Form(""),
    sub_path: str = Form(""),
    video_file: UploadFile = File(None),
    sub_file: UploadFile = File(None),
    mode: str = Form("path"),
):
    """Preview sync changes — path mode or upload mode."""
    from .syncer import sync_preview

    if mode == "path":
        video = Path(video_path)
        sub = Path(sub_path)
        if not video.exists():
            return JSONResponse(content={"error": f"Video not found: {video_path}"}, status_code=404)
        if not sub.exists():
            return JSONResponse(content={"error": f"Subtitle not found: {sub_path}"}, status_code=404)

        cfg_engine = config.sync_settings().get("engine", "ffsubsync")
        try:
            preview = sync_preview(video, sub, engine=cfg_engine)
            return _format_preview(preview)
        except Exception as e:
            return JSONResponse(content={"error": f"Sync failed: {e}"}, status_code=500)

    # Upload mode
    if not video_file or not sub_file:
        return JSONResponse(content={"error": "Both video and subtitle files required in upload mode"}, status_code=400)

    # Check size and disk
    video_data = await video_file.read()
    _check_upload_size(len(video_data))
    _check_disk()

    sub_data = await sub_file.read()
    _check_upload_size(len(sub_data))

    # Save to temp files
    video_suffix = Path(video_file.filename or "video").suffix or ".mkv"
    sub_suffix = Path(sub_file.filename or "sub").suffix or ".srt"

    tmp_dir = Path(tempfile.mkdtemp(dir=UPLOAD_DIR, prefix="sync_"))
    try:
        vpath = tmp_dir / f"video{video_suffix}"
        spath = tmp_dir / f"sub{sub_suffix}"
        vpath.write_bytes(video_data)
        spath.write_bytes(sub_data)

        cfg_engine = config.sync_settings().get("engine", "ffsubsync")
        preview = sync_preview(vpath, spath, engine=cfg_engine)
        return _format_preview(preview)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/api/sync/apply")
async def api_sync_apply(_=Depends(_require_write_auth),
    video_path: str = Form(""),
    sub_path: str = Form(""),
    video_file: UploadFile = File(None),
    sub_file: UploadFile = File(None),
    mode: str = Form("path"),
    offset: float = Form(0.0),
):
    """Apply sync and return synced file."""
    from .syncer import sync_apply, async_sync_apply

    async with _SYNC_LOCK:
        if mode == "path":
            video = Path(video_path)
            sub = Path(sub_path)
            if not video.exists():
                return JSONResponse(content={"error": f"Video not found: {video_path}"}, status_code=404)
            if not sub.exists():
                return JSONResponse(content={"error": f"Subtitle not found: {sub_path}"}, status_code=404)

            cfg_engine = config.sync_settings().get("engine", "ffsubsync")
            output_name = f"{sub.stem}.synced{sub.suffix}"
            output_path = UPLOAD_DIR / output_name

            try:
                await async_sync_apply(video, sub, output_path, engine=cfg_engine, offset=offset)
                return FileResponse(
                    output_path, filename=output_name,
                    media_type="application/octet-stream",
                )
            except Exception as e:
                return JSONResponse(content={"error": f"Sync failed: {e}"}, status_code=500)

        # Upload mode
        if not video_file or not sub_file:
            return JSONResponse(content={"error": "Both video and subtitle files required in upload mode"}, status_code=400)

        video_data = await video_file.read()
        _check_upload_size(len(video_data))
        _check_disk()

        sub_data = await sub_file.read()
        _check_upload_size(len(sub_data))

        video_suffix = Path(video_file.filename or "video").suffix or ".mkv"
        sub_suffix = Path(sub_file.filename or "sub").suffix or ".srt"
        out_name = f"{Path(sub_file.filename or 'sub').stem}.synced{sub_suffix}"

        tmp_dir = Path(tempfile.mkdtemp(dir=UPLOAD_DIR, prefix="sync_"))
        try:
            vpath = tmp_dir / f"video{video_suffix}"
            spath = tmp_dir / f"sub{sub_suffix}"
            opath = tmp_dir / f"out{sub_suffix}"
            vpath.write_bytes(video_data)
            spath.write_bytes(sub_data)

            # Compute offset first via preview (handles ASS/SRT reliably)
            real_offset = offset
            cfg_engine = config.sync_settings().get("engine", "ffsubsync")
            try:
                preview = sync_preview(vpath, spath, engine=cfg_engine)
                if abs(preview.offset_seconds) > 0.01:
                    real_offset = preview.offset_seconds
                elif abs(offset) > 0.01:
                    real_offset = offset
            except Exception:
                pass

            # Apply sync
            from .syncer import _offset_apply
            try:
                sync_apply(vpath, spath, opath, engine=cfg_engine, offset=real_offset)
                # Compute actual drift from output (most reliable)
                import pysubs2
                orig_subs = pysubs2.load(str(spath), encoding="utf-8-sig")
                out_subs = pysubs2.load(str(opath), encoding="utf-8-sig")
                if orig_subs.events and out_subs.events:
                    drift_ms = out_subs.events[0].start - orig_subs.events[0].start
                    real_offset = drift_ms / 1000.0
            except Exception:
                _offset_apply(spath, opath, real_offset)

            # Persist sync result to grab history
            import uuid
            sync_job_id = f"sync_{uuid.uuid4().hex[:12]}"
            _grab_jobs[sync_job_id] = {
                "status": "done",
                "steps": [f"Synced {sub_file.filename} with {video_file.filename}"],
                "progress_pct": 100,
                "download_url": f"/download/grab/{out_name}",
                "filename": out_name,
                "error": None,
                "embedded_langs": [],
                "needs_translation": False,
                "found": True,
                "model_used": None,
                "finished_at": time.time(),
            }
            _save_grab_state()

            return FileResponse(
                str(opath), filename=out_name,
                media_type="application/octet-stream",
                headers={"X-Sync-Offset": str(round(real_offset, 3))},
                background=lambda: shutil.rmtree(tmp_dir, ignore_errors=True),
            )
        except Exception as e:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return JSONResponse(content={"error": f"Sync failed: {e}"}, status_code=500)


def _format_preview(preview) -> dict:
    return {
        "offset_seconds": round(preview.offset_seconds, 3),
        "engine": preview.engine,
        "sample_lines": preview.sample_lines,
    }


# Batch sync — zip of video+sub pairs
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt"}


def _pair_files(files: list[Path]) -> list[tuple[Path, Path]]:
    """Match video files to subtitle files by stem prefix."""
    videos = [f for f in files if f.suffix.lower() in VIDEO_EXTS]
    subs = [f for f in files if f.suffix.lower() in SUB_EXTS]
    pairs = []

    for sub in subs:
        sub_stem = sub.stem.lower()
        # Find best video match: video stem is a prefix of sub stem
        best = None
        best_len = 0
        for vid in videos:
            vid_stem = vid.stem.lower()
            if sub_stem.startswith(vid_stem) and len(vid_stem) > best_len:
                best = vid
                best_len = len(vid_stem)
        if best:
            pairs.append((best, sub))

    return pairs


@app.post("/api/sync/batch")
async def api_sync_batch(_=Depends(_require_write_auth),
    file: UploadFile = File(...),
):
    """Batch sync: upload a zip of video+subtitle pairs, get back synced subs as zip."""
    import zipfile as zf_mod
    import io

    if not file.filename or not file.filename.lower().endswith(".zip"):
        return JSONResponse(content={"error": "Upload a .zip file containing video and subtitle files"}, status_code=400)

    # Read and check size
    zip_data = await file.read()
    _check_upload_size(len(zip_data))
    _check_disk()

    async with _SYNC_LOCK:
        tmp_dir = Path(tempfile.mkdtemp(dir=UPLOAD_DIR, prefix="batchsync_"))
        try:
            # Extract zip
            zip_path = tmp_dir / "batch.zip"
            zip_path.write_bytes(zip_data)
            extract_dir = tmp_dir / "extracted"
            extract_dir.mkdir()

            with zf_mod.ZipFile(zip_path) as zf:
                zf.extractall(extract_dir)

            # Find all files
            all_files = list(extract_dir.rglob("*"))
            all_files = [f for f in all_files if f.is_file()]

            # Pair videos with subs
            pairs = _pair_files(all_files)
            if not pairs:
                return JSONResponse(content={"error": "No video+subtitle pairs found in zip. Files must share filename prefixes."}, status_code=400)

            # Sync each pair
            from .syncer import sync_apply, async_sync_apply
            cfg_engine = config.sync_settings().get("engine", "ffsubsync")
            results = []
            synced_dir = tmp_dir / "synced"
            synced_dir.mkdir()

            for vid, sub in pairs:
                out_path = synced_dir / f"{sub.stem}.synced{sub.suffix}"
                try:
                    sync_apply(vid, sub, out_path, engine=cfg_engine)
                    results.append((sub.name, out_path, None))
                except Exception as e:
                    results.append((sub.name, None, str(e)))

            # Build result zip
            result_zip = io.BytesIO()
            synced_count = 0
            failed_count = 0
            with zf_mod.ZipFile(result_zip, "w", zf_mod.ZIP_DEFLATED) as out_zf:
                for sub_name, path, error in results:
                    if path and path.exists():
                        out_zf.write(str(path), Path(sub_name).stem + ".synced" + Path(sub_name).suffix)
                        synced_count += 1
                    else:
                        failed_count += 1

            result_zip.seek(0)

            stem = Path(file.filename).stem
            download_name = f"{stem}.synced.zip"

            from fastapi.responses import Response
            return Response(
                content=result_zip.getvalue(),
                media_type="application/zip",
                headers={
                    "Content-Disposition": f'attachment; filename="{download_name}"',
                    "X-Synced-Count": str(synced_count),
                    "X-Failed-Count": str(failed_count),
                },
            )
        except zf_mod.BadZipFile:
            return JSONResponse(content={"error": "Invalid or corrupted zip file"}, status_code=400)
        except Exception as e:
            return JSONResponse(content={"error": f"Batch sync failed: {e}"}, status_code=500)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ═══════════════════════════════════════════════
# Multi-Provider Search & Download
# ═══════════════════════════════════════════════

_registry: "ProviderRegistry | None" = None


def _get_registry() -> "ProviderRegistry":
    global _registry
    if _registry is None:
        from .config import build_provider_registry
        _registry = build_provider_registry()
    return _registry


@app.post("/api/search")
async def api_search(
    query: str = Form(""),
    video_path: str = Form(""),
    language: str = Form("en"),
    season: int = Form(None),
    episode: int = Form(None),
    _=Depends(_require_write_auth),
):
    """Search all enabled providers for subtitles."""
    registry = _get_registry()

    try:
        results = await registry.search_all(
            query=query,
            language=language,
            season=season,
            episode=episode,
            video_path=Path(video_path) if video_path else None,
        )

        formatted = [
            {
                "id": r.id,
                "filename": r.filename,
                "language": r.language,
                "provider": r.provider,
                "downloads": r.downloads,
                "rating": r.rating,
                "hearing_impaired": r.hearing_impaired,
                "release_info": r.release_info,
                # metadata lets the download endpoint re-invoke the provider
                # statelessly (video_path stripped — internal path)
                "metadata": {k: v for k, v in (r.metadata or {}).items() if k != "video_path"},
            }
            for r in results[:30]
        ]

        return {
            "results": formatted,
            "total": len(results),
            "providers_searched": registry.names,
        }
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.post("/api/download/{file_id}")
async def api_download_sub(file_id: str, request: Request, _=Depends(_require_write_auth)):
    """Download a subtitle by provider-prefixed ID (e.g. 'subdl_3197651').

    Accepts an optional JSON body {"metadata": {...}} from the search page so
    the provider gets the same download identifiers it returned at search time.
    Without a body, falls back to reconstructing provider-specific keys from
    the ID (works for SubDL sd_id and OpenSubtitles file_id).
    """
    registry = _get_registry()

    try:
        # file_id format: "providername_restofid"
        provider_name, _, rest_id = file_id.partition("_")
        # Resolve provider name: exact → case-insensitive → alias map
        # (search results use abbreviations like "os" for OpenSubtitles)
        _PROVIDER_ALIASES = {
            "os": "OpenSubtitles",
            "opensubtitles": "OpenSubtitles",
            "subdl": "SubDL",
            "gestdown": "Gestdown",
            "podnapisi": "Podnapisi",
            "embedded": "Embedded",
        }
        provider = registry.get(provider_name)
        if not provider:
            provider_name_lower = provider_name.lower()
            for key in registry.names:
                if key.lower() == provider_name_lower:
                    provider = registry.get(key)
                    provider_name = key
                    break
        if not provider and provider_name_lower in _PROVIDER_ALIASES:
            alias_target = _PROVIDER_ALIASES[provider_name_lower]
            provider = registry.get(alias_target)
            if provider:
                provider_name = alias_target
        if not provider:
            return JSONResponse(content={"error": f"Provider '{provider_name}' not found"}, status_code=400)

        # Prefer metadata supplied by the client (from the search response)
        metadata: dict = {}
        try:
            body = await request.json()
            if isinstance(body, dict) and isinstance(body.get("metadata"), dict):
                metadata = body["metadata"]
        except Exception:
            pass
        if not metadata:
            # Fallback: reconstruct the keys each provider understands
            if provider_name.lower() == "subdl":
                metadata = {"sd_id": rest_id}
            elif provider_name.lower() == "opensubtitles":
                try:
                    metadata = {"file_id": int(rest_id)}
                except (TypeError, ValueError):
                    metadata = {}
            else:
                metadata = {"remote_id": rest_id}

        output_path = UPLOAD_DIR / f"{file_id}.srt"
        from .types import SubtitleResult
        result = SubtitleResult(
            id=file_id,
            filename=f"{file_id}.srt",
            language="en",
            provider=provider_name,
            metadata=metadata,
        )
        downloaded = await provider.download(result, output_path)

        return FileResponse(
            downloaded,
            filename=downloaded.name,
            media_type="application/octet-stream",
        )
    except Exception as e:
        return JSONResponse(content={"error": f"Download failed: {e}"}, status_code=500)


# ═══════════════════════════════════════════════
# Grab Pipeline — upload video → get synced subs
# ═══════════════════════════════════════════════

# ── Shared grab pipeline ──

async def _run_grab_pipeline(
    video_path: Path,
    video_filename: str,
    tmp_dir: Path,
    language: str,
    sync: bool,
    registry: "ProviderRegistry",
    on_step=None,
) -> dict:
    """Run the grab pipeline on a single video. Returns result dict."""
    pipeline_result = {
        "video": str(video_filename),
        "steps": [],
        "found": False,
        "needs_translation": False,
        "embedded_langs": [],
        "output_path": "",
        "error": None,
        "model_used": None,
    }

    print(f"[GRAB_PIPE] ENTER: {video_filename}", flush=True)
    try:
        # Helper to translate non-English subs
        ts = config.translation_settings()
        _backends = config.translation_backends()
        def _do_translate(sub_path: Path, source_lang: str) -> tuple[Path, str]:
            translated = tmp_dir / f"{sub_path.stem}.en{sub_path.suffix}"
            from .translator import translate_subtitles_multi
            model_used = translate_subtitles_multi(
                sub_path, translated, source_lang, "en",
                backends=_backends,
                temperature=ts.get("temperature", 0.1),
                max_tokens=ts.get("max_tokens", 4096),
                chunk_size=ts.get("chunk_size", 50),
                max_retries=ts.get("max_retries", 3),
                timeout=ts.get("timeout", 120),
            )
            # Inject model info into subtitle file header
            _inject_model_header(translated, model_used)
            return translated, (model_used or "unknown")


        # Step 1: Probe embedded subtitles with smart track selection
        from .providers.embedded import EmbeddedProvider
        embedded_prov = EmbeddedProvider()
        print(f"[GRAB_PIPE] Probing embedded for {video_filename}...", flush=True)
        best_embedded, embedded_langs = await embedded_prov.get_embedded_result(video_path)
        print(f"[GRAB_PIPE] Probe done: best={best_embedded is not None}, langs={embedded_langs}", flush=True)
        pipeline_result["embedded_langs"] = embedded_langs

        if best_embedded:
            if best_embedded.language == "en":
                pipeline_result["steps"].append(f"Found embedded English subtitle ({best_embedded.release_info})")
                if on_step:
                    on_step(pipeline_result["steps"][-1])
            else:
                pipeline_result["steps"].append(f"Found embedded {best_embedded.language.upper()} subtitle ({best_embedded.release_info}) — auto-translating")
                if on_step:
                    on_step(pipeline_result["steps"][-1])
                pipeline_result["needs_translation"] = True

            sub_path = tmp_dir / f"extracted{Path(best_embedded.filename).suffix}"
            await embedded_prov.download(best_embedded, sub_path)
            pipeline_result["found"] = True

            if best_embedded.language == "en":
                pipeline_result["output_path"] = str(sub_path)
            else:
                pipeline_result["steps"].append("Translating via LLM...")
                if on_step:
                    on_step(pipeline_result["steps"][-1])
                loop = asyncio.get_running_loop()
                trans_result = await loop.run_in_executor(
                    None, _do_translate, sub_path, best_embedded.language
                )
                pipeline_result["output_path"] = str(trans_result[0])
                pipeline_result["model_used"] = trans_result[1]

        else:
            # Step 2: Search providers
            query = Path(video_filename).stem
            _log.info("GRAB_PIPE no embedded subs, searching %d providers for '%s'", registry.count, sanitize_log(query))
            pipeline_result["steps"].append(f"Searching {registry.count} providers for '{query}'...")
            if on_step:
                on_step(pipeline_result["steps"][-1])

            results = []
            for try_lang in [language] + [l for l in config.selection_settings()["language_priority"] if l != language]:
                results = await registry.search_all(
                    query=query, language=try_lang, video_path=video_path,
                )
                if results:
                    break

            if not results:
                pipeline_result["steps"].append("No subtitles found")
                if on_step:
                    on_step(pipeline_result["steps"][-1])
                return pipeline_result

            best = results[0]
            pipeline_result["steps"].append(f"Found '{best.filename}' via {best.provider}")
            if on_step:
                on_step(pipeline_result["steps"][-1])

            sub_path = tmp_dir / best.filename
            await registry.download(best, tmp_dir)
            pipeline_result["found"] = True

            # Auto-translate if provider result is non-English
            if best.language != "en":
                pipeline_result["steps"].append(f"Translating {best.language.upper()} → en via LLM...")
                if on_step:
                    on_step(pipeline_result["steps"][-1])
                loop = asyncio.get_running_loop()
                trans_result = await loop.run_in_executor(
                    None, _do_translate, sub_path, best.language
                )
                pipeline_result["output_path"] = str(trans_result[0])
                pipeline_result["model_used"] = trans_result[1]
                pipeline_result["needs_translation"] = True
            else:
                pipeline_result["output_path"] = str(sub_path)

        # Step 3: Sync (if requested by user checkbox)
        print(f"[GRAB_PIPE] Sync check: found={pipeline_result['found']} sync={sync}", flush=True)
        if pipeline_result["found"] and sync:
            _log.info("GRAB_PIPE syncing external sub for %s", sanitize_log(video_filename))
            pipeline_result["steps"].append("Syncing with ffsubsync...")
            if on_step:
                on_step(pipeline_result["steps"][-1])
            sub_path = Path(pipeline_result["output_path"])
            synced_path = tmp_dir / f"synced{sub_path.suffix}"
            from .syncer import async_sync_apply
            await async_sync_apply(
                video_path, sub_path, synced_path,
                engine=config.sync_settings().get("engine", "ffsubsync"),
            )
            pipeline_result["output_path"] = str(synced_path)
            pipeline_result["steps"].append("Sync complete")
            if on_step:
                on_step(pipeline_result["steps"][-1])


        pipeline_result["steps"].append("\u2713 Ready")
        if on_step:
            on_step(pipeline_result["steps"][-1])
        return pipeline_result

    except Exception as e:
        pipeline_result["steps"].append(f"\u2717 Error: {e}")
        if on_step:
            on_step(pipeline_result["steps"][-1])
        pipeline_result["error"] = str(e)
        return pipeline_result


def _grab_step(job_id: str, step: str) -> None:
    """Record a pipeline step for a grab job with timing."""
    job = _grab_jobs.get(job_id)
    if not job:
        return

    now = time.time()
    prev = _step_timers.get(job_id, now)
    _step_timers[job_id] = now
    elapsed = now - prev

    # Format elapsed time for this phase
    if "\u2713 Ready" in step or "\u2717 Error" in step:
        # Total time since recording started for this job
        startup = _step_timers.get(f"{job_id}_start", now)
        total = now - startup
        if total < 60:
            step = f"{step}  \u2014 total {total:.1f}s"
        else:
            mins = int(total // 60)
            secs = round(total % 60)
            if secs == 60:
                mins += 1
                secs = 0
            step = f"{step}  \u2014 total {mins}m{secs}s"
    elif "File received" in step:
        # Record start time, no phase timing for this one
        _step_timers[f"{job_id}_start"] = now
    else:
        # Append phase duration
        if elapsed < 1:
            step = f"{step} ({elapsed*1000:.0f}ms)"
        elif elapsed < 60:
            step = f"{step} ({elapsed:.1f}s)"
        else:
            mins = int(elapsed // 60)
            secs = round(elapsed % 60)
            if secs == 60:
                mins += 1
                secs = 0
            step = f"{step} ({mins}m{secs}s)"

    # Append the step text so the frontend sees it
    job["steps"].append(step)
    _save_grab_state()  # persist to disk

    if "Probing" in step or "embedded" in step.lower():
        job["status"] = "probing"
        job["progress_pct"] = max(job["progress_pct"], 15)
    elif "Searching" in step or "providers" in step.lower():
        job["status"] = "searching"
        job["progress_pct"] = max(job["progress_pct"], 30)
    elif "Found via" in step or "Downloading" in step:
        job["status"] = "downloading"
        job["progress_pct"] = max(job["progress_pct"], 50)
    elif "Translating" in step:
        job["status"] = "translating"
        job["progress_pct"] = max(job["progress_pct"], 70)
    elif "Syncing" in step:
        job["status"] = "syncing"
        job["progress_pct"] = max(job["progress_pct"], 85)

def _inject_model_header(sub_path: Path, model_name: str) -> None:
    """Add AI translation metadata to the subtitle file header."""
    if not model_name or model_name == "none":
        return
    try:
        with open(sub_path, 'r', encoding='utf-8-sig') as f:
            content_text = f.read()
        header = f"; AI translated by Subber — model: {model_name}\n; https://github.com/completeBeta/Subber\n"
        if content_text.startswith(";") or content_text.startswith("["):
            # SRT/ASS: prepend comment
            with open(sub_path, 'w', encoding='utf-8') as f:
                f.write(header + content_text)
    except Exception:
        pass  # Don't break the pipeline for header injection


async def _run_grab_job(
    job_id: str,
    video_path: Path,
    video_filename: str,
    tmp_dir: Path,
    language: str,
    sync: bool,
    registry: "ProviderRegistry",
) -> None:
    """Run the grab pipeline in background, updating _grab_jobs as it goes."""
    print(f"[GRAB] _run_grab_job ENTER: {job_id} {video_filename}", flush=True)
    job = _grab_jobs.get(job_id)
    if not job:
        print(f"[GRAB] _run_grab_job: job {job_id} NOT FOUND in _grab_jobs!", flush=True)
        return

    try:
        print(f"[GRAB] Starting pipeline for {job_id}", flush=True)
        job["status"] = "probing"
        _grab_step(job_id, "Probing for embedded subtitles...")
        job["progress_pct"] = 10

        print(f"[GRAB] Calling _run_grab_pipeline for {video_filename}", flush=True)
        result = await _run_grab_pipeline(
            video_path, video_filename, tmp_dir, language, sync, registry,
            on_step=lambda step: _grab_step(job_id, step),
        )

        # Steps already recorded live via _grab_step callbacks — no merge needed
        job["found"] = result.get("found", False)
        job["embedded_langs"] = result.get("embedded_langs", [])
        job["needs_translation"] = result.get("needs_translation", False)
        job["model_used"] = result.get("model_used")

        _log.info("GRAB pipeline complete: found=%s path=%s", result.get("found"), result.get("output_path", "none"))
        if result.get("found") and result.get("output_path"):
            # Name output file after the video, not a random hash
            ext = Path(result["output_path"]).suffix
            safe_name = Path(video_filename).stem
            final_path = UPLOAD_DIR / f"{safe_name}{ext}"
            # Handle collisions
            counter = 1
            while final_path.exists():
                final_path = UPLOAD_DIR / f"{safe_name}_{counter}{ext}"
                counter += 1
            shutil.copy2(Path(result["output_path"]), final_path)
            job["download_url"] = f"/download/grab/{final_path.name}"
            job["filename"] = final_path.name
            job["status"] = "done"
            job["progress_pct"] = 100
            job["finished_at"] = time.time()
            _grab_step(job_id, "\u2713 Ready")
        else:
            job["status"] = "done"
            job["progress_pct"] = 100
            job["finished_at"] = time.time()
            job["error"] = result.get("error", "No subtitles found")

    except Exception as e:
        _log.error("GRAB pipeline FAILED: %s", e, exc_info=True)
        job["status"] = "failed"
        job["finished_at"] = time.time()
        job["error"] = str(e)
        _grab_step(job_id, f"\u2717 Error: {e}")
    finally:
        # Cleanup tmp_dir after 1 hour (give time for download)
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/api/grab")
async def api_grab(request: Request, _=Depends(_require_write_auth),
    video_file: UploadFile = File(...),
    language: str = Form("en"),
    sync: bool = Form(True),
    provider: str = Form(""),
):
    """Full pipeline: stream to disk → return job_id → process in background.
    
    Returns immediately with a job_id. Poll GET /api/grab/{job_id} for live status.
    """
    import uuid
    _check_disk()
    registry = _get_registry()

    original_name = video_file.filename or "video.mkv"
    video_suffix = Path(original_name).suffix or ".mkv"

    job_id = f"grab_{uuid.uuid4().hex[:12]}"
    tmp_dir = Path(tempfile.mkdtemp(dir=UPLOAD_DIR, prefix="grab_"))
    video_path = tmp_dir / f"source{video_suffix}"

    # Stream file to disk (chunked) — avoids reading 421MB into RAM
    total_bytes = 0
    with open(video_path, "wb") as f:
        while chunk := await video_file.read(1024 * 1024):  # 1MB chunks
            f.write(chunk)
            total_bytes += len(chunk)

    _check_upload_size(total_bytes)

    # Initialize job status
    _grab_jobs[job_id] = {
        "status": "uploaded",
        "steps": [],
        "progress_pct": 5,
        "download_url": None,
        "filename": None,
        "error": None,
        "embedded_langs": [],
        "needs_translation": False,
        "found": False,
        "tmp_dir": str(tmp_dir),
    }

    _grab_step(job_id, "File received — starting pipeline...")

    # Run pipeline in background
    task = asyncio.create_task(_run_grab_job(job_id, video_path, original_name, tmp_dir, language, sync, registry))
    print(f"[GRAB] Task created for {job_id}, task name: {task.get_name()}", flush=True)

    return {"job_id": job_id}



@app.post("/api/grab/clear")
async def api_grab_clear(_=Depends(_require_write_auth),):
    """Clear all grab job history."""
    global _grab_jobs, _grab_batches, _step_timers
    _grab_jobs.clear()
    _grab_batches.clear()
    _step_timers.clear()
    if _GRAB_STATE_FILE.exists():
        _GRAB_STATE_FILE.unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/grab/history")
async def api_grab_history():
    """Return all active grab batches and standalone jobs for UI restoration."""
    if _GRAB_STATE_FILE.exists():
        try:
            with open(_GRAB_STATE_FILE) as f:
                state = json.load(f)
            _grab_jobs.clear()
            _grab_jobs.update(state.get("jobs", {}))
            _grab_batches.clear()
            _grab_batches.update(state.get("batches", {}))
        except Exception:
            pass
    now = time.time()
    active_batches = []
    for bid, batch in _grab_batches.items():
        age = now - batch.get("created_at", now)
        if age > 86400:
            continue
        active_batches.append({
            "batch_id": bid,
            "zip_name": batch.get("zip_name", ""),
            "status": batch.get("status", "unknown"),
            "created_at": batch.get("created_at", 0),
            "age_hours": round(age / 3600, 1),
            "job_ids": [j["job_id"] for j in batch.get("jobs", [])],
            "jobs": batch.get("jobs", []),
        })

    standalone_jobs = []
    batch_job_ids = set()
    for batch in _grab_batches.values():
        for j in batch.get("jobs", []):
            batch_job_ids.add(j.get("job_id"))

    for jid, job in _grab_jobs.items():
        if jid in batch_job_ids:
            continue
        age = now - job.get("finished_at", now)
        if age > 86400:
            continue
        standalone_jobs.append({
            "job_id": jid,
            "status": job.get("status", "unknown"),
            "filename": job.get("filename"),
            "download_url": job.get("download_url"),
            "progress_pct": job.get("progress_pct", 0),
            "found": job.get("found", False),
            "needs_translation": job.get("needs_translation", False),
            "model_used": job.get("model_used"),
            "error": job.get("error"),
            "finished_at": job.get("finished_at", 0),
            "age_hours": round(age / 3600, 1) if job.get("finished_at") else 0,
        })

    return {
        "batches": active_batches,
        "standalone_jobs": standalone_jobs,
    }


@app.get("/api/grab/{job_id}")
async def api_grab_status(job_id: str):
    """Poll for grab pipeline status."""
    job = _grab_jobs.get(job_id)
    if not job:
        return JSONResponse(content={"error": "Job not found"}, status_code=404)
    return {
        "status": job["status"],
        "steps": list(job["steps"]),
        "progress_pct": job["progress_pct"],
        "download_url": job.get("download_url"),
        "filename": job.get("filename"),
        "error": job.get("error"),
        "embedded_langs": job.get("embedded_langs", []),
        "found": job.get("found", False),
        "needs_translation": job.get("needs_translation", False),
        "model_used": job.get("model_used"),
    }


@app.post("/api/grab/batch")
async def api_grab_batch(_=Depends(_require_write_auth),
    file: UploadFile = File(...),
    language: str = Form("en"),
    sync: bool = Form(True),
):
    """Batch grab: upload a zip → extract videos → queue each as async grab job.
    
    Returns immediately with batch_id + list of job_ids. Frontend polls each job
    individually via the existing GET /api/grab/{job_id} endpoint.
    """
    import uuid
    import zipfile as zf_mod

    if not file.filename or not file.filename.lower().endswith(".zip"):
        return JSONResponse(content={"error": "Upload a .zip file containing video files"}, status_code=400)

    batch_id = f"batch_{uuid.uuid4().hex[:10]}"
    tmp_dir = Path(tempfile.mkdtemp(dir=UPLOAD_DIR, prefix="batch_"))
    zip_path = tmp_dir / "upload.zip"

    # Stream zip to disk (chunked, same as single-file)
    total_bytes = 0
    with open(zip_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
            total_bytes += len(chunk)
    _check_upload_size(total_bytes)
    _check_disk()
    registry = _get_registry()

    # Return immediately — extract and queue in background
    _grab_batches[batch_id] = {
        "status": "extracting",
        "zip_name": file.filename,
        "tmp_dir": str(tmp_dir),
        "created_at": time.time(),
    }

    async def _process_bg():
        try:
            extract_dir = tmp_dir / "extracted"
            extract_dir.mkdir()
            with zf_mod.ZipFile(zip_path) as zf:
                zf.extractall(extract_dir)

            # Find video files
            VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm"}
            video_files = sorted(
                [f for f in extract_dir.rglob("*")
                 if f.is_file() and f.suffix.lower() in VIDEO_EXTS],
                key=lambda f: f.name,
            )

            if not video_files:
                _grab_batches[batch_id] = {"status": "failed", "error": "No video files found in zip"}
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return

            # Queue each video as its own grab job
            jobs = []
            for vf in video_files:
                job_id = f"grab_{uuid.uuid4().hex[:12]}"
                vid_tmp = Path(tempfile.mkdtemp(dir=tmp_dir, prefix="vid_"))
                # Copy video to job's tmp dir
                vid_path = vid_tmp / f"source{vf.suffix}"
                shutil.copy2(vf, vid_path)

                _grab_jobs[job_id] = {
                    "status": "uploaded",
                    "steps": [],
                    "progress_pct": 5,
                    "download_url": None,
                    "filename": None,
                    "error": None,
                    "embedded_langs": [],
                    "needs_translation": False,
                    "found": False,
                    "model_used": None,
                    "tmp_dir": str(vid_tmp),
                }
                _grab_step(job_id, f"File received — {vf.name}")

                asyncio.create_task(
                    _run_grab_job(job_id, vid_path, vf.name, vid_tmp, language, sync, registry)
                )
                jobs.append({"job_id": job_id, "filename": vf.name, "size_mb": round(vf.stat().st_size / 1_048_576, 1)})

            _grab_batches[batch_id] = {
                "jobs": jobs,
                "zip_name": file.filename,
                "tmp_dir": str(tmp_dir),
                "status": "processing",
                "created_at": time.time(),
            }
        except zf_mod.BadZipFile:
            _grab_batches[batch_id] = {"status": "failed", "error": "Invalid or corrupted zip file"}
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception as e:
            _grab_batches[batch_id] = {"status": "failed", "error": str(e)}
            shutil.rmtree(tmp_dir, ignore_errors=True)

    asyncio.create_task(_process_bg())

    return {"batch_id": batch_id, "status": "extracting"}


@app.get("/api/grab/batch/{batch_id}")
async def api_grab_batch_status(batch_id: str):
    """Poll batch status — returns summary + per-job status."""
    batch = _grab_batches.get(batch_id)
    if not batch:
        return JSONResponse(content={"error": "Batch not found"}, status_code=404)

    if batch.get("status") in ("extracting", "failed"):
        return {
            "batch_id": batch_id,
            "status": batch["status"],
            "total": 0, "done": 0, "failed": 0,
            "jobs": [],
            "error": batch.get("error"),
        }

    jobs_status = []
    done = 0
    failed = 0
    for j in batch["jobs"]:
        gj = _grab_jobs.get(j["job_id"], {})
        status = gj.get("status", "unknown")
        if status == "done":
            done += 1
        elif status == "failed":
            failed += 1
        jobs_status.append({
            "job_id": j["job_id"],
            "filename": j["filename"],
            "status": status,
            "progress_pct": gj.get("progress_pct", 0),
            "download_url": gj.get("download_url"),
            "model_used": gj.get("model_used"),
            "error": gj.get("error"),
        })

    total = len(batch["jobs"])
    if done + failed == total:
        batch["status"] = "complete"

    return {
        "batch_id": batch_id,
        "status": batch["status"],
        "total": total,
        "done": done,
        "failed": failed,
        "jobs": jobs_status,
    }


@app.get("/download/grab/{filename}")
async def download_grab_result(filename: str):
    """Download a grab pipeline result."""
    path = UPLOAD_DIR / filename
    if not path.exists():
        return HTMLResponse("File not found or expired.", status_code=404)
    return FileResponse(
        path, filename=filename,
        media_type="application/octet-stream",
    )


# ═══════════════════════════════════════════════
# Background tasks
# ═══════════════════════════════════════════════

async def _run_translation(job_id: str) -> None:
    job = _jobs.get(job_id)
    if not job:
        return
    import time as _time, logging
    _log = logging.getLogger("subber")
    _t0 = _time.monotonic()
    try:
        job.status = JobStatus.TRANSLATING
        input_path = Path(job.input_path)
        output_path = UPLOAD_DIR / f"{job_id}_out{input_path.suffix}"
        job.output_path = str(output_path)

        _log.info("Starting translation: %s (%s -> %s)", sanitize_log(job.original_name), job.source_lang, job.target_lang)

        translator = _get_translator()

        import pysubs2
        subs = pysubs2.load(str(input_path), encoding="utf-8-sig")
        total_lines = len(subs.events)
        chunk_size = translator.chunk_size
        job.total_chunks = max(1, (total_lines + chunk_size - 1) // chunk_size)
        _log.info("  %d lines, %d chunks (chunk_size=%d)", total_lines, job.total_chunks, chunk_size)

        def on_progress(chunk: int, total: int) -> None:
            job.chunks_done = chunk
            job.total_chunks = total
            _log.info("  Chunk %d/%d", chunk, total)

        translator.translate(
            input_path, output_path,
            job.source_lang, job.target_lang,
            on_progress=on_progress,
            cancel_check=lambda: job.status == JobStatus.FAILED,
        )
        _elapsed = _time.monotonic() - _t0
        _log.info("Translation complete in %.1fs: %s", _elapsed, sanitize_log(job.original_name))
        job.status = JobStatus.DONE
    except TranslationCancelled:
        _log.info("Translation cancelled: %s", sanitize_log(job.original_name))
    except Exception as e:
        if job.status != JobStatus.FAILED:
            job.status = JobStatus.FAILED
            job.error = str(e)
        _log.error("Translation failed: %s — %s", sanitize_log(job.original_name), e)


async def _cleanup_expired() -> None:
    """Remove jobs, batches, and files older than 24 hours."""
    while True:
        await asyncio.sleep(3600)
        now = time.time()
        # ── Old translation jobs ──
        expired_ids = [
            jid for jid, job in _jobs.items()
            if now - job.created_at > 86400
        ]
        for jid in expired_ids:
            job = _jobs.pop(jid, None)
            if job:
                job.status = JobStatus.EXPIRED
                for p in (job.input_path, job.output_path):
                    if p and Path(p).exists():
                        Path(p).unlink(missing_ok=True)

        # ── Old translation batches ──
        expired_batch_ids = [
            bid for bid, batch in _batches.items()
            if now - batch.created_at > 86400
        ]
        for bid in expired_batch_ids:
            _batches.pop(bid, None)

        # ── Old grab batches (dict-based) ──
        expired_grab_ids = [
            bid for bid, batch in _grab_batches.items()
            if now - batch.get("created_at", 0) > 86400
        ]
        for bid in expired_grab_ids:
            batch = _grab_batches.pop(bid, None)
            if batch and batch.get("tmp_dir"):
                shutil.rmtree(batch["tmp_dir"], ignore_errors=True)

        # ── Old grab jobs ──
        expired_grab_jobs = [
            jid for jid, job in _grab_jobs.items()
            if job.get("status") in ("done", "failed")
            and now - job.get("finished_at", 0) > 86400
        ]
        for jid in expired_grab_jobs:
            _grab_jobs.pop(jid, None)

        # ── Orphaned temp dirs (sync_*, batch_*, grab_*) ──
        for pattern in ("sync_*", "batch_*", "grab_*"):
            for tmp in UPLOAD_DIR.glob(pattern):
                try:
                    if tmp.is_dir() and (now - tmp.stat().st_mtime) > 86400:
                        shutil.rmtree(tmp, ignore_errors=True)
                except Exception:
                    pass


# ── Library routes ──

import json as _json_lib
from . import library_db as _libdb
from . import library_pipeline as _libpipe
from . import config as _subber_config

# Library scan lock — only one scan at a time
_lib_scan_lock = asyncio.Lock()
_lib_active_scans: dict = {}  # scan_id → {status, progress, files_total, files_processed}

@app.get("/library", response_class=HTMLResponse)
async def library_page(request: Request):
    api_key = (config.get().get("ui", {}) or {}).get("api_key", "")
    return HTMLResponse(
        jinja_env.get_template("library.html").render(api_key=api_key),
        headers=_NO_CACHE_HEADERS,
    )

async def _launch_library_scan(
    scan_type: str = "full",
    paths: list[str] | None = None,
    dry_run: bool = False,
    media_types: list[str] | None = None,
) -> int:
    """Start a library scan in the background. Returns scan_id.

    Shared by the /api/library/scan endpoint and the auto-scan scheduler.
    Raises RuntimeError if a scan is already active or paused.
    """
    active = _libdb.get_active_scan()
    if active:
        raise RuntimeError(
            f"Scan {active['id']} is already "
            f"{'running' if active.get('status') == 'running' else 'paused'}."
        )

    lib_cfg = _subber_config.get_section("library")
    max_concurrent = lib_cfg.get("max_concurrent", 2)
    drift_threshold = lib_cfg.get("drift_threshold_ms", 200)

    _libdb.init_db()

    # Auto-backup before every scan (best effort — never blocks scanning).
    try:
        bk = _libdb.create_backup(kind="auto")
        print(f"[LIBRARY] Auto-backup before scan {scan_type}: {bk['name']} ({bk['size']} bytes)", flush=True)
    except Exception as e:
        print(f"[LIBRARY] Auto-backup failed (scan continues): {e}", flush=True)

    scan_id = _libdb.create_scan(scan_type)
    _lib_active_scans[scan_id] = {"status": "running", "files_total": 0, "files_processed": 0}

    async def _run_scan():
        async with _lib_scan_lock:
            try:
                await _libpipe.run_scan(
                    scan_id=scan_id,
                    scan_type=scan_type,
                    paths=paths,
                    dry_run=dry_run,
                    media_types=media_types,
                    max_concurrent=max_concurrent,
                    drift_threshold_ms=drift_threshold,
                )
                cur = _libdb.get_scan(scan_id)
                if cur and cur.get("status") == "paused":
                    _lib_active_scans[scan_id] = {"status": "paused", **_lib_active_scans.get(scan_id, {})}
                else:
                    _lib_active_scans[scan_id] = {"status": "completed", **_lib_active_scans.get(scan_id, {})}
            except Exception as e:
                _lib_active_scans[scan_id] = {"status": "failed", "error": str(e)}
                _libdb.update_scan(scan_id, status="failed", error_message=str(e))

    task = asyncio.create_task(_run_scan())
    _lib_active_scans[scan_id]["task"] = task
    return scan_id


@app.post("/api/library/scan")
async def api_library_scan(request: Request, _=Depends(_require_write_auth)):
    """Start a library scan. Returns scan_id immediately."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    scan_type = body.get("type", "full")
    paths = body.get("paths")
    dry_run = body.get("dry_run", False)
    media_types = body.get("media_types")

    try:
        scan_id = await _launch_library_scan(scan_type, paths, dry_run, media_types)
    except RuntimeError as e:
        return JSONResponse(content={"error": str(e)}, status_code=409)

    return JSONResponse(content={"scan_id": scan_id})


async def _auto_scan_scheduler() -> None:
    """Periodically trigger a scan when `library.scan_interval_hours` is set.

    The interval was previously stored-only (no scheduler consumed it), so
    auto-scan silently did nothing. This loop:
      - checks config every 10 minutes,
      - skips if interval is 0 (manual) or a scan is already active/paused,
      - triggers a full scan when the configured interval has elapsed.
    """
    _last_auto_scan = time.monotonic()  # don't fire immediately on startup
    while True:
        await asyncio.sleep(600)  # check every 10 min
        try:
            lib_cfg = _subber_config.get_section("library")
            interval_hours = int(lib_cfg.get("scan_interval_hours", 0) or 0)
            if interval_hours <= 0:
                _last_auto_scan = time.monotonic()  # reset timer while disabled
                continue

            # Never overlap a manual/paused scan.
            active = _libdb.get_active_scan()
            if active:
                continue

            elapsed = time.monotonic() - _last_auto_scan
            if elapsed < interval_hours * 3600:
                continue

            _log.info("Auto-scan scheduler: interval %dh elapsed — starting full scan", interval_hours)
            scan_id = await _launch_library_scan("full")
            _last_auto_scan = time.monotonic()
            _log.info("Auto-scan scheduler: started scan %d", scan_id)
        except Exception as e:
            _log.warning("Auto-scan scheduler error: %s", e)

@app.get("/api/library/scan/{scan_id}")
async def api_library_scan_status(scan_id: str):
    """Poll scan progress."""
    try:
        sid = int(scan_id)
    except ValueError:
        return JSONResponse(content={"error": "Invalid scan ID"}, status_code=400)

    # Get from active scans first
    active = _lib_active_scans.get(sid)
    db_scan = _libdb.get_scan(sid)

    if db_scan:
        result = {
            "scan_id": sid,
            "status": db_scan["status"],
            "files_total": db_scan["files_total"] or 0,
            "files_processed": db_scan["files_processed"] or 0,
            "files_skipped": db_scan["files_skipped"] or 0,
            "files_failed": db_scan["files_failed"] or 0,
            "translation_cost": db_scan["translation_cost"] or 0,
            "error_message": db_scan.get("error_message"),
            "started_at": db_scan["started_at"],
            "completed_at": db_scan.get("completed_at"),
        }
    else:
        return JSONResponse(content={"error": "Scan not found"}, status_code=404)

    return JSONResponse(content=result)

@app.delete("/api/library/scan/{scan_id}")
async def api_library_scan_cancel(scan_id: str, _=Depends(_require_write_auth)):
    """Cancel a running library scan."""
    try:
        sid = int(scan_id)
    except ValueError:
        return JSONResponse(content={"error": "Invalid scan ID"}, status_code=400)
    cancelled = _libpipe.cancel_scan(sid)
    if not cancelled:
        return JSONResponse(content={"error": "Scan not found or already completed"}, status_code=404)
    _libdb.update_scan(sid, status="cancelled", error_message="Cancelled by user")
    return JSONResponse(content={"scan_id": sid, "status": "cancelled"})

@app.put("/api/library/scan/{scan_id}/pause")
async def api_library_scan_pause(scan_id: str, _=Depends(_require_write_auth)):
    """Pause a running library scan."""
    try:
        sid = int(scan_id)
    except ValueError:
        return JSONResponse(content={"error": "Invalid scan ID"}, status_code=400)
    if _libpipe.pause_scan(sid):
        return JSONResponse(content={"scan_id": sid, "status": "paused"})
    return JSONResponse(content={"error": "Scan not found or not running"}, status_code=404)

@app.put("/api/library/scan/{scan_id}/resume")
async def api_library_scan_resume(scan_id: str, _=Depends(_require_write_auth)):
    """Resume a paused library scan.

    If the scan task is still alive in this process, just flip the DB flag.
    If the task died (e.g. container restart), spawn a fresh scan reusing the
    same scan_id — _process_file skips already-done files, so this safely picks
    up where the old scan left off without redoing completed work.
    """
    try:
        sid = int(scan_id)
    except ValueError:
        return JSONResponse(content={"error": "Invalid scan ID"}, status_code=400)

    # If the actual asyncio task is still alive in this process → just unpause it
    live_entry = _lib_active_scans.get(sid)
    live_task = live_entry.get("task") if isinstance(live_entry, dict) else None
    if live_task is not None and not live_task.done():
        _libpipe.resume_scan(sid)
        return JSONResponse(content={"scan_id": sid, "status": "running"})

    # No live task (restart or dead task) → verify the scan exists and isn't finished
    scan = _libdb.get_scan(sid)
    if not scan:
        return JSONResponse(content={"error": "Scan not found"}, status_code=404)
    if scan.get("status") in ("completed", "failed", "cancelled"):
        return JSONResponse(content={"error": "Scan not found or not paused"}, status_code=404)

    lib_cfg = _subber_config.get_section("library")
    max_concurrent = lib_cfg.get("max_concurrent", 2)
    drift_threshold = lib_cfg.get("drift_threshold_ms", 200)

    _libdb.update_scan(sid, status="running", error_message=None)
    _lib_active_scans[sid] = {"status": "running", "files_total": 0, "files_processed": 0}

    async def _run_resumed_scan():
        async with _lib_scan_lock:
            try:
                await _libpipe.run_scan(
                    scan_id=sid,
                    scan_type="full",  # _process_file skips done files anyway
                    paths=None,
                    dry_run=False,
                    media_types=None,
                    max_concurrent=max_concurrent,
                    drift_threshold_ms=drift_threshold,
                    skip_walk=True,  # DB already has the file list — don't re-walk 24K files
                )
                cur = _libdb.get_scan(sid)
                if cur and cur.get("status") == "paused":
                    _lib_active_scans[sid] = {"status": "paused", **_lib_active_scans.get(sid, {})}
                else:
                    _lib_active_scans[sid] = {"status": "completed", **_lib_active_scans.get(sid, {})}
            except Exception as e:
                _lib_active_scans[sid] = {"status": "failed", "error": str(e)}
                _libdb.update_scan(sid, status="failed", error_message=str(e))

    _task = asyncio.create_task(_run_resumed_scan())
    _lib_active_scans[sid]["task"] = _task
    return JSONResponse(content={"scan_id": sid, "status": "running"})

@app.get("/api/library/status")
async def api_library_status():
    """Get aggregate library statistics."""
    _libdb.init_db()
    stats = _libdb.get_stats()
    # Include active scan ID for cancel button
    active = _libdb.get_active_scan()
    if active:
        stats['active_scan_id'] = active['id']
        stats['active_scan_status'] = active['status']
    return JSONResponse(content=stats)

@app.get("/api/library/files")
async def api_library_files(
    status: str = "all",
    media_type: str = "all",
    page: int = 1,
    limit: int = 50,
    sort: str = "updated_at",
    order: str = "desc",
    search: str = "",
    action: str = "all",
):
    """Query library files with filtering, sorting, and pagination."""
    _libdb.init_db()
    result = _libdb.query_files(
        status=status,
        media_type=media_type,
        page=page,
        limit=limit,
        sort=sort,
        order=order,
        search=search if search else None,
        action=action if action else None,
    )
    return JSONResponse(content=result)

@app.post("/api/library/retry")
async def api_library_retry(request: Request, _=Depends(_require_write_auth)):
    """Retry processing for specific file IDs — actually processes them."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    file_ids = body.get("file_ids", [])
    dry_run = body.get("dry_run", False)

    if not file_ids:
        return JSONResponse(content={"error": "No file IDs provided"}, status_code=400)

    # Reset files to pending and queue them for processing
    results = []
    records_to_process = []
    for fid in file_ids:
        record = _libdb.get_file(fid)
        if record:
            _libdb.update_file_status(fid, status="pending", error_message="")
            records_to_process.append(record)
            results.append({"id": fid, "status": "queued"})

    # If not dry-run, process the files in background
    if not dry_run and records_to_process:
        lib_cfg = _subber_config.get_section("library")
        max_concurrent = lib_cfg.get("max_concurrent", 2)
        drift_threshold = lib_cfg.get("drift_threshold_ms", 200)

        async def _process_retries():
            try:
                print(f'[LIBRARY] Background retry started for {len(records_to_process)} files', flush=True)
                # Re-mount shares — container restarts clear CIFS mounts and a
                # retry against a dead mount fails deep inside ffmpeg/shutil with
                # confusing ENOENT errors (this bit us once: .subber_* move died).
                mount_errors = _libpipe._mount_shares(_libpipe._get_mounts())
                if mount_errors:
                    print(f'[LIBRARY] Retry mount errors: {mount_errors}', flush=True)
                semaphore = asyncio.Semaphore(max_concurrent)
                tasks = [
                    _libpipe._process_file_with_semaphore(
                        semaphore, rec, 0, drift_threshold
                    )
                    for rec in records_to_process
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for i, r in enumerate(results):
                    if isinstance(r, Exception):
                        print(f'[LIBRARY] Error processing {records_to_process[i].get("file_path","?")}: {r}', flush=True)
                    else:
                        print(f'[LIBRARY] Done processing: {r}', flush=True)
            except Exception as e:
                print(f'[LIBRARY] Background retry task FAILED: {e}', flush=True)
                import traceback; traceback.print_exc()

        asyncio.create_task(_process_retries())

    return JSONResponse(content={"retried": len(results), "results": results})

@app.post("/api/library/reset")
async def api_library_reset(_=Depends(_require_write_auth),):
    """Reset library statistics — clears DB records but keeps subtitle files."""
    try:
        _libdb.init_db()
        import sqlite3
        conn = _libdb._connect()
        try:
            deleted = conn.execute("DELETE FROM library_files").rowcount
            conn.execute("DELETE FROM scan_history")
            # Also delete orphaned SRT/ASS files next to videos (optional? no — too dangerous)
            conn.commit()
            return JSONResponse(content={"status": "ok", "deleted": deleted})
        finally:
            conn.close()
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


# ═══════════════════════════════════════════════
# Library DB backups — list, create, restore, import, download
# ═══════════════════════════════════════════════

@app.get("/api/library/backups")
async def api_library_backups():
    """List all DB backups (auto + manual + pre_restore snapshots)."""
    try:
        return JSONResponse(content={"backups": _libdb.list_backups(),
                                     "keep": _libdb.BACKUP_KEEP})
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.post("/api/library/backups")
async def api_library_backup_create(_=Depends(_require_write_auth)):
    """Create a manual backup of the library DB."""
    try:
        info = _libdb.create_backup(kind="manual")
        return JSONResponse(content={"status": "ok", **info})
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.post("/api/library/backups/restore")
async def api_library_backup_restore(request: Request, _=Depends(_require_write_auth)):
    """Restore a named backup over the live DB.

    Safety: a pre_restore snapshot of the CURRENT db is taken first, so this
    can always be undone by restoring that snapshot.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = body.get("name", "")
    if not name:
        return JSONResponse(content={"error": "Missing backup name"}, status_code=400)
    try:
        result = _libdb.restore_backup(name)
        return JSONResponse(content={"status": "ok", **result})
    except ValueError as e:
        return JSONResponse(content={"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.get("/api/library/backups/download")
async def api_library_backup_download(name: str = ""):
    """Download a backup file by name (validated against the backup pattern)."""
    import re as _re
    if not name or not _libdb._BACKUP_NAME_RE.match(name):
        return JSONResponse(content={"error": "Invalid backup name"}, status_code=400)
    path = _libdb.BACKUP_DIR / name
    if not path.is_file():
        return JSONResponse(content={"error": "Backup not found"}, status_code=404)
    return FileResponse(path=str(path), filename=name,
                        media_type="application/octet-stream")


@app.post("/api/library/backups/import")
async def api_library_backup_import(file: UploadFile, _=Depends(_require_write_auth)):
    """Import an external Subber DB file: validate, snapshot current, replace.

    Accepts any valid library DB (not just our backup naming) so users can
    bring a db copied from another install. Same safety net as restore:
    a pre_restore snapshot is taken before the import takes effect.
    """
    if not file.filename or not file.filename.lower().endswith((".db", ".sqlite", ".sqlite3")):
        return JSONResponse(
            content={"error": "Upload must be a .db / .sqlite file"}, status_code=400)

    _libdb.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime as _dt
    ts = _dt.now().strftime("%Y%m%d_%H%M%S")
    staged = _libdb.BACKUP_DIR / f"_staged_{ts}.db"
    try:
        content = await file.read()
        if len(content) < 100:
            return JSONResponse(content={"error": "File too small to be a database"},
                                status_code=400)
        staged.write_bytes(content)

        ok, msg = _libdb.validate_backup_file(staged)
        if not ok:
            staged.unlink(missing_ok=True)
            return JSONResponse(content={"error": msg}, status_code=400)

        # Safety net before the import overwrites anything
        snapshot = _libdb.create_backup(kind="pre_restore")

        # Replace the live DB with the imported one
        import sqlite3 as _sq
        src = _sq.connect(str(staged), timeout=30)
        try:
            dst = _sq.connect(str(_libdb.DB_PATH), timeout=30)
            try:
                with _libdb._lock:
                    src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        # Archive the import under the standard naming so it shows in the list
        imported = _libdb.BACKUP_DIR / f"library_{ts}_imported.db"
        staged.rename(imported)
        conn = _sq.connect(str(_libdb.DB_PATH), timeout=30)
        try:
            rows = conn.execute("SELECT COUNT(*) FROM library_files").fetchone()[0]
        finally:
            conn.close()

        return JSONResponse(content={
            "status": "ok",
            "imported": imported.name,
            "safety_snapshot": snapshot["name"],
            "file_count": rows,
        })
    except Exception as e:
        staged.unlink(missing_ok=True)
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.delete("/api/library/backups/{name}")
async def api_library_backup_delete(name: str, _=Depends(_require_write_auth)):
    """Delete one backup by name."""
    if not _libdb._BACKUP_NAME_RE.match(name):
        return JSONResponse(content={"error": "Invalid backup name"}, status_code=400)
    path = _libdb.BACKUP_DIR / name
    if not path.is_file():
        return JSONResponse(content={"error": "Backup not found"}, status_code=404)
    try:
        path.unlink()
        return JSONResponse(content={"status": "ok", "deleted": name})
    except OSError as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.post("/api/library/config")
async def api_library_config(request: Request, _=Depends(_require_write_auth)):
    """Update library configuration."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    try:
        updated = _subber_config.update("library", body)
        return JSONResponse(content={"status": "ok", "library": updated})
    except KeyError as e:
        return JSONResponse(content={"error": str(e)}, status_code=400)


@app.get("/api/library/mounts")
async def api_library_mounts():
    """List configured SMB mounts."""
    lib_cfg = _subber_config.get_section("library") or {}
    mounts = lib_cfg.get("mounts", [])
    # Mask passwords in response
    safe = []
    for m in mounts:
        m2 = dict(m)
        if m2.get("password"):
            m2["password"] = "********"
        safe.append(m2)
    return JSONResponse(content={"mounts": safe})


@app.post("/api/library/mounts")
async def api_library_mounts_save(request: Request, _=Depends(_require_write_auth)):
    """Save SMB mount configuration.

    Preserves existing passwords when the client sends a blank or masked
    value (the UI clears masked '********' → '' on load; saving without the
    user re-entering the password must not wipe the real credential).
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(content={"error": "Invalid JSON"}, status_code=400)
    new_mounts = body.get("mounts", [])
    try:
        # Reload from disk so the guard sees the freshest state (the mount
        # endpoint is infrequently called; a stale cache here could re-wipe
        # a password that was just restored externally).
        _subber_config.reload()
        lib_cfg = _subber_config.get_section("library") or {}
        old_mounts = {_mount_key(m): m for m in lib_cfg.get("mounts", [])
                      if _mount_key(m)}
        restored = 0
        for nm in new_mounts:
            pw = nm.get("password", "")
            if not pw or pw == "********":
                key = _mount_key(nm)
                if key and key in old_mounts and old_mounts[key].get("password"):
                    nm["password"] = old_mounts[key]["password"]
                    restored += 1
        if restored:
            print(f"[CONFIG] Restored {restored} mount password(s) from existing config", flush=True)
        _subber_config.update("library", {"mounts": new_mounts})
        return JSONResponse(content={"status": "ok", "count": len(new_mounts)})
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


def _mount_key(m: dict) -> str | None:
    """Stable key for mount identity — server + share uniquely identifies
    a mount regardless of mount_point or display name."""
    srv = (m.get("server") or "").strip()
    shr = (m.get("share") or "").strip()
    if srv and shr:
        return f"{srv}//{shr}"
    return None


@app.post("/api/library/mounts/test")
async def api_library_mounts_test(request: Request, _=Depends(_require_write_auth)):
    """Test an SMB mount connection (temporary mount + unmount)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(content={"error": "Invalid JSON"}, status_code=400)
    import tempfile, os
    mp = tempfile.mkdtemp(prefix="subber-mount-test-")
    try:
        result = subprocess.run(
            ["mount", "-t", "cifs",
             f"//{body['server']}/{body['share']}", mp,
             "-o", f"username={body['username']},password={body['password']},rw,vers=3.0"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            # List files to verify
            files = os.listdir(mp)[:5]
            subprocess.run(["umount", "-l", mp], capture_output=True, timeout=5)
            return JSONResponse(content={"status": "ok", "files": files})
        else:
            return JSONResponse(content={"status": "failed", "error": result.stderr.strip()}, status_code=400)
    except Exception as e:
        subprocess.run(["umount", "-l", mp], capture_output=True, timeout=5)
        return JSONResponse(content={"status": "failed", "error": str(e)}, status_code=500)
    finally:
        try:
            os.rmdir(mp)
        except OSError:
            pass


@app.get("/api/library/cost")
async def api_library_cost():
    """Get cost breakdown by show and by month."""
    _libdb.init_db()
    breakdown = _libdb.get_cost_breakdown()
    return JSONResponse(content=breakdown)


# ── Library report endpoints ──

@app.post("/api/library/report")
async def api_library_generate_report(request: Request, _=Depends(_require_write_auth)):
    """Generate a library report (Markdown) and save it. Returns report content + path."""
    _libdb.init_db()
    try:
        report = _libdb.generate_report()
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

    # Save to /app/data/reports/
    report_dir = Path("/app/data/reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime as dt
    timestamp = dt.now().strftime("%Y%m%d_%H%M%S")
    report_path = report_dir / f"library_report_{timestamp}.md"
    report_path.write_text(report, encoding="utf-8")

    return JSONResponse(content={
        "name": report_path.name,
        "content": report,
        "path": str(report_path),
    })


@app.get("/api/library/reports")
async def api_library_list_reports():
    """List all saved reports."""
    report_dir = Path("/app/data/reports")
    reports = []
    if report_dir.exists():
        for f in sorted(report_dir.glob("library_report_*.md"), reverse=True):
            reports.append({
                "name": f.name,
                "size": f.stat().st_size,
                "modified": f.stat().st_mtime,
            })
    return JSONResponse(content={"reports": reports})


@app.get("/api/library/report/{name}")
async def api_library_get_report(name: str):
    """Get a specific report's content."""
    report_path = Path("/app/data/reports") / name
    if not report_path.exists():
        return JSONResponse(content={"error": "Report not found"}, status_code=404)
    content = report_path.read_text(encoding="utf-8")
    return JSONResponse(content={"name": name, "content": content})

@app.get("/api/library/report/{name}/download")
async def api_library_download_report(name: str):
    """Download a specific report as a file."""
    report_path = Path("/app/data/reports") / name
    if not report_path.exists():
        return JSONResponse(content={"error": "Report not found"}, status_code=404)
    report_content = report_path.read_text(encoding="utf-8")
    return Response(
        content=report_content,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ── OpenSubtitles usage endpoint ──

@app.get("/api/providers/subdl/usage")
async def api_subdl_usage():
    """Get current SubDL download-quota stats (synced from SubDL /api/v2/me).

    Returns downloads-per-day usage — the real limit users should care about
    (free: 50/day, PRO: 2,000/day) — instead of the raw API request count.
    """
    try:
        ps = _subber_config.providers_settings()
        cfg = ps.get("subdl", {})
        from .providers.subdl import SubDLProvider, DOWNLOAD_LIMITS, USAGE_FILE
        prov = SubDLProvider(api_key=cfg.get("api_key", ""), pro_mode=bool(cfg.get("pro_mode")))
        entry = await prov.sync_usage()
        if entry is None:
            # Sync failed — fall back to cached file or tier defaults
            entry = prov._cached_usage()
            if entry is None:
                tier = "pro" if cfg.get("pro_mode") else "free"
                entry = {
                    "used": 0,
                    "limit": DOWNLOAD_LIMITS[tier],
                    "remaining": DOWNLOAD_LIMITS[tier],
                    "synced_at": None,
                }
        return JSONResponse(content={
            "plan": "pro" if cfg.get("pro_mode") else "free",
            "downloads_used_today": entry.get("used", 0),
            "daily_limit": entry.get("limit", DOWNLOAD_LIMITS["pro" if cfg.get("pro_mode") else "free"]),
            "remaining": entry.get("remaining", 0),
            "synced_at": entry.get("synced_at"),
        })
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.get("/api/providers/paid-status")
async def api_paid_status():
    """Combined paid-subscription status for the library banner.

    A "paid" flag is shown when the user has ANY paid subtitle source:
      - SubDL in PRO mode, OR
      - OpenSubtitles on a paid tier (Light or higher), OR
      - OpenSubtitles in VIP tier.
    Returns both the flag and the per-provider breakdown so the banner can
    render a human-readable summary.
    """
    ps = _subber_config.providers_settings()

    subdl_cfg = ps.get("subdl", {})
    subdl_pro = bool(subdl_cfg.get("pro_mode"))

    os_cfg = ps.get("opensubtitles", {})
    os_tier = (os_cfg.get("tier") or "free").lower()
    # Paid tiers = Light or higher, plus VIP (which is a paid .org subscription).
    _OS_PAID_TIERS = {"lite", "startup", "basic", "premium", "pro", "vip"}
    os_paid = os_tier in _OS_PAID_TIERS

    return JSONResponse(content={
        "paid": subdl_pro or os_paid,
        "subdl": {"pro": subdl_pro},
        "opensubtitles": {"tier": os_tier, "paid": os_paid},
    })


@app.get("/api/providers/opensubtitles/usage")
async def api_opensubtitles_usage():
    """Get current OpenSubtitles usage stats."""
    try:
        ps = _subber_config.providers_settings()
        os_cfg = ps.get("opensubtitles", {})
        from .providers.opensubtitles import OpenSubtitlesProvider
        prov = OpenSubtitlesProvider(
            api_key=os_cfg.get("api_key", ""),
            username=os_cfg.get("username", ""),
            password=os_cfg.get("password", ""),
            tier=os_cfg.get("tier", "free"),
        )
        return JSONResponse(content=prov.get_usage())
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


# Initialize DB on startup
@app.on_event("startup")
async def _init_library_db():
    _libdb.init_db()


# ── Log viewer routes ──

@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    api_key = (config.get().get("ui", {}) or {}).get("api_key", "")
    return HTMLResponse(
        jinja_env.get_template("logs.html").render(api_key=api_key),
        headers=_NO_CACHE_HEADERS,
    )


@app.get("/api/logs")
async def api_logs(
    lines: int = 200,
    search: str = "",
    level: str = "",
):
    """Return recent log lines with optional search and level filter."""
    if not _LOG_FILE.exists():
        return JSONResponse(content={"lines": [], "total": 0, "file": str(_LOG_FILE)})

    try:
        # Read file backwards efficiently
        with open(_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError:
        return JSONResponse(content={"lines": [], "total": 0, "error": "Cannot read log file"})

    # Apply filters
    filtered = []
    for line in all_lines:
        if search and search.lower() not in line.lower():
            continue
        if level and f"[{level.upper()}]" not in line:
            continue
        filtered.append(line.rstrip("\n"))

    total = len(filtered)

    # Return last N lines
    result = filtered[-lines:] if lines > 0 else filtered

    return JSONResponse(content={
        "lines": result,
        "total": total,
        "showing": len(result),
        "file": str(_LOG_FILE),
        "file_size": _LOG_FILE.stat().st_size if _LOG_FILE.exists() else 0,
    })


@app.get("/api/logs/download")
async def api_logs_download():
    """Download the full log file."""
    if not _LOG_FILE.exists():
        return JSONResponse(content={"error": "Log file not found"}, status_code=404)
    return FileResponse(
        path=str(_LOG_FILE),
        filename="subber.log",
        media_type="text/plain",
    )


@app.get("/api/logs/export")
async def api_logs_export():
    """Export FULL log history (current + all rotated daily files) as one file.

    Files are concatenated oldest → newest. TimedRotatingFileHandler names
    rotated files subber.log.YYYY-MM-DD, so a lexicographic sort is a
    chronological sort.
    """
    import io
    buf = io.StringIO()

    # TimedRotatingFileHandler names rotated files subber.log.YYYY-MM-DD,
    # so sorting by the date suffix gives chronological order (oldest first).
    def _rot_key(p):
        return p.name.rsplit(".", 1)[-1]  # YYYY-MM-DD sorts lexicographically

    rotated = sorted(
        [p for p in _LOG_FILE.parent.glob(_LOG_FILE.name + ".*")
         if not p.name.endswith((".gz", ".zip", ".tar", ".db"))],
        key=_rot_key, reverse=True,
    )
    count = 0
    for p in rotated:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                buf.write(f"===== {p.name} =====\n")
                buf.write(f.read())
                buf.write("\n")
            count += 1
        except OSError:
            continue
    if _LOG_FILE.exists():
        try:
            with open(_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                buf.write(f"===== {_LOG_FILE.name} (current) =====\n")
                buf.write(f.read())
            count += 1
        except OSError:
            pass
    if count == 0:
        return JSONResponse(content={"error": "No log files found"}, status_code=404)
    data = buf.getvalue()
    return Response(
        content=data,
        media_type="text/plain",
        headers={"Content-Disposition": "attachment; filename=subber_logs_full.txt"},
    )


def _redact_line(line: str) -> str:
    """Scrub secrets from a line for the diagnostics bundle."""
    import re as _re
    line = _re.sub(r"(?i)(api[_-]?key|password|token|secret|authorization)[\"'\s:=]+[\"']?([A-Za-z0-9_\-\.+/=]{6,})[\"']?",
                   r"\1=[REDACTED]", line)
    line = _re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "[IP]", line)
    line = _re.sub(r"sk-[A-Za-z0-9_\-]{10,}", "[REDACTED]", line)
    return line


@app.get("/api/logs/diagnostics")
async def api_logs_diagnostics(_=Depends(_require_write_auth)):
    """Redacted diagnostics bundle: system info, recent errors, config shape.

    Secrets are masked, IPs scrubbed. Safe to paste into a bug report.
    """
    import platform
    import shutil as _shutil
    from datetime import datetime as _dt, timezone as _tz

    bundle = {
        "generated_at": _dt.now(_tz.utc).isoformat(),
        "system": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
        },
    }

    # Memory / disk
    try:
        with open("/proc/self/status") as f:
            for ln in f:
                if ln.startswith("VmRSS:"):
                    bundle["system"]["rss_kb"] = int(ln.split()[1])
                    break
    except OSError:
        pass
    try:
        usage = _shutil.disk_usage("/app/data")
        bundle["system"]["data_disk_free_mb"] = usage.free // (1024 * 1024)
    except OSError:
        pass

    # Scan state
    try:
        history = library_db.get_scan_history(limit=1)
        status = history[0] if history else None
        bundle["last_scan"] = {k: status.get(k) for k in ("id", "status", "total_files", "processed_files", "failed_files")} if status else None
    except Exception:
        bundle["last_scan"] = None

    # Config shape only — keys masked
    try:
        cfg = config.get()
        providers = cfg.get("providers", {})
        bundle["providers_enabled"] = {
            name: {k: (_mask(str(v)) if k in ("api_key", "vip_api_key", "username", "password", "cookies") else v)
                   for k, v in (p.items() if isinstance(p, dict) else [("enabled", p)])}
            for name, p in providers.items()
        }
        backends = cfg.get("translation", {}).get("backends", [])
        bundle["translation_backends"] = [
            {"name": b.get("name"), "model": b.get("model"),
             "api_base": b.get("api_base"), "api_key": _mask(str(b.get("api_key", "")))}
            for b in backends
        ]
    except Exception as e:
        bundle["config_error"] = str(e)

    # Recent errors/warnings from the log (redacted)
    errors = []
    if _LOG_FILE.exists():
        try:
            with open(_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            for ln in reversed(lines):
                if "[ERROR]" in ln or "[WARNING]" in ln:
                    errors.append(_redact_line(ln.rstrip()))
                    if len(errors) >= 50:
                        break
        except OSError:
            pass
    bundle["recent_errors_warnings"] = errors

    # Lifecycle history: restarts + last shutdown reason (the thing that was
    # invisible during the 07:53 incident — we couldn't tell WHY it restarted).
    try:
        events = _read_lifecycle_history()
        boots = [e for e in events if e.get("kind") == "boot"]
        shutdowns = [e for e in events if e.get("kind") == "shutdown"]
        bundle["lifecycle"] = {
            "boot_count": len(boots),
            "last_boot": boots[-1].get("ts") if boots else None,
            "last_shutdown": shutdowns[-1].get("ts") if shutdowns else None,
            "shutdown_count": len(shutdowns),
            # A boot with no preceding shutdown = hard kill (OOM/SIGKILL/crash)
            "last_event_was_boot_without_shutdown": bool(boots) and (
                not shutdowns or boots[-1].get("ts") > shutdowns[-1].get("ts")
            ),
            "recent_events": events[-20:],
        }
    except Exception as e:
        bundle["lifecycle_error"] = str(e)

    # Hung-file history: any files currently stuck 'in_progress' > 30 min
    try:
        stale = library_db.get_stale_in_progress(minutes=30)
        bundle["hung_files"] = [
            {"id": s.get("id"), "minutes_stale": s.get("minutes_stale"),
             "path": _redact_line(str(s.get("file_path", "?")))}
            for s in (stale or [])
        ][:20]
    except Exception as e:
        bundle["hung_files_error"] = str(e)

    return JSONResponse(content=bundle)


@app.get("/api/logs/stats")
async def api_logs_stats():
    """Return today's provider API call stats."""
    from .providers import provider_stats
    from .providers.opensubtitles import OpenSubtitlesProvider, USAGE_FILE
    today_stats = provider_stats.get_today_stats()
    all_stats = provider_stats.get_stats(days=7)

    # OpenSubtitles download counts tracked by provider_stats can drift from the
    # API's real quota (the API returns 406 with the authoritative number, which
    # the provider syncs into its usage file). Override with the synced count.
    try:
        usage = json.loads(USAGE_FILE.read_text())
        from datetime import datetime as _dt, timezone as _tz
        today = _dt.now(_tz.utc).strftime("%Y-%m-%d")
        synced = int(usage.get(today, 0))
        if "OpenSubtitles" in today_stats:
            today_stats["OpenSubtitles"]["downloads"] = synced
        for date_str, providers in all_stats.items():
            if "OpenSubtitles" in providers and date_str in usage:
                providers["OpenSubtitles"]["downloads"] = int(usage[date_str])
    except Exception:
        pass

    return JSONResponse(content={
        "today": today_stats,
        "history": all_stats,
    })


def _record_lifecycle_event(kind: str) -> None:
    """Append a boot/shutdown event to the lifecycle history file.

    JSON-lines, so the diagnostics bundle can count restarts and show the last
    shutdown reason without parsing the rotated log files.
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz
    _LIFECYCLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "ts": _dt.now(_tz.utc).isoformat(),
        "kind": kind,
        "pid": os.getpid(),
    }
    with open(_LIFECYCLE_FILE, "a", encoding="utf-8") as f:
        f.write(_json.dumps(event) + "\n")


def _read_lifecycle_history() -> list[dict]:
    """Read the lifecycle history file (newest last). Returns [] if absent."""
    import json as _json
    if not _LIFECYCLE_FILE.exists():
        return []
    events = []
    try:
        with open(_LIFECYCLE_FILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(_json.loads(line))
                except Exception:
                    continue
    except OSError:
        return []
    return events


@app.on_event("startup")
async def startup():
    # Set up file logging — daily rotation, 45-day retention.
    # (Previously: 5MB size rotation with only 3 backups — lost history fast
    # during busy scans and made debugging OOMs impossible.)
    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.TimedRotatingFileHandler(
        str(_LOG_FILE), when="midnight", interval=1, backupCount=45,
    )
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    fh.setLevel(logging.DEBUG)
    root_logger = logging.getLogger("subber")
    root_logger.addHandler(fh)
    root_logger.setLevel(logging.DEBUG)
    _log.info("File logging started (daily rotation, 45d retention): %s", _LOG_FILE)

    # Record boot in the shutdown-history file so restarts are countable and
    # the diagnostics bundle can show "N boots, last shutdown reason=X".
    try:
        _record_lifecycle_event("boot")
    except Exception as e:
        _log.warning("Could not record boot event: %s", e)

    _load_grab_state()
    asyncio.create_task(_cleanup_expired())
    asyncio.create_task(_stale_in_progress_watchdog())
    asyncio.create_task(_auto_scan_scheduler())


@app.on_event("shutdown")
async def _log_shutdown() -> None:
    """Log shutdown + dump active asyncio tasks.

    A graceful shutdown (SIGTERM/SIGINT → uvicorn exit) lands here and leaves a
    trail; an OOM-kill or SIGKILL does NOT — so the ABSENCE of this line plus a
    fresh "File logging started" on restart is itself the diagnostic signal for
    a hard kill. The task dump shows what was running (e.g. a stuck scan task)
    and where it was awaiting.
    """
    try:
        _log.warning("Application shutting down (graceful) — dumping active asyncio tasks")
        for t in asyncio.all_tasks():
            if t is asyncio.current_task():
                continue
            stack = ""
            try:
                frames = t.get_stack()
                if frames:
                    stack = " | " + " <- ".join(
                        f"{f.f_code.co_name}:{f.f_lineno}" for f in frames[:8]
                    )
            except Exception:
                pass
            _log.warning("  active task: %s%s", t.get_name(), stack)
    except Exception as e:
        _log.warning("Shutdown logging error: %s", e)
    try:
        _record_lifecycle_event("shutdown")
    except Exception:
        pass


async def _stale_in_progress_watchdog() -> None:
    """Periodically reset files stuck 'in_progress' for too long.

    A file legitimately being processed gets its updated_at refreshed as it
    moves through stages. If a row stays 'in_progress' with no update for 30+
    minutes, the owning task is dead (OOM kill, container restart, ffmpeg
    hang) — reset it to 'pending' so it doesn't read as frozen forever and
    gets retried on the next resume.
    """
    while True:
        await asyncio.sleep(60)
        try:
            from . import library_db
            # Log WHICH files are hung (and for how long) before resetting —
            # previously only a count was logged, which gave no clue about
            # what file/provider caused the stall.
            stale = library_db.get_stale_in_progress(minutes=30)
            if stale:
                for s in stale:
                    _log.warning(
                        "Stale-progress watchdog: file_id=%s hung %s min — %s",
                        s.get("id"), s.get("minutes_stale"),
                        sanitize_log(s.get("file_path", "?")),
                    )
            reset = library_db.mark_stale_in_progress(minutes=30)
            if reset:
                _log.warning("Stale-progress watchdog: reset %d hung in_progress file(s)", reset)
        except Exception as e:
            _log.warning("Stale-progress watchdog error: %s", e)


def main():
    """Entry point for `subber-web` command."""
    import uvicorn
    host = os.environ.get("SUBBER_HOST", "0.0.0.0")
    port = int(os.environ.get("SUBBER_PORT", "8676"))
    uvicorn.run("subber.web:app", host=host, port=port, reload=False)
