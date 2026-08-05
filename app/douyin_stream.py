#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抖音直播流地址解析 - yt-dlp 优先，Chrome headless 兜底"""

import re
import json
import subprocess
import os
import shutil
import sys
import urllib.parse
from pathlib import Path


def extract_live_url(douyin_url, log_fn=None):
    """从抖音直播间获取真实推流地址（m3u8/flv）

    优先使用 yt-dlp（无需浏览器），失败则用 Chrome headless 兜底。
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)

    # --- 方案1: yt-dlp（推荐，无需浏览器）---
    stream_url = _try_ytdlp(douyin_url, _log)
    if stream_url:
        return stream_url

    # --- 方案2: Chrome headless 兜底 ---
    _log("yt-dlp 失败，尝试 Chrome headless...")
    return _try_chrome(douyin_url, _log)


def _try_ytdlp(douyin_url, _log):
    """使用 yt-dlp 获取直播流地址（自动截短URL避免参数干扰）"""
    yt_dlp = _find_ytdlp()
    if not yt_dlp:
        _log("未找到 yt-dlp，跳过")
        return None

    version = _yt_dlp_version(yt_dlp)
    _log(f"已找到 yt-dlp{(' ' + version) if version else ''}")

    clean_url = douyin_url.split('?')[0]
    candidates = []
    for item in (clean_url, clean_url.replace('live.douyin.com', 'www.douyin.com')):
        if item and item not in candidates:
            candidates.append(item)

    for index, candidate in enumerate(candidates, start=1):
        try:
            _log(f"正在使用 yt-dlp 获取直播流({index}/{len(candidates)})...")
            result = subprocess.run(
                [yt_dlp, '-g', '--no-warnings', '--extractor-args', 'douyin:web_fmt=0', candidate],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=45,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                stderr = (result.stderr or b"").decode('utf-8', errors='ignore')[:300]
                _log(f"yt-dlp 失败: {stderr}")
                continue

            urls = [
                line.strip()
                for line in (result.stdout or b"").decode('utf-8', errors='ignore').splitlines()
                if line.strip().startswith(('http://', 'https://'))
            ]
            if urls:
                _log("解析成功 (yt-dlp)")
                return _pick_best_stream_url(urls)
        except subprocess.TimeoutExpired:
            _log("yt-dlp 超时(45s)")
        except Exception as e:
            _log(f"yt-dlp 异常: {e}")
    return None


def _find_ytdlp():
    """查找 yt-dlp 可执行文件"""
    candidates = []

    env_path = os.environ.get("YT_DLP_PATH", "")
    if env_path:
        candidates.append(env_path)

    for name in ("yt-dlp", "yt-dlp.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    try:
        exe = Path(sys.executable)
        candidates.append(str(exe.parent / "Scripts" / "yt-dlp.exe"))
        candidates.append(str(exe.parent / "Scripts" / "yt-dlp"))
    except Exception:
        pass

    local_programs = Path.home() / "AppData" / "Local" / "Programs" / "Python"
    try:
        for path in local_programs.glob("Python*/Scripts/yt-dlp.exe"):
            candidates.append(str(path))
    except Exception:
        pass

    for cmd in ['where', 'where.exe']:
        try:
            r = subprocess.run([cmd, 'yt-dlp'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0:
                for line in (r.stdout or b"").decode('utf-8', errors='ignore').splitlines():
                    if line.strip():
                        candidates.append(line.strip())
        except Exception:
            pass

    seen = set()
    for raw in candidates:
        path = str(raw or "").strip().strip('"')
        key = path.lower()
        if not path or key in seen:
            continue
        seen.add(key)
        if os.path.exists(path):
            return path
    return None


def _yt_dlp_version(path):
    try:
        result = subprocess.run(
            [path, '--version'],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        lines = (result.stdout or b"").decode("utf-8", errors="ignore").strip().splitlines()
        return lines[0] if lines else ""
    except Exception:
        return ""


def _pick_best_stream_url(urls):
    quality_score = {
        "origin": 0,
        "source": 0,
        "uhd": 5,
        "4k": 5,
        "full_hd1": 10,
        "full_hd": 10,
        "1080p": 10,
        "1080": 10,
        "hd1": 30,
        "hd": 30,
        "720p": 30,
        "720": 30,
        "sd1": 55,
        "sd": 55,
        "480p": 55,
        "480": 55,
        "ld": 80,
        "360p": 80,
        "360": 80,
    }

    def _quality(url):
        lower = urllib.parse.unquote(str(url or "")).lower()
        parsed = urllib.parse.urlparse(lower)
        query = urllib.parse.parse_qs(parsed.query)
        values = [parsed.path]
        for key in ("biz_quality", "quality", "definition", "ratio", "unique_id"):
            values.extend(query.get(key, []))
        text = " ".join(str(value) for value in values)
        for marker, score in quality_score.items():
            if re.search(rf"(^|[^a-z0-9]){re.escape(marker)}([^a-z0-9]|$)", text):
                return score
        return 70

    def _score_url(url):
        lower = url.lower()
        format_score = 0 if ".flv" in lower else 1 if ".m3u8" in lower else 2
        return (_quality(url), format_score, len(url))

    return sorted(urls, key=_score_url)[0]


def _try_chrome(douyin_url, _log):
    """使用 Chrome headless 获取渲染后的页面源码提取推流地址"""
    try:
        _log("正在使用 Chrome 加载直播页面...")
        chrome_paths = [
            r'C:\Program Files\Google\Chrome\Application\chrome.exe',
            r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
            r'C:\Users\Administrator\AppData\Local\Google\Chrome\Application\chrome.exe',
            os.path.expanduser(r'~\AppData\Local\Google\Chrome\Application\chrome.exe'),
        ]
        chrome = None
        for p in chrome_paths:
            if os.path.exists(p):
                chrome = p
                break
        if not chrome:
            result = subprocess.run(['where', 'chrome'], capture_output=True, timeout=5,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode == 0:
                chrome = result.stdout.decode('utf-8', errors='ignore').strip().split('\n')[0]
        if not chrome:
            _log("未找到 Chrome 浏览器")
            return None

        result = subprocess.run(
            [chrome, '--headless=new', '--disable-gpu', '--dump-dom', douyin_url],
            capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        html = result.stdout.decode('utf-8', errors='ignore')
    except subprocess.TimeoutExpired:
        _log("Chrome 加载超时(20s)")
        return None
    except FileNotFoundError:
        _log("找不到 Chrome 可执行文件")
        return None
    except Exception as e:
        _log("Chrome 执行失败: " + str(e))
        return None

    if not html or len(html) < 1000:
        _log("页面内容过短，可能被拦截")
        return None

    m3u8_urls = re.findall(r'(https?://[^\s"\'<>]+?\.m3u8[^\s"\'<>]*)', html)
    flv_urls = re.findall(r'(https?://[^\s"\'<>]+?\.flv[^\s"\'<>]*)', html)

    stream_urls = flv_urls + m3u8_urls
    if stream_urls:
        best = _pick_best_stream_url(stream_urls).replace("\\u0026", "&")
        _log("解析成功 (Chrome best quality)")
        return best

    if "直播已结束" in html or "直播已经结束" in html or "暂无直播" in html:
        _log("直播间未开播或已结束")
        return None
    _log("页面未找到推流地址，可能直播间未开播")
    return None


if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://live.douyin.com/18000475830"
    result = extract_live_url(url, print)
    if result:
        print("流地址:", result)
        try:
            r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                                "-show_streams", result],
                               capture_output=True, text=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0:
                info = json.loads(r.stdout)
                print("FFprobe OK, streams:", len(info.get("streams", [])))
            else:
                print("FFprobe failed:", r.returncode)
        except Exception as e:
            print("FFprobe error:", e)
    else:
        print("未获取到流地址")
