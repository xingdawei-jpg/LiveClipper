from __future__ import annotations

import socket
import threading
import time
import webbrowser
import os
import sys
import json
import urllib.request
from pathlib import Path
import winreg

import uvicorn


TOOL_RUN_FLAG = "--liveclipper-run-tool"
MODULE_WEB_DIR = Path(__file__).resolve().parent
RUNTIME_LAYOUT_VERSION = 3
LAUNCHER_HEALTH_REPORT_TIMEOUT = 30.0
LAUNCHER_HEALTH_REQUEST_TIMEOUT = 4.0
LAUNCHER_HEALTH_RETRY_DELAY = 0.5
LEGACY_RUNTIME_ROOT = Path(os.environ.get("APPDATA", Path.home())) / "LiveClipper"

if getattr(sys, "frozen", False):
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent / "_internal"))
    REPO_ROOT = BUNDLE_DIR
    os.environ["LIVECLIPPER_BUNDLE_DIR"] = str(BUNDLE_DIR)
    os.environ["LIVECLIPPER_FROZEN"] = "1"
    os.environ["LIVECLIPPER_CODE_SOURCE"] = "bundled"
else:
    BUNDLE_DIR = MODULE_WEB_DIR.parent
    REPO_ROOT = MODULE_WEB_DIR.parent
    os.environ.pop("LIVECLIPPER_BUNDLE_DIR", None)
    os.environ.pop("LIVECLIPPER_FROZEN", None)
    os.environ["LIVECLIPPER_CODE_SOURCE"] = "source"

os.environ["LIVECLIPPER_RUNTIME_LAYOUT"] = str(RUNTIME_LAYOUT_VERSION)
os.environ["LIVECLIPPER_LEGACY_OVERLAYS_PRESENT"] = (
    "1"
    if any((LEGACY_RUNTIME_ROOT / name).exists() for name in ("app", "web_client", "tools"))
    else "0"
)


def _repair_tool_stdio() -> None:
    for name, fd, mode in (("stdin", 0, "r"), ("stdout", 1, "w"), ("stderr", 2, "w")):
        existing = getattr(sys, name, None)
        if existing is not None:
            try:
                existing.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
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


def _load_server_app():
    for module_dir in (BUNDLE_DIR / "app", MODULE_WEB_DIR):
        module_text = str(module_dir)
        if module_dir.is_dir() and module_text not in sys.path:
            sys.path.insert(0, module_text)

    from server import app

    return app


def _icon_path() -> str | None:
    candidates = []
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


def _launcher_health_destination(destination_text: str, token: str) -> Path:
    destination = Path(destination_text).resolve()
    if destination.name != f"{token}.json":
        raise ValueError("health receipt filename does not match launcher token")
    if destination.parent.name.lower() != "launcher_health":
        raise ValueError("health receipt is outside the launcher health directory")
    if destination.parent.parent.name.lower() != "liveclipper":
        raise ValueError("health receipt directory is outside LiveClipper data")
    return destination


