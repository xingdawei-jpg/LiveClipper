#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 扫描独立页面——支持多视频排队处理，使用云端ASR/Whisper，不包含去重/裁切/画中画"""

import os
import tkinter as tk
from tkinter import ttk, filedialog
import threading

# 样式常量（与 gui.py 保持一致）
C = {
    "bg": "#1a1a2e",
    "card": "#16213e",
    "card_border": "#1f3460",
    "inp": "#0f3460",
    "dim": "#8892b0",
    "text": "#e6f1ff",
    "btn_sel": "#4fc3f7",
    "btn_del": "#e74c3c",
    "btn_go": "#00c853",
    "btn_go2": "#00e676",
}
FNT_B = ("Microsoft YaHei", 12, "bold")
FNT_S = ("Microsoft YaHei", 10)
FNT_T = ("Microsoft YaHei", 14, "bold")


class ProductScanPage(tk.Frame):
    """AI 扫描页面"""

    def __init__(self, parent, app=None):
        super().__init__(parent, bg=C["bg"])
        self.app = app
        self._video_list = []
        self._processing = False
        self._build_ui()

    def _build_ui(self):
        # 文件选择栏
        bar = tk.Frame(self, bg=C["card"], padx=12, pady=10,
                       highlightbackground=C["card_border"], highlightthickness=1)
        bar.pack(fill="x", padx=16, pady=(2,6))

        tk.Label(bar, text="AI 扫描", font=FNT_B, fg=C["text"], bg=C["card"]).pack(side="left")
        tk.Button(bar, text="+ 添加视频", font=FNT_S, fg="white", bg=C["btn_sel"],
                  relief="flat", cursor="hand2", padx=10,
                  command=self._add_videos).pack(side="right")
        tk.Button(bar, text="删除", font=FNT_S, fg="white", bg=C["btn_del"],
                  relief="flat", cursor="hand2", padx=8,
                  command=self._del_selected).pack(side="right", padx=(0,4))
        tk.Button(bar, text="清空", font=FNT_S, fg="white", bg=C["btn_del"],
                  relief="flat", cursor="hand2", padx=8,
                  command=self._clear_all).pack(side="right", padx=(0,4))

        # 视频列表
        self._video_listbox = tk.Listbox(self, font=FNT_S, bg=C["inp"], fg=C["text"],
                                          selectbackground=C["btn_sel"], height=4,
                                          relief="flat", bd=0)
        self._video_listbox.pack(fill="x", padx=16)
        self._video_listbox.bind("<Delete>", lambda e: self._del_selected())

        # 结果列表（支持多选）
        self._results_tree = ttk.Treeview(self, columns=("name","time","video","text"),
                                           show="headings", height=8,
                                           selectmode="extended")
        self._results_tree.heading("name", text="单品")
        self._results_tree.heading("time", text="时间范围")
        self._results_tree.heading("video", text="来源视频")
        self._results_tree.heading("text", text="内容")
        self._results_tree.column("name", width=120)
        self._results_tree.column("time", width=130)
        self._results_tree.column("video", width=200)
        self._results_tree.column("text", width=300)
        self._results_tree.pack(fill="both", expand=True, padx=16, pady=(4,4))
        self._results_tree.bind("<<TreeviewSelect>>", lambda e: self._update_sel_label())

        # 选中信息 + 操作栏
        sel_row = tk.Frame(self, bg=C["bg"])
        sel_row.pack(fill="x", padx=16, pady=(0,4))
        self._sel_label = tk.Label(sel_row, text="已选 0 个", font=FNT_S,
                                   fg=C["dim"], bg=C["bg"])
        self._sel_label.pack(side="left")
        tk.Button(sel_row, text="全选", font=FNT_S, fg="white", bg=C["btn_sel"],
                  relief="flat", cursor="hand2", padx=8,
                  command=self._select_all).pack(side="left", padx=(8,2))
        tk.Button(sel_row, text="取消全选", font=FNT_S, fg=C["dim"], bg=C["inp"],
                  relief="flat", cursor="hand2", padx=8,
                  command=self._deselect_all).pack(side="left")

        # 自动导出开关
        self._auto_export_var = tk.BooleanVar(value=False)
        tk.Checkbutton(sel_row, text="扫描后自动导出到输出目录",
                       variable=self._auto_export_var,
                       font=FNT_S, fg=C["dim"], bg=C["bg"],
                       selectcolor=C["inp"], activebackground=C["bg"],
                       cursor="hand2").pack(side="right")

        # 输出目录
        out_row = tk.Frame(self, bg=C["bg"])
        out_row.pack(fill="x", padx=16, pady=(0,4))
        tk.Label(out_row, text="📁 导出到:", font=FNT_S, fg=C["text"],
                 bg=C["bg"]).pack(side="left")
        self._scan_output_var = tk.StringVar(value="（点击浏览选择）")
        tk.Label(out_row, textvariable=self._scan_output_var, font=FNT_S, fg=C["dim"],
                 bg=C["bg"]).pack(side="left", fill="x", padx=(8,8))
        tk.Button(out_row, text="打开", font=FNT_S, fg="white", bg=C["btn_sel"],
                  relief="flat", cursor="hand2", padx=8,
                  command=lambda: os.startfile(getattr(self, '_scan_output_dir', os.path.dirname(self._video_list[0]) if self._video_list else os.getcwd()))).pack(side="right")
        tk.Button(out_row, text="浏览选择", font=FNT_S, fg="white", bg=C["btn_sel"],
                  relief="flat", cursor="hand2", padx=8,
                  command=self._browse_scan_output).pack(side="right", padx=(0,4))

        # 控制按钮
        ctl = tk.Frame(self, bg=C["bg"])
        ctl.pack(fill="x", padx=16, pady=(0,10))
        self._start_btn = tk.Button(ctl, text="▶ 开始扫描", font=FNT_B,
                                    fg="white", bg=C["btn_go"], activebackground=C["btn_go2"],
                                    relief="flat", cursor="hand2", padx=16, pady=6,
                                    command=self._start_scan)
        self._start_btn.pack(side="left")

        # 跨文件合并按钮
        self._merge_btn = tk.Button(ctl, text="🔗 跨文件合并", font=FNT_S,
                                    fg="white", bg="#e67e22", relief="flat",
                                    cursor="hand2", padx=10, pady=4,
                                    command=self._merge_across_files, state="disabled")
        self._merge_btn.pack(side="left", padx=(8,0))

        # 导出合并结果按钮
        self._export_merge_btn = tk.Button(ctl, text="📥 导出合并结果", font=FNT_S,
                                           fg="white", bg="#1976d2", relief="flat",
                                           cursor="hand2", padx=10, pady=4,
                                           command=self._export_merged, state="disabled")
        self._export_merge_btn.pack(side="left", padx=(8,0))

        # 日志
        log_frame = tk.Frame(self, bg=C["card"])
        log_frame.pack(fill="both", expand=True, padx=16, pady=(0,10))
        log_hdr = tk.Frame(log_frame, bg=C["card"])
        log_hdr.pack(fill="x")
        tk.Label(log_hdr, text="运行日志", font=FNT_S, fg=C["dim"],
                 bg=C["card"]).pack(side="left")
        tk.Label(log_hdr, text="🗑️", font=FNT_S, fg=C["dim"],
                 bg=C["card"], cursor="hand2").pack(side="right")
        self._log_text = tk.Text(log_frame, font=("Consolas", 9), bg=C["inp"], fg=C["text"],
                                 relief="flat", bd=0, height=8, wrap="word")
        self._log_text.pack(fill="both", expand=True, pady=(2,0))

        # 导出按钮
        self._export_btn = tk.Button(self, text="📥 导出选中单品", font=FNT_B,
                                     fg="white", bg="#1976d2", relief="flat",
                                     cursor="hand2", padx=12, pady=4,
                                     command=self._export_selected)
        # _export_btn 在扫描结果出来后由 _display_results 显示

    def _export_selected(self):
        """导出选中的单品为视频片段"""
        sel = self._results_tree.selection()
        if not sel:
            self._log("请先在结果列表中选择要导出的单品", "warn")
            return

        # 使用用户设置的输出目录，未设置则弹窗选择
        out_dir = getattr(self, '_scan_output_dir', None)
        if not out_dir:
            out_dir = filedialog.askdirectory(title="选择输出目录")
            if not out_dir:
                return
            self._scan_output_dir = out_dir

        from product_scanner import ProductScanner
        from ai_clipper import load_settings
        settings = load_settings()
        scanner = ProductScanner(
            api_key=settings.get("api_key", ""),
            base_url=settings.get("base_url", "https://api.deepseek.com"),
            model=settings.get("model", "deepseek-chat"),
        )

        self._export_btn.configure(state="disabled", text="导出中...")
        exported = 0

        def _worker():
            nonlocal exported
            for item_id in sel:
                values = self._results_tree.item(item_id, "values")
                prod_name = values[0]
                # 找到对应 product 数据
                try:
                    for p in self._products:
                        if p["name"] == prod_name and p.get("_video"):
                            vpath = p["_video"]
                            self._log(f"导出: {prod_name}", "info")
                            path = scanner.extract_clip(vpath, p, out_dir, prod_name)
                            if path:
                                exported += 1
                                self._log(f"  ✓ {os.path.basename(path)}", "ok")
                            else:
                                self._log(f"  ✗ {prod_name} 导出失败", "err")
                            break
                except Exception as e:
                    self._log(f"  ✗ {prod_name}: {e}", "err")

            self._export_btn.configure(state="normal", text="📥 导出选中单品")
            if exported:
                self._log(f"导出完成: {exported}/{len(sel)} 个", "ok")

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def _log(self, msg, tag=None):
        """页面内日志"""
        tag_map = {"err": "#ff5252", "warn": "#ffa726", "ok": "#69f0ae", "info": C["dim"]}
        color = tag_map.get(tag, C["dim"])
        self._log_text.configure(state="normal")
        self._log_text.insert("end", msg + "\n")
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _try_cloud_asr(self, video_path, provider=None):
        """尝试云端ASR（provider=volc/aliyun/None都试），返回SRT路径或None"""
        import json, os, tempfile, hashlib, subprocess
        from ai_clipper import load_settings
        cfg = load_settings()
        if not cfg:
            return None

        temp_dir = os.path.join(tempfile.gettempdir(), "live_cutter_stt")
        os.makedirs(temp_dir, exist_ok=True)
        vhash = hashlib.md5(video_path.encode()).hexdigest()[:12]
        wav = os.path.join(temp_dir, f"audio_{vhash}.wav")
        ffmpeg = "ffmpeg"
        try:
            from platform_config import FFMPEG_CMD as fc
            if os.path.exists(fc): ffmpeg = fc
        except:
            pass
        r = subprocess.run([ffmpeg, "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le",
                            "-ar", "16000", "-ac", "1", wav], capture_output=True, timeout=300)
        if r.returncode != 0 or not os.path.exists(wav):
            return None

        # 火山引擎
        va, vt = cfg.get("volc_app_id", ""), cfg.get("volc_access_token", "")
        vak, vsk = cfg.get("volc_tos_ak", ""), cfg.get("volc_tos_sk", "")
        vb = cfg.get("volc_bucket", "livec")
        v_apikey = cfg.get("volc_api_key", "")
        # 新版API Key优先，旧版app_id+token兜底
        if (provider is None or provider == "volc") and all([vak, vsk]) and (v_apikey or all([va, vt])):
            try:
                from volcengine_asr import volcengine_asr as vasr
                segs = vasr(wav, va, vt, vak, vsk, bucket=vb,
                            log_fn=lambda m: self._log(f"  {m}", "info"), api_key=v_apikey or None)
                if segs:
                    srt = video_path.rsplit(".", 1)[0] + ".srt"
                    _sl = []
                    for _i, _s in enumerate(segs, 1):
                        _st = _s["start"] if isinstance(_s, dict) else _s[0]
                        _et = _s["end"] if isinstance(_s, dict) else _s[1]
                        _tx = _s["text"] if isinstance(_s, dict) else _s[2]
                        def _fmt(t): h=int(t//3600);m=int(t%3600//60);s=t%60;return f"{h:02d}:{m:02d}:{s:06.3f}"
                        _sl.append(f"{_i}\n{_fmt(_st)} --> {_fmt(_et)}\n{_tx}\n")
                    with open(srt, "w", encoding="utf-8") as _sf:
                        _sf.write("\n".join(_sl))
                    self._log(f"  火山引擎ASR完成", "ok")
                    return srt
            except Exception as e:
                self._log(f"  火山引擎ASR失败: {e}", "warn")

        # 阿里云
        ak = cfg.get("aliyun_api_key", "")
        aak, ask = cfg.get("aliyun_oss_ak", ""), cfg.get("aliyun_oss_sk", "")
        ab = cfg.get("aliyun_bucket", "")
        ae = cfg.get("aliyun_endpoint", "oss-cn-beijing.aliyuncs.com")
        if (provider is None or provider == "aliyun") and ak and aak and ask and ab:
            try:
                from aliyun_asr import aliyun_asr as aasr
                segs = aasr(wav, app_key=ak, oss_ak=aak, oss_sk=ask,
                            oss_bucket=ab, oss_endpoint=ae,
                            log_fn=lambda m: self._log(f"  {m}", "info"))
                if segs:
                    srt = video_path.rsplit(".", 1)[0] + ".srt"
                    _sl = []
                    for _i, _s in enumerate(segs, 1):
                        _st = _s["start"] if isinstance(_s, dict) else _s[0]
                        _et = _s["end"] if isinstance(_s, dict) else _s[1]
                        _tx = _s["text"] if isinstance(_s, dict) else _s[2]
                        def _fmt(t): h=int(t//3600);m=int(t%3600//60);s=t%60;return f"{h:02d}:{m:02d}:{s:06.3f}"
                        _sl.append(f"{_i}\n{_fmt(_st)} --> {_fmt(_et)}\n{_tx}\n")
                    with open(srt, "w", encoding="utf-8") as _sf:
                        _sf.write("\n".join(_sl))
                    self._log(f"  阿里云ASR完成", "ok")
                    return srt
            except Exception as e:
                self._log(f"  阿里云ASR失败: {e}", "warn")

        return None

    def _add_videos(self):
        paths = filedialog.askopenfilenames(
            title="选择视频文件（可多选，支持 MP4/TS/FLV）",
            filetypes=[("视频文件", "*.mp4 *.ts *.avi *.mov *.mkv *.flv"), ("所有文件", "*.*")]
        )
        if not paths:
            return
        for p in paths:
            if p not in self._video_list:
                self._video_list.append(p)
                self._video_listbox.insert("end", os.path.basename(p))

    def _del_selected(self):
        sel = self._video_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        del self._video_list[idx]
        self._video_listbox.delete(idx)

    def _clear_all(self):
        self._video_list.clear()
        self._video_listbox.delete(0, "end")
        self._results_tree.delete(*self._results_tree.get_children())
        self._count_label.configure(text="已选 0 个视频")

    # ---- 扫描逻辑 ----

    def _select_all(self):
        """全选结果"""
        for child in self._results_tree.get_children():
            self._results_tree.selection_add(child)
        self._update_sel_label()

    def _deselect_all(self):
        """取消全选"""
        self._results_tree.selection_remove(*self._results_tree.get_children())
        self._update_sel_label()

    def _update_sel_label(self, event=None):
        count = len(self._results_tree.selection())
        total = len(self._results_tree.get_children())
        self._sel_label.configure(text=f"已选 {count}/{total} 个")

    def _browse_scan_output(self):
        """选择单品导出目录"""
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self._scan_output_dir = path
            self._scan_output_var.set(path)
            self._log(f"输出目录: {path}", "info")

    def _start_scan(self):
        if not self._video_list:
            self._log("请先添加视频", "warn")
            return
        if self._processing:
            return
        self._processing = True
        self._start_btn.configure(state="disabled", text="扫描中...")
        self._results_tree.delete(*self._results_tree.get_children())

        def _worker():
            all_products = []
            for idx, vpath in enumerate(self._video_list):
                vname = os.path.basename(vpath)
                self._log(f"[{idx+1}/{len(self._video_list)}] {vname}", "info")

                # 1. 生成 SRT（优先云端ASR，失败再用Whisper）
                srt_path = vpath.rsplit(".", 1)[0] + ".srt"
                if not os.path.exists(srt_path):
                    self._log(f"  生成字幕...", "info")
                    _srt = self._try_cloud_asr(vpath)
                    if not _srt:
                        self._log(f"  使用本地Whisper...", "info")
                        from stt import generate_srt
                        _srt = generate_srt(vpath, log_fn=lambda m: self._log(f"  {m}", "info"))
                    srt_path = _srt
                    if not srt_path or not os.path.exists(srt_path):
                        self._log(f"  字幕生成失败，跳过", "err")
                        continue
                else:
                    self._log(f"  SRT已存在: {os.path.basename(srt_path)}", "info")

                # 2. AI 扫描单品
                self._log(f"  正在 AI 分析单品...", "info")
                try:
                    from ai_clipper import load_settings
                    settings = load_settings()
                    from product_scanner import ProductScanner
                    scanner = ProductScanner(
                        api_key=settings.get("api_key", ""),
                        base_url=settings.get("base_url", "https://api.deepseek.com"),
                        model=settings.get("model", "deepseek-chat"),
                    )
                    products = scanner.scan(srt_path, log_fn=lambda m: self._log(f"  {m}", "info"))
                    if products:
                        for p in products:
                            p["_video"] = vpath
                        all_products.extend(products)
                        self._log(f"  发现 {len(products)} 个单品", "ok")
                    else:
                        self._log(f"  未发现单品", "warn")
                except Exception as e:
                    self._log(f"  AI 扫描失败: {e}", "err")

            # 3. Fuzzy merge + drop short isolated
            try:
                from product_scanner import fuzzy_merge_products
                all_products = fuzzy_merge_products(all_products, log_fn=lambda m: self._log(f"  {m}", "info"))
            except Exception as _fe:
                self._log(f"  fuzzy merge error: {_fe}", "err")
            self._display_results(all_products)
            self._all_scan_results = list(all_products)
            self._processing = False
            self._start_btn.configure(state="normal", text="▶ 开始扫描")

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def _display_results(self, products):
        self._products = products if products else []
        if not self._products:
            self._log("扫描完成，未发现单品", "warn")
            return
        self._merge_btn.configure(state="normal")
        self._results_tree.delete(*self._results_tree.get_children())
        for i, p in enumerate(products):
            if p.get("segments") and len(p["segments"]) > 1:
                seg_strs = [f"{s:.0f}-{e:.0f}" for s, e in p["segments"]]
                time_str = "; ".join(seg_strs[:4])
                if len(p["segments"]) > 4:
                    time_str += "..."
            else:
                time_str = f"{p['start']:.1f}-{p['end']:.1f}s"
            vname = os.path.basename(p.get("_video", ""))
            txt = (p.get("calibrated_text", "") or p.get("text", "") or "")[:50]
            self._results_tree.insert("", "end", iid=str(i),
                                       values=(p.get("_display_name", p["name"]), time_str, vname, txt))
        # 显示导出按钮
        self._export_btn.pack(fill="x", padx=16, pady=(0,10))
        self._log(f"扫描完成，共发现 {len(products)} 个单品，点击「导出选中单品」生成视频", "ok")

        # 自动导出
        if self._auto_export_var.get():
            out_dir = getattr(self, '_scan_output_dir', None)
            if out_dir:
                self._select_all()
                self._export_selected()
    def _merge_across_files(self):
        from product_scanner import merge_across_files
        all_ps = getattr(self, "_all_scan_results", [])
        if not all_ps:
            self._log("没有可合并的扫描结果", "warn")
            return
        merged = merge_across_files(all_ps, log_fn=lambda m: self._log(m, "info"))
        if not merged:
            self._log("合并后无结果", "warn")
            return
        self._merged_products = merged
        self._results_tree.delete(*self._results_tree.get_children())
        for i, p in enumerate(merged):
            self._results_tree.insert("", "end", iid="m" + str(i), values=(p["name"], str(int(p["total_duration"])) + "s(" + str(p["source_count"]) + "次)", str(p["source_count"]) + "个文件", ""))
        self._log("跨文件合并完成: " + str(len(merged)) + " 个单品", "ok")
        self._export_merge_btn.configure(state="normal")

    def _export_merged(self):
        from product_scanner import ProductScanner
        merged = getattr(self, "_merged_products", None)
        if not merged:
            self._log("请先执行跨文件合并", "warn")
            return
        out_dir = getattr(self, "_scan_output_dir", None)
        if not out_dir:
            self._log("请先选择输出目录", "warn")
            return
        scanner = ProductScanner()
        self._log("开始导出 " + str(len(merged)) + " 个单品...", "info")
        results = scanner.extract_cross_file(merged, out_dir, log_fn=lambda m: self._log(m, "info"))
        ok_count = len([r for r in results if r.get("output_path")])
        self._log("导出完成: " + str(ok_count) + "/" + str(len(merged)), "ok" if ok_count else "warn")
