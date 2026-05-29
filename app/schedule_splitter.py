"""

时间表分割工具 V3 — 修复时间偏移计算

"""

import os, re, subprocess

from datetime import datetime

from typing import Optional, List



def _parse_datetime_from_name(path):
    m = re.search(r"(\d{14})", os.path.basename(str(path)))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    except Exception:
        return None


def sort_videos_by_start(video_list):
    """Sort videos by timestamp in filename when every selected file has one."""
    items = [(v, _parse_datetime_from_name(v)) for v in video_list]
    if items and all(dt is not None for _, dt in items):
        return [v for v, _ in sorted(items, key=lambda x: x[1])]
    return list(video_list)




def read_excel(filepath: str, log_fn=None):
    """读取时间表Excel，支持旧格式和新格式自动检测"""
    try:
        import openpyxl
    except ImportError:
        if log_fn: log_fn("需要安装 openpyxl: pip install openpyxl")
        return [], None

    wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return [], None

    # 自动检测表格格式
    header = rows[0] if rows else []
    is_new_format = False
    if header and len(header) >= 5:
        # 新格式检测: 第1列有"商品"且第5+列有"讲解"
        has_product_col = any("商品" in str(h) for h in header[:3] if h)
        has_time_col = any(("讲解" in str(h)) for h in header[4:] if h)
        if has_product_col and has_time_col:
            is_new_format = True

    live_start = None
    results = []

    if is_new_format:
        live_start = _parse_datetime_from_name(filepath)
        if live_start and log_fn:
            log_fn("从文件名识别直播开始时间: %s" % live_start.strftime("%Y-%m-%d %H:%M:%S"))
        # 自动匹配: 商品名称在第0列或第1列, 讲解时段从含"讲解"的列开始
        # 确定商品名称列和讲解时段列
        _name_col = 0
        _time_start = 4
        if len(header) > 1 and header[1] and ("商品标题" in str(header[1]) or "商品名称" in str(header[1])):
            _name_col = 1
            _time_start = 6
        for row in rows[1:]:
            if len(row) <= _time_start:
                continue
            name = str(row[_name_col]).strip() if row[_name_col] else ""
            if not name or name in ("None", "商品标题", "商品封面", ""):
                continue
            pid = str(row[1]).strip() if len(row) > 1 and row[1] and "商品ID" in str(header[1] if len(header) > 1 else "") else (str(row[2]).strip() if len(row) > 2 and row[2] else "")
            for ci in range(_time_start, min(_time_start + 7, len(row))):
                tv = row[ci]
                if tv is None:
                    continue
                ts = str(tv).strip()
                if not ts or ts == "None":
                    continue
                sep = "-" if "-" in ts else ("~" if "~" in ts else None)
                if not sep:
                    continue
                parts = ts.split(sep, 1)
                p1, p2 = parts[0].strip(), parts[1].strip()
                def _to_seconds(t):
                    parts = t.split(":")
                    if len(parts) == 3:
                        return int(parts[0])*3600 + int(parts[1])*60 + int(parts[2])
                    elif len(parts) == 2:
                        return int(parts[0])*60 + int(parts[1])
                    return 0
                if ":" in p1:
                    start = _to_seconds(p1)
                    end = _to_seconds(p2)
                else:
                    continue
                if end <= start:
                    continue
                item = {"name": name, "start_offset": start, "end_offset": end}
                if pid:
                    item["product_id"] = pid
                results.append(item)
    else:
        # 旧格式
        if len(rows) > 1 and rows[1] and rows[1][0]:
            raw = str(rows[1][0]).strip()
            live_start = _parse_datetime(raw)

        for row in rows[1:]:
            if len(row) < 5:
                continue
            name = str(row[4]).strip() if row[4] else ""
            time_str = str(row[2]).strip() if row[2] else ""
            if not name or not time_str or name in ("None", "商品名称"):
                continue
            if time_str in ("None", "讲解日期时间"):
                continue
            start_offset = 0.0
            end_offset = 0.0
            sep = None
            for s in ["～", "〜", "~", "-", " "]:
                if s in time_str:
                    sep = s
                    break
            if sep:
                parts = time_str.split(sep, 1)
                clock_start = _parse_time_only(parts[0].strip())
                clock_end = _parse_time_only(parts[1].strip()) if len(parts) > 1 else None
                if clock_start is None:
                    continue
                start_offset = clock_start
                end_offset = clock_end if clock_end is not None else clock_start + 30
                if live_start:
                    ls = live_start.hour * 3600 + live_start.minute * 60 + live_start.second
                    start_offset -= ls
                    end_offset -= ls
            else:
                dt = _parse_datetime(time_str)
                if dt is None:
                    continue
                if live_start:
                    start_offset = (dt - live_start).total_seconds()
                else:
                    start_offset = dt.hour * 3600 + dt.minute * 60 + dt.second
                end_offset = start_offset + 30
            if start_offset < 0:
                continue
            item = {"name": name, "start_offset": start_offset, "end_offset": end_offset}
            if row[3]:
                item["product_id"] = str(row[3]).strip()
            results.append(item)

    if not results:
        return [], None

    results.sort(key=lambda x: x["start_offset"])
    if log_fn:
        dur = results[-1]["end_offset"] / 3600 if results else 0
        log_fn("解析出 %d 条讲解时段 (总时长 %.1f小时)" % (len(results), dur))
        log_fn("时间范围: %.0fs ~ %.0fs" % (results[0]["start_offset"], results[-1]["end_offset"]))

    # 清理剩余缓存
    try:
        import tempfile
        _td = os.path.join(tempfile.gettempdir(), "livec_schedule")
        if os.path.exists(_td):
            for _f in os.listdir(_td):
                try: os.remove(os.path.join(_td, _f))
                except: pass
            try: os.rmdir(_td)
            except: pass
    except:
        pass
    return results, live_start

