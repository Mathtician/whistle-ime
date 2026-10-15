"""Dictation backend for the whistle-ime fcitx5 addon: speech in, preedit and commits out.

The addon starts this process once and keeps it running, so the model stays loaded. During a session it
transcribes the last 5 s every 0.1 s and votes across the windows (needle.agent.live). A word is committed
the moment no future window can reach it, which is about 5.3 s after it was spoken; everything after that
is shown as preedit and may still change.

Protocol, one message per line, fields separated by tabs (text never contains tabs or newlines):
  addon -> backend
    START <known> <before>
                    begin a session; <known> is 1 if the app reports the text before the cursor, which is then
                    <before> (its last 200 characters, with backslash, tab and newline escaped as \\, \t, \n)
    STOP            end the session: one last pass trusting the recording's true end, commit the rest, DONE
    CANCEL          end the session, dropping whatever is not committed yet, DONE
  backend -> addon
    READY                            the model is loaded (once, at startup)
    COMMIT <text>                    final text: insert it
    PREEDIT <settled> <tentative>    the uncommitted rest, shown after the committed text; replaces the last one
    DONE                             the session is over
    ERROR <message>
STOP and CANCEL outside a session are ignored: the session may have just ended on an error.
"""
from __future__ import annotations

import argparse
import itertools
import os
import queue
import subprocess
import sys
import threading
import time
import traceback

import numpy

from needle.agent import live

from whistle_ime.context import fit_first_word, needs_space, unescape

SAMPLE_RATE = 16000


def _field(text):
    assert "\t" not in text and "\n" not in text, repr(text)
    return text


class Session:
    """One dictation: audio in, COMMIT/PREEDIT messages out. No threads or audio devices; see run_session.

    before is the text before the cursor, or None when the app does not report it. It decides the leading
    space and the first word's casing (see context.py).
    """

    def __init__(self, hear, before, emit, keywords=()):
        self.hear = hear
        self.emit = emit
        self.before = before
        self.keywords = keywords
        self.recording = live.Recording(SAMPLE_RATE)
        self.transcript = live.Transcript()
        self.needs_space = before is not None and needs_space(before)  # the next text starts with a space
        self.committed = 0  # words of transcript.frozen already committed
        self.preedit = ("", "")
        self.unheard = False  # audio arrived since the last pass

    def append(self, samples):
        if len(samples):
            self.recording.append(samples)
            self.unheard = True

    def tick(self, closes=False):
        """One pass over the last window, unless nothing new arrived; closes trusts the window's right edge."""
        if self.recording.heard() > 0 and (self.unheard or closes):
            self.transcript.add(self.recording.observe(self.hear, closes=closes))
            self.unheard = False
        self.publish()

    def publish(self):
        # Transcript.frozen only ever grows at its end, so committing its new tail never revises earlier text.
        # The first word is fitted to the text before the cursor; that depends only on its own text, so the
        # preview shows exactly what will be committed.
        frozen = len(self.transcript.frozen)
        words = self.transcript.words()
        if words:
            words[0] = (fit_first_word(words[0][0], self.before, self.keywords), words[0][1])
        new = [text for text, _ in words[self.committed:frozen]]
        if new:
            self.emit("COMMIT", self._spaced(" ".join(new)))
            self.committed = frozen
            self.needs_space = True
        rest = words[frozen:]
        settled = [text for text, _ in itertools.takewhile(lambda word: word[1], rest)]
        tentative = [text for text, _ in rest[len(settled):]]
        preedit = (" ".join(settled), " ".join(tentative))
        if preedit[0] and self.needs_space:
            preedit = (" " + preedit[0], preedit[1])
        elif preedit[1] and self.needs_space:
            preedit = (preedit[0], " " + preedit[1])
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


def run_session(hear, before, commands, emit, recorder, interval=live.INTERVAL_SECONDS, clock=time.monotonic,
                keywords=()):
    """Run one session until STOP or CANCEL, then emit DONE. Returns False if commands ended (EOF)."""
    session = Session(hear, before, emit, keywords)
    next_tick = clock() + interval
    keep_serving = True
    try:
        while True:
            try:
                command = commands.get(timeout=max(0.0, next_tick - clock()))
            except queue.Empty:
                command = TICK
            if command is TICK:
                session.append(recorder.take())
                session.tick()
                next_tick = max(next_tick + interval, clock())  # fell behind: skip ticks rather than burst
            elif command == "STOP":
                recorder.stop()
                session.append(recorder.take())
                session.tick(closes=True)
                break
            elif command == "CANCEL":
                break
            elif command is None:
                keep_serving = False
                break
            else:
                emit("ERROR", f"unexpected {command!r} during a session")
    except Exception as error:  # noqa: BLE001 - report it to the addon rather than dying with the model loaded
        traceback.print_exc()
        emit("ERROR", f"{type(error).__name__}: {error}".replace("\t", " ").replace("\n", " "))
    finally:
        recorder.stop()
    emit("DONE")
    return keep_serving


def serve(hear, commands, emit, record=PwRecorder, interval=live.INTERVAL_SECONDS, clock=time.monotonic,
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
                emit("DONE")
                continue
            if not run_session(hear, before, commands, emit, recorder, interval, clock, keywords):
                return
        elif kind not in ("STOP", "CANCEL"):
            emit("ERROR", f"unexpected {command!r} outside a session")


def _stdin_lines(into):
    for line in sys.stdin:
        into.put(line.rstrip("\n"))
    into.put(None)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="whistle", choices=live.MODELS)
    parser.add_argument("--language", default=None)
    parser.add_argument("--keywords", default="", help="Words and phrases to favour, comma separated")
    parser.add_argument("--interval", type=float, default=None, help="Seconds between passes")
    args = parser.parse_args()

    # Only protocol messages may reach the addon: keep a private copy of stdout for them and send everything
    # else, including output from native code, to stderr.
    protocol = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    def emit(*fields):
        protocol.write("\t".join(_field(field) for field in fields) + "\n")

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    hear = live.load(args.model, args.language, keywords)
    commands = queue.Queue()
    threading.Thread(target=_stdin_lines, args=(commands,), daemon=True).start()
    serve(hear, commands, emit, interval=args.interval or live.INTERVALS[args.model], keywords=keywords)


if __name__ == "__main__":
    main()
