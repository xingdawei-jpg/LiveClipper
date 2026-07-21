import sys
# -*- coding: utf-8 -*-
"""
Smart Crop 智能裁切模块 v7
- 三级检测：HOG人体 → Haar上半身 → Haar人脸
- 智能兜底：根据人物位置/大小自动限制最大zoom，绝不裁掉头部
- 裁切程度可调（轻/中/重），独立于去重选项
- 底部不裁切
- cv2不可用 → 降级标准裁切
"""

import os
import random

_CV2_AVAILABLE = False
try:
    import cv2
    import numpy as np
    _CV2_AVAILABLE = True
except ImportError:
        _LOG.warning("cv2 import failed", exc_info=True)

# HOG 人体检测器（OpenCV内置，无需额外文件）
_HOG = None

# Haar 级联检测器缓存
_CASCADES = {}

# 裁切程度配置
CROP_LEVELS = {
    'light':  {'max_zoom': 1.05, 'min_zoom': 1.02, 'keep_body': 0.94, 'head_pos': 0.055, 'label': '轻'},
    'medium': {'max_zoom': 1.12, 'min_zoom': 1.055, 'keep_body': 0.88, 'head_pos': 0.070, 'label': '中'},
    'heavy':  {'max_zoom': 1.25, 'min_zoom': 1.08, 'keep_body': 0.82, 'head_pos': 0.085, 'label': '重'},
}


def _get_hog():
    global _HOG
    if _HOG is None and _CV2_AVAILABLE:
        try:
            _HOG = cv2.HOGDescriptor()
            _HOG.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        except Exception:
            _HOG = None
    return _HOG


def _get_cascade(name):
    if name in _CASCADES:
        return _CASCADES[name]
    if not _CV2_AVAILABLE:
        return None
    # Priority 1: app/ directory (bundled with the tool, works in PyInstaller)
    _app_dir = os.path.dirname(os.path.abspath(__file__))
    _app_path = os.path.join(_app_dir, name)
    if os.path.exists(_app_path):
        cascade = cv2.CascadeClassifier(_app_path)
        if not cascade.empty():
            _CASCADES[name] = cascade
            return cascade
    # Priority 2: cv2.data.haarcascades (system OpenCV installation)
    _cv2_path = os.path.join(cv2.data.haarcascades, name)
    if os.path.exists(_cv2_path):
        cascade = cv2.CascadeClassifier(_cv2_path)
        if not cascade.empty():
            _CASCADES[name] = cascade
            return cascade
    return None


def _detect_persons(frame, conf_threshold=0.3, _log_fn=None):
    """四级人体检测：HOG人体 -> Haar上半身 -> Haar人脸 -> 皮肤色检测"""
    if not _CV2_AVAILABLE:
        return []

    h, w = frame.shape[:2]
    all_detections = []

    # Level 1: HOG 人体检测（检测全身/半身）
    hog = _get_hog()
    if hog is not None:
        try:
            scale = min(1.0, 640.0 / max(w, h))
            if scale < 1.0:
                small = cv2.resize(frame, (int(w * scale), int(h * scale)))
            else:
                small = frame
                scale = 1.0
            regions, weights = hog.detectMultiScale(
                small, winStride=(8, 8), padding=(4, 4), scale=1.05
            )
            if len(regions) > 0:
                for idx, (x, y, rw, rh) in enumerate(regions):
                    wt = float(weights[idx][0]) if idx < len(weights) else 0.0
                    if wt > conf_threshold:
                        all_detections.append((
                            int(x / scale), int(y / scale),
                            int(rw / scale), int(rh / scale),
                            wt, 'body'
                        ))
        except Exception:
                        _LOG.warning("detection error", exc_info=True)

    if all_detections:
        return all_detections

    # Level 2: Haar 上半身检测
    upper_cascade = _get_cascade('haarcascade_upperbody.xml')
    if upper_cascade is not None:
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            bodies = upper_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=3, minSize=(60, 60))
            for x, y, bw, bh in bodies:
                all_detections.append((x, y, bw, bh, 0.8, 'upper'))
        except Exception:
                        _LOG.warning("detection error", exc_info=True)

    if all_detections:
        return all_detections

    # Level 3: Haar 人脸检测（兜底）-> 扩展为上半身估算
    face_cascade = _get_cascade('haarcascade_frontalface_default.xml')
    if face_cascade is not None:
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=3, minSize=(30, 30))
            for x, y, fw, fh in faces:
                expand_y = int(fh * 0.5)
                expand_h = int(fh * 3)
                new_x = max(0, x - int(fw * 0.3))
                new_y = max(0, y - expand_y)
                new_w = int(fw * 1.6)
                new_h = min(fh + expand_h + expand_y, h - new_y)
                all_detections.append((new_x, new_y, new_w, new_h, 0.6, 'face_expanded'))
        except Exception:
                        _LOG.warning("detection error", exc_info=True)

    if all_detections:
        return all_detections

    # Level 4: 皮肤色检测（无需外部文件，所有OpenCV版本通用）
    try:
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        mask1 = cv2.inRange(hsv, np.array([0, 30, 60], dtype=np.uint8), np.array([25, 150, 255], dtype=np.uint8))
        mask2 = cv2.inRange(hsv, np.array([170, 30, 60], dtype=np.uint8), np.array([180, 150, 255], dtype=np.uint8))
        mask = cv2.bitwise_or(mask1, mask2)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            largest = max(contours, key=cv2.contourArea)
            area = cv2.contourArea(largest)
            min_area = h * w * 0.03
            if area >= min_area:
                x, y, bw, bh = cv2.boundingRect(largest)
                all_detections.append((x, y, bw, bh, 0.5, 'skin'))
    except Exception:
                _LOG.warning("detection error", exc_info=True)

    return all_detections
