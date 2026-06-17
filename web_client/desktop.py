from __future__ import annotations

import socket
import threading
import time
import webbrowser
import os
import sys
from pathlib import Path

import uvicorn


def _version_key(version: str) -> tuple[int, int, int, int]:
    parts: list[int] = []
    for chunk in str(version or "").replace("-", ".").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple((parts + [0, 0, 0, 0])[:4])


def _read_version(path: Path) -> str:
    try:
        import json

        version_file = path / "app" / "version.json"
        if not version_file.exists():
            version_file = Path(os.environ.get("APPDATA", Path.home())) / "LiveClipper" / "app" / "version.json"
        data = json.loads(version_file.read_text(encoding="utf-8-sig"))
        return data.get("version") or data.get("latest_version") or "0"
    except Exception:
        return "0"


def _configure_update_path() -> None:
    update_web = Path(os.environ.get("APPDATA", Path.home())) / "LiveClipper" / "web_client"
    if not (update_web / "server.py").exists():
        return
    if not (update_web / "frontend" / "index.html").exists():
        return

    bundled_web = Path(__file__).resolve().parent
    if _version_key(_read_version(update_web.parent)) < _version_key(_read_version(bundled_web.parent)):
        return
    sys.path.insert(0, str(update_web))


def _pick_port(preferred: int = 8765) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


def _start_server(port: int) -> uvicorn.Server:
    _configure_update_path()
    from server import app

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server


def main() -> None:
    port = _pick_port()
    server = _start_server(port)
    from server import emit_log

    url = f"http://127.0.0.1:{port}"
    emit_log("info", f"桌面客户端已启动: {url}", "system")

    time.sleep(0.8)
    try:
        import webview

        window = webview.create_window(
            "LiveClipper",
            url,
            width=1280,
            height=820,
            min_size=(980, 640),
            text_select=True,
        )
        webview.start()
        server.should_exit = True
        return
    except Exception:
        webbrowser.open(url)
        try:
            while not server.should_exit:
                time.sleep(0.5)
        except KeyboardInterrupt:
            server.should_exit = True


if __name__ == "__main__":
    main()
