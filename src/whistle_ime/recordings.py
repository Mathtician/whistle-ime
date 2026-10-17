"""Opt-in saving of dictation sessions, for tuning against real audio (hallucinations on noise, for example).

On while ~/.config/whistle-ime/save-sessions exists, checked at the start of each dictation. Each session
writes <cache>/whistle-ime/sessions/<time>.wav (16 kHz mono) and <time>.json: every pass of the model (where
its window lies in the recording and the words it heard, with times and probabilities) and every message sent
to the front end. Of the text before the cursor only its length is kept.
"""
from __future__ import annotations

import json
import os
import time
import wave
from pathlib import Path

import numpy

SAMPLE_RATE = 16000


def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "whistle-ime"


def sessions_dir():
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "whistle-ime" / "sessions"


def start(before, now=time.time):
    """A SessionRecording if saving is switched on, else None."""
    if not (config_dir() / "save-sessions").exists():
        return None
    return SessionRecording(sessions_dir(), before, now())


class SessionRecording:
    def __init__(self, directory, before, started):
        self.directory = Path(directory)
        self.started = started
        self.before = None if before is None else {"chars": len(before)}
        self.chunks = []
        self.heard = 0  # samples so far
        self.passes = []
        self.events = []

    def add_audio(self, samples):
        if len(samples):
            self.chunks.append(numpy.asarray(samples, numpy.float32))
            self.heard += len(samples)

    def wrap_hear(self, hear):
        def recorded(samples):
            words = hear(samples)
            # The window is the newest audio, so it ends where the recording so far ends.
            end = self.heard / SAMPLE_RATE
            self.passes.append({"start": round(end - len(samples) / SAMPLE_RATE, 3), "end": round(end, 3),
                                "words": [[w, round(e, 3), round(p, 3)] for w, e, p in words]})
            return words
        return recorded

    def wrap_emit(self, emit):
        def recorded(*fields):
            self.events.append([round(self.heard / SAMPLE_RATE, 3), *fields])
            emit(*fields)
        return recorded

    def save(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        millis = int(self.started * 1000) % 1000  # two dictations can start within one second
        stem = self.directory / (time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(self.started)) + f"-{millis:03d}")  # not ".": with_suffix would replace it
        audio = numpy.concatenate(self.chunks) if self.chunks else numpy.zeros(0, numpy.float32)
        with wave.open(str(stem.with_suffix(".wav")), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(SAMPLE_RATE)
            out.writeframes((numpy.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        stem.with_suffix(".json").write_text(json.dumps(
            {"seconds": round(len(audio) / SAMPLE_RATE, 3), "before": self.before, "passes": self.passes,
             "events": self.events}, indent=1))
        return stem
