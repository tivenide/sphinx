"""Gesprochene Textform und Satzsegmentierung.

Ändere hier gezielt:
- maximale Antwortlänge: MAX_SPOKEN_SENTENCES / MAX_SPOKEN_WORDS
- frühe erste Sätze: _extract_spoken_segment und Segment-Konstanten
- Satzenderkennung und Abkürzungen: _find_sentence_end / _NON_TERMINAL_TOKENS
- Opener-Merging und Textbereinigung: _merge_opener_with_following_sentence /
  _sanitize_spoken_text

Dieses Modul entscheidet nur, welcher Text gesprochen wird. LLM-Streaming,
TTS- und PCM-Verarbeitung liegen in speech.py.
"""

import re
import unicodedata

# Feature: Gesprochene Länge und Segment-Tuning.
MAX_SPOKEN_SENTENCES = 4
MAX_SPOKEN_WORDS = 70
# Only the FIRST spoken sentence is split off early (for fast first-audio); everything after it is
# synthesized as ONE call. CosyVoice pays a large fixed cost (~1-1.5s) per synthesis call regardless
# of text length, so splitting a reply into many short sentences makes TTS run slower than real time.
# We only cut at complete sentence boundaries; if the model has not produced a finished, useful
# first sentence yet, the reply is synthesized as one smoother call instead of chopping mid-clause.
FIRST_SEGMENT_MAX_WORDS = 18
# The 2-call seam is itself audible (prosody reset + a dead-air gap whenever the first sentence's
# audio is shorter than the second call's fixed cost — measured 0.2-1.2s holes in tts.log). So the
# split must buy something: short replies are synthesized as ONE smoother call, and stub first
# segments (whose audio would under-run the second call almost surely) are never split off.
# 9 words ≈ 3.2-3.5s of audio — enough to cover the second call's lead-in (~2.6-3.5s incl. its
# first two synthesis chunks), which 6-word first segments (~2.5s) measurably under-ran.
SHORT_REPLY_SINGLE_CALL_CHARS = 120
MIN_FIRST_SEGMENT_WORDS = 9

STANDALONE_OPENERS = {
    "ah",
    "alles klar",
    "einen moment",
    "genau",
    "gerne",
    "gut",
    "gute frage",
    "ich denke kurz darueber nach",
    "ich denke kurz darüber nach",
    "ich sortiere das kurz",
    "ich ueberlege kurz",
    "ich überlege kurz",
    "interessante frage",
    "ja",
    "kein problem",
    "klar",
    "lass mich kurz ueberlegen",
    "lass mich kurz überlegen",
    "lass mich ueberlegen",
    "lass mich überlegen",
    "moment",
    "natuerlich",
    "natürlich",
    "okay",
    "ok",
    "sicher",
    "stimmt",
    "verstehe",
}

# The prompt steers the register, but a small model can still repeat a colloquialism from the
# conversation context. This deterministic guard keeps the prohibited word and its common German
# adjective forms out of both returned reply text and the TTS input. Replacing the stem preserves
# grammatical endings: "geile Idee" becomes "hervorragende Idee".
_PROHIBITED_COLLOQUIALISM_RE = re.compile(
    r"\bgeil(?P<suffix>erem|eren|erer|eres|ere|stem|sten|ster|stes|ste|em|en|er|es|e|st)?\b",
    re.IGNORECASE,
)


