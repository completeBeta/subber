"""Persistent configuration — YAML file, web-writable."""
import hashlib
import os
from pathlib import Path

import yaml


CONFIG_PATH = Path(os.environ.get("SUBBER_CONFIG", "/app/config/config.yaml"))

DEFAULTS = {
    "translation": {
        "api_base": "http://localhost:11434/v1",
        "api_key": "ollama",
        "model": "translategemma:4b",
        "source_lang": "auto",
        "target_lang": "en",
        "temperature": 0.1,
        "max_tokens": 4096,
        "chunk_size": 20,
        "max_retries": 3,
        "timeout": 120,
        "backends": [],  # list of {name, api_base, api_key, model, priority}
        "glossary": [],  # ["source = target", ...] terminology injected into the prompt
        "wrap_chars": 42,  # subtitle line width in characters; 0 disables wrapping
    },
    "sync": {
        "engine": "ffsubsync",
        "auto_sync": False,
        "default_offset": 0.0,
        "ffmpeg_path": "ffmpeg",
    },
    "providers": {
        "enabled": ["embedded", "subdl", "gestdown", "podnapisi", "opensubtitles"],
        "subdl": {
            "api_key": "",
            "pro_mode": False,
        },
        "gestdown": {},
        "opensubtitles": {
            "api_key": "",
            "vip_api_key": "",
            "username": "",
            "password": "",
            "tier": "free",
            "daily_limit": 0,
        },
        "podnapisi": {},
        "embedded": {},
    },
    "selection": {
        "language_priority": ["en", "ja", "ko", "zh", "fr", "de", "es", "it", "pt", "ru", "ar"],
        "acceptable_track_types": ["dialogue", "sdh"],
        "track_type_priority": ["dialogue", "sdh", "forced", "commentary", "signs"],
    },

    "ad_removal": {
        "mode": "off",           # "off" | "adverts" | "adverts_and_credits"
        "window_seconds": 60,    # intro/outro window scanned for ad/credit lines
        "patterns": [],          # extra case-insensitive regex patterns
    },
    "asr": {
        "mode": "off",               # "off" | "auto"  (auto = grab-tab fallback available)
        "model": "large-v3-turbo",   # faster-whisper size alias or HF repo id
        "language": "auto",          # "auto" or a whisper language code (ja, en, ...)
        "timeout": 600,              # per-request seconds for the transcription endpoint
        "max_audio_seconds": 3600,   # hard safety cap — abort if audio is longer
        "backends": [],              # ordered failover: [{name, url, api_key, model}]
    },

    "library": {
        "paths": [],
        "drift_threshold_ms": 200,
        "max_concurrent": 5,
        "scan_interval_hours": 6,
        # What the auto-scan scheduler runs when the interval elapses:
        #   "incremental" — only files not already in the DB (new additions) [default]
        #   "full"        — re-check the whole tree (still skips files already done)
        "auto_scan_type": "incremental",
        "dry_run_default": True,
        "asr_fallback": False,       # transcribe audio via ASR when no subtitle exists
        "watchdog_timeout_min": 30,  # mark a file failed if stuck in_progress this long
        "providers": {
            "enabled": ["embedded", "subdl", "gestdown"],
        },
    },

    "cost": {
        "input_cost_per_million": 0.14,
        "output_cost_per_million": 0.28,
        "chars_per_token": 2.5,
        # Per-model list prices (USD per million tokens). The estimator uses the
        # entry matching the backend's model id, so switching translation model
        # doesn't silently keep reporting the old model's price. Verified against
        # OpenRouter's published pricing on 2026-10-24; override or add your own
        # under cost.models.<model-id>.
        "models": {
            "tencent/hy-mt2-30b-a3b": {"input_cost_per_million": 0.074, "output_cost_per_million": 0.295},
            "tencent/hy-mt2-7b": {"input_cost_per_million": 0.074, "output_cost_per_million": 0.295},
            "tencent/hy-mt2-1.8b": {"input_cost_per_million": 0.044, "output_cost_per_million": 0.177},
            "meta-llama/llama-3.1-8b-instruct": {"input_cost_per_million": 0.05, "output_cost_per_million": 0.08},
            # Self-hosted Ollama: no metered cost.
            "translategemma:4b": {"input_cost_per_million": 0.0, "output_cost_per_million": 0.0},
        },
        "peak_enabled": False,
        "peak_multiplier": 2.0,
        "peak_hours_utc": [[1,4],[6,10]],
        "timezone": "UTC",
    },

    "ui": {
        "theme": "dark",
        "api_key": "",         # empty = no auth required; set to enable write protection
    },
    "limits": {
        "max_upload_mb": 2048,
        "min_free_disk_mb": 1024,
        "max_sync_concurrent": 1,
    },
}


def _load(blob: bytes | None = None) -> dict:
    """Load config from disk (or from an already-read blob), merging with defaults."""
    if blob is None:
        try:
            blob = CONFIG_PATH.read_bytes()
        except OSError:
            return dict(DEFAULTS)
    loaded = yaml.safe_load(blob.decode("utf-8")) or {}
    return _deep_merge(dict(DEFAULTS), loaded)


