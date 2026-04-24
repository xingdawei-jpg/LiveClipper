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
    pass

# HOG 人体检测器（OpenCV内置，无需额外文件）
_HOG = None

# Haar 级联检测器缓存
_CASCADES = {}

# 裁切程度配置
CROP_LEVELS = {
    'light':  {'max_zoom': 1.05, 'label': '轻'},
    'medium': {'max_zoom': 1.12, 'label': '中'},
    'heavy':  {'max_zoom': 1.25, 'label': '重'},
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
            pass

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
            pass

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
            pass

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
        pass

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
                head_tops.append(best[1] / frame_h)

        if person_xs:
            cx = sorted(person_xs)[len(person_xs) // 2]
            cy = sorted(person_ys)[len(person_ys) // 2]
            avg_size = sum(person_sizes) / len(person_sizes)
            min_head_top = min(head_tops)

            results[i] = {
                'person_cx_ratio': cx,
                'person_cy_ratio': cy,
                'person_size_ratio': avg_size,
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

    if person_info is None:
        return _random_crop(max_zoom)

    cx = person_info['person_cx_ratio']
    person_size = person_info.get('person_size_ratio', 0)
    head_top = person_info.get('head_top_ratio', 0.1)

    # ====== 头部安全兜底 ======
    # 底部不裁切：crop_y + crop_h = 1.0
    # 需要头部在裁切区域内且上方留5%边距
    # 即 crop_y <= head_top - 0.05
    # 所以 crop_h >= 1.0 - (head_top - 0.05)
    # safe_zoom = 1.0 / crop_h <= 1.0 / (1.0 - head_top + 0.05)
    head_margin = 0.05
    if head_top > 0:
        safe_max_zoom = 1.0 / (1.0 - head_top + head_margin)
        safe_max_zoom = max(1.0, min(safe_max_zoom, 2.0))
    else:
        safe_max_zoom = max_zoom

    # 实际最大zoom = min(用户选择程度, 安全上限)
    actual_max_zoom = min(max_zoom, safe_max_zoom)

    # 根据人物大小决定zoom力度
    if person_size > 0.5:
        # 人物已经很大，几乎不zoom
        zoom = 1.0 + random.uniform(0.0, min(0.03, actual_max_zoom - 1.0))
    elif person_size > 0.3:
        # 中等距离
        zoom = 1.0 + random.uniform(0.01, min(0.06, actual_max_zoom - 1.0))
    else:
        # 人物较远，正常zoom
        upper = actual_max_zoom - 1.0
        if upper > 0.02:
            zoom = 1.0 + random.uniform(0.02, upper)
        else:
            zoom = 1.0 + random.uniform(0.0, max(0.01, upper))

    zoom = max(1.0, min(zoom, actual_max_zoom))

    crop_w = 1.0 / zoom
    crop_h = 1.0 / zoom

    # 水平居中于人物，加微小随机偏移
    crop_x = cx - crop_w / 2 + random.uniform(-0.02, 0.02)
    crop_x = max(0, min(crop_x, 1.0 - crop_w))

    # 垂直：底部不裁切
    crop_y = 1.0 - crop_h

    # 二次安全检查：确保头部在裁切区域内
    if head_top > 0 and crop_y > head_top - head_margin:
        crop_y = max(0, head_top - head_margin)
        crop_h = 1.0 - crop_y
        zoom = 1.0 / min(crop_w, crop_h)

    crop_y = max(0, min(crop_y, 1.0 - crop_h))

    if log_fn:
        log_fn("SmartCrop: zoom=%.2fx (安全上限=%.2fx, 程度=%s)" % (zoom, safe_max_zoom, crop_level))

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


def ken_burns_filter(clip_duration, w=1080, h=1920, fps=30, log_fn=None):
    """Ken Burns: crop+scale 二次编码, 用 n(帧号) 做动画

    在已切好的片段上做动画, n 从0开始, 不受 seeking 影响。
    crop 居中 (iw-ow)/2, 不做平移。scale 回原尺寸保证输出稳定。

    返回: FFmpeg 滤镜字符串, 可直接用于二次编码的 -vf
    """
    direction = random.choice(['in', 'out'])
    target_zoom = random.uniform(0.08, 0.25)  # 8-25%
    total_frames = max(1, int(fps * clip_duration))

    if direction == 'in':
        # 推进: n=0时满画幅, n=total时缩到1-target_zoom
        cw_expr = "iw-iw*%.4f*n/%d" % (target_zoom, total_frames)
        ch_expr = "ih-ih*%.4f*n/%d" % (target_zoom, total_frames)
    else:
        # 拉远: n=0时缩到1-target_zoom, n=total时满画幅
        cw_expr = "iw-iw*%.4f*(%d-n)/%d" % (target_zoom, total_frames, total_frames)
        ch_expr = "ih-ih*%.4f*(%d-n)/%d" % (target_zoom, total_frames, total_frames)

    result = "crop=%s:%s:(iw-ow)/2:(ih-oh)/2,scale=%d:%d" % (cw_expr, ch_expr, w, h)

    if log_fn:
        label = '\u63a8\u8fdb' if direction == 'in' else '\u62c9\u8fdc'
        log_fn("KenBurns: %s %.0f%% (%d frames)" % (label, target_zoom * 100, total_frames))

    return result


def apply_ken_burns_opencv(clip_path, output_path, clip_duration, w, h, fps, ffmpeg_cmd, log_fn=None):
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

    direction = random.choice(['in', 'out'])
    target_zoom = random.uniform(0.08, 0.25)  # 8-25%
    total_frames = max(1, int(fps * clip_duration))

    cap = cv2.VideoCapture(clip_path)
    if not cap.isOpened():
        if log_fn:
            log_fn("KenBurns: cannot open video")
        return False

    actual_fps = cap.get(cv2.CAP_PROP_FPS) or fps

    # FFmpeg pipe: raw video from stdin + audio from original clip
    _cflags = 0x08000000 if sys.platform == "win32" else 0
    pipe_cmd = [
        ffmpeg_cmd, "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", "%dx%d" % (w, h), "-pix_fmt", "bgr24",
        "-r", str(actual_fps),
        "-i", "-",
        "-i", clip_path,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-map", "0:v:0",
        "-map", "1:a:0?",
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

    frame_idx = 0
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            # Calculate zoom for this frame (ease-in-out curve)
            progress = min(frame_idx, total_frames) / max(total_frames, 1)
            # Smooth ease-in-out: slow start, fast middle, slow end
            eased = 0.5 - 0.5 * (1.0 - 2.0 * progress) * abs(1.0 - 2.0 * progress) if progress < 0.5 else 0.5 + 0.5 * (2.0 * progress - 1.0) * abs(2.0 * progress - 1.0)
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
                resized = cv2.resize(cropped, (w, h), interpolation=cv2.INTER_LINEAR)
            else:
                resized = cropped

            try:
                proc.stdin.write(resized.tobytes())
            except BrokenPipeError:
                break

            frame_idx += 1
    except Exception as e:
        if log_fn:
            log_fn("KenBurns: frame processing error: %s" % e)
    finally:
        cap.release()
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.wait()

    if proc.returncode != 0:
        if log_fn:
            log_fn("KenBurns: FFmpeg encode failed rc=%d" % proc.returncode)
        if os.path.exists(output_path):
            os.remove(output_path)
        return False

    label = '\u63a8\u8fdb' if direction == 'in' else '\u62c9\u8fdc'
    if log_fn:
        log_fn("KenBurns: %s %.0f%% (%d frames, OpenCV)" % (label, target_zoom * 100, total_frames))

    return True
