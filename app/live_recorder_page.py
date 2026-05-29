#!/usr/bin/env python3

# -*- coding: utf-8 -*-

"""直播录制管理页面——监控+录制一体，输出FLV格式"""



import os

import re

import json

import time

import threading

import subprocess

from datetime import datetime

import tkinter as tk

from tkinter import ttk, messagebox, filedialog



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

    "btn_no": "#FF453A",

    "bar": "#0A84FF",

}

FNT_T = ("Microsoft YaHei", 13, "bold")

FNT_B = ("Microsoft YaHei", 11, "bold")

FNT_S = ("Microsoft YaHei", 10)

FNT_L = ("Microsoft YaHei", 9)





class LiveRecorderPage(tk.Frame):

    PLATFORM_PATTERNS = {

        "自定义RTMP": "",

        "抖音直播": "live.douyin.com|douyin.com/live",

    }



    def __init__(self, parent, app=None):

        super().__init__(parent, bg=C["bg"])

        self.app = app

        self._tasks = []

        self._task_id_counter = 0

        self._monitor_running = False

        self._monitor_thread = None

        self._stop_monitor = threading.Event()

        self._selected_tid_var = tk.StringVar(value="")
        self._selected_tid_var = tk.StringVar(value='')
        self._selected_tid_var.trace_add('write', lambda *a: self._select_row(self._selected_tid_var.get()) if self._selected_tid_var.get() else None)
        self._current_filter = '全部'
        self._save_dir = os.path.join(os.path.expanduser("~"), "Videos", "直播录制")

        self._check_interval_var = tk.IntVar(value=30)

        self._segment_var = tk.StringVar(value="不限")

        seg_options = ["不限", "30分钟", "1小时", "2小时", "4小时"]



        self._build_ui()

        self._load_tasks()



        # Auto-start monitoring on launch

        # Disabled: auto-start monitor was causing confusion

        # self.after(500, self._auto_start_monitor)



    # ────────────────────────────────────────────

    #  UI

    # ────────────────────────────────────────────



    def _build_ui(self):

        # Top bar: title + buttons

        bar = tk.Frame(self, bg=C["card"], padx=12, pady=6)

        bar.pack(fill="x")

        tk.Label(bar, text="录制管理", font=FNT_T, fg=C["text"],

                 bg=C["card"]).pack(side="left")



        tk.Button(bar, text="添加直播间", font=FNT_B, fg="white",

                  bg=C["btn_sel"], relief="flat", cursor="hand2", padx=12, pady=4,

                  command=self._add_task_dialog).pack(side="left", padx=(16, 4))



        self._monitor_btn = tk.Button(bar, text="开始监控", font=FNT_S, fg="white",

                  bg=C["btn_go"], relief="flat", cursor="hand2", padx=10, pady=4,

                  command=self._toggle_monitor)

        self._monitor_btn.pack(side="left", padx=2)



        tk.Button(bar, text="删除全部", font=FNT_S, fg="white",

                  bg=C["btn_del"], relief="flat", cursor="hand2", padx=10, pady=4,

                  command=self._batch_delete).pack(side="left", padx=2)



        # Task list - custom scrollable rows with per-item buttons

        self._task_container = tk.Frame(self, bg=C["bg"])

        self._task_container.pack(fill="both", expand=True)



        # Header row

        hdr = tk.Frame(self._task_container, bg=C["card"], padx=8, pady=3,

                       highlightbackground=C["card_border"], highlightthickness=1)

        hdr.pack(fill="x")

        for txt, w in [("直播间名称", 140), ("平台", 60), ("直播地址", 150),

                        ("状态", 60), ("录制时长", 80), ("文件大小", 70), ("操作", 80)]:

            tk.Label(hdr, text=txt, font=FNT_S, fg=C["dim"], bg=C["card"],

                     width=w//7).pack(side="left")



        # Scrollable body

        self._task_body = tk.Frame(self._task_container, bg=C["bg"])

        self._task_body.pack(fill="both", expand=True)

        self._task_canvas = tk.Canvas(self._task_body, bg=C["bg"], highlightthickness=0)

        self._task_scroll = tk.Frame(self._task_body, bg=C["bg"])

        vsb = tk.Scrollbar(self._task_body, orient="vertical",

                           command=self._task_canvas.yview)

        self._task_inner = tk.Frame(self._task_canvas, bg=C["bg"])

        self._task_inner.bind("<Configure>",

            lambda e: self._task_canvas.configure(scrollregion=self._task_canvas.bbox("all")))

        self._task_canvas.create_window((0, 0), window=self._task_inner, anchor="nw")

        self._task_canvas.configure(yscrollcommand=vsb.set)

        self._task_canvas.pack(side="left", fill="both", expand=True)

        vsb.pack(side="right", fill="y")



        # Bottom action bar

        # Settings row

        set_frame = tk.Frame(self, bg=C["card"], padx=12, pady=4,

                             highlightbackground=C["card_border"], highlightthickness=1)

        set_frame.pack(fill="x")



        tk.Label(set_frame, text="保存目录:", font=FNT_S, fg=C["dim"],

                 bg=C["card"]).pack(side="left")

        self._save_dir_var = tk.StringVar(value=self._save_dir)

        tk.Entry(set_frame, textvariable=self._save_dir_var, font=FNT_S,

                 bg=C["inp"], fg=C["text"], relief="flat", bd=0,

                 width=28).pack(side="left", padx=(4, 2))

        tk.Button(set_frame, text="浏览", font=FNT_S, fg="white", bg=C["btn_sel"],

                  relief="flat", cursor="hand2", padx=6, pady=1,

                  command=self._browse_save_dir).pack(side="left", padx=(0, 5))
        tk.Button(set_frame, text="打开目录", font=FNT_S, fg="white",
                  bg=C["bar"], relief="flat", cursor="hand2", padx=6, pady=1,
                  command=self._open_dir_selected).pack(side="left", padx=(0, 10))

        tk.Label(set_frame, text="分段:", font=FNT_S, fg=C["dim"],

                 bg=C["card"]).pack(side="left")

        seg_options = ["不限", "30分钟", "1小时", "2小时", "4小时"]

        seg_combo = ttk.Combobox(set_frame, textvariable=self._segment_var,

                     values=seg_options, font=FNT_S, width=8,

                     state="readonly")

        seg_combo.pack(side="left", padx=(2, 10))

        seg_combo.bind("<<ComboboxSelected>>", lambda e: self._save_tasks())



        tk.Label(set_frame, text="检测:", font=FNT_S, fg=C["dim"],

                 bg=C["card"]).pack(side="left")

        interval_spin = tk.Spinbox(set_frame, from_=5, to=300,

                   textvariable=self._check_interval_var,

                   width=4, font=FNT_S, bg=C["inp"], fg=C["text"],

                   relief="flat", bd=0, buttonbackground=C["inp"])

        interval_spin.pack(side="left", padx=(2, 0))

        interval_spin.bind("<KeyRelease>", lambda e: self._save_tasks())

        interval_spin.bind("<<Increment>>", lambda e: self._save_tasks())

        interval_spin.bind("<<Decrement>>", lambda e: self._save_tasks())

        tk.Label(set_frame, text="秒", font=FNT_S, fg=C["dim"],
                 bg=C["card"]).pack(side="left", padx=(2, 0))

        tk.Label(self, text="⚠ 免责声明：本工具仅用于录制您拥有版权或已获授权的直播内容。请遵守抖音等平台用户协议，录制和发布产生的法律责任由用户自行承担。",
              font=("Microsoft YaHei", 8), fg="#666", bg=C["card"], anchor="w").pack(fill="x", padx=12, pady=(0, 2))

        # Log area

        self._log_text = tk.Text(self, font=FNT_L, bg=C["inp"],

                                  fg=C["dim"], relief="flat", bd=0, height=5)

        self._log_text.pack(fill="x", pady=(2, 0))



        # Timer

        self._update_timer()



    # ── Log ──



    def _log(self, msg, _type="info"):

        tag_map = {"info": C["text"], "ok": "#00c853", "warn": "#ff9f0a", "err": "#e74c3c"}

        tag = _type if _type in tag_map else "info"

        if hasattr(self, "_log_text"):

            try:

                self._log_text.insert("end", msg + "\n", tag)

                self._log_text.tag_config(tag, foreground=tag_map.get(tag, C["text"]))

                self._log_text.see("end")

            except:

                pass

        print(f"[{_type}] {msg}")



    # ────────────────────────────────────────────

    #  Task management

    # ────────────────────────────────────────────



    def _tasks_path(self):

        return os.path.join(self._get_save_dir(), "_live_recorder_tasks.json")



    def _save_tasks(self):

        save_dir = self._get_save_dir()

        try:

            os.makedirs(save_dir, exist_ok=True)

            data = []

            for t in self._tasks:

                item = {"id": t["id"], "name": t["name"], "platform": t["platform"],

                        "stream_url": t["stream_url"]}

                data.append(item)

            settings = {

                "save_dir": self._save_dir,

                "segment": self._segment_var.get(),

                "check_interval": self._check_interval_var.get()

            }

            with open(self._tasks_path(), "w", encoding="utf-8") as f:

                json.dump({"settings": settings, "tasks": data}, f, ensure_ascii=False, indent=2)

        except Exception as e:

            self._log("保存任务失败: " + str(e), "err")



    def _load_tasks(self):

        tp = self._tasks_path()

        if not os.path.exists(tp):

            return

        try:

            with open(tp, "r", encoding="utf-8") as f:

                raw = json.load(f)



            # Support both new format {settings, tasks} and old format [list]

            if isinstance(raw, dict) and "settings" in raw:

                settings = raw.get("settings", {})

                if settings.get("save_dir"):

                    self._save_dir = settings["save_dir"]

                    self._save_dir_var.set(self._save_dir)

                if settings.get("segment"):

                    self._segment_var.set(settings["segment"])

                if settings.get("check_interval"):

                    self._check_interval_var.set(int(settings["check_interval"]))

                data = raw.get("tasks", [])

            else:

                data = raw if isinstance(raw, list) else []



            for d in data:

                task = {

                    "id": d["id"],

                    "name": d["name"],

                    "platform": d["platform"],

                    "stream_url": d["stream_url"],

                    "status": "idle",

                    "recording_process": None,

                    "recording_start": 0,

                    "recording_file": "",

                    "recording_size": 0,

                    "error_msg": "",

                    "_sel_monitoring": False,

                }

                self._tasks.append(task)

                if d["id"] >= self._task_id_counter:

                    self._task_id_counter = d["id"] + 1

            self._log("已加载 " + str(len(data)) + " 个录制任务", "info")

            self._refresh_task_list()

        except Exception as e:

            self._log("加载任务失败: " + str(e), "err")



    def _add_task_dialog(self):

        dialog = tk.Toplevel(self)

        dialog.title("新增录制任务")

        dialog.configure(bg=C["bg"])

        dialog.transient(self)

        dialog.grab_set()

        w, h = 480, 320

        sw = dialog.winfo_screenwidth()

        sh = dialog.winfo_screenheight()

        dialog.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")



        frame = tk.Frame(dialog, bg=C["card"], padx=16, pady=12)

        frame.pack(fill="both", expand=True)



        nf = tk.Frame(frame, bg=C["card"])

        nf.pack(fill="x", pady=(4, 0))

        tk.Label(nf, text="名称:", font=FNT_S, fg=C["dim"],

                 bg=C["card"]).pack(side="left")

        name_var = tk.StringVar()

        tk.Entry(nf, textvariable=name_var, font=FNT_S,

                 bg=C["inp"], fg=C["text"], relief="flat", bd=0,

                 width=30).pack(side="left", padx=(8, 0))



        uf = tk.Frame(frame, bg=C["card"])

        uf.pack(fill="x", pady=(8, 0))

        tk.Label(uf, text="地址:", font=FNT_S, fg=C["dim"],

                 bg=C["card"]).pack(side="left")

        url_var = tk.StringVar()

        tk.Entry(uf, textvariable=url_var, font=FNT_S,

                 bg=C["inp"], fg=C["text"], relief="flat", bd=0,

                 width=40).pack(side="left", padx=(8, 0), fill="x", expand=True)



        pf = tk.Frame(frame, bg=C["card"])

        pf.pack(fill="x", pady=(8, 0))

        tk.Label(pf, text="平台:", font=FNT_S, fg=C["dim"],

                 bg=C["card"]).pack(side="left")

        plat_var = tk.StringVar(value="自定义RTMP")

        plat_combo = ttk.Combobox(pf, textvariable=plat_var,

                                   values=list(self.PLATFORM_PATTERNS.keys()),

                                   font=FNT_S, width=15, state="readonly")

        plat_combo.pack(side="left", padx=(8, 0))



        detect_btn = tk.Button(pf, text="检测流地址", font=FNT_S, fg="white",

                                bg="#FF9F0A", relief="flat", cursor="hand2",

                                padx=8, pady=2,

                                command=lambda: self._detect_stream(url_var, plat_var, detect_btn))

        detect_btn.pack(side="right")



        def do_add():

            name = name_var.get().strip()

            url = url_var.get().strip()

            platform = plat_var.get()

            if not url:

                messagebox.showwarning("提示", "请输入直播地址")

                return

            if not name:

                name = platform + "直播_" + str(len(self._tasks) + 1)

            self._add_task(name, url, platform)

            dialog.destroy()



        bf = tk.Frame(frame, bg=C["card"])

        bf.pack(fill="x", pady=(16, 0))

        tk.Button(bf, text="确定", font=FNT_B, fg="white",

                  bg=C["btn_sel"], relief="flat", cursor="hand2", padx=20, pady=4,

                  command=do_add).pack(side="right", padx=(8, 0))

        tk.Button(bf, text="取消", font=FNT_S, fg=C["dim"],

                  bg=C["inp"], relief="flat", cursor="hand2", padx=12, pady=4,

                  command=dialog.destroy).pack(side="right")



    def _detect_stream(self, url_var, plat_var, btn):

        url = url_var.get().strip()

        if not url:

            self._log("请先输入直播地址", "warn")

            return

        btn.configure(state="disabled", text="解析中...")

        self._log("正在解析直播流地址...", "info")



        def _do_detect():

            try:

                from douyin_stream import extract_live_url

                stream_url = extract_live_url(url, self._log)

                if stream_url:

                    url_var.set(stream_url)

                    plat_var.set("自定义RTMP")

                    self._log("解析成功，已填入流地址", "ok")

                else:

                    self._log("未解析到流地址，请确认直播间是否开播", "err")

            except Exception as e:

                self._log("解析失败: " + str(e), "err")

            finally:

                btn.configure(state="normal", text="检测流地址")



        threading.Thread(target=_do_detect, daemon=True).start()



    def _add_task(self, name, url, platform="自定义RTMP"):

        task = {

            "id": self._task_id_counter,

            "name": name,

            "platform": platform,

            "stream_url": url,

            "status": "idle",

            "recording_process": None,

            "recording_start": 0,

            "recording_file": "",

            "recording_size": 0,

            "error_msg": "",

            "_sel_monitoring": False,

        }

        self._task_id_counter += 1

        self._tasks.append(task)

        self._log("新增录制: " + name + " (" + platform + ")", "ok")

        self._refresh_task_list()

        self._save_tasks()



    def _delete_task(self, task_id):

        task = next((t for t in self._tasks if t["id"] == task_id), None)

        if task and task["status"] == "recording":

            self._stop_recording(task)

        self._tasks = [t for t in self._tasks if t["id"] != task_id]

        self._refresh_task_list()

        self._save_tasks()



    def _batch_delete(self):

        if not self._tasks:

            return

        if messagebox.askyesno("确认", "确认删除所有录制任务?"):

            for task in self._tasks[:]:

                self._delete_task(task["id"])

            self._log("已删除所有任务", "info")

            self._refresh_task_list()



    def _refresh_task_list(self):

        for w in self._task_inner.winfo_children():

            w.destroy()



        for task in self._tasks:

            status = task["status"]

            if self._current_filter != "全部":

                fm = {"录制中": "recording", "直播中": "live", "未开播": "idle",

                      "录制错误": "error"}

                if fm.get(self._current_filter) != status:

                    continue



            dur = ""

            if status == "recording" and task["recording_start"]:

                elapsed = int(time.time() - task["recording_start"])

                m, s = divmod(elapsed, 60)

                h, m = divmod(m, 60)

                dur = "{:02d}:{:02d}:{:02d}".format(h, m, s)



            fsize = ""

            if task["recording_file"] and os.path.exists(task["recording_file"]):

                sz = os.path.getsize(task["recording_file"])

                fsize = "{:.1f}MB".format(sz/1024/1024) if sz > 1024*1024 else "{:.0f}KB".format(sz/1024)



            s_label = self._get_status_label(status)



            row = tk.Frame(self._task_inner, bg=C["card"], padx=8, pady=3)

            row.pack(fill="x", pady=1)

            row.tid = task["id"]

            for w in (row,):

                pass



            bg_color = C["card"]

            if status == "recording":

                bg_color = "#1a3a1a"

            elif status == "error":

                bg_color = "#3a1a1a"



            tk.Label(row, text=task["name"][:20], font=FNT_S, fg=C["text"],

                     bg=bg_color, width=20, anchor="w").pack(side="left")

            tk.Label(row, text=task["platform"], font=FNT_S, fg=C["dim"],

                     bg=bg_color, width=8, anchor="w").pack(side="left")

            tk.Label(row, text=task["stream_url"][:25], font=FNT_S, fg=C["dim"],

                     bg=bg_color, width=25, anchor="w").pack(side="left")

            tk.Label(row, text=s_label, font=FNT_S, fg=C["text"],

                     bg=bg_color, width=8, anchor="w").pack(side="left")

            tk.Label(row, text=dur, font=FNT_S, fg=C["text"],

                     bg=bg_color, width=10, anchor="w").pack(side="left")

            tk.Label(row, text=fsize, font=FNT_S, fg=C["dim"],

                     bg=bg_color, width=10, anchor="w").pack(side="left")



            tid = task["id"]

            if status == "recording":

                btn = tk.Button(row, text="停止录制", font=FNT_S, fg="white",

                                bg="#E74C3C", relief="flat", cursor="hand2",

                                padx=6, pady=1,

                                command=lambda tid=tid: self._rec_btn_stop(tid))

            else:

                btn = tk.Button(row, text="开始录制", font=FNT_S, fg="white",

                                bg="#4fc3f7", relief="flat", cursor="hand2",

                                padx=6, pady=1,

                                command=lambda tid=tid: self._rec_btn_start(tid))

            btn.pack(side="right", padx=(4, 0))
            del_btn = tk.Button(row, text="删除", font=FNT_S, fg="white",
                                bg="#e67e22", relief="flat", cursor="hand2",
                                padx=4, pady=1,
                                command=lambda tid=tid: self._delete_single(tid))
            del_btn.pack(side="right", padx=(2, 0))
            open_btn = tk.Button(row, text="目录", font=FNT_S, fg="white",
                                 bg="#3498db", relief="flat", cursor="hand2",
                                 padx=4, pady=1,
                                 command=lambda tid=tid: self._open_dir_for(tid))
            open_btn.pack(side="right", padx=(2, 0))

#             self._sel_count_label.configure(text="共 " + str(len(self._tasks)) + " 个")



    def _get_status_label(self, status):

        m = {"idle": "未开播", "live": "直播中", "recording": "录制中",

             "error": "录制错误", "done": "录制完成"}

        return m.get(status, status)



    def _resolve_stream_url(self, url, task_for_cache=None):

        if url.endswith(('.flv', '.m3u8')):

            return url

        if 'douyin.com' in url:

            try:

                from douyin_stream import extract_live_url

                resolved = extract_live_url(url, self._log)

                if resolved:

                    if task_for_cache is not None:

                        task_for_cache['_resolved_url'] = resolved

                    return resolved

            except Exception:

                pass

        return url



    def _wait_and_record(self, task):
        while not task.get("_waiting_cancel", False):
            url = task["stream_url"]
            stream_url = url
            resolved = False
            if "douyin.com" in url and not url.endswith((".flv", ".m3u8")):
                try:
                    from douyin_stream import extract_live_url
                    fresh = extract_live_url(url, self._log)
                    if fresh:
                        stream_url = fresh
                        resolved = True
                except:
                    pass
            if resolved:
                self._log(f"检测到开播，自动开始录制: {task['name']}")
                self.after(0, lambda t=task, su=stream_url: self._start_recording(t, forced_url=su))
                return
            elif url.endswith((".flv", ".m3u8")):
                try:
                    proc = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", url], capture_output=True, text=True, timeout=10)
                    if proc.returncode == 0:
                        self._log(f"检测到开播，自动开始录制: {task['name']}")
                        self.after(0, lambda t=task, su=url: self._start_recording(t, forced_url=su))
                        return
                except:
                    pass
            time.sleep(15)
        self._log(f"等待录制已取消: {task['name']}")

    def _delete_single(self, tid):
        task = next((t for t in self._tasks if t["id"] == tid), None)
        if task:
            if task["status"] == "recording":
                self._stop_recording(task)
            self._tasks.remove(task)
            self._refresh_task_list()
            self._save_tasks()

    def _open_dir_for(self, tid):
        task = next((t for t in self._tasks if t["id"] == tid), None)
        if task and task.get("recording_file"):
            d = os.path.dirname(task["recording_file"])
            os.startfile(d)

    def _rec_btn_start(self, tid):
        task = next((t for t in self._tasks if t["id"] == tid), None)
        if task:
            task["status"] = "waiting"
            task["_waiting_cancel"] = False
            self._refresh_task_list()
            self._save_tasks()
            self._log(f"等待开播: {task['name']}")
            threading.Thread(target=self._wait_and_record, args=(task,), daemon=True).start()

    def _rec_btn_stop(self, tid):
        task = next((t for t in self._tasks if t["id"] == tid), None)
        if task:
            if task["status"] == "waiting":
                task["_waiting_cancel"] = True
                task["status"] = "idle"
                self._refresh_task_list()
                self._save_tasks()
                self._log(f"已取消等待: {task['name']}")
            else:
                self._stop_recording(task)



    def _delete_selected(self):

        if not self._tasks:

            return

        tid = self._selected_tid_var.get()

        if tid is None and self._tasks:

            tid = self._tasks[0]["id"]

        if tid is None:

            return

        for task in self._tasks[:]:

            if task["id"] == tid:

                if task["status"] == "recording":

                    self._stop_recording(task)

                self._tasks.remove(task)

                break

        self._refresh_task_list()

        self._save_tasks()

        self._selected_task_id = None



    def _on_row_click(self, event):

        w = event.widget

        # Walk up to find the row frame (which has .tid)

        while w != self._task_inner:

            if hasattr(w, 'tid'):

                self._select_row(w.tid)

                return

            w = w.master

        # If clicked on row frame itself

        if hasattr(event.widget, 'tid'):

            self._select_row(event.widget.tid)



    def _select_row(self, tid):

        self._selected_task_id = tid

        for ch in self._task_inner.winfo_children():

            if not isinstance(ch, tk.Frame): continue

            bg = "#1a3a1a" if getattr(ch, 'tid', None) and any(t["id"] == getattr(ch, 'tid') and t["status"] == "recording" for t in self._tasks) else C["bg"]

            ch.configure(bg=bg)

            for w in ch.winfo_children():

                if isinstance(w, tk.Label):

                    w.configure(bg=bg)

        for ch in self._task_inner.winfo_children():

            if getattr(ch, 'tid', None) == tid:

                ch.configure(bg="#2a2a5e")

                for w in ch.winfo_children():

                    if isinstance(w, tk.Label):

                        w.configure(bg="#2a2a5e")

                return



    def _open_dir_selected(self):

        if not self._tasks:

            self._log("没有录制任务", "warn")

            return

        task = self._tasks[0]

        if task and task["recording_file"]:

            d = os.path.dirname(task["recording_file"])

            if os.path.exists(d):

                os.startfile(d)

        else:

            d = self._get_save_dir()

            if os.path.exists(d):

                os.startfile(d)



    def _open_video_dir(self):

        d = self._get_save_dir()

        if os.path.exists(d):

            os.startfile(d)



    def _get_save_dir(self):

        return self._save_dir



    def _browse_save_dir(self):

        d = filedialog.askdirectory(initialdir=self._save_dir)

        if d:

            self._save_dir_var.set(d)

            self._save_dir = d

            self._save_tasks()

            self._log("保存目录已设置: " + d, "ok")



    def _apply_save_dir(self):

        d = self._save_dir_var.get().strip()

        if d and os.path.isdir(d):

            self._save_dir = d

            self._log("录制保存目录已设为: " + d, "ok")

        else:

            self._log("目录无效: " + d, "err")



    # ────────────────────────────────────────────

    #  Timer

    # ────────────────────────────────────────────



    def _update_timer(self):

        # In-place label update for recording tasks only (no rebuild, no flash)

        has_rec = False

        now = time.time()

        for task in self._tasks:

            if task["status"] == "recording" and task["recording_start"]:

                has_rec = True

                elapsed = int(now - task["recording_start"])

                m, s = divmod(elapsed, 60)

                h, m = divmod(m, 60)

                dur = "{:02d}:{:02d}:{:02d}".format(h, m, s)

                fsize = ""

                if task["recording_file"] and os.path.exists(task["recording_file"]):

                    sz = os.path.getsize(task["recording_file"])

                    fsize = "{:.1f}MB".format(sz/1024/1024) if sz > 1024*1024 else "{:.0f}KB".format(sz/1024)

                # Update row labels in-place

                for ch in self._task_inner.winfo_children():

                    labs = [w for w in ch.winfo_children() if isinstance(w, tk.Label)]

                    if len(labs) >= 6 and labs[0].cget("text") == task["name"][:20]:

                        labs[4].configure(text=dur)

                        labs[5].configure(text=fsize)

                        break

        if has_rec:

            self.after(1000, self._update_timer)

        else:

            # Check again in 1s in case recording just started

            self.after(1000, self._update_timer)



    def _update_task_durations(self):

        self._refresh_task_list()



    # ────────────────────────────────────────────

    #  Monitor (batch)

    # ────────────────────────────────────────────



    def _toggle_monitor(self):

        if self._monitor_running:

            self._batch_stop_monitor()

        else:

            self._batch_start_monitor()



    def _batch_start_monitor(self):

        if self._monitor_running:

            self._log("监控已在运行中", "info")

            return

        if not self._tasks:

            self._log("No tasks, please add via + button first", "warn")

            return

        try:

            from license_guard import require_feature_access

            if not require_feature_access("直播录制", self.winfo_toplevel(), self._log, refresh=False):

                return

        except Exception as e:

            self._log("授权检查异常: " + str(e), "err")

            return

        self._start_monitor_bg()

        if hasattr(self, "_monitor_btn"):

            self._monitor_btn.configure(text="停止监控", bg="#E74C3C")

        self._log("已开启 " + str(len(self._tasks)) + " 个监控", "ok")



    def _batch_stop_monitor(self):

        if not self._monitor_running:

            self._log("监控已停止", "info")

            return

        self._stop_monitor_bg()

        time.sleep(1)

        for task in self._tasks:

            if task["status"] == "recording":

                self._stop_recording(task)

                self.after(100, lambda: None)  # yield to tk

            task["status"] = "idle"

            task["_sel_monitoring"] = False

        if hasattr(self, "_monitor_btn"):

            self._monitor_btn.configure(text="开始监控", bg=C["btn_go"])

        self._log("已停止监控和录制", "info")

        self._refresh_task_list()



    def _auto_start_monitor(self):

        """Auto-start monitoring on startup if tasks exist"""

        if self._tasks and not self._monitor_running:

            self._batch_start_monitor()



    def _start_monitor_bg(self):

        if self._monitor_running:

            self._log("监控已在运行", "info")

            return

        self._monitor_running = True

        self._stop_monitor.clear()

        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)

        self._monitor_thread.start()

        interval = self._check_interval_var.get()

        self._log("开播检测已启动（每" + str(interval) + "秒检测一次）", "info")



    def _stop_monitor_bg(self):

        self._stop_monitor.set()

        if self._monitor_thread:

            self._monitor_thread.join(timeout=3)

            self._monitor_thread = None

        self._monitor_running = False

        self._log("检测已停止", "info")



    # ────────────────────────────────────────────

    #  Single task monitor

    # ────────────────────────────────────────────



    def _monitor_loop(self):

        while not self._stop_monitor.is_set():

            for task in self._tasks:

                if self._stop_monitor.is_set():

                    return

                if task["status"] not in ("idle", "live"):

                    continue

                url = task["stream_url"]

                stream_url = url

                resolved = False

                if "douyin.com" in url and not url.endswith(".flv") and not url.endswith(".m3u8"):

                    try:

                        from douyin_stream import extract_live_url

                        fresh = extract_live_url(url, self._log)

                        if fresh:

                            stream_url = fresh

                            resolved = True

                    except:

                        pass



                if resolved:

                    # extract_live_url success = m3u8 received = stream is live

                    if task["status"] == "idle":

                        task["status"] = "live"

                        self._log("检测到开播: " + task["name"], "info")

                        self.after(0, self._refresh_task_list)

                        self._log("检测到流，准备自动录制: " + task["name"], "info")

                        self.after(0, lambda t=task, su=stream_url: self._start_recording(t, forced_url=su))

                    elif task["status"] == "live":

                        self.after(0, self._refresh_task_list)

                else:

                    # For m3u8/flv urls directly, use check_stream

                    try:

                        if self._check_stream(stream_url):

                            if task["status"] == "idle":

                                task["status"] = "live"

                                self._log("检测到开播: " + task["name"], "info")

                                self.after(0, self._refresh_task_list)

                                self._log("检测到流，准备自动录制: " + task["name"], "info")

                                self.after(0, lambda t=task, su=stream_url: self._start_recording(t, forced_url=su))

                            elif task["status"] == "live":

                                self.after(0, self._refresh_task_list)

                        else:

                            if task["status"] == "live":

                                task["status"] = "idle"

                                self.after(0, self._refresh_task_list)

                    except:

                        pass



            interval = self._check_interval_var.get()

            for _ in range(interval):

                if self._stop_monitor.is_set():

                    return

                time.sleep(1)



    def _check_stream(self, url):

        """Quick check if stream is accessible via ffprobe"""

        # Auto-resolve douyin links

        check_url = url

        if "douyin.com" in url and not url.endswith(".flv") and not url.endswith(".m3u8"):

            try:

                from douyin_stream import extract_live_url

                fresh = extract_live_url(url, None)

                if fresh:

                    check_url = fresh

            except:

                pass

        try:

            from config import FFMPEG_PATH

            ffprobe = (FFMPEG_PATH or "ffprobe").replace("ffmpeg", "ffprobe")

        except:

            ffprobe = "ffprobe"

        try:

            r = subprocess.run([ffprobe, "-v", "quiet", "-print_format", "json",

                                "-show_streams", "-i", check_url],

                               timeout=10, capture_output=True)

            return r.returncode == 0

        except:

            return False



    # ────────────────────────────────────────────

    #  Recording

    # ────────────────────────────────────────────



    def _start_recording(self, task, forced_url=None):

        try:

            from license_guard import require_feature_access

            if not require_feature_access("直播录制", self.winfo_toplevel(), self._log, refresh=False):

                task["status"] = "idle"

                self._refresh_task_list()

                return

        except Exception as e:

            task["status"] = "error"

            task["error_msg"] = str(e)

            self._log("授权检查异常: " + str(e), "err")

            self._refresh_task_list()

            return

        try:

            from config import FFMPEG_PATH

            ffmpeg = FFMPEG_PATH or "ffmpeg"

        except:

            ffmpeg = "ffmpeg"



        if forced_url:

            stream_url = forced_url

            self._log("使用监控检测到的流地址", "info")

        else:

            stream_url = task["stream_url"]



        source_url = stream_url

        if "douyin.com" in stream_url or "live.douyin" in stream_url:

            source_url = stream_url

        elif task.get("_source_url"):

            source_url = task["_source_url"]

        try:

            from douyin_stream import extract_live_url

            fresh = extract_live_url(source_url, None)

            if fresh:

                stream_url = fresh

                self._log("已获取最新流地址", "ok")

        except Exception as e:

            self._log("重新解析流地址失败，使用旧地址: " + str(e), "warn")



        save_dir = self._get_save_dir()

        os.makedirs(save_dir, exist_ok=True)

        ts = time.strftime("%Y%m%d_%H%M%S")

        safe_name = re.sub(r'[\\/:*?"<>|]', '_', task["name"])[:10]


        # 缩短文件名防路径过长
        ts_short = time.strftime("%m%d_%H%M")
        short_hash = str(hash(task["name"] + ts))[-6:]

        # Each task gets its own folder

        task_dir = os.path.join(save_dir, safe_name)

        os.makedirs(task_dir, exist_ok=True)

        output = os.path.join(task_dir, safe_name[:6] + "_" + short_hash + "_" + ts_short + ".flv")



        seg_str = self._segment_var.get() if hasattr(self, "_segment_var") else "不限"

        dur_map = {"不限": 36000, "30分钟": 1800, "1小时": 3600, "2小时": 7200, "4小时": 14400}

        max_dur = dur_map.get(seg_str, 36000)





        cmd = [ffmpeg, "-y",

               "-reconnect", "1", "-reconnect_streamed", "1",

               "-reconnect_delay_max", "30",

               "-i", stream_url,

               "-c", "copy",

               "-t", str(max_dur),

               output]



        try:

            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,

                                    stderr=subprocess.DEVNULL)

            task["status"] = "recording"

            task["recording_process"] = proc

            task["recording_start"] = time.time()

            task["recording_file"] = output

            self._log("开始录制: " + task["name"] + " -> " + os.path.basename(output), "ok")

            self._refresh_task_list()

            self._update_timer()

            threading.Thread(target=self._monitor_recording, args=(task,), daemon=True).start()

        except Exception as e:

            task["status"] = "error"

            task["error_msg"] = str(e)

            self._log("录制失败 [" + task["name"] + "]: " + str(e), "err")

            self._refresh_task_list()



    def _stop_recording(self, task):

        if task["recording_process"]:

            proc = task["recording_process"]

            fpath = task.get("recording_file", "")

            try:

                proc.terminate()

                proc.wait(timeout=5)

            except subprocess.TimeoutExpired:

                try:

                    proc.kill()

                except:

                    pass

            except Exception:

                try:

                    proc.kill()

                except:

                    pass

            task["recording_process"] = None



        # 录制完成后保持 live 状态（避免监控重复触发）

        if task["status"] != "error":

            task["status"] = "live"



        fpath = task.get("recording_file", "")

        if fpath and os.path.exists(fpath):

            sz = os.path.getsize(fpath)

            self._log("录制完成: " + os.path.basename(fpath) + " ({:.1f}MB)".format(sz/1024/1024), "ok")

            try:

                from license_guard import consume_trial_after_success

                consume_trial_after_success("直播录制", root=None, log_fn=self._log)

            except Exception:

                pass

        self._refresh_task_list()



    def _monitor_recording(self, task):

        """Monitor a recording process - update info until done"""

        proc = task["recording_process"]

        if not proc:

            return

        try:

            proc.wait()

        except:

            pass

        if task["status"] == "recording":

            self.after(0, lambda t=task: self._stop_recording(t))



    # ────────────────────────────────────────────

    #  Recorded videos page

    # ────────────────────────────────────────────



    def _build_videos_page(self, parent):

        bar = tk.Frame(parent, bg=C["card"], padx=12, pady=6)

        bar.pack(fill="x")

        tk.Label(bar, text="已录制视频", font=FNT_T, fg=C["text"],

                 bg=C["card"]).pack(side="left")

        tk.Button(bar, text="刷新", font=FNT_S, fg="white", bg=C["btn_sel"],

                  relief="flat", cursor="hand2", padx=10, pady=2,

                  command=self._refresh_videos).pack(side="right")

        tk.Button(bar, text="开目录", font=FNT_S, fg="white", bg=C["bar"],

                  relief="flat", cursor="hand2", padx=10, pady=2,

                  command=self._open_video_dir).pack(side="right", padx=(0, 4))



        col_frame = tk.Frame(parent, bg=C["card"],

           highlightbackground=C["card_border"], highlightthickness=1)

        col_frame.pack(fill="both", expand=True, padx=0, pady=4)



        columns = ("name", "date", "size", "path")

        self._video_tree = ttk.Treeview(col_frame, columns=columns, show="headings", height=12)

        self._video_tree.heading("name", text="文件名")

        self._video_tree.heading("date", text="录制时间")

        self._video_tree.heading("size", text="大小")

        self._video_tree.heading("path", text="路径")



        self._video_tree.column("name", width=200)

        self._video_tree.column("date", width=100)

        self._video_tree.column("size", width=80)

        self._video_tree.column("path", width=300)



        vsb = ttk.Scrollbar(col_frame, orient="vertical", command=self._video_tree.yview)

        self._video_tree.configure(yscrollcommand=vsb.set)

        self._video_tree.pack(side="left", fill="both", expand=True)

        vsb.pack(side="right", fill="y")



        self._log_text_videos = tk.Text(parent, font=FNT_L, bg=C["inp"],

                                         fg=C["dim"], relief="flat", bd=0, height=4)

        self._log_text_videos.pack(fill="x", pady=(0, 2))



    def _refresh_videos(self):

        for item in self._video_tree.get_children():

            self._video_tree.delete(item)

        save_dir = self._get_save_dir()

        if not os.path.exists(save_dir):

            self._vlog("录制目录不存在: " + save_dir)

            return

        files = [f for f in os.listdir(save_dir) if f.endswith((".mp4", ".ts", ".flv"))]

        files.sort(reverse=True)

        for f in files[:200]:

            fp = os.path.join(save_dir, f)

            try:

                sz = os.path.getsize(fp)

                sz_str = "{:.1f}MB".format(sz/1024/1024) if sz > 1024*1024 else "{:.0f}KB".format(sz/1024)

                mtime = os.path.getmtime(fp)

                date_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")

                self._video_tree.insert("", "end", values=(f, date_str, sz_str, fp))

            except:

                pass

        self._vlog("已加载 " + str(len(files)) + " 个视频文件")



    def _vlog(self, msg):

        try:

            self._log_text_videos.insert("end", msg + "\n")

            self._log_text_videos.see("end")

        except:

            pass



    # ────────────────────────────────────────────

    #  Settings page

    # ────────────────────────────────────────────



    def _build_settings_page(self, parent):

        bar = tk.Frame(parent, bg=C["card"], padx=12, pady=6)

        bar.pack(fill="x")

        tk.Label(bar, text="录制设置", font=FNT_T, fg=C["text"],

                 bg=C["card"]).pack(side="left")



        sf = tk.Frame(parent, bg=C["card"], padx=16, pady=10,

                      highlightbackground=C["card_border"], highlightthickness=1)

        sf.pack(fill="x", pady=6)



        r1 = tk.Frame(sf, bg=C["card"])

        r1.pack(fill="x", pady=4)

        tk.Label(r1, text="录制保存目录:", font=FNT_S, fg=C["dim"],

                 bg=C["card"]).pack(side="left")

        self._save_dir_var = tk.StringVar(value=self._save_dir)

        tk.Entry(r1, textvariable=self._save_dir_var, font=FNT_S,

                 bg=C["inp"], fg=C["text"], relief="flat", bd=0,

                 width=50).pack(side="left", padx=(8, 4), fill="x", expand=True)

        tk.Button(r1, text="浏览", font=FNT_S, fg="white", bg=C["btn_sel"],

                  relief="flat", cursor="hand2", padx=8,

                  command=self._browse_save_dir).pack(side="right")

        tk.Button(r1, text="应用", font=FNT_S, fg="white", bg=C["bar"],

                  relief="flat", cursor="hand2", padx=8,

                  command=self._apply_save_dir).pack(side="right", padx=(0, 4))



        r2 = tk.Frame(sf, bg=C["card"])

        r2.pack(fill="x", pady=6)

        tk.Label(r2, text="录制分段:", font=FNT_S, fg=C["dim"],

                 bg=C["card"]).pack(side="left")

        seg_options = ["不限", "30分钟", "1小时", "2小时", "4小时"]

        self._segment_var = tk.StringVar(value=seg_options[0])

        ttk.Combobox(r2, textvariable=self._segment_var,

                     values=seg_options, font=FNT_S, width=10,

                     state="readonly").pack(side="left", padx=(8, 0))

        tk.Label(r2, text="（超时自动分段）", font=FNT_S,

                 fg=C["dim"], bg=C["card"]).pack(side="left", padx=(8, 0))



        r3 = tk.Frame(sf, bg=C["card"])

        r3.pack(fill="x", pady=4)

        tk.Label(r3, text="开播检测间隔:", font=FNT_S, fg=C["dim"],

                 bg=C["card"]).pack(side="left")

        tk.Spinbox(r3, from_=5, to=300, textvariable=self._check_interval_var,

                   width=4, font=FNT_S, bg=C["inp"], fg=C["text"],

                   relief="flat", bd=0, buttonbackground=C["inp"]).pack(side="left", padx=(8, 0))

        tk.Label(r3, text="秒", font=FNT_S, fg=C["dim"],

                 bg=C["card"]).pack(side="left", padx=(4, 0))



        self._log_text_settings = tk.Text(parent, font=FNT_L, bg=C["inp"],

                                           fg=C["dim"], relief="flat", bd=0, height=6)

        self._log_text_settings.pack(fill="x", pady=(4, 0))



        self._log_text = self._log_text_settings
