"""The input-method side of whistle-ime, independent of any framework: keys, focus and backend events in;
commits, preedit and forwarded keys out. The IBus engine (ibus_engine.py) is a thin layer over this; the
fcitx5 addon (addon/whistle-ime.cpp) is its C++ twin with a D-Bus trigger instead of a key.

Session states:
  idle -> recording        trigger key, into the focused field
  recording -> finishing   trigger key or Escape (STOP); a plain key also STOPs and is replayed after the
                           final commit, so it lands after the dictated text; a chord (Ctrl/Alt/Super) STOPs
                           and passes straight through, because replayed keys lose their modifiers on GNOME
  recording/finishing -> cancelling
                           focus left the field (CANCEL); it can no longer receive commits, so everything
                           until DONE is dropped
  any -> idle              DONE

A "ui" is one text field's handle, supplied by the framework layer. It has:
  before: the text before the cursor as the app reports it, or None if the app reports nothing
  cursor: the app's last reported cursor rectangle (any comparable value)
  commit(text), preedit(settled, tentative), forward(key), status(message)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from whistle_ime.shadow import Shadow

CHORD_MODS = frozenset({"ctrl", "alt", "super"})
CONTEXT_CHARS = 200


@dataclass(frozen=True)
class Key:
    name: str  # keysym name: "v", "Escape", "BackSpace", "Super_L", ...
    text: str  # what the key types; "" for modifiers, chords and control keys
    mods: frozenset  # held modifiers among "shift", "ctrl", "alt", "super"
    release: bool = False
    modifier: bool = False  # the key itself is a modifier
    raw: tuple = field(default=(), compare=False)  # the framework's own encoding, for forwarding the key


@dataclass(frozen=True)
class Trigger:
    name: str  # lowercase keysym name
    mods: frozenset

    @classmethod
    def parse(cls, spec):
        """"Super+Alt+V" -> Trigger("v", {"super", "alt"})."""
        *mods, name = spec.split("+")
        aliases = {"control": "ctrl", "ctrl": "ctrl", "alt": "alt", "super": "super", "shift": "shift", "meta": "super"}
        parsed = frozenset(aliases[m.strip().lower()] for m in mods)
        assert name.strip(), spec
        return cls(name.strip().lower(), parsed)

    def matches(self, key):
        return key.name.lower() == self.name and key.mods == self.mods


class Frontend:
    def __init__(self, backend, trigger, clock=time.monotonic):
        """backend has start(before), stop() and cancel(); its events come back through on_backend()."""
        self.backend = backend
        self.trigger = trigger
        self.clock = clock
        self.state = "idle"
        self.owner = None  # the ui of the current session
        self.held = []  # plain keys pressed after STOP, replayed in order after the final commit
        self.shadow = Shadow()

    # ---- keys ----------------------------------------------------------------------------------------

    def key(self, ui, key):
        """Handle a key in `ui`; True if it was consumed, False if it goes on to the app."""
        trigger = self.trigger.matches(key)
        if key.release:
            return trigger  # the trigger's release belongs to us too; nothing else is held back
        if self.state == "idle":
            if trigger:
                self.start(ui)
                return True
            self.follow(ui, key)
            return False
        if self.state == "recording":
            if trigger or key.name == "Escape":
                self.stop()
                return True
            if key.modifier:
                return False
            self.stop()
            if key.mods & CHORD_MODS:
                return False
            self.held.append(key)
            return True
        if self.state == "finishing":
            if trigger or key.name == "Escape":
                return True
            if key.modifier or key.mods & CHORD_MODS:
                return False
            self.held.append(key)
            return True
        # cancelling: the field already lost focus; only our own trigger is ours
        return trigger

    def follow(self, ui, key):
        """A key reaching the app while idle: typed text extends the shadow, Backspace shortens it, anything
        else (Enter, arrows, Home, Tab, Escape, chords) may move the cursor or end the line."""
        if not self.shadow.tracks(ui) or key.modifier:
            return
        if key.text and not key.mods & CHORD_MODS:
            self.shadow.append(key.text, self.clock())
        elif key.name == "BackSpace" and not key.mods & CHORD_MODS:
            self.shadow.backspace(self.clock())
        else:
            self.shadow.invalidate()

    # ---- sessions ------------------------------------------------------------------------------------

    def context(self, ui):
        """The text before the cursor: the app's report if it has text; else the shadow if it still describes
        this field; else the app's empty report (the start of a field); else None (unknown)."""
        if ui.before:
            return ui.before[-CONTEXT_CHARS:]
        if self.shadow.usable(ui, ui.cursor) and self.shadow.text:
            return self.shadow.text[-CONTEXT_CHARS:]
        return ui.before

    def start(self, ui):
        before = self.context(ui)  # read before the shadow is restarted below
        if not self.shadow.usable(ui, ui.cursor):
            self.shadow.reset(ui, ui.cursor, self.clock())
        self.owner, self.held, self.state = ui, [], "recording"
        self.backend.start(before)
        ui.status("🎤 Dictating")

    def stop(self):
        self.state = "finishing"
        self.backend.stop()

    def focus_out(self, ui):
        if self.shadow.tracks(ui):
            self.shadow.invalidate()
        if ui is self.owner and self.state in ("recording", "finishing"):
            self.backend.cancel()
            self.end("cancelling")

    def cursor_moved(self, ui):
        if self.shadow.tracks(ui):
            self.shadow.cursor_moved(ui.cursor, self.state != "idle", self.clock())

    def on_backend(self, kind, *fields):
        if kind == "ERROR":
            if self.owner is not None:
                self.owner.status("⚠ " + fields[0])
        elif self.state == "cancelling":
            if kind == "DONE":
                self.state = "idle"
        elif kind == "COMMIT":
            self.owner.commit(fields[0])
            if self.shadow.tracks(self.owner):
                self.shadow.append(fields[0], self.clock())
        elif kind == "PREEDIT":
            self.owner.preedit(*fields)
            self.shadow.touch(self.clock())
        elif kind == "DONE":
            self.end("idle")

    def end(self, next_state):
        """Leave the session: clear the preedit and, unless cancelled, replay held keys."""
        owner, held = self.owner, self.held
        owner.preedit("", "")
        self.held, self.state = [], next_state
        if next_state == "idle":
            self.shadow.touch(self.clock())  # the app is about to redraw after the final commit
            owner.status("")
            for key in held:
                if key.text:
                    owner.commit(key.text)  # typed text as a commit, which every client delivers
                else:
                    owner.forward(key)
                self.follow(owner, key)
