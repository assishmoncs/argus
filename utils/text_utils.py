"""
Utility functions for text normalization, homoglyph replacement, zero-width stripping,
and obfuscation detection.
"""

import re
import unicodedata

# Compilation of typical emojis/symbols to strip
EMOJI_REGEX = re.compile(
    r"[\u2600-\u27bf\U0001f300-\U0001f9ff\U0001f600-\U0001f64f\U0001f680-\U0001f6ff\U0001fa70-\U0001faff]",
    flags=re.UNICODE,
)

# Zero-width and invisible control characters used to evade filters
ZERO_WIDTH_REGEX = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")

# Homoglyph map: character replacements for lookalikes (latin, cyrillic, greek, leetspeak, etc.)
HOMOGLYPH_MAP = {
    # Cyrillic lookalikes
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya", "і": "i", "ї": "yi", "є": "e", "ѕ": "s", "ј": "j",
    # Greek lookalikes
    "α": "a", "β": "b", "ε": "e", "ζ": "z", "η": "h", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p",
    "τ": "t", "υ": "u", "χ": "x", "ω": "w",
    # Common leetspeak / symbol substitutions
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "8": "b", "$": "s", "@": "a", "!": "i", "|": "i",
}


def strip_zero_width(text: str) -> str:
    """Remove zero-width and invisible formatting characters."""
    if not text:
        return ""
    return ZERO_WIDTH_REGEX.sub("", text)


def remove_emojis(text: str) -> str:
    """Remove emojis and decorative symbols from text."""
    if not text:
        return ""
    return EMOJI_REGEX.sub("", text)


def normalize_unicode(text: str) -> str:
    """Decompose accents and convert fancy styled letters (mathematical bold, script, etc.) to standard ASCII."""
    if not text:
        return ""
    text = strip_zero_width(text)
    # NFKD decomposes characters into base characters and combining characters
    nfkd_form = unicodedata.normalize("NFKD", text)
    # Filter out combining diacritical marks
    decomposed = "".join(c for c in nfkd_form if not unicodedata.combining(c))

    # Replace lookalikes from our map
    result = []
    for char in decomposed:
        char_lower = char.lower()
        if char_lower in HOMOGLYPH_MAP:
            result.append(HOMOGLYPH_MAP[char_lower])
        else:
            result.append(char)

    return "".join(result)


def clean_obfuscation(text: str) -> str:
    """Remove spaces, dots, dashes, underscores and other punctuation used to bypass filters."""
    if not text:
        return ""
    # Retain only letters and numbers
    return re.sub(r"[^a-zA-Z0-9]", "", text)


def collapse_duplicates(text: str) -> str:
    """Collapse consecutive duplicate letters (e.g. 'cooool' -> 'col')."""
    if not text:
        return ""

    result = [text[0]]
    for char in text[1:]:
        if char.lower() != result[-1].lower():
            result.append(char)
    return "".join(result)


def normalize_for_cache(text: str) -> str:
    """Produce a canonical normalized string for AI cache key deduplication."""
    if not text:
        return ""
    cleaned = strip_zero_width(text).strip().lower()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned
