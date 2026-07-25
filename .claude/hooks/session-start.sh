#!/bin/bash
# Installs what the test suite needs. Without this a fresh container reports
# ~84 collection errors that look like broken code and are actually a bare
# environment.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"

# The image ships a Debian-packaged cryptography whose _cffi_backend is missing,
# which surfaces as `pyo3_runtime.PanicException` from unrelated tests. Force a
# pip-managed cffi over the top; --ignore-installed because the Debian package
# has no RECORD file and cannot be uninstalled.
pip install -q --ignore-installed cffi

# Required for the suite to import at all. pytest-asyncio matters more than it
# looks: without it every @pytest.mark.asyncio test fails rather than skipping,
# which reads as 26 broken tests instead of one missing package.
pip install -q pytest pytest-asyncio httpx nicegui pydantic PyYAML websockets \
                python-dateutil

# Used by the ML/analytics paths. Missing these does not break collection, but
# tests that exercise them skip silently, which quietly shrinks the suite.
pip install -q numpy scikit-learn lightgbm joblib yfinance matplotlib psutil \
                anthropic keyring

# Optional, best-effort:
#   telethon  -> its pyaes dependency has no wheel and its sdist fails to build
#                on 3.11. Only the live Telegram client needs it; the suite does
#                not, and it must not fail the hook.
#   MetaTrader5 -> Windows-only by design (sys_platform marker in
#                requirements.txt). Every import of it in this repo is
#                function-local, so tests never reach one.
pip install -q telethon 2>/dev/null || true

# The audit tooling reads git history (see tools/refactor_audit/). A shallow
# clone makes those checks silently find nothing -- the exact false-green this
# repo's Phase 0 audit exists to catch.
if [ -f .git/shallow ]; then
  git fetch --unshallow --quiet 2>/dev/null || true
fi

echo "session-start: test environment ready"
