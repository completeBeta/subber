"""Subber CLI — subtitle grabber and translator."""

import sys
from pathlib import Path

import click

from . import __version__


@click.group()
@click.version_option(__version__, prog_name="subber")
def main() -> None:
    """Subber — grab and translate subtitles using LLMs.

    \b
    Examples:
      subber scan ~/media/anime
      subber translate ~/media/anime/Show.S01E01.mkv
      subber fetch "One Punch Man" -s 1 -e 5
      subber watch ~/media/anime
    """
    pass


@main.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--recursive/--no-recursive", default=True,
    help="Scan subdirectories recursively (default: true)."
)
@click.option(
    "--missing-only/--all", default=True,
    help="Show only files missing English subs (default: true)."
)
@click.option(
    "--json", "output_json", is_flag=True,
    help="Output as JSON for scripting."
)
def scan(path: Path, recursive: bool, missing_only: bool, output_json: bool) -> None:
    """Scan a directory for video files and their subtitle status.

    PATH is a directory containing video files.
    """
    from .scanner import find_missing, scan_directory

    click.echo(f"Scanning {path}...")
    targets = scan_directory(path, recursive=recursive)

    if missing_only:
        targets = find_missing(targets)

    if output_json:
        _output_json(targets)
    else:
        _output_table(targets, missing_only)


@main.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--source-lang", "-s", default="auto",
    help="Source language code (default: auto-detect)."
)
@click.option(
    "--target-lang", "-t", default="en",
    help="Target language code (default: en)."
)
@click.option(
    "--api-base", default="https://api.deepseek.com/v1",
    help="OpenAI-compatible API base URL.",
    envvar="SUBBER_API_BASE",
)
@click.option(
    "--api-key", default="",
    help="API key for the LLM service.",
    envvar="SUBBER_API_KEY",
)
@click.option(
    "--model", "-m", default="deepseek-chat",
    help="Model name."
)
@click.option(
    "--dry-run", is_flag=True,
    help="Show what would be done without translating."
)
def translate(
    path: Path,
    source_lang: str,
    target_lang: str,
    api_base: str,
    api_key: str,
    model: str,
    dry_run: bool,
) -> None:
    """Translate subtitles for a video file or subtitle file.

    PATH can be a video file (looks for adjacent subs) or a .srt/.ass file directly.
    """
    from .parser import detect_language, read_raw_texts

    # Determine if path is a video or sub file
    if path.suffix.lower() in {".srt", ".ass", ".ssa", ".vtt"}:
        sub_path = path
    else:
        # Video file — find adjacent subs
        from .scanner import find_subs
        subs = find_subs(path)
        non_en = [s for s in subs if s.language != "en"]
        if not non_en:
            click.echo("No translatable subtitles found adjacent to this video.")
            sys.exit(1)
        sub_path = non_en[0].path

    # Auto-detect source language
    if source_lang == "auto":
        texts = read_raw_texts(sub_path)
        sample = " ".join(texts[:30])
        source_lang = detect_language(sample)
        click.echo(f"Detected source language: {source_lang}")

    output_path = sub_path.with_stem(f"{sub_path.stem}.{target_lang}")

    if dry_run:
        click.echo(f"Would translate: {sub_path.name}")
        click.echo(f"  Source: {source_lang} → Target: {target_lang}")
        click.echo(f"  API: {api_base} ({model})")
        click.echo(f"  Output: {output_path}")
        return

    click.echo(f"Translating {sub_path.name} ({source_lang} → {target_lang})...")

    from .translator import translate_subtitles
    result = translate_subtitles(
        sub_path, output_path, source_lang, target_lang,
        api_base=api_base, api_key=api_key, model=model,
    )

    click.echo(f"✓ Saved: {result}")


