"""Product identity contracts, not a keyword-based sentence selector.

The model resolves spoken references in source context. Code only compares
its explicit identity/relationship receipts with the user's immutable target.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence
import re

VERSION = "director-product-v2"
PRODUCT_TYPES = {
    "top": ("上衣",), "shirt": ("衬衫", "衬衣"),
    "tshirt": ("T恤", "t恤", "polo", "Polo衫"),
    "knitwear": ("针织衫", "针织罩衫", "毛衣", "羊毛衫", "羊绒衫", "开衫"),
    "sweatshirt": ("卫衣",), "outerwear": ("外套", "风衣", "大衣", "西装", "夹克", "防晒衣"),
    "down_jacket": ("羽绒服", "鹅绒服", "鸭绒服", "棉服"),
    "vest": ("马甲", "背心", "吊带"), "pants": ("裤子", "长裤", "短裤", "牛仔裤", "休闲裤", "卫裤", "阔腿裤", "西裤"),
    "skirt": ("半身裙", "短裙", "长裙"), "dress": ("连衣裙",),
    "set": ("套装", "两件套", "三件套"), "underwear": ("内衣", "文胸", "内裤", "睡衣", "家居服", "袜子"),
    "shoes": ("鞋子", "女鞋", "男鞋", "靴子"), "bag": ("包袋", "手提包", "双肩包"),
    "other": (), "unknown": (),
}
TOP_TYPES = {"top", "shirt", "tshirt", "knitwear", "sweatshirt", "outerwear", "down_jacket", "vest"}
AUTO = {"", "自动", "自动识别", "自动检测", "auto", "默认", "none"}
BROAD = {"衣服", "服装", "服饰", "女装", "男装", "服饰内衣", "女装上衣"}


def clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in AUTO else text[:120]


def named_types(value: Any) -> set[str]:
    """Match user/AI product NAMES only, never spoken sentences or clip text."""
    text = clean(value).lower()
    if text in PRODUCT_TYPES:
        return {text}
    found = {kind for kind, names in PRODUCT_TYPES.items() if any(name.lower() in text for name in names)}
    if len(found) > 1:
        found.discard("top")
    return found


def compatible(left: str, right: str) -> bool:
    return left == right or (left == "top" and right in TOP_TYPES) or (right == "top" and left in TOP_TYPES) or {left, right} <= {"outerwear", "down_jacket"}


_EXPLICIT_PRODUCT_TERMS = {
    "top": (r"上衣",), "sweatshirt": (r"卫衣",),
    "tshirt": (r"[TtＴｔ]恤",), "shirt": (r"衬衫",),
    "knitwear": (r"针织衫|毛衣|羊毛衫|羊绒衫|开衫",),
    "pants": (r"裤子|牛仔裤|阔腿裤|西裤|短裤|长裤|裤脚|裤长",),
    "skirt": (r"半身裙|短裙|长裙",), "dress": (r"连衣裙",),
    "set": (r"套装|两件套|三件套",),
}


def foreign_product_ranges(main_type: str, subtitles: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Find sustained explicit foreign-product talk, never score semantics.

    A lone styling mention is not a switch. Two explicit mentions close in the
    transcript, or a strong ownership/sales phrase, form a bounded exclusion
    range. This is a safety cross-check for AI-declared source sections.
    """
    if main_type == "set":
        return []
    anchors: list[tuple[int, str]] = []
    for row in subtitles:
        try:
            sid = int(row.get("id") or 0)
        except (TypeError, ValueError):
            continue
        text = str(row.get("text") or "")
        for kind, patterns in _EXPLICIT_PRODUCT_TERMS.items():
            if compatible(main_type, kind):
                continue
            if any(re.search(pattern, text) for pattern in patterns):
                anchors.append((sid, kind))
    ranges: list[dict[str, Any]] = []
    for kind in {kind for _, kind in anchors}:
        ids = [sid for sid, actual in anchors if actual == kind]
        clusters: list[list[int]] = []
        for sid in ids:
            if clusters and sid - clusters[-1][-1] <= 20:
                clusters[-1].append(sid)
            else:
                clusters.append([sid])
        for cluster in clusters:
            strong = any(re.search(r"我(?:特别)?(?:喜欢)?我?这(?:条|件|款)|主推|强推", str(next((r.get("text") for r in subtitles if int(r.get("id") or 0) == sid), ""))) for sid in cluster)
            if len(cluster) < 2 and not strong:
                continue
            ranges.append({"start_id": cluster[0], "end_id": cluster[-1],
                           "product_type": kind, "anchor_ids": cluster})
    return sorted(ranges, key=lambda item: item["start_id"])