def prepare_face_detector(app_dir=None, log_fn=None):
    """初始化检测器（兼容旧接口）"""
    if not _CV2_AVAILABLE:
        if log_fn:
            log_fn("SmartCrop: 需要完整安装包（当前为增量更新，使用标准裁切）")
        return False

    # 预加载 HOG
    _get_hog()

    if log_fn:
        log_fn("SmartCrop: 检测器就绪（HOG人体+Haar级联）")
    return True


def batch_detect_clips(video_path, clips, log_fn=None, ffmpeg_cmd=None, frame_w=0, frame_h=0):
    """批量检测片段中的人物位置（使用FFmpeg提取帧，兼容中文路径）"""
    if not _CV2_AVAILABLE:
        if log_fn:
            log_fn("SmartCrop: \u9700\u8981\u5b8c\u6574\u5b89\u88c5\u5305\uff0c\u4f7f\u7528\u6807\u51c6\u88c1\u5207")
        return {i: None for i in range(len(clips))}

    results = {}
    prepare_face_detector(log_fn=log_fn)

    # \u4f18\u5148\u4f7f\u7528FFmpeg\u63d0\u53d6\u5e27\uff08\u517c\u5bb9\u4e2d\u6587\u8def\u5f84\uff09
    use_ffmpeg = ffmpeg_cmd is not None
    cap = None

    if frame_w <= 0 or frame_h <= 0:
        if log_fn:
            log_fn("SmartCrop: \u65e0\u89c6\u9891\u5c3a\u5bf8\uff0c\u964d\u7ea7\u4e3a\u6807\u51c6\u88c1\u5207")
        return {i: None for i in range(len(clips))}

    smart_count = 0
    for i, clip in enumerate(clips):
        start = clip[2]
        end = clip[3]
        duration = end - start

        sample_times = [
            start + duration * 0.2,
            start + duration * 0.5,
            start + duration * 0.8,
        ]

        person_xs = []
        person_ys = []
        person_sizes = []
        person_ws = []
        person_hs = []
        person_bottoms = []
        head_tops = []

        for ti, t in enumerate(sample_times):
            frame = None
            if use_ffmpeg:
                frame = _extract_frame_ffmpeg(ffmpeg_cmd, video_path, t, log_fn)
            elif cap is not None:
                fps_val = cap.get(cv2.CAP_PROP_FPS) or 30
                frame_idx = int(t * fps_val)
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
                ret, frame = cap.read()
                if not ret:
                    frame = None

            if frame is None:
                continue

            detections = _detect_persons(frame, _log_fn=log_fn)
            if detections:
                best = max(detections, key=lambda d: d[2] * d[3])
                cx = (best[0] + best[2] / 2) / frame_w
                cy = (best[1] + best[3] / 2) / frame_h
                person_xs.append(cx)
                person_ys.append(cy)
                person_sizes.append(max(best[2], best[3]) / max(frame_w, frame_h))
                person_ws.append(best[2] / frame_w)
                person_hs.append(best[3] / frame_h)
                person_bottoms.append((best[1] + best[3]) / frame_h)
                head_tops.append(best[1] / frame_h)

        if person_xs:
            cx = sorted(person_xs)[len(person_xs) // 2]
            cy = sorted(person_ys)[len(person_ys) // 2]
            avg_size = sum(person_sizes) / len(person_sizes)
            avg_w = sum(person_ws) / len(person_ws) if person_ws else 0
            avg_h = sum(person_hs) / len(person_hs) if person_hs else avg_size
            avg_bottom = sum(person_bottoms) / len(person_bottoms) if person_bottoms else min(1.0, cy + avg_h / 2)
            min_head_top = min(head_tops)

            results[i] = {
                'person_cx_ratio': cx,
                'person_cy_ratio': cy,
                'person_size_ratio': avg_size,
                'person_w_ratio': avg_w,
                'person_h_ratio': avg_h,
                'person_bottom_ratio': avg_bottom,
                'head_top_ratio': min_head_top,
                'frame_w': frame_w,
                'frame_h': frame_h,
            }
            smart_count += 1
        else:
            results[i] = None

    if cap is not None:
        cap.release()
    if log_fn:
        log_fn("SmartCrop: %d/%d \u7247\u6bb5\u68c0\u6d4b\u5230\u4eba\u7269" % (smart_count, len(clips)))
    return results


def _extract_frame_ffmpeg(ffmpeg_cmd, video_path, timestamp, log_fn=None):
    """\u4f7f\u7528FFmpeg\u63d0\u53d6\u6307\u5b9a\u65f6\u95f4\u70b9\u7684\u4e00\u5e27\uff08\u4e34\u65f6\u6587\u4ef6\u65b9\u5f0f\uff0c\u517c\u5bb9\u6240\u6709Windows\u73af\u5883\uff09"""
    import subprocess as _sp
    import tempfile
    _cflags = 0x08000000 if sys.platform == "win32" else 0
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        proc = _sp.run(
            [ffmpeg_cmd, "-y", "-ss", "%.2f" % timestamp,
             "-i", video_path,
             "-vframes", "1", tmp_path],
            capture_output=True, timeout=8, creationflags=_cflags)
        if proc.returncode == 0 and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 100:
            frame = cv2.imdecode(np.fromfile(tmp_path, dtype=np.uint8), cv2.IMREAD_COLOR)
            return frame
        else:
            if log_fn:
                _err = proc.stderr.decode("utf-8", errors="ignore")[-150:] if proc.stderr else ""
                if proc.returncode == 0:
                    log_fn("SmartCrop: 该时间点未提取到画面，可能是片段时间超出源视频或来源映射错误 (%ss)" % ("%.2f" % timestamp))
                else:
                    log_fn("SmartCrop: FFmpeg\u63d0\u5e27\u5931\u8d25 rc=%d %s" % (proc.returncode, _err.replace("\n", " ")))
    except Exception as _e:
        if log_fn:
            log_fn("SmartCrop: FFmpeg\u63d0\u53d6\u5e27\u5f02\u5e38: " + str(_e))
    finally:
        if tmp_path:
            try: os.remove(tmp_path)
            except: pass
    return None


def compute_smart_crop(person_info, frame_w, frame_h, crop_level='medium', log_fn=None):
    """计算智能裁切参数，含头部安全兜底

    Args:
        person_info: batch_detect_clips 的检测结果，None表示未检测到人物
        frame_w: 视频宽度
        frame_h: 视频高度
        crop_level: 裁切程度 'light'(轻)/'medium'(中)/'heavy'(重)
        log_fn: 日志函数
    """
    level_cfg = CROP_LEVELS.get(crop_level, CROP_LEVELS['medium'])
    max_zoom = level_cfg['max_zoom']
    min_zoom = level_cfg.get('min_zoom', 1.03)
    keep_body = level_cfg.get('keep_body', 0.88)
    target_head_pos = level_cfg.get('head_pos', 0.07)

    if person_info is None:
        return _random_crop(max_zoom)

    cx = person_info['person_cx_ratio']
    person_size = person_info.get('person_size_ratio', 0)
    person_h = person_info.get('person_h_ratio', person_size)
    person_bottom = person_info.get(
        'person_bottom_ratio',
        min(1.0, person_info.get('person_cy_ratio', 0.5) + person_h / 2),
    )
    head_top = person_info.get('head_top_ratio', 0.1)

    # 封面构图：优先保护头部，同时保留大部分身体，不再强制底部完全不裁。
    head_margin = 0.035
    body_margin = 0.025
    safe_top = max(0.0, head_top - head_margin)
    protected_bottom = min(1.0, min(person_bottom, head_top + person_h * keep_body) + body_margin)
    protected_h = max(0.35, protected_bottom - safe_top)
    safe_max_zoom = max(1.0, min(1.0 / protected_h, 2.0))

    actual_max_zoom = min(max_zoom, safe_max_zoom)

    if actual_max_zoom >= min_zoom:
        zoom = random.uniform(min_zoom, actual_max_zoom)
    elif actual_max_zoom > 1.01:
        zoom = random.uniform(1.01, actual_max_zoom)
    else:
        zoom = actual_max_zoom

    zoom = max(1.0, min(zoom, actual_max_zoom))

    crop_w = 1.0 / zoom
    crop_h = 1.0 / zoom

    # 水平居中于人物，加微小构图偏移。
    crop_x = cx - crop_w / 2 + random.uniform(-0.015, 0.015)
    crop_x = max(0, min(crop_x, 1.0 - crop_w))

    crop_y = head_top - (target_head_pos * crop_h)

    if head_top > 0:
        crop_y = min(crop_y, max(0.0, head_top - head_margin))
    crop_y = max(0, min(crop_y, 1.0 - crop_h))

    if log_fn:
        log_fn(
            "SmartCrop: cover zoom=%.2fx (safe=%.2fx, level=%s, head=%.2f, body=%.2f, y=%.2f)"
            % (zoom, safe_max_zoom, crop_level, head_top, person_h, crop_y)
        )

    return {
        'crop_w': crop_w,
        'crop_h': crop_h,
        'crop_x': crop_x,
        'crop_y': crop_y,
        'method': 'smart',
    }


def _random_crop(max_zoom=1.08):
    """无人检测时的随机裁切（保守，不裁头）"""
    upper = min(0.04, max_zoom - 1.0)
    if upper <= 0:
        upper = 0.01
    zoom = 1.0 + random.uniform(0.0, upper)
    crop_w = 1.0 / zoom
    crop_h = 1.0 / zoom
    crop_x = random.uniform(0, 1.0 - crop_w)
    crop_y = 1.0 - crop_h  # 底部不裁切
    return {
        'crop_w': crop_w,
        'crop_h': crop_h,
        'crop_x': crop_x,
        'crop_y': crop_y,
        'method': 'random',
    }


def _even(v):
    """确保偶数"""
    v = int(v)
    return v + (v % 2)


def _clamp(v, lo, hi):
    return max(lo, min(int(v), hi))


def _ken_burns_motion(intensity=None, max_zoom_delta=None):
    direction = random.choice(['in', 'out'])
    if intensity in ('light', '轻'):
        target_zoom = random.uniform(0.03, 0.10)
    elif intensity in ('heavy', '重'):
        target_zoom = random.uniform(0.15, 0.40)
    else:
        target_zoom = random.uniform(0.08, 0.25)
    if max_zoom_delta is not None:
        try:
            cap = max(0.02, float(max_zoom_delta))
            target_zoom = min(target_zoom, cap)
        except Exception:
                        _LOG.warning("smartcrop error", exc_info=True)
    return direction, target_zoom


def ken_burns_filter(clip_duration, w=1080, h=1920, fps=30, log_fn=None, intensity=None, max_zoom_delta=None):
    """Ken Burns filter using FFmpeg zoompan with stable CFR timestamps."""
    direction, target_zoom = _ken_burns_motion(intensity, max_zoom_delta=max_zoom_delta)
    target_fps = max(15.0, min(float(fps or 30), 60.0))
    total_frames = max(1, int(round(target_fps * clip_duration)))
    denom = max(total_frames - 1, 1)
    progress_expr = "(on/%d)" % denom
    ease_expr = "(3*pow(%s,2)-2*pow(%s,3))" % (progress_expr, progress_expr)

    if direction == 'in':
        zoom_expr = "1+%.6f*%s" % (target_zoom, ease_expr)
    else:
        zoom_expr = "1+%.6f*(1-%s)" % (target_zoom, ease_expr)

    result = (
        "fps=fps=%.3f:round=near,"
        "zoompan=z='%s':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        "d=1:s=%dx%d:fps=%.3f,"
        "setpts=N/(%.3f*TB),format=yuv420p"
    ) % (target_fps, zoom_expr, w, h, target_fps, target_fps)

    if log_fn:
        label = '\u63a8\u8fdb' if direction == 'in' else '\u62c9\u8fdc'
        cap_text = ""
        if max_zoom_delta is not None:
            try:
                cap_text = ", 画质上限 %.0f%%" % (float(max_zoom_delta) * 100)
            except Exception:
                cap_text = ""
        log_fn("KenBurns: %s %.0f%% (%d frames @ %.0ffps%s, FFmpeg)" % (label, target_zoom * 100, total_frames, target_fps, cap_text))

    return result


def apply_ken_burns_ffmpeg(clip_path, output_path, clip_duration, w, h, fps, ffmpeg_cmd, log_fn=None, intensity=None, max_zoom_delta=None):
    """Apply Ken Burns with FFmpeg zoompan. Much faster than Python/OpenCV frame piping."""
    import subprocess as _sp

    actual_fps = max(15.0, min(float(fps or 30), 60.0))
    safe_duration = max(0.05, float(clip_duration or 0.05))
    vf = ken_burns_filter(clip_duration, w=w, h=h, fps=actual_fps, log_fn=log_fn, intensity=intensity, max_zoom_delta=max_zoom_delta)
    af = "atrim=0:%.3f,asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0" % safe_duration
    _cflags = 0x08000000 if sys.platform == "win32" else 0
    cmd = [
        ffmpeg_cmd, "-y",
        "-i", clip_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "12",
        "-pix_fmt", "yuv420p",
        "-af", af,
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        "-shortest",
        "-movflags", "+faststart",
        output_path,
    ]
    try:
        proc = _sp.run(cmd, stdout=_sp.DEVNULL, stderr=_sp.PIPE,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=max(60, int(float(clip_duration or 1) * 12)),
                       creationflags=_cflags)
    except Exception as e:
        if log_fn:
            log_fn("KenBurns: FFmpeg zoompan failed: %s" % e)
        return False

    if proc.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) <= 1000:
        if log_fn:
            err = (proc.stderr or "").strip().split("\n")[-2:]
            log_fn("KenBurns: FFmpeg zoompan failed rc=%d %s" % (proc.returncode, " ".join(err)[:180]))
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
                        _LOG.warning("smartcrop error", exc_info=True)
        return False
    return True


def apply_ken_burns_opencv(clip_path, output_path, clip_duration, w, h, fps, ffmpeg_cmd, log_fn=None, intensity=None, max_zoom_delta=None):
    """Ken Burns effect using OpenCV frame-by-frame processing.
    
    Reads each frame via cv2, applies animated zoom (crop+resize),
    writes via FFmpeg pipe with audio from original clip.
    
    Returns True if successful, False otherwise.
    """
    if not _CV2_AVAILABLE:
        if log_fn:
            log_fn("KenBurns: OpenCV not available, skip")
        return False

    import subprocess as _sp
    import time as _time

    direction, target_zoom = _ken_burns_motion(intensity, max_zoom_delta=max_zoom_delta)
    target_fps = max(15.0, min(float(fps or 30), 60.0))
    safe_duration = max(0.05, float(clip_duration or 0.05))
    total_frames = max(1, int(round(target_fps * safe_duration)))
    started_at = _time.perf_counter()

    cap = cv2.VideoCapture(clip_path)
    if not cap.isOpened():
        if log_fn:
            log_fn("KenBurns: cannot open video")
        return False
    source_fps = cap.get(cv2.CAP_PROP_FPS) or target_fps
    if not source_fps or source_fps <= 1 or source_fps > 240:
        source_fps = target_fps
    source_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if source_frames > 0:
        source_duration = source_frames / float(source_fps)
        if source_duration > 0:
            total_frames = max(1, min(total_frames, int(round(source_duration * target_fps))))

    # FFmpeg pipe: raw video from stdin + audio from original clip
    _cflags = 0x08000000 if sys.platform == "win32" else 0
    pipe_cmd = [
        ffmpeg_cmd, "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", "%dx%d" % (w, h), "-pix_fmt", "bgr24",
        "-r", str(target_fps),
        "-i", "-",
        "-i", clip_path,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "12",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-af", "atrim=0:%.3f,asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0" % safe_duration,
        "-map", "0:v:0",
        "-map", "1:a:0?",
        "-shortest",
        "-movflags", "+faststart",
        output_path
    ]

    try:
        proc = _sp.Popen(pipe_cmd, stdin=_sp.PIPE,
                         stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                         creationflags=_cflags)
    except Exception as e:
        cap.release()
        if log_fn:
            log_fn("KenBurns: FFmpeg pipe failed: %s" % e)
        return False

    try:
        last_frame = None
        src_idx = -1
        eof_padding = 0
        written_frames = 0
        for out_idx in range(total_frames):
            desired_src_idx = int(round((out_idx / target_fps) * source_fps))
            while src_idx < desired_src_idx:
                ret, next_frame = cap.read()
                if not ret:
                    eof_padding += 1
                    break
                src_idx += 1
                last_frame = next_frame
                eof_padding = 0

            if last_frame is None:
                ret, last_frame = cap.read()
                if not ret:
                    break
                src_idx += 1

            if last_frame is None:
                break
            if eof_padding > 3:
                break
            frame = last_frame

            # Calculate zoom for this frame (ease-in-out curve)
            progress = min(out_idx, total_frames - 1) / max(total_frames - 1, 1)
            eased = (3 * progress * progress) - (2 * progress * progress * progress)
            if direction == 'in':
                zoom = 1.0 + target_zoom * eased
            else:
                zoom = 1.0 + target_zoom * (1.0 - eased)

            fh, fw = frame.shape[:2]
            crop_w = int(fw / zoom)
            crop_h = int(fh / zoom)
            # Ensure even
            crop_w -= crop_w % 2
            crop_h -= crop_h % 2

            # Center crop
            cx = (fw - crop_w) // 2
            cy = (fh - crop_h) // 2
            cropped = frame[cy:cy+crop_h, cx:cx+crop_w]

            # Resize to target
            if cropped.shape[1] != w or cropped.shape[0] != h:
                resized = cv2.resize(cropped, (w, h), interpolation=cv2.INTER_LANCZOS4)
            else:
                resized = cropped

            try:
                proc.stdin.write(resized.tobytes())
                written_frames += 1
            except BrokenPipeError:
                break
    except Exception as e:
        if log_fn:
            log_fn("KenBurns: frame processing error: %s" % e)
    finally:
        cap.release()
        try:
            proc.stdin.close()
        except Exception:
                        _LOG.warning("smartcrop error", exc_info=True)
        proc.wait()

    if proc.returncode != 0:
        if log_fn:
            log_fn("KenBurns: FFmpeg encode failed rc=%d" % proc.returncode)
        if os.path.exists(output_path):
            os.remove(output_path)
        return False
    if written_frames < max(1, int(total_frames * 0.85)):
        if log_fn:
            log_fn("KenBurns: OpenCV wrote too few frames (%d/%d), fallback needed" % (written_frames, total_frames))
        if os.path.exists(output_path):
            os.remove(output_path)
        return False

    label = '\u63a8\u8fdb' if direction == 'in' else '\u62c9\u8fdc'
    if log_fn:
        elapsed = max(0.0, _time.perf_counter() - started_at)
        output_duration = written_frames / target_fps if target_fps > 0 else float(clip_duration or 0)
        speed = (output_duration / elapsed) if elapsed > 0 else 0.0
        log_fn("KenBurns: %s %.0f%% (%d/%d frames @ %.0ffps, OpenCV, %.1fs, %.2fx)" % (
            label, target_zoom * 100, written_frames, total_frames, target_fps, elapsed, speed))

    return True
