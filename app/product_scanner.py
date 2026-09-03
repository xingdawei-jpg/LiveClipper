"""
产品扫描模块 v1.0
独立功能：读取 SRT → AI 识别单品→ 校准 → 输出单品列表
不依赖 cutter_logic.py / gui.py 的主流程
"""

import logging
_LOG = logging.getLogger("liveclipper.product_scanner")

import os
import re
import json
import subprocess
import sys
import tempfile
import difflib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from srt_parser import open_srt, _time_to_seconds
from config import FFMPEG_PATH, sanitize_forbidden_title
from ai_model_config import (
    DEEPSEEK_DEFAULT_BASE_URL,
    DEEPSEEK_DEFAULT_MODEL,
    normalize_ai_base_url,
)
from ai_cost_ledger import record_ai_call


# ---------- SRT 工具 ----------

def _srt_to_text(srt_path):
    """读取 SRT 返回纯文本条目列表 [(index, start_sec, end_sec, text), ...]"""
    raw = open_srt(srt_path)
    if not raw:
        return []
    # open_srt 返回 (subtitles_list, encoding) 元组
    entries = raw[0] if isinstance(raw, (tuple, list)) and len(raw) == 2 else raw
    if not entries:
        return []
    result = []
    for e in entries:
        start = _time_to_seconds(e.start) if hasattr(e, 'start') else 0
        end = _time_to_seconds(e.end) if hasattr(e, 'end') else 0
        text = getattr(e, 'text', '') or ''
        text = text.strip().replace("\n", " ")
        idx = getattr(e, 'index', 0)
        result.append((idx, start, end, text))
    return result


