"""
LiveClipper 自动更新模块
- 启动时后台检查 GitHub Releases 是否有新版本
- 支持强制更新、下载进度、SHA256 校验
- 更新后提示用户重启
"""

import os
import sys
import json
import hashlib
import threading
import tempfile
import subprocess
import time
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
from pathlib import Path


# ============ 配置（发布时修改） ============

# GitHub 仓库（私有仓库需在 version.json 里放完整 URL）
GITHUB_REPO = "xingdawei-jpg/LiveClipper"

# version.json 的远程地址（优先使用这个）
# 如果设置了这个，会忽略 GITHUB_REPO
VERSION_URL = ""  # 使用 GITHUB_REPO 自动生成

# 当前版本号（每次发布时更新）
CURRENT_VERSION = "8.3.0"



def init_installed_version():
    """First launch: create .installed_version from version.json in package"""
    try:
        vf = _get_installed_version_file()
        if not os.path.exists(vf):
            # Read version from bundled version.json
            if getattr(sys, 'frozen', False):
                vj = os.path.join(os.path.dirname(sys.executable), '_internal', 'version.json')
            else:
                vj = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'version.json')
            if os.path.exists(vj):
                with open(vj, 'r', encoding='utf-8-sig') as f:
                    vdata = json.load(f)
                ver = vdata.get('latest_version', '')
                if ver:
                    _set_installed_version(ver)
                    return
            _set_installed_version(CURRENT_VERSION)
    except Exception:
        _set_installed_version(CURRENT_VERSION)


def _get_installed_version_file():
    """Path to local version tracking file"""
    base = _get_install_base() if hasattr(sys, 'modules') else os.path.dirname(os.path.abspath(__file__))
    try:
        base = _get_install_base()
    except Exception:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, ".installed_version")

def _get_installed_version():
    """Read installed version from local file, fallback to CURRENT_VERSION"""
    try:
        vf = _get_installed_version_file()
        if os.path.exists(vf):
            with open(vf, "r", encoding="utf-8") as f:
                v = f.read().strip()
            if v:
                return v
    except Exception:
        pass
    return CURRENT_VERSION

def _set_installed_version(version):
    """Write installed version to local file after update"""
    try:
        vf = _get_installed_version_file()
        with open(vf, "w", encoding="utf-8") as f:
            f.write(version.strip())
    except Exception:
        pass


def init_installed_version():
    """First-launch: create .installed_version from version.json if not exists.
    Call this once at app startup before any update check."""
    try:
        vf = _get_installed_version_file()
        if not os.path.exists(vf):
            # Read version from bundled version.json
            if getattr(sys, 'frozen', False):
                vj = os.path.join(os.path.dirname(sys.executable), '_internal', 'version.json')
            else:
                vj = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'version.json')
            if os.path.exists(vj):
                with open(vj, 'r', encoding='utf-8-sig') as f:
                    data = json.load(f)
                ver = data.get('version', CURRENT_VERSION)
                _set_installed_version(ver)
                return ver
            else:
                _set_installed_version(CURRENT_VERSION)
                return CURRENT_VERSION
    except Exception:
        pass
    return CURRENT_VERSION

# 检查更新的 API 地址（GitHub Releases）
def get_version_url():
    """获取 version.json 的实际地址"""
    if VERSION_URL:
        return VERSION_URL
    if GITHUB_REPO and "/" in GITHUB_REPO:
        return f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/app/version.json"
    return ""


# ============ 版本比较 ============

def parse_version(version_str):
    """解析语义化版本号，返回可比较的元组"""
    import re
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", str(version_str))
    if not match:
        return (0, 0, 0)
    return tuple(int(x) for x in match.groups())


def is_newer(remote_version, local_version):
    """判断远程版本是否比本地新"""
    rv = parse_version(remote_version)
    lv = parse_version(local_version)
    return rv > lv


# ============ 下载与校验 ============

def compute_sha256(filepath):
    """计算文件的 SHA256 哈希值"""
    sha = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()


