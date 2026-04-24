"""
剪辑核心逻辑 v3.0（GUI 和 CLI 共用）
- 自动语音识别（无 SRT 时自动生成）
- 字幕叠加
- 文案逻辑优化（每类最多2个、模糊匹配、语气词过滤）
"""

import os
import sys
import re
import time
import shutil

# 多版本缓存：process_video 写入，process_video_multi 读取
_multi_result_cache = {}
import subprocess
import json
import glob
import random
import math
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from srt_parser import open_srt, _time_to_seconds
from config import (
    CLIP_KEYWORDS, CLIP_ORDER, VIDEO_CONFIG, FFMPEG_PATH,
    DEDUP_CONFIG, DEDUP_PRESET, SUBTITLE_OVERLAY,
    FILLER_WORDS, CLIP_DURATION_RANGE,
    NEGATIVE_SIGNALS, NEGATION_WORDS, TEXT_OPTIMIZATION,
    TARGET_DURATION, TARGET_DURATION_TOLERANCE, REQUIRED_CLIP_TYPES,
    TIME_WINDOW_MINUTES,
)



_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
def get_ffmpeg_cmd():
    from platform_config import FFMPEG_CMD
    if os.path.exists(FFMPEG_CMD):
        return FFMPEG_CMD
    # 回退：打包目录中查找
    import sys
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    for name in ["ffmpeg", "ffmpeg.exe"]:
        p = os.path.join(base, "ffmpeg", name)
        if os.path.exists(p):
            return p
    return "ffmpeg"


# ============================================================
# 去重逻辑（保持不变）
# ============================================================

def rand_range(min_val, max_val, decimals=2):
    return round(random.uniform(min_val, max_val), decimals)


def apply_preset(preset):
    """应用去重预设。返回 (方法数量, 配置字典)"""
    config = dict(DEDUP_CONFIG)
    methods = dict(config.get("methods", {}))
    strategy = config.get("strategy", "classic")

    if preset == "none":
        for m in methods: methods[m]["enabled"] = False
        return 0, methods, strategy
    elif preset == "light":
        # 轻度：只镜像 + 轻微变速
        for m in methods: methods[m]["enabled"] = False
        return 0, methods, strategy
    elif preset == "medium":
        return 0, methods, strategy
    elif preset == "heavy":
        return 0, methods, strategy
    else:
        return 0, methods, strategy


def _generate_random_dedup_params(clip_index):
    """
    生成随机去重参数（每段唯一）
    返回: dict with speed, crop_w, crop_h, crop_x, crop_y, audio_pitch
    """
    cfg = DEDUP_CONFIG
    rng = random.Random(clip_index * 7919 + random.randint(0, 99999))

    params = {}

    # 1. 加权随机变速
    if cfg.get("variable_speed", {}).get("enabled"):
        sc = cfg["variable_speed"]
        if rng.random() <= sc["weight_low"]:
            speed = round(rng.uniform(sc["min_rate"], 1.20), sc["decimal_precision"])
        else:
            speed = round(rng.uniform(1.20, sc["max_rate"]), sc["decimal_precision"])
        speed = min(speed, sc["max_rate"])
        params["speed"] = speed
    else:
        params["speed"] = 1.0

    # 2. 随机微裁剪
    if cfg.get("random_crop", {}).get("enabled"):
        rc = cfg["random_crop"]
        params["crop_w"] = round(rng.uniform(rc["crop_min"], rc["crop_max"]), 3)
        params["crop_h"] = round(rng.uniform(rc["crop_min"], rc["crop_max"]), 3)
        params["crop_x"] = round(rng.uniform(rc["offset_min"], rc["offset_max"]), 3)
        params["crop_y"] = round(rng.uniform(rc["offset_min"], rc["offset_max"]), 3)
    else:
        params["crop_w"] = 1.0
        params["crop_h"] = 1.0
        params["crop_x"] = 0.0
        params["crop_y"] = 0.0

    # 3. 音频微pitch
    if cfg.get("audio_pitch", {}).get("enabled"):
        ap = cfg["audio_pitch"]
        params["audio_pitch"] = round(rng.uniform(ap["min_pitch"], ap["max_pitch"]), 2)
    else:
        params["audio_pitch"] = 0.0

    # 4. 伽马微调
    if cfg.get("gamma_shift", {}).get("enabled"):
        g = cfg["gamma_shift"]["range"]
        params["gamma"] = round(rng.uniform(g[0], g[1]), 3)
    else:
        params["gamma"] = 0.0

    # 5. 新增方法开关
    params["corner_mask"] = cfg.get("corner_mask", {}).get("enabled", False)
    params["audio_reverb"] = cfg.get("audio_reverb", {}).get("enabled", False) and rng.random() < cfg.get("audio_reverb", {}).get("probability", 0.5)
    params["noise_fusion"] = cfg.get("noise_fusion", {}).get("enabled", False) and rng.random() < cfg.get("noise_fusion", {}).get("probability", 0.4)
    params["frame_interp"] = cfg.get("frame_interpolation", {}).get("enabled", False) and rng.random() < cfg.get("frame_interpolation", {}).get("probability", 0.3)

    return params


def build_dedup_filters(width, height, clip_index=0):
    """
    构建去重滤镜链
    - enhanced模式: 镜像 + 随机变速 + 随机微裁剪（pitch已移除）
    - classic模式: 原有随机方法（兼容）
    """
    _, methods, strategy = apply_preset("custom")

    if strategy == "enhanced":
        return _build_enhanced_dedup(width, height, clip_index)
    else:
        return _build_classic_dedup(width, height, clip_index, methods)


def _build_enhanced_dedup(width, height, clip_index):
    """增强版去重：镜像 + 随机变速 + 随机微裁剪（pitch已移除，修复音画不同步）"""
    cfg = DEDUP_CONFIG
    params = _generate_random_dedup_params(clip_index)

    vf_list = []
    af_list = []
    applied = []

    # 1. 水平镜像（80%概率开启，增加随机性）
    if cfg.get("mirror", {}).get("enabled") and random.random() < 0.8:
        vf_list.append("hflip")
        applied.append("mirror")

    # 2. 随机微裁剪（先裁再缩放，保证输出分辨率不变）
    cw, ch = params["crop_w"], params["crop_h"]
    cx, cy = params["crop_x"], params["crop_y"]
    if cw < 1.0 or ch < 1.0:
        crop_w = int(width * cw) + (int(width * cw) % 2)
        crop_h = int(height * ch) + (int(height * ch) % 2)
        crop_x = int(width * cx)
        crop_y = int(height * cy)
        # 确保裁剪区域不超出画面
        if crop_x + crop_w > width: crop_x = width - crop_w
        if crop_y + crop_h > height: crop_y = height - crop_h
        vf_list.append(f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y}")
        vf_list.append(f"scale={width}:{height}")
        applied.append(f"crop({cw:.3f}x{ch:.3f})")

    # [v9.1] 变速：setpts和atempo使用同一个speed值，确保音画绝对同步
    # pitch已移除——之前视频用speed变速、音频用speed*pitch变速，速率不一致是音画不同步的根因
    speed = params["speed"]

    if speed != 1.0:
        vf_list.append(f"setpts=PTS/{speed}")   # 视频变速
        af_list.append(f"atempo={speed}")         # 音频变速（同值，保证同步）
        applied.append(f"speed({speed}x)")

    # 5. 伽马微调（等级1，肉眼不可见）
    gamma = params.get("gamma", 0.0)
    if gamma != 0.0:
        vf_list.append(f"eq=gamma={1.0 + gamma:.3f}")
        applied.append(f"gamma({gamma:+.3f})")

    # 6. 四角微遮罩（等级2）
    if params.get("corner_mask"):
        s = cfg.get("corner_mask", {})
        pct = s.get("size_pct", 0.005)
        clr = s.get("color", "0x000000")
        bw = max(int(width * pct), 2)
        bh = max(int(height * pct), 2)
        # 四个角各画一个小矩形
        corners = [
            f"drawbox=x=0:y=0:w={bw}:h={bh}:c={clr}:t=fill",
            f"drawbox=x=iw-{bw}:y=0:w={bw}:h={bh}:c={clr}:t=fill",
            f"drawbox=x=0:y=ih-{bh}:w={bw}:h={bh}:c={clr}:t=fill",
            f"drawbox=x=iw-{bw}:y=ih-{bh}:w={bw}:h={bh}:c={clr}:t=fill",
        ]
        vf_list.extend(corners)
        applied.append("corner_mask")

    # 7. 音频极轻微混响（等级2）
    if params.get("audio_reverb"):
        af_list.append("aecho=0.8:0.88:60:0.4")
        applied.append("reverb")

    # 8. 双音轨融合 - 原音+极轻白噪音（等级3）
    if params.get("noise_fusion"):
        nv = cfg.get("noise_fusion", {}).get("noise_volume", 0.001)
        af_list.append(f"aevalsrc=-{nv}*random(0):c=stereo:s=44100")
        af_list.append("amix=inputs=2:duration=first:dropout_transition=0")
        applied.append("noise_fusion")

    # 9. 单帧插值（等级3，默认关闭）
    if params.get("frame_interp"):
        vf_list.append("minterpolate=mi_mode=blend:fps=30.5")
        applied.append("frame_interp")

    # 日志输出参数
    _log_dedup_params(params)

    return {
        "video_filters": ",".join(vf_list),
        "audio_filters": ",".join(af_list),
        "applied": applied,
    }


def _build_classic_dedup(width, height, clip_index, methods):
    """经典去重模式（兼容原有逻辑）"""
    enabled_methods = [name for name, c in methods.items() if c.get("enabled")]
    if not enabled_methods:
        return {"video_filters": "", "audio_filters": "", "applied": []}

    count = min(2, len(enabled_methods))
    chosen = random.sample(enabled_methods, count)
    vf_list, af_list = [], []
    rng = random.Random(clip_index * 1000 + random.randint(0, 9999))

    if "speed_change" in chosen:
        s = round(rng.uniform(methods["speed_change"]["min_speed"], methods["speed_change"]["max_speed"]), 3)
        vf_list.append(f"setpts=PTS/{s}"); af_list.append(f"atempo={s}")
    if "zoom_crop" in chosen:
        sc = round(rng.uniform(methods["zoom_crop"]["min_scale"], methods["zoom_crop"]["max_scale"]), 3)
        nw = int(width*sc) + int(width*sc)%2; nh = int(height*sc) + int(height*sc)%2
        vf_list.append(f"scale={nw}:{nh}"); vf_list.append(f"crop={width}:{height}")
    if "mirror" in chosen: vf_list.append("hflip")

    return {"video_filters": ",".join(vf_list), "audio_filters": ",".join(af_list), "applied": chosen}


def _log_dedup_params(params):
    """输出本次去重参数（供追溯）"""
    import sys
    _log = lambda msg: None  # 会在 process_video 里通过 _log 函数使用
    # 这里用 print 输出到标准输出，process_video 的 _log 会捕获
    pass


# ============================================================
# v3.0: 文案逻辑优化
# ============================================================

def _clean_text(text):
    """清理语气词和冗余 + Whisper 识别纠错"""
    t = text
    for w in FILLER_WORDS:
        t = t.replace(w, "")
    # 去掉多余空格
    t = re.sub(r"\s+", "", t).strip()
    # 去掉纯标点
    t = re.sub(r"^[，。！？、\s]+", "", t)
    t = re.sub(r"[，。！？、\s]+$", "", t)

    # Whisper 常见误识别修复（服装直播场景）
    whisper_fixes = {
        "30米": "30元", "100单": "100单", "1,000单": "1000单",
        "米的优惠券": "元的优惠券", "米的券": "元的券",
        "到手架隔": "到手价", "到手只要1": "到手只要",
        "给到我们到手": "到手",
        "还给你们": "",
        "是的": "",
        "对首批": "首批",
        "应该是7": "应该是7天",
    }
    for wrong, right in whisper_fixes.items():
        t = t.replace(wrong, right)

    t = re.sub(r"\s+", "", t).strip()
    return t


def _score_block(block):
    """
    语义块综合评分（0-100）
    - 关键词命中 (0-30)
    - 文案长度适中 (0-20): 5-25字最佳
    - 时长适中 (0-20): 3-8秒最佳
    - 无问号/纯疑问 (0-15)
    - 含数字/价格 (0-15): 带货文案加分
    """
    score = 0
    text = block["text"]
    dur = block["duration"]

    # 关键词命中
    score += min(block["kw_count"] * 8, 30)

    # 文案长度
    text_len = len(text)
    if 10 <= text_len <= 35:
        score += 20
    elif 5 <= text_len <= 40:
        score += 12
    elif 3 <= text_len <= 45:
        score += 6

    # 时长适中
    if 3 <= dur <= 8:
        score += 20
    elif 2 <= dur <= 10:
        score += 10
    elif 1.5 <= dur <= 12:
        score += 4

    # 无问号加分（问号多的片段通常不适合做短视频文案）
    question_marks = text.count("？") + text.count("?")
    if question_marks == 0:
        score += 15
    elif question_marks == 1:
        score += 5

    # 含数字/价格加分（"119""199""30元"等具体数字更有说服力）
    has_numbers = bool(re.search(r"\d+", text))
    if has_numbers:
        score += 15

    return min(score, 100)


def _match_keyword(text, keywords):
    """模糊匹配：去掉标点后匹配"""
    clean = re.sub(r"[，。！？、\s,.\-!?]", "", text)
    for key in keywords:
        if re.sub(r"[，。！？、\s,.\-!?]", "", key) in clean:
            return True
    return False


