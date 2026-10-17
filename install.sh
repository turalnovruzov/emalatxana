#!/bin/sh
# Installs emx from the repo this clone came from (or the URL given) into $EMX_HOME, default
# ~/.local/share/emx, and links $EMX_BIN_HOME/emx, default ~/.local/bin/emx. Edits no shell file.
set -eu

here=$(cd "$(dirname "$0")" && pwd)

for tool in git tar python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "emx needs $tool, and it is not on PATH." >&2
        exit 1
    fi
done
if ! command -v tmux >/dev/null 2>&1; then
    echo "emx runs in tmux, which is not on PATH; install it before you run emx." >&2
fi

url=${1:-$(git -C "$here" remote get-url origin)}
exec python3 -I "$here/bin/emx" install "$url"
