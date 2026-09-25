"""Opening and closing the sync port when a machine becomes, or stops being, a VPS.

Owner, 2026-09-25: the installer must NOT open TCP 8765 on every Windows
machine, because many people run the app on their main PC with no VPS at all.
The port opens only when the operator presses "Make this node a VPS" on
Settings > Remote node, and closes again on "Stop being a VPS".

Adding a firewall rule needs admin. The app first tries plainly (a VPS usually
runs as the built-in Administrator, where that just works), and only if that is
refused asks Windows to elevate, which shows the operator the normal UAC
prompt. Declining it is an answer, not a crash.

Nothing here runs netsh: a scripted runner answers by what it is asked.
"""
from __future__ import annotations

import base64
import subprocess

import pytest

from backend.src.services.cluster.sync import reachability as reach


class _Windows:
    def __init__(self, *, rule_exists=False, direct_rc=0, elevated_rc=0):
        self.rule_exists = rule_exists
        self.direct_rc = direct_rc
        self.elevated_rc = elevated_rc
        self.calls: list = []

    def __call__(self, args, **_kw):
        self.calls.append(args)
        if isinstance(args, list) and args[0] == "powershell":
            rc = self.elevated_rc
        elif " show rule " in args:
            rc = 0 if self.rule_exists else 1
        else:
            rc = self.direct_rc
        if rc == 0 and isinstance(args, str) and " add rule " in args:
            self.rule_exists = True
        if rc == 0 and isinstance(args, str) and " delete rule " in args:
            self.rule_exists = False
        return subprocess.CompletedProcess(args, rc, "", "")

    def of(self, kind: str) -> list:
        return [c for c in self.calls if isinstance(c, str) and f" {kind} rule " in c]

    @property
    def elevated(self) -> list[str]:
        """The PowerShell scripts run elevated, decoded."""
        out = []
        for c in self.calls:
            if isinstance(c, list) and c[0] == "powershell":
                out.append(base64.b64decode(c[-1]).decode("utf-16-le"))
        return out


@pytest.fixture
def windows(monkeypatch):
    def _make(**kw):
        w = _Windows(**kw)
        monkeypatch.setattr(reach, "_platform", "win32")
        monkeypatch.setattr(reach, "_run", w)
        reach.reset_cache()
        return w
    return _make


class TestOpening:
    def test_it_adds_the_rule_for_the_port(self, windows):
        w = windows()

        assert reach.open_port(8765) == "open"
        [add] = w.of("add")
        assert 'name="FOREX Trader Sync (port 8765)"' in add
        assert "dir=in" in add and "action=allow" in add
        assert "protocol=TCP" in add and "localport=8765" in add

    def test_it_is_not_limited_to_the_private_profile(self, windows):
        """A VPS's network is almost always Public."""
        w = windows()
        reach.open_port(8765)

        assert "profile=private" not in w.of("add")[0].lower()

    def test_an_existing_rule_is_not_added_twice(self, windows):
        """netsh stacks duplicate rules happily."""
        w = windows(rule_exists=True)

        assert reach.open_port(8765) == "open"
        assert w.of("add") == []

    def test_without_admin_it_asks_windows_to_elevate(self, windows):
        w = windows(direct_rc=1, elevated_rc=0)

        assert reach.open_port(8765) == "open"
        [script] = w.elevated
        assert "-Verb RunAs" in script
        assert 'name="FOREX Trader Sync (port 8765)"' in script

    def test_a_declined_prompt_is_reported_not_raised(self, windows):
        windows(direct_rc=1, elevated_rc=1)

        assert reach.open_port(8765) == "declined"

    def test_the_status_is_re_read_after_opening(self, windows):
        """The tab caches the check for a minute; it must not keep saying
        "not open" for a minute after the port was opened."""
        windows()
        assert reach.describe(8765)["firewall"] == "missing"

        reach.open_port(8765)

        assert reach.describe(8765)["firewall"] == "open"

    def test_off_windows_it_touches_nothing(self, monkeypatch):
        calls = []
        monkeypatch.setattr(reach, "_platform", "darwin")
        monkeypatch.setattr(reach, "_run", lambda *a, **k: calls.append(a))

        assert reach.open_port(8765) == "not-applicable"
        assert calls == []


class TestClosing:
    def test_it_deletes_the_rule_it_made(self, windows):
        w = windows(rule_exists=True)

        assert reach.close_port(8765) == "closed"
        [delete] = w.of("delete")
        assert 'name="FOREX Trader Sync (port 8765)"' in delete

    def test_nothing_to_close_is_already_closed(self, windows):
        w = windows(rule_exists=False)

        assert reach.close_port(8765) == "closed"
        assert w.of("delete") == []

    def test_without_admin_it_asks_windows_to_elevate(self, windows):
        w = windows(rule_exists=True, direct_rc=1, elevated_rc=0)

        assert reach.close_port(8765) == "closed"
        assert "delete rule" in w.elevated[0]

    def test_a_declined_prompt_is_reported(self, windows):
        windows(rule_exists=True, direct_rc=1, elevated_rc=1)

        assert reach.close_port(8765) == "declined"
