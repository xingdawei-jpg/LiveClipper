#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抖音直播流地址解析 - yt-dlp 优先，Chrome headless 兜底"""

import re
import json
import subprocess
import os


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

    # 截断URL query参数并替换域名（yt-dlp不支持live.douyin.com）
    clean_url = douyin_url.split('?')[0].replace('live.douyin.com', 'www.douyin.com')

    try:
        _log("正在使用 yt-dlp 获取直播流...")
        result = subprocess.run(
            [yt_dlp, '-g', '--no-warnings', '--extractor-args', 'douyin:web_fmt=0', clean_url],
            capture_output=True, timeout=30
        )
        if result.returncode != 0:
            stderr = result.stderr.decode('utf-8', errors='ignore')[:200]
            _log(f"yt-dlp 失败: {stderr}")
            return None

        url = result.stdout.decode('utf-8', errors='ignore').strip().split('\n')[0]
        if url and (url.startswith('http://') or url.startswith('https://')):
            _log("解析成功 (yt-dlp)")
            return url
        return None
    except subprocess.TimeoutExpired:
        _log("yt-dlp 超时(30s)")
        return None
    except Exception as e:
        _log(f"yt-dlp 异常: {e}")
        return None


def _find_ytdlp():
    """查找 yt-dlp 可执行文件"""
    candidates = ['yt-dlp', 'yt-dlp.exe']
    # PATH 中查找
    for cmd in ['where', 'where.exe']:
        try:
            r = subprocess.run([cmd, 'yt-dlp'], capture_output=True, timeout=5)
            if r.returncode == 0:
                path = r.stdout.decode('utf-8', errors='ignore').strip().split('\n')[0]
                if path and os.path.exists(path):
                    return path
        except Exception:
            pass
    return None


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
            result = subprocess.run(['where', 'chrome'], capture_output=True, timeout=5)
            if result.returncode == 0:
                chrome = result.stdout.decode('utf-8', errors='ignore').strip().split('\n')[0]
        if not chrome:
            _log("未找到 Chrome 浏览器")
            return None

        result = subprocess.run(
            [chrome, '--headless=new', '--disable-gpu', '--dump-dom', douyin_url],
            capture_output=True, timeout=20
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

    quality_order = {"uhd": 0, "origin": 1, "full_hd1": 2, "hd1": 3, "sd1": 4, "sd": 5}
    def _score_url(url):
        for qname, score in quality_order.items():
            if qname in url.lower():
                return score
        return 99

    if flv_urls:
        best = sorted(flv_urls, key=_score_url)[0].replace("\\u0026", "&")
        _log("解析成功 (Chrome flv)")
        return best
    if m3u8_urls:
        best = sorted(m3u8_urls, key=_score_url)[0].replace("\\u0026", "&")
        _log("解析成功 (Chrome m3u8)")
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
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                info = json.loads(r.stdout)
                print("FFprobe OK, streams:", len(info.get("streams", [])))
            else:
                print("FFprobe failed:", r.returncode)
        except Exception as e:
            print("FFprobe error:", e)
    else:
        print("未获取到流地址")
