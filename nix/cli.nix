# whistle-ime: the command a desktop shortcut runs. It asks the addon, over fcitx5's D-Bus connection, to
# start dictating into the focused text field or to stop.
{ writeShellApplication, dbus }:
writeShellApplication {
  name = "whistle-ime";
  runtimeInputs = [ dbus ];
  text = ''
    usage="usage: whistle-ime [toggle]

    Start dictating into the focused text field, or stop. Bind this to a shortcut in your desktop's settings.
    While dictating, Escape also stops, and typing stops and then types after the dictated text."
    case "$*" in
      "" | toggle) ;;
      -h | --help) echo "$usage"; exit 0 ;;
      *) echo "$usage" >&2; exit 2 ;;
    esac
    if ! reply=$(dbus-send --session --print-reply=literal --dest=org.fcitx.Fcitx5 /whistle_ime \
                   org.fcitx.Fcitx.WhistleIme1.Toggle 2>&1); then
      echo "whistle-ime: the fcitx5 addon did not answer: $reply" >&2
      echo "Is fcitx5 running with whistle-ime? On KDE, select Fcitx 5 under System Settings > Keyboard > Virtual Keyboard." >&2
      exit 1
    fi
    reply="''${reply#"''${reply%%[![:space:]]*}"}"  # dbus-send indents its reply
    echo "$reply"
  '';
}
