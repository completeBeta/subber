"""Integration test for grab pipeline and multi-provider system."""

from subber.providers.registry import ProviderRegistry, _merge_results, _normalize
from subber.types import SubtitleResult


# ── _normalize tests ──

def test_normalize_strips_extension():
    """_normalize strips file extension."""
    assert _normalize("Breaking.Bad.S01E01.srt") == "breaking bad s01e01"


def test_normalize_lowercases():
    """_normalize lowercases all text."""
    assert _normalize("Show.Name.S01E01.720p.srt") == "show name s01e01 720p"


def test_normalize_replaces_delimiters():
    """_normalize replaces dots, underscores, hyphens with spaces."""
    assert _normalize("Show_Name-S01E01.720p.srt") == "show name s01e01 720p"


def test_normalize_no_extension():
    """_normalize handles filenames with no extension."""
    assert _normalize("BreakingBad") == "breakingbad"


def test_normalize_multiple_delimiters():
    """_normalize collapses multiple delimiters."""
    assert _normalize("Show..Name...S01E01.srt") == "show name s01e01"


# ── _merge_results tests ──

def test_merge_results_deduplicates():
    """_merge_results removes near-duplicate filenames that normalize to same key."""
    results = [
        SubtitleResult(
            id="1", filename="Breaking.Bad.S01E01.srt",
            language="en", provider="SubDL", downloads=500,
        ),
        SubtitleResult(
            id="2", filename="Breaking.Bad.S01E01.srt",
            language="en", provider="Addic7ed", downloads=300,
        ),
        SubtitleResult(
            id="3", filename="Breaking-Bad-S01E01.srt",
            language="en", provider="Podnapisi", downloads=100,
        ),
    ]
    merged = _merge_results(results)
    # All three normalize to "breaking bad s01e01" — should deduplicate to 1
    assert len(merged) == 1
    assert merged[0].downloads == 500  # highest download count kept


def test_merge_results_preserves_different_shows():
    """_merge_results keeps different shows separate."""
    results = [
        SubtitleResult(
            id="1", filename="Breaking.Bad.S01E01.srt",
            language="en", provider="SubDL", downloads=500,
        ),
        SubtitleResult(
            id="2", filename="Better.Call.Saul.S01E01.srt",
            language="en", provider="SubDL", downloads=300,
        ),
    ]
    merged = _merge_results(results)
    assert len(merged) == 2


def test_merge_empty():
    """_merge_results handles empty list."""
    assert _merge_results([]) == []


def test_merge_single():
    """_merge_results handles single result."""
    result = SubtitleResult(
        id="1", filename="test.srt", language="en", provider="Test",
    )
    assert _merge_results([result]) == [result]


def test_merge_sorts_by_downloads():
    """_merge_results keeps highest-download result when deduplicating."""
    results = [
        SubtitleResult(
            id="low", filename="Show.S01E01.srt",
            language="en", provider="A", downloads=10,
        ),
        SubtitleResult(
            id="high", filename="Show.S01E01.srt",
            language="en", provider="B", downloads=1000,
        ),
        SubtitleResult(
            id="mid", filename="Show_S01E01.srt",
            language="en", provider="C", downloads=100,
        ),
    ]
    merged = _merge_results(results)
    assert len(merged) == 1
    assert merged[0].id == "high"
    assert merged[0].downloads == 1000


def test_merge_preserves_different_languages():
    """_merge_results keeps different language results even for same show."""
    results = [
        SubtitleResult(
            id="1", filename="Show.S01E01.en.srt",
            language="en", provider="SubDL", downloads=500,
        ),
        SubtitleResult(
            id="2", filename="Show.S01E01.fr.srt",
            language="fr", provider="SubDL", downloads=200,
        ),
    ]
    merged = _merge_results(results)
    # Different filenames (en vs fr) means different normalize keys
    assert len(merged) == 2


# ── SubtitleResult tests ──

def test_subtitle_result_creation():
    """SubtitleResult creates with correct fields."""
    r = SubtitleResult(
        id="subdl_12345",
        filename="Show.S01E01.srt",
        language="en",
        provider="SubDL",
        downloads=1000,
        rating=4.5,
        hearing_impaired=False,
        release_info="BluRay",
        metadata={"sd_id": "12345"},
    )
    assert r.id == "subdl_12345"
    assert r.filename == "Show.S01E01.srt"
    assert r.language == "en"
    assert r.provider == "SubDL"
    assert r.downloads == 1000
    assert r.rating == 4.5
    assert r.hearing_impaired is False
    assert r.release_info == "BluRay"
    assert r.metadata["sd_id"] == "12345"


