#!/bin/sh
# Copy-on-Start with session persistence.
#
# /workspace-ro  → bind-mounted real workspace (read-only snapshot)
# /workspace     → Docker named volume (persists across bash calls in
#                   the same session, changes survive between calls)
#
# Only copy on the FIRST call — when the volume is empty.  Subsequent
# calls reuse the accumulated state.

if [ ! "$(ls -A /workspace 2>/dev/null)" ]; then
    # Volume is empty — first call (or manually cleared)
    if [ -d /workspace-ro ] && [ -n "$(ls -A /workspace-ro 2>/dev/null)" ]; then
        echo "[sandbox] First call: copying files from /workspace-ro to /workspace..."
        cp -r /workspace-ro/. /workspace/ 2>&1 || echo "[sandbox] Copy had errors (exit $?)"
        echo "[sandbox] $(ls -A /workspace 2>/dev/null | wc -l) files in /workspace"
    else
        echo "[sandbox] /workspace-ro not mounted or empty, starting with empty /workspace"
    fi
else
    echo "[sandbox] Reusing existing /workspace ($(ls -A /workspace 2>/dev/null | wc -l) files)"
fi

exec "$@"
