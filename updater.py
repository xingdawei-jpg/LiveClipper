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
import urllib.request
import urllib.error
import ssl
import tkinter as tk
from tkinter import messagebox
from pathlib import Path


# ============ 配置（发布时修改） ============

# GitHub 仓库（私有仓库需在 version.json 里放完整 URL）
GITHUB_REPO = "xingdawei-jpg/LiveClipper"  # 格式: owner/repo，后续填入

# version.json 的远程地址（优先使用这个）
# 如果设置了这个，会忽略 GITHUB_REPO
VERSION_URL = ""

# 国内加速镜像列表（优先使用，失败自动回退）
MIRROR_PREFIXES = [
    "https://ghfast.top/",
    "https://mirror.ghproxy.com/",
    "https://gh-proxy.com/",
    "https://ghps.cc/",
]

# 当前版本号（每次发布时更新）
CURRENT_VERSION = "1.0.0"

# 检查更新的 API 地址（GitHub Releases）
def get_version_url():
    """获取 version.json 的实际地址"""
    if VERSION_URL:
        return VERSION_URL
    if GITHUB_REPO and "/" in GITHUB_REPO:
        return f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/version.json"
    return ""


def get_mirrored_url(raw_github_url):
    """将 GitHub raw URL 转为镜像列表（镜像优先，原始地址兜底）"""
    if not raw_github_url or "raw.githubusercontent.com" not in raw_github_url:
        return [raw_github_url]
    path = raw_github_url.replace("https://raw.githubusercontent.com/", "")
    urls = [prefix + path for prefix in MIRROR_PREFIXES]
    urls.append(raw_github_url)  # 原始地址兜底
    return urls


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
    下载文件，支持进度回调
    progress_callback(downloaded_bytes, total_bytes) 
    """
    # 创建不受证书验证影响的 context（某些环境需要）
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, headers={
        "User-Agent": "LiveClipper-Updater/1.0"
    })
    
    with urllib.request.urlopen(req, context=ctx, timeout=30) as response:
        total_size = int(response.headers.get("Content-Length", 0))
        downloaded = 0
        
        with open(dest_path, "wb") as f:
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if progress_callback:
                    progress_callback(downloaded, total_size)


# ============ 检查更新 ============

def check_update():
    """
    检查是否有新版本
    返回 dict（包含版本信息）或 None（无更新/出错）
    """
    # 生成带镜像的 URL 列表
    base_url = get_version_url()
    urls = get_mirrored_url(base_url)
    
    for try_url in urls:
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            
            req = urllib.request.Request(try_url, headers={
                "User-Agent": "LiveClipper-Updater/1.0"
            })
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                # 镜像可能返回 HTML 错误页，验证是 JSON
                if not raw.strip().startswith("{"):
                    continue
                data = json.loads(raw)
            
            remote_ver = data.get("latest_version", "")
            if not remote_ver or not is_newer(remote_ver, CURRENT_VERSION):
                return None
            
            # 将 download_url 也走镜像加速
            dl_url = data.get("download_url", "")
            if dl_url and "raw.githubusercontent.com" in dl_url:
                data["download_url"] = get_mirrored_url(dl_url)[0]
            
            return data
        
        except Exception:
            continue
    
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
        
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = tk.Progressbar(
            self, variable=self.progress_var,
            maximum=100, length=350
        )
        self.progress_bar.pack(pady=5)
        
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
        download_url = self.version_info.get("download_url", "")
        if not download_url:
            self.on_error("下载地址无效")
            self.destroy()
            return
        
        expected_sha = self.version_info.get("sha256", "")
        
        def progress_cb(downloaded, total):
            if self.cancelled:
                return
            if total > 0:
                pct = downloaded / total * 100
                self.progress_var.set(pct)
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
                self.after(0, lambda: self.on_complete(temp_path, filename))
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


def _on_download_complete(filepath, filename):
    """下载完成，提示用户运行安装包"""
    try:
        result = messagebox.askyesno(
            "下载完成",
            f"更新包已下载完成。\n\n文件: {filename}\n\n点击「是」将关闭当前程序并运行安装包。\n也可以稍后手动运行。",
            icon="info"
        )
        if result:
            # 启动安装包
            subprocess.Popen([filepath], shell=True)
            # 关闭当前程序
            try:
                root = tk._default_root
                if root:
                    root.quit()
            except Exception:
                pass
            sys.exit(0)
    except Exception:
        pass


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
