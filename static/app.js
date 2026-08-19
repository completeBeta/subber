// Subber — frontend logic (v0.3 — real progress + multi-file + persistence)
const dropZone = document.getElementById("drop-zone");
const fileInput = document.getElementById("file-input");
const fileInfo = document.getElementById("file-info");
const fileName = document.getElementById("file-name");
const fileCount = document.getElementById("file-count");
const btnRemove = document.getElementById("btn-remove");
const btnTranslate = document.getElementById("btn-translate");
const btnCancel = document.getElementById("btn-cancel");
const progressSection = document.getElementById("progress-section");
const progressFill = document.getElementById("progress-fill");
const progressText = document.getElementById("progress-text");
const resultSection = document.getElementById("result-section");
const resultSuccess = document.getElementById("result-success");
const resultError = document.getElementById("result-error");
const resultMsg = document.getElementById("result-msg");
const errorMsg = document.getElementById("error-msg");
const btnDownload = document.getElementById("btn-download");
const historyList = document.getElementById("history-list");
const uploadForm = document.getElementById("upload-form");

let selectedFiles = [];
let currentJobId = null;
let currentBatchId = null;
let pollTimer = null;
let _historyTimer = null;

// ── Drag & Drop (multi-file) ──
dropZone.addEventListener("click", () => fileInput.click());
dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropZone.classList.add("drag-over");
});
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("drag-over");
    handleFiles(Array.from(e.dataTransfer.files));
});
fileInput.addEventListener("change", () => handleFiles(Array.from(fileInput.files)));

btnRemove.addEventListener("click", () => {
    selectedFiles = [];
    fileInput.value = "";
    fileInfo.style.display = "none";
    dropZone.style.display = "block";
    btnTranslate.disabled = true;
});

function handleFiles(files) {
    if (!files || files.length === 0) return;
    selectedFiles = files;
    dropZone.style.display = "none";
    fileInfo.style.display = "block";
    if (files.length === 1) {
        fileName.textContent = files[0].name;
        fileCount.style.display = "none";
    } else {
        fileName.textContent = files.map(f => f.name).join(", ");
        fileCount.textContent = files.length + " files";
        fileCount.style.display = "inline";
    }
    btnTranslate.disabled = false;
}

function resetForm() {
    stopPolling();
    selectedFiles = [];
    currentJobId = null;
    currentBatchId = null;
    fileInput.value = "";
    fileInfo.style.display = "none";
    dropZone.style.display = "block";
    btnTranslate.textContent = "Translate";
    btnTranslate.disabled = true;
    btnTranslate.onclick = startTranslation;
    btnCancel.style.display = "none";
    progressSection.style.display = "none";
    resultSection.style.display = "none";
    btnDownload.style.display = "none";
}

function showError(msg) {
    stopPolling();
    progressSection.style.display = "none";
    resultSection.style.display = "block";
    resultSuccess.style.display = "none";
    resultError.style.display = "flex";
    errorMsg.textContent = msg || "Translation failed.";
    btnTranslate.textContent = "Try Again";
    btnTranslate.disabled = false;
    btnCancel.style.display = "none";
    refreshHistory();
}

function showSuccess(data) {
    resultSection.style.display = "block";
    resultSuccess.style.display = "flex";
    resultError.style.display = "none";
    if (data.batch_id) {
        resultMsg.textContent = `Batch complete — ${data.file_count || "?"} files`;
        btnDownload.href = `/download/batch/${data.batch_id}`;
        btnDownload.style.display = "block";
        btnDownload.textContent = "Download ZIP";
    } else {
        resultMsg.textContent = "Translation complete!";
        btnDownload.href = `/download/${data.job_id || currentJobId}`;
        btnDownload.style.display = "block";
        btnDownload.textContent = "Download Translated File";
    }
}