def build_product_target(controls: Mapping[str, Any] | None) -> dict[str, Any]:
    controls = controls or {}
    main, leaf = clean(controls.get("main_product")), clean(controls.get("leaf_category"))
    main_types, leaf_types = named_types(main), named_types(leaf)
    # An explicit SKU/name wins over stale category preferences. A broad word
    # such as 衣服 is NOT silently translated into trousers or into a shirt.
    kinds = main_types or leaf_types
    if "set" in main_types:
        kinds = {"set"}
    specific = bool(main and main not in BROAD)
    # Source titles describe the user's asset, not semantic sentence content.
    # Only a single unambiguous type shared by all supplied titles may bind
    # automatic mode. Explicit user settings always win; ambiguous titles do not.
    hints = [named_types(hint) for hint in controls.get("source_product_hints") or [] if clean(hint)]
    title_type = next(iter(hints[0])) if hints and all(len(h) == 1 and h == hints[0] for h in hints) else ""
    use_title = bool(title_type and not main and not leaf)
    if use_title:
        kinds = {title_type}
    return {
        "version": VERSION, "mode": "locked" if specific or leaf or use_title else "auto",
        "main_product": main, "leaf_category": leaf,
        "product_type": next(iter(kinds)) if len(kinds) == 1 else "unknown",
        "identity_source": "main_product" if specific else "leaf_category" if leaf else "source_title" if use_title else "transcript",
        "needs_specific_category": bool(main in BROAD and not leaf),
        "sales_scope": "explicit_set" if kinds == {"set"} else "single_product" if specific or leaf or use_title else "auto_single_product",
        "supporting_products": "block" if controls.get("supporting_products") == "block" else "allow",
    }


def scope_errors(scope: Any, target: Mapping[str, Any]) -> list[str]:
    scope = dict(scope or {}) if isinstance(scope, Mapping) else {}
    errors = []
    product_type = str(scope.get("product_type") or "unknown")
    if not clean(scope.get("main_product")) or product_type not in PRODUCT_TYPES or product_type == "unknown":
        errors.append("主商品身份没有核实")
    expected = target.get("product_type", "unknown")
    if expected != "unknown" and not compatible(expected, product_type):
        errors.append(f"指定品类 {expected} 与导演识别 {product_type} 冲突")
    claimed_types = named_types(scope.get("main_product")) - {"unknown", "other"}
    if claimed_types and product_type != "set" and not any(compatible(t, product_type) for t in claimed_types):
        errors.append("主商品名称与声明品类矛盾")
    if target.get("mode") == "locked" and scope.get("target_confirmation") != "match":
        errors.append("尚未确认用户指定的主商品，不能自动换商品")
    if target.get("sales_scope") == "single_product" and scope.get("sales_scope") == "explicit_set":
        errors.append("单品不能自动升级为套装")
    if not scope.get("identity_evidence_ids"):
        errors.append("缺少主商品的原字幕身份证据")
    sections = scope.get("source_product_sections")
    if not isinstance(sections, list) or not sections:
        errors.append("未标记原片换品范围")
    else:
        for section in sections:
            if not isinstance(section, Mapping):
                errors.append("原片换品范围格式错误")
                continue
            try:
                valid_range = int(section.get("start_id")) > 0 and int(section.get("end_id")) >= int(section.get("start_id"))
            except (TypeError, ValueError):
                valid_range = False
            if not valid_range or str(section.get("product_type") or "unknown") not in PRODUCT_TYPES:
                errors.append("原片换品范围缺少有效ID或商品类型")
    return errors


