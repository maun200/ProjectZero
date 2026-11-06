#!/bin/sh
# ProjectZero installer for Linux and macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/maun200/ProjectZero/main/install.sh | sh
#
# What it does (no sudo, nothing system-wide):
#   1. finds Python 3.11+ on your computer
#   2. creates a private virtual environment in ~/.local/share/projectzero
#   3. installs ProjectZero into it and links the `projectzero` command to ~/.local/bin
#   4. starts the app in your browser (set PROJECTZERO_NO_START=1 to skip)
#
# Options (environment variables):
#   PROJECTZERO_DIR      install location (default: ~/.local/share/projectzero)
#   PROJECTZERO_SOURCE   what to install (default: the latest main branch archive from GitHub)
#   PROJECTZERO_NO_START set to 1 to install without starting the app
#
# Uninstall: rm -rf ~/.local/share/projectzero ~/.local/bin/projectzero
#            (your account and settings live in ~/.config/projectzero or
#             ~/Library/Application Support/ProjectZero)
set -eu

REPO_ARCHIVE="https://github.com/maun200/ProjectZero/archive/refs/heads/main.tar.gz"
DIR="${PROJECTZERO_DIR:-$HOME/.local/share/projectzero}"
SOURCE="${PROJECTZERO_SOURCE:-$REPO_ARCHIVE}"
BIN="$HOME/.local/bin"

say() { printf '%s\n' "ProjectZero: $*"; }
fail() { printf '%s\n' "ProjectZero: $*" >&2; exit 1; }

find_python() {
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1 &&
           "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

PY="$(find_python)" || {
    say "Python 3.11 or newer is required but was not found."
    case "$(uname -s)" in
        Darwin) say "Install it with Homebrew: brew install python@3.12  (or from https://www.python.org/downloads/)" ;;
        *) say "Install it with your package manager, for example:"
           say "  Debian/Ubuntu: sudo apt install python3 python3-venv"
           say "  Fedora:        sudo dnf install python3"
           say "  Arch:          sudo pacman -S python" ;;
    esac
    exit 1
}
say "using $("$PY" --version 2>&1) ($PY)"

if ! "$PY" -c 'import venv, ensurepip' >/dev/null 2>&1; then
    fail "Python's venv module is missing. On Debian/Ubuntu: sudo apt install python3-venv"
fi

mkdir -p "$DIR" "$BIN"
if [ ! -x "$DIR/venv/bin/python" ]; then
    say "creating virtual environment in $DIR/venv"
    "$PY" -m venv "$DIR/venv"
fi
say "installing ProjectZero (this can take a minute)"
"$DIR/venv/bin/python" -m pip install --quiet --upgrade pip
"$DIR/venv/bin/python" -m pip install --quiet --upgrade "$SOURCE"
ln -sf "$DIR/venv/bin/projectzero" "$BIN/projectzero"

if [ "$(uname -s)" = "Linux" ]; then
    mkdir -p "$HOME/.local/share/applications"
    cat > "$HOME/.local/share/applications/projectzero.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=ProjectZero
Comment=Share computing power for AI, chat and earn credits
Exec=$DIR/venv/bin/projectzero
Terminal=false
Categories=Network;Utility;
EOF
fi

VERSION="$("$DIR/venv/bin/projectzero" --version)"
say "installed version $VERSION"
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) say "note: $BIN is not on your PATH. Add this line to your shell profile:"
       say "  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
say "start it any time with: projectzero"
say "try it without any AI model:  projectzero demo"
say "real AI answers need Ollama (https://ollama.com) on this computer if you want to share compute"

if [ "${PROJECTZERO_NO_START:-0}" != "1" ]; then
    say "starting the app ..."
    exec "$DIR/venv/bin/projectzero"
fi
