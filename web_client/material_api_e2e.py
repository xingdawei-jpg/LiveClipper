# -*- coding: utf-8 -*-
"""素材库 web 接口端到端测试（用真实 App 解释器 + FastAPI TestClient）。

覆盖：状态 / 选择目录 / 试扫 / 建索引（复用已有 SRT）/ 检索 / 联想 / 视频列表 /
      标注主播 / 打开视频（只校验不真开播放器）/ 清空。

运行：
    "C:\\Program Files\\Python312\\python.exe" material_api_e2e.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(r"D:\LC\LiveClipper")
WORK = Path(tempfile.mkdtemp(prefix="material_api_e2e_"))
USER_DATA = WORK / "userdata"
ROOT = WORK / "录屏"
DIR_A = ROOT / "小雅服饰"
DIR_B = ROOT / "阿文穿搭"

# 必须在 import server 之前设置：config.py 读这个环境变量决定用户数据目录
os.environ["LIVECLIPPER_USER_DATA_DIR"] = str(USER_DATA)
os.environ.setdefault("LIVECLIPPER_RUNTIME_LAYOUT", "3")
USER_DATA.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(REPO / "web_client"))
sys.path.insert(0, str(REPO / "app"))

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
        FAILURES.append(f"{name} :: {detail}")
        print(f"  FAIL  {name}  -> {detail}")


def srt_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    return "%02d:%02d:%02d,%03d" % (hours, minutes, secs, millis)


def make_srt(cues):
    return "\n".join(
        f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n"
        for index, (start, end, text) in enumerate(cues, 1)
    )


DIR_A.mkdir(parents=True, exist_ok=True)
DIR_B.mkdir(parents=True, exist_ok=True)

VIDEO_A = DIR_A / "xiaoya_20260923_200000.flv"
VIDEO_B = DIR_B / "awen_20260924_193000.mp4"

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

# 视频文件大到超过 MIN_VIDEO_BYTES（512KB），否则会被当成占位文件跳过
PAYLOAD = b"\x00" * (600 * 1024)
VIDEO_A.write_bytes(PAYLOAD)
VIDEO_B.write_bytes(PAYLOAD)
VIDEO_A.with_suffix(".srt").write_text(make_srt(CUES_A), encoding="utf-8")
VIDEO_B.with_suffix(".srt").write_text(make_srt(CUES_B), encoding="utf-8")

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402

# 素材库的"建索引"是授权功能：这里打桩模拟"已激活"，好让其余用例继续跑完整流程。
# 门禁本身（未激活必须被拒）在用例 0 里用真实逻辑验证。
_LICENSE = {"granted": True}


def _fake_feature_access(feature_name):
    if _LICENSE["granted"]:
        return
    raise RuntimeError("未激活。加客服微信 LeyiDeco 可领 3 天免费试用码，或在官网购买后自动获取激活码。")


server._ensure_feature_access = _fake_feature_access

client = TestClient(server.app)


def wait_task(task_id, timeout=90.0):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        data = client.get("/api/tasks").json()
        for task in data.get("tasks", []):
            if task.get("id") == task_id:
                last = task
                if task.get("status") in ("completed", "failed", "cancelled"):
                    return task
        time.sleep(0.3)
    return last


def main():
    print("临时素材目录:", ROOT)

    print()
    print("=" * 74)
    print("0. 授权门禁：建索引需要激活，检索不需要")
    print("=" * 74)
    import license_guard
    import license_client

    real_access = license_guard.get_feature_access(refresh=False)
    print("  本机真实授权状态:", {k: v for k, v in real_access.items() if k != "raw"})
    print("  原始:", real_access.get("raw"))
    check("授权判定能给出明确结论（已激活或有激活码）",
          "ok" in real_access and ("activated" in real_access),
          real_access)
    check("试用次数机制已停用（不再有免费次数）",
          license_client.TRIAL_ENABLED is False, license_client.TRIAL_ENABLED)

    # 真实状态已激活时，另外验证"未激活必须被拒"这条规则
    if real_access.get("ok"):
        _LICENSE["granted"] = False
        denied = client.post("/api/material/index/start", json={
            "path": str(ROOT), "recursive": True, "generate_srt": False, "publish": False,
        })
        print("  模拟未激活 ->", denied.status_code, str(denied.json())[:120])
        check("未激活时建索引被拒（403）", denied.status_code == 403, denied.status_code)
        check("拒绝原因可读且指向激活",
              "激活" in str(denied.json().get("detail") or ""), denied.json())
        check("被拒时不会留下僵尸任务",
              not any(item.get("scope") == "material" and item.get("status") in ("queued", "running")
                      for item in client.get("/api/tasks").json().get("tasks", [])),
              client.get("/api/tasks").json().get("tasks"))
        # 检索、试扫、状态一律不受授权影响
        check("未激活也能看索引状态", client.get("/api/material/status").status_code == 200)
        check("未激活也能试扫目录",
              client.post("/api/material/probe", json={"path": str(ROOT), "recursive": True}).status_code == 200)
        check("未激活也能检索（空库时给提示而非 403）",
              client.post("/api/material/search", json={"query": "显瘦"}).status_code == 200)
        _LICENSE["granted"] = True

    allowed = client.post("/api/material/index/start", json={
        "path": str(ROOT), "recursive": True, "generate_srt": False, "publish": False,
    })
    check("已激活时建索引放行", allowed.status_code == 200, allowed.text[:200])
    wait_task(allowed.json()["task_id"])
    client.post("/api/material/clear")  # 复原，后面的用例要从未初始化的空库开始

    print()
    print("=" * 74)
    print("1. 状态接口")
    print("=" * 74)
    resp = client.get("/api/material/status")
    check("GET /api/material/status 返回 200", resp.status_code == 200, resp.status_code)
    status = resp.json()
    print("  ", {k: v for k, v in status.items() if k not in ("publishers",)})
    check("默认素材目录是 E:\\切片\\录屏 或已存在目录",
          str(status.get("default_root", "")).endswith("录屏"), status.get("default_root"))
    check("初始为空库", status.get("videos") == 0 and status.get("sentences") == 0, status)
    check("索引落在用户数据目录下",
          os.path.normcase(str(USER_DATA)) in os.path.normcase(str(status.get("index_path", "")))
          or os.path.normcase(str(Path(USER_DATA).resolve())) in os.path.normcase(str(status.get("index_path", ""))),
          status.get("index_path"))

    print()
    print("=" * 74)
    print("2. 试扫目录")
    print("=" * 74)
    resp = client.post("/api/material/probe", json={"path": str(ROOT), "recursive": True, "publish": True})
    check("POST /api/material/probe 返回 200", resp.status_code == 200, resp.status_code)
    probe = resp.json()
    print("  ", {k: v for k, v in probe.items() if k not in ("samples", "publishers")})
    print("   主播:", probe.get("publishers"))
    check("试扫到 2 个视频", probe.get("videos") == 2, probe.get("videos"))
    check("2 个都有字幕", probe.get("with_srt") == 2, probe.get("with_srt"))
    check("待识别 0 个", probe.get("pending_asr") == 0, probe.get("pending_asr"))
    check("识别出两个主播", len(probe.get("publishers") or []) == 2, probe.get("publishers"))

    print()
    print("=" * 74)
    print("3. 建索引（已有 SRT，不需要跑 ASR）")
    print("=" * 74)
    resp = client.post("/api/material/index/start", json={
        "path": str(ROOT), "recursive": True, "generate_srt": False, "publish": True,
    })
    check("POST /api/material/index/start 返回 200", resp.status_code == 200, resp.text[:200])
    started = resp.json()
    task = wait_task(started["task_id"])
    print("  任务状态:", task.get("status"), "|", task.get("message"))
    check("建索引任务完成", task.get("status") == "completed", task)
    check("任务入库数 = 2", int(task.get("result_count") or 0) == 2, task.get("result_count"))

    resp = client.get("/api/material/status")
    status = resp.json()
    print("  ", {k: v for k, v in status.items() if k != "publishers"})
    check("索引里有 2 个视频", status.get("videos") == 2, status.get("videos"))
    check("句子数 >= 16", status.get("sentences", 0) >= 16, status.get("sentences"))
    check("FTS 已启用", status.get("fts_enabled") is True, status.get("fts_enabled"))
    check("主播分组正确",
          sorted(item["publisher"] for item in status.get("publishers") or []) == ["小雅服饰", "阿文穿搭"],
          status.get("publishers"))

    print()
    print("=" * 74)
    print("4. 检索：关键词 / 整句 / 分词")
    print("=" * 74)
    resp = client.post("/api/material/search", json={"query": "显瘦", "context": 1})
    check("搜索接口 200", resp.status_code == 200, resp.text[:200])
    result = resp.json()
    print(f"  关键词『显瘦』: 命中 {result['hits']} 处 / {result['videos']} 个视频 /"
          f" {len(result['groups'])} 组，{result['elapsed_ms']}ms，模式 {result['used_mode']}")
    check("关键词命中 2 个视频", result["videos"] == 2, result["videos"])
    for group in result["groups"]:
        for window in group["windows"]:
            print(f"    [{group['publisher']}] {window['hit_start']:.1f}s  {window['hit_text'][:30]}")
    check("每组都带主播名", all(g.get("publisher") for g in result["groups"]), result["groups"])
    check("上下文有多行", any(len(w["lines"]) >= 3 for g in result["groups"] for w in g["windows"]))

    exact = client.post("/api/material/search", json={
        "query": "微胖的姐妹放心拍，这个版型特别显瘦，腰线收得很好",
        "mode": "exact", "context": 1,
    }).json()
    print(f"  整句检索: 命中 {exact['hits']} 处 / {exact['videos']} 个视频，模式 {exact['used_mode']}")
    check("整句命中 2 个视频", exact["videos"] == 2, exact["videos"])
    check("整句命中带高亮区间",
          all(w["hit_spans"] for g in exact["groups"] for w in g["windows"]),
          [w["hit_spans"] for g in exact["groups"] for w in g["windows"]])

    terms = client.post("/api/material/search", json={"query": "桑蚕丝 柔软", "mode": "terms"}).json()
    print(f"  分词检索『桑蚕丝 柔软』: 命中 {terms['hits']} 处 / {terms['videos']} 个视频")
    check("分词检索只命中 1 个视频", terms["videos"] == 1, terms["videos"])

    print()
    print("=" * 74)
    print("5. 句内两次命中 -> 两个不同时间点")
    print("=" * 74)
    twice = client.post("/api/material/search", json={"query": "真的绝了", "mode": "exact", "context": 0}).json()
    stamps = sorted(f"{w['hit_start']:.1f}" for g in twice["groups"] for w in g["windows"])
    print("  命中时间(秒):", stamps)
    check("找到 2 处", len(stamps) == 2, stamps)
    check("两处时间不同", len(set(stamps)) == 2, stamps)

    print()
    print("=" * 74)
    print("6. 筛选：主播 / 视频 / 视频路径")
    print("=" * 74)
    by_pub = client.post("/api/material/search", json={"query": "显瘦", "publisher": "阿文穿搭"}).json()
    check("按主播筛选", by_pub["videos"] == 1, by_pub["videos"])
    by_name = client.post("/api/material/search", json={"query": "显瘦", "video": VIDEO_A.name}).json()
    check("按视频名筛选", by_name["videos"] == 1 and by_name["hits"] == 1, (by_name["videos"], by_name["hits"]))
    by_path = client.post("/api/material/search", json={"query": "显瘦", "video": str(VIDEO_A)}).json()
    check("按视频路径筛选", by_path["videos"] == 1, (by_path["videos"], by_path.get("message")))

    videos = client.post("/api/material/videos", json={"publisher": ""}).json()
    check("视频列表返回 2 条", len(videos.get("videos") or []) == 2, videos.get("videos"))
    print("   视频列表:", [v["name"] for v in videos.get("videos") or []])

    print()
    print("=" * 74)
    print("7. 联想")
    print("=" * 74)
    suggest = client.post("/api/material/suggest", json={"query": "微胖"}).json()
    print("   联想:", (suggest.get("items") or [])[:3])
    check("联想有结果", len(suggest.get("items") or []) >= 1, suggest)
    top = client.post("/api/material/suggest", json={"query": ""}).json()
    check("空输入给热门文案", len(top.get("items") or []) >= 1, top)

    print()
    print("=" * 74)
    print("8. 标注主播")
    print("=" * 74)
    resp = client.post("/api/material/label", json={"folder": str(DIR_B), "publisher": "阿文(改)"})
    check("标注接口 200", resp.status_code == 200, resp.text[:200])
    check("标注更新 1 行", resp.json().get("updated") == 1, resp.json())
    relabeled = client.post("/api/material/search", json={"query": "显瘦", "publisher": "阿文(改)"}).json()
    check("按新主播名能搜到", relabeled["videos"] == 1, relabeled["videos"])
    check("空主播名被拒绝",
          client.post("/api/material/label", json={"folder": str(DIR_B), "publisher": " "}).status_code == 400)

    print()
    print("=" * 74)
    print("9. 打开视频（只校验路径不存在时的错误处理）")
    print("=" * 74)
    resp = client.post("/api/material/open", json={"path": str(WORK / "nope.flv"), "start": 0})
    check("不存在的视频返回 404", resp.status_code == 404, resp.status_code)

    print()
    print("=" * 74)
    print("10. 清空索引")
    print("=" * 74)
    resp = client.post("/api/material/clear")
    check("清空接口 200", resp.status_code == 200, resp.text[:200])
    status = client.get("/api/material/status").json()
    check("清空后视频数 0", status.get("videos") == 0, status.get("videos"))
    check("清空后句子数 0", status.get("sentences") == 0, status.get("sentences"))
    left_libs = client.post("/api/material/libraries", json={}).json()["libraries"]
    check("清空后不残留空索引", left_libs == [], left_libs)
    print(f"   清空后索引列表: {[(l['name'], l['videos']) for l in left_libs]}")
    empty = client.post("/api/material/search", json={"query": "显瘦"}).json()
    check("空库检索给出提示而不是报错", empty.get("ok") is False and "素材库" in str(empty.get("message")), empty)
    check("空查询被拒绝", client.post("/api/material/search", json={"query": "  "}).status_code == 400)

    print()
    print("=" * 74)
    print("11. 不勾选「包含子文件夹」时的兜底（回归：曾静默入库 0 个）")
    print("=" * 74)
    client.post("/api/material/clear")
    resp = client.post("/api/material/index/start", json={
        "path": str(ROOT), "recursive": False, "generate_srt": False, "publish": False,
    })
    task = wait_task(resp.json()["task_id"])
    print("  任务:", task.get("status"), "| 入库", task.get("result_count"))
    check("不勾选子文件夹也会兜底入库（不再静默 0 个）",
          int(task.get("result_count") or 0) == 2, task)
    status = client.get("/api/material/status").json()
    check("兜底后索引里有 2 个视频", status.get("videos") == 2, status.get("videos"))
    check("兜底后仍能检索", client.post("/api/material/search", json={"query": "显瘦"}).json().get("videos") == 2)

    print()
    print("=" * 74)
    print("12. 空目录给出可执行提示")
    print("=" * 74)
    empty_dir = WORK / "空目录"
    empty_dir.mkdir(exist_ok=True)
    client.post("/api/material/clear")
    resp = client.post("/api/material/index/start", json={
        "path": str(empty_dir), "recursive": True, "generate_srt": False, "publish": False,
    })
    wait_task(resp.json()["task_id"])
    # 提示走的是日志通道（前端日志面板收的就是它），不是 /api/tasks
    logs = " ".join(str(item.get("message") or "") for item in server._snapshot_logs(0))
    print("  日志含提示:", "该目录是空的" in logs)
    check("空目录日志给出下一步提示", "该目录是空的" in logs, logs[-240:])

    print()
    print("=" * 74)
    print("13. 导出 / 导入索引（按主播分卷共享）")
    print("=" * 74)
    _LICENSE["granted"] = True
    client.post("/api/material/clear")
    resp = client.post("/api/material/index/start", json={
        "path": str(ROOT), "recursive": True, "generate_srt": False, "publish": False,
    })
    wait_task(resp.json()["task_id"])
    check("准备数据：库里 2 个视频", client.get("/api/material/status").json()["videos"] == 2)

    import gzip as _gzip
    import sqlite3 as _sqlite3
    import zipfile as _zipfile

    # 索引列表：一个素材目录 = 一个索引
    libs = client.post("/api/material/libraries", json={}).json()
    check("索引列表接口可用", libs.get("ok") is True, libs)
    lib_list = libs.get("libraries") or []
    check("已经有一个索引（刚才建的那个目录）", len(lib_list) >= 1, lib_list)
    target_lib = lib_list[0]
    check("索引记录了素材目录", bool(target_lib.get("root")), target_lib)
    check("索引统计了视频数", int(target_lib.get("videos") or 0) >= 1, target_lib)
    print(f"   索引: {target_lib['name']} ({target_lib['kind']}) {target_lib['videos']} 视频 / {target_lib['sentences']} 句")

    # 搜索范围可以限定到某一个索引
    scoped = client.post("/api/material/search", json={
        "query": "显瘦", "mode": "smart", "lib_ids": [target_lib["id"]],
    }).json()
    full = client.post("/api/material/search", json={"query": "显瘦", "mode": "smart"}).json()
    check("按索引限定检索可用", scoped.get("ok") is True, scoped.get("message"))
    check("检索结果回显了索引范围（防旧版静默全搜）",
          scoped.get("scope_lib_ids") == [target_lib["id"]], scoped.get("scope_lib_ids"))
    check("不限定时不带索引范围", not full.get("scope_lib_ids"), full.get("scope_lib_ids"))
    check("限定单个索引时结果不超过全库",
          int(scoped.get("videos") or 0) <= int(full.get("videos") or 0),
          (scoped.get("videos"), full.get("videos")))
    print(f"   全库 {full.get('videos')} 视频 / 限定索引 {scoped.get('videos')} 视频")

    # 导出这个索引
    lib_zip = WORK / "一个索引.zip"
    resp = client.post("/api/material/export", json={
        "lib_ids": [target_lib["id"]], "choose": False, "path": str(lib_zip),
    })
    check("按索引导出返回 200", resp.status_code == 200, resp.text[:200])
    lib_payload = resp.json()
    check("按索引导出用了索引维度", lib_payload.get("mode") == "library", lib_payload.get("mode"))
    lib_task = wait_task(lib_payload["task_id"])
    check("按索引导出完成", lib_task.get("status") == "completed", lib_task)
    with _zipfile.ZipFile(lib_payload["path"]) as zf:
        inner_lib = WORK / "lib.sqlite3"
        inner_lib.write_bytes(_gzip.decompress(zf.read("material_index.sqlite3.gz")))
    conn = _sqlite3.connect(str(inner_lib))
    conn.row_factory = _sqlite3.Row
    lib_videos = conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"]
    conn.close()
    check("按索引导出的视频数与索引一致", lib_videos == int(target_lib["videos"]),
          (lib_videos, target_lib["videos"]))
    print(f"   索引导出: {lib_videos} 视频")

    options = client.post("/api/material/export-options", json={}).json()
    check("导出选项返回主播列表", len(options.get("streamers") or []) == 2, options)
    check("导出选项返回目录列表", len(options.get("directories") or []) == 2, options.get("directories"))
    print("   主播:", [(item["label"], item["videos"]) for item in options["streamers"]])
    print("   目录:", [(item["label"], item["videos"]) for item in options["directories"]])

    # 按目录分卷导出
    dir_zip = WORK / "一个目录.zip"
    target_dir = options["directories"][0]["path"]
    expect_dir_videos = options["directories"][0]["videos"]
    resp = client.post("/api/material/export", json={
        "paths": [target_dir], "choose": False, "path": str(dir_zip),
    })
    check("按目录导出返回 200", resp.status_code == 200, resp.text[:200])
    dir_payload = resp.json()
    check("按目录导出用了目录维度", dir_payload.get("mode") == "directory", dir_payload.get("mode"))
    dir_task = wait_task(dir_payload["task_id"])
    check("按目录导出完成", dir_task.get("status") == "completed", dir_task)
    with _zipfile.ZipFile(dir_payload["path"]) as zf:
        inner_dir = WORK / "dir.sqlite3"
        inner_dir.write_bytes(_gzip.decompress(zf.read("material_index.sqlite3.gz")))
    conn = _sqlite3.connect(str(inner_dir))
    conn.row_factory = _sqlite3.Row
    dir_videos = conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"]
    dirs_in_pack = [
        str(row["dir"]) for row in conn.execute("SELECT DISTINCT dir FROM videos")
    ]
    conn.close()
    check("按目录导出只含该目录", dir_videos == expect_dir_videos, (dir_videos, expect_dir_videos))
    check("包内目录唯一", len(dirs_in_pack) == 1, dirs_in_pack)
    print(f"   目录导出: {target_dir} -> {dir_videos} 视频")

    archive = WORK / "一卷.zip"
    resp = client.post("/api/material/export", json={
        "publishers": ["小雅服饰"], "choose": False, "path": str(archive),
    })
    check("导出接口返回 200", resp.status_code == 200, resp.text[:200])
    payload = resp.json()
    check("导出已启动", payload.get("ok") is True, payload)
    task = wait_task(payload["task_id"])
    check("导出任务完成", task.get("status") == "completed", task)
    check("导出文件已生成", Path(payload["path"]).is_file(), payload.get("path"))
    print("   导出到:", payload["path"])

    # 导出里只有 1 个主播，不含另一个
    with _zipfile.ZipFile(payload["path"]) as zf:
        inner = WORK / "exported.sqlite3"
        inner.write_bytes(_gzip.decompress(zf.read("material_index.sqlite3.gz")))
    conn = _sqlite3.connect(str(inner))
    conn.row_factory = _sqlite3.Row
    pubs = [str(row["publisher"]) for row in conn.execute("SELECT DISTINCT publisher FROM videos")]
    leak = conn.execute(
        "SELECT COUNT(*) AS n FROM videos WHERE path LIKE 'E:%' OR path LIKE 'C:%'"
    ).fetchone()["n"]
    conn.close()
    check("导出的卷只含所选主播", pubs == ["小雅服饰"], pubs)
    check("导出包不含本机绝对路径", leak == 0, leak)

    # 导入到空库：用临时用户数据目录再造一个索引不合适，这里直接导入原库（应全部跳过）
    resp = client.post("/api/material/import", json={"paths": [payload["path"]]})
    check("导入接口返回 200", resp.status_code == 200, resp.text[:200])
    imp_task = wait_task(resp.json()["task_id"])
    check("导入任务完成", imp_task.get("status") == "completed", imp_task)
    print("   导入结果:", imp_task.get("message"))
    check("相同内容重复导入被跳过", int(imp_task.get("result_count") or 0) == 0,
          imp_task.get("result_count"))
    check("导入后视频数不变", client.get("/api/material/status").json()["videos"] == 2)

    resp = client.post("/api/material/import", json={"paths": [str(WORK / "不存在.zip")]})
    check("不存在的索引包被拒绝", resp.status_code == 400, resp.status_code)

    # 防回归：机器调用（choose=False）不给 path 时，绝不能写进用户的素材目录
    resp = client.post("/api/material/export", json={"lib_ids": [target_lib["id"]], "choose": False})
    if resp.status_code == 200:
        auto_path = str(resp.json().get("path") or "")
        check("机器调用导出不写进素材目录",
              not auto_path.lower().startswith(str(ROOT).lower()), auto_path)
        check("机器调用导出落到用户数据目录",
              "material_exports" in auto_path.replace("/", "\\").lower(), auto_path)
        wait_task(resp.json()["task_id"])
        print(f"   自动导出路径: {auto_path}")
    else:
        # 也可能因为正在忙被拒（409），同样不算写进素材目录
        check("机器调用导出未写到素材目录（被拒也算通过）", resp.status_code in (409, 200), resp.status_code)

    # 删除索引：只删它自己的，且视频文件不受影响
    resp = client.post("/api/material/library/remove", json={"library_id": 999999})
    check("删除不存在的索引被拒绝", resp.status_code == 404, resp.status_code)
    before_libs = len(client.post("/api/material/libraries", json={}).json()["libraries"])
    resp = client.post("/api/material/library/remove", json={"library_id": target_lib["id"]})
    check("删除索引返回 200", resp.status_code == 200, resp.text[:200])
    after_libs = len(client.post("/api/material/libraries", json={}).json()["libraries"])
    check("删除后索引数减少", after_libs == before_libs - 1, (before_libs, after_libs))
    check("视频文件仍在硬盘上", Path(target_dir).is_dir(), target_dir)
    print(f"   索引数 {before_libs} -> {after_libs}，素材目录仍在: {Path(target_dir).is_dir()}")

    # 打开视频地址：真实文件可定位，不存在的路径要被明确拒绝
    real_video = ""
    for item in (client.post("/api/material/videos", json={}).json().get("videos") or []):
        if Path(str(item.get("path") or "")).is_file():
            real_video = str(item["path"])
            break
    if real_video:
        import server as _srv
        calls = []
        _orig_popen = __import__("subprocess").Popen
        __import__("subprocess").Popen = lambda *a, **k: calls.append(a) or type("P", (), {"pid": 1})()
        try:
            resp = client.post("/api/material/reveal", json={"path": real_video})
        finally:
            __import__("subprocess").Popen = _orig_popen
        check("打开视频地址返回 200", resp.status_code == 200, resp.text[:200])
        check("真的调用了资源管理器 /select", bool(calls), calls)
        print(f"   打开地址: {Path(real_video).name[:40]} -> explorer /select")
    resp = client.post("/api/material/reveal", json={"path": str(WORK / "没有这个文件.mp4")})
    check("不存在的视频地址被拒绝", resp.status_code == 404, resp.status_code)
    resp = client.post("/api/material/reveal", json={"path": ""})
    check("空路径被拒绝", resp.status_code == 404, resp.status_code)

    print()
    print("=" * 74)
    print(f"汇总: {PASSED} 通过 / {FAILED} 失败")
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