def parse_srt_clips(srt_path, log_fn=None):
    """
    智能片段提取 v5.0 - 基于 AYOBE/小贤实际爆款视频分析
    - 6个核心类型（hook/selling_point/price/size/urgency/cta）
    - 时间窗口聚类：优先在5分钟窗口内选片段，不跳来跳去
    - selling_point允许重复（实际话术反复讲卖点）
    - Whisper纠错 + 语义块合并
    """
    def _log(msg):
        if log_fn: log_fn(msg)

    _log(f"解析字幕: {os.path.basename(srt_path)}")
    try:
        subs, encoding = open_srt(srt_path)
        _log(f"字幕编码: {encoding}，共 {len(subs)} 条")
    except Exception as e:
        _log(f"解析 SRT 失败: {e}")
        return []

    _log("第一步：逐句分类，合并语义块...")
    sentences = []
    for sub in subs:
        text = sub.text.strip()
        if not text: continue
        start = _time_to_seconds(sub.start)
        end = _time_to_seconds(sub.end)
        duration = end - start
        if duration < 0.5 or duration > 15: continue
        if any(neg in text for neg in NEGATIVE_SIGNALS): continue

        best_type, best_kw = None, 0
        for ct, cfg in CLIP_KEYWORDS.items():
            kw = sum(1 for k in cfg["keywords"] if k in text)
            if kw > best_kw: best_kw, best_type = kw, ct
        if not best_type or best_kw == 0: continue

        positive = False
        for kw in CLIP_KEYWORDS[best_type]["keywords"]:
            if kw in text:
                idx = text.find(kw)
                if text[max(0, idx-1):idx] not in NEGATION_WORDS:
                    positive = True; break
        if not positive: continue
        sentences.append((best_type, text, start, end, best_kw))

    if not sentences:
        _log("未找到有效句子！"); return []

    # 语义块合并
    blocks, cur = [], None
    for st, txt, ss, se, kw in sentences:
        if cur:
            gap, md = ss - cur["end"], se - cur["start"]
            if cur["type"] == st and gap < 8 and md <= 20:
                cur["text"] += " " + txt; cur["end"] = se
                cur["kw_count"] += kw; continue
            blocks.append(cur)
        cur = {"type": st, "text": txt, "start": ss, "end": se, "kw_count": kw}
    if cur: blocks.append(cur)

    for b in blocks:
        b["text"] = _clean_text(b["text"])
        if not b["text"]: continue
        if b["text"][-1] not in "!?!?.": b["text"] += "！"
        b["duration"] = b["end"] - b["start"]
        b["score"] = _score_block(b)

    fb = [b for b in blocks if b["text"] and 2 <= b["duration"] <= 20 and 5 <= len(b["text"]) <= 45]
    if not fb:
        _log("无有效片段！"); return []
    _log(f"  有效语义块: {len(fb)} 个")

    # 时间窗口聚类：找最密集的5分钟窗口
    _log("第二步：时间窗口聚类...")
    ws = TIME_WINDOW_MINUTES * 60
    mt = min(b["start"] for b in fb)
    xt = max(b["start"] for b in fb)
    best_t, best_s = mt, 0
    t = mt
    while t <= xt:
        we = t + ws
        wb = [b for b in fb if t <= b["start"] <= we]
        types = set(b["type"] for b in wb)
        score = len(types) * 20 + sum(b["score"] for b in wb)
        for rt in REQUIRED_CLIP_TYPES:
            if rt in types: score += 50
        if score > best_s: best_s, best_t = score, t
        t += 30

    we = best_t + ws
    wb = [b for b in fb if best_t <= b["start"] <= we]
    if len(wb) < 5:
        wb = fb
        _log("  窗口片段不足，使用全部")
    else:
        _log(f"  最佳窗口: {int(best_t//60)}分{int(best_t%60):02d}秒起，{len(wb)}个片段")

    # 分组评分
    _log("第三步：黄金链路编排...")
    tp = {}
    for b in wb:
        tp.setdefault(b["type"], []).append(b)
    for bt in tp:
        tp[bt].sort(key=lambda x: -x["score"])
        tp[bt] = tp[bt][:1]  # 每类型仅1个

    tgt = TARGET_DURATION
    mn = tgt - TARGET_DURATION_TOLERANCE
    mx = tgt + TARGET_DURATION_TOLERANCE
    oc, td, ut = [], 0.0, {}

    # 第一轮：各类型取1个
    for ct in CLIP_ORDER:
        if td >= tgt: break
        if ct in tp and tp[ct]:
            b = tp[ct].pop(0); oc.append(b)
            ut[ct] = ut.get(ct, 0) + 1; td += b["duration"]

    # 第二轮：补充 selling_point（允许重复到3个）
    if "selling_point" in tp:
        for b in list(tp["selling_point"]):
            if td >= mn or ut.get("selling_point", 0) >= 5: break
            oc.append(b); ut["selling_point"] = ut.get("selling_point", 0) + 1
            td += b["duration"]

    # 第三轮：时长不足，从其他类型补充
    if td < mn:
        rem = sorted([b for bs in tp.values() for b in bs], key=lambda x: -x["score"])
        for b in rem:
            if td >= tgt: break
            if oc and oc[-1]["type"] == b["type"]: continue
            oc.append(b); td += b["duration"]

    # 时长过长裁剪
    while td > mx and len(oc) > 3:
        wi, ws2 = None, 999
        for i, b in enumerate(oc):
            if b["type"] not in REQUIRED_CLIP_TYPES and b["score"] < ws2:
                ws2, wi = b["score"], i
        if wi is None: break
        td -= oc.pop(wi)["duration"]

    # 输出
    fc = [(b["type"], b["text"], b["start"], b["end"], b["score"], b["duration"]) for b in oc]
    _log(f"{'='*65}")
    _log(f"最终片段（{len(fc)} 个，总时长 {sum(d for _,_,_,_,_,d in fc):.1f}s）")
    _log(f"{'='*65}")
    for i, (ct, txt, s, e, sc, d) in enumerate(fc):
        _log(f"  [{i+1:02d}] {ct:<14s} | {s:7.2f}s-{e:7.2f}s ({d:.1f}s) | {sc:3.0f}分 | {txt}")
    _log("-" * 65)
    return fc

# ============================================================
# v3.0: 字幕叠加 - 生成 ASS 文件
# ============================================================


def _split_subtitle_text(text, max_chars=12):
    """将长文案拆分为短句，按标点和语义停顿点分割"""
    import re
    # 按标点分割
    parts = re.split(r'([，。！？、；：,])', text)
    # 重新组合：标点跟前面的文字
    segments = []
    current = ""
    for p in parts:
        if re.match(r'^[，。！？、；：,]$', p):
            current += p
            if current.strip():
                segments.append(current.strip())
                current = ""
        else:
            current += p
    if current.strip():
        segments.append(current.strip())

    # 如果某个 segment 还是太长，按 max_chars 强制拆
    result = []
    for seg in segments:
        if len(seg) <= max_chars:
            result.append(seg)
        else:
            # 尝试在词边界拆，避免截断词语
            i = 0
            while i < len(seg):
                end = min(i + max_chars, len(seg))
                # 如果不是切到末尾，尝试微调到词边界
                if end < len(seg):
                    # 向前找助词/连词位置
                    adjusted = end
                    for offset in range(0, 3):
                        pos = end - offset
                        if pos <= i:
                            break
                        if seg[pos-1] in '的了着过是在也都还很最把被让给和与但而':
                            adjusted = pos
                            break
                    end = adjusted
                chunk = seg[i:end]
                if chunk:
                    result.append(chunk)
                i = end
    return result


def _highlight_text(text, keywords, sc):
    """对文字中的关键词进行高亮处理（黄色+放大）"""
    kw_size = sc.get("keyword_font_size", sc["font_size"] + 4)
    kw_color = sc.get("keyword_font_color", "&H0000FFFF")
    kw_bold = "-1" if sc.get("keyword_bold") else "0"
    base_color = sc["font_color"]
    base_bold = "-1" if sc.get("bold", True) else "0"

    # 构建正则：按关键词长度降序匹配（优先匹配长词）
    import re
    sorted_kw = sorted(keywords, key=len, reverse=True)
    pattern = "|".join(re.escape(k) for k in sorted_kw)
    if not pattern:
        return text

    result = []
    last_end = 0
    for m in re.finditer(pattern, text):
        # 前面的普通文字
        if m.start() > last_end:
            normal = text[last_end:m.start()]
            result.append(f"{{\\c&H{base_color[2:]}&\\b{base_bold}\\fs{sc['font_size']}}}{normal}")
        # 关键词
        kw_text = m.group()
        result.append(f"{{\\c&H{kw_color[2:]}&\\b{kw_bold}\\fs{kw_size}}}{kw_text}")
        last_end = m.end()

    # 剩余文字
    if last_end < len(text):
        normal = text[last_end:]
        result.append(f"{{\\c&H{base_color[2:]}&\\b{base_bold}\\fs{sc['font_size']}}}{normal}")

    return "".join(result)


def generate_ass(clips, width, height, output_path):
    """为片段生成 ASS 字幕文件（支持关键词高亮）"""
    from config import SUBTITLE_KEYWORDS
    from platform_config import FONT_BOLD_NAME
    sc = dict(SUBTITLE_OVERLAY)  # 复制避免修改原配置
    sc["font_name"] = FONT_BOLD_NAME
    margin_v = sc["margin_v"]
    outline_w = sc.get("outline_width", 3)
    # 底部对齐：用 PlayResY - margin_v
    if sc["position"] == "top":
        margin_v = sc["margin_v"]
        alignment = 8  # top center
    elif sc["position"] == "center":
        margin_v = 0
        alignment = 5  # center
    else:
        alignment = 2  # bottom center

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{sc['font_name']},{sc['font_size']},{sc['font_color']},&H000000FF,{sc['outline_color']},&H80000000,-1,0,0,0,100,100,0,0,1,{outline_w},1,{alignment},10,10,{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    current_time = 0.0
    for c_type, text, start, end, score, dur, *_ in clips:
        duration = end - start
        # 去除AI标注的【】标记，提取重点词
        ai_keywords = re.findall(r'【(.*?)】', text)
        clean_text = re.sub(r'【|】', '', text)
        # 拆分为短句
        segments = _split_subtitle_text(clean_text, max_chars=12)
        if not segments:
            segments = [clean_text]
        # 合并关键词列表（AI标注 + 静态配置）
        all_keywords = list(set(ai_keywords + SUBTITLE_KEYWORDS))
        # 按短句数分配时间
        seg_dur = duration / len(segments)
        for i, seg in enumerate(segments):
            seg_start = current_time + i * seg_dur
            seg_end = current_time + (i + 1) * seg_dur
            ass_s = _sec_to_ass_time(seg_start)
            ass_e = _sec_to_ass_time(seg_end)
            highlighted = _highlight_text(seg, all_keywords, sc)
            lines.append(f"Dialogue: 0,{ass_s},{ass_e},Default,,0,0,0,,{highlighted}")
        current_time += duration

    with open(output_path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines))


def _sec_to_ass_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _get_clip_duration(filepath):
    """读取 MP4 文件的精确时长（解析 moov/mvhd 原子，无需 ffprobe）"""
    try:
        fsize = os.path.getsize(filepath)
        with open(filepath, 'rb') as f:
            head = f.read(min(fsize, 1 * 1024 * 1024))
            idx = head.find(b'moov')
            if idx < 0:
                f.seek(max(0, fsize - 1 * 1024 * 1024))
                tail = f.read()
                idx = tail.find(b'moov')
                if idx < 0:
                    return None
                head = tail
            search_end = min(idx + 100000, len(head))
            mvhd_idx = head.find(b'mvhd', idx, search_end)
            if mvhd_idx < 0:
                return None
            version = head[mvhd_idx + 8] if mvhd_idx + 8 < len(head) else 0
            if version == 0:
                if mvhd_idx + 28 > len(head):
                    return None
                timescale = int.from_bytes(head[mvhd_idx+20:mvhd_idx+24], 'big')
                duration = int.from_bytes(head[mvhd_idx+24:mvhd_idx+28], 'big')
            else:
                if mvhd_idx + 40 > len(head):
                    return None
                timescale = int.from_bytes(head[mvhd_idx+28:mvhd_idx+32], 'big')
                duration = int.from_bytes(head[mvhd_idx+32:mvhd_idx+40], 'big')
            if timescale <= 0:
                return None
            return duration / timescale
    except Exception:
        return None




# ============================================================
# v3.0: 核心流程
# ============================================================



def _build_cut_report(ordered_clips, success_count, total_clips, output_path, size_mb):
    """构建切割评分报告"""
    import os
    report = {
        "ok": True,
        "clips_count": success_count,
        "clips_total": total_clips,
        "size_mb": round(size_mb, 1),
        "duration": 0.0,
        "has_hook": False,
        "hook_type": "",
        "category": "",
        "score": 0,
        "warnings": [],
    }
    if not ordered_clips:
        report["warnings"].append("没有选中任何片段")
        report["score"] = 0
        return report

    # 计算总时长
    total_dur = 0.0
    types_seen = []
    hook_found = None
    for clip in ordered_clips:
        if isinstance(clip, (list, tuple)) and len(clip) >= 6:
            c_type, text, start, end, score, dur = clip[0], clip[1], clip[2], clip[3], clip[4], clip[5]  # clip[6]=focus ignored here
        elif isinstance(clip, dict):
            c_type = clip.get("type", "")
            start = clip.get("start", 0)
            end = clip.get("end", 0)
            dur = end - start
        else:
            continue
        total_dur += dur
        if c_type not in types_seen:
            types_seen.append(c_type)
        if hook_found is None and "hook" in c_type.lower():
            hook_found = c_type

    report["duration"] = round(total_dur, 1)
    if hook_found:
        report["has_hook"] = True
        report["hook_type"] = hook_found

    # 品类（从片段类型中推断）
    cat_types = [t for t in types_seen if t not in ("hook", "bridge", "close", "cta", "transition")]
    if cat_types:
        report["category"] = cat_types[0]

    # ---- 评分 ----
    score = 0

    # 时长分 (0-30)
    if 50 <= total_dur <= 65:
        score += 30
    elif 40 <= total_dur <= 75:
        score += 22
    elif 30 <= total_dur <= 90:
        score += 15
        if total_dur < 50:
            report["warnings"].append(f"时长偏短({total_dur:.0f}s，建议50s+)")
        else:
            report["warnings"].append(f"时长偏长({total_dur:.0f}s，建议60s以内)")
    else:
        score += 5
        report["warnings"].append(f"时长异常({total_dur:.0f}s)")

    # Hook分 (0-25)
    if report["has_hook"]:
        score += 25
    else:
        score += 5
        report["warnings"].append("缺少Hook开头，建议保留吸引眼球的片段")

    # 片段数分 (0-20)
    if 5 <= success_count <= 8:
        score += 20
    elif 3 <= success_count <= 10:
        score += 14
    elif success_count >= 2:
        score += 8
    else:
        score += 2
        report["warnings"].append("片段太少，成品信息密度不足")

    # 类型多样性 (0-15)
    type_count = len(types_seen)
    if type_count >= 4:
        score += 15
    elif type_count >= 3:
        score += 11
    elif type_count >= 2:
        score += 7
    else:
        score += 3
        report["warnings"].append("片段类型单一，建议混搭不同类型")

    # 有收尾 (0-10)
    close_types = [t for t in types_seen if t in ("close", "cta", "urgency")]
    if close_types:
        score += 10
    else:
        score += 3
        report["warnings"].append("缺少收尾(价格/尺码/号召)，建议加上促转化的片段")

    report["score"] = min(score, 100)
    return report