def _format_srt_for_prompt(entries):
    """将 SRT 条目压缩为紧凑格式：[分:秒] 文本"""
    lines = []
    for idx, start, end, text in entries:
        start_m = int(start // 60)
        start_s = int(start % 60)
        end_m = int(end // 60)
        end_s = int(end % 60)
        lines.append(f"[{start_m:02d}:{start_s:02d}-{end_m:02d}:{end_s:02d}] {text}")
    return "\n".join(lines)


def _calibrate_with_srt(entries, ai_result, log_fn=None):
    """用 SRT 原文校准 AI 返回的单品时间范围
    ai_result: [{name, start, end, keywords, confidence}, ...]
    """
    if not ai_result:
        return []

    # 构建 SRT 文本索引：关键词 → 匹配的条目
    calibrated = []
    for item in ai_result:
        name = item.get("name", "").strip()
        if not name:
            continue
        keywords = item.get("keywords", [name])
        confidence = item.get("confidence", "low")

        # 通过关键词在 SRT 中查找匹配条目，只在AI窗口+5秒范围内搜索
        matched = []
        ai_start = item.get("start", 0)
        ai_end = item.get("end", 0)
        search_start = max(0, ai_start - 5)
        search_end = ai_end + 5
        for idx, start, end, text in entries:
            if start > search_end or end < search_start:
                continue
            for kw in keywords:
                if kw and kw in text:
                    matched.append((start, end, text))
                    break

        if matched:
            # 取第一个匹配的起始时间和最后一个匹配的结束时间
            cal_start = min(m[0] for m in matched)
            cal_end = max(m[1] for m in matched)
            calibrated.append({
                "name": name,
                "start": cal_start,
                "end": cal_end,
                "confidence": confidence,
                "keyword_hits": len(matched),
            })
        else:
            # 校准失败，用 AI 返回的原始值（如果有）
            calibrated.append({
                "name": name,
                "start": item.get("start", 0),
                "end": item.get("end", 0),
                "confidence": "low",
                "keyword_hits": 0,
            })

    # 合并同名且相隔很近的单品
    if len(calibrated) > 1:
        calibrated = _merge_same_name_products(calibrated, log_fn)
    return calibrated


def _snap_end_to_entry_boundary(entries, calibrated):
    """检查每个单品的 end 是否切断了一条 SRT 条目，如果是则对齐到该条目末尾
    entries: [(index, start_sec, end_sec, text), ...] 按时间升序
    calibrated: [{name, start, end, ...}, ...]
    """
    if not entries or not calibrated:
        return calibrated

    for item in calibrated:
        cal_end = item["end"]
        # 找到 cal_end 落在哪条条目内（排除端点：刚好在条目末尾不算切断）
        for idx, e_start, e_end, e_text in entries:
            if e_start < cal_end < e_end:
                # cal_end 在这条条目中间，对齐到该条目末尾
                item["end"] = e_end
                break

    return calibrated



def _call_ai_products(srt_text, api_key, base_url, model):
    """Single-shot AI analysis: read all SRT, find product transitions, output segments."""
    import http.client as hc
    import json as _json
    sys_prompt = (
        "你是一个直播带货视频分析助手。我给你一段直播录像的SRT字幕（约30分钟）。\n"
        "这段录像是直播中随机截取的一段，开头可能切在一个商品中间，结尾也可能切在商品中间。\n"
        "你的任务是：通读SRT，找到每一个商品精讲时段，准确标出每个商品的开始和结束时间。\n\n"
        "### 切换信号（有任一个就是新商品开始）\n"
        "- **品类词切换（最硬信号）**：之前讲裤子的内容里出现\"这件上衣\"\"袖子的设计\"\"领子\"\"下摆\"→切换到上衣。品类变了，商品一定变了\n"
        "- **切换语**：\"那这个/然后这个/来看一下/还有一个/来换/换款/换一件/讲到下一个/再讲一下/下一个\" + 新品类词\n"
        "- **\"裤子先到这儿/上衣先到这儿/这个讲完了\"**等结束语+新商品介绍\n"
        "- **上链接/改价/抢购/拍好了**之后开始讲新商品（直播间节奏：介绍→上链接→下一件）\n"
        "- **主播长时间未提当前商品关键词**：如果30秒以上没提当前商品名称/品类词，可能已经切换到别的了\n\n"
        "### 注意事项\n"
        "- **不准限制时长**：主播一个品可能讲3-40分钟甚至更久，中间偶尔穿插几分钟讲别的品是正常的\n"
        "- **3分钟以下的小单品要保留**：直播间可能有快速过款，几秒到几分钟不等\n"
        "- **focus商品为主**：主播展示配搭时（穿上衣配裤子），以正在讲的那个为主，另一个算配搭关键词\n"
        "- **暖场/闲聊/福袋/抽奖/感谢大家/等待**等无商品内容的时间不归属任何商品，跳过\n"
        "- **宁多勿漏**：不确定的给合理名称+confidence为low\n\n"
        "输出格式（严格JSON数组，不要markdown代码块，不要推理文字）：\n"
        "[{\"name\":\"商品名称\",\"start\":开始秒数,\"end\":结束秒数,\"confidence\":\"high/medium/low\",\"keywords\":[\"关键词1\",\"关键词2\"]}]\n"
        "start/end以视频开头为0秒，取每个商品精讲时段"
    )
    user_prompt = "分析以下直播SRT字幕，识别所有单品及其时间范围：\n%s\n返回JSON数组" % srt_text
    payload = {"model": model, "messages": [{"role":"system","content":sys_prompt},{"role":"user","content":user_prompt}], "temperature":0.1, "max_tokens":8192}
    body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
    base_url = normalize_ai_base_url(base_url)
    parts = base_url.replace("https://","").replace("http://","").split("/")
    host = parts[0]; path_api = "/" + "/".join(parts[1:]) if len(parts)>1 else ""; path_api += "/chat/completions"
    try:
        conn = hc.HTTPSConnection(host, timeout=180)
        conn.request("POST", path_api, body=body, headers={"Content-Type":"application/json","Authorization":"Bearer "+api_key})
        resp = conn.getresponse(); data = resp.read().decode("utf-8"); conn.close()
        result = _json.loads(data)
        record_ai_call(
            module="product_scanner", stage="product_scan_full", model=model,
            request_payload=payload, response_payload=result, success=True,
        )
        if "choices" not in result or not result["choices"]: return []
        content = result["choices"][0]["message"].get("content","").strip()
        if content.startswith("```"): content = re.sub(r"^```(?:json)?\s*","",content); content = re.sub(r"\s*```$","",content)
        parsed = _json.loads(content)
        if isinstance(parsed, list): return parsed
        return []
    except Exception as e:
        record_ai_call(
            module="product_scanner", stage="product_scan_full", model=model,
            request_payload=payload, success=False, error_type=type(e).__name__,
        )
        print("_call_ai_products error:", e, flush=True)
        return []

def _call_ai(srt_text, api_key, base_url, model):
    """调用 AI 分析 SRT，返回单品列表"""
    import http.client
    import json

    system_prompt = """你是一个直播带货讲解分析助手。
任务：分析直播录像的 SRT 字幕，识别每个"单品"（正在讲解的商品）的名称、开始时间和结束时间。

输出要求：
1. 只输出合法的 JSON 数组，不要 markdown 代码块包裹，不要任何额外文字
2. 每件商品格式：{"name": "商品名称", "start": 开始秒数, "end": 结束秒数, "keywords": ["关键词1","关键词2"]}
3. start/end 以视频开头为 0 秒，单位秒（整数）
4. keywords 是从 SRT 原文中提取的 2-5 个能定位该商品的关键词
5. 同一个商品被多次提到时，合并为一个条目，取最早 start 和最晚 end
6. 如果一段对话描述一个商品，但名字没有直接出现，给一个合理的名称并标注 confidence 为 "low"
7. SRT 中的重复轮播属于同一个商品，合并为一条
8. 不要输出任何非 JSON 内容，不要推理过程"""

    user_prompt = f"""分析以下直播 SRT 字幕，识别所有单品及其时间范围：

SRT:
{srt_text}

返回 JSON 数组，格式：[{{"name":"商品名","start":秒数,"end":秒数,"keywords":["关键词"]}}]"""

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 4096,
    }

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    # 解析 base_url
    base_url = normalize_ai_base_url(base_url)
    url_parts = base_url.replace("https://", "").replace("http://", "").split("/")
    host = url_parts[0]
    path = "/" + "/".join(url_parts[1:]) if len(url_parts) > 1 else ""
    path += "/chat/completions"

    try:
        conn = http.client.HTTPSConnection(host, timeout=120)
        conn.request("POST", path, body=body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        })
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        conn.close()

        result = json.loads(data)
        record_ai_call(
            module="product_scanner", stage="product_scan_legacy", model=model,
            request_payload=payload, response_payload=result, success=True,
        )
        if "choices" not in result or not result["choices"]:
            return []

        content = result["choices"][0].get("message", {}).get("content", "")

        # 清理 markdown 代码块包裹
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)

        parsed = json.loads(content)
        if isinstance(parsed, list):
            return parsed
        return []
    except Exception as error:
        record_ai_call(
            module="product_scanner", stage="product_scan_legacy", model=model,
            request_payload=payload, success=False, error_type=type(error).__name__,
        )
        return []


