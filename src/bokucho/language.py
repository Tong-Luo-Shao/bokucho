"""Canonical language-region tags and deterministic translation-input matching."""

import re
import unicodedata
from collections.abc import Callable, Iterable
from functools import cache, lru_cache

_SCRIPT_SUBTAG = re.compile(r"^([a-z]{2,8}(?:-[a-z]{3}){0,3})-[a-z]{4}(?=-|$)")
_LEGACY_TAGS = {"zh-hant": "zh-tw", "zh-hans": "zh-cn"}
_KANA = re.compile(r"[\u3040-\u30ff]")
_HANGUL = re.compile(r"[\uac00-\ud7af]")
_HAN = re.compile(r"[\u3400-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")
_LATIN_SOURCES = frozenset({"en", "fr", "de", "es", "pt", "it", "pl", "nl", "tr", "vi", "id", "ms"})
_LATIN_SAMPLE_LETTERS = 20
_LANGUAGE_SAMPLE_SIZE = 2000


@cache
def _detector():
    """Lingua's detector, built on first use: the patcher never loads it."""
    from lingua import Language, LanguageDetectorBuilder

    return LanguageDetectorBuilder.from_languages(
        Language.JAPANESE,
        Language.ENGLISH,
        Language.CHINESE,
        Language.KOREAN,
        Language.FRENCH,
        Language.GERMAN,
        Language.SPANISH,
        Language.PORTUGUESE,
        Language.ITALIAN,
        Language.RUSSIAN,
        Language.UKRAINIAN,
        Language.POLISH,
        Language.DUTCH,
        Language.TURKISH,
        Language.ARABIC,
        Language.THAI,
        Language.VIETNAMESE,
        Language.INDONESIAN,
        Language.MALAY,
        Language.HINDI,
    ).build()


def normalize_language_tag(tag: str) -> str:
    """Use language and region as identity; accept script tags from old projects."""
    code = tag.strip().lower().replace("_", "-")
    return _SCRIPT_SUBTAG.sub(r"\1", _LEGACY_TAGS.get(code, code))


@lru_cache(maxsize=65536)
def detected_language(text: str) -> str:
    detected = _detector().detect_language_of(text)
    if detected is None:
        return "und"
    language = detected.iso_code_639_1.name.lower()
    if language != "zh":
        return language
    converters = _chinese_converters()
    if converters is None:
        return language
    s2t, t2s, _ = converters
    simplified = s2t(text) != text
    traditional = t2s(text) != text
    if simplified and not traditional:
        return "zh-cn"
    if traditional and not simplified:
        return "zh-tw"
    return language


def present_languages(texts: Iterable[str], default_language: str = "") -> list[str]:
    """Find languages represented by meaningful source text without classifying every row."""
    languages: set[str] = set()
    samples: dict[str, list[str]] = {"latin": [], "other": []}
    has_text = False
    configured = normalize_language_tag(default_language)
    for text in texts:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            has_text = True
            if _KANA.search(line):
                languages.add("ja")
                continue
            if _HANGUL.search(line):
                languages.add("ko")
                continue
            letters = sum(character.isalpha() for character in line)
            if _HAN.search(line):
                if letters >= 6:
                    language = _line_language(line, configured.split("-")[0])
                    if language.split("-")[0] == "zh":
                        languages.add(language)
            elif _LATIN.search(line):
                if letters >= _LATIN_SAMPLE_LETTERS:
                    samples["latin"].append(line)
            elif letters >= 6:
                samples["other"].append(line)

    if has_text and configured not in {"", "und"}:
        languages.add(configured)
    for lines in samples.values():
        sample = ""
        for line in lines:
            if sample and len(sample) + len(line) + 1 > _LANGUAGE_SAMPLE_SIZE:
                languages.add(detected_language(sample))
                sample = ""
            sample += f"{line}\n"
        if sample:
            languages.add(detected_language(sample))
    if any(language.startswith("zh-") for language in languages):
        languages.discard("zh")
    return sorted(languages - {"und"})


def matches_language_tag(detected: str, source_lang: str) -> bool:
    """Match a detected language against a BCP 47 source tag."""
    source = normalize_language_tag(source_lang).split("-")
    if source[0] in {"", "und"}:
        return True
    detected = normalize_language_tag(detected)
    if source[0] == "zh":
        if any(region in source for region in ("tw", "hk", "mo")):
            return detected in ("zh-tw", "zh")
        if any(region in source for region in ("cn", "sg")):
            return detected in ("zh-cn", "zh")
    return detected != "und" and detected.split("-")[0] == source[0]


def matches_source_language(text: str, source_lang: str) -> bool:
    """Match a BCP 47 source tag; und leaves input unrestricted."""
    source = normalize_language_tag(source_lang).split("-")[0]
    return matches_language_tag(_line_language(text, source), source_lang)


def _line_language(line: str, source: str) -> str:
    """Read kanji as Japanese in a Japanese project.

    Menus, items and skills are often kanji alone, or kanji with a middle dot
    or a little katakana, which Lingua reads as Chinese; only a form Chinese
    alone writes makes such a line Chinese.
    """
    if (
        source == "ja"
        and _HAN.search(line)
        and not _HANGUL.search(line)
        and not _has_chinese_form(line)
    ):
        return "ja"
    if source in _LATIN_SOURCES and _short_latin(line):
        return source
    return detected_language(line)


def _short_latin(line: str) -> bool:
    """Too few letters for Lingua to tell one Latin-script language from another."""
    letters = [character for character in line if character.isalpha()]
    return 0 < len(letters) < _LATIN_SAMPLE_LETTERS and all(
        unicodedata.name(character, "").startswith("LATIN") for character in letters
    )


@lru_cache(maxsize=65536)
def _has_chinese_form(text: str) -> bool:
    """Two traditional forms Japanese writes differently, or a simplified one CP932 lacks.

    A single old form is one Japanese still writes, as in names. A simplified
    form must also be missing from CP932, as s2t rewrites some forms both
    languages share by context. Without OpenCC nothing is known to be Chinese.
    """
    converters = _chinese_converters()
    if converters is None:
        return False
    s2t, _, t2jp = converters
    han = _HAN.findall(text)
    return sum(t2jp(character) != character for character in han) >= 2 or any(
        s2t(character) != character and not character.encode("cp932", "ignore")
        for character in han
    )


@cache
def _chinese_converters() -> (
    tuple[Callable[[str], str], Callable[[str], str], Callable[[str], str]] | None
):
    try:
        from opencc import OpenCC
    except ImportError:
        return None
    return OpenCC("s2t").convert, OpenCC("t2s").convert, OpenCC("t2jp").convert


def source_language_index(
    records: Iterable[tuple[tuple[str, str], str]],
    default_language: str = "",
) -> tuple[list[str], dict[tuple[str, str], frozenset[str]]]:
    """Classify source units once so queue filtering only checks cached tags."""
    loaded = list(records)
    source = normalize_language_tag(default_language).split("-")[0]
    languages = present_languages((text for _, text in loaded), default_language)
    chinese_regions = {language for language in languages if language.startswith("zh-")}
    index: dict[tuple[str, str], frozenset[str]] = {}
    for key, text in loaded:
        found = {
            _line_language(line.strip(), source)
            for line in text.splitlines()
            if line.strip()
        } - {"und"}
        if chinese_regions and "zh" in found:
            found.remove("zh")
            found.update(chinese_regions)
        index[key] = frozenset(found)
    return languages, index
