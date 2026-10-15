import pytest

from whistle_ime.context import fit_first_word, needs_space, starts_sentence, unescape


@pytest.mark.parametrize("before, expected", [
    ("", False), ("word", True), ("word,", True), ("word ", False), ("line\n", False), ("(", False),
    ("“", False), ("tab\t", False),
])
def test_needs_space(before, expected):
    assert needs_space(before) is expected


@pytest.mark.parametrize("before, expected", [
    ("", True), ("   ", True), ("Dear Bob,\n", True), ("Done.", True), ("Done. ", True), ("Really?", True),
    ("Wow!  ", True), ("He said “stop.” ", True), ("(See above.) ", True), ("Wait…", True),
    ("like this, ", False), ("like this", False), ("a list:", False),
    ("e.g. ", True),  # abbreviations look like sentence ends
])
def test_starts_sentence(before, expected):
    assert starts_sentence(before) is expected


@pytest.mark.parametrize("word, before, expected", [
    ("And", "like this, ", "and"),
    ("And", "Done. ", "And"),
    ("and", "Done. ", "And"),
    ("And", None, "And"),  # nothing reported: Whistle's casing stands
    ("and", None, "and"),
    ("And", "", "And"),  # an empty field is the start
    ("I", "and then ", "I"),
    ("I'm", "and then ", "I'm"),
    ("NASA", "and then ", "NASA"),
    ("Siobhan,", "and then ", "Siobhan,"),  # a keyword
    ("Hello,", "and then ", "hello,"),
    ("“Stop", "he said ", "“stop"),
    ("“stop", "He said. ", "“Stop"),
    ("42", "about ", "42"),
])
def test_fit_first_word(word, before, expected):
    assert fit_first_word(word, before, keywords=["Siobhan", "Krzysztof Nowak"]) == expected


def test_every_word_of_a_keyword_is_spared():
    assert fit_first_word("Nowak", "and ", keywords=["Krzysztof Nowak"]) == "Nowak"


def test_unescape_reverses_the_addon_escaping():
    assert unescape(r"a\\b\tc\nd") == "a\\b\tc\nd"
