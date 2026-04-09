"""
简易版直播带货切片工具 v2.0
导入 SRT + 视频 → 自动提炼爆款文案 → 匹配时间戳 → 一键 FFmpeg 剪辑 + 去重

新增：10 种视频去重方式，可随机组合或预设方案
"""

import os
import sys
import re
import shutil
import subprocess
import glob
import random
import math

# 确保能找到同目录的模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from srt_parser import open_srt, _time_to_seconds
from config import (
    CLIP_KEYWORDS, CLIP_ORDER, VIDEO_CONFIG, FFMPEG_PATH,
    DEDUP_CONFIG, DEDUP_PRESET,
)

_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


# ============================================================
# 工具函数
# ============================================================

def get_ffmpeg_cmd():
    if FFMPEG_PATH and os.path.exists(FFMPEG_PATH):
        return FFMPEG_PATH
    return "ffmpeg"


def rand_range(min_val, max_val, decimals=2):
    """在范围内生成随机浮点数"""
    return round(random.uniform(min_val, max_val), decimals)


# ============================================================
# 视频去重：生成 FFmpeg 滤镜链
# ============================================================

def apply_preset(preset):
    """根据预设调整去重配置，返回 (random_count, methods)"""
    config = dict(DEDUP_CONFIG)  # 浅拷贝
    methods = dict(config["methods"])

    if preset == "none":
        for m in methods:
            methods[m]["enabled"] = False
        return 0, methods

    elif preset == "light":
        for m in methods:
            methods[m]["enabled"] = False
        methods["speed_change"]["enabled"] = True
        methods["zoom_crop"]["enabled"] = True
        return 2, methods

    elif preset == "medium":
        # 大部分启用，每片段随机 2~3 个
        return 3, methods

    elif preset == "heavy":
        # 全部启用，每片段随机 4~5 个
        return 5, methods

    else:  # custom
        return config["random_count"], methods


