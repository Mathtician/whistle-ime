# services.whistle-ime: the fcitx5 addon and its backend, configured. Both are built with the system's
# nixpkgs, so the addon matches the running fcitx5.
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
  command = lib.escapeShellArgs (
    [ (lib.getExe backend) ]
    ++ lib.optionals (cfg.language != null) [ "--language" cfg.language ]
    ++ lib.optionals (cfg.keywords != [ ]) [ "--keywords" (lib.concatStringsSep "," cfg.keywords) ]
  );
in
{
  options.services.whistle-ime = {
    enable = lib.mkEnableOption "dictation into the focused text field with whistle-ime (an fcitx5 addon)";
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
    triggerKey = lib.mkOption {
      type = lib.types.str;
      default = "Super+Alt+V";
      description = "fcitx5 key that starts and stops dictation. KDE global shortcuts take precedence over it.";
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
        sections.TriggerKey."0" = cfg.triggerKey;
      };
    };
  };
}
