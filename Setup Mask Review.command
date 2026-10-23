#!/bin/sh
# First-time app-local dependency setup, followed by the editor.
desktop_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
sh "$desktop_dir/Mask Review.command" --setup "$@"
