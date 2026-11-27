"""Windows entry point for Meniscus, used by the packaged build and usable from source.

  Meniscus.exe                     start the server if needed and open the app window
  Meniscus.exe --serve             run only the server, in the foreground
  meniscus-engine.exe gallery-dl …  run the bundled gallery-dl (the server uses this)
  meniscus-engine.exe yt-dlp …      run the bundled yt-dlp

The window is a Chrome, Edge or Brave app window with its own profile under the data folder.
When it is closed, Meniscus keeps running until queued and running downloads have finished,
then exits.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

PORT = int(os.environ.get("MENISCUS_PORT", "47821"))
URL = f"http://127.0.0.1:{PORT}/"
IDLE_EXIT_SECONDS = 8      # how long with no window and nothing downloading before exiting
FIRST_CONNECT_SECONDS = 90  # how long to wait for the window to connect the first time


def run_engine(tool: str, args: list[str]) -> None:
    for stream in (sys.stdout, sys.stderr):  # the server reads these as UTF-8, line by line
        if stream is not None:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.argv = [tool, *args]
    if tool == "gallery-dl":
        import gallery_dl
        sys.exit(gallery_dl.main())
    import yt_dlp
    yt_dlp.main()


def server_up() -> bool:
    try:
        urllib.request.urlopen(URL + "api/state", timeout=1).close()
        return True
    except OSError:
        return False


def open_window(profile: Path) -> None:
    from login import find_browser
    browser = find_browser()
    if not browser:
        webbrowser.open(URL)
        return
    subprocess.Popen([browser, f"--app={URL}", f"--user-data-dir={profile}", "--no-first-run",
                      "--no-default-browser-check", "--window-size=1440,920"])


def message(text: str) -> None:
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, text, "Meniscus", 0x10)


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in ("gallery-dl", "yt-dlp"):
        return run_engine(args[0], args[1:])

    import server  # sets up the data folder, so import it only for the app itself

    home = server.HOME or server.APP_DATA.parent
    if args[:1] == ["--serve"]:
        return server.main()

    if sys.stdout is None or getattr(sys, "frozen", False):  # a windowed exe has no console
        log = open(home / "meniscus.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
        print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} starting", flush=True)

    profile = home / "browser"
    if server_up():  # already running: just bring up another window
        return open_window(profile)

    t = threading.Thread(target=server.main, daemon=True)
    t.start()
    deadline = time.time() + 60
    while not server_up():
        if not t.is_alive() or time.time() > deadline:
            message(f"Meniscus could not start its server on port {PORT}.\n\nDetails are in {home / 'meniscus.log'}")
            sys.exit(1)
        time.sleep(0.2)
    open_window(profile)

    started, seen_window, idle_since = time.time(), False, None
    while t.is_alive():
        time.sleep(1)
        if server.BUS.subs:
            seen_window, idle_since = True, None
            continue
        if not seen_window and time.time() - started < FIRST_CONNECT_SECONDS:
            continue
        if server.LOGINS.active() or any(j["status"] in ("queued", "running") for j in list(server.JOBS.values())):
            idle_since = None
            continue
        idle_since = idle_since or time.time()
        if time.time() - idle_since > IDLE_EXIT_SECONDS:
            break
    print("window closed and nothing downloading; exiting", flush=True)
    os._exit(0)


if __name__ == "__main__":
    main()
