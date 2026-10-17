from whistle_ime.collisions import gnome_collisions, parse_gnome_accel
from whistle_ime.frontend import Trigger


def test_parse_gnome_accelerators():
    assert parse_gnome_accel("<Super><Alt>v") == Trigger("v", frozenset({"super", "alt"}))
    assert parse_gnome_accel("<Primary><Alt>d") == Trigger("d", frozenset({"ctrl", "alt"}))
    assert parse_gnome_accel("<Control><Shift>V") == Trigger("v", frozenset({"ctrl", "shift"}))
    assert parse_gnome_accel("") is None
    assert parse_gnome_accel("<Hyper>x") is None


def test_collisions_are_found_by_chord_not_by_spelling():
    trigger = Trigger.parse("Super+Alt+V")
    bindings = {
        ("org.gnome.desktop.wm.keybindings", "minimize"): ["<Super>h"],
        ("org.gnome.shell.keybindings", "clipboard"): ["<Alt><Super>V"],  # other order, other case
        ("custom", "terminal"): ["<Primary><Alt>t"],
        ("org.gnome.desktop.wm.keybindings", "switch-to-workspace-down"): ["<Control><Alt>Down"],
    }
    assert gnome_collisions(trigger, bindings) == [("org.gnome.shell.keybindings", "clipboard")]
    assert gnome_collisions(Trigger.parse("Control+Alt+D"), bindings) == []  # "Down" is not "d"
