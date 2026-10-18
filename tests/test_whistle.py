from whistle_ime.whistle import without_quotes


def test_quotation_marks_go_but_apostrophes_stay():
    assert without_quotes('"Thank') == "Thank"
    assert without_quotes('much."') == "much."
    assert without_quotes("«Bonjour»") == "Bonjour"
    assert without_quotes("„Hallo“") == "Hallo"
    assert without_quotes("don't") == "don't" and without_quotes("don’t") == "don’t"
    assert without_quotes(' " ') == ""
