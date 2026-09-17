# -*- coding: utf-8 -*-
import json, urllib.request, time, os

BASE = "http://127.0.0.1:8767"
VID = r"C:\工作\小贤\单品素材\7-22单品\AYOBE_小贤 7月22日09_00新品 韩系学姐清纯减龄感学院风连衣裙\AYOBE_小贤 7月22日09_00新品 韩系学姐清纯减龄感学院风连衣裙_1.mp4"
payload = {
    "video_paths": [VID], "output_dir": "",
    "primary_category": "服饰内衣", "category": "自动检测", "focus_hint": "自动",
    "ai_controls": {
        "content_policy": {"price": "block", "cta": "block", "inventory_pressure": "block",
                           "source_claim": "block", "social_proof": "body_only", "after_sale": "block",
                           "size_interaction": "body_only", "live_interaction": "block", "custom_rules": []},
        "avoid": ["无关闲聊", "无效重复"],
        "director_controls": {"contract_version": "director-controls-v2", "primary_category": "服饰内衣",
            "secondary_category": "", "leaf_category": "", "main_product": "",
            "source_product_hints": ["AYOBE_小贤 7月22日09_00新品 韩系学姐清纯减龄感学院风连衣裙_1"],
            "director_direction": "", "extra_instruction": "", "supporting_products": "allow",
            "priority_theme": "", "preferred_topics": [], "preferred_terms": [], "preference_weights": {},
            "avoid": ["无关闲聊", "无效重复"], "opening_style": "", "ending_style": ""},
    },
    "target_duration": 90, "duration_tolerance": None, "versions": 1, "dedup_preset": "custom",
    "video": {"mirror": True, "crop": True, "crop_value": 5, "speed": True, "speed_value": 115,
              "frame_structure": True, "frame_structure_level": "heavy", "blur": False, "blur_value": 2,
              "sharpen": False, "sharpen_value": 30, "gamma_shift": True, "corner_mask": True, "bg_fill": False},
}
def post(p, b):
    r = urllib.request.Request(BASE + p, data=json.dumps(b).encode("utf-8"),
                               headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))
def get(p):
    with urllib.request.urlopen(BASE + p, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))

s = post("/api/mix/preview/start", payload)
pid = s.get("preview_id")
print("preview_id:", pid, "task:", s.get("task_id"))
for i in range(240):
    time.sleep(6)
    p = get("/api/mix/preview/" + pid)
    if p.get("status") in ("ready", "failed"):
        json.dump(p, open("_verify3.json", "w", encoding="utf-8"), ensure_ascii=False)
        print("FINAL:", p.get("status"), str(p.get("error") or "")[:150], "task:", s.get("task_id"))
        break
    if i % 5 == 0:
        print(f"[{i*6}s]", p.get("status"))
