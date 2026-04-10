"""
Clip Tuple Validator — 一键检测所有 .py 文件中的元组解包兼容性
用法: python clip_tuple_check.py [expected_len]
默认 expected_len=7 (当前 clip 元组长度)
"""
import os, re, sys

EXPECTED = int(sys.argv[1]) if len(sys.argv) > 1 else 7
APP_DIR = os.path.dirname(os.path.abspath(__file__))

# clip 元组字段名（按顺序）
CLIP_FIELDS = ['ct', 'c_type', 'text', 'c_text', 'start', 'c_start', 'end', 'c_end', 'score', 'c_score', 'dur', 'c_dur', 'focus', 's', 'e', 'sc', 'd']

errors = []
warnings = []

for fname in sorted(os.listdir(APP_DIR)):
    if not fname.endswith('.py') or fname.startswith('_'):
        continue
    fpath = os.path.join(APP_DIR, fname)
    with open(fpath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if s.startswith('#') or 'import' in s:
            continue
        
        # 1. 检测 N 变量解包赋值 (不含 [:6] 或 *_ 或索引)
        #    e.g. ct, text, s, e, sc, d = clip
        m = re.match(r'^[^#]*?(\w+)\s*,\s*(\w+)\s*,\s*(\w+)\s*,\s*(\w+)\s*,\s*(\w+)\s*,\s*(\w+)\s*=\s*(.+)', s)
        if m:
            n_vars = 6  # 6 comma-separated = 6 variables
            rhs = m.group(7).strip()
            # 跳过函数调用参数（如 volcengine_asr(a, b, c, d, e, f)）
            if rhs.startswith('(') or rhs.startswith('"') or rhs.startswith("'"):
                continue
            # 跳过已修复的
            if '[:6]' in rhs or '[_]' in rhs or '[0]' in rhs or 'len(' in rhs or 'c[:7]' in rhs or '(*c' in rhs:
                continue
            # 检查变量名是否像 clip 字段
            all_vars = [m.group(j) for j in range(1, 7)]
            is_clip_related = any(v in CLIP_FIELDS for v in all_vars)
            if is_clip_related:
                errors.append(f"{fname}:{i}: {n_vars}-var unpack without [:{EXPECTED}] or *_: {s[:120]}")
        
        # 2. 检测 for 循环中的 N 变量解包
        m2 = re.match(r'for\s+(\w+)\s*,\s*(\w+)\s*,\s*(\w+)\s*,\s*(\w+)\s*,\s*(\w+)\s*,\s*(\w+)\s+in\s+(.+)', s)
        if m2:
            rhs = m2.group(7).strip()
            if '[:6]' in rhs or '[_]' in rhs or '*_' in s:
                continue
            all_vars = [m2.group(j) for j in range(1, 7)]
            is_clip_related = any(v in CLIP_FIELDS for v in all_vars)
            if is_clip_related:
                errors.append(f"{fname}:{i}: for-loop {len(all_vars)}-var unpack: {s[:120]}")
        
        # 3. 检测 append 中的元组构造（不含 focus/*_）
        if '.append((' in s and ', ' in s:
            try:
                inner = s[s.index('.append((')+9:s.rindex('))')]
            except ValueError:
                continue
            depth = 0
            commas = 0
            for ch in inner:
                if ch == '(': depth += 1
                elif ch == ')': depth -= 1
                elif ch == ',' and depth == 0: commas += 1
            n_elements = commas + 1
            if n_elements == 6 and '*_' not in inner and 'focus' not in inner:
                is_clip = any(v in inner for v in ['c_type', 'ct', 'c_text', 'c_start', 'c_end', 'c_score', 'c_dur'])
                if is_clip:
                    errors.append(f"{fname}:{i}: append {n_elements}-tuple (expected {EXPECTED}): {s[:120]}")

if errors:
    print(f"❌ Found {len(errors)} issues (clip tuple expected {EXPECTED} elements):")
    for e in errors:
        print(f"  {e}")
else:
    print(f"✅ All clip tuple unpacking is compatible with {EXPECTED}-element tuples")

if warnings:
    print(f"\n⚠️  Warnings ({len(warnings)}):")
    for w in warnings:
        print(f"  {w}")

sys.exit(1 if errors else 0)