def audit_product_selection(story: Mapping[str, Any], cast: Mapping[str, Any], *,
                            target: Mapping[str, Any], subtitles: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    scope = dict(story.get("product_scope") or {})
    errors = scope_errors(scope, target)
    source_ids = {int(r["id"]) for r in subtitles if str(r.get("id", "")).isdigit()}
    source_text = {int(r["id"]): str(r.get("text") or "") for r in subtitles if str(r.get("id", "")).isdigit()}
    def evidence_ok(value: Any) -> bool:
        return isinstance(value, list) and bool(value) and all(isinstance(i, int) and i in source_ids for i in value)
    if not evidence_ok(scope.get("identity_evidence_ids")):
        errors.append("主商品身份证据未指向真实字幕")
    main_type = str(scope.get("product_type") or "unknown")
    foreign_ranges = foreign_product_ranges(main_type, subtitles)
    sections = [dict(s) for s in scope.get("source_product_sections") or [] if isinstance(s, Mapping)]
    source_span_issues = []
    for sid in source_ids:
        owners = [section for section in sections if int(section.get("start_id") or 0) <= sid <= int(section.get("end_id") or 0)]
        if len(owners) > 1:
            source_span_issues.append(f"字幕 {sid} 同时落入多个商品范围")
    errors.extend(source_span_issues)
    checks = []
    for chapter in cast.get("chapter_packets") or []:
        if not isinstance(chapter, Mapping):
            continue
        for group in ("beats", "alternative_beats"):
            for beat in chapter.get(group) or []:
                if not isinstance(beat, Mapping):
                    continue
                relation = beat.get("product_relation")
                subject_type = str(beat.get("subject_product_type") or "unknown")
                issues = []
                if relation not in {"main_product", "styling_support"}:
                    issues.append("其他商品或归属不明")
                if not clean(beat.get("subject_product")) or subject_type in {"", "unknown"} or subject_type not in PRODUCT_TYPES:
                    issues.append("缺少明确讲述对象")
                named = named_types(beat.get("subject_product")) - {"other", "unknown"}
                # The model sometimes writes a descriptive subject such as
                # “大帽子与外套搭配” while correctly declaring the selected
                # sentence as a sweatshirt.  A foreign noun inside that
                # description is not enough to overrule a compatible explicit
                # subject type and valid evidence receipt.
                if (
                    named and subject_type != "set"
                    and not any(compatible(t, subject_type) for t in named)
                    and not (relation == "main_product" and compatible(main_type, subject_type))
                ):
                    issues.append("讲述对象名称与声明品类矛盾")
                if relation == "main_product" and not compatible(main_type, subject_type):
                    # An explicitly sold set may legitimately describe one component.
                    if main_type != "set" or not beat.get("set_component"):
                        issues.append("将其他单品的效果归给主商品")
                if relation == "styling_support" and not clean(beat.get("supports_main_product")):
                    issues.append("搭配句未说明如何服务主商品")
                if relation == "styling_support" and target.get("supporting_products") == "block":
                    issues.append("本次已排除其他商品搭配")
                if not evidence_ok(beat.get("product_evidence_ids")):
                    issues.append("缺少可回查的商品指代依据")
                beat_ids = [int(i) for i in beat.get("subtitle_ids") or [] if isinstance(i, int)]
                beat_owners = []
                for sid in beat_ids:
                    owners = [section for section in sections if int(section.get("start_id") or 0) <= sid <= int(section.get("end_id") or 0)]
                    beat_owners.extend(owners)
                    if len(owners) != 1:
                        issues.append("选句没有唯一的原片商品范围")
                        continue
                    owner_type = str(owners[0].get("product_type") or "unknown")
                    # A main-product or styling-support sentence must occur
                    # while the source is actually discussing the main item.
                    # Cross-product benefits remain unusable even if relabelled.
                    section_allowed = compatible(main_type, owner_type) or (
                        main_type == "set" and bool(beat.get("set_component")) and owner_type not in {"unknown", "other"}
                    )
                    if not section_allowed:
                        issues.append("选句位于其他商品的讲解范围")
                    explicit_main_in_same_sentence = any(
                        re.search(pattern, source_text.get(sid, ""))
                        for kind, patterns in _EXPLICIT_PRODUCT_TERMS.items()
                        if compatible(main_type, kind) for pattern in patterns
                    )
                    if main_type != "set" and not (relation == "styling_support" and explicit_main_in_same_sentence) and any(int(item["start_id"]) <= sid <= int(item["end_id"]) for item in foreign_ranges):
                        issues.append("选句邻近字幕持续明确讲其他商品")
                checks.append({"subtitle_ids": list(beat.get("subtitle_ids") or []), "group": group,
                               "product_relation": relation, "subject_product": beat.get("subject_product"),
                               "subject_product_type": subject_type, "source_section": beat_owners[0] if len(beat_ids) == 1 and len(beat_owners) == 1 else {},
                               "issues": list(dict.fromkeys(issues))})
    conflicts = [item for item in checks if item["issues"]]
    selected_conflicts = [item for item in conflicts if item["group"] == "beats"]
    return {"version": VERSION, "target": dict(target), "resolved_scope": scope,
            "scope_errors": list(dict.fromkeys(errors)), "beats": checks,
            "conflicting_subtitle_ids": [i for b in selected_conflicts for i in b["subtitle_ids"]],
            "alternative_conflicting_subtitle_ids": [i for b in conflicts if b["group"] == "alternative_beats" for i in b["subtitle_ids"]],
            "foreign_product_ranges": foreign_ranges,
            "status": "conflict" if errors or selected_conflicts else "consistent",
            "semantic_selection_owner": "AI", "program_deleted_beats": False}
