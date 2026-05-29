"""
单品扫描页面 — 读取 Excel 时间表，按时间戳切割视频合并同品
"""
import os
import tkinter as tk
from tkinter import filedialog, messagebox
import threading

# 颜色常量（与主界面保持一致）
from config import C, FNT_S, FNT_B


class SchedulePage(tk.Frame):
    """单品扫描页面"""

    def __init__(self, parent, app=None):
        super().__init__(parent, bg=C["bg"])
        self.app = app
        self._video_list = []
        self._advance_secs = tk.IntVar(value=0)
        self._output_dir = ""
        self._live_start = None
        self._schedule = []
        self._groups = []
        self._processing = False
        self._build_ui()

    def _build_ui(self):
        bar = tk.Frame(self, bg=C["card"], padx=12, pady=10,
                       highlightbackground=C["card_border"], highlightthickness=1)
        bar.pack(fill="x", padx=16, pady=(2, 6))
        tk.Label(bar, text="单品扫描", font=FNT_B, fg=C["text"], bg=C["card"]).pack(side="left")

        # 选择 Excel
        ef = tk.Frame(self, bg=C["bg"])
        ef.pack(fill="x", padx=16, pady=(8, 2))
        tk.Label(ef, text="📊 飞书导出的 Excel:", font=FNT_S, fg=C["text"], bg=C["bg"]).pack(side="left")
        self._excel_label = tk.Label(ef, text="（未选择）", font=FNT_S, fg=C["dim"], bg=C["bg"])
        self._excel_label.pack(side="left", fill="x", padx=(8, 4))
        tk.Button(ef, text="浏览", font=FNT_S, fg="white", bg=C["btn_sel"],
                  relief="flat", cursor="hand2", padx=10, command=self._browse_excel).pack(side="right")

        # 多视频列表
        vf = tk.Frame(self, bg=C["bg"])
        vf.pack(fill="x", padx=16, pady=(4, 2))
        tk.Label(vf, text="🎬 直播视频(可多选):", font=FNT_S, fg=C["text"], bg=C["bg"]).pack(side="left")
        tk.Button(vf, text="+ 添加", font=FNT_S, fg="white", bg=C["btn_sel"],
                  relief="flat", cursor="hand2", padx=10, command=self._add_videos).pack(side="right")
        tk.Button(vf, text="清空", font=FNT_S, fg="white", bg=C["btn_del"],
                  relief="flat", cursor="hand2", padx=8, command=self._clear_videos).pack(side="right", padx=(0, 4))

        self._video_listbox = tk.Listbox(self, font=FNT_S, bg=C["inp"], fg=C["text"],
                                          selectbackground=C["btn_sel"], height=3, relief="flat", bd=0)
        self._video_listbox.pack(fill="x", padx=16)

        # 输出目录
        of = tk.Frame(self, bg=C["bg"])
        of.pack(fill="x", padx=16, pady=(4, 2))
        tk.Label(of, text="📁 导出到:", font=FNT_S, fg=C["text"], bg=C["bg"]).pack(side="left")
        self._out_label = tk.Label(of, text="（点击浏览选择）", font=FNT_S, fg=C["dim"], bg=C["bg"])
        self._out_label.pack(side="left", fill="x", padx=(8, 4))
        tk.Button(of, text="浏览", font=FNT_S, fg="white", bg=C["btn_sel"],
                  relief="flat", cursor="hand2", padx=10, command=self._browse_output).pack(side="right")
        tk.Button(of, text="打开", font=FNT_S, fg=C["text"], bg=C["inp"],
                  relief="flat", cursor="hand2", padx=8, command=self._open_output).pack(side="right", padx=(4,0))

        # 商品预览列表
        list_frame = tk.Frame(self, bg=C["card"], padx=8, pady=6,
                              highlightbackground=C["card_border"], highlightthickness=1)
        list_frame.pack(fill="both", expand=True, padx=16, pady=(6, 4))
        tk.Label(list_frame, text="预览（读取 Excel 后显示）", font=FNT_S,
                 fg=C["dim"], bg=C["card"]).pack(anchor="w")
        self._listbox = tk.Listbox(list_frame, font=("Consolas", 9),
                                    bg=C["inp"], fg=C["text"],
                                    relief="flat", bd=0, height=6)
        self._listbox.pack(fill="both", expand=True, pady=(2, 0))

        # 操作按钮
