# -*- coding: utf-8 -*-
"""AI candidate content review used before the final clip director."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import ssl
from ssl_context import create_ssl_context
import tempfile
import time
from typing import Any, Iterable, Mapping, Sequence
import urllib.request

from ai_model_config import ai_chat_completions_url


CONTENT_REVIEW_VERSION = "content-review-v12"
CONTENT_REVIEW_ENV = "LIVECLIPPER_CONTENT_REVIEW_MODE"
CONTENT_REVIEW_DEFAULT_MODE = "off"
CONTENT_REVIEW_MAX_CARDS = 80
CONTENT_REVIEW_TARGET_DURATION = 180.0
CONTENT_REVIEW_MIN_DURATION = 60.0
CONTENT_REVIEW_CACHE_DAYS = 60
CONTENT_REVIEW_CACHE_MAX_FILES = 128
CONTENT_REVIEW_CACHE_MAX_BYTES = 50 * 1024 * 1024

_VALID_TIERS = {"main", "reserve"}
_VALID_DEPENDENCIES = {"independent", "needs_previous", "needs_next", "needs_both"}
_VALID_ROLES = {"effect", "evidence", "scene", "objection", "product"}
_TOPIC_FAMILY_RULES = (
    ("\u53e3\u611f\u98df\u6b32", ("\u53e3\u611f", "\u5473\u9053", "\u9999\u5473", "\u98df\u6b32", "\u597d\u5403", "\u8106", "\u751c", "\u9c9c")),
    ("\u65b0\u9c9c\u54c1\u8d28", ("\u65b0\u9c9c", "\u73b0\u6458", "\u73b0\u505a", "\u54c1\u8d28", "\u4fdd\u8d28")),
    ("\u4ea7\u5730\u6eaf\u6e90", ("\u4ea7\u5730", "\u6eaf\u6e90", "\u679c\u56ed", "\u57fa\u5730")),
    ("\u89c4\u683c\u5206\u91cf", ("\u89c4\u683c", "\u5206\u91cf", "\u51c0\u91cd", "\u514b\u91cd", "\u5927\u5c0f", "\u4efd\u91cf")),
    ("\u53d1\u8d27\u4fdd\u9c9c", ("\u53d1\u8d27", "\u7269\u6d41", "\u4fdd\u9c9c", "\u5305\u88c5", "\u51b7\u94fe")),
    ("\u573a\u666f\u5403\u6cd5", ("\u5403\u6cd5", "\u65e9\u9910", "\u4e0b\u5348\u8336", "\u96f6\u98df", "\u70f9\u996a")),
    ("\u989c\u8272\u6c1b\u56f4", ("\u989c\u8272", "\u663e\u767d", "\u80a4\u8272", "\u8272\u5f69", "\u4eae\u8272", "\u9971\u548c", "\u8272\u8c03")),
    ("\u7248\u578b\u663e\u7626", ("\u7248\u578b", "\u663e\u7626", "\u906e\u8089", "\u4fee\u9970", "\u6536\u8170", "\u817f\u578b", "\u80a9\u578b")),
    ("\u9762\u6599\u8d28\u611f", ("\u9762\u6599", "\u6750\u8d28", "\u6210\u5206", "\u83b1\u8d5b\u5c14", "\u4e9a\u9ebb", "\u5168\u68c9", "\u624b\u611f", "\u4eb2\u80a4", "\u900f\u6c14", "\u5782\u5760", "\u6297\u76b1", "\u6c34\u6d17")),
    ("\u5c3a\u5bf8\u957f\u5ea6", ("\u5c3a\u7801", "\u5c3a\u5bf8", "\u957f\u5ea6", "\u8eab\u9ad8", "\u4f53\u91cd")),
    ("\u7a7f\u7740\u4f53\u9a8c", ("\u7a7f\u7740", "\u8212\u9002", "\u51c9\u5feb", "\u4e0d\u624e", "\u4e0d\u95f7", "\u5f39\u529b")),
    ("\u5de5\u827a\u7ec6\u8282", ("\u5de5\u827a", "\u505a\u5de5", "\u8d70\u7ebf", "\u7ebd\u6263", "\u62fc\u63a5", "\u7ec6\u8282", "\u8bbe\u8ba1")),
    ("\u573a\u666f\u642d\u914d", ("\u573a\u666f", "\u642d\u914d", "\u901a\u52e4", "\u7ea6\u4f1a", "\u51fa\u95e8", "\u804c\u573a", "\u5ea6\u5047", "\u4eba\u7fa4")),
    ("\u5bf9\u6bd4\u4f18\u52bf", ("\u5bf9\u6bd4", "\u4f18\u52bf", "\u4e0d\u540c", "\u72ec\u5bb6", "\u666e\u901a\u6b3e")),
    ("\u6d41\u884c\u8d8b\u52bf", ("\u6d41\u884c", "\u8d8b\u52bf", "\u65f6\u9ae6", "\u590d\u53e4", "\u98ce\u683c")),
    ("\u6027\u4ef7\u6bd4", ("\u6027\u4ef7\u6bd4", "\u5212\u7b97", "\u503c\u5f97")),
    ("\u60c5\u7eea\u611f\u67d3", ("\u60ca\u8273", "\u559c\u6b22", "\u597d\u770b", "\u6f02\u4eae", "\u5e05", "\u60c5\u7eea")),
    ("\u7d27\u8feb\u7a00\u7f3a", ("\u7a00\u7f3a", "\u9650\u91cf", "\u65ad\u7801", "\u5e93\u5b58")),
)


class ContentReviewError(RuntimeError):
    pass


def normalize_review_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "": CONTENT_REVIEW_DEFAULT_MODE,
        "false": "off",
        "0": "off",
        "disabled": "off",
        "true": "on",
        "1": "on",
        "enabled": "on",
    }
    text = aliases.get(text, text)
    return text if text in {"off", "shadow", "on"} else CONTENT_REVIEW_DEFAULT_MODE


def resolve_review_mode(settings: Mapping[str, Any] | None = None) -> str:
    env_value = os.environ.get(CONTENT_REVIEW_ENV)
    if env_value is not None:
        return normalize_review_mode(env_value)
    return normalize_review_mode((settings or {}).get("content_review_mode"))


def _clean_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _clean_list(value: Any, *, limit: int, item_limit: int) -> tuple[str, ...]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = _clean_text(raw, item_limit)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
        if len(result) >= limit:
            break
    return tuple(result)

def _normalize_topic(value: Any) -> str:
    topic = _clean_text(value, 40) or "\u5176\u4ed6"
    if topic in {"\u5176\u4ed6", "\u901a\u7528\u5356\u70b9"}:
        return topic
    for family, keywords in _TOPIC_FAMILY_RULES:
        if topic == family or any(keyword in topic for keyword in keywords):
            return family
    return topic


@dataclass(frozen=True)
class ContentCard:
    candidate_id: int
    topic: str
    subtopic: str
    buyer_value: str
    evidence_type: str
    evidence_quote: str
    roles: tuple[str, ...]
    dependency: str
    quality_tags: tuple[str, ...]
    tier: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "topic": self.topic,
            "subtopic": self.subtopic,
            "buyer_value": self.buyer_value,
            "evidence_type": self.evidence_type,
            "evidence_quote": self.evidence_quote,
            "roles": list(self.roles),
            "dependency": self.dependency,
            "quality_tags": list(self.quality_tags),
            "tier": self.tier,
        }


@dataclass(frozen=True)
class HookPair:
    hook_id: int
    followup_id: int
    topic: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "followup_id": self.followup_id,
            "topic": self.topic,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ContentReviewBundle:
    cache_key: str
    candidate_digest: str
    category: str
    model: str
    cards: tuple[ContentCard, ...]
    retained_duration: float
    hook_pairs: tuple[HookPair, ...] = ()
    cache_hit: bool = False
    version: str = CONTENT_REVIEW_VERSION

    @property
    def allowed_candidate_ids(self) -> set[int]:
        return {card.candidate_id for card in self.cards}

    @property
    def hook_candidate_ids(self) -> set[int]:
        return {pair.hook_id for pair in self.hook_pairs}

    def card_map(self) -> dict[int, ContentCard]:
        return {card.candidate_id: card for card in self.cards}

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "cache_key": self.cache_key,
            "candidate_digest": self.candidate_digest,
            "category": self.category,
            "model": self.model,
            "retained_duration": round(self.retained_duration, 3),
            "cards": [card.to_dict() for card in self.cards],
        }

    def summary(self, mode: str, fallback_reason: str = "") -> dict[str, Any]:
        return {
            "mode": normalize_review_mode(mode),
            "version": self.version,
            "cache_hit": bool(self.cache_hit),
            "card_count": len(self.cards),
            "main_count": sum(1 for card in self.cards if card.tier == "main"),
            "reserve_count": sum(1 for card in self.cards if card.tier == "reserve"),
            "retained_duration": round(self.retained_duration, 1),
            "grounded_card_count": len(self.cards),
            "fallback_reason": str(fallback_reason or ""),
        }

    def director_hint(self) -> str:
        lines = [
            "\n\u2605AI\u5185\u5bb9\u5ba1\u7a3f\u5df2\u5b8c\u6210\u2605 \u4e0b\u5217\u7f16\u53f7\u662f\u7ecf\u8fc7\u540c\u4e3b\u9898\u6bd4\u8f83\u540e\u4fdd\u7559\u7684\u4e3b\u9009/\u5907\u7528\u5185\u5bb9\u3002",
            "\u5fc5\u987b\u4fdd\u6301\u539f\u53e5\u548c\u7f16\u53f7\uff1bmain\u4f18\u5148\uff0creserve\u4ec5\u7528\u4e8e\u8865\u8db3\u4e0d\u540c\u5356\u70b9\u6216\u65f6\u957f\u3002",
            "\u5185\u5bb9\u5361\u53ea\u8bf4\u660e\u5356\u70b9\u4ef7\u503c\u548c\u4e0a\u4e0b\u6587\u4f9d\u8d56\uff0c\u4e0d\u6307\u5b9aHook\u6216Close\u3002\u4f60\u5fc5\u987b\u7ed3\u5408\u5168\u7247\u53d9\u4e8b\u72ec\u7acb\u51b3\u5b9a\u5f00\u5934\u548c\u6536\u5c3e\u3002",
        ]
        for card in self.cards:
            role_text = "/".join(card.roles) or "product"
            tag_text = "/".join(card.quality_tags)
            lines.append(
                f"- #{card.candidate_id:02d} [{card.tier}] {card.topic}/{card.subtopic}; "
                f"\u4ef7\u503c:{card.buyer_value}; \u8bc1\u636e:{card.evidence_type or '\u65e0'}; "
                f"\u539f\u6587\u8bc1\u636e:\"{card.evidence_quote}\"; "
                f"\u89d2\u8272:{role_text}; \u4f9d\u8d56:{card.dependency}"
                + (f"; \u6807\u7b7e:{tag_text}" if tag_text else "")
            )

        return "\n".join(lines) + "\n"

    def topic_support(self, inventory: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
        inventory_map = {int(item.get("srt_index") or 0): item for item in inventory}
        result: dict[str, dict[str, float]] = {}
        for card in self.cards:
            topic = card.topic.strip()
            if not topic or topic in {"\u5176\u4ed6", "\u901a\u7528\u5356\u70b9"}:
                continue
            item = result.setdefault(topic, {"main": 0.0, "reserve": 0.0, "evidence": 0.0, "duration": 0.0})
            item[card.tier] += 1.0
            if card.evidence_type and card.evidence_type not in {"\u65e0", "\u6cdb\u6cdb\u5938\u8d5e"}:
                item["evidence"] += 1.0
            item["duration"] += float(inventory_map.get(card.candidate_id, {}).get("duration_sec") or 0.0)
        return result


def _user_data_dir() -> Path:
    try:
        from config import USER_DATA_DIR
        return Path(USER_DATA_DIR)
    except Exception:
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "LiveClipper"


def content_review_cache_dir() -> Path:
    return _user_data_dir() / "cache" / "ai_content_review"


def build_cache_key(
    candidate_digest: str,
    category: str,
    main_product: str,
    avoid: Iterable[str],
    model: str,
) -> str:
    payload = {
        "version": CONTENT_REVIEW_VERSION,
        "candidate_digest": str(candidate_digest or ""),
        "category": _clean_text(category, 80),
        "main_product": _clean_text(main_product, 100),
        "avoid": sorted(_clean_list(list(avoid or []), limit=30, item_limit=80)),
        "model": _clean_text(model, 120),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_path(cache_key: str) -> Path:
    return content_review_cache_dir() / f"{cache_key}.json"


def _load_cache(cache_key: str) -> dict[str, Any] | None:
    path = _cache_path(cache_key)
    try:
        if not path.is_file():
            return None
        max_age = CONTENT_REVIEW_CACHE_DAYS * 86400
        if time.time() - path.stat().st_mtime > max_age:
            path.unlink(missing_ok=True)
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
                        _LOG.warning("os error", exc_info=True)
        return None


def _cleanup_cache() -> None:
    root = content_review_cache_dir()
    if not root.is_dir():
        return
    now = time.time()
    max_age = CONTENT_REVIEW_CACHE_DAYS * 86400
    files: list[tuple[float, int, Path]] = []
    for path in root.glob("*.json"):
        try:
            stat = path.stat()
            if now - stat.st_mtime > max_age:
                path.unlink(missing_ok=True)
                continue
            files.append((stat.st_mtime, stat.st_size, path))
        except OSError:
            continue
    files.sort(reverse=True)
    kept_bytes = 0
    for index, (_mtime, size, path) in enumerate(files):
        kept_bytes += size
        if index >= CONTENT_REVIEW_CACHE_MAX_FILES or kept_bytes > CONTENT_REVIEW_CACHE_MAX_BYTES:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                                _LOG.warning("os error", exc_info=True)


def _write_cache(bundle: ContentReviewBundle) -> None:
    root = content_review_cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    target = _cache_path(bundle.cache_key)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", delete=False, dir=str(root), suffix=".tmp"
        ) as handle:
            temp_name = handle.name
            json.dump(bundle.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        temp_name = ""
        _cleanup_cache()
    finally:
        if temp_name:
            try:
                os.remove(temp_name)
            except OSError:
                                _LOG.warning("os error", exc_info=True)


def _extract_json_object(content: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*", "", str(content or "").strip(), flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    match = re.search(r"\{[\s\S]*\}", cleaned)
    data = json.loads(match.group(0) if match else cleaned)
    if not isinstance(data, dict):
        raise ContentReviewError("\u5ba1\u7a3f\u54cd\u5e94\u4e0d\u662fJSON\u5bf9\u8c61")
    return data


def _candidate_map(inventory: Sequence[Mapping[str, Any]]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for raw in inventory:
        try:
            candidate_id = int(raw.get("srt_index") or 0)
        except (TypeError, ValueError):
            continue
        if candidate_id <= 0:
            continue
        result[candidate_id] = {
            "srt_index": candidate_id,
            "source": _clean_text(raw.get("source"), 24),
            "duration_sec": max(0.0, float(raw.get("duration_sec") or 0.0)),
            "text": _clean_text(raw.get("text"), 240),
        }
    return result

def _reviewable_candidate_text(text: Any) -> bool:
    cleaned = re.sub(r"^\s*\[V\d+\]\s*", "", str(text or ""), flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) < 6:
        return False
    prefix_match = re.match(r"^[\u4e00-\u9fffA-Za-z][,\uff0c\u3001;\uff1b:\uff1a]", cleaned)
    if prefix_match and prefix_match.group(0)[0] not in "\u54c7\u5450\u54e6\u5bf9\u662f\u8fd9\u90a3\u54ce":
        return False
    if re.search(r"(?:\u4f46\u662f|\u800c\u4e14|\u56e0\u4e3a|\u6240\u4ee5|\u7136\u540e)(?:\u6211|\u4f60|\u4ed6|\u5979|\u5b83)?[.\u3002!\uff01?\uff1f]$", cleaned):
        return False
    if re.search(r"([\u6211\u4f60\u4ed6\u5979\u5b83])\1{2,}", cleaned):
        return False
    if re.search(r"(\u522b\u5435|\u522b\u8bf4\u8bdd|\u5b89\u9759)[,\uff0c\s]*\1", cleaned):
        return False
    if re.search(r"(.{2,4})\1{2,}", cleaned):
        return False
    return True

def _reviewable_hook_text(text: Any) -> bool:
    if not _reviewable_candidate_text(text):
        return False
    cleaned = re.sub(r"^\s*\[V\d+\]\s*", "", str(text or ""), flags=re.I).strip()
    if re.search(
        r"(?:\u8fd9\u4e2a|\u90a3\u4e2a|\u5b83|\u8fd9\u4ef6)\s*(?:\u548c|\u8ddf|\u4e0e)\s*[\u4e00-\u9fffA-Za-z0-9]{1,8}[\u3002.!\uff01?\uff1f]?$",
        cleaned,
    ):
        return False
    weak_prefixes = (
        "\u7136\u540e", "\u800c\u4e14", "\u4f46\u662f", "\u56e0\u4e3a", "\u5c31\u662f",
        "\u5176\u5b9e", "\u597d\u4e86", "\u597d\u7684", "\u662f\u7684", "\u5bf9\uff0c", "\u4e0d\u642d\u8fb9",
    )
    return not cleaned.startswith(weak_prefixes)


def _grounded_evidence_quote(value: Any, candidate_text: Any) -> str:
    quote = _clean_text(value, 120)
    source = re.sub(r"^\s*\[V\d+\]\s*", "", str(candidate_text or ""), flags=re.I).strip()
    quote_chars = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", quote).lower()
    if len(quote_chars) < 2:
        return ""

    source_chars: list[str] = []
    source_positions: list[int] = []
    for position, character in enumerate(source):
        if re.match(r"[0-9A-Za-z\u4e00-\u9fff]", character):
            source_chars.append(character.lower())
            source_positions.append(position)
    match_at = "".join(source_chars).find(quote_chars)
    if match_at < 0:
        return ""

    start = source_positions[match_at]
    end = source_positions[match_at + len(quote_chars) - 1] + 1
    return source[start:end].strip()


def _card_rejection_reason(
    raw: Any,
    allowed_ids: set[int],
    candidates: Mapping[int, Mapping[str, Any]],
) -> str:
    if isinstance(raw, (list, tuple)):
        if len(raw) >= 10:
            raw = {
                "candidate_id": raw[0],
                "evidence_quote": raw[5],
                "tier": raw[9],
            }
        elif len(raw) >= 9:
            raw = {
                "candidate_id": raw[0],
                "tier": raw[8],
            }
        else:
            return "schema"
    if not isinstance(raw, Mapping):
        return "schema"
    try:
        candidate_id = int(raw.get("candidate_id") or raw.get("srt_index") or 0)
    except (TypeError, ValueError):
        return "candidate_id"
    if candidate_id not in allowed_ids:
        return "candidate_id"
    if _clean_text(raw.get("tier"), 16).lower() not in _VALID_TIERS:
        return "tier"
    return "semantic"


def _topic_from_candidate_text(value: Any) -> str:
    text = _clean_text(value, 240)
    best_family = "\u5176\u4ed6"
    best_score = 0
    for family, keywords in _TOPIC_FAMILY_RULES:
        score = sum(len(keyword) for keyword in keywords if keyword in text)
        if score > best_score:
            best_family = family
            best_score = score
    return best_family


def _normalize_card(
    raw: Any,
    allowed_ids: set[int],
    candidates: Mapping[int, Mapping[str, Any]],
) -> ContentCard | None:
    if isinstance(raw, (list, tuple)):
        if len(raw) >= 10:
            raw = {
                "candidate_id": raw[0],
                "topic": raw[1],
                "subtopic": raw[2],
                "buyer_value": raw[3],
                "evidence_type": raw[4],
                "evidence_quote": raw[5],
                "roles": raw[6],
                "dependency": raw[7],
                "quality_tags": raw[8],
                "tier": raw[9],
            }
        elif len(raw) >= 9:
            raw = {
                "candidate_id": raw[0],
                "topic": raw[1],
                "subtopic": raw[2],
                "buyer_value": raw[3],
                "evidence_type": raw[4],
                "roles": raw[5],
                "dependency": raw[6],
                "quality_tags": raw[7],
                "tier": raw[8],
            }
        else:
            return None
    if not isinstance(raw, Mapping):
        return None
    try:
        candidate_id = int(raw.get("candidate_id") or raw.get("srt_index") or 0)
    except (TypeError, ValueError):
        return None
    if candidate_id not in allowed_ids:
        return None
    tier = _clean_text(raw.get("tier"), 16).lower()
    if tier not in _VALID_TIERS:
        return None
    dependency = _clean_text(raw.get("dependency"), 24).lower()
    if dependency not in _VALID_DEPENDENCIES:
        dependency = "independent"

    candidate_text = re.sub(
        r"^\s*\[V\d+\]\s*", "", str(candidates.get(candidate_id, {}).get("text") or ""), flags=re.I
    ).strip()
    evidence_quote = _grounded_evidence_quote(raw.get("evidence_quote"), candidate_text)
    evidence_bound = not bool(evidence_quote)
    if evidence_bound:
        evidence_quote = _clean_text(candidate_text, 120)
    topic = _normalize_topic(raw.get("topic"))
    subtopic = _clean_text(raw.get("subtopic"), 60) or topic
    buyer_value = _clean_text(raw.get("buyer_value"), 100) or "\u8865\u5145\u5546\u54c1\u4fe1\u606f"
    evidence_type = _clean_text(raw.get("evidence_type"), 40) or "\u539f\u6587\u7ed1\u5b9a"
    roles = tuple(
        role for role in _clean_list(raw.get("roles"), limit=4, item_limit=24)
        if role in _VALID_ROLES
    ) or ("product",)
    quality_tags = _clean_list(raw.get("quality_tags"), limit=4, item_limit=30)
    if evidence_bound and "\u539f\u6587\u7ed1\u5b9a" not in quality_tags:
        quality_tags = tuple((*quality_tags, "\u539f\u6587\u7ed1\u5b9a")[:4])
    return ContentCard(
        candidate_id=candidate_id,
        topic=topic,
        subtopic=subtopic,
        buyer_value=buyer_value,
        evidence_type=evidence_type,
        evidence_quote=evidence_quote,
        roles=roles,
        dependency=dependency,
        quality_tags=quality_tags,
        tier=tier,
    )

def _fallback_card(candidate_id: int, candidate_text: Any) -> ContentCard:
    evidence_quote = re.sub(r"^\s*\[V\d+\]\s*", "", str(candidate_text or ""), flags=re.I)
    evidence_quote = _clean_text(evidence_quote, 120)
    return ContentCard(
        candidate_id=candidate_id,
        topic="\u5176\u4ed6",
        subtopic="\u672a\u5206\u7ea7\u5b89\u5168\u5019\u9009",
        buyer_value="\u5b89\u5168\u5e93\u5b58\u4fdd\u7559",
        evidence_type="",
        evidence_quote=evidence_quote,
        roles=("product",),
        dependency="independent",
        quality_tags=("\u7d20\u6750\u4e0d\u8db3\u4fdd\u7559",),
        tier="reserve",
    )

def _rebalance_card_tiers(
    cards: Sequence[ContentCard], candidates: Mapping[int, Mapping[str, Any]]
) -> list[ContentCard]:
    sources = {
        str(candidates.get(card.candidate_id, {}).get("source") or "").strip().upper()
        for card in cards
        if str(candidates.get(card.candidate_id, {}).get("source") or "").strip()
    }
    mixed = len(sources) > 1
    topic_main_counts: dict[str, int] = {}
    source_topic_main_counts: dict[tuple[str, str], int] = {}
    balanced: list[ContentCard] = []
    for card in cards:
        if card.tier != "main":
            balanced.append(card)
            continue
        source = str(candidates.get(card.candidate_id, {}).get("source") or "").strip().upper()
        topic_key = card.topic or "\u5176\u4ed6"
        source_topic_key = (source, topic_key)
        if topic_main_counts.get(topic_key, 0) >= 4 or (
            mixed and source_topic_main_counts.get(source_topic_key, 0) >= 2
        ):
            balanced.append(replace(card, tier="reserve"))
            continue
        topic_main_counts[topic_key] = topic_main_counts.get(topic_key, 0) + 1
        source_topic_main_counts[source_topic_key] = source_topic_main_counts.get(source_topic_key, 0) + 1
        balanced.append(card)
    return balanced

def _validate_bundle(
    data: Mapping[str, Any],
    *,
    inventory: Sequence[Mapping[str, Any]],
    cache_key: str,
    candidate_digest: str,
    category: str,
    model: str,
    required_sources: Mapping[str, int] | None,
) -> ContentReviewBundle:
    candidates = _candidate_map(inventory)
    allowed_ids = {
        candidate_id for candidate_id, item in candidates.items()
        if _reviewable_candidate_text(item.get("text"))
    }
    if not allowed_ids:
        raise ContentReviewError("\u6ca1\u6709\u5b89\u5168\u5019\u9009")

    cards: list[ContentCard] = []
    rejection_counts: dict[str, int] = {}
    seen_ids: set[int] = set()
    signature_counts: dict[tuple[str, str, str], int] = {}
    raw_cards = data.get("cards") if isinstance(data.get("cards"), list) else []
    for raw in raw_cards:
        card = _normalize_card(raw, allowed_ids, candidates)
        if card is None:
            reason = _card_rejection_reason(raw, allowed_ids, candidates)
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            continue
        if card.candidate_id in seen_ids:
            rejection_counts["duplicate"] = rejection_counts.get("duplicate", 0) + 1
            continue
        signature = (card.topic, card.subtopic, card.buyer_value)
        if signature_counts.get(signature, 0) >= 3:
            continue
        signature_counts[signature] = signature_counts.get(signature, 0) + 1
        seen_ids.add(card.candidate_id)
        cards.append(card)
        if len(cards) >= CONTENT_REVIEW_MAX_CARDS:
            break

    safe_duration = sum(candidates[candidate_id]["duration_sec"] for candidate_id in allowed_ids)
    if safe_duration <= CONTENT_REVIEW_TARGET_DURATION:
        for candidate_id in sorted(allowed_ids - seen_ids):
            if len(cards) >= CONTENT_REVIEW_MAX_CARDS:
                break
            cards.append(_fallback_card(candidate_id, candidates[candidate_id].get("text")))
            seen_ids.add(candidate_id)

    if not cards:
        rejection_text = ",".join(
            f"{reason}={count}" for reason, count in sorted(rejection_counts.items())
        ) or "none"
        top_keys = ",".join(sorted(str(key) for key in data.keys()))[:120] or "none"
        raise ContentReviewError(
            f"\u5ba1\u7a3f\u672a\u8fd4\u56de\u53ef\u7528\u5185\u5bb9\u5361"
            f"\uff08cards={len(raw_cards)}, reject={rejection_text}, keys={top_keys}\uff09"
        )
    cards = _rebalance_card_tiers(cards, candidates)
    retained_duration = sum(candidates[card.candidate_id]["duration_sec"] for card in cards)
    required_duration = min(safe_duration, CONTENT_REVIEW_MIN_DURATION)
    if retained_duration + 0.1 < required_duration:
        raise ContentReviewError(
            f"\u5ba1\u7a3f\u4fdd\u7559\u65f6\u957f\u4e0d\u8db3 {retained_duration:.1f}s/{required_duration:.1f}s"
            f"\uff08\u901a\u8fc7{len(cards)}/\u539f\u59cb{len(raw_cards)}\u5f20\uff09"
        )

    requirements = {
        str(source or "").strip().upper(): max(1, int(count or 1))
        for source, count in (required_sources or {}).items()
        if str(source or "").strip()
    }
    if requirements:
        kept_by_source: dict[str, int] = {}
        for card in cards:
            source = str(candidates[card.candidate_id].get("source") or "").strip().upper()
            if source:
                kept_by_source[source] = kept_by_source.get(source, 0) + 1
        missing = [source for source, minimum in requirements.items() if kept_by_source.get(source, 0) < minimum]
        if missing:
            raise ContentReviewError("\u5ba1\u7a3f\u5019\u9009\u7f3a\u5c11\u6df7\u526a\u6765\u6e90:" + ",".join(missing))

    return ContentReviewBundle(
        cache_key=cache_key,
        candidate_digest=str(candidate_digest or ""),
        category=_clean_text(category, 80),
        model=_clean_text(model, 120),
        cards=tuple(cards),
        retained_duration=retained_duration,
    )


def _review_prompts(
    inventory: Sequence[Mapping[str, Any]],
    *,
    category: str,
    main_product: str,
    avoid: Sequence[str],
    required_sources: Mapping[str, int] | None,
    format_retry: bool,
) -> tuple[str, str]:
    compact_inventory = [
        [
            int(item.get("srt_index") or 0),
            _clean_text(item.get("source"), 24),
            round(max(0.0, float(item.get("duration_sec") or 0.0)), 1),
            _clean_text(item.get("text"), 240),
        ]
        for item in inventory
    ]
    system_prompt = (
        "\u4f60\u662f\u5e26\u8d27\u77ed\u89c6\u9891\u7684\u5185\u5bb9\u5ba1\u7a3f\uff0c\u4e0d\u662f\u6700\u7ec8\u526a\u8f91\u5bfc\u6f14\u3002"
        "\u4f60\u53ea\u8d1f\u8d23\u8bc6\u522b\u503c\u5f97\u4f7f\u7528\u7684\u5177\u4f53\u5185\u5bb9\u3001\u4e3b\u9898\u3001\u8d2d\u4e70\u4ef7\u503c\u3001\u4e0a\u4e0b\u6587\u4f9d\u8d56\u548c\u8f6c\u5199\u98ce\u9669\u3002"
        "\u4e0d\u5f97\u6307\u5b9aHook\u3001\u627f\u63a5\u6bb5\u3001Close\u6216\u6210\u7247\u987a\u5e8f\uff0c\u4e0d\u5f97\u6539\u5199\u5b57\u5e55\u6216\u7f16\u9020\u7f16\u53f7\u3002"
        "\u6bcf\u5f20\u5361\u53ea\u9700\u9009\u62e9\u771f\u5b9e\u5019\u9009\u7f16\u53f7\u5e76\u5224\u65ad\u5185\u5bb9\u4ef7\u503c\uff1b\u7a0b\u5e8f\u4f1a\u7528\u7f16\u53f7\u7ed1\u5b9a\u539f\u5b57\u5e55\u4f5c\u4e3a\u552f\u4e00\u8bc1\u636e\u3002"
        "\u53ea\u8f93\u51fa\u5355\u884c\u7d27\u51d1JSON\u5bf9\u8c61\uff0c\u4e0d\u8981Markdown\u3001\u89e3\u91ca\u6216\u7f29\u8fdb\u3002"
    )
    source_rule = ""
    if required_sources:
        source_rule = "\u6df7\u526a\u6765\u6e90\u4e0d\u5f97\u9057\u6f0f:" + "\u3001".join(
            f"{source}\u81f3\u5c11{max(1, int(count or 1))}\u5f20\u5361"
            for source, count in sorted(required_sources.items())
        )
        source_rule += (
            "\u3002\u8d28\u91cf\u76f8\u5f53\u65f6\uff0ccards\u548cmain\u90fd\u8981\u4fdd\u7559\u5404\u6765\u6e90\u7684\u4ee3\u8868\u6027\u5185\u5bb9\uff1b"
            "\u4e0d\u5f97\u8ba9\u5355\u4e00\u6765\u6e90\u5360\u7edd\u5927\u591a\u6570\u3002"
        )
    retry_rule = "\u4e0a\u6b21\u54cd\u5e94\u683c\u5f0f\u65e0\u6548\uff0c\u8fd9\u6b21\u4e25\u683c\u6309schema\u8f93\u51fa\u3002" if format_retry else ""
    user_prompt = f"""{retry_rule}