def download_file(url, dest_path, progress_callback=None):
    """
    下载文件，使用 curl.exe（兼容 Gitee 多级重定向）
    progress_callback(downloaded_bytes, total_bytes)
    """
    # 先用 HEAD 获取文件大小
    total_size = 0
    try:
        result = subprocess.run(
            ["curl.exe", "-s", "-k", "-L", "-I", url],
            capture_output=True, encoding="utf-8", timeout=15
        )
        # 取最后一次重定向后的 Content-Length
        for line in reversed(result.stdout.splitlines()):
            if line.lower().startswith("content-length:"):
                val = line.split(":", 1)[1].strip()
                if val.isdigit():
                    total_size = int(val)
                break
    except Exception:
        pass

    # 用 curl 下载
    process = subprocess.Popen(
        ["curl.exe", "-s", "-k", "-L", "-o", dest_path, url],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )

    # 轮询文件大小上报进度
    while process.poll() is None:
        if progress_callback and total_size > 0:
            downloaded = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
            progress_callback(downloaded, total_size)
        time.sleep(0.3)

    process.wait()
    if process.returncode != 0:
        raise Exception(f"curl 下载失败 (code {process.returncode})")

    if progress_callback:
        downloaded = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
        progress_callback(downloaded, downloaded)

    # Check if downloaded file is HTML (Gitee CDN sometimes returns error pages)
    if os.path.exists(dest_path):
        with open(dest_path, 'rb') as f:
            header = f.read(512)
        if b'<html' in header.lower() or b'<!doctype' in header.lower():
            os.remove(dest_path)
            raise Exception("下载被拦截，服务器返回了网页而非更新文件。请检查网络或手动下载更新。")


# ============ 检查更新 ============

def check_update():
    """
    检查是否有新版本
    返回 dict（包含版本信息）或 None（无更新/出错）
    """
    url = get_version_url()
    if not url:
        return None

    try:
        result = subprocess.run(
            ["curl.exe", "-s", "-k", "--max-time", "10", url],
            capture_output=True, encoding="utf-8", timeout=15
        )
        data = json.loads(result.stdout)

        remote_ver = data.get("version", "")
        if not remote_ver or not is_newer(remote_ver, _get_installed_version()):
            return None

        return data

    except Exception:
        return None


# ============ GUI 组件 ============

class UpdateDialog(tk.Toplevel):
    """更新提示对话框"""
    
    def __init__(self, parent, version_info):
        super().__init__(parent)
        self.version_info = version_info
        self.result = None  # "update" / "skip" / "later"
        
        self.title("发现新版本")
        self.geometry("420x280")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        
        # 居中显示
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - 420) // 2
        y = parent.winfo_y() + (parent.winfo_height() - 280) // 2
        self.geometry(f"+{x}+{y}")
        
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_later)
    
    def _build_ui(self):
        vi = self.version_info
        version = vi.get("latest_version", "?")
        notes = vi.get("release_notes", "修复了一些问题")
        # Check if incremental update is available
        is_update = _is_installed() and vi.get("update_url", "")
        if is_update:
            notes += "\n\n（增量更新，仅下载变更文件，几秒完成）"
        force = vi.get("force_update", False)
        
        # 标题
        tk.Label(
            self, text=f"🎉 新版本 v{version} 可用",
            font=("Microsoft YaHei UI", 13, "bold")
        ).pack(pady=(15, 5))
        
        # 更新说明
        tk.Label(
            self, text=notes,
            font=("Microsoft YaHei UI", 9),
            wraplength=380, justify="left",
            fg="#555555"
        ).pack(padx=20, pady=5)
        
        # 强制更新提示
        if force:
            tk.Label(
                self, text="⚠️ 此版本为重要更新，需要立即升级",
                font=("Microsoft YaHei UI", 9),
                fg="#E74C3C"
            ).pack(pady=(0, 5))
        
        # 按钮
        btn_frame = tk.Frame(self)
        btn_frame.pack(pady=10)
        
        if force:
            # 强制更新：只有更新按钮
            tk.Button(
                btn_frame, text="立即更新", width=15,
                command=self._on_update,
                bg="#2196F3", fg="white",
                font=("Microsoft YaHei UI", 10)
            ).pack(side="left", padx=5)
        else:
            tk.Button(
                btn_frame, text="立即更新", width=12,
                command=self._on_update,
                font=("Microsoft YaHei UI", 10)
            ).pack(side="left", padx=5)
            tk.Button(
                btn_frame, text="稍后提醒", width=12,
                command=self._on_later,
                font=("Microsoft YaHei UI", 10)
            ).pack(side="left", padx=5)
    
    def _on_update(self):
        self.result = "update"
        self.destroy()
    
    def _on_later(self):
        self.result = "later"
        self.destroy()