# ---------- 公开接口 ----------



# ---- Segment scan helpers ----

def _segment_entries(entries, max_sec=600):
    if not entries:
        return []
    segments = []
    seg_start = entries[0][1]
    seg_idx = 0
    current = []
    for e in entries:
        if e[1] - seg_start >= max_sec and current:
            remaining = len(entries) - len(current)
            if remaining < 5:
                current.append(e)
            else:
                segments.append((seg_idx, seg_start, e[1], current))
                seg_idx += 1
                seg_start = e[1]
                current = [e]
        else:
            current.append(e)
    if current:
        segments.append((seg_idx, seg_start, current[-1][2], current))
    return segments


def _call_ai_segment(srt_text_segment, seg_start, api_key, base_url, model):
    import http.client as hc
    sys_ = (
        "你是一个直播带货讲解分析助手。任务：分析一段直播SRT字幕，识别其中所有单品（正在讲解的商品）。\n\n"
        "判断规则：\n"
        "1. 商品切换信号：主播说来换/换款/换一下/然后这个/那这件/来开这个/还有这个等，说明开始讲新商品\n"
        "2. 品类关键词：T恤/衬衫/裙子/裤子/上衣/外套等品类词出现，说明正在讲什么\n"
        "3. 价格信号：到手XX/XX块钱/改价/上链接等往往伴随商品讲解高潮\n"
        "4. **时间范围必须精准**：只取精讲该商品的时段，不要覆盖到其他商品。宁可短一点，不要包含别的内容。\n"
        "   例如：主播先讲裙子5分钟，又说[好，然后这件T恤]，裙子end应在[然后]之前\n"
        "5. 如果本段没有明确的商品讲解（暖场/闲聊），返回空数组 []\n\n"
        "输出规则：1.只输出JSON数组，不要markdown代码块，不要推理文字 "
        "2.每个条目格式：{\"name\":\"商品名称\",\"start\":开始秒,\"end\":结束秒,\"confidence\":\"high/medium/low\",\"keywords\":[\"关键词\"]} "
        "3.start/end以本段开头为0秒 4.关键词必须是SRT原文出现过的词 "
        "5.宁多勿漏：不确定品类时给合理名称并设confidence为low"
    )
    user_ = "分析以下段（从%d秒开始），识别所有单品：\n%s\n返回JSON数组" % (seg_start, srt_text_segment)
    payload = {"model": model, "messages": [{"role":"system","content":sys_},{"role":"user","content":user_}], "temperature":0.1, "max_tokens":4096}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    base_url = normalize_ai_base_url(base_url)
    parts = base_url.replace("https://","").replace("http://","").split("/")
    host = parts[0]; path = "/" + "/".join(parts[1:]) if len(parts)>1 else ""; path += "/chat/completions"
    try:
        conn = hc.HTTPSConnection(host, timeout=120)
        conn.request("POST", path, body=body, headers={"Content-Type":"application/json","Authorization":"Bearer "+api_key})
        resp = conn.getresponse(); data = resp.read().decode("utf-8"); conn.close()
        result = json.loads(data)
        record_ai_call(
            module="product_scanner", stage="product_scan_segment", model=model,
            request_payload=payload, response_payload=result, success=True,
        )
        if "choices" not in result or not result["choices"]: return []
        content_ = result["choices"][0]["message"].get("content","").strip()
        if content_.startswith("```"): content_ = re.sub(r"^```(?:json)?\s*","",content_); content_ = re.sub(r"\s*```$","",content_)
        parsed = json.loads(content_)
        if isinstance(parsed, list):
            for item in parsed:
                if "start" in item and item["start"] is not None: item["start"] += seg_start
                if "end" in item and item["end"] is not None: item["end"] += seg_start
            return parsed
        return []
    except Exception as error:
        record_ai_call(
            module="product_scanner", stage="product_scan_segment", model=model,
            request_payload=payload, success=False, error_type=type(error).__name__,
        )
        return []
