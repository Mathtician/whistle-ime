"""Voting across overlapping windows: the core of whistle-ime's live transcript.

Every 0.1 s the last 5 s of audio are transcribed again, so each word is heard by dozens of windows. Each
window's words carry end times in the recording, which agree across windows to about one 80 ms frame
(start times do not: a word swallows the pause before it), so a window is aligned to the running transcript
with a DP that only matches words whose end times are close. Matched words vote for themselves in their
slot, a slot the window covers but has no word for votes for nothing there, and each slot shows the word
with the most votes.

Words near a cut are where the model goes wrong: at the right edge it confidently truncates or invents
("your country" -> "you are calling"), at the left edge it guesses a half-heard word. So a vote is weighted
by its probability and by how far its word sits from either cut.

Moved here from needle/agent/live.py in a local branch (whistle-live) of github.com/cactus-compute/needle.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy


WINDOW_SECONDS = 5.0  # the encoder is about 5x slower past 64 frames (5.12 s)
INTERVAL_SECONDS = 0.1
RIGHT_RAMP = 1.0  # a word ending this close to the window's right cut gets less than full weight
LEFT_RAMP = 0.5
FLOOR = 0.05  # the least weight a vote gets, so the newest words still show
MATCH_SECONDS = 0.3  # the largest end-time gap at which two windows' words can be the same word
SETTLED = 2.0  # how far, in full votes, the winner leads the runner-up before it counts as settled
FRAME_SECONDS = 0.08
NOTHING = None  # the candidate for "no word in this slot"
# What the model says over silence or background noise, alone and near the window's start, wherever that start
# is. A pass that hears only one of these, that early, counts as hearing nothing. So a real "Thank you." still
# shows after other words or later in a window, but not alone in the first seconds of a dictation.
SILENCE_PHRASES = {("thank", "you"), ("you",)}
# In recorded silence and noise, 0.9 s kept it from being committed, and 1.3 s also kept it out of the
# preview (where 11 passes put it at 1.2 s). Later lone passes are outvoted by the ignored ones around them.
SILENCE_PHRASE_SECONDS = 1.3


def normalize(word):
    """The form votes are counted under: case and punctuation do not split a vote."""
    core = re.sub(r"[^\w']", "", word.lower()).strip("'")
    return core or word


@dataclass(frozen=True)
class Window:
    start: float  # seconds into the recording
    end: float
    words: tuple  # (text, end time in seconds into the recording, probability), in spoken order
    opens_recording: bool  # start is the start of the recording rather than a cut
    closes_recording: bool  # end is the end of the recording rather than a cut

    def __post_init__(self):
        assert 0 <= self.start < self.end, (self.start, self.end)
        for text, end, probability in self.words:
            assert text and 0 <= probability <= 1, (text, probability)
            assert self.start - FRAME_SECONDS <= end <= self.end + FRAME_SECONDS, (text, end, self.start, self.end)

    def weight(self, t):
        """How much this window's view of a word ending at t counts, from FLOOR next to a cut up to 1."""
        right = 1.0 if self.closes_recording else min(1.0, max(0.0, (self.end - t) / RIGHT_RAMP))
        left = 1.0 if self.opens_recording else min(1.0, max(0.0, (t - self.start) / LEFT_RAMP))
        return max(FLOOR, right * left)


@dataclass
class Slot:
    time: float  # weighted mean end time of the words voted into it
    votes: dict = field(default_factory=dict)  # normalized word or NOTHING -> weight
    forms: dict = field(default_factory=dict)  # normalized word -> {as written -> weight}
    timed: float = 0.0  # weight behind time

    def vote(self, text, t, weight):
        word = normalize(text)
        self.votes[word] = self.votes.get(word, 0.0) + weight
        forms = self.forms.setdefault(word, {})
        forms[text] = forms.get(text, 0.0) + weight
        if weight > 0:
            self.time = (self.time * self.timed + t * weight) / (self.timed + weight)
            self.timed += weight

    def vote_nothing(self, weight):
        self.votes[NOTHING] = self.votes.get(NOTHING, 0.0) + weight

    def ranked(self):
        return sorted(self.votes.items(), key=lambda item: -item[1])

    def leader(self):
        """The best word that is not NOTHING, to favour matching the same word again."""
        words = [word for word, _ in self.ranked() if word is not NOTHING]
        return words[0] if words else NOTHING

    def shown(self):
        """(text, settled) for the winning word, or None when the winner is NOTHING."""
        ranked = self.ranked()
        word, weight = ranked[0]
        if word is NOTHING:
            return None
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        text = max(self.forms[word].items(), key=lambda item: item[1])[0]
        return text, weight - runner_up >= SETTLED


def _match_score(slot, text, t):
    gap = abs(slot.time - t)
    if gap > MATCH_SECONDS:
        return None
    return 1.0 - gap / MATCH_SECONDS + (1.0 if normalize(text) == slot.leader() else 0.0)


