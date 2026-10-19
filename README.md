# whistle-ime

Dictation into any text field, with a live preview. Press **Super+Alt+V**, speak, and your words appear at
the cursor as provisional (underlined) text that firms up as you talk; press it again to finish.
Speech recognition is [Whistle](https://huggingface.co/Cactus-Compute/whistle), a 17 MB model running
locally on the CPU.

Text goes in through the desktop's input-method system, with no simulated keystrokes and no clipboard: as an
[fcitx5](https://fcitx-im.org) addon on KDE Plasma, and as an [IBus](https://github.com/ibus/ibus) engine on
GNOME (including stock Ubuntu). Both share one backend.

## Install on KDE (NixOS)

```nix
# flake.nix
inputs.whistle-ime = {
  url = "github:Mathtician/whistle-ime";
  inputs.nixpkgs.follows = "nixpkgs";
};

# configuration.nix (with `inputs` passed through specialArgs)
imports = [ inputs.whistle-ime.nixosModules.default ];
services.whistle-ime = {
  enable = true;
  language = "en";            # en de fr es it nl pl, or null to detect
  keywords = [ "Siobhan" ];   # favoured words; they also keep their capital
  trigger = "Super+Alt+V";    # fcitx5's notation
  interleave = false;         # true: typed punctuation and Backspace don't stop dictation
};
```

KWin checks its own global shortcuts before an input method sees a key, so pick a trigger KWin doesn't use;
the addon logs a warning (`journalctl --user -b | grep whistle-ime`) if it finds one in
`~/.config/kglobalshortcutsrc`. `whistle-ime toggle` does the same as the trigger, for scripts or a desktop
shortcut, and prints what happened (`started`, `stopping`, `no text field has focus`, ...).

The module enables fcitx5 with its Wayland frontend. On KDE, select **Fcitx 5** under System Settings >
Keyboard > Virtual Keyboard so KWin launches it, and make sure nothing else autostarts fcitx5 (the fcitx5
package's own autostart entry will otherwise grab its D-Bus name first; hide it with a
`~/.config/autostart/org.fcitx.Fcitx5.desktop` containing `Hidden=true`).

## Install on GNOME / Ubuntu (development)

Distribution packages are on the way; until then, on Debian or Ubuntu with a source checkout:

```sh
sudo apt install python3-numpy gir1.2-ibus-1.0 pipewire-bin
sudo ibus/install-dev.sh path/to/libneedle.so path/to/whistle.cact   # pinned in nix/backend.nix
ibus restart
```

Then add **Whistle dictation** under Settings > Keyboard > Input Sources (it is listed under English) and
switch to it. Its keyboard layout is "default", so it keeps whichever layout you used last: switch to your
layout, then to Whistle, and type normally; Super+Alt+V dictates. Settings go in
`~/.config/whistle-ime/settings.ini`, followed by `ibus restart`:

```ini
[whistle-ime]
trigger = Super+Alt+V
language = en
keywords = Siobhan, Krzysztof Nowak
```

GNOME asks the input method about each key before running its own shortcuts, so the trigger always reaches
Whistle; the engine logs a warning (`~/.cache/whistle-ime/engine.log`) if a GNOME shortcut uses the same
chord, since that shortcut stops working while Whistle is the input source. Ubuntu, for example, binds
Ctrl+Alt+D to Show Desktop.

## Using it

- **The trigger** (Super+Alt+V): start; again (or Escape): stop. A final pass commits the rest.
- **Typing while dictating** stops dictation, and the key decides how the dictation ends:

  | key | what happens to the provisional text |
  |---|---|
  | `,` `.` `;` `:` `!` `?` | committed with its trailing punctuation replaced by yours |
  | Backspace | committed without its last character |
  | Ctrl+Backspace | committed without its last word |
  | anything else | committed, then the key is typed after it |

  With `interleave`, punctuation, Backspace and Ctrl+Backspace do the same and dictation goes on, so you
  can speak and punctuate as you go; what you say next is heard from 0.2 s after the last key.

- **Focus leaving the field** discards whatever is still provisional; committed text stays.
- Dictations under 5 s are committed when you stop; in longer ones, text becomes final 4 to 6 s after you
  say it. Until then it is preedit and may change.
- The first word is fitted to the text before the cursor: capitalized at the start of a field or sentence,
  lowercased mid-sentence, with a space added where needed. Apps that don't report that text (terminals;
  Konsole stopped on purpose in [konsole!800](https://invent.kde.org/utilities/konsole/-/merge_requests/800))
  get a reconstruction from what was dictated and typed there since, valid until focus leaves, a key moves
  the cursor (Enter, arrows, Home, Tab, chords) or the cursor moves by itself (a click).

## How it works

The fcitx5 addon (`addon/`, C++) is thin: it keeps one backend process running so the model stays loaded,
maps the trigger key (or the D-Bus method `Toggle`, `org.fcitx.Fcitx.WhistleIme1` at `/whistle_ime`, which
`whistle-ime toggle` calls) to `START`/`STOP`, and applies what the backend sends. The IBus engine
(`src/whistle_ime/ibus_engine.py`) does the same over `frontend.py`, with the backend in a thread of its own
process.

The backend (`src/whistle_ime/`, Python) records with `pw-record` and cuts the recording into 5 s segments
starting every 2.5 s. Every 0.1 s it transcribes the open segment, from its start to the newest audio, and
shows that as the preview; a dictation under 5 s is just that, committed with one last pass when you stop.
Whistle reads a window cleanly except near its cuts, so consecutive segments are stitched in the middle of
their overlap, at a word both read alike (same word, overlapping in time), and each moment comes from the
segment where it lies furthest from a cut; the older segment's words up to the stitch are then committed.
`segments.py` has the details; `context.py` has the spacing and casing rules, which follow Android's
keyboard (AOSP LatinIME) for sentence boundaries.

Windows are 5 s because Whistle's encoder gets about 5x slower past 64 frames (5.12 s).

Protocol between addon and backend, one message per line, tab-separated:

| addon to backend | |
|---|---|
| `START <known> <before>` | begin; `<known>` is 1 if the app reports the text before the cursor, which follows (escaped) |
| `STOP [<name> <mods> <text>]` | finish: final pass, commit the rest; if a key ended the dictation, its keysym name, modifiers and text, from which `finish.py` decides what to commit |
| `CANCEL` | finish, dropping uncommitted text |
| `CONTINUE <known> <before>` | after `NEXT`: go on dictating, with the context as in `START` |

| backend to addon | |
|---|---|
| `READY` | model loaded |
| `COMMIT <text>` | insert text; never revised |
| `PREEDIT <settled> <tentative>` | the uncommitted rest, replacing the previous preedit |
| `DONE <absorbed>` | session over; `<absorbed>` is 1 if the key that ended it was used up, else the front end types it after the commit |
| `NEXT <absorbed>` | with `--interleave`: as `DONE`, but dictation goes on; the front end answers `CONTINUE` |
| `ERROR <message>` | |

## Development

To collect real sessions for tuning (for example hallucinations on background noise), `touch
~/.config/whistle-ime/save-sessions`: each dictation then saves its audio and a JSON log of every model pass
and every message to `~/.cache/whistle-ime/sessions/`. Of the text before the cursor only its length is kept.
Remove the file to stop, and delete the folder when done.

`nix develop` gives cmake and fcitx5 for the addon; the backend is a uv project (`uv run pytest`). Running
the backend outside Nix needs `WHISTLE_IME_LIB` (libneedle.so) and `WHISTLE_IME_WEIGHTS` (whistle.cact),
which `nix/backend.nix` pins.

## Credits and licences

whistle-ime is MIT licensed. The Whistle weights and the Needle engine (libneedle.so) are by
[Cactus Compute](https://huggingface.co/Cactus-Compute), Apache-2.0, and are downloaded at build time, not
redistributed here. The addon's structure and several fcitx5 details follow
[fcitx5-voiceinput](https://github.com/GavinLiuOnline/fcitx5-voiceinput) (MIT).
