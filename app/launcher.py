"""
LiveClipper 启动器
- 极简入口，只负责定位当前安装包内的 app/ 目录并启动主程序
- 用户数据目录不允许提供或覆盖程序代码
"""
import os
import sys

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


def _is_valid_app_dir(app_dir):
    required = ("gui.py", "config.py", "schedule_page.py", "product_scan_page.py")
    return (
        os.path.isdir(app_dir)
        and all(os.path.isfile(os.path.join(app_dir, name)) for name in required)
    )


def find_app_dir():
    """定位源码目录或当前安装包内唯一的 app/ 目录。"""
    base = _get_base_path()

    if not getattr(sys, 'frozen', False):
        return base if _is_valid_app_dir(base) else None

    candidates = [
        os.path.join(base, '_internal', 'app'),
        os.path.join(base, 'app'),
    ]

    if hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, 'app'))

    for d in candidates:
        d = os.path.normpath(d)
        if _is_valid_app_dir(d):
            return d

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