\u54c1\u7c7b:{category or '\u901a\u7528'}
\u4e3b\u5546\u54c1:{main_product or '\u672a\u6307\u5b9a'}
\u7528\u6237\u8981\u6c42\u907f\u5f00:{'\u3001'.join(avoid) if avoid else '\u65e0'}
{source_rule}

\u8981\u6c42:
1. \u5ba1\u9605\u5168\u90e8\u5019\u9009\uff0c\u4f46\u53ea\u8f93\u51fa\u503c\u5f97\u4ea4\u7ed9\u5bfc\u6f14\u7684\u5361\u3002cards\u4e0a\u9650{CONTENT_REVIEW_MAX_CARDS}\u4e0d\u662f\u586b\u6ee1\u76ee\u6807\uff0c\u901a\u5e3825-55\u9879\uff1b\u5185\u5bb9\u5145\u8db3\u65f6main+reserve\u539f\u7247\u5408\u8ba1\u5c3d\u91cf\u8fbe\u5230{CONTENT_REVIEW_TARGET_DURATION:.0f}\u79d2\u3002\u8d28\u91cf\u6c38\u8fdc\u9ad8\u4e8e\u65f6\u957f\uff0c\u5b81\u53ef\u5c11\u4e8e\u76ee\u6807\u4e5f\u4e0d\u5f97\u7528\u6b8b\u53e5\u3001\u4e71\u7801\u3001\u95f2\u804a\u3001\u7eaf\u5c55\u793a\u94fa\u57ab\u6216\u91cd\u590d\u5185\u5bb9\u51d1\u79d2\u6570\u3002
2. \u540c\u4e00\u5177\u4f53\u5b50\u4e3b\u9898\u53ea\u75591\u4e2amain\u548c1-2\u4e2areserve\u3002\u5019\u9009\u8d28\u91cf\u7528\u5c3d\u5c31\u505c\uff0c\u4e0d\u8981\u7528\u91cd\u590d\u6a21\u677f\u51d1\u6570\u3002
3. topic\u7528\u7a33\u5b9a\u5356\u70b9\u7c7b\u522b\uff0csubtopic\u5199\u5177\u4f53\u95ee\u9898\uff1bbuyer_value\u53ea\u6982\u62ec\u8be5\u539f\u53e5\u5bf9\u8d2d\u4e70\u51b3\u7b56\u7684\u4ef7\u503c\u3002
4. roles\u53ea\u80fd\u4f7f\u7528effect,evidence,scene,objection,product\u3002\u8fd9\u4e9b\u53ea\u662f\u5185\u5bb9\u529f\u80fd\uff0c\u4e0d\u662f\u6210\u7247\u4f4d\u7f6e\u3002dependency\u53ea\u80fd\u4f7f\u7528independent,needs_previous,needs_next,needs_both\u3002
5. topic/subtopic/buyer_value\u5fc5\u987b\u53ea\u4f9d\u636e\u8be5\u7f16\u53f7\u539f\u5b57\u5e55\uff0c\u4e0d\u5f97\u628a\u201c\u5c55\u793a\u4e00\u4e0b\u201d\u81c6\u6d4b\u6210\u5ea6\u5047\u3001\u901a\u52e4\u6216\u5176\u4ed6\u672a\u8bf4\u51fa\u7684\u573a\u666f\uff1b\u4e0d\u5f97\u6539\u5199\u5b57\u5e55\uff0c\u7a0b\u5e8f\u4f1a\u6309\u7f16\u53f7\u7ed1\u5b9a\u539f\u6587\u3002
6. quality_tags\u7528\u7b80\u77ed\u6807\u7b7e\uff0c\u5982\u5177\u4f53\u6548\u679c\u3001\u539f\u56e0\u89e3\u91ca\u3001\u5b9e\u6d4b\u8bc1\u636e\u3001\u4eba\u7fa4\u660e\u786e\u3001\u573a\u666f\u6e05\u6670\u3001ASR\u98ce\u9669\u3002
7. \u660e\u663e\u4ece\u534a\u53e5\u5f00\u59cb\u6216\u7ed3\u5c3e\u672a\u5b8c\u3001\u6307\u4ee3\u4e0d\u660e\u3001ASR\u4e71\u7801\u3001\u8fde\u7eed\u7ed3\u5df4\u91cd\u590d\u3001\u7eaf\u4e92\u52a8\u6216\u7eaf\u94fa\u57ab\u7684\u5019\u9009\u4e0d\u5f97\u8fdb\u5165main/reserve\u3002\u4e0d\u8981\u628a\u6b8b\u53e5\u4ec5\u6807\u8bb0dependency\u540e\u7ee7\u7eed\u4fdd\u7559\u3002
8. tier\u5fc5\u987b\u771f\u5b9e\u5206\u5c42\uff1amain\u53ea\u7ed9\u540c\u7c7b\u4e2d\u6700\u5b8c\u6574\u3001\u6700\u5177\u4f53\u3001\u8bc1\u636e\u6700\u5f3a\u7684\u8868\u8fbe\uff0c\u5176\u4f59\u9ad8\u8d28\u91cf\u8868\u8fbe\u6807reserve\u3002\u5185\u5bb9\u5145\u8db3\u65f6main\u7ea6\u536035%-65%\uff0c\u7981\u6b62\u5168\u90e8\u6807main\uff1b\u540c\u4e00\u5927topic\u6700\u591a4\u5f20main\uff0c\u6df7\u526a\u65f6\u540c\u4e00\u6765\u6e90+\u540c\u4e00\u5927topic\u6700\u591a2\u5f20main\u3002
9. topic\u4f7f\u7528\u7a33\u5b9a\u5927\u7c7b\uff1a\u670d\u9970\u4f18\u5148\u7528\u7248\u578b\u663e\u7626/\u9762\u6599\u8d28\u611f/\u989c\u8272\u6c1b\u56f4/\u573a\u666f\u642d\u914d/\u5de5\u827a\u7ec6\u8282/\u5c3a\u5bf8\u957f\u5ea6/\u7a7f\u7740\u4f53\u9a8c/\u5bf9\u6bd4\u4f18\u52bf/\u6d41\u884c\u8d8b\u52bf\uff1b\u98df\u54c1\u4f18\u5148\u7528\u53e3\u611f\u98df\u6b32/\u65b0\u9c9c\u54c1\u8d28/\u4ea7\u5730\u6eaf\u6e90/\u89c4\u683c\u5206\u91cf/\u53d1\u8d27\u4fdd\u9c9c/\u573a\u666f\u5403\u6cd5\uff1b\u65b0\u54c1\u7c7b\u7528\u5bf9\u5e94\u7a33\u5b9a\u5927\u7c7b\u3002

