#!/bin/zsh
# Double-click to start the Brennan UI. Close this window (or press Ctrl+C) to stop it.
cd "$(dirname "$0")"
exec python3 brennan.py "$@"