def _merge_products(seg_products_list):
    merged = {}
    for seg_products in seg_products_list:
        for p in seg_products:
            name = p.get("name","").strip()
            if not name: continue
            if name in merged:
                ex = merged[name]
                ex["start"] = min(ex.get("start",99999), p.get("start",0))
                ex["end"] = max(ex.get("end",0), p.get("end",0))
                cr = {"none":0,"low":1,"medium":2,"high":3}
                nc = p.get("confidence","low")
                if cr.get(nc,0) > cr.get(ex.get("confidence","low"),0): ex["confidence"]=nc
                kws = set(ex.get("keywords",[]))
                for kw in p.get("keywords",[]): kws.add(kw)
                ex["keywords"] = list(kws)
            else:
                merged[name] = copy.deepcopy(p)
    result = sorted(merged.values(), key=lambda x: x.get("start",0))
    for i in range(len(result)-1):
        c = result[i]; n = result[i+1]; ce = c.get("end",0); ns = n.get("start",0)
        if ce > ns and c.get("name") != n.get("name"):
            ov = ce - ns; cd = ce - c.get("start",0)
            if cd > 0 and ov/cd > 0.3: c["end"] = ns
    return result





def _get_concat_encoder():
    """返回拼接用的视频编码参数，优先硬件加速（QSV > AMF > NVENC > libx264）"""
    try:
        from platform_config import HARDWARE_ENCODER
        he = HARDWARE_ENCODER
        if he == "h264_qsv":
            return ["-c:v", "h264_qsv", "-preset", "fast", "-global_quality", "22"]
        elif he == "h264_amf":
            return ["-c:v", "h264_amf", "-quality", "speed", "-qp", "22"]
        elif he == "h264_nvenc":
            return ["-c:v", "h264_nvenc", "-preset", "p1", "-qp", "22"]
    except Exception:
        _LOG.warning("unexpected error", exc_info=True)
        pass
    return ["-c:v", "libx264", "-preset", "fast", "-crf", "23"]


def _merge_same_name_products(calibrated, log_fn=None):
    """Merge same-named products into one entry with multiple time ranges.
    E.g. 天丝人丝小斗篷上衣 792-848s + 1021-1622s → single entry with two ranges.
    Middle products are kept independent (not merged into the range)."""
    if not calibrated:
        return calibrated

    # Group by name
    groups = {}
    order = []
    for item in calibrated:
        name = item.get("name", "")
        if name not in groups:
            groups[name] = []
            order.append(name)
        groups[name].append(item)

    merged = []
    for name in order:
        items = groups[name]
        if len(items) == 1:
            merged.append(items[0])
        else:
            # Merge: keep all segments, mark as multi-range
            ranges = [(i.get("start", 0), i.get("end", 0)) for i in items]
            start = min(r[0] for r in ranges)
            end = max(r[1] for r in ranges)
            best = max(items, key=lambda x: x.get("confidence", 0) if isinstance(x.get("confidence"), (int, float)) else 0)
            merged.append({
                "name": name,
                "start": start,
                "end": end,
                "segments": ranges,  # list of (start, end) tuples
                "confidence": best.get("confidence", "medium"),
                "keyword_hits": max(i.get("keyword_hits", 0) for i in items),
            })
            if log_fn:
                ranges_str = ", ".join("%d-%d" % r for r in ranges)
                log_fn("  合并同名单品: %s (%s)" % (name, ranges_str))

    return merged