// ── Upload ──
async function startTranslation() {
    const form = new FormData();
    form.append("source_lang", document.getElementById("source-lang").value);
    form.append("target_lang", document.getElementById("target-lang").value);
    const isMulti = selectedFiles.length > 1;
    if (!isMulti) {
        form.append("file", selectedFiles[0]);
    } else {
        for (const f of selectedFiles) form.append("files", f);
    }

    progressSection.style.display = "block";
    resultSection.style.display = "none";
    progressFill.style.width = "10%";
    progressFill.classList.add("pulse");
    progressText.textContent = "Uploading...";
    btnTranslate.textContent = "Uploading...";
    btnTranslate.disabled = true;
    btnCancel.style.display = "inline-block";

    try {
        const url = isMulti ? "/api/upload-batch" : "/api/upload";
        const resp = await fetch(url, { method: "POST", body: form });
        const data = await resp.json();
        if (data.error) { showError(data.error); return; }

        progressFill.classList.remove("pulse");

        // Batch (multi-file)
        if (data.batch_id) {
            currentBatchId = data.batch_id;
            currentJobId = null;
            btnTranslate.textContent = "Translating...";
            progressText.textContent = `Translating ${data.file_count} files...`;
            progressFill.style.width = "5%";
            pollBatch(currentBatchId);
            return;
        }

        // Single file — already in target language
        if (data.skipped) {
            progressFill.style.width = "100%";
            progressText.textContent = data.reason || "Already in target language";
            btnTranslate.textContent = "Translate Another";
            btnTranslate.disabled = false;
            btnCancel.style.display = "none";
            btnTranslate.onclick = resetForm;
            currentJobId = data.job_id;
            resultSection.style.display = "block";
            resultSuccess.style.display = "flex";
            resultError.style.display = "none";
            resultMsg.textContent = data.reason || "File is already in the target language.";
            btnDownload.href = `/download/${data.job_id}`;
            btnDownload.style.display = "block";
            btnDownload.textContent = "Download Original File";
            refreshHistory();
            return;
        }

        // Single file translation
        currentJobId = data.job_id;
        currentBatchId = null;
        btnTranslate.textContent = "Translating...";
        progressText.textContent = "Translating...";
        progressFill.style.width = "5%";
        pollJob(currentJobId);
    } catch (err) {
        showError("Upload failed. Check connection.");
    }
}

btnTranslate.addEventListener("click", startTranslation);
btnCancel.addEventListener("click", () => {
    stopPolling();
    btnTranslate.textContent = "Translation cancelled";
    btnTranslate.disabled = false;
    btnCancel.style.display = "none";
    progressText.textContent = "Cancelled";
});

// ── Real progress polling (no fake bar) ──
function stopPolling() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
}

async function pollJob(jobId) {
    const poll = async () => {
        try {
            const resp = await fetch(`/api/jobs/${jobId}`);
            const data = await resp.json();
            if (data.error) { showError(data.error); return; }

            if (data.status === "translating") {
                const pct = data.progress_pct || 0;
                progressFill.style.width = Math.max(5, pct) + "%";
                progressText.textContent = `Translating... ${data.chunks_done || 0}/${data.total_chunks || "?"} chunks`;
                pollTimer = setTimeout(poll, 800);
            } else if (data.status === "done") {
                progressFill.style.width = "100%";
                progressText.textContent = "Done!";
                btnTranslate.textContent = "Translate Another";
                btnTranslate.disabled = false;
                btnCancel.style.display = "none";
                btnTranslate.onclick = resetForm;
                showSuccess(data);
                refreshHistory();
            } else if (data.status === "failed") {
                showError(data.error || "Translation failed.");
            }
        } catch (err) {
            showError("Lost connection to server.");
        }
    };
    poll();
}

async function pollBatch(batchId) {
    const poll = async () => {
        try {
            const resp = await fetch(`/api/batch/${batchId}`);
            const data = await resp.json();
            if (data.error) { showError(data.error); return; }

            const done = data.done || 0;
            const total = data.total || 1;
            const pct = (done / total) * 100;
            progressFill.style.width = Math.max(5, pct) + "%";
            progressText.textContent = `Batch: ${done}/${total} files done`;
            btnTranslate.textContent = `Translating... ${done}/${total}`;

            if (done >= total) {
                progressFill.style.width = "100%";
                progressText.textContent = "Done!";
                btnTranslate.textContent = "Translate Another";
                btnTranslate.disabled = false;
                btnCancel.style.display = "none";
                btnTranslate.onclick = resetForm;
                showSuccess(data);
                refreshHistory();
            } else {
                pollTimer = setTimeout(poll, 1500);
            }
        } catch (err) {
            showError("Lost connection to server.");
        }
    };
    poll();
}

