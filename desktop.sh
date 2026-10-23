#!/bin/sh
# Portable terminal launcher. Use `sh desktop.sh --setup` for first-time setup.
set -eu
desktop_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
if [ -n "${MASK_REVIEW_PYTHON:-}" ]; then
    desktop_python=$MASK_REVIEW_PYTHON
else
    desktop_python=''
    for desktop_candidate in "$desktop_dir/.venv/bin/python" \
        "$(command -v python3 || true)" /opt/homebrew/bin/python3 \
        /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
        "$(command -v python || true)"; do
        if [ -x "$desktop_candidate" ] && "$desktop_candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' 2>/dev/null; then
            desktop_python=$desktop_candidate
            break
        fi
    done
    if [ -z "$desktop_python" ]; then
        printf '%s\n' 'Python 3.10 or newer is required. Install Python, then run sh desktop.sh --setup.' >&2
        exit 1
    fi
fi
exec "$desktop_python" "$desktop_dir/desktop_launch.py" "$@"