def merge_across_files(all_file_products, log_fn=None):
    """
    跨文件合并同品名产品。

    all_file_products: [
        [{"name":"校服裤","start":0,"end":231,"segments":[(0,231)],"keyword_hits":3,"_video":".../段1.mp4"}, ...],
        [{"name":"校服裤","start":0,"end":180,"segments":[(0,180)],"keyword_hits":5,"_video":".../段2.mp4"}, ...],
    ]

    返回: [
        {"name":"校服裤", "segments":[("段1.mp4",0,231),("段2.mp4",0,180),...],
         "total_duration":411, "source_count":2, "keyword_hits":8},
        ...
    ]
    按总时长降序排列。
    """
    if not all_file_products:
        return []

    # 将所有产品的名称标准化（去空格、繁体转简体等）
    import unicodedata
    def norm(name):
        return unicodedata.normalize('NFKC', name).strip().lower()

    # 按标准名分组
    groups = {}  # norm_name -> {original_name, sources:[], total_dur, keyword_hits, segments:[(video,start,end)]}
    order = []

    for file_idx, products in enumerate(all_file_products):
        if isinstance(products, str):
            continue
        for p in products:
            if not isinstance(p, dict):
                continue
            name = p.get("name", "")
            if not name:
                continue
            key = norm(name)

            if key not in groups:
                groups[key] = {
                    "name": name,
                    "segments": [],
                    "total_duration": 0,
                    "keyword_hits": 0,
                    "source_count": 0,
                }
                order.append(key)

            g = groups[key]
            # Use the first-seen original name
            if key == norm(g["name"]) and len(name) > len(g["name"]):
                g["name"] = name

            video = p.get("_video", "")
            kw = p.get("keyword_hits", 0) or 0

            # Get ranges
            segments = p.get("segments", None)
            if segments:
                for s_start, s_end in segments:
                    g["segments"].append((video, s_start, s_end))
                    g["total_duration"] += (s_end - s_start)
            else:
                s_start = p.get("start", 0)
                s_end = p.get("end", 0)
                if s_end > s_start:
                    g["segments"].append((video, s_start, s_end))
                    g["total_duration"] += (s_end - s_start)

            g["keyword_hits"] += kw
            g["source_count"] += 1

    # Build result, sorted by total_duration descending
    result = []
    for key in order:
        g = groups[key]
        result.append({
            "name": g["name"],
            "segments": g["segments"],
            "total_duration": round(g["total_duration"], 1),
            "keyword_hits": g["keyword_hits"],
            "source_count": g["source_count"],
            "confidence": "high" if g["keyword_hits"] > 3 else "medium",
        })

    result.sort(key=lambda x: x["total_duration"], reverse=True)

    if log_fn:
        log_fn(f"跨文件合并完成: {len(result)} 个单品 (来自 {len(all_file_products)} 个文件)")
        for r in result[:5]:
            log_fn(f"  {r['name']}: {r['total_duration']:.0f}s, {r['source_count']}次出现")
        if len(result) > 5:
            log_fn(f"  ...还有 {len(result)-5} 个")

    return result


def _norm_base_name(name):
    import re
    n = name
    # Remove () brackets and their content
    n = re.sub(r'\([^)]*\)', '', n)
    n = re.sub(r'[【-】（-）\[\]〔〕]', '', n)
    # Remove 第X波/次/轮/段/批 patterns (first/second/third waves/rounds)
    import unicodedata
    digits = '一二三四五六七八九十'
    n = re.sub(r'第[' + digits + r']+波', '', n)
    n = re.sub(r'第[' + digits + r']+次', '', n)
    n = re.sub(r'第[' + digits + r']+轮', '', n)
    n = re.sub(r'第[' + digits + r']+段', '', n)
    n = re.sub(r'第[' + digits + r']+批', '', n)
    n = re.sub(r'\([^)]*\)', '', n)  # again in case stripping revealed more
    return n.strip().lower()

