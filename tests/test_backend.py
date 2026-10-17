import queue

import numpy
import pytest

from whistle_ime import voting as live
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


def run(seconds, *commands, before=None, keywords=()):
    out = Out()
    keep = backend.run_session(fake_model, before, Script(int(round(seconds / live.INTERVAL_SECONDS)), *commands),
                               out, FakeRecorder(seconds), clock=lambda: 0.0, keywords=keywords)
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

    backend.run_session(fake_model, None, Script(80, "STOP"), watch, FakeRecorder(8.0), clock=lambda: 0.0)
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
    out, _ = run(8.0, "STOP", before="Well.")
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
    backend.run_session(broken, None, Script(3, "STOP"), out, FakeRecorder(1.0), clock=lambda: 0.0)
    assert out.messages[-2] == ("ERROR", "RuntimeError: engine fell over") and out.messages[-1] == ("DONE", "0")


def test_serve_ignores_a_late_stop_and_runs_sessions_until_input_ends():
    commands = queue.Queue()
    for command in ("STOP", "CANCEL", "START\t0\t", "STOP", None):
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
    for command in ("START\t0\t", "STOP", None):
        commands.put(command)
    out = Out()
    backend.serve(fake_model, commands, out, record=no_mic)
    assert out.messages == [("READY",), ("ERROR", "cannot record: pw-record"), ("DONE", "0")]


def test_mid_sentence_the_first_word_loses_its_capital_in_preview_and_commit():
    out, _ = run(8.0, "STOP", before="When I start a sentence like this, ")
    assert "".join(out.commits()) == "and so, my fellow Americans, ask not what your country can do for you."
    previews = [m[1] + m[2] for m in out.messages if m[0] == "PREEDIT" and (m[1] or m[2])]
    assert any(p.startswith("and") for p in previews) and not any(p.startswith("And") for p in previews)


def test_at_a_sentence_start_the_first_word_is_capitalized():
    out, _ = run(8.0, "STOP", before="Done. ")
    assert "".join(out.commits()).startswith("And so,")


def test_serve_passes_the_escaped_context_to_the_session():
    commands = queue.Queue()
    for command in ("START\t1\tline one\\nand so on, ", "STOP", None):
        commands.put(command)
    out = Out()
    backend.serve(fake_model, commands, out, record=lambda: FakeRecorder(3.0), interval=0.1)
    assert "".join(out.commits()) == "and so, my fellow Americans,"


def test_typed_punctuation_ending_a_dictation_replaces_the_models_and_is_absorbed():
    out, _ = run(8.0, "STOP\tcomma\t\t,")
    assert "".join(out.commits()) == FULL[:-1] + ","
    assert out.messages[-1] == ("DONE", "1")


def test_ctrl_backspace_ending_a_dictation_drops_the_last_word():
    out, _ = run(8.0, "STOP\tBackSpace\tctrl\t")
    assert "".join(out.commits()) == FULL[:-len("you.")]
    assert out.messages[-1] == ("DONE", "1")


def test_a_letter_ending_a_dictation_is_handed_back_for_replay():
    out, _ = run(8.0, "STOP\ta\t\ta")
    assert "".join(out.commits()) == FULL and out.messages[-1] == ("DONE", "0")


def test_an_escaped_backslash_in_the_end_key_text_round_trips():
    from whistle_ime.backend import parse_end_key
    from whistle_ime.context import escape
    assert parse_end_key(["backslash", "", escape("\\")]).text == "\\"
    assert parse_end_key([]) is None


def test_sessions_are_saved_only_while_switched_on(tmp_path, monkeypatch):
    import json
    import wave

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    run(2.0, "STOP")
    assert not (tmp_path / "cache").exists()  # off by default
    (tmp_path / "config" / "whistle-ime").mkdir(parents=True)
    (tmp_path / "config" / "whistle-ime" / "save-sessions").touch()
    out, _ = run(3.0, "STOP", before="Secret text, ")
    saved = sorted((tmp_path / "cache" / "whistle-ime" / "sessions").iterdir())
    assert [p.suffix for p in saved] == [".json", ".wav"]
    record = json.loads(saved[0].read_text())
    assert record["before"] == {"chars": len("Secret text, ")}  # never the text itself
    assert record["seconds"] == 3.0 and record["passes"] and record["passes"][-1]["end"] == 3.0
    assert any(e[1] == "COMMIT" for e in record["events"]) and record["events"][-1][1:] == ["DONE", "0"]
    with wave.open(str(saved[1])) as w:
        assert w.getframerate() == 16000 and w.getnframes() == 3 * 16000


def test_two_sessions_within_one_second_keep_separate_files(tmp_path):
    from whistle_ime.recordings import SessionRecording

    stems = [SessionRecording(tmp_path, None, 1700000000.25).save(), SessionRecording(tmp_path, None, 1700000000.75).save()]
    assert stems[0] != stems[1]
    assert len(list(tmp_path.iterdir())) == 4
