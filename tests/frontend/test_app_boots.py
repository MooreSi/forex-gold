"""The app actually boots and serves HTTP.

Everything else in this suite tests pieces. This starts run.py as a real
subprocess, waits for the server, and asks it for a page — the check that
would have caught any of the refactor's composition-root mistakes, and the
one thing no unit test can stand in for.

It is skipped when the port is busy rather than failing, because a
developer running the app locally should not see a red suite. What it must
never do is pass silently when the app did not start, so the wait loop
distinguishes "not up yet" from "process died" and reports the log tail
either way.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
# The app reads its port from config, and the licence activation screen
# hardcodes 8888, so this smoke test uses the app's own port and skips
# when it is busy rather than pretending to control it.
# A port of its own. 8888 is the app's default, so hardcoding it meant this
# smoke test either skipped (whenever the developer's app was up) or spawned a
# SECOND app on the running one's port -- and run.py frees a busy port by
# killing whoever holds it, so the test could take down the app it was sharing a
# machine with. Confirmed 2026-08-26.
PORT = 8899
BOOT_TIMEOUT = 120   # cold start builds the schema and starts several tasks


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _serving(port: int) -> bool:
    return not _port_free(port)


@pytest.fixture(scope="module")
def running_app(tmp_path_factory):
    if not _port_free(PORT):
        pytest.skip(f"port {PORT} already in use")

    data_dir = tmp_path_factory.mktemp("appdata")
    env = {
        **os.environ,
        # Keep the smoke test off the developer's real database and config.
        # FOREX_TRADER_DATA_DIR is the name config.py actually reads -- the
        # FOREX_DATA_DIR/FOREX_UI_PORT this used were read by nothing at all,
        # in this tree or before it, so the "isolated" smoke test had always
        # booted against the real ~/Library/Application Support/ForexTrader.
        # The port comes from the config file written below, since run.py takes
        # it from config, not the environment.
        "FOREX_TRADER_DATA_DIR": str(data_dir),
    }
    # NiceGUI switches into screen-test mode when it sees pytest in the
    # environment, and then demands NICEGUI_SCREEN_TEST_PORT. The child
    # inherits our pytest variables, so ui.run() died with a KeyError
    # instead of starting a server -- the app was fine, the harness was
    # lying about the environment.
    for leaked in list(env):
        if leaked.startswith("PYTEST") or leaked.startswith("NICEGUI"):
            env.pop(leaked)
    # run.py reads its port from config.yaml in the data dir.
    (data_dir / "data").mkdir(parents=True, exist_ok=True)
    (data_dir / "config.yaml").write_text(f"port: {PORT}\n", encoding="utf-8")

    log_path = data_dir / "boot.log"
    with open(log_path, "w") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "run.py", "--no-browser"],
            cwd=REPO, env=env, stdout=log_file, stderr=subprocess.STDOUT,
        )

    try:
        deadline = time.time() + BOOT_TIMEOUT
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail(
                    f"run.py exited with code {proc.returncode} before serving.\n"
                    f"--- log tail ---\n{_tail(log_path)}"
                )
            # Only an accepting socket counts as ready. NiceGUI prints
            # "NiceGUI ready to go" BEFORE the listener starts accepting, so
            # breaking on the log line let the test fetch a port that was not
            # up yet -- ConnectionRefused, intermittently, and only under the
            # load of a full suite run (it passed every time in isolation,
            # which is exactly what made it look like a real failure).
            if _serving(PORT):
                break
            time.sleep(1)
        else:
            proc.kill()
            pytest.fail(
                f"run.py did not serve within {BOOT_TIMEOUT}s.\n"
                f"--- log tail ---\n{_tail(log_path)}"
            )
        yield proc, log_path
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _tail(path: Path, lines: int = 40) -> str:
    return "\n".join(_read(path).splitlines()[-lines:])


def test_the_app_starts_and_reports_ready(running_app):
    _proc, log_path = running_app
    assert "NiceGUI ready" in _read(log_path), (
        f"server never reported ready:\n{_tail(log_path)}"
    )


def test_the_app_serves_its_page(running_app):
    """One route by design -- frontend/app.py owns the single @ui.page('/')
    and every tab renders inside it.

    On an unlicensed checkout this is the activation screen rather than the
    dashboard, which is correct behaviour and still proves the server, the
    composition root and the page stack are all working. The dashboard
    itself is covered by test_pages_render.py.
    """
    import re
    import urllib.request

    _proc, log_path = running_app
    # The activation screen hardcodes 8888, but a LICENSED app serves on its
    # configured port (and debug mode is designed to let a dev run it that way),
    # so read the port the server actually reported rather than assuming 8888.
    log = _read(log_path)
    m = (re.search(r"NiceGUI ready to go on http://[\d.]+:(\d+)", log)
         or re.search(r"laddr=\('[\d.]+', (\d+)\)", log))
    port = int(m.group(1)) if m else PORT
    # The proxy in this environment must not intercept a localhost call.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    # A licensed app gates every route behind /login (307) — following the
    # redirect still proves the server, composition root and page stack work.
    with opener.open(f"http://127.0.0.1:{port}/", timeout=30) as response:
        assert response.status == 200
        body = response.read().decode("utf-8", errors="replace")
    assert body.strip(), "served an empty body"


def test_the_boot_log_has_no_traceback(running_app):
    """A page that fails to import logs a traceback and leaves the app
    running but broken -- exactly the failure a 200 on / would hide."""
    _proc, log_path = running_app
    log = _read(log_path)
    assert "Traceback (most recent call last)" not in log, (
        f"the app logged a traceback during startup:\n{_tail(log_path, 60)}"
    )


def test_this_file_says_which_path_it_actually_exercised(running_app):
    """States the limit of this test out loud, rather than letting three
    green ticks imply more coverage than they carry.

    On a checkout with no licence, enforce() serves the ACTIVATION screen
    and the dashboard never renders. That is correct behaviour, and this
    file still proves the real things it set out to prove -- the process
    starts, binds a port, answers HTTP and logs no traceback -- but it does
    NOT prove the trading dashboard renders. That is what
    test_pages_render.py covers, and there is no dev bypass for the licence
    check on purpose.
    """
    _proc, log_path = running_app
    log = _read(log_path)
    activation = "showing activation screen" in log
    if activation:
        pytest.skip(
            "unlicensed checkout: exercised the activation screen, not the "
            "dashboard -- dashboard page code is covered by "
            "tests/frontend/test_pages_render.py"
        )
    assert "NiceGUI ready" in log
