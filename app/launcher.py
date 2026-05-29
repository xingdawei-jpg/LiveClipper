"""
LiveClipper 启动器
- 极简入口，只负责定位 app/ 目录并启动主程序
- PyInstaller 只打包此文件 → exe 极小且几乎不需要更新
- 所有业务代码在 app/ 目录，可增量替换
"""
import os
import sys
import json

try:
    import gui  # PyInstaller: 自动收集 gui 及其依赖
except ImportError:
    pass  # 开发模式下 app/ 还未加入 sys.path


# ---------- 工具 ----------

def _get_base_path():
    """获取可执行文件或脚本所在目录（统一路径）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))


def _get_update_dir():
    """获取持久更新目录（不受 EXE 打包/重装影响）"""
    return os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'LiveClipper', 'app')


def _version_key(version):
    parts = []
    for chunk in str(version or "").replace("-", ".").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts[:4] + [0] * (4 - len(parts)))


def _read_app_version(app_dir):
    try:
        with open(os.path.join(app_dir, "version.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("version") or data.get("latest_version") or "0"
    except Exception:
        return "0"


def _is_valid_app_dir(app_dir):
    required = ("gui.py", "config.py", "schedule_page.py", "product_scan_page.py")
    return (
        os.path.isdir(app_dir)
        and all(os.path.isfile(os.path.join(app_dir, name)) for name in required)
    )


def find_app_dir():
    """
    定位 app/ 目录。

    老版本会把更新文件放在 %APPDATA%/LiveClipper/app/。如果那里残留了
    不完整或更旧的文件，不能让它盖掉新安装包内置的完整 app。
    """
    base = _get_base_path()
    update_dir = _get_update_dir()

    candidates = []
    if not getattr(sys, 'frozen', False):
        candidates.append((base, 100))
    candidates.extend([
        (os.path.join(base, '_internal', 'app'), 90),  # PyInstaller onedir bundled app
        (os.path.join(base, 'app'), 80),               # exe sibling app
        (update_dir, 60),                              # legacy incremental update app
    ])

    # 最终尝试：从 _MEIPASS 查找（兼容旧 onefile 包）
    if getattr(sys, 'frozen', False) and hasattr(sys, "_MEIPASS"):
        candidates.append((os.path.join(sys._MEIPASS, 'app'), 70))

    valid = []
    for d, priority in candidates:
        d = os.path.normpath(d)
        if _is_valid_app_dir(d):
            valid.append((_version_key(_read_app_version(d)), priority, d))

    if valid:
        valid.sort(reverse=True)
        return valid[0][2]

    return None


# ---------- 主入口 ----------

def main():
    base = _get_base_path()
    os.chdir(base)

    app_dir = find_app_dir()
    if app_dir is None:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "启动失败",
            "找不到 app/ 目录，请确认程序文件完整。\n\n"
            "预期位置：程序所在目录的 app/ 文件夹"
        )
        sys.exit(1)

    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)

    from gui import main as gui_main
    gui_main()


if __name__ == '__main__':
    main()
