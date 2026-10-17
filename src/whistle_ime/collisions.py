"""Does the desktop already use the dictation trigger?

On GNOME the input method sees keys before GNOME's own shortcuts, so a shared chord silently disables the GNOME
shortcut; on KDE, KWin sees them first, so a shared chord silently disables dictation (the fcitx5 addon checks
that case itself). Either way the user should hear about it, so the front ends warn at startup.
"""
from __future__ import annotations

import re

from whistle_ime.frontend import Trigger

GNOME_MODS = {"super": "super", "alt": "alt", "primary": "ctrl", "control": "ctrl", "ctrl": "ctrl",
              "shift": "shift", "meta": "super"}


def parse_gnome_accel(accel):
    """"<Super><Alt>v" -> Trigger("v", {"super", "alt"}); None for an empty or disabled binding."""
    mods = re.findall(r"<([^>]+)>", accel)
    name = re.sub(r"<[^>]+>", "", accel).strip()
    if not name:
        return None
    known = [GNOME_MODS.get(m.lower()) for m in mods]
    if None in known:
        return None  # a modifier we don't model (Hyper, ...): can't be our chord
    return Trigger(name.lower(), frozenset(known))


def gnome_collisions(trigger, bindings):
    """The (schema, key) names among `bindings` ({(schema, key): [accelerators]}) that use `trigger`."""
    return sorted(where for where, accels in bindings.items()
                  if any(parse_gnome_accel(a) == trigger for a in accels))
