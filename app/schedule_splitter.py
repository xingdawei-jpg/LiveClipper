"""

时间表分割工具 V3 — 修复时间偏移计算

"""

import json
import hashlib
import os, re, shutil, subprocess, tempfile, time
from config import sanitize_forbidden_title

from datetime import datetime

from typing import Optional, List


_TS_EDIT_EXTENSIONS = {".ts", ".mts", ".m2ts"}
_NORMALIZE_DISK_RESERVE = 2 * 1024 * 1024 * 1024


def _needs_ts_normalization(path):
    return os.path.splitext(str(path or ""))[1].lower() in _TS_EDIT_EXTENSIONS


def _create_schedule_temp_dir():
    try:
        import config as _config

        cache_root = os.path.join(str(_config.USER_DATA_DIR), "cache", "processing", "product_scan")
        os.makedirs(cache_root, exist_ok=True)
        return tempfile.mkdtemp(prefix="schedule_", dir=cache_root)
    except Exception:
        return tempfile.mkdtemp(prefix="liveclipper_schedule_")


def _enough_normalize_space(source_path, temp_dir):
    try:
        source_size = max(0, os.path.getsize(source_path))
        margin = max(512 * 1024 * 1024, int(source_size * 0.12))
        return shutil.disk_usage(temp_dir).free >= source_size + margin + _NORMALIZE_DISK_RESERVE
    except Exception:
        return False