class DownloadDialog(tk.Toplevel):
    """下载进度对话框"""
    
    def __init__(self, parent, version_info, on_complete=None, on_error=None):
        super().__init__(parent)
        self.version_info = version_info
        self.on_complete = on_complete
        self.on_error = on_error
        self.cancelled = False
        
        self.title("正在下载更新")
        self.geometry("400x140")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        
        # 居中
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - 400) // 2
        y = parent.winfo_y() + (parent.winfo_height() - 140) // 2
        self.geometry(f"+{x}+{y}")
        
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        
        # 开始下载
        self.after(100, self._start_download)
    
    def _build_ui(self):
        tk.Label(
            self, text="⬇️ 正在下载更新包...",
            font=("Microsoft YaHei UI", 11)
        ).pack(pady=(15, 5))
        
        self._progress_value = 0
        self._progress_canvas = tk.Canvas(self, width=350, height=20, bg="#E0E0E0", highlightthickness=0)
        self._progress_canvas.pack(pady=5)
        self._progress_bar = self._progress_canvas.create_rectangle(0, 0, 0, 20, fill="#2196F3", outline="")
        
        self.status_label = tk.Label(
            self, text="准备中...",
            font=("Microsoft YaHei UI", 9),
            fg="#888888"
        )
        self.status_label.pack()
        
        tk.Button(
            self, text="取消", width=10,
            command=self._on_cancel,
            font=("Microsoft YaHei UI", 9)
        ).pack(pady=(5, 0))
    
    def _format_size(self, size_bytes):
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
    
    def _start_download(self):
        # Choose download URL: small update for existing installs, full for new
        is_update = _is_installed() and self.version_info.get("update_url", "")
        if is_update:
            download_url = self.version_info["update_url"]
        else:
            download_url = self.version_info.get("download_url", "")
        if not download_url:
            self.on_error("下载地址无效")
            self.destroy()
            return
        
        expected_sha = self.version_info.get("sha256", "")
        self._is_incremental_update = is_update
        
        def progress_cb(downloaded, total):
            if self.cancelled:
                return
            if total > 0:
                pct = downloaded / total * 100
                self._progress_canvas.coords(self._progress_bar, 0, 0, int(350 * pct / 100), 20)
                self.status_label.config(
                    text=f"{self._format_size(downloaded)} / {self._format_size(total)}"
                )
            else:
                self.status_label.config(text=f"已下载 {self._format_size(downloaded)}")
        
        def download_thread():
            try:
                # 下载到临时目录
                temp_dir = tempfile.mkdtemp(prefix="liveclipper_update_")
                # 从 URL 提取文件名
                filename = download_url.split("/")[-1] or "LiveClipper_Setup.exe"
                temp_path = os.path.join(temp_dir, filename)
                
                download_file(download_url, temp_path, progress_cb)
                
                if self.cancelled:
                    return
                
                # SHA256 校验
                if expected_sha:
                    self.after(0, lambda: self.status_label.config(text="正在校验文件完整性..."))
                    actual_sha = compute_sha256(temp_path)
                    if actual_sha.lower() != expected_sha.lower():
                        self.after(0, lambda: self.on_error(
                            f"文件校验失败\n期望: {expected_sha[:16]}...\n实际: {actual_sha[:16]}..."
                        ))
                        self.after(0, self.destroy)
                        return
                
                # 下载成功
                self.after(0, lambda: self.on_complete(temp_path, filename, self._is_incremental_update))
                self.after(0, self.destroy)
                
            except Exception as e:
                if not self.cancelled:
                    self.after(0, lambda: self.on_error(f"下载失败: {str(e)}"))
                    self.after(0, self.destroy)
        
        threading.Thread(target=download_thread, daemon=True).start()
    
    def _on_cancel(self):
        self.cancelled = True
        self.destroy()


# ============ 主入口 ============

def check_and_prompt_update(parent_window):
    """
    在后台检查更新，如果有新版本则弹出提示
    parent_window: tkinter 根窗口
    """
    def _check():
        version_info = check_update()
        if version_info:
            # 在主线程弹出对话框
            parent_window.after(0, lambda: _show_dialog(version_info))
    
    threading.Thread(target=_check, daemon=True).start()


def _show_dialog(version_info):
    """在主线程中显示更新对话框"""
    try:
        root = tk._default_root
    except AttributeError:
        return
    
    if not root or not root.winfo_exists():
        return
    
    dlg = UpdateDialog(root, version_info)
    root.wait_window(dlg)
    
    if dlg.result == "update":
        # 显示下载对话框
        download_dlg = DownloadDialog(
            root, version_info,
            on_complete=_on_download_complete,
            on_error=_on_download_error
        )
        root.wait_window(download_dlg)


