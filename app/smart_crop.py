"""
Smart Crop 智能裁切模块 v3
原则：只裁左右+微裁顶部，绝不裁底部（裤子/裙子不能丢）
zoom上限1.15x
"""

import os
import cv2
import numpy as np
import random

_NET = None


def _detect_faces(frame, conf_threshold=0.5):
    net = _NET
    if net is None:
        net = "haar"
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
    return False


def batch_detect_clips(video_path, clips, log_fn=None):
    """
    批量检测人物位置
    
    Returns:
        {clip_index: {'face_cx_ratio', 'face_cy_ratio', 'frame_w', 'frame_h'} or None}
    """
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
                face_xs.append((best[0] + best[2] / 2) / frame_w)  # 归一化
                face_ys.append((best[1] + best[3] / 2) / frame_h)

        if face_xs:
            # 用中位数，比均值更稳
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
    """
    计算裁切参数
    
    v3原则：
    1. 绝不裁底部 → crop底部对齐画面底部（crop_y + crop_h = 1.0）
    2. 只裁左右+微裁顶部
    3. zoom上限1.15x
    """
    if person_info is None:
        return _random_crop()

    cx = person_info['face_cx_ratio']  # 人脸x位置 0-1
    cy = person_info['face_cy_ratio']  # 人脸y位置 0-1

    # zoom: 1.05-1.15x，非常保守
    zoom = 1.0 + random.uniform(0.03, 0.12)
    
    crop_w = 1.0 / zoom
    crop_h = 1.0 / zoom

    # X方向：以人脸为中心，微偏移
    crop_x = cx - crop_w / 2 + random.uniform(-0.03, 0.03)
    crop_x = max(0, min(crop_x, 1.0 - crop_w))

    # Y方向：底部对齐画面底部（绝不裁底部！）
    # crop_y + crop_h = 1.0 → crop_y = 1.0 - crop_h
    crop_y = 1.0 - crop_h  # 底部对齐

    # 但如果人脸太靠上，稍微往下拉一点让脸可见
    # 人脸至少要在裁切框的上70%内
    face_in_crop = (cy - crop_y) / crop_h if crop_h > 0 else 0.5
    if face_in_crop > 0.7:
        # 脸太低了（相对于裁切框），下调框
        crop_y = cy - crop_h * 0.5
        crop_y = max(0, min(crop_y, 1.0 - crop_h))

    if log_fn:
        log_fn("SmartCrop: zoom=%.2fx, 人脸位置=(%.0f%%,%.0f%%)" % (zoom, cx * 100, cy * 100))

    return {
        'crop_w': crop_w,
        'crop_h': crop_h,
        'crop_x': crop_x,
        'crop_y': crop_y,
        'method': 'smart',
    }


def _random_crop():
    """降级：几乎不裁"""
    crop_w = random.uniform(0.88, 0.98)
    crop_h = random.uniform(0.88, 0.98)
    crop_x = random.uniform(0, 1.0 - crop_w)
    crop_y = 1.0 - crop_h  # 底部对齐
    return {
        'crop_w': crop_w,
        'crop_h': crop_h,
        'crop_x': crop_x,
        'crop_y': crop_y,
        'method': 'random',
    }
