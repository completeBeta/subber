"""Language detection: the writing system outranks the statistical guess."""
import re

def test_detects_traditional_chinese_instead_of_korean():
    from subber.parser import detect_language
    text = "走過路過不要錯過 大靈玩 一級的駐顏丹啊 靈符法器，應有盡有啊"
    assert detect_language(text) == "zh"


def test_detects_japanese_by_kana():
    from subber.parser import detect_language
    assert detect_language("（スバル）何だよ、それは。リアが来たぞ") == "ja"


def test_detects_korean_by_hangul():
    from subber.parser import detect_language
    assert detect_language("안녕하세요 오늘은 날씨가 좋네요 정말 감사합니다") == "ko"


def test_falls_back_to_statistics_for_latin_scripts():
    from subber.parser import detect_language
    assert detect_language("Bonjour, je voudrais un café et un croissant s'il vous plaît") == "fr"