def _save(cfg: dict) -> None:
    """Write full config to disk."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)


def _deep_merge(base: dict, overrides: dict) -> dict:
    """Recursively merge overrides into base."""
    result = {}
    for key, value in base.items():
        if key in overrides and isinstance(value, dict) and isinstance(overrides[key], dict):
            result[key] = _deep_merge(value, overrides[key])
        elif key in overrides:
            result[key] = overrides[key]
        else:
            result[key] = value
    for key, value in overrides.items():
        if key not in result:
            result[key] = value
    return result


# ── Public API ──

_cache: dict | None = None
_cache_digest: str | None = None


def _read_digest() -> tuple[bytes | None, str | None]:
    """Config file bytes plus a content digest (None when the file is absent)."""
    try:
        blob = CONFIG_PATH.read_bytes()
    except OSError:
        return None, None
    return blob, hashlib.sha1(blob).hexdigest()


def get() -> dict:
    """Get the full config dict, reloading it when the file's *contents* change.

    The cache exists so every request doesn't re-parse the YAML, but the operator
    also edits config.yaml directly (docker hosts, `vi` over SSH, ansible). A
    plain process-lifetime cache made those edits invisible until a restart: a
    model/backend switch written to the file silently kept running the old
    backend, which is exactly how a two-model comparison turned into the same
    model twice.

    The check is a content digest rather than mtime: mtime only has coarse
    granularity on some filesystems, so two saves inside the same tick would be
    missed. The file is ~2 KB, so reading it per call is cheaper than the bug.
    """
    global _cache, _cache_digest
    blob, digest = _read_digest()
    if _cache is None or digest != _cache_digest:
        _cache = _load(blob)
        _cache_digest = digest
    return _cache


def reload() -> dict:
    """Force reload from disk."""
    global _cache, _cache_digest
    _cache = None
    _cache_digest = None
    return get()


def update(section: str, values: dict) -> dict:
    """Update a config section and persist to disk."""
    global _cache
    cfg = get()
    if section not in cfg:
        raise KeyError(f"Unknown config section: {section}")
    # Strip orphaned flat keys when nested keys are provided
    if section == "providers":
        for nested_key in ("subdl", "gestdown", "opensubtitles"):
            if nested_key in values:
                providers = cfg.get("providers", {})
                providers.pop(f"{nested_key}_api_key", None)
                providers.pop(f"{nested_key}_pro_mode", None)
                providers.pop(f"{nested_key}_cookies", None)
                providers.pop(f"{nested_key}_username", None)
                providers.pop(f"{nested_key}_password", None)
                providers.pop(f"{nested_key}_tier", None)
    cfg[section].update(values)
    _save(cfg)
    _cache = cfg
    _cache_digest = _read_digest()[1]
    return cfg[section]


def get_section(name: str) -> dict:
    """Get a specific config section."""
    return dict(get().get(name, {}))


# Convenience accessors
def translation_settings() -> dict:
    return get_section("translation")


def sync_settings() -> dict:
    return get_section("sync")


def limits_settings() -> dict:
    return get_section("limits")


def providers_settings() -> dict:
    return get_section("providers")


def selection_settings() -> dict:
    return get_section("selection")


def translation_backends() -> list[dict]:
    """Return the ordered list of translation backends.
    
    If 'backends' list is configured, returns those sorted by priority.
    Otherwise returns a single backend from legacy api_base/api_key/model fields.
    """
    ts = translation_settings()
    backends = ts.get("backends", [])
    if backends:
        return sorted(backends, key=lambda b: b.get("priority", 99))
    return [{
        "name": "default",
        "api_base": ts.get("api_base", "http://localhost:11434/v1"),
        "api_key": ts.get("api_key", "ollama"),
        "model": ts.get("model", "translategemma:4b"),
        "priority": 0,
    }]

def library_settings() -> dict:
    return get_section("library")


def ad_removal_settings() -> dict:
    return get_section("ad_removal")


def asr_settings() -> dict:
    return get_section("asr")

def build_provider_registry() -> "ProviderRegistry":
    """Build a ProviderRegistry from current config settings.

    Reloads config from disk first — multi-worker uvicorn keeps per-process
    caches, so provider config changes must be re-read before building.
    """
    reload()
    import os
    from .providers import ProviderRegistry
    from .providers.embedded import EmbeddedProvider
    from .providers.subdl import SubDLProvider
    from .providers.gestdown import GestdownProvider
    from .providers.podnapisi import PodnapisiProvider
    from .providers.opensubtitles import OpenSubtitlesProvider

    ps = providers_settings()
    enabled = ps.get("enabled", [])
    registry = ProviderRegistry()

    if "embedded" in enabled:
        registry.add(EmbeddedProvider())

    if "subdl" in enabled:
        subdl_cfg = ps.get("subdl", {})
        key = subdl_cfg.get("api_key", "") or os.environ.get("SUBDL_API_KEY", "")
        registry.add(SubDLProvider(api_key=key, pro_mode=subdl_cfg.get("pro_mode", False)))

    if "gestdown" in enabled:
        registry.add(GestdownProvider())

    if "podnapisi" in enabled:
        registry.add(PodnapisiProvider())

    if "opensubtitles" in enabled:
        os_cfg = ps.get("opensubtitles", {})
        tier = os_cfg.get("tier", "free")
        # VIP mode uses its OWN api key box (vip_api_key) + username/password
        # login. The REST API still requires the Api-Key header for VIP users —
        # the token only raises the download limit to 1,000/day. .com mode uses
        # the regular api_key. The two never share or overwrite each other.
        api_key = os_cfg.get("vip_api_key", "") if tier == "vip" else os_cfg.get("api_key", "")
        registry.add(OpenSubtitlesProvider(
            api_key=api_key,
            username=os_cfg.get("username", ""),
            password=os_cfg.get("password", ""),
            tier=tier,
            daily_limit=int(os_cfg.get("daily_limit", 0) or 0),
        ))


    return registry