def _on_download_complete(filepath, filename, is_incremental=False):
    """下载完成，处理更新"""
    try:
        if is_incremental and filename.endswith('.zip'):
            # Incremental update: extract zip and restart
            success = _apply_update(filepath)
            # Clean up temp file
            try:
                os.remove(filepath)
                os.rmdir(os.path.dirname(filepath))
            except Exception:
                pass
            if success:
                # _apply_update handles restart via bat script
                pass
            else:
                messagebox.showerror(
                    "更新失败",
                    "解压更新包失败，请尝试重新下载。",
                    icon="error"
                )
        else:
            # Full update: prompt user to run installer
            result = messagebox.askyesno(
                "下载完成",
                f"更新包已下载完成。\n\n文件: {filename}\n\n点击「是」将关闭当前程序并运行安装包。\n也可以稍后手动运行。",
                icon="info"
            )
            if result:
                subprocess.Popen([filepath], shell=True)
                try:
                    root = tk._default_root
                    if root:
                        root.quit()
                except Exception:
                    pass
                sys.exit(0)
    except Exception:
        pass


def _is_installed():
    """Check if this is an existing installation (has FFmpeg)"""
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
        ffmpeg_path = os.path.join(base, '_internal', 'ffmpeg', 'ffmpeg.exe')
        return os.path.exists(ffmpeg_path)
    return False


def _get_install_base():
    """Get the installation base directory"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _apply_update(zip_path):
    """Extract update zip via bat script to handle locked exe"""
    import zipfile, tempfile
    base = _get_install_base()
    try:
        # Extract to a temp staging directory first
        staging = os.path.join(tempfile.gettempdir(), "liveclipper_update_staging")
        if os.path.exists(staging):
            import shutil
            shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging, exist_ok=True)
        
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(staging)
        
        # Read version from extracted version.json
        try:
            vj = os.path.join(staging, "_internal", "version.json")
            if os.path.exists(vj):
                import json as _json
                with open(vj, "r", encoding="utf-8") as f:
                    vdata = _json.load(f)
                new_ver = vdata.get("latest_version", "")
                if new_ver:
                    _set_installed_version(new_ver)
        except Exception:
            pass
        
        # Create a bat script that: waits for app exit -> copies files -> restarts
        if getattr(sys, 'frozen', False):
            exe_name = os.path.basename(sys.executable)
        else:
            exe_name = None
        
        bat_path = os.path.join(tempfile.gettempdir(), "liveclipper_update.bat")
        bat_lines = [
            "@echo off",
            "chcp 65001 >nul 2>&1",
            "echo Updating LiveClipper...",
        ]
        if exe_name:
            # Wait for the exe process to exit
            bat_lines.append('taskkill /IM "' + exe_name + '" /F >nul 2>&1')
            bat_lines.append("timeout /t 2 /nobreak >nul")
        
        # Copy all files from staging to install dir
            bat_lines.append('xcopy "' + staging + '\\*" "' + base + '\\" /E /Y /Q >nul 2>&1')
        
        # Cleanup staging
            bat_lines.append('rmdir /S /Q "' + staging + '" >nul 2>&1')
        
        # Restart app
        if exe_name:
            bat_lines.append('start "" "' + os.path.join(base, exe_name) + '"')
        
        # Self-delete the bat
        bat_lines.append('del "%~f0"')
        
        with open(bat_path, 'w', encoding='utf-8') as bf:
            bf.write("\n".join(bat_lines))
        
        # Launch the bat and exit current app
        import subprocess
        subprocess.Popen([bat_path], shell=True, creationflags=0x08000000)  # CREATE_NO_WINDOW
        
        # Exit current app
        try:
            root = tk._default_root
            if root:
                root.quit()
        except Exception:
            pass
        sys.exit(0)
        
    except Exception as e:
        return False


def _restart_app():
    """Restart the application"""
    try:
        if getattr(sys, 'frozen', False):
            exe = sys.executable
        else:
            exe = sys.argv[0]
        subprocess.Popen([exe], shell=True)
    except Exception:
        pass
    try:
        root = tk._default_root
        if root:
            root.quit()
    except Exception:
        pass
    sys.exit(0)


def _on_download_error(msg):
    """下载失败"""
    messagebox.showerror("更新失败", msg, icon="error")


# ============ 独立运行测试 ============

if __name__ == "__main__":
    # 测试模式：直接运行检查更新
    print(f"当前版本: {CURRENT_VERSION}")
    print(f"检查更新: {get_version_url() or '(未配置)'}")
    
    info = check_update()
    if info:
        print(f"发现新版本: v{info.get('latest_version')}")
        print(f"更新说明: {info.get('release_notes', '无')}")
        print(f"强制更新: {info.get('force_update', False)}")
    else:
        print("已是最新版本，或检查更新失败。")
