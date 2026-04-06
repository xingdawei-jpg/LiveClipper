"""
纯 Python SRT 解析器（不依赖 pysrt）
支持 UTF-8 / UTF-8-BOM / GBK 编码
"""


class SrtSubtitle:
    """单条字幕"""
    def __init__(self, index, start, end, text):
        self.index = index
        self.start = start  # (hours, minutes, seconds, milliseconds)
        self.end = end
        self.text = text

    def __repr__(self):
        return f"SrtSubtitle({self.index}, {self.start}->{self.end}, '{self.text[:30]}...')"


def _parse_time(time_str):
    """解析 SRT 时间戳 '00:01:23,456' -> (h, m, s, ms)"""
    time_str = time_str.strip().replace(".", ",")
    try:
        time_part, ms_part = time_str.split(",")
        ms = int(ms_part)
    except ValueError:
        time_part = time_str
        ms = 0

    parts = time_part.split(":")
    if len(parts) == 3:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    elif len(parts) == 2:
        h, m, s = 0, int(parts[0]), int(parts[1])
    else:
        h, m, s = 0, 0, int(parts[0])

    return (h, m, s, ms)


def _time_to_seconds(time_tuple):
    """(h, m, s, ms) -> 秒（浮点数）"""
    h, m, s, ms = time_tuple
    return h * 3600 + m * 60 + s + ms / 1000


def open_srt(file_path):
    """
    打开 SRT 文件，自动检测编码。
    返回 SrtSubtitle 列表。
    """
    # 尝试不同编码
    encodings = ["utf-8-sig", "utf-8", "gbk", "gb2312", "gb18030", "latin-1"]
    content = None
    used_encoding = None

    for enc in encodings:
        try:
            with open(file_path, "r", encoding=enc) as f:
                content = f.read()
            used_encoding = enc
            break
        except (UnicodeDecodeError, UnicodeError):
            continue

    if content is None:
        raise ValueError(f"无法解码文件: {file_path}")

    # 清理 Windows 换行符
    content = content.replace("\r\n", "\n").replace("\r", "\n")

    # 按空行分割字幕块
    blocks = content.strip().split("\n\n")
    subtitles = []

    for block in blocks:
        lines = [l.strip() for l in block.strip().split("\n") if l.strip()]
        if len(lines) < 2:
            continue

        # 查找时间戳行（包含 -->）
        time_line_idx = -1
        for i, line in enumerate(lines):
            if "-->" in line:
                time_line_idx = i
                break

        if time_line_idx < 0:
            continue

        # 序号（可选）
        try:
            index = int(lines[0]) if time_line_idx > 0 else len(subtitles) + 1
        except ValueError:
            index = len(subtitles) + 1

        # 解析时间戳
        time_line = lines[time_line_idx]
        try:
            start_str, end_str = time_line.split("-->", 1)
            start = _parse_time(start_str)
            end = _parse_time(end_str)
        except Exception:
            continue

        # 文本（时间戳行之后的所有行）
        text_lines = lines[time_line_idx + 1:]
        text = " ".join(text_lines)

        # 清理 HTML 标签
        import re
        text = re.sub(r"<[^>]+>", "", text).strip()

        if text:
            sub = SrtSubtitle(index, start, end, text)
            subtitles.append(sub)

    return subtitles, used_encoding
