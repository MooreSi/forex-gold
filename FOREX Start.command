#!/bin/bash
# FOREX Trader — Start
# Double-click this file to launch the app.
# On first run it sets up everything automatically — no user input needed.
# On every run after that it skips setup and starts immediately.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
REQS_FILE="$SCRIPT_DIR/requirements.txt"
MARKER="$VENV_DIR/.setup_complete"
LIBOMP_NOTICE="$VENV_DIR/.libomp_notice_shown"
GIT_NOTICE="$VENV_DIR/.git_notice_shown"
USER_DATA="$HOME/Library/Application Support/ForexTrader"

# ── Clear Gatekeeper quarantine from the sibling launchers ───────────────────
# Files downloaded through a browser carry com.apple.quarantine, and macOS
# blocks each one separately the first time it is double-clicked. Getting here
# means the user already cleared that hurdle for this script, so lift it from
# the other launchers too rather than making them repeat the System Settings >
# Privacy & Security approval for Stop, the bridge, and the uninstaller.
# Only this folder's own scripts are touched, so it stays instant — recursing
# into .venv would mean thousands of files on every launch for no benefit.
for f in "$SCRIPT_DIR"/*.command "$SCRIPT_DIR"/*.sh; do
    [ -f "$f" ] && xattr -d com.apple.quarantine "$f" 2>/dev/null
done
xattr -d com.apple.quarantine "$SCRIPT_DIR" 2>/dev/null || true

# ── Helpers ───────────────────────────────────────────────────────────────────

reqs_hash() {
    md5 -q "$REQS_FILE" 2>/dev/null \
        || md5sum "$REQS_FILE" 2>/dev/null | cut -d' ' -f1 \
        || echo "unknown"
}

find_python() {
    for p in \
        "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3" \
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3" \
        "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3" \
        "/opt/homebrew/bin/python3" \
        "/usr/local/bin/python3"; do
        if [ -f "$p" ]; then
            echo "$p"; return 0
        fi
    done
    for cmd in python3.13 python3.12 python3.11 python3; do
        if command -v "$cmd" &>/dev/null; then
            ver=$("$cmd" -c "import sys; print(sys.version_info >= (3,11))" 2>/dev/null)
            [ "$ver" = "True" ] && { command -v "$cmd"; return 0; }
        fi
    done
    return 1
}

# ── libomp (OpenMP runtime — required by LightGBM on macOS) ──────────────────
# LightGBM's pip wheel does not bundle libomp on macOS; it must be installed
# at the OS level.  Without it LightGBM falls back to slower RandomForest.
# Installation is silent and requires no user input when Homebrew is present.

libomp_installed() {
    [ -f "/opt/homebrew/opt/libomp/lib/libomp.dylib" ] || \
    [ -f "/usr/local/opt/libomp/lib/libomp.dylib" ]
}

find_brew() {
    for b in "/opt/homebrew/bin/brew" "/usr/local/bin/brew"; do
        [ -x "$b" ] && echo "$b" && return 0
    done
    command -v brew >/dev/null 2>&1 && command -v brew && return 0
    return 1
}

ensure_libomp() {
    libomp_installed && return 0

    BREW=$(find_brew || true)
    if [ -n "$BREW" ]; then
        echo "  Installing libomp (OpenMP runtime for LightGBM)..."
        "$BREW" install libomp --quiet 2>/dev/null
        if libomp_installed; then
            echo "  libomp installed."
            rm -f "$LIBOMP_NOTICE"
            return 0
        fi
        echo "  libomp install failed — will retry next launch."
        return 1
    fi

    # Homebrew not available — show a one-time notice; app still runs with fallback
    if [ ! -f "$LIBOMP_NOTICE" ]; then
        echo ""
        echo "  ─────────────────────────────────────────────────────────────"
        echo "  Note: Homebrew not found — cannot auto-install libomp."
        echo "  LightGBM will use a slower fallback until this is resolved."
        echo ""
        echo "  To fix (one-time setup, ~2 min):"
        echo "    1. Install Homebrew: https://brew.sh"
        echo "    2. Open Terminal and run: brew install libomp"
        echo "  ─────────────────────────────────────────────────────────────"
        echo ""
        touch "$LIBOMP_NOTICE"
    fi
    return 1
}

# ── git (needed for the Settings > Update page's GitHub self-update panel, and
# for the git-checkout bootstrap that runs after every admin-console push —
# see remote/client.py's _apply_update()) ─────────────────────────────────────
# Most Macs already have a `git` shim from Xcode Command Line Tools, so this
# is a smaller gap than on Windows, but not guaranteed on a fresh machine.
# Mirrors ensure_libomp()'s shape: silent brew install when Homebrew is
# present, one-time manual notice otherwise — never blocks the app itself.

# `command -v git` is not enough on macOS: without the Command Line Tools,
# /usr/bin/git is a stub that exists, is executable, and fails every
# invocation with "xcrun: error: invalid active developer path". Run it.
have_git() {
    git --version >/dev/null 2>&1
}

ensure_git() {
    have_git && return 0

    BREW=$(find_brew || true)
    if [ -n "$BREW" ]; then
        echo "  Installing git..."
        "$BREW" install git --quiet 2>/dev/null
        if have_git; then
            echo "  git installed."
            rm -f "$GIT_NOTICE"
            return 0
        fi
        echo "  git install failed — trying the Command Line Tools instead."
    fi

    # No Homebrew (or brew could not do it): ask macOS for the Command Line
    # Tools, which is where git comes from on a stock Mac. This opens Apple's
    # own installer window and returns immediately — it does not block the
    # launch, and it is a no-op ("already installed") on a machine that has
    # them. Printing an instruction and leaving it at that meant a user who
    # never opens Terminal never got git, and the update feature simply did
    # not exist on their machine (reported 2026-09-06).
    if [ ! -f "$GIT_NOTICE" ]; then
        echo ""
        echo "  ─────────────────────────────────────────────────────────────"
        echo "  git is needed for automatic updates and is not installed."
        echo "  Opening Apple's Command Line Tools installer — click Install"
        echo "  in the window that appears, then restart FOREX Trader."
        echo ""
        echo "  Until then, Settings > Update will say git is not installed."
        echo "  Alternative: install Homebrew (https://brew.sh), then this"
        echo "  script installs git for you on the next launch."
        echo "  ─────────────────────────────────────────────────────────────"
        echo ""
        xcode-select --install >/dev/null 2>&1 || true
        touch "$GIT_NOTICE"
    fi
    return 1
}

# ── First-run / dependency check ──────────────────────────────────────────────
# Setup runs when the venv is missing or requirements.txt has changed.

CURRENT_HASH=$(reqs_hash)
STORED_HASH=$(cat "$MARKER" 2>/dev/null || echo "")

# ── Venv health check ─────────────────────────────────────────────────────────
if [ -d "$VENV_DIR" ] && ! "$VENV_DIR/bin/python" --version >/dev/null 2>&1; then
    echo "  Existing environment is broken or incompatible — rebuilding for this machine..."
    rm -rf "$VENV_DIR"
    rm -f "$MARKER"
    STORED_HASH=""
fi

if [ ! -f "$MARKER" ] || [ "$STORED_HASH" != "$CURRENT_HASH" ]; then

    clear
    echo ""
    echo "  ╔════════════════════════════════╗"
    echo "  ║       FOREX Trader Setup       ║"
    echo "  ╚════════════════════════════════╝"
    echo ""
    echo "  Setting up for the first time — please wait."
    echo "  This window will close and the app will open automatically."
    echo ""

    # ── Find Python ───────────────────────────────────────────────────────────
    PYTHON=$(find_python || true)
    if [ -z "$PYTHON" ]; then
        echo "  ──────────────────────────────────────────"
        echo "  Python 3.11 or newer is required but was not found."
        echo ""
        echo "  Please install Python, then double-click FOREX Start.command again."
        echo "  Download: https://www.python.org/downloads/"
        echo "  ──────────────────────────────────────────"
        echo ""
        open "https://www.python.org/downloads/" 2>/dev/null || true
        echo "  Press any key to close this window."
        read -rn1
        exit 1
    fi

    echo "  Python found: $PYTHON"
    echo ""

    # ── Create virtual environment ────────────────────────────────────────────
    if [ ! -d "$VENV_DIR" ]; then
        echo "  Creating virtual environment..."
        "$PYTHON" -m venv "$VENV_DIR" 2>&1 | sed 's/^/    /'
        echo "  Done."
        echo ""
    fi

    # ── Install / upgrade Python dependencies ─────────────────────────────────
    echo "  Installing / upgrading dependencies (this may take a minute)..."
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet --upgrade -r "$REQS_FILE"
    echo "  Done."
    echo ""

    # ── Install libomp (OpenMP runtime — required by LightGBM) ───────────────
    ensure_libomp

    # ── Install git (needed for the GitHub self-update panel) ────────────────
    ensure_git

    # ── Create user data directory ────────────────────────────────────────────
    mkdir -p "$USER_DATA/data/sessions"

    # ── Copy default config for new users ────────────────────────────────────
    if [ ! -f "$USER_DATA/config.yaml" ]; then
        cp "$SCRIPT_DIR/config.yaml.example" "$USER_DATA/config.yaml" 2>/dev/null || true
        echo "  Default config created — enter your credentials in Settings after the app opens."
        echo ""
    fi

    # ── Mark setup complete (store requirements hash) ─────────────────────────
    echo "$CURRENT_HASH" > "$MARKER"

    echo "  Setup complete. Starting FOREX Trader..."
    echo ""
    sleep 1

fi

# ── libomp check on every launch (instant file-existence test) ───────────────
# Catches the case where libomp was removed or was never installed.
# Fast no-op when already present; installs silently via brew when missing.
# Even if this is skipped (old script on existing installs), ml_engine.py
# contains the same self-healing logic as a defence-in-depth fallback.
ensure_libomp || true

# ── git check on every launch (instant command-check when already present) ───
ensure_git || true

# ── Migrate stale Claude model IDs in config.yaml ────────────────────────────
# Runs on every launch; instant no-op when the value is already current.
# Pattern avoids strict anchors so it matches regardless of trailing whitespace,
# quoting style, or Windows CRLF line endings in the YAML file.
CONFIG_YAML="$USER_DATA/config.yaml"
if [ -f "$CONFIG_YAML" ]; then
    sed -i '' \
        -e 's/claude_model:.*claude-haiku-4-5[^0-9].*/claude_model: claude-haiku-4-5-20251001/' \
        -e 's/claude_model:.*claude-sonnet-4-5[^-].*/claude_model: claude-sonnet-4-6/' \
        -e 's/claude_model:.*claude-opus-4-5[^-].*/claude_model: claude-opus-4-8/' \
        "$CONFIG_YAML" 2>/dev/null || true
fi

# ── Free port 8888 if something else is holding it ───────────────────────────
PIDS=$(lsof -ti:8888 2>/dev/null || true)
if [ -n "$PIDS" ]; then
    kill $PIDS 2>/dev/null || true
    sleep 1
fi

# ── Launch ────────────────────────────────────────────────────────────────────
cd "$SCRIPT_DIR"
exec "$VENV_DIR/bin/python" run.py