def group_by_product(schedule: List[dict]) -> List[dict]:

    groups, order = {}, []

    for item in schedule:

        key = (item.get("product_id") or item["name"]).strip().lower()

        if key not in groups:

            groups[key] = {"name": item["name"], "segments": [], "total_duration": 0}

            if "product_id" in item:

                groups[key]["product_id"] = item["product_id"]

            order.append(key)

        g = groups[key]

        g["segments"].append((item["start_offset"], item["end_offset"]))

        g["total_duration"] += item["end_offset"] - item["start_offset"]



    result = []

    for key in order:

        g = groups[key]

        segs = sorted(g["segments"])

        merged = []

        for s, e in segs:

            if merged and s - merged[-1][1] < 10:

                merged[-1] = (merged[-1][0], max(merged[-1][1], e))

            else:

                merged.append((s, e))

        g["segments"] = merged

        g["total_duration"] = sum(e - s for s, e in merged)

        result.append(g)



    result.sort(key=lambda x: x["total_duration"], reverse=True)

    return result





_video_durations_cache = {}

def _parse_video_time(filename):

    """从文件名提取视频开始时间（秒从午夜）"""

    import re as _re

    from datetime import datetime as _dt

    m = _re.search(r"(\d{14})", os.path.basename(filename))

    if m:

        try:

            dt = _dt.strptime(m.group(1), "%Y%m%d%H%M%S")

            return dt.hour * 3600 + dt.minute * 60 + dt.second

        except:

            pass

    return None



def _get_video_timeline(video_list, durations):

    """计算每个视频在时间线上的起始偏移（相对于 live_start）"""

    import re as _re

    from datetime import datetime as _dt

    starts = []

    # 先尝试从文件名解析

    all_ok = True

    for v in video_list:

        m = _re.search(r"(\d{14})", os.path.basename(v))

        if m:

            try:

                dt = _dt.strptime(m.group(1), "%Y%m%d%H%M%S")

                starts.append(dt.hour * 3600 + dt.minute * 60 + dt.second)

            except:

                starts.append(None)

                all_ok = False

        else:

            starts.append(None)

            all_ok = False

    # 如果有解析失败的，用累积时长兜底

    if not all_ok:

        cum = 0

        for i in range(len(video_list)):

            starts[i] = cum

            cum += durations[i] if i < len(durations) else 0

        return starts

    # normalizace: odeect prvn video (relativne k live start)

    base = starts[0]

    for i in range(len(starts)):

        starts[i] -= base

    return starts









