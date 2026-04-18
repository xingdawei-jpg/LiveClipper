"""
用字符比例估算收紧AI选片的时间范围
原理: 如果SRT条目很长(>5s),AI只选了其中一部分文字,
按字符位置比例估算对应的精确时间
"""
import re

def tighten_clip_boundaries(clips, srt_text, log_fn=None):
    def _log(msg):
        if log_fn:
            log_fn(msg)

    if not srt_text or not clips:
        return clips

    # 解析SRT
    srt_entries = []
    for block in srt_text.strip().split('\n\n'):
        lines = block.strip().split('\n')
        if len(lines) < 3:
            continue
        time_line = lines[1]
        text = ' '.join(lines[2:]).strip()
        m = re.match(r'(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)', time_line)
        if m:
            start_s = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3)) + int(m.group(4))/1000
            end_s = int(m.group(5))*3600 + int(m.group(6))*60 + int(m.group(7)) + int(m.group(8))/1000
            srt_entries.append((start_s, end_s, text))

    if not srt_entries:
        return clips

    PUNCT = set('.,!? \u3002\uff0c\uff01\uff1f\u3001\uff1b\uff1a\u201c\u201d\u2018\u2019\uff08\uff09\u3010\u3011')

    def _clean(t):
        return ''.join(c for c in t if c not in PUNCT)

    def _find_sub_time(srt_start, srt_end, srt_text_clean, ai_text_clean, position='start'):
        """在一条长SRT条目中，按字符比例估算AI文本的起止时间"""
        srt_dur = srt_end - srt_start
        srt_len = len(srt_text_clean)
        if srt_len < 2 or srt_dur < 2:
            return srt_start if position == 'start' else srt_end

        if position == 'start':
            # 找AI文本开头在SRT文本中的位置
            kw = ai_text_clean[:min(6, len(ai_text_clean))]
            idx = srt_text_clean.find(kw)
            if idx < 0:
                # 尝试更短关键词
                kw = ai_text_clean[:3]
                idx = srt_text_clean.find(kw)
            if idx >= 0:
                ratio = idx / srt_len
                return srt_start + ratio * srt_dur
            return srt_start
        else:  # end
            kw = ai_text_clean[-min(6, len(ai_text_clean)):]
            idx = srt_text_clean.rfind(kw)
            if idx < 0:
                kw = ai_text_clean[-3:]
                idx = srt_text_clean.rfind(kw)
            if idx >= 0:
                ratio = (idx + len(kw)) / srt_len
                ratio = min(ratio, 1.0)
                return srt_start + ratio * srt_dur
            return srt_end

    tightened = []
    for clip in clips:
        c_type, c_text, c_start, c_end, c_score, c_dur = clip[:6]
        clean_ai = _clean(c_text)
        # Hook片段不做任何边界调整
        if 'hook' in c_type.lower():
            tightened.append(clip)
            continue


        # 找与片段重叠的SRT条目
        in_range = [(s, e, t) for s, e, t in srt_entries
                    if s < c_end + 1 and e > c_start - 1]

        if not in_range:
            tightened.append(clip)
            continue

        new_start = c_start
        new_end = c_end

        # 第一条SRT：可能需要收紧start
        first_srt = in_range[0]
        first_clean = _clean(first_srt[2])
        if first_srt[0] < c_start + 0.3 and len(first_clean) > 10 and c_start < first_srt[1]:
            est = _find_sub_time(first_srt[0], first_srt[1], first_clean, clean_ai, 'start')
            if est > c_start + 0.5:
                new_start = est

        # 最后一条SRT：可能需要收紧end
        last_srt = in_range[-1]
        last_clean = _clean(last_srt[2])
        if last_srt[1] > c_end - 0.3 and len(last_clean) > 10 and c_end > last_srt[0]:
            est = _find_sub_time(last_srt[0], last_srt[1], last_clean, clean_ai, 'end')
            if est < c_end - 0.5 and est > new_start:
                new_end = est
        # Close片段：跳过start/end收紧，只加+0.5s防吃字缓冲
        if 'close' in c_type.lower():
            # 找与片段重叠的SRT条目
            _close_range = [(s, e, t) for s, e, t in srt_entries
                          if s < c_end + 1 and e > c_start - 1]
            if _close_range:
                extend_end = c_end + 0.5
                extend_srts = [(s, e, t) for s, e, t in _close_range
                              if s < extend_end and e > c_end]
                _skip_extend = False
                try:
                    from config import ALL_CATEGORIES
                    for _s, _e, _t in extend_srts:
                        for _cat, _kws in ALL_CATEGORIES.items():
                            if _cat != '上衣':
                                for _kw in _kws:
                                    if _kw in _t:
                                        _skip_extend = True
                                        break
                                if _skip_extend:
                                    break
                except ImportError:
                    pass
                if not _skip_extend:
                    c_end = c_end + 0.5
                    c_dur = c_end - c_start
                    _log(f"tighten [{c_type}]: +0.5s缓冲 -> {c_start:.1f}-{c_end:.1f}s")
                    tightened.append((c_type, c_text, c_start, c_end, c_score, c_dur, *clip[6:]))
                else:
                    tightened.append(clip)
            else:
                tightened.append(clip)
            continue
        new_dur = new_end - new_start
        if new_dur < 2.0:
            tightened.append(clip)
        elif abs(new_start - c_start) > 0.3 or abs(new_end - c_end) > 0.3:
            _log(f"tighten [{c_type}]: {c_start:.1f}-{c_end:.1f}s -> {new_start:.1f}-{new_end:.1f}s ({new_dur:.1f}s)")
            tightened.append((c_type, c_text, new_start, new_end, c_score, new_dur, *clip[6:]))
        else:
            tightened.append(clip)

    total_before = sum(c[5] for c in clips)
    total_after = sum(c[5] for c in tightened)
    _log(f"tighten total: {total_before:.1f}s -> {total_after:.1f}s (saved {total_before-total_after:.1f}s)")

    return tightened


