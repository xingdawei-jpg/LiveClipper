"""
ASR后处理：拆分长SRT条目
火山引擎ASR经常生成5-10秒的长条目，导致AI选片精度差
此模块将长条目按标点拆分为短条目，时间按字符比例分配
"""
import re

def split_long_srt_entries(srt_text, max_duration=5.0, log_fn=None):
    """拆分超过max_duration的SRT条目"""
    def _log(msg):
        if log_fn:
            log_fn(msg)

    if not srt_text or not srt_text.strip():
        return srt_text

    # 解析SRT
    entries = []
    blocks = srt_text.strip().split('\n\n')
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 3:
            entries.append(block)
            continue
        idx_line = lines[0]
        time_line = lines[1]
        text = ' '.join(lines[2:]).strip()
        m = re.match(r'(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)', time_line)
        if not m:
            entries.append(block)
            continue
        start_s = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3)) + int(m.group(4))/1000
        end_s = int(m.group(5))*3600 + int(m.group(6))*60 + int(m.group(7)) + int(m.group(8))/1000
        dur = end_s - start_s

        if dur <= max_duration:
            entries.append(block)
            continue

        # 长条目：按标点拆分
        # 中文标点: ，。！？、；：
        # 日文/其他: ､｡
        parts = re.split(r'([，。！？、；：､｡])', text)
        # 重新组合标点到前面的部分
        segments = []
        current = ''
        for p in parts:
            current += p
            if p in '，。！？、；：､｡':
                segments.append(current)
                current = ''
        if current:
            segments.append(current)

        if len(segments) <= 1:
            # 没有标点可拆，按固定长度拆
            seg_len = max(4, len(text) // max(1, int(dur / max_duration)))
            segments = [text[i:i+seg_len] for i in range(0, len(text), seg_len)]

        # 按字符数比例分配时间
        total_chars = sum(len(s) for s in segments)
        if total_chars == 0:
            entries.append(block)
            continue

        current_start = start_s
        for seg in segments:
            ratio = len(seg) / total_chars
            seg_dur = dur * ratio
            seg_end = current_start + seg_dur

            # 格式化时间
            def fmt_time(t):
                h = int(t // 3600)
                m = int((t % 3600) // 60)
                s = int(t % 60)
                ms = int((t % 1) * 1000)
                return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

            entries.append(f"{fmt_time(current_start)} --> {fmt_time(seg_end)}\n{seg}")
            current_start = seg_end

    # 重新编号
    result = []
    for i, entry in enumerate(entries, 1):
        lines = entry.strip().split('\n')
        if len(lines) >= 2:
            result.append(f"{i}\n{chr(10).join(lines)}")
        else:
            result.append(entry)

    split_count = len(result) - len(blocks)
    if split_count > 0 and log_fn:
        _log(f"SRT拆分: {len(blocks)}条 -> {len(result)}条 (拆出{split_count}条短条目)")

    return '\n\n'.join(result)
