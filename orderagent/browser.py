"""How the agent gets a browser -- and why it is not the obvious way.

Playwright's own launch is the obvious way, and it does not work here.
Starbucks' order flow dies silently under it: the store-detail request
leaves the page and no response ever arrives, so the app shows
「通信エラーが発生しました」 forever. The same click in an ordinary Chrome
succeeds. The difference is not the automation -- CDP driving an ordinary
Chrome works perfectly -- it is the ~40 flags Playwright passes at launch
(2026-08-19, established by testing headless, headful, desktop and mobile
variants against a plain Chrome with a debugging port).

So: start Chrome the way a person's Chrome starts, then attach over CDP.
The browser is ordinary; only the hands are ours.

This also gives McDonald's a better home than its own launch call, but
that side is left alone -- it works, and it is now MENU_ONLY anyway.
"""
from __future__ import annotations

import contextlib
import socket
import subprocess
import time
from typing import Any, Iterator, Optional

from . import config

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CDP_PORT = int(__import__("os").environ.get("ORDER_CDP_PORT", "9222"))
_STARTUP_TIMEOUT_SECONDS = 25.0


def _port_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def launch_chrome(url: str = "about:blank", headless_window: bool = True) -> Optional[subprocess.Popen]:
    """Start an ordinary Chrome on the agent's profile, with CDP open.

    Returns None when one is already listening -- attaching to the browser
    Daisuke is looking at is a feature, not a collision: he can watch what
    the agent does, and take over mid-flow.

    `headless_window` positions the window off-screen rather than passing
    --headless, because --headless is itself one of the flags that breaks
    the Starbucks flow.
    """
    if _port_open(CDP_PORT):
        return None
    config.ensure_dirs()
    args = [CHROME_PATH,
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={config.PROFILE_DIR}",
            "--no-first-run",
            "--window-size=460,920"]
    if headless_window:
        # Far off the visible desktop: the page still renders and reports
        # real geometry (which --headless changes), but nothing covers
        # what Daisuke is doing.
        args.append("--window-position=-2400,0")
    args.append(url)
    process = subprocess.Popen(args)
    deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _port_open(CDP_PORT):
            return process
        time.sleep(0.5)
    raise RuntimeError("Chrome did not open its debugging port in time")


@contextlib.contextmanager
def attached_page(url: str = "about:blank",
                  headless_window: bool = True) -> Iterator[Any]:
    """A page in an ordinary Chrome, driven over CDP.

    The browser is left running on exit when this call did not start it,
    so a session Daisuke opened by hand survives the agent using it.
    """
    from playwright.sync_api import sync_playwright

    started = launch_chrome(url, headless_window=headless_window)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        try:
            yield page
        finally:
            with contextlib.suppress(Exception):
                browser.close()   # detaches; does not kill the browser
            if started is not None:
                with contextlib.suppress(Exception):
                    started.terminate()
