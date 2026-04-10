"""
后处理：裁剪过长的片段
AI经常选11-14秒的片段，里面含大量废话
此模块将长片段按SRT条目拆分，只保留包含AI关键词的子段
"""
import re

def trim_long_clips(clips, srt_text, max_dur=7.0, log_fn=None):
    """裁剪超过max_dur的片段，只保留匹配AI文字的SRT子段"""
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

    def _text_overlap(ai_clean, srt_clean):
        """AI文本和SRT文本是否有显著重叠"""
        if not ai_clean or not srt_clean:
            return False
        # 取AI文本的连续4字片段，看有多少出现在SRT中
        ai_chunks = [ai_clean[i:i+4] for i in range(len(ai_clean)-3)]
        if not ai_chunks:
            return False
        matches = sum(1 for ch in ai_chunks if ch in srt_clean)
        ratio = matches / len(ai_chunks)
        return ratio > 0.3  # 30%以上重叠就算相关

    result = []
    for clip in clips:
        c_type, c_text, c_start, c_end, c_score, c_dur = clip[:6]

        if c_dur <= max_dur:
            result.append(clip)
            continue

        # 长片段：找范围内的SRT条目
        in_range = [(s, e, t) for s, e, t in srt_entries
                    if s >= c_start - 0.5 and e <= c_end + 0.5 and e > s]

        if not in_range:
            # 放宽搜索
            in_range = [(s, e, t) for s, e, t in srt_entries
                        if s < c_end and e > c_start and e > s]

        if len(in_range) <= 1:
            # 只有1条或没有，无法拆分，按比例截取
            # 取AI文本中间70%
            clean_ai = _clean(c_text)
            total_len = len(clean_ai)
            trim_start = int(total_len * 0.15)
            trim_end = int(total_len * 0.85)
            new_dur = c_dur * 0.7
            new_start = c_start + c_dur * 0.15
            new_end = new_start + new_dur
            _log(f"trim [{c_type}]: {c_start:.1f}-{c_end:.1f}s ({c_dur:.1f}s) -> {new_start:.1f}-{new_end:.1f}s ({new_dur:.1f}s) [proportional]")
            result.append((c_type, c_text, new_start, new_end, c_score, new_dur, *clip[6:]))
            continue

        # 多条SRT：只保留与AI文本重叠的条目
        clean_ai = _clean(c_text)
        matched = [(s, e, t) for s, e, t in in_range if _text_overlap(clean_ai, _clean(t))]

        if not matched:
            # 没有匹配的，取中间段
            mid = len(in_range) // 2
            matched = in_range[max(0, mid-1):mid+2]

        new_start = matched[0][0]
        new_end = matched[-1][1]
        new_dur = new_end - new_start

        # close额外+0.5s
        if 'close' in c_type.lower():
            new_end += 0.5
            new_dur = new_end - new_start

        if new_dur < 2.0:
            result.append(clip)
            _log(f"trim skip [{c_type}]: too short after trim")
        else:
            _log(f"trim [{c_type}]: {c_start:.1f}-{c_end:.1f}s ({c_dur:.1f}s) -> {new_start:.1f}-{new_end:.1f}s ({new_dur:.1f}s) [{len(matched)}/{len(in_range)} SRT]")
            result.append((c_type, c_text, new_start, new_end, c_score, new_dur, *clip[6:]))

    return result
