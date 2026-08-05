"""
独立去重工具 - 手剪视频去重页面
用户自由选择去重参数：镜像/裁切/变速/模糊/锐化/色相/噪点/画中画/音频等
"""
import os, subprocess, threading, json, shutil, sys, tkinter as tk
from tkinter import filedialog, messagebox
from config import C, FNT_S, FNT_B


def _base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _find_tool(name):
    exe_name = name + (".exe" if os.name == "nt" else "")
    base = _base_dir()
    candidates = [
        os.path.join(base, "ffmpeg", exe_name),
        os.path.join(base, "_internal", "ffmpeg", exe_name),
        os.path.join(os.path.dirname(base), "_internal", "ffmpeg", exe_name),
        os.path.join(os.path.dirname(base), "ffmpeg", exe_name),
    ]
    found = shutil.which(exe_name) or shutil.which(name)
    if found:
        candidates.append(found)
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return name

DEDUP_DEFAULTS = {
    "mirror": {"enabled": False},
    "crop": {"enabled": False, "ratio": 0.97},
    "scale": {"enabled": False, "ratio": 1.0},
    "speed": {"enabled": False, "rate": 1.0},
    "rotate": {"enabled": False, "angle": 0.0},
    "blur": {"enabled": False, "strength": 0.5},
    "sharpen": {"enabled": False, "strength": 0.3},
    "gamma": {"enabled": False, "value": 1.0},
    "hue": {"enabled": False, "shift": 0.0},
    "saturation": {"enabled": False, "value": 1.0},
    "contrast": {"enabled": False, "value": 1.0},
    "noise": {"enabled": False, "strength": 0.02},
    "corner_mask": {"enabled": False},
    "bg_fill": {"enabled": False, "color": "#000000"},
    "frame_interp": {"enabled": False},
    "frame_rate": {"enabled": False, "value": 30},
    # PIP
    "pip": {"enabled": False, "video": "", "opacity": 0.3, "size": 0.2, "pos": "右下"},
    # Audio
    "volume": {"enabled": False, "level": 1.0},
    "pitch": {"enabled": False, "shift": 0.0},
    "reverb": {"enabled": False},
}