def build_dedup_filters(width, height, clip_index):
    """
    为单个片段构建去重滤镜链。
    返回: {
        "video_filters": "...",   # FFmpeg -vf 参数
        "audio_filters": "...",   # FFmpeg -af 参数
        "applied": ["speed_change", "zoom_crop", ...],  # 实际应用的方法列表
    }
    """
    random_count, methods = apply_preset(DEDUP_PRESET)

    # 收集所有启用的方法
    enabled_methods = [name for name, cfg in methods.items() if cfg.get("enabled")]

    if not enabled_methods or random_count == 0:
        return {"video_filters": "", "audio_filters": "", "applied": []}

    # 随机选择 N 个方法（不超过启用数量）
    count = min(random_count, len(enabled_methods))
    chosen = random.sample(enabled_methods, count)

    video_filters = []
    audio_filters = []

    # 用 clip_index 作为随机种子的一部分，让同一次运行结果可复现
    rng = random.Random(clip_index * 1000 + random.randint(0, 9999))

    # ---------- 1. 变速 ----------
    if "speed_change" in chosen:
        cfg = methods["speed_change"]
        speed = rng.uniform(cfg["min_speed"], cfg["max_speed"])
        speed = round(speed, 3)
        # 视频变速
        video_filters.append(f"setpts=PTS/{speed}")
        # 音频变速（atempo 范围 0.5~100，需要链式处理超过 2.0 的情况）
        audio_filters.append(f"atempo={speed}")

    # ---------- 2. 画面放大裁切 ----------
    if "zoom_crop" in chosen:
        cfg = methods["zoom_crop"]
        scale = rng.uniform(cfg["min_scale"], cfg["max_scale"])
        scale = round(scale, 3)
        new_w = int(width * scale)
        new_h = int(height * scale)
        # 确保是偶数（FFmpeg 要求）
        new_w = new_w + (new_w % 2)
        new_h = new_h + (new_h % 2)
        video_filters.append(f"scale={new_w}:{new_h}")
        video_filters.append(f"crop={width}:{height}")

    # ---------- 3. 水平镜像 ----------
    if "mirror" in chosen:
        video_filters.append("hflip")

    # ---------- 4. 抽帧（降低帧率） ----------
    if "frame_drop" in chosen:
        cfg = methods["frame_drop"]
        fps = int(rng.uniform(cfg["min_fps"], cfg["max_fps"]))
        video_filters.append(f"fps={fps}")

    # ---------- 5. 色调微调 ----------
    if "color_shift" in chosen:
        cfg = methods["color_shift"]
        brightness = rng.uniform(*cfg["brightness_range"])
        contrast = rng.uniform(*cfg["contrast_range"])
        saturation = rng.uniform(*cfg["saturation_range"])
        video_filters.append(
            f"eq=brightness={brightness:.3f}:contrast={contrast:.3f}:saturation={saturation:.3f}"
        )

    # ---------- 6. 轻微旋转 ----------
    if "rotation" in chosen:
        cfg = methods["rotation"]
        angle = rng.uniform(-cfg["max_angle"], cfg["max_angle"])
        # 用旋转 + 放大裁切实现（避免黑边）
        rad = abs(math.radians(angle))
        expand_w = int(width * (math.cos(rad) + math.sin(rad)))
        expand_h = int(height * (math.cos(rad) + math.sin(rad)))
        expand_w = expand_w + (expand_w % 2)
        expand_h = expand_h + (expand_h % 2)
        # 先放大再旋转再裁切
        video_filters.append(f"scale={expand_w}:{expand_h}")
        video_filters.append(f"rotate={angle/180:.6f}*PI/180")
        video_filters.append(f"crop={width}:{height}")

    # ---------- 7. 添加噪点 ----------
    if "noise" in chosen:
        cfg = methods["noise"]
        video_filters.append(f"noise=alls={cfg['strength']}:allf=t+u")

    # ---------- 8. 像素偏移 ----------
    if "pixel_shift" in chosen:
        cfg = methods["pixel_shift"]
        shift_x = rng.randint(-cfg["max_shift_x"], cfg["max_shift_x"])
        shift_y = rng.randint(-cfg["max_shift_y"], cfg["max_shift_y"])
        if shift_x != 0 or shift_y != 0:
            # 放大一点再偏移裁切
            pad = max(abs(shift_x), abs(shift_y)) * 2 + 2
            new_w = width + pad * 2
            new_h = height + pad * 2
            video_filters.append(f"scale={new_w}:{new_h}")
            video_filters.append(f"crop={width}:{height}:{pad + shift_x}:{pad + shift_y}")

    # ---------- 9. 边缘模糊羽化 ----------
    if "edge_blur" in chosen:
        # 在视频边缘添加轻微模糊
        video_filters.append("boxblur=1:1:cr=0:ar=0")

    # ---------- 10. 音频音调微调 ----------
    if "audio_pitch" in chosen and "speed_change" not in chosen:
        cfg = methods["audio_pitch"]
        pitch = rng.uniform(*cfg["pitch_range"])
        # asetrate 改变采样率来调整音调，然后用 aresample 恢复
        original_rate = 44100
        new_rate = int(original_rate * pitch)
        audio_filters.append(f"asetrate={new_rate}")
        audio_filters.append(f"aresample={original_rate}")

    # 组合滤镜
    vf = ",".join(video_filters) if video_filters else ""
    af = ",".join(audio_filters) if audio_filters else ""

    return {
        "video_filters": vf,
        "audio_filters": af,
        "applied": chosen,
    }


# ============================================================
# 第一步：解析 SRT
# ============================================================

def parse_srt(srt_path):
    print(f"  [parse] 正在解析字幕: {os.path.basename(srt_path)}")

    try:
        subs, encoding = open_srt(srt_path)
        print(f"  [parse] 字幕编码: {encoding}，共 {len(subs)} 条")
    except Exception as e:
        print(f"  [error] 解析 SRT 失败: {e}")
        return []

    core_clips = {}

    for sub in subs:
        text = sub.text.strip()
        if not text:
            continue

        start = _time_to_seconds(sub.start)
        end = _time_to_seconds(sub.end)
        duration = end - start

        if duration < 1 or duration > 10:
            continue

        clip_type = None
        for c_type, keywords in CLIP_KEYWORDS.items():
            if any(key in text for key in keywords):
                clip_type = c_type
                break

        if not clip_type or clip_type in core_clips:
            continue

        optimized = text
        for noise in ["我说实话", "其实", "跟你说", "你知道吗", "我跟你们讲"]:
            optimized = optimized.replace(noise, "")
        optimized = optimized.strip()
        if optimized and optimized[-1] not in "！!。.？?":
            optimized += "！"

        if optimized:
            core_clips[clip_type] = (optimized, start, end)

    ordered_clips = []
    for clip_type in CLIP_ORDER:
        if clip_type in core_clips:
            text, start, end = core_clips[clip_type]
            ordered_clips.append((clip_type, text, start, end))

    print(f"\n  [result] 提取到 {len(ordered_clips)} 个核心片段:")
    print("  " + "-" * 65)
    for i, (c_type, text, start, end) in enumerate(ordered_clips):
        print(f"    [{i+1:02d}] {c_type:<14s} | {start:7.2f}s - {end:7.2f}s | {text}")
    print("  " + "-" * 65)

    return ordered_clips


