#!/bin/bash
# Install strata-takeout (and strata-filecat if it is checked out next to it) into a private venv and link the tools into ~/.local/bin.
#   scripts/install.sh            venv: ~/.local/share/strata/venv   links: ~/.local/bin/{filecat,strata-takeout-safe-extract,strata-takeout-inbox,takeout-pull,takeout-extract,setup-exports-disk}
# Both packages go into ONE venv: filecat finds the `takeout` profile through its entry point group (filecat.profiles).
# Links are replaced atomically, so a running script keeps its old file. Units are in systemd/ (copy or `chezmoi add` them yourself).
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd); FC=${STRATA_FILECAT:-$HERE/../strata-filecat}
VENV=${STRATA_VENV:-$HOME/.local/share/strata/venv}; BIN=${BIN:-$HOME/.local/bin}
[ -d "$VENV" ] || uv venv -q "$VENV"
args=(-e "$HERE"); [ -d "$FC" ] && args+=(-e "$FC") || echo "note: $FC not found: filecat is not installed (strata-filecat is optional)" >&2
uv pip install -q --python "$VENV/bin/python" "${args[@]}"
mkdir -p "$BIN"
link() { ln -s "$1" "$BIN/.$2.new" && mv -T "$BIN/.$2.new" "$BIN/$2"; echo "$BIN/$2 -> $1"; }
link "$VENV/bin/strata-takeout-safe-extract" strata-takeout-safe-extract
link "$VENV/bin/strata-takeout-inbox" strata-takeout-inbox
[ -x "$VENV/bin/filecat" ] && link "$VENV/bin/filecat" filecat
for s in scripts/google/takeout-pull scripts/google/takeout-extract scripts/setup-exports-disk; do link "$HERE/$s" "$(basename "$s")"; done
