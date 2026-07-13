from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class CategoryProfile:
    key: str
    family: str
    feedback_bucket: str
    aliases: tuple[str, ...]
    product_keywords: tuple[str, ...]
    filename_keywords: tuple[str, ...]
    focus_order: tuple[str, ...]
    prompt_rule: str
    system_overlay: str = ""

    def matches(self, value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        return any(text == item or text in item or item in text for item in (self.key, *self.aliases))


VERTICAL_CATEGORY_PROFILES: tuple[CategoryProfile, ...] = (
    CategoryProfile(
        key="食品/生鲜",
        family="food_fresh",
        feedback_bucket="food_fresh",
        aliases=("食品", "生鲜", "零食", "农产品"),
        product_keywords=(),
        filename_keywords=(),
        focus_order=(
            "口感食欲", "新鲜品质", "产地溯源", "规格分量", "发货保鲜",
            "场景吃法", "性价比", "情绪感染", "紧迫稀缺", "对比优势", "其他",
        ),
        prompt_rule="主推食品/生鲜品类。先识别本场主商品和子类型，再围绕口感、新鲜、产地、规格、保鲜和吃法组织内容。",
    ),
    CategoryProfile(
        key="美妆护肤",
        family="beauty",
        feedback_bucket="beauty",
        aliases=("美妆", "护肤", "彩妆", "个护"),
        product_keywords=(
            "口红", "唇釉", "粉底", "气垫", "遮瑕", "散粉", "眼影", "腮红",
            "睫毛膏", "眉笔", "面霜", "精华", "乳液", "爽肤水", "面膜", "防晒",
            "洁面", "洗面奶", "卸妆", "香水", "护发", "洗发水",
        ),
        filename_keywords=(
            "口红", "唇釉", "粉底", "气垫", "遮瑕", "散粉", "眼影", "腮红",
            "精华", "面霜", "乳液", "面膜", "防晒", "洁面", "香水",
        ),
        focus_order=(
            "使用效果", "肤感质地", "颜色妆效", "成分特点", "适用人群",
            "使用方法", "持妆体验", "场景搭配", "对比优势", "其他",
        ),
        prompt_rule="主推美妆护肤品类。围绕真实妆效、肤感、质地、使用方法和适用场景组织内容，不套用服装版型规则。",
        system_overlay=(
            "[品类覆盖: 美妆护肤直播切片]\n"
            "先锁定具体商品与用途，再选择真实使用效果、肤感质地、颜色妆效、成分特点、适用人群和使用方法。\n"
            "不得把普通化妆品剪成治疗、修复疾病、永久改变身体或保证见效的医疗功效；安全过滤仍按全局硬规则执行。"
        ),
    ),
    CategoryProfile(
        key="家居百货",
        family="household",
        feedback_bucket="household",
        aliases=("百货", "家居", "日用百货", "家清"),
        product_keywords=(
            "收纳", "清洁", "拖把", "纸巾", "垃圾袋", "洗衣液", "洗洁精", "锅",
            "杯子", "餐具", "床品", "四件套", "枕头", "毛巾", "衣架", "置物架",
            "小家电", "风扇", "台灯", "保温杯",
        ),
        filename_keywords=(
            "收纳", "拖把", "纸巾", "垃圾袋", "洗衣液", "洗洁精", "锅具",
            "餐具", "床品", "枕头", "毛巾", "衣架", "置物架", "小家电", "保温杯",
        ),
        focus_order=(
            "功能效果", "使用演示", "材质做工", "规格容量", "适用场景",
            "清洁维护", "耐用体验", "对比优势", "其他",
        ),
        prompt_rule="主推家居百货品类。优先保留功能演示、使用前后效果、材质做工、规格容量和真实使用场景。",
        system_overlay=(
            "[品类覆盖: 家居百货直播切片]\n"
            "先锁定具体商品，再按痛点、功能演示、效果证据、规格材质、使用场景和维护方式推进。\n"
            "不得套用服装上身、显瘦、尺码等规则，也不得用无法从字幕证明的绝对耐用或安全承诺。"
        ),
    ),
)


def iter_vertical_profiles() -> Iterable[CategoryProfile]:
    return VERTICAL_CATEGORY_PROFILES


def resolve_vertical_profile(category: str | None) -> CategoryProfile | None:
    text = str(category or "").strip()
    if not text:
        return None
    for profile in VERTICAL_CATEGORY_PROFILES:
        if profile.matches(text):
            return profile
    return None


def category_family(category: str | None) -> str:
    profile = resolve_vertical_profile(category)
    if profile:
        return profile.family
    return "clothing" if str(category or "").strip() else "general"