class DedupApp:
    def __init__(self, parent):
        self.parent = parent
        self.win = tk.Toplevel(parent)
        self.win.title("视频去重工具")
        self.win.geometry("820x820")
        self.win.configure(bg=C["bg"])
        self.win.transient(parent)
        self.win.grab_set()

        self.video_path = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.pip_path = tk.StringVar()
        self._cancel_event = None
        self._worker = None
        self.params = {}

        self._build_ui()

    def _add_grid_item(self, parent, row, col, label, var_key, default, from_=0, to=2, resolution=0.01):
        """Add a grid cell with checkbox + label + slider"""
        cell = tk.Frame(parent, bg=C["card"], padx=8, pady=4)
        cell.grid(row=row, column=col, sticky="ew", padx=3, pady=2)
        parent.columnconfigure(col, weight=1, uniform="param")
        # Top row: checkbox + label
        top = tk.Frame(cell, bg=C["card"])
        top.pack(fill="x")
        cb = tk.Checkbutton(top, text=label, variable=self.params[var_key]["cb"],
                            font=FNT_S, fg=C["text"], bg=C["card"],
                            selectcolor=C["inp"], activebackground=C["card"],
                            command=lambda: self._toggle_slider(var_key))
        cb.pack(side="left")
        # Slider below
        self.params[var_key]["slider"] = tk.Scale(cell, from_=from_, to=to, resolution=resolution,
                                                   orient="horizontal", length=140,
                                                   fg=C["text"], bg=C["card"], troughcolor=C["inp"],
                                                   font=("Consolas", 8), showvalue=True)
        self.params[var_key]["slider"].set(default)
        self.params[var_key]["slider"].pack(fill="x", padx=2)
        self.params[var_key]["slider"].config(state="disabled")

    def _toggle_slider(self, key):
        enabled = self.params[key]["cb"].get()
        if self.params[key]["slider"]:
            self.params[key]["slider"].config(state="normal" if enabled else "disabled")

    def _build_ui(self):
        # Colors - cleaner modern dark theme
        BG = "#1e1e2e"
        CARD = "#2a2a3e"
        INP = "#3a3a50"
        TXT = "#e0e0e0"
        DIM = "#8888aa"
        GREEN = "#10b981"
        BLUE = "#3b82f6"

        # Top row: file selection with preview
        top_frame = tk.Frame(self.win, bg=BG)
        top_frame.pack(fill="x", padx=16, pady=(10, 4))

        # Left: video info
        info_frame = tk.Frame(top_frame, bg=BG)
        info_frame.pack(side="left", fill="x", expand=True)

        # Source video
        src_row = tk.Frame(info_frame, bg=BG)
        src_row.pack(fill="x", pady=2)
        tk.Label(src_row, text="源视频:", font=("Microsoft YaHei", 10, "bold"),
                 fg=TXT, bg=BG, width=8, anchor="w").pack(side="left")
        tk.Entry(src_row, textvariable=self.video_path, font=("Microsoft YaHei", 10),
                 fg=TXT, bg=INP, relief="flat", insertbackground=TXT).pack(side="left", fill="x", expand=True, padx=4)
        tk.Button(src_row, text="浏览", font=("Microsoft YaHei", 10),
                  fg="white", bg=BLUE, relief="flat", padx=12, pady=2, cursor="hand2",
                  command=self._select_video).pack(side="left")

        # Output dir
        out_row = tk.Frame(info_frame, bg=BG)
        out_row.pack(fill="x", pady=2)
        tk.Label(out_row, text="输出目录:", font=("Microsoft YaHei", 10, "bold"),
                 fg=TXT, bg=BG, width=8, anchor="w").pack(side="left")
        tk.Entry(out_row, textvariable=self.output_dir, font=("Microsoft YaHei", 10),
                 fg=TXT, bg=INP, relief="flat", insertbackground=TXT).pack(side="left", fill="x", expand=True, padx=4)
        tk.Button(out_row, text="浏览", font=("Microsoft YaHei", 10),
                  fg="white", bg=BLUE, relief="flat", padx=12, pady=2, cursor="hand2",
                  command=self._select_output).pack(side="left")
        tk.Button(out_row, text="打开", font=("Microsoft YaHei", 10),
                  fg=TXT, bg=INP, relief="flat", padx=8, pady=2, cursor="hand2",
                  command=self._open_output).pack(side="left", padx=(4,0))

        # Right: thumbnail preview
        self._preview_label = tk.Label(top_frame, text="未选择视频", font=("Microsoft YaHei", 9),
                                        fg=DIM, bg=CARD, width=20, height=6, relief="flat")
        self._preview_label.pack(side="right", padx=(10, 0))

        # Separator
        tk.Frame(self.win, height=1, bg="#3a3a50").pack(fill="x", padx=16, pady=6)

        # Scrollable parameter area
        mid_frame = tk.Frame(self.win, bg=BG)
        mid_frame.pack(fill="both", expand=True)

        canvas = tk.Canvas(mid_frame, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(mid_frame, orient="vertical", command=canvas.yview)
        sp = tk.Frame(canvas, bg=BG)
        sp.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=sp, anchor="nw", width=740)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(16, 0))
        scrollbar.pack(side="right", fill="y", padx=(0, 16))

        # Initialize params
        for k, v in DEDUP_DEFAULTS.items():
            self.params[k] = {"cb": tk.BooleanVar(value=v["enabled"]), "data": v, "slider": None}

        # === 画面处理 ===
        sec_label = tk.Label(sp, text="═ 画面处理 ═", font=("Microsoft YaHei", 11, "bold"),
                              fg=BLUE, bg=BG, anchor="w")
        sec_label.pack(fill="x", padx=2, pady=(4, 4))

        video_params = [
            ("mirror", "镜像翻转", 0, 0, 1),
            ("crop", "微裁切", 0.97, 0.85, 1.0),
            ("scale", "画面缩放", 1.0, 0.8, 1.2),
            ("speed", "视频变速", 1.0, 0.8, 1.5),
            ("rotate", "微旋转(度)", 0, -5, 5, 0.1),
            ("blur", "高斯模糊", 0.5, 0, 5, 0.1),
            ("sharpen", "锐化", 0.3, 0, 2),
            ("gamma", "伽马校正", 1.0, 0.5, 2.0),
            ("hue", "色相偏移", 0, -180, 180, 1),
            ("saturation", "饱和度", 1.0, 0, 3),
            ("contrast", "对比度", 1.0, 0, 3),
            ("noise", "噪点融合", 0.02, 0, 0.1, 0.001),
            ("corner_mask", "角标遮罩", 0, 0, 1),
            ("frame_interp", "光流补帧", 0, 0, 1),
            ("frame_rate", "帧率(fps)", 30, 15, 60, 1),
        ]
        grid1 = tk.Frame(sp, bg=BG)
        grid1.pack(fill="x", padx=2)
        for i, p in enumerate(video_params):
            key, label = p[0], p[1]
            default, fr, to = p[2], p[3], p[4]
            res = p[5] if len(p) > 5 else 0.01
            cell = tk.Frame(grid1, bg=CARD, padx=8, pady=4)
            cell.grid(row=i//3, column=i%3, sticky="ew", padx=3, pady=2)
            grid1.columnconfigure(i%3, weight=1, uniform="a")
            cb = tk.Checkbutton(cell, text=label, variable=self.params[key]["cb"],
                                font=("Microsoft YaHei", 9), fg=TXT, bg=CARD,
                                selectcolor=INP, activebackground=CARD,
                                command=lambda k=key: self._toggle_slider(k))
            cb.pack(anchor="w")
            self.params[key]["slider"] = tk.Scale(cell, from_=fr, to=to, resolution=res,
                                                   orient="horizontal", length=140,
                                                   fg=TXT, bg=CARD, troughcolor=INP,
                                                   font=("Consolas", 7), showvalue=True)
            self.params[key]["slider"].set(default)
            self.params[key]["slider"].pack(fill="x", padx=2)
            self.params[key]["slider"].config(state="disabled")

        # Background fill row
        bg_row = tk.Frame(sp, bg=BG)
        bg_row.pack(fill="x", padx=2, pady=4)
        self.params["bg_fill"]["cb"] = tk.BooleanVar(value=False)
        tk.Checkbutton(bg_row, text="背景填充", variable=self.params["bg_fill"]["cb"],
                       font=("Microsoft YaHei", 9), fg=TXT, bg=BG,
                       selectcolor=INP, activebackground=BG).pack(side="left")
        tk.Button(bg_row, text="颜色", font=("Microsoft YaHei", 9),
                  fg=TXT, bg=INP, relief="flat", padx=8, cursor="hand2",
                  command=self._pick_color).pack(side="left", padx=4)
        self._bg_color_btn = tk.Label(bg_row, text="  ", bg="#000000", width=2, relief="solid")
        self._bg_color_btn.pack(side="left", padx=2)
        self._bg_img_path = tk.StringVar()
        tk.Entry(bg_row, textvariable=self._bg_img_path, font=("Microsoft YaHei", 9),
                 fg=DIM, bg=INP, relief="flat", width=18, insertbackground=TXT).pack(side="left", padx=4)
        tk.Button(bg_row, text="背景图片", font=("Microsoft YaHei", 9),
                  fg=TXT, bg=INP, relief="flat", padx=8, cursor="hand2",
                  command=self._select_bg_img).pack(side="left")

        # === 画中画 ===
        tk.Label(sp, text="═ 画中画(PIP) ═", font=("Microsoft YaHei", 11, "bold"),
                 fg=BLUE, bg=BG, anchor="w").pack(fill="x", padx=2, pady=(8, 4))
        pip_frame = tk.Frame(sp, bg=BG)
        pip_frame.pack(fill="x", padx=2)

        # Row 1: enable + file select
        pip_r1 = tk.Frame(pip_frame, bg=CARD, padx=8, pady=4)
        pip_r1.pack(fill="x", pady=2)
        self.params["pip"]["cb"] = tk.BooleanVar(value=False)
        tk.Checkbutton(pip_r1, text="启用画中画", variable=self.params["pip"]["cb"],
                       font=("Microsoft YaHei", 9), fg=TXT, bg=CARD,
                       selectcolor=INP, activebackground=CARD).pack(side="left")
        tk.Entry(pip_r1, textvariable=self.pip_path, font=("Microsoft YaHei", 9),
                 fg=TXT, bg=INP, relief="flat", insertbackground=TXT).pack(side="left", fill="x", expand=True, padx=4)
        tk.Button(pip_r1, text="选择视频", font=("Microsoft YaHei", 9),
                  fg="white", bg=BLUE, relief="flat", padx=8, cursor="hand2",
                  command=self._select_pip).pack(side="left")

        # Row 2: opacity, size, position
        pip_r2 = tk.Frame(pip_frame, bg=CARD, padx=8, pady=4)
        pip_r2.pack(fill="x", pady=2)
        for label, key, default, fr, to in [
            ("透明度", "opacity", 0.3, 0.01, 1.0),
            ("尺寸", "size", 0.2, 0.05, 1.0),
        ]:
            tk.Label(pip_r2, text=label, font=("Microsoft YaHei", 9),
                     fg=DIM, bg=CARD, width=5, anchor="e").pack(side="left")
            self.params["pip"]["slider_" + key] = tk.Scale(pip_r2, from_=fr, to=to, resolution=0.01,
                                                            orient="horizontal", length=100,
                                                            fg=TXT, bg=CARD, troughcolor=INP,
                                                            font=("Consolas", 7), showvalue=True)
            self.params["pip"]["slider_" + key].set(default)
            self.params["pip"]["slider_" + key].pack(side="left", padx=(2, 8))

        tk.Label(pip_r2, text="位置", font=("Microsoft YaHei", 9),
                 fg=DIM, bg=CARD, width=3, anchor="e").pack(side="left")
        self.params["pip"]["pos_var"] = tk.StringVar(value="右下")
        for pos in ["右下", "右上", "左下", "左上"]:
            tk.Radiobutton(pip_r2, text=pos, variable=self.params["pip"]["pos_var"],
                           value=pos, font=("Microsoft YaHei", 9), fg=TXT, bg=CARD,
                           selectcolor=INP, activebackground=CARD).pack(side="left", padx=2)

        # === 音频处理 ===
        tk.Label(sp, text="═ 音频处理 ═", font=("Microsoft YaHei", 11, "bold"),
                 fg=BLUE, bg=BG, anchor="w").pack(fill="x", padx=2, pady=(8, 4))
        audio_params = [
            ("volume", "音量", 1.0, 0, 3),
            ("pitch", "变调(semitones)", 0, -5, 5, 0.5),
            ("reverb", "混响", 0, 0, 1),
        ]
        grid3 = tk.Frame(sp, bg=BG)
        grid3.pack(fill="x", padx=2)
        for i, p in enumerate(audio_params):
            key, label = p[0], p[1]
            default, fr, to = p[2], p[3], p[4]
            res = p[5] if len(p) > 5 else 0.01
            cell = tk.Frame(grid3, bg=CARD, padx=8, pady=4)
            cell.grid(row=0, column=i, sticky="ew", padx=3, pady=2)
            grid3.columnconfigure(i, weight=1, uniform="b")
            cb = tk.Checkbutton(cell, text=label, variable=self.params[key]["cb"],
                                font=("Microsoft YaHei", 9), fg=TXT, bg=CARD,
                                selectcolor=INP, activebackground=CARD,
                                command=lambda k=key: self._toggle_slider(k))
            cb.pack(anchor="w")
            self.params[key]["slider"] = tk.Scale(cell, from_=fr, to=to, resolution=res,
                                                   orient="horizontal", length=140,
                                                   fg=TXT, bg=CARD, troughcolor=INP,
                                                   font=("Consolas", 7), showvalue=True)
            self.params[key]["slider"].set(default)
            self.params[key]["slider"].pack(fill="x", padx=2)
            self.params[key]["slider"].config(state="disabled")

        # === Bottom buttons + progress + log ===
        btn_row = tk.Frame(self.win, bg=BG)
        btn_row.pack(fill="x", padx=16, pady=(6, 2))
        self._start_btn = tk.Button(btn_row, text="▶ 开始去重", font=("Microsoft YaHei", 12, "bold"),
                                    fg="white", bg=GREEN, relief="flat", padx=24, pady=6, cursor="hand2",
                                    command=self._start)
        self._start_btn.pack(side="left")
        tk.Button(btn_row, text="重置默认", font=("Microsoft YaHei", 10),
                  fg=TXT, bg=INP, relief="flat", padx=12, cursor="hand2",
                  command=self._reset).pack(side="left", padx=6)

        self._progress = tk.ttk.Progressbar(self.win, orient="horizontal", length=0, mode="indeterminate")
        self._log_box = tk.Text(self.win, font=("Consolas", 9), fg=DIM, bg="#1a1a2e",
                                relief="flat", padx=8, pady=4, height=6, wrap="word")
        self._log_box.pack(fill="x", padx=16, pady=(0, 8))
    def _log(self, msg):
        self._log_box.insert("end", msg + "\n")
        self._log_box.see("end")
        self.win.update_idletasks()

    def _select_video(self):
        fp = filedialog.askopenfilename(title="选择视频文件",
                                         filetypes=[("视频文件", "*.mp4 *.avi *.mov *.mkv *.flv")])
        if fp:
            self.video_path.set(fp)
            if not self.output_dir.get():
                self.output_dir.set(os.path.dirname(fp) + "/dedup_output")

    def _select_output(self):
        dp = filedialog.askdirectory(title="选择输出目录")
        if dp:
            self.output_dir.set(dp)

    def _open_output(self):
        dp = self.output_dir.get()
        if dp and os.path.exists(dp):
            os.startfile(dp)
        else:
            self._log("输出目录不存在")

    def _select_pip(self):
        fp = filedialog.askopenfilename(title="选择画中画视频",
                                         filetypes=[("视频文件", "*.mp4 *.avi *.mov *.mkv")])
        if fp:
            self.pip_path.set(fp)

    def _select_bg_img(self):
        fp = filedialog.askopenfilename(title="选择背景图片",
                                         filetypes=[("图片文件", "*.jpg *.jpeg *.png *.bmp")])
        if fp:
            self._bg_img_path.set(fp)

    def _pick_color(self):
        from tkinter import colorchooser
        c = colorchooser.askcolor(title="选择背景色", parent=self.win)
        if c and c[1]:
            self._bg_color_btn.config(bg=c[1])
            self.params["bg_fill"]["color"] = c[1]

    def _reset(self):
        for k, v in DEDUP_DEFAULTS.items():
            if k in self.params:
                self.params[k]["cb"].set(v["enabled"])
                sl = self.params[k].get("slider")
                if sl and "slider" in v and v["slider"] is not None:
                    sl.set(v["slider"])
                elif sl:
                    sl.set(list(DEDUP_DEFAULTS[k].values())[1] if len(DEDUP_DEFAULTS) > 1 else 0)

    def _start(self):
        if self._worker and self._worker.is_alive():
            if self._cancel_event:
                self._cancel_event.set()
            self._start_btn.config(text="▶ 开始去重", bg=C["btn_go"])
            self._log("已请求停止")
            return

        if not self.video_path.get() or not os.path.exists(self.video_path.get()):
            messagebox.showerror("提示", "请选择源视频", parent=self.win)
            return

        try:
            from license_guard import require_feature_access
            if not require_feature_access("去重工具", self.win, self._log, refresh=False):
                return
        except Exception as e:
            self._log("授权检查异常: " + str(e))
            return

        self._start_btn.config(text="■ 停止", bg=C["btn_no"])
        self._cancel_event = threading.Event()
        self._worker = threading.Thread(target=self._run_dedup, daemon=True)
        self._worker.start()

    def _build_ffmpeg_cmd(self, input_path, output_path, W, H):
        """Build ffmpeg command based on user params"""
        vf_list = []
        af_list = []
        applied = []

        # 1. Mirror
        if self.params["mirror"]["cb"].get():
            vf_list.append("hflip")
            applied.append("mirror")

        # 2. Background fill (add black bars or image to change aspect ratio)
        if self.params["bg_fill"]["cb"].get():
            bg_img = self._bg_img_path.get().strip()
            if bg_img and os.path.exists(bg_img):
                vf_list.append(f"pad=iw+80:ih+80:(ow-iw)/2:(oh-ih)/2:color=black@0")
                vf_list.append(f"overlay={bg_img}:x=(W-w)/2:y=(H-h)/2")
                applied.append("bg_img")
            else:
                vf_list.append(f"pad=iw+40:ih+40:(ow-iw)/2:(oh-ih)/2:color={self._bg_color_btn.cget('bg')}")
                applied.append("bg_fill")

        # 3. Crop
        if self.params["crop"]["cb"].get():
            r = self.params["crop"]["slider"].get()
            vf_list.append(f"crop=iw*{r}:ih*{r}")
            applied.append(f"crop({r})")

        # 4. Scale
        if self.params["scale"]["cb"].get():
            r = self.params["scale"]["slider"].get()
            if r != 1.0:
                vf_list.append(f"scale=iw*{r}:ih*{r}:flags=bilinear")
                applied.append(f"scale({r})")

        # 5. Rotate
        if self.params["rotate"]["cb"].get():
            a = self.params["rotate"]["slider"].get()
            if a != 0:
                vf_list.append(f"rotate={a}*PI/180:fill=black")
                applied.append(f"rotate({a})")

        # 6. Blur
        if self.params["blur"]["cb"].get():
            s = self.params["blur"]["slider"].get()
            if s > 0:
                vf_list.append(f"gblur=sigma={s}")
                applied.append(f"blur({s})")

        # 7. Sharpen
        if self.params["sharpen"]["cb"].get():
            s = self.params["sharpen"]["slider"].get()
            if s > 0:
                vf_list.append(f"unsharp=luma_msize_x=3:luma_msize_y=3:luma_amount={s}")
                applied.append(f"sharpen({s})")

        # 8. Gamma
        if self.params["gamma"]["cb"].get():
            g = self.params["gamma"]["slider"].get()
            if g != 1.0:
                vf_list.append(f"eq=gamma={g}")
                applied.append(f"gamma({g})")

        # 9. Hue
        if self.params["hue"]["cb"].get():
            h = self.params["hue"]["slider"].get()
            if h != 0:
                vf_list.append(f"hue=h={h}")
                applied.append(f"hue({h})")

        # 10. Saturation
        if self.params["saturation"]["cb"].get():
            s = self.params["saturation"]["slider"].get()
            if s != 1.0:
                vf_list.append(f"eq=saturation={s}")
                applied.append(f"saturation({s})")

        # 11. Contrast
        if self.params["contrast"]["cb"].get():
            c = self.params["contrast"]["slider"].get()
            if c != 1.0:
                vf_list.append(f"eq=contrast={c}")
                applied.append(f"contrast({c})")

        # 12. Noise
        if self.params["noise"]["cb"].get():
            s = self.params["noise"]["slider"].get()
            if s > 0:
                vf_list.append(f"noise=alls={int(s*100)}:allf=t+u")
                applied.append(f"noise({s})")

        # 13. Corner mask
        if self.params["corner_mask"]["cb"].get():
            vf_list.append(f"drawbox=x=0:y=0:w=40:h=40:color=black@0.3:t=fill")
            applied.append("corner_mask")

        # 14. Frame interpolation (minterpolate)
        if self.params["frame_interp"]["cb"].get():
            vf_list.append("minterpolate=mi_mode=mci:mc_mode=aobmc:fps=60")
            applied.append("frame_interp")

        # 15. Frame rate change
        if self.params["frame_rate"]["cb"].get():
            fr = self.params["frame_rate"]["slider"].get()
            vf_list.append(f"fps={fr}")
            applied.append(f"fps({fr})")

        # 16. PIP (画中画)
        pip_video = self.pip_path.get()
        if self.params["pip"]["cb"].get() and pip_video and os.path.exists(pip_video):
            opacity = self.params["pip"]["slider_opacity"].get()
            psize = self.params["pip"]["slider_size"].get()
            pos = self.params["pip"]["pos_var"].get()

            # Calculate overlay position
            pos_map = {"右下": "(W-w)/2:(H-h)/2", "右上": "(W-w):0",
                       "左下": "0:(H-h)", "左上": "0:0"}
            pip_pos = pos_map.get(pos, "(W-w)/2:(H-h)/2")

            # PIP uses overlay filter with a separate input
            pip_scale = f"scale=iw*{psize}:ih*{psize}"
            pip_alpha = f"format=rgba,colorchannelmixer=aa={opacity}"
            vf_list.append(f"[1:v]{pip_scale},{pip_alpha}[pip];[0:v][pip]overlay={pip_pos}")
            applied.append(f"pip({pos},{psize},{opacity})")

        # 15. Speed (includes audio tempo)
        speed = 1.0
        if self.params["speed"]["cb"].get():
            speed = self.params["speed"]["slider"].get()
            if speed != 1.0:
                vf_list.append(f"setpts={1/speed}*PTS")
                af_list.append(f"atempo={speed}")
                applied.append(f"speed({speed})")

        # --- Audio filters ---
        # Volume
        if self.params["volume"]["cb"].get():
            v = self.params["volume"]["slider"].get()
            if v != 1.0:
                af_list.append(f"volume={v}")
                applied.append(f"volume({v})")

        # Pitch
        if self.params["pitch"]["cb"].get():
            p = self.params["pitch"]["slider"].get()
            if p != 0:
                # semitones -> rate: 2^(n/12)
                rate = 2 ** (p / 12)
                af_list.append(f"asetrate=44100*{rate},aresample=44100")
                applied.append(f"pitch({p})")

        # Reverb
        if self.params["reverb"]["cb"].get():
            r = self.params["reverb"]["slider"].get()
            if r > 0:
                af_list.append(f"aecho=0.8:0.7:{r*100}:0.3")
                applied.append(f"reverb({r})")

        # Build command
        cmd = [_find_tool("ffmpeg"), "-y", "-i", input_path]
        pip_v = self.pip_path.get()
        pip_on = self.params["pip"]["cb"].get() and pip_v and os.path.exists(pip_v)
        if pip_on:
            cmd.extend(["-i", pip_v, "-an"])

        if pip_on and vf_list:
            # PIP requires filter_complex (multiple inputs)
            cmd.extend(["-filter_complex", ",".join(vf_list)])
        elif vf_list:
            cmd.extend(["-vf", ",".join(vf_list)])
        if af_list:
            cmd.extend(["-af", ",".join(af_list)])

        cmd.extend(["-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k", output_path])

        return cmd, applied

    def _run_dedup(self):
        self._progress.start(15)
        self._progress.pack(fill="x", padx=12, pady=(0, 2))
        try:
            input_path = self.video_path.get()
            out_dir = self.output_dir.get()
            os.makedirs(out_dir, exist_ok=True)

            stem = os.path.splitext(os.path.basename(input_path))[0]
            output_path = os.path.join(out_dir, f"{stem}_dedup.mp4")

            # Get video dimensions
            probe = subprocess.run(
                [_find_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height",
                 "-of", "csv=p=0", input_path],
                capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            dims = probe.stdout.strip().split(",")
            W, H = int(dims[0]), int(dims[1]) if len(dims) >= 2 else (1080, 1920)

            cmd, applied = self._build_ffmpeg_cmd(input_path, output_path, W, H)
            self._log(f"去重参数: {', '.join(applied) if applied else '无'}")
            self._log(f"输出: {output_path}")

            if self._cancel_event and self._cancel_event.is_set():
                self._log("已取消")
                return

            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            stdout, stderr = proc.communicate(timeout=3600)

            if proc.returncode == 0:
                self._log(f"✅ 去重完成: {os.path.getsize(output_path)//1024//1024}MB")
                try:
                    from license_guard import consume_trial_after_success
                    consume_trial_after_success("去重工具", root=None, log_fn=self._log)
                except Exception:
                    pass
                # 自动打开输出目录
                if out_dir and os.path.exists(out_dir):
                    os.startfile(out_dir)
            else:
                self._log(f"❌ 去重失败: {stderr.decode('utf-8', errors='ignore')[:200]}")

        except Exception as e:
            self._log(f"❌ 错误: {str(e)}")
        finally:
            self._start_btn.config(text="▶ 开始去重", bg=C["btn_go"])
            self._progress.stop()
            self._progress.pack_forget()
