#!/bin/bash
# FOREX Trader — Complete Uninstall (macOS)
#
# The macOS counterpart of "Windows Uninstall.bat". Removes the app folder
# (code + .venv), all local trading data (databases, logs, sessions,
# config.yaml), the licence activation, and any Desktop shortcut. Requires
# two separate confirmations before deleting anything.
#
# This is for the manually-deployed setup (FOREX Start.command / git
# checkout) — the only way FOREX Trader is installed on macOS. There is no
# .pkg/.app installer receipt to clean up.
#
# A shell script cannot safely delete the folder it is running from (bash
# reads the file lazily as it executes, and the working directory would
# vanish underneath it), so this copies itself to the temp folder and
# re-execs from there, passing the real app folder as an argument, before
# doing any deleting.

set -u

# ── Relaunch from temp ────────────────────────────────────────────────────────

if [ "${2:-}" != "RELAUNCHED" ]; then
    APP_DIR="$(cd "$(dirname "$0")" && pwd)"
    SELF_COPY="${TMPDIR:-/tmp}/FOREX_Uninstall_$$.command"
    if ! cp "$0" "$SELF_COPY" 2>/dev/null; then
        echo ""
        echo "  ERROR: Could not prepare the uninstaller (could not write to ${TMPDIR:-/tmp})."
        echo ""
        read -r -p "Press Return to close..." _
        exit 1
    fi
    chmod +x "$SELF_COPY"
    cd / || exit 1
    exec "$SELF_COPY" "$APP_DIR" RELAUNCHED
fi

APP_DIR="$1"
USER_DATA="$HOME/Library/Application Support/ForexTrader"
USER_DATA_DEV="$HOME/Library/Application Support/ForexTrader-Refactor2"
LICENCE_FILE="$HOME/.forex_trader_licence"

