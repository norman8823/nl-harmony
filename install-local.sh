#!/usr/bin/env bash
# Install a standalone local CLI and the additive Codex prompt hook once.
set -euo pipefail

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_dir="${HOME}/.local/share/harmony"
bin_dir="${HOME}/.local/bin"
codex_dir=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix|--bin-dir|--codex-home)
      if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        printf 'A directory is required after %s\n' "$1" >&2
        exit 2
      fi
      case "$1" in
        --prefix) install_dir=$2 ;;
        --bin-dir) bin_dir=$2 ;;
        --codex-home) codex_dir=$2 ;;
      esac
      shift 2 ;;
    *) printf 'Usage: bash install-local.sh [--prefix DIR] [--bin-dir DIR] [--codex-home DIR]\n' >&2; exit 2 ;;
  esac
done

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11 or newer is required for the Codex connection.")'
if ! command -v codex >/dev/null 2>&1; then
  printf 'Install Codex CLI first, then rerun this installer.\n' >&2
  exit 1
fi
install_dir=$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).expanduser().resolve())' "$install_dir")
bin_dir=$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).expanduser().resolve())' "$bin_dir")
if [ -e "$install_dir" ] && [ ! -f "$install_dir/.harmony-local-install" ]; then
  printf 'Existing directory is not a Harmony installation: %s\nChoose a different --prefix; no files were replaced.\n' "$install_dir" >&2
  exit 1
fi
mkdir -p "$install_dir"
printf 'harmony-local-v1\n' > "$install_dir/.harmony-local-install"
python3 -m venv "$install_dir/venv"
"$install_dir/venv/bin/python" -m pip install --upgrade "$source_dir"
if [ -n "$codex_dir" ]; then
  "$install_dir/venv/bin/nlh" codex install --codex-home "$codex_dir"
else
  "$install_dir/venv/bin/nlh" codex install
fi

mkdir -p "$bin_dir"
if [ ! -e "$bin_dir/nlh" ] && [ ! -L "$bin_dir/nlh" ]; then
  ln -s "$install_dir/venv/bin/nlh" "$bin_dir/nlh"
elif [ ! -L "$bin_dir/nlh" ] || [ "$(readlink "$bin_dir/nlh")" != "$install_dir/venv/bin/nlh" ]; then
  printf 'Your existing nlh command was preserved. Use the installed executable below.\n'
fi
printf '\nLocal Harmony is installed. Restart already-open Codex sessions once.\n'
printf 'Start Companion with your existing board directory:\n  %q --project /path/to/board-project companion\n' "$install_dir/venv/bin/nlh"
printf 'Then use ordinary Codex CLI in any repository on this computer.\n'
printf 'The task-intake hook uses the installed executable directly; no shell activation is needed.\n'
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) printf 'To use the short nlh command, add this directory to your shell PATH: %s\n' "$bin_dir" ;;
esac