def ensure_sentence_complete(clips, srt_text, log_fn=None):
    """
    [v9.6] 确保所有片段在语意断句处切割，不被半句截断。

    ASR按声音停顿断句，SRT条目的end时间经常落在句子中间。
    本函数检查每个片段最后一条SRT条目，如果语句不完整，
    延伸到下一个完整断句点。

    判断方式（双信号）：
    1. 句末标点（。？！）→ 语句完整
    2. SRT条目间语音间隔 > 0.5s → 有停顿，视为断句
    3. 弱结尾词（然后/因为/所以...）→ 明显没说完

    保护：不超过下一个片段start-0.3s，最多延伸3秒
    """
    def _log(msg):
        if log_fn: log_fn(msg)

    if not srt_text or not clips:
        return clips

    # 解析SRT
    srt_entries = []
    for block in srt_text.strip().split('\n\n'):
        lines = block.strip().split('\n')
        if len(lines) < 3:
            continue
        time_line = lines[1]
        text = ' '.join(lines[2:]).strip()
        m = re.match(r'(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)', time_line)
        if m:
            start_s = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3)) + int(m.group(4))/1000
            end_s = int(m.group(5))*3600 + int(m.group(6))*60 + int(m.group(7)) + int(m.group(8))/1000
            srt_entries.append((start_s, end_s, text))

    if not srt_entries:
        return clips

    # 强句末标点 → 一定完整
    STRONG_END = set("。？！.?!")
    # 弱结尾词 → 一定不完整
    WEAK_ENDINGS = ["然后", "就是", "其实", "而且", "但是", "不过", "所以",
                    "因为", "如果", "虽然", "不仅", "并且", "以及",
                    "这个", "那个", "一件", "一套", "一条", "一个",
                    "觉得", "感觉", "发现", "看到",
                    "的", "呀", "呢", "吧", "咯", "啊", "哈", "啦", "嘛",
                    "很", "最", "更", "还", "又", "再"]

    def is_definitely_complete(txt):
        """以强句末标点结尾 → 一定完整"""
        if not txt: return False
        t = txt.rstrip()
        return bool(t) and t[-1] in STRONG_END

    def is_obviously_incomplete(txt):
        """以弱结尾词结尾 → 一定不完整"""
        if not txt: return True
        t = txt.rstrip()
        if not t: return True
        # 以逗号/顿号结尾 → 还有后续
        if t[-1] in "，,、；;：:":
            return True
        for w in WEAK_ENDINGS:
            if t.endswith(w):
                return True
        return False

    def speech_gap_after(entry_idx):
        """当前SRT条目到下一条的语音间隔"""
        if entry_idx >= len(srt_entries) - 1:
            return 999  # 最后一条，后面没有语音了
        curr_end = srt_entries[entry_idx][1]
        next_start = srt_entries[entry_idx + 1][0]
        return max(0, next_start - curr_end)

    # 每个片段的 end 上限 = 下一个片段的 start - 0.3s
    clips_by_start = sorted(range(len(clips)), key=lambda i: clips[i][2])
    end_limits = {}
    for si, idx in enumerate(clips_by_start):
        if si + 1 < len(clips_by_start):
            next_idx = clips_by_start[si + 1]
            next_start = clips[next_idx][2]
            end_limits[idx] = next_start - 0.3
        else:
            end_limits[idx] = 99999

    MAX_EXTENSION = 5.0
    result = list(clips)
    extended_count = 0

    for idx in range(len(result)):
        clip = result[idx]
        c_type, c_text, c_start, c_end, c_score, c_dur = clip[:6]
        rest = clip[6:]

        # Close片段不做延伸——延伸只会拖入收尾废话
        if 'close' in c_type.lower():
            continue

        # 找片段 end 时间落在哪个 SRT 条目
        end_entry_idx = None
        for ei, (s, e, t) in enumerate(srt_entries):
            if s <= c_end <= e + 0.3:
                end_entry_idx = ei
                break
            if s > c_end:
                break

        if end_entry_idx is None:
            continue

        entry_text = srt_entries[end_entry_idx][2]
        entry_end = srt_entries[end_entry_idx][1]

        # 判断是否需要延伸
        need_extend = False

        if is_definitely_complete(entry_text):
            # 有句末标点，但 end 在条目中间 → 延伸到条目末尾
            if c_end < entry_end - 0.3:
                need_extend = True
            else:
                continue  # 完整且对齐，不需要
        elif is_obviously_incomplete(entry_text):
            need_extend = True  # 明显不完整
        else:
            # 中间情况：无标点结尾，检查语音间隔
            gap = speech_gap_after(end_entry_idx)
            if gap < 0.5:
                need_extend = True  # 还在连续说话
            else:
                continue  # 有停顿，视为断句

        if not need_extend:
            continue

        # 沿SRT条目找下一个完整断句点
        extended_end = c_end
        end_limit = end_limits.get(idx, 99999)

        for ei in range(end_entry_idx, len(srt_entries)):
            s, e, t = srt_entries[ei]
            if s > c_end + MAX_EXTENSION:
                break
            extended_end = e

            # 停止条件：找到完整断句
            if is_definitely_complete(t):
                break  # 强句末标点
            gap = speech_gap_after(ei)
            if gap > 0.5 and not is_obviously_incomplete(t):
                break  # 有停顿且不是弱结尾
            # 否则继续延伸

        # 应用延伸（受上限保护）
        extended_end = min(extended_end, end_limit)
        actual_extension = extended_end - c_end

        if 0.1 < actual_extension <= MAX_EXTENSION:
            new_dur = extended_end - c_start
            result[idx] = (c_type, c_text, c_start, extended_end, c_score, new_dur, *rest)
            extended_count += 1
            _log(f"语句延伸: [{c_type}] {c_end:.1f}s→{extended_end:.1f}s (+{actual_extension:.1f}s)")
        elif actual_extension > MAX_EXTENSION:
            _log(f"语句延伸: [{c_type}] 需延伸{actual_extension:.1f}s超限，保持原样")

    if extended_count > 0:
        total_before = sum(c[5] for c in clips)
        total_after = sum(c[5] for c in result)
        _log(f"语句完整性: 延伸了 {extended_count}/{len(clips)} 个片段, 总时长 {total_before:.1f}s→{total_after:.1f}s")

    return result



