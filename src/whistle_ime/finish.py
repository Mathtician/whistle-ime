"""What to commit when a key ends a dictation.

When the user types while dictating, the dictation stops and the key decides what happens to the preview (the
text not yet committed). Typing punctuation overrides Whistle's own trailing punctuation: the model guessed
where the utterance ended, and the user, seeing the preview, corrected it. Deleting keys act on the preview as
if it were already text in the field. Commits are always whole words, so the preview always holds whole words
and the last word is never split across committed text and preview.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

PUNCTUATION = ",.;:!?"  # typed marks that replace the preview's trailing punctuation
TRAILING = re.compile(r"[.,;:!?…]+$")
# Ctrl+Backspace's word, as text editors see it: letters, digits and apostrophes ("don't" is one word, but
# "well-known" is two), with any punctuation attached after it. Without a word, the last non-space run.
LAST_WORD = re.compile(r"[\w'’]+[^\w\s]*\s*$")
LAST_RUN = re.compile(r"\S+\s*$")


@dataclass(frozen=True)
class EndKey:
    """The key that ended a dictation, as the front end saw it."""

    name: str  # keysym name: "comma", "BackSpace", "Return", "a", ...
    text: str  # what the key types; "" for control keys and chords
    mods: frozenset  # held modifiers among "shift", "ctrl", "alt", "super"


def continues_dictation(key):
    """Whether, with --interleave, the key ends only a piece of the dictation: it does what it
    does at the end of any dictation (see finish), then dictation goes on."""
    if key is None or key.mods & {"ctrl", "alt", "super"} - ({"ctrl"} if key.name == "BackSpace" else set()):
        return False
    return key.text in tuple(PUNCTUATION) or key.name == "BackSpace"


def finish(preview, key):
    """(text to commit, whether the key is absorbed) for a dictation ended by `key` with `preview` uncommitted.

    An absorbed key has done its job on the preview; otherwise the front end replays it after the commit, so
    it lands after the dictated text.
    """
    if key is None or not preview:
        return preview, False
    chord = key.mods & {"ctrl", "alt", "super"}
    if key.text in tuple(PUNCTUATION) and not chord:
        return TRAILING.sub("", preview) + key.text, True
    if key.name == "BackSpace" and not key.mods:
        return preview[:-1], True
    if key.name == "BackSpace" and key.mods == {"ctrl"}:
        match = LAST_WORD.search(preview) or LAST_RUN.search(preview)
        return (preview[:match.start()] if match else preview), True
    return preview, False
