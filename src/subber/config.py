"""Persistent configuration — YAML file, web-writable."""
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
    },
    "sync": {
        "engine": "ffsubsync",
        "auto_sync": False,
        "default_offset": 0.0,
        "ffmpeg_path": "ffmpeg",
    },
    "providers": {
        "enabled": ["embedded", "subdl", "addic7ed", "podnapisi", "subscene", "opensubtitles"],
        "subdl": {
            "api_key": "",
            "pro_mode": False,
        },
        "addic7ed": {
            "username": "",
            "password": "",
            "cookies": "",
        },
        "opensubtitles": {
            "api_key": "",
            "vip_api_key": "",
            "username": "",
            "password": "",
            "tier": "free",
            "daily_limit": 0,
        },
        "subscene": {},
        "podnapisi": {},
        "embedded": {},
    },
    "selection": {
        "language_priority": ["en", "ja", "ko", "zh", "fr", "de", "es", "it", "pt", "ru", "ar"],
        "acceptable_track_types": ["dialogue", "sdh"],
        "track_type_priority": ["dialogue", "sdh", "forced", "commentary", "signs"],
    },

    "library": {
        "paths": [],
        "drift_threshold_ms": 200,
        "max_concurrent": 2,
        "scan_interval_hours": 6,
        "dry_run_default": True,
        "providers": {
            "enabled": ["embedded", "subdl", "addic7ed", "subscene"],
            "addic7ed_proxy": "",
        },
    },

    "cost": {
        "input_cost_per_million": 0.14,
        "output_cost_per_million": 0.28,
        "chars_per_token": 2.5,
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


def _load() -> dict:
    """Load config from disk, merging with defaults."""
    if not CONFIG_PATH.exists():
        return dict(DEFAULTS)
    with open(CONFIG_PATH, "r") as f:
        loaded = yaml.safe_load(f) or {}
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


def get() -> dict:
    """Get the full config dict (cached)."""
    global _cache
    if _cache is None:
        _cache = _load()
    return _cache


def reload() -> dict:
    """Force reload from disk."""
    global _cache
    _cache = None
    return get()


def update(section: str, values: dict) -> dict:
    """Update a config section and persist to disk."""
    global _cache
    cfg = get()
    if section not in cfg:
        raise KeyError(f"Unknown config section: {section}")
    # Strip orphaned flat keys when nested keys are provided
    if section == "providers":
        for nested_key in ("subdl", "addic7ed", "opensubtitles"):
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
    from .providers.addic7ed import Addic7edProvider
    from .providers.podnapisi import PodnapisiProvider
    from .providers.subscene import SubsceneProvider
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

    if "addic7ed" in enabled:
        addi_cfg = ps.get("addic7ed", {})
        registry.add(Addic7edProvider(
            username=addi_cfg.get("username", ""),
            password=addi_cfg.get("password", ""),
            cookies=addi_cfg.get("cookies", ""),
        ))

    if "podnapisi" in enabled:
        registry.add(PodnapisiProvider())

    if "subscene" in enabled:
        registry.add(SubsceneProvider())

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
