# -*- coding: utf-8 -*-
"""素材库：按关键词/整句反查"哪个主播、哪场直播、第几秒说过"。

一句话定位的原理
----------------
* 录屏目录约定是 ``<素材根目录>/<主播名>/xxx.flv``，同名 ``.srt`` 就是它的字幕。
* 入库单位是**字幕句**（SRT cue，相邻碎 cue 会合并），每句自带 start/end，
  所以命中天然能落到"第几秒"。
* 用 SQLite 扛中文子串检索，分两条路：
  - **3 字以上**：FTS5 + ``tokenize='trigram'``。注意 trigram 分词器要求
    查询至少 3 个字符（每个汉字 3 字节），所以 2 字查询在 FTS 里永远是空集；
  - **1~2 字**（中文最高频的检索长度）：自建 **3-gram 倒排表**。3 个汉字
    打包成一个整数当键，2 字查询取"左补任意字 / 右补任意字"的 3 字窗口做
    候选，再用 LIKE 复核，避免全表扫描。实测 2 万句规模下单次 2 字查询
    约 9ms、索引约 12MB。
* 查询与入库共用同一套归一化（全角→半角、繁→简、去标点空白），因此
  "顯瘦？？"能命中"显瘦"。

缺失字幕的视频可以调用 ``generate_srt_for_video()`` 走一遍现成的
SenseVoice/Paraformer 链路把字幕补齐，产物留在视频旁边，下次直接复用。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

SCHEMA_VERSION = 2

VIDEO_EXTENSIONS = (".mp4", ".flv", ".ts", ".mkv", ".mov", ".avi", ".m4v", ".wmv")

DEFAULT_CONTEXT_SENTENCES = 2
DEFAULT_GROUP_GAP_SECONDS = 20.0
DEFAULT_MAX_VIDEOS = 4000
MIN_VIDEO_BYTES = 512 * 1024

#: 一次最多取多少个命中句作为候选（只影响 id 列表，不搬正文）
DEFAULT_LIMIT_SENTENCES = 1500
#: 最多返回多少个视频（一个视频 = 一张结果卡片）
DEFAULT_LIMIT_GROUPS = 200
#: 每个视频默认展开几段上下文，其余用"该视频还有 N 处"提示（不静默丢弃）
DEFAULT_WINDOWS_PER_GROUP = 3
#: 同一视频内两处命中相隔多久以内仍算"同一段"（仅用于排序观感）
DEFAULT_GROUP_GAP_SECONDS = 20.0

SEARCH_MODES = ("smart", "exact", "terms")

ProgressFn = Callable[[str, str], None]

_fts5_state: bool | None = None


# ==========================================================================
# 文本归一化
# ==========================================================================

# 全角 -> 半角（只覆盖标准全角 ASCII 区，避免误伤 ħ 这类字母）
_FULLWIDTH_MAP = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
_FULLWIDTH_MAP[0x3000] = 0x20

_FULLWIDTH_PUNCT_RE = re.compile("[\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65]+")

#: 标点/空白/零宽字符（中文、字母、数字之外一律丢弃）
#: ASCII 段 ``[!-/:-@\[-`{-~]`` 覆盖全部 ASCII 标点，连字符放最后避免区间歧义。
_PUNCT_RE = re.compile(
    "["
    "\u2000-\u206f"
    "\u3000-\u303f"
    "\u00a0"
    "\u200b-\u200d"
    "\ufeff"
    "!-/:-@\\[-`{-~"
    "\\s"
    "]+"
)

#: 繁体 -> 简体常用字（只覆盖直播文案高频字，零依赖够用）
#: 注意：这里放的是"繁体 -> 简体"，同字不同码位的（絕/绝、織/织、真/真）
#: 必须逐对写清楚，漏一个就会导致繁体查询搜不到。
_T2S_PAIRS = [
    # 高频虚词与动词
    ("這", "这"), ("個", "个"), ("們", "们"), ("嗎", "吗"), ("說", "说"), ("話", "话"),
    ("麼", "么"), ("買", "买"), ("賣", "卖"), ("價", "价"), ("錢", "钱"), ("讓", "让"),
    ("給", "给"), ("對", "对"), ("錯", "错"), ("過", "过"), ("來", "来"), ("進", "进"),
    ("開", "开"), ("關", "关"), ("門", "门"), ("問", "问"), ("題", "题"), ("時", "时"),
    ("間", "间"), ("點", "点"), ("樣", "样"), ("還", "还"), ("會", "会"), ("種", "种"),
    ("類", "类"), ("總", "总"), ("結", "结"), ("實", "实"), ("東", "东"), ("專", "专"),
    ("業", "业"), ("簡", "简"), ("單", "单"), ("純", "纯"), ("淨", "净"), ("輕", "轻"),
    ("軟", "软"), ("順", "顺"), ("暢", "畅"), ("熱", "热"), ("涼", "凉"), ("溫", "温"),
    ("響", "响"), ("聲", "声"), ("聽", "听"), ("講", "讲"), ("語", "语"), ("調", "调"),
    ("擇", "择"), ("選", "选"), ("報", "报"), ("價", "价"), ("費", "费"), ("賬", "账"),
    ("訂", "订"), ("貨", "货"), ("運", "运"), ("遞", "递"), ("換", "换"), ("售", "售"),
    ("後", "后"), ("優", "优"), ("滿", "满"), ("減", "减"), ("贈", "赠"), ("搶", "抢"),
    ("飛", "飞"), ("賺", "赚"), ("虧", "亏"), ("贏", "赢"), ("發", "发"),
    # 服装 / 面料
    ("紅", "红"), ("綠", "绿"), ("藍", "蓝"), ("黃", "黄"), ("顏", "颜"), ("顔", "颜"),
    ("絲", "丝"), ("織", "织"), ("纖", "纤"), ("維", "维"), ("綿", "绵"), ("綸", "纶"),
    ("綢", "绸"), ("緞", "缎"), ("絨", "绒"), ("縐", "皱"), ("縫", "缝"), ("縮", "缩"),
    ("褲", "裤"), ("裝", "装"), ("襯", "衬"), ("襪", "袜"), ("領", "领"), ("袖", "袖"),
    ("腳", "脚"), ("鞋", "鞋"), ("鏡", "镜"), ("飾", "饰"), ("鏈", "链"), ("錶", "表"),
    ("緊", "紧"), ("彈", "弹"), ("顯", "显"), ("絕", "绝"), ("頭", "头"), ("髮", "发"),
    ("護", "护"), ("膚", "肤"), ("體", "体"), ("試", "试"), ("號", "号"), ("碼", "码"),
    ("標", "标"), ("準", "准"), ("質", "质"), ("檢", "检"), ("驗", "验"), ("產", "产"),
    ("邊", "边"), ("約", "约"), ("氣", "气"), ("價", "价"), ("靚", "靓"), ("韓", "韩"),
    ("灣", "湾"), ("紅", "红"),
    # 数字与其他高频字
    ("兩", "两"), ("萬", "万"), ("億", "亿"), ("臺", "台"), ("張", "张"), ("條", "条"),
    ("筆", "笔"), ("塊", "块"), ("噸", "吨"), ("絲", "丝"), ("愛", "爱"), ("兒", "儿"),
    ("媽", "妈"), ("寶", "宝"), ("貝", "贝"), ("數", "数"), ("門", "门"), ("員", "员"),
    ("華", "华"), ("麗", "丽"), ("觀", "观"), ("眾", "众"), ("場", "场"),
]
_T2S_TABLE = str.maketrans({src: dst for src, dst in _T2S_PAIRS})


def normalize_for_match(text: Any) -> str:
    """把文本归一化成可比对的检索键（查询与入库共用）。

    规则：全角转半角 -> 去标点空白 -> 繁体转简体 -> NFKC -> 转小写。
    满足 ``normalize(a + b) == normalize(a) + normalize(b)``，所以
    "逐字符归一化"的拼接与整串归一化结果一致，命中位置映射才可信。
    """
    if text is None:
        return ""
    raw = str(text)
    if not raw:
        return ""
    raw = raw.translate(_FULLWIDTH_MAP)
    raw = _FULLWIDTH_PUNCT_RE.sub("", raw)
    raw = _PUNCT_RE.sub("", raw)
    if not raw:
        return ""
    raw = raw.translate(_T2S_TABLE)
    raw = unicodedata.normalize("NFKC", raw)
    return raw.lower()


def split_segment_texts(raw_text: str) -> list[str]:
    """把一句入库文本还原成原始字幕块（入库时各块用空格拼接）。"""
    if not raw_text:
        return []
    return [part for part in re.split(r"\s+", raw_text.strip()) if part]


def format_timestamp(seconds: Any) -> str:
    """秒 -> 00:32:15（用于展示与复制到播放器跳转）。"""
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        return "00:00:00"
    if total < 0:
        total = 0.0
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    secs = int(total % 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_bytes(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return ""


# 常见直播平台/工具的目录名，不能当作"主播名"
_GENERIC_DIR_NAMES = {
    "", "videos", "video", "录制", "录屏", "直播录制", "抖音直播录制", "屏幕录制",
    "captures", "desktop", "output", "outputs", "downloads", "下载", "素材",
    "素材库", "剪辑", "已完成", "待剪辑", "clips", "temp", "tmp", "live",
    "liveclipper", "users", "administrator", "user", "public", "work", "切片",
}


def guess_publisher(video_path: str, root_dir: str = "") -> str:
    """推断主播名：取"视频所在的子目录名"。

    纯数字/哈希目录（房间号、时间戳）不猜；通用容器目录只有在它**就是扫描
    根目录本身**时才不猜 —— 用户把 ``E:\\切片\\录屏`` 当根目录选，下面
    ``录屏/主播A/`` 依然要认出主播 A。
    """
    path = Path(video_path)
    folder = path.parent.name.strip()
    if not folder:
        return ""
    lowered = folder.lower()
    if re.fullmatch(r"[\d_\-.]+", folder):
        return ""
    if re.fullmatch(r"[0-9a-f]{8,}", lowered):
        return ""
    if lowered in _GENERIC_DIR_NAMES:
        if not root_dir:
            return ""
        try:
            same = path.resolve().parent == Path(root_dir).resolve()
        except OSError:
            same = folder == Path(root_dir).name.strip()
        return "" if same else folder
    return folder


def guess_session_epoch(name: str) -> float:
    """从文件名猜开播时间：688250693939-20260923-220806-5e30c5.flv。"""
    if not name:
        return 0.0
    patterns = (
        r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})[-_. T]?(\d{2})[-_.:]?(\d{2})[-_.:]?(\d{2})",
        r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})",
        r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})[-_. T]?(\d{2})[-_.:]?(\d{2})",
    )
    for pattern in patterns:
        match = re.search(pattern, name)
        if not match:
            continue
        groups = [int(value) for value in match.groups()]
        while len(groups) < 6:
            groups.append(0)
        year, month, day, hour, minute, second = groups[:6]
        if not (2000 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
            continue
        try:
            return time.mktime((year, month, day, hour, minute, second, 0, 0, -1))
        except (OverflowError, ValueError):
            continue
    return 0.0


# ==========================================================================
# SRT 解析
# ==========================================================================

_SRT_TIME_RE = re.compile(
    r"(\d{1,3}):([0-5]?\d):([0-5]?\d)(?:[,.](\d{1,3}))?\s*-->\s*"
    r"(\d{1,3}):([0-5]?\d):([0-5]?\d)(?:[,.](\d{1,3}))?"
)
_SRT_HTML_RE = re.compile(r"<[^>]+>")


def _srt_clock_to_seconds(hours: Any, minutes: Any, seconds: Any, fraction: Any) -> float:
    value = int(hours) * 3600 + int(minutes) * 60 + int(seconds)
    fraction_text = str(fraction or "").strip()
    if fraction_text:
        value += int(fraction_text.ljust(3, "0")[:3]) / 1000.0
    return float(value)


def parse_srt_text(content: str) -> list[dict[str, Any]]:
    """解析 SRT 文本 -> [{'start':秒,'end':秒,'text':文本}]。"""
    if not content:
        return []
    content = content.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    cues: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", content):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        time_index = -1
        for index, line in enumerate(lines):
            if "-->" in line:
                time_index = index
                break
        if time_index < 0:
            continue
        match = _SRT_TIME_RE.search(lines[time_index])
        if not match:
            continue
        groups = match.groups()
        start = _srt_clock_to_seconds(*groups[0:4])
        end = _srt_clock_to_seconds(*groups[4:8])
        text = _SRT_HTML_RE.sub("", " ".join(lines[time_index + 1:])).strip()
        if not text:
            continue
        if end < start:
            end = start
        cues.append({"start": start, "end": end, "text": text})
    return cues


def read_srt_file(srt_path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """读 SRT 文件（自动试编码）。"""
    path = Path(srt_path)
    if not path.is_file():
        return []
    try:
        from srt_parser import open_srt  # type: ignore

        subtitles, _encoding = open_srt(str(path))
        cues = [
            {
                "start": _srt_clock_to_seconds(*[str(v) for v in item.start]),
                "end": _srt_clock_to_seconds(*[str(v) for v in item.end]),
                "text": item.text,
            }
            for item in subtitles
        ]
        if cues:
            return cues
    except Exception:
        pass
    content = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5", "latin-1"):
        try:
            content = path.read_text(encoding=encoding)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
        except OSError:
            return []
    return parse_srt_text(content) if content else []


def group_cues_into_sentences(
    cues: Sequence[Mapping[str, Any]],
    *,
    max_chars: int = 60,
    max_gap: float = 1.5,
    max_duration: float = 15.0,
) -> list[dict[str, Any]]:
    """把字幕后处理成"句"：过短且紧邻的 cue 合并，语义完整、上下文好读。"""
    sentences: list[dict[str, Any]] = []
    buffer: list[dict[str, Any]] = []

    def flush() -> None:
        if not buffer:
            return
        sentences.append(
            {
                "start": buffer[0]["start"],
                "end": buffer[-1]["end"],
                "text": " ".join(item["text"] for item in buffer),
            }
        )
        buffer.clear()

    for cue in cues:
        text = str(cue.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(cue["start"])
            end = float(cue["end"])
        except (KeyError, TypeError, ValueError):
            continue
        item = {"start": start, "end": end, "text": text}
        if not buffer:
            buffer.append(item)
            continue
        combined_len = sum(len(part["text"]) for part in buffer) + len(text)
        gap = start - buffer[-1]["end"]
        span = end - buffer[0]["start"]
        previous_len = len(buffer[-1]["text"])
        if (
            gap <= max_gap
            and combined_len <= max_chars
            and span <= max_duration
            and (previous_len < 8 or len(text) < 8)
        ):
            buffer.append(item)
        else:
            flush()
            buffer.append(item)
    flush()
    return sentences


# ==========================================================================
# 索引
# ==========================================================================

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS videos (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        path            TEXT NOT NULL UNIQUE,
        path_key        TEXT NOT NULL DEFAULT '',
        lib_id          INTEGER NOT NULL DEFAULT 0,
        rel_path        TEXT NOT NULL DEFAULT '',
        origin_label    TEXT NOT NULL DEFAULT '',
        content_id      TEXT NOT NULL DEFAULT '',
        name            TEXT NOT NULL DEFAULT '',
        dir             TEXT NOT NULL DEFAULT '',
        dir_key         TEXT NOT NULL DEFAULT '',
        publisher       TEXT NOT NULL DEFAULT '',
        duration        REAL NOT NULL DEFAULT 0,
        session_epoch   REAL NOT NULL DEFAULT 0,
        size_bytes      INTEGER NOT NULL DEFAULT 0,
        mtime           REAL NOT NULL DEFAULT 0,
        asr_provider    TEXT NOT NULL DEFAULT '',
        has_word_timing INTEGER NOT NULL DEFAULT 0,
        sentence_count  INTEGER NOT NULL DEFAULT 0,
        indexed_at      REAL NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS material_roots (
        path      TEXT PRIMARY KEY,
        added_at  REAL NOT NULL DEFAULT 0,
        universal INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS libraries (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT NOT NULL DEFAULT '',
        kind         TEXT NOT NULL DEFAULT 'directory',
        root         TEXT NOT NULL DEFAULT '',
        source       TEXT NOT NULL DEFAULT '',
        origin_label TEXT NOT NULL DEFAULT '',
        videos       INTEGER NOT NULL DEFAULT 0,
        sentences    INTEGER NOT NULL DEFAULT 0,
        duration     REAL NOT NULL DEFAULT 0,
        created_at   REAL NOT NULL DEFAULT 0,
        built_at     REAL NOT NULL DEFAULT 0,
        note         TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS import_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        file_name   TEXT NOT NULL DEFAULT '',
        source      TEXT NOT NULL DEFAULT '',
        videos      INTEGER NOT NULL DEFAULT 0,
        sentences   INTEGER NOT NULL DEFAULT 0,
        imported_at REAL NOT NULL DEFAULT 0,
        note        TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sentences (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id  INTEGER NOT NULL,
        seq       INTEGER NOT NULL,
        start     REAL NOT NULL,
        end       REAL NOT NULL,
        raw_text  TEXT NOT NULL,
        norm_text TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sentence_grams (
        gram        INTEGER NOT NULL,
        sentence_id INTEGER NOT NULL,
        PRIMARY KEY (gram, sentence_id)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS suggestions (
        norm_text   TEXT PRIMARY KEY,
        text        TEXT NOT NULL DEFAULT '',
        count       INTEGER NOT NULL DEFAULT 0,
        video_count INTEGER NOT NULL DEFAULT 0
    )
    """,
)


