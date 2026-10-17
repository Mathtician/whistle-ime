"""Shadow: the text before the cursor in a field whose app does not report it (Python twin of addon/shadow.h).

Terminals report no surrounding text (Konsole deliberately, since konsole!800), so a dictation continuing a
sentence would start capitalized. The shadow reconstructs the text before the cursor from what we committed
there and the keys typed since. An input method cannot tell fields apart within one input context, so "the
same field" means an unbroken stretch: the shadow dies on focus out, on any key that can move the cursor or
end the line, and when the app's cursor moves while we didn't cause it (a click). A cursor move within a
second of our own change is the app redrawing after it, so it is followed rather than fatal.
"""
from __future__ import annotations

KEEP = 400  # characters; only the tail matters for spacing and casing
GRACE = 1.0  # seconds after our own change during which a cursor move is the app redrawing


class Shadow:
    def __init__(self):
        self.valid = False
        self.owner = None
        self.text = ""
        self.cursor = None
        self.last_change = 0.0

    def reset(self, owner, cursor, now):
        """Start tracking `owner` from an unknown point: the text before our first commit is not known."""
        self.owner, self.cursor, self.text, self.valid = owner, cursor, "", True
        self.last_change = now

    def invalidate(self):
        self.valid = False

    def tracks(self, owner):
        return self.valid and self.owner is owner

    def usable(self, owner, cursor):
        """Whether the shadow describes this field right now: same owner, cursor where we left it."""
        return self.tracks(owner) and cursor == self.cursor

    def append(self, text, now):
        self.text = (self.text + text)[-KEEP:]
        self.last_change = now

    def backspace(self, now):
        """Backspace removes the last character; past what we know of, the shadow knows nothing."""
        if not self.text:
            self.invalidate()
            return
        self.text = self.text[:-1]
        self.last_change = now

    def cursor_moved(self, cursor, ours, now):
        """The app reported a new cursor position. `ours` is true during a dictation, when every change is ours."""
        if ours or now - self.last_change <= GRACE:
            self.cursor = cursor
        else:
            self.invalidate()

    def touch(self, now):
        self.last_change = now