def align(slots, words):
    """Needleman-Wunsch over slots and words, both in time order: maximise the summed match scores.

    Returns the merged order as (slot index or None, word index or None) pairs; skipping either side scores 0,
    so any pair close enough in time to match at all is matched unless that would cross a better pair.
    Unmatched slots and words come out in time order: on a tie the step taken last is the later one.
    """
    n, m = len(slots), len(words)
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    step = [[None] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        for j in range(m + 1):
            if i == 0 and j == 0:
                continue
            options = []  # (score, time of the item this step puts last, kind); a match wins ties
            if i and j:
                score = _match_score(slots[i - 1], words[j - 1][0], words[j - 1][1])
                if score is not None:
                    options.append((best[i - 1][j - 1] + score, float("inf"), "match"))
            if i:
                options.append((best[i - 1][j], slots[i - 1].time, "slot"))
            if j:
                options.append((best[i][j - 1], words[j - 1][1], "word"))
            best[i][j], _, step[i][j] = max(options, key=lambda option: option[:2])
    pairs, i, j = [], n, m
    while i or j:
        kind = step[i][j]
        if kind == "match":
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif kind == "slot":
            pairs.append((i - 1, None))
            i -= 1
        else:
            pairs.append((None, j - 1))
            j -= 1
    return pairs[::-1]


class Transcript:
    """The running vote. Slots are in spoken order; those no later window can reach are frozen into text.

    A slot opened late still hears from the windows before it: each earlier window that covered its time
    without a word there votes for nothing, as it would have had the slot existed then. Otherwise a word
    invented by a few windows just before its time falls behind the window (the model says "Thank you."
    after a scrap of speech at a window's left edge) is shown with nothing against it.
    """

    def __init__(self):
        self.frozen = []  # (text, settled) for slots past every future window
        self.slots = []
        self.recent = []  # earlier windows that may cover a slot opened from now on
        self._last_start = 0.0

    def add(self, window):
        assert window.start >= self._last_start, "windows must not move backwards"
        self._last_start = window.start
        while self.slots and self.slots[0].time < window.start - MATCH_SECONDS:
            shown = self.slots.pop(0).shown()
            if shown is not None:
                self.frozen.append(shown)
        reach = [i for i, slot in enumerate(self.slots) if slot.time <= window.end + MATCH_SECONDS]
        lo, hi = 0, (reach[-1] + 1 if reach else 0)
        merged = []
        for i, j in align(self.slots[lo:hi], window.words):
            if j is None:
                slot = self.slots[lo + i]
                if window.start <= slot.time <= window.end:
                    slot.vote_nothing(window.weight(slot.time))
            else:
                text, t, probability = window.words[j]
                if i is None:
                    slot = Slot(time=t)
                    for earlier in self.recent:
                        if earlier.start <= t <= earlier.end:
                            slot.vote_nothing(earlier.weight(t))
                else:
                    slot = self.slots[lo + i]
                slot.vote(text, t, probability * window.weight(t))
            merged.append(slot)
        self.slots[lo:hi] = merged
        # a word ends no earlier than a frame before its window starts, and window starts only move forward
        self.recent = [w for w in self.recent if w.end >= window.start - FRAME_SECONDS] + [window]
        if window.closes_recording:
            for slot in self.slots:
                shown = slot.shown()
                if shown is not None:
                    self.frozen.append(shown)
            self.slots = []

    def words(self):
        """(text, settled) for every word currently shown, in order."""
        live = [slot.shown() for slot in self.slots]
        return self.frozen + [shown for shown in live if shown is not None]

    def text(self):
        return " ".join(text for text, _ in self.words())


def is_silence_phrase(words):
    """Whether a pass's words, timed from its window's start, are only the model's stock phrase for silence."""
    return (tuple(normalize(text) for text, _, _ in words) in SILENCE_PHRASES
            and words[-1][1] <= SILENCE_PHRASE_SECONDS)


class Recording:
    """16 kHz samples heard so far, keeping only the last window's worth."""

    def __init__(self, sample_rate):
        self.rate = sample_rate
        self.keep = int(WINDOW_SECONDS * sample_rate)
        self.samples = numpy.zeros(0, numpy.float32)
        self.dropped = 0  # samples before self.samples[0]

    def append(self, samples):
        self.samples = numpy.concatenate([self.samples, numpy.asarray(samples, numpy.float32)])
        extra = len(self.samples) - self.keep
        if extra > 0:
            self.samples = self.samples[extra:]
            self.dropped += extra

    def heard(self):
        return (self.dropped + len(self.samples)) / self.rate

    def observe(self, hear, closes=False):
        start = self.dropped / self.rate
        heard = [(text, end, probability) for text, end, probability in hear(self.samples) if text]
        if is_silence_phrase(heard):
            heard = []
        words = tuple((text, start + end, probability) for text, end, probability in heard)
        return Window(start, self.heard(), words, opens_recording=self.dropped == 0, closes_recording=closes)