def _probe_durations(video_list, log_fn=None, ffmpeg_cmd=None):

    global _video_durations_cache

    cache_key = []
    for v in video_list:
        try:
            cache_key.append((os.path.abspath(v), os.path.getmtime(v), os.path.getsize(v)))
        except Exception:
            cache_key.append((os.path.abspath(v), 0, 0))
    cache_key = tuple(cache_key)

    if cache_key in _video_durations_cache:

        return list(_video_durations_cache[cache_key])

    durations = []

    _probe_cmd = os.path.join(os.path.dirname(ffmpeg_cmd), "ffprobe.exe") if ffmpeg_cmd else "ffprobe"

    for v in video_list:

        try:

            r = subprocess.run([_probe_cmd, "-v", "error", "-show_entries", "format=duration",

                                "-of", "default=noprint_wrappers=1:nokey=1", v],

                               capture_output=True, text=True, timeout=120, creationflags=0x8000000)

            dur = float(r.stdout.strip()) if r.stdout.strip() else 0

            durations.append(dur)

            if log_fn: log_fn("  视频时长: %s = %.0f分" % (os.path.basename(v), dur/60))

        except:

            durations.append(0)

    _video_durations_cache[cache_key] = list(durations)

    return durations





def _find_video_for_time(video_list, offset_sec, durations=None):

    """找到视频，返回 (video_path, relative_offset)"""

    if len(video_list) == 1:

        return video_list[0], offset_sec

    if not durations:

        return video_list[0], offset_sec

    starts = _get_video_timeline(video_list, durations)

    for i, v in enumerate(video_list):

        start = starts[i]

        dur = durations[i] if i < len(durations) else 0

        if start <= offset_sec < start + dur:

            return v, offset_sec - start

    return video_list[-1], offset_sec - starts[-1]





def extract_by_schedule(groups, video_list, output_dir, ffmpeg="ffmpeg", log_fn=None):
    """按分组切割导出每段独立文件，不拼接"""
    import os, subprocess, shutil, tempfile

    results = []

    video_list = sort_videos_by_start(video_list)

    # 计算视频时长和时间线；有时间戳文件名时按文件名时间定位，否则按选择顺序连续拼接定位。
    durations = _probe_durations(video_list, log_fn=log_fn, ffmpeg_cmd=ffmpeg)
    if not durations or len(durations) != len(video_list):
        durations = [99999] * len(video_list)
    starts = _get_video_timeline(video_list, durations)
    timeline = []
    for i, v in enumerate(video_list):
        dur = durations[i] if i < len(durations) else 0
        timeline.append((v, starts[i], starts[i] + dur))

    def _split_across_videos(start, end):
        parts = []
        for video, vs, ve in timeline:
            cut_start = max(start, vs)
            cut_end = min(end, ve)
            if cut_end - cut_start >= 0.5:
                parts.append((video, cut_start - vs, cut_end - cut_start, cut_start, cut_end))
        return parts

    for g in groups:
        name = g.get("name", "")
        segs = g.get("segments", [])
        if not segs:
            continue
        safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in name)[:40]
        out_dir = os.path.join(output_dir, safe_name)
        os.makedirs(out_dir, exist_ok=True)
        exported = 0
        for si, (st, et) in enumerate(segs):
            dur = et - st
            if dur < 10:
                continue
            parts = _split_across_videos(st, et)
            if not parts:
                if log_fn:
                    log_fn("  未找到覆盖时段 %.0fs-%.0fs 的视频" % (st, et))
                continue

            for part_idx, (video, rel_st, part_dur, _, _) in enumerate(parts):
                suffix = "%d" % (si + 1) if len(parts) == 1 else "%d_%d" % (si + 1, part_idx + 1)
                out_path = os.path.join(out_dir, "%s_%s.mp4" % (safe_name[:30], suffix))
                try:
                    # 快切 -c copy
                    r = subprocess.run([ffmpeg, "-y", "-ss", str(float(rel_st)), "-i", video,
                        "-t", str(float(part_dur)), "-c", "copy", out_path],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300, creationflags=0x8000000)
                    if r.returncode != 0 or not (os.path.exists(out_path) and os.path.getsize(out_path) > 1000):
                        # 快切失败，重编码
                        r = subprocess.run([ffmpeg, "-y", "-ss", str(float(rel_st)), "-i", video,
                            "-t", str(float(part_dur)), "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-c:a", "aac", "-b:a", "128k", out_path],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600, creationflags=0x8000000)
                    if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
                        mb = os.path.getsize(out_path) / 1024 / 1024
                        results.append({"name": name, "output_path": out_path, "size_mb": round(mb, 1)})
                        exported += 1
                except Exception as _ee:
                    if log_fn: log_fn("  切割失败: " + str(_ee)[:60])
        if exported and log_fn:
            log_fn("  %s: %d\u6bb5" % (name, exported))

    # \u6e05\u7406\u4e34\u65f6\u76ee\u5f55
    try:
        td = os.path.join(tempfile.gettempdir(), "livec_schedule")
        if os.path.exists(td):
            for f in os.listdir(td):
                try: os.remove(os.path.join(td, f))
                except: pass
            try: os.rmdir(td)
            except: pass
    except:
        pass
    return results
