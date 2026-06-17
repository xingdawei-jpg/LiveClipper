"""LiveClipper verify - run after every code change."""
import ast
import hashlib
import json
import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
THREE_DIRS = [
    os.path.join(os.environ.get("USERPROFILE", ""), ".openclaw-autoclaw", "workspace", "live_cutter", "app"),
    os.path.join(os.environ.get("USERPROFILE", ""), "Documents", "GitHub", "LiveClipper", "app"),
    os.path.join(os.environ.get("USERPROFILE", ""), "LiveClipper", "app"),
]

SKIP_SYNTAX_FILES = {
    "gui.py.bak",
    "gui_clean.py",
    "gui_fresh.py",
    "gui_tmp.py",
    "cutter_logic_corrupted.py.bak",
}

errors = []
warnings = []
passed = 0


def check(name, condition, ok_msg="OK", fail_msg="FAIL"):
    global passed
    if condition:
        print(f"  [OK] {name}: {ok_msg}")
        passed += 1
    else:
        print(f"  [!!] {name}: {fail_msg}")
        errors.append(name)


def warn(name, msg):
    warnings.append(name)
    print(f"  [WARN] {name}: {msg}")


print("\n[1] Syntax check")
for filename in sorted(os.listdir(APP_DIR)):
    if not filename.endswith(".py"):
        continue
    if filename in SKIP_SYNTAX_FILES or filename.startswith("_"):
        continue

    path = os.path.join(APP_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            ast.parse(f.read(), filename=filename)
    except SyntaxError as e:
        check(filename, False, fail_msg=f"line {e.lineno}: {e.msg}")
        break
    except Exception as e:
        check(filename, False, fail_msg=str(e)[:80])
        break
else:
    check("Runtime .py files", True, ok_msg="syntax OK")


print("\n[2] Core function tests")
sys.path.insert(0, APP_DIR)

try:
    from srt_splitter import split_long_srt_entries

    srt = "1\n00:00:00,000 --> 00:00:02,000\ntest1\n\n2\n00:00:03,000 --> 00:00:05,000\ntest2\n\n"
    result = split_long_srt_entries(srt)
    lines = result.strip().split("\n")
    has_dup = any(
        lines[i].strip().isdigit() and i + 1 < len(lines) and lines[i + 1].strip().isdigit()
        for i in range(len(lines) - 1)
    )
    check("srt_splitter", not has_dup, ok_msg="no dup index", fail_msg="DUPLICATE INDEX BUG!")
except Exception as e:
    check("srt_splitter", False, fail_msg=str(e)[:60])

try:
    from multi_version import _arrange_version

    clips = [("product", "fabric", 10, 15, 8, 5), ("hook", "look!", 5, 10, 9, 5), ("close", "buy", 30, 35, 7, 5)]
    arranged = _arrange_version(clips)
    check("_arrange_version", arranged[0][0] == "hook", ok_msg="hook first", fail_msg=f"first={arranged[0][0]}")
except Exception as e:
    check("_arrange_version", False, fail_msg=str(e)[:60])

try:
    from license_client import PLAN_DAYS, validate_code

    result = validate_code("invalid")
    expected_plans = {"00": 3, "01": 30, "02": 90, "03": 365, "04": 36500}
    check("validate_code", not result["ok"], ok_msg="rejects invalid", fail_msg="ACCEPTED invalid!")
    check("PLAN_DAYS", PLAN_DAYS == expected_plans, ok_msg=str(PLAN_DAYS), fail_msg=str(PLAN_DAYS))
except Exception as e:
    check("license_client", False, fail_msg=str(e)[:60])

try:
    from ai_clipper import _filter_price_and_cta

    with open(os.path.join(APP_DIR, "keywords.json"), "r", encoding="utf-8-sig") as f:
        keywords = json.load(f)
    forbidden = [w for w in keywords.get("forbidden_phrases", []) if w]
    check("forbidden_config", bool(forbidden), ok_msg=f"{len(forbidden)} terms", fail_msg="empty forbidden_phrases")

    sample = forbidden[0] if forbidden else "price"
    clips = [("product", f"test {sample} text", 0, 3, 8, 3), ("product", "normal selling point", 3, 6, 8, 3)]
    filtered = _filter_price_and_cta(clips)
    check(
        "forbidden_filter",
        len(filtered) == 1 and filtered[0][1] == "normal selling point",
        ok_msg="removes configured forbidden terms",
        fail_msg=f"filtered={filtered}",
    )
except Exception as e:
    check("forbidden_words", False, fail_msg=str(e)[:60])


print("\n[3] 3-location consistency")
all_hashes = []
for directory in THREE_DIRS:
    h_map = {}
    if os.path.isdir(directory):
        for filename in sorted(os.listdir(directory)):
            path = os.path.join(directory, filename)
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    h_map[filename] = hashlib.sha256(f.read()).hexdigest()[:12]
    all_hashes.append(h_map)

existing_hashes = [h for h in all_hashes if h]
if len(existing_hashes) >= 2:
    skip = {".installed_version", "ai_settings.json"}
    diff_files = []
    for filename in existing_hashes[0]:
        if filename in skip:
            continue
        vals = [h.get(filename) for h in existing_hashes]
        if len(set(v for v in vals if v)) > 1:
            diff_files.append(filename)
    if diff_files:
        warn("3 locations", f"{len(diff_files)} diff: {', '.join(diff_files[:5])}")
    else:
        check("3 locations", True, ok_msg="all match")
else:
    warn("3 locations", "only current workspace found")


print("\n[4] Version")
try:
    for directory in THREE_DIRS:
        version_path = os.path.join(directory, "version.json")
        if os.path.exists(version_path):
            with open(version_path, "r", encoding="utf-8-sig") as f:
                version = json.load(f)
            print(f"  v{version.get('version', '?')}")
            break
except Exception as e:
    warn("version", str(e))


print(f"\n{'=' * 40}")
if errors:
    print(f"[FAIL] {len(errors)} items failed: {', '.join(errors)}")
    sys.exit(1)

if warnings:
    print(f"[PASS] All {passed} checks OK ({len(warnings)} warnings)")
else:
    print(f"[PASS] All {passed} checks OK")
