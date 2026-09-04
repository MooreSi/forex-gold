"""The page the browser sits on while the app restarts into its new licence.

`/licence-activated` is plain HTML with no socket.io, deliberately, so it
survives the NiceGUI process dying underneath it. Its whole job is to notice
when the replacement process is up and take the user there. It had two ways of
failing to do that, and both end the same way: the user stares at "Licence
Activated / Loading..." until they relaunch the app by hand.

1. It polled `/` and navigated on the first HTTP 200. The process that serves
   this page also serves `/`, and answers 200 right up until it exits — the
   guard navigates here, sleeps 0.6 s and then restarts, while the first poll
   fires 800 ms after the page loads. A margin of ~200 ms was the only thing
   standing between the user and a navigation triggered by the dying process.
   The probe path fixes this by construction rather than by timing: the
   activation process answers it 200, and the app that replaces it does not
   serve that path at all, so a NON-ok response is the signal.

2. It called `location.reload()`. This page's URL is `/licence-activated`,
   which only the activation screen ever registers — so reloading it once the
   real app is up re-requests a path that app does not serve, and lands on a
   404. Even when the timing worked, this is where it went. The fix is
   `location.replace('/')`.

These tests read the served document. They do not execute its JavaScript —
nothing in this suite runs a browser — so they pin the contract the page
depends on (which URL it asks about, and where it sends the user), not the
behaviour of the interpreter that runs it.
"""
from __future__ import annotations

from backend.src.config.licence import guard


def _script_code(html: str) -> str:
    """The page's JavaScript with its `//` comments stripped.

    The comments in that block necessarily name the calls they exist to warn
    against, so asserting over the raw document would be asserting that the
    page agrees with its own prose -- which it always does. Assert over the
    code that actually runs.
    """
    block = html.split("<script>", 1)[1].split("</script>", 1)[0]
    return "\n".join(line.split("//", 1)[0] for line in block.splitlines())


class TestTheProbeTellsTheTwoProcessesApart:

    def test_the_activation_process_answers_the_probe(self):
        assert guard._activation_probe() == {"stage": "activation"}

    def test_the_probe_lives_under_the_activation_only_path(self):
        """The signal is "this path 404s now", so it must be a path the main
        app has no reason to serve. Anything under /licence-activated/ is
        registered by the activation screen alone."""
        assert guard._ACTIVATION_PROBE_PATH.startswith("/licence-activated/")


class TestTheWaitPageWaitsForTheRightThing:

    def test_it_polls_the_probe_and_not_the_root(self):
        code = _script_code(guard._activation_html())

        assert guard._ACTIVATION_PROBE_PATH in code
        assert "fetch('/'" not in code and 'fetch("/"' not in code, (
            "the dying activation process answers / with 200, so polling it "
            "navigates away before the app it is waiting for exists"
        )

    def test_it_leaves_this_page_rather_than_reloading_it(self):
        """`/licence-activated` is not a route the main app serves."""
        code = _script_code(guard._activation_html())

        assert "location.replace('/')" in code
        assert "location.reload" not in code, (
            "reloading re-requests /licence-activated, which the app that "
            "just started does not serve — a 404 instead of the app"
        )

    def test_the_manual_fallback_link_goes_to_the_app(self):
        """The escape hatch shown when the wait runs long. It is the last
        thing left if the poll logic is wrong, so it must not be wired to the
        same reload that fails."""
        html = guard._activation_html()

        assert 'href="/"' in html
        assert "onclick" not in html

    def test_the_template_placeholder_is_fully_substituted(self):
        html = guard._activation_html()

        assert "__PROBE_PATH__" not in html
