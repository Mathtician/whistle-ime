"""Dictation backend for the whistle-ime fcitx5 addon: speech in, preedit and commits out.

The addon starts this process once and keeps it running, so the model stays loaded. During a session it
reads the open 5 s segment every 0.1 s with Whistle and stitches overlapping segments (segments.py). A word
is committed once the segment after the one it was taken from is complete, 3.75 to 6.25 s after it was spoken;
everything after that is shown as preedit and may still change.

Protocol, one message per line, fields separated by tabs (text never contains tabs or newlines):
  addon -> backend
    START <known> <before>
                    begin a session; <known> is 1 if the app reports the text before the cursor, which is then
                    <before> (its last 200 characters, with backslash, tab and newline escaped as \\, \t, \n)
    STOP [<name> <mods> <text>]
                    end the session: one last pass trusting the recording's true end, then commit the rest. If a
                    key ended the dictation, its keysym name, comma-separated modifiers (shift,ctrl,alt,super) and
                    escaped text follow, and finish.py decides what to commit
    CANCEL          end the session, dropping whatever is not committed yet, DONE
  backend -> addon
    READY                            the model is loaded (once, at startup)
    COMMIT <text>                    final text: insert it
    PREEDIT <settled> <tentative>    the uncommitted rest, shown after the committed text; replaces the last one
    DONE <absorbed>                  the session is over; <absorbed> is 1 if the key that ended it was used up
                                     (otherwise the front end replays it after the commit)
    ERROR <message>
STOP and CANCEL outside a session are ignored: the session may have just ended on an error.
"""
from __future__ import annotations

import argparse
import os
import queue
import subprocess
import sys
import threading
import time
import traceback

import numpy

from whistle_ime import recordings, segments
from whistle_ime.context import capitalize, fit_first_word, needs_space, starts_sentence, unescape
from whistle_ime.finish import EndKey, finish
from whistle_ime.whistle import LANGUAGES, Whistle

SAMPLE_RATE = 16000


def _field(text):
    assert "\t" not in text and "\n" not in text, repr(text)
    return text


