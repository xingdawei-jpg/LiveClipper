# -*- coding: utf-8 -*-
"""混剪成片 - 独立页面，布局和智能成片一致"""

import os, time, threading, json, tkinter as tk
from tkinter import filedialog, ttk
from cutter_logic import process_video_mix
from config import DEDUP_CONFIG, SETTINGS_PATH

C = {
    "bg": "#1A1A2E", "card": "#25253A", "inp": "#2C2C3A", "text": "#E5E5EA",
    "dim": "#9A9AAF", "btn_go": "#4A6CF7", "btn_go2": "#3B5DE7",
    "btn_sel": "#4A6CF7", "btn_no": "#E55353", "btn_del": "#E55353",
    "ok": "#00C853", "warn": "#FF9100", "err": "#E55353",
}
FNT_B = ("Segoe UI", 10, "bold"); FNT_S = ("Segoe UI", 9); FNT_T = ("Segoe UI", 20, "bold")
DEDUP_CLR = {"none":C["dim"], "light":C["warn"], "medium":C["warn"], "heavy":C["err"], "custom":"#7C4DFF"}


class MixPage(tk.Frame):
    def __init__(self, parent, app=None):
        super().__init__(parent, bg=C["bg"])
        self.app = app
        self._mix_videos = []
        self._mix_cancel = None
        self._mix_worker = None
        self._dedup_collapsed = True
        self._setup_vars()
        self._build()

    def _setup_vars(self):
        self.main_category_var = tk.StringVar(value="自动检测")
        self.ai_focus_var = tk.StringVar(value="自动")
        self.duration_var = tk.StringVar(value="60s")
        self.dedup = tk.StringVar(value="medium")
        self.mirror_var = tk.BooleanVar(value=bool(DEDUP_CONFIG.get("mirror", {}).get("enabled", True)))
        self.smart_crop_var = tk.BooleanVar(value=True)
        self.crop_level_var = tk.StringVar(value="中")
        self.ken_burns_var = tk.BooleanVar(value=True)
        self.kb_intensity_var = tk.StringVar(value="中")
        self.pip_path = ""
        self.subtitle_var = tk.BooleanVar(value=True)
        self.output_dir = ""

    def _build(self):
        # ====== 视频操作 ======
        top = tk.Frame(self, bg=C["bg"])
        top.pack(fill="x", padx=16, pady=(12,4))
        tk.Label(top, text="混剪成片", font=FNT_B, fg=C["text"], bg=C["bg"]).pack(side="left")
        tk.Button(top, text="+ 添加视频", font=FNT_S, fg="white", bg=C["btn_sel"],
                  relief="flat", cursor="hand2", padx=10,
                  command=self._add_videos).pack(side="right")
        tk.Button(top, text="删除", font=FNT_S, fg="white", bg=C["btn_del"],
                  relief="flat", cursor="hand2", padx=8,
                  command=self._remove).pack(side="right", padx=(0,4))
        tk.Button(top, text="清空", font=FNT_S, fg="white", bg=C["btn_del"],
                  relief="flat", cursor="hand2", padx=8,
                  command=self._clear_all).pack(side="right", padx=(0,4))

        # ====== 视频列表 ======
        vf = tk.Frame(self, bg=C["card"])
        vf.pack(fill="x", padx=16, pady=(0,4))
        lf = tk.Frame(vf, bg=C["inp"])
        lf.pack(fill="x", pady=(8,0))
        self._listbox = tk.Listbox(lf, font=FNT_S, bg=C["inp"], fg=C["text"],
                                    selectbackground=C["btn_sel"], height=4,
                                    relief="flat", bd=0)
        sb = tk.Scrollbar(lf, command=self._listbox.yview, bg=C["card"])
        self._listbox.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._listbox.pack(side="left", fill="both", expand=True)
        self._listbox.bind("<Delete>", lambda e: self._remove())
        self.count_label = tk.Label(vf, text="已选 0 个视频", font=FNT_S,
                                    fg=C["dim"], bg=C["card"])
        self.count_label.pack(side="right", anchor="e", pady=(4,0))

        # ====== 主推类目 ======
        tk.Label(vf, text="主推类目:", font=FNT_S, fg=C["dim"],
                 bg=C["card"]).pack(side="left", padx=(4,2), pady=(4,0))
        self.main_category_combo = ttk.Combobox(vf, textvariable=self.main_category_var,
                                          values=["自动检测","上衣","裤子","裙子","外套","套装","鞋子","配饰"],
                                          width=10, font=FNT_S, state="readonly")
        self.main_category_combo.pack(side="left", pady=(4,0))

        # ====== 剪辑数量 ======
        tk.Label(vf, text="  剪辑数量:", font=FNT_S, fg=C["dim"],
                 bg=C["card"]).pack(side="left", padx=(4,2), pady=(4,0))
        self.num_versions_var = tk.StringVar(value="1")
        ttk.Combobox(vf, textvariable=self.num_versions_var,
                     values=["1", "2", "3"], width=3,
                     font=FNT_S, state="readonly").pack(side="left", pady=(4,0))

        # ====== AI偏好 ======
        tk.Label(vf, text="  AI偏好:", font=FNT_S, fg=C["dim"],
                 bg=C["card"]).pack(side="left", padx=(4,2), pady=(4,0))
        self.ai_focus_combo = ttk.Combobox(vf, textvariable=self.ai_focus_var,
                     values=["自动","面料质感","颜色氛围","版型显瘦","穿着场景","性价比","情绪感染","流行趋势"],
                     width=8, font=FNT_S, state="readonly")
        self.ai_focus_combo.pack(side="left", pady=(4,0))

        # ====== 成片时长 ======
        tk.Label(vf, text="  成片时长:", font=FNT_S, fg=C["dim"],
                 bg=C["card"]).pack(side="left", padx=(4,2), pady=(4,0))
        ttk.Combobox(vf, textvariable=self.duration_var,
                     values=["30s", "60s", "90s"], width=4,
                     font=FNT_S, state="readonly").pack(side="left", pady=(4,0))


        # ====== 去重/裁切/动态缩放/画中画（可折叠）=====
        self._dedup_card = tk.Frame(self, bg=C["card"], padx=12, pady=6)
        self._dedup_card.pack(fill="x", padx=16, pady=2)
        opt = tk.Frame(self._dedup_card, bg=C["card"])
        opt.pack(fill="x")

        self._dedup_toggle_lbl = tk.Label(opt, text="\u25b6", font=FNT_S,
                                         fg=C["btn_sel"], bg=C["inp"], cursor="hand2",
                                         padx=6, pady=2)
        self._dedup_toggle_lbl.pack(side="left")
        self._dedup_toggle_lbl.bind("<Button-1>", self._toggle_dedup)

        # 去重
        tk.Label(opt, text="去重:", font=FNT_B, fg=C["text"],
                 bg=C["card"]).pack(side="left")
        for val, txt in [("none","不去重"),("light","轻微"),("medium","中度"),("heavy","重度"),("custom","自定义")]:
            fg = DEDUP_CLR[val]
            rb = tk.Radiobutton(opt, text=txt, variable=self.dedup, value=val,
                           font=FNT_S, fg=fg, bg=C["card"], selectcolor=C["inp"],
                           activebackground=C["card"], activeforeground=fg,
                           indicatoron=0, padx=6, pady=2, relief="flat", bd=2,
                           cursor="hand2", command=self._on_dedup_change)
            rb.pack(side="left", padx=1)

        tk.Checkbutton(opt, text="镜像", variable=self.mirror_var,
                        font=FNT_S, fg=C["text"], bg=C["card"], selectcolor=C["inp"],
                        activebackground=C["card"], cursor="hand2").pack(side="left", padx=(6, 1))

        # 裁切
        tk.Frame(opt, width=1, bg=C["dim"]).pack(side="left", fill="y", padx=6, pady=2)
        tk.Label(opt, text="\U0001f3ac裁切:", font=FNT_S, fg=C["text"], bg=C["card"]).pack(side="left")
        _sc_cb = tk.Checkbutton(opt, text="开", variable=self.smart_crop_var,
                        font=FNT_S, fg=C["btn_sel"], bg=C["card"], selectcolor=C["inp"],
                        activebackground=C["card"], cursor="hand2")
        _sc_cb.pack(side="left", padx=1)
        ttk.Combobox(opt, textvariable=self.crop_level_var,
                     values=["轻", "中", "重"], width=2,
                     font=FNT_S, state="readonly").pack(side="left", padx=1)

        # 动态缩放
        tk.Frame(opt, width=1, bg=C["dim"]).pack(side="left", fill="y", padx=6, pady=2)
        tk.Label(opt, text="\U0001f3a5动态缩放:", font=FNT_S, fg=C["text"], bg=C["card"]).pack(side="left")
        tk.Checkbutton(opt, text="开", variable=self.ken_burns_var,
                        font=FNT_S, fg=C["btn_sel"], bg=C["card"], selectcolor=C["inp"],
                        activebackground=C["card"], cursor="hand2").pack(side="left", padx=1)
        tk.Label(opt, text="强度", font=FNT_S, fg=C["dim"], bg=C["card"]).pack(side="left", padx=(2,0))
        ttk.Combobox(opt, textvariable=self.kb_intensity_var, width=3, font=FNT_S, state="readonly",
                     values=["轻", "中", "重"]).pack(side="left", padx=1)

        # 画中画
        tk.Frame(opt, width=1, bg=C["dim"]).pack(side="left", fill="y", padx=6, pady=2)
        tk.Label(opt, text="画中画:", font=FNT_S, fg=C["text"], bg=C["card"]).pack(side="left")
        tk.Button(opt, text="选择", font=FNT_S, fg="white", bg=C["btn_sel"],
                  relief="flat", cursor="hand2", padx=6,
                  command=self._browse_pip).pack(side="left", padx=2)
        tk.Button(opt, text="清除", font=FNT_S, fg=C["dim"], bg=C["card"],
                  relief="flat", cursor="hand2", padx=4,
                  command=self._clear_pip).pack(side="left", padx=1)
        self._pip_label = tk.Label(opt, text="", font=FNT_S, fg=C["dim"], bg=C["card"])
        self._pip_label.pack(side="left", padx=2)
        self.pip_size_var = tk.StringVar(value="15%")
        self.pip_opacity_var = tk.StringVar(value="3%")
        self.pip_pos_var = tk.StringVar(value="\u53f3\u4e0a")
        tk.Label(opt, text="\u5927\u5c0f", font=FNT_S, fg=C["dim"], bg=C["card"]).pack(side="left", padx=(4,0))
        ttk.Combobox(opt, textvariable=self.pip_size_var, width=4, font=FNT_S, state="readonly",
                     values=["10%","15%","20%","25%"]).pack(side="left", padx=1)
        tk.Label(opt, text="\u900f\u660e\u5ea6", font=FNT_S, fg=C["dim"], bg=C["card"]).pack(side="left", padx=(2,0))
        ttk.Combobox(opt, textvariable=self.pip_opacity_var, width=4, font=FNT_S, state="readonly",
                     values=["3%","5%","10%","15%","20%"]).pack(side="left", padx=1)
        tk.Label(opt, text="\u4f4d\u7f6e", font=FNT_S, fg=C["dim"], bg=C["card"]).pack(side="left", padx=(2,0))
        ttk.Combobox(opt, textvariable=self.pip_pos_var, width=5, font=FNT_S, state="readonly",
                     values=["\u5de6\u4e0a","\u53f3\u4e0a","\u5de6\u4e0b","\u53f3\u4e0b"]).pack(side="left", padx=1)

        self._dedup_frame = tk.Frame(self._dedup_card, bg=C["card"], padx=12, pady=4)
        self._build_custom_dedup_panel()
        self._dedup_frame.pack_forget()

        # 字幕(移至按钮行)
        # 已移至按钮行

        # ====== 开始按钮 + 字幕 + 输出目录（同一行）=====
        act_row = tk.Frame(self, bg=C["bg"])
        act_row.pack(fill="x", padx=16, pady=(8,4))
        self._out_dir_label = tk.Label(act_row, text="自动", font=FNT_S, fg=C["dim"], bg=C["bg"])
        self._out_dir_label.pack(side="right", fill="x", padx=(8,8))
        tk.Button(act_row, text="打开", font=FNT_S, fg="white", bg=C["btn_sel"],
                  relief="flat", cursor="hand2", padx=8,
                  command=self._open_output).pack(side="right", padx=(2,0))
        tk.Button(act_row, text="浏览", font=FNT_S, fg="white", bg=C["btn_sel"],
                  relief="flat", cursor="hand2", padx=8,
                  command=self._choose_output).pack(side="right", padx=(2,0))
        tk.Label(act_row, text="输出:", font=FNT_S, fg=C["dim"],
                 bg=C["bg"]).pack(side="right")
        tk.Frame(act_row, width=1, bg=C["dim"]).pack(side="right", fill="y", padx=6, pady=2)
        tk.Checkbutton(act_row, text="字幕叠加", variable=self.subtitle_var,
                       font=FNT_S, fg=C["text"], bg=C["bg"],
                       selectcolor=C["inp"], activebackground=C["bg"],
                       cursor="hand2").pack(side="right", padx=4)
        self._btn = tk.Button(act_row, text="\u25b6  开始混剪", font=FNT_B,
                         fg="white", bg=C["btn_go"],
                         activeforeground="white", relief="flat", cursor="hand2",
                         padx=16, pady=6, command=self._toggle)
        self._btn.pack(side="left")

        # ====== 日志 ======
        self._log = tk.Text(self, bg=C["inp"], fg=C["text"], font=FNT_S,
                             height=14, relief="flat", borderwidth=0, padx=8, pady=8,
                             state="disabled", wrap="word")
        self._log.pack(fill="both", expand=True, padx=16, pady=(6,8))

    def _toggle_dedup(self, e=None):
        if self._dedup_collapsed:
            if self.dedup.get() == "custom":
                self._load_dedup_custom()
                self._dedup_frame.pack(fill="x")
            self._dedup_toggle_lbl.configure(text="\u25bc")
            self._dedup_collapsed = False
        else:
            self._dedup_frame.pack_forget()
            self._dedup_toggle_lbl.configure(text="\u25b6")
            self._dedup_collapsed = True

    def _on_dedup_change(self):
        if self.dedup.get() == "custom":
            self._load_dedup_custom()
            self._dedup_frame.pack(fill="x")
            self._dedup_toggle_lbl.configure(text="\u25bc")
            self._dedup_collapsed = False
        else:
            self._dedup_frame.pack_forget()
            self._dedup_toggle_lbl.configure(text="\u25b6")
            self._dedup_collapsed = True

    def _build_custom_dedup_panel(self):
        f = self._dedup_frame
        cfg = DEDUP_CONFIG

        sec1 = tk.Frame(f, bg=C["card"])
        sec1.pack(fill="x", pady=(4, 2))
        tk.Label(sec1, text="画面", font=FNT_B, fg=C["text"], bg=C["card"]).pack(anchor="w")
        r1 = tk.Frame(sec1, bg=C["card"])
        r1.pack(fill="x", pady=2)

        self._dv_mirror = self.mirror_var
        tk.Checkbutton(r1, text="镜像翻转", variable=self._dv_mirror, font=FNT_S,
                       fg=C["text"], bg=C["card"], selectcolor=C["inp"],
                       cursor="hand2").pack(side="left", padx=(0, 16))

        self._dv_crop = tk.BooleanVar(value=cfg.get("random_crop", {}).get("enabled", True))
        tk.Checkbutton(r1, text="微裁剪", variable=self._dv_crop, font=FNT_S,
                       fg=C["text"], bg=C["card"], selectcolor=C["inp"],
                       cursor="hand2").pack(side="left", padx=(0, 16))

        self._dv_gamma = tk.BooleanVar(value=cfg.get("gamma_shift", {}).get("enabled", True))
        tk.Checkbutton(r1, text="亮度微调", variable=self._dv_gamma, font=FNT_S,
                       fg=C["text"], bg=C["card"], selectcolor=C["inp"],
                       cursor="hand2").pack(side="left", padx=(0, 16))

        self._dv_corner = tk.BooleanVar(value=cfg.get("corner_mask", {}).get("enabled", True))
        tk.Checkbutton(r1, text="四角遮罩", variable=self._dv_corner, font=FNT_S,
                       fg=C["text"], bg=C["card"], selectcolor=C["inp"],
                       cursor="hand2").pack(side="left")

        sec2 = tk.Frame(f, bg=C["card"])
        sec2.pack(fill="x", pady=(6, 2))
        tk.Label(sec2, text="速度", font=FNT_B, fg=C["text"], bg=C["card"]).pack(anchor="w")
        r2 = tk.Frame(sec2, bg=C["card"])
        r2.pack(fill="x", pady=2)

        self._dv_speed = tk.BooleanVar(value=cfg.get("variable_speed", {}).get("enabled", True))
        tk.Checkbutton(r2, text="变速", variable=self._dv_speed, font=FNT_S,
                       fg=C["text"], bg=C["card"], selectcolor=C["inp"],
                       cursor="hand2").pack(side="left", padx=(0, 12))
        tk.Label(r2, text="范围:", font=FNT_S, fg=C["dim"], bg=C["card"]).pack(side="left")
        self._dv_speed_min = tk.StringVar(value=str(cfg.get("variable_speed", {}).get("min_rate", 1.10)))
        self._dv_speed_max = tk.StringVar(value=str(cfg.get("variable_speed", {}).get("max_rate", 1.30)))
        tk.Entry(r2, textvariable=self._dv_speed_min, font=FNT_S, fg=C["text"], bg=C["inp"],
                 width=5, relief="flat").pack(side="left", padx=2)
        tk.Label(r2, text="~", font=FNT_S, fg=C["dim"], bg=C["card"]).pack(side="left")
        tk.Entry(r2, textvariable=self._dv_speed_max, font=FNT_S, fg=C["text"], bg=C["inp"],
                 width=5, relief="flat").pack(side="left", padx=2)
        tk.Label(r2, text="倍", font=FNT_S, fg=C["dim"], bg=C["card"]).pack(side="left", padx=(4, 16))

        tk.Label(r2, text="低速占比:", font=FNT_S, fg=C["dim"], bg=C["card"]).pack(side="left")
        self._dv_speed_weight = tk.IntVar(value=int(cfg.get("variable_speed", {}).get("weight_low", 0.7) * 100))
        tk.Scale(r2, from_=0, to=100, orient="horizontal", variable=self._dv_speed_weight,
                 font=FNT_S, fg=C["dim"], bg=C["card"], highlightthickness=0,
                 troughcolor=C["inp"], length=80, showvalue=True, sliderlength=12).pack(side="left")

        sec3 = tk.Frame(f, bg=C["card"])
        sec3.pack(fill="x", pady=(6, 2))
        tk.Label(sec3, text="音频", font=FNT_B, fg=C["text"], bg=C["card"]).pack(anchor="w")
        r3 = tk.Frame(sec3, bg=C["card"])
        r3.pack(fill="x", pady=2)

        self._dv_pitch = tk.BooleanVar(value=cfg.get("audio_pitch", {}).get("enabled", True))
        tk.Checkbutton(r3, text="音高微调", variable=self._dv_pitch, font=FNT_S,
                       fg=C["text"], bg=C["card"], selectcolor=C["inp"],
                       cursor="hand2").pack(side="left", padx=(0, 16))

        self._dv_reverb = tk.BooleanVar(value=cfg.get("audio_reverb", {}).get("enabled", True))
        tk.Checkbutton(r3, text="轻微混响", variable=self._dv_reverb, font=FNT_S,
                       fg=C["text"], bg=C["card"], selectcolor=C["inp"],
                       cursor="hand2").pack(side="left", padx=(0, 16))

        self._dv_noise = tk.BooleanVar(value=cfg.get("noise_fusion", {}).get("enabled", True))
        tk.Checkbutton(r3, text="白噪音融合", variable=self._dv_noise, font=FNT_S,
                       fg=C["text"], bg=C["card"], selectcolor=C["inp"],
                       cursor="hand2").pack(side="left")

        btn_f = tk.Frame(f, bg=C["card"])
        btn_f.pack(fill="x", pady=(6, 2))
        tk.Button(btn_f, text="恢复默认", font=FNT_S, fg=C["dim"], bg=C["inp"],
                  relief="flat", cursor="hand2", padx=10,
                  command=self._reset_dedup_defaults).pack(side="right", padx=2)
        tk.Button(btn_f, text="保存设置", font=FNT_S, fg="white", bg=C["btn_sel"],
                  relief="flat", cursor="hand2", padx=10,
                  command=self._save_dedup_custom).pack(side="right", padx=2)

    def _save_dedup_custom(self):
        try:
            data = {}
            if os.path.exists(SETTINGS_PATH):
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            data["dedup_custom"] = {
                "mirror": self._dv_mirror.get(),
                "random_crop": self._dv_crop.get(),
                "gamma_shift": self._dv_gamma.get(),
                "corner_mask": self._dv_corner.get(),
                "variable_speed": self._dv_speed.get(),
                "speed_min": float(self._dv_speed_min.get()),
                "speed_max": float(self._dv_speed_max.get()),
                "speed_weight_low": self._dv_speed_weight.get() / 100.0,
                "audio_pitch": self._dv_pitch.get(),
                "audio_reverb": self._dv_reverb.get(),
                "noise_fusion": self._dv_noise.get(),
            }
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._log_msg("自定义去重设置已保存")
        except Exception as e:
            self._log_msg(f"保存自定义去重失败: {e}")

    def _load_dedup_custom(self):
        try:
            if not os.path.exists(SETTINGS_PATH):
                return
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            d = data.get("dedup_custom", {})
            if not d:
                return
            self._dv_mirror.set(d.get("mirror", True))
            self._dv_crop.set(d.get("random_crop", True))
            self._dv_gamma.set(d.get("gamma_shift", True))
            self._dv_corner.set(d.get("corner_mask", True))
            self._dv_speed.set(d.get("variable_speed", True))
            self._dv_speed_min.set(str(d.get("speed_min", 1.10)))
            self._dv_speed_max.set(str(d.get("speed_max", 1.30)))
            self._dv_speed_weight.set(int(d.get("speed_weight_low", 0.7) * 100))
            self._dv_pitch.set(d.get("audio_pitch", True))
            self._dv_reverb.set(d.get("audio_reverb", True))
            self._dv_noise.set(d.get("noise_fusion", True))
        except Exception:
            pass

    def _reset_dedup_defaults(self):
        cfg = DEDUP_CONFIG
        self._dv_mirror.set(cfg.get("mirror", {}).get("enabled", True))
        self._dv_crop.set(cfg.get("random_crop", {}).get("enabled", True))
        self._dv_gamma.set(cfg.get("gamma_shift", {}).get("enabled", True))
        self._dv_corner.set(cfg.get("corner_mask", {}).get("enabled", True))
        self._dv_speed.set(cfg.get("variable_speed", {}).get("enabled", True))
        self._dv_speed_min.set(str(cfg.get("variable_speed", {}).get("min_rate", 1.10)))
        self._dv_speed_max.set(str(cfg.get("variable_speed", {}).get("max_rate", 1.30)))
        self._dv_speed_weight.set(int(cfg.get("variable_speed", {}).get("weight_low", 0.7) * 100))
        self._dv_pitch.set(cfg.get("audio_pitch", {}).get("enabled", True))
        self._dv_reverb.set(cfg.get("audio_reverb", {}).get("enabled", True))
        self._dv_noise.set(cfg.get("noise_fusion", {}).get("enabled", True))
        self._log_msg("去重参数已恢复默认")

    def _apply_dedup_custom(self):
        import config as _cfg
        try:
            d = {}
            if os.path.exists(SETTINGS_PATH):
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    d = json.load(f).get("dedup_custom", {})
            if d:
                _cfg.DEDUP_CONFIG["mirror"]["enabled"] = d.get("mirror", True)
                _cfg.DEDUP_CONFIG["random_crop"]["enabled"] = d.get("random_crop", True)
                _cfg.DEDUP_CONFIG["gamma_shift"]["enabled"] = d.get("gamma_shift", True)
                _cfg.DEDUP_CONFIG["corner_mask"]["enabled"] = d.get("corner_mask", True)
                _cfg.DEDUP_CONFIG["variable_speed"]["enabled"] = d.get("variable_speed", True)
                _cfg.DEDUP_CONFIG["variable_speed"]["min_rate"] = d.get("speed_min", 1.10)
                _cfg.DEDUP_CONFIG["variable_speed"]["max_rate"] = d.get("speed_max", 1.30)
                _cfg.DEDUP_CONFIG["variable_speed"]["weight_low"] = d.get("speed_weight_low", 0.7)
                _cfg.DEDUP_CONFIG["audio_pitch"]["enabled"] = d.get("audio_pitch", True)
                _cfg.DEDUP_CONFIG["audio_reverb"]["enabled"] = d.get("audio_reverb", True)
                _cfg.DEDUP_CONFIG["noise_fusion"]["enabled"] = d.get("noise_fusion", True)
            _cfg.DEDUP_CONFIG["mirror"]["enabled"] = self.mirror_var.get()
            self._log_msg("已加载自定义去重配置")
        except Exception as e:
            self._log_msg(f"加载自定义去重配置失败: {e}")

    def _log_msg(self, m):
        self._log.configure(state="normal")
        self._log.insert("end", m + "\n"); self._log.see("end")
        self._log.configure(state="disabled"); self.update_idletasks()

    def _add_videos(self):
        fs = filedialog.askopenfilenames(title="选择视频",
            filetypes=[("视频", "*.mp4 *.mov *.mkv *.avi *.flv *.ts"), ("所有", "*.*")])
        for f in fs:
            if f not in [v[0] for v in self._mix_videos]:
                self._mix_videos.append((f, os.path.basename(f)))
                self._listbox.insert("end", os.path.basename(f))
        self.count_label.configure(text=f"已选 {len(self._mix_videos)} 个视频")

    def _remove(self):
        s = self._listbox.curselection()
        if s: i = s[0]; self._listbox.delete(i); self._mix_videos.pop(i)
        self.count_label.configure(text=f"已选 {len(self._mix_videos)} 个视频")

    def _clear_all(self):
        self._listbox.delete(0, "end"); self._mix_videos.clear()
        self.count_label.configure(text="已选 0 个视频")

    def _choose_output(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d: self.output_dir = d; self._out_dir_label.configure(text=os.path.basename(d) or d, fg=C["text"])

    def _open_output(self):
        d = self.output_dir or (os.path.dirname(self._mix_videos[0][0]) if self._mix_videos else os.getcwd())
        try: os.startfile(d)
        except: self._log_msg(f"打开失败: {d}")

    def _browse_pip(self):
        f = filedialog.askopenfilename(title="选择画中画素材",
            filetypes=[("视频/图片", "*.mp4 *.mov *.png *.jpg *.gif"), ("所有", "*.*")])
        if f: self.pip_path = f; self._pip_label.configure(text=os.path.basename(f)[:20], fg=C["text"])

    def _clear_pip(self):
        self.pip_path = ""; self._pip_label.configure(text="", fg=C["dim"])

    def _toggle(self):
        if self._mix_worker and self._mix_worker.is_alive():
            if self._mix_cancel: self._mix_cancel.set()
            self._btn.configure(text="\u25b6  开始混剪", bg=C["btn_go"]); self._log_msg("已停止"); return
        if len(self._mix_videos) < 2:
            self._log_msg("请至少添加2个视频"); return

        try:
            from license_guard import require_feature_access
            if not require_feature_access("混剪成片", self.winfo_toplevel(), self._log_msg, refresh=False):
                return
        except Exception as e:
            self._log_msg("授权检查异常: " + str(e))
            return

        if self.dedup.get() == "custom":
            self._save_dedup_custom()
            self._apply_dedup_custom()

        self._btn.configure(text="\u25a0  停止", bg=C["btn_no"])
        self._mix_cancel = threading.Event()
        nver = int(self.num_versions_var.get())

        def run():
            try:
                first = self._mix_videos[0][0]
                out_dir = self.output_dir or os.path.dirname(first)
                os.makedirs(os.path.join(out_dir, "mix_output"), exist_ok=True)
                cat = self.main_category_var.get()
                for vi in range(nver):
                    if self._mix_cancel and self._mix_cancel.is_set(): break
                    try:
                        from license_guard import require_feature_access
                        if not require_feature_access(
                            "混剪成片", None, self._log_msg, show_dialog=False, refresh=False
                        ):
                            break
                    except Exception as e:
                        self._log_msg("授权检查异常: " + str(e))
                        break
                    suf = "" if nver == 1 else f"_v{vi+1}"
                    out = os.path.join(out_dir, "mix_output", f"mix_{time.strftime('%H%M%S')}{suf}.mp4")
                    self._log_msg(f"\n=== 版本 {vi+1}/{nver} ===")
                    import random; random.seed(vi * 7919)
                    result = process_video_mix(
                        [v[0] for v in self._mix_videos], output_path=out,
                        dedup_preset=self.dedup.get(), subtitle_overlay=self.subtitle_var.get(),
                        log_fn=self._log_msg, cancel_event=self._mix_cancel,
                        force_category=None if cat=="自动检测" else cat,
                        focus_hint=None if self.ai_focus_var.get()=="自动" else self.ai_focus_var.get(),
                        target_duration=int(self.duration_var.get().replace("s","")),
                        pip_path=self.pip_path or "",
                        pip_size=int(self.pip_size_var.get().replace("%",""))/100,
                        pip_opacity=int(self.pip_opacity_var.get().replace("%",""))/100,
                        pip_pos=self.pip_pos_var.get(),
                        smart_crop_enabled=self.smart_crop_var.get(),
                        crop_level={"\u8f7b":"light","\u4e2d":"medium","\u91cd":"heavy"}.get(self.crop_level_var.get(),"medium"),
                        ken_burns_enabled=self.ken_burns_var.get(),
                        mirror_enabled=self.mirror_var.get(),
                        kb_intensity=self.kb_intensity_var.get()
                    )
                    if result:
                        sz = os.path.getsize(out)/1024/1024
                        self._log_msg(f"\u2713 版本{vi+1}: {sz:.1f}MB\n{out}")
                        try:
                            from license_guard import consume_trial_after_success
                            consume_trial_after_success("混剪成片", root=None, log_fn=self._log_msg)
                        except Exception:
                            pass
                    else:
                        self._log_msg(f"\u2717 版本{vi+1} 失败")
            except Exception as e:
                self._log_msg(f"\u2717 {e}")
            finally:
                self._btn.configure(text="\u25b6  开始混剪", bg=C["btn_go"])
        self._mix_worker = threading.Thread(target=run, daemon=True)
        self._mix_worker.start()
