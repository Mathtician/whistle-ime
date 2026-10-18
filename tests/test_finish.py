import pytest

from whistle_ime.finish import EndKey, finish

NONE = frozenset()


def key(name, text="", mods=NONE):
    return EndKey(name, text, frozenset(mods))


@pytest.mark.parametrize("preview, typed, expected", [
    (" and finish it.", ",", " and finish it,"),  # the model's period overridden
    (" and finish it?", ".", " and finish it."),
    (" apples, pears...", ",", " apples, pears,"),  # an ellipsis is trailing punctuation too
    (" and finish it", ",", " and finish it,"),  # nothing to replace: the mark is appended
    (" Wow!", "?", " Wow?"),
])
def test_typed_punctuation_replaces_the_previews_trailing_punctuation(preview, typed, expected):
    names = {",": "comma", ".": "period", "?": "question", "!": "exclam", ";": "semicolon", ":": "colon"}
    assert finish(preview, key(names[typed], typed)) == (expected, True)


def test_backspace_deletes_the_last_character_of_the_preview():
    assert finish(" and finish it.", key("BackSpace")) == (" and finish it", True)


@pytest.mark.parametrize("preview, expected", [
    (" Hello there, world.", " Hello there, "),  # the last word goes with its punctuation; what precedes it stays
    (" Hello there,world", " Hello there,"),  # no space: punctuation before the word is kept
    (" single", " "),
    ("single", ""),
    (" it's done.", " it's "),  # an apostrophe belongs to its word
    (" a well-known", " a well-"),  # a hyphen splits words, as in text editors
    (" wait —", " wait "),  # no word at the end: the last non-space run
])
def test_ctrl_backspace_deletes_the_last_word_of_the_preview(preview, expected):
    assert finish(preview, key("BackSpace", mods={"ctrl"})) == (expected, True)


@pytest.mark.parametrize("end", [key("a", "a"), key("Return"), key("Left"), key("comma", ",", {"ctrl"}),
                                 key("BackSpace", mods={"shift"}), key("minus", "-"), None])
def test_other_keys_leave_the_preview_alone_and_are_replayed(end):
    assert finish(" and finish it.", end) == (" and finish it.", False)


def test_with_nothing_left_to_commit_every_key_is_replayed():
    assert finish("", key("BackSpace")) == ("", False)
    assert finish("", key("comma", ",")) == ("", False)


def test_punctuation_and_backspace_continue_dictation_but_other_keys_and_chords_end_it():
    from whistle_ime.finish import continues_dictation

    def key(name, text="", *mods):
        return EndKey(name, text, frozenset(mods))

    assert all(continues_dictation(key(n, t)) for n, t in [("comma", ","), ("period", "."), ("question", "?")])
    assert continues_dictation(key("exclam", "!", "shift"))  # Shift is how ! is typed
    assert continues_dictation(key("BackSpace")) and continues_dictation(key("BackSpace", "", "ctrl"))
    assert not continues_dictation(key("a", "a")) and not continues_dictation(key("Return"))
    assert not continues_dictation(key("comma", ",", "ctrl")) and not continues_dictation(key("BackSpace", "", "alt"))
    assert not continues_dictation(None)