# The venv normally lives inside the app folder, but it can be a symlink to a
# venv held elsewhere (this is how the development checkout is set up). Deleting
# the app folder would then remove only the symlink and silently strand several
# GB of packages, so resolve it now and list the real target below.
VENV_TARGET=""
if [ -L "$APP_DIR/.venv" ]; then
    VENV_TARGET="$(cd "$APP_DIR/.venv" 2>/dev/null && pwd -P || true)"
    case "$VENV_TARGET" in
        "$APP_DIR"|"$APP_DIR"/*) VENV_TARGET="" ;;   # inside the folder anyway
        "$HOME"|/|"") VENV_TARGET="" ;;              # never delete these
    esac
fi

# ── Summary ───────────────────────────────────────────────────────────────────

clear
echo ""
echo "  =========================================================="
echo "    FOREX Trader — COMPLETE UNINSTALL"
echo "  =========================================================="
echo ""
echo "  This will PERMANENTLY DELETE:"
echo ""
echo "    - The entire app folder, including the Python environment"
echo "      and any local git history:"
echo "        $APP_DIR"
if [ -n "$VENV_TARGET" ]; then
echo ""
echo "    - The Python environment this app's .venv points at:"
echo "        $VENV_TARGET"
fi
echo ""
echo "    - All local trading data and settings:"
echo "        $USER_DATA"
echo "        $USER_DATA_DEV"
echo "      (trade databases, history, logs, session data, config.yaml)"
echo ""
echo "    - Your licence activation:"
echo "        $LICENCE_FILE"
echo ""
echo "    - The Desktop shortcut, if present"
echo ""
echo "  THIS CANNOT BE UNDONE. There is no backup and no recovery —"
echo "  trade history, logs, settings and the licence activation will"
echo "  be gone for good."
echo ""
echo "  If FOREX Trader is running right now, it will be force-stopped"
echo "  as part of this uninstall. Any trade currently open at your"
echo "  broker stays open, but this app will no longer be monitoring"
echo "  or managing it (stop-loss trailing, TP handling, etc.) once"
echo "  it's stopped — close or hand off any open trades first if that"
echo "  matters to you."
echo ""
echo "  Note: Homebrew, libomp, git, CrossOver/Wine and the MetaTrader 5"
echo "  bottle are left in place, since other tools on this Mac may"
echo "  depend on them. Remove the bottle manually from"
echo "  ~/Library/Application Support/CrossOver/Bottles/MetaTrader 5"
echo "  if you don't need it."
echo ""
echo "  =========================================================="
echo ""

RUNNING_PIDS=""
for PORT in 8888 8890; do
    PIDS="$(lsof -ti:"$PORT" 2>/dev/null || true)"
    [ -n "$PIDS" ] && RUNNING_PIDS="$RUNNING_PIDS $PIDS"
done
RUNNING_PIDS="$(echo "$RUNNING_PIDS" | xargs 2>/dev/null || true)"
if [ -n "$RUNNING_PIDS" ]; then
    echo "  WARNING: FOREX Trader appears to be running right now [PID $RUNNING_PIDS]."
    echo ""
fi

# ── Two confirmations ─────────────────────────────────────────────────────────

read -r -p "Type DELETE (all caps) to continue, or press Return to cancel: " CONFIRM1
if [ "$CONFIRM1" != "DELETE" ]; then
    echo ""
    echo "  Cancelled — nothing was deleted."
    echo ""
    read -r -p "Press Return to close..." _
    exit 0
fi

echo ""
echo "  Last chance. This is permanent and cannot be undone."
read -r -p "Type YES to uninstall now: " CONFIRM2
if [ "$CONFIRM2" != "YES" ]; then
    echo ""
    echo "  Cancelled — nothing was deleted."
    echo ""
    read -r -p "Press Return to close..." _
    exit 0
fi

echo ""
echo "  Uninstalling..."
echo ""

# ── Stop anything still running ───────────────────────────────────────────────

for PORT in 8888 8890; do
    PIDS="$(lsof -ti:"$PORT" 2>/dev/null || true)"
    for PID in $PIDS; do
        kill "$PID" 2>/dev/null || true
        echo "  Stopped running instance [PID $PID]"
    done
done
sleep 2
for PORT in 8888 8890; do
    PIDS="$(lsof -ti:"$PORT" 2>/dev/null || true)"
    for PID in $PIDS; do
        kill -9 "$PID" 2>/dev/null || true
    done
done

# ── Licence activation ────────────────────────────────────────────────────────

if [ -f "$LICENCE_FILE" ]; then
    rm -f "$LICENCE_FILE" 2>/dev/null || true
    echo "  Removed licence activation"
fi

# ── Desktop shortcut ──────────────────────────────────────────────────────────
# Only symlinks pointing into the app folder are removed — a real file or
# folder on the Desktop that happens to share the name is left untouched.

for NAME in "FOREX Trader" "FOREX Trader.app" "FOREX Start.command" "FOREX Trader.command"; do
    LINK="$HOME/Desktop/$NAME"
    if [ -L "$LINK" ]; then
        TARGET="$(readlink "$LINK")"
        case "$TARGET" in
            "$APP_DIR"|"$APP_DIR"/*)
                rm -f "$LINK" 2>/dev/null || true
                echo "  Removed Desktop shortcut: $NAME"
                ;;
        esac
    fi
done

# ── User data ─────────────────────────────────────────────────────────────────

if [ -d "$USER_DATA" ]; then
    rm -rf "$USER_DATA" 2>/dev/null || true
    echo "  Removed $USER_DATA (databases, logs, settings)"
fi
if [ -d "$USER_DATA_DEV" ]; then
    rm -rf "$USER_DATA_DEV" 2>/dev/null || true
    echo "  Removed $USER_DATA_DEV"
fi

# ── Python environment held outside the app folder ────────────────────────────

if [ -n "$VENV_TARGET" ] && [ -d "$VENV_TARGET" ]; then
    rm -rf "$VENV_TARGET" 2>/dev/null || true
    echo "  Removed Python environment: $VENV_TARGET"
fi

# ── App folder ────────────────────────────────────────────────────────────────

echo "  Removing app folder: $APP_DIR"
rm -rf "$APP_DIR" 2>/dev/null || true
if [ -d "$APP_DIR" ]; then
    echo ""
    echo "  WARNING: Could not fully remove $APP_DIR"
    echo "  Some files may still be in use, or need different permissions —"
    echo "  close any open windows or terminals in that folder and delete it"
    echo "  manually (drag it to the Trash)."
else
    echo "  App folder removed."
fi

echo ""
echo "  =========================================================="
echo "    Uninstall complete."
echo "  =========================================================="
echo ""
read -r -p "Press Return to close..." _

rm -f "$0" 2>/dev/null || true
