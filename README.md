# whistle-ime

Dictation into any Wayland text field, with a live preview. Press your dictation shortcut, speak, and your
words appear at the cursor as provisional (underlined) text that firms up as you talk; press it again to
finish.
Speech recognition is [Whistle](https://huggingface.co/Cactus-Compute/whistle), a 17 MB model running
locally on the CPU.

It is an [fcitx5](https://fcitx-im.org) addon, so text goes in through the input-method protocol: no
simulated keystrokes, no clipboard. Developed on KDE Plasma (KWin).

## Install (NixOS)

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
};
```

Then bind the command `whistle-ime` to a shortcut of your choosing, next to your others:

- **KDE**: System Settings > Keyboard > Shortcuts > Add New > Command or Script, command `whistle-ime`.
- **Sway**: `bindsym $mod+Alt+v exec whistle-ime` (Hyprland, river etc. alike).

`whistle-ime` asks the addon over fcitx5's D-Bus connection to start dictating into the focused text field,
or to stop; it prints what happened (`started`, `stopping`, `no text field has focus`, ...).

The module enables fcitx5 with its Wayland frontend. On KDE, select **Fcitx 5** under System Settings >
Keyboard > Virtual Keyboard so KWin launches it, and make sure nothing else autostarts fcitx5 (the fcitx5
package's own autostart entry will otherwise grab its D-Bus name first; hide it with a
`~/.config/autostart/org.fcitx.Fcitx5.desktop` containing `Hidden=true`).

If the first word of a dictation gets cut off, your microphone may be fading in after PipeWire suspended
it. Disabling suspension for inputs fixes it:

```nix
services.pipewire.wireplumber.extraConfig."51-mic-no-suspend"."monitor.alsa.rules" = [{
  matches = [ { "node.name" = "~alsa_input.*"; } ];
  actions.update-props."session.suspend-timeout-seconds" = 0;
}];
```

## Using it

- **Your shortcut** (`whistle-ime`): start; again (or Escape): stop. A final pass commits the rest.
- **Typing while dictating** stops dictation; the key lands after the dictated text.
- **Focus leaving the field** discards whatever is still provisional; committed text stays.
- Text becomes final about 5 s after you say it; until then it is preedit and may change.
- The first word is fitted to the text before the cursor: capitalized at the start of a field or sentence,
  lowercased mid-sentence, with a space added where needed. Apps that don't report that text (terminals;
  Konsole stopped on purpose in [konsole!800](https://invent.kde.org/utilities/konsole/-/merge_requests/800))
  get a reconstruction from what was dictated and typed there since, valid until focus leaves, a key moves
  the cursor (Enter, arrows, Home, Tab, chords) or the cursor moves by itself (a click).

## How it works

The addon (`addon/`, C++) is thin: it keeps one backend process running so the model stays loaded, maps the
D-Bus method `Toggle` (`org.fcitx.Fcitx.WhistleIme1` at `/whistle_ime`, called by the `whistle-ime`
command) to `START`/`STOP`, and applies what the backend sends.

The backend (`src/whistle_ime/`, Python) records with `pw-record` and, every 0.1 s, transcribes the last 5 s
again. Every word is heard by dozens of overlapping windows, and the windows vote: each window's words are
aligned to the running transcript by their end times (consistent across windows to about 80 ms), votes are
weighted by confidence and by distance from the window's cuts (where the model truncates or invents
words), and a word is committed once no future window can reach it. `voting.py` has the details;
`context.py` has the spacing and casing rules, which follow Android's keyboard (AOSP LatinIME) for
sentence boundaries.

Windows are 5 s because Whistle's encoder gets about 5x slower past 64 frames (5.12 s).

Protocol between addon and backend, one message per line, tab-separated:

| addon to backend | |
|---|---|
| `START <known> <before>` | begin; `<known>` is 1 if the app reports the text before the cursor, which follows (escaped) |
| `STOP` | finish: final pass, commit the rest |
| `CANCEL` | finish, dropping uncommitted text |

| backend to addon | |
|---|---|
| `READY` | model loaded |
| `COMMIT <text>` | insert text; never revised |
| `PREEDIT <settled> <tentative>` | the uncommitted rest, replacing the previous preedit |
| `DONE` | session over |
| `ERROR <message>` | |

## Development

`nix develop` gives cmake and fcitx5 for the addon; the backend is a uv project (`uv run pytest`). Running
the backend outside Nix needs `WHISTLE_IME_LIB` (libneedle.so) and `WHISTLE_IME_WEIGHTS` (whistle.cact),
which `nix/backend.nix` pins.

## Credits and licences

whistle-ime is MIT licensed. The Whistle weights and the Needle engine (libneedle.so) are by
[Cactus Compute](https://huggingface.co/Cactus-Compute), Apache-2.0, and are downloaded at build time, not
redistributed here. The addon's structure and several fcitx5 details follow
[fcitx5-voiceinput](https://github.com/GavinLiuOnline/fcitx5-voiceinput) (MIT).
