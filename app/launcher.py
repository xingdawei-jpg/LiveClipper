"""
LiveClipper 启动器
- 极简入口，只负责定位 app/ 目录并启动主程序
- PyInstaller 只打包此文件 → exe 极小且几乎不需要更新
- 所有业务代码在 app/ 目录，可增量替换
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


def _get_update_dir():
    """获取持久更新目录（不受 EXE 打包/重装影响）"""
    return os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'LiveClipper', 'app')


def find_app_dir():
    """
    定位 app/ 目录（优先级：持久更新 > exe同级 > 内嵌 > 临时）

    持久更新目录 %APPDATA%/LiveClipper/app/ 保证：
    - onefile 和 onedir 模式都可用
    - 重启不被 PyInstaller 覆盖
    - 重装 EXE 也不丢失
    """
    base = _get_base_path()
    update_dir = _get_update_dir()

    candidates = [
        update_dir,                               # ① 持久更新目录（推荐，所有版本通用）
        os.path.join(base, 'app'),                # ② exe 同级的持久目录
        os.path.join(base, '_internal', 'app'),   # ③ PyInstaller 内嵌（onedir）
    ]

    for d in candidates:
        d = os.path.normpath(d)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, 'gui.py')):
            return d

    # ④ 最终尝试：从 _MEIPASS 查找（onefile 临时目录）
    if getattr(sys, 'frozen', False):
        d = os.path.join(sys._MEIPASS, 'app')
        d = os.path.normpath(d)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, 'gui.py')):
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
