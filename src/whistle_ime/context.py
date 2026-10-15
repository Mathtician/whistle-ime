"""Fitting dictated text to the text already before the cursor: spacing and the first word's casing.

Whistle transcribes each window as a fresh utterance, so the first word of a dictation always comes out
capitalized. Whether it should be depends on what precedes the cursor, which the app may report.
"""
from __future__ import annotations

import re

OPENERS = "([{\"'“‘«¿¡"
CLOSERS = ")]}\"'”’»"
SENTENCE_ENDS = ".!?…"
I_FORMS = {"i", "i'm", "i'll", "i've", "i'd", "i’m", "i’ll", "i’ve", "i’d"}


def unescape(field):
    """Undo the addon's escaping of the context field: \\\\, \\t and \\n."""
    return re.sub(r"\\(.)", lambda m: {"\\": "\\", "t": "\t", "n": "\n"}[m.group(1)], field)


def needs_space(before):
    """Whether dictated text needs a space between it and `before`."""
    return bool(before) and not before[-1].isspace() and before[-1] not in OPENERS


def starts_sentence(before):
    """Whether text after `before` starts a sentence: at the start, after a newline or a sentence end.

    Follows Android's keyboard (AOSP LatinIME CapsModeUtils.getCapsMode, English rules): opening punctuation
    at the cursor is skipped, closing quotes and brackets may follow the terminator, and a period ends a
    sentence unless it ends an abbreviation of letters and single periods ("e.g.", "U.S.") or stands alone.
    Unlike Android it needs no space after the terminator, since the backend adds one.
    """
    text = before.rstrip(OPENERS).rstrip(" \t")
    if not text or text.endswith("\n"):
        return True
    text = text.rstrip(CLOSERS)
    if not text:
        return False
    if text[-1] in "!?…":
        return True
    return text[-1] == "." and _period_ends_sentence(text[:-1])


def _period_ends_sentence(text):
    """Whether a period right after `text` is a full stop; Android's state machine, read backwards."""
    state = "start"
    for c in reversed(text):
        if state == "start":  # just before the period
            if c.isalpha():
                state = "word"
            elif c.isspace():
                return False  # a stand-alone period
            else:
                return True  # several periods or symbols in a row, digits ("3.")
        elif state == "word":  # within the word before the period
            if c == ".":
                state = "period"
            elif not c.isalpha():
                return True  # a word with a full stop
        elif state == "period":  # a period within a potential abbreviation
            if not c.isalpha():
                return True
            state = "letter"
        elif state == "letter":  # a letter within a potential abbreviation
            if c == ".":
                state = "period"
            elif not c.isalpha():
                return False  # an abbreviation
    return state not in ("start", "letter")


def fit_first_word(word, before, keywords=()):
    """The dictation's first word, cased for what precedes it; `before` is None when the app reports nothing.

    Lowercasing undoes Whistle's sentence-initial capital, so it spares words whose capital is their own:
    I and its contractions, all-caps acronyms and the keywords (names the user asked to favour).
    """
    if before is None:
        return word
    letters = [i for i, c in enumerate(word) if c.isalpha()]
    if not letters:
        return word
    first = letters[0]
    if starts_sentence(before):
        return word[:first] + word[first].upper() + word[first + 1:]
    core = re.sub(r"^\W+|\W+$", "", word)
    spared = {w.casefold() for k in keywords for w in k.split()}
    if core.casefold() in I_FORMS or (len(core) > 1 and core.isupper()) or core.casefold() in spared:
        return word
    return word[:first] + word[first].lower() + word[first + 1:]