def fuzzy_merge_products(products, log_fn=None, threshold=0.6, max_gap=30):
    if not products:
        return []

    with_base = [(p, _norm_base_name(p.get("name", ""))) for p in products]
    used = set()
    merged_groups = []
    unmatched = []

    for i, (p, base_i) in enumerate(with_base):
        if i in used:
            continue
        group = [i]
        used.add(i)
        for j, (q, base_j) in enumerate(with_base):
            if j in used:
                continue
            if not base_i or not base_j:
                continue
            ratio = difflib.SequenceMatcher(None, base_i, base_j).ratio()
            if ratio >= threshold:
                group.append(j)
                used.add(j)
        if len(group) > 1:
            merged_groups.append(group)
        else:
            unmatched.append(i)

    merged = []
    for group_indices in merged_groups:
        items = [with_base[i][0] for i in group_indices]
        longest = max(items, key=lambda x: len(x.get("name", "")))
        base = _norm_base_name(longest.get("name", ""))

        all_segs = []
        start = float('inf')
        end = 0
        best_conf = 0
        best_kw = 0
        video = None
        for item in items:
            s = item.get("start", 0)
            e = item.get("end", 0)
            all_segs.append((s, e))
            start = min(start, s)
            end = max(end, e)
            c = item.get("confidence", 0)
            if isinstance(c, (int, float)) and c > best_conf:
                best_conf = c
            kw = item.get("keyword_hits", 0)
            if kw > best_kw:
                best_kw = kw
            if item.get("_video"):
                video = item.get("_video")

        all_segs.sort()
        merged_segs = []
        for s, e in all_segs:
            if merged_segs and s - merged_segs[-1][1] < max_gap:
                merged_segs[-1] = (merged_segs[-1][0], max(merged_segs[-1][1], e))
            else:
                merged_segs.append((s, e))

        entry = {
            "name": longest.get("name", ""),
            "start": start,
            "end": end,
            "segments": merged_segs,
            "confidence": best_conf if best_conf else longest.get("confidence", "medium"),
            "keyword_hits": best_kw,
            "_merged": True,
            "_display_name": longest.get("name","") + " (merge)",
            "_merged_count": len(items),
        }
        if video:
            entry["_video"] = video
        merged.append(entry)

        if log_fn:
            log_fn("  fuzzy merge: %s (%d items, %d segs)" % (
                longest.get("name", "")[:30], len(items), len(merged_segs)))

    for idx in unmatched:
        item = with_base[idx][0]
        dur = item.get("end", 0) - item.get("start", 0)
        if dur < 60:
            if log_fn:
                log_fn("  drop short: %s (%.0fs)" % (item.get("name", "")[:30], dur))
            continue
        merged.append(item)

    merged.sort(key=lambda x: x.get("start", 0))
    return merged

