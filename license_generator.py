"""
激活码生成器 - 管理员使用
用于为付费用户生成激活码

使用方式：
  python license_generator.py monthly    # 生成月付激活码
  python license_generator.py yearly     # 生成年付激活码
  python license_generator.py monthly 3  # 批量生成3个月付码
"""

import hmac
import hashlib
import time
import sys
import os
import json
import secrets

# ============================================================
# ⚠️ 密钥 - 必须与 license_client.py 中的密钥完全一致
# 生成新密钥: python -c "print(os.urandom(32).hex())"
# ============================================================
SECRET_KEY = "lc8f3a2e7d1b9c6f4e0a5d8c3f7b2e9a1d4c6f0e8b3a5d7c9f2e4b6a8d0c3f7"

PLAN_CONFIG = {
    "monthly": {"days": 30, "label": "月付", "price": "69 yuan/month"},
    "quarterly": {"days": 90, "label": "季付", "price": "169 yuan/quarter"},
    "yearly": {"days": 365, "label": "年付", "price": "399 yuan/year"},
    "permanent": {"days": 36500, "label": "永久", "price": "999 yuan/lifetime"},
}


def generate_code(plan="monthly", distributor_id="00"):
    """
    生成一个激活码
    返回: (code, info_dict)
    
    码结构（34位十六进制，含分销商ID）：
      XX          - 套餐类型（01=月付, 02=季付, 03=年付）
      XX          - 分销商ID（00=直销, 01-FF=分销商A/B/...）
      XXXXXXXX    - 过期时间戳（Unix timestamp, hex）
      XX          - 随机数（防重复）
      XXXXXXXXXXXXXXXXXX - HMAC-SHA256 签名（20位，80bit）
    
    注意：旧版32位码（无分销商ID）仍然有效，兼容验证
    """
    now = int(time.time())
    days = PLAN_CONFIG[plan]["days"]
    expires_at = now + (days * 86400)
    nonce = secrets.token_hex(1)  # 2 hex chars

    # 编码 payload
    plan_hex = {"monthly": "01", "quarterly": "02", "yearly": "03", "permanent": "04"}[plan]
    expires_hex = format(expires_at & 0xFFFFFFFF, "08x")  # 8 hex chars
    dist_hex = distributor_id.lower().zfill(2)[:2]  # 确保2位hex
    payload = plan_hex + dist_hex + expires_hex + nonce  # 2+2+8+2 = 14

    # HMAC 签名
    signature = hmac.new(
        SECRET_KEY.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()[:20]

    # 组合
    raw_code = payload + signature  # 2+2+8+2+20 = 34 hex chars

    # 格式化: XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XX
    parts = [raw_code[i:i+4] for i in range(0, 32, 4)]
    parts.append(raw_code[32:34])
    formatted = "-".join(parts)  # 9组

    from datetime import datetime
    info = {
        "code": formatted,
        "plan": plan,
        "plan_label": PLAN_CONFIG[plan]["label"],
        "price": PLAN_CONFIG[plan]["price"],
        "distributor_id": dist_hex,
        "expires_at": expires_at,
        "expires_date": datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d"),
        "generated_at": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M"),
    }

    return formatted, info


def batch_generate(plan, count=1, distributor_id="00"):
    """批量生成激活码
    
    Args:
        plan: 套餐类型
        count: 生成数量
        distributor_id: 分销商ID（"00"=直销）
    """
    codes = []
    for _ in range(count):
        code, info = generate_code(plan, distributor_id)
        codes.append(info)
    return codes


def save_to_json(codes, filepath="generated_codes.json"):
    """保存生成的码到 JSON 文件"""
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(codes, f, ensure_ascii=False, indent=2)
    return filepath


def main():
    if len(sys.argv) < 2:
        print("激活码生成器 v2.0（含分销商ID）")
        print("=" * 50)
        print(f"用法: python license_generator.py <monthly|quarterly|yearly> [数量] [分销商ID]")
        print(f"示例: python license_generator.py monthly 3")
        print(f"      python license_generator.py monthly 3 01  # 分销商01")
        print(f"      python license_generator.py monthly 1 00  # 直销(默认)")
        print()
        # 交互模式
        plan = input("套餐类型 (1=月付69元, 2=季付169元, 3=年付399元, 4=永久999元): ").strip()
        plan = {"1": "monthly", "2": "quarterly", "3": "yearly", "4": "permanent"}.get(plan, "monthly")
        count = int(input("生成数量: ").strip() or "1")
        dist_input = input("分销商ID (00=直销, 01-FF=分销商, 回车默认00): ").strip()
        distributor_id = dist_input if dist_input else "00"
    else:
        plan = sys.argv[1].lower()
        count = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        distributor_id = sys.argv[3] if len(sys.argv) > 3 else "00"

    if plan not in PLAN_CONFIG:
        print(f"无效套餐: {plan}，请用 monthly/quarterly/yearly/permanent")
        sys.exit(1)
    
    # 验证分销商ID格式
    try:
        int(distributor_id, 16)
        if len(distributor_id) > 2:
            print(f"分销商ID必须是1-2位十六进制（00-FF），收到: {distributor_id}")
            sys.exit(1)
    except ValueError:
        print(f"分销商ID格式错误: {distributor_id}，需要十六进制（00-FF）")
        sys.exit(1)

    codes = batch_generate(plan, count, distributor_id)
    save_to_json(codes)

    print(f"\n{'='*50}")
    print(f"已生成 {count} 个{PLAN_CONFIG[plan]['label']}激活码")
    print(f"{'='*50}")

    for i, c in enumerate(codes, 1):
        print(f"\n[{i}] {c['code']}")
        print(f"    套餐: {c['plan_label']} ({c['price']})")
        print(f"    分销商: {c.get('distributor_id', '00')} ({'直销' if c.get('distributor_id', '00') == '00' else '分销商'})")
        print(f"    过期: {c['expires_date']}")
        print(f"    生成: {c['generated_at']}")

    print(f"\n已保存到 generated_codes.json")


if __name__ == "__main__":
    main()
