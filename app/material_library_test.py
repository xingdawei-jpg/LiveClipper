# -*- coding: utf-8 -*-
"""素材库核心逻辑自测（web 版 app/material_library.py）。

直接运行：
    python material_library_test.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from material_library import (  # noqa: E402
    MaterialIndex,
    _build_norm_mapping,
    _find_spans,
    _norm_span_to_raw_span,
    format_timestamp,
    guess_publisher,
    guess_session_epoch,
    normalize_for_match,
    parse_srt_text,
    read_srt_file,
)

PASSED = 0
FAILED = 0
FAILURES = []


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print("  PASS  " + name)
    else:
        FAILED += 1
        FAILURES.append(name + (f" :: {detail}" if detail else ""))
        print("  FAIL  " + name + (f"  -> {detail}" if detail else ""))


def srt_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    return "%02d:%02d:%02d,%03d" % (hours, minutes, secs, millis)


def make_srt(cues):
    blocks = []
    for index, (start, end, text) in enumerate(cues, 1):
        blocks.append(f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n")
    return "\n".join(blocks)


WORK = Path(tempfile.mkdtemp(prefix="material_lib_test_"))
ROOT = WORK / "录屏"
DIR_A = ROOT / "小雅服饰"
DIR_B = ROOT / "阿文穿搭"
DIR_A.mkdir(parents=True, exist_ok=True)
DIR_B.mkdir(parents=True, exist_ok=True)

VIDEO_A = DIR_A / "xiaoya_20260923_200000.flv"
VIDEO_B = DIR_B / "awen_20260924_193000.mp4"
SRT_A = VIDEO_A.with_suffix(".srt")
SRT_B = VIDEO_B.with_suffix(".srt")

CUES_A = [
    (0.0, 3.0, "姐妹们晚上好，欢迎来到直播间。"),
    (3.2, 7.0, "今天这件真丝衬衫，我给你们争取到全网最低价。"),
    (7.2, 11.0, "面料是十九姆米的桑蚕丝，摸上去非常非常柔软。"),
    (11.2, 16.0, "这个料子真的绝了，一点都不扎皮肤，敏感肌也能穿。"),
    (16.2, 21.0, "我身高一米六五，体重一百二十斤，穿的是M码。"),
    (21.2, 26.0, "微胖的姐妹放心拍，这个版型特别显瘦，腰线收得很好。"),
    (26.2, 31.0, "而且它这个颜色是今年最流行的奶油白。"),
    (31.2, 36.0, "袖子做了微微的泡泡袖，能遮住拜拜肉。"),
    (36.2, 41.0, "尺码从S到三个叉都有，一百六十斤以内都能穿。"),
    (41.2, 46.0, "现在下单还送一条同色系的丝巾，只有前一百名有。"),
    (46.2, 51.0, "真的绝了，这个价格你去专柜是绝对买不到的。"),
]
CUES_B = [
    (0.0, 4.0, "哈喽大家好，我是阿文。"),
    (4.2, 9.0, "微胖的姐妹放心拍，这个版型特别显瘦，腰线收得很好。"),
    (9.2, 14.0, "我身上这件是空气层面料，垂坠感特别好。"),
    (14.2, 19.0, "一百二十斤穿M码刚刚好，完全不会勒。"),
    (19.2, 24.0, "这条裤子也是微胖姐妹的救星，显瘦又显高。"),
]

SRT_A.write_text(make_srt(CUES_A), encoding="utf-8")
SRT_B.write_text(make_srt(CUES_B), encoding="utf-8")
VIDEO_A.write_bytes(b"fake-video-a" * 10)
VIDEO_B.write_bytes(b"fake-video-b" * 10)
DB = WORK / "material.sqlite3"


def main():
    print("=" * 74)
    print("0. 纯函数")
    print("=" * 74)
    check("去标点", normalize_for_match("真的绝了！！！") == "真的绝了", normalize_for_match("真的绝了！！！"))
    check("全角转半角", normalize_for_match("Ｍ码") == "m码", normalize_for_match("Ｍ码"))
    check("繁体转简体", normalize_for_match("這個真的很絕") == "这个真的很绝", normalize_for_match("這個真的很絕"))
    check("去空白", normalize_for_match(" 真 的 绝 了 ") == "真的绝了", normalize_for_match(" 真 的 绝 了 "))
    check(
        "可加性（逐字符归一化 == 整串归一化）",
        normalize_for_match("微胖，姐妹！") == normalize_for_match("微胖") + normalize_for_match("，姐妹！"),
        normalize_for_match("微胖，姐妹！"),
    )
    check("时间戳", format_timestamp(1955) == "00:32:35", format_timestamp(1955))
    check("主播名（子目录）", guess_publisher(str(VIDEO_A), str(ROOT)) == "小雅服饰", guess_publisher(str(VIDEO_A), str(ROOT)))
    check("通用目录当根目录时仍取子目录", guess_publisher(str(VIDEO_A), str(WORK)) == "小雅服饰", guess_publisher(str(VIDEO_A), str(WORK)))
    check("房间号目录不算主播", guess_publisher("C:/V/抖音直播录制/688250693939/x.flv", "C:/V") == "", guess_publisher("C:/V/抖音直播录制/688250693939/x.flv", "C:/V"))
    epoch = guess_session_epoch("688250693939-20260923-220806-5e30c5.flv")
    check("从文件名猜开播时间", abs(epoch - time.mktime((2026, 9, 23, 22, 8, 6, 0, 0, -1))) < 2, epoch)
    check("SRT 解析条数", len(parse_srt_text(SRT_A.read_text(encoding="utf-8"))) == 11)
    check("SRT 文件读取", len(read_srt_file(str(SRT_A))) == 11)

    print()
    print("=" * 74)
    print("1. 建库")
    print("=" * 74)
    index = MaterialIndex(str(DB))
    for video, srt in ((VIDEO_A, SRT_A), (VIDEO_B, SRT_B)):
        info = index.upsert_video(
            video_path=str(video),
            cues=read_srt_file(str(srt)),
            publisher=guess_publisher(str(video), str(ROOT)),
            session_epoch=guess_session_epoch(video.name),
            asr_provider="sensevoice",
        )
        print("  入库:", info)
    stats = index.stats()
    print("  stats:", json.dumps({k: v for k, v in stats.items() if k != "publishers"}, ensure_ascii=False))
    check("FTS5 可用", stats["fts_enabled"] is True, stats["fts_enabled"])
    check("2 个视频", stats["videos"] == 2, stats["videos"])
    check("句子数 >= 16", stats["sentences"] >= 16, stats["sentences"])
    check(
        "主播分组",
        sorted(item["publisher"] for item in stats["publishers"]) == ["小雅服饰", "阿文穿搭"],
        stats["publishers"],
    )

    print()
    print("=" * 74)
    print("2. 关键词检索（两个视频都说过）")
    print("=" * 74)
    result = index.search("显瘦", context=1)
    check("命中 2 个视频", result["videos"] == 2, result["videos"])
    check("命中 3 处（A 两处 + B 一处）", result["hits"] == 3, result["hits"])
    check("每组都带主播名", sorted(g["publisher"] for g in result["groups"]) == ["小雅服饰", "阿文穿搭"], [g["publisher"] for g in result["groups"]])
    check("上下文行数 >= 3", any(len(w["lines"]) >= 3 for g in result["groups"] for w in g["windows"]))
    check("没有空窗口（回归：窗口必须含命中句本身）",
          all(w["lines"] for g in result["groups"] for w in g["windows"]),
          [w["seq"] for g in result["groups"] for w in g["windows"] if not w["lines"]])
    for group in result["groups"]:
        for window in group["windows"]:
            print(f"  [{group['publisher']}] {format_timestamp(window['hit_start'])}  {window['hit_text']}")
            for line in window["lines"]:
                print(("    > " if line["is_hit"] else "      ") + format_timestamp(line["start"]) + " " + line["text"])

    print()
    print("=" * 74)
    print("3. 整句检索（精准定位）")
    print("=" * 74)
    sentence = "微胖的姐妹放心拍，这个版型特别显瘦，腰线收得很好"
    result = index.search(sentence, mode="exact", context=1)
    check("整句命中 2 个视频", result["videos"] == 2, result["videos"])
    group_a = [g for g in result["groups"] if g["publisher"] == "小雅服饰"][0]
    stamp = format_timestamp(group_a["windows"][0]["hit_start"])
    check("小雅服饰在 00:00:21", stamp == "00:00:21", stamp)
    check("命中句文本正确", "显瘦" in group_a["windows"][0]["hit_text"], group_a["windows"][0]["hit_text"])
    check("命中位置高亮区间存在", bool(group_a["windows"][0]["hit_spans"]), group_a["windows"][0]["hit_spans"])

    print()
    print("=" * 74)
    print("4. 句内不同位置 -> 不同时间点")
    print("=" * 74)
    result = index.search("真的绝了", mode="exact", context=0)
    stamps = sorted(
        format_timestamp(w["hit_start"]) for g in result["groups"] for w in g["windows"]
    )
    print("  命中时间:", stamps)
    check("同一视频两次命中都找到", len(stamps) == 2, stamps)
    check("两次命中时间不同", len(set(stamps)) >= 2, stamps)
    check("第二次在 00:00:46 附近", any(s.startswith("00:00:46") for s in stamps), stamps)

    print()
    print("=" * 74)
    print("4b. 高亮区间必须是「原文坐标」（回归：曾用归一化坐标，标点前整体偏 1）")
    print("=" * 74)
    WORDS_WITH_PUNCT = [
        "是不是？很显瘦，你看这样穿起来超显瘦，",
        "好，很显瘦。就是以干净利落精致为主，",
        "穿到身上又有黑色的显瘦程度的同时呢，",
        "很显瘦，它不会让你显胖，",
        "【很显】瘦",
    ]
    for raw in WORDS_WITH_PUNCT:
        mapping = _build_norm_mapping(raw)
        for start, end in _find_spans(mapping["norm"], ["显瘦"], mode="exact"):
            raw_start, raw_end = _norm_span_to_raw_span(mapping, start, end)
            highlighted = raw[raw_start:raw_end]
            check(f"高亮正确: {raw[:18]}…",
                  "显瘦" in normalize_for_match(highlighted),
                  f"raw[{raw_start}:{raw_end}]={highlighted!r}")

    # 端到端：检索结果里每一段高亮文本都要包含查询词
    touched = []
    for group in index.search("显瘦", mode="exact", context=1)["groups"]:
        for window in group["windows"]:
            for line in window["lines"]:
                for start, end in line["spans"]:
                    touched.append(str(line["text"])[start:end])
    check("检索结果里高亮文本都包含查询词",
          bool(touched) and all("显瘦" in normalize_for_match(t) for t in touched),
          touched)

    print()
    print("=" * 74)
    print("4c. 一个视频只能出现一张卡片（回归：曾按时间切成多张，看起来像重复）")
    print("=" * 74)
    sparse_cues = [
        {"start": 0.0, "end": 3.0, "text": "第一个显瘦的位置，隔得很远。"},
        {"start": 600.0, "end": 603.0, "text": "第二个显瘦的位置，中间隔了十分钟。"},
        {"start": 1800.0, "end": 1803.0, "text": "第三个显瘦的位置，隔了半小时。"},
    ]
    sparse_video = WORK / "sparse.mp4"
    index.upsert_video(
        video_path=str(sparse_video), cues=sparse_cues,
        publisher="稀疏主播", duration=1803.0,
    )
    sparse = index.search("显瘦", mode="exact", context=1)
    paths = [group["video_path"] for group in sparse["groups"]]
    check("同一视频只出现一次（不再按时间切卡片）",
          paths.count(str(sparse_video.resolve())) == 1, paths)
    check("卡片数 == 视频数", len(sparse["groups"]) == sparse["videos"],
          (len(sparse["groups"]), sparse["videos"]))
    sparse_group = [g for g in sparse["groups"] if g["video_path"] == str(sparse_video.resolve())][0]
    check("相隔 10/30 分钟的三处命中合并进同一张卡", int(sparse_group["hits"]) == 3,
          sparse_group["hits"])
    check("卡片如实记录总命中数", int(sparse_group["windows_total"]) == 3,
          sparse_group["windows_total"])
    check("展示段数受 windows_per_group 限制", len(sparse_group["windows"]) <= 3,
          len(sparse_group["windows"]))
    index.remove_video(str(sparse_video))

    print()
    print("=" * 74)
    print("5. 分词 AND 检索")
    print("=" * 74)
    result = index.search("桑蚕丝 柔软", mode="terms", context=1)
    check("分词命中", result["hits"] >= 1, result["hits"])
    check("只命中一个视频", result["videos"] == 1, result["videos"])

    print()
    print("=" * 74)
    print("6. 容错：繁体 / 标点 / 全角")
    print("=" * 74)
    check("繁体+标点命中 3 处（显瘦）", index.search("顯瘦？？", mode="exact")["hits"] == 3, index.search("顯瘦？？", mode="exact")["hits"])
    check("带标点整句命中 1 处", index.search("全网最低价", mode="exact")["hits"] == 1)
    check("单字可检索", index.search("码", mode="exact")["hits"] >= 2, index.search("码", mode="exact")["hits"])

    print()
    print("=" * 74)
    print("7. 过滤 / 聚合 / 联想")
    print("=" * 74)
    check("按主播过滤", index.search("显瘦", publisher="阿文穿搭")["videos"] == 1)
    check("按视频名过滤", index.search("显瘦", video=VIDEO_A.name)["videos"] == 1)
    check("按视频绝对路径过滤", index.search("显瘦", video=str(VIDEO_A))["videos"] == 1, index.search("显瘦", video=str(VIDEO_A))["videos"])
    suggest = index.suggest("微胖", limit=5)
    check("联想有结果", len(suggest) >= 1, suggest)
    print("  联想:", suggest[:3])

    print()
    print("=" * 74)
    print("8. 增量更新 / 删除 / 清空")
    print("=" * 74)
    index.remove_video(str(VIDEO_B))
    check("删除后剩 1 个视频", index.search("显瘦")["videos"] == 1, index.search("显瘦")["videos"])
    check("删除后该视频的命中确实消失（A 只剩 1 处）", index.search("显瘦")["hits"] == 1, index.search("显瘦")["hits"])
    check("删除后特有句也搜不到", index.search("空气层面料", mode="exact")["hits"] == 0, index.search("空气层面料", mode="exact")["hits"])
    index.upsert_video(video_path=str(VIDEO_B), cues=read_srt_file(str(SRT_B)), publisher="阿文穿搭")
    check("重复入库不产生重复视频", index.stats()["videos"] == 2, index.stats()["videos"])
    check("重新入库后可检索", index.search("显瘦")["videos"] == 2)
    check("标注主播生效", index.set_publisher_for_folder(str(DIR_B), "阿文(新)") == 1, index.set_publisher_for_folder(str(DIR_B), "阿文(新)"))
    check("标注后可按新主播筛到", index.search("显瘦", publisher="阿文(新)")["videos"] == 1, index.search("显瘦", publisher="阿文(新)")["videos"])
    stats = index.stats()
    check("标注后统计更新", any(item["publisher"] == "阿文(新)" for item in stats["publishers"]), stats["publishers"])
    index.clear()
    check("清空后为 0", index.stats()["videos"] == 0, index.stats()["videos"])

    print()
    print("=" * 74)
    print("汇总: %d 通过 / %d 失败" % (PASSED, FAILED))
    for item in FAILURES:
        print("  失败:", item)
    print("=" * 74)
    return 1 if FAILED else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    raise SystemExit(code)
