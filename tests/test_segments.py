import numpy

from whistle_ime import segments
from whistle_ime.segments import Reading, Transcript, Word, normalize, read, stitch


def words(*spec):
    """Words from (text, start, end) triples."""
    return tuple(Word(text, start, end, 0.9) for text, start, end in spec)


def texts(ws):
    return [w.text for w in ws]


def joined(older, newer, at):
    k, l = stitch(older, newer, at)
    return texts(older[:k] + newer[l:])


def test_normalize_ignores_case_and_punctuation_but_keeps_apostrophes():
    assert normalize("Keys.") == normalize("keys") == "keys"
    assert normalize("Don't,") == "don't"
    assert normalize("...") == "..."


def test_stitch_joins_at_an_agreed_word_whatever_side_of_a_pause_each_gave_it():
    # the older segment gave the pause after "keys" to "should", the newer to "keys"
    older = words(("which", 1.44, 1.68), ("keys", 1.68, 2.16), ("should", 2.16, 2.8), ("do", 2.8, 2.96))
    newer = words(("which", 1.4, 1.68), ("keys", 1.68, 2.48), ("should", 2.48, 2.8), ("do", 2.8, 2.96))
    assert joined(older, newer, at=2.3) == ["which", "keys", "should", "do"]


def test_stitch_leaves_out_garbage_at_both_cuts():
    # the older segment ends at 5.0 and garbles its last word; the newer starts at 2.5 and mishears its first
    older = words(("ask", 3.3, 3.84), ("not", 3.84, 4.36), ("calling", 4.36, 4.9))
    newer = words(("Task", 2.5, 3.0), ("ask", 3.3, 3.84), ("not", 3.84, 4.36), ("what", 4.36, 5.64))
    assert joined(older, newer, at=3.75) == ["ask", "not", "what"]


def test_stitch_joins_at_the_agreed_word_nearest_the_cut_point():
    # they disagree on the middle word; after the join, the newer segment's reading wins
    older = words(("the", 3.0, 3.3), ("noisy", 3.3, 3.9), ("recordings", 3.9, 4.6))
    newer = words(("the", 3.0, 3.3), ("nosy", 3.3, 3.9), ("recordings", 3.9, 4.6))
    assert joined(older, newer, at=3.4) == ["the", "nosy", "recordings"]
    assert joined(older, newer, at=4.4) == ["the", "noisy", "recordings"]


def test_stitch_without_agreement_splits_at_the_cut_point():
    older = words(("one", 3.0, 3.5), ("two", 3.5, 4.0))
    newer = words(("won", 3.0, 3.5), ("too", 3.5, 4.0))
    assert joined(older, newer, at=3.6) == ["one", "too"]


def test_stitch_over_silence_keeps_both_sides():
    older = words(("end.", 2.0, 2.6))
    newer = words(("Start", 4.6, 5.0))
    assert joined(older, newer, at=3.75) == ["end.", "Start"]


def test_a_word_said_twice_stays_twice():
    older = words(("very", 3.0, 3.3), ("very", 3.3, 3.7), ("much", 3.7, 4.1))
    newer = words(("very", 3.0, 3.35), ("very", 3.35, 3.7), ("much", 3.7, 4.2))
    assert joined(older, newer, at=3.5) == ["very", "very", "much"]


def test_the_first_segment_is_shown_as_read_and_closing_commits_it():
    t = Transcript()
    t.partial(Reading(0.0, 2.0, words(("Hello", 0.0, 0.5), ("there.", 0.5, 1.6))))
    assert t.final == [] and texts(t.preview()[1]) == ["Hello", "there."]
    t.close(Reading(0.0, 2.2, words(("Hello", 0.0, 0.5), ("there.", 0.5, 1.6))))
    assert texts(t.final) == ["Hello", "there."] and t.preview() == ([], [])


def test_a_full_segment_becomes_final_up_to_its_stitch_with_the_next():
    t = Transcript()
    first = words(("a", 0.0, 1.0), ("b", 1.0, 2.0), ("c", 2.0, 3.0), ("d", 3.0, 3.8), ("e", 3.8, 4.9))
    t.full(Reading(0.0, 5.0, first))
    assert t.final == [] and texts(t.preview()[0]) == ["a", "b", "c", "d", "e"]  # nothing follows it yet
    t.partial(Reading(2.5, 5.2, words(("c", 2.5, 3.0), ("d", 3.0, 3.8), ("e", 3.8, 4.8), ("f", 4.8, 5.2))))
    settled, tentative = t.preview()
    assert texts(settled) == ["a", "b", "c", "d"] and texts(tentative) == ["e", "f"]
    t.full(Reading(2.5, 7.5, words(("c", 2.5, 3.0), ("d", 3.0, 3.8), ("e", 3.8, 4.8), ("f", 4.8, 7.0))))
    assert texts(t.final) == ["a", "b", "c", "d"]
    assert texts(t.preview()[0]) == ["e", "f"]


def test_a_lone_thank_you_early_in_a_window_counts_as_nothing():
    def heard(ws):
        return read(lambda samples: ws, numpy.zeros(4 * 16000, numpy.float32), 16000).words

    assert heard([("Thank", 0.0, 0.3, 0.9), ("you.", 0.3, 0.8, 0.9)]) == ()
    assert heard([("you", 0.0, 0.4, 0.6)]) == ()  # what it says on the first, very short windows
    # said later in the window, or with anything else, it is kept
    assert len(heard([("Thank", 2.0, 2.4, 0.9), ("you.", 2.4, 2.8, 0.9)])) == 2
    assert len(heard([("Thank", 0.0, 0.3, 0.9), ("you.", 0.3, 0.8, 0.9), ("Bob.", 0.8, 1.2, 0.9)])) == 3


def test_read_places_words_in_the_recording():
    reading = read(lambda samples: [("Hi", 0.0, 0.5, 0.9)], numpy.zeros(16000, numpy.float32), 2 * 16000)
    assert reading == Reading(2.0, 3.0, (Word("Hi", 2.0, 2.5, 0.9),))
    assert segments.CUT_SECONDS == 1.25


def test_a_lone_bye_early_in_a_window_counts_as_nothing():
    def heard(ws):
        return read(lambda samples: ws, numpy.zeros(4 * 16000, numpy.float32), 0).words

    assert heard([("Bye.", 0.0, 0.5, 0.9)]) == ()
    assert len(heard([("Bye.", 2.0, 2.5, 0.9)])) == 1
    assert len(heard([("Good", 0.0, 0.3, 0.9), ("bye.", 0.3, 0.6, 0.9)])) == 2