#: 索引语句单独放：老库缺列时，必须先补列再建索引，否则 CREATE INDEX 会报
#: "no such column"，整个素材库直接打不开。
_SCHEMA_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_videos_path_key ON videos(path_key)",
    "CREATE INDEX IF NOT EXISTS idx_videos_dir_key ON videos(dir_key)",
    "CREATE INDEX IF NOT EXISTS idx_videos_publisher ON videos(publisher)",
    "CREATE INDEX IF NOT EXISTS idx_videos_session ON videos(session_epoch)",
    "CREATE INDEX IF NOT EXISTS idx_videos_name ON videos(name)",
    "CREATE INDEX IF NOT EXISTS idx_videos_content_id ON videos(content_id)",
    "CREATE INDEX IF NOT EXISTS idx_videos_lib ON videos(lib_id)",
    "CREATE INDEX IF NOT EXISTS idx_sentences_video ON sentences(video_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_sentences_norm ON sentences(norm_text)",
    "CREATE INDEX IF NOT EXISTS idx_grams_sentence ON sentence_grams(sentence_id)",
    "CREATE INDEX IF NOT EXISTS idx_suggestions_count ON suggestions(count DESC)",
)

_FTS_STATEMENTS = (
    # content='sentences' + content_rowid='id'：FTS 索引直接挂主表行，
    # 主表删除时索引行自动消失，省掉触发器和手工清理。
    "CREATE VIRTUAL TABLE IF NOT EXISTS sent_seg_fts "
    "USING fts5(norm_text, content='sentences', content_rowid='id', tokenize='trigram')",
)


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=15.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA temp_store=MEMORY")
    except sqlite3.Error:
        pass
    return connection


def fts5_available() -> bool:
    """探测当前解释器的 SQLite 是否支持 fts5 + trigram。"""
    global _fts5_state
    if _fts5_state is not None:
        return _fts5_state
    try:
        probe = sqlite3.connect(":memory:")
        try:
            probe.execute("CREATE VIRTUAL TABLE _probe USING fts5(x, tokenize='trigram')")
            probe.execute("INSERT INTO _probe(x) VALUES ('测试文本')")
            probe.execute("SELECT rowid FROM _probe WHERE _probe MATCH '测试'").fetchall()
            _fts5_state = True
        finally:
            probe.close()
    except sqlite3.Error:
        _fts5_state = False
    return _fts5_state


