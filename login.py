"""Site logins: sign in once in a Meniscus-owned browser window, download with that session.

gallery-dl and yt-dlp can read cookies straight from a browser on Linux, but on Windows current
Chrome and Edge encrypt them with app-bound encryption, which those tools can't undo, and the
cookie database is locked while the browser runs. So Meniscus opens its own Chrome/Edge/Brave
window with a separate profile and local remote debugging. While that window is open it asks the
browser for its cookies every couple of seconds over the DevTools protocol and writes them to a
cookies.txt, which each download gets a copy of. The debugging port only exists while the
sign-in window is open, and only on 127.0.0.1.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

# Where to send people for the sites that are most often asked about; others get their home page.
LOGIN_PAGES = {
    "instagram.com": "https://www.instagram.com/accounts/login/",
    "x.com": "https://x.com/i/flow/login",
    "twitter.com": "https://x.com/i/flow/login",
    "reddit.com": "https://www.reddit.com/login/",
    "tiktok.com": "https://www.tiktok.com/login",
    "facebook.com": "https://www.facebook.com/login/",
    "pixiv.net": "https://accounts.pixiv.net/login",
    "tumblr.com": "https://www.tumblr.com/login",
}
POLL_SECONDS = 2


def find_browser() -> str | None:
    """A Chromium-family browser: needed for the app window and for sign-in."""
    if sys.platform == "win32":
        roots = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        for rel in (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe",
                    r"BraveSoftware\Brave-Browser\Application\brave.exe", r"Chromium\Application\chrome.exe"):
            for root in roots:
                if root and (Path(root) / rel).is_file():
                    return str(Path(root) / rel)
        return None
    for name in ("google-chrome", "chromium", "chromium-browser", "brave-browser", "microsoft-edge"):
        if shutil.which(name):
            return shutil.which(name)
    return None


def login_url(target: str) -> str:
    t = (target or "").strip() or "instagram.com"
    if "://" not in t:
        t = "https://" + t
    host = (urlparse(t).hostname or "").lower()
    if not host:
        raise ValueError("that doesn't look like a site")
    for domain, page in LOGIN_PAGES.items():
        if host == domain or host.endswith("." + domain):
            return page
    return f"https://{host}/"


def site_of(domain: str) -> str:
    """'.www.instagram.com' -> 'instagram.com' (good enough for showing which sites are signed in)."""
    parts = domain.lstrip(".").lower().split(".")
    keep = 3 if len(parts) > 2 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "net", "org") else 2
    return ".".join(parts[-keep:])


def to_netscape(cookies: list[dict]) -> str:
    lines = ["# Netscape HTTP Cookie File", "# Written by Meniscus from its sign-in window", ""]
    for c in cookies:
        domain = c["domain"]
        expires = int(c.get("expires") or 0)
        lines.append("\t".join([domain, "TRUE" if domain.startswith(".") else "FALSE", c.get("path") or "/",
                                "TRUE" if c.get("secure") else "FALSE", str(max(expires, 0)),
                                c["name"], c.get("value", "")]))
    return "\n".join(lines) + "\n"


class Logins:
    def __init__(self, home: Path, on_change=None) -> None:
        self.profile = home / "login-browser"
        self.cookies_txt = home / "cookies.txt"
        self.on_change = on_change or (lambda status: None)
        self._lock = threading.Lock()
        self._watcher: threading.Thread | None = None
        self.last_error = ""

    # ------------------------------------------------------------------ state
    def active(self) -> bool:
        return bool(self._watcher and self._watcher.is_alive())

    def sites(self) -> list[str]:
        try:
            text = self.cookies_txt.read_text(encoding="utf-8")
        except OSError:
            return []
        out = set()
        for line in text.splitlines():
            if line and not line.startswith("#"):
                out.add(site_of(line.split("\t", 1)[0]))
        return sorted(out)

    def status(self) -> dict:
        return {"sites": self.sites(), "active": self.active(), "browser": bool(find_browser()),
                "error": self.last_error}

    # ---------------------------------------------------------------- actions
    def open(self, target: str) -> dict:
        url = login_url(target)
        browser = find_browser()
        if not browser:
            raise RuntimeError("Signing in needs Chrome, Edge or Brave, and none was found")
        with self._lock:
            self.profile.mkdir(parents=True, exist_ok=True)
            port_file = self.profile / "DevToolsActivePort"
            fresh = not self.active()
            if fresh:
                port_file.unlink(missing_ok=True)
            # If the sign-in browser is already open this just adds a window to it.
            # Port 0 lets Chrome pick a free port (it listens on 127.0.0.1 only) and write it to
            # DevToolsActivePort in the profile.
            subprocess.Popen([browser, f"--user-data-dir={self.profile}", "--remote-debugging-port=0",
                              "--no-first-run", "--no-default-browser-check", "--new-window", url])
            if fresh:
                self.last_error = ""
                self._watcher = threading.Thread(target=self._watch, args=(port_file,), daemon=True)
                self._watcher.start()
        status = self.status()
        status["active"] = True
        return status

    def forget(self) -> dict:
        if self.active():
            raise RuntimeError("Close the sign-in window first")
        self.cookies_txt.unlink(missing_ok=True)
        shutil.rmtree(self.profile, ignore_errors=True)
        status = self.status()
        self.on_change(status)
        return status

    # ---------------------------------------------------------------- watcher
    def _watch(self, port_file: Path) -> None:
        from websockets.exceptions import ConnectionClosed
        from websockets.sync.client import connect

        deadline = time.time() + 30
        while not port_file.exists():
            if time.time() > deadline:
                self.last_error = "the sign-in window didn't open a debugging connection"
                return self.on_change(self.status())
            time.sleep(0.2)
        time.sleep(0.2)  # Chrome writes the file in two lines; let it finish
        try:
            port, path = port_file.read_text().split()[:2]
        except (OSError, ValueError):
            self.last_error = "couldn't read the sign-in window's debugging port"
            return self.on_change(self.status())
        self.on_change(self.status())
        last, n = None, 0
        try:
            with connect(f"ws://127.0.0.1:{port}{path}", max_size=None, open_timeout=10) as ws:
                while True:
                    n += 1
                    ws.send(json.dumps({"id": n, "method": "Storage.getCookies"}))
                    while True:
                        msg = json.loads(ws.recv(timeout=15))
                        if msg.get("id") == n:
                            break
                    text = to_netscape(msg.get("result", {}).get("cookies", []))
                    if text != last:
                        tmp = self.cookies_txt.with_suffix(".tmp")
                        tmp.write_text(text, encoding="utf-8")
                        os.replace(tmp, self.cookies_txt)
                        last = text
                        self.on_change(self.status())
                    time.sleep(POLL_SECONDS)
        except (ConnectionClosed, OSError, TimeoutError):
            pass  # the sign-in window was closed
        except Exception as e:  # noqa: BLE001 - report it rather than dying silently
            self.last_error = f"sign-in window: {e}"
        finally:
            self._watcher = None
            self.on_change(self.status())
