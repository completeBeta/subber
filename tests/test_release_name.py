"""Tests for turning provider release names into readable search results."""
import pytest

from subber.release_name import describe_release, short_label


def test_scene_release_with_sxxeyy():
    info = describe_release(
        "SUBDL::loop-7-kaime-no-akuyaku-reijou-wa-moto-tekikoku-de.srt",
        "7th.Time.Loop.The.Villainess.Enjoys.a.Carefree.Life.Married.to.Her."
        "Worst.Enemy.S01E02.WEB.H264-SKYANiME",
    )
    assert (info["season"], info["episode"]) == (1, 2)
    assert info["is_pack"] is False
    assert short_label(info) == "S01E02"
    assert info["show"].startswith("7th Time Loop")
    assert info["quality"] == "WEB"


def test_season_pack_is_flagged():
    info = describe_release(
        "SUBDL::show.srt",
        "7th.Time.Loop.The.Villainess.S01.WEBRip.NF",
    )
    assert info["is_pack"] is True
    assert "pack" in short_label(info)


def test_complete_season_word():
    info = describe_release("Re.Zero.kara.Hajimeru.Isekai.Seikatsu.S02.COMPLETE.1080p.BluRay.srt")
    assert info["season"] == 2
    assert info["is_pack"] is True


def test_anime_episode_number_in_brackets():
    info = describe_release("[Erai-raws]Loop7-kaimenoAkuyakuReijouwaMotoTekikoku-05.srt")
    assert info["episode"] == 5
    assert info["is_pack"] is False
    assert "Erai" not in info["show"]  # release group stripped


def test_dash_trailing_episode():
    info = describe_release("Some.Show - 12.srt")
    assert info["episode"] == 12
    assert info["show"] == "Some Show"


def test_episode_range_is_a_pack():
    info = describe_release("Kaguya-sama.Love.is.War.S01E01-12.BDRip.srt")
    assert info["is_pack"] is True
    assert info["episodes"] == list(range(1, 13))


def test_number_inside_title_is_not_an_episode():
    """`loop-7-kaime` must not become episode 7."""
    info = describe_release("loop-7-kaime-no-akuyaku-reijou-wa-moto-tekikoku-de.srt")
    assert info["episode"] is None
    assert info["is_pack"] is False


def test_multi_episode_file():
    info = describe_release("Show.Name.S01E01E02.720p.WEB.srt")
    assert info["episodes"] == [1, 2]
    assert info["is_pack"] is True


def test_empty_input_does_not_crash():
    info = describe_release("", "")
    assert info["show"] == "Unknown"
    assert info["is_pack"] is False
    assert short_label(info) == "Unknown episode"


def test_trailing_tag_after_episode_number():
    """`…-01[Crunchyroll]` — the tag must not hide the episode, and must not eat
    the hyphenated title word either."""
    info = describe_release("x.srt", "Loop7-kaimenoAkuyakuReijouwa-01[Crunchyroll]")
    assert info["episode"] == 1
    assert info["show"] == "Loop7-kaimenoAkuyakuReijouwa"


def test_release_group_prefix_stripped_but_hyphenated_title_kept():
    info = describe_release("x.srt", "[Erai-raws]Loop7-kaimenoAkuyakuReijouwa-05")
    assert info["episode"] == 5
    assert info["show"] == "Loop7-kaimenoAkuyakuReijouwa"


def test_episode_word_range_is_a_pack():
    info = describe_release(
        "x.srt",
        "Kaguya Sama Love Is War Season 3 English Subtitles Episode 1 ~ 13/Kaguya-sama Love is War",
    )
    assert info["is_pack"] is True
    assert info["episodes"][0] == 1 and info["episodes"][-1] == 13
    assert "pack" in short_label(info)


def test_folder_style_name_uses_last_segment_as_title():
    info = describe_release("x.srt", "Some Show Season 2 Episode 1 ~ 12/Some Show Name")
    assert info["show"] == "Some Show Name"


def test_full_feature_is_not_an_episode():
    info = describe_release("x.srt", "Kaguya-sama: Love Is War - Stairway to Adulthood - EN - FULL")
    assert info["episode"] is None
    assert info["is_pack"] is False
    assert short_label(info) == "Full feature"


def test_season_zero_is_a_special():
    info = describe_release("x.srt", "Show.S00E05.1080p.WEB-DL")
    assert short_label(info) == "Special 5"


def test_no_markers_still_gives_a_title():
    info = describe_release("Some.Show.2023.1080p.BluRay.x264-GROUP.srt")
    assert info["show"].startswith("Some Show")
    assert info["episode"] is None
    assert info["quality"] == "1080P"

