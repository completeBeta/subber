"""The auto-scan scheduler must honour ``library.auto_scan_type``.

Regression: the scheduler hardcoded ``_launch_library_scan("full")``, so any
interval configured in Settings re-walked the entire library (24K files over
CIFS) instead of picking up just the new files — heavy on the media disks and
the exact complaint that produced this option.
"""
import asyncio

import pytest

from subber import web


class _Stop(Exception):
    """Sentinel raised from the patched sleep to break the scheduler loop."""


def _drive(monkeypatch, *, interval_hours, auto_scan_type="incremental",
           section_missing_key=False, active_scan=None):
    """Run exactly one scheduler tick with stubbed config/DB/sleep."""
    started: list[str] = []

    async def fake_launch(scan_type, *args, **kwargs):
        started.append(scan_type)
        return 7

    section = {"scan_interval_hours": interval_hours}
    if not section_missing_key:
        section["auto_scan_type"] = auto_scan_type

    monkeypatch.setattr(web, "_launch_library_scan", fake_launch)
    monkeypatch.setattr(web._subber_config, "get_section", lambda name: section)
    monkeypatch.setattr(web._libdb, "get_active_scan", lambda: active_scan)

    # monotonic: a big step between calls so the interval always looks elapsed,
    # regardless of how many times the event loop itself samples the clock
    # before the scheduler's own first read.
    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 10 ** 9
        return clock["t"]

    monkeypatch.setattr(web.time, "monotonic", fake_monotonic)

    ticks = {"n": 0}

    async def fake_sleep(_seconds):
        ticks["n"] += 1
        if ticks["n"] > 1:
            raise _Stop()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def drive():
        with pytest.raises(_Stop):
            await web._auto_scan_scheduler()

    asyncio.run(drive())
    return started


def test_scheduler_runs_incremental_when_configured(monkeypatch):
    assert _drive(monkeypatch, interval_hours=24, auto_scan_type="incremental") == ["incremental"]


def test_scheduler_runs_full_when_configured(monkeypatch):
    assert _drive(monkeypatch, interval_hours=24, auto_scan_type="full") == ["full"]


def test_scheduler_defaults_to_incremental_without_config_key(monkeypatch):
    # Existing installs have no auto_scan_type in config.yaml — the scheduler
    # must not silently fall back to the expensive full scan.
    assert _drive(monkeypatch, interval_hours=6, section_missing_key=True) == ["incremental"]


def test_scheduler_rejects_unknown_type(monkeypatch):
    assert _drive(monkeypatch, interval_hours=6, auto_scan_type="weird") == ["incremental"]


def test_scheduler_manual_interval_starts_nothing(monkeypatch):
    assert _drive(monkeypatch, interval_hours=0) == []


def test_scheduler_skips_when_a_scan_is_already_active(monkeypatch):
    active = {"id": 34, "status": "running"}
    assert _drive(monkeypatch, interval_hours=6, active_scan=active) == []
