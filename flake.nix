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
      # Builds with this flake's nixpkgs. On NixOS use the module instead: it builds against the system's
      # nixpkgs, which the addon needs to match the running fcitx5.
      packages.${system} = rec {
        backend = pkgs.callPackage ./nix/backend.nix { };
        addon = pkgs.callPackage ./addon/package.nix { };
        default = addon;
      };

      nixosModules.default = ./nix/module.nix;

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
