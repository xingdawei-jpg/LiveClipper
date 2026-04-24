"""
直播带货切片工具 v3.0 - GUI
- 只需选择视频，自动语音识别
- 批量处理
- 字幕叠加开关
"""

# PyInstaller 隐式导入补全 — 确保标准库被打包
import email, email.message, email.policy
import html, html.parser
import http, http.server, http.client
import socketserver, mimetypes, calendar, fnmatch
import shutil, random, math, argparse, configparser
import logging, logging.handlers
import pathlib, threading, concurrent, concurrent.futures
import importlib, importlib.resources, importlib.metadata
import typing, dataclasses
import urllib, urllib.request, urllib.error, urllib.parse
import ssl, socket, platform, uuid, json, hashlib, time, re
import subprocess, traceback, warnings, tempfile, struct
import collections, itertools, functools, datetime, csv, glob
import base64, binascii, codecs, locale
import xml.etree.ElementTree

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tkinter as tk
from tkinter import filedialog, messagebox
from updater import check_and_prompt_update, _get_installed_version
import tkinter.ttk as ttk
import queue

from config import FFMPEG_PATH, VIDEO_CONFIG, DEDUP_PRESET, DEDUP_CONFIG, SUBTITLE_OVERLAY
from cutter_logic import process_video, process_video_multi
from license_client import check_activation, activate_with_code, check_trial, consume_trial_use, deactivate_device
# 样式
C = {
    "bg":"#1C1C1E","card":"#2C2C3A","text":"#E5E5EA","dim":"#9898A8",
    "ok":"#30D158","warn":"#FF9F0A","err":"#FF453A","inp":"#232338",
    "bar_bg":"#3A3A4D","bar":"#0A84FF","btn_go":"#30D158","btn_go2":"#28A745",
    "btn_no":"#FF453A","btn_sel":"#0A84FF","btn_del":"#63687A","card_border":"#3A3A52",
}
FNT=("Segoe UI",10); FNT_B=("Segoe UI",10,"bold"); FNT_T=("Segoe UI",20,"bold")
FNT_S=("Segoe UI",9); FNT_L=("Consolas",9)
DEDUP_CLR={"none":"#63687A","light":"#FFD60A","medium":"#0A84FF","heavy":"#A78BFA","custom":"#FF6B6B"}