def _prepare_ts_source(video, temp_dir, ffmpeg, log_fn=None):
    if not _needs_ts_normalization(video):
        return None
    if not _enough_normalize_space(video, temp_dir):
        if log_fn:
            log_fn("  TS临时标准化空间不足，改用逐片同步处理: %s" % os.path.basename(video))
        return None

    digest = hashlib.sha256(os.path.abspath(video).encode("utf-8", errors="replace")).hexdigest()[:12]
    output_path = os.path.join(temp_dir, "normalized_%s.mp4" % digest)
    started = time.perf_counter()
    source_offset = _probe_av_start_offset_seconds(video, ffmpeg)
    if log_fn:
        log_fn("  TS无损标准化中（不重新编码）: %s" % os.path.basename(video))
    try:
        proc = subprocess.run(
            [
                ffmpeg, "-y",
                "-fflags", "+genpts",
                "-i", video,
                "-map", "0:v:0",
                "-map", "0:a:0?",
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                output_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=1800,
            creationflags=0x8000000,
        )
        usable = (
            proc.returncode == 0
            and os.path.isfile(output_path)
            and os.path.getsize(output_path) > 1000
        )
        if usable:
            output_offset = _probe_av_start_offset_seconds(output_path, ffmpeg)
            usable = (
                source_offset is not None
                and output_offset is not None
                and abs(output_offset - source_offset) <= 0.03
            )
        if usable:
            if log_fn:
                log_fn(
                    "  TS无损标准化完成: %s（%.1fs，后续片段按同一时间轴同步切割）"
                    % (os.path.basename(video), time.perf_counter() - started)
                )
            return output_path
        if log_fn:
            log_fn("  TS无损标准化未通过音画校验，改用逐片同步处理: %s" % os.path.basename(video))
    except Exception as exc:
        if log_fn:
            log_fn("  TS无损标准化失败，改用逐片同步处理: %s" % str(exc)[:120])
    try:
        if os.path.exists(output_path):
            os.remove(output_path)
    except OSError:
        pass
    return None



def _ffprobe_for(ffmpeg):
    try:
        base = os.path.basename(str(ffmpeg)).lower()
        if base in ("ffmpeg", "ffmpeg.exe"):
            probe_name = "ffprobe.exe" if base.endswith(".exe") else "ffprobe"
            candidate = os.path.join(os.path.dirname(str(ffmpeg)), probe_name)
            if os.path.exists(candidate):
                return candidate
        text = str(ffmpeg)
        if text.lower().endswith("ffmpeg.exe"):
            return text[:-10] + "ffprobe.exe"
        if text.lower().endswith("ffmpeg"):
            return text[:-6] + "ffprobe"
    except Exception:
        pass
    return "ffprobe"


def _probe_av_start_offset_seconds(path, ffmpeg):
    try:
        proc = subprocess.run(
            [
                _ffprobe_for(ffmpeg),
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,start_time",
                "-of",
                "json",
                path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            creationflags=0x8000000,
        )
        if proc.returncode != 0:
            return None
        data = json.loads((proc.stdout or b"{}").decode("utf-8", errors="replace") or "{}")
        streams = data.get("streams") or []
        video = next((item for item in streams if item.get("codec_type") == "video"), None)
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        if not video:
            return None
        if not audio:
            return 0.0
        return float(audio.get("start_time") or 0.0) - float(video.get("start_time") or 0.0)
    except Exception:
        return None


def _probe_av_start_gap_seconds(path, ffmpeg):
    offset = _probe_av_start_offset_seconds(path, ffmpeg)
    return None if offset is None else abs(offset)


def _av_start_gap_seconds(path, ffmpeg):
    gap = _probe_av_start_gap_seconds(path, ffmpeg)
    if gap is None:
        return 0.0
    return gap


def _audio_sync_filter(av_start_offset=0.0):
    offset = float(av_start_offset or 0.0)
    if offset >= 0:
        pts = "asetpts=PTS-STARTPTS-%.6f/TB" % offset
    else:
        pts = "asetpts=PTS-STARTPTS+%.6f/TB" % abs(offset)
    return pts + ",aresample=async=1:first_pts=0"


def _sync_reencode_segment(
    ffmpeg, rel_st, part_dur, video, out_path, av_start_offset=0.0
):
    return subprocess.run(
        [
            ffmpeg, "-y",
            "-fflags", "+genpts",
            "-ss", str(float(rel_st)),
            "-accurate_seek",
            "-i", video,
            "-t", str(float(part_dur)),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-vf", "setpts=PTS-STARTPTS,fps=30,format=yuv420p",
            "-r", "30",
            "-vsync", "cfr",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-af", _audio_sync_filter(av_start_offset),
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-ac", "2",
            "-shortest",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            out_path,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=600,
        creationflags=0x8000000,
    )


def _fast_copy_segment(ffmpeg, rel_st, part_dur, video, out_path):
    return subprocess.run(
        [
            ffmpeg, "-y",
            "-ss", str(float(rel_st)),
            "-i", video,
            "-t", str(float(part_dur)),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            out_path,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=600,
        creationflags=0x8000000,
    )


def _probe_media_duration_seconds(path, ffmpeg):
    try:
        proc = subprocess.run(
            [
                _ffprobe_for(ffmpeg),
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            creationflags=0x8000000,
        )
        if proc.returncode != 0:
            return None
        raw = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        return float(raw) if raw else None
    except Exception:
        return None


def _validate_segment_duration(path, ffmpeg, expected_duration):
    """Reject a generated file when it is materially shorter/longer than requested."""
    output_duration = _probe_media_duration_seconds(path, ffmpeg)
    if output_duration is None:
        return False, "无法读取切片时长", None
    duration_tolerance = max(3.0, min(8.0, float(expected_duration) * 0.01))
    duration_error = abs(float(output_duration) - float(expected_duration))
    if duration_error > duration_tolerance:
        return (
            False,
            "切片时长偏差 %.2fs：实际 %.2fs，应为 %.2fs"
            % (duration_error, output_duration, expected_duration),
            output_duration,
        )
    return True, "切片时长 %.2fs" % output_duration, output_duration


def _validate_fast_copy_segment(path, ffmpeg, expected_duration, _expected_av_offset):
    output_offset = _probe_av_start_offset_seconds(path, ffmpeg)
    if output_offset is None:
        return False, "无法读取切片音画起点"

    # An arbitrary stream-copy seek starts on nearby codec packet boundaries,
    # so its first packet offset is not expected to equal the container-level
    # offset at the beginning of the full source.  The copied packets retain
    # their timestamps; reject only an audible/visible output start gap.
    output_gap = abs(float(output_offset))
    if output_gap > 0.08:
        return False, "切片音画起点相差 %.3fs" % output_gap

    duration_ok, duration_detail, _ = _validate_segment_duration(path, ffmpeg, expected_duration)
    if not duration_ok:
        return False, duration_detail

    return True, "音画起点差 %.3fs" % output_gap



def _parse_datetime_from_name(path):
    m = re.search(r"(20\d{10}(?:\d{2})?)", os.path.basename(str(path)))
    if not m:
        return None
    try:
        value = m.group(1)
        fmt = "%Y%m%d%H%M%S" if len(value) == 14 else "%Y%m%d%H%M"
        return datetime.strptime(value, fmt)
    except Exception:
        return None


def sort_videos_by_start(video_list):
    """Sort videos by timestamp in filename when every selected file has one."""
    items = [(v, _parse_datetime_from_name(v)) for v in video_list]
    if items and all(dt is not None for _, dt in items):
        return [v for v, _ in sorted(items, key=lambda x: x[1])]
    return list(video_list)




def _parse_schedule_time_value(value, time_basis="relative"):
    """Parse one new-format schedule endpoint without guessing its meaning.

    ``relative`` treats ``01:04`` as one minute and four seconds.  ``clock``
    treats it as 01:04 on the clock.  The caller explicitly selects the basis
    because the two forms are otherwise ambiguous.
    """
    text = str(value or "").strip().replace("：", ":")
    parts = text.split(":")
    if len(parts) not in {2, 3}:
        return None
    try:
        values = [int(part.strip()) for part in parts]
    except (TypeError, ValueError):
        return None
    if any(number < 0 for number in values):
        return None
    if len(values) == 3:
        hours, minutes, seconds = values
    elif time_basis == "clock":
        hours, minutes = values
        seconds = 0
    else:
        hours = 0
        minutes, seconds = values
    if minutes >= 60 or seconds >= 60:
        return None
    if time_basis == "clock" and hours >= 24:
        return None
    return hours * 3600 + minutes * 60 + seconds


def normalize_clock_schedule_to_live_offsets(schedule, live_start, log_fn=None):
    """Convert clock-of-day schedule values into offsets from live start.

    A negative difference greater than twelve hours is treated as the next
    day, which keeps an overnight live continuous while leaving genuinely
    earlier same-day rows outside the selected live range.
    """
    if live_start is None:
        raise ValueError("时钟时间需要直播开播时间作为换算基准")
    live_clock_seconds = (
        live_start.hour * 3600 + live_start.minute * 60 + live_start.second
    )
    day_seconds = 24 * 3600
    for item in schedule:
        start_clock = float(item.get("start_offset", 0) or 0)
        end_clock = float(item.get("end_offset", 0) or 0)
        start_offset = start_clock - live_clock_seconds
        end_offset = end_clock - live_clock_seconds
        if start_offset < -(day_seconds / 2):
            start_offset += day_seconds
        if end_offset < -(day_seconds / 2):
            end_offset += day_seconds
        while end_offset <= start_offset:
            end_offset += day_seconds
        item["start_offset"] = start_offset
        item["end_offset"] = end_offset
    schedule.sort(key=lambda item: float(item.get("start_offset", 0) or 0))
    if log_fn:
        log_fn(
            "已按直播开播 %s 将表格时钟时间换算为直播时间。"
            % live_start.strftime("%H:%M:%S")
        )
    return schedule


def _locate_explain_time_range(header):
    """在表头中定位“讲解时段1..N”列的起止范围。

    返回 (起始列下标, 结束列下标+1)。飞书导出的排品表模板并不固定 ——
    有的含“封面”图片列、有的不含，讲解时段列会整体左右偏移；因此不能
    写死成第 7 列。找不到任何“讲解时段/讲解时间”列时返回 (None, None)。
    """
    texts = [str(h or "").strip().replace(" ", "") for h in (header or [])]
    matched = []
    for idx, text in enumerate(texts):
        if not text or "次数" in text:
            continue
        if text.startswith("讲解时段") or text.startswith("讲解时间"):
            matched.append(idx)
        elif text.startswith("讲解") and text[-1:].isdigit():
            matched.append(idx)
    if not matched:
        return None, None
    start = min(matched)
    # 时段列逐格有严格内容校验，窗口放宽后靠校验过滤无关列
    end = min(max(matched) + 1, start + 16, len(texts))
    if end <= start:
        end = min(start + 1, len(texts))
    return start, end


def read_excel(filepath: str, log_fn=None, time_basis="relative"):
    """读取时间表Excel，支持旧格式和新格式自动检测。"""
    time_basis = "clock" if str(time_basis or "").strip().lower() == "clock" else "relative"
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
        # 自动匹配: 商品名称在第0列或第1列; 讲解时段列按表头动态定位,
        # 不写死列号 —— 部分导出表会少“封面”等列, 导致时段整体左移一列
        _name_col = 0
        for _hi in range(min(3, len(header))):
            if "商品标题" in str(header[_hi] or "") or "商品名称" in str(header[_hi] or ""):
                _name_col = _hi
                break
        _time_start, _time_end = _locate_explain_time_range(header)
        if _time_start is None:
            # 兜底: 表头未标明“讲解时段”列时, 沿用历史固定位置
            _time_start = 6 if _name_col == 1 else 4
            _time_end = min(_time_start + 7, len(header))
        for row in rows[1:]:
            if len(row) <= _time_start:
                continue
            name = str(row[_name_col]).strip() if row[_name_col] else ""
            if not name or name in ("None", "商品标题", "商品封面", ""):
                continue
            pid = str(row[1]).strip() if len(row) > 1 and row[1] and "商品ID" in str(header[1] if len(header) > 1 else "") else (str(row[2]).strip() if len(row) > 2 and row[2] else "")
            for ci in range(_time_start, _time_end):
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
                if ":" not in p1:
                    continue
                start = _parse_schedule_time_value(p1, time_basis=time_basis)
                end = _parse_schedule_time_value(p2, time_basis=time_basis)
                if start is None or end is None:
                    continue
                if end <= start and time_basis != "clock":
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
                if live_start and time_basis != "clock":
                    ls = live_start.hour * 3600 + live_start.minute * 60 + live_start.second
                    start_offset -= ls
                    end_offset -= ls
            else:
                dt = _parse_datetime(time_str)
                if dt is None:
                    continue
                if time_basis == "clock":
                    start_offset = dt.hour * 3600 + dt.minute * 60 + dt.second
                elif live_start:
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


def _safe_output_stem(name, fallback="未命名商品", max_chars=120):
    text = sanitize_forbidden_title(name, fallback=fallback)
    safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in text)
    safe = re.sub(r"\s+", " ", safe).strip(" ._") or fallback
    if len(safe) <= max_chars:
        return safe
    stamp_match = re.search(r"(_20\d{6}_\d{6}(?:_\d{2})?)$", safe)
    if stamp_match:
        stamp = stamp_match.group(1)
        prefix_limit = max(1, max_chars - len(stamp))
        prefix = safe[:prefix_limit].rstrip(" ._") or fallback
        return (prefix + stamp)[:max_chars].rstrip(" ._")
    return safe[:max_chars].rstrip(" ._") or fallback


_video_durations_cache = {}

def _parse_video_time(filename):

    """从文件名提取视频开始时间（秒从午夜）"""

    import re as _re

    from datetime import datetime as _dt

    m = _re.search(r"(20\d{10}(?:\d{2})?)", os.path.basename(filename))

    if m:

        try:

            value = m.group(1)
            fmt = "%Y%m%d%H%M%S" if len(value) == 14 else "%Y%m%d%H%M"
            dt = _dt.strptime(value, fmt)

            return dt.hour * 3600 + dt.minute * 60 + dt.second

        except:

            pass

    return None



def _get_video_timeline(video_list, durations):

    """计算每个视频在时间线上的起始偏移（相对于 live_start）"""

    # Keep the complete date while normalising.  Seconds-from-midnight breaks
    # at midnight and can turn a real gap into a false continuous range.
    named_times = [_parse_datetime_from_name(video) for video in video_list]
    if named_times and all(value is not None for value in named_times):
        base = named_times[0]
        return [(value - base).total_seconds() for value in named_times]

    import re as _re

    from datetime import datetime as _dt

    starts = []

    # 先尝试从文件名解析

    all_ok = True

    for v in video_list:

        m = _re.search(r"(20\d{10}(?:\d{2})?)", os.path.basename(v))

        if m:

            try:

                value = m.group(1)
                fmt = "%Y%m%d%H%M%S" if len(value) == 14 else "%Y%m%d%H%M"
                dt = _dt.strptime(value, fmt)

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





def build_video_timeline(video_list, ffmpeg_cmd=None, log_fn=None):
    """Return the real selected-file timeline without filling timestamp gaps."""
    ordered_videos = sort_videos_by_start(video_list)
    durations = _probe_durations(ordered_videos, log_fn=log_fn, ffmpeg_cmd=ffmpeg_cmd)
    starts = _get_video_timeline(ordered_videos, durations)
    timestamped = [_parse_datetime_from_name(video) for video in ordered_videos]
    has_precise_timestamps = bool(timestamped) and all(value is not None for value in timestamped)
    timeline = []
    for index, video in enumerate(ordered_videos):
        duration = max(0.0, float(durations[index] if index < len(durations) else 0.0))
        start = float(starts[index] if index < len(starts) else 0.0)
        timeline.append({
            "video": video,
            "name": os.path.basename(video),
            "start": start,
            "end": start + duration,
            "duration": duration,
            "timestamp": timestamped[index].isoformat() if timestamped[index] else "",
            "estimated": not has_precise_timestamps,
        })
    return timeline


def _coverage_gaps(start, end, parts):
    """Return non-covered intervals after merging overlapping source pieces."""
    cursor = float(start)
    gaps = []
    for part in sorted(parts, key=lambda item: (item["timeline_start"], item["timeline_end"])):
        part_start = max(float(start), float(part["timeline_start"]))
        part_end = min(float(end), float(part["timeline_end"]))
        if part_end <= part_start:
            continue
        if part_start > cursor + 0.5:
            gaps.append({"start": cursor, "end": part_start, "duration": part_start - cursor})
        cursor = max(cursor, part_end)
    if float(end) > cursor + 0.5:
        gaps.append({"start": cursor, "end": float(end), "duration": float(end) - cursor})
    return gaps


def build_schedule_coverage(groups, video_list, ffmpeg_cmd=None, log_fn=None):
    """Map schedule ranges to real selected-file coverage for preview and export."""
    timeline = build_video_timeline(video_list, ffmpeg_cmd=ffmpeg_cmd, log_fn=log_fn)
    coverage_groups = []
    for group in groups:
        records = []
        covered_duration = 0.0
        missing_duration = 0.0
        for start, end in list(group.get("segments") or []):
            start = float(start)
            end = float(end)
            expected_duration = max(0.0, end - start)
            parts = []
            for source in timeline:
                clip_start = max(start, float(source["start"]))
                clip_end = min(end, float(source["end"]))
                if clip_end - clip_start < 0.5:
                    continue
                parts.append({
                    "video": source["name"],
                    "video_path": source["video"],
                    "timeline_start": clip_start,
                    "timeline_end": clip_end,
                    "file_start": clip_start - float(source["start"]),
                    "file_end": clip_end - float(source["start"]),
                    "duration": clip_end - clip_start,
                })
            gaps = _coverage_gaps(start, end, parts)
            missing = sum(float(item["duration"]) for item in gaps)
            covered = max(0.0, expected_duration - missing)
            status = "missing" if not parts else ("partial" if missing >= 0.5 else "covered")
            records.append({
                "schedule_start": start,
                "schedule_end": end,
                "expected_duration": expected_duration,
                "covered_duration": covered,
                "missing_duration": missing,
                "status": status,
                "parts": parts,
                "missing_ranges": gaps,
            })
            covered_duration += covered
            missing_duration += missing
        status = "missing"
        if records and all(item["status"] == "covered" for item in records):
            status = "covered"
        elif any(item["parts"] for item in records):
            status = "partial"
        coverage_groups.append({
            "name": group.get("name", ""),
            "product_id": group.get("product_id", ""),
            "segments": len(records),
            "total_duration": sum(float(record["expected_duration"]) for record in records),
            "covered_duration": covered_duration,
            "missing_duration": missing_duration,
            "status": status,
            "ranges": records,
        })
    return coverage_groups, timeline


def groups_with_coverage(coverage_groups):
    """Keep only groups with at least one real source intersection."""
    export_groups = []
    for group in coverage_groups:
        segments = [
            (float(record["schedule_start"]), float(record["schedule_end"]))
            for record in group.get("ranges") or []
            if record.get("parts")
        ]
        if not segments:
            continue
        item = {
            "name": group.get("name", ""),
            "segments": segments,
            "total_duration": sum(end - start for start, end in segments),
        }
        if group.get("product_id"):
            item["product_id"] = group["product_id"]
        export_groups.append(item)
    return export_groups


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





def _extract_by_schedule_fast_copy(groups, video_list, output_dir, ffmpeg="ffmpeg", log_fn=None):
    """Export independent source-file pieces with input-side stream-copy seeking.

    This intentionally trades a small keyframe-boundary time deviation for speed.
    It does not normalise TS files, probe each output, or retry with re-encoding.
    """
    results = []
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    video_list = sort_videos_by_start(video_list)
    durations = _probe_durations(video_list, log_fn=log_fn, ffmpeg_cmd=ffmpeg)
    if not durations or len(durations) != len(video_list):
        durations = [99999] * len(video_list)
    starts = _get_video_timeline(video_list, durations)
    timeline = [
        (video, starts[index], starts[index] + (durations[index] if index < len(durations) else 0))
        for index, video in enumerate(video_list)
    ]

    def _split_across_videos(start, end):
        parts = []
        for video, video_start, video_end in timeline:
            cut_start = max(start, video_start)
            cut_end = min(end, video_end)
            if cut_end - cut_start >= 0.5:
                parts.append((video, cut_start - video_start, cut_end - cut_start))
        return parts

    def _error_tail(process):
        raw = getattr(process, "stderr", b"") or b""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        lines = str(raw).strip().splitlines()
        return lines[-1] if lines else ""

    if log_fn:
        log_fn("  极速切割：按关键帧直接分段，不标准化、不校验、不重编码；跨文件分别输出。")

    for group in groups:
        name = sanitize_forbidden_title(group.get("name", ""), fallback="未命名商品")
        segments = group.get("segments", [])
        if not segments:
            continue
        flat_output = bool(group.get("flat_output"))
        exact_name = bool(group.get("exact_name"))
        safe_name = _safe_output_stem(name, max_chars=140 if exact_name else 80)
        safe_dir_name = _safe_output_stem(name, max_chars=80)
        out_dir = output_dir if flat_output else os.path.join(output_dir, safe_name)
        os.makedirs(out_dir, exist_ok=True)
        exported = 0
        for segment_index, (start, end) in enumerate(segments):
            if end - start < 10:
                continue
            parts = _split_across_videos(start, end)
            if not parts:
                if log_fn:
                    log_fn("  未找到覆盖时段 %.0fs-%.0fs 的视频" % (start, end))
                continue
            for part_index, (video, relative_start, part_duration) in enumerate(parts):
                suffix = (
                    "%d" % (segment_index + 1)
                    if len(parts) == 1
                    else "%d_%d" % (segment_index + 1, part_index + 1)
                )
                if exact_name and len(segments) == 1 and len(parts) == 1:
                    out_path = os.path.join(out_dir, "%s.mp4" % safe_name)
                else:
                    out_path = os.path.join(out_dir, "%s_%s.mp4" % (safe_dir_name[:40], suffix))
                try:
                    process = _fast_copy_segment(
                        ffmpeg,
                        relative_start,
                        part_duration,
                        video,
                        out_path,
                    )
                    exported_ok = (
                        process.returncode == 0
                        and os.path.isfile(out_path)
                        and os.path.getsize(out_path) > 1000
                    )
                    if exported_ok:
                        results.append({
                            "name": name,
                            "output_path": out_path,
                            "size_mb": round(os.path.getsize(out_path) / 1024 / 1024, 1),
                            "expected_duration": round(float(part_duration), 3),
                            "duration_seconds": round(float(part_duration), 3),
                            "source_video": os.path.basename(video),
                            "cut_mode": "fast-copy",
                        })
                        exported += 1
                    else:
                        try:
                            if os.path.exists(out_path):
                                os.remove(out_path)
                        except OSError:
                            pass
                        if log_fn:
                            detail = _error_tail(process)
                            log_fn("  极速切割失败: %s%s" % (
                                os.path.basename(out_path),
                                (" " + detail[:160]) if detail else "",
                            ))
                except Exception as exc:
                    if log_fn:
                        log_fn("  极速切割失败: " + str(exc)[:100])
        if exported and log_fn:
            log_fn("  %s: %d 个独立片段" % (name, exported))
    return results


def extract_by_schedule(groups, video_list, output_dir, ffmpeg="ffmpeg", log_fn=None, fast_copy=False):
    """按分组切割导出每段独立文件，不拼接"""
    if fast_copy:
        return _extract_by_schedule_fast_copy(
            groups,
            video_list,
            output_dir,
            ffmpeg=ffmpeg,
            log_fn=log_fn,
        )
    results = []
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    video_list = sort_videos_by_start(video_list)
    durations = _probe_durations(video_list, log_fn=log_fn, ffmpeg_cmd=ffmpeg)
    if not durations or len(durations) != len(video_list):
        durations = [99999] * len(video_list)
    starts = _get_video_timeline(video_list, durations)
    timeline = [
        (video, starts[index], starts[index] + (durations[index] if index < len(durations) else 0))
        for index, video in enumerate(video_list)
    ]
    normalized_sources = {}
    normalized_offsets = {}
    normalization_attempted = set()
    schedule_temp_dir = None

    def _split_across_videos(start, end):
        parts = []
        for video, vs, ve in timeline:
            cut_start = max(start, vs)
            cut_end = min(end, ve)
            if cut_end - cut_start >= 0.5:
                parts.append((video, cut_start - vs, cut_end - cut_start, cut_start, cut_end))
        return parts

    def _source_for_cut(video):
        nonlocal schedule_temp_dir
        if not _needs_ts_normalization(video):
            return video, False, 0.0
        key = os.path.abspath(video)
        if key not in normalization_attempted:
            normalization_attempted.add(key)
            if schedule_temp_dir is None:
                schedule_temp_dir = _create_schedule_temp_dir()
            normalized_sources[key] = _prepare_ts_source(
                video,
                schedule_temp_dir,
                ffmpeg,
                log_fn=log_fn,
            )
        normalized = normalized_sources.get(key)
        cut_source = normalized or video
        if key not in normalized_offsets:
            normalized_offsets[key] = _probe_av_start_offset_seconds(cut_source, ffmpeg)
        offset = normalized_offsets.get(key)
        return cut_source, bool(normalized), float(offset or 0.0)

    def _error_tail(process):
        raw = getattr(process, "stderr", b"") or b""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        lines = str(raw).strip().splitlines()
        return lines[-1] if lines else ""

    try:
        for group in groups:
            name = sanitize_forbidden_title(group.get("name", ""), fallback="未命名商品")
            segments = group.get("segments", [])
            if not segments:
                continue
            flat_output = bool(group.get("flat_output"))
            exact_name = bool(group.get("exact_name"))
            safe_name = _safe_output_stem(name, max_chars=140 if exact_name else 80)
            safe_dir_name = _safe_output_stem(name, max_chars=80)
            out_dir = output_dir if flat_output else os.path.join(output_dir, safe_name)
            os.makedirs(out_dir, exist_ok=True)
            exported = 0
            for segment_index, (start, end) in enumerate(segments):
                if end - start < 10:
                    continue
                parts = _split_across_videos(start, end)
                if not parts:
                    if log_fn:
                        log_fn("  未找到覆盖时段 %.0fs-%.0fs 的视频" % (start, end))
                    continue

                for part_index, (video, relative_start, part_duration, _, _) in enumerate(parts):
                    suffix = (
                        "%d" % (segment_index + 1)
                        if len(parts) == 1
                        else "%d_%d" % (segment_index + 1, part_index + 1)
                    )
                    if exact_name and len(segments) == 1 and len(parts) == 1:
                        out_path = os.path.join(out_dir, "%s.mp4" % safe_name)
                    else:
                        out_path = os.path.join(out_dir, "%s_%s.mp4" % (safe_dir_name[:40], suffix))
                    last_error = ""
                    try:
                        cut_source, normalized, av_start_offset = _source_for_cut(video)
                        ts_source = _needs_ts_normalization(video)
                        reencode_attempted = False
                        actual_duration = None
                        if ts_source and normalized:
                            if log_fn:
                                log_fn(
                                    "  TS片段快速切割（无重编码）: %.0fs-%.0fs"
                                    % (relative_start, relative_start + part_duration)
                                )
                            process = _fast_copy_segment(
                                ffmpeg,
                                relative_start,
                                part_duration,
                                cut_source,
                                out_path,
                            )
                            final_ok = (
                                process.returncode == 0
                                and os.path.isfile(out_path)
                                and os.path.getsize(out_path) > 1000
                            )
                            validation_detail = ""
                            if final_ok:
                                final_ok, validation_detail = _validate_fast_copy_segment(
                                    out_path,
                                    ffmpeg,
                                    part_duration,
                                    av_start_offset,
                                )
                            if final_ok:
                                if log_fn:
                                    log_fn("  TS快速切割完成（%s）" % validation_detail)
                            else:
                                last_error = validation_detail or _error_tail(process)
                                if log_fn:
                                    log_fn(
                                        "  TS快速切割校验未通过，改用同步转码: %s"
                                        % (last_error or "输出无效")
                                    )
                                process = _sync_reencode_segment(
                                    ffmpeg,
                                    relative_start,
                                    part_duration,
                                    cut_source,
                                    out_path,
                                    av_start_offset=av_start_offset,
                                )
                                reencode_attempted = True
                                final_ok = (
                                    process.returncode == 0
                                    and os.path.isfile(out_path)
                                    and os.path.getsize(out_path) > 1000
                                )
                        elif ts_source:
                            if log_fn:
                                log_fn(
                                    "  TS片段同步转码（保持原音画对应）: %.0fs-%.0fs"
                                    % (relative_start, relative_start + part_duration)
                                )
                            process = _sync_reencode_segment(
                                ffmpeg,
                                relative_start,
                                part_duration,
                                cut_source,
                                out_path,
                                av_start_offset=av_start_offset,
                            )
                            reencode_attempted = True
                            final_ok = (
                                process.returncode == 0
                                and os.path.isfile(out_path)
                                and os.path.getsize(out_path) > 1000
                            )
                        else:
                            process = subprocess.run(
                                [
                                    ffmpeg, "-y",
                                    "-ss", str(float(relative_start)),
                                    "-i", cut_source,
                                    "-t", str(float(part_duration)),
                                    "-c", "copy",
                                    "-avoid_negative_ts", "make_zero",
                                    "-movflags", "+faststart",
                                    out_path,
                                ],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE,
                                timeout=300,
                                creationflags=0x8000000,
                            )
                            final_ok = (
                                process.returncode == 0
                                and os.path.isfile(out_path)
                                and os.path.getsize(out_path) > 1000
                            )
                        if final_ok and not ts_source:
                            gap = _av_start_gap_seconds(out_path, ffmpeg)
                            if gap > 0.08:
                                if log_fn:
                                    log_fn(
                                        "  音画时间戳偏移 %.3fs，切换同步安全重编码: %s"
                                        % (gap, os.path.basename(out_path))
                                    )
                                process = _sync_reencode_segment(
                                    ffmpeg, relative_start, part_duration, cut_source, out_path
                                )
                                reencode_attempted = True
                                final_ok = (
                                    process.returncode == 0
                                    and os.path.isfile(out_path)
                                    and os.path.getsize(out_path) > 1000
                                )
                        if not final_ok and not reencode_attempted:
                            last_error = _error_tail(process)
                            process = _sync_reencode_segment(
                                ffmpeg, relative_start, part_duration, cut_source, out_path
                            )
                            final_ok = (
                                process.returncode == 0
                                and os.path.isfile(out_path)
                                and os.path.getsize(out_path) > 1000
                            )
                            reencode_attempted = True
                        if final_ok and not ts_source:
                            duration_ok, duration_detail, actual_duration = _validate_segment_duration(
                                out_path, ffmpeg, part_duration
                            )
                            if not duration_ok:
                                last_error = duration_detail
                                if not reencode_attempted:
                                    if log_fn:
                                        log_fn("  %s，改用同步安全重编码" % duration_detail)
                                    process = _sync_reencode_segment(
                                        ffmpeg,
                                        relative_start,
                                        part_duration,
                                        cut_source,
                                        out_path,
                                        av_start_offset=av_start_offset,
                                    )
                                    reencode_attempted = True
                                    final_ok = (
                                        process.returncode == 0
                                        and os.path.isfile(out_path)
                                        and os.path.getsize(out_path) > 1000
                                    )
                                    if final_ok:
                                        duration_ok, duration_detail, actual_duration = _validate_segment_duration(
                                            out_path, ffmpeg, part_duration
                                        )
                                        if not duration_ok:
                                            final_ok = False
                                            last_error = duration_detail
                                else:
                                    final_ok = False
                        if not final_ok:
                            last_error = last_error or _error_tail(process)

                        if final_ok:
                            mb = os.path.getsize(out_path) / 1024 / 1024
                            results.append({
                                "name": name,
                                "output_path": out_path,
                                "size_mb": round(mb, 1),
                                "expected_duration": round(float(part_duration), 3),
                                "duration_seconds": round(float(actual_duration or part_duration), 3),
                                "source_video": os.path.basename(video),
                            })
                            exported += 1
                        else:
                            try:
                                if os.path.exists(out_path):
                                    os.remove(out_path)
                            except OSError:
                                pass
                            if log_fn:
                                detail = (" " + last_error[:160]) if last_error else ""
                                log_fn("  导出失败: %s%s" % (os.path.basename(out_path), detail))
                    except Exception as exc:
                        if log_fn:
                            log_fn("  切割失败: " + str(exc)[:60])
            if exported and log_fn:
                log_fn("  %s: %d段" % (name, exported))
    finally:
        if schedule_temp_dir:
            shutil.rmtree(schedule_temp_dir, ignore_errors=True)
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
    has_any_video_ts = any(_parse_video_time(v) is not None for v in video_list)
    if total_duration > 0 and min_start >= -300 and max_end <= total_duration + 300 and not has_any_video_ts:
        if log_fn:
            log_fn("[align] schedule already matches selected video timeline, no adjustment")
        return schedule

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
