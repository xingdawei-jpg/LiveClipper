"""
Smart Crop 智能裁切模块 v5
- cv2不可用时自动pip install opencv-python-headless（方案A）
- pip安装失败则降级为随机裁切
- 只裁左右+微裁顶部，绝不裁底部
- zoom上限1.15x
"""

import os
import sys
import subprocess
import random

# 尝试导入cv2
_CV2_AVAILABLE = False
_CV2_AUTO_INSTALLED = False

try:
    import cv2
    import numpy as np
    _CV2_AVAILABLE = True
except ImportError:
    pass


def _try_install_cv2(log_fn=None):
    """自动安装opencv-python-headless（方案A）"""
    global _CV2_AVAILABLE, _CV2_AUTO_INSTALLED

    if _CV2_AVAILABLE or _CV2_AUTO_INSTALLED:
        return _CV2_AVAILABLE

    _CV2_AUTO_INSTALLED = True  # 只尝试一次

    if log_fn:
        log_fn("SmartCrop: 正在安装OpenCV（首次使用需要下载，约40MB）...")

    try:
        # 用清华镜像加速下载
        subprocess.check_call(
            [sys.executable, '-m', 'pip', 'install',
             'opencv-python-headless', '-q',
             '-i', 'https://pypi.tuna.tsinghua.edu.cn/simple'],
            timeout=300,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # 重新导入
        import importlib
        if 'cv2' in sys.modules:
            importlib.reload(sys.modules['cv2'])
        else:
            import cv2
        import numpy as np
        _CV2_AVAILABLE = True
        if log_fn:
            log_fn("SmartCrop: OpenCV安装成功，智能裁切已启用")
    except Exception as e:
        if log_fn:
            log_fn("SmartCrop: OpenCV安装失败，使用标准裁切（%s）" % str(e)[:50])

    return _CV2_AVAILABLE


def _detect_faces(frame, conf_threshold=0.5):
    if not _CV2_AVAILABLE:
        return []

    net = _NET if _NET is not None else "haar"
    h, w = frame.shape[:2]

    if net == "haar":
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        if not os.path.exists(cascade_path):
            return []
        cascade = cv2.CascadeClassifier(cascade_path)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=3, minSize=(30, 30))
        return [(x, y, fw, fh, 1.0) for x, y, fw, fh in faces]

    blob = cv2.dnn.blobFromImage(frame, 1.0, (300, 300), (104.0, 177.0, 123.0))
    net.setInput(blob)
    detections = net.forward()
    results = []
    for i in range(detections.shape[2]):
        confidence = detections[0, 0, i, 2]
        if confidence < conf_threshold:
            continue
        box = detections[0, 0, i, 3:7] * np.array([w, h, w, h])
        (x1, y1, x2, y2) = box.astype("int")
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        results.append((x1, y1, x2 - x1, y2 - y1, float(confidence)))
    return results


def prepare_face_detector(app_dir=None, log_fn=None):
    global _NET

    if not _CV2_AVAILABLE:
        # 尝试自动安装
        _try_install_cv2(log_fn=log_fn)
        if not _CV2_AVAILABLE:
            return False

    if app_dir is None:
        app_dir = os.path.dirname(os.path.abspath(__file__))

    detector_dir = os.path.join(app_dir, "face_detector")
    proto_path = os.path.join(detector_dir, "deploy.prototxt")
    model_path = os.path.join(detector_dir, "res10_300x300_ssd_iter_140000.caffemodel")

    if os.path.exists(proto_path) and os.path.exists(model_path):
        try:
            _NET = cv2.dnn.readNetFromCaffe(proto_path, model_path)
            if log_fn:
                log_fn("SmartCrop: DNN人脸检测模型已就绪")
            return True
        except Exception:
            pass

    _NET = "haar"
    if log_fn:
        log_fn("SmartCrop: 使用Haar级联检测")
    return True


def batch_detect_clips(video_path, clips, log_fn=None):
    if not _CV2_AVAILABLE:
        _try_install_cv2(log_fn=log_fn)
        if not _CV2_AVAILABLE:
            if log_fn:
                log_fn("SmartCrop: OpenCV不可用，使用标准裁切")
            return {i: None for i in range(len(clips))}

    results = {}
    prepare_face_detector(log_fn=log_fn)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        if log_fn:
            log_fn("SmartCrop: 无法打开视频，降级为标准裁切")
        return {i: None for i in range(len(clips))}

    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

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

        face_xs = []
        face_ys = []
        for t in sample_times:
            frame_idx = int(t * fps)
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = cap.read()
            if not ret:
                continue
            faces = _detect_faces(frame)
            if faces:
                best = max(faces, key=lambda f: f[2] * f[3])
                face_xs.append((best[0] + best[2] / 2) / frame_w)
                face_ys.append((best[1] + best[3] / 2) / frame_h)

        if face_xs:
            cx = sorted(face_xs)[len(face_xs) // 2]
            cy = sorted(face_ys)[len(face_ys) // 2]
            results[i] = {
                'face_cx_ratio': cx,
                'face_cy_ratio': cy,
                'frame_w': frame_w,
                'frame_h': frame_h,
            }
            smart_count += 1
        else:
            results[i] = None

    cap.release()
    if log_fn:
        log_fn("SmartCrop: %d/%d 片段检测到人物" % (smart_count, len(clips)))
    return results


def compute_smart_crop(person_info, frame_w, frame_h, log_fn=None):
    if person_info is None:
        return _random_crop()

    cx = person_info['face_cx_ratio']
    cy = person_info['face_cy_ratio']

    zoom = 1.0 + random.uniform(0.03, 0.12)
    crop_w = 1.0 / zoom
    crop_h = 1.0 / zoom

    crop_x = cx - crop_w / 2 + random.uniform(-0.03, 0.03)
    crop_x = max(0, min(crop_x, 1.0 - crop_w))

    # Y：底部对齐（绝不裁底部）
    crop_y = 1.0 - crop_h
    face_in_crop = (cy - crop_y) / crop_h if crop_h > 0 else 0.5
    if face_in_crop > 0.7:
        crop_y = cy - crop_h * 0.5
        crop_y = max(0, min(crop_y, 1.0 - crop_h))

    if log_fn:
        log_fn("SmartCrop: zoom=%.2fx" % zoom)

    return {
        'crop_w': crop_w,
        'crop_h': crop_h,
        'crop_x': crop_x,
        'crop_y': crop_y,
        'method': 'smart',
    }


def _random_crop():
    crop_w = random.uniform(0.88, 0.98)
    crop_h = random.uniform(0.88, 0.98)
    crop_x = random.uniform(0, 1.0 - crop_w)
    crop_y = 1.0 - crop_h
    return {
        'crop_w': crop_w,
        'crop_h': crop_h,
        'crop_x': crop_x,
        'crop_y': crop_y,
        'method': 'random',
    }
