"""Overlapping 5 s segments, stitched where they agree: the core of whistle-ime's live transcript.

Whistle reads a window cleanly except near its cuts: a word within about 0.25 s of a cut disagrees with
the other windows a third of the time, and from about a second in, rarely (measured on recorded sessions).
So the recording is cut into segments of WINDOW seconds starting every STRIDE seconds, and each moment is
taken from the segment in which it lies furthest from a cut.

The open segment, the earliest that has not reached its full length, is read again on every tick, from its
start to the newest audio: that reading is the preview. When it reaches full length, its last reading is
final. Two consecutive segments overlap by WINDOW - STRIDE; they are stitched near the middle of the overlap
(3/4 of the way through the older, 1/4 through the newer), at a word both read alike. The older one's words up
to that word are then final and committed; the newer one's words after it continue the text.

Short dictations never stitch: the first segment starts at the start of the recording, so until it is full
the preview is simply its latest reading, and stopping commits one last reading of everything.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SAMPLE_RATE = 16000
WINDOW_SECONDS = 5.0  # the encoder is about 5x slower past 64 frames (5.12 s)
STRIDE_SECONDS = 2.5  # two segments cover each moment
INTERVAL_SECONDS = 0.1  # between preview readings
FRAME_SECONDS = 0.08  # Whistle's times are whole frames of this
WINDOW = int(WINDOW_SECONDS * SAMPLE_RATE)
STRIDE = int(STRIDE_SECONDS * SAMPLE_RATE)
# Where two segments are stitched, in seconds after the newer one's start: the middle of their overlap.
CUT_SECONDS = (WINDOW_SECONDS - STRIDE_SECONDS) / 2

# What the model says over silence or background noise, alone and near the window's start, wherever that start
# is. A reading of only one of these, that early, counts as hearing nothing. So a real "Thank you." still
# shows after other words or later in a window, but not alone in the first seconds of a segment.
SILENCE_PHRASES = {("thank", "you"), ("you",)}
# In recorded silence and noise the phrase ended up to 1.2 s into the window when it was alone.
SILENCE_PHRASE_SECONDS = 1.3


def normalize(word):
    """The form words are compared under: case and punctuation do not matter."""
    core = re.sub(r"[^\w']", "", word.lower()).strip("'")
    return core or word


@dataclass(frozen=True)
class Word:
    text: str
    start: float  # seconds into the recording
    end: float
    probability: float

    def __post_init__(self):
        assert self.text and self.start <= self.end and 0 <= self.probability <= 1, self


@dataclass(frozen=True)
class Reading:
    """What one pass of the model heard in [start, end) of the recording, in spoken order."""

    start: float
    end: float
    words: tuple = ()

    def __post_init__(self):
        assert 0 <= self.start < self.end, (self.start, self.end)
        for word in self.words:
            assert self.start - FRAME_SECONDS <= word.start and word.end <= self.end + FRAME_SECONDS, (word, self)


def read(hear, samples, start_sample):
    """The Reading of samples that begin start_sample samples into the recording.

    hear(samples) -> [(text, start, end, probability)], times in seconds into the samples. A reading that is
    only the model's stock phrase for silence, early in its window, counts as hearing nothing.
    """
    heard = [(text, s, e, p) for text, s, e, p in hear(samples) if text]
    if tuple(normalize(text) for text, *_ in heard) in SILENCE_PHRASES and heard[-1][2] <= SILENCE_PHRASE_SECONDS:
        heard = []
    t0 = start_sample / SAMPLE_RATE
    return Reading(t0, t0 + len(samples) / SAMPLE_RATE,
                   tuple(Word(text, t0 + s, t0 + e, p) for text, s, e, p in heard))


def _same(a, b):
    """Whether two readings' words are the same spoken word: equal under normalize, and overlapping in time.

    Overlap rather than close end times: Whistle's words tile its window, so a pause between words goes to one
    of them, and windows disagree about which ("keys" ended at 2.16 s in some, 2.48 s in others).
    """
    return normalize(a.text) == normalize(b.text) and min(a.end, b.end) > max(a.start, b.start)


def _matches(older, newer):
    """The longest sequence of (i, j) with older[i] and newer[j] the same word, in order (an LCS)."""
    n, m = len(older), len(newer)
    best = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            best[i][j] = max(best[i + 1][j], best[i][j + 1],
                             best[i + 1][j + 1] + 1 if _same(older[i], newer[j]) else 0)
    pairs, i, j = [], 0, 0
    while i < n and j < m:
        if _same(older[i], newer[j]) and best[i][j] == best[i + 1][j + 1] + 1:
            pairs.append((i, j))
            i, j = i + 1, j + 1
        elif best[i + 1][j] >= best[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def stitch(older, newer, at):
    """(k, l): the text is older[:k] + newer[l:], joined at the moment `at`.

    older and newer are word sequences from overlapping segments. They are joined right after the word both read
    alike whose end is nearest `at`. Then neither a word split by a cut nor a pause given to different words
    can be doubled or lost, and a disagreement next to the join goes to whichever side lies further from its
    own cut there. If they agree on nothing (silence there, or a misheard stretch), each keeps the words whose
    middles lie on its side of `at`.
    """
    pairs = _matches(older, newer)
    if pairs:
        i, j = min(pairs, key=lambda pair: abs((older[pair[0]].end + newer[pair[1]].end) / 2 - at))
        return i + 1, j + 1
    middle = lambda word: (word.start + word.end) / 2  # noqa: E731
    k = sum(1 for word in older if middle(word) < at)
    l = sum(1 for word in newer if middle(word) < at)
    assert all(middle(w) < at for w in older[:k]) and all(middle(w) >= at for w in newer[l:])
    return k, l


class Transcript:
    """The text so far: final words, then the preview (the last full segment's part, then the open one's)."""

    def __init__(self):
        self.final = []  # Words never revised again
        self.older = None  # the last full segment's words after its stitch to the one before; None before one
        self.latest = None  # the open segment's newest Reading

    def _join(self, reading):
        """(older's words, reading's words) continuing the final text, older first."""
        if self.older is None:
            return [], list(reading.words)  # the first segment: nothing before it
        k, l = stitch(self.older, reading.words, reading.start + CUT_SECONDS)
        return list(self.older[:k]), list(reading.words[l:])

    def partial(self, reading):
        """The open segment's newest reading."""
        self.latest = reading

    def full(self, reading):
        """The open segment reached full length and this is its final reading: stitch it to the one before,
        whose words up to the stitch become final."""
        settled, rest = self._join(reading)
        self.final += settled
        self.older, self.latest = tuple(rest), None

    def close(self, reading):
        """The last reading, up to the recording's true end: everything becomes final."""
        settled, rest = self._join(reading)
        self.final += settled + rest
        self.older, self.latest = None, None

    def preview(self):
        """(settled, tentative) Words after the final ones: from the last full segment, then the open one."""
        if self.latest is None:
            return list(self.older or ()), []
        return self._join(self.latest)
