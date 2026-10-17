from whistle_ime.frontend import Frontend, Key, Trigger

NONE = frozenset()
SUPER_ALT = frozenset({"super", "alt"})


def key(name, text="", mods=NONE, release=False, modifier=False):
    return Key(name, text, frozenset(mods), release, modifier)


TRIGGER_PRESS = key("v", mods=SUPER_ALT)
TRIGGER_RELEASE = key("v", mods=SUPER_ALT, release=True)


class UI:
    def __init__(self, before=None, cursor=(0, 0)):
        self.before, self.cursor = before, cursor
        self.out = []

    def commit(self, text):
        self.out.append(("commit", text))

    def preedit(self, settled, tentative):
        self.out.append(("preedit", settled, tentative))

    def forward(self, k):
        self.out.append(("forward", k.name))

    def status(self, message):
        pass

    def text(self):
        return "".join(e[1] for e in self.out if e[0] == "commit")


class Backend:
    def __init__(self):
        self.calls = []

    def start(self, before):
        self.calls.append(("start", before))

    def stop(self):
        self.calls.append(("stop",))

    def cancel(self):
        self.calls.append(("cancel",))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def frontend():
    clock = Clock()
    return Frontend(Backend(), Trigger.parse("Super+Alt+V"), clock), clock


def test_trigger_parsing_and_matching():
    t = Trigger.parse("Super+Alt+V")
    assert t == Trigger("v", SUPER_ALT)
    assert t.matches(key("V", mods=SUPER_ALT)) and not t.matches(key("v", mods={"super"}))
    assert not t.matches(key("v", mods={"super", "alt", "shift"}))
    assert Trigger.parse("Control+Alt+d") == Trigger("d", frozenset({"ctrl", "alt"}))


def test_the_trigger_starts_and_stops_and_both_of_its_events_are_swallowed():
    f, _ = frontend()
    ui = UI(before="Hello, ")
    assert f.key(ui, TRIGGER_PRESS) and f.state == "recording"
    assert f.key(ui, TRIGGER_RELEASE)
    assert f.backend.calls == [("start", "Hello, ")]
    assert f.key(ui, TRIGGER_PRESS) and f.state == "finishing"
    assert f.backend.calls[-1] == ("stop",)
    f.on_backend("COMMIT", "and so on")
    f.on_backend("DONE")
    assert f.state == "idle" and ui.text() == "and so on"
    assert ui.out[-1] == ("preedit", "", "")


def test_ordinary_keys_pass_through_while_idle():
    f, _ = frontend()
    ui = UI()
    assert not f.key(ui, key("a", "a")) and not f.key(ui, key("s", mods={"ctrl"}))
    assert f.backend.calls == []


def test_a_plain_key_while_dictating_stops_and_lands_after_the_final_text():
    f, _ = frontend()
    ui = UI(before="")
    f.key(ui, TRIGGER_PRESS)
    assert f.key(ui, key("x", "x")) and f.state == "finishing"
    assert f.key(ui, key("BackSpace"))  # pressed during the final pass: held too
    f.on_backend("COMMIT", "Hello")
    f.on_backend("DONE")
    assert ui.out[-3:] == [("preedit", "", ""), ("commit", "x"), ("forward", "BackSpace")]


def test_a_chord_while_dictating_stops_and_passes_straight_through():
    f, _ = frontend()
    ui = UI(before="")
    f.key(ui, TRIGGER_PRESS)
    assert not f.key(ui, key("Return", mods={"ctrl"})) and f.state == "finishing"
    f.on_backend("DONE")
    assert ("forward", "Return") not in ui.out


def test_modifiers_alone_never_stop_dictation():
    f, _ = frontend()
    ui = UI(before="")
    f.key(ui, TRIGGER_PRESS)
    assert not f.key(ui, key("Shift_L", modifier=True)) and f.state == "recording"


def test_escape_stops():
    f, _ = frontend()
    ui = UI(before="")
    f.key(ui, TRIGGER_PRESS)
    assert f.key(ui, key("Escape")) and f.state == "finishing"


def test_focus_loss_cancels_and_drops_everything_until_done():
    f, _ = frontend()
    ui = UI(before="")
    f.key(ui, TRIGGER_PRESS)
    f.on_backend("COMMIT", "kept")
    f.focus_out(ui)
    assert f.state == "cancelling" and f.backend.calls[-1] == ("cancel",)
    f.on_backend("COMMIT", "dropped")
    f.on_backend("PREEDIT", "also", "dropped")
    f.on_backend("DONE")
    assert f.state == "idle" and ui.text() == "kept"


def test_context_prefers_the_report_then_the_shadow_then_the_empty_report():
    f, clock = frontend()
    reporting = UI(before="Done. ")
    assert f.context(reporting) == "Done. "
    terminal = UI(before=None)
    assert f.context(terminal) is None
    empty = UI(before="")
    assert f.context(empty) == ""
    assert f.context(UI(before="x" * 500)) == "x" * 200


def test_the_shadow_carries_a_sentence_across_dictations_in_a_silent_app():
    f, clock = frontend()
    term = UI(before=None)
    f.key(term, TRIGGER_PRESS)
    assert f.backend.calls[-1] == ("start", None)  # nothing known yet
    f.key(term, TRIGGER_PRESS)
    f.on_backend("COMMIT", "When I start a sentence like this.")
    f.on_backend("DONE")
    clock.now += 5
    f.key(term, key("BackSpace"))  # remove the period
    f.key(term, TRIGGER_PRESS)
    assert f.backend.calls[-1] == ("start", "When I start a sentence like this")


def test_a_cursor_move_nobody_caused_or_a_navigation_key_kills_the_shadow():
    f, clock = frontend()
    term = UI(before=None)
    f.key(term, TRIGGER_PRESS)
    f.key(term, TRIGGER_PRESS)
    f.on_backend("COMMIT", "text")
    f.on_backend("DONE")
    term.cursor = (5, 0)
    clock.now += 0.3
    f.cursor_moved(term)  # the redraw after our commit: followed
    assert f.shadow.usable(term, term.cursor)
    clock.now += 10
    term.cursor = (0, 9)
    f.cursor_moved(term)  # a click
    assert not f.shadow.tracks(term)
    f2, _ = frontend()
    t2 = UI(before=None)
    f2.key(t2, TRIGGER_PRESS)
    f2.key(t2, TRIGGER_PRESS)
    f2.on_backend("COMMIT", "text")
    f2.on_backend("DONE")
    f2.key(t2, key("Left"))
    assert not f2.shadow.tracks(t2)


def test_focus_out_of_another_field_does_not_cancel():
    f, _ = frontend()
    a, b = UI(before=""), UI(before="")
    f.key(a, TRIGGER_PRESS)
    f.focus_out(b)
    assert f.state == "recording"


def test_errors_are_shown_to_the_owner():
    f, _ = frontend()
    shown = []
    ui = UI(before="")
    ui.status = shown.append
    f.key(ui, TRIGGER_PRESS)
    f.on_backend("ERROR", "cannot record: pw-record")
    assert "⚠ cannot record: pw-record" in shown