def _escape_like(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _path_key(path: str) -> str:
    """路径比较键：统一斜杠方向 + 小写，容忍 ``E:/a`` 与 ``E:\\a`` 这类写法差异。"""
    return os.path.normcase(str(path or "").strip().replace("/", os.sep))


def _path_keys_match(stored_path: str, value: str) -> bool:
    """路径是否等价。

    Windows 上 ``Path.resolve()`` 有时给 8.3 短名（``ADMINI~1``）有时给规范名
    （``Administrator``），同一个文件会有两种写法。这里两种形式都比一遍，
    避免"按视频筛选""删除视频""标注主播"因为路径写法差异静默失效。
    """
    if _path_key(stored_path) == _path_key(value):
        return True
    try:
        return _path_key(str(Path(stored_path).resolve())) == _path_key(str(Path(value).resolve()))
    except (OSError, ValueError):
        return False


def _relative_to_root(video_path: str, root: str) -> str:
    """视频相对素材根目录的路径（用 \\ 分隔），不在该目录下则返回空串。

    跨机器共享索引时存这个，而不是本机绝对路径。
    """
    video = _path_key(video_path).rstrip("\\")
    base = _path_key(root).rstrip("\\")
    if not base or not video:
        return ""
    if video == base:
        return ""
    prefix = base + "\\"
    if not video.startswith(prefix):
        return ""
    return video[len(prefix):].strip("\\")


def _label_from_rel(rel_path: str) -> str:
    """从相对路径里取一个可读来源标签。

    名创目录形如 ``8.16\\<商品>\\<文件>`` 或 ``7-25\\7-25单品\\<商品>\\<文件>``，
    取"最外层但没有意义的那一层"作为日期标签，取不到就返回空。
    """
    parts = [part for part in re.split(r"[\\/]+", str(rel_path or "")) if part]
    if len(parts) < 2:
        return ""
    first = parts[0]
    # 形如 8.16 / 7-25 / 20260926 的都当日期批次
    if re.fullmatch(r"\d{1,4}([-_.]\d{1,2}){0,2}", first):
        if len(parts) >= 3 and re.fullmatch(r"\d{1,2}[-_.]?\d{0,2}单品", parts[1]):
            return f"{first} / {parts[1]}"
        return first
    # 直属子目录（非日期）也不当作来源，交给调用方用根目录名兜底
    return ""


def _common_parent(paths: Sequence[str]) -> str:
    """取一组路径的公共父目录（用于自动推导"通用素材根目录"）。"""
    cleaned = [_path_key(item).rstrip("\\") for item in paths if str(item or "").strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return str(Path(cleaned[0]).parent)
    split = [item.split("\\") for item in cleaned]
    shared: list[str] = []
    for parts in zip(*split):
        if len(set(parts)) != 1:
            break
        shared.append(parts[0])
    if len(shared) < 2:
        return ""
    return "\\".join(shared)


def _is_local_video_file(video_path: str) -> bool:
    """本机是否存在这个视频文件（导入的共享索引通常不存在）。"""
    raw = str(video_path or "").strip()
    if not raw:
        return False
    try:
        if Path(raw).is_file():
            return True
    except OSError:
        return False
    # 共享索引的占位路径长这样：index\\<来源>\\<相对路径>\\<文件名>
    return not raw.replace("/", "\\").lower().startswith("index\\")


def _video_identity(name: str, publisher: str, rel_path: str, content_id: str = "") -> str:
    """跨机器识别同一个视频的身份字符串。

    优先内容指纹（最可靠）；没有指纹时退化为 主播 + 文件名 + 相对路径——
    因为存在"同名不同场次"（同一文件名出现在多个日期目录下），只看文件名会误并。
    """
    fingerprint = str(content_id or "").strip()
    if fingerprint:
        return "cid:" + fingerprint
    safe_name = str(name or "").strip().lower()
    safe_pub = str(publisher or "").strip().lower()
    safe_rel = str(rel_path or "").strip().replace("/", "\\").lower()
    if safe_rel:
        return f"rel:{safe_pub}|{safe_name}|{safe_rel}"
    return f"np:{safe_pub}|{safe_name}"


#: 3-gram 倒排表用的字符范围（CJK 统一汉字区，够覆盖直播间口播）
_GRAM_CHARS = [chr(code) for code in range(0x4E00, 0xA000)]


def _pack_gram(gram: str) -> int:
    """把 3 个汉字打包成一个整数键（每字 16 位）。"""
    return (ord(gram[0]) << 32) | (ord(gram[1]) << 16) | ord(gram[2])


def _grams_of(text: str, size: int = 3) -> set[int]:
    """一句话的全部去重 n-gram（标点已在归一化时去掉）。"""
    if len(text) < size:
        return set()
    return {_pack_gram(text[index:index + size]) for index in range(len(text) - size + 1)}


def _two_char_gram_candidates(phrase: str) -> list[int]:
    """2 字查询要用到的 3 字窗口：左补任意字 + 右补任意字。"""
    head, tail = phrase[0], phrase[1]
    grams: set[int] = set()
    for char in _GRAM_CHARS:
        grams.add(_pack_gram(char + head + tail))
        grams.add(_pack_gram(head + tail + char))
    return sorted(grams)


def _merge_spans(spans: Iterable[Sequence[int]]) -> list[tuple[int, int]]:
    ordered = sorted((int(s), int(e)) for s, e in spans if int(e) > int(s))
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


class MaterialIndex:
    """素材库索引：建库、增量入库、检索。线程安全靠"每次操作用独立连接"。"""

    def __init__(self, db_path: str | os.PathLike[str]):
        self.db_path = str(db_path)
        self._fts_ok = False
        with self._session() as connection:
            self._fts_ok = self._ensure_schema(connection)

    # ---------------- 连接 ----------------

    class _Session:
        def __init__(self, owner: "MaterialIndex"):
            self.owner = owner
            self.connection: sqlite3.Connection | None = None

        def __enter__(self) -> sqlite3.Connection:
            self.connection = _connect(self.owner.db_path)
            return self.connection

        def __exit__(self, exc_type, exc, tb) -> bool:
            assert self.connection is not None
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
            self.connection.close()
            self.connection = None
            return False

    def _session(self) -> "MaterialIndex._Session":
        return MaterialIndex._Session(self)

    @property
    def fts_enabled(self) -> bool:
        return self._fts_ok

    @staticmethod
    def _ensure_columns(connection: sqlite3.Connection) -> None:
        """老库补列（``CREATE TABLE IF NOT EXISTS`` 不会给已存在的表加列）。"""
        expected = {
            "videos": {
                "path_key": "TEXT NOT NULL DEFAULT ''",
                "lib_id": "INTEGER NOT NULL DEFAULT 0",
                "dir_key": "TEXT NOT NULL DEFAULT ''",
                "rel_path": "TEXT NOT NULL DEFAULT ''",
                "origin_label": "TEXT NOT NULL DEFAULT ''",
                "content_id": "TEXT NOT NULL DEFAULT ''",
                "asr_provider": "TEXT NOT NULL DEFAULT ''",
                "has_word_timing": "INTEGER NOT NULL DEFAULT 0",
                "sentence_count": "INTEGER NOT NULL DEFAULT 0",
                "indexed_at": "REAL NOT NULL DEFAULT 0",
            },
            "material_roots": {
                "universal": "INTEGER NOT NULL DEFAULT 0",
            },
        }
        for table, columns in expected.items():
            try:
                existing = {
                    str(row["name"])
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
            except sqlite3.Error:
                continue
            if not existing:
                continue
            for column, definition in columns.items():
                if column in existing:
                    continue
                try:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                    )
                except sqlite3.Error:
                    pass
        # 补列后回填比较键，保证老数据也能被路径筛选命中
        try:
            connection.execute(
                "UPDATE videos SET path_key=lower(replace(path, '/', '\\')) "
                "WHERE path_key='' AND path<>''"
            )
            connection.execute(
                "UPDATE videos SET dir_key=lower(replace(dir, '/', '\\')) "
                "WHERE dir_key='' AND dir<>''"
            )
        except sqlite3.Error:
            pass

    def _ensure_schema(self, connection: sqlite3.Connection) -> bool:
        # 先建表，再补列，最后才建索引：索引可能引用新列，顺序错了会直接报错。
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        self._ensure_columns(connection)
        for statement in _SCHEMA_INDEX_STATEMENTS:
            try:
                connection.execute(statement)
            except sqlite3.Error:
                pass
        meta = {
            row["key"]: row["value"]
            for row in connection.execute("SELECT key, value FROM meta").fetchall()
        }
        stored = meta.get("schema_version")
        if stored is not None and stored != str(SCHEMA_VERSION):
            # 结构升级：句子只是视频的派生物，清空后重新入库即可，
            # 避免把旧结构的索引当成有效数据继续用。
            connection.execute("DELETE FROM sentences")
            connection.execute("DELETE FROM sentence_grams")
            connection.execute("DELETE FROM suggestions")
            connection.execute("UPDATE videos SET sentence_count=0")
            for table in ("sent_seg_fts", "sent_bi_fts"):
                try:
                    connection.execute(f"DROP TABLE IF EXISTS {table}")
                except sqlite3.Error:
                    pass
        fts_ok = fts5_available()
        if fts_ok:
            for statement in _FTS_STATEMENTS:
                connection.execute(statement)
            # 主表已经清空/重建过，FTS 索引要跟着重建一次
            try:
                connection.execute("INSERT INTO sent_seg_fts(sent_seg_fts) VALUES('rebuild')")
            except sqlite3.Error:
                pass
        connection.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        connection.commit()
        # 素材根目录：没有登记过就从现有视频推导一个，保证相对路径可用
        try:
            self.seed_root_from_videos()
        except Exception:
            pass
        # 老数据升级：把没有索引归属的视频按素材目录归类到各自的索引
        try:
            self.migrate_libraries()
        except Exception:
            pass
        return fts_ok

    def _delete_sentence_rows(self, connection: sqlite3.Connection, video_id: int) -> None:
        """删掉一条视频的句子及其 gram 倒排（FTS 挂在主表上会自动跟随）。"""
        ids = [
            int(row["id"])
            for row in connection.execute(
                "SELECT id FROM sentences WHERE video_id=?", (int(video_id),)
            ).fetchall()
        ]
        if ids:
            for index in range(0, len(ids), 500):
                batch = ids[index:index + 500]
                placeholders = ",".join("?" for _ in batch)
                connection.execute(
                    f"DELETE FROM sentence_grams WHERE sentence_id IN ({placeholders})", batch
                )
        connection.execute("DELETE FROM sentences WHERE video_id=?", (int(video_id),))

    # ---------------- 入库 ----------------

    def resolve_library_for_video(self, connection: sqlite3.Connection, video_path: str,
                                  default_lib_id: int = 0) -> int:
        """视频属于哪个索引：优先匹配已登记的素材目录（最具体的那个）。"""
        rows = connection.execute(
            "SELECT id, root FROM libraries WHERE kind='directory' AND root<>''"
        ).fetchall()
        best_id, best_len = 0, -1
        for row in rows:
            root = str(row["root"])
            if _relative_to_root(video_path, root):
                length = len(_path_key(root))
                if length > best_len:
                    best_id, best_len = int(row["id"]), length
        return best_id or int(default_lib_id or 0)

    def upsert_video(
        self,
        *,
        video_path: str,
        cues: Sequence[Mapping[str, Any]],
        publisher: str = "",
        duration: float = 0.0,
        session_epoch: float = 0.0,
        asr_provider: str = "",
        has_word_timing: bool = False,
        size_bytes: int = 0,
        mtime: float = 0.0,
        max_chars: int = 60,
        content_id: str = "",
        rel_path: str = "",
        origin_label: str = "",
        reuse_existing_id: int | None = None,
        lib_id: int = 0,
    ) -> dict[str, Any]:
        """把一条视频的字幕写进索引（同路径覆盖更新）。

        ``reuse_existing_id``：把这份字幕挂到一条**已存在**的视频记录上
        （导入索引时如果本地已经有同名同主播的视频，就直接接到它上面，
        而不是多出一条"指向不存在的占位路径"的重复记录）。

        ``rel_path`` / ``origin_label`` 是**跨机器共享用**的身份信息：相对素材
        根目录的路径 + 可读来源标签。不传时按已登记的素材根目录自动推断。
        """
        path = str(Path(video_path).resolve()) if video_path else ""
        sentences = group_cues_into_sentences(cues, max_chars=max_chars)
        folder = str(Path(path).parent) if path else ""
        file_name = Path(path).name if path else ""
        if not duration and sentences:
            duration = float(sentences[-1]["end"])

        with self._session() as connection:
            if not rel_path:
                rel_path, auto_label = self._relative_for(connection, path)
                if not origin_label:
                    origin_label = auto_label

            reuse_row = None
            if reuse_existing_id:
                reuse_row = connection.execute(
                    "SELECT id FROM videos WHERE id=?", (int(reuse_existing_id),)
                ).fetchone()
            if reuse_row:
                # 接到本地已有的那条视频上：保持它原来的真实路径与文件信息
                video_id = int(reuse_row["id"])
                self._delete_sentence_rows(connection, video_id)
                existing = connection.execute(
                    "SELECT path, path_key, dir, dir_key, name, rel_path, origin_label, "
                    "content_id FROM videos WHERE id=?", (video_id,)
                ).fetchone()
                connection.execute(
                    """
                    UPDATE videos
                       SET publisher=CASE WHEN ?<>'' THEN ? ELSE publisher END,
                           duration=?, session_epoch=?,
                           asr_provider=CASE WHEN ?<>'' THEN ? ELSE asr_provider END,
                           has_word_timing=?, sentence_count=?, indexed_at=?,
                           rel_path=CASE WHEN ?<>'' THEN ? ELSE rel_path END,
                           origin_label=CASE WHEN ?<>'' THEN ? ELSE origin_label END,
                           content_id=CASE WHEN ?<>'' THEN ? ELSE content_id END
                     WHERE id=?
                    """,
                    (
                        publisher, publisher,
                        float(duration), float(session_epoch or 0),
                        asr_provider, asr_provider,
                        1 if has_word_timing else 0, len(sentences), time.time(),
                        str(rel_path or ""), str(rel_path or ""),
                        str(origin_label or ""), str(origin_label or ""),
                        str(content_id or ""), str(content_id or ""),
                        video_id,
                    ),
                )
            else:
                row = connection.execute("SELECT id FROM videos WHERE path=?", (path,)).fetchone()
                if row:
                    video_id = int(row["id"])
                    self._delete_sentence_rows(connection, video_id)
                    connection.execute("DELETE FROM videos WHERE id=?", (video_id,))
                resolved_lib = int(lib_id or 0) or self.resolve_library_for_video(connection, path)
                cursor = connection.execute(
                    """
                    INSERT INTO videos(path, path_key, lib_id, rel_path, origin_label, content_id, name,
                                       dir, dir_key, publisher, duration, session_epoch,
                                       size_bytes, mtime, asr_provider, has_word_timing,
                                       sentence_count, indexed_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        path, _path_key(path), resolved_lib,
                        str(rel_path or ""), str(origin_label or ""),
                        str(content_id or ""), file_name, folder, _path_key(folder), publisher,
                        float(duration), float(session_epoch or 0), int(size_bytes or 0),
                        float(mtime or 0), asr_provider, 1 if has_word_timing else 0,
                        len(sentences), time.time(),
                    ),
                )
                video_id = int(cursor.lastrowid)
                if resolved_lib:
                    self._refresh_library_stats(connection, resolved_lib)

            inserted = 0
            gram_rows: list[tuple[int, int]] = []
            suggestion_rows: dict[str, str] = {}
            sentence_rows: list[tuple[Any, ...]] = []
            for seq, sentence in enumerate(sentences):
                norm = normalize_for_match(sentence["text"])
                if not norm:
                    continue
                # 这一句在 sentence_rows 里的槽位，用来推它的 sentence id
                slot = len(sentence_rows)
                sentence_rows.append(
                    (
                        video_id, seq, float(sentence["start"]), float(sentence["end"]),
                        sentence["text"], norm,
                    )
                )
                inserted += 1
                for gram in _grams_of(norm):
                    gram_rows.append((gram, slot))
                if len(norm) >= 4 and norm not in suggestion_rows:
                    suggestion_rows[norm] = str(sentence["text"])[:80]

            if sentence_rows:
                connection.executemany(
                    "INSERT INTO sentences(video_id, seq, start, end, raw_text, norm_text) "
                    "VALUES(?,?,?,?,?,?)",
                    sentence_rows,
                )
                # 主表 id 是自增连续的：最后一条 id 往前推 inserted 条即可
                last_id = int(
                    connection.execute("SELECT MAX(id) AS n FROM sentences").fetchone()["n"] or 0
                )
                first_id = last_id - inserted + 1
                if gram_rows:
                    connection.executemany(
                        "INSERT OR IGNORE INTO sentence_grams(gram, sentence_id) VALUES(?,?)",
                        [(gram, first_id + slot) for gram, slot in gram_rows],
                    )
                if self._fts_ok:
                    # content='sentences' + content_rowid='id'：FTS 跟着主表行走
                    connection.executemany(
                        "INSERT INTO sent_seg_fts(rowid, norm_text) VALUES(?,?)",
                        [(first_id + slot, row[5]) for slot, row in enumerate(sentence_rows)],
                    )
            connection.execute(
                "UPDATE videos SET sentence_count=? WHERE id=?", (inserted, video_id)
            )
            self._refresh_suggestions(connection, suggestion_rows)
        return {"video_id": video_id, "path": path, "sentences": inserted, "publisher": publisher}

    @staticmethod
    def _refresh_suggestions(
        connection: sqlite3.Connection, texts: Iterable[str] | Mapping[str, str]
    ) -> None:
        """把字幕块收进"热门文案"，用于搜索框联想。

        接受"文本序列"或"归一化键 -> 展示文本"的映射（后者是入库时顺手
        去重后的结果，省一次归一化）。一条视频一次批量写，避免长素材库把
        整场字幕攒在内存里。
        """
        if isinstance(texts, Mapping):
            entries = [(str(key), str(value)) for key, value in texts.items()]
        else:
            entries = []
            for text in texts:
                norm = normalize_for_match(text)
                if len(norm) >= 4:
                    entries.append((norm, str(text)[:80]))
        if not entries:
            return
        norms = [key for key, _ in entries]
        if len(norms) < 400:
            placeholders = ",".join("?" for _ in norms)
            existing = {
                str(row["norm_text"])
                for row in connection.execute(
                    f"SELECT norm_text FROM suggestions WHERE norm_text IN ({placeholders})",
                    norms,
                ).fetchall()
            }
        else:
            existing = set()
        fresh = [(key, value) for key, value in entries if key not in existing]
        if fresh:
            connection.executemany(
                "INSERT OR IGNORE INTO suggestions(norm_text, text, count, video_count) "
                "VALUES(?,?,1,1)",
                fresh,
            )
        if existing:
            connection.executemany(
                "UPDATE suggestions SET count=count+1 WHERE norm_text=?",
                [(key,) for key in existing],
            )

    # ---------------- 批量导入（共享索引用） ----------------

    def begin_bulk_import(self) -> None:
        """导入大量视频前清空 FTS：先把数据灌进去，最后一次性重建索引。"""
        if not self._fts_ok:
            return
        with self._session() as connection:
            try:
                connection.execute("DROP TABLE IF EXISTS sent_seg_fts")
            except sqlite3.Error:
                pass

    def finish_bulk_import(self) -> None:
        """重建 FTS 与热门文案（批量导入收尾）。"""
        if self._fts_ok:
            with self._session() as connection:
                for statement in _FTS_STATEMENTS:
                    try:
                        connection.execute(statement)
                    except sqlite3.Error:
                        pass
                try:
                    connection.execute(
                        "INSERT INTO sent_seg_fts(sent_seg_fts) VALUES('rebuild')"
                    )
                except sqlite3.Error:
                    pass
        self.rebuild_suggestions()

    def _find_video_row(
        self, connection: sqlite3.Connection, name: str, publisher: str, rel_path: str = ""
    ) -> sqlite3.Row | None:
        """找本地已有的同一条视频。

        注意：**同名不同场次**（同一文件名出现在多个日期目录下）很常见，
        所以只在相对路径也一致（或两边都没有相对路径）时才算同一条，
        否则会把不同场次合并成一行、丢数据。
        """
        target_rel = str(rel_path or "").strip().lower().replace("/", "\\")
        if publisher:
            rows = connection.execute(
                "SELECT id, path, rel_path FROM videos WHERE name=? AND publisher=?",
                (name, publisher),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT id, path, rel_path FROM videos WHERE name=?", (name,)
            ).fetchall()
        if not rows:
            return None
        for row in rows:
            stored = str(row["rel_path"] or "").strip().lower().replace("/", "\\")
            if stored == target_rel:
                return row
        return None

    def bulk_insert_video(
        self,
        connection: sqlite3.Connection,
        *,
        name: str,
        publisher: str,
        sentences: Sequence[Mapping[str, Any]],
        duration: float = 0.0,
        session_epoch: float = 0.0,
        asr_provider: str = "imported",
        has_word_timing: bool = False,
        rel_path: str = "",
        origin_label: str = "",
        content_id: str = "",
        placeholder_path: str = "",
        lib_id: int = 0,
    ) -> dict[str, Any]:
        """把一条视频的句子与 gram 直接批量写入（调用方负责最后重建 FTS）。

        共享索引里已经带好句子切分与 gram 倒排，这里不走 upsert 的逐条
        FTS 写入路径，导入几百个视频才不至于跑几十分钟。
        """
        existing = self._find_video_row(connection, name, publisher, rel_path)
        if existing is not None:
            # 本地已有同一个视频：把导入的字幕挂到它上面，保留真实文件路径
            # （这样能直接跳转播放）。同名不同场次不会走到这里（上面按相对路径区分了）。
            video_id = int(existing["id"])
            self._delete_sentence_rows(connection, video_id)
            connection.execute(
                """
                UPDATE videos
                   SET publisher=CASE WHEN ?<>'' THEN ? ELSE publisher END,
                       duration=?, session_epoch=?,
                       asr_provider=CASE WHEN ?<>'' THEN ? ELSE asr_provider END,
                       has_word_timing=?, sentence_count=?, indexed_at=?,
                       rel_path=CASE WHEN ?<>'' THEN ? ELSE rel_path END,
                       origin_label=CASE WHEN ?<>'' THEN ? ELSE origin_label END,
                       content_id=CASE WHEN ?<>'' THEN ? ELSE content_id END,
                       lib_id=CASE WHEN ?>0 THEN ? ELSE lib_id END
                 WHERE id=?
                """,
                (
                    publisher, publisher, float(duration), float(session_epoch or 0),
                    asr_provider, asr_provider, 1 if has_word_timing else 0,
                    len(sentences), time.time(),
                    str(rel_path or ""), str(rel_path or ""),
                    str(origin_label or ""), str(origin_label or ""),
                    str(content_id or ""), str(content_id or ""),
                    int(lib_id or 0), int(lib_id or 0), video_id,
                ),
            )
            reused = True
        else:
            # 这条视频本机没有：可能有一行"占位"的记录（之前导入过同一条），
            # 先清掉再重写，避免留下指向不存在文件的重复行。
            placeholder = connection.execute(
                "SELECT id FROM videos WHERE path=?",
                (str(placeholder_path or f"shared/{name}"),),
            ).fetchone()
            if placeholder is not None:
                old_id = int(placeholder["id"])
                self._delete_sentence_rows(connection, old_id)
                connection.execute("DELETE FROM videos WHERE id=?", (old_id,))
            path = str(placeholder_path or f"shared/{name}")
            cursor = connection.execute(
                """
                INSERT INTO videos(path, path_key, lib_id, rel_path, origin_label, content_id, name,
                                   dir, dir_key, publisher, duration, session_epoch,
                                   size_bytes, mtime, asr_provider, has_word_timing,
                                   sentence_count, indexed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    path, "", int(lib_id or 0), str(rel_path or ""), str(origin_label or ""),
                    str(content_id or ""), name, "", "", publisher,
                    float(duration), float(session_epoch or 0), 0, 0.0,
                    asr_provider, 1 if has_word_timing else 0, len(sentences), time.time(),
                ),
            )
            video_id = int(cursor.lastrowid)
            reused = False

        rows: list[tuple[Any, ...]] = []
        for seq, sentence in enumerate(sentences):
            text = str(sentence.get("text") or "")
            norm = normalize_for_match(text)
            if not norm:
                continue
            rows.append(
                (
                    video_id, seq, float(sentence.get("start") or 0.0),
                    float(sentence.get("end") or 0.0), text, norm,
                )
            )
        if not rows:
            connection.execute(
                "UPDATE videos SET sentence_count=0 WHERE id=?", (video_id,)
            )
            return {"video_id": video_id, "sentences": 0, "reused_local": reused}

        connection.executemany(
            "INSERT INTO sentences(video_id, seq, start, end, raw_text, norm_text) "
            "VALUES(?,?,?,?,?,?)",
            rows,
        )
        last_id = int(
            connection.execute("SELECT MAX(id) AS n FROM sentences").fetchone()["n"] or 0
        )
        first_id = last_id - len(rows) + 1
        gram_rows: list[tuple[int, int]] = []
        for slot, row in enumerate(rows):
            for gram in _grams_of(row[5]):
                gram_rows.append((gram, first_id + slot))
        if gram_rows:
            connection.executemany(
                "INSERT OR IGNORE INTO sentence_grams(gram, sentence_id) VALUES(?,?)",
                gram_rows,
            )
        connection.execute(
            "UPDATE videos SET sentence_count=? WHERE id=?", (len(rows), video_id)
        )
        return {"video_id": video_id, "sentences": len(rows), "reused_local": reused}

    # ---------------- 索引库（一个素材目录 / 一个导入包 = 一个索引） ----------------

    def library_name_for_root(self, root: str) -> str:
        """素材目录的索引名：取目录名（`E:\\切片\\jccc` -> `jccc`）。"""
        name = Path(str(root or "")).name.strip()
        return name or str(root or "").strip()

    def create_library(
        self,
        *,
        name: str,
        kind: str = "directory",
        root: str = "",
        source: str = "",
        origin_label: str = "",
        note: str = "",
    ) -> int:
        with self._session() as connection:
            cursor = connection.execute(
                """
                INSERT INTO libraries(name, kind, root, source, origin_label, created_at, built_at, note)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    str(name or "").strip() or "未命名索引",
                    str(kind or "directory"),
                    str(root or ""),
                    str(source or ""),
                    str(origin_label or ""),
                    time.time(), time.time(), str(note or ""),
                ),
            )
            return int(cursor.lastrowid)

    def update_library(self, lib_id: int, **fields: Any) -> None:
        allowed = {"name", "kind", "root", "source", "origin_label", "note"}
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key}=?")
                params.append(str(value or ""))
        if not sets:
            return
        params.append(int(lib_id))
        with self._session() as connection:
            connection.execute(f"UPDATE libraries SET {', '.join(sets)} WHERE id=?", params)

    def find_library_by_root(self, root: str) -> sqlite3.Row | None:
        target = _path_key(root).rstrip("\\")
        if not target:
            return None
        with self._session() as connection:
            rows = connection.execute(
                "SELECT * FROM libraries WHERE root<>'' ORDER BY id"
            ).fetchall()
        for row in rows:
            if _path_key(str(row["root"])).rstrip("\\") == target:
                return row
        return None

    def library(self, lib_id: int) -> sqlite3.Row | None:
        with self._session() as connection:
            return connection.execute(
                "SELECT * FROM libraries WHERE id=?", (int(lib_id),)
            ).fetchone()

    def _refresh_library_stats(self, connection: sqlite3.Connection, lib_id: int) -> None:
        row = connection.execute(
            "SELECT COUNT(*) AS videos, COALESCE(SUM(sentence_count),0) AS sentences, "
            "COALESCE(SUM(duration),0) AS duration FROM videos WHERE lib_id=?",
            (int(lib_id),),
        ).fetchone()
        connection.execute(
            "UPDATE libraries SET videos=?, sentences=?, duration=?, built_at=? WHERE id=?",
            (
                int(row["videos"] or 0), int(row["sentences"] or 0),
                float(row["duration"] or 0.0), time.time(), int(lib_id),
            ),
        )

    def libraries(self) -> list[dict[str, Any]]:
        """所有索引（素材目录建的 + 导入的），供界面列表与搜索范围选择。"""
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT l.id, l.name, l.kind, l.root, l.source, l.origin_label,
                       l.created_at, l.built_at, l.note,
                       COUNT(v.id) AS videos,
                       COALESCE(SUM(v.sentence_count),0) AS sentences,
                       COALESCE(SUM(v.duration),0) AS duration,
                       SUM(CASE WHEN v.path LIKE 'index\\%' THEN 1 ELSE 0 END) AS need_download
                FROM libraries l LEFT JOIN videos v ON v.lib_id = l.id
                GROUP BY l.id
                ORDER BY l.kind DESC, l.built_at DESC, l.id ASC
                """
            ).fetchall()
        result = []
        for row in rows:
            result.append(
                {
                    "id": int(row["id"]),
                    "name": str(row["name"] or ""),
                    "kind": str(row["kind"] or "directory"),
                    "root": str(row["root"] or ""),
                    "source": str(row["source"] or ""),
                    "origin_label": str(row["origin_label"] or ""),
                    "videos": int(row["videos"] or 0),
                    "sentences": int(row["sentences"] or 0),
                    "duration": float(row["duration"] or 0.0),
                    "need_download": int(row["need_download"] or 0),
                    "created_at": float(row["created_at"] or 0.0),
                    "built_at": float(row["built_at"] or 0.0),
                    "note": str(row["note"] or ""),
                }
            )
        return result

    def remove_library(self, lib_id: int) -> dict[str, int]:
        """删掉一个索引（连同它的视频与句子），不影响其他索引。"""
        lib_id = int(lib_id)
        with self._session() as connection:
            video_ids = [
                int(row["id"])
                for row in connection.execute(
                    "SELECT id FROM videos WHERE lib_id=?", (lib_id,)
                ).fetchall()
            ]
            removed_sentences = 0
            for index in range(0, len(video_ids), 200):
                batch = video_ids[index:index + 200]
                marks = ",".join("?" for _ in batch)
                sentence_ids = [
                    int(row["id"]) for row in connection.execute(
                        f"SELECT id FROM sentences WHERE video_id IN ({marks})", batch
                    ).fetchall()
                ]
                removed_sentences += len(sentence_ids)
                for start in range(0, len(sentence_ids), 800):
                    chunk = sentence_ids[start:start + 800]
                    chunk_marks = ",".join("?" for _ in chunk)
                    connection.execute(
                        f"DELETE FROM sentence_grams WHERE sentence_id IN ({chunk_marks})", chunk
                    )
                connection.execute(f"DELETE FROM sentences WHERE video_id IN ({marks})", batch)
                connection.execute(f"DELETE FROM videos WHERE id IN ({marks})", batch)
            connection.execute("DELETE FROM libraries WHERE id=?", (lib_id,))
            connection.commit()
        # FTS 是外部内容表，删完要重建一次，否则会留"幽灵"命中
        if self._fts_ok:
            with self._session() as connection:
                try:
                    connection.execute(
                        "INSERT INTO sent_seg_fts(sent_seg_fts) VALUES('rebuild')"
                    )
                except sqlite3.Error:
                    pass
        self.rebuild_suggestions()
        return {"videos": len(video_ids), "sentences": removed_sentences}

    def migrate_libraries(self) -> dict[str, Any]:
        """把历史数据（没有索引归属的视频）按素材目录分组，补建索引。

        老版本是"一个全局大库"，v3 起改成"一个素材目录 = 一个索引"，
        所以升级后要把已有视频分到各自目录对应的索引里，否则列表是空的。
        """
        with self._session() as connection:
            unassigned = connection.execute(
                "SELECT id, path FROM videos WHERE lib_id=0"
            ).fetchall()
            if not unassigned:
                existing = connection.execute("SELECT COUNT(*) AS n FROM libraries").fetchone()["n"]
                return {"created": 0, "assigned": 0, "libraries": int(existing or 0)}

            roots = [
                str(row["path"])
                for row in connection.execute(
                    "SELECT path FROM material_roots ORDER BY length(path) DESC"
                ).fetchall()
            ]
            fallback = _common_parent(
                [str(row["path"]) for row in unassigned if not str(row["path"]).startswith("index\\")]
            )
            groups: dict[str, list[int]] = {}
            for row in unassigned:
                video_path = str(row["path"])
                chosen = ""
                for root in roots:
                    if _relative_to_root(video_path, root):
                        chosen = root
                        break
                if not chosen:
                    chosen = fallback
                if not chosen:
                    chosen = "(未归类)"
                groups.setdefault(chosen, []).append(int(row["id"]))

            created = 0
            assigned = 0
            for root, ids in groups.items():
                name = self.library_name_for_root(root) if root != "(未归类)" else "未归类"
                cursor = connection.execute(
                    """
                    INSERT INTO libraries(name, kind, root, origin_label, created_at, built_at, note)
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        name, "directory", "" if root == "(未归类)" else root, name,
                        time.time(), time.time(), f"由旧数据自动归类（{root}）",
                    ),
                )
                lib_id = int(cursor.lastrowid)
                created += 1
                for index in range(0, len(ids), 400):
                    batch = ids[index:index + 400]
                    marks = ",".join("?" for _ in batch)
                    connection.execute(
                        f"UPDATE videos SET lib_id=? WHERE id IN ({marks})", [lib_id, *batch]
                    )
                    assigned += len(batch)
                self._refresh_library_stats(connection, lib_id)
            connection.commit()
            total = connection.execute("SELECT COUNT(*) AS n FROM libraries").fetchone()["n"]
        return {"created": created, "assigned": assigned, "libraries": int(total or 0)}

    def rebuild_suggestions(self, limit: int = 20000) -> int:
        """按句子重新统计热门文案。"""
        with self._session() as connection:
            connection.execute("DELETE FROM suggestions")
            rows = connection.execute(
                """
                SELECT norm_text, MIN(raw_text) AS sample, COUNT(*) AS hits,
                       COUNT(DISTINCT video_id) AS videos
                FROM sentences
                WHERE length(norm_text) >= 4
                GROUP BY norm_text
                ORDER BY hits DESC, length(norm_text) ASC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            connection.executemany(
                "INSERT OR REPLACE INTO suggestions(norm_text, text, count, video_count) "
                "VALUES(?,?,?,?)",
                [
                    (row["norm_text"], str(row["sample"])[:80], int(row["hits"]), int(row["videos"]))
                    for row in rows
                ],
            )
            return len(rows)

    def remove_video(self, video_path: str) -> None:
        with self._session() as connection:
            row = None
            if video_path:
                row = connection.execute(
                    "SELECT id FROM videos WHERE path_key=?", (_path_key(video_path),)
                ).fetchone()
                if not row:
                    matched = self._match_rows_by_path(
                        connection, "videos", "path", video_path, limit=1
                    )
                    if matched:
                        row = connection.execute(
                            "SELECT id FROM videos WHERE path=?", (matched[0],)
                        ).fetchone()
            if not row:
                return
            video_id = int(row["id"])
            self._delete_sentence_rows(connection, video_id)
            connection.execute("DELETE FROM videos WHERE id=?", (video_id,))

    def prune_missing(self) -> int:
        """清理索引里源文件已经不在的记录。"""
        removed = 0
        with self._session() as connection:
            rows = connection.execute("SELECT id, path FROM videos").fetchall()
            for row in rows:
                if Path(str(row["path"])).is_file():
                    continue
                video_id = int(row["id"])
                self._delete_sentence_rows(connection, video_id)
                connection.execute("DELETE FROM videos WHERE id=?", (video_id,))
                removed += 1
        return removed

    def clear(self) -> None:
        with self._session() as connection:
            # 索引条目一起清掉：否则会留下"0 视频"的空索引挂在列表里，
            # 用户清空后看到一排行还以为没清干净。
            for table in ("sentences", "sentence_grams", "suggestions", "videos",
                          "libraries", "import_history", "material_roots"):
                connection.execute(f"DELETE FROM {table}")
            if self._fts_ok:
                try:
                    connection.execute("INSERT INTO sent_seg_fts(sent_seg_fts) VALUES('rebuild')")
                except sqlite3.Error:
                    pass


    def set_publisher_for_folder(self, folder: str, publisher: str) -> int:
        """给某个目录下的所有视频统一标注主播名。"""
        with self._session() as connection:
            target = _path_key(folder)
            rows = connection.execute(
                "SELECT id FROM videos WHERE dir_key=?", (target,)
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if not ids:
                matched = set(self._match_rows_by_path(connection, "videos", "dir", folder))
                if matched:
                    for index in range(0, len(matched), 400):
                        batch = sorted(matched)[index:index + 400]
                        placeholders = ",".join("?" for _ in batch)
                        rows = connection.execute(
                            f"SELECT id FROM videos WHERE path IN ({placeholders})", batch
                        ).fetchall()
                        ids.extend(int(row["id"]) for row in rows)
            if not ids:
                return 0
            updated = 0
            for index in range(0, len(ids), 400):
                batch = ids[index:index + 400]
                placeholders = ",".join("?" for _ in batch)
                cursor = connection.execute(
                    f"UPDATE videos SET publisher=? WHERE id IN ({placeholders})",
                    [str(publisher or "")] + batch,
                )
                updated += int(cursor.rowcount or 0)
            return updated

    def publisher_of_folder(self, folder: str) -> str:
        with self._session() as connection:
            row = connection.execute(
                "SELECT publisher FROM videos WHERE dir_key=? AND publisher<>'' "
                "ORDER BY session_epoch DESC LIMIT 1",
                (_path_key(folder),),
            ).fetchone()
            if row:
                return str(row["publisher"])
            matched = set(self._match_rows_by_path(connection, "videos", "dir", folder))
            if not matched:
                return ""
            for index in range(0, len(matched), 400):
                batch = sorted(matched)[index:index + 400]
                placeholders = ",".join("?" for _ in batch)
                row = connection.execute(
                    f"SELECT publisher FROM videos WHERE path IN ({placeholders}) "
                    "AND publisher<>'' ORDER BY session_epoch DESC LIMIT 1",
                    batch,
                ).fetchone()
                if row:
                    return str(row["publisher"])
        return ""

    # ---------------- 素材根目录（相对路径的基准） ----------------

    def root_rels(self, universal_only: bool = False) -> list[str]:
        """已登记素材根目录（按路径长度倒序，便于匹配最具体的那个）。"""
        with self._session() as connection:
            if universal_only:
                rows = connection.execute(
                    "SELECT path FROM material_roots WHERE universal=1 "
                    "ORDER BY length(path) DESC"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT path FROM material_roots ORDER BY length(path) DESC"
                ).fetchall()
        return [str(row["path"]) for row in rows]

    def add_root(self, root: str, *, universal: bool = False) -> int:
        """登记素材根目录，并回填该目录下视频的相对路径。

        ``universal=True`` 表示"通用根"（例如 ``E:\\切片``）：以后不管从哪个
        子目录入库，都按它算相对路径，这样跨机器共享的身份才稳定。
        返回本次回填了多少条。
        """
        target = str(root or "").strip()
        if not target:
            return 0
        with self._session() as connection:
            connection.execute(
                "INSERT INTO material_roots(path, added_at, universal) VALUES(?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET universal=max(universal, excluded.universal)",
                (target, time.time(), 1 if universal else 0),
            )
            return self._backfill_relative(connection, target)

    def _backfill_relative(self, connection: sqlite3.Connection, root: str) -> int:
        key = _path_key(root).rstrip("\\")
        like = key + "\\%"
        rows = connection.execute(
            "SELECT id, path FROM videos WHERE lower(replace(path, '/', '\\')) LIKE ? "
            "AND (rel_path='' OR origin_label='')",
            (like,),
        ).fetchall()
        updated = 0
        for row in rows:
            video_path = str(row["path"])
            rel = _relative_to_root(video_path, root)
            if not rel:
                continue
            # 只在拿到非空值时写入：空值留给导出时的"父目录名"兜底，
            # 否则同名视频会因为没有 rel_path 被误并成一个。
            connection.execute(
                "UPDATE videos SET rel_path=?, origin_label=? WHERE id=?",
                (rel, _label_from_rel(rel) or Path(root).name, int(row["id"])),
            )
            updated += 1
        return updated

    def _relative_for(self, connection: sqlite3.Connection, video_path: str) -> tuple[str, str]:
        """算出视频的相对路径与来源标签：先试已登记的根，再回退到共同父目录。"""
        for root in self.root_rels():
            guess = _relative_to_root(video_path, root)
            if guess:
                return guess, _label_from_rel(guess) or Path(root).name
        cached = getattr(self, "_fallback_root_cache", None)
        if cached is None:
            paths = [
                str(row["path"])
                for row in connection.execute(
                    "SELECT path FROM videos WHERE path<>'' AND path NOT LIKE 'index\\%'"
                ).fetchall()
            ]
            paths.append(str(video_path))
            cached = _common_parent(paths)
            self._fallback_root_cache = cached
        if cached:
            guess = _relative_to_root(video_path, cached)
            if guess:
                return guess, _label_from_rel(guess) or Path(cached).name
        return "", ""

    def seed_root_from_videos(self) -> str:
        """没有登记过根目录时，从现有视频路径推导一个通用根并回填相对路径。

        素材往往分放在几个子目录（例如 ``E:\\切片\\录屏``、``E:\\切片\\jccc\\单品短素材``），
        共享索引要稳定的相对路径，就得有一个共同的基准目录。
        """
        with self._session() as connection:
            if self.root_rels():
                return ""
            paths = [
                str(row["path"])
                for row in connection.execute(
                    "SELECT path FROM videos WHERE path<>'' AND path NOT LIKE 'index\\%'"
                ).fetchall()
            ]
            root = _common_parent(paths)
            if not root:
                return ""
            connection.execute(
                "INSERT OR REPLACE INTO material_roots(path, added_at, universal) VALUES(?,?,1)",
                (root, time.time()),
            )
            self._backfill_relative(connection, root)
            return root

    def set_relative(self, video_id: int, rel_path: str, origin_label: str = "") -> None:
        with self._session() as connection:
            connection.execute(
                "UPDATE videos SET rel_path=?, origin_label=? WHERE id=?",
                (str(rel_path or ""), str(origin_label or ""), int(video_id)),
            )

    def find_video_by_name(self, name: str, publisher: str = "") -> sqlite3.Row | None:
        """按文件名（+可选主播）找本地视频，用于把导入的索引接上本地文件。"""
        target = str(name or "").strip()
        if not target:
            return None
        with self._session() as connection:
            if publisher:
                row = connection.execute(
                    "SELECT * FROM videos WHERE name=? AND publisher=? "
                    "ORDER BY session_epoch DESC LIMIT 1",
                    (target, publisher),
                ).fetchone()
                if row:
                    return row
            return connection.execute(
                "SELECT * FROM videos WHERE name=? ORDER BY session_epoch DESC LIMIT 1",
                (target,),
            ).fetchone()

    def known_identities(self) -> set:
        """已有视频的跨机身份集合，导入时用来去重。"""
        with self._session() as connection:
            rows = connection.execute(
                "SELECT name, publisher, rel_path, content_id FROM videos"
            ).fetchall()
        identities = set()
        for row in rows:
            identities.add(_video_identity(
                str(row["name"]), str(row["publisher"]), str(row["rel_path"]),
                str(row["content_id"]),
            ))
        return identities

    # ---------------- 统计 ----------------

    def stats(self) -> dict[str, Any]:
        with self._session() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS videos, COALESCE(SUM(duration),0) AS duration FROM videos"
            ).fetchone()
            sentence_row = connection.execute("SELECT COUNT(*) AS n FROM sentences").fetchone()
            publishers = [
                {
                    "publisher": str(item["publisher"] or ""),
                    "videos": int(item["videos"] or 0),
                    "sentences": int(item["sentences"] or 0),
                }
                for item in connection.execute(
                    """
                    SELECT publisher, COUNT(*) AS videos, COALESCE(SUM(sentence_count),0) AS sentences
                    FROM videos GROUP BY publisher
                    ORDER BY videos DESC, publisher ASC
                    """
                ).fetchall()
            ]
        return {
            "videos": int(row["videos"] or 0),
            "sentences": int(sentence_row["n"] or 0),
            "duration": float(row["duration"] or 0.0),
            "publishers": publishers,
            "fts_enabled": bool(self._fts_ok),
        }

    def list_videos(self, publisher: str = "", limit: int = 500) -> list[dict[str, Any]]:
        with self._session() as connection:
            if publisher:
                rows = connection.execute(
                    "SELECT * FROM videos WHERE publisher=? "
                    "ORDER BY session_epoch DESC, name DESC LIMIT ?",
                    (publisher, int(limit)),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM videos ORDER BY session_epoch DESC, name DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        return [dict(row) for row in rows]

    def _match_rows_by_path(
        self,
        connection: sqlite3.Connection,
        table: str,
        column: str,
        value: str,
        limit: int = 500,
    ) -> list[str]:
        """按路径宽松匹配取回 ``path`` 列（先比比较键，再比解析后的规范名）。"""
        key = _path_key(value)
        rows = connection.execute(f"SELECT path, {column} AS target FROM {table}").fetchall()
        matched: list[str] = []
        for row in rows:
            target = str(row["target"] or "")
            if _path_key(target) == key or _path_keys_match(target, value):
                matched.append(str(row["path"]))
                if len(matched) >= max(1, int(limit)):
                    break
        return matched

    def resolve_video_paths(self, video: str, limit: int = 500) -> list[str]:
        """把前端传来的视频标识解析成具体路径（支持完整路径或文件名）。

        注意不要对传入路径做 ``resolve()``：Windows 上它可能返回 8.3 短名
        （``ADMINI~1``），和入库时存的规范路径对不上，筛选就会静默失效。
        这里改成"原样优先 + 宽松匹配兜底"，短名/长名、斜杠方向都能对上。
        """
        value = str(video or "").strip()
        if not value:
            return []
        if Path(value).is_absolute() or "/" in value or "\\" in value:
            with self._session() as connection:
                row = connection.execute(
                    "SELECT path FROM videos WHERE path=?", (value,)
                ).fetchone()
                if row:
                    return [str(row["path"])]
                matched = self._match_rows_by_path(connection, "videos", "path", value, limit)
                if matched:
                    return matched
            return [value]
        with self._session() as connection:
            rows = connection.execute(
                "SELECT path FROM videos WHERE name=? ORDER BY session_epoch DESC LIMIT ?",
                (value, int(limit)),
            ).fetchall()
        return [str(row["path"]) for row in rows]

    def record_import(
        self,
        *,
        file_name: str,
        source: str,
        videos: int,
        sentences: int,
        note: str = "",
    ) -> None:
        """记一笔导入历史：界面上要能看出这个库里装了哪几批索引。"""
        with self._session() as connection:
            connection.execute(
                "INSERT INTO import_history(file_name, source, videos, sentences, imported_at, note) "
                "VALUES(?,?,?,?,?,?)",
                (
                    str(file_name or ""), str(source or ""),
                    int(videos or 0), int(sentences or 0), time.time(), str(note or ""),
                ),
            )

    def import_history(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT file_name, source, videos, sentences, imported_at, note "
                "FROM import_history ORDER BY imported_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [
            {
                "file_name": str(row["file_name"] or ""),
                "source": str(row["source"] or ""),
                "videos": int(row["videos"] or 0),
                "sentences": int(row["sentences"] or 0),
                "imported_at": float(row["imported_at"] or 0.0),
                "note": str(row["note"] or ""),
            }
            for row in rows
        ]

    def sources(self) -> dict[str, Any]:
        """库里内容的来源构成：按批次汇总 + 导入历史。

        本机索引只有一个库文件（导入的主播索引都合并进它，这样"全部主播"
        能一起搜），所以界面必须把这些来源列出来，否则用户以为只导进来了一个。
        """
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT origin_label,
                       COUNT(*) AS videos,
                       COALESCE(SUM(sentence_count),0) AS sentences
                FROM videos GROUP BY origin_label
                ORDER BY videos DESC, origin_label ASC
                """
            ).fetchall()
        batches = [
            {
                "label": str(row["origin_label"] or "未标注批次"),
                "videos": int(row["videos"] or 0),
                "sentences": int(row["sentences"] or 0),
            }
            for row in rows
        ]
        return {
            "batches": batches,
            "batch_count": len(batches),
            "imports": self.import_history(limit=20),
        }

    def directory_options(self) -> list[dict[str, Any]]:
        """按目录汇总，供"导出哪个目录的索引"选择用。

        目录多、路径长的时候只显示去掉公共前缀后的相对部分，例如
        ``录屏\\Ayobe小贤``、``jccc\\单品短素材\\8.16``，用户才看得懂。
        """
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT dir,
                       COUNT(*) AS videos,
                       COALESCE(SUM(sentence_count),0) AS sentences,
                       COALESCE(SUM(duration),0) AS duration
                FROM videos
                WHERE dir<>'' AND dir NOT LIKE 'index\\%'
                GROUP BY dir
                ORDER BY videos DESC, dir ASC
                """
            ).fetchall()
        dirs = [str(row["dir"]) for row in rows]
        prefix = _common_parent(dirs) if len(dirs) > 1 else ""
        options = []
        for row in rows:
            full = str(row["dir"])
            label = _relative_to_root(full, prefix) if prefix else Path(full).name
            options.append(
                {
                    "path": full,
                    "label": label or full,
                    "videos": int(row["videos"] or 0),
                    "sentences": int(row["sentences"] or 0),
                    "duration": float(row["duration"] or 0.0),
                }
            )
        return options

    def streamer_options(self) -> list[dict[str, Any]]:
        """按主播汇总，供"导出哪个主播的索引"选择用。

        大机构动辄几十个主播、几百 TB 素材，索引要能按主播分卷分发。
        """
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT publisher,
                       COUNT(*) AS videos,
                       COALESCE(SUM(sentence_count),0) AS sentences,
                       COALESCE(SUM(duration),0) AS duration
                FROM videos GROUP BY publisher
                ORDER BY videos DESC, publisher ASC
                """
            ).fetchall()
        return [
            {
                "publisher": str(row["publisher"] or ""),
                "label": str(row["publisher"] or "(未标注主播)"),
                "videos": int(row["videos"] or 0),
                "sentences": int(row["sentences"] or 0),
                "duration": float(row["duration"] or 0.0),
            }
            for row in rows
        ]

    def top_suggestions(self, limit: int = 20, publisher: str = "") -> list[str]:
        """热门文案。给了 publisher 就只算这个主播的句子。

        ``suggestions`` 表是全局统计，所以按主播取的时候现算一次——
        导入共享索引后用户常按主播筛选，热门文案要跟着换。
        """
        if str(publisher or "").strip():
            with self._session() as connection:
                rows = connection.execute(
                    """
                    SELECT s.raw_text AS text, COUNT(DISTINCT s.video_id) AS videos
                    FROM sentences s JOIN videos v ON v.id = s.video_id
                    WHERE v.publisher = ? AND length(s.norm_text) BETWEEN 4 AND 26
                    GROUP BY s.norm_text
                    ORDER BY videos DESC, length(s.norm_text) ASC
                    LIMIT ?
                    """,
                    (str(publisher), int(limit)),
                ).fetchall()
            return [str(row["text"]) for row in rows]
        with self._session() as connection:
            rows = connection.execute(
                "SELECT text FROM suggestions ORDER BY count DESC, length(norm_text) ASC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [str(row["text"]) for row in rows]

    def suggest(self, prefix: str, limit: int = 20, publisher: str = "") -> list[str]:
        norm = normalize_for_match(prefix)
        if not norm:
            return self.top_suggestions(limit, publisher=publisher)
        with self._session() as connection:
            rows = connection.execute(
                "SELECT text FROM suggestions WHERE norm_text LIKE ? ESCAPE '\\' "
                "ORDER BY count DESC, length(norm_text) ASC LIMIT ?",
                (_escape_like(norm) + "%", int(limit)),
            ).fetchall()
            found = [str(row["text"]) for row in rows]
            if len(found) < limit:
                seen = set(found)
                extra = connection.execute(
                    "SELECT text FROM suggestions WHERE norm_text LIKE ? ESCAPE '\\' "
                    "ORDER BY count DESC, length(norm_text) ASC LIMIT ?",
                    ("%" + _escape_like(norm) + "%", int(limit) * 2),
                ).fetchall()
                for row in extra:
                    text = str(row["text"])
                    if text in seen:
                        continue
                    found.append(text)
                    seen.add(text)
                    if len(found) >= limit:
                        break
        return found

    # ---------------- 检索 ----------------

    def search(
        self,
        query: str,
        *,
        mode: str = "smart",
        context: int = DEFAULT_CONTEXT_SENTENCES,
        publisher: str = "",
        publishers: Sequence[str] | None = None,
        batches: Sequence[str] | None = None,
        directories: Sequence[str] | None = None,
        lib_ids: Sequence[int] | None = None,
        video: str = "",
        limit_sentences: int = DEFAULT_LIMIT_SENTENCES,
        limit_groups: int = DEFAULT_LIMIT_GROUPS,
        windows_per_group: int = DEFAULT_WINDOWS_PER_GROUP,
        group_gap: float = DEFAULT_GROUP_GAP_SECONDS,
    ) -> dict[str, Any]:
        """检索一句话/一个关键词，返回命中的视频与时间段。

        搜索**只查索引**。范围可以在搜索前限定：
        - ``lib_ids``：只在这些索引库里搜（一个素材目录 / 一个导入包 = 一个索引）；
        - ``batches``：只在这些索引批次（``origin_label``）里搜；
        - ``publishers``：只在这些主播里搜（空字符串代表"未标注主播"）；
        - ``directories``：只在这些目录里搜。
        都不给就是全索引搜。
        """
        started = time.time()
        query_raw = str(query or "").strip()
        result: dict[str, Any] = {
            "query": query_raw,
            "mode": str(mode or "smart"),
            "context": max(0, int(context)),
            "hits": 0,
            "hits_shown": 0,
            "sentences": 0,
            "videos": 0,
            "groups": [],
            "truncated": False,
            "elapsed_ms": 0.0,
            "used_mode": "none",
            "terms": [],
        }
        if not query_raw:
            return result

        terms = _prepare_terms(query_raw)
        result["terms"] = terms
        if not terms:
            return result

        publisher = str(publisher or "").strip()
        scope_publishers = [str(item) for item in (publishers or [])]
        scope_batches = [str(item) for item in (batches or [])]
        scope_dirs = [str(item) for item in (directories or []) if str(item).strip()]
        scope_libs = [int(item) for item in (lib_ids or []) if int(item or 0) > 0]
        video_paths = self.resolve_video_paths(video) if video else []

        with self._session() as connection:
            candidate_ids: list[int] = []
            used_mode = ""
            for attempt in _attempt_order(mode):
                candidate_ids = self._candidate_ids(
                    connection, terms, exact=(attempt == "exact"),
                    limit=max(1, int(limit_sentences)),
                )
                if candidate_ids:
                    used_mode = attempt
                    break
            result["used_mode"] = used_mode or "none"
            result["truncated"] = len(candidate_ids) >= max(1, int(limit_sentences))
            if not candidate_ids:
                result["elapsed_ms"] = round((time.time() - started) * 1000, 2)
                return result

            sentence_rows = self._load_sentences(
                connection, candidate_ids, publisher=publisher,
                publishers=scope_publishers, batches=scope_batches,
                directories=scope_dirs, lib_ids=scope_libs,
                video_paths=video_paths,
            )
            result["sentences"] = len(sentence_rows)
            hit_entries = self._build_hits(sentence_rows, terms, mode=mode)
            windows = self._build_windows(
                connection, hit_entries, context=max(0, int(context))
            )
            groups = _group_windows(
                windows,
                videos=hit_entries["videos"],
                gap=float(group_gap),
                windows_per_group=max(1, int(windows_per_group)),
                limit_groups=max(1, int(limit_groups)),
            )
            result["groups"] = groups
            result["hits"] = int(hit_entries.get("total_hits") or len(hit_entries["hits"]))
            shown = sum(len(group.get("windows") or []) for group in groups)
            result["hits_shown"] = shown
            result["videos_total"] = len(hit_entries.get("videos") or {})
            # 面向用户的"命中 N 个视频"用真实视频数（不受展示上限影响）
            result["videos"] = result["videos_total"]
            # 有多少处命中没展开（每个视频只展开前 windows_per_group 段）
            result["hits_hidden"] = max(0, int(result["hits"]) - shown)
            result["videos_hidden"] = max(0, int(result["videos_total"]) - len(groups))
            # 命中或视频过多时如实标记"没全展示"，前端据此提示
            if result["hits_hidden"] or result["videos_hidden"]:
                result["truncated"] = True
        result["elapsed_ms"] = round((time.time() - started) * 1000, 2)
        return result

    def _candidate_ids(
        self,
        connection: sqlite3.Connection,
        terms: Sequence[str],
        *,
        exact: bool,
        limit: int,
    ) -> list[int]:
        if exact and terms:
            return self._match_phrase(connection, terms[0], limit=limit)
        # 多词 AND：每个词各取一批候选求交集。候选限量放宽一些，
        # 否则"的""这个"这类超高频词会把交集误剪掉。
        sets: list[set[int]] = []
        for term in terms:
            ids = self._match_phrase(connection, term, limit=max(limit, 3000))
            if not ids:
                return []
            sets.append(set(ids))
        if not sets:
            return []
        intersection = sets[0] if len(sets) == 1 else set.intersection(*sets)
        return sorted(int(value) for value in intersection)[:limit]

    def _match_phrase(
        self, connection: sqlite3.Connection, phrase: str, *, limit: int
    ) -> list[int]:
        """按短语取候选句子 id。

        三条路，按可靠性排序：
        1. 3 字及以上：FTS5 trigram MATCH（快，且结果就是子串匹配）；
        2. 2 字：3-gram 倒排求候选，再用 LIKE 复核（trigram MATCH 对 2 字
           永远是空集，这是分词器 3 字符起的硬限制）；
        3. 1 字 / FTS 不可用 / 上面都没结果：LIKE 全表兜底。
        """
        length = len(phrase)
        limit = max(1, int(limit))
        if length >= 3 and self._fts_ok:
            ids = self._fts_match(connection, phrase, limit)
            if ids:
                return ids
        elif length == 2 and not phrase[0].isascii() and not phrase[1].isascii():
            ids = self._gram_candidates(connection, phrase, limit)
            if ids:
                return ids
        rows = connection.execute(
            "SELECT id FROM sentences WHERE norm_text LIKE ? ESCAPE '\\' LIMIT ?",
            ("%" + _escape_like(phrase) + "%", limit),
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def _gram_candidates(
        self, connection: sqlite3.Connection, phrase: str, limit: int
    ) -> list[int]:
        """2 字查询：用 3-gram 倒排缩小范围，再 LIKE 复核成精确结果。"""
        grams = _two_char_gram_candidates(phrase)
        candidates: list[int] = []
        for index in range(0, len(grams), 800):
            batch = grams[index:index + 800]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"SELECT DISTINCT sentence_id FROM sentence_grams "
                f"WHERE gram IN ({placeholders}) LIMIT ?",
                batch + [max(limit * 4, 2000)],
            ).fetchall()
            candidates.extend(int(row["sentence_id"]) for row in rows)
            if len(candidates) >= max(limit * 4, 2000):
                break
        if not candidates:
            return []
        candidates = sorted(set(candidates))[: max(limit * 4, 2000)]
        result: list[int] = []
        for index in range(0, len(candidates), 500):
            batch = candidates[index:index + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"SELECT id FROM sentences WHERE id IN ({placeholders}) "
                "AND norm_text LIKE ? ESCAPE '\\' ORDER BY id ASC LIMIT ?",
                batch + ["%" + _escape_like(phrase) + "%", int(limit)],
            ).fetchall()
            result.extend(int(row["id"]) for row in rows)
            if len(result) >= limit:
                break
        return result[:limit]

    @staticmethod
    def _fts_match(
        connection: sqlite3.Connection, phrase: str, limit: int
    ) -> list[int]:
        escaped = phrase.replace('"', '""')
        try:
            rows = connection.execute(
                "SELECT rowid FROM sent_seg_fts WHERE sent_seg_fts MATCH ? "
                "ORDER BY rowid ASC LIMIT ?",
                ('"' + escaped + '"', int(limit)),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [int(row["rowid"]) for row in rows]

    def _load_sentences(
        self,
        connection: sqlite3.Connection,
        sentence_ids: Sequence[int],
        *,
        publisher: str,
        publishers: Sequence[str] | None = None,
        batches: Sequence[str] | None = None,
        directories: Sequence[str] | None = None,
        lib_ids: Sequence[int] | None = None,
        video_paths: Sequence[str],
    ) -> list[sqlite3.Row]:
        rows: list[sqlite3.Row] = []
        # 搜索范围：索引库 / 索引批次 / 主播集合 / 目录集合。空串代表"未标注主播"。
        scope_publishers = [str(item) for item in (publishers or [])]
        scope_batches = [str(item) for item in (batches or [])]
        scope_dirs = [str(item) for item in (directories or [])]
        scope_libs = [int(item) for item in (lib_ids or []) if int(item or 0) > 0]
        for index in range(0, len(sentence_ids), 400):
            batch = list(sentence_ids[index:index + 400])
            placeholders = ",".join("?" for _ in batch)
            sql = (
                "SELECT s.id, s.video_id, s.seq, s.start, s.end, s.raw_text, "
                "       v.name AS video_name, v.path AS video_path, v.publisher, v.dir, "
                "       v.duration, v.session_epoch, v.origin_label "
                "FROM sentences s JOIN videos v ON v.id = s.video_id "
                f"WHERE s.id IN ({placeholders})"
            )
            params: list[Any] = list(batch)
            if publisher:
                sql += " AND v.publisher = ?"
                params.append(publisher)
            if scope_publishers:
                marks = ",".join("?" for _ in scope_publishers)
                sql += f" AND v.publisher IN ({marks})"
                params.extend(scope_publishers)
            if scope_batches:
                marks = ",".join("?" for _ in scope_batches)
                sql += f" AND v.origin_label IN ({marks})"
                params.extend(scope_batches)
            if scope_libs:
                marks = ",".join("?" for _ in scope_libs)
                sql += f" AND v.lib_id IN ({marks})"
                params.extend(scope_libs)
            if scope_dirs:
                marks = ",".join("?" for _ in scope_dirs)
                sql += f" AND v.dir IN ({marks})"
                params.extend(scope_dirs)
            if video_paths:
                marks = ",".join("?" for _ in video_paths)
                sql += f" AND v.path IN ({marks})"
                params.extend(video_paths)
            rows.extend(connection.execute(sql, params).fetchall())
        rows.sort(key=lambda row: (str(row["video_path"]), int(row["seq"])))
        return rows

    def _build_hits(
        self,
        sentence_rows: Sequence[sqlite3.Row],
        terms: Sequence[str],
        *,
        mode: str,
    ) -> dict[str, Any]:
        """在原始文本上定位命中位置，换算成真实时间点。"""
        hits: list[dict[str, Any]] = []
        videos: dict[int, dict[str, Any]] = {}
        for row in sentence_rows:
            raw_text = str(row["raw_text"] or "")
            if not raw_text:
                continue
            mapping = _build_norm_mapping(raw_text)
            norm = mapping["norm"]
            if not norm:
                continue
            spans = _find_spans(norm, terms, mode=mode)
            if not spans:
                continue
            video_id = int(row["video_id"])
            videos.setdefault(
                video_id,
                {
                    "video_id": video_id,
                    "video_path": str(row["video_path"]),
                    "video_name": str(row["video_name"]),
                    "publisher": str(row["publisher"] or ""),
                    "dir": str(row["dir"] or ""),
                    "duration": float(row["duration"] or 0.0),
                    "session_epoch": float(row["session_epoch"] or 0.0),
                    "origin_label": str(row["origin_label"] or ""),
                    # 本机有没有这个视频文件：没有的多半是导入的共享索引，
                    # 结果卡片据此提示"需去网盘下载"。
                    "local_file": _is_local_video_file(str(row["video_path"])),
                },
            )
            sentence_start = float(row["start"])
            sentence_end = float(row["end"])
            for span_start, span_end in spans:
                # 归一化坐标必须转成"原文坐标"再交给前端：前端是拿原始字幕
                # 文本做高亮的，直接用归一化坐标会随标点数量整体偏移。
                raw_start, raw_end = _norm_span_to_raw_span(mapping, span_start, span_end)
                hits.append(
                    {
                        "sentence_id": int(row["id"]),
                        "video_id": video_id,
                        "seq": int(row["seq"]),
                        "sentence_start": sentence_start,
                        "sentence_end": sentence_end,
                        "raw_text": raw_text,
                        "spans": [(raw_start, raw_end)],
                        "start": _span_time(
                            mapping, raw_text, span_start, sentence_start, sentence_end,
                            is_start=True,
                        ),
                        "end": _span_time(
                            mapping, raw_text, span_end, sentence_start, sentence_end,
                            is_start=False,
                        ),
                    }
                )
        hits.sort(key=lambda item: (item["video_id"], item["sentence_start"], item["spans"][0][0]))
        return {
            "hits": hits,
            "videos": videos,
            "total_hits": len(hits),
        }

    def _build_windows(
        self,
        connection: sqlite3.Connection,
        hit_entries: Mapping[str, Any],
        *,
        context: int,
    ) -> list[dict[str, Any]]:
        """取每个命中句的上下文窗口。

        两条关键优化（否则高频词搜索会慢到秒级）：
        1. 同一视频里**重叠的窗口先合并成区间**，每个区间只查一次库，
           而不是每处命中查一次；
        2. 只为最终会返回的命中构造行数据，超出的直接丢弃。
        """
        hits: list[dict[str, Any]] = list(hit_entries.get("hits") or [])
        if not hits:
            return []
        context = max(0, int(context))
        context = min(context, 8)
        hits_by_sentence: dict[int, list[dict[str, Any]]] = {}
        seqs_by_video: dict[int, set[int]] = {}
        for hit in hits:
            hits_by_sentence.setdefault(int(hit["sentence_id"]), []).append(hit)
            seqs_by_video.setdefault(int(hit["video_id"]), set()).add(int(hit["seq"]))

        # 命中句按视频聚拢，供后面的窗口合并使用（不再做任何截断：
        # 一个视频一张卡片，命中全都要保留，否则用户会以为"只有这几处"）。
        selected: list[tuple[int, list[dict[str, Any]]]] = []
        for video_id, seqs in seqs_by_video.items():
            per_video = [
                (sentence_id, sentence_hits)
                for sentence_id, sentence_hits in hits_by_sentence.items()
                if int(sentence_hits[0]["video_id"]) == video_id
            ]
            per_video.sort(key=lambda item: float(item[1][0]["start"]))
            selected.extend(per_video)
        selected.sort(key=lambda item: float(item[1][0]["start"]))

        rows_by_video: dict[int, list[sqlite3.Row]] = {}
        ranges_by_video: dict[int, list[list[int]]] = {}
        # 注意：selected 的元素是 (sentence_id, hits)，视频 id 要从 hit 里取，
        # 别把句子 id 当视频 id 用（那样区间会建到不存在的键上，窗口全空）。
        for video_id in {int(hits_[0]["video_id"]) for _, hits_ in selected}:
            seqs = sorted(
                int(hit["seq"])
                for _, sentence_hits in selected
                if int(sentence_hits[0]["video_id"]) == video_id
                for hit in sentence_hits
            )
            ranges: list[list[int]] = []
            for seq in seqs:
                low = max(0, seq - context)
                high = seq + context
                if ranges and low <= ranges[-1][1] + 1:
                    ranges[-1][1] = max(ranges[-1][1], high)
                else:
                    ranges.append([low, high])
            collected: list[sqlite3.Row] = []
            for low, high in ranges:
                collected.extend(
                    connection.execute(
                        "SELECT id, seq, start, end, raw_text FROM sentences "
                        "WHERE video_id=? AND seq BETWEEN ? AND ? ORDER BY seq ASC",
                        (video_id, low, high),
                    ).fetchall()
                )
            rows_by_video[video_id] = collected
            ranges_by_video[video_id] = ranges

        #: 每个窗口最多带多少行上下文（密集命中时防止单个窗口膨胀到几百行）
        lines_cap = max(8, 4 * context + 4)

        windows: list[dict[str, Any]] = []
        for sentence_id, sentence_hits in selected:
            first = sentence_hits[0]
            video_id = int(first["video_id"])
            seq = int(first["seq"])
            # 窗口只从"合并后的区间"里剪自己那一段，避免把整片密集命中都塞进来
            candidates = [
                row for row in rows_by_video.get(video_id, [])
                if _seq_in_ranges(int(row["seq"]), ranges_by_video.get(video_id, []))
            ]
            lines = []
            for row in _select_window_rows(candidates, seq, hits_by_sentence, lines_cap):
                row_id = int(row["id"])
                line_hits = hits_by_sentence.get(row_id, [])
                line_spans: list[tuple[int, int]] = []
                for item in line_hits:
                    line_spans.extend(item["spans"])
                lines.append(
                    {
                        "sentence_id": row_id,
                        "seq": int(row["seq"]),
                        "start": float(row["start"]),
                        "end": float(row["end"]),
                        "text": str(row["raw_text"]),
                        "is_hit": bool(line_hits),
                        "spans": _merge_spans(line_spans),
                    }
                )
            windows.append(
                {
                    "sentence_id": sentence_id,
                    "video_id": video_id,
                    "seq": int(first["seq"]),
                    "start": float(first["sentence_start"]),
                    "end": float(first["sentence_end"]),
                    "hit_start": float(first["start"]),
                    "hit_end": float(first["end"]),
                    "lines": lines,
                    "hit_spans": _merge_spans(
                        [span for item in sentence_hits for span in item["spans"]]
                    ),
                    "hit_text": str(first["raw_text"]),
                }
            )
        windows.sort(key=lambda item: (int(item["video_id"]), float(item["hit_start"])))
        return windows

    def open_source(self, video_path: str, start: float = 0.0) -> dict[str, Any]:
        """用系统默认播放器打开源视频（前端另附时间点，便于手动跳转）。"""
        path = str(video_path or "").strip()
        if not path or not Path(path).is_file():
            raise FileNotFoundError(f"视频文件不存在：{path}")
        os.startfile(path)  # type: ignore[attr-defined]
        return {"ok": True, "path": path, "start": float(start or 0)}

    def reveal_source(self, video_path: str) -> dict[str, Any]:
        """在文件资源管理器里定位这个视频（打开所在文件夹并选中它）。

        导入的共享索引本机没有文件，这里会给出可执行的提示而不是静默失败。
        """
        import subprocess

        path = str(video_path or "").strip()
        if not path:
            raise FileNotFoundError("这条结果没有记录源视频路径（可能来自导入的索引）。")
        target = Path(path)
        if not target.is_file():
            # 导入索引的占位路径：找出它来自哪个批次，提示用户去网盘找
            raise FileNotFoundError(
                f"本机没有这个视频文件：{target.name}。"
                "它来自导入的索引，请按这个文件名到网盘查找下载。"
            )
        try:
            # /select 会在资源管理器里打开父目录并选中该文件
            subprocess.Popen(["explorer", "/select,", str(target)])
        except Exception:
            os.startfile(str(target.parent))  # type: ignore[attr-defined]
        return {"ok": True, "path": str(target), "folder": str(target.parent)}


# ==========================================================================
# 检索辅助
# ==========================================================================

def _attempt_order(mode: str) -> tuple[str, ...]:
    normalized = str(mode or "smart").strip().lower()
    if normalized == "terms":
        return ("terms", "exact")
    if normalized == "exact":
        return ("exact", "terms")
    return ("exact", "terms")


def _prepare_terms(query: str) -> list[str]:
    """把输入切成检索词：换行/逗号/顿号/竖线/空格都是分隔符，其余当整句。"""
    text = str(query or "").strip()
    if not text:
        return []
    parts = [part for part in re.split(r"[\s,，、|;；/]+", text) if part.strip()]
    terms: list[str] = []
    for part in parts:
        norm = normalize_for_match(part)
        if norm and norm not in terms:
            terms.append(norm)
    return terms


def _part_offsets(raw_text: str) -> list[tuple[int, int]]:
    """每个字幕块在原文里的 (起点, 终点)，按真实空白长度推进。"""
    offsets: list[tuple[int, int]] = []
    cursor = 0
    length = len(raw_text)
    while cursor < length:
        while cursor < length and raw_text[cursor].isspace():
            cursor += 1
        if cursor >= length:
            break
        start = cursor
        while cursor < length and not raw_text[cursor].isspace():
            cursor += 1
        offsets.append((start, cursor))
    return offsets


def _build_norm_mapping(raw_text: str) -> dict[str, Any]:
    """建立"归一化文本 -> 原始文本字符位置"的映射。"""
    norm_chars: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(raw_text):
        piece = normalize_for_match(char)
        if not piece:
            continue
        for sub in piece:
            norm_chars.append(sub)
            origin.append(index)
    return {
        "norm": "".join(norm_chars),
        "origin": origin,
        "raw_len": len(raw_text),
        "parts": split_segment_texts(raw_text),
        "part_offsets": _part_offsets(raw_text),
    }


def _norm_index_to_raw_index(mapping: Mapping[str, Any], norm_index: int) -> int:
    origin: list[int] = mapping["origin"]
    if not origin:
        return 0
    if norm_index < 0:
        return 0
    if norm_index >= len(origin):
        return int(mapping["raw_len"])
    return int(origin[norm_index])


def _norm_span_to_raw_span(
    mapping: Mapping[str, Any], norm_start: int, norm_end: int
) -> tuple[int, int]:
    """归一化区间 -> 原文区间。

    归一化会去掉标点空白，所以归一化下标与原文下标不是一回事；统一在
    "原文坐标"里表达区间，前端才能正确高亮。

    口径：返回的原文区间**归一化后必须包含命中词**。如果命中字的中间夹着
    标点或空格（如字幕特效 ``【很显】瘦``、断词 ``很 显 瘦``），区间会覆盖
    这一整段（多带一个标点），但绝不会漏掉命中的字。
    """
    origin: list[int] = mapping["origin"]
    norm = str(mapping.get("norm") or "")
    if not origin or not norm:
        return (0, 0)
    start = max(0, min(int(norm_start), len(norm)))
    end = max(start, min(int(norm_end), len(norm)))
    if end <= start:
        return (0, 0)
    raw_start = int(origin[start])
    raw_end = int(origin[end - 1]) + 1
    # 中段若有"被跳过的字符"（标点/空格），说明区间有洞，向右补齐到连续
    hole = False
    for index in range(start + 1, end):
        if int(origin[index]) != int(origin[index - 1]) + 1:
            hole = True
            break
    if hole:
        limit = int(mapping["raw_len"])
        while raw_end < limit:
            cursor = index
            while cursor < end and int(origin[cursor]) < raw_end:
                cursor += 1
            if cursor >= end:
                break
            if int(origin[cursor]) != raw_end:
                break
            raw_end += 1
    return (raw_start, max(raw_start + 1, raw_end))


def _raw_span_to_parts(mapping: Mapping[str, Any], raw_start: int, raw_end: int) -> list[int]:
    parts: list[str] = list(mapping.get("parts") or [])
    if not parts:
        return [0]
    offsets = list(mapping.get("part_offsets") or [])
    if len(offsets) != len(parts):
        offsets = []
        cursor = 0
        for part in parts:
            offsets.append((cursor, cursor + len(part)))
            cursor += len(part) + 1
    covered = [
        index
        for index, (part_start, part_end) in enumerate(offsets)
        if part_end > raw_start and part_start < raw_end
    ]
    return covered or [0]


def _parts_times(
    raw_text: str, sentence_start: float, sentence_end: float
) -> list[tuple[float, float]]:
    """没有逐块时间戳时，按字数把句子时长均摊给各字幕块（近似定位）。"""
    parts = split_segment_texts(raw_text)
    if not parts:
        return [(sentence_start, sentence_end)]
    total_chars = sum(len(part) for part in parts) or 1
    span = max(0.0, sentence_end - sentence_start)
    times: list[tuple[float, float]] = []
    cursor = sentence_start
    for part in parts:
        share = span * (len(part) / total_chars)
        times.append((cursor, cursor + share))
        cursor += share
    return times


def _span_time(
    mapping: Mapping[str, Any],
    raw_text: str,
    norm_index: int,
    sentence_start: float,
    sentence_end: float,
    *,
    is_start: bool,
) -> float:
    """命中位置 -> 时间点：能对上字幕块就用块的真实时间，否则按字数均摊。"""
    raw_index = _norm_index_to_raw_index(mapping, norm_index)
    covered = _raw_span_to_parts(mapping, raw_index, raw_index + 1)
    times = _parts_times(raw_text, sentence_start, sentence_end)
    index = covered[0] if covered else 0
    if index >= len(times):
        index = len(times) - 1
    return float(times[index][0] if is_start else times[index][1])


def _find_all(haystack: str, needle: str) -> list[tuple[int, int]]:
    if not needle:
        return []
    spans: list[tuple[int, int]] = []
    start = haystack.find(needle)
    while start >= 0:
        spans.append((start, start + len(needle)))
        start = haystack.find(needle, start + 1)
    return spans


def _find_spans(norm_text: str, terms: Sequence[str], *, mode: str) -> list[tuple[int, int]]:
    """在归一化文本上找出命中区间（归一化坐标）。"""
    if not norm_text or not terms:
        return []
    normalized_mode = str(mode or "smart").strip().lower()
    if normalized_mode in ("exact", "smart") and len(terms) == 1:
        spans = _find_all(norm_text, terms[0])
        if spans:
            return _merge_spans(spans)
        if normalized_mode == "exact":
            return []
    per_term: list[list[tuple[int, int]]] = []
    for term in terms:
        found = _find_all(norm_text, term)
        if not found:
            if normalized_mode == "terms":
                return []
            longest = max(terms, key=len)
            return _merge_spans(_find_all(norm_text, longest)) if longest else []
        per_term.append(found)
    spans: list[tuple[int, int]] = []
    for found in per_term:
        spans.extend(found)
    return _merge_spans(spans)


def _seq_in_ranges(seq: int, ranges: Sequence[Sequence[int]]) -> bool:
    for low, high in ranges:
        if low <= seq <= high:
            return True
    return False


def _select_window_rows(
    rows: Sequence[sqlite3.Row],
    seq: int,
    hits_by_sentence: Mapping[int, Sequence[Any]],
    cap: int,
) -> list[sqlite3.Row]:
    """挑出要展示的窗口行：**命中行必须全部保留**，剩余额度再补命中句附近的上下文。

    不能只按"离本句最近"来截断：密集命中时那样会把窗口内其他命中的行挤掉，
    用户会看到"这处命中的上下文里没有高亮"。
    """
    if cap <= 0 or len(rows) <= cap:
        return list(rows)
    hit_seqs = {
        int(row["seq"]) for row in rows if hits_by_sentence.get(int(row["id"]))
    }
    kept: set[int] = set(hit_seqs)
    remaining = cap - len(kept)
    if remaining > 0:
        ordered = sorted(rows, key=lambda row: abs(int(row["seq"]) - seq))
        for row in ordered:
            if remaining <= 0:
                break
            row_seq = int(row["seq"])
            if row_seq in kept:
                continue
            kept.add(row_seq)
            remaining -= 1
    return [row for row in rows if int(row["seq"]) in kept]


def _group_windows(
    windows: Sequence[Mapping[str, Any]],
    *,
    videos: Mapping[int, Mapping[str, Any]],
    gap: float,
    windows_per_group: int,
    limit_groups: int,
) -> list[dict[str, Any]]:
    """按**视频**聚合成结果卡片：一个视频一张卡。

    这里刻意不按时间间隔切分卡片：之前按"间隔超过 20 秒就另起一组"来切，
    结果同一个视频会被切成好几张卡（用户看到的就是"同一视频重复出现"）。
    现在一个视频只有一张卡，卡内按时间列出命中段，超出 windows_per_group
    的部分用 "该视频还有 N 处命中" 提示。
    """
    by_video: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    for window in windows:
        video_id = int(window["video_id"])
        group = by_video.get(video_id)
        if group is None:
            video = videos.get(video_id) or {}
            group = {
                "video_id": video_id,
                "video_path": str(video.get("video_path") or ""),
                "video_name": str(video.get("video_name") or ""),
                "publisher": str(video.get("publisher") or ""),
                "duration": float(video.get("duration") or 0.0),
                "session_epoch": float(video.get("session_epoch") or 0.0),
                # 共享索引用：来源批次 + 本机是否真的有这个文件
                "origin_label": str(video.get("origin_label") or ""),
                "local_file": bool(video.get("local_file")),
                "hits": 0,
                "windows": [],
                "windows_total": 0,
                "first_hit": float(window["hit_start"]),
                "last_hit": float(window["hit_start"]),
            }
            by_video[video_id] = group
            order.append(video_id)
        group["hits"] = int(group["hits"]) + 1
        group["windows_total"] = int(group["windows_total"]) + 1
        group["last_hit"] = max(float(group["last_hit"]), float(window["hit_end"]))
        group["first_hit"] = min(float(group["first_hit"]), float(window["hit_start"]))
        if len(group["windows"]) < windows_per_group:
            group["windows"].append(dict(window))
    groups = [by_video[video_id] for video_id in order]
    # 命中多的视频排前面，方便先看信息量大的
    groups.sort(key=lambda item: (-int(item["hits"]), float(item["first_hit"])))
    return groups[:limit_groups]


# ==========================================================================
# 入库管线
# ==========================================================================

def collect_video_files(
    root: str,
    *,
    recursive: bool = True,
    extensions: Sequence[str] = VIDEO_EXTENSIONS,
    min_bytes: int = MIN_VIDEO_BYTES,
    max_videos: int = DEFAULT_MAX_VIDEOS,
) -> list[str]:
    """列出目录里可入库的视频（去重、跳过隐藏文件与过小文件）。"""
    base = Path(str(root or ""))
    if not base.is_dir():
        return []
    lowered = tuple(ext.lower() for ext in extensions)
    found: list[str] = []
    seen: set[str] = set()

    def consider(path: Path) -> None:
        try:
            name = path.name
            if name.startswith(".") or name.startswith("~"):
                return
            if path.suffix.lower() not in lowered:
                return
            resolved = str(path.resolve())
            if resolved in seen:
                return
            if path.stat().st_size < max(0, int(min_bytes)):
                return
            seen.add(resolved)
            found.append(resolved)
        except OSError:
            return

    if recursive:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [
                name for name in dirnames
                if not name.startswith(".") and name.lower() != "__pycache__"
            ]
            for filename in filenames:
                consider(Path(dirpath) / filename)
                if len(found) >= max_videos:
                    break
            if len(found) >= max_videos:
                break
    else:
        try:
            for entry in sorted(base.iterdir()):
                if entry.is_file():
                    consider(entry)
                if len(found) >= max_videos:
                    break
        except OSError:
            return []

    found.sort(key=_file_sort_key)
    return found


def _file_sort_key(path: str) -> tuple:
    name = Path(path).name
    epoch = guess_session_epoch(name)
    return (0 if epoch else 1, epoch, name)


def resolve_srt_path(video_path: str) -> str:
    """视频对应的字幕路径：同名 .srt / .SRT。"""
    path = Path(video_path)
    for suffix in (".srt", ".SRT"):
        candidate = path.with_suffix(suffix)
        if candidate.is_file():
            return str(candidate)
    return ""


def srt_is_fresh(video_path: str, srt_path: str) -> tuple[bool, str]:
    """已有字幕能否直接复用。

    用户手写/外部导入的 SRT 没有缓存身份，永远信任；App 生成的 SRT 带
    ``.asr-cache.json``，视频被替换过就判过期，避免拿旧字幕定位新视频。
    """
    if not srt_path or not Path(srt_path).is_file():
        return False, "srt_missing"
    try:
        from asr_cache import inspect_cache  # type: ignore

        status = inspect_cache(video_path, srt_path)
        if status.get("valid"):
            return True, "verified" if status.get("managed") else "user_srt"
        if not status.get("managed"):
            return True, "unverified_user_srt"
        return False, str(status.get("reason") or "invalid")
    except Exception:
        return True, "unverified"


def resolve_publisher(video_path: str, root: str, index: MaterialIndex | None = None) -> str:
    """推断主播名：目录名 -> 同目录历史标注 -> 空。"""
    publisher = guess_publisher(video_path, root)
    if publisher:
        return publisher
    if index is not None:
        try:
            return index.publisher_of_folder(str(Path(video_path).resolve().parent))
        except Exception:
            pass
    return ""


def _detect_asr_provider(srt_path: str, reused: bool) -> str:
    try:
        from asr_cache import metadata_path  # type: ignore

        metadata = metadata_path(srt_path)
        if metadata.is_file():
            payload = json.loads(metadata.read_text(encoding="utf-8-sig"))
            provider = str((payload.get("asr") or {}).get("provider") or "")
            if provider:
                return provider
    except Exception:
        pass
    return "user_srt" if reused else ""


def generate_srt_for_video(
    video_path: str,
    *,
    log: Callable[[str], None] | None = None,
    asr_engine: str = "auto",
    cancel_event: Any = None,
    worker_session: Any = None,
) -> str:
    """给单个视频跑一次现成的本地 ASR 生成字幕（产物留在视频旁边）。"""
    def _log(message: str) -> None:
        if log:
            try:
                log(str(message))
            except Exception:
                pass

    try:
        from stt import generate_srt  # type: ignore
    except Exception as exc:
        _log(f"无法加载语音识别模块：{type(exc).__name__}: {exc}")
        return ""
    try:
        result = generate_srt(
            video_path,
            log_fn=_log,
            asr_engine=asr_engine or "auto",
            cancel_event=cancel_event,
            worker_session=worker_session,
        )
    except Exception as exc:
        _log(f"识别异常：{type(exc).__name__}: {exc}")
        return ""
    return str(result or "")


def try_open_asr_worker_session() -> Any:
    """尝试复用常驻 ASR worker（批量识别时省去反复加载模型）。"""
    try:
        from stt import LocalASRWorkerSession  # type: ignore

        return LocalASRWorkerSession()
    except Exception:
        return None


def close_asr_worker_session(session: Any) -> None:
    if session is None:
        return
    try:
        session.close()
    except Exception:
        pass


class ScanOptions:
    """一次建库扫描的参数。"""

    def __init__(
        self,
        root: str,
        *,
        recursive: bool = True,
        generate_missing_srt: bool = True,
        asr_engine: str = "auto",
        default_publisher: str = "",
        min_bytes: int = MIN_VIDEO_BYTES,
        max_videos: int = DEFAULT_MAX_VIDEOS,
        prune_missing: bool = True,
        max_sentence_chars: int = 60,
    ):
        self.root = str(root or "")
        self.recursive = bool(recursive)
        self.generate_missing_srt = bool(generate_missing_srt)
        self.asr_engine = str(asr_engine or "auto")
        self.default_publisher = str(default_publisher or "")
        self.min_bytes = int(min_bytes)
        self.max_videos = int(max_videos)
        self.prune_missing = bool(prune_missing)
        self.max_sentence_chars = int(max_sentence_chars)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "recursive": self.recursive,
            "generate_missing_srt": self.generate_missing_srt,
            "asr_engine": self.asr_engine,
            "default_publisher": self.default_publisher,
            "min_bytes": self.min_bytes,
            "max_videos": self.max_videos,
            "prune_missing": self.prune_missing,
            "max_sentence_chars": self.max_sentence_chars,
        }


def _cancelled(cancel_event: Any) -> bool:
    if cancel_event is None:
        return False
    try:
        return bool(cancel_event.is_set())
    except Exception:
        return False


def scan_and_index(
    index: MaterialIndex,
    options: ScanOptions,
    *,
    progress: ProgressFn | None = None,
    cancel_event: Any = None,
    only_paths: Sequence[str] | None = None,
    lib_id: int = 0,
) -> dict[str, Any]:
    """扫描目录（或指定文件）并把字幕写进索引。

    ``lib_id`` 指定这些视频归属哪个索引（一个素材目录 = 一个索引）。
    """
    started = time.time()
    if only_paths:
        videos = [str(Path(item).resolve()) for item in only_paths if Path(item).is_file()]
    else:
        videos = collect_video_files(
            options.root,
            recursive=options.recursive,
            min_bytes=options.min_bytes,
            max_videos=options.max_videos,
        )
        if not videos and not options.recursive:
            # 常见误操作：素材根目录下只有"主播子文件夹"、视频都在里面，
            # 而"包含子文件夹"没勾。这里自动兜底再递归扫一次，并说清楚原因，
            # 避免用户只看到"没有找到可入库的视频"却不知道错在哪。
            nested = collect_video_files(
                options.root,
                recursive=True,
                min_bytes=options.min_bytes,
                max_videos=options.max_videos,
            )
            if nested:
                videos = nested
                _emit(
                    progress,
                    f"当前目录下没有直接放着的视频，但子文件夹里有 {len(nested)} 个；"
                    "已自动改为包含子文件夹（也可以勾选「包含子文件夹」）。",
                    "warning",
                )
    report: dict[str, Any] = {
        "root": options.root,
        "videos_found": len(videos),
        "videos_indexed": 0,
        "srt_reused": 0,
        "srt_generated": 0,
        "failed": 0,
        "skipped": 0,
        "sentences": 0,
        "cancelled": False,
        "elapsed": 0.0,
        "failures": [],
    }
    _emit(progress, f"发现 {len(videos)} 个视频，开始入库…", "info")
    if not videos:
        report["elapsed"] = round(time.time() - started, 2)
        _emit(progress, f"该目录下没有找到可入库的视频：{options.root}", "warning")
        _emit(progress, _empty_scan_hint(options.root), "warning")
        return report

    worker_session = try_open_asr_worker_session() if options.generate_missing_srt else None
    try:
        for position, video_path in enumerate(videos, 1):
            if _cancelled(cancel_event):
                report["cancelled"] = True
                _emit(progress, "已中断入库。", "warning")
                break
            outcome = _index_one_video(
                index,
                video_path,
                options=options,
                progress=progress,
                position=position,
                total=len(videos),
                worker_session=worker_session,
                cancel_event=cancel_event,
                lib_id=int(lib_id or 0),
            )
            report["sentences"] += int(outcome.get("sentences") or 0)
            status = outcome.get("status")
            if status in ("indexed", "reused"):
                report["videos_indexed"] += 1
                if status == "reused":
                    report["srt_reused"] += 1
                else:
                    report["srt_generated"] += 1
            elif status == "failed":
                report["failed"] += 1
                report["failures"].append(
                    {"video": video_path, "reason": str(outcome.get("reason") or "")}
                )
            else:
                report["skipped"] += 1
    finally:
        close_asr_worker_session(worker_session)

    if options.prune_missing and not report["cancelled"] and not only_paths:
        removed = index.prune_missing()
        if removed:
            _emit(progress, f"清理了 {removed} 个已不存在的视频记录。", "info")

    report["elapsed"] = round(time.time() - started, 2)
    _emit(
        progress,
        "入库完成："
        f"入库 {report['videos_indexed']} 个（复用字幕 {report['srt_reused']}，"
        f"新识别 {report['srt_generated']}），共 {report['sentences']} 句，"
        f"耗时 {report['elapsed']:.1f}s",
        "warning" if report["failed"] else "success",
    )
    return report


def _index_one_video(
    index: MaterialIndex,
    video_path: str,
    *,
    options: ScanOptions,
    progress: ProgressFn | None,
    position: int,
    total: int,
    worker_session: Any = None,
    cancel_event: Any = None,
    lib_id: int = 0,
) -> dict[str, Any]:
    started = time.time()
    name = Path(video_path).name
    prefix = f"[{position}/{total}]"
    publisher = resolve_publisher(video_path, options.root, index) or options.default_publisher

    srt_path = resolve_srt_path(video_path)
    srt_reused = False
    if srt_path:
        usable, reason = srt_is_fresh(video_path, srt_path)
        if usable:
            srt_reused = True
            _emit(progress, f"{prefix} 复用字幕：{name}（{reason}）", "info")
        else:
            _emit(progress, f"{prefix} 字幕已过期（{reason}），重新识别：{name}", "warning")
            srt_path = ""

    if not srt_path:
        if not options.generate_missing_srt:
            _emit(progress, f"{prefix} 跳过（无字幕且未开启自动识别）：{name}", "warning")
            return {"status": "skipped", "reason": "missing_srt", "sentences": 0}
        _emit(progress, f"{prefix} 开始识别字幕：{name}（首次较慢，之后复用）", "info")
        srt_path = generate_srt_for_video(
            video_path,
            log=lambda message: _emit(progress, f"    {message}", "info"),
            asr_engine=options.asr_engine,
            cancel_event=cancel_event,
            worker_session=worker_session,
        )
        if not srt_path:
            _emit(progress, f"{prefix} 识别失败：{name}", "error")
            return {"status": "failed", "reason": "asr_failed", "sentences": 0}

    cues = read_srt_file(srt_path)
    if not cues:
        _emit(progress, f"{prefix} 字幕为空，无法入库：{name}", "error")
        return {"status": "failed", "reason": "empty_srt", "sentences": 0}

    stat = None
    try:
        stat = Path(video_path).stat()
    except OSError:
        pass
    try:
        info = index.upsert_video(
            video_path=video_path,
            cues=cues,
            publisher=publisher,
            duration=float(cues[-1]["end"]),
            session_epoch=guess_session_epoch(name),
            asr_provider=_detect_asr_provider(srt_path, srt_reused),
            has_word_timing=Path(srt_path).with_suffix(".words.json").is_file(),
            size_bytes=int(stat.st_size) if stat else 0,
            mtime=float(stat.st_mtime) if stat else 0.0,
            max_chars=options.max_sentence_chars,
            lib_id=int(lib_id or 0),
        )
    except Exception as exc:
        _emit(progress, f"{prefix} 写索引失败：{name} —— {type(exc).__name__}: {exc}", "error")
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}", "sentences": 0}

    sentences = int(info.get("sentences") or 0)
    duration = float(cues[-1]["end"])
    _emit(
        progress,
        f"{prefix} 已入库：{name}（{sentences} 句 / {duration:.0f}s）"
        + (f"，主播={publisher}" if publisher else "，⚠ 未识别到主播名，可在结果里手动标注"),
        "success",
    )
    return {
        "status": "reused" if srt_reused else "indexed",
        "sentences": sentences,
        "publisher": publisher,
        "srt_path": srt_path,
        "elapsed": round(time.time() - started, 2),
    }


def _empty_scan_hint(root: str) -> str:
    """扫不到视频时给出可执行的下一步，而不是只说"没有找到"。"""
    base = Path(str(root or ""))
    if not base.is_dir():
        return f"目录不存在或当前读不到（可能是外置盘/网络盘未就绪）：{root}"
    try:
        entries = list(base.iterdir())
    except OSError as exc:
        return f"目录打不开：{type(exc).__name__}（检查权限或盘符是否在线）"
    subdirs = [item for item in entries if item.is_dir()]
    if subdirs:
        return (
            f"该目录下有 {len(subdirs)} 个子文件夹但没有直接放视频，"
            "视频应该在子文件夹里；请勾选「包含子文件夹」后重试。"
        )
    return (
        f"该目录是空的（{len(entries)} 个条目）。请选择真正存放录屏的目录，"
        "例如 <录屏根目录>\\<主播名>\\xxx.mp4 的上层目录。"
    )


def _emit(progress: ProgressFn | None, message: str, level: str = "info") -> None:
    if progress is None:
        return
    try:
        progress(str(message), str(level))
    except TypeError:
        try:
            progress(str(message))  # type: ignore[call-arg]
        except Exception:
            pass
    except Exception:
        pass


def probe_directory(
    root: str, *, recursive: bool = True, index: MaterialIndex | None = None
) -> dict[str, Any]:
    """只统计不建库，用于前端"先看看有多少视频/多少有字幕"。

    给了 ``index`` 就顺带算出"这个目录里有多少是**已经索引过**的、多少是新的"，
    用户才能判断点「建立/更新索引」要等多久——索引是累积的，重复扫描只是跳过。
    """
    videos = collect_video_files(root, recursive=recursive)
    indexed_paths: set[str] = set()
    if index is not None:
        try:
            with index._session() as connection:
                indexed_paths = {
                    _path_key(str(row["path"]))
                    for row in connection.execute(
                        "SELECT path FROM videos WHERE path<>'' AND path NOT LIKE 'index\\%'"
                    ).fetchall()
                }
        except Exception:
            indexed_paths = set()
    with_srt = 0
    pending = 0
    already_indexed = 0
    new_videos = 0
    publishers: dict[str, int] = {}
    total_bytes = 0
    samples: list[dict[str, Any]] = []
    for video_path in videos:
        srt_path = resolve_srt_path(video_path)
        if srt_path:
            with_srt += 1
        else:
            pending += 1
        is_indexed = _path_key(video_path) in indexed_paths
        if is_indexed:
            already_indexed += 1
        else:
            new_videos += 1
        publisher = guess_publisher(video_path, root)
        publishers[publisher] = publishers.get(publisher, 0) + 1
        try:
            total_bytes += int(Path(video_path).stat().st_size)
        except OSError:
            pass
        if len(samples) < 200:
            samples.append(
                {
                    "path": video_path,
                    "name": Path(video_path).name,
                    "publisher": publisher,
                    "has_srt": bool(srt_path),
                    "session_epoch": guess_session_epoch(Path(video_path).name),
                    "size_bytes": _safe_size(video_path),
                }
            )
    return {
        "ok": True,
        "root": str(root),
        "exists": Path(str(root or "")).is_dir(),
        "videos": len(videos),
        "with_srt": with_srt,
        "pending_asr": pending,
        "already_indexed": already_indexed,
        "new_videos": new_videos,
        "total_bytes": total_bytes,
        "publishers": [
            {"publisher": key, "videos": value}
            for key, value in sorted(publishers.items(), key=lambda item: (-item[1], item[0]))
        ],
        "samples": samples,
    }


def _safe_size(video_path: str) -> int:
    try:
        return int(Path(video_path).stat().st_size)
    except OSError:
        return 0


def probe_media_duration(path: str) -> float:
    """尽力读视频时长（失败返回 0，不影响功能）。

    入库本来就用字幕末尾时间当时长，这个函数只作为外部调用时的补充手段。
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return float((result.stdout or "").strip() or 0.0)
    except Exception:
        return 0.0
