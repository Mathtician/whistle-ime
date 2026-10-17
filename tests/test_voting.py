import numpy
import pytest

from whistle_ime import voting as live
from whistle_ime.voting import Slot, Transcript, Window, align, normalize


def window(start, end, words, opens=None, closes=False):
    return Window(start, end, tuple(words), opens_recording=start == 0 if opens is None else opens, closes_recording=closes)


def test_normalize_ignores_case_and_punctuation_but_keeps_apostrophes():
    assert normalize('"Americans?"') == normalize("americans,") == "americans"
    assert normalize("Don't!") == "don't"
    assert normalize("—") == "—"


def test_weight_ramps_down_at_cuts_but_not_at_the_ends_of_the_recording():
    w = window(1.0, 6.0, [])
    assert w.weight(4.0) == 1.0
    assert w.weight(5.5) == pytest.approx(0.5)
    assert w.weight(6.0) == live.FLOOR
    assert w.weight(1.25) == pytest.approx(0.5)
    assert window(0.0, 5.0, []).weight(0.1) == 1.0
    assert window(1.0, 6.0, [], closes=True).weight(6.0) == 1.0


def test_window_rejects_words_outside_it():
    with pytest.raises(AssertionError):
        window(1.0, 6.0, [("late", 6.5, 0.9)])


def test_align_matches_by_end_time_and_inserts_in_order():
    slots = [Slot(time=1.0), Slot(time=2.0)]
    slots[0].vote("so", 1.0, 1.0)
    slots[1].vote("my", 2.0, 1.0)
    words = [("and", 0.5, 1.0), ("so", 1.05, 1.0), ("fellow", 1.5, 1.0), ("my", 2.1, 1.0)]
    assert align(slots, words) == [(None, 0), (0, 1), (None, 2), (1, 3)]


def test_align_prefers_the_closer_slot_for_a_word_between_two():
    slots = [Slot(time=7.16), Slot(time=7.56)]
    slots[0].vote("for", 7.16, 1.0)
    slots[1].vote("you", 7.56, 1.0)
    assert align(slots, [("you", 7.46, 1.0)]) == [(0, None), (1, 0)]


def test_align_keeps_unmatched_slots_and_words_in_time_order():
    slots = [Slot(time=1.0), Slot(time=3.0)]
    slots[0].vote("so", 1.0, 1.0)
    slots[1].vote("ask", 3.0, 1.0)
    words = [("and", 0.5, 1.0), ("fellow", 2.0, 1.0), ("not", 4.0, 1.0)]
    assert align(slots, words) == [(None, 0), (0, None), (None, 1), (1, None), (None, 2)]


def test_align_never_matches_words_too_far_apart():
    slots = [Slot(time=1.0)]
    slots[0].vote("so", 1.0, 1.0)
    assert align(slots, [("sew", 1.0 + live.MATCH_SECONDS + 0.01, 1.0)]) == [(0, None), (None, 0)]
    assert align(slots, [("so", 1.0 + live.SAME_WORD_SECONDS + 0.01, 1.0)]) == [(0, None), (None, 0)]
    # the slot's own word drifts further
    assert align(slots, [("So,", 1.0 + live.SAME_WORD_SECONDS - 0.01, 1.0)]) == [(0, 0)]


def test_mid_window_readings_outvote_a_confident_right_edge_guess():
    transcript = Transcript()
    transcript.add(window(0.0, 2.0, [("my", 1.3, 0.9), ("marriage", 1.95, 0.9)]))
    for end in (2.5, 3.0, 3.5, 4.0):
        transcript.add(window(0.0, end, [("my", 1.3, 0.9), ("fellow", 1.7, 0.9), ("Americans", 2.0, 0.9)]))
    assert transcript.text() == "my fellow Americans"


def test_a_word_later_windows_do_not_hear_disappears():
    transcript = Transcript()
    transcript.add(window(0.0, 3.0, [("so", 1.0, 0.9), ("um", 1.5, 0.9), ("my", 2.0, 0.9)]))
    assert transcript.text() == "so um my"
    transcript.add(window(0.0, 3.5, [("so", 1.0, 0.9), ("my", 2.0, 0.9)]))
    transcript.add(window(0.0, 4.0, [("so", 1.0, 0.9), ("my", 2.0, 0.9)]))
    assert transcript.text() == "so my"


def test_a_word_first_heard_late_hears_from_the_windows_that_covered_its_time_before():
    transcript = Transcript()
    for end in (3.0, 3.5, 4.0, 4.5, 5.0):
        transcript.add(window(0.0, end, [("country", 1.0, 1.0)]))
    transcript.add(window(0.8, 5.8, [("country", 1.0, 1.0), ("Thank", 1.5, 1.0), ("you.", 1.8, 1.0)], opens=False))
    assert transcript.text() == "country"


