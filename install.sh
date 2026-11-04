#!/usr/bin/env bash
# Sumie one-step installer for macOS (Intel and Apple Silicon) and Linux (x86_64, experimental).
#   ./install.sh                 # core install
#   ./install.sh --ai            # also install PyTorch + AI models (~400 MB)
#   ./install.sh --game "/path/to/steamapps/common/Draw & Guess"
#   ./install.sh --check         # only report what would happen
#   ./install.sh --uninstall     # remove the plugin (keeps BepInEx)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

AI=0; CHECK=0; UNINSTALL=0; GAME=""
while [ $# -gt 0 ]; do
  case "$1" in
    --ai) AI=1 ;;
    --check) CHECK=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --game) shift; GAME="${1:-}" ;;
    -h|--help) sed -n 2,8p "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\033[1;35m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

OS="$(uname -s)"; ARCH="$(uname -m)"
case "$OS" in
  Darwin) ;;
  Linux)  [ "$ARCH" = "x86_64" ] || die "Linux is only supported on x86_64 (this machine is $ARCH)." ;;
  *)      die "install.sh is for macOS and Linux. On Windows use install.ps1." ;;
esac
if [ "$OS" = "Darwin" ] && [ "$ARCH" = "arm64" ]; then
  # The game is an x86_64 Unity build: Apple Silicon runs it through Rosetta 2.
  if ! /usr/bin/pgrep -q oahd 2>/dev/null; then
    say "Apple Silicon detected: installing Rosetta 2 (needed by the game itself)"
    softwareupdate --install-rosetta --agree-to-license || die "Rosetta 2 install failed; run: softwareupdate --install-rosetta"
  fi
fi
if [ "$OS" = "Darwin" ]; then
  HINT_PY="brew install python"; HINT_NET="brew install --cask dotnet-sdk"
else
  HINT_PY="sudo apt install python3 python3-venv   (Fedora: sudo dnf install python3; Arch: sudo pacman -S python)"
  HINT_NET="sudo apt install dotnet-sdk-8.0   (Fedora: sudo dnf install dotnet-sdk-8.0; Arch: sudo pacman -S dotnet-sdk)"
fi
PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "python3 not found. Install it with: $HINT_PY"
"$PY" - <<'P' || die "Python 3.10 or newer is required."
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
P
# the .NET SDK is checked by tools/install.py (it also looks in ~/.dotnet and $DOTNET_ROOT); hint: $HINT_NET

GAME_ARGS=()
[ -n "$GAME" ] && GAME_ARGS=(--game "$GAME")

if [ "$UNINSTALL" = 1 ]; then
  say "Removing the plugin"
  exec "$PY" tools/install.py --uninstall ${GAME_ARGS[@]+"${GAME_ARGS[@]}"}
fi
if [ "$CHECK" = 1 ]; then
  exec "$PY" tools/install.py --check ${GAME_ARGS[@]+"${GAME_ARGS[@]}"}
fi

say "1/2  Setting up the Python environment (.venv inside this folder)"
if [ "$AI" = 1 ]; then "$PY" tools/setup.py --ai; else "$PY" tools/setup.py; fi
say "2/2  Building and installing the plugin (close the game first)"
"$PY" tools/install.py ${GAME_ARGS[@]+"${GAME_ARGS[@]}"}
say "Done. Set the Steam launch option printed above once, start the game, then open http://127.0.0.1:8765/"
