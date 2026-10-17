#!/bin/sh
# Install the IBus engine from a source checkout on Debian/Ubuntu, for development (distro packaging comes later).
# Needs: python3-numpy gir1.2-ibus-1.0 pipewire-bin. Usage: sudo ibus/install-dev.sh <libneedle.so> <whistle.cact>
set -eu
[ "$#" -eq 2 ] || { echo "usage: $0 <libneedle.so> <whistle.cact>" >&2; exit 2; }
here=$(cd "$(dirname "$0")/.." && pwd)
prefix=/usr/local/lib/whistle-ime
exec_path=/usr/local/libexec/ibus-engine-whistle
install -d "$prefix" /usr/local/libexec
rm -rf "$prefix/src" && cp -r "$here/src" "$prefix/src"
install -m 644 "$1" "$prefix/libneedle.so"
install -m 644 "$2" "$prefix/whistle.cact"
cat > "$exec_path" <<LAUNCHER
#!/bin/sh
export WHISTLE_IME_LIB=$prefix/libneedle.so WHISTLE_IME_WEIGHTS=$prefix/whistle.cact PYTHONPATH=$prefix/src
[ -f "\$HOME/.config/whistle-ime/debug" ] && export WHISTLE_IME_DEBUG=1  # touch it to log every key
exec python3 -m whistle_ime.ibus_engine "\$@"
LAUNCHER
chmod 755 "$exec_path"
sed "s|@EXEC@|$exec_path --ibus --language en|" "$here/ibus/whistle.xml.in" > /usr/share/ibus/component/whistle.xml
echo "installed; run 'ibus restart' in the desktop session, then add the Whistle input source"
