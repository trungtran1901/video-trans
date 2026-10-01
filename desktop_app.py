import os
import sys
import socket
import threading
import time
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8756


def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _prepend_bundled_ffmpeg_to_path():
    ffmpeg_dir = _app_root() / "ffmpeg" / "bin"
    if ffmpeg_dir.exists():
        os.environ["PATH"] = str(ffmpeg_dir) + os.pathsep + os.environ.get("PATH", "")


def _run_server():
    import uvicorn
    from app.main import app
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


def _wait_for_server(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, PORT), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def main():
    _prepend_bundled_ffmpeg_to_path()

    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()

    if not _wait_for_server():
        print("Khong the khoi dong may chu noi bo. Kiem tra cong 8756 co dang bi chiem dung khong.")
        sys.exit(1)

    import webview

    window = webview.create_window(
        "Video Translator & Dubbing",
        f"http://{HOST}:{PORT}",
        width=1360,
        height=880,
        min_size=(1024, 700),
    )
    webview.start(gui="edgechromium")


if __name__ == "__main__":
    main()