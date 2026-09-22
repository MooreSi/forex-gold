"""Capture a dashboard screenshot for the README, from the app that is running.

The README's images (`docs/images/dashboard-*.png`) are 1680x1020 and have the
account number and every money figure blurred out of the header. Both were done
by hand until 2026-09-22, which is why the Trading and Settings shots stayed on
the NiceGUI dashboard for four days after React replaced it: reshooting them
was a manual chore nobody wanted. This makes it one command.

It drives headless Chrome over the DevTools protocol against `localhost:8888`,
clicks the tabs you name, blurs the header, and writes the PNG.

    .venv/bin/python -m tools.dashboard_screenshot out.png Trading "EA templates"
    .venv/bin/python -m tools.dashboard_screenshot out.png Parsing Settings

Each positional after the output path is the exact visible text of a control to
click, in order, and clicking waits for that control to actually come up
selected rather than sleeping and hoping.

**It clicks what you name and nothing else.** Every label above is a tab. Do
not name a control that trades: the header's account badge switches the app
between the live and demo environments, and Market order, Limit order, Execute
and Save all act on the running app. This script has no idea which of the
labels you hand it are safe -- you do.

It reads the app as it is. It does not start it, and it cannot tell a live
environment from a demo one, so check the badge before you shoot: the blur
covers the account number and the balances, and nothing else.

Two things that cost an hour when this was written, kept here so they are not
relearned:

- **React ignores `element.click()`** here -- the tab stays put and the call
  reports success. Clicks have to be real input events through
  `Input.dispatchMouseEvent`, which is why this talks CDP rather than using
  `Runtime.evaluate` for everything.
- **The template rows report `aria-pressed`, the tabs report `aria-selected`.**
  Checking only the latter reads a selected template as unselected, so the
  retry loop clicks it again -- and an even number of clicks lands back where
  it started, with the editor showing "Pick a template to edit it".
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
WIDTH, HEIGHT = 1680, 1020

# The header's account number and money figures. Everything the README's other
# screenshots have blurred, by the test id the component already carries.
BLURRED = ("account-badge", "stat-balance", "stat-free", "stat-equity", "stat-lifetime")

BLUR_JS = """
(() => {
  for (const t of %s) {
    for (const el of document.querySelectorAll(`[data-testid="${t}"]`)) {
      el.style.filter = 'blur(5px)';
    }
  }
  return true;
})()
"""

# Locate by exact visible text. `selected` covers both spellings: tabs say
# aria-selected, the template rows say aria-pressed.
LOCATE_JS = """
(() => {
  const want = %s;
  const els = [...document.querySelectorAll('button,[role="tab"],a')];
  const hit = els.find(e => (e.textContent || '').trim() === want && e.offsetParent !== null);
  if (!hit) return JSON.stringify({miss: want});
  const r = hit.getBoundingClientRect();
  return JSON.stringify({
    x: r.left + r.width / 2,
    y: r.top + r.height / 2,
    selected: hit.getAttribute('aria-selected') === 'true'
           || hit.getAttribute('aria-pressed') === 'true'
           || [...document.querySelectorAll('h1,h2,h3,h4')]
                .some(h => (h.textContent || '').trim() === want),
  });
})()
"""


def _start_chrome(port: int, profile: str) -> subprocess.Popen:
    if not Path(CHROME).exists():
        sys.exit(f"Google Chrome not found at {CHROME}")
    return subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={port}",
         f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
         "--hide-scrollbars", "--force-color-profile=srgb",
         f"--window-size={WIDTH},{HEIGHT}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _debugger_url(port: int, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
            pages = [t for t in tabs if t["type"] == "page"]
            if pages:
                return pages[0]["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.5)
    sys.exit("headless Chrome did not come up")


async def capture(out: Path, clicks: list[str], url: str, port: int) -> None:
    import websockets  # local: only this tool needs it

    # Chrome writes its profile out as it shuts down, so the directory cannot
    # be removed by a context manager that exits the moment the socket closes.
    profile = tempfile.mkdtemp(prefix="dashboard-shot-")
    chrome = _start_chrome(port, profile)
    counter = 0

    try:
        async with websockets.connect(_debugger_url(port), max_size=100 * 1024 * 1024) as ws:
            async def cmd(method: str, params: dict | None = None) -> dict:
                nonlocal counter
                counter += 1
                await ws.send(json.dumps({"id": counter, "method": method,
                                          "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == counter:
                        return msg.get("result", {})

            async def js(expr: str):
                r = await cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True})
                return r.get("result", {}).get("value")

            async def click(text: str) -> bool:
                for _ in range(8):
                    where = json.loads(await js(LOCATE_JS % json.dumps(text)))
                    if "miss" in where:
                        await asyncio.sleep(1.0)
                        continue
                    for event in ("mousePressed", "mouseReleased"):
                        await cmd("Input.dispatchMouseEvent", {
                            "type": event, "x": where["x"], "y": where["y"],
                            "button": "left", "clickCount": 1,
                        })
                    # Poll rather than check once: the lists re-render on the
                    # app's own poll, and a click that lands mid-render is lost.
                    for _ in range(10):
                        await asyncio.sleep(0.5)
                        if json.loads(await js(LOCATE_JS % json.dumps(text))).get("selected"):
                            print(f"  {text} -> selected")
                            return True
                print(f"  {text} -> NOT SELECTED", file=sys.stderr)
                return False

            await cmd("Page.enable")
            await cmd("Runtime.enable")
            await cmd("Emulation.setDeviceMetricsOverride",
                      {"width": WIDTH, "height": HEIGHT,
                       "deviceScaleFactor": 1, "mobile": False})
            await cmd("Page.navigate", {"url": url})
            await asyncio.sleep(9)  # first paint waits on the bridge's first poll

            missed = [text for text in clicks if not await click(text)]
            if missed:
                raise SystemExit(
                    f"never selected {missed} -- refusing to write a screenshot "
                    f"of the wrong screen")

            await js(BLUR_JS % json.dumps(list(BLURRED)))
            await asyncio.sleep(0.5)
            shot = await cmd("Page.captureScreenshot", {"format": "png"})
            out.write_bytes(base64.b64decode(shot["data"]))
            await cmd("Browser.close")

    finally:
        try:
            chrome.wait(timeout=15)
        except subprocess.TimeoutExpired:
            chrome.kill()
        shutil.rmtree(profile, ignore_errors=True)

    print(f"wrote {out} ({WIDTH}x{HEIGHT})")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", type=Path, help="PNG to write")
    ap.add_argument("clicks", nargs="*", help="exact label of each control to click, in order")
    ap.add_argument("--url", default="http://localhost:8888", help="the running app")
    ap.add_argument("--port", type=int, default=9333, help="Chrome debugging port")
    args = ap.parse_args(argv)
    asyncio.run(capture(args.out, args.clicks, args.url, args.port))


if __name__ == "__main__":
    main()