// ── History (persistent across page nav) ──
async function refreshHistory() {
    try {
        const resp = await fetch("/api/jobs");
        const data = await resp.json();
        const jobs = data.jobs || [];
        const batches = data.batches || [];

        if (jobs.length === 0 && batches.length === 0) {
            historyList.innerHTML = '<p class="empty">No translations yet.</p>';
            stopHistoryPolling();
            const cr = document.getElementById("history-clear-row");
            if (cr) cr.style.display = "none";
            return;
        }

        let html = "";
        let hasActive = false;

        batches.forEach(b => {
            const age = b.age_hours < 1 ? "just now" : `${b.age_hours.toFixed(1)}h ago`;
            html += `<div class="history-item batch-item">
                <div><strong title="${escapeHtml(b.original_name)}">📦 ${escapeHtml(b.original_name)}</strong>
                <span class="meta">${b.file_count} files · ${b.source_lang} → ${b.target_lang} · ${age}</span></div>
                <div style="display:flex;align-items:center;gap:0.5rem">
                <a href="/download/batch/${b.id}">Download ZIP</a></div></div>`;
        });

        jobs.forEach(j => {
            const cls = j.status === "done" ? "status-done" : j.status === "failed" ? "status-failed" : "status-translating";
            const label = j.status.charAt(0).toUpperCase() + j.status.slice(1);
            const bar = j.status === "translating"
                ? `<div style="width:80px;height:3px;background:rgba(255,255,255,0.1);border-radius:2px;margin:2px 0"><div style="height:100%;background:var(--accent);border-radius:2px;width:${j.progress_pct||0}%;"></div></div>`
                : "";
            const dl = j.status === "done" ? `<a href="/download/${j.id}">Download</a>` : "";
            if (j.status === "translating" || j.status === "pending") hasActive = true;
            html += `<div class="history-item">
                <div><strong title="${escapeHtml(j.original_name)}">${escapeHtml(j.original_name)}</strong>
                <span class="meta">${j.source_lang} → ${j.target_lang}</span>${bar}</div>
                <div style="display:flex;align-items:center;gap:0.5rem">
                <span class="status ${cls}">${label}</span>${dl}</div></div>`;
        });

        historyList.innerHTML = html;

        const cr = document.getElementById("history-clear-row");
        if (cr) cr.style.display = (jobs.length + batches.length > 0) ? "block" : "none";

        if (hasActive && !_historyTimer) _historyTimer = setInterval(refreshHistory, 5000);
        else if (!hasActive) stopHistoryPolling();
    } catch (err) { /* silent */ }
}

function stopHistoryPolling() {
    if (_historyTimer) { clearInterval(_historyTimer); _historyTimer = null; }
}

function clearHistory() {
    historyList.innerHTML = '<p class="empty">No translations yet.</p>';
    const cr = document.getElementById("history-clear-row");
    if (cr) cr.style.display = "none";
    stopHistoryPolling();
}

// ── Restore active job on page load ──
async function restoreActiveJob() {
    try {
        const resp = await fetch("/api/jobs");
        const data = await resp.json();
        const active = (data.jobs || []).find(j => j.status === "translating" || j.status === "pending");
        if (!active) return;

        if (active.batch_id) {
            currentBatchId = active.batch_id;
            currentJobId = null;
            btnTranslate.textContent = "Restoring...";
            btnCancel.style.display = "inline-block";
            progressSection.style.display = "block";
            progressFill.style.width = active.progress_pct + "%";
            progressText.textContent = "Restoring batch...";
            btnTranslate.disabled = true;
            pollBatch(currentBatchId);
        } else {
            currentJobId = active.id;
            currentBatchId = null;
            btnTranslate.textContent = "Restoring...";
            btnCancel.style.display = "inline-block";
            progressSection.style.display = "block";
            progressFill.style.width = active.progress_pct + "%";
            progressText.textContent = "Restoring... " + (active.chunks_done || 0) + " chunks";
            btnTranslate.disabled = true;
            pollJob(currentJobId);
        }
    } catch (err) { /* silent */ }
}

function escapeHtml(text) {
    const d = document.createElement("div");
    d.textContent = text;
    return d.innerHTML;
}

// Init
refreshHistory();
restoreActiveJob();
