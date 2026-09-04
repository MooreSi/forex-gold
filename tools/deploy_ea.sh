#!/usr/bin/env bash
#
# Deploy mql5/ForexTraderBridge.mq5 into every MetaTrader 5 Experts folder
# on this machine, so MetaEditor compiles the source that is actually in
# the repo.
#
# WHY THIS EXISTS (2026-08-04)
# ---------------------------
# The repo copy and MetaTrader's own copy are two completely separate
# files. Nothing links them. Editing mql5/ForexTraderBridge.mq5 does not
# change what MetaEditor compiles, and MetaEditor gives no indication that
# the file it just built is months older than the one you edited.
#
# That silently cost a full day: a batch of EA fixes was written, reviewed,
# committed, and "recompiled" several times, while the terminal kept
# compiling a source from three weeks earlier. The symptom was a template
# ignoring close_full_on_last and flattening a position at its last TP,
# because the running build still hardcoded `closeFullOnLast = true` and
# knew nothing about the field. Every fix was correct; none were ever
# loaded.
#
# So: never hand-copy this file again. Run this, then compile.
#
# THIS IS THE HAND-RUN, MACOS-ONLY PATH. The automated one is
# backend/src/services/broker/ea_deploy.py, which does the same copy on
# Windows and macOS and runs itself after every app self-update, so a remote
# machine needs nobody in front of it. Keep the two in step, or fix one and
# delete the other -- do not let them diverge silently, which is the whole
# failure this file was written about.
#
# USAGE
#   tools/deploy_ea.sh            deploy repo -> every MT5 Experts folder
#   tools/deploy_ea.sh --check    report drift only, change nothing
#
# AFTER DEPLOYING you still have to compile in MetaEditor (F7) and let the
# chart reload the EA. MetaEditor's /compile CLI does not work headlessly
# under CrossOver (it exits 0, writes no log, and rebuilds nothing), so
# this script deliberately does not pretend to compile for you. It tells
# you what to do and, on the next run, whether you actually did it.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_ROOT/mql5/ForexTraderBridge.mq5"
EA_NAME="ForexTraderBridge"

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

if [ ! -f "$SRC" ]; then
    echo "FATAL: repo source not found at $SRC" >&2
    exit 1
fi

# Every Experts folder that already holds a copy of this EA. Discovered
# rather than hardcoded: there are typically several MT5 installs per
# machine (a live one, a demo/validation one, and the roaming Terminal
# profile), and which one is live changes. A folder is only a deploy
# target if the EA is already there, so this can never scatter the file
# into an unrelated terminal.
TARGETS=()
while IFS= read -r found; do
    [ -n "$found" ] && TARGETS+=("$(dirname "$found")")
done < <(
    find "$HOME/Library/Application Support/CrossOver/Bottles" \
         "$HOME/AppData/Roaming/MetaQuotes/Terminal" \
         "/Applications/MetaTrader 5.app" \
         -name "${EA_NAME}.mq5" \
         -path "*/MQL5/Experts/*" 2>/dev/null | sort -u
)

if [ ${#TARGETS[@]} -eq 0 ]; then
    echo "No MetaTrader Experts folder containing ${EA_NAME}.mq5 was found."
    echo "If this is a fresh MT5 install, copy the file in by hand once;"
    echo "this script will pick it up from then on."
    exit 1
fi

src_sum=$(shasum -a 256 "$SRC" | awk '{print $1}')
src_mt=$(stat -f %m "$SRC")

echo "repo source : $SRC"
echo "            : $(wc -c < "$SRC" | tr -d ' ') bytes, sha ${src_sum:0:12}"
echo

deployed=0
needs_compile=0

for dir in "${TARGETS[@]}"; do
    dst="$dir/${EA_NAME}.mq5"
    ex5="$dir/${EA_NAME}.ex5"
    label="${dir/#$HOME/~}"

    dst_sum=""
    [ -f "$dst" ] && dst_sum=$(shasum -a 256 "$dst" | awk '{print $1}')

    if [ "$dst_sum" = "$src_sum" ]; then
        status="already current"
    else
        status="STALE"
    fi
    echo "target      : $label"
    echo "  source    : $status"

    if [ "$CHECK_ONLY" -eq 0 ] && [ "$dst_sum" != "$src_sum" ]; then
        # Keep the copy being replaced. It is not under version control,
        # so this backup is its only remaining trace.
        if [ -f "$dst" ]; then
            bak="$dst.bak-$(date +%Y%m%d-%H%M%S)"
            cp -p "$dst" "$bak" || { echo "  ERROR: backup failed, not overwriting" >&2; continue; }
            echo "  backup    : $(basename "$bak")"
        fi
        cp "$SRC" "$dst" || { echo "  ERROR: copy failed" >&2; continue; }
        # Verify rather than trust the exit code.
        new_sum=$(shasum -a 256 "$dst" | awk '{print $1}')
        if [ "$new_sum" != "$src_sum" ]; then
            echo "  ERROR     : copy verify FAILED, target does not match repo" >&2
            continue
        fi
        echo "  deployed  : OK (verified byte-identical)"
        deployed=$((deployed + 1))
    fi

    # Compiled-binary staleness. An .ex5 older than the .mq5 beside it is
    # the exact condition that hid the original problem, so surface it
    # loudly every run, deploy or check.
    if [ -f "$ex5" ]; then
        mq5_now=$(stat -f %m "$dst" 2>/dev/null || echo 0)
        ex5_mt=$(stat -f %m "$ex5")
        if [ "$ex5_mt" -ge "$mq5_now" ]; then
            echo "  binary    : .ex5 newer than source, compiled ($(date -r "$ex5_mt" '+%d %b %H:%M'))"
        else
            echo "  binary    : *** .ex5 is OLDER than source, NOT COMPILED YET ***"
            needs_compile=$((needs_compile + 1))
        fi
    else
        echo "  binary    : no .ex5 present, never compiled here"
        needs_compile=$((needs_compile + 1))
    fi
    echo
done

if [ "$CHECK_ONLY" -eq 1 ]; then
    echo "check only, nothing written."
    exit 0
fi

echo "deployed to $deployed folder(s)."
if [ "$needs_compile" -gt 0 ]; then
    cat <<'EOF'

NOT DONE YET. Open MetaEditor, press F7 to compile, and let the chart
reload the EA. Then re-run:

    tools/deploy_ea.sh --check

and confirm every target reads "compiled" rather than "NOT COMPILED YET".
EOF
fi