def test_a_new_word_at_the_right_edge_still_shows():
    transcript = Transcript()
    transcript.add(window(0.0, 1.8, [("so", 1.0, 1.0)]))
    transcript.add(window(0.0, 1.9, [("so", 1.0, 1.0)]))  # "my" ends at 1.9: not yet a word to hear
    transcript.add(window(0.0, 2.2, [("so", 1.0, 1.0), ("my", 1.9, 1.0)]))
    assert transcript.words()[-1] == ("my", False)


def test_silence_votes_down_what_was_there():
    transcript = Transcript()
    transcript.add(window(0.0, 2.0, [("hello", 1.0, 0.5)]))
    transcript.add(window(0.0, 2.5, []))
    assert transcript.text() == ""


def test_spellings_share_a_vote_and_the_most_voted_one_is_shown():
    transcript = Transcript()
    for end, form in ((3.0, "Americans!"), (3.5, "Americans,"), (4.0, "Americans,")):
        transcript.add(window(0.0, end, [(form, 2.0, 0.9)]))
    assert transcript.text() == "Americans,"
    assert len(transcript.slots) == 1


def test_new_words_are_unsettled_until_they_lead_by_enough():
    transcript = Transcript()
    transcript.add(window(0.0, 2.0, [("so", 1.9, 1.0)]))
    assert transcript.words() == [("so", False)]
    for end in (3.0, 3.5, 4.0):
        transcript.add(window(0.0, end, [("so", 1.9, 1.0)]))
    assert transcript.words() == [("so", True)]


def test_slots_behind_the_window_freeze_and_stay_in_the_text():
    transcript = Transcript()
    transcript.add(window(0.0, 5.0, [("and", 0.7, 1.0), ("so", 1.0, 1.0), ("my", 4.0, 1.0)]))
    transcript.add(window(2.0, 7.0, [("my", 4.0, 1.0), ("fellow", 4.5, 1.0)]))
    assert [slot.time for slot in transcript.slots] == [4.0, 4.5]
    assert transcript.text() == "and so my fellow"


def test_windows_must_not_move_backwards():
    transcript = Transcript()
    transcript.add(window(2.0, 7.0, []))
    with pytest.raises(AssertionError):
        transcript.add(window(1.0, 6.0, []))


def test_the_closing_window_trusts_its_right_edge_and_freezes_everything():
    transcript = Transcript()
    transcript.add(window(0.0, 2.0, [("you", 1.7, 1.0), ("are", 1.95, 1.0)]))
    transcript.add(window(0.0, 2.0, [("your", 1.8, 1.0), ("country", 1.98, 1.0)], closes=True))
    assert transcript.slots == []
    assert transcript.text() == "your country"



def test_a_lone_thank_you_early_in_a_window_counts_as_nothing():
    from whistle_ime.voting import Recording

    def heard(words):
        recording = Recording(16000)
        recording.append(numpy.zeros(4 * 16000, numpy.float32))
        return recording.observe(lambda samples: words).words

    assert heard([("Thank", 0.3, 0.9), ("you.", 0.8, 0.9)]) == ()
    assert heard([("you", 0.4, 0.6)]) == ()  # what it says on the first, very short windows
    # said later in the window, or with anything else, it is kept
    assert len(heard([("Thank", 2.4, 0.9), ("you.", 2.8, 0.9)])) == 2
    assert len(heard([("Thank", 0.3, 0.9), ("you.", 0.8, 0.9), ("Bob.", 1.2, 0.9)])) == 3
    assert len(heard([("Thank", 0.3, 0.9)])) == 1


def test_a_word_whose_end_drifts_later_stays_one_word():
    # "keys" ends at 2.1 s until the next word arrives, then at 2.48 s: more than MATCH_SECONDS apart
    transcript = Transcript()
    for end in (2.4, 2.5, 2.6, 2.7, 2.8):
        transcript.add(window(0.0, end, [("which", 1.7, 0.9), ("keys", 2.1, 0.9)]))
    for end in (3.0, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6):
        transcript.add(window(0.0, end, [("which", 1.7, 0.9), ("keys", 2.48, 0.9), ("should", 2.8, 0.9)]))
    assert transcript.text() == "which keys should"


def test_a_word_said_twice_stays_two_words():
    transcript = Transcript()
    for end in (2.0, 2.1, 2.2, 2.3, 2.4):
        transcript.add(window(0.0, end, [("very", 1.2, 0.9), ("very", 1.6, 0.9), ("much", 1.9, 0.9)]))
    assert transcript.text() == "very very much"
