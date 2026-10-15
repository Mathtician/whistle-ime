{
  description = "Dictation with live preview into any Wayland text field: an fcitx5 addon and a Whistle backend";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { nixpkgs, ... }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
    in
    {
      # The addon. The backend is a uv project (pyproject.toml); the addon's Backend option runs it.
      packages.${system}.default = pkgs.callPackage ./addon/package.nix { };

      devShells.${system}.default = pkgs.mkShell {
        packages = [
          pkgs.cmake
          pkgs.pkg-config
          pkgs.fcitx5
          pkgs.uv
        ];
      };
    };
}