# ============================================================
# 第二步：FFmpeg 切割 + 去重 + 拼接
# ============================================================

def cut_video(video_path, ordered_clips, output_path):
    ffmpeg = get_ffmpeg_cmd()
    temp_dir = os.path.join(os.path.dirname(os.path.abspath(output_path)), "..", "temp")
    os.makedirs(temp_dir, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    cfg = VIDEO_CONFIG
    w, h = cfg["resolution"].split(":")

    print(f"\n  [cut] 去重预设: {DEDUP_PRESET}")
    print(f"  [cut] 正在切割 {len(ordered_clips)} 个片段（含去重处理）...\n")

    temp_files = []

    for i, (c_type, text, start, end) in enumerate(ordered_clips):
        temp_file = os.path.join(temp_dir, f"clip_{i:02d}.mp4")

        # 构建去重滤镜
        dedup = build_dedup_filters(int(w), int(h), i)

        # 基础视频滤镜：缩放 + 画面比
        base_vf = f"scale={cfg['resolution']},setdar=9/16"
        if dedup["video_filters"]:
            # 去重滤镜放在缩放之前（除了一些需要在缩放之后的）
            vf = dedup["video_filters"] + "," + base_vf
        else:
            vf = base_vf

        # 构建命令
        cmd = [ffmpeg, "-y"]
        cmd += ["-ss", f"{start:.3f}", "-i", video_path]
        cmd += ["-ss", "0", "-t", f"{end - start:.3f}"]
        cmd += ["-vf", vf]
        cmd += ["-r", str(cfg["fps"])]
        cmd += ["-b:v", cfg["bitrate_v"]]
        cmd += ["-c:v", cfg["codec_v"], "-preset", cfg["preset"]]

        # 音频滤镜
        if dedup["audio_filters"]:
            cmd += ["-af", dedup["audio_filters"]]

        cmd += ["-c:a", cfg["codec_a"], "-b:a", cfg["bitrate_a"]]
        cmd += ["-avoid_negative_ts", "make_zero"]
        cmd += [temp_file]

        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=_NO_WINDOW)

        applied_str = ",".join(dedup["applied"]) if dedup["applied"] else "none"

        if result.returncode == 0 and os.path.exists(temp_file):
            size_mb = os.path.getsize(temp_file) / (1024 * 1024)
            print(f"    OK [{c_type:<14s}] {start:.2f}s-{end:.2f}s -> {size_mb:.1f}MB | dedup: {applied_str}")
            temp_files.append(temp_file)
        else:
            print(f"    FAIL [{c_type:<14s}] {start:.2f}s-{end:.2f}s | dedup: {applied_str}")
            # 如果去重导致失败，回退到无去重版本
            cmd_clean = [ffmpeg, "-y"]
            cmd_clean += ["-ss", f"{start:.3f}", "-i", video_path]
            cmd_clean += ["-ss", "0", "-t", f"{end - start:.3f}"]
            cmd_clean += ["-vf", base_vf]
            cmd_clean += ["-r", str(cfg["fps"]), "-b:v", cfg["bitrate_v"]]
            cmd_clean += ["-c:v", cfg["codec_v"], "-preset", cfg["preset"]]
            cmd_clean += ["-c:a", cfg["codec_a"], "-b:a", cfg["bitrate_a"]]
            cmd_clean += ["-avoid_negative_ts", "make_zero"]
            cmd_clean += [temp_file]
            result2 = subprocess.run(cmd_clean, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=_NO_WINDOW)
            if result2.returncode == 0 and os.path.exists(temp_file):
                size_mb = os.path.getsize(temp_file) / (1024 * 1024)
                print(f"    RETRY OK (no dedup) -> {size_mb:.1f}MB")
                temp_files.append(temp_file)

    if not temp_files:
        print("  [error] 没有成功切割任何片段！")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return False

    # 生成 concat 列表
    list_file = os.path.join(temp_dir, "file_list.txt")
    with open(list_file, "w", encoding="ascii") as f:
        for tf in temp_files:
            abs_path = os.path.abspath(tf).replace("\\", "/")
            f.write(f"file '{abs_path}'\n")

    # 拼接
    print(f"\n  [concat] 正在拼接 {len(temp_files)} 个片段...")
    concat_cmd = [
        ffmpeg, "-y",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-c", "copy", "-movflags", "+faststart",
        output_path
    ]

    result = subprocess.run(concat_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=_NO_WINDOW)

    if result.returncode == 0 and os.path.exists(output_path):
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"\n  [done] 爆款视频生成成功！")
        print(f"    path: {os.path.abspath(output_path)}")
        print(f"    size: {size_mb:.1f} MB")
        print(f"    resolution: {cfg['resolution']} (9:16)")
        print(f"    clips: {len(temp_files)}")
        print(f"    dedup: {DEDUP_PRESET}")
    else:
        print("  [error] 拼接失败！")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return False

    shutil.rmtree(temp_dir, ignore_errors=True)
    print(f"  [clean] temp files removed")

    return True