def _replace_prohibited_colloquialism(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        source = match.group(0)
        replacement = f"hervorragend{(match.group('suffix') or '').lower()}"
        if source.isupper():
            return replacement.upper()
        if source[0].isupper():
            return replacement.capitalize()
        return replacement

    return _PROHIBITED_COLLOQUIALISM_RE.sub(replace, text)


# Feature: Satzgrenzen, Opener und Textbereinigung.
def _extract_spoken_segment(
    buffer: str,
    final: bool,
    max_words: int = FIRST_SEGMENT_MAX_WORDS,
) -> tuple[str, str]:
    sentence, rest = _pop_leading_sentence(buffer)
    if sentence:
        if _is_standalone_opener(sentence):
            second_sentence, after_second = _pop_leading_sentence(rest)
            if second_sentence:
                combined = _merge_opener_with_following_sentence(sentence, second_sentence)
                if len(combined.split()) <= max_words or final:
                    return combined, after_second
            if final and rest.strip():
                return _merge_opener_with_following_sentence(sentence, rest.strip()), ""
            if final:
                return sentence, rest
            return "", buffer

        if len(sentence.split()) <= max_words or final:
            return sentence, rest

    if final and buffer.strip():
        return buffer.strip(), ""
    return "", buffer


_SENTENCE_END_RE = re.compile(r'[.!?](?:["”\']?)(?:\s+|$)')
# Tokens whose trailing "." is NOT a sentence end: German abbreviations and their parts
# ("z. B.", "Dr.", "ca."). Digits ("am 3. Juli") are handled separately in _find_sentence_end.
_NON_TERMINAL_TOKENS = {
    "z",
    "b",
    "d",
    "h",
    "u",
    "a",
    "ca",
    "bzw",
    "usw",
    "ggf",
    "evtl",
    "inkl",
    "mind",
    "max",
    "nr",
    "dr",
    "prof",
}
_TOKEN_BEFORE_PUNCT_RE = re.compile(r"([0-9A-Za-zÄÖÜäöüß]+)$")


def _find_sentence_end(text: str, start: int = 0) -> int | None:
    """Index just past the next real sentence end, or None.

    Skips periods that terminate ordinals/dates ("am 3. Juli") and known German
    abbreviations ("z. B.", "Dr.") — a TTS call boundary there would render
    sentence-final falling intonation mid-clause.
    """
    for match in _SENTENCE_END_RE.finditer(text, start):
        if text[match.start()] == ".":
            token_match = _TOKEN_BEFORE_PUNCT_RE.search(text, 0, match.start())
            if token_match and token_match.end() == match.start():
                token = token_match.group(1)
                if token.isdigit() or token.lower() in _NON_TERMINAL_TOKENS:
                    continue
        return match.end()
    return None


def _split_sentences(text: str) -> list[str]:
    sentences: list[str] = []
    position = 0
    while (end := _find_sentence_end(text, position)) is not None:
        sentence = text[position:end].strip()
        if sentence:
            sentences.append(sentence)
        position = end
    tail = text[position:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def _drop_unterminated_tail(text: str) -> str:
    """Drop a trailing fragment without sentence-final punctuation.

    A max_tokens cut mid-sentence would otherwise be spoken with a stamped-on
    period and just stop. Only drops when at least one complete sentence
    remains — a fragment-only reply is still better spoken than silence.
    """
    stripped = text.strip()
    if not stripped or re.search(r'[.!?]["”\']?$', stripped):
        return stripped
    last_end = None
    position = 0
    while (end := _find_sentence_end(stripped, position)) is not None:
        last_end = end
        position = end
    if last_end is None:
        return stripped
    return stripped[:last_end].strip()


def _pop_leading_sentence(buffer: str) -> tuple[str, str]:
    end = _find_sentence_end(buffer)
    if end is None:
        return "", buffer
    return buffer[:end].strip(), buffer[end:].lstrip()


def _is_standalone_opener(sentence: str) -> bool:
    normalized = _normalize_opener(sentence)
    return normalized in STANDALONE_OPENERS


def _merge_opener_with_following_sentence(opener: str, following: str) -> str:
    opener = re.sub(r"[.!?…]+$", "", opener.strip()).rstrip(",;:")
    following = following.strip()
    if len(following) >= 2 and following[0].isupper() and following[1].islower():
        following = following[0].lower() + following[1:]
    return f"{opener}, {following}".strip()


def _normalize_opener(sentence: str) -> str:
    text = unicodedata.normalize("NFKC", sentence).lower()
    text = re.sub(r"[.!?…,:;\"'“”„`´\-–—]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _limit_spoken_reply(
    raw_reply: str,
    max_words: int = MAX_SPOKEN_WORDS,
    max_sentences: int = MAX_SPOKEN_SENTENCES,
) -> str:
    reply = _sanitize_spoken_text(raw_reply)
    if not reply:
        return ""

    sentences = _split_sentences(reply)
    selected: list[str] = []
    word_count = 0
    for sentence in sentences:
        words = sentence.split()
        if not words:
            continue
        remaining = max_words - word_count
        if remaining <= 0 or len(selected) >= max_sentences:
            break
        if len(words) > remaining:
            # Chop mid-sentence only when nothing was selected yet (something spoken beats
            # silence); otherwise DROP the overflowing sentence — a word-level cut with a
            # stamped-on period is spoken as speech that just stops.
            if not selected:
                shortened = " ".join(words[:remaining]).rstrip(",;:-")
                selected.append(f"{shortened}.")
            break
        selected.append(sentence)
        word_count += len(words)

    return " ".join(selected).strip()


def _sanitize_spoken_text(raw_text: str) -> str:
    text = str(raw_text or "")
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"[*_`#~]", "", text)
    text = "".join(character for character in text if not unicodedata.category(character).startswith(("So", "Cs")))
    text = _replace_prohibited_colloquialism(text)
    return re.sub(r"\s+", " ", text).strip()