class ProductScanner:
    """产品扫描器"""

    def __init__(self, api_key=None, base_url=None, model=None):
        self.api_key = api_key
        self.base_url = normalize_ai_base_url(base_url or DEEPSEEK_DEFAULT_BASE_URL)
        self.model = model or DEEPSEEK_DEFAULT_MODEL

    def scan(self, srt_path, log_fn=None):
        """Analyze SRT as a whole: AI reads all SRT, finds product switches, outputs segments."""
        entries = _srt_to_text(srt_path)
        if not entries:
            if log_fn: log_fn("SRT为空")
            return []

        srt_text = _format_srt_for_prompt(entries)
        if not srt_text.strip():
            if log_fn: log_fn("SRT内容为空")
            return []

        if log_fn:
            max_sec = max(e[2] for e in entries)
            log_fn("SRT总时长 %.0fs，共 %d 条，正在AI分析..." % (max_sec, len(entries)))

        # Use single-shot AI with the new detailed prompt
        ai_result = _call_ai_products(srt_text, self.api_key, self.base_url, self.model)

        if not ai_result:
            # 重试一次（AI 偶发空结果）
            if log_fn: log_fn("AI未识别到单品，重试一次...")
            import time as _t
            _t.sleep(2)
            ai_result = _call_ai_products(srt_text, self.api_key, self.base_url, self.model)

        if not ai_result:
            if log_fn: log_fn("AI未识别到单品")
            return []

        # Calibrate with SRT (only within AI window +5s)
        calibrated = _calibrate_with_srt(entries, ai_result, log_fn)
        calibrated = _snap_end_to_entry_boundary(entries, calibrated)

        if log_fn: log_fn("共识别 %d 个单品" % len(calibrated))
        return calibrated

    def extract_clip(self, video_path, product, output_dir, output_name=None):
        """Cut product segment. Split into 15min chunks if too long.
        Multi-segment products: extract each segment, concat into one file.
        Returns list of paths (single file for multi-segment products)."""
        if not os.path.exists(video_path):
            return []
        os.makedirs(output_dir, exist_ok=True)
        name = sanitize_forbidden_title(output_name or product.get("name", "product"), fallback="product")
        safe_name = re.sub(r"[" + chr(92) + r'/:*?"<>|]', "_", name)
        short_name = safe_name[:40]
        ffmpeg = FFMPEG_PATH or "ffmpeg"
        MAX_SEG = 900

        # Get ranges: multi-segment or single
        segments = product.get("segments", None)
        if segments:
            ranges = [(max(0, s[0] - 0.5), s[1] + 0.5) for s in segments]
        else:
            ranges = [(max(0, product.get("start", 0) - 0.5), product.get("end", 0) + 0.5)]

        # If multi-segment: extract each part to temp, concat into final file
        output_paths = []
        if len(ranges) > 1:
            import tempfile
            tmp_dir = os.path.join(output_dir, ".concat_tmp_" + short_name)
            os.makedirs(tmp_dir, exist_ok=True)
            tmp_files = []
            out = ""
            try:
                for si, (st, ed) in enumerate(ranges):
                    dur = ed - st
                    if dur < 1:
                        continue
                    tmp = os.path.join(tmp_dir, "part_%d.mp4" % si)
                    try:
                        subprocess.run([ffmpeg, "-y", "-ss", str(st), "-i", video_path, "-to", str(dur),
                            "-c", "copy", tmp],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                        if os.path.exists(tmp) and os.path.getsize(tmp) > 1000:
                            tmp_files.append(tmp)
                    except Exception:
                        _LOG.warning("unexpected error", exc_info=True)
                        pass

                if len(tmp_files) >= 2:
                    concat_list = os.path.join(tmp_dir, "list.txt")
                    with open(concat_list, "w", encoding="utf-8") as f:
                        for tmpf in tmp_files:
                            f.write("file '%s'\n" % tmpf.replace(chr(92), "/"))

                    out = os.path.join(output_dir, short_name + ".mp4")
                    if os.path.exists(out):
                        base, ext = os.path.splitext(out)
                        idx_num = 1
                        while os.path.exists(base + "_" + str(idx_num) + ext):
                            idx_num += 1
                        out = base + "_" + str(idx_num) + ext
                    try:
                        cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", concat_list, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-c:a", "aac", "-b:a", "128k", out]
                        subprocess.run(cmd,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                        if os.path.exists(out) and os.path.getsize(out) > 1000:
                            import shutil
                            shutil.rmtree(tmp_dir, ignore_errors=True)
                            return [out]
                    except Exception:
                        _LOG.warning("unexpected error", exc_info=True)
                        pass

                # Concat failed or only 1 part: fall through to return individual parts
                # (tmp files stay as output)
                for tmpf in tmp_files:
                    # Move to output dir
                    import shutil
                    dst = os.path.join(output_dir, os.path.basename(tmpf))
                    if os.path.exists(dst):
                        base, ext = os.path.splitext(dst)
                        idx_num = 1
                        while os.path.exists(base + "_" + str(idx_num) + ext):
                            idx_num += 1
                        dst = base + "_" + str(idx_num) + ext
                    shutil.move(tmpf, dst)
                    output_paths.append(dst)
                try:
                    import shutil
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception:
                    _LOG.warning("unexpected error", exc_info=True)
                    pass
                return output_paths or ([out] if out and os.path.exists(out) else [])

            except Exception:
                try:
                    import shutil
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception:
                    _LOG.warning("unexpected error", exc_info=True)
                    pass

        # Single segment or fallback
        output_paths = []
        for st, ed in [ranges[0]] if ranges else []:
            duration = ed - st
            if duration <= MAX_SEG:
                out = os.path.join(output_dir, short_name + ".mp4")
                if os.path.exists(out):
                    base, ext = os.path.splitext(out)
                    idx_num = 1
                    while os.path.exists(base + "_" + str(idx_num) + ext):
                        idx_num += 1
                    out = base + "_" + str(idx_num) + ext
                try:
                    subprocess.run([ffmpeg, "-y", "-ss", str(st), "-i", video_path, "-to", str(duration),
                        "-c", "copy", out],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if os.path.exists(out) and os.path.getsize(out) > 1000:
                        output_paths.append(out)
                except Exception:
                    _LOG.warning("unexpected error", exc_info=True)
                    pass
            else:
                seg_count = int(duration // MAX_SEG) + (1 if duration % MAX_SEG > 30 else 0)
                for seg_i in range(seg_count):
                    chunk_start = st + seg_i * MAX_SEG
                    chunk_dur = min(MAX_SEG, ed - chunk_start)
                    if chunk_dur < 30:
                        continue
                    out = os.path.join(output_dir, "%s_%d.mp4" % (short_name, seg_i + 1))
                    if os.path.exists(out):
                        os.remove(out)
                    try:
                        subprocess.run([ffmpeg, "-y", "-ss", str(chunk_start), "-i", video_path, "-to", str(chunk_dur),
                            "-c", "copy", out],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                        if os.path.exists(out) and os.path.getsize(out) > 1000:
                            output_paths.append(out)
                    except Exception:
                        _LOG.warning("unexpected error", exc_info=True)
                        pass
        return output_paths
    def extract_all(self, video_path, products, output_dir):
        """Batch extract products. Each may have multiple segments."""
        results = []
        for p in products:
            paths = self.extract_clip(video_path, p, output_dir)
            results.append({
                "name": p.get("name", "?"),
                "start": p.get("start", 0),
                "end": p.get("end", 0),
                "output_paths": paths,
                "segment_count": len(paths),
            })
        return results

    def extract_cross_file(self, merged_products, output_dir, log_fn=None):
        """跨文件导出合并后的产品"""
        import re, subprocess, os as _os
        ffmpeg = "ffmpeg"
        results = []
        for prod in merged_products:
            name = sanitize_forbidden_title(prod.get("name", "product"), fallback="product")
            safe = re.sub(r'[\\/:*?"<>|]', "_", name)[:40]
            segments = prod.get("segments", [])
            if not segments:
                continue
            out_path = _os.path.join(output_dir, safe + ".mp4")
            if _os.path.exists(out_path):
                base, ext = _os.path.splitext(out_path)
                idx_num = 1
                while _os.path.exists(base + "_" + str(idx_num) + ext):
                    idx_num += 1
                out_path = base + "_" + str(idx_num) + ext
            tmp_dir = _os.path.join(output_dir, ".concat_tmp_" + safe)
            _os.makedirs(tmp_dir, exist_ok=True)
            tmp_files = []
            try:
                for si, (video, st, ed) in enumerate(segments):
                    if not _os.path.exists(video):
                        continue
                    dur = ed - st
                    if dur < 1:
                        continue
                    tmp = _os.path.join(tmp_dir, "part_%d.mp4" % si)
                    try:
                        subprocess.run([ffmpeg, "-y", "-ss", str(st), "-i", video,
                            "-to", str(dur), "-c", "copy", tmp],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                        if _os.path.exists(tmp) and _os.path.getsize(tmp) > 1000:
                            tmp_files.append(tmp)
                    except Exception:
                        _LOG.warning("unexpected error", exc_info=True)
                        pass
                if len(tmp_files) >= 2:
                    concat_list = _os.path.join(tmp_dir, "list.txt")
                    with open(concat_list, "w", encoding="utf-8") as f:
                        for tmpf in tmp_files:
                            f.write("file '%s'\n" % tmpf.replace(chr(92), "/"))
                    subprocess.run([ffmpeg, "-y", "-f", "concat", "-safe", "0",
                        "-i", concat_list, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-c:a", "aac", "-b:a", "128k", out_path],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                elif len(tmp_files) == 1:
                    import shutil
                    shutil.copy2(tmp_files[0], out_path)
                else:
                    continue
                if _os.path.exists(out_path) and _os.path.getsize(out_path) > 1000:
                    results.append({"name": name, "output_path": out_path,
                                    "size_mb": round(_os.path.getsize(out_path)/1024/1024, 1),
                                    "segment_count": len(tmp_files)})
                    if log_fn: log_fn("  " + name + ": " + str(results[-1]['size_mb']) + "MB (" + str(len(tmp_files)) + "\u6bb5)")
                for f in tmp_files:
                    try: _os.remove(f)
                    except: pass
                try: _os.rmdir(tmp_dir)
                except: pass
            except Exception as e:
                if log_fn: log_fn("  " + name + ": " + str(e))
        return results

# ---------- 独立入口 ----------

if __name__ == "__main__":
    # 测试用法
    import sys
    if len(sys.argv) >= 2:
        srt_file = sys.argv[1]
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        scanner = ProductScanner(api_key=api_key)
        products = scanner.scan(srt_file)
        print(json.dumps(products, ensure_ascii=False, indent=2))
    else:
        print("用法: python product_scanner.py <srt_path>")
