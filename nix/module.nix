# services.whistle-ime: the fcitx5 addon, its backend and the `whistle-ime` command, configured. All are
# built with the system's nixpkgs, so the addon matches the running fcitx5. Dictation starts and stops with
# the trigger key; `whistle-ime toggle` does the same from scripts or a desktop shortcut.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.whistle-ime;
  backend = pkgs.callPackage ./backend.nix { };
  addon = pkgs.callPackage ../addon/package.nix { };
  cli = pkgs.callPackage ./cli.nix { };
  command = lib.escapeShellArgs (
    [ (lib.getExe backend) ]
    ++ lib.optionals (cfg.language != null) [ "--language" cfg.language ]
    ++ lib.optionals (cfg.keywords != [ ]) [ "--keywords" (lib.concatStringsSep "," cfg.keywords) ]
    ++ lib.optionals cfg.interleave [ "--interleave" ]
  );
in
{
  options.services.whistle-ime = {
    enable = lib.mkEnableOption "dictation into the focused text field with whistle-ime (an fcitx5 addon)";
    trigger = lib.mkOption {
      type = lib.types.str;
      default = "Super+Alt+V";
      description = ''
        fcitx5 key that starts and stops dictation, in fcitx5's notation ("Control+Alt+D"). KWin checks its
        global shortcuts first, so a chord KWin also uses never reaches the addon; it logs a warning then.
      '';
    };
    language = lib.mkOption {
      type = lib.types.nullOr (lib.types.enum [ "en" "de" "fr" "es" "it" "nl" "pl" ]);
      default = "en";
      description = "Language to transcribe; null detects it in each 5 s window, which can flip mid-sentence.";
    };
    keywords = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "Siobhan" "Krzysztof Nowak" ];
      description = "Words and phrases to favour; they also keep their capital at the start of a dictation.";
    };
    interleave = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Interleave typing and dictation: punctuation (, . ; : ! ?), Backspace or Ctrl+Backspace typed while
        dictating does what it does at the end of a dictation (punctuation replaces the preview's own, Backspace
        deletes from it), commits, and dictation goes on 0.2 s after the last key. The trigger key, Escape and
        other keys stop dictation as usual.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = config.i18n.inputMethod.type == "fcitx5";
        message = "services.whistle-ime is an fcitx5 addon: set i18n.inputMethod.type = \"fcitx5\".";
      }
      {
        assertion = lib.all (k: !(lib.hasInfix "," k)) cfg.keywords;
        message = "services.whistle-ime.keywords cannot contain commas (the backend takes them comma separated).";
      }
    ];
    i18n.inputMethod = {
      enable = lib.mkDefault true;
      type = lib.mkDefault "fcitx5";
      # Leave GTK_IM_MODULE/QT_IM_MODULE unset so apps use Wayland's text-input; select "Fcitx 5" under
      # System Settings > Keyboard > Virtual Keyboard so KWin launches it.
      fcitx5.waylandFrontend = lib.mkDefault true;
      fcitx5.addons = [ addon ];
      fcitx5.settings.addons.whistle-ime = {
        globalSection.Backend = "exec ${command}";
        sections.TriggerKey."0" = cfg.trigger;
      };
    };
    environment.systemPackages = [ cli ];
  };
}
