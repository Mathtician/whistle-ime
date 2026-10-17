from whistle_ime.whistle import without_quotes


def test_quotation_marks_go_but_apostrophes_stay():
    assert without_quotes('"Thank') == "Thank"
    assert without_quotes("«Bonjour") == "Bonjour"
    assert without_quotes("don't") == "don't" and without_quotes("don’t") == "don’t"
    assert without_quotes('"') is None


def test_a_word_a_quote_closes_is_dropped_since_its_time_is_the_quotes():
    assert without_quotes('much."') is None
    assert without_quotes("„Hallo“") is None
    assert without_quotes("Bonjour»") is None
