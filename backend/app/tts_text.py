"""German text normalization for speech output (TTS).

The CosyVoice sidecar's vendored text frontend normalizes non-Chinese text with
ENGLISH rules (wetext-EN + inflect), so digits in German replies were spoken as
English words ("3D" -> "three D"). The sidecar therefore runs with
``text_frontend=False`` and this module normalizes German text in the backend
before it reaches any TTS sidecar (engine-agnostic: CosyVoice and Qwen3-TTS).

Only the text sent to TTS is normalized — ``reply_delta``/history keep the
original written form. Rules are conservative: anything not matched passes
through unchanged. Known approximations (documented, deliberate):
- date ordinals use the weak dative form ("am 3. Juli" -> "dritten Juli"),
  which fits the dominant "am/vom/zum" contexts;
- an abbreviation at the very end of a sentence loses its final period
  ("…, usw." -> "… und so weiter") — harmless for TTS;
- times are only expanded with an explicit "Uhr" suffix, so scores ("2:1")
  and verse references stay untouched.
"""

import re

from num2words import num2words

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)\s]+\)")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_ELLIPSIS_MARKER_RE = re.compile(r"\[(?:…|\.\.\.)\]")
_EMPTY_PARENS_RE = re.compile(r"\(\s*\)")
# "10.000" / "1.234,56" — thousands separators (optionally with a decimal part) must be
# handled before the date/ordinal and plain-integer rules see the digits.
_THOUSANDS_RE = re.compile(r"\b(\d{1,3}(?:\.\d{3})+)(?:,(\d+))?\b")
_MONTHS = "Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember"
_DATE_ORDINAL_RE = re.compile(rf"\b(\d{{1,2}})\.\s*(?=(?:{_MONTHS})\b)")
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s+Uhr\b")
_DECIMAL_RE = re.compile(r"\b(\d+),(\d+)\b")
_PERCENT_RE = re.compile(r"(?<=\d)\s*%")
_DEGREE_CELSIUS_RE = re.compile(r"(?<=\d)\s*°\s*C\b")
# Colon-adjacent integers (scores "2:1", verses "3:16", times without "Uhr") stay digits —
# converting one side of the colon would read "zwei:eins".
_INT_RE = re.compile(r"\b(?<!:)\d+\b(?!:)")
# Integers longer than this stay digits — a 20-digit ID read as one number word helps nobody.
_MAX_SPOKEN_INT_DIGITS = 9

_ABBREVIATIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bz\.\s?B\."), "zum Beispiel"),
    (re.compile(r"\bd\.\s?h\."), "das heißt"),
    (re.compile(r"\bu\.\s?a\."), "unter anderem"),
    (re.compile(r"\bca\."), "circa"),
    (re.compile(r"\bbzw\."), "beziehungsweise"),
    (re.compile(r"\busw\."), "und so weiter"),
    (re.compile(r"\bggf\."), "gegebenenfalls"),
    (re.compile(r"\bevtl\."), "eventuell"),
    (re.compile(r"\binkl\."), "inklusive"),
    (re.compile(r"\bmind\."), "mindestens"),
    (re.compile(r"\bmax\."), "maximal"),
    (re.compile(r"\bNr\."), "Nummer"),
    (re.compile(r"\bDr\."), "Doktor"),
    (re.compile(r"\bProf\."), "Professor"),
]


def _int_to_german(value: int) -> str:
    # Years like 1984 are read in the hundreds form ("neunzehnhundertvierundachtzig");
    # num2words alone would produce the unnatural "eintausendneunhundert…".
    if 1100 <= value <= 1999:
        hundreds = num2words(value // 100, lang="de")
        remainder = value % 100
        if remainder:
            return f"{hundreds}hundert{num2words(remainder, lang='de')}"
        return f"{hundreds}hundert"
    return num2words(value, lang="de")


def _spell_digits(digits: str) -> str:
    return " ".join(num2words(int(digit), lang="de") for digit in digits)


def _replace_thousands(match: re.Match[str]) -> str:
    digits = match.group(1).replace(".", "")
    if len(digits) > _MAX_SPOKEN_INT_DIGITS:
        return match.group(0)
    integer = int(digits)
    spoken = num2words(integer, lang="de")
    if match.group(2):
        return f"{spoken} Komma {_spell_digits(match.group(2))}"
    return spoken


def _replace_date_ordinal(match: re.Match[str]) -> str:
    day = int(match.group(1))
    if not 1 <= day <= 31:
        return match.group(0)
    return f"{num2words(day, lang='de', to='ordinal')}n "


def _replace_time(match: re.Match[str]) -> str:
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return match.group(0)
    hour_word = "ein" if hour == 1 else num2words(hour, lang="de")
    if minute == 0:
        return f"{hour_word} Uhr"
    return f"{hour_word} Uhr {num2words(minute, lang='de')}"


def _replace_decimal(match: re.Match[str]) -> str:
    if len(match.group(1)) > _MAX_SPOKEN_INT_DIGITS or len(match.group(2)) > _MAX_SPOKEN_INT_DIGITS:
        return match.group(0)
    return f"{num2words(int(match.group(1)), lang='de')} Komma {_spell_digits(match.group(2))}"


def _replace_int(match: re.Match[str]) -> str:
    if len(match.group(0)) > _MAX_SPOKEN_INT_DIGITS:
        return match.group(0)
    # A decimal deliberately left untouched above must stay intact as a whole.
    before, after = match.string[:match.start()], match.string[match.end():]
    if (len(before) >= 2 and before[-1] == "," and before[-2].isdigit()) or (
        len(after) >= 2 and after[0] == "," and after[1].isdigit()
    ):
        return match.group(0)
    return _int_to_german(int(match.group(0)))


def normalize_for_tts(text: str) -> str:
    """Return ``text`` with numbers/abbreviations/markup expanded to spoken German."""
    result = str(text or "")
    result = _MARKDOWN_LINK_RE.sub(r"\1", result)
    result = _URL_RE.sub(" ", result)
    result = _ELLIPSIS_MARKER_RE.sub(" ", result)
    result = _EMPTY_PARENS_RE.sub(" ", result)
    # Expand units while adjacent numbers are still digits (including decimals).
    result = _PERCENT_RE.sub(" Prozent", result)
    result = _DEGREE_CELSIUS_RE.sub(" Grad Celsius", result)
    result = _THOUSANDS_RE.sub(_replace_thousands, result)
    result = _DATE_ORDINAL_RE.sub(_replace_date_ordinal, result)
    result = _TIME_RE.sub(_replace_time, result)
    result = _DECIMAL_RE.sub(_replace_decimal, result)
    for pattern, replacement in _ABBREVIATIONS:
        result = pattern.sub(replacement, result)
    result = _INT_RE.sub(_replace_int, result)
    return re.sub(r"\s+", " ", result).strip()


def has_speakable_content(text: str) -> bool:
    """True if the text contains anything a TTS could voice (letters or digits).

    With ``text_frontend=False`` the CosyVoice sidecar no longer filters
    punctuation-only input itself, and it rejects empty text with HTTP 400 —
    callers must skip the TTS call for unspeakable segments.
    """
    return bool(re.search(r"[0-9A-Za-zÄÖÜäöüß]", text or ""))
