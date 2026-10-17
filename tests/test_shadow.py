from whistle_ime import shadow
from whistle_ime.shadow import Shadow

KONSOLE, FIREFOX = object(), object()
HERE, THERE = (10, 20, 1, 20), (50, 20, 1, 20)


def test_committed_text_then_typing_and_backspacing_past_what_it_knows():
    s = Shadow()
    assert not s.tracks(KONSOLE)
    s.reset(KONSOLE, HERE, 0.0)
    assert s.tracks(KONSOLE) and not s.tracks(FIREFOX) and s.text == ""
    s.append("When I start a sentence like this.", 0.0)
    s.backspace(0.0)
    assert s.text == "When I start a sentence like this"
    s.append(" é", 0.0)
    s.backspace(0.0)
    assert s.text == "When I start a sentence like this "  # a whole character
    empty = Shadow()
    empty.reset(KONSOLE, HERE, 0.0)
    empty.backspace(0.0)
    assert not empty.tracks(KONSOLE)  # deleting text it never saw


def test_only_the_tail_is_kept():
    s = Shadow()
    s.reset(KONSOLE, HERE, 0.0)
    s.append("a" * (shadow.KEEP + 50) + "end", 0.0)
    assert len(s.text) == shadow.KEEP and s.text.endswith("end")


def test_the_cursor_is_followed_right_after_our_change_and_fatal_otherwise():
    s = Shadow()
    s.reset(KONSOLE, HERE, 0.0)
    s.append("text", 0.0)
    s.cursor_moved(THERE, False, 0.5)  # the app redrawing after our commit
    assert s.usable(KONSOLE, THERE) and not s.usable(KONSOLE, HERE) and not s.usable(FIREFOX, THERE)
    s.cursor_moved(HERE, False, 3.0)  # nobody here moved it: a click
    assert not s.tracks(KONSOLE)


def test_during_a_dictation_every_move_is_ours():
    s = Shadow()
    s.reset(KONSOLE, HERE, 0.0)
    s.cursor_moved(THERE, True, 10.0)
    assert s.usable(KONSOLE, THERE)


def test_invalidation_is_final_until_the_next_reset():
    s = Shadow()
    s.reset(KONSOLE, HERE, 0.0)
    s.invalidate()
    assert not s.usable(KONSOLE, HERE)
    s.reset(KONSOLE, HERE, 0.0)
    assert s.usable(KONSOLE, HERE)