@main.command()
@click.argument("query")
@click.option("--language", "-l", default="en", help="Target language code.")
@click.option("--season", "-s", type=int, help="Season number (for TV shows).")
@click.option("--episode", "-e", type=int, help="Episode number.")
@click.option("--output", "-o", type=click.Path(path_type=Path), help="Output directory.")
def fetch(
    query: str,
    language: str,
    season: int | None,
    episode: int | None,
    output: Path | None,
) -> None:
    """Search and download subtitles from OpenSubtitles.

    QUERY can be a movie/show name or path to a video file.
    """
    from .downloader import OpenSubtitlesClient

    # Check if query is a file path
    query_path = Path(query)
    client = OpenSubtitlesClient()

    try:
        if query_path.is_file():
            click.echo(f"Searching by hash: {query_path.name}")
            results = client.search_by_hash(query_path, languages=language)
        else:
            click.echo(f"Searching by name: {query}")
            results = client.search_by_name(query, languages=language, season=season, episode=episode)

        if not results:
            click.echo("No subtitles found.")
            return

        _display_search_results(results, language)

        # Download the first (best) match
        best = results[0]
        file_id = best["id"]
        filename = best["attributes"]["filename"]

        out_dir = output or Path.cwd()
        out_path = out_dir / filename
        out_path = client.download(file_id, out_path)
        click.echo(f"✓ Downloaded: {out_path}")

    finally:
        client.close()


@main.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--language", "-l", default="en", help="Target language code.")
@click.option("--translate/--no-translate", default=False, help="Translate non-English subs.")
@click.option("--sync/--no-sync", default=True, help="Sync with ffsubsync.")
@click.option("--output", "-o", type=click.Path(path_type=Path), help="Output path.")
def grab(
    path: Path,
    language: str,
    translate: bool,
    sync: bool,
    output: Path | None,
) -> None:
    """One-shot subtitle pipeline: upload a video, get synced subs back.

    PATH is a video file (.mkv, .mp4, etc).
    Pipeline: probe embedded → search providers → download → sync → translate.
    """
    import asyncio
    import tempfile
    asyncio.run(_grab_async(path, language, translate, sync, output))


async def _grab_async(
    video_path: Path, language: str, translate: bool, sync: bool, output: Path | None,
) -> None:
    """Async implementation of grab command."""
    from .config import build_provider_registry, translation_settings

    registry = build_provider_registry()

    click.echo(f"🔍 Probing: {video_path.name}")

    # Step 1: Embedded with smart track selection
    from .providers.embedded import EmbeddedProvider
    embedded = EmbeddedProvider()
    best_embedded, embedded_langs = await embedded.get_embedded_result(video_path)
    sub_path: Path | None = None
    best_filename = ""

    if best_embedded:
        if best_embedded.language == "en":
            click.echo(f"  ✓ Found embedded English subtitle ({best_embedded.release_info})")
        else:
            click.echo(f"  ✓ Found embedded {best_embedded.language.upper()} subtitle ({best_embedded.release_info})")
        
        sub_path = video_path.parent / best_embedded.filename
        sub_path = await embedded.download(best_embedded, sub_path)
        best_filename = best_embedded.filename
        
        if best_embedded.language != "en" and translate:
            click.echo(f"  🌐 Translating {best_embedded.language} → en...")
            ts = translation_settings()
            from .translator import translate_subtitles
            tmp_sub = sub_path
            sub_path = video_path.parent / f"{video_path.stem}.en{Path(best_filename).suffix}"
            translate_subtitles(
                tmp_sub, sub_path, best_embedded.language, "en",
                api_base=ts.get("api_base"), api_key=ts.get("api_key"),
                model=ts.get("model"),
            )
            if tmp_sub != sub_path:
                tmp_sub.unlink(missing_ok=True)
    else:
        # Step 2: Search providers (walk language priority)
        query = video_path.stem
        click.echo(f"  Searching {registry.count} providers for '{query}'...")
        from .config import selection_settings
        lang_priority = selection_settings()["language_priority"]
        results = []
        for try_lang in [language] + [l for l in lang_priority if l != language]:
            results = await registry.search_all(
                query=query, language=try_lang, video_path=video_path,
            )
            if results:
                break

        if not results:
            click.echo("✗ No subtitles found.")
            await registry.close()
            sys.exit(1)

        best = results[0]
        click.echo(f"  ✓ Found via {best.provider}: {best.filename}")

        # Download
        import tempfile
        tmp_dir = Path(tempfile.mkdtemp())
        sub_path = tmp_dir / best.filename
        sub_path = await registry.download(best, tmp_dir)
        best_filename = best.filename

    # Step 3: Sync
    if sync and sub_path:
        click.echo("  🎵 Syncing with ffsubsync...")
        from .syncer import async_sync_apply
        synced_path = sub_path.parent / f"{Path(best_filename).stem}.synced{Path(best_filename).suffix}"
        await async_sync_apply(video_path, sub_path, synced_path)
        sub_path = synced_path
        click.echo("  ✓ Sync complete")

    # Step 4: Final save
    out = output or video_path.parent / (Path(best_filename).name if best_filename else f"{video_path.stem}.en.srt")
    import shutil
    shutil.copy2(sub_path, out)
    click.echo(f"✓ Saved: {out}")

    await registry.close()