def _write_launcher_health_diagnostic(destination: Path | None, message: str) -> None:
    candidates: list[Path] = []
    if destination is not None:
        candidates.extend(
            [
                destination.parent / "runtime-health.log",
                destination.parent.parent / "update_logs" / "runtime-health.log",
            ]
        )
    fallback = Path(
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("APPDATA")
        or Path.home()
    ) / "LiveClipper" / "update_logs" / "runtime-health.log"
    candidates.append(fallback)

    written: set[str] = set()
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
    for path in candidates:
        key = os.path.normcase(str(path))
        if key in written:
            continue
        written.add(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except Exception:
            continue


def _report_launcher_health(
    port: int,
    report_timeout: float = LAUNCHER_HEALTH_REPORT_TIMEOUT,
) -> bool:
    token = str(os.environ.get("LIVECLIPPER_HEALTH_TOKEN") or "").strip()
    destination_text = str(os.environ.get("LIVECLIPPER_HEALTH_FILE") or "").strip()
    expected_version = str(os.environ.get("LIVECLIPPER_ACTIVE_VERSION") or "").strip()
    if not token or not destination_text or not expected_version:
        return False

    destination: Path | None = None
    try:
        destination = _launcher_health_destination(destination_text, token)
    except Exception as exc:
        _write_launcher_health_diagnostic(
            destination,
            f"health receipt rejected before reporting: {type(exc).__name__}: {exc}",
        )
        return False

    deadline = time.monotonic() + max(1.0, float(report_timeout))
    attempt = 0
    last_error = "unknown error"
    while time.monotonic() < deadline:
        attempt += 1
        temporary = destination.with_name(destination.name + f".tmp-{os.getpid()}")
        try:
            remaining = max(0.5, deadline - time.monotonic())
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/runtime",
                timeout=min(LAUNCHER_HEALTH_REQUEST_TIMEOUT, remaining),
            ) as response:
                runtime = json.loads(response.read().decode("utf-8-sig"))
            integrity = runtime.get("runtime_integrity") if isinstance(runtime, dict) else {}
            healthy = bool(
                isinstance(runtime, dict)
                and runtime.get("version") == expected_version
                and int(runtime.get("runtime_layout_version") or 0) == RUNTIME_LAYOUT_VERSION
                and runtime.get("code_source") == "bundled"
                and isinstance(integrity, dict)
                and integrity.get("ok") is True
            )
            if not healthy:
                actual = {
                    "version": runtime.get("version") if isinstance(runtime, dict) else None,
                    "runtime_layout_version": (
                        runtime.get("runtime_layout_version") if isinstance(runtime, dict) else None
                    ),
                    "code_source": runtime.get("code_source") if isinstance(runtime, dict) else None,
                    "runtime_integrity": integrity,
                }
                raise RuntimeError(
                    "runtime validation failed: "
                    + json.dumps(actual, ensure_ascii=False, separators=(",", ":"))
                )

            destination.parent.mkdir(parents=True, exist_ok=True)
            receipt = {
                "token": token,
                "version": expected_version,
                "runtime_integrity_ok": True,
                "pid": os.getpid(),
                "port": port,
                "reported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "attempt": attempt,
            }
            temporary.unlink(missing_ok=True)
            temporary.write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            confirmed = json.loads(temporary.read_text(encoding="utf-8-sig"))
            if confirmed.get("token") != token or confirmed.get("version") != expected_version:
                raise RuntimeError("health receipt read-back validation failed")
            os.replace(temporary, destination)
            _write_launcher_health_diagnostic(
                destination,
                f"health receipt confirmed for {expected_version} on attempt {attempt}",
            )
            return True
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            temporary.unlink(missing_ok=True)
            _write_launcher_health_diagnostic(
                destination,
                f"health attempt {attempt} failed for {expected_version}: {last_error}",
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(LAUNCHER_HEALTH_RETRY_DELAY, remaining))

    _write_launcher_health_diagnostic(
        destination,
        f"health reporting exhausted after {attempt} attempts for {expected_version}: {last_error}",
    )
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


def _version_at_least(version: str, minimum: str) -> bool:
    def parts(value: str) -> list[int]:
        result = []
        for chunk in str(value or "").split("."):
            digits = "".join(ch for ch in chunk if ch.isdigit())
            result.append(int(digits) if digits else 0)
        return (result + [0, 0, 0, 0])[:4]

    return parts(version) >= parts(minimum)


def _registry_value(root: int, path: str, name: str) -> str:
    try:
        with winreg.OpenKey(root, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value or "")
    except Exception:
        return ""


def _bundled_webview2_runtime() -> Path | None:
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(BUNDLE_DIR / "webview2_runtime")
        candidates.append(Path(sys.executable).resolve().parent / "_internal" / "webview2_runtime")
    else:
        candidates.append(
            REPO_ROOT
            / "vendor"
            / "webview2_runtime_x64"
            / "Microsoft.WebView2.FixedVersionRuntime.149.0.4022.98.x64"
        )
    for path in candidates:
        if (path / "msedgewebview2.exe").exists():
            return path
    return None