# AI 预设
AI_PRESETS = {
    "自定义":  {"base_url": "", "model": ""},
    "DeepSeek V3": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "豆包 Pro":  {"base_url": "https://ark.cn-beijing.volces.com/api/v3", "model": "doubao-1-5-pro-32k"},
    "通义千问":  {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "GPT-4o":    {"base_url": "https://api.openai.com/v1", "model": "gpt-4o"},
    "GLM-4":     {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4"},
    "DeepSeek R1": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-reasoner"},
    "豆包Seed": {"base_url": "https://ark.cn-beijing.volces.com/api/v3", "model": "doubao-seed-2.0-pro-260215"},
}

# 去重等级说明
DEDUP_TIPS = {
    "none":   "保留原画面，不做任何去重处理",
    "light":  "仅调整帧率+轻微调色，保留原画面完整性",
    "medium": "中度画面+音频处理，平衡质量与去重效果",
    "heavy":  "全维度画面+音频处理，过审概率最高，画面改动较大",
    "custom": "使用自定义去重参数",
}


class ToolTip:
    """简易悬浮提示"""
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, event=None):
        x = self.widget.winfo_rootx() + 30
        y = self.widget.winfo_rooty() + 25
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, bg="#2C2C3A", fg="#E5E5EA",
                 relief="solid", bd=1, font=FNT_S, padx=8, pady=4,
                 wraplength=250, justify="left").pack()

    def hide(self, event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class Worker(threading.Thread):
    def __init__(self, cb, **kw):
        super().__init__(daemon=True)
        self.cb = cb; self.kw = kw
    def run(self):
        try:
            ok = process_video(log_fn=self.cb, **self.kw)
            self.cb("__DONE__" if ok else "__FAIL__")
        except Exception as e:
            import traceback; self.cb(f"__ERR__{e}\n{traceback.format_exc()}")
        finally:
            self.cb("__END__")



def _friendly_error(err_msg):
    """将技术错误信息翻译为用户友好的提示"""
    err_lower = err_msg.lower()

    # FFmpeg 相关
    if "ffmpeg" in err_lower or "ffprobe" in err_lower:
        if "not found" in err_lower or "找不到" in err_lower or "[winerror 2]" in err_lower:
            return "FFmpeg 未找到，请确认 ffmpeg.exe 在工具目录下"
        if "permission" in err_lower or "denied" in err_lower:
            return "FFmpeg 被占用或权限不足，请关闭其他视频软件后重试"
        return f"视频处理出错，请重试（如持续出现请联系微信 LeyiDeco）"

    # 网络/API 相关
    if "timeout" in err_lower or "timed out" in err_lower:
        return "网络连接超时，请检查网络后重试"
    if "connection" in err_lower or "connect" in err_lower:
        return "无法连接服务器，请检查网络连接"
    if "api_key" in err_lower or "unauthorized" in err_lower or "401" in err_lower:
        return "AI 供应商 API Key 无效，请检查设置"
    if "quota" in err_lower or "429" in err_lower or "rate" in err_lower:
        return "AI 接口调用额度不足，请稍后再试或更换 API Key"
    if "balance" in err_lower or "insufficient" in err_lower:
        return "AI 接口余额不足，请充值后重试"

    # 文件相关
    if "no such file" in err_lower or "找不到" in err_lower:
        return "视频文件不存在或已被移动，请重新选择"
    if "permission" in err_lower or "denied" in err_lower:
        if "output" in err_lower or "write" in err_lower:
            return "输出目录无法写入，请更换输出目录或关闭占用该文件的程序"
        return "文件访问被拒绝，请检查文件权限"
    if "disk" in err_lower or "space" in err_lower or "enospc" in err_lower:
        return "磁盘空间不足，请清理后重试"
    if "codec" in err_lower or "decode" in err_lower:
        return "视频格式不支持，请尝试转换为 MP4 后重试"

    # ASR 相关
    if "whisper" in err_lower or "asr" in err_lower:
        return "语音识别失败，可尝试提供 SRT 字幕文件"

    # 通用
    if len(err_msg) > 100:
        return "处理过程中出错，如需帮助请联系微信 LeyiDeco"
    return f"处理出错: {err_msg}（如需帮助请联系微信 LeyiDeco）"


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(f"直播带货切片工具 v{_get_installed_version()}")
        self.root.geometry("800x820")
        self.root.configure(bg=C["bg"])
        self.root.minsize(550, 650)
        self.videos = []  # [(path, name), ...]
        self.worker = None
        self._cancel_event = None
        self._log_queue = queue.Queue()
        self._build()
        self._poll_queue()  # 启动队列轮询
        # 启动时恢复AI和ASR启用状态
        self.root.after(100, self._restore_toggle_states)
        self._log(f"[v{_get_installed_version()}] GUI 已启动 {__import__('time').strftime('%H:%M:%S')}")

    def _restore_toggle_states(self):
        """启动时恢复所有设置到UI（不触发自动保存）"""
        try:
            from ai_clipper import load_settings
            s = load_settings()

            # --- 恢复 AI 设置 ---
            if s.get("api_key"):
                self.ai_key_var.set(s["api_key"])
            if s.get("base_url"):
                self.ai_url_var.set(s["base_url"])
            if s.get("model"):
                self.ai_model_var.set(s["model"])

            # AI 预设（优先用保存的名称，否则按 url/model 匹配）
            ai_matched = s.get("ai_preset", "")
            if not ai_matched or ai_matched not in AI_PRESETS:
                ai_matched = "\u81ea\u5b9a\u4e49"
                for name, cfg in AI_PRESETS.items():
                    if name == "\u81ea\u5b9a\u4e49":
                        continue
                    if (s.get("base_url","") == cfg["base_url"] and
                        s.get("model","") == cfg["model"]):
                        ai_matched = name
                        break
            self.ai_preset_var.set(ai_matched)
            # Restore AI偏好
            _focus = s.get("ai_focus", "自动")
            if hasattr(self, "ai_focus_var"):
                self.ai_focus_var.set(_focus)

            # AI 启用状态（保持收缩，只更新按钮外观）
            if s.get("enabled"):
                self.ai_enabled_var.set(True)
                # 只更新按钮文字，不展开面板
                self.ai_toggle.configure(text="✅ 启用", fg="#4caf50")

            # --- 恢复 ASR 设置 ---
            if s.get("asr_api_key"):
                self.asr_key_var.set(s["asr_api_key"])
            if s.get("asr_base_url"):
                self.asr_url_var.set(s["asr_base_url"])
            if s.get("asr_model"):
                self.asr_model_var.set(s["asr_model"])
            # 阿里云 ASR 配置
            if s.get("aliyun_api_key"):
                self.aliyun_api_key_var.set(s["aliyun_api_key"])
            if s.get("aliyun_oss_ak"):
                self.aliyun_oss_ak_var.set(s["aliyun_oss_ak"])
            if s.get("aliyun_oss_sk"):
                self.aliyun_oss_sk_var.set(s["aliyun_oss_sk"])
            if s.get("aliyun_bucket"):
                self.aliyun_bucket_var.set(s["aliyun_bucket"])
            if s.get("aliyun_endpoint"):
                self.aliyun_endpoint_var.set(s["aliyun_endpoint"])
            if s.get("volc_app_id"):
                self.volc_app_id_var.set(s["volc_app_id"])
            if s.get("volc_access_token"):
                self.volc_token_var.set(s["volc_access_token"])
            if s.get("volc_tos_ak"):
                self.volc_tos_ak_var.set(s["volc_tos_ak"])
            if s.get("volc_tos_sk"):
                self.volc_tos_sk_var.set(s["volc_tos_sk"])
            if s.get("volc_bucket"):
                self.volc_bucket_var.set(s["volc_bucket"])
            if "whisper_model" in s:
                self._whisper_model_var.set(s["whisper_model"])

            # ASR 预设
            asr_matched = s.get("asr_preset", "") or s.get("asr_provider", "")
            if asr_matched:
                self.asr_preset_var.set(asr_matched)

            # ASR 启用状态（保持收缩，只更新按钮外观）
            asr_on = bool(s.get("asr_enabled", False))
            if asr_on:
                self.asr_enabled_var.set(True)
                # 只更新按钮文字，不展开面板
                self.asr_toggle.configure(text="✓ 启用", fg="#4caf50")

            # --- 恢复完成，启用保存 ---
            self._init_done = True
            self._save_ai()  # 确保文件包含所有恢复的值

        except Exception:
            import traceback
            traceback.print_exc()
            self._init_done = True  # 即使出错也允许保存

    def _build(self):
        m = tk.Frame(self.root, bg=C["bg"])
        m.pack(fill="both", expand=True)

        # 标题
        hdr = tk.Frame(m, bg=C["bg"])
        hdr.pack(fill="x", padx=16, pady=(16,4))
        tk.Label(hdr, text="直播带货切片工具", font=FNT_T,
                 fg=C["text"], bg=C["bg"]).pack(side="left")
        tk.Button(hdr, text="🔓 解绑", font=FNT_S, fg=C["dim"], bg=C["inp"],
                  relief="flat", cursor="hand2", padx=10, pady=2,
                  command=self._deactivate_device).pack(side="right")
        tk.Button(hdr, text="💬 反馈", font=FNT_S, fg=C["dim"], bg=C["inp"],
                  relief="flat", cursor="hand2", padx=10, pady=2,
                  command=self._show_feedback).pack(side="right")
        tk.Label(hdr, text=f"选择视频 → AI智能选片 → 自动剪辑+字幕  ·  v{_get_installed_version()}",
                 font=FNT_S, fg=C["dim"], bg=C["bg"]).pack(side="left", padx=(12,0))

        # 视频选择
        vf = tk.Frame(m, bg=C["card"], padx=12, pady=10, highlightbackground=C["card_border"], highlightthickness=1)
        vf.pack(fill="x", padx=16, pady=(2,6))
        top = tk.Frame(vf, bg=C["card"]); top.pack(fill="x")
        tk.Label(top, text="直播视频", font=FNT_B, fg=C["text"],
                 bg=C["card"]).pack(side="left")
        tk.Button(top, text="+ 添加视频", font=FNT_S, fg="white", bg=C["btn_sel"],
                  relief="flat", cursor="hand2", padx=10,
                  command=self._add_videos).pack(side="right")
        tk.Button(top, text="删除", font=FNT_S, fg="white", bg=C["btn_del"],
                  relief="flat", cursor="hand2", padx=8,
                  command=self._del_selected).pack(side="right", padx=(0,4))
        tk.Button(top, text="清空", font=FNT_S, fg="white", bg=C["btn_del"],
                  relief="flat", cursor="hand2", padx=8,
                  command=self._clear_videos).pack(side="right", padx=(0,4))

        # 视频列表
        lf = tk.Frame(vf, bg=C["inp"])
        lf.pack(fill="x", pady=(8,0))
        self.video_listbox = tk.Listbox(lf, font=FNT_S, bg=C["inp"], fg=C["text"],
                                         selectbackground=C["btn_sel"], height=4,
                                         relief="flat", bd=0)
        sb = tk.Scrollbar(lf, command=self.video_listbox.yview, bg=C["card"])
        self.video_listbox.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.video_listbox.pack(side="left", fill="both", expand=True)
        self.video_listbox.bind("<Delete>", lambda e: self._del_selected())
        self.count_label = tk.Label(vf, text="已选 0 个视频", font=FNT_S,
                                    fg=C["dim"], bg=C["card"])
        self.count_label.pack(side="right", anchor="e", pady=(4,0))
        # 主推类目
        tk.Label(vf, text="主推类目:", font=FNT_S, fg=C["dim"],
                 bg=C["card"]).pack(side="left", padx=(4,2), pady=(4,0))
        self.main_category_var = tk.StringVar(value="自动检测")
        self.main_category_combo = ttk.Combobox(vf, textvariable=self.main_category_var,
                                          values=["自动检测","上衣","裤子","裙子","外套","套装","鞋子","配饰"],
                                          width=10, font=FNT_S, state="readonly")
        self.main_category_combo.pack(side="left", pady=(4,0))
        # 剪辑数量
        tk.Label(vf, text="  剪辑数量:", font=FNT_S, fg=C["dim"],
                 bg=C["card"]).pack(side="left", padx=(4,2), pady=(4,0))
        self.num_versions_var = tk.StringVar(value="1")
        ttk.Combobox(vf, textvariable=self.num_versions_var,
                     values=["1", "2", "3"], width=3,
                     font=FNT_S, state="readonly").pack(side="left", pady=(4,0))

        # AI偏好
        tk.Label(vf, text="  AI偏好:", font=FNT_S, fg=C["dim"],
                 bg=C["card"]).pack(side="left", padx=(4,2), pady=(4,0))
        self.ai_focus_var = tk.StringVar(value="自动")
        self.ai_focus_combo = ttk.Combobox(vf, textvariable=self.ai_focus_var,
                     values=["自动","面料质感","颜色氛围","版型显瘦","穿着场景","性价比","紧迫稀缺","情绪感染","流行趋势"],
                     width=8, font=FNT_S, state="readonly")
        self.ai_focus_combo.pack(side="left", pady=(4,0))

        # 画中画已移到去重行
        # SRT 字幕（藏到去重面板里，不单独显示）

        # 去重 + 字幕（可折叠）
        dedup_card = tk.Frame(m, bg=C["card"], padx=12, pady=6)
        dedup_card.pack(fill="x", padx=16, pady=2)
        opt = tk.Frame(dedup_card, bg=C["card"])
        opt.pack(fill="x")

        self._dedup_collapsed = True
        self._dedup_toggle_lbl = tk.Label(opt, text="▶", font=FNT_S,
                                         fg=C["btn_sel"], bg=C["inp"], cursor="hand2",
                                         padx=6, pady=2)
        self._dedup_toggle_lbl.pack(side="left")
        self._dedup_toggle_lbl.bind("<Button-1>", self._toggle_dedup_collapse)

        # 去重
        tk.Label(opt, text="去重:", font=FNT_B, fg=C["text"],
                 bg=C["card"]).pack(side="left")
        self.dedup = tk.StringVar(value=DEDUP_PRESET)
        for val, txt in [("none","不去重"),("light","轻微"),("medium","中度"),("heavy","重度"),("custom","自定义")]:
            fg = DEDUP_CLR[val]
            rb = tk.Radiobutton(opt, text=txt, variable=self.dedup, value=val,
                           font=FNT_S, fg=fg, bg=C["card"], selectcolor=C["inp"],
                           activebackground=C["card"], activeforeground=fg,
                           indicatoron=0, padx=6, pady=2, relief="flat", bd=2,
                           cursor="hand2", command=self._on_dedup_change)
            rb.pack(side="left", padx=1)
            ToolTip(rb, DEDUP_TIPS[val])

        # 画中画（与去重同行，| 分隔）
        # Smart Crop 智能裁切（独立于去重）
        tk.Frame(opt, width=1, bg=C["dim"]).pack(side="left", fill="y", padx=6, pady=2)
        tk.Label(opt, text="🎬裁切:", font=FNT_S, fg=C["text"], bg=C["card"]).pack(side="left")
        self.smart_crop_var = tk.BooleanVar(value=True)
        _sc_cb = tk.Checkbutton(opt, text="开", variable=self.smart_crop_var,
                        font=FNT_S, fg=C["btn_sel"], bg=C["card"], selectcolor=C["inp"],
                        activebackground=C["card"], cursor="hand2")
        _sc_cb.pack(side="left", padx=1)
        self.crop_level_var = tk.StringVar(value="中")
        _sc_combo = ttk.Combobox(opt, textvariable=self.crop_level_var,
                         values=["轻", "中", "重"], width=2,
                         font=FNT_S, state="readonly")
        _sc_combo.pack(side="left", padx=1)

        tk.Frame(opt, width=1, bg=C["dim"]).pack(side="left", fill="y", padx=6, pady=2)
        tk.Label(opt, text="🎥缩放:", font=FNT_S, fg=C["text"], bg=C["card"]).pack(side="left")
        self.ken_burns_var = tk.BooleanVar(value=True)
        _kb_cb = tk.Checkbutton(opt, text="开", variable=self.ken_burns_var,
                        font=FNT_S, fg=C["btn_sel"], bg=C["card"], selectcolor=C["inp"],
                        activebackground=C["card"], cursor="hand2")
        _kb_cb.pack(side="left", padx=1)

        tk.Frame(opt, width=1, bg=C["dim"]).pack(side="left", fill="y", padx=6, pady=2)
        tk.Label(opt, text="画中画:", font=FNT_S, fg=C["text"], bg=C["card"]).pack(side="left")
        self.pip_var = tk.StringVar(value="留空=无")
        self.pip_path = ""
        tk.Button(opt, text="选择", font=FNT_S, fg="white", bg=C["btn_sel"],
                  relief="flat", cursor="hand2", padx=6,
                  command=self._browse_pip).pack(side="left", padx=2)
        tk.Button(opt, text="清除", font=FNT_S, fg=C["dim"], bg=C["card"],
                  relief="flat", cursor="hand2", padx=4,
                  command=self._clear_pip).pack(side="left", padx=1)
        self.pip_path_label = tk.Label(opt, textvariable=self.pip_var, font=FNT_S, fg=C["dim"],
                 bg=C["card"])
        self.pip_path_label.pack(side="left", padx=4)
        # PIP配置（选择文件后显示）
        self.pip_size_var = tk.StringVar(value="15%")
        self.pip_opacity_var = tk.StringVar(value="3%")
        self.pip_pos_var = tk.StringVar(value="右下")
        self._pip_cfg_widgets = []
        for _lbl, _var, _vals, _w in [
            ("大小", self.pip_size_var, ["10%","15%","20%","25%","30%","50%","100%"], 3),
            ("透明度", self.pip_opacity_var, ["1%","3%","5%","10%","20%","50%","100%"], 4),
            ("位置", self.pip_pos_var, ["左上","右上","左下","右下"], 3),
        ]:
            _cb = ttk.Combobox(opt, textvariable=_var, values=_vals, width=_w,
                               font=FNT_S, state="readonly")
            _cb.pack(side="left")
            self._pip_cfg_widgets.append(_cb)
            _l = tk.Label(opt, text=f" {_lbl}:", font=FNT_S, fg=C["dim"], bg=C["card"])
            _l.pack(side="left")
            self._pip_cfg_widgets.append(_l)
        # 初始隐藏PIP配置（选了文件后显示）
        for _w in self._pip_cfg_widgets:
            _w.pack_forget()
        # 字幕叠加开关（移到输出行）

        # 自定义去重面板
        self._dedup_frame = tk.Frame(dedup_card, bg=C["card"], padx=12, pady=4)
        self._build_custom_dedup_panel()
        self._dedup_frame.pack_forget()

        # 品类选择（已移到视频列表左下角，此处隐藏）
        # cat_f = tk.Frame(m, bg=C["card"], padx=12, pady=8)
        # tk.Label(cat_f, text="主推品类", font=FNT_B, fg=C["text"],
        #          bg=C["card"]).pack(side="left")
        # 已移到视频列表左下角
        self.category_var = tk.StringVar(value="自动检测")
        cat_options = ["自动检测", "上衣", "裤子", "裙子", "外套", "套装", "鞋子"]
        # cat_menu = tk.OptionMenu(cat_f, self.category_var, *cat_options)
        # cat_menu.configure(font=FNT_S, fg=C["text"], bg=C["inp"],
        # cat_menu["menu"].configure(font=FNT_S, fg=C["text"], bg=C["card"],
        # cat_menu.pack(side="left", padx=(8,0))
        # tk.Label(cat_f, text="多品混播时手动指定主推品类，避免品类误判", font=FNT_S,
        #          fg=C["dim"], bg=C["card"]).pack(side="left", padx=(12,0))

        # 输出目录（移到按钮行，不单独占行）
        self.output_dir = ""
        self.output_var = tk.StringVar(value="默认: output/")
        self.ai_enabled_var = tk.BooleanVar(value=False)
        # AI 设置（可折叠）
        self.ai_frame = tk.Frame(m, bg=C["card"], padx=12, pady=6)
        self.ai_frame.pack(fill="x", padx=16, pady=2)
        ai_hdr = tk.Frame(self.ai_frame, bg=C["card"])
        ai_hdr.pack(fill="x")
        self._ai_collapsed = True
        self._init_done = False  # block auto-save until startup restoration completes
        self._ai_toggle_lbl = tk.Label(ai_hdr, text="▶", font=FNT_S,
                                     fg=C["btn_sel"], bg=C["inp"], cursor="hand2",
                                     padx=6, pady=2)
        self._ai_toggle_lbl.pack(side="left")
        self._ai_toggle_lbl.bind("<Button-1>", self._toggle_ai_collapse)
        tk.Label(ai_hdr, text="🤖 AI 智能选片（可选）", font=FNT_B, fg=C["text"],
             bg=C["card"]).pack(side="left")
        self.ai_toggle = tk.Button(ai_hdr, text="启用", font=FNT_S,
              fg="#4fc3f7", bg=C["card"], relief="flat", cursor="hand2", padx=6,
              command=self._toggle_ai_toggle)
        self.ai_toggle.pack(side="left", padx=(8,0))
        tk.Button(ai_hdr, text="💾 保存", font=FNT_S, fg="white", bg=C["btn_sel"],
              relief="flat", cursor="hand2", padx=8,
              command=self._save_ai).pack(side="left", padx=(8,0))

        # 关键词管理按钮（始终可见，不依赖AI面板展开）
        tk.Button(ai_hdr, text="📝 关键词管理", font=FNT_S, fg="white", bg="#5b21b6",
              relief="flat", cursor="hand2", padx=10,
              command=self._open_keyword_manager).pack(side="right")

        # AI 设置内容（默认隐藏）
        self.ai_detail = tk.Frame(self.ai_frame, bg=C["card"])

        # 供应商预设
        ai_row0 = tk.Frame(self.ai_detail, bg=C["card"])
        ai_row0.pack(fill="x", pady=(4,0))
        tk.Label(ai_row0, text="供应商预设", font=FNT_S, fg=C["text"],
             bg=C["card"], width=10).pack(side="left")
        self.ai_preset_var = tk.StringVar(value="自定义")
        preset_menu = tk.OptionMenu(ai_row0, self.ai_preset_var, *AI_PRESETS.keys(),
                                 command=self._on_preset_change)
        preset_menu.configure(font=FNT_S, fg=C["text"], bg=C["inp"],
                          activebackground=C["card"], activeforeground=C["text"],
                          highlightthickness=0, relief="flat", bd=2)
        preset_menu["menu"].configure(font=FNT_S, fg=C["text"], bg=C["card"],
                                  activebackground=C["btn_sel"], activeforeground="white",
                                  relief="flat")
        preset_menu.pack(side="left", padx=(4,0))

        # API Key（密码框 + 小眼睛）
        ai_row1 = tk.Frame(self.ai_detail, bg=C["card"])
        ai_row1.pack(fill="x", pady=(4,0))
        tk.Label(ai_row1, text="API Key", font=FNT_S, fg=C["text"],
             bg=C["card"], width=10).pack(side="left")
        self.ai_key_var = tk.StringVar()
        self._key_visible = False
        self.ai_key_entry = tk.Entry(ai_row1, textvariable=self.ai_key_var,
                                  font=("Consolas", 9), fg=C["text"], bg=C["inp"],
                                  show="*", relief="flat", highlightthickness=1,
                                  highlightbackground=C["inp"], highlightcolor=C["btn_sel"])
        self.ai_key_entry.pack(side="left", fill="x", expand=True, padx=(4,0))
        tk.Label(ai_row1, text="📑 DeepSeek: platform.deepseek.com/api_keys", font=("Arial", 8), fg=C["dim"], bg=C["card"]).pack(side="right", padx=(8,0))
        tk.Button(ai_row1, text="✓测试", font=FNT_S, fg=C["ok"], bg=C["card"],
              relief="flat", cursor="hand2", padx=6, pady=0,
              command=self._test_ai_connection).pack(side="right", padx=(2,0))
        self.ai_key_entry.bind("<FocusIn>", lambda e: self.ai_key_entry.configure(highlightbackground=C["btn_sel"]))
        self.ai_key_entry.bind("<FocusOut>", lambda e: (self.ai_key_entry.configure(highlightbackground=C["inp"])))
        tk.Button(ai_row1, text="👁", font=FNT_S, fg=C["dim"], bg=C["card"],
              relief="flat", cursor="hand2", padx=4, pady=0,
              command=self._toggle_key_vis).pack(side="right")

        # Base URL（聚焦边框变色）
        ai_row2 = tk.Frame(self.ai_detail, bg=C["card"])
        ai_row2.pack(fill="x", pady=(2,0))
        tk.Label(ai_row2, text="Base URL", font=FNT_S, fg=C["text"],
             bg=C["card"], width=10).pack(side="left")
        self.ai_url_var = tk.StringVar()
        self.ai_url_entry = tk.Entry(ai_row2, textvariable=self.ai_url_var,
              font=("Consolas", 9), fg=C["text"], bg=C["inp"],
              relief="flat", highlightthickness=1,
              highlightbackground=C["inp"], highlightcolor=C["btn_sel"])
        self.ai_url_entry.pack(side="left", fill="x", expand=True, padx=(4,0))
        self.ai_url_entry.bind("<FocusIn>", lambda e: self.ai_url_entry.configure(highlightbackground=C["btn_sel"]))
        self.ai_url_entry.bind("<FocusOut>", lambda e: (self.ai_url_entry.configure(highlightbackground=C["inp"])))

        # 模型（聚焦边框变色）
        ai_row3 = tk.Frame(self.ai_detail, bg=C["card"])
        ai_row3.pack(fill="x", pady=(2,0))
        tk.Label(ai_row3, text="模型", font=FNT_S, fg=C["text"],
             bg=C["card"], width=10).pack(side="left")
        self.ai_model_var = tk.StringVar()
        self.ai_model_entry = tk.Entry(ai_row3, textvariable=self.ai_model_var,
              font=("Consolas", 9), fg=C["text"], bg=C["inp"],
              relief="flat", highlightthickness=1,
              highlightbackground=C["inp"], highlightcolor=C["btn_sel"])
        self.ai_model_entry.pack(side="left", fill="x", expand=True, padx=(4,0))
        self.ai_model_entry.bind("<FocusIn>", lambda e: self.ai_model_entry.configure(highlightbackground=C["btn_sel"]))
        self.ai_model_entry.bind("<FocusOut>", lambda e: (self.ai_model_entry.configure(highlightbackground=C["inp"])))

        # 默认隐藏 AI 详情和云端识别详情
        self.ai_detail.pack_forget()

        # ========== 云端ASR（独立配置区） ==========
        asr_card = tk.Frame(m, bg=C["card"], padx=12, pady=10)
        asr_card.pack(fill="x", padx=16, pady=2)
        asr_hdr = tk.Frame(asr_card, bg=C["card"])
        asr_hdr.pack(fill="x")
        self._asr_collapsed = True
        self._asr_toggle_lbl = tk.Label(asr_hdr, text="▶", font=FNT_S,
                                     fg=C["btn_sel"], bg=C["inp"], cursor="hand2",
                                     padx=6, pady=2)
        self._asr_toggle_lbl.pack(side="left")
        self._asr_toggle_lbl.bind("<Button-1>", self._toggle_asr_collapse)
        self.asr_enabled_var = tk.BooleanVar(value=False)
        tk.Label(asr_hdr, text="☁️ 云端ASR（替代 Whisper）", font=FNT_S, fg="#81c784", bg=C["card"],
                 anchor="w").pack(side="left")
        self.asr_toggle = tk.Button(asr_hdr, text="启用", font=FNT_S,
              fg="#4fc3f7", bg=C["card"], relief="flat", cursor="hand2", padx=6,
              command=self._toggle_asr_toggle)
        self.asr_toggle.pack(side="left", padx=(8,0))
        tk.Button(asr_hdr, text="💾 保存", font=FNT_S, fg="white", bg=C["btn_sel"],
              relief="flat", cursor="hand2", padx=8,
              command=self._save_ai).pack(side="left", padx=(8,0))
        # Whisper模型选择
        tk.Frame(asr_hdr, width=1, bg=C["dim"]).pack(side="left", fill="y", padx=8, pady=2)
        tk.Label(asr_hdr, text="Whisper:", font=FNT_S, fg=C["dim"], bg=C["card"]).pack(side="left")
        self._whisper_model_var = tk.StringVar(value="small")
        wm_combo = ttk.Combobox(asr_hdr, textvariable=self._whisper_model_var,
                                values=["small", "medium"],
                                width=7, font=FNT_S, state="readonly")
        wm_combo.pack(side="left", padx=2)
        wm_combo.bind("<<ComboboxSelected>>", lambda e: self._save_ai())
        # SRT 字幕（与云端ASR互斥）
        tk.Frame(asr_hdr, width=1, bg=C["dim"]).pack(side="left", fill="y", padx=8, pady=2)
        self.srt_var = tk.StringVar(value="留空 = 自动语音识别")
        self.srt_path = ""
        tk.Label(asr_hdr, text="SRT:", font=FNT_S, fg=C["dim"], bg=C["card"]).pack(side="left")
        tk.Label(asr_hdr, textvariable=self.srt_var, font=FNT_S, fg=C["dim"],
                 bg=C["card"], width=18, anchor="w").pack(side="left", padx=4)
        tk.Button(asr_hdr, text="浏览", font=FNT_S, fg="white", bg=C["btn_sel"],
                  relief="flat", cursor="hand2", padx=6,
                  command=self._browse_srt).pack(side="left", padx=2)
        tk.Button(asr_hdr, text="清除", font=FNT_S, fg=C["dim"], bg=C["card"],
                  relief="flat", cursor="hand2", padx=4,
                  command=self._clear_srt).pack(side="left")


        # ASR预设下拉
        self.asr_preset_row = tk.Frame(asr_card, bg=C["card"])
        self.asr_preset_row.pack(fill="x", pady=(4,0))
        tk.Label(self.asr_preset_row, text="ASR预设", font=FNT_S, fg=C["text"],
             bg=C["card"], width=10).pack(side="left")
        self.asr_preset_var = tk.StringVar(value="自定义")
        asr_preset_menu = tk.OptionMenu(self.asr_preset_row, self.asr_preset_var,
                                 *["阿里云", "火山引擎", "自定义"],
                                 command=self._on_asr_preset_change)
        asr_preset_menu.configure(font=FNT_S, fg=C["text"], bg=C["inp"],
                          activebackground=C["card"], activeforeground=C["text"],
                          highlightthickness=0, relief="flat", bd=2)
        asr_preset_menu["menu"].configure(font=FNT_S, fg=C["text"], bg=C["card"],
                                 activebackground=C["btn_sel"], activeforeground="white",
                                 relief="flat")
        asr_preset_menu.pack(side="left", padx=(4,0))
        tk.Label(self.asr_preset_row, text="预设只是快捷填充，所有字段均可自由修改", font=FNT_S,
             fg=C["dim"], bg=C["card"]).pack(side="left", padx=(12,0))

        # --- 阿里云/自定义 字段 ---
        self.asr_fields = tk.Frame(asr_card, bg=C["card"])

        _ar1 = tk.Frame(self.asr_fields, bg=C["card"])
        _ar1.pack(fill="x", pady=(2,0))
        tk.Label(_ar1, text="ASR Key", font=FNT_S, fg=C["text"],
             bg=C["card"], width=10).pack(side="left")
        self.asr_key_var = tk.StringVar()
        tk.Entry(_ar1, textvariable=self.asr_key_var,
              font=("Consolas", 9), fg=C["text"], bg=C["inp"],
              show="*", relief="flat").pack(side="left", fill="x", expand=True, padx=(4,0))

        _ar2 = tk.Frame(self.asr_fields, bg=C["card"])
        _ar2.pack(fill="x", pady=(2,0))
        tk.Label(_ar2, text="ASR URL", font=FNT_S, fg=C["text"],
             bg=C["card"], width=10).pack(side="left")
        self.asr_url_var = tk.StringVar(value="https://dashscope.aliyuncs.com")
        tk.Entry(_ar2, textvariable=self.asr_url_var,
              font=("Consolas", 9), fg=C["text"], bg=C["inp"],
              relief="flat").pack(side="left", fill="x", expand=True, padx=(4,0))

        _ar3 = tk.Frame(self.asr_fields, bg=C["card"])
        _ar3.pack(fill="x", pady=(2,0))
        tk.Label(_ar3, text="ASR模型", font=FNT_S, fg=C["text"],
             bg=C["card"], width=10).pack(side="left")
        self.asr_model_var = tk.StringVar(value="paraformer-v2")
        self.asr_model_combo = ttk.Combobox(
        _ar3, textvariable=self.asr_model_var,
        values=[
        "paraformer-v2",
        "paraformer-v1",
        "whisper-large-v3",
        ],
        font=("Consolas", 9),
        )
        self.asr_model_combo.pack(side="left", fill="x", expand=True, padx=(4,0))
        self.asr_model_combo.bind("<<ComboboxSelected>>", self._on_asr_model_change)

        # --- 火山引擎 字段 ---
        self.volc_fields = tk.Frame(asr_card, bg=C["card"])

        _vr1 = tk.Frame(self.volc_fields, bg=C["card"])
        _vr1.pack(fill="x", pady=(2,0))
        tk.Label(_vr1, text="APP ID", font=FNT_S, fg=C["text"],
             bg=C["card"], width=10).pack(side="left")
        self.volc_app_id_var = tk.StringVar()
        tk.Entry(_vr1, textvariable=self.volc_app_id_var,
              font=("Consolas", 9), fg=C["text"], bg=C["inp"],
              relief="flat").pack(side="left", fill="x", expand=True, padx=(4,0))

        _vr2 = tk.Frame(self.volc_fields, bg=C["card"])
        _vr2.pack(fill="x", pady=(2,0))
        tk.Label(_vr2, text="Access Token", font=FNT_S, fg=C["text"],
             bg=C["card"], width=10).pack(side="left")
        self.volc_token_var = tk.StringVar()
        tk.Entry(_vr2, textvariable=self.volc_token_var,
              font=("Consolas", 9), fg=C["text"], bg=C["inp"],
              show="*", relief="flat").pack(side="left", fill="x", expand=True, padx=(4,0))

        _vr3 = tk.Frame(self.volc_fields, bg=C["card"])
        _vr3.pack(fill="x", pady=(2,0))
        tk.Label(_vr3, text="TOS AK", font=FNT_S, fg=C["text"],
             bg=C["card"], width=10).pack(side="left")
        self.volc_tos_ak_var = tk.StringVar()
        tk.Entry(_vr3, textvariable=self.volc_tos_ak_var,
              font=("Consolas", 9), fg=C["text"], bg=C["inp"],
              relief="flat").pack(side="left", fill="x", expand=True, padx=(4,0))

        _vr4 = tk.Frame(self.volc_fields, bg=C["card"])
        _vr4.pack(fill="x", pady=(2,0))
        tk.Label(_vr4, text="TOS SK", font=FNT_S, fg=C["text"],
             bg=C["card"], width=10).pack(side="left")
        self.volc_tos_sk_var = tk.StringVar()
        tk.Entry(_vr4, textvariable=self.volc_tos_sk_var,
              font=("Consolas", 9), fg=C["text"], bg=C["inp"],
              show="*", relief="flat").pack(side="left", fill="x", expand=True, padx=(4,0))

        # 火山引擎测试连接按钮
        _vr_test = tk.Frame(self.volc_fields, bg=C["card"])
        _vr_test.pack(fill="x", pady=(4,0))
        tk.Label(_vr_test, text="", font=FNT_S, bg=C["card"], width=10).pack(side="left")
        tk.Button(_vr_test, text="✓ 测试火山引擎连接", font=FNT_S, fg=C["ok"], bg=C["card"],
              relief="flat", cursor="hand2", padx=8, pady=2,
              command=self._test_volc_connection).pack(side="left", padx=(4,0))
        _vr5 = tk.Frame(self.volc_fields, bg=C["card"])
        _vr5.pack(fill="x", pady=(2,0))
        tk.Label(_vr5, text="TOS 桶名", font=FNT_S, fg=C["text"],
             bg=C["card"], width=10).pack(side="left")
        self.volc_bucket_var = tk.StringVar(value="livec")
        tk.Entry(_vr5, textvariable=self.volc_bucket_var,
              font=("Consolas", 9), fg=C["text"], bg=C["inp"],
              relief="flat").pack(side="left", fill="x", expand=True, padx=(4,0))


        self.aliyun_fields = tk.Frame(asr_card, bg=C["card"])

        _ar1 = tk.Frame(self.aliyun_fields, bg=C["card"])
        _ar1.pack(fill="x", pady=(2,0))
        tk.Label(_ar1, text="API Key", font=FNT_S, fg=C["text"],
             bg=C["card"], width=10).pack(side="left")
        self.aliyun_api_key_var = tk.StringVar()
        tk.Entry(_ar1, textvariable=self.aliyun_api_key_var,
              font=("Consolas", 9), fg=C["text"], bg=C["inp"],
              relief="flat").pack(side="left", fill="x", expand=True, padx=(4,0))

        _ar2 = tk.Frame(self.aliyun_fields, bg=C["card"])
        _ar2.pack(fill="x", pady=(2,0))
        tk.Label(_ar2, text="OSS AK", font=FNT_S, fg=C["text"],
             bg=C["card"], width=10).pack(side="left")
        self.aliyun_oss_ak_var = tk.StringVar()
        tk.Entry(_ar2, textvariable=self.aliyun_oss_ak_var,
              font=("Consolas", 9), fg=C["text"], bg=C["inp"],
              relief="flat").pack(side="left", fill="x", expand=True, padx=(4,0))

        _ar3 = tk.Frame(self.aliyun_fields, bg=C["card"])
        _ar3.pack(fill="x", pady=(2,0))
        tk.Label(_ar3, text="OSS SK", font=FNT_S, fg=C["text"],
             bg=C["card"], width=10).pack(side="left")
        self.aliyun_oss_sk_var = tk.StringVar()
        tk.Entry(_ar3, textvariable=self.aliyun_oss_sk_var,
              font=("Consolas", 9), fg=C["text"], bg=C["inp"],
              show="*", relief="flat").pack(side="left", fill="x", expand=True, padx=(4,0))

        _ar4 = tk.Frame(self.aliyun_fields, bg=C["card"])
        _ar4.pack(fill="x", pady=(2,0))
        tk.Label(_ar4, text="OSS桶名", font=FNT_S, fg=C["text"],
             bg=C["card"], width=10).pack(side="left")
        self.aliyun_bucket_var = tk.StringVar()
        tk.Entry(_ar4, textvariable=self.aliyun_bucket_var,
              font=("Consolas", 9), fg=C["text"], bg=C["inp"],
              relief="flat").pack(side="left", fill="x", expand=True, padx=(4,0))

        _ar5 = tk.Frame(self.aliyun_fields, bg=C["card"])
        _ar5.pack(fill="x", pady=(2,0))
        tk.Label(_ar5, text="Endpoint", font=FNT_S, fg=C["text"],
             bg=C["card"], width=10).pack(side="left")
        self.aliyun_endpoint_var = tk.StringVar(value="oss-cn-beijing.aliyuncs.com")
        tk.Entry(_ar5, textvariable=self.aliyun_endpoint_var,
              font=("Consolas", 9), fg=C["text"], bg=C["inp"],
              relief="flat").pack(side="left", fill="x", expand=True, padx=(4,0))

        # 阿里云测试连接按钮
        _ar_test = tk.Frame(self.aliyun_fields, bg=C["card"])
        _ar_test.pack(fill="x", pady=(4,0))
        tk.Label(_ar_test, text="", font=FNT_S, bg=C["card"], width=10).pack(side="left")
        tk.Button(_ar_test, text="✓ 测试阿里云连接", font=FNT_S, fg=C["ok"], bg=C["card"],
              relief="flat", cursor="hand2", padx=8, pady=2,
              command=self._test_aliyun_connection).pack(side="left", padx=(4,0))

        # 默认隐藏所有ASR字段（勾选checkbox后才显示）
        self.asr_preset_row.pack_forget()
        self.asr_fields.pack_forget()
        self.volc_fields.pack_forget()
        self.aliyun_fields.pack_forget()

        # ASR行已移至独立卡片

        # 开始按钮 + 输出目录（同一行）
        act_row = tk.Frame(m, bg=C["bg"])
        act_row.pack(fill="x", padx=16, pady=(8,4))
        tk.Button(act_row, text="浏览", font=FNT_S, fg="white", bg=C["btn_sel"],
                  relief="flat", cursor="hand2", padx=8,
                  command=self._browse_output).pack(side="right", padx=(2,0))
        tk.Button(act_row, text="打开", font=FNT_S, fg="white", bg=C["btn_sel"],
                  relief="flat", cursor="hand2", padx=8,
                  command=self._open_output).pack(side="right", padx=(2,0))
        tk.Label(act_row, text="输出:", font=FNT_S, fg=C["dim"],
                 bg=C["bg"]).pack(side="right")
        tk.Label(act_row, textvariable=self.output_var, font=FNT_S, fg=C["dim"],
                 bg=C["bg"]).pack(side="right", fill="x", padx=(8,8))
        tk.Frame(act_row, width=1, bg=C["dim"]).pack(side="right", fill="y", padx=6, pady=2)
        self.subtitle_var = tk.BooleanVar(value=SUBTITLE_OVERLAY.get("enabled"))
        tk.Checkbutton(act_row, text="字幕叠加", variable=self.subtitle_var,
                       font=FNT_S, fg=C["text"], bg=C["bg"],
                       selectcolor=C["inp"], activebackground=C["bg"],
                       cursor="hand2").pack(side="right", padx=4)
        self.btn = tk.Button(act_row, text="▶ 开始切割", font=FNT_B,
                         fg="white", bg=C["btn_go"], activebackground=C["btn_go2"],
                         activeforeground="white", relief="flat", cursor="hand2",
                         padx=16, pady=6, command=self._toggle)
        self.btn.pack(side="left")

        # 进度条 + 步骤说明
        prog_frame = tk.Frame(m, bg=C["bg"])
        prog_frame.pack(fill="x", padx=16, pady=(0,2))
        self.step_label = tk.Label(prog_frame, text="就绪", font=FNT_S, fg=C["dim"],
                                    bg=C["bg"], anchor="w")
        self.step_label.pack(side="left")
        self.pbar = tk.Canvas(prog_frame, height=4, bg=C["bar_bg"], highlightthickness=0)
        self.pbar.pack(fill="x")
        self._bar = self.pbar.create_rectangle(0, 0, 0, 4, fill=C["bar"], outline="")

        # 日志区（可折叠 + 清空）
        self._log_collapsed = False
        log_frame = tk.Frame(m, bg=C["card"])
        log_frame.pack(fill="both", expand=True, padx=16, pady=(0,12))
        log_hdr = tk.Frame(log_frame, bg=C["card"])
        log_hdr.pack(fill="x")
        self._log_toggle_lbl = tk.Label(log_hdr, text="▾ 运行日志", font=FNT_S,
                                     fg=C["dim"], bg=C["card"], cursor="hand2")
        self._log_toggle_lbl.pack(side="left")
        self._log_toggle_lbl.bind("<Button-1>", self._toggle_log)
        tk.Button(log_hdr, text="清空", font=FNT_S, fg=C["dim"], bg=C["card"],
              relief="flat", cursor="hand2", padx=6,
              command=self._clear_log).pack(side="right")
        self._log_content = tk.Frame(log_frame, bg=C["card"])
        self._log_content.pack(fill="both", expand=True)
        self.log = tk.Text(self._log_content, font=FNT_L, bg=C["inp"], fg=C["text"],
                       relief="flat", padx=8, pady=6, wrap="word",
                       state="disabled", height=12)
        sb2 = tk.Scrollbar(self._log_content, command=self.log.yview, bg=C["card"])
        self.log.configure(yscrollcommand=sb2.set)
        sb2.pack(side="right", fill="y"); self.log.pack(side="left", fill="both", expand=True)
        for tag, fg in [("ok",C["ok"]),("warn",C["warn"]),("err",C["err"]),("dim",C["dim"])]:
            self.log.tag_configure(tag, foreground=fg)
        self._log("就绪。添加视频文件，然后点击开始。", "dim")

        # ---- 输入框交互 ----

    def _toggle_dedup_collapse(self, event=None):
        if self._dedup_collapsed:
            # 只有选了"自定义"才展开自定义面板
            if self.dedup.get() == "custom":
                self._dedup_frame.pack(fill="x")
            self._dedup_toggle_lbl.configure(text="▼")
            self._dedup_collapsed = False
        else:
            self._dedup_frame.pack_forget()
            self._dedup_toggle_lbl.configure(text="▶")
            self._dedup_collapsed = True

    def _on_dedup_change(self):
        """去重预设切换时展开/收起自定义面板"""
        if self.dedup.get() == "custom":
            self._dedup_frame.pack(in_=self._dedup_frame.master, fill="x")
            self._dedup_toggle_lbl.configure(text="▼")
            self._dedup_collapsed = False
            self._load_dedup_custom()
        else:
            self._dedup_frame.pack_forget()
            self._dedup_toggle_lbl.configure(text="▶")
            self._dedup_collapsed = True

    def _build_custom_dedup_panel(self):
        """构建自定义去重参数面板"""
        f = self._dedup_frame
        cfg = DEDUP_CONFIG

        # --- 画面区 ---
        sec1 = tk.Frame(f, bg=C["card"])
        sec1.pack(fill="x", pady=(4, 2))
        tk.Label(sec1, text="🎬 画面", font=FNT_B, fg=C["text"], bg=C["card"]).pack(anchor="w")

        r1 = tk.Frame(sec1, bg=C["card"])
        r1.pack(fill="x", pady=2)

        # 镜像
        self._dv_mirror = tk.BooleanVar(value=cfg.get("mirror", {}).get("enabled", True))
        tk.Checkbutton(r1, text="镜像翻转", variable=self._dv_mirror, font=FNT_S,
                       fg=C["text"], bg=C["card"], selectcolor=C["inp"],
                       cursor="hand2").pack(side="left", padx=(0, 16))

        # 随机微裁
        self._dv_crop = tk.BooleanVar(value=cfg.get("random_crop", {}).get("enabled", True))
        tk.Checkbutton(r1, text="微裁剪", variable=self._dv_crop, font=FNT_S,
                       fg=C["text"], bg=C["card"], selectcolor=C["inp"],
                       cursor="hand2").pack(side="left", padx=(0, 16))

        # 伽马微调
        self._dv_gamma = tk.BooleanVar(value=cfg.get("gamma_shift", {}).get("enabled", True))
        tk.Checkbutton(r1, text="亮度微调", variable=self._dv_gamma, font=FNT_S,
                       fg=C["text"], bg=C["card"], selectcolor=C["inp"],
                       cursor="hand2").pack(side="left")

        r1b = tk.Frame(sec1, bg=C["card"])
        r1b.pack(fill="x", pady=2)

        # 四角遮罩
        self._dv_corner = tk.BooleanVar(value=cfg.get("corner_mask", {}).get("enabled", True))
        tk.Checkbutton(r1b, text="四角遮罩", variable=self._dv_corner, font=FNT_S,
                       fg=C["text"], bg=C["card"], selectcolor=C["inp"],
                       cursor="hand2").pack(side="left", padx=(0, 16))

        # --- 速度区 ---
        sec2 = tk.Frame(f, bg=C["card"])
        sec2.pack(fill="x", pady=(6, 2))
        tk.Label(sec2, text="⚡ 速度", font=FNT_B, fg=C["text"], bg=C["card"]).pack(anchor="w")

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

        # --- 音频区 ---
        sec3 = tk.Frame(f, bg=C["card"])
        sec3.pack(fill="x", pady=(6, 2))
        tk.Label(sec3, text="🔊 音频", font=FNT_B, fg=C["text"], bg=C["card"]).pack(anchor="w")

        r3 = tk.Frame(sec3, bg=C["card"])
        r3.pack(fill="x", pady=2)

        # 音高微调
        self._dv_pitch = tk.BooleanVar(value=cfg.get("audio_pitch", {}).get("enabled", True))
        tk.Checkbutton(r3, text="音高微调", variable=self._dv_pitch, font=FNT_S,
                       fg=C["text"], bg=C["card"], selectcolor=C["inp"],
                       cursor="hand2").pack(side="left", padx=(0, 16))

        # 轻微混响
        self._dv_reverb = tk.BooleanVar(value=cfg.get("audio_reverb", {}).get("enabled", True))
        tk.Checkbutton(r3, text="轻微混响", variable=self._dv_reverb, font=FNT_S,
                       fg=C["text"], bg=C["card"], selectcolor=C["inp"],
                       cursor="hand2").pack(side="left", padx=(0, 16))

        # 白噪音融合
        self._dv_noise = tk.BooleanVar(value=cfg.get("noise_fusion", {}).get("enabled", True))
        tk.Checkbutton(r3, text="白噪音融合", variable=self._dv_noise, font=FNT_S,
                       fg=C["text"], bg=C["card"], selectcolor=C["inp"],
                       cursor="hand2").pack(side="left")

        # 底部按钮
        btn_f = tk.Frame(f, bg=C["card"])
        btn_f.pack(fill="x", pady=(6, 2))
        tk.Button(btn_f, text="恢复默认", font=FNT_S, fg=C["dim"], bg=C["inp"],
                  relief="flat", cursor="hand2", padx=10,
                  command=self._reset_dedup_defaults).pack(side="right", padx=2)
        tk.Button(btn_f, text="保存设置", font=FNT_S, fg="white", bg=C["btn_sel"],
                  relief="flat", cursor="hand2", padx=10,
                  command=self._save_dedup_custom).pack(side="right", padx=2)

    def _save_dedup_custom(self):
        """保存自定义去重参数到 ai_settings.json"""
        try:
            settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_settings.json")
            data = {}
            if os.path.exists(settings_path):
                with open(settings_path, "r", encoding="utf-8") as f:
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
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._log("自定义去重设置已保存", "ok")
        except Exception as e:
            self._log(f"保存失败: {e}", "err")

    def _load_dedup_custom(self):
        """从 ai_settings.json 加载自定义去重参数"""
        try:
            settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_settings.json")
            if not os.path.exists(settings_path):
                return
            with open(settings_path, "r", encoding="utf-8") as f:
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
        """恢复去重参数为默认值"""
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
        self._log("去重参数已恢复默认", "ok")


    def _toggle_key_vis(self):
        self._key_visible = not self._key_visible
        self.ai_key_entry.configure(show="" if self._key_visible else "*")

    def _test_ai_connection(self):
        api_key = self.ai_key_var.get().strip()
        base_url = self.ai_url_var.get().strip()
        if not api_key:
            messagebox.showwarning("测试连接", "API Key 为空，请先填写")
            return
        if not base_url:
            messagebox.showwarning("测试连接", "Base URL 为空，请先填写")
            return
        try:
            url = base_url.rstrip("/") + "/models"
            result = subprocess.run(
                ["curl", "-s", "-w", "\n%{http_code}", url,
                 "-H", f"Authorization: Bearer {api_key}"],
                capture_output=True, text=True, timeout=10,
                creationflags=0x08000000 if sys.platform == "win32" else 0
            )
            lines = result.stdout.strip().split("\n")
            http_code = lines[-1] if lines else "0"
            if http_code == "200":
                messagebox.showinfo("测试连接", "✅ 连接成功！API Key 有效")
            elif http_code == "401":
                messagebox.showerror("测试连接", "❌ 401 认证失败！API Key 无效或已过期\n请检查：\n1. Key 是否以 sk- 开头\n2. platform.deepseek.com 确认Key状态\n3. 账户是否已充值")
            else:
                messagebox.showerror("测试连接", f"❌ 连接失败 (HTTP {http_code})\n请检查 Base URL 是否正确")
        except Exception as e:
            messagebox.showerror("测试连接", f"❌ 连接异常: {e}")

    def _test_volc_connection(self):
        app_id = self.volc_app_id_var.get().strip()
        access_token = self.volc_token_var.get().strip()
        tos_ak = self.volc_tos_ak_var.get().strip()
        tos_sk = self.volc_tos_sk_var.get().strip()
        if not app_id or not access_token:
            messagebox.showwarning("测试连接", "App ID 或 Access Token 为空")
            return
        if not tos_ak or not tos_sk:
            messagebox.showwarning("测试连接", "TOS AK/SK 为空")
            return
        try:
            submit_url = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
            req_id = hashlib.md5(str(time.time()).encode()).hexdigest()
            payload = json.dumps({
                "user": {"uid": "test"},
                "audio": {"format": "wav", "url": "https://example.com/test.wav"},
                "request": {"model_name": "bigmodel", "show_utterances": True}
            })
            result = subprocess.run(
                ["curl", "-s", "-w", "\n%{http_code}", submit_url,
                 "-X", "POST",
                 "-H", "Content-Type: application/json",
                 "-H", f"X-Api-App-Key: {app_id}",
                 "-H", f"X-Api-Access-Key: {access_token}",
                 "-H", "X-Api-Resource-Id: volc.bigasr.auc",
                 "-H", f"X-Api-Request-Id: {req_id}",
                 "-H", "X-Api-Sequence: -1",
                 "-d", payload],
                capture_output=True, text=True, timeout=15,
                creationflags=0x08000000 if sys.platform == "win32" else 0
            )
            lines = result.stdout.strip().split("\n")
            http_code = lines[-1] if lines else "0"
            body = "\n".join(lines[:-1]) if len(lines) > 1 else ""
            if http_code == "200":
                messagebox.showinfo("测试连接", "✅ App ID 和 Access Token 有效！\nTOS 上传需要实际音频文件才能验证，请直接运行一次完整流程测试")
            elif http_code == "401":
                messagebox.showerror("测试连接", "❌ 401 认证失败！App ID 或 Access Token 错误\n请检查：\n1. 是否开通了「录音文件识别大模型」\n2. App ID和Token是否同一个应用\n3. 教程: https://www.feishu.cn/docx/QdJDdGpzGofSSuxmPDjc4lrxnVb")
            else:
                if "SignatureDoesNotMatch" in body:
                    messagebox.showerror("测试连接", "❌ TOS AK/SK 错误（签名不匹配）\n请检查 TOS AK 和 TOS SK 是否正确")
                elif "AccessDenied" in body:
                    messagebox.showerror("测试连接", "❌ TOS 权限不足\n请确认 TOS 桶已创建且 AK/SK 有访问权限")
                else:
                    messagebox.showwarning("测试连接", f"⚠️ HTTP {http_code}\n{body[:200]}")
        except Exception as e:
            messagebox.showerror("测试连接", f"❌ 连接异常: {e}")


    def _test_aliyun_connection(self):
        """Test Alibaba Cloud ASR connectivity"""
        api_key = self.aliyun_api_key_var.get().strip()
        oss_ak = self.aliyun_oss_ak_var.get().strip()
        oss_sk = self.aliyun_oss_sk_var.get().strip()
        bucket = self.aliyun_bucket_var.get().strip().rstrip("/")
        endpoint = self.aliyun_endpoint_var.get().strip()

        if not api_key:
            messagebox.showwarning("测试连接", "API Key 为空")
            return
        if not oss_ak or not oss_sk:
            messagebox.showwarning("测试连接", "OSS AK/SK 为空")
            return
        if not bucket:
            messagebox.showwarning("测试连接", "OSS 桶名为空")
            return

        errors = []

        # Test 1: DashScope API Key
        try:
            result = subprocess.run(
                ["curl", "-s", "-w", "\n%{http_code}",
                 "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation",
                 "-X", "POST",
                 "-H", "Content-Type: application/json",
                 "-H", "Authorization: Bearer " + api_key,
                 "-d", '{"model":"qwen-turbo","input":{"messages":[{"role":"user","content":"hi"}]}}'],
                capture_output=True, text=True, timeout=15,
                creationflags=0x08000000 if sys.platform == "win32" else 0
            )
            rlines = result.stdout.strip().split("\n")
            http_code = rlines[-1] if rlines else "0"
            if http_code == "200":
                pass
            elif http_code in ("401", "403"):
                errors.append("❌ API Key 无效（HTTP " + http_code + "）" + chr(10) + "请检查 API Key 是否正确，以及是否开通了 DashScope 服务")
            else:
                errors.append("⚠️ API Key 验证返回 HTTP " + http_code)
        except Exception as e:
            errors.append("❌ API Key 测试异常: " + str(e))

        # Test 2: OSS Bucket
        try:
            import oss2
            auth = oss2.Auth(oss_ak, oss_sk)
            bucket_obj = oss2.Bucket(auth, endpoint, bucket)
            bucket_obj.list_objects(max_keys=1)
        except ImportError:
            try:
                check_url = "https://" + bucket + "." + endpoint + "/"
                result = subprocess.run(
                    ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", check_url],
                    capture_output=True, text=True, timeout=10,
                    creationflags=0x08000000 if sys.platform == "win32" else 0
                )
                code = result.stdout.strip()
                if code not in ("200", "403", "404"):
                    errors.append("⚠️ OSS 连接返回 HTTP " + code)
            except Exception as e2:
                errors.append("⚠️ OSS 测试异常: " + str(e2))
        except Exception as e:
            err_str = str(e)
            if "NoSuchBucket" in err_str:
                errors.append("❌ OSS 桶不存在！" + chr(10) + "请检查桶名是否正确、Endpoint 是否匹配、桶是否已创建")
            elif "AccessDenied" in err_str or "SignatureDoesNotMatch" in err_str:
                errors.append("❌ OSS AK/SK 错误或权限不足！" + chr(10) + "请检查 AK/SK 是否正确，RAM 用户是否有 OSS 读写权限")
            else:
                errors.append("❌ OSS 连接失败: " + err_str[:200])

        if not errors:
            messagebox.showinfo("测试连接", "✅ 阿里云配置有效！" + chr(10) + "• API Key 有效" + chr(10) + "• OSS 桶可访问" + chr(10) + "可以正常使用阿里云语音识别")
        else:
            messagebox.showerror("测试连接", "阿里云配置问题：" + chr(10) + chr(10) + chr(10).join(errors))


    def _on_preset_change(self, value):
        preset = AI_PRESETS.get(value, {})
        if preset.get("base_url"):
            self.ai_url_var.set(preset["base_url"])
        if preset.get("model"):
            self.ai_model_var.set(preset["model"])
        self._save_ai()
        # 自动保存预设配置

    # ---- 文件操作 ----

    def _add_videos(self):
        paths = filedialog.askopenfilenames(title="选择直播视频（可多选）",
                                            filetypes=[("视频","*.mp4 *.mov *.avi *.mkv")])
        for p in paths:
            if p not in [v[0] for v in self.videos]:
                self.videos.append((p, os.path.basename(p)))
        self._refresh_list()

    def _clear_videos(self):
        if not self.videos:
            return
        if messagebox.askyesno("确认清空", "确定要清空所有已添加视频吗？"):
            self.videos.clear(); self._refresh_list()

    def _del_selected(self):
        sel = self.video_listbox.curselection()
        if sel:
            self.videos.pop(sel[0]); self._refresh_list()

    def _refresh_list(self):
        self.video_listbox.delete(0, tk.END)
        for p, name in self.videos:
            self.video_listbox.insert(tk.END, f"  {name}")
        self.count_label.configure(text=f"已选 {len(self.videos)} 个视频")

    def _browse_srt(self):
        p = filedialog.askopenfilename(filetypes=[("SRT","*.srt")])
        if p:
            self.srt_path = p
            self.srt_var.set(os.path.basename(p))
            self._log(f"SRT: {os.path.basename(p)}", "ok")

    def _browse_pip(self):
        p = filedialog.askopenfilename(filetypes=[("视频","*.mp4 *.mov *.avi *.mkv"), ("图片","*.png *.jpg *.jpeg")])
        if p:
            self.pip_path = p
            name = os.path.basename(p)
            self.pip_var.set(name[:25] + '...' if len(name) > 25 else name)
            for _w in self._pip_cfg_widgets:
                _w.pack(side="right")
            self._log(f"画中画: {os.path.basename(p)}", "ok")

    def _clear_pip(self):
        self.pip_path = ""
        self.pip_var.set("留空 = 自动去重")
        for _w in self._pip_cfg_widgets:
            _w.pack_forget()
        self.pip_cfg_frame.pack_forget()

    def _clear_srt(self):
        self.srt_path = ""
        self.srt_var.set("留空 = 自动语音识别")

    def _browse_output(self):
        p = filedialog.askdirectory(title="选择输出目录")
        if p:
            self.output_dir = p
            self.output_var.set(p)

    def _deactivate_device(self):
        """解绑当前设备"""
        from license_client import deactivate_device as _deact, check_activation as _check
        status = _check()
        if not status.get("activated"):
            self._log("当前未激活，无需解绑", "warn")
            return
        if messagebox.askyesno("解绑确认", "解绑后当前设备将恢复试用模式，\n可在新设备上重新激活。\n\n确定要解绑吗？"):
            self._log("正在解绑设备...")
            result = _deact()
            if result["ok"]:
                self._log("✅ " + result["msg"], "ok")
                messagebox.showinfo("解绑成功", result["msg"] + "\n\n程序将退出，请重新启动。")
                self.root.quit()
            else:
                self._log("解绑失败: " + result["msg"], "err")
                messagebox.showerror("解绑失败", result["msg"])

    def _show_feedback(self):
        """提交反馈弹窗"""
        dlg = tk.Toplevel(self.root)
        dlg.title("提交反馈")
        dlg.configure(bg=C["bg"])
        dlg.geometry("480x400")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        tk.Label(dlg, text="💬 提交反馈", font=FNT_T,
                 fg=C["text"], bg=C["bg"]).pack(anchor="w", padx=16, pady=(16,8))
        tk.Label(dlg, text="请描述你遇到的问题或建议。\n默认会附带本地日志，便于快速定位问题。",
                 font=FNT_S, fg=C["dim"], bg=C["bg"], justify="left").pack(anchor="w", padx=16)

        # 反馈输入框
        txt = tk.Text(dlg, font=FNT_L, bg=C["inp"], fg=C["text"],
                      relief="flat", padx=10, pady=8, wrap="word", height=8)
        txt.pack(fill="both", expand=True, padx=16, pady=(8,4))
        txt.focus_set()

        # 日志附带提示
        log_var = tk.BooleanVar(value=True)
        tk.Checkbutton(dlg, text="附带本地运行日志", variable=log_var,
                       font=FNT_S, fg=C["dim"], bg=C["bg"],
                       selectcolor=C["inp"], activebackground=C["bg"]).pack(anchor="w", padx=16)

        # 底部按钮
        btn_row = tk.Frame(dlg, bg=C["bg"])
        btn_row.pack(fill="x", padx=16, pady=(8,16))

        def _submit():
            content = txt.get("1.0", "end").strip()
            if not content:
                txt.configure(bg="#3a2020")
                txt.after(500, lambda: txt.configure(bg=C["inp"]))
                return
            log_text = ""
            if log_var.get():
                # 优先附加最近的运行日志文件
                try:
                    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
                    if os.path.isdir(log_dir):
                        logs = sorted([f for f in os.listdir(log_dir) if f.endswith(".json")])
                        if logs:
                            with open(os.path.join(log_dir, logs[-1]), "r", encoding="utf-8") as f:
                                log_text = f.read().strip()
                except Exception:
                    pass
                if not log_text:
                    try:
                        log_text = self.log.get("1.0", "end").strip()
                    except Exception:
                        pass
            msg = f"【LiveClipper 用户反馈】\n{content}"
            if log_text:
                msg += f"\n\n--- 运行日志（末尾2000字） ---\n{log_text[-2000:]}"
            # 保存本地备份
            fb_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feedback")
            os.makedirs(fb_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            fb_path = os.path.join(fb_dir, f"feedback_{ts}.txt")
            with open(fb_path, "w", encoding="utf-8") as f:
                f.write(msg)
            # 发送到飞书 webhook
            import subprocess, json
            payload = json.dumps({"msg_type": "text", "content": {"text": msg}}, ensure_ascii=False)
            webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/6122f651-65a4-4766-a64f-1aba33bee3ac"
            try:
                r = subprocess.run(
                    ["curl.exe", "-s", "-X", "POST", "-H", "Content-Type: application/json",
                     "-d", payload, webhook],
                    capture_output=True, timeout=10, encoding="utf-8",
                    creationflags=0x08000000 if sys.platform == "win32" else 0)
                ok = '"StatusCode":0' in (r.stdout or "")
            except Exception:
                ok = False
            label = "✅ 反馈已发送，感谢！" if ok else "⚠️ 发送失败，已保存到本地"
            label_color = "#4fc3f7" if ok else "#ff9800"
            tk.Label(dlg, text=label, font=FNT_B,
                     fg=label_color, bg=C["bg"]).pack(pady=(0,8))
            dlg.after(1500, dlg.destroy)

        tk.Button(btn_row, text="取消", font=FNT_S, fg=C["dim"], bg=C["inp"],
                  relief="flat", cursor="hand2", padx=16, pady=4,
                  command=dlg.destroy).pack(side="right", padx=(8,0))
        tk.Button(btn_row, text="提交", font=FNT_B, fg="white", bg=C["btn_go"],
                  relief="flat", cursor="hand2", padx=20, pady=4,
                  command=_submit).pack(side="right")

    def _open_output(self):
        if self.output_dir:
            path = self.output_dir
        elif self.videos:
            path = os.path.join(os.path.dirname(self.videos[0][0]), "output")
        else:
            self._log("请先添加视频或设置输出目录", "warn"); return
        path = os.path.normpath(path)
        os.makedirs(path, exist_ok=True)
        os.startfile(path)

    # ---- 日志操作 ----

    def _toggle_log(self, event=None):
        self._log_collapsed = not self._log_collapsed
        if self._log_collapsed:
            self._log_content.pack_forget()
            self._log_toggle_lbl.configure(text="▶ 运行日志")
        else:
            self._log_content.pack(fill="both", expand=True)
            self._log_toggle_lbl.configure(text="▼ 运行日志")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _toggle_ai_collapse(self, event=None):
        self._ai_collapsed = not self._ai_collapsed
        if self._ai_collapsed:
            self.ai_detail.pack_forget()
            self._ai_toggle_lbl.configure(text="▶")
        else:
            self.ai_detail.pack(fill="x")
            self._ai_toggle_lbl.configure(text="▼")

    def _toggle_asr_collapse(self, event=None):
        self._asr_collapsed = not self._asr_collapsed
        if self._asr_collapsed:
            self.asr_preset_row.pack_forget()
            self.asr_fields.pack_forget()
            self.volc_fields.pack_forget()
            self.aliyun_fields.pack_forget()
            self._asr_toggle_lbl.configure(text="▶")
        else:
            self.asr_preset_row.pack(fill="x", pady=(4,0))
            if self.asr_enabled_var.get():
                preset = self.asr_preset_var.get()
                if preset == "火山引擎":
                    self.volc_fields.pack(fill="x", pady=(2,0))
                elif preset == "阿里云":
                    self.aliyun_fields.pack(fill="x", pady=(2,0))
                else:
                    self.asr_fields.pack(fill="x", pady=(2,0))
            self._asr_toggle_lbl.configure(text="▼")

    def _toggle_ai_toggle(self):
        """切换AI启用状态（按钮触发）"""
        self.ai_enabled_var.set(not self.ai_enabled_var.get())
        self._save_ai()  # save FIRST, so _toggle_ai reads correct state
        self._toggle_ai()

    def _toggle_ai(self):
        """只管 UI 展开/折叠 + 按钮外观，不再加载设置"""
        if self.ai_enabled_var.get():
            self.ai_toggle.configure(text="✅ 启用", fg="#4caf50")
        else:
            self.ai_toggle.configure(text="启用", fg="#4fc3f7")
        if self.ai_enabled_var.get():
            self._ai_collapsed = False
            self._ai_toggle_lbl.configure(text="▾ ")
            self.ai_detail.pack(fill="x")
        else:
            self.ai_detail.pack_forget()

    def _apply_dedup_custom(self):
        """将自定义去重参数应用到 DEDUP_CONFIG（运行时生效）"""
        import config as _cfg
        try:
            settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_settings.json")
            d = {}
            if os.path.exists(settings_path):
                with open(settings_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                d = data.get("dedup_custom", {})
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
            self._log("已加载自定义去重配置")
        except Exception as e:
            self._log(f"加载自定义去重配置失败: {e}", "err")

    def _save_ai(self):
        if not getattr(self, "_init_done", True):
            return  # skip saves before startup finishes
        from ai_clipper import save_settings
        settings = {
            "api_key": self.ai_key_var.get().strip(),
            "base_url": self.ai_url_var.get().strip(),
            "model": self.ai_model_var.get().strip(),
            "enabled": self.ai_enabled_var.get(),
            "asr_enabled": self.asr_enabled_var.get(),
            "asr_api_key": self.asr_key_var.get().strip(),
            "asr_base_url": self.asr_url_var.get().strip(),
            "asr_model": self.asr_model_var.get().strip(),
            "volc_app_id": self.volc_app_id_var.get().strip(),
            "volc_access_token": self.volc_token_var.get().strip(),
            "volc_tos_ak": self.volc_tos_ak_var.get().strip(),
            "volc_tos_sk": self.volc_tos_sk_var.get().strip(),
            "volc_bucket": self.volc_bucket_var.get().strip(),
            "whisper_model": self._whisper_model_var.get() if hasattr(self, "_whisper_model_var") else "small",
            "ai_preset": self.ai_preset_var.get() if hasattr(self, "ai_preset_var") else "",
            "asr_preset": self.asr_preset_var.get() if hasattr(self, "asr_preset_var") else "",
            "asr_provider": self.asr_preset_var.get() if hasattr(self, "asr_preset_var") else "",
            "aliyun_api_key": self.aliyun_api_key_var.get().strip() if hasattr(self, "aliyun_api_key_var") else "",
            "aliyun_oss_ak": self.aliyun_oss_ak_var.get().strip() if hasattr(self, "aliyun_oss_ak_var") else "",
            "aliyun_oss_sk": self.aliyun_oss_sk_var.get().strip() if hasattr(self, "aliyun_oss_sk_var") else "",
            "aliyun_bucket": self.aliyun_bucket_var.get().strip() if hasattr(self, "aliyun_bucket_var") else "",
            "aliyun_endpoint": self.aliyun_endpoint_var.get().strip() if hasattr(self, "aliyun_endpoint_var") else "",
            "ai_focus": self.ai_focus_var.get() if hasattr(self, "ai_focus_var") else "自动",
        }
        if save_settings(settings):
            self._log("AI 设置已保存", "ok")
        else:
            self._log("AI 设置保存失败", "err")

    # ---- 关键词管理 ----

    def _open_keyword_manager(self):
        """打开关键词管理窗口"""
        import tkinter.ttk as ttk

        win = tk.Toplevel(self.root)
        win.title("关键词管理")
        win.geometry("600x520")
        win.configure(bg=C["bg"])
        win.resizable(True, True)
        win.transient(self.root)
        win.grab_set()

        # 加载已有配置
        kw_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keywords.json")
        kw_data = {}
        try:
            with open(kw_path, "r", encoding="utf-8") as f:
                kw_data = json.load(f)
        except Exception:
            pass

        # 默认值：从代码中提取当前配置
        from config import CLIP_KEYWORDS, NEGATIVE_SIGNALS, FILLER_WORDS

        # 构建 Tab
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("KW.TNotebook", background=C["bg"], borderwidth=0)
        style.configure("KW.TNotebook.Tab", background=C["card"], foreground=C["text"],
                        padding=[12, 4], font=FNT_S)
        style.map("KW.TNotebook.Tab",
                  background=[("selected", "#5b21b6")],
                  foreground=[("selected", "white")])

        nb = ttk.Notebook(win, style="KW.TNotebook")
        nb.pack(fill="both", expand=True, padx=12, pady=(12, 0))

        text_widgets = {}

        # === Tab 1: 卖点关键词（按类型分） ===
        tab1 = tk.Frame(nb, bg=C["card"])
        nb.add(tab1, text=" 卖点关键词 ")
        tk.Label(tab1, text="按类型分组，每行格式：类型=关键词\n例如：hook=绝了  |  price=到手价",
                 font=FNT_S, fg=C["dim"], bg=C["card"], justify="left").pack(anchor="w", padx=8, pady=(6, 2))
        t1 = tk.Text(tab1, font=("Consolas", 10), fg=C["text"], bg=C["inp"],
                     relief="flat", padx=6, pady=4, wrap="word", height=18)
        t1.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        # 填充当前配置
        saved_custom = kw_data.get("clip_keywords", {})
        lines1 = []
        for ct, cfg in CLIP_KEYWORDS.items():
            kws = saved_custom.get(ct, cfg.get("keywords", []))
            desc = cfg.get("description", "")
            lines1.append(f"# 【{ct}】{desc}")
            for kw in kws:
                lines1.append(f"{ct}={kw}")
            lines1.append("")
        t1.insert("1.0", "\n".join(lines1))
        text_widgets["clip_keywords"] = t1

        # === Tab 2: 违禁词 ===
        tab2 = tk.Frame(nb, bg=C["card"])
        nb.add(tab2, text=" 违禁词 ")
        tk.Label(tab2, text="命中这些词的片段会被直接移除，每行一个词或短语",
                 font=FNT_S, fg=C["dim"], bg=C["card"], justify="left").pack(anchor="w", padx=8, pady=(6, 2))
        t2 = tk.Text(tab2, font=("Consolas", 10), fg=C["text"], bg=C["inp"],
                     relief="flat", padx=6, pady=4, wrap="word", height=18)
        t2.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        from config import FORBIDDEN_PHRASES
        forbidden = kw_data.get("forbidden_phrases", list(FORBIDDEN_PHRASES))
        t2.insert("1.0", "\n".join(forbidden))
        text_widgets["forbidden_phrases"] = t2

        # === Tab 3: 废话词 ===
        tab3 = tk.Frame(nb, bg=C["card"])
        nb.add(tab3, text=" 废话词 ")
        tk.Label(tab3, text="主播回弹幕的闲聊词，会被过滤掉，每行一个词",
                 font=FNT_S, fg=C["dim"], bg=C["card"], justify="left").pack(anchor="w", padx=8, pady=(6, 2))
        t3 = tk.Text(tab3, font=("Consolas", 10), fg=C["text"], bg=C["inp"],
                     relief="flat", padx=6, pady=4, wrap="word", height=18)
        t3.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        filler = kw_data.get("filler_words", list(FILLER_WORDS))
        t3.insert("1.0", "\n".join(filler))
        text_widgets["filler_words"] = t3


        # === Tab 4: 偏好关键词 ===
        tab4 = tk.Frame(nb, bg=C["card"])
        nb.add(tab4, text=" 偏好关键词 ")
        tk.Label(tab4, text="AI选片偏好匹配词，每行格式：偏好名=关键词\n例如：版型显瘦=显瘦  |  情绪感染=绝了",
                 font=FNT_S, fg=C["dim"], bg=C["card"], justify="left").pack(anchor="w", padx=8, pady=(6, 2))
        t4 = tk.Text(tab4, font=("Consolas", 10), fg=C["text"], bg=C["inp"],
                     relief="flat", padx=6, pady=4, wrap="word", height=18)
        t4.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        # 填充当前偏好关键词（内嵌默认值，避免导入ai_clipper失败）
        _default_pref_kw = {
            "版型显瘦": "显瘦,遮肉,藏肉,收腰,包容,不挑人,微胖,遮胯,遮肚,收腹,提臀,显高,小个子,梨形,苹果型,腿粗,拜拜肉,瘦十斤,小一号,秒变,立瘦,显腿长,显腰细,比例好,拉长比例,遮得住,收腰显瘦,遮副乳,托胸,胯宽,大骨架,纸片人,小肚腩,背厚,肩宽",
            "颜色氛围": "显白,提亮,抬气色,显肤色,黄皮,黑皮,衬肤色,不挑肤色,冷白皮,暖白皮,气色好,衬人白,高级灰,显嫩,温柔色,显贵色,不挑皮,上镜色,拍照好看,老钱风,奶油色,燕麦色,雾霾蓝,牛油果,奶茶色,焦糖色,香芋紫,橡皮粉,百搭色,抬肤色",
            "穿着场景": "通勤,约会,度假,日常,出门,上班,逛街,实穿,职场,聚会,拍照,旅游,出差,叠穿,内搭,外穿,单穿,一年四季,懒人,一套搞定,见家长,见前男友,同学聚会,相亲,年会,踏青,遛娃,送孩子,百搭,穿得出去",
            "性价比": "划算,超值,性价比,品质,质感,做工,同款,外面买不到,大牌平替,代工厂,专柜,商场,物超所值,比外面,比商场,同品质,这个价,这个品质,商场同款,自己家工厂,源头,出厂价,直播间专属,老粉,闭眼冲,不踩坑,买过都说好,回购率,对得起这个价,回头客",
            "紧迫稀缺": "限量,库存,最后,抢,不多,少数,断货,售罄,抢完,没了,没码,补货,少量,限时,赶紧,手慢无,错过,独家,不撞款,定制,稀缺,马上,不等人,只剩,不多了,剩最后,这一批,下次不知道,不会再上,手速",
            "情绪感染": "绝了,太漂亮,美爆,太好看了,太爱,神仙,封神,超级超级,特别特别,真的真的,非常非常,天呐,妈呀,我的天,受不了,爱了爱了,绝绝子,yyds,信我,相信我,不骗你,真心,自留,我自己也,美哭,好看死,太绝了,我天,天哪,疯了吧,哇塞,我自己都",
            "流行趋势": "流行,当季,新款,设计,原创,不撞款,爆款,热门,趋势,法式,韩系,日系,欧美,ins风,极简,复古,国风,新中式,设计师,小心机,细节,小众,轻奢,时髦,小香风,千金风,老钱,清冷感,氛围感,松弛感,财阀千金,甜酷,美拉德,多巴胺,静奢",
            "面料质感": "面料,手感,亲肤,质感,桑蚕丝,冰感,软糯,透气,真丝,羊毛,羊绒,纯棉,雪纺,缎面,蕾丝,牛仔,针织,垂感,弹力,厚实,做工,走线,不起球,不褪色,抗皱,免熨,垂坠,丝滑,软乎乎,厚薄适中,垂坠感,糯糯的,像云朵,婴儿肌,裸感",
        }
        saved_pref = kw_data.get("preference_keywords", {})
        lines4 = []
        # 合并：代码默认 + 用户自定义（用户自定义优先）
        all_pref_names = list(dict.fromkeys(list(_default_pref_kw.keys()) + list(saved_pref.keys())))
        for pname in all_pref_names:
            if pname in saved_pref:
                pkws = saved_pref[pname]
            else:
                pkws = _default_pref_kw.get(pname, "").split(",")
            lines4.append(f"# 【{pname}】")
            for kw in pkws:
                if kw.strip():
                    lines4.append(f"{pname}={kw.strip()}")
            lines4.append("")
        t4.insert("1.0", "\n".join(lines4))
        text_widgets["preference_keywords"] = t4


        # 底部按钮
        btn_row = tk.Frame(win, bg=C["bg"])
        btn_row.pack(fill="x", padx=12, pady=10)

        def _save_keywords():
            result = {}
            # 解析卖点关键词
            clip_kw = {}
            raw1 = text_widgets["clip_keywords"].get("1.0", "end").strip()
            for line in raw1.split("\n"):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    ct, kw = line.split("=", 1)
                    ct = ct.strip()
                    kw = kw.strip()
                    if ct and kw:
                        clip_kw.setdefault(ct, []).append(kw)
            result["clip_keywords"] = clip_kw

            # 解析违禁词
            raw2 = text_widgets["forbidden_phrases"].get("1.0", "end").strip()
            result["forbidden_phrases"] = [l.strip() for l in raw2.split("\n") if l.strip()]

            # 解析废话词
            raw3 = text_widgets["filler_words"].get("1.0", "end").strip()
            result["filler_words"] = [l.strip() for l in raw3.split("\n") if l.strip()]

            # 解析偏好关键词
            pref_kw = {}
            if "preference_keywords" in text_widgets:
                raw4 = text_widgets["preference_keywords"].get("1.0", "end").strip()
                for line in raw4.split("\n"):
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        pname, kw = line.split("=", 1)
                        pname = pname.strip()
                        kw = kw.strip()
                        if pname and kw:
                            pref_kw.setdefault(pname, []).append(kw)
            result["preference_keywords"] = pref_kw

            try:
                with open(kw_path, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                self._log("关键词配置已保存", "ok")
                messagebox.showinfo("保存成功", "关键词配置已保存，下次剪辑时生效。", parent=win)
            except Exception as e:
                self._log(f"关键词保存失败: {e}", "err")
                messagebox.showerror("保存失败", str(e), parent=win)

        def _reset_keywords():
            if messagebox.askyesno("确认重置", "将恢复为默认关键词，自定义内容会丢失。确认？", parent=win):
                # 删除配置文件，恢复默认
                try:
                    os.remove(kw_path)
                except Exception:
                    pass
                win.destroy()
                self._open_keyword_manager()  # 重新打开

        tk.Button(btn_row, text="恢复默认", font=FNT_S, fg=C["dim"], bg=C["card"],
                  relief="flat", cursor="hand2", padx=12, command=_reset_keywords).pack(side="right")
        tk.Button(btn_row, text="保存", font=FNT_B, fg="white", bg=C["btn_sel"],
                  relief="flat", cursor="hand2", padx=20, command=_save_keywords).pack(side="right", padx=(0, 8))

    def _toggle_asr_toggle(self):
        """切换ASR启用状态（按钮触发）"""
        self.asr_enabled_var.set(not self.asr_enabled_var.get())
        self._save_ai()  # save FIRST
        self._toggle_asr()

    def _toggle_asr(self):
        """切换云端ASR字段显示"""
        if self.asr_enabled_var.get():
            self.asr_toggle.configure(text="✓ 启用", fg="#4caf50")
        else:
            self.asr_toggle.configure(text="启用", fg="#4fc3f7")
        if self.asr_enabled_var.get():
            self._asr_collapsed = False
            self._asr_toggle_lbl.configure(text="▼")
            self.asr_preset_row.pack(fill="x", pady=(4,0))
            preset = self.asr_preset_var.get()
            if preset == "火山引擎":
                self.volc_fields.pack(fill="x", pady=(2,0))
            elif preset == "阿里云":
                self.aliyun_fields.pack(fill="x", pady=(2,0))
            else:
                self.asr_fields.pack(fill="x", pady=(2,0))
        else:
            self.asr_preset_row.pack_forget()
            self.asr_fields.pack_forget()
            self.volc_fields.pack_forget()
            self.aliyun_fields.pack_forget()

    def _on_asr_model_change(self, event=None):
        """ASR模型变更时自动填充 base_url"""
        model = self.asr_model_var.get()
        url_map = {
            "groq-whisper-large-v3": "https://api.groq.com/openai/v1",
        }
        if model in url_map:
            self.asr_url_var.set(url_map[model])

    def _on_asr_preset_change(self, value):
        """ASR预设切换，自动填充字段 + 切换显示"""
        # 隐藏所有字段
        self.asr_fields.pack_forget()
        self.volc_fields.pack_forget()
        self.aliyun_fields.pack_forget()

        if value == "阿里云":
            if self.asr_enabled_var.get():
                self.aliyun_fields.pack(fill="x", pady=(2,0))
            self.asr_url_var.set("https://dashscope.aliyuncs.com")
            self.asr_model_var.set("paraformer-v2")
        elif value == "火山引擎":
            if self.asr_enabled_var.get():
                self.volc_fields.pack(fill="x", pady=(2,0))
        else:  # 自定义
            if self.asr_enabled_var.get():
                self.asr_fields.pack(fill="x", pady=(2,0))
        self._save_ai()
    def _log(self, msg, tag=None):
        self._log_queue.put(("log", msg, tag))

    def _set_bar(self, pct):
        self._log_queue.put(("bar", pct, None))

    def _set_step(self, text):
        self._log_queue.put(("step", text, None))

    def _poll_queue(self):
        """主线程每 50ms 轮询队列，更新 UI（永不停止）"""
        try:
            while True:
                kind, msg, tag = self._log_queue.get_nowait()
                try:
                    if kind == "log":
                        self.log.configure(state="normal")
                        self.log.insert("end", msg+"\n", tag if tag else ())
                        self.log.see("end")
                        self.log.configure(state="disabled")
                    elif kind == "bar":
                        w = self.pbar.winfo_width()
                        self.pbar.coords(self._bar, 0, 0, int(w * msg), 4)
                    elif kind == "step":
                        self.step_label.configure(text=msg)
                except Exception:
                    pass  # 单条消息更新失败不影响后续
        except queue.Empty:
            pass
        except Exception:
            pass
        # 关键：无论发生什么，必须继续调度
        try:
            self.root.after(50, self._poll_queue)
        except Exception:
            pass

    # ---- 执行 ----

    def _toggle(self):
        if self.worker and self.worker.is_alive():
            if self._cancel_event:
                self._cancel_event.set()
            self.btn.configure(text="▶  开始生成爆款切片", bg=C["btn_go"])
            self._log("❌ 已请求停止，当前视频处理完后会自动停止。", "warn")
            return

        if not self.videos:
            self._log("请先添加视频文件！", "err"); return

        self.btn.configure(text="■  停止", bg=C["btn_no"])
        self._set_bar(0)

        # 【重要】剪辑前检查激活/试用状态，防止超限使用
        try:
            from license_client import check_activation
            _lic = check_activation()
            if _lic.get("need_activate"):
                self._log("⚠ 请先激活或试用次数已用完，无法开始剪辑", "err")
                from tkinter import messagebox
                messagebox.showerror("提示", _lic.get("reason", "请激活后使用"))
                return
        except Exception:
            pass

        # 先读 tkinter 变量（主线程），避免子线程报 RuntimeError
        _dedup = self.dedup.get()
        _subtitle = self.subtitle_var.get()
        # 如果是自定义去重，应用自定义配置
        if _dedup == "custom":
            self._apply_dedup_custom()
        _cat = self.main_category_var.get()
        _category = None if _cat == "自动检测" else _cat

        # 起一个线程逐个处理
        def batch_run():
            total = len(self.videos)
            for idx, (video_path, video_name) in enumerate(self.videos):
                if self._cancel_event and self._cancel_event.is_set():
                    self._log("__BATCH_CANCEL__")
                    break

                self._log(f"\n{'='*45}")
                self._log(f"[{idx+1}/{total}] {video_name}")
                self._log(f"{'='*45}")

                # 输出路径（加时间戳，不覆盖）
                import time as _time
                ts = _time.strftime("%H%M%S")
                stem = os.path.splitext(video_name)[0]
                if self.output_dir:
                    output_dir = self.output_dir
                else:
                    output_dir = os.path.join(os.path.dirname(video_path), "output")
                output = os.path.join(output_dir, f"{stem}_{ts}.mp4")

                srt = self.srt_path if self.srt_path else None
                try:
                    _nver = int(self.num_versions_var.get())
                    _process_fn = process_video_multi if _nver > 1 else process_video
                    _focus = self.ai_focus_var.get() if hasattr(self, "ai_focus_var") else "自动"
                    _kwargs = dict(
                        video_path=video_path, srt_path=srt, output_path=output,
                        dedup_preset=_dedup,
                        subtitle_overlay=_subtitle,
                        force_category=_category,
                        cancel_event=self._cancel_event,
                        focus_hint=_focus,
                        pip_path=self.pip_path if self.pip_path else "auto",
                        pip_size=int(self.pip_size_var.get().replace("%",""))/100,
                        pip_opacity=int(self.pip_opacity_var.get().replace("%",""))/100,
                        pip_pos=self.pip_pos_var.get(),
                        smart_crop_enabled=self.smart_crop_var.get() if hasattr(self, "smart_crop_var") else True,
                        crop_level={"轻":"light","中":"medium","重":"heavy"}.get(self.crop_level_var.get() if hasattr(self, "crop_level_var") else "中", "medium"),
                        ken_burns_enabled=self.ken_burns_var.get() if hasattr(self, "ken_burns_var") else True,
                        log_fn=lambda msg, _idx=idx, _total=total: self._batch_log(msg, _idx, _total)
                    )
                    if _nver > 1:
                        _kwargs["num_versions"] = _nver
                    ok = _process_fn(**_kwargs)
                except Exception as e:
                    import traceback
                    err_msg = str(e)
                    friendly = _friendly_error(err_msg)
                    self._log(f"❌ {friendly}", "err")
                    # 原始错误写入日志（用户可展开查看）
                    self._log(f"[调试] {err_msg}", "dim")
                    ok = False
                # 处理返回值（兼容 bool 和 dict）
                if isinstance(ok, dict):
                    result_info = ok
                    ok = result_info.get("ok", False)
                else:
                    result_info = {"ok": ok}
                self._log(f"__BATCH_RESULT__{'OK' if ok else 'FAIL'}__")
                # 试用模式下，切割成功后扣减一次
                if ok:
                    try:
                        from license_client import check_activation, consume_trial_use
                        status = check_activation()
                        if status.get("trial"):
                            left = consume_trial_use()
                            if left >= 0:
                                self._log(f"试用剩余 {left} 次")
                            else:
                                self._log("⚠ 试用次数已用完，请激活继续使用")
                    except Exception:
                        pass

            # 批处理结束，恢复按钮
            was_cancelled = self._cancel_event and self._cancel_event.is_set()
            if was_cancelled:
                self._log("已停止。")
            self._log("__BATCH_DONE__")
            try:
                self.root.after(0, lambda: self._reset_btn(cancelled=was_cancelled))
            except: pass

        self._cancel_event = threading.Event()
        self.worker = threading.Thread(target=batch_run, daemon=True)
        self.worker.start()

    def _batch_log(self, msg, idx, total):
        if msg.startswith("[PROGRESS]"):
            try:
                pct = float(msg.split(" ")[1])
                base = idx / total
                self._set_bar(base + pct / total)
            except ValueError: pass
        elif msg.startswith("[STEP]"):
            self._set_step(msg.replace("[STEP] ", ""))
        else:
            self._log(msg)

    def _on_msg(self, msg):
        pass  # 不再使用 Worker 直接调用

    def _reset_btn(self, cancelled=False):
        """批处理完成后恢复按钮状态"""
        self._cancel_event = None
        self._set_bar(0)
        self._set_step("就绪")
        if cancelled:
            self.btn.configure(text="❌ 已停止", bg=C["btn_go"], activebackground=C["btn_go2"])
        else:
            self.btn.configure(text="✅ 剪辑已完成", bg=C["btn_go"], activebackground=C["btn_go2"])



def _show_activation_check(root):
    """检查激活状态/试用状态，需要激活则弹出对话框"""
    try:
        import license_client as _lc
        status = check_activation()
        if status.get("activated"):
            pass  # 已激活，静默通过
        elif status.get("trial"):
            # 试用中：不弹激活码输入框，只在剩余≤3次时提醒
            uses = status["uses_left"]
            if uses <= 3:
                _show_trial_dialog(root, uses, force=(uses <= 0))
        elif status.get("need_activate"):
            _show_activate_dialog(root)
    except Exception:
        pass



def _show_trial_dialog(root, uses_left, force=False):
    """试用剩余次数提醒"""
    if force:
        msg = f"试用次数已用完，请激活以继续使用全部功能。"
        btn_text = "立即激活"
    else:
        msg = f"免费试用还剩 {uses_left} 次，激活后可无限制使用。"
        btn_text = "现在激活"

    result = messagebox.askyesno("试用提醒", msg)
    if result:
        _show_activate_dialog(root)


def _show_activate_dialog(root):
    """激活码输入对话框"""
    dlg = tk.Toplevel(root)
    dlg.title("激活 LiveClipper")
    dlg.geometry("440x300")
    dlg.resizable(False, False)
    dlg.transient(root)
    dlg.grab_set()

    # 居中
    dlg.update_idletasks()
    x = root.winfo_x() + (root.winfo_width() - 440) // 2
    y = root.winfo_y() + (root.winfo_height() - 260) // 2
    dlg.geometry(f"+{max(0,x)}+{max(0,y)}")

    # 标题
    tk.Label(
        dlg, text="欢迎使用直播带货切片工具",
        font=("Microsoft YaHei UI", 12, "bold")
    ).pack(pady=(20, 5))

    tk.Label(
        dlg, text="请输入激活码以解锁全部功能",
        font=("Microsoft YaHei UI", 9),
        fg="#888888"
    ).pack(pady=(0, 10))

    # 输入框
    entry_frame = tk.Frame(dlg)
    entry_frame.pack(pady=5)

    tk.Label(entry_frame, text="激活码:", font=("Microsoft YaHei UI", 10)).pack(side="left", padx=(0, 8))
    code_var = tk.StringVar()
    entry = tk.Entry(entry_frame, textvariable=code_var, width=50, font=("Consolas", 11))
    entry.pack(side="left")
    entry.focus_set()

    # 状态标签
    msg_label = tk.Label(dlg, text="", font=("Microsoft YaHei UI", 9), fg="#E74C3C")
    msg_label.pack(pady=(5, 0))

    def do_activate():
        code = code_var.get().strip()
        if not code:
            msg_label.config(text="请输入激活码", fg="#E74C3C")
            return
        result = activate_with_code(code)
        if result["ok"]:
            info = result["info"]
            dlg.destroy()  # 立即关闭弹窗，无需等待
        else:
            msg_label.config(text=result.get("msg", "激活失败"), fg="#E74C3C")

    # 按钮
    btn_frame = tk.Frame(dlg)
    btn_frame.pack(pady=15)

    tk.Button(
        btn_frame, text="激活", width=10,
        command=do_activate,
        font=("Microsoft YaHei UI", 10)
    ).pack(side="left", padx=5)

    def skip_trial():
        """跳过激活，进入试用"""
        dlg.destroy()

    def on_close():
        root.quit()
        root.destroy()

    # 始终显示试用按钮（新用户首次启动也会看到）
    trial = check_trial()
    # 无论是否在试用期，都显示试用按钮（首次启动时check_trial会自动初始化试用）
    trial_label = ""
    if trial.get("in_trial") and trial.get("uses_left", 0) > 0:
        trial_label = f"（剩余{trial['uses_left']}次）"
    tk.Button(
        btn_frame, text=f"试用{trial_label}", width=12,
        command=skip_trial,
        font=("Microsoft YaHei UI", 10)
    ).pack(side="left", padx=5)

    tk.Button(
        btn_frame, text="退出", width=10,
        command=on_close,
        font=("Microsoft YaHei UI", 10)
    ).pack(side="left", padx=5)

    # 解绑设备按钮（已激活时显示）
    if check_activation().get("activated"):
        def do_deactivate():
            if messagebox.askyesno("解绑确认", "解绑后当前设备将无法使用，确定要解绑吗？"):
                result = deactivate_device()
                if result["ok"]:
                    messagebox.showinfo("解绑成功", result["msg"])
                    dlg.destroy()
                    root.quit()
                    root.destroy()
                else:
                    messagebox.showerror("解绑失败", result["msg"])
        tk.Button(
            btn_frame, text="解绑设备", width=10,
            command=do_deactivate,
            font=("Microsoft YaHei UI", 10), fg="#FF453A"
        ).pack(side="left", padx=5)

    dlg.protocol("WM_DELETE_WINDOW", on_close)



def _show_welcome_guide(root):
    """首次启动引导（可跳过）"""
    # 检测是否首次启动
    settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_settings.json")
    guide_done_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".guide_done")
    if os.path.exists(guide_done_path) or os.path.exists(settings_path):
        return  # 已配置过或已完成引导

    dlg = tk.Toplevel(root)
    dlg.title("欢迎使用直播带货切片工具")
    dlg.geometry("480x420")
    dlg.resizable(False, False)
    dlg.transient(root)
    dlg.grab_set()
    dlg.configure(bg=C["bg"])

    # 居中
    dlg.update_idletasks()
    x = root.winfo_x() + (root.winfo_width() - 480) // 2
    y = root.winfo_y() + (root.winfo_height() - 420) // 2
    dlg.geometry(f"+{max(0,x)}+{max(0,y)}")

    # 页面容器
    pages = []
    current_page = [0]

    def make_page():
        frame = tk.Frame(dlg, bg=C["bg"])
        frame.pack(fill="both", expand=True, padx=24, pady=12)
        pages.append(frame)
        return frame

    # ---- 页面 0: 欢迎 ----
    p0 = make_page()
    tk.Label(p0, text="🎬 欢迎使用", font=("Microsoft YaHei UI", 18, "bold"),
             fg=C["text"], bg=C["bg"]).pack(pady=(20, 4))
    tk.Label(p0, text="直播带货切片工具", font=("Microsoft YaHei UI", 14),
             fg=C["btn_sel"], bg=C["bg"]).pack(pady=(0, 16))
    tk.Label(p0, text="三步生成爆款切片：", font=("Microsoft YaHei UI", 10),
             fg=C["text"], bg=C["bg"]).pack(anchor="w", pady=(8, 4))
    for step_text in [
        "1️⃣  添加直播回放视频",
        "2️⃣  AI 自动选片（需配置 API Key，也可先用关键词模式）",
        "3️⃣  点击开始 → 自动剪辑+字幕+去重",
    ]:
        tk.Label(p0, text=step_text, font=("Microsoft YaHei UI", 10),
                 fg=C["dim"], bg=C["bg"], anchor="w", justify="left").pack(anchor="w", pady=2, padx=12)

    # ---- 页面 1: AI 配置提示 ----
    p1 = make_page()
    p1.pack_forget()  # 默认隐藏
    tk.Label(p1, text="🤖 AI 智能选片配置（可选）", font=("Microsoft YaHei UI", 14, "bold"),
             fg=C["text"], bg=C["bg"]).pack(pady=(16, 8))
    tk.Label(p1, text="推荐使用 DeepSeek V3（性价比最高）",
             font=("Microsoft YaHei UI", 10), fg=C["ok"], bg=C["bg"]).pack(pady=(0, 12))
    tk.Label(p1, text="配置步骤：", font=("Microsoft YaHei UI", 10, "bold"),
             fg=C["text"], bg=C["bg"]).pack(anchor="w")
    for tip in [
        "1. 在主界面展开「AI 智能选片」面板",
        "2. 供应商预设选择「DeepSeek V3」",
        "3. 填入 API Key（platform.deepseek.com/api_keys）",
        "4. 点击「保存」",
        "",
        "💡 不配置也可以用关键词模式，但 AI 选片效果更好",
    ]:
        fg = C["dim"] if tip.startswith("💡") else C["text"]
        tk.Label(p1, text=tip, font=("Microsoft YaHei UI", 9),
                 fg=fg, bg=C["bg"], anchor="w", justify="left").pack(anchor="w", pady=1, padx=8)

    # ---- 页面 2: ASR 提示 ----
    p2 = make_page()
    p2.pack_forget()
    tk.Label(p2, text="🎙 语音识别配置", font=("Microsoft YaHei UI", 14, "bold"),
             fg=C["text"], bg=C["bg"]).pack(pady=(16, 8))
    tk.Label(p2, text="默认使用本地 Whisper（免费，无需配置）",
             font=("Microsoft YaHei UI", 10), fg=C["ok"], bg=C["bg"]).pack(pady=(0, 12))
    tk.Label(p2, text="识别不准时可以：",
             font=("Microsoft YaHei UI", 10), fg=C["text"], bg=C["bg"]).pack(anchor="w")
    for tip in [
        "• 提供自己的 SRT 字幕文件（识别最准）",
        "• 开启云端 ASR（火山引擎/阿里云）",
        "• 这些都可以稍后再配，先用默认设置试试",
    ]:
        tk.Label(p2, text=tip, font=("Microsoft YaHei UI", 9),
                 fg=C["dim"], bg=C["bg"], anchor="w").pack(anchor="w", pady=1, padx=8)

    # ---- 底部按钮 ----
    btn_frame = tk.Frame(dlg, bg=C["bg"])
    btn_frame.pack(fill="x", padx=24, pady=(0, 16))

    prev_btn = tk.Button(btn_frame, text="上一步", font=("Microsoft YaHei UI", 9),
                         fg=C["dim"], bg=C["card"], relief="flat", padx=12, pady=4,
                         command=lambda: _switch_page(-1))
    prev_btn.pack(side="left")

    skip_btn = tk.Button(btn_frame, text="跳过，直接使用 →", font=("Microsoft YaHei UI", 9),
                         fg=C["dim"], bg=C["bg"], relief="flat", padx=8, pady=4,
                         cursor="hand2", command=lambda: _close_guide())
    skip_btn.pack(side="left", padx=(12, 0))

    next_btn = tk.Button(btn_frame, text="下一步 →", font=("Microsoft YaHei UI", 10, "bold"),
                         fg="white", bg=C["btn_sel"], relief="flat", padx=16, pady=6,
                         cursor="hand2", command=lambda: _switch_page(1))
    next_btn.pack(side="right")

    def _switch_page(delta):
        pages[current_page[0]].pack_forget()
        current_page[0] = max(0, min(len(pages) - 1, current_page[0] + delta))
        pages[current_page[0]].pack(fill="both", expand=True, padx=24, pady=12)
        # 更新按钮状态
        prev_btn.configure(state="normal" if current_page[0] > 0 else "disabled")
        if current_page[0] == len(pages) - 1:
            next_btn.configure(text="开始使用 ✅", command=lambda: _close_guide())
        else:
            next_btn.configure(text="下一步 →", command=lambda: _switch_page(1))

    def _close_guide():
        # 标记引导已完成
        try:
            with open(guide_done_path, "w") as f:
                f.write("done")
        except Exception:
            pass
        dlg.destroy()

    # 初始化按钮状态
    prev_btn.configure(state="disabled")
    dlg.protocol("WM_DELETE_WINDOW", _close_guide)


def main():
    try:
        import tkinterdnd2; root = tkinterdnd2.Tk()
    except ImportError:
        root = tk.Tk()
    # DPI: use system default (no override)
    # tk scaling: system default
    # 字体设置：修改默认字体（瞬间生效，不递归遍历）
    try:
        from tkinter import font as tkfont
        tkfont.nametofont('TkDefaultFont').configure(family='Microsoft YaHei UI', size=11)
        tkfont.nametofont('TkTextFont').configure(family='Microsoft YaHei UI', size=11)
        tkfont.nametofont('TkFixedFont').configure(family='Consolas', size=11)
    except:
        pass
    # 启动时检查激活状态
    _show_activation_check(root)
    # 首次启动引导
    _show_welcome_guide(root)
    App(root)
    # 首次启动初始化版本号
    try:
        from updater import init_installed_version
        init_installed_version()
    except Exception:
        pass
    # 启动时后台检查更新（不阻塞主程序）
    try:
        check_and_prompt_update(root)
    except Exception:
        pass
    root.mainloop()


if __name__ == "__main__":
    main()
