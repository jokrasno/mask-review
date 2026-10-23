#!/bin/sh
# Finder double-click entry point. The terminal remains open while editing.
desktop_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
sh "$desktop_dir/desktop.sh" "$@"
desktop_status=$?
if [ "$desktop_status" -ne 0 ]; then
    printf '\n%s\n' 'Mask Review could not start. Read the message above and DESKTOP_SETUP.md.'
    printf '%s' 'Press Return to close: '
    read -r desktop_reply || true
fi
exit "$desktop_status"
