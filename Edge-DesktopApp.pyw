"""Edge v2.0 — frameless desktop-app launcher.

Spawns the Streamlit server in the background and opens a native-looking
Chrome/Edge window pointed at it (via --app= flag). Close the window to
shut down the server too. Zero terminal visible.

Double-click this .pyw file to launch - Windows runs .pyw with pythonw.exe
so there's no console window.

Requires: Python 3.10+, Streamlit, and either Chrome or Edge installed.
"""
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = 8601
URL = f"http://localhost:{PORT}"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except (OSError, socket.timeout):
        return False


def _wait_for_server(timeout_s: int = 30) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _port_open(PORT):
            return True
        time.sleep(0.4)
    return False


def _find_browser_app_mode():
    """Try Chrome then Edge in --app= mode for a chromeless window."""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def main():
    # start server if not already up
    server = None
    if not _port_open(PORT):
        server = subprocess.Popen(
            [sys.executable, "-m", "streamlit", "run", str(HERE / "app.py"),
             f"--server.port={PORT}", "--server.headless=true",
             "--browser.gatherUsageStats=false"],
            cwd=str(HERE),
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    if not _wait_for_server(30):
        # fall back to default browser
        webbrowser.open(URL)
        return

    browser = _find_browser_app_mode()
    if browser:
        proc = subprocess.Popen([browser, f"--app={URL}", "--new-window"])
        proc.wait()  # block until the app window closes
    else:
        webbrowser.open(URL)
        if server:
            server.wait()
    if server and server.poll() is None:
        try:
            server.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()
