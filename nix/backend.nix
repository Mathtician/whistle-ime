# The backend: a Python program speaking the addon's protocol on stdin/stdout. Whistle's engine and weights
# are pinned downloads (Apache-2.0, huggingface.co/Cactus-Compute); the launcher passes their paths.
{
  lib,
  python3Packages,
  fetchurl,
  runCommand,
  unzip,
  pipewire,
}:
let
  # libneedle.so depends only on glibc, so it loads from a Nix Python unpatched.
  engine =
    runCommand "libneedle-3.1.0"
      {
        src = fetchurl {
          url = "https://huggingface.co/Cactus-Compute/needle3/resolve/93e88ede867e4311c39151285e74df513e4b835b/python/cactus_needle-3.1.0-py3-none-manylinux2014_x86_64.whl";
          hash = "sha256-oPwvaC7xhSXU0YWDCBrDHB9vIRq+bAs77kSBHtfgNH4=";
        };
        nativeBuildInputs = [ unzip ];
      }
      ''
        mkdir -p $out/lib
        unzip -p $src needle/libneedle3.so > $out/lib/libneedle.so
      '';
  weights = fetchurl {
    url = "https://huggingface.co/Cactus-Compute/whistle/resolve/ca5287601bef25af26dcf2e1b2bdc0843a7c19e5/whistle.cact";
    hash = "sha256-tuAvBIVorF0BogQlVsZYBh5pmsvAqioUOfUvPUYd/+s=";
  };
in
python3Packages.buildPythonApplication {
  pname = "whistle-ime-backend";
  version = "0.1.0";
  pyproject = true;
  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../pyproject.toml
      ../README.md
      ../src
      ../tests
    ];
  };
  build-system = [ python3Packages.hatchling ];
  dependencies = [ python3Packages.numpy ];
  nativeCheckInputs = [ python3Packages.pytestCheckHook ];
  makeWrapperArgs = [
    "--set WHISTLE_IME_LIB ${engine}/lib/libneedle.so"
    "--set WHISTLE_IME_WEIGHTS ${weights}"
    "--prefix PATH : ${lib.makeBinPath [ pipewire ]}" # pw-record
  ];
  passthru = { inherit engine weights; };
  meta = {
    description = "Dictation backend for the whistle-ime fcitx5 addon: Whistle speech-to-text with live voting";
    license = lib.licenses.mit;
    platforms = [ "x86_64-linux" ];
    mainProgram = "whistle-ime-backend";
  };
}
