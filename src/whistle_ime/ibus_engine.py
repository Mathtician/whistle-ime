"""whistle-ime as an IBus engine: the input method GNOME (and Ubuntu) use out of the box.

ibus-daemon starts this process (`--ibus`) when the "Whistle" input source is first used. IBus makes one engine
object per text field; each acts as a "ui" for the shared Frontend (frontend.py), which owns the single
dictation at a time. Typing passes straight through, so with the engine's layout declared as "default" the user
keeps their own keyboard layout; the trigger key belongs to the engine, which GNOME Shell asks about every key
before running its own shortcuts.
"""
from __future__ import annotations

import argparse
import logging
import os
import queue
import threading
from pathlib import Path

import gi

gi.require_version("IBus", "1.0")
from gi.repository import GLib, GObject, IBus  # noqa: E402

from whistle_ime import backend, voting  # noqa: E402
from whistle_ime.frontend import Frontend, Key, Trigger  # noqa: E402
from whistle_ime.whistle import LANGUAGES, Whistle  # noqa: E402

log = logging.getLogger("whistle-ime")
FRONTEND = None  # set in main(), before IBus creates any engine object

MODIFIER_KEYS = {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Super_L", "Super_R", "Meta_L",
                 "Meta_R", "Hyper_L", "Hyper_R", "ISO_Level3_Shift", "ISO_Level5_Shift", "Caps_Lock", "Num_Lock"}


def to_key(keyval, keycode, state):
    mods = set()
    if state & IBus.ModifierType.SHIFT_MASK:
        mods.add("shift")
    if state & IBus.ModifierType.CONTROL_MASK:
        mods.add("ctrl")
    if state & IBus.ModifierType.MOD1_MASK:
        mods.add("alt")
    if state & (IBus.ModifierType.MOD4_MASK | IBus.ModifierType.SUPER_MASK):
        mods.add("super")
    name = IBus.keyval_name(keyval) or ""
    char = IBus.keyval_to_unicode(keyval)
    printable = char >= 0x20 and char != 0x7f and not mods & {"ctrl", "alt", "super"}
    return Key(name, chr(char) if printable else "", frozenset(mods), bool(state & IBus.ModifierType.RELEASE_MASK),
               name in MODIFIER_KEYS, (keyval, keycode, state))


class ThreadBackend:
    """backend.run_session in a worker thread; its events return to the GLib main loop."""

    def __init__(self, hear, keywords, interval):
        self.hear, self.keywords, self.interval = hear, keywords, interval
        self.commands = None

    def start(self, before):
        assert self.commands is None, "one session at a time"
        commands = self.commands = queue.Queue()

        def emit(*fields):
            GLib.idle_add(self._deliver, fields)

        def run():
            try:
                recorder = backend.PwRecorder()
            except OSError as error:
                emit("ERROR", f"cannot record: {error}")
                emit("DONE")
                return
            backend.run_session(self.hear, before, commands, emit, recorder, self.interval, keywords=self.keywords)

        threading.Thread(target=run, daemon=True).start()

    def _deliver(self, fields):
        if fields[0] == "DONE":
            self.commands = None
        FRONTEND.on_backend(*fields)
        return False  # run once

    def stop(self):
        if self.commands is not None:
            self.commands.put("STOP")

    def cancel(self):
        if self.commands is not None:
            self.commands.put("CANCEL")


class WhistleEngine(IBus.Engine):
    """One text field. IBus creates these through the factory registered in main()."""

    __gtype_name__ = "WhistleEngine"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.before = None  # text before the cursor as the app reports it; None if it reports nothing
        self.cursor = None
        self.supports_surrounding = False

    # ---- IBus -> frontend ----

    def do_process_key_event(self, keyval, keycode, state):
        key = to_key(keyval, keycode, state)
        handled = FRONTEND.key(self, key)
        log.debug("key %s mods=%s release=%s state=0x%x -> %s (%s)", key.name, sorted(key.mods), key.release, state,
                  "consumed" if handled else "passed", FRONTEND.state)
        return handled

    def do_enable(self):
        self.get_surrounding_text()  # tells IBus this engine wants surrounding text from now on

    def do_set_capabilities(self, caps):
        self.supports_surrounding = bool(caps & IBus.Capabilite.SURROUNDING_TEXT)
        if not self.supports_surrounding:
            self.before = None

    def do_set_surrounding_text(self, text, cursor_pos, anchor_pos):
        if self.supports_surrounding:
            whole = text.get_text()
            self.before = whole[:cursor_pos]

    def do_set_cursor_location(self, x, y, w, h):
        self.cursor = (x, y, w, h)
        FRONTEND.cursor_moved(self)

    def do_focus_in(self):
        log.debug("focus in: surrounding=%s", self.supports_surrounding)

    def do_focus_out(self):
        log.debug("focus out")
        FRONTEND.focus_out(self)

    def do_disable(self):
        FRONTEND.focus_out(self)  # switching input source mid-dictation: drop the rest, like a focus change

    # ---- frontend -> IBus ----

    def commit(self, text):
        self.commit_text(IBus.Text.new_from_string(text))

    def preedit(self, settled, tentative):
        whole = settled + tentative
        if not whole:
            self.hide_preedit_text()
            return
        text = IBus.Text.new_from_string(whole)
        text.append_attribute(IBus.AttrType.UNDERLINE, IBus.AttrUnderline.SINGLE, 0, len(whole))
        self.update_preedit_text_with_mode(text, len(whole), True, IBus.PreeditFocusMode.CLEAR)

    def forward(self, key):
        self.forward_key_event(*key.raw)

    def status(self, message):
        if message:
            self.update_auxiliary_text(IBus.Text.new_from_string(message), True)
        else:
            self.hide_auxiliary_text()


def main():
    parser = argparse.ArgumentParser(description="whistle-ime IBus engine")
    parser.add_argument("--ibus", action="store_true", help="started by ibus-daemon")
    parser.add_argument("--language", default=None, choices=LANGUAGES)
    parser.add_argument("--keywords", default="", help="Words and phrases to favour, comma separated")
    parser.add_argument("--trigger", default="Super+Alt+V")
    args = parser.parse_args()

    logdir = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "whistle-ime"
    logdir.mkdir(parents=True, exist_ok=True)
    debug = os.environ.get("WHISTLE_IME_DEBUG") == "1"  # logs every key (names only, no text) and state change
    logging.basicConfig(filename=logdir / "engine.log", level=logging.DEBUG if debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    log.info("starting: language=%s trigger=%s", args.language, args.trigger)

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    whistle = Whistle()

    def hear(samples):
        return whistle.words(samples, args.language, keywords)

    global FRONTEND
    FRONTEND = Frontend(ThreadBackend(hear, keywords, voting.INTERVAL_SECONDS), Trigger.parse(args.trigger))

    IBus.init()
    bus = IBus.Bus()
    loop = GLib.MainLoop()
    bus.connect("disconnected", lambda *_: loop.quit())
    factory = IBus.Factory.new(bus.get_connection())
    factory.add_engine("whistle", GObject.type_from_name("WhistleEngine"))
    if args.ibus:
        bus.request_name("org.freedesktop.IBus.Whistle", 0)
    log.info("ready")
    loop.run()


if __name__ == "__main__":
    main()
