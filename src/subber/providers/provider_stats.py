"""Provider API call tracking — counts searches/downloads per provider per day.

Stores stats in /app/data/provider_stats.json as:
  {"YYYY-MM-DD": {"SubDL": {"searches": N, "downloads": N}, ...}}
"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

STATS_FILE = Path("/app/data/provider_stats.json")
_lock = threading.Lock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load() -> dict:
    try:
        if STATS_FILE.exists():
            return json.loads(STATS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save(stats: dict) -> None:
    STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Prune old dates (>14 days)
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")
    stats = {k: v for k, v in stats.items() if k >= cutoff}
    STATS_FILE.write_text(json.dumps(stats, indent=2))


def record_search(provider_name: str) -> None:
    """Record a search API call for a provider."""
    with _lock:
        stats = _load()
        today = _today()
        if today not in stats:
            stats[today] = {}
        if provider_name not in stats[today]:
            stats[today][provider_name] = {"searches": 0, "downloads": 0}
        stats[today][provider_name]["searches"] += 1
        _save(stats)


def record_download(provider_name: str) -> None:
    """Record a download API call for a provider."""
    with _lock:
        stats = _load()
        today = _today()
        if today not in stats:
            stats[today] = {}
        if provider_name not in stats[today]:
            stats[today][provider_name] = {"searches": 0, "downloads": 0}
        stats[today][provider_name]["downloads"] += 1
        _save(stats)


def get_stats(days: int = 7) -> dict:
    """Return stats for the last N days."""
    stats = _load()
    result = {}
    for date_str, providers in sorted(stats.items(), reverse=True)[:days]:
        result[date_str] = dict(providers)
    return result


def get_today_stats() -> dict:
    """Return today's stats only."""
    return _load().get(_today(), {})
