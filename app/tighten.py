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

        # close额外+0.5s防吃字
        if 'close' in c_type.lower():
            new_end += 0.5

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