def test_subtitle_result_defaults():
    """SubtitleResult has sensible defaults."""
    r = SubtitleResult(id="1", filename="test.srt", language="en", provider="Test")
    assert r.downloads == 0
    assert r.rating == 0.0
    assert r.hearing_impaired is False
    assert r.release_info == ""
    assert r.metadata == {}


# ── ProviderRegistry tests ──

def test_registry_init_empty():
    """ProviderRegistry starts empty."""
    registry = ProviderRegistry()
    assert registry.count == 0
    assert registry.names == []


def test_registry_init_with_providers():
    """ProviderRegistry accepts providers in constructor."""
    from subber.providers.embedded import EmbeddedProvider
    from subber.providers.podnapisi import PodnapisiProvider

    p1 = EmbeddedProvider()
    p2 = PodnapisiProvider()
    registry = ProviderRegistry([p1, p2])
    assert registry.count == 2
    assert "Embedded" in registry.names
    assert "Podnapisi" in registry.names


def test_registry_add():
    """ProviderRegistry add/get work correctly."""
    from subber.providers.subdl import SubDLProvider

    registry = ProviderRegistry()
    p = SubDLProvider()
    registry.add(p)
    assert registry.count == 1
    assert "SubDL" in registry.names
    assert registry.get("SubDL") is p


def test_registry_remove():
    """ProviderRegistry remove works."""
    from subber.providers.subdl import SubDLProvider

    registry = ProviderRegistry()
    p = SubDLProvider()
    registry.add(p)
    assert registry.count == 1
    registry.remove("SubDL")
    assert registry.count == 0
    assert registry.get("SubDL") is None


def test_registry_remove_nonexistent():
    """ProviderRegistry remove on missing name does not raise."""
    registry = ProviderRegistry()
    registry.remove("Nonexistent")  # should not raise


def test_registry_get_nonexistent():
    """ProviderRegistry get returns None for unknown provider."""
    registry = ProviderRegistry()
    assert registry.get("Nonexistent") is None


def test_registry_duplicate_name_overwrites():
    """Adding a provider with duplicate name overwrites the previous."""
    from subber.providers.subdl import SubDLProvider

    registry = ProviderRegistry()
    p1 = SubDLProvider(api_key="key1")
    p2 = SubDLProvider(api_key="key2")
    registry.add(p1)
    registry.add(p2)
    assert registry.count == 1
    assert registry.get("SubDL") is p2


# ── Provider capabilities tests ──

def test_embedded_provider_capabilities():
    """EmbeddedProvider has correct capabilities."""
    from subber.providers.embedded import EmbeddedProvider

    p = EmbeddedProvider()
    assert p.name == "Embedded"
    assert p.capabilities.free is True
    assert p.capabilities.requires_auth is False
    assert p.capabilities.supports_hash_search is False
    assert p.capabilities.supports_name_search is False
    assert p.capabilities.supports_season_episode is False


def test_subdl_provider_capabilities():
    """SubDLProvider has correct capabilities."""
    from subber.providers.subdl import SubDLProvider

    p = SubDLProvider()
    assert p.name == "SubDL"
    assert p.capabilities.free is True
    assert p.capabilities.requires_auth is True
    assert p.capabilities.supports_hash_search is False
    assert p.capabilities.supports_name_search is True
    assert p.capabilities.supports_season_episode is True


def test_gestdown_provider_capabilities():
    """GestdownProvider has correct capabilities."""
    from subber.providers.gestdown import GestdownProvider

    p = GestdownProvider()
    assert p.name == "Gestdown"
    assert p.capabilities.free is True
    assert p.capabilities.requires_auth is False
    assert p.capabilities.supports_hash_search is False
    assert p.capabilities.supports_name_search is True
    assert p.capabilities.supports_season_episode is True


def test_podnapisi_provider_capabilities():
    """PodnapisiProvider has correct capabilities."""
    from subber.providers.podnapisi import PodnapisiProvider

    p = PodnapisiProvider()
    assert p.name == "Podnapisi"
    assert p.capabilities.free is True
    assert p.capabilities.requires_auth is False
    assert p.capabilities.supports_hash_search is False
    assert p.capabilities.supports_name_search is True
    assert p.capabilities.supports_season_episode is True


def test_all_providers_instantiable():
    """All five providers can be instantiated without errors."""
    from subber.providers.embedded import EmbeddedProvider
    from subber.providers.gestdown import GestdownProvider
    from subber.providers.opensubtitles import OpenSubtitlesProvider
    from subber.providers.podnapisi import PodnapisiProvider
    from subber.providers.subdl import SubDLProvider

    providers = [
        EmbeddedProvider(),
        SubDLProvider(),
        GestdownProvider(),
        PodnapisiProvider(),
        OpenSubtitlesProvider(),
    ]
    names = {p.name for p in providers}
    assert names == {"Embedded", "SubDL", "Gestdown", "Podnapisi", "OpenSubtitles"}