def trim_repetitive_filler(clips, srt_text, log_fn=None):
    """
    [v8.5.2] 裁剪片段末尾的重复语气词/废话。
    
    检测规则：同一个词（2-4字）在片段末尾连续出现≥2次，
    将end截到第一次出现之前对应的SRT条目的end时间。
    
    示例：
    - "哎呀 哎呀 哎呀" → 截到第一个"哎呀"之前
    - "就没有了 就没有了" → 截到第一个"就没有了"之前
    """
    def _log(msg):
        if log_fn: log_fn(msg)
    
    if not srt_text or not clips:
        return clips
    
    # 解析SRT
    srt_entries = []
    for block in srt_text.strip().split('\n\n'):
        lines = block.strip().split('\n')
        if len(lines) < 3:
            continue
        import re
        time_line = lines[1]
        text = ' '.join(lines[2:]).strip()
        m = re.match(r'(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)', time_line)
        if m:
            start_s = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3)) + int(m.group(4))/1000
            end_s = int(m.group(5))*3600 + int(m.group(6))*60 + int(m.group(7)) + int(m.group(8))/1000
            srt_entries.append((start_s, end_s, text))
    
    if not srt_entries:
        return clips
    
    # 常见重复语气词（2-4字）
    FILLER_PATTERNS = [
        '哎呀', '好吧', '对吧', '真的', '对对', '好好', '是的',
        '没有了', '没有了啊', '就没有了', '就没了', '断码了',
        'OK', 'ok', '没有了', '嗯嗯', '好好好', '对对对',
        '是吧', '对吧', '好吧', '知道吗', '晓得吧',
    ]
    
    result = list(clips)
    trimmed_count = 0
    
    for idx in range(len(result)):
        clip = result[idx]
        c_type, c_text, c_start, c_end, c_score, c_dur = clip[:6]
        rest = clip[6:]
        
        # 收集片段末尾的SRT条目（最后3秒内）
        tail_entries = []
        for s, e, t in srt_entries:
            if s >= c_end - 3.0 and s < c_end:
                tail_entries.append((s, e, t))
        
        if len(tail_entries) < 2:
            continue
        
        # 检查末尾是否有重复词
        tail_texts = [t for s, e, t in tail_entries]
        tail_text_joined = ' '.join(tail_texts)
        
        trim_before_srt_idx = None  # 从哪个SRT条目开始截
        
        # 方法1：检查已知重复语气词
        for filler in sorted(FILLER_PATTERNS, key=len, reverse=True):
            count = tail_text_joined.count(filler)
            if count >= 2:
                # 找到第一个出现该词的SRT条目
                for si, (s, e, t) in enumerate(tail_entries):
                    if filler in t:
                        trim_before_srt_idx = si
                        break
                if trim_before_srt_idx is not None:
                    break
        
        # 方法2：检查任意2-4字词连续重复
        if trim_before_srt_idx is None:
            for length in [4, 3, 2]:
                for i in range(len(tail_texts) - 1):
                    # 取每个条目的最后length个字
                    w1 = tail_texts[i].strip()[-length:] if len(tail_texts[i].strip()) >= length else tail_texts[i].strip()
                    w2 = tail_texts[i+1].strip()[-length:] if len(tail_texts[i+1].strip()) >= length else tail_texts[i+1].strip()
                    if w1 == w2 and len(w1) >= 2:
                        trim_before_srt_idx = i
                        break
                if trim_before_srt_idx is not None:
                    break
        
        if trim_before_srt_idx is not None:
            # 截到重复词首次出现之前的SRT条目的end时间
            if trim_before_srt_idx > 0:
                new_end = tail_entries[trim_before_srt_idx - 1][1]
            else:
                new_end = tail_entries[trim_before_srt_idx][0]  # 截到该条目开头
            
            # 确保不会把片段缩太短（至少保留3秒）
            if new_end - c_start < 3.0:
                _log(f"重复废话: [{c_type}] 截断后不足3s，跳过")
                continue
            
            old_dur = c_end - c_start
            new_dur = new_end - c_start
            result[idx] = (c_type, c_text, c_start, new_end, c_score, new_dur, *rest)
            trimmed_count += 1
            _log(f"重复废话: [{c_type}] {c_end:.1f}s→{new_end:.1f}s (-{c_end-new_end:.1f}s)")
    
    if trimmed_count > 0:
        total_before = sum(c[5] for c in clips)
        total_after = sum(c[5] for c in result)
        _log(f"重复废话裁剪: 修剪了 {trimmed_count}/{len(clips)} 个片段, 总时长 {total_before:.1f}s→{total_after:.1f}s")
    
    return result