class Session:
    """One dictation: audio in, COMMIT/PREEDIT messages out. No threads or audio devices; see run_session.

    before is the text before the cursor, or None when the app does not report it. It decides the leading
    space and the first word's casing (see context.py). on_pass(start_sample, end_sample, reading), if given,
    sees every pass of the model (for saving sessions).
    """

    def __init__(self, hear, before, emit, keywords=(), on_pass=None):
        self.hear = hear
        self.emit = emit
        self.before = before
        self.keywords = keywords
        self.on_pass = on_pass
        self.audio = numpy.zeros(0, numpy.float32)  # the recording from sample `offset` on
        self.offset = 0
        self.open_start = 0  # sample where the open segment starts; audio before it is no longer needed
        self.transcript = segments.Transcript()
        self.needs_space = before is not None and needs_space(before)  # the next text starts with a space
        self.committed = 0  # words of transcript.final already committed
        self.preedit = ("", "")
        self.unheard = False  # audio arrived since the last pass

    def heard(self):
        """Samples recorded so far."""
        return self.offset + len(self.audio)

    def append(self, samples):
        if len(samples):
            self.audio = numpy.concatenate([self.audio, numpy.asarray(samples, numpy.float32)])
            self.unheard = True

    def _read(self, start, end):
        assert self.offset <= start < end <= self.heard(), (self.offset, start, end, self.heard())
        reading = segments.read(self.hear, self.audio[start - self.offset:end - self.offset], start)
        if self.on_pass is not None:
            self.on_pass(start, end, reading)
        return reading

    def _fill(self):
        """Final readings of every open segment that has reached full length; each opens the next."""
        while self.heard() - self.open_start >= segments.WINDOW:
            self.transcript.full(self._read(self.open_start, self.open_start + segments.WINDOW))
            self.open_start += segments.STRIDE
            self.audio = self.audio[self.open_start - self.offset:]
            self.offset = self.open_start

    def tick(self):
        """Read the open segment again, unless nothing new arrived."""
        if self.unheard:
            self._fill()
            if self.heard() > self.open_start:
                self.transcript.partial(self._read(self.open_start, self.heard()))
            self.unheard = False
        self.publish()

    def close(self, end_key=None):
        """The last reading, up to the recording's true end; commits the rest as `finish` says for the key that
        ended the dictation. Returns whether that key was absorbed."""
        self._fill()
        if self.heard() > self.open_start:
            self.transcript.close(self._read(self.open_start, self.heard()))
        words = self._words()
        rest = [text for text, _ in words[self.committed:]]
        text, absorbed = finish(self._spaced(" ".join(rest)) if rest else "", end_key)
        if text:
            self.emit("COMMIT", text)
            self.needs_space = True
        self.committed = len(words)
        if self.preedit != ("", ""):
            self.emit("PREEDIT", "", "")
            self.preedit = ("", "")
        return absorbed

    def _words(self):
        """(text, settled) for every word so far: the final ones, then the preview's.

        The first word is fitted to the text before the cursor, and a word after a sentence's end is
        capitalized (segments may disagree on case: one ends "now." and the next goes on "can you"). Each
        depends only on the word itself and the one before, and final words are a prefix that never changes,
        so the preview shows exactly what will be committed.
        """
        settled, tentative = self.transcript.preview()
        words = ([(w.text, True) for w in self.transcript.final] + [(w.text, True) for w in settled]
                 + [(w.text, False) for w in tentative])
        if words:
            words[0] = (fit_first_word(words[0][0], self.before, self.keywords), words[0][1])
        for i in range(1, len(words)):
            if starts_sentence(words[i - 1][0]):
                words[i] = (capitalize(words[i][0]), words[i][1])
        return words

    def publish(self):
        # transcript.final only ever grows at its end, so committing its new tail never revises earlier text.
        final = len(self.transcript.final)
        words = self._words()
        new = [text for text, _ in words[self.committed:final]]
        if new:
            self.emit("COMMIT", self._spaced(" ".join(new)))
            self.committed = final
            self.needs_space = True
        rest = words[final:]
        settled = " ".join(text for text, is_settled in rest if is_settled)
        tentative = " ".join(text for text, is_settled in rest if not is_settled)
        if settled and tentative:
            tentative = " " + tentative  # the front end shows the two parts back to back
        if self.needs_space and (settled or tentative):
            settled, tentative = (" " + settled, tentative) if settled else (settled, " " + tentative)
        preedit = (settled, tentative)
        if preedit != self.preedit:
            self.emit("PREEDIT", *preedit)
            self.preedit = preedit

    def _spaced(self, text):
        return " " + text if self.needs_space else text


