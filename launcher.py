"""
LiveClipper 启动器
- 极简入口，只负责定位 app/ 目录并启动主程序
- PyInstaller 只打包此文件 → exe 极小且几乎不需要更新
- 所有业务代码在 app/ 目录，可增量替换
"""
import os
import sys


def find_app_dir():
    """定位 app/ 目录"""
    # 1. PyInstaller 打包模式：_MEIPASS 临时目录
    if getattr(sys, 'frozen', False):
        candidates = [
            os.path.join(sys._MEIPASS, 'app'),           # _MEIPASS/app/
            os.path.join(sys._MEIPASS, '..', 'app'),      # 上一级（极少见）
        ]
    else:
        # 2. 开发模式：脚本所在目录
        base = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(base, 'app'),                     # 同级 app/
        ]

    for d in candidates:
        d = os.path.normpath(d)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, 'gui.py')):
            return d

    # 3. 兜底：直接在当前工作目录找
    d = os.path.join(os.getcwd(), 'app')
    if os.path.isdir(d) and os.path.isfile(os.path.join(d, 'gui.py')):
        return d

    return None


def main():
    app_dir = find_app_dir()
    if app_dir is None:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "启动失败",
            "找不到 app/ 目录，请确认程序文件完整。\n\n"
            "预期位置：与启动器同级的 app/ 文件夹"
        )
        sys.exit(1)

    # app/ 优先加入 sys.path
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)

    # 启动主程序
    from gui import main as gui_main
    gui_main()


if __name__ == '__main__':
    main()
