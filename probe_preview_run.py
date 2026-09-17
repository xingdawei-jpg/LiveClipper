# -*- coding: utf-8 -*-
import json, urllib.request, time, sys

BASE = "http://127.0.0.1:8767"
VID = r"C:\工作\小贤\8-22单品\【AYOBE】小贤 8月22日1000新品 废土松弛风酷感连帽工装夹克外套\【AYOBE】小贤 8月22日1000新品 废土松弛风酷感连帽工装夹克外套 8.22.mp4"
SRT = r"C:\工作\小贤\8-22单品\【AYOBE】小贤 8月22日1000新品 废土松弛风酷感连帽工装夹克外套\【AYOBE】小贤 8月22日1000新品 废土松弛风酷感连帽工装夹克外套 8.22.srt"

payload = {
    "video_paths": [VID],
    "srt_path": SRT,
    "output_dir": "",
    "output_naming_mode": "source_timestamp",
    "primary_category": "服饰内衣",
    "category": "自动检测",
    "focus_hint": "自动",
    "ai_controls": {
        "content_policy": {
            "price": "block", "cta": "block", "inventory_pressure": "block",
            "source_claim": "block", "social_proof": "body_only", "after_sale": "block",
            "size_interaction": "body_only", "live_interaction": "block", "custom_rules": [],
        },
        "avoid": ["无关闲聊", "无效重复"],
        "director_controls": {
            "contract_version": "director-controls-v2",
            "primary_category": "服饰内衣",
            "secondary_category": "", "leaf_category": "", "main_product": "",
            "source_product_hints": ["【AYOBE】小贤 8月22日1000新品 废土松弛风酷感连帽工装夹克外套 8.22"],
            "director_direction": "", "extra_instruction": "",
            "supporting_products": "allow", "priority_theme": "",
            "preferred_topics": [], "preferred_terms": [], "preference_weights": {},
            "avoid": ["无关闲聊", "无效重复"], "opening_style": "", "ending_style": "",
        },
    },
    "target_duration": 90,
    "duration_tolerance": None,
    "versions": 1,
    "dedup_preset": "custom",
    "video": {
        "mirror": True, "crop": True, "crop_value": 5,
        "speed": True, "speed_value": 115,
        "frame_structure": True, "frame_structure_level": "heavy",
        "blur": False, "blur_value": 2, "sharpen": False, "sharpen_value": 30,
        "gamma_shift": True, "corner_mask": True, "bg_fill": False,
    },
}

def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))

start = post("/api/smart-cut/commerce-director/preview/start", payload)
pid = start.get("preview_id") or start.get("id")
print("started:", json.dumps(start, ensure_ascii=False)[:300])
print("preview_id:", pid)
json.dump({"preview_id": pid, "payload": payload}, open("probe_run.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

for i in range(200):  # 最多等 ~20 分钟
    time.sleep(6)
    try:
        p = get("/api/smart-cut/preview/" + pid)
    except Exception as e:
        print("poll error", e); continue
    st = p.get("status")
    if i % 5 == 0:
        print(f"[{i*6}s] status={st} clips={len(p.get('clips') or [])}")
    if st in ("ready", "failed"):
        json.dump(p, open("probe_preview.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("FINAL status:", st, " error:", str(p.get("error") or "")[:300])
        break