class PwRecorder:
    """The default microphone through pw-record, as 16 kHz float samples, from the moment it is created."""

    def __init__(self):
        self.proc = subprocess.Popen(
            ["pw-record", "--raw", "--format", "s16", "--rate", str(SAMPLE_RATE), "--channels", "1",
             "--latency", "20ms", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.lock = threading.Lock()
        self.pending = bytearray()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        while chunk := self.proc.stdout.read1(65536):
            with self.lock:
                self.pending += chunk

    def take(self):
        """Every whole sample read since the last take."""
        with self.lock:
            whole = len(self.pending) // 2 * 2
            data, self.pending = bytes(self.pending[:whole]), self.pending[whole:]
        return numpy.frombuffer(data, numpy.int16).astype(numpy.float32) / 32768.0

    def stop(self):
        """Stop recording; take() then returns the rest."""
        if self.proc.poll() is None:
            self.proc.terminate()
        self.proc.wait()
        self.reader.join()


TICK = object()


def parse_end_key(fields):
    """The key in a STOP command's fields, or None for a plain STOP."""
    if not fields:
        return None
    assert len(fields) == 3, fields
    name, mods, text = fields
    return EndKey(name, unescape(text), frozenset(m for m in mods.split(",") if m))


def run_session(hear, before, commands, emit, recorder, interval=segments.INTERVAL_SECONDS, clock=time.monotonic,
                keywords=()):
    """Run one session until STOP or CANCEL, then emit DONE. Returns False if commands ended (EOF)."""
    saving = recordings.start(before)  # None unless the user switched session saving on
    if saving is not None:
        emit = saving.wrap_emit(emit)

    def take():
        samples = recorder.take()
        if saving is not None:
            saving.add_audio(samples)
        return samples

    session = Session(hear, before, emit, keywords, on_pass=saving.add_pass if saving is not None else None)
    next_tick = clock() + interval
    keep_serving = True
    absorbed = False
    try:
        while True:
            try:
                command = commands.get(timeout=max(0.0, next_tick - clock()))
            except queue.Empty:
                command = TICK
            if command is TICK:
                session.append(take())
                if saving is not None:
                    saving.tick()
                session.tick()
                next_tick = max(next_tick + interval, clock())  # fell behind: skip ticks rather than burst
            elif command is None:
                keep_serving = False
                break
            elif command.split("\t")[0] == "STOP":
                end_key = parse_end_key(command.split("\t")[1:])
                if saving is not None:
                    saving.stop = command.split("\t")[1:]
                recorder.stop()
                session.append(take())
                absorbed = session.close(end_key)
                break
            elif command == "CANCEL":
                break
            else:
                emit("ERROR", f"unexpected {command!r} during a session")
    except Exception as error:  # noqa: BLE001 - report it to the addon rather than dying with the model loaded
        traceback.print_exc()
        emit("ERROR", f"{type(error).__name__}: {error}".replace("\t", " ").replace("\n", " "))
    finally:
        recorder.stop()
    emit("DONE", "1" if absorbed else "0")
    if saving is not None:
        try:
            saving.save()
        except OSError:
            traceback.print_exc()  # a full disk must not break dictation
    return keep_serving


def serve(hear, commands, emit, record=PwRecorder, interval=segments.INTERVAL_SECONDS, clock=time.monotonic,
          keywords=()):
    """Answer sessions until commands end. commands yields lines without newlines, then None."""
    emit("READY")
    while (command := commands.get()) is not None:
        kind, *fields = command.split("\t")
        if kind == "START":
            assert len(fields) == 2 and fields[0] in ("0", "1"), command
            before = unescape(fields[1]) if fields[0] == "1" else None
            try:
                recorder = record()
            except OSError as error:
                emit("ERROR", f"cannot record: {error}")
                emit("DONE", "0")
                continue
            if not run_session(hear, before, commands, emit, recorder, interval, clock, keywords):
                return
        elif kind not in ("STOP", "CANCEL"):  # a late STOP or CANCEL: the session already ended
            emit("ERROR", f"unexpected {command!r} outside a session")


def _stdin_lines(into):
    for line in sys.stdin:
        into.put(line.rstrip("\n"))
    into.put(None)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--language", default=None, choices=LANGUAGES, help="Force the language (default: detect)")
    parser.add_argument("--keywords", default="", help="Words and phrases to favour, comma separated")
    parser.add_argument("--interval", type=float, default=segments.INTERVAL_SECONDS, help="Seconds between passes")
    args = parser.parse_args()

    # Only protocol messages may reach the addon: keep a private copy of stdout for them and send everything
    # else, including output from native code, to stderr.
    protocol = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    def emit(*fields):
        protocol.write("\t".join(_field(field) for field in fields) + "\n")

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    whistle = Whistle()

    def hear(samples):
        return whistle.words(samples, args.language, keywords)

    commands = queue.Queue()
    threading.Thread(target=_stdin_lines, args=(commands,), daemon=True).start()
    serve(hear, commands, emit, interval=args.interval, keywords=keywords)


if __name__ == "__main__":
    main()