@main.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--api-base", default="https://api.deepseek.com/v1",
    help="OpenAI-compatible API base URL.",
    envvar="SUBBER_API_BASE",
)
@click.option(
    "--api-key", default="",
    help="API key.",
    envvar="SUBBER_API_KEY",
)
@click.option(
    "--model", "-m", default="deepseek-chat",
    help="Model name."
)
def watch(path: Path, api_base: str, api_key: str, model: str) -> None:
    """Watch a directory and auto-translate new subtitles.

    Monitors for new files and processes them automatically.
    """
    from watchfiles import watch as watch_files

    click.echo(f"Watching {path} for new files... (Ctrl+C to stop)")

    for changes in watch_files(str(path)):
        for change_type, file_path in changes:
            file_path = Path(file_path)
            if file_path.suffix.lower() in {".srt", ".ass", ".ssa", ".vtt"}:
                from .parser import detect_language, read_raw_texts
                texts = read_raw_texts(file_path)
                sample = " ".join(texts[:30])
                lang = detect_language(sample)

                if lang != "en":
                    click.echo(f"New subtitle detected: {file_path.name} ({lang})")
                    output_path = file_path.with_stem(f"{file_path.stem}.en")
                    from .translator import translate_subtitles
                    translate_subtitles(
                        file_path, output_path, lang, "en",
                        api_base=api_base, api_key=api_key, model=model,
                    )
                    click.echo(f"✓ Translated: {output_path}")


def _output_table(targets, missing_only: bool) -> None:
    """Display scan results as a formatted table."""
    if not targets:
        click.echo("No video files found." if not missing_only else "All files have English subtitles. ✓")
        return

    from .types import SubStatus

    status_icons = {
        SubStatus.FOUND: "✓",
        SubStatus.DOWNLOADED: "↓",
        SubStatus.TRANSLATED: "🌐",
        SubStatus.MISSING: "✗",
        SubStatus.SKIPPED: "—",
    }

    click.echo(f"\n{'STATUS':<8} {'AVAILABLE SUBS':<24} {'FILE'}")
    click.echo("-" * 80)

    for t in targets:
        sub_info = ", ".join(
            f"{s.language}.{s.format.value}" for s in t.existing_subs
        ) or "(none)"
        icon = status_icons.get(t.status, "?")
        click.echo(f"  {icon:<6} {sub_info:<24} {t.path.name}")

    click.echo(f"\n{len(targets)} file(s) missing English subtitles.")


def _output_json(targets) -> None:
    """Output scan results as JSON."""
    import json
    data = [
        {
            "path": str(t.path),
            "status": t.status.value,
            "subs": [
                {"path": str(s.path), "format": s.format.value, "language": s.language}
                for s in t.existing_subs
            ],
        }
        for t in targets
    ]
    click.echo(json.dumps(data, indent=2))


def _display_search_results(results: list[dict], language: str) -> None:
    """Display OpenSubtitles search results."""
    for i, r in enumerate(results[:10], 1):
        attrs = r["attributes"]
        click.echo(
            f"  {i}. [{attrs.get('language', '?')}] "
            f"{attrs.get('filename', 'unknown')} "
            f"({attrs.get('download_count', 0)} downloads)"
        )
