"""
产品扫描窗口 v1.0
独立 Toplevel 窗口，不依赖 gui.py 主界面布局
"""

import os
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from product_scanner import ProductScanner
from ai_model_config import DEEPSEEK_DEFAULT_MODEL, normalize_ai_base_url


def _get_ai_settings():
    """从 ai_settings.json 读取 AI 配置"""
    try:
        from ai_clipper import load_settings
        data = load_settings()
    except Exception:
        settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_settings.json")
        if not os.path.exists(settings_path):
            return None, None, None
        try:
            import json
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None, None, None
    try:
        api_key = data.get("api_key", "")
        base_url = normalize_ai_base_url(data.get("base_url"))
        model = data.get("model") or DEEPSEEK_DEFAULT_MODEL
        return api_key, base_url, model
    except Exception:
        return None, None, None


class ProductScanWindow:
    """产品扫描窗口"""

    def __init__(self, parent):
        self.win = tk.Toplevel(parent)
        self.win.title("AI 扫描")
        self.win.geometry("750x520")
        self.win.resizable(True, True)

        self._scanning = False
        self._products = []
        self._video_path = tk.StringVar()
        self._srt_path = tk.StringVar()
        self._status = tk.StringVar(value="就绪")
        self._output_dir = tk.StringVar(
            value=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "product_clips")
        )

        # 读取 AI 配置
        self._api_key, self._base_url, self._model = _get_ai_settings()
        self._scanner = ProductScanner(
            api_key=self._api_key,
            base_url=self._base_url,
            model=self._model,
        )

        self._build_ui()
        self.win.transient(parent)
        self.win.grab_set()

    def _build_ui(self):
        """构建界面"""
        # --- 视频/SRT 选择 ---
        f0 = ttk.LabelFrame(self.win, text="输入文件", padding=8)
        f0.pack(fill="x", padx=10, pady=(10, 5))

        row1 = ttk.Frame(f0)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="视频文件:", width=10).pack(side="left")
        ttk.Entry(row1, textvariable=self._video_path).pack(side="left", fill="x", expand=True, padx=5)
        ttk.Button(row1, text="浏览...", command=self._browse_video, width=8).pack(side="right")

        row2 = ttk.Frame(f0)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="SRT 字幕:", width=10).pack(side="left")
        ttk.Entry(row2, textvariable=self._srt_path).pack(side="left", fill="x", expand=True, padx=5)
        ttk.Button(row2, text="浏览...", command=self._browse_srt, width=8).pack(side="right")

        # --- 输出目录 ---
        row3 = ttk.Frame(f0)
        row3.pack(fill="x", pady=2)
        ttk.Label(row3, text="输出目录:", width=10).pack(side="left")
        ttk.Entry(row3, textvariable=self._output_dir).pack(side="left", fill="x", expand=True, padx=5)
        ttk.Button(row3, text="浏览...", command=self._browse_output, width=8).pack(side="right")

        # --- 操作按钮 ---
        f1 = ttk.Frame(self.win)
        f1.pack(fill="x", padx=10, pady=5)
        self._btn_scan = ttk.Button(f1, text="开始扫描", command=self._start_scan, width=12)
        self._btn_scan.pack(side="left", padx=5)
        self._btn_extract = ttk.Button(f1, text="切割选中单品", command=self._extract_selected, width=14, state="disabled")
        self._btn_extract.pack(side="left", padx=5)
        ttk.Label(f1, textvariable=self._status).pack(side="right", padx=5)

        # --- 进度条 ---
        self._progress = ttk.Progressbar(self.win, mode="indeterminate", length=400)
        self._progress.pack(fill="x", padx=10, pady=2)

        # --- 结果列表 ---
        f2 = ttk.LabelFrame(self.win, text="单品列表", padding=4)
        f2.pack(fill="both", expand=True, padx=10, pady=5)

        columns = ("select", "name", "start", "end", "duration", "confidence")
        self._tree = ttk.Treeview(f2, columns=columns, show="headings", height=12)
        self._tree.heading("select", text="")
        self._tree.heading("name", text="商品名称")
        self._tree.heading("start", text="开始(秒)")
        self._tree.heading("end", text="结束(秒)")
        self._tree.heading("duration", text="时长(秒)")
        self._tree.heading("confidence", text="置信度")
        self._tree.column("select", width=30, anchor="center")
        self._tree.column("name", width=200)
        self._tree.column("start", width=80, anchor="center")
        self._tree.column("end", width=80, anchor="center")
        self._tree.column("duration", width=80, anchor="center")
        self._tree.column("confidence", width=80, anchor="center")

        vsb = ttk.Scrollbar(f2, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self._tree.bind("<ButtonRelease-1>", self._on_tree_click)

    def _browse_video(self):
        path = filedialog.askopenfilename(
            title="选择视频文件（支持 MP4/TS/FLV）",
            filetypes=[("视频文件", "*.mp4 *.ts *.flv"), ("所有文件", "*.*")],
        )
        if path:
            self._video_path.set(path)
            # 自动匹配 SRT
            base, _ = os.path.splitext(path)
            srt_candidates = [
                base + ".srt",
                base.replace("(1)", "") + ".srt",
                base.replace("-1", "") + ".srt",
            ]
            for s in srt_candidates:
                if os.path.exists(s):
                    self._srt_path.set(s)
                    break
            else:
                # 检查同目录下的 .srt
                srt_dir = os.path.dirname(path)
                srts = [f for f in os.listdir(srt_dir) if f.endswith(".srt")]
                if srts:
                    self._srt_path.set(os.path.join(srt_dir, srts[0]))

    def _browse_srt(self):
        path = filedialog.askopenfilename(
            title="选择 SRT 字幕文件",
            filetypes=[("SRT 字幕", "*.srt"), ("所有文件", "*.*")],
        )
        if path:
            self._srt_path.set(path)

    def _browse_output(self):
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self._output_dir.set(path)

    def _start_scan(self):
        srt_path = self._srt_path.get().strip()
        if not srt_path or not os.path.exists(srt_path):
            messagebox.showwarning("提示", "请选择有效的 SRT 文件")
            return

        if not self._scanner.api_key:
            # 尝试从 cutter_logic 获取 api key
            api_key, base_url, model = _get_ai_settings()
            if not api_key:
                messagebox.showwarning("提示", "未检测到 AI 配置，请在主界面设置 API Key")
                return
            self._scanner = ProductScanner(api_key=api_key, base_url=base_url, model=model)

        self._scanning = True
        self._btn_scan.configure(text="扫描中...", state="disabled")
        self._progress.start(15)

        def _do_scan():
            try:
                products = self._scanner.scan(srt_path)
                self._scanning = False
                self.win.after(0, lambda: self._on_scan_done(products))
            except Exception as e:
                self._scanning = False
                self.win.after(0, lambda: self._on_scan_error(str(e)))

        threading.Thread(target=_do_scan, daemon=True).start()

    def _on_scan_done(self, products):
        self._progress.stop()
        self._btn_scan.configure(text="开始扫描", state="normal")
        self._products = products

        # 清空旧数据
        for item in self._tree.get_children():
            self._tree.delete(item)

        if not products:
            self._status.set("未识别到单品（AI 可能未返回有效结果）")
            return

        for i, p in enumerate(products):
            dur = round(p["end"] - p["start"], 1)
            conf = p.get("confidence", "low")
            self._tree.insert("", "end", iid=str(i), values=("☐", p["name"], p["start"], p["end"], dur, conf))

        self._status.set(f"识别到 {len(products)} 个单品（点击勾选后切割）")
        self._btn_extract.configure(state="normal")

    def _on_scan_error(self, err):
        self._progress.stop()
        self._btn_scan.configure(text="开始扫描", state="normal")
        self._status.set(f"扫描失败: {err}")
        messagebox.showerror("扫描失败", err)

    def _on_tree_click(self, event):
        """点击切换选中状态"""
        region = self._tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        item = self._tree.identify_row(event.y)
        if not item:
            return
        col = self._tree.identify_column(event.x)
        if col != "#1":  # 只有第一列是勾选
            return

        values = list(self._tree.item(item, "values"))
        if values[0] == "☐":
            values[0] = "☑"
        else:
            values[0] = "☐"
        self._tree.item(item, values=values)

    def _get_selected_indices(self):
        """获取勾选的条目索引"""
        selected = []
        for item in self._tree.get_children():
            values = self._tree.item(item, "values")
            if values and values[0] == "☑":
                selected.append(int(item))
        return selected

    def _extract_selected(self):
        selected = self._get_selected_indices()
        if not selected:
            messagebox.showinfo("提示", "请先勾选要切割的单品")
            return

        video_path = self._video_path.get().strip()
        if not video_path or not os.path.exists(video_path):
            messagebox.showwarning("提示", "请选择有效的视频文件")
            return

        output_dir = self._output_dir.get().strip() or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "product_clips"
        )

        products_to_cut = [self._products[i] for i in selected if i < len(self._products)]

        self._status.set("正在切割...")
        self._btn_extract.configure(state="disabled")
        self._progress.start(15)

        def _do_extract():
            try:
                results = self._scanner.extract_all(video_path, products_to_cut, output_dir)
                self.win.after(0, lambda: self._on_extract_done(results))
            except Exception as e:
                self.win.after(0, lambda: self._on_extract_error(str(e)))

        threading.Thread(target=_do_extract, daemon=True).start()

    def _on_extract_done(self, results):
        self._progress.stop()
        self._btn_extract.configure(state="normal")
        success = sum(1 for r in results if r.get("output_path"))
        fail = sum(1 for r in results if not r.get("output_path"))
        msg = f"切割完成: {success} 个成功"
        if fail:
            msg += f", {fail} 个失败"
        self._status.set(msg)
        messagebox.showinfo("完成", msg)

    def _on_extract_error(self, err):
        self._progress.stop()
        self._btn_extract.configure(state="normal")
        self._status.set("切割失败")
        messagebox.showerror("切割失败", err)

    def show(self):
        self.win.deiconify()
        self.win.lift()


def open_scan_window(parent):
    """外部调用入口：在 gui.py 的按钮回调中使用"""
    ProductScanWindow(parent)
