"""Packaged-app entry point.

Starts Flask in a background daemon thread, waits for it to accept
connections, opens the default browser, then hands control to the
system-tray icon loop.  The process exits cleanly when the user clicks
Quit in the tray menu.
"""
import logging
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

import pystray
import requests
from PIL import Image, ImageDraw

from mtgcompare import web

PORT = 5000
URL = f"http://127.0.0.1:{PORT}"


def _data_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home())) / "mtgcompare"


def _setup_logging() -> None:
    """Route WARNING+ logs to %APPDATA%\mtgcompare\app.log when frozen."""
    if not getattr(sys, "frozen", False):
        return
    log_dir = _data_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_dir / "app.log", encoding="utf-8")],
    )


def _make_icon() -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([2, 2, size - 2, size - 2], fill="#5b8def")
    text = "M"
    bbox = draw.textbbox((0, 0), text)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) // 2, (size - th) // 2 - 1), text, fill="white")
    return img


def _wait_for_server(timeout: int = 20) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            requests.get(URL, timeout=1)
            return True
        except Exception:
            time.sleep(0.15)
    return False


def _open_browser(_icon=None, _item=None) -> None:
    webbrowser.open(URL)


def _quit(icon: pystray.Icon, _item=None) -> None:
    icon.stop()
    # Flask thread is a daemon; the process exits once the tray loop ends.


def main() -> None:
    _setup_logging()

    server_thread = threading.Thread(
        target=lambda: web.app.run(
            host="127.0.0.1", port=PORT, debug=False, use_reloader=False
        ),
        daemon=True,
        name="flask",
    )
    server_thread.start()

    if _wait_for_server():
        webbrowser.open(URL)
    else:
        logging.error("Flask server did not start within timeout.")

    icon = pystray.Icon(
        name="mtgcompare",
        icon=_make_icon(),
        title="mtgcompare",
        menu=pystray.Menu(
            pystray.MenuItem("Open browser", _open_browser, default=True),
            pystray.MenuItem("Quit", _quit),
        ),
    )
    icon.run()


if __name__ == "__main__":
    main()