def trim_tail_filler(clips, srt_text, log_fn=None):
    """
    [v8.5.2] 截掉片段末尾的收尾废话。
    
    混合方案：
    1. 用clip文本检测废话（可靠）
    2. 用SRT条目估算截断时间点（比字符比例准确）
    3. 如果SRT匹配失败，回退到字符比例
    """
    import re
    def _log(msg):
        if log_fn: log_fn(msg)
    
    if not clips:
        return clips
    
    # 解析SRT（用于时间估算）
    srt_entries = []
    if srt_text:
        for block in srt_text.strip().split('\n\n'):
            lines = block.strip().split('\n')
            if len(lines) < 3:
                continue
            time_line = lines[1]
            text = ' '.join(lines[2:]).strip()
            m = re.match(r'(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)', time_line)
            if m:
                start_s = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3)) + int(m.group(4))/1000
                end_s = int(m.group(5))*3600 + int(m.group(6))*60 + int(m.group(7)) + int(m.group(8))/1000
                srt_entries.append((start_s, end_s, text))
    
    # 收尾废话短语
    TAIL_FILLER_PHRASES = [
        '我觉得你们', '直接断码了', '已经断码了',
        '我觉得你', '已经没了', '真的没了',
        '抓紧时间', '冲榜啦', '早点拍',
        '不要快点', '不要犹豫', '不要等',
        '没有了', '秒没了', '直接断码',
        '断码了', '好快', '我觉得',
        '知道吗', '知道吧', '就这样',
        '好吧好吧', '没啦', '没了',
        '真没了', '就没了',
        '已经断码', '好快哦', '好快呀',
        '冲榜', '冲啊', '早拍早发',
        '不要点', '快点冲', '抓紧',
        '不要纠结', '不敢给你', '都不敢',
    ]
    
    def find_filler_start_in_text(txt):
        """在clip文本中找到收尾废话的最早起始位置（字符索引）"""
        txt = txt.strip()
        if not txt or len(txt) < 4:
            return None
        earliest_idx = None
        for phrase in TAIL_FILLER_PHRASES:
            idx = txt.find(phrase)
            if idx < 0:
                continue
            if idx > len(txt) * 0.25:
                if earliest_idx is None or idx < earliest_idx:
                    earliest_idx = idx
        return earliest_idx
    
    def find_trim_time_via_srt(c_start, c_end, filler_char_idx, clip_text_len):
        """用SRT条目找更准确的截断时间点"""
        if not srt_entries:
            return None
        
        # 找clip时间范围内的SRT条目
        clip_srt = [(s, e, t) for s, e, t in srt_entries if s >= c_start - 0.3 and s < c_end + 0.3]
        if not clip_srt:
            return None
        
        # 累积字符数，找到废话起始字符对应的SRT条目
        cum_chars = 0
        for s, e, t in clip_srt:
            cum_chars += len(t)
            if cum_chars >= filler_char_idx:
                # 废话在这个SRT条目里
                # 该条目前面已经累积了多少字符
                prev_chars = cum_chars - len(t)
                # 废话在这个条目内的偏移
                in_entry_offset = filler_char_idx - prev_chars
                # 按条目内字符比例估算时间
                ratio = in_entry_offset / max(len(t), 1)
                return s + (e - s) * ratio
        
        return None
    
    # 不完整结尾检测
    INCOMPLETE_ENDINGS = ['这件衣', '这个面', '这个版', '这个布', '这个质']
    
    result = list(clips)
    trimmed_count = 0
    
    for idx in range(len(result)):
        clip = result[idx]
        c_type, c_text, c_start, c_end, c_score, c_dur = clip[:6]
        rest = clip[6:]
        
        if not c_text:
            continue
        
        trim_end = None
        
        # 策略1：检测收尾废话短语
        filler_idx = find_filler_start_in_text(c_text)
        if filler_idx is not None:
            # 优先用SRT估算时间
            srt_trim = find_trim_time_via_srt(c_start, c_end, filler_idx, len(c_text.strip()))
            if srt_trim is not None and srt_trim > c_start + 1.5:
                trim_end = srt_trim
                _log(f"收尾废话: [{c_type}] SRT估算 {c_end:.1f}s\u2192{trim_end:.1f}s")
            else:
                # 回退到字符比例
                ratio = filler_idx / len(c_text.strip())
                char_trim = c_start + c_dur * ratio
                if char_trim > c_start + 1.5:
                    trim_end = char_trim
                    _log(f"收尾废话: [{c_type}] 字符比例 {c_end:.1f}s\u2192{trim_end:.1f}s")
        
        # 策略2：检测不完整结尾
        if trim_end is None:
            for ie in INCOMPLETE_ENDINGS:
                if c_text.strip().endswith(ie):
                    ie_ratio = (len(c_text.strip()) - len(ie)) / len(c_text.strip())
                    trim_end = c_start + c_dur * ie_ratio
                    _log(f"收尾废话: [{c_type}] 不完整结尾'{ie}'")
                    break
        
        if trim_end is not None and trim_end > c_start + 1.5:
            new_dur = trim_end - c_start
            result[idx] = (c_type, c_text, c_start, trim_end, c_score, new_dur, *rest)
            trimmed_count += 1
            _log(f"收尾废话: [{c_type}] {c_end:.1f}s\u2192{trim_end:.1f}s (-{c_end-trim_end:.1f}s)")
        elif trim_end is not None:
            _log(f"收尾废话: [{c_type}] 截断后不足1.5s，跳过")
    
    if trimmed_count > 0:
        total_before = sum(c[5] for c in clips)
        total_after = sum(c[5] for c in result)
        _log(f"收尾废话裁剪: 修剪了 {trimmed_count}/{len(clips)} 个片段, 总时长 {total_before:.1f}s\u2192{total_after:.1f}s")
    
    return result