def _has_webview2_runtime() -> bool:
    if _bundled_webview2_runtime():
        return True

    net_release = _registry_value(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full",
        "Release",
    )
    try:
        if int(net_release or "0") < 394802:  # .NET Framework 4.6.2
            return False
    except ValueError:
        return False

    client_ids = (
        "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",  # WebView2 Runtime
        "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",  # Beta
        "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}",  # Dev
        "{65C35B14-6C1D-4122-AC46-7148CC9D6497}",  # Canary
    )
    roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    prefixes = (
        r"SOFTWARE\Microsoft\EdgeUpdate\Clients",
        r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients",
    )
    for root in roots:
        for prefix in prefixes:
            for client_id in client_ids:
                version = _registry_value(root, rf"{prefix}\{client_id}", "pv")
                if version and _version_at_least(version, "86.0.622.0"):
                    return True
    return False


def _show_webview2_error(url: str, error: Exception | None = None) -> None:
    detail = f"\n\n错误信息：{error}" if error else ""
    message = (
        "LiveClipper 需要 WebView2 Runtime 才能显示桌面界面。\n\n"
        "当前没有检测到可用的系统 WebView2，且包内固定版 Runtime 未能启动。\n"
        "为了避免旧 IE 内核导致界面错乱，本次不会继续打开内置窗口。\n\n"
        f"本次将临时用系统浏览器打开：{url}"
        f"{detail}"
    )
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("需要安装 WebView2 Runtime", message)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)


def _open_in_system_browser(url: str) -> None:
    webbrowser.open(url)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        return


def _running_task_count(port: int) -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tasks", timeout=1.5) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return 0
    tasks = data.get("tasks") if isinstance(data, dict) else []
    if not isinstance(tasks, list):
        return 0
    return sum(1 for task in tasks if str((task or {}).get("status") or "").lower() in {"queued", "running"})


def _minimize_window_later(window) -> None:
    def _run() -> None:
        time.sleep(0.05)
        try:
            window.minimize()
        except Exception:
            try:
                window.hide()
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()


def _protect_running_tasks_on_close(window, port: int, emit_log) -> None:
    def _on_closing() -> bool | None:
        count = _running_task_count(port)
        if count <= 0:
            return None
        try:
            emit_log("warning", f"检测到 {count} 个任务仍在运行，窗口已最小化到任务栏，后台继续处理。", "system")
        except Exception:
            pass
        _minimize_window_later(window)
        return False

    try:
        window.events.closing += _on_closing
    except Exception:
        pass


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

    if os.environ.get("LIVECLIPPER_HEALTH_TOKEN"):
        if _report_launcher_health(port):
            emit_log("info", "Runtime V3 启动健康确认已提交。", "system")
        else:
            emit_log("error", "Runtime V3 启动健康确认失败，将由启动器回滚。", "system")

    if not _has_webview2_runtime():
        _show_webview2_error(url)
        _open_in_system_browser(url)
        server.should_exit = True
        return

    try:
        import webview
        bundled_runtime = _bundled_webview2_runtime()
        if bundled_runtime:
            webview.settings["WEBVIEW2_RUNTIME_PATH"] = str(bundled_runtime)

        window = webview.create_window(
            "LiveClipper",
            url,
            width=1280,
            height=820,
            min_size=(980, 640),
            text_select=True,
        )
        _protect_running_tasks_on_close(window, port, emit_log)
        icon_path = _icon_path()
        try:
            kwargs = {"gui": "edgechromium"}
            if icon_path:
                kwargs["icon"] = icon_path
            webview.start(**kwargs)
        except TypeError:
            webview.start(gui="edgechromium")
        server.should_exit = True
        return
    except Exception as exc:
        _show_webview2_error(url, exc)
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