\u8f93\u51faschema\uff08\u5fc5\u987b\u7528\u6570\u7ec4\u77ed\u683c\u5f0f\uff09:
{{"cards":[[1,"\u7248\u578b\u663e\u7626","\u80a9\u5bbd\u4fee\u9970","\u8bf4\u6e05\u80a9\u7ebf\u5982\u4f55\u5411\u5185\u6536","\u539f\u56e0\u89e3\u91ca",["effect","evidence"],"independent",["\u5177\u4f53\u6548\u679c"],"main"]]}}
cards\u5b57\u6bb5\u987a\u5e8f:[\u5019\u9009\u7f16\u53f7,topic,subtopic,buyer_value,evidence_type,roles,dependency,quality_tags,tier]

\u5b89\u5168\u5019\u9009\u5b57\u6bb5\u987a\u5e8f:[\u7f16\u53f7,\u6765\u6e90,\u65f6\u957f\u79d2,\u539f\u5b57\u5e55]
{json.dumps(compact_inventory, ensure_ascii=False, separators=(',', ':'))}"""
    return system_prompt, user_prompt

def _post_review_request(
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> str:
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "top_p": 0.8,
        "max_tokens": 6000,
        "response_format": {"type": "json_object"},
    }
    if "deepseek" in model.lower() and "seed" not in model.lower():
        body["thinking"] = {"type": "disabled"}
    if "seed" in model.lower():
        body["reasoning_effort"] = "low"
    request = urllib.request.Request(
        ai_chat_completions_url(base_url),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    context = create_ssl_context()
    with urllib.request.urlopen(request, timeout=180, context=context) as response:
        result = json.loads(response.read().decode("utf-8"))
    content = str(result.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
    if not content:
        raise ContentReviewError("\u5ba1\u7a3fAI\u8fd4\u56de\u7a7a\u5185\u5bb9")
    return content


def review_candidates(
    *,
    api_key: str,
    base_url: str,
    model: str,
    inventory: Sequence[Mapping[str, Any]],
    candidate_digest: str,
    category: str = "",
    main_product: str = "",
    avoid: Sequence[str] = (),
    required_sources: Mapping[str, int] | None = None,
    log_fn=None,
) -> ContentReviewBundle:
    def log(message: str) -> None:
        if log_fn:
            log_fn(message)

    review_inventory = tuple(
        item for item in inventory
        if isinstance(item, Mapping) and _reviewable_candidate_text(item.get("text"))
    )
    rejected_count = len(inventory) - len(review_inventory)
    if rejected_count:
        log(f"AI\u5185\u5bb9\u5ba1\u7a3f: \u5ba1\u7a3f\u524d\u6392\u9664 {rejected_count} \u6761\u660e\u663e\u8f6c\u5199\u6b8b\u7247")
    if not review_inventory:
        raise ContentReviewError("\u6ca1\u6709\u53ef\u5ba1\u7a3f\u7684\u5b8c\u6574\u5019\u9009")

    cache_key = build_cache_key(candidate_digest, category, main_product, avoid, model)
    cached = _load_cache(cache_key)
    if cached:
        try:
            bundle = _validate_bundle(
                cached,
                inventory=review_inventory,
                cache_key=cache_key,
                candidate_digest=candidate_digest,
                category=category,
                model=model,
                required_sources=required_sources,
            )
            log("AI\u5185\u5bb9\u5ba1\u7a3f: \u547d\u4e2d\u672c\u5730\u7f13\u5b58")
            return replace(bundle, cache_hit=True)
        except ContentReviewError:
            try:
                _cache_path(cache_key).unlink(missing_ok=True)
            except OSError:
                                _LOG.warning("os error", exc_info=True)

    last_format_error: Exception | None = None
    for attempt in range(2):
        system_prompt, user_prompt = _review_prompts(
            review_inventory,
            category=category,
            main_product=main_product,
            avoid=list(avoid or []),
            required_sources=required_sources,
            format_retry=bool(attempt),
        )
        log("AI\u5185\u5bb9\u5ba1\u7a3f: \u8c03\u7528\u6a21\u578b..." if not attempt else "AI\u5185\u5bb9\u5ba1\u7a3f: \u683c\u5f0f\u91cd\u8bd5...")
        content = _post_review_request(api_key, base_url, model, system_prompt, user_prompt)
        try:
            data = _extract_json_object(content)
        except (json.JSONDecodeError, ContentReviewError, ValueError) as exc:
            last_format_error = exc
            if attempt == 0:
                continue
            raise ContentReviewError(str(exc)) from exc
        raw_cards = data.get("cards") if isinstance(data.get("cards"), list) else []
        first_shape = "none"
        if raw_cards:
            first = raw_cards[0]
            if isinstance(first, (list, tuple)):
                first_shape = f"array[{len(first)}]"
            elif isinstance(first, Mapping):
                first_shape = "object[" + ",".join(sorted(str(key) for key in first.keys()))[:120] + "]"
            else:
                first_shape = type(first).__name__
        log(
            "AI\u5185\u5bb9\u5ba1\u7a3f: \u54cd\u5e94\u7ed3\u6784 "
            f"keys={','.join(sorted(str(key) for key in data.keys()))[:120] or 'none'}, "
            f"cards={len(raw_cards)}, first={first_shape}"
        )
        try:
            bundle = _validate_bundle(
                data,
                inventory=review_inventory,
                cache_key=cache_key,
                candidate_digest=candidate_digest,
                category=category,
                model=model,
                required_sources=required_sources,
            )
        except Exception as exc:
            log(
                f"AI\u5185\u5bb9\u5ba1\u7a3f: \u7ed3\u6784\u6821\u9a8c\u5f02\u5e38 "
                f"{type(exc).__name__}: {exc}"
            )
            raise
        log(
            f"AI\u5185\u5bb9\u5ba1\u7a3f: \u7ed3\u6784\u6821\u9a8c\u901a\u8fc7 "
            f"cards={len(bundle.cards)}, duration={bundle.retained_duration:.1f}s"
        )
        _write_cache(bundle)
        log("AI\u5185\u5bb9\u5ba1\u7a3f: \u7f13\u5b58\u5199\u5165\u5b8c\u6210")
        return bundle
    raise ContentReviewError(str(last_format_error or "\u5185\u5bb9\u5ba1\u7a3f\u683c\u5f0f\u5931\u8d25"))

@dataclass(frozen=True)
class FinalSequenceReview:
    status: str
    issues: tuple[str, ...]
    clips: tuple[dict[str, Any], ...]
    expansion_plan: tuple[dict[str, Any], ...] = ()

    def summary(self, *, applied: bool = False, fallback_reason: str = "") -> dict[str, Any]:
        return {
            "status": self.status,
            "issue_count": len(self.issues),
            "applied": bool(applied),
            "expansion_count": len(self.expansion_plan),
            "fallback_reason": str(fallback_reason or ""),
        }


def _normalize_final_sequence_review(
    data: Mapping[str, Any],
    *,
    allowed_candidate_ids: Iterable[int],
) -> FinalSequenceReview:
    status = _clean_text(data.get("status"), 16).lower()
    if status not in {"pass", "revise"}:
        raise ContentReviewError("\u6210\u7247\u7ec8\u5ba1\u7f3a\u5c11\u6709\u6548status")
    issues = _clean_list(data.get("issues"), limit=8, item_limit=100)
    if status == "pass":
        return FinalSequenceReview(status="pass", issues=issues, clips=())

    allowed_ids = {int(value) for value in allowed_candidate_ids}
    raw_clips = data.get("clips")
    if not isinstance(raw_clips, list) or not raw_clips:
        raise ContentReviewError("\u6210\u7247\u7ec8\u5ba1\u4fee\u8ba2\u7247\u5355\u4e3a\u7a7a")

    normalized: list[dict[str, Any]] = []
    used_ids: set[int] = set()
    for raw in raw_clips:
        if not isinstance(raw, Mapping):
            raise ContentReviewError("\u6210\u7247\u7ec8\u5ba1\u7247\u5355\u9879\u683c\u5f0f\u65e0\u6548")
        clip_type = _clean_text(raw.get("clip_type"), 16).lower()
        if clip_type not in {"hook", "product", "close"}:
            raise ContentReviewError("\u6210\u7247\u7ec8\u5ba1\u5305\u542b\u65e0\u6548clip_type")
        indices = raw.get("srt_indices")
        if isinstance(indices, int) and not isinstance(indices, bool):
            indices = [indices]
        if not isinstance(indices, list) or not 1 <= len(indices) <= 3:
            raise ContentReviewError("\u6210\u7247\u7ec8\u5ba1\u5305\u542b\u65e0\u6548\u5019\u9009\u7f16\u53f7")
        try:
            normalized_ids = [int(value) for value in indices if not isinstance(value, bool)]
        except (TypeError, ValueError):
            raise ContentReviewError("\u6210\u7247\u7ec8\u5ba1\u5019\u9009\u7f16\u53f7\u4e0d\u662f\u6574\u6570")
        if (
            len(normalized_ids) != len(indices)
            or normalized_ids != sorted(set(normalized_ids))
            or any(right != left + 1 for left, right in zip(normalized_ids, normalized_ids[1:]))
            or any(value not in allowed_ids for value in normalized_ids)
            or used_ids.intersection(normalized_ids)
        ):
            raise ContentReviewError("\u6210\u7247\u7ec8\u5ba1\u5019\u9009\u7f16\u53f7\u8d8a\u754c\u3001\u91cd\u590d\u6216\u4e0d\u8fde\u7eed")
        used_ids.update(normalized_ids)
        try:
            trim_priority = max(0, int(raw.get("trim_priority") or 0))
        except (TypeError, ValueError):
            trim_priority = 0
        normalized.append({
            "clip_type": clip_type,
            "srt_indices": normalized_ids,
            "focus": _clean_text(raw.get("focus"), 40),
            "reason": _clean_text(raw.get("reason"), 60),
            "trim_priority": trim_priority,
        })

    hook_positions = [index for index, item in enumerate(normalized) if item["clip_type"] == "hook"]
    close_positions = [index for index, item in enumerate(normalized) if item["clip_type"] == "close"]
    if not hook_positions and normalized:
        normalized[0]["clip_type"] = "hook"
        hook_positions = [0]
        issues = tuple((*issues, "\u9996\u6bb5Hook\u7c7b\u578b\u5df2\u5f52\u4e00")[:8])
    if hook_positions != [0]:
        raise ContentReviewError("\u6210\u7247\u7ec8\u5ba1\u4fee\u8ba2\u7ed3\u679c\u5fc5\u987b\u4e14\u53ea\u80fd\u4ee5Hook\u5f00\u5934")
    if close_positions and close_positions != [len(normalized) - 1]:
        raise ContentReviewError("\u6210\u7247\u7ec8\u5ba1Close\u53ea\u80fd\u4f4d\u4e8e\u672b\u6bb5")

    selected_groups = {tuple(item["srt_indices"]) for item in normalized}
    close_group = tuple(normalized[-1]["srt_indices"]) if close_positions else ()
    raw_expansion = data.get("expansion_plan")
    expansion_plan: list[dict[str, Any]] = []
    expansion_ids: set[int] = set()
    priorities: set[int] = set()
    if isinstance(raw_expansion, list):
        for response_order, raw in enumerate(raw_expansion):
            if not isinstance(raw, Mapping):
                continue
            indices = raw.get("srt_indices")
            if isinstance(indices, int) and not isinstance(indices, bool):
                indices = [indices]
            anchor = raw.get("after_srt_indices")
            if isinstance(anchor, int) and not isinstance(anchor, bool):
                anchor = [anchor]
            try:
                normalized_ids = [int(value) for value in indices if not isinstance(value, bool)]
                anchor_ids = tuple(int(value) for value in anchor if not isinstance(value, bool))
                priority = int(raw.get("priority", response_order + 1) or response_order + 1)
                after_order = max(1, int(raw.get("after_order") or 1))
            except (TypeError, ValueError):
                continue
            if (
                not isinstance(indices, list)
                or len(normalized_ids) != len(indices)
                or not 1 <= len(normalized_ids) <= 3
                or normalized_ids != sorted(set(normalized_ids))
                or any(right != left + 1 for left, right in zip(normalized_ids, normalized_ids[1:]))
                or any(value not in allowed_ids for value in normalized_ids)
                or used_ids.intersection(normalized_ids)
                or expansion_ids.intersection(normalized_ids)
                or anchor_ids not in selected_groups
                or anchor_ids == close_group
                or priority <= 0
                or priority in priorities
            ):
                continue
            priorities.add(priority)
            expansion_ids.update(normalized_ids)
            expansion_plan.append({
                "priority": priority,
                "after_srt_indices": list(anchor_ids),
                "after_order": after_order,
                "srt_indices": normalized_ids,
                "focus": _clean_text(raw.get("focus"), 40),
                "reason": _clean_text(raw.get("reason"), 80),
            })
            if len(expansion_plan) >= 8:
                break
    expansion_plan.sort(key=lambda item: int(item["priority"]))
    return FinalSequenceReview(
        status="revise",
        issues=issues,
        clips=tuple(normalized),
        expansion_plan=tuple(expansion_plan),
    )


def review_final_sequence(
    *,
    api_key: str,
    base_url: str,
    model: str,
    selected_sequence: Sequence[Mapping[str, Any]],
    inventory: Sequence[Mapping[str, Any]],
    allowed_candidate_ids: Iterable[int],
    category: str = "",
    preference: str = "",
    duration_low: float = 0.0,
    duration_high: float = 0.0,
    required_sources: Mapping[str, int] | None = None,
    log_fn=None,
) -> FinalSequenceReview:
    allowed_ids = {int(value) for value in allowed_candidate_ids}
    compact_inventory = [
        {
            "srt_index": int(item.get("srt_index") or 0),
            "source": _clean_text(item.get("source"), 24),
            "duration_sec": round(max(0.0, float(item.get("duration_sec") or 0.0)), 1),
            "text": _clean_text(item.get("text"), 240),
        }
        for item in inventory
        if int(item.get("srt_index") or 0) in allowed_ids
    ]
    if not selected_sequence or not compact_inventory:
        raise ContentReviewError("\u6210\u7247\u7ec8\u5ba1\u7f3a\u5c11\u7247\u5355\u6216\u5019\u9009")
    candidate_durations = sorted(
        float(item.get("duration_sec") or 0.0) for item in compact_inventory
        if float(item.get("duration_sec") or 0.0) > 0.0
    )
    typical_duration = candidate_durations[len(candidate_durations) // 2] if candidate_durations else 4.0
    count_divisor = max(4.0, min(8.0, typical_duration))
    recommended_min_clips = max(6, int(math.ceil(float(duration_low or 0.0) / count_divisor)))
    selected_duration = sum(float(item.get("duration_sec") or 0.0) for item in selected_sequence)
    inventory_duration = sum(candidate_durations)
    known_issues: list[str] = []
    first_text = _clean_text(selected_sequence[0].get("text"), 240)
    compact_first = re.sub(r"[\s,\uff0c\u3002.!\uff01?\uff1f]", "", first_text)
    if re.search(r"(?:\u60f3\u8981|\u9700\u8981|\u60f3\u770b).{0,24}\u7684[\u554a\u5440\u5462\u5427]?$", compact_first):
        known_issues.append("\u5f53\u524dHook\u53ea\u53ec\u5524\u4eba\u7fa4\u9700\u6c42\uff0c\u6ca1\u6709\u8bf4\u660e\u5546\u54c1\u7ed3\u679c")
    if re.search(r"\u60f3\u642d.{0,20}\u7ed9\u4f60\u770b\u4e00\u773c|\u5c31\u8fd9\u4e48\u642d", compact_first):
        known_issues.append("\u5f53\u524dHook\u662f\u5c55\u793a\u94fa\u57ab\uff0c\u4e0d\u662f\u72ec\u7acb\u8d2d\u4e70\u4ef7\u503c")
    production_pattern = re.compile(
        r"\u5e2e\u6211\u6295\u56de|\u6295\u56de\u521a\u521a|\u8bbe\u8ba1\u70b9|\u5bfc\u64ad|\u5207\u56de|\u4e0a\u753b\u9762"
    )
    for order, item in enumerate(selected_sequence, 1):
        text = _clean_text(item.get("text"), 240)
        duration = float(item.get("duration_sec") or 0.0)
        if production_pattern.search(text):
            known_issues.append(f"\u7b2c{order}\u6bb5\u542b\u5bfc\u64ad/\u6295\u6d41/\u5207\u753b\u9762\u6307\u4ee4")
        if str(item.get("clip_type") or "").lower() == "product" and duration > 12.0:
            known_issues.append(f"\u7b2c{order}\u6bb5{duration:.1f}\u79d2\u8fc7\u957f\uff0c\u5e94\u6362\u6210\u66f4\u77ed\u7684\u5b8c\u6574\u5356\u70b9")
    known_issue_text = "\uff1b".join(dict.fromkeys(known_issues)) or "\u65e0\u7a0b\u5e8f\u9884\u6807\u8bb0\uff0c\u4ecd\u9700\u4f60\u901a\u8bfb\u5224\u65ad"

    system_prompt = (
        "\u4f60\u662f\u5e26\u8d27\u77ed\u89c6\u9891\u7684\u6700\u7ec8\u5185\u5bb9\u4e3b\u7f16\u3002"
        "\u8f93\u5165\u7684selected_sequence\u662f\u5bfc\u6f14\u5df2\u7ecf\u6392\u597d\u7684\u5b8c\u6574\u7247\u5355\uff0cinventory\u662f\u5141\u8bb8\u66ff\u6362\u7684\u5168\u90e8\u5b89\u5168\u5019\u9009\u3002"
        "\u4f60\u53ea\u6709\u4e00\u6b21\u7ec8\u5ba1\u673a\u4f1a\uff1b\u5408\u683c\u5c31pass\uff0c\u4e0d\u5408\u683c\u5fc5\u987b\u8fd4\u56de\u4e00\u4efd\u5b8c\u6574\u65b0\u7247\u5355\uff0c\u4e0d\u80fd\u53ea\u8fd4\u56de\u5c40\u90e8\u589e\u5220\u64cd\u4f5c\u3002"
        "\u4e0d\u5f97\u6539\u5199\u5b57\u5e55\u3001\u65f6\u95f4\u6233\u6216\u6765\u6e90\uff0c\u53ea\u80fd\u4f7f\u7528inventory\u4e2d\u7684srt_index\u3002"
        "\u53ea\u8f93\u51faJSON\u5bf9\u8c61\uff0c\u4e0d\u8981Markdown\u6216\u89e3\u91ca\u3002"
    )
    source_rule = ""
    if required_sources:
        source_rule = "\u6df7\u526a\u6765\u6e90\u6700\u4f4e\u8981\u6c42:" + "\u3001".join(
            f"{source}\u81f3\u5c11{max(1, int(count or 1))}\u6bb5"
            for source, count in sorted(required_sources.items())
        )
    user_prompt = (
        f"\u54c1\u7c7b:{category or '\u901a\u7528'}\n"
        f"\u504f\u597d\u4e3b\u7ebf:{preference or '\u81ea\u52a8'}\n"
        f"\u539f\u7247\u65f6\u957f\u5408\u540c:{float(duration_low):.1f}-{float(duration_high):.1f}\u79d2\n"
        f"\u5f53\u524d\u7247\u5355\u539f\u7247\u5408\u8ba1:{selected_duration:.1f}\u79d2\uff1b\u5168\u90e8\u5ba1\u7a3f\u5019\u9009\u5408\u8ba1:{inventory_duration:.1f}\u79d2\uff1b"
        f"\u82e5revise\u5efa\u8bae\u81f3\u5c11{recommended_min_clips}\u4e2a\u7247\u6bb5\uff0c\u6700\u7ec8\u4ee5\u6309inventory.duration_sec\u9010\u9879\u5b9e\u7b97\u8fbe\u5230\u4e0b\u9650\u4e3a\u51c6\n"
        f"\u7a0b\u5e8f\u9884\u6807\u8bb0\u95ee\u9898:{known_issue_text}\n"
        f"{source_rule}\n"
        "\u7ec8\u5ba1\u6807\u51c6:\n"
        "1. Hook\u5fc5\u987b\u8131\u79bb\u4e0a\u4e0b\u6587\u4e5f\u80fd\u72ec\u7acb\u8bf4\u5b8c\u4e00\u4e2a\u5177\u4f53\u8d2d\u4e70\u4ef7\u503c\uff0c\u7b2c2\u6bb5\u5fc5\u987b\u7acb\u5373\u89e3\u91ca\u3001\u8bc1\u660e\u6216\u5151\u73b0\u5b83\u3002\u201c\u60f3\u8981X\u7684\u201d\u3001\u201c\u9700\u8981X\u7684\u201d\u53ea\u662f\u53ec\u5524\u4eba\u7fa4\uff0c\u6ca1\u6709\u8bf4\u660e\u5546\u54c1\u5982\u4f55\u89e3\u51b3\u95ee\u9898\uff0c\u4e0d\u662fHook\u3002"
        "\u201c\u60f3\u770bX\u5c31\u7ed9\u4f60\u770b\u4e00\u773c\u201d\u3001\u201c\u5c31\u8fd9\u4e48\u642d\u201d\u7c7b\u5c55\u793a\u94fa\u57ab\u4e0d\u662fHook\u3002\n"
        "2. \u5220\u6389\u660e\u663eASR\u4e71\u7801\u3001\u534a\u53e5\u3001\u6307\u4ee3\u4e0d\u660e\u3001\u7eaf\u4e92\u52a8\u3001\u5bfc\u64ad/\u6295\u6d41/\u5207\u753b\u9762\u6307\u4ee4\u3001\u4e0e\u8d2d\u4e70\u4ef7\u503c\u65e0\u5173\u7684\u73a9\u7b11\u548c\u7a7a\u6d1e\u5938\u8d5e\u3002\n"
        "3. \u540c\u4e49\u91cd\u590d\u53ea\u7559\u6700\u5b8c\u6574\u3001\u6700\u5177\u4f53\u7684\u8868\u8fbe\uff1b\u504f\u597d\u662f\u4e3b\u7ebf\u800c\u4e0d\u662f\u552f\u4e00\u4e3b\u9898\uff0c\u6b63\u6587\u5e94\u8986\u76d6\u81f3\u5c113\u4e2a\u6709\u8bc1\u636e\u7684\u5356\u70b9\u89d2\u5ea6\u3002\n"
        "4. \u76f8\u90bb\u7247\u6bb5\u8981\u80fd\u81ea\u7136\u542c\u61c2\uff0c\u7ed3\u5c3e\u5fc5\u987b\u662f\u81ea\u7136\u603b\u7ed3\u6216\u9009\u62e9\u7406\u7531\uff0c\u4e0d\u5f97\u4ee5\u6b8b\u53e5\u3001\u7eaf\u5c3a\u7801\u6216\u65e0\u5173\u5bf9\u8bdd\u7ed3\u675f\u3002\n"
        "5. revise\u5fc5\u987b\u4fdd\u6301\u65f6\u957f\u5408\u540c\u548c\u6df7\u526a\u6765\u6e90\u6700\u4f4e\u8981\u6c42\uff0c\u53ea\u67091\u4e2aHook\u4e14\u5728\u9996\u6bb5\uff0cClose\u5982\u5b58\u5728\u53ea\u80fd\u5728\u672b\u6bb5\u3002\u4fee\u8ba2\u4e0d\u5f97\u53ea\u5220\u7247\uff1b\u5148\u4fdd\u7559\u5f53\u524d\u7247\u5355\u4e2d\u7684\u4f18\u8d28\u9aa8\u67b6\uff0c\u518d\u4eceinventory\u66ff\u6362\u3001\u8865\u8db3\u4e0d\u540c\u5356\u70b9\u3002\u8f93\u51fa\u524d\u5fc5\u987b\u5728\u5185\u90e8\u9010\u9879\u76f8\u52a0duration_sec\uff0c\u4f4e\u4e8e\u4e0b\u9650\u65f6\u7ee7\u7eed\u8865\u5165\u5b8c\u6574\u9ad8\u8d28\u91cf\u7247\u6bb5\uff0c\u4e0d\u5f97\u63d0\u4ea4\u65f6\u957f\u4e0d\u8db3\u7684revise\u3002\u53ea\u8981\u7a0b\u5e8f\u9884\u6807\u8bb0\u4e86\u95ee\u9898\uff0c\u5c31\u7981\u6b62pass\uff0crevise\u5fc5\u987b\u81f3\u5c11\u66f4\u6362\u3001\u5220\u9664\u6216\u91cd\u63921\u4e2a\u7f16\u53f7\uff0c\u4e0d\u5f97\u539f\u6837\u4ea4\u5377\u3002\n"
        "6. revise\u9664\u5b8c\u6574clips\u5916\uff0c\u8fd8\u5fc5\u987b\u8f93\u51fa4-8\u4e2aexpansion_plan\u5907\u7528Product\u3002\u5907\u7528\u7247\u4e0d\u5f97\u4e0eclips\u91cd\u590d\uff0cpriority\u4ece1\u5f00\u59cb\u4e14\u4e0d\u91cd\u590d\uff1bafter_srt_indices\u5fc5\u987b\u7cbe\u786e\u6307\u5411clips\u4e2d\u7684\u975eClose\u7247\u6bb5\uff0c\u8868\u793a\u5e94\u63d2\u5728\u8be5\u6bb5\u4e4b\u540e\u3002\u5907\u7528\u7247\u4e5f\u5fc5\u987b\u5b8c\u6574\u3001\u5e72\u51c0\u3001\u5177\u4f53\uff0c\u5e76\u6309\u5bf9\u53d9\u4e8b\u7684\u5e2e\u52a9\u6392\u5e8f\u3002\n"
        "\u8f93\u51fa\u683c\u5f0f:\n"
        '{"status":"pass","issues":[],"clips":[]}\n'
        "\u6216\n"
        '{"status":"revise","issues":["Hook\u53ea\u662f\u5c55\u793a\u94fa\u57ab"],"clips":['
        '{"clip_type":"hook","srt_indices":[3],"focus":"\u5177\u4f53\u6548\u679c","reason":"\u72ec\u7acb\u8d2d\u4e70\u4ef7\u503c","trim_priority":0},'
        '{"clip_type":"product","srt_indices":[8],"focus":"\u539f\u56e0\u8bc1\u636e","reason":"\u7acb\u5373\u5151\u73b0Hook","trim_priority":0},'
        '{"clip_type":"close","srt_indices":[12],"focus":"\u603b\u7ed3","reason":"\u81ea\u7136\u6536\u675f","trim_priority":0}],"expansion_plan":['
        '{"priority":1,"after_srt_indices":[8],"after_order":2,"srt_indices":[18],"focus":"\u4e0d\u540c\u5356\u70b9","reason":"\u65f6\u957f\u4e0d\u8db3\u65f6\u4f18\u5148\u8865\u5165"}]}\n'
        "\u5f53\u524d\u5b8c\u6574\u7247\u5355:\n"
        + json.dumps(list(selected_sequence), ensure_ascii=False, separators=(",", ":"))
        + "\n\u5168\u90e8\u53ef\u7528\u5019\u9009:\n"
        + json.dumps(compact_inventory, ensure_ascii=False, separators=(",", ":"))
    )
    if log_fn:
        log_fn("AI\u6210\u7247\u7ec8\u5ba1: \u8c03\u7528\u6a21\u578b...")
    content = _post_review_request(api_key, base_url, model, system_prompt, user_prompt)
    data = _extract_json_object(content)
    return _normalize_final_sequence_review(data, allowed_candidate_ids=allowed_ids)