# ============================================================
# 主函数
# ============================================================

def main():
    print("=" * 65)
    print("  Live Stream Clip Cutter v2.0 (with dedup)")
    print("=" * 65)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    input_dir = os.path.join(script_dir, "input")
    output_dir = os.path.join(script_dir, "output")

    # 自动查找文件
    video_path = None
    srt_path = None

    for ext in ["*.mp4", "*.mov", "*.avi", "*.mkv", "*.flv", "*.MP4", "*.MOV"]:
        matches = glob.glob(os.path.join(input_dir, ext))
        if matches:
            video_path = matches[0]
            break

    srt_matches = glob.glob(os.path.join(input_dir, "*.srt")) + glob.glob(os.path.join(input_dir, "*.SRT"))
    if srt_matches:
        srt_path = srt_matches[0]

    # 命令行参数
    # 用法: python cutter.py [视频路径] [SRT路径] [--dedup=none|light|medium|heavy|custom]
    dedup_override = None
    if len(sys.argv) >= 3:
        video_path = sys.argv[1]
        srt_path = sys.argv[2]

    for arg in sys.argv[1:]:
        if arg.startswith("--dedup="):
            dedup_override = arg.split("=")[1]

    if dedup_override:
        global DEDUP_PRESET
        DEDUP_PRESET = dedup_override
        print(f"  [config] dedup preset overwritten: {DEDUP_PRESET}")

    # 校验
    if not video_path or not os.path.exists(video_path):
        print(f"  [error] video not found! put video in: {input_dir}")
        if os.path.exists(input_dir):
            print(f"  [info] files in input/: {os.listdir(input_dir)}")
        return

    if not srt_path or not os.path.exists(srt_path):
        print(f"  [error] srt not found! put .srt in: {input_dir}")
        if os.path.exists(input_dir):
            print(f"  [info] files in input/: {os.listdir(input_dir)}")
        return

    print(f"\n  video: {os.path.basename(video_path)}")
    print(f"  srt:   {os.path.basename(srt_path)}")

    # 验证 FFmpeg
    ffmpeg = get_ffmpeg_cmd()
    try:
        subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
        print(f"  ffmpeg: OK")
    except Exception:
        print(f"  [error] FFmpeg not available! check FFMPEG_PATH in config.py")
        return

    print()

    # 执行
    ordered_clips = parse_srt(srt_path)

    if not ordered_clips:
        print("  [error] no clips extracted. check keywords in config.py")
        return

    video_name = os.path.splitext(os.path.basename(video_path))[0]
    output_path = os.path.join(output_dir, f"{video_name}_dedup.mp4")

    success = cut_video(video_path, ordered_clips, output_path)

    if success:
        print("\n" + "=" * 65)
        print("  ALL DONE! check output folder")
        print("=" * 65)
        print()
        print("  usage tips:")
        print("    python cutter.py                          # medium dedup")
        print("    python cutter.py --dedup=none             # no dedup")
        print("    python cutter.py --dedup=light            # light dedup")
        print("    python cutter.py --dedup=heavy            # heavy dedup")
        print("    python cutter.py --dedup=custom           # custom from config")


if __name__ == "__main__":
    main()