def align_schedule_to_video(schedule, video_list, live_start, log_fn=None, ffmpeg_cmd=None):
    """Align schedule to each video's filename timestamp."""
    import re as _re2

    if not schedule or not video_list or live_start is None:
        return schedule

    align_key = "|".join(os.path.basename(str(v)) for v in video_list) + "|" + live_start.strftime("%Y-%m-%d %H:%M:%S")
    if schedule and all(item.get("_aligned_key") == align_key for item in schedule):
        if log_fn: log_fn("[align] schedule already aligned, skip")
        return schedule

    excel_sec = live_start.hour * 3600 + live_start.minute * 60 + live_start.second

    # Get durations to know each video's time range
    if log_fn:
        log_fn("[align] probing %d video(s) with ffprobe..." % len(video_list))
    durations = _probe_durations(video_list, ffmpeg_cmd=ffmpeg_cmd)
    if not durations or len(durations) != len(video_list):
        if log_fn: log_fn("[align] cannot probe durations, skip")
        return schedule

    if log_fn:
        for i, d in enumerate(durations):
            log_fn("[align]   video %d: %.0f min" % (i+1, d/60))

    total_duration = sum(d for d in durations if d and d > 0)
    try:
        min_start = min(float(item.get("start_offset", 0)) for item in schedule)
        max_end = max(float(item.get("end_offset", 0)) for item in schedule)
    except Exception:
        min_start, max_end = 0, 0
    if total_duration > 0 and min_start >= -300 and max_end <= total_duration + 300:
        if log_fn:
            log_fn("[align] schedule already matches selected video timeline, no adjustment")
        return schedule

    has_any_video_ts = any(_parse_video_time(v) is not None for v in video_list)
    if not has_any_video_ts:
        if log_fn:
            log_fn("[align] video filenames have no precise start timestamp, keep schedule offsets")
        return schedule

    # Build per-video info: (start_abs, end_abs, delta)
    # start_abs = video filename timestamp (seconds from midnight)
    # If filename has no timestamp, use cumulative from previous
    timeline = []  # (abs_start_sec, abs_end_sec, delta_to_excel)
    cum_start = 0
    all_have_ts = True
    for i, v in enumerate(video_list):
        ts = _parse_video_time(v)  # seconds from midnight from filename
        dur = durations[i] if i < len(durations) else 0
        if ts is not None:
            abs_start = ts
            delta = ts - excel_sec
            cum_start = abs_start
        else:
            all_have_ts = False
            abs_start = cum_start
            delta = cum_start - excel_sec
        abs_end = abs_start + dur
        cum_start = abs_end
        timeline.append((abs_start, abs_end, delta, os.path.basename(v)))

    # Log each video's delta
    if log_fn:
        log_fn("[align] --- video timeline ---")
        for i, (als, ale, d, vn) in enumerate(timeline):
            hms_s = "%02d:%02d:%02d" % (als//3600, (als%3600)//60, als%60)
            hms_e = "%02d:%02d:%02d" % (ale//3600, (ale%3600)//60, ale%60)
            base_note = " (base +%ds)" % (d - timeline[0][2]) if abs(d - timeline[0][2]) >= 1 else ""
            log_fn("[align]   video %d: %s~%s  [%s]%s" % (i+1, hms_s, hms_e, vn[:30], base_note))

    # Use first video delta globally (keeps timeline consistent)
    delta = timeline[0][2] if timeline else 0
    if abs(delta) < 1:
        if log_fn: log_fn("[align] videos match excel, no adjustment")
        return schedule
    for item in schedule:
        item["start_offset"] -= delta
        item["end_offset"] -= delta
        item["_aligned_key"] = align_key
    if log_fn:
        log_fn("[align] global skew %+ds, adjusted %d items" % (delta, len(schedule)))
    return schedule

def _parse_datetime(s):
    if s is None:
        return None
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"]:

        try:

            return datetime.strptime(s.strip(), fmt)

        except ValueError:

            continue

    return None





def _parse_time_only(s):
    if s is None:
        return None
    for fmt in ["%H:%M:%S", "%H:%M"]:

        try:

            dt = datetime.strptime(s.strip(), fmt)

            return dt.hour * 3600 + dt.minute * 60 + dt.second

        except ValueError:

            continue

    return None
