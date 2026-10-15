# The fcitx5 addon. Build it against the same nixpkgs as the running fcitx5 (the system flake makes this
# flake's nixpkgs follow its own), or the addon may not load.
{
  lib,
  stdenv,
  cmake,
  pkg-config,
  fcitx5,
}:
stdenv.mkDerivation {
  pname = "fcitx5-whistle-ime";
  version = "0.1.0";
  src = ./.;
  nativeBuildInputs = [ cmake pkg-config ];
  buildInputs = [ fcitx5 ];
  meta = {
    description = "fcitx5 addon: dictation with live preview, backed by whistle-ime-backend";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
  };
}