def _print_cut_report(report, _log):
    """在日志中打印切割评分报告"""
    bar_len = 20
    score = report["score"]
    filled = int(bar_len * score / 100)
    bar = "█" * filled + "░" * (bar_len - filled)

    # 评分等级
    if score >= 85:
        grade = "优秀"
        grade_icon = "🌟"
    elif score >= 70:
        grade = "良好"
        grade_icon = "👍"
    elif score >= 55:
        grade = "一般"
        grade_icon = "⚡"
    else:
        grade = "需改进"
        grade_icon = "🔧"

    _log("")
    _log("━━━ 切割报告 ━━━━━━━━━━━━━━━━━━")
    _log(f"  {grade_icon} 综合评分: {score}/100 {grade}")
    _log(f"  [{bar}]")
    _log(f"  ⏱ 总时长: {report['duration']:.0f}s | 🎬 片段: {report['clips_count']}/{report['clips_total']}段")
    hook_str = f"{report['hook_type']} ✅" if report['has_hook'] else "无 ❌"
    _log(f"  🪝 Hook: {hook_str}")
    if report['category']:
        _log(f"  🏷 品类: {report['category']}")
    for w in report['warnings']:
        _log(f"  ⚠️ {w}")
    _log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    _log("")
def process_video(video_path, srt_path=None, output_path=None,
                   dedup_preset="medium", subtitle_overlay=True,
                   log_fn=None, force_category=None, cancel_event=None,
                   pip_path=None, pip_size=0.15, pip_opacity=0.03, pip_pos="右下",
                   _clips_only=False, _asr_only=False, focus_hint="自动", smart_crop_enabled=True, crop_level="medium", ken_burns_enabled=True):
    """
    完整处理流程：
    1. 如果没有 SRT，自动语音识别
    2. 解析字幕提取片段
    3. 切割 + 去重
    4. 字幕叠加 + 拼接
    返回 True/False
    """
    def _log(msg):
        if log_fn: log_fn(msg)

    def _cancelled():
        return cancel_event and cancel_event.is_set()

    # ---- 运行日志 ----
    import time as _time, json as _json
    _run_log = {
        "时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "视频": video_path,
        "结果": "进行中",
        "耗时": None,
        "参数": {
            "去重": dedup_preset,
            "字幕叠加": subtitle_overlay,
            "指定SRT": srt_path or "自动识别",
            "画中画": pip_path or "无",
            "主推品类": force_category or "自动",
        },
        "选片": {},
        "输出": None,
        "错误": None,
    }
    _run_start = _time.time()
    _run_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

    def _save_run_log():
        """写入运行日志 JSON"""
        try:
            _run_log["耗时"] = f"{_time.time() - _run_start:.1f}s"
            os.makedirs(_run_log_dir, exist_ok=True)
            vname = os.path.splitext(os.path.basename(video_path))[0][:20]
            ts = _time.strftime("%Y%m%d_%H%M%S", _time.localtime(_run_start))
            status = "成功" if _run_log["结果"] == "成功" else "失败"
            fname = f"{ts}_{vname}_{status}.json"
            with open(os.path.join(_run_log_dir, fname), "w", encoding="utf-8") as f:
                _json.dump(_run_log, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    auto_srt = False
    temp_srt = None

    # 1. 自动语音识别（如果没给 SRT）
    if _cancelled():
        _log("已取消。"); return {"ok": False, "error": "cancelled"}

    if not srt_path:
        # 检查云端ASR是否启用
        _volc_asr_on = False
        _volc_used = False
        try:
            import json as _j3, os as _o3, sys as _s3
            if getattr(_s3, "frozen", False):
                _sd3 = _o3.path.dirname(_s3.executable)
            else:
                _sd3 = _o3.path.dirname(_o3.path.abspath(__file__))
            _sp3 = _o3.path.join(_sd3, "ai_settings.json")
            if _o3.path.exists(_sp3):
                with open(_sp3, "r", encoding="utf-8-sig") as _f3:
                    _volc_asr_on = _j3.load(_f3).get("asr_enabled", False)
        except Exception:
            pass
        # 记录AI模型到运行日志
        try:
            import json as _jl, os as _ol, sys as _sl
            _sdl = _ol.path.dirname(_sl.executable) if getattr(_sl, "frozen", False) else _ol.path.dirname(_ol.path.abspath(__file__))
            _spl = _ol.path.join(_sdl, "ai_settings.json")
            if _ol.path.exists(_spl):
                with open(_spl, "r", encoding="utf-8-sig") as _fl:
                    _s = _jl.load(_fl)
                    _run_log["参数"]["AI模型"] = _s.get("model", "deepseek-chat")
                    _run_log["参数"]["云端ASR"] = _s.get("asr_enabled", False)
                    _run_log["参数"]["ASR预设"] = _s.get("asr_preset", "自定义")
        except Exception:
            pass
        if _volc_asr_on:
            # 使用火山引擎 ASR（断句精准），失败则降级到本地 Whisper
            _volc_used = False
            try:
                import json as _json2, os as _os2, sys as _sys2
                if getattr(_sys2, "frozen", False):
                    _sd2 = _os2.path.dirname(_sys2.executable)
                else:
                    _sd2 = _os2.path.dirname(_os2.path.abspath(__file__))
                _sp2 = _os2.path.join(_sd2, "ai_settings.json")
                if _os2.path.exists(_sp2):
                    with open(_sp2, "r", encoding="utf-8-sig") as f2:
                        _cfg2 = _json2.load(f2)
                    # --- 阿里云 ASR ---
                    _asr_preset = _cfg2.get("asr_preset", "") or _cfg2.get("asr_provider", "")
                    if _asr_preset == "阿里云" and not _volc_used:
                        _ali_api_key = _cfg2.get("aliyun_api_key", "")
                        _ali_oss_ak = _cfg2.get("aliyun_oss_ak", "")
                        _ali_oss_sk = _cfg2.get("aliyun_oss_sk", "")
                        _ali_bucket = _cfg2.get("aliyun_bucket", "")
                        _ali_endpoint = _cfg2.get("aliyun_endpoint", "oss-cn-beijing.aliyuncs.com")
                        _ali_model = _cfg2.get("asr_model", "paraformer-v2") or "paraformer-v2"
                        if _ali_api_key and _ali_oss_ak and _ali_oss_sk and _ali_bucket:
                            _log("启动阿里云语音识别...")
                            try:
                                from aliyun_asr import aliyun_asr
                                import tempfile as _tf_ali, hashlib as _hl_ali
                                _td_ali = _os2.path.join(_tf_ali.gettempdir(), "live_cutter_stt")
                                _os2.makedirs(_td_ali, exist_ok=True)
                                _vh_ali = _hl_ali.md5(video_path.encode("utf-8")).hexdigest()[:8]
                                _wav_ali = _os2.path.join(_td_ali, f"audio_{_vh_ali}.wav")
                                _srt_ali = _os2.path.join(_td_ali, f"sub_{_vh_ali}.srt")
                                _ff_ali = get_ffmpeg_cmd()
                                _ext_ali = [_ff_ali, "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", _wav_ali]
                                _pk_ali = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                _p_ali = subprocess.Popen(_ext_ali, **_pk_ali, creationflags=_NO_WINDOW)
                                _p_ali.wait(timeout=120)
                                if _p_ali.returncode == 0 and _os2.path.exists(_wav_ali):
                                    _segs_ali = aliyun_asr(_wav_ali, app_key=_ali_api_key, model=_ali_model,
                                                           oss_ak=_ali_oss_ak, oss_sk=_ali_oss_sk,
                                                           oss_bucket=_ali_bucket, oss_endpoint=_ali_endpoint,
                                                           log_fn=_log)
                                    if _segs_ali:
                                        _srt_ali_lines = []
                                        for _i_ali, _seg_ali in enumerate(_segs_ali, 1):
                                            _st_ali = float(_seg_ali.get("start", 0))
                                            _et_ali = float(_seg_ali.get("end", _st_ali + 3))
                                            _txt_ali = _seg_ali.get("text", "").strip()
                                            for _ch in "，。！？、；：“”‘’（）《》【】…—·,.!?:;'\"()[]{}<>":
                                                _txt_ali = _txt_ali.replace(_ch, "")
                                            _txt_ali = _txt_ali.strip()
                                            _txt_ali = _txt_ali.strip()
                                            if not _txt_ali:
                                                continue
                                            _srt_ali_lines.append(str(_i_ali))
                                            _srt_ali_lines.append(
                                                f"{int(_st_ali//3600):02d}:{int((_st_ali%3600)//60):02d}:{int(_st_ali%60):02d},{int((_st_ali%1)*1000):03d}"
                                                f" --> "
                                                f"{int(_et_ali//3600):02d}:{int((_et_ali%3600)//60):02d}:{int(_et_ali%60):02d},{int((_et_ali%1)*1000):03d}"
                                            )
                                            _srt_ali_lines.append(_txt_ali)
                                            _srt_ali_lines.append("")
                                        with open(_srt_ali, "w", encoding="utf-8") as _f_ali:
                                            _f_ali.write(chr(10).join(_srt_ali_lines))
                                        srt_path = _srt_ali
                                        auto_srt = True
                                        _volc_used = True
                                        temp_srt = _srt_ali
                                        _log(f"阿里云语音识别成功: {len(_segs_ali)} 条语音段")
                                    else:
                                        _log("阿里云 ASR 识别失败，将降级")
                                else:
                                    _log("音频提取失败，降级到本地 Whisper")
                            except Exception as _e_ali:
                                _log(f"阿里云 ASR 异常: {_e_ali}")
                        else:
                            _log("阿里云 ASR 配置不完整（需要 API Key + OSS AK/SK/Bucket），降级")
                    # --- 以下是火山引擎 ASR（仅在阿里云未成功时执行） ---
                    _v2_app_id = _cfg2.get("volc_app_id", "")
                    _v2_token = _cfg2.get("volc_access_token", "")
                    _v2_tos_ak = _cfg2.get("volc_tos_ak", "")
                    _v2_tos_sk = _cfg2.get("volc_tos_sk", "")
                    _v2_bucket = _cfg2.get("volc_bucket", "livec")
                    if not _volc_used and all([_v2_app_id, _v2_token, _v2_tos_ak, _v2_tos_sk]):
                        _log("启动火山引擎语音识别...")
                        from volcengine_asr import volcengine_asr
                        import tempfile as _tf2
                        import hashlib as _hl2
                        _temp_dir2 = _os2.path.join(_tf2.gettempdir(), "live_cutter_stt")
                        _os2.makedirs(_temp_dir2, exist_ok=True)
                        _vhash = _hl2.md5(video_path.encode("utf-8")).hexdigest()[:8]
                        _wav2 = _os2.path.join(_temp_dir2, f"audio_{_vhash}.wav")
                        _srt2 = _os2.path.join(_temp_dir2, f"sub_{_vhash}.srt")
                        # 提取音频
                        _ff2 = get_ffmpeg_cmd()
                        _ext_cmd = [_ff2, "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", _wav2]
                        _pk2 = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        _p2 = subprocess.Popen(_ext_cmd, **_pk2, creationflags=_NO_WINDOW)
                        _p2.wait(timeout=120)
                        if _p2.returncode == 0 and _os2.path.exists(_wav2):
                            _segs2 = volcengine_asr(_wav2, _v2_app_id, _v2_token, _v2_tos_ak, _v2_tos_sk, bucket=_v2_bucket, log_fn=_log)
                            if _segs2:
                                # 生成 SRT 文件
                                _srt_lines = []
                                for _i2, _seg2 in enumerate(_segs2, 1):
                                    _st2 = _seg2["start"]
                                    _et2 = _seg2["end"]
                                    _txt2 = _seg2["text"].strip()
                                    # 清理标点符号和语气词
                                    _txt2 = re.sub(r"[，。！？、；：“”‘’（）《》【】…—·,.!?:;'\"()\[\]{}<>\/\\-]", "", _txt2)
                                    _txt2 = re.sub(r"^[\u554a\u5462\u55ef\u54e6\u54c8]+|[\u554a\u5462\u55ef\u54e6\u54c8]+$", "", _txt2)
                                    _txt2 = _txt2.strip()
                                    if not _txt2:
                                        continue
                                    _srt_lines.append(str(_i2))
                                    _srt_lines.append(
                                        f"{int(_st2//3600):02d}:{int((_st2%3600)//60):02d}:{int(_st2%60):02d},{int((_st2%1)*1000):03d}"
                                        f" --> "
                                        f"{int(_et2//3600):02d}:{int((_et2%3600)//60):02d}:{int(_et2%60):02d},{int((_et2%1)*1000):03d}"
                                    )
                                    _srt_lines.append(_txt2)
                                    _srt_lines.append("")
                                with open(_srt2, "w", encoding="utf-8") as _f2:
                                    _f2.write("\n".join(_srt_lines))
                                srt_path = _srt2
                                auto_srt = True
                                _volc_used = True
                                temp_srt = _srt2
                                _log(f"火山引擎语音识别成功: {len(_segs2)} 条语音段")
                            else:
                                _log("⚠️ 云端语音识别失败，已自动切换到本地识别")
                        else:
                            _log("音频提取失败，降级到本地 Whisper")
                    elif _volc_used:
                        pass  # 阿里云已成功
                    else:
                        _log("未配置云端语音识别，使用本地识别")
            except Exception as _e2:
                _log(f"⚠️ 云端语音识别异常，已自动切换到本地识别")
        
        if not _volc_used:
            _log("[STEP] 🎬 语音识别中...")
            _log("启动本地语音识别 (Whisper)...")
            try:
                from stt import generate_srt
                # Read whisper model preference from settings
                _wmodel = "small"
                try:
                    import json as _json
                    _spath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_settings.json")
                    if os.path.exists(_spath):
                        with open(_spath, "r", encoding="utf-8-sig") as _sf:
                            _sdata = _json.load(_sf)
                        _wmodel = _sdata.get("whisper_model", "small")
                except Exception:
                    pass
                temp_srt = generate_srt(video_path, log_fn=_log, whisper_model=_wmodel)
            except Exception as _whisper_err:
                _err_str = str(_whisper_err).lower()
                if "huggingface" in _err_str or "hf_hub" in _err_str:
                    _log("❌ Whisper 模型下载失败（国内可能无法访问 HuggingFace）")
                    _log("💡 建议：1) 开启云端ASR（火山引擎）或 2) 手动提供 SRT 字幕文件")
                elif "winerror" in _err_str or "connection" in _err_str or "connect" in _err_str:
                    _log("❌ Whisper 模型下载失败：网络连接被中断")
                    _log("💡 建议：检查网络连接，或开启云端ASR / 提供SRT字幕文件")
                elif "cuda" in _err_str or "gpu" in _err_str:
                    _log("❌ Whisper GPU 加载失败，请尝试在设置中切换为 CPU 模式")
                else:
                    _log(f"❌ 语音识别失败: {_whisper_err}")
                    _log("💡 建议：开启云端ASR 或 手动提供 SRT 字幕文件")
                temp_srt = None
        if not temp_srt:
            _log("语音识别失败！")
            _run_log["结果"] = "失败"; _run_log["错误"] = "ASR识别失败"; _save_run_log(); return {"ok": False, "error": "asr_failed"}
        srt_path = temp_srt
        auto_srt = True
    # 2. 自动生成输出路径
    if not output_path:
        video_name = os.path.splitext(os.path.basename(video_path))[0]
        output_dir = os.path.join(os.path.dirname(video_path), "output")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"{video_name}_爆款切片.mp4")

    # 3. 解析字幕（AI 模式 或 关键词模式）
    # 多版本缓存（global声明，供_asr_only和_clips_only使用）
    global _multi_result_cache
    # _asr_only: 只做ASR，跳过AI选片
    if _asr_only:
        if isinstance(_multi_result_cache, dict):
            _srt_file = srt_path
            if _srt_file and os.path.exists(_srt_file):
                try:
                    with open(_srt_file, 'r', encoding='utf-8') as _f:
                        _multi_result_cache['srt_text'] = _f.read()
                except Exception:
                    pass
        _log("ASR完成，跳过AI选片（_asr_only模式）")
        return {"ok": True, "asr_only": True}

    from ai_clipper import is_enabled as ai_is_enabled, ai_analyze_clips, fallback_clips
    if _cancelled():
        _log("已取消。"); return {"ok": False, "error": "cancelled"}
    if ai_is_enabled():
        _log("[STEP] 🤖 AI 选片中...")
        _log("🤖 AI 智能选片模式已启用...")
        try:
            with open(srt_path, "r", encoding="utf-8") as f:
                srt_text = f.read()
            # 单版本：focus_hint传给AI（"自动"=随机偏好，指定=用指定偏好）
            _fh = focus_hint if focus_hint and focus_hint != "自动" else None
            ordered_clips = ai_analyze_clips(srt_text, log_fn=_log, force_category=force_category, multi_version=_clips_only, focus_hint=_fh)
            if not ordered_clips:
                _log("AI 选片为空，启动兜底逻辑...")
                ordered_clips = fallback_clips(srt_path, log_fn=_log, force_category=force_category)
        except Exception as e:
            _log(f"AI 调用失败: {e}，启动兜底逻辑...")
            try:
                ordered_clips = fallback_clips(srt_path, log_fn=_log, force_category=force_category)
            except Exception:
                ordered_clips = parse_srt_clips(srt_path, log_fn=_log)
    else:
        ordered_clips = parse_srt_clips(srt_path, log_fn=_log)
    if not ordered_clips:
        _log("未提取到核心片段！")
        if auto_srt and temp_srt:
            from stt import cleanup_srt; cleanup_srt(temp_srt)
        return False

    # 多版本缓存：保存选片结果和SRT内容，供 process_video_multi 使用
    try:
        if isinstance(_multi_result_cache, dict):
            _multi_result_cache['clips'] = list(ordered_clips)
            # 保存SRT内容
            if srt_path and os.path.exists(srt_path):
                with open(srt_path, "r", encoding="utf-8") as _f:
                    _multi_result_cache['srt_text'] = _f.read()
    except Exception:
        pass

    # 多版本模式：只做AI选片，跳过切割/去重/字幕（省30-60秒）
    if _clips_only:
        _log("🎬 多版本: AI选片完成，跳过全量处理（节省时间）")
        return {"ok": True, "clips_cached": True}

    # 4. 切割 + 去重 + 字幕叠加
    ffmpeg = get_ffmpeg_cmd()
    cfg = VIDEO_CONFIG

    # 动态检测源视频分辨率，短边=宽，长边=宽*16/9，偶数对齐
    _ff_dir = os.path.dirname(get_ffmpeg_cmd())
    _ffprobe = os.path.join(_ff_dir, "ffprobe" + (".exe" if sys.platform == "win32" else ""))
    _probe = [_ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams", "-select_streams", "v:0", "-i", video_path]
    try:
        _pr = subprocess.run(_probe, capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        _prj = json.loads(_pr.stdout)
        _vs = _prj.get("streams", [{}])[0]
        _video_fps = 30
        _rfr = _vs.get("r_frame_rate", "30/1")
        try:
            if "/" in str(_rfr):
                _n, _d = str(_rfr).split("/")
                _video_fps = int(_n) / max(int(_d), 1)
            else:
                _video_fps = float(_rfr)
        except:
            _video_fps = 30
        _sw, _sh = int(_vs.get("width", 0)), int(_vs.get("height", 0))
        if _sw > 0 and _sh > 0:
            # 直接使用源视频分辨率，只做偶数对齐
            w = _sw if _sw % 2 == 0 else _sw + 1
            h = _sh if _sh % 2 == 0 else _sh + 1
            _log(f"检测到源视频 {_sw}x{_sh}，输出分辨率: {w}x{h}")
        else:
            w, h = map(int, cfg["resolution"].split(":"))
            _log(f"无法获取视频分辨率，使用默认 {w}x{h}")
    except Exception as _e:
        w, h = map(int, cfg["resolution"].split(":"))
        _log(f"分辨率检测失败({_e})，使用默认 {w}x{h}")
    total_clips = len(ordered_clips)

    # 覆盖全局预设
    global DEDUP_PRESET
    old_preset = DEDUP_PRESET
    DEDUP_PRESET = dedup_preset

    # 临时目录
    temp_dir = os.path.join("C:\\", "lc_temp")
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)
    os.makedirs(temp_dir, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    will_subtitle = subtitle_overlay and SUBTITLE_OVERLAY.get("enabled")
    _log(f"去重: {dedup_preset} | 字幕叠加: {'开（后置Whisper+DeepSeek修复）' if will_subtitle else '关'}")
    # [v9.6] Parse SRT boundaries for hook tail buffer
    _srt_boundaries = []
    try:
        import re as _re
        with open(srt_path, "r", encoding="utf-8") as _sf:
            _srt_text = _sf.read()
        for _m in _re.finditer(r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})", _srt_text):
            _ts = _m.group(1).replace(",", ".").strip()
            _te = _m.group(2).replace(",", ".").strip()
            _h, _mi, _s = _ts.split(":")
            _start = int(_h)*3600 + int(_mi)*60 + float(_s)
            _h2, _mi2, _s2 = _te.split(":")
            _end = int(_h2)*3600 + int(_mi2)*60 + float(_s2)
            _srt_boundaries.append((_start, _end))
        _srt_boundaries.sort()
    except Exception:
        pass

    _log(f"开始切割 {total_clips} 个片段...")

    # 获取视频时长
    _log("检测视频时长...")
    try:
        ffmpeg_cmd = get_ffmpeg_cmd()
        if not os.path.exists(ffmpeg_cmd):
            ffmpeg_cmd = "ffmpeg"
        _log(f"FFmpeg: {ffmpeg_cmd}")
        probe_cmd = [ffmpeg_cmd, "-i", video_path]
        proc = subprocess.Popen(probe_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
        _, stderr_data = proc.communicate(timeout=45)
        import re as _re
        m = _re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", stderr_data)
        if m:
            video_duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 100
            _log(f"视频时长: {video_duration:.1f}s")
        else:
            video_duration = 9999
            _log("无法检测视频时长，跳过安全检查")
    except Exception:
        video_duration = 9999
        _log("无法检测视频时长，跳过安全检查")

    # ============================================================
    # 切割：每片段独立生成 ASS + 切割时烧录字幕（零漂移）
    # ============================================================
    temp_files = []
    success_count = 0
    _log(f"[STEP] ✂ 切割片段中 ({total_clips}段)...")
    # ===== Smart Crop 批量检测 =====
    _sc_results = None
    if smart_crop_enabled:
        try:
            from smart_crop import batch_detect_clips, compute_smart_crop, _even
            _sc_results = batch_detect_clips(video_path, ordered_clips, log_fn=_log, ffmpeg_cmd=ffmpeg_cmd, frame_w=w, frame_h=h)
        except ImportError:
            _log("SmartCrop: smart_crop.py 不可用，使用标准裁切")
            smart_crop_enabled = False
        except Exception as _sce:
            _log(f"SmartCrop: 检测失败({_sce})，使用标准裁切")
            smart_crop_enabled = False
    else:
        try:
            from smart_crop import _even
        except ImportError:
            def _even(v): return v + (v % 2)

    # Ken Burns import (independent of Smart Crop)
    if ken_burns_enabled:
        try:
            from smart_crop import apply_ken_burns_opencv
        except ImportError:
            _log("KenBurns: apply_ken_burns_opencv 不可用")
            ken_burns_enabled = False

    _log(f"开始切割 {total_clips} 个片段 (FFmpeg: {ffmpeg_cmd})...")
    _log(f"[T] {time.strftime('%H:%M:%S')} enter cut loop, total={total_clips}")

    try:
        _clip_starts = []
        _clip_ends = []
        for clip_idx, clip in enumerate(ordered_clips):
            c_type, text, start, end, score, dur = clip[0], clip[1], clip[2], clip[3], clip[4], clip[5]
            _log(f"[T] [{time.strftime('%H:%M:%S')}] loop clip_idx={clip_idx}")
            if _cancelled():
                _log("已取消，跳过剩余切割。"); break
            _log(f"切割 [{clip_idx+1}/{total_clips}] {c_type} ({start:.1f}s-{end:.1f}s)...")
            temp_file = os.path.join(temp_dir, f"clip_{clip_idx:02d}.mp4")
            _clip_starts.append(start)
            _clip_ends.append(end)

            # [v9.5] 尾部缓冲已禁用：会导致拖入其他片段内容产生重复
            start_buf = 0
            end_buf = 0
            # [v9.6] Hook尾部ASR补偿：SRT时间戳常比实际语音早0.2-0.4s
            # 用下一条SRT的start卡上限，不跨入下一句
            if 'hook' in c_type.lower() and _srt_boundaries:
                _next_srt = None
                for _ts, _te in _srt_boundaries:
                    if _ts > end + 0.01:
                        _next_srt = _ts
                        break
                _max_ext = min(0.5, _next_srt - end) if _next_srt else 0.5
                if _max_ext > 0:
                    end = min(video_duration, end + _max_ext)
            start = max(0, start - start_buf)
            end = min(video_duration, end + end_buf)

            if start >= video_duration:
                _log(f"SKIP [{c_type}] 起始 {start:.1f}s > 视频时长 {video_duration:.1f}s")
                continue
            if end > video_duration:
                end = video_duration - 0.1
                if end <= start:
                    continue

            # [v9.2] 切割编码模式 + Smart Crop + mirror
            mirror_vf = ""
            if random.random() < 0.5:
                mirror_vf = "hflip"
            clip_duration = end - start

            # Smart Crop VF
            if smart_crop_enabled and _sc_results is not None:
                _sc_info = _sc_results.get(clip_idx, None)
                _sc_crop = compute_smart_crop(_sc_info, w, h, crop_level=crop_level, log_fn=_log)
                if _sc_crop and _sc_crop.get("method") == "smart":
                    _cw = _even(int(w * _sc_crop["crop_w"]))
                    _ch = _even(int(h * _sc_crop["crop_h"]))
                    _cx = _even(int(w * _sc_crop["crop_x"]))
                    _cy = _even(int(h * _sc_crop["crop_y"]))
                    combined_vf = "crop=%d:%d:%d:%d,scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d" % (_cw, _ch, _cx, _cy, w, h, w, h)
                else:
                    _rc = _sc_crop or {}
                    _rcw = _even(int(w / _rc.get("zoom", 1.08)))
                    _rch = _even(int(h / _rc.get("zoom", 1.08)))
                    _rcx = _even(int((w - _rcw) / 2))
                    _rcy = _even(int(h - _rch))  # bottom preserved
                    combined_vf = "crop=%d:%d:%d:%d,scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d" % (_rcw, _rch, _rcx, _rcy, w, h, w, h)
            else:
                aspect_vf = r"crop=min(iw\,trunc(ih*9/16/2)*2):min(ih\,trunc(iw*16/9/2)*2)"
                combined_vf = aspect_vf

            if mirror_vf:
                combined_vf += "," + mirror_vf
            _log("[T] VF: " + combined_vf[:200])

            cmd = [ffmpeg, "-y"]
            # input seeking（-ss放-i前面）：重新编码下帧级精确
            cmd += ["-ss", f"{start:.3f}", "-i", video_path]
            cmd += ["-t", f"{clip_duration:.3f}"]
            cmd += ["-fflags", "+genpts"]
            cmd += ["-vsync", "cfr"]
            cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18"]
            cmd += ["-vf", combined_vf]
            cmd += ["-pix_fmt", "yuv420p"]
            cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", "-async", "1",
                   "-af", f"afade=t=in:st=0:d=0.15"]
            cmd += ["-movflags", "+faststart"]
            cmd += [temp_file]
            _log(f"[T] [{time.strftime('%H:%M:%S')}] Popen start")

            try:
                popen_kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                proc = subprocess.Popen(cmd, **popen_kwargs, creationflags=_NO_WINDOW)
                rc = proc.wait(timeout=120)
                _log(f"[T] [{time.strftime('%H:%M:%S')}] rc={rc}")
            except subprocess.TimeoutExpired:
                proc.kill()
                _log(f"TIMEOUT [{c_type}] {start:.2f}s-{end:.2f}s")
                continue
            except Exception as e:
                _log(f"[T] subprocess error: {type(e).__name__}: {e}")
                continue

            if rc == 0 and os.path.exists(temp_file) and os.path.getsize(temp_file) > 1000:
                size_kb = os.path.getsize(temp_file) / 1024
                _log(f"OK [{c_type}] {start:.1f}s-{end:.1f}s -> {size_kb:.0f}KB")
                temp_files.append(temp_file)
                success_count += 1
            else:
                _log(f"FAIL [{c_type}] rc={rc}")

            _log(f"[PROGRESS] {(clip_idx + 1) / total_clips * 0.3:.2f}")

    except Exception as e:
        _log(f"[T] FATAL: {type(e).__name__}: {e}")
        import traceback
        _log(traceback.format_exc())

    if not temp_files:
        _log("没有成功切割任何片段！")
        shutil.rmtree(temp_dir, ignore_errors=True)
        if auto_srt and temp_srt:
            from stt import cleanup_srt; cleanup_srt(temp_srt)
        DEDUP_PRESET = old_preset
        return False

    # ============================================================
    # 第二步：拼接（stream copy → 中间文件）
    # ============================================================
    if _cancelled():
        _log("已取消。"); shutil.rmtree(temp_dir, ignore_errors=True)
        if auto_srt and temp_srt:
            from stt import cleanup_srt; cleanup_srt(temp_srt)
        DEDUP_PRESET = old_preset
        return False

    _log(f"拼接 {len(temp_files)} 个片段...")
    list_file = os.path.join(temp_dir, "file_list.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for tf in temp_files:
            f.write(f"file '{os.path.abspath(tf).replace(chr(92), '/')}'\n")

        # ===== Ken Burns second pass =====
    if ken_burns_enabled:
        _log("KB: second pass start")
        _kb_ok = 0
        for _kbi, _clip_file in enumerate(temp_files):
            if cancel_event and cancel_event.is_set():
                break
            try:
                _kb_dur = _clip_ends[_kbi] - _clip_starts[_kbi] if _kbi < len(_clip_starts) else 10.0
                _kb_out = _clip_file.replace(".mp4", "_kb.mp4")
                _kb_ok_flag = apply_ken_burns_opencv(
                    _clip_file, _kb_out, _kb_dur, w, h, _video_fps,
                    ffmpeg_cmd=get_ffmpeg_cmd(), log_fn=_log)
                if _kb_ok_flag and os.path.exists(_kb_out):
                    os.replace(_kb_out, _clip_file)
                    _kb_ok += 1
                else:
                    if os.path.exists(_kb_out):
                        os.remove(_kb_out)
            except Exception as _kbe:
                _log(f"KB error clip {_kbi}: {_kbe}")
        _log("KB: %d/%d done" % (_kb_ok, len(temp_files)))

    raw_file = os.path.join(temp_dir, "raw_concat.mp4")
    concat_cmd = [
        ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_file,
        "-c:v", "copy", "-c:a", "copy",
        raw_file
    ]

    try:
        proc = subprocess.Popen(concat_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
        _, stderr_data = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        stderr_out = proc.communicate()[0]
        _log("拼接超时！(>120s)")
        if stderr_out:
            for line in stderr_out.strip().split("\n")[-5:]:
                if line.strip(): _log(f"  ffmpeg: {line.strip()}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        if auto_srt and temp_srt:
            from stt import cleanup_srt; cleanup_srt(temp_srt)
        DEDUP_PRESET = old_preset
        return False

    if proc.returncode != 0 or not os.path.exists(raw_file):
        _log(f"拼接失败！(exit={proc.returncode})")
        if stderr_data:
            for line in stderr_data.strip().split("\n")[-5:]:
                if line.strip(): _log(f"  ffmpeg: {line.strip()}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        if auto_srt and temp_srt:
            from stt import cleanup_srt; cleanup_srt(temp_srt)
        DEDUP_PRESET = old_preset
        return False

    raw_mb = os.path.getsize(raw_file) / (1024 * 1024)
    _log("[STEP] 🔗 拼接合并中...")
    _log(f"拼接完成: {raw_mb:.1f}MB")
    _log(f"[PROGRESS] 0.5")

    # ============================================================
    # 第三步：去重重编码（全程无字幕，避免镜像携带字幕）
    # ============================================================
    if _cancelled():
        _log("已取消。"); shutil.rmtree(temp_dir, ignore_errors=True)
        if auto_srt and temp_srt:
            from stt import cleanup_srt; cleanup_srt(temp_srt)
        DEDUP_PRESET = old_preset
        return False

    _log(f"整体去重 ({dedup_preset})...")

    nosub_file = os.path.join(temp_dir, "nosub.mp4")

    if dedup_preset == "none":
        import shutil as _shutil
        _shutil.copy2(raw_file, nosub_file)
    else:
        _log(f"去重步骤使用分辨率: {w}x{h}，去重预设: {dedup_preset}")
        dedup = build_dedup_filters(w, h, 0)
        # [v9.1] 9:16裁剪+镜像+afade从切割步骤移至去重步骤
        # 字幕在去重后添加，镜像不会影响字幕
        vf = f"setpts=PTS-STARTPTS,scale=-2:{h}:force_original_aspect_ratio=decrease,crop={w}:{h}"
        # 随机镜像（50%概率）
        if random.random() < 0.5:
            vf = "hflip," + vf
        # 音频淡入淡出（消除片段间硬切感）+ 异步重采样
        af = "afade=t=in:st=0:d=0.3"
        if dedup["video_filters"]:
            vf = dedup["video_filters"] + "," + vf
        if dedup["audio_filters"]:
            af = dedup["audio_filters"] + "," + af

        # 输出去重参数详情
        applied = ",".join(dedup["applied"]) if dedup["applied"] else "none"
        _log(f"去重效果: {applied}")

        # 判断是否需要 filter_complex（aevalsrc 会创建额外音频流）
        needs_complex = "aevalsrc" in af or "amix" in af

        dedup_cmd = [ffmpeg, "-y", "-i", raw_file]

        if needs_complex:
            af_parts = af.split(",")
            simple_af_parts = []
            noise_src = None
            for part in af_parts:
                if part.startswith("aevalsrc="):
                    noise_src = part
                elif part.startswith("amix="):
                    continue
                else:
                    simple_af_parts.append(part)
            simple_af = ",".join(simple_af_parts)
            if noise_src:
                complex_a = f"[0:a]{simple_af}[a1];{noise_src}[noise];[a1][noise]amix=inputs=2:duration=first:dropout_transition=0[out_a]"
            else:
                complex_a = f"[0:a]{simple_af}[out_a]" if simple_af else "[0:a]anull[out_a]"

            complex_v = f"[0:v]{vf}[out_v]"

            complex_graph = f"{complex_v};{complex_a}"
            dedup_cmd += ["-filter_complex", complex_graph]
            dedup_cmd += ["-map", "[out_v]", "-map", "[out_a]"]
        else:
            dedup_cmd += ["-vf", vf]
            dedup_cmd += ["-af", af]
        dedup_cmd += ["-r", str(cfg["fps"]), "-vsync", "cfr", "-b:v", cfg["bitrate_v"]]
        dedup_cmd += ["-c:v", cfg["codec_v"], "-preset", "ultrafast"]
        dedup_cmd += ["-c:a", cfg["codec_a"], "-b:a", cfg["bitrate_a"]]
        dedup_cmd += ["-movflags", "+faststart"]
        dedup_cmd += [nosub_file]

        try:
            proc = subprocess.Popen(dedup_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
            _, stderr_data = proc.communicate(timeout=600)
            if proc.returncode != 0:
                _log(f"去重FFmpeg返回 {proc.returncode}")
                _log(f"去重stderr: {stderr_data[-300:]}")
                # 去重失败时直接输出原始拼接
                import shutil as _shutil
                _shutil.copy2(raw_file, nosub_file)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            _log("去重超时，直接输出原始拼接...")
            import shutil as _shutil
            _shutil.copy2(raw_file, nosub_file)

        if not os.path.exists(nosub_file):
            _log(f"去重失败，直接输出原始拼接...")
            import shutil as _shutil
            _shutil.copy2(raw_file, nosub_file)

    nosub_mb = os.path.getsize(nosub_file) / (1024 * 1024)
    _log("[STEP] 📝 字幕处理中...")
    _log(f"[PROGRESS] 0.6")
    _log(f"去重完成: {nosub_mb:.1f}MB")

    # ============================================================
    # 第四步：字幕后置处理（Whisper识别最终视频 + DeepSeek修复错别字）
    # ============================================================
    if _cancelled():
        _log("已取消。"); shutil.rmtree(temp_dir, ignore_errors=True)
        if auto_srt and temp_srt:
            from stt import cleanup_srt; cleanup_srt(temp_srt)
        DEDUP_PRESET = old_preset
        return False

    # 画中画：auto模式在字幕关闭时也需要加；指定文件时总是加
    if pip_path and pip_path != "auto" and os.path.exists(pip_path):
        has_pip = True
    elif pip_path == "auto" and not will_subtitle:
        has_pip = True
    else:
        has_pip = False
    _log(f"字幕={will_subtitle}, 画中画={has_pip} ({pip_path})")
    if will_subtitle and os.path.exists(nosub_file) and os.path.getsize(nosub_file) > 10000:
        _add_subtitles_final(nosub_file, output_path, w, h, temp_dir, _log, pip_path, pip_size, pip_opacity, pip_pos)
    elif has_pip and os.path.exists(nosub_file):
        # auto模式用视频本身做画中画素材
        _effective_pip = video_path if pip_path == "auto" else pip_path
        _add_pip_only(nosub_file, output_path, temp_dir, _log, _effective_pip, pip_size, pip_opacity, pip_pos)
    else:
        import shutil as _shutil
        _shutil.copy2(nosub_file, output_path)

    # 第五步：AI 画面质量分析与替换
    if _cancelled():
        _log("已取消，跳过画面替换。")
    # ============================================================
    try:
        from vision_replace import is_vision_enabled, vision_replace_pipeline
        if os.path.exists(output_path):
            # Auto-enable: 只要 API Key 和视觉模型配置了就运行
            try:
                from vision_replace import load_vision_settings
                vs = load_vision_settings()
                auto_vision = bool(vs.get("api_key") and vs.get("base_url"))
            except Exception:
                auto_vision = False
            if auto_vision:
                # 传入剪辑片段信息用于画面分析
                clip_info = [{"start": c.get("start", 0) if isinstance(c, dict) else c[3], "end": c.get("end", 0) if isinstance(c, dict) else c[4]} for c in ordered_clips]
                vision_replace_pipeline(output_path, clip_info, log_fn=_log)
    except ImportError:
        pass  # vision_replace.py 不存在则跳过
    except Exception as e:
        _log(f"AI画面替换出错: {e}")

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    _log(f"[PROGRESS] 1.0")

    # ---- 切割评分 ----
    report = _build_cut_report(ordered_clips, success_count, total_clips, output_path, size_mb)
    _print_cut_report(report, _log)

    _log(f"生成成功！")
    _log(f"  路径: {output_path}")
    _log(f"  大小: {size_mb:.1f} MB")
    _log(f"  片段: {success_count}/{total_clips}")

    # 清理
    shutil.rmtree(temp_dir, ignore_errors=True)
    if auto_srt and temp_srt:
        from stt import cleanup_srt; cleanup_srt(temp_srt)
    DEDUP_PRESET = old_preset
    _run_log["结果"] = "成功"
    _run_log["选片"] = report
    _run_log["输出"] = output_path
    _save_run_log()
    return {"ok": True, "report": report}


# 兼容旧接口
def cut_and_dedup(video_path, srt_path, output_path, dedup_preset="medium", log_fn=None, cancel_event=None):
    return process_video(video_path, srt_path=srt_path, output_path=output_path,
                          dedup_preset=dedup_preset, subtitle_overlay=False, log_fn=log_fn,
                          cancel_event=cancel_event)



def _parse_srt_to_segments(srt_text):
    """解析 SRT 格式文本为 segments 列表: [{"start": float, "end": float, "text": str}, ...]"""
    import re
    segments = []
    lines = srt_text.strip().split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = re.match(r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})', line)
        if m:
            start = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3)) + int(m.group(4))/1000.0
            end = int(m.group(5))*3600 + int(m.group(6))*60 + int(m.group(7)) + int(m.group(8))/1000.0
            text = ""
            j = i + 1
            while j < len(lines) and lines[j].strip():
                text += lines[j].strip()
                j += 1
            if text.strip():
                segments.append({"start": start, "end": end, "text": text.strip()})
            i = j
        else:
            i += 1
    return segments



def _get_video_duration(path, ffmpeg_cmd):
    """Get video duration in seconds using ffprobe"""
    import subprocess, json
    ffprobe = ffmpeg_cmd.replace("ffmpeg", "ffprobe")
    if ffprobe == ffmpeg_cmd:
        ffprobe = "ffprobe"
    cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        data = json.loads(proc.stdout)
        return float(data.get("format", {}).get("duration", 0))
    except Exception:
        return 0


def _calc_pip_loop(main_path, pip_path, ffmpeg_cmd):
    """Calculate how many times pip video needs to loop to cover main video duration"""
    main_dur = _get_video_duration(main_path, ffmpeg_cmd)
    pip_dur = _get_video_duration(pip_path, ffmpeg_cmd)
    if pip_dur <= 0 or main_dur <= 0:
        return 2
    import math
    return max(2, math.ceil(main_dur / pip_dur) + 1)


def _add_pip_only(video_path, output_path, temp_dir, _log, pip_path, pip_size=0.15, pip_opacity=0.03, pip_pos="\u53f3\u4e0b"):
    """\u53ea\u53e0\u52a0\u753b\u4e2d\u753b\uff0c\u4e0d\u70e7\u5f55\u5b57\u5e55"""
    from platform_config import IS_MAC
    import subprocess, os, sys
    ffmpeg = get_ffmpeg_cmd()

    loop_n = _calc_pip_loop(video_path, pip_path, ffmpeg)
    _pos_map = {"\u5de6\u4e0a": "10:10", "\u53f3\u4e0a": "W-w-10:10", "\u5de6\u4e0b": "10:H-h-10", "\u53f3\u4e0b": "W-w-10:H-h-10"}
    _pip_pos = _pos_map.get(pip_pos, "W-w-10:H-h-10")
    _pip_fc = f"[1:v]scale=iw*{pip_size}:ih*{pip_size},format=rgba,colorchannelmixer=aa={pip_opacity}[pip];[0:v][pip]overlay={_pip_pos}[out_v]"

    _norm_output = output_path.replace("/", os.sep)
    cmd = [
        ffmpeg, "-y", "-i", video_path, "-stream_loop", str(loop_n), "-i", pip_path,
        "-filter_complex", _pip_fc,
        "-map", "[out_v]", "-map", "0:a",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "copy",
        "-shortest",
        "-movflags", "+faststart",
        _norm_output
    ]

    popen_kw = dict(stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")

    _log(f"叠加画中画: {os.path.basename(pip_path)}")
    try:
        proc = subprocess.Popen(cmd, **popen_kw, creationflags=_NO_WINDOW)
        _, stderr = proc.communicate(timeout=450)
        if proc.returncode == 0 and os.path.exists(output_path):
            _log("\u753b\u4e2d\u753b\u53e0\u52a0\u6210\u529f!")
        else:
            _log(f"\u753b\u4e2d\u753b\u53e0\u52a0\u5931\u8d25: {stderr[-200:] if stderr else ''}")
            import shutil as _shutil; _shutil.copy2(video_path, output_path)
    except Exception as e:
        _log(f"\u753b\u4e2d\u753b\u53e0\u52a0\u5f02\u5e38: {e}")
        import shutil as _shutil; _shutil.copy2(video_path, output_path)


def _add_subtitles_final(video_path, output_path, w, h, temp_dir, _log, pip_path=None, pip_size=0.15, pip_opacity=0.03, pip_pos="右下"):
    """
    字幕后置处理：对去重后的视频做 ASR 识别(云端优先) → DeepSeek修复 → 烧录字幕。
    时间戳来自最终视频本身，100% 对齐，不受镜像/拼接/变速影响。
    """
    import json as _json

    _log("=" * 50)
    _log("第四步：字幕后置处理")
    _log("=" * 50)

    # --- 4a: 提取音频 ---
    wav_path = os.path.join(temp_dir, "final_audio.wav")
    ffmpeg = get_ffmpeg_cmd()
    extract_cmd = [ffmpeg, "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", wav_path]
    try:
        popen_kw = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc = subprocess.Popen(extract_cmd, **popen_kw, creationflags=_NO_WINDOW)
        rc = proc.wait(timeout=60)
        if rc != 0 or not os.path.exists(wav_path):
            _log("音频提取失败，跳过字幕")
            import shutil as _shutil; _shutil.copy2(video_path, output_path)
            return
    except Exception as e:
        _log(f"音频提取异常: {e}，跳过字幕")
        import shutil as _shutil; _shutil.copy2(video_path, output_path)
        return

    wav_mb = os.path.getsize(wav_path) / (1024 * 1024)
    _log(f"音频提取完成: {wav_mb:.1f}MB")
    _log("[PROGRESS] 0.65")

    # --- 4b: Whisper(时间戳) + 云端ASR(准文字) 并行识别 ---
    import os as _os
    _os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'

    raw_segments = None
    volcengine_success = False
    cloud_reference = ""  # 云端ASR的准确全文，用于AI修正

    def _run_aliyun_asr_subtitle():
        """阿里云 ASR：高精度字级时间戳+按标点断句"""
        try:
            with open(sp, "r", encoding="utf-8-sig") as _af:
                _acfg = _json.load(_af)
                if not _acfg.get("asr_enabled", False):
                    _log("云端ASR未启用，跳过阿里云")
                    return
        except Exception:
            pass
        nonlocal raw_segments, volcengine_success
        try:
            _log("正在尝试阿里云 ASR...")
            if getattr(sys, "frozen", False):
                sd = os.path.dirname(sys.executable)
            else:
                sd = os.path.dirname(os.path.abspath(__file__))
            sp2 = os.path.join(sd, "ai_settings.json")
            if not os.path.exists(sp2):
                _log("aliyun_asr: ai_settings.json 不存在，跳过")
                return
            with open(sp2, "r", encoding="utf-8-sig") as f:
                cfg = _json.load(f)
            _ali_api_key = cfg.get("aliyun_api_key", "")
            _ali_oss_ak = cfg.get("aliyun_oss_ak", "")
            _ali_oss_sk = cfg.get("aliyun_oss_sk", "")
            _ali_bucket = cfg.get("aliyun_bucket", "")
            _ali_endpoint = cfg.get("aliyun_endpoint", "oss-cn-beijing.aliyuncs.com")
            _ali_model = cfg.get("asr_model", "paraformer-v2") or "paraformer-v2"
            if not all([_ali_api_key, _ali_oss_ak, _ali_oss_sk, _ali_bucket]):
                _log("aliyun_asr: 未配置阿里云参数，跳过")
                return
            from aliyun_asr import aliyun_asr
            segs = aliyun_asr(wav_path, app_key=_ali_api_key, model=_ali_model,
                             oss_ak=_ali_oss_ak, oss_sk=_ali_oss_sk,
                             oss_bucket=_ali_bucket, oss_endpoint=_ali_endpoint,
                             log_fn=_log)
            if segs:
                raw_segments = segs
                volcengine_success = True
                _log(f"阿里云 ASR 成功: {len(raw_segments)} 条语音段")
            else:
                _log("阿里云 ASR 失败，将降级")
        except Exception as e:
            _log(f"阿里云 ASR 异常: {e}")

    def _run_volcengine_asr():
        """火山引擎大模型 ASR：高精度时间戳+断句"""
        # 仅当云端ASR启用时才执行
        try:
            with open(sp, "r", encoding="utf-8-sig") as _vf:
                if not _json.load(_vf).get("asr_enabled", False):
                    _log("云端ASR未启用，跳过火山引擎")
                    return
        except Exception:
            pass
        nonlocal raw_segments, volcengine_success
        try:
            _log("正在尝试火山引擎 ASR...")
            if getattr(sys, "frozen", False):
                sd = os.path.dirname(sys.executable)
            else:
                sd = os.path.dirname(os.path.abspath(__file__))
            sp = os.path.join(sd, "ai_settings.json")
            if not os.path.exists(sp):
                _log("volcengine_asr: ai_settings.json 不存在，跳过")
                return
            with open(sp, "r", encoding="utf-8-sig") as f:
                cfg = _json.load(f)
            v_app_id = cfg.get("volc_app_id", "")
            v_token = cfg.get("volc_access_token", "")
            v_tos_ak = cfg.get("volc_tos_ak", "")
            v_tos_sk = cfg.get("volc_tos_sk", "")
            if not all([v_app_id, v_token, v_tos_ak, v_tos_sk]):
                _log("volcengine_asr: 未配置火山引擎参数，跳过")
                return
            from volcengine_asr import volcengine_asr
            v_bucket = cfg.get("volc_bucket", "livec")
            segs = volcengine_asr(wav_path, v_app_id, v_token, v_tos_ak, v_tos_sk,
                                 bucket=v_bucket, log_fn=_log)
            if segs:
                raw_segments = segs
                volcengine_success = True
                _log(f"火山引擎 ASR 成功: {len(raw_segments)} 条语音段")
            else:
                _log("火山引擎 ASR 失败，将降级到 Whisper")
        except Exception as e:
            _log(f"火山引擎 ASR 异常: {e}")

    def _run_whisper():
        """Whisper: 精确时间戳（降级方案）"""
        nonlocal raw_segments
        _log("正在用 Whisper 识别最终视频音频...")
        try:
            from faster_whisper import WhisperModel
            from platform_config import WHISPER_DEVICE, WHISPER_COMPUTE
            model = WhisperModel("small", device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
            segs_iter, info = model.transcribe(wav_path, language="zh", beam_size=5, vad_filter=True)
            _log(f"Whisper 识别语言: {info.language} (概率: {info.language_probability:.2f})")
            raw_segments = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segs_iter]
            _log(f"Whisper 识别完成: {len(raw_segments)} 条语音段")
            del model
        except Exception as e:
            _log(f"Whisper 识别失败: {e}")

    def _build_fallback_segments(cloud_text, wav_path, _log):
        """[PATCH] When Whisper fails, use cloud ASR text with estimated timestamps"""
        if not cloud_text or len(cloud_text) < 10:
            return None
        try:
            from faster_whisper import WhisperModel
            _log("Whisper失败，加载Whisper做时间对齐...")
            from platform_config import WHISPER_DEVICE, WHISPER_COMPUTE
            model = WhisperModel("small", device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
            segs_iter, _info = model.transcribe(wav_path, language="zh", beam_size=5, vad_filter=True)
            w_segs = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segs_iter]
            del model
            if not w_segs:
                return None
            # Distribute cloud text across Whisper timestamps by character ratio
            total_w_chars = sum(len(s["text"]) for s in w_segs)
            if total_w_chars == 0:
                return None
            chars = list(cloud_text)
            total_c = len(chars)
            ci = 0
            result = []
            for seg in w_segs:
                ratio = len(seg["text"]) / total_w_chars
                seg_len = max(1, int(total_c * ratio))
                seg_text = "".join(chars[ci:ci + seg_len]).strip()
                ci += seg_len
                if seg_text:
                    result.append({"start": seg["start"], "end": seg["end"], "text": seg_text})
            return result if result else None
        except Exception as e:
            _log(f"备用字幕生成失败: {e}")
            return None

    def _run_cloud_asr():
        """云端ASR: 准确全文"""
        nonlocal cloud_reference
        try:
            from asr_api import is_asr_enabled as _ce, cloud_asr as _ca
            if _ce():
                _log("正在用云端 ASR 获取准确文本(并行)...")
                import subprocess as _sp, tempfile as _tf
                mp3_p = wav_path.replace(".wav", "_ref.mp3")
                ffmpeg = get_ffmpeg_cmd()
                kw = dict(stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                p = _sp.Popen([ffmpeg, "-y", "-i", video_path, "-vn", "-acodec",
                              "libmp3lame", "-ar", "16000", "-ac", "1", "-q:a", "4", mp3_p], **kw)
                p.wait(timeout=60)
                if p.returncode == 0:
                    srt_text = _ca(mp3_p)
                    try: _os.remove(mp3_p)
                    except: pass
                    if srt_text:
                        # Extract just the text lines from SRT
                        import re
                        texts = []
                        for line in srt_text.split("\n"):
                            if not line.strip() or re.match(r"^\d+$", line.strip()) or "-->" in line:
                                continue
                            texts.append(line.strip())
                        cloud_reference = "".join(texts)
                        _log(f"云端ASR参考文本: {len(cloud_reference)}字")
                    else:
                        _log("云端ASR参考文本获取失败")
        except Exception as e:
            _log(f"云端ASR参考获取跳过: {e}")

    # 先尝试火山引擎 ASR，失败则降级到 Whisper + 云端ASR
    import threading

    # 字幕阶段：跟随用户ASR选项（阿里云/火山引擎）
    _asr_preset_sub = ""
    _use_cloud_sub = False
    try:
        if getattr(sys, "frozen", False):
            _sub_sd = os.path.dirname(sys.executable)
        else:
            _sub_sd = os.path.dirname(os.path.abspath(__file__))
        _sub_sp = os.path.join(_sub_sd, "ai_settings.json")
        with open(_sub_sp, "r", encoding="utf-8-sig") as _cf:
            _sub_cfg = _json.load(_cf)
            _use_cloud_sub = _sub_cfg.get("asr_enabled", False)
            _asr_preset_sub = _sub_cfg.get("asr_preset", "") or _sub_cfg.get("asr_provider", "")
    except:
        pass
    if _use_cloud_sub:
        if _asr_preset_sub == "阿里云":
            _log("字幕阶段：云端ASR已启用，优先阿里云")
            t_ali = threading.Thread(target=_run_aliyun_asr_subtitle)
            t_ali.start()
            t_ali.join(timeout=180)
            if not volcengine_success:
                _log("阿里云ASR失败，降级到本地Whisper")
                t1 = threading.Thread(target=_run_whisper)
                t1.start()
                t1.join(timeout=180)
        else:
            _log("字幕阶段：云端ASR已启用，优先火山引擎")
            t_volc = threading.Thread(target=_run_volcengine_asr)
            t_volc.start()
            t_volc.join(timeout=120)
            if not volcengine_success:
                _log("火山引擎ASR失败，降级到本地Whisper")
                t1 = threading.Thread(target=_run_whisper)
                t1.start()
                t1.join(timeout=180)
    else:
        _log("字幕阶段：云端ASR未启用，使用本地Whisper")
        t1 = threading.Thread(target=_run_whisper)
        t1.start()
        t1.join(timeout=180)

    if not volcengine_success:
        if not raw_segments:
            if cloud_reference:
                _log("Whisper失败，尝试用云端ASR文本生成字幕...")
                fallback = _build_fallback_segments(cloud_reference, wav_path, _log)
                if fallback:
                    raw_segments = fallback
                    _log(f"云端ASR备用字幕: {len(raw_segments)} 条")
                else:
                    _log("云端ASR备用也失败，跳过字幕")
                    import shutil as _shutil; _shutil.copy2(video_path, output_path)
                    return
            else:
                _log("Whisper失败且无云端参考，跳过字幕")
                if pip_path and pip_path != "auto" and os.path.exists(pip_path):
                    _log("尝试只叠加画中画（无字幕）...")
                    _add_pip_only(video_path, output_path, temp_dir, _log, pip_path, pip_size, pip_opacity, pip_pos)
                else:
                    import shutil as _shutil; _shutil.copy2(video_path, output_path)
                return

    _log("[PROGRESS] 0.75")

# --- 4c: DeepSeek修复错别字 + 繁简转换 + 长句切分 ---
    if False:  # 始终走DeepSeek修复
        _log("云端ASR也需要DeepSeek修复")
        # 仍然清理标点符号和语气词（不跳过）
        _punct_re = re.compile(r"[，。！？、；：“”‘’（）《》【】…—·,.!?;:\'\"()\[\]{}<>]")
        _filler_re = re.compile(r"^[啊呢嗯哦哈]+|[啊呢嗯哦哈]+$")
        for seg in raw_segments:
            seg["text"] = _punct_re.sub("", seg["text"])
            seg["text"] = _filler_re.sub("", seg["text"])
            seg["text"] = seg["text"].strip()
        fixed_segments = raw_segments
    else:
        _log("正在用DeepSeek修复字幕（错别字+繁简转换+断句）...")
        try:
            if getattr(sys, "frozen", False):
                settings_dir = os.path.dirname(sys.executable)
            else:
                settings_dir = os.path.dirname(os.path.abspath(__file__))
            settings_path = os.path.join(settings_dir, "ai_settings.json")
            if os.path.exists(settings_path):
                with open(settings_path, "r", encoding="utf-8-sig") as f:
                    settings = _json.load(f)
                api_key = settings.get("api_key", "")
                base_url = settings.get("base_url", "").rstrip("/")
                model = settings.get("model", "")
            else:
                api_key = ""; base_url = ""; model = ""
        except Exception:
            api_key = ""; base_url = ""; model = ""

        if not api_key:
            _log("未找到 AI API Key，跳过DeepSeek修复，直接用 Whisper 原始文本")
            fixed_segments = raw_segments
        else:
            seg_text = "\n".join([f"[{s['start']:.2f}-{s['end']:.2f}] {s['text']}" for s in raw_segments])
            ref_note = ""
            if cloud_reference:
                ref_note = f"\n\n参考（另一个更准确的语音识别结果，用于纠错参考）：\n{cloud_reference}"

            fix_prompt = f"""你是抖音直播字幕修复专家。请修复以下 Whisper 语音识别结果：
1. 修正错别字（女装术语：网纱、晴纶、锦纶、阔腿裤、罩衫、连衣裙、风衣、夹克等）
2. 繁体字转简体（褲→裤、襯→衬、風→风、夾→夹、羽絨→羽绒等）
3. 去除废话词(呕嗯然后对对对就是那个这个) + 句内重复词(已经。已经→已经) + 填充音(啊啊啊)
4. 保持时间戳不变，严格按行输出，每行格式：[start-end] 文本

原始字幕：
{seg_text}
{ref_note}

直接输出修复后的字幕，每行格式同上，不要加任何解释："""

            try:
                import urllib.request
                import ssl as _ssl
                ctx = _ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = _ssl.CERT_NONE

                req_body = _json.dumps({
                    "model": model,
                    "messages": [{"role": "user", "content": fix_prompt}],
                    "temperature": 0.1,
                    "max_tokens": 4000
                }).encode("utf-8")

                req = urllib.request.Request(
                    f"{base_url}/chat/completions",
                    data=req_body,
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
                )
                with urllib.request.urlopen(req, timeout=45, context=ctx) as resp:
                    resp_data = _json.loads(resp.read().decode("utf-8"))

                fixed_text = resp_data["choices"][0]["message"]["content"].strip()
                # V4 Flash sometimes wraps in markdown code blocks
                if fixed_text.startswith("```"):
                    fixed_text = re.sub(r"^```[a-z]*\n?|\n?```$", "", fixed_text).strip()
                _log("DeepSeek修复完成")

                import re as _re
                fixed_segments = []
                for line in fixed_text.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    m = _re.match(r'\[(\d+\.?\d*)-(\d+\.?\d*)\]\s*(.*)', line)
                    if m:
                        fixed_segments.append({
                            "start": float(m.group(1)),
                            "end": float(m.group(2)),
                            "text": m.group(3).strip()
                        })

                if len(fixed_segments) < len(raw_segments) // 2:
                    _log(f"DeepSeek返回解析异常（{len(fixed_segments)}条 vs 原始{len(raw_segments)}条），回退到 Whisper 原始文本")
                    fixed_segments = raw_segments
                else:
                    _log(f"DeepSeek修复: {len(fixed_segments)} 条字幕")
            except Exception as e:
                _log(f"DeepSeek修复失败: {e}，回退到 Whisper 原始文本")
                fixed_segments = raw_segments

    _log("[PROGRESS] 0.85")

    # --- 去重叠：相邻 segment 时间交叉时截断前一个的 end ---
    if len(fixed_segments) > 1:
        deduped = [fixed_segments[0].copy()]
        for seg in fixed_segments[1:]:
            prev = deduped[-1]
            if seg["start"] < prev["end"]:
                prev["end"] = seg["start"]  # 截断前一个
            if seg["end"] > seg["start"]:  # 有效段才保留
                deduped.append(seg.copy())
        if len(deduped) != len(fixed_segments):
            _log(f"字幕去重叠: {len(fixed_segments)} → {len(deduped)} 条")
        fixed_segments = deduped

        # [已移除] 字幕阶段不做文本去重：中文口语字符集重叠率高，Jaccard>60%会误杀有效字幕行
    # AI选片阶段已做语义去重，字幕阶段只需忠实显示ASR识别内容
    # 2026-04-21: 修复字幕内容错位问题（误删导致后续字幕时间戳不错但文本错位）

# --- 长句拆分：把超过 max_chars 的 segment 按标点拆成多段，时间按字符比分配 ---
    max_sub = 14
    min_sub = 4  # 最短片段不低于4字
    split_segments = []
    for seg in fixed_segments:
        text = seg["text"].strip()
        if not text:
            continue
        seg_dur = seg["end"] - seg["start"]
        if len(text) <= max_sub:
            split_segments.append(seg)
            continue
        # 按标点 + 语气词优先断句
        parts = []
        while len(text) > max_sub:
            cut = -1
            # 优先找标点（在 max_sub 范围内找最后一个标点）
            for p in ["，", "。", "！", "？", "、", "；", "：", "~", "—", ",", ".", "!", "?", ";", ":"]:
                pos = text.rfind(p, 1, max_sub + 1)
                if pos > 0 and (len(text) - pos >= min_sub or len(text) <= max_sub * 2):
                    cut = pos + 1
                    break
            # 如果找不到标点，找语气词
            if cut <= 0:
                for word in ["啊", "呢", "吧", "嘛", "哦", "呀", "哈", "哎", "嗯", "嘛", "啦", "哟", "哇", "噢"]:
                    pos = text.rfind(word, 1, max_sub + 1)
                    if pos > 0 and pos + len(word) <= max_sub + 1:
                        cut = pos + len(word)
                        break
            # 仍找不到则硬切，在词边界处切，避免截断词语
            if cut <= 0:
                cut = max_sub
                remaining = len(text) - cut
                if remaining > 0 and remaining < min_sub:
                    cut = len(text) - min_sub
                # 词边界检测：向前/向后扫描找助词/连词位置（在助词后断句）
                if 0 < cut < len(text):
                    best = cut
                    # 向前扫：找到「的了着过是在也都还很最把被让给和与但而」结尾位置
                    for offset in range(0, 6):
                        pos = cut - offset
                        if pos <= 1:
                            break
                        if text[pos-1] in '的了着过是在也都还很最把被让给和与但而':
                            if len(text) - pos >= min_sub:
                                best = pos
                                break
                    # 向后扫：如果向前没找到，向后找下一个助词位置
                    if best == cut and len(text) > cut:
                        for offset in range(1, 6):
                            pos = cut + offset
                            if pos >= len(text):
                                break
                            if text[pos-1] in '的了着过是在也都还很最把被让给和与但而':
                                if len(text) - pos >= min_sub:
                                    best = pos
                                    break
                    cut = best
                    # 检查是否切断了常见双字词（如"特点"→"特"+"点"）
                    if 0 < cut < len(text):
                        pair = text[cut-1:cut+1]
                        _common_pairs = {'特点','特色','特别','非常','相当','所以','因为','但是',
                            '然后','而且','或者','以及','已经','正在','可以','能够','应该',
                            '我们','这个','那个','整个','全部','完全','很多','很好','最好',
                            '最后','出来','起来','下来','一点','一下','一直','一切','衣服',
                            '面料','颜色','尺码','版型','款式','腰线','领口','袖子','下摆',
                            '细节','设计','汉麻','天丝','真丝','棉麻','雪纺','女装','新款',
                            '老款','补货','现货','差不多','不少','不行','不能','好的','行了'}
                        if pair in _common_pairs:
                            if cut - 1 >= 4 and len(text) - (cut - 1) >= min_sub:
                                cut = cut - 1
                            elif cut + 1 < len(text) and len(text) - (cut + 1) >= min_sub:
                                cut = cut + 1
                if cut <= 0 or cut >= len(text):
                    cut = min(max_sub, len(text))
            parts.append(text[:cut])
            text = text[cut:]
        if text.strip():
            parts.append(text)
        # 过滤掉太短的片段（合并到前一个）
        merged = []
        for part in parts:
            p = part.strip()
            if not p:
                continue
            if len(p) < min_sub and merged:
                merged[-1] = merged[-1] + p
            elif len(p) < min_sub and not merged:
                merged.append(p)  # 第一个片段保留
            else:
                merged.append(p)
        # 按字符数比例分配时间
        total_chars = sum(len(p) for p in merged)
        t = seg["start"]
        for part in merged:
            ratio = len(part) / total_chars if total_chars > 0 else 1 / len(merged)
            p_dur = seg_dur * ratio
            split_segments.append({
                "start": t,
                "end": t + p_dur,
                "text": part.strip()
            })
            t += p_dur
    if len(split_segments) != len(fixed_segments):
        _log(f"长句拆分: {len(fixed_segments)} → {len(split_segments)} 条")
    fixed_segments = split_segments

    # --- 字幕文本 ASR 修正（修正识别错误） ---
    try:
        from config import ASR_CORRECTIONS as _asr_corrections
        if _asr_corrections:
            _asr_fixed = 0
            for seg in fixed_segments:
                t = seg["text"]
                for wrong, right in _asr_corrections.items():
                    if wrong in t:
                        t = t.replace(wrong, right)
                if t != seg["text"]:
                    seg["text"] = t
                    _asr_fixed += 1
            if _asr_fixed:
                _log(f"字幕ASR修正: {len(fixed_segments)} 条中修正了 {_asr_fixed} 条")
    except ImportError:
        pass  # ASR_CORRECTIONS not defined in config

        # --- 去除字幕标点符号 ---
    _punct_chars = '，。！？、；：\u201c\u201d\u2018\u2019（）《》【】…—·,.!?;:\\\'\\"()[]{}<>~～·・'
    for seg in fixed_segments:
        seg["text"] = seg["text"].translate(str.maketrans('', '', _punct_chars)).strip()
    _log(f"字幕标点已清除")

    # --- 4d+4e: drawtext 逐条烧录字幕 ---
    # 不用 subtitles/ass 滤镜（Windows 上 fontconfig 不可靠）
    # 直接用 drawtext + textfile + enable 逐条烧录，最可靠
    _log("正在用 drawtext 烧录字幕...")
    from platform_config import DRAWTEXT_FONT_PATH, FONT_BOLD_PATH, IS_MAC
    # Copy font to temp dir to avoid Chinese path issues with fontconfig
    _font_dest = os.path.join(temp_dir, "drawtext_font.ttc")
    if os.path.exists(FONT_BOLD_PATH) and not os.path.exists(_font_dest):
        import shutil as _shutil_font
        _shutil_font.copy2(FONT_BOLD_PATH, _font_dest)
    if os.path.exists(_font_dest):
        _drawtext_font = _font_dest.replace(os.sep, "/").replace(":", "\\:")
    else:
        _drawtext_font = DRAWTEXT_FONT_PATH  # fallback
    sc = SUBTITLE_OVERLAY
    font_size = sc.get("font_size", 52)
    # 根据视频宽度自适应缩放字号（基准1080px）
    if w and w > 0:
        font_size = max(28, int(font_size * w / 1080))
    outline_w = sc.get("outline_width", 4)
    margin_v = sc.get("margin_v", 270) + 100  # 上移100

    try:
        drawtext_filters = []
        text_files = []
        for seg in fixed_segments:
            if not seg["text"]:
                continue
            lines = [seg["text"]]

            # 为每行创建一个 drawtext
            for li, line in enumerate(lines):
                txt_path = os.path.join(temp_dir, f"sub_{seg['start']:.2f}_{li}.txt")
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(line)
                text_files.append(txt_path)

                # 文本文件路径转义（Windows 需要转义冒号和反斜杠）
                if IS_MAC:
                    tf = txt_path.replace("'", "'\\''")
                else:
                    tf = txt_path.replace("\\", "/").replace(":", "\\:")
                font = f"fontfile='{_drawtext_font}'"
                s_start = seg['start']
                s_end = seg['end']
                # 单行居中，y = h - margin_v
                line_offset = li * (font_size + 6)
                dt = (
                    f"drawtext={font}:textfile='{tf}'"
                    f":fontsize={font_size}:fontcolor=white"
                    f":shadowx=2:shadowy=2:shadowcolor=black@0.5"
                    f":x=(w-text_w)/2:y=h-{margin_v}-{line_offset}"
                    f":enable='between(t\\,{s_start:.3f}\\,{s_end:.3f})'"
                )
                drawtext_filters.append(dt)

        if not drawtext_filters:
            _log("无有效字幕文本，跳过烧录")
            import shutil as _shutil; _shutil.copy2(video_path, output_path)
        else:
            vf_chain = ",".join(drawtext_filters)
            _log(f"drawtext 滤镜数量: {len(drawtext_filters)}")
            _log(f"视频: {os.path.getsize(video_path)/(1024*1024):.1f}MB")

            # 画中画：在字幕步骤一起叠加，不增加编码次数
            _has_pip = pip_path is not None  # "auto" or actual file path
            # 位置映射
            _pos_map = {"左上":"10:10", "右上":"W-w-10:10", "左下":"10:H-h-10", "右下":"W-w-10:H-h-10"}
            _pip_pos = _pos_map.get(pip_pos, "W-w-10:H-h-10")
            if _has_pip:
                if pip_path and pip_path != "auto" and os.path.exists(pip_path):
                    _pip_fc = f"[1:v]scale=iw*{pip_size}:ih*{pip_size},format=rgba,colorchannelmixer=aa={pip_opacity}[pip];[0:v][pip]overlay={_pip_pos}[with_pip]"
                    _log(f"画中画: 叠加 {os.path.basename(pip_path)} (大小={pip_size:.0%}, 透明度={pip_opacity:.0%}, 位置={pip_pos})")
                    _log(f"画中画filter: {_pip_fc}")
                    _norm_output = output_path.replace("/", os.sep)
                    # drawtext 在 [with_pip] 上，输出 [out_v]
                    _drawtext_fc = "[with_pip]" + vf_chain + ",copy[out_v]"
                    loop_n = _calc_pip_loop(video_path, pip_path, ffmpeg)
                    sub_cmd = [
                        ffmpeg, "-y", "-i", video_path, "-stream_loop", str(loop_n), "-i", pip_path,
                        "-filter_complex",
                        f"{_pip_fc};{_drawtext_fc}",
                        "-map", "[out_v]", "-map", "0:a",
                        "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "copy",
                        "-shortest",
                        "-movflags", "+faststart",
                        _norm_output
                    ]
                else:
                    _log("画中画: 自动模式（自身缩小叠加）")
                    _pip_fc = f"[0:v]split[main][pip];[pip]scale=iw*0.15:ih*0.15,format=rgba,colorchannelmixer=aa=0.03[overlay];[main][overlay]overlay={_pip_pos}[with_pip]"
                    _norm_output = output_path.replace("/", os.sep)
                    _drawtext_fc = "[with_pip]" + vf_chain
                    sub_cmd = [
                        ffmpeg, "-y", "-i", video_path,
                        "-filter_complex",
                        f"{_pip_fc};{_drawtext_fc}",
                        "-map", "0:a",
                        "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "copy",
                        "-shortest",
                        "-movflags", "+faststart",
                        _norm_output
                    ]
            else:
                # 无画中画：保持原有 -vf 方式
                _norm_output = output_path.replace("/", os.sep)
                sub_cmd = [
                    ffmpeg, "-y", "-i", video_path,
                    "-vf", vf_chain,
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "copy",
                    "-movflags", "+faststart",
                    _norm_output
                ]

            popen_kw = dict(stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
            # Windows 下禁用 fontconfig，避免 drawtext 初始化失败
            fc_env = None
            if sys.platform == "win32":
                fc_conf = os.path.join(temp_dir, "fonts.conf")
                with open(fc_conf, "w", encoding="utf-8") as f:
                    f.write('<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n<fontconfig></fontconfig>\n')
                popen_kw["env"] = dict(os.environ)
                popen_kw["env"]["FONTCONFIG_FILE"] = fc_conf
            proc = subprocess.Popen(sub_cmd, **popen_kw, creationflags=_NO_WINDOW)
            _, stderr_data = proc.communicate(timeout=450)
            if proc.returncode != 0 or not os.path.exists(output_path):
                _log("字幕烧录失败，输出无字幕版本")
                _log(f"FFmpeg exit code: {proc.returncode}")
                if stderr_data:
                    for line in stderr_data.strip().split("\n")[-10:]:
                        if line.strip(): _log(f"  ffmpeg: {line.strip()}")
                import shutil as _shutil; _shutil.copy2(video_path, output_path)
            else:
                _log("字幕烧录成功！")
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        _log("字幕烧录超时，输出无字幕版本")
        import shutil as _shutil; _shutil.copy2(video_path, output_path)
    except Exception as e:
        _log(f"字幕烧录异常: {e}，输出无字幕版本")
        import shutil as _shutil; _shutil.copy2(video_path, output_path)

    _log("字幕处理完成")


def process_video_multi(video_path, srt_path=None, output_path=None,
                        dedup_preset="medium", subtitle_overlay=True,
                        log_fn=None, force_category=None, cancel_event=None,
                        pip_path=None, pip_size=0.15, pip_opacity=0.03, pip_pos="右下",
                        num_versions=1, focus_hint="自动", smart_crop_enabled=True, crop_level="medium", ken_burns_enabled=True):
    """多版本输出：AI直接输出3个独立叙事方案，每个方案完整裁切
    
    策略(v2)：AI选片时直接出3个不同角度的方案，代码层只做裁切。
    比旧方案（一次选片+代码拆分）叙事更完整、版本差异化更好。
    """
    def _log(msg):
        if log_fn: log_fn(msg)
    
    if num_versions <= 1:
        return process_video(video_path, srt_path, output_path,
                           dedup_preset, subtitle_overlay, log_fn,
                           force_category, cancel_event,
                           pip_path, pip_size, pip_opacity, pip_pos,
                               smart_crop_enabled=smart_crop_enabled, crop_level=crop_level, ken_burns_enabled=ken_burns_enabled)
    
    _log(f"🎬 多版本模式(v2): AI直接出{num_versions}个独立叙事方案")
    
    # Step 1: 检查AI模式
    from ai_clipper import is_enabled as ai_is_enabled
    if not ai_is_enabled():
        _log("多版本需要AI模式，降级为单版本")
        return process_video(video_path, srt_path, output_path,
                           dedup_preset, subtitle_overlay, log_fn,
                           force_category, cancel_event,
                           pip_path, pip_size, pip_opacity, pip_pos,
                               smart_crop_enabled=smart_crop_enabled, crop_level=crop_level, ken_burns_enabled=ken_burns_enabled)
    
    # Step 2: 只跑ASR，不跑AI选片（AI留给多版本一次调用）
    global _multi_result_cache
    _multi_result_cache = {}
    
    _log("🎬 多版本: 运行ASR（跳过单版本AI选片）...")
    asr_result = process_video(video_path, srt_path, output_path,
                 dedup_preset, subtitle_overlay, log_fn,
                 force_category, cancel_event,
                 pip_path, pip_size, pip_opacity, pip_pos,
                 _asr_only=True,
                 smart_crop_enabled=smart_crop_enabled, crop_level=crop_level, ken_burns_enabled=ken_burns_enabled)
    
    _recorded_srt_text = _multi_result_cache.get('srt_text', '')
    
    # 保存SRT到固定文件，供后续版本复用
    _multi_srt_path = srt_path
    if not _recorded_srt_text:
        _log("ASR失败（无SRT文本），降级为单版本")
        return process_video(video_path, srt_path, output_path,
                           dedup_preset, subtitle_overlay, log_fn,
                           force_category, cancel_event,
                           pip_path, pip_size, pip_opacity, pip_pos,
                               smart_crop_enabled=smart_crop_enabled, crop_level=crop_level, ken_burns_enabled=ken_burns_enabled)
    if not _multi_srt_path:
        _multi_srt_path = os.path.join(
            os.path.dirname(video_path),
            f"_multi_version_{os.path.splitext(os.path.basename(video_path))[0]}.srt"
        )
        with open(_multi_srt_path, "w", encoding="utf-8") as f:
            f.write(_recorded_srt_text)
        _log(f"🎬 多版本: SRT已保存")
    
    # Step 3: 用AI多版本选片（直接出3个独立方案）
    if _recorded_srt_text:
        from ai_clipper import ai_analyze_multi_versions
        _log("🎬 多版本: AI重新选片（3个独立方案）...")
        multi_result = ai_analyze_multi_versions(_recorded_srt_text, log_fn=_log, force_category=force_category, focus_hint=focus_hint, num_versions=num_versions)
    else:
        multi_result = {"versions": []}
    versions_data = multi_result.get("versions", [])
    
    if not versions_data:
        _log("AI多版本选片失败，降级为旧方案（代码拆分）")
        # Fallback: 输出单版本
        _log("🎬 多版本: 选片失败，降级为单版本输出")
        return process_video(video_path, _multi_srt_path, output_path,
                           dedup_preset, subtitle_overlay, log_fn,
                           force_category, cancel_event,
                           pip_path, pip_size, pip_opacity, pip_pos,
                               smart_crop_enabled=smart_crop_enabled, crop_level=crop_level, ken_burns_enabled=ken_burns_enabled)
    
    if len(versions_data) < 1:
        _log("无有效版本，输出单版本")
        return process_video(video_path, _multi_srt_path, output_path,
                           dedup_preset, subtitle_overlay, log_fn,
                           force_category, cancel_event,
                           pip_path, pip_size, pip_opacity, pip_pos,
                               smart_crop_enabled=smart_crop_enabled, crop_level=crop_level, ken_burns_enabled=ken_burns_enabled)
    
    _log(f"🎬 多版本: AI输出 {len(versions_data)} 个方案")
    
    # Step 4: 每个版本单独裁切
    video_name = os.path.splitext(os.path.basename(video_path))[0]
    if output_path:
        output_dir = os.path.dirname(output_path)
    else:
        output_dir = os.path.join(os.path.dirname(video_path), "output")
    os.makedirs(output_dir, exist_ok=True)
    
    results = []
    for vi, ver in enumerate(versions_data):
        if cancel_event and cancel_event.is_set():
            break
        
        angle = ver.get("angle", f"方案{vi+1}")
        ver_clips = ver.get("clips", [])
        
        _log(f"\n🎬 === 版本 {vi+1}/{len(versions_data)} [{angle}] ===")
        for ct, text, s, e, sc, d, *_ in ver_clips:
            _log(f"  {ct:<16s} | {s:.1f}-{e:.1f}s ({d:.1f}s) | {text[:30]}")
        
        v_output = os.path.join(output_dir, f"{video_name}_切片_v{vi+1}.mp4")
        
        result = _process_version_with_clips(
            video_path, _multi_srt_path, v_output,
            ver_clips, dedup_preset, subtitle_overlay,
            log_fn, cancel_event,
            pip_path, pip_size, pip_opacity, pip_pos,
            smart_crop_enabled=smart_crop_enabled, crop_level=crop_level, ken_burns_enabled=ken_burns_enabled
        )
        results.append(result)
    
    _log(f"\n✅ 多版本输出完成: {len(results)} 个版本")
    
    # 清理临时 SRT 文件
    if _multi_srt_path and _multi_srt_path != srt_path:
        try:
            if os.path.exists(_multi_srt_path):
                os.remove(_multi_srt_path)
        except Exception:
            pass
    
    return {"ok": any(r.get("ok", False) if isinstance(r, dict) else r for r in results), "版本数": len(results)}


def _process_version_with_clips(video_path, srt_path, output_path,
                                 clips, dedup_preset="medium",
                                 subtitle_overlay=True, log_fn=None,
                                 cancel_event=None, pip_path=None,
                                 pip_size=0.15, pip_opacity=0.03, pip_pos="右下",
                                 smart_crop_enabled=True, crop_level="medium", ken_burns_enabled=True):
    """Process a single version with pre-determined clips (bypass AI selection)"""
    import time as _time
    from ai_clipper import is_enabled as ai_is_enabled
    
    def _log(msg):
        if log_fn: log_fn(msg)
    
    def _cancelled():
        return cancel_event and cancel_event.is_set()
    
    if _cancelled():
        return {"ok": False, "error": "cancelled"}
    
    # This is a simplified version of process_video that skips AI selection
    # and uses the provided clips directly
    # We need to call the internal cutting/dedup/subtitle logic
    
    # For now, we use a workaround: temporarily patch ai_analyze_clips to return our clips
    import ai_clipper as _ai
    _original_fn = _ai.ai_analyze_clips
    
    def _mock_analyze(*args, **kwargs):
        return clips
    
    _ai.ai_analyze_clips = _mock_analyze
    
    try:
        result = process_video(video_path, srt_path, output_path,
                              dedup_preset, subtitle_overlay, log_fn,
                              None, cancel_event,  # force_category=None (already filtered)
                              pip_path, pip_size, pip_opacity, pip_pos,
                               smart_crop_enabled=smart_crop_enabled, crop_level=crop_level, ken_burns_enabled=ken_burns_enabled)
        return result
    finally:
        _ai.ai_analyze_clips = _original_fn