# 提前秒数设置 (字段整体前移)
        pre_frame = tk.Frame(self, bg=C["bg"])
        pre_frame.pack(fill="x", padx=16, pady=(2, 2))
        tk.Label(pre_frame, text="▲ 提前(s):", font=FNT_S, fg=C["dim"], bg=C["bg"]).pack(side="left")
        tk.Spinbox(pre_frame, from_=0, to=600, increment=10, width=6,
                   textvariable=self._advance_secs, font=FNT_S,
                   bg=C["inp"], fg=C["text"], relief="flat", bd=0,
                   buttonbackground=C["card"]).pack(side="left", padx=(6,0))
        tk.Label(pre_frame, text=". 片段整体前移，覆盖上架前的讲解", font=FNT_S, fg=C["dim"], bg=C["bg"]).pack(side="left", padx=(8,0))

        btn_frame = tk.Frame(self, bg=C["bg"])
        btn_frame.pack(fill="x", padx=16, pady=(4, 6))
        self._read_btn = tk.Button(btn_frame, text="📖 读取时间表", font=FNT_B,
                                    fg="white", bg="#e67e22", relief="flat",
                                    cursor="hand2", padx=14, pady=6,
                                    command=self._read_excel)
        self._read_btn.pack(side="left")
        self._start_btn = tk.Button(btn_frame, text="✂️ 开始分割", font=FNT_B,
                                     fg="white", bg=C["btn_go"], relief="flat",
                                     cursor="hand2", padx=14, pady=6,
                                     command=self._start_split, state="disabled")
        self._start_btn.pack(side="left", padx=(8, 0))

        # 日志
        log_frame = tk.Frame(self, bg=C["card"])
        log_frame.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        tk.Label(log_frame, text="运行日志", font=FNT_S, fg=C["dim"], bg=C["card"]).pack(anchor="w")
        self._log_text = tk.Text(log_frame, font=("Consolas", 9), bg=C["inp"], fg=C["text"],
                                  relief="flat", bd=0, height=8, wrap="word")
        self._log_text.pack(fill="both", expand=True, pady=(2, 0))

    def _log(self, msg, tag="info"):
        """线程安全的日志（用 after 调度到主线程）"""
        def _do_log():
            self._log_text.insert("end", msg + "\n")
            self._log_text.see("end")
        try:
            self.after(0, _do_log)
        except:
            pass

    def _browse_excel(self):
        path = filedialog.askopenfilename(title="选择飞书导出的 Excel",
                                           filetypes=[("Excel文件", "*.xlsx *.xls"), ("All", "*.*")])
        if path:
            self._excel_path = path
            self._excel_label.configure(text=os.path.basename(path))

    def _add_videos(self):
        paths = filedialog.askopenfilenames(title="选择直播视频文件",
                                            filetypes=[("视频文件", "*.mp4 *.avi *.mov *.mkv *.ts *.flv"), ("All", "*.*")])
        if paths:
            for p in paths:
                if p not in self._video_list:
                    self._video_list.append(p)
            self._sort_and_refresh_videos()

    def _sort_and_refresh_videos(self):
        try:
            from schedule_splitter import sort_videos_by_start
            self._video_list = sort_videos_by_start(self._video_list)
        except Exception:
            pass
        self._video_listbox.delete(0, "end")
        for p in self._video_list:
            self._video_listbox.insert("end", os.path.basename(p))

    def _clear_videos(self):
        self._video_list = []
        self._video_listbox.delete(0, "end")

    def _browse_output(self):
        path = filedialog.askdirectory(title="选择导出目录")
        if path:
            self._output_dir = path
            self._out_label.configure(text=path)

    def _open_output(self):
        if self._output_dir and os.path.exists(self._output_dir):
            os.startfile(self._output_dir)
        else:
            self._log("输出目录不存在")

    def _read_excel(self):
        if not self._excel_path or not os.path.exists(self._excel_path):
            self._log("请先选择 Excel 文件")
            return
        from schedule_splitter import read_excel
        self._schedule, self._live_start = read_excel(self._excel_path, log_fn=lambda m: self._log(m))
        if not self._schedule:
            self._log("未解析出任何商品，请检查 Excel 格式")
            return
        from schedule_splitter import group_by_product
        self._groups = group_by_product(self._schedule)
        self._listbox.delete(0, "end")
        for g in self._groups:
            mins = g["total_duration"] / 60
            segs = len(g["segments"])
            self._listbox.insert("end", f"{g['name'][:50]:50s} {segs}段  {mins:.0f}分")
        self._log(f"读取完成: {len(self._schedule)} 条记录, {len(self._groups)} 个商品")
        self._start_btn.configure(state="normal")

    def _start_split(self):
        if not self._groups:
            self._log("请先读取时间表")
            return
        if not self._video_list:
            self._log("请先添加视频文件")
            return
        if not self._output_dir:
            self._log("请先选择导出目录")
            return
        self._sort_and_refresh_videos()
        try:
            from license_guard import require_feature_access
            if not require_feature_access("单品扫描", self.winfo_toplevel(), self._log, refresh=False):
                return
        except Exception as e:
            self._log("授权检查异常: " + str(e))
            return
        self._start_btn.configure(state="disabled", text="分割中...")

        def worker():
            ffmpeg = "ffmpeg"
            try:
                from platform_config import FFMPEG_CMD as fc
                if os.path.exists(fc):
                    ffmpeg = fc
            except:
                pass
            try:
                from schedule_splitter import read_excel
                fresh_schedule, fresh_live_start = read_excel(self._excel_path)
                if fresh_schedule:
                    self._schedule = fresh_schedule
                    self._live_start = fresh_live_start
            except Exception as e:
                self._log("重新读取时间表失败: " + str(e))
            from schedule_splitter import align_schedule_to_video
            align_schedule_to_video(self._schedule, self._video_list, self._live_start, log_fn=lambda m: self._log(m), ffmpeg_cmd=ffmpeg)
            from schedule_splitter import group_by_product
            self._groups = group_by_product(self._schedule)
            adv = self._advance_secs.get()
            if adv > 0:
                for g in self._groups:
                    segs = [(max(0, s - adv), max(0, e - adv)) for s, e in g["segments"]]
                    # drop < 60s segments
                    segs = [(s, e) for s, e in segs if e - s >= 60]
                    g["segments"] = segs
                    g["total_duration"] = sum(ee - ss for ss, ee in segs)
            # Skip products with no valid segments left
            self._groups = [g for g in self._groups if g.get("segments")]
            from schedule_splitter import extract_by_schedule
            self._log(f"开始分割 {len(self._groups)} 个商品 ({len(self._video_list)} 个视频文件)...")
            try:
                results = extract_by_schedule(
                    self._groups, self._video_list, self._output_dir,
                    ffmpeg=ffmpeg, log_fn=lambda m: self._log(m))
                if isinstance(results, tuple):
                    results = results[0]
                ok = len([r for r in results if r.get("output_path")])
                self._log(f"完成: {ok}/{len(self._groups)} 个商品导出成功")
                if ok:
                    try:
                        from license_guard import consume_trial_after_success
                        consume_trial_after_success("单品扫描", units=ok, root=None, log_fn=self._log)
                    except Exception:
                        pass
            except Exception as e:
                self._log(f"分割异常: {e}")
            finally:
                self._start_btn.configure(state="normal", text="开始分割")

        threading.Thread(target=worker, daemon=True).start()
