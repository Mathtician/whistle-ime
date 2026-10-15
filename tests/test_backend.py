import queue

import numpy
import pytest

from needle.agent import live
from whistle_ime import backend

SPOKEN = [("And", 0.72), ("so,", 0.96), ("my", 1.28), ("fellow", 1.68), ("Americans,", 2.24), ("ask", 3.84),
          ("not", 4.36), ("what", 5.64), ("your", 5.92), ("country", 6.40), ("can", 6.72), ("do", 6.96),
          ("for", 7.20), ("you.", 7.56)]
FULL = " ".join(text for text, _ in SPOKEN)


def fake_model(samples):
    """Hears SPOKEN, garbling the word at a window's right edge the way the real models do.

    The samples are the recording's own clock (sample i holds i / 16000), so a window knows where it starts.
    """
    start, end = float(samples[0]), float(samples[-1]) + 1 / 16000
    return [("calling" if end - t < 0.3 else text, t - start, 0.95 if end - t < 0.3 else 0.9)
            for text, t in SPOKEN if start + 0.2 < t <= end]


class FakeRecorder:
    """A microphone hearing `seconds` of the recording clock, interval seconds per take()."""

    def __init__(self, seconds, interval=live.INTERVAL_SECONDS):
        self.clock = numpy.arange(int(seconds * 16000), dtype=numpy.float64) / 16000
        self.step = int(round(interval * 16000))
        self.position = 0
        self.stopped = False

    def take(self):
        end = len(self.clock) if self.stopped else min(len(self.clock), self.position + self.step)
        chunk, self.position = self.clock[self.position:end], end
        return chunk

    def stop(self):
        self.stopped = True


class Script:
    """Stands in for the command queue: `ticks` passes elapse, then each command arrives in turn."""

    def __init__(self, ticks, *commands):
        self.ticks = ticks
        self.commands = list(commands)

    def get(self, timeout=None):
        if self.ticks:
            assert timeout is not None, "a tick is due, so the wait must time out"
            self.ticks -= 1
            raise queue.Empty
        return self.commands.pop(0)


class Out:
    def __init__(self):
        self.messages = []

    def __call__(self, *fields):
        assert all("\t" not in f and "\n" not in f for f in fields)
        self.messages.append(fields)

    def commits(self):
        return [m[1] for m in self.messages if m[0] == "COMMIT"]

    def kinds(self):
        return [m[0] for m in self.messages]


def run(seconds, *commands, needs_space=False):
    out = Out()
    keep = backend.run_session(fake_model, needs_space, Script(int(round(seconds / live.INTERVAL_SECONDS)), *commands),
                               out, FakeRecorder(seconds), clock=lambda: 0.0)
    return out, keep


def test_stop_commits_the_whole_transcript_and_commits_only_ever_append():
    out, keep = run(8.0, "STOP")
    assert keep and out.kinds()[-1] == "DONE"
    assert "".join(out.commits()) == FULL
    assert out.messages[-2] == ("PREEDIT", "", "")


def test_words_are_committed_during_the_session_once_no_window_can_reach_them():
    out, _ = run(8.0, "CANCEL")
    before_stop = "".join(out.commits())
    assert before_stop.startswith("And so, my fellow Americans,")  # spoken by 2.24 s, final by ~7.6 s
    assert FULL.startswith(before_stop) and before_stop != FULL


def test_committed_text_plus_preedit_always_reads_as_the_current_transcript():
    out = Out()
    shown = []

    def watch(*fields):
        out(*fields)
        if fields[0] == "PREEDIT":
            shown.append("".join(out.commits()) + fields[1] + (" " if fields[1] and fields[2] else "") + fields[2])

    backend.run_session(fake_model, False, Script(80, "STOP"), watch, FakeRecorder(8.0), clock=lambda: 0.0)
    assert shown[-1] == FULL  # once everything is committed the preedit is empty
    assert "calling" in " ".join(shown)  # the garbled right edge was previewed...
    assert "calling" not in "".join(out.commits())  # ...but never committed
    for text in shown:
        assert "  " not in text and not text.startswith(" ")


def test_cancel_drops_what_is_not_committed():
    out, keep = run(8.0, "CANCEL")
    assert keep and out.kinds()[-1] == "DONE"
    assert "".join(out.commits()) != FULL


def test_a_needed_space_goes_before_the_first_text_and_only_there():
    out, _ = run(8.0, "STOP", needs_space=True)
    commits = out.commits()
    assert all(c.startswith(" ") for c in commits)
    assert "".join(commits) == " " + FULL
    first_preedit = next(m for m in out.messages if m[0] == "PREEDIT")
    assert (first_preedit[1] or first_preedit[2]).startswith(" ")


def test_stop_before_any_speech_commits_nothing():
    out, _ = run(0.1, "STOP")
    assert out.commits() == [] and out.kinds()[-1] == "DONE"


def test_end_of_input_during_a_session_ends_serving():
    out, keep = run(1.0, None)
    assert not keep and out.kinds()[-1] == "DONE"


def test_a_failing_model_reports_an_error_and_the_session_still_ends():
    def broken(samples):
        raise RuntimeError("engine fell over")

    out = Out()
    backend.run_session(broken, False, Script(3, "STOP"), out, FakeRecorder(1.0), clock=lambda: 0.0)
    assert out.messages[-2] == ("ERROR", "RuntimeError: engine fell over") and out.kinds()[-1] == "DONE"


def test_serve_ignores_a_late_stop_and_runs_sessions_until_input_ends():
    commands = queue.Queue()
    for command in ("STOP", "CANCEL", "START\t0", "STOP", None):
        commands.put(command)
    out = Out()
    backend.serve(fake_model, commands, out, record=lambda: FakeRecorder(0.5), interval=0.1)
    assert out.kinds()[0] == "READY" and "ERROR" not in out.kinds() and out.kinds()[-1] == "DONE"


def test_serve_reports_an_unknown_command():
    commands = queue.Queue()
    for command in ("HELLO", None):
        commands.put(command)
    out = Out()
    backend.serve(fake_model, commands, out, record=lambda: FakeRecorder(0.5))
    assert out.messages == [("READY",), ("ERROR", "unexpected 'HELLO' outside a session")]


def test_serve_reports_a_microphone_that_cannot_start():
    def no_mic():
        raise FileNotFoundError("pw-record")

    commands = queue.Queue()
    for command in ("START\t0", "STOP", None):
        commands.put(command)
    out = Out()
    backend.serve(fake_model, commands, out, record=no_mic)
    assert out.messages == [("READY",), ("ERROR", "cannot record: pw-record"), ("DONE",)]
