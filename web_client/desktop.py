from __future__ import annotations

import socket
import threading
import time
import webbrowser
import os
import sys
import importlib.util
from pathlib import Path

import uvicorn


TOOL_RUN_FLAG = "--liveclipper-run-tool"


def _repair_tool_stdio() -> None:
    for name, fd, mode in (("stdin", 0, "r"), ("stdout", 1, "w"), ("stderr", 2, "w")):
        if getattr(sys, name, None) is not None:
            continue
        try:
            stream = os.fdopen(os.dup(fd), mode, encoding="utf-8", errors="replace", buffering=1)
        except Exception:
            fallback = "r" if mode == "r" else "w"
            stream = open(os.devnull, fallback, encoding="utf-8", errors="replace")
        setattr(sys, name, stream)


def _run_tool_subprocess() -> bool:
    if len(sys.argv) < 3 or sys.argv[1] != TOOL_RUN_FLAG:
        return False
    import runpy

    _repair_tool_stdio()
    script = Path(sys.argv[2]).resolve()
    if not script.exists():
        print(f"Tool script not found: {script}", file=sys.stderr, flush=True)
        raise SystemExit(2)
    sys.argv = [str(script), *sys.argv[3:]]
    script_dir = str(script.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    runpy.run_path(str(script), run_name="__main__")
    return True


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


def _updated_server_path() -> Path | None:
    update_web = Path(os.environ.get("APPDATA", Path.home())) / "LiveClipper" / "web_client"
    if not (update_web / "server.py").exists():
        return None
    if not (update_web / "frontend" / "index.html").exists():
        return None

    bundled_web = Path(__file__).resolve().parent
    if _version_key(_read_version(update_web.parent)) <= _version_key(_read_version(bundled_web.parent)):
        return None
    return update_web / "server.py"


def _load_server_app():
    update_server = _updated_server_path()
    if update_server:
        update_web = update_server.parent
        update_app = update_web.parent / "app"
        if update_app.is_dir() and str(update_app) not in sys.path:
            sys.path.insert(0, str(update_app))
        if str(update_web) not in sys.path:
            sys.path.insert(0, str(update_web))
        spec = importlib.util.spec_from_file_location("server", update_server)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            sys.modules["server"] = module
            spec.loader.exec_module(module)
            return module.app

    from server import app

    return app


def _icon_path() -> str | None:
    candidates = []
    update_icon = Path(os.environ.get("APPDATA", Path.home())) / "LiveClipper" / "web_client" / "frontend" / "assets" / "liveclipper.ico"
    candidates.append(update_icon)

    web_dir = Path(__file__).resolve().parent
    candidates.append(web_dir / "frontend" / "assets" / "liveclipper.ico")

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "_internal" / "web_client" / "frontend" / "assets" / "liveclipper.ico")
        candidates.append(exe_dir / "_internal" / "assets" / "liveclipper.ico")

    for path in candidates:
        if path.exists():
            return str(path)
    return None


def _pick_port(preferred: int = 8765) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


def _start_server(port: int) -> uvicorn.Server:
    app = _load_server_app()

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


def _wait_for_port(port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.25)
    return False


def _show_startup_error(port: int) -> None:
    message = (
        "LiveClipper 本地服务没有启动成功。\n\n"
        f"访问地址：http://127.0.0.1:{port}\n\n"
        "请先关闭 LiveClipper，重新打开一次。\n"
        "如果仍然失败，请右键 LiveClipperWeb.exe，选择“以管理员身份运行”，"
        "并在 Windows 安全软件/防火墙提示时选择允许。"
    )
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("LiveClipper 启动失败", message)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)


def main() -> None:
    port = _pick_port()
    server = _start_server(port)
    from server import emit_log

    url = f"http://127.0.0.1:{port}"
    emit_log("info", f"桌面客户端已启动: {url}", "system")

    if not _wait_for_port(port):
        server.should_exit = True
        _show_startup_error(port)
        return

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
        icon_path = _icon_path()
        try:
            webview.start(icon=icon_path) if icon_path else webview.start()
        except TypeError:
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
    if _run_tool_subprocess():
        raise SystemExit(0)
    main()
