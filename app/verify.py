"""LiveClipper verify - run after every code change"""
import sys, os, py_compile, hashlib

APP_DIR = os.path.dirname(os.path.abspath(__file__))
THREE_DIRS = [
    os.path.join(os.environ.get('USERPROFILE', ''), '.openclaw-autoclaw', 'workspace', 'live_cutter', 'app'),
    os.path.join(os.environ.get('USERPROFILE', ''), 'Documents', 'GitHub', 'LiveClipper', 'app'),
    os.path.join(os.environ.get('USERPROFILE', ''), 'LiveClipper', 'app'),
]

errors = []
passed = 0

def check(name, condition, ok_msg="OK", fail_msg="FAIL"):
    global errors, passed
    if condition:
        print(f"  [OK] {name}: {ok_msg}")
        passed += 1
    else:
        print(f"  [!!] {name}: {fail_msg}")
        errors.append(name)

# === 1. Syntax Check ===
print("\n[1] Syntax check")
for f in sorted(os.listdir(APP_DIR)):
    if f.endswith('.py'):
        try:
            py_compile.compile(os.path.join(APP_DIR, f), doraise=True)
        except py_compile.PyCompileError as e:
            check(f, False, fail_msg=str(e)[:80])
            break
else:
    check("All .py files", True, ok_msg="syntax OK")

# === 2. Key Function Tests ===
print("\n[2] Core function tests")
sys.path.insert(0, APP_DIR)

# srt_splitter: no duplicate index
try:
    from srt_splitter import split_long_srt_entries
    srt = '1\n00:00:00,000 --> 00:00:02,000\ntest1\n\n2\n00:00:03,000 --> 00:00:05,000\ntest2\n\n'
    result = split_long_srt_entries(srt)
    lines = result.strip().split('\n')
    has_dup = any(lines[i].strip().isdigit() and i+1 < len(lines) and lines[i+1].strip().isdigit() 
                  for i in range(len(lines)-1))
    check("srt_splitter", not has_dup, ok_msg="no dup index", fail_msg="DUPLICATE INDEX BUG!")
except Exception as e:
    check("srt_splitter", False, fail_msg=str(e)[:60])

# multi_version: hook first
try:
    from multi_version import _arrange_version
    clips = [('product','fabric',10,15,8,5),('hook','look!',5,10,9,5),('close','buy',30,35,7,5)]
    arranged = _arrange_version(clips)
    check("_arrange_version", arranged[0][0]=='hook', ok_msg="hook first", fail_msg=f"first={arranged[0][0]}")
except Exception as e:
    check("_arrange_version", False, fail_msg=str(e)[:60])

# license_client: invalid code rejected + plan_days
try:
    from license_client import validate_code, PLAN_DAYS
    result = validate_code('invalid')
    check("validate_code", not result['ok'], ok_msg="rejects invalid", fail_msg="ACCEPTED invalid!")
    check("PLAN_DAYS", PLAN_DAYS == {'01':30,'02':90,'03':365}, ok_msg=str(PLAN_DAYS), fail_msg=str(PLAN_DAYS))
except Exception as e:
    check("license_client", False, fail_msg=str(e)[:60])

# forbidden words exist
try:
    from ai_clipper import _filter_price_and_cta
    import inspect
    src = inspect.getsource(_filter_price_and_cta)
    has_free = 'free' in src.lower()
    has_ban = '\u699c\u4e8c' in src  # 榜二
    check("forbidden_words", has_free and has_ban, ok_msg="has free/bang'er", fail_msg="MISSING forbidden words!")
except Exception as e:
    check("forbidden_words", False, fail_msg=str(e)[:60])

# === 3. File Consistency ===
print("\n[3] 3-location consistency")
all_hashes = []
for d in THREE_DIRS:
    h_map = {}
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            fp = os.path.join(d, f)
            if os.path.isfile(fp):
                h_map[f] = hashlib.sha256(open(fp,'rb').read()).hexdigest()[:12]
    all_hashes.append(h_map)

if len(all_hashes) >= 2 and all_hashes[0] and all_hashes[1]:
    skip = {'.installed_version', 'ai_settings.json'}
    diff_files = []
    for f in all_hashes[0]:
        if f in skip:
            continue
        vals = [h.get(f) for h in all_hashes if h]
        if len(set(v for v in vals if v)) > 1:
            diff_files.append(f)
    check("3 locations", len(diff_files)==0, ok_msg="all match", fail_msg=f"{len(diff_files)} diff: {', '.join(diff_files[:5])}")
else:
    check("3 locations", False, fail_msg="dir not found")

# === 4. version.json ===
print("\n[4] Version")
try:
    import json
    for d in THREE_DIRS:
        vf = os.path.join(d, 'version.json')
        if os.path.exists(vf):
            with open(vf,'r',encoding='utf-8') as f:
                v = json.load(f)
            ver = v.get('version','?')
            print(f"  v{ver}")
            break
except Exception as e:
    print(f"  warn: {e}")

# === Summary ===
print(f"\n{'='*40}")
if errors:
    print(f"[FAIL] {len(errors)} items failed: {', '.join(errors)}")
    sys.exit(1)
else:
    print(f"[PASS] All {passed} checks OK")
