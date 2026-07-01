#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""POC: monitor active Douyin live-commerce product signals while recording.

This script deliberately separates two concepts:
- catalog products: optional metadata only;
- active product: the current product being explained, accepted only from
  explicit runtime/network/IM/DOM signals.

If no strong active-product signal is found, the timeline records
active_product_unresolved and any fallback segment is marked
pending_user_confirm without a product id.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from douyin_chrome_live_poc import (  # noqa: E402
    CDPClient,
    close_ffmpeg_log,
    close_target,
    collect_stream_candidates,
    describe_stream_candidates,
    effective_stream_quality_label,
    find_ffmpeg,
    find_live_target,
    get_targets,
    get_page_state,
    launch_chrome,
    mask_url,
    open_new_tab,
    output_suffix_for_stream,
    page_has_playing_video,
    room_id_from_url,
    rank_stream_candidates_with_probe,
    safe_stem,
    snapshot_catalog,
    start_ffmpeg,
    stream_quality_label,
    wait_for_devtools,
)


DEFAULT_PORT = 9222
DEFAULT_SECONDS = 300.0
DEFAULT_PROBE_INTERVAL = 5.0
DEFAULT_STREAM_WAIT_SECONDS = 75.0
STRONG_THRESHOLD = 80
ACTIVE_CANDIDATE_CONFIDENCE = 70
ACTIVE_CANDIDATE_CONFIRM_WINDOW_SEC = 10.0
ACTIVE_POP_CANDIDATE_CONFIDENCE = 80
ACTIVE_POP_SHORT_CONFIRM_WINDOW_SEC = 30.0
ACTIVE_POP_LONG_REVIEW_WINDOW_SEC = 300.0
PARTIAL_ACTIVE_CONFIRM_WINDOW_SEC = 3.0
CONTROL_STOP_NOTE = "__liveclipper_stop__"
QUALITY_RANKS = {
    "未知清晰度": 0,
    "低清": 1,
    "480p": 2,
    "720p": 3,
    "1080p": 4,
    "超清": 5,
    "原画": 6,
}


PROBE_INSTALL_JS = r"""
(() => {
  const PROBE_VERSION = 2;
  if (window.__lc_active_product_probe_installed && window.__lc_active_product_probe_version === PROBE_VERSION) {
    return {
      installed: true,
      reused: true,
      version: PROBE_VERSION,
      queueLength: (window.__lc_active_product_probe_queue || []).length
    };
  }
  try {
    if (window.__lc_active_product_probe_observer) {
      window.__lc_active_product_probe_observer.disconnect();
    }
  } catch (err) {
    // Best effort cleanup for older probe versions.
  }

  const TERMS = [
    "promotion_id",
    "product_id",
    "currentPromotionId",
    "activePromotion",
    "defaultPromotion",
    "openDetailPromotion",
    "explain_type",
    "is_explaining",
    "\u6b63\u5728\u8bb2\u89e3",
    "\u8bb2\u89e3\u4e2d",
    "\u5f53\u524d\u8bb2\u89e3",
    "\u6b63\u5728\u4ecb\u7ecd",
    "\u53bb\u8d2d\u4e70",
    "\u5546\u54c1\u5361",
    "\u5c0f\u9ec4\u8f66"
  ];

  window.__lc_active_product_probe_installed = true;
  window.__lc_active_product_probe_version = PROBE_VERSION;
  window.__lc_active_product_probe_queue = [];
  window.__lc_active_product_probe_seen = new Set();

  function clean(value) {
    if (value === undefined || value === null) return "";
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    if (typeof value !== "string") return "";
    return value.replace(/\s+/g, " ").trim();
  }

  function first(obj, keys) {
    if (!obj || typeof obj !== "object") return "";
    for (const key of keys) {
      if (Object.prototype.hasOwnProperty.call(obj, key) && obj[key] !== undefined && obj[key] !== null) {
        return obj[key];
      }
    }
    return "";
  }

  function productFrom(obj) {
    if (!obj || typeof obj !== "object") return null;
    const data = obj.promotion && typeof obj.promotion === "object" ? obj.promotion : obj;
    let detailUrl = clean(first(data, ["detail_url", "detailUrl", "jump_url", "jumpUrl", "url", "schema"]));
    detailUrl = detailUrl.replace(/\\u0026/g, "&");
    const rawProductId = clean(first(data, ["product_id", "productId", "productID", "commodity_id", "commodityId", "item_id"]));
    let rawPromotionId = clean(first(data, ["promotion_id", "promotionId", "promotionID", "roomPromotionId"]));
    const fallbackId = clean(first(data, ["id"]));
    const title = clean(first(data, ["title", "name", "product_name", "productName", "short_title", "shortTitle", "desc"]));
    const isProductUrl = /ecom\.douyin\.com|ecommerce|trade\/detail/.test(detailUrl);
    if (!rawPromotionId && /^\d{8,}$/.test(fallbackId) && (rawProductId || isProductUrl || title)) {
      rawPromotionId = fallbackId;
    }
    const productId = /^\d{8,}$/.test(rawProductId) ? rawProductId : "";
    const promotionId = /^\d{8,}$/.test(rawPromotionId) || isProductUrl ? rawPromotionId : "";
    const product = {
      product_id: productId,
      promotion_id: promotionId,
      shop_id: clean(first(data, ["shop_id", "shopId", "shopID"])),
      title,
      detail_url: detailUrl,
      index: clean(first(data, ["index", "real_index", "rank", "rankIndex", "sequence", "sort"])),
      status: clean(first(data, ["status", "status_text", "statusText"])),
      explain_type: clean(first(data, ["explain_type", "explainType", "explain_status", "explainStatus"]))
    };
    if (!(product.product_id || product.promotion_id || isProductUrl)) return null;
    return product;
  }

  function productKey(product) {
    if (!product) return "";
    return product.promotion_id || product.product_id || product.detail_url || product.title || "";
  }

  function hasProductId(product) {
    return !!(product && (product.promotion_id || product.product_id || product.detail_url));
  }

  function pushSignal(source, reason, confidence, product, evidence) {
    if (!product && evidence && evidence.product) product = evidence.product;
    if (!product || !productKey(product)) return;
    const isStrong = confidence >= 80 && hasProductId(product);
    const event = {
      type: "candidate_signal",
      source,
      reason,
      confidence,
      is_strong: isStrong,
      product,
      evidence: evidence || {}
    };
    const dedupeKey = JSON.stringify({
      s: source,
      r: reason,
      k: productKey(product),
      c: confidence,
      p: event.evidence.path || "",
      text: (event.evidence.text || "").slice(0, 80)
    });
    if (window.__lc_active_product_probe_seen.has(dedupeKey)) return;
    window.__lc_active_product_probe_seen.add(dedupeKey);
    window.__lc_active_product_probe_queue.push({
      ts: Date.now(),
      ...event
    });
    if (window.__lc_active_product_probe_queue.length > 1000) {
      window.__lc_active_product_probe_queue.splice(0, window.__lc_active_product_probe_queue.length - 1000);
    }
  }

  function inspectObject(obj, source, path) {
    if (!obj || typeof obj !== "object") return;
    const product = productFrom(obj);
    const explainType = clean(first(obj, ["explain_type", "explainType", "explain_status", "explainStatus"]));
    if (product && explainType && explainType !== "0") {
      pushSignal(source, "explain_type", 85, product, { path, explain_type: explainType });
    }
    if (product && (obj.is_explaining || obj.isExplaining || obj.is_current || obj.isCurrent || obj.active || obj.isActive)) {
      pushSignal(source, "is_explaining", 85, product, { path });
    }

    const currentPromotionId = clean(first(obj, ["currentPromotionId", "current_promotion_id", "currentPromotionID"]));
    if (currentPromotionId) {
      pushSignal(source, "currentPromotionId", 95, { promotion_id: currentPromotionId }, { path, currentPromotionId });
    }

    for (const key of [
      "activePromotion",
      "active_promotion",
      "currentPromotion",
      "current_promotion",
      "defaultPromotion",
      "default_promotion",
      "openDetailPromotion",
      "open_detail_promotion",
      "explainingPromotion",
      "explainPromotion"
    ]) {
      const value = obj[key];
      if (value && typeof value === "object") {
        const p = productFrom(value);
        if (p) pushSignal(source, key, 90, p, { path: `${path}.${key}` });
      } else if (value) {
        pushSignal(source, key, 85, { promotion_id: clean(value) }, { path: `${path}.${key}` });
      }
    }

    if (product) {
      pushSignal(source, "product_object_weak", 35, product, { path });
    }
  }

  function walkObject(obj, source, path, depth, seen, budget) {
    if (!obj || typeof obj !== "object" || depth > 14 || budget.count > 50000) return;
    if (seen.has(obj)) return;
    seen.add(obj);
    budget.count += 1;
    inspectObject(obj, source, path);

    const keys = Object.keys(obj);
    const priority = [];
    const rest = [];
    for (const key of keys) {
      if (/^(stateNode|ownerDocument|parentNode|parentElement|firstChild|lastChild|nextSibling|previousSibling|socket|xhr|transport|player|video)$/.test(key)) {
        continue;
      }
      if (/^(memoizedProps|pendingProps|memoizedState|state|props|return|child|sibling|alternate|_owner|children|data|list|items|promotions|promotion|product|commodity|goods|ecom|cart|messageInstance)$/i.test(key)) {
        priority.push(key);
      } else if (/(promotion|product|commodity|goods|detail|schema|shop|room|cart|ecom|title|price|current|active|default|explain|message|payload|msgs)/i.test(key)) {
        priority.push(key);
      } else {
        rest.push(key);
      }
    }
    for (const key of priority.concat(rest.slice(0, 25))) {
      const value = obj[key];
      if (value && typeof value === "object") {
        walkObject(value, source, `${path}.${key}`, depth + 1, seen, budget);
      }
    }
  }

  function reactRoots() {
    return Array.from(document.querySelectorAll(
      "[data-e2e='living-container'],[data-e2e='yellowCart-container'],[data-e2e='__e_commerce__'],[data-e2e='live-promotion-list'],[data-e2e='shop-buyBtn'],[data-e2e='promotion-title']"
    ));
  }

  function scanReactRoots(source) {
    const roots = reactRoots();
    let started = 0;
    for (const root of roots) {
      for (const key of Object.keys(root)) {
        if (!key.startsWith("__reactFiber$") && !key.startsWith("__reactProps$")) continue;
        started += 1;
        walkObject(root[key], source, `${root.getAttribute("data-e2e") || root.tagName}.${key}`, 0, new WeakSet(), { count: 0 });
      }
    }
    return started;
  }

  function scanWindowGlobals() {
    const keys = Object.keys(window)
      .filter((key) => !key.startsWith("__lc_active_product_probe"))
      .filter((key) => /(promotion|product|commodity|goods|ecom|cart|live|room)/i.test(key))
      .slice(0, 80);
    for (const key of keys) {
      try {
        const value = window[key];
        if (value && typeof value === "object") {
          walkObject(value, "runtime_global", `window.${key}`, 0, new WeakSet(), { count: 0 });
        }
      } catch (err) {
        // Cross-origin or guarded getters are expected on some keys.
      }
    }
    return keys.length;
  }

  function scanDomNode(node) {
    if (!node || node.nodeType !== 1) return;
    const text = clean(node.innerText || node.textContent || "");
    if (!text) return;
    const hasActiveText = TERMS.some((term) => text.includes(term));
    if (!hasActiveText) return;
    pushSignal(
      "dom_mutation",
      "active_text",
      45,
      { title: text.slice(0, 120) },
      {
        text: text.slice(0, 300),
        tag: node.tagName,
        e2e: node.getAttribute && node.getAttribute("data-e2e")
      }
    );
    let cur = node;
    for (let depth = 0; cur && depth < 8; depth += 1, cur = cur.parentElement) {
      for (const key of Object.keys(cur)) {
        if (!key.startsWith("__reactFiber$") && !key.startsWith("__reactProps$")) continue;
        walkObject(cur[key], "dom_mutation_react", `${cur.tagName}.${key}`, 0, new WeakSet(), { count: 0 });
      }
    }
  }

  function installMutationObserver() {
    const observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        if (mutation.target) scanDomNode(mutation.target.nodeType === 1 ? mutation.target : mutation.target.parentElement);
        for (const node of mutation.addedNodes || []) {
          scanDomNode(node.nodeType === 1 ? node : node.parentElement);
        }
      }
    });
    observer.observe(document.documentElement || document.body, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true
    });
    window.__lc_active_product_probe_observer = observer;
  }

  function findMessageInstances() {
    const out = [];
    const seen = new WeakSet();
    function walk(obj, path, depth, budget) {
      if (!obj || typeof obj !== "object" || depth > 14 || budget.count > 30000) return;
      if (seen.has(obj)) return;
      seen.add(obj);
      budget.count += 1;
      if (obj.roomID && obj._onMessage && obj.decoder) out.push({ path, mi: obj });
      if (obj.messageInstance && obj.messageInstance._onMessage) out.push({ path: `${path}.messageInstance`, mi: obj.messageInstance });
      const keys = Object.keys(obj);
      const priority = [];
      const rest = [];
      for (const key of keys) {
        if (/^(stateNode|ownerDocument|parentNode|parentElement|firstChild|lastChild|nextSibling|previousSibling)$/.test(key)) continue;
        if (/^(memoizedProps|pendingProps|memoizedState|state|props|return|child|sibling|alternate|_owner|children|messageInstance)$/i.test(key)) priority.push(key);
        else rest.push(key);
      }
      for (const key of priority.concat(rest.slice(0, 16))) {
        const value = obj[key];
        if (value && typeof value === "object") walk(value, `${path}.${key}`, depth + 1, budget);
      }
    }
    for (const root of reactRoots()) {
      for (const key of Object.keys(root)) {
        if (!key.startsWith("__reactFiber$") && !key.startsWith("__reactProps$")) continue;
        walk(root[key], `${root.getAttribute("data-e2e") || root.tagName}.${key}`, 0, { count: 0 });
      }
    }
    return out;
  }

  function installMessagePatches() {
    let patched = 0;
    for (const { path, mi } of findMessageInstances()) {
      if (mi.__lc_active_product_probe_patched || typeof mi._onMessage !== "function") continue;
      const original = mi._onMessage;
      mi._onMessage = function (...args) {
        walkObject(args, "im_onMessage_before", `${path}._onMessage.args`, 0, new WeakSet(), { count: 0 });
        const result = original.apply(this, args);
        Promise.resolve(result)
          .then(() => walkObject(args, "im_onMessage_after", `${path}._onMessage.args`, 0, new WeakSet(), { count: 0 }))
          .catch(() => walkObject(args, "im_onMessage_error", `${path}._onMessage.args`, 0, new WeakSet(), { count: 0 }));
        return result;
      };
      mi.__lc_active_product_probe_patched = true;
      patched += 1;
    }
    return patched;
  }

  window.__lc_active_product_probe_poll = function () {
    const reactRootsScanned = scanReactRoots("runtime_react_poll");
    const globalsScanned = scanWindowGlobals();
    const patchedMessageInstances = installMessagePatches();
    return {
      reactRootsScanned,
      globalsScanned,
      patchedMessageInstances,
      queueLength: window.__lc_active_product_probe_queue.length
    };
  };

  window.__lc_active_product_probe_drain = function () {
    const events = window.__lc_active_product_probe_queue.splice(0, window.__lc_active_product_probe_queue.length);
    return events;
  };

  installMutationObserver();
  const initialRoots = scanReactRoots("runtime_react_initial");
  const initialGlobals = scanWindowGlobals();
  const patched = installMessagePatches();
  return {
    installed: true,
    reused: false,
    version: PROBE_VERSION,
    initialRoots,
    initialGlobals,
    patchedMessageInstances: patched,
    queueLength: window.__lc_active_product_probe_queue.length
  };
})()
"""


POLL_JS = r"""
(() => {
  if (!window.__lc_active_product_probe_poll) return { missing: true };
  return window.__lc_active_product_probe_poll();
})()
"""


DRAIN_JS = r"""
(() => {
  if (!window.__lc_active_product_probe_drain) return [];
  return window.__lc_active_product_probe_drain();
})()
"""


SIGNAL_KEYWORDS = (
    "promotion_id",
    "promotionid",
    "product_id",
    "productid",
    "currentpromotionid",
    "activepromotion",
    "defaultpromotion",
    "opendetailpromotion",
    "explain_type",
    "is_explaining",
    "explaining",
    "ecom",
    "commerce",
    "commodity",
)

ACTIVE_TEXT_TERMS = (
    "\u6b63\u5728\u8bb2\u89e3",
    "\u8bb2\u89e3\u4e2d",
    "\u5f53\u524d\u8bb2\u89e3",
    "\u6b63\u5728\u4ecb\u7ecd",
    "\u53bb\u8d2d\u4e70",
    "\u5546\u54c1\u5361",
    "\u5c0f\u9ec4\u8f66",
)

SIGNAL_SOURCE_BUCKETS = {
    "runtime_global": ("runtime_global", "runtime_react", "runtime_poll"),
    "dom_mutation": ("dom_mutation", "dom_active_card"),
    "im_message": ("im_onMessage",),
    "network": ("network_", "network_json", "network_body", "network_request", "network_response"),
    "websocket_frame": ("websocket_frame",),
    "ocr": ("ocr",),
}


ACTIVE_CARD_JS = r"""
(() => {
  function clean(value) {
    return String(value || '').replace(/\s+/g, ' ').trim();
  }
  function compact(value) {
    return clean(value).replace(/\s+/g, '');
  }
  function parseCardText(raw) {
    const text = clean(raw);
    const compactText = compact(raw);
    if (!compactText || !/(¥|￥|\d+\.?\d*)/.test(compactText)) return null;
    const indexMatch = compactText.match(/(?:^|[^A-Za-z0-9])x(\d{1,4})(?=[\u4e00-\u9fffA-Za-z])/i);
    const priceMatch = compactText.match(/[¥￥]\s*([0-9]+(?:\.[0-9]+)?)/);
    const servicePos = compactText.search(/运费险|7天无理由|满\d+件|满[0-9.]+减|券后价|券|去购买|去抢购|领券|全部商品|[¥￥]/);
    let title = '';
    if (indexMatch) {
      const start = indexMatch.index + indexMatch[0].length;
      const end = servicePos > start ? servicePos : Math.min(compactText.length, start + 96);
      title = compactText.slice(start, end);
    }
    if (!title) {
      title = compactText
        .replace(/^[xX]\d{1,4}/, '')
        .split(/运费险|7天无理由|满\d+件|满[0-9.]+减|券后价|券|去购买|去抢购|领券|全部商品|[¥￥]/)[0] || '';
    }
    if (!title) {
      const lines = text.split(/\n+/).map(line => clean(line)).filter(Boolean);
      const bad = /^(x?\d{1,4}|运费险|7天无理由|去购买|去抢购|领券|全部商品|满\d+件.*|.*券.*|[¥￥]?[0-9]+(?:\.[0-9]+)?|刷新|进入全屏|退出网页全屏|标清|高清|超清|原画)$/;
      const candidates = lines.filter(line => /[\u4e00-\u9fff]/.test(line) && !bad.test(line) && !/直播|小时榜|人气榜|客户端|更多直播|长时间无操作/.test(line));
      candidates.sort((a, b) => b.length - a.length);
      title = compact(candidates[0] || '');
    }
    title = title.replace(/^[xX]?\d{1,4}/, '').replace(/(运费险|7天无理由|满\d+件|满[0-9.]+减|券后价|券|去购买|去抢购|领券|全部商品|[¥￥]).*$/, '');
    if (title.length < 10 || !/[\u4e00-\u9fff]/.test(title) || /退货|无理由|运费险|去抢购|去购买/.test(title)) return null;
    return {
      index: indexMatch ? indexMatch[1] : '',
      title,
      price: priceMatch ? priceMatch[1] : '',
      active_marker: Boolean(indexMatch) || /讲解中|正在讲解|当前讲解/.test(text),
      text: text.slice(0, 500)
    };
  }
  const cards = [];
  const nodes = [...document.querySelectorAll('*')];
  for (const node of nodes) {
    const raw = node.innerText || node.textContent || '';
    if (!raw || raw.length > 1200) continue;
    if (!/(¥|￥|运费险|去购买|去抢购|全部商品)/.test(raw)) continue;
    const rect = node.getBoundingClientRect();
    if (rect.width < 60 || rect.height < 20 || rect.left < -5 || rect.top < -5 || rect.left > innerWidth || rect.top > innerHeight) continue;
    const parsed = parseCardText(raw);
    if (!parsed) continue;
    cards.push({
      ...parsed,
      tag: node.tagName,
      className: typeof node.className === 'string' ? node.className.slice(0, 160) : '',
      rect: {
        x: Math.round(rect.left),
        y: Math.round(rect.top),
        width: Math.round(rect.width),
        height: Math.round(rect.height)
      }
    });
  }
  cards.sort((a, b) => Number(Boolean(b.active_marker)) - Number(Boolean(a.active_marker)) || a.text.length - b.text.length || (a.rect.width * a.rect.height) - (b.rect.width * b.rect.height));
  const seen = new Set();
  const unique = [];
  for (const card of cards) {
    const key = card.title + '|' + card.price;
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(card);
  }
  return {cards: unique.slice(0, 20), textLen: document.body ? document.body.innerText.length : 0};
})()
"""


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def is_control_stop_note(note: str) -> bool:
    text = str(note or "").strip().lower()
    return text == CONTROL_STOP_NOTE or text.strip("_") == "liveclipper_stop"


def log(message: str) -> None:
    line = f"[{dt.datetime.now().strftime('%H:%M:%S')}] {message}"
    try:
        print(line, flush=True)
        return
    except (BrokenPipeError, OSError, ValueError):
        pass
    fallback = os.environ.get("LIVECLIPPER_TOOL_STDIO_FALLBACK_LOG", "").strip()
    if not fallback:
        return
    try:
        path = Path(fallback)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        return


def jsonl_write(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def normalize_min_stream_quality(value: str) -> str:
    text = str(value or "").strip().lower()
    if text in {"", "auto", "best", "highest", "none", "any", "自动", "自动最高"}:
        return ""
    aliases = {
        "ld": "低清",
        "low": "低清",
        "低清": "低清",
        "360": "低清",
        "360p": "低清",
        "sd": "480p",
        "sd1": "480p",
        "480": "480p",
        "480p": "480p",
        "hd": "720p",
        "hd1": "720p",
        "720": "720p",
        "720p": "720p",
        "full_hd": "1080p",
        "full_hd1": "1080p",
        "1080": "1080p",
        "1080p": "1080p",
        "uhd": "超清",
        "4k": "超清",
        "超清": "超清",
        "origin": "原画",
        "source": "原画",
        "原画": "原画",
    }
    return aliases.get(text, "")


def stream_quality_meets_min(
    label: str,
    min_quality: str,
    stream_url: str = "",
    probe_info: dict[str, Any] | None = None,
) -> bool:
    required = normalize_min_stream_quality(min_quality)
    if not required:
        return True
    required_rank = QUALITY_RANKS.get(required, 0)
    label_rank = QUALITY_RANKS.get(label or "未知清晰度", 0)
    if required_rank >= QUALITY_RANKS["1080p"]:
        if probe_info and probe_info.get("ok"):
            return label_rank >= required_rank
        named_quality = stream_quality_label(stream_url) if stream_url else (label or "")
        if named_quality in {"1080p", "超清"}:
            return QUALITY_RANKS.get(named_quality, 0) >= required_rank
        return False
    return label_rank >= required_rank


def resolve_douyin_short_url(url: str, timeout: float = 8.0) -> str:
    text = str(url or "").strip()
    if not re.search(r"https?://v\.douyin\.com/", text, re.I):
        return text
    request = urllib.request.Request(
        text,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        resolved = response.geturl()
    return str(resolved or text).strip() or text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Douyin active-product probe POC.")
    parser.add_argument("--url", required=True, help="Douyin live room URL.")
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS, help="Probe/record duration. 0 means until Ctrl+C.")
    parser.add_argument("--output-dir", default="", help="Default: ~/Videos/LiveClipperActiveProbe")
    parser.add_argument("--room-name", default="", help="Optional room name for output files.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Chrome DevTools port.")
    parser.add_argument("--probe-interval", type=float, default=DEFAULT_PROBE_INTERVAL, help="Runtime probe interval in seconds.")
    parser.add_argument("--stream-wait-seconds", type=float, default=DEFAULT_STREAM_WAIT_SECONDS, help="How long to wait for stream URL.")
    parser.add_argument("--min-stream-quality", default="", help="Minimum accepted stream quality: 1080p, 720p, 480p, low, or auto.")
    parser.add_argument("--capture-catalog", action="store_true", help="Optionally write catalog snapshots for metadata only.")
    parser.add_argument("--open-catalog", action="store_true", help="Open product list for catalog capture. Never used as active-product evidence.")
    parser.add_argument("--no-record", action="store_true", help="Probe only; do not start ffmpeg.")
    parser.add_argument("--manual-markers", action="store_true", help="Enable console manual switch markers even if stdin is not interactive.")
    parser.add_argument("--no-manual-markers", action="store_true", help="Disable console manual switch markers.")
    parser.add_argument("--manual-match-window", type=float, default=10.0, help="Seconds around a manual marker used to match active_product_change.")
    return parser.parse_args()


def prepare_chrome(port: int, url: str) -> dict[str, Any]:
    try:
        return wait_for_devtools(port, timeout=1.5)
    except Exception:
        profile_root = Path(os.environ.get("APPDATA", Path.home())) / "LiveClipper" / "chrome-live-poc"
        launch_chrome(port, url, profile_root)
        log("If Douyin asks for login, finish login in the Chrome window. This script will keep polling.")
        return wait_for_devtools(port, timeout=25.0)


def wait_for_opened_tab_navigation(port: int, opened: dict[str, Any], expected_room_id: str, timeout: float = 25.0) -> dict[str, Any] | None:
    target_id = str(opened.get("id") or "")
    if not target_id:
        return None
    deadline = time.time() + timeout
    last: dict[str, Any] | None = opened
    while time.time() < deadline:
        for target in get_targets(port):
            if str(target.get("id") or "") != target_id:
                continue
            last = target
            target_url = str(target.get("url") or "")
            lower = target_url.lower()
            if "live.douyin.com" in lower:
                return target
            if expected_room_id and expected_room_id in target_url and "v.douyin.com" not in lower and "webcast.amemv.com" not in lower:
                return target
        time.sleep(0.5)
    return last


def ensure_live_tab(port: int, url: str) -> tuple[dict[str, Any], bool]:
    expected_room_id = room_id_from_url(url)
    try:
        existing = find_live_target(port, url, timeout=5.0)
        if existing:
            return existing, False
    except Exception:
        pass
    opened = open_new_tab(port, url)
    if opened and opened.get("webSocketDebuggerUrl"):
        candidate = wait_for_opened_tab_navigation(port, opened, expected_room_id) or opened
        opened_url = str(candidate.get("url") or "")
        lower = opened_url.lower()
        if expected_room_id and expected_room_id in opened_url and "v.douyin.com" not in lower:
            return candidate, True
        if not expected_room_id and "v.douyin.com" not in lower and ("live.douyin.com" in lower or "webcast.amemv.com" in lower):
            return candidate, True
    try:
        return find_live_target(port, url, timeout=3.0), False
    except Exception:
        return find_live_target(port, url, timeout=25.0), False


def validate_target_room(target: dict[str, Any], room_url: str, page_state: dict[str, Any] | None = None) -> None:
    expected_room_id = room_id_from_url(room_url)
    if not expected_room_id:
        return
    candidates = [
        str(target.get("url") or ""),
        str((page_state or {}).get("href") or ""),
    ]
    if "webcast.amemv.com" in str(room_url or "").lower() and any("live.douyin.com" in value.lower() for value in candidates):
        return
    if not any(expected_room_id in value for value in candidates):
        actual = (page_state or {}).get("href") or target.get("url") or ""
        raise RuntimeError(f"Target room mismatch: expected {expected_room_id}, got {actual}")


def product_key(product: dict[str, Any] | None) -> str:
    if not isinstance(product, dict):
        return ""
    return str(product.get("promotion_id") or product.get("product_id") or product.get("detail_url") or product.get("title") or "")


def product_id_key(product: dict[str, Any] | None) -> str:
    if not isinstance(product, dict):
        return ""
    return "|".join(
        [
            str(product.get("promotion_id") or "").strip(),
            str(product.get("product_id") or "").strip(),
        ]
    ).strip("|")


def product_has_ids(product: dict[str, Any] | None) -> bool:
    return bool(isinstance(product, dict) and product.get("product_id") and product.get("promotion_id"))


def product_has_metadata(product: dict[str, Any] | None) -> bool:
    return bool(
        isinstance(product, dict)
        and (product.get("title") or product.get("product_id") or product.get("detail_url"))
    )


def merge_product_metadata(base: dict[str, Any] | None, extra: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(base or {})
    for key, value in (extra or {}).items():
        if value and not merged.get(key):
            merged[key] = value
    return merged


def is_status_2_product(product: dict[str, Any] | None) -> bool:
    if not isinstance(product, dict):
        return False
    return str(product.get("status") or "").strip() == "2"


def is_status_2_candidate_context(path: str, source_url: str = "") -> bool:
    normalized_path = str(path or "")
    lower_url = str(source_url or "").lower()
    if normalized_path == "$.promotions[0]":
        return True
    active_url_tokens = (
        "promotion",
        "promotions",
        "product",
        "commodity",
        "ecom",
        "commerce",
        "explain",
        "detail",
        "room/web/enter",
        "reflow/info",
        "webcast",
    )
    return bool(lower_url and any(token in lower_url for token in active_url_tokens))


def is_live_pop_product_context(path: str, source_url: str = "") -> bool:
    normalized_path = str(path or "")
    lower_url = str(source_url or "").lower()
    return normalized_path == "$.promotions[0]" and "/live/promotions/pop" in lower_url


def is_detail_context(source_url: str, path: str = "") -> bool:
    text = f"{source_url} {path}".lower()
    return any(token in text for token in ("detail", "trade/detail", "item/detail", "product/detail", "promotion/detail"))


def title_tokens(title: str) -> list[str]:
    text = re.sub(r"\s+", "", str(title or "").strip())
    if not text:
        return []
    chunks = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{3,}", text)
    tokens: list[str] = []
    for chunk in chunks:
        if len(chunk) <= 6:
            tokens.append(chunk.lower())
        else:
            tokens.extend(chunk[i : i + 4].lower() for i in range(0, max(1, len(chunk) - 3), 2))
    return list(dict.fromkeys(token for token in tokens if len(token) >= 2))[:20]


def title_matches_text(title: str, text: str) -> bool:
    compact_text = re.sub(r"\s+", "", str(text or "").lower())
    if not compact_text:
        return False
    tokens = title_tokens(title)
    if not tokens:
        return False
    hits = sum(1 for token in tokens if token in compact_text)
    return hits >= 2 or (hits >= 1 and len(tokens) <= 2)


def compact_product_title(value: str) -> str:
    return re.sub(r"[\s\u3000，,。.!！?？、:：;；|｜/\\\"'“”‘’（）()\[\]【】\-_%]+", "", str(value or "")).lower()


def title_match_score(left: str, right: str) -> int:
    a = compact_product_title(left)
    b = compact_product_title(right)
    if not a or not b:
        return 0
    if a == b:
        return 100
    if len(a) >= 10 and len(b) >= 10 and (a in b or b in a):
        return 92
    left_tokens = title_tokens(left)
    right_text = b
    if not left_tokens:
        return 0
    hits = sum(1 for token in left_tokens if token in right_text)
    return int((hits / max(1, len(left_tokens))) * 80)


def normalize_product(product: Any) -> dict[str, Any]:
    if not isinstance(product, dict):
        return {}
    detail_url = str(product.get("detail_url") or product.get("detailUrl") or product.get("url") or product.get("schema") or "").replace("\\u0026", "&").strip()
    title = re.sub(r"\s+", " ", str(product.get("title") or product.get("name") or product.get("product_name") or "")).strip()
    raw_product_id = str(product.get("product_id") or product.get("productId") or product.get("commodity_id") or "").strip()
    raw_promotion_id = str(product.get("promotion_id") or product.get("promotionId") or product.get("roomPromotionId") or "").strip()
    fallback_id = str(product.get("id") or "").strip()
    is_product_url = bool(re.search(r"ecom\.douyin\.com|ecommerce|trade/detail", detail_url, re.I))
    if not raw_promotion_id and re.fullmatch(r"\d{8,}", fallback_id) and (raw_product_id or is_product_url or title):
        raw_promotion_id = fallback_id
    product_id = raw_product_id if re.fullmatch(r"\d{8,}", raw_product_id) else ""
    promotion_id = raw_promotion_id if (re.fullmatch(r"\d{8,}", raw_promotion_id) or is_product_url) else ""
    return {
        "product_id": product_id,
        "promotion_id": promotion_id,
        "shop_id": str(product.get("shop_id") or product.get("shopId") or "").strip(),
        "title": title,
        "detail_url": detail_url,
        "index": str(product.get("index") or product.get("real_index") or "").strip(),
        "status": str(product.get("status") or "").strip(),
        "explain_type": str(product.get("explain_type") or product.get("explainType") or "").strip(),
    }


def has_signal_text(value: str) -> bool:
    lower = value.lower()
    if any(token in lower for token in SIGNAL_KEYWORDS):
        return True
    return any(term in value for term in ACTIVE_TEXT_TERMS)


def id_from_text(text: str) -> dict[str, str]:
    product_id = ""
    promotion_id = ""
    product_match = re.search(r"(?:product_id|productId|commodity_id|commodityId)[^\d]{0,8}(\d{8,})", text, re.I)
    if product_match:
        product_id = product_match.group(1)
    promotion_match = re.search(r"(?:promotion_id|promotionId|roomPromotionId|currentPromotionId)[^\d]{0,8}(\d{8,})", text, re.I)
    if promotion_match:
        promotion_id = promotion_match.group(1)
    detail_match = re.search(r"https?://[^\s\"'<>]+(?:ecommerce|trade/detail)[^\s\"'<>]+", text, re.I)
    detail_url = detail_match.group(0).replace("\\u0026", "&") if detail_match else ""
    if not promotion_id and detail_url:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(detail_url).query)
        promotion_id = (query.get("id") or [""])[0]
    return {"product_id": product_id, "promotion_id": promotion_id, "detail_url": detail_url}


def event_identity(event: dict[str, Any]) -> str:
    product = event.get("product") if isinstance(event.get("product"), dict) else {}
    event_type = str(event.get("type") or "")
    source = str(event.get("source") or "")
    elapsed_bucket = ""
    if event_type in {"active_product_candidate", "active_product_manual_or_detail_candidate"} or source.startswith("network"):
        try:
            elapsed_bucket = str(int(float(event.get("elapsed") or 0)))
        except Exception:
            elapsed_bucket = ""
    return "|".join(
        [
            event_type,
            source,
            str(event.get("reason") or ""),
            str(product.get("promotion_id") or ""),
            str(product.get("product_id") or ""),
            str(product.get("detail_url") or "")[:120],
            str(event.get("evidence", {}).get("path") or ""),
            str(event.get("evidence", {}).get("url_masked") or ""),
            elapsed_bucket,
        ]
    )


def normalize_probe_event(raw: dict[str, Any], elapsed: float, source_override: str | None = None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    product = normalize_product(raw.get("product"))
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
    confidence = int(float(raw.get("confidence") or 0))
    is_strong = bool(raw.get("is_strong")) and confidence >= STRONG_THRESHOLD and bool(product_key(product))
    return {
        "type": "candidate_signal",
        "ts": now_iso(),
        "elapsed": round(elapsed, 3),
        "source": source_override or str(raw.get("source") or "unknown"),
        "reason": str(raw.get("reason") or ""),
        "confidence": confidence,
        "is_strong": is_strong,
        "product": product,
        "evidence": evidence,
    }


def candidate_from_text(source: str, reason: str, text: str, elapsed: float, confidence: int, evidence: dict[str, Any]) -> dict[str, Any] | None:
    if not has_signal_text(text):
        return None
    ids = id_from_text(text)
    product = normalize_product(ids)
    if not product_key(product) and not any(term in text for term in ACTIVE_TEXT_TERMS):
        return None
    is_active_text = any(term in text for term in ACTIVE_TEXT_TERMS)
    is_field_active = any(token in text.lower() for token in ("currentpromotionid", "activepromotion", "defaultpromotion", "opendetailpromotion", "explain_type", "is_explaining"))
    is_strong = bool(product_key(product)) and (is_active_text or is_field_active or confidence >= STRONG_THRESHOLD)
    return {
        "type": "candidate_signal",
        "ts": now_iso(),
        "elapsed": round(elapsed, 3),
        "source": source,
        "reason": reason,
        "confidence": confidence,
        "is_strong": is_strong,
        "product": product,
        "evidence": {**evidence, "snippet": text[:500]},
    }


def scan_json_signals(
    value: Any,
    source: str,
    elapsed: float,
    path: str = "$",
    depth: int = 0,
    out: list[dict[str, Any]] | None = None,
    source_url: str = "",
) -> list[dict[str, Any]]:
    if out is None:
        out = []
    if depth > 8 or len(out) > 80:
        return out
    if isinstance(value, dict):
        product = normalize_product(value)
        product_identity = product.get("promotion_id") or product.get("product_id") or product.get("detail_url")
        lower_keys = {str(key).lower(): key for key in value.keys()}
        if product_identity:
            reason = "product_object_weak"
            confidence = 35
            is_strong = False
            event_type = "candidate_signal"
            needs_confirm = False
            confirm_window_sec = 0.0
            if any(key.lower() in lower_keys for key in ("currentpromotionid", "activepromotion", "defaultpromotion", "opendetailpromotion")):
                reason = "active_product_field"
                confidence = 90
                is_strong = True
            explain_type = str(value.get("explain_type") or value.get("explainType") or "")
            if explain_type and explain_type != "0":
                reason = "explain_type"
                confidence = 85
                is_strong = True
            if value.get("is_explaining") or value.get("isExplaining") or value.get("is_current") or value.get("isCurrent"):
                reason = "is_explaining"
                confidence = 85
                is_strong = True
            if (
                not is_strong
                and product_has_ids(product)
                and is_status_2_product(product)
                and is_status_2_candidate_context(path, source_url)
            ):
                event_type = "active_product_candidate"
                reason = "network_status_2_candidate"
                confidence = ACTIVE_CANDIDATE_CONFIDENCE
                needs_confirm = True
                confirm_window_sec = ACTIVE_CANDIDATE_CONFIRM_WINDOW_SEC
                if is_live_pop_product_context(path, source_url):
                    reason = "network_pop_v3_status_2_candidate"
                    confidence = ACTIVE_POP_CANDIDATE_CONFIDENCE
                    confirm_window_sec = ACTIVE_POP_SHORT_CONFIRM_WINDOW_SEC
            elif not is_strong and product_has_ids(product) and is_detail_context(source_url, path):
                event_type = "active_product_manual_or_detail_candidate"
                reason = "detail_product_candidate"
                confidence = 65
                needs_confirm = True
                confirm_window_sec = ACTIVE_CANDIDATE_CONFIRM_WINDOW_SEC
            evidence = {"path": path}
            if source_url:
                evidence["url_masked"] = mask_url(source_url)
            out.append(
                {
                    "type": event_type,
                    "ts": now_iso(),
                    "elapsed": round(elapsed, 3),
                    "source": source,
                    "reason": reason,
                    "confidence": confidence,
                    "is_strong": bool(is_strong and product_key(product)),
                    "needs_confirm": needs_confirm,
                    "confirm_window_sec": confirm_window_sec,
                    "review_window_sec": ACTIVE_POP_LONG_REVIEW_WINDOW_SEC if is_live_pop_product_context(path, source_url) else confirm_window_sec,
                    "product": product,
                    "evidence": evidence,
                }
            )
        for field in ("currentPromotionId", "activePromotion", "defaultPromotion", "openDetailPromotion"):
            if field in value and value[field]:
                if isinstance(value[field], dict):
                    product = normalize_product(value[field])
                else:
                    product = {"promotion_id": str(value[field]).strip(), "product_id": "", "shop_id": "", "title": "", "detail_url": "", "index": "", "status": "", "explain_type": ""}
                if product_key(product):
                    out.append(
                        {
                            "type": "candidate_signal",
                            "ts": now_iso(),
                            "elapsed": round(elapsed, 3),
                            "source": source,
                            "reason": field,
                            "confidence": 95 if field == "currentPromotionId" else 90,
                            "is_strong": True,
                            "product": product,
                            "evidence": {"path": f"{path}.{field}"},
                        }
                    )
        for key, child in list(value.items())[:160]:
            if isinstance(child, (dict, list)):
                scan_json_signals(child, source, elapsed, f"{path}.{key}", depth + 1, out, source_url)
    elif isinstance(value, list):
        for index, item in enumerate(value[:80]):
            if isinstance(item, (dict, list)):
                scan_json_signals(item, source, elapsed, f"{path}[{index}]", depth + 1, out, source_url)
    return out


def event_url(event: dict[str, Any]) -> str:
    params = event.get("params") or {}
    method = event.get("method")
    if method == "Network.requestWillBeSent":
        request = params.get("request") or {}
        return str(request.get("url") or "")
    if method == "Network.responseReceived":
        response = params.get("response") or {}
        return str(response.get("url") or "")
    return ""


def should_scan_response_body(url: str, mime: str, body: str = "") -> bool:
    lower_url = str(url or "").lower()
    lower_mime = str(mime or "").lower()
    if "json" in lower_mime:
        return True
    if any(token in lower_url for token in ("ecom", "promotion", "promotions", "product", "commodity", "commerce", "cart", "pop/v3")):
        return True
    return bool(body and has_signal_text(body[:20000]))


def scan_network_response_body(
    body: str,
    url: str,
    status: Any,
    mime: str,
    elapsed: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not body or not should_scan_response_body(url, mime, body):
        return out
    stripped = body.strip()
    if stripped and stripped[0] in "[{":
        try:
            parsed = json.loads(stripped)
            out.extend(scan_json_signals(parsed, "network_json", elapsed, source_url=url))
            return out
        except Exception:
            pass
    if has_signal_text(body[:20000]):
        candidate = candidate_from_text(
            "network_body",
            "body_text",
            body,
            elapsed,
            60,
            {"url_masked": mask_url(url), "status": status, "mime": mime},
        )
        if candidate:
            out.append(candidate)
    return out


def process_network_event(
    client: CDPClient,
    event: dict[str, Any],
    elapsed: float,
    request_urls: dict[str, str],
    response_meta: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    method = event.get("method")
    params = event.get("params") or {}
    out: list[dict[str, Any]] = []

    if method == "Network.requestWillBeSent":
        request = params.get("request") or {}
        request_id = str(params.get("requestId") or "")
        url = str(request.get("url") or "")
        if request_id:
            request_urls[request_id] = url
        if has_signal_text(url):
            candidate = candidate_from_text(
                "network_request",
                "request_url",
                url,
                elapsed,
                55,
                {"url_masked": mask_url(url), "method": request.get("method")},
            )
            if candidate:
                out.append(candidate)

    elif method == "Network.responseReceived":
        request_id = str(params.get("requestId") or "")
        response = params.get("response") or {}
        url = str(response.get("url") or request_urls.get(request_id) or "")
        mime = str(response.get("mimeType") or "")
        if request_id and response_meta is not None and should_scan_response_body(url, mime):
            response_meta[request_id] = {"url": url, "mime": mime, "status": response.get("status")}
        if has_signal_text(url):
            candidate = candidate_from_text(
                "network_response",
                "response_url",
                url,
                elapsed,
                55,
                {"url_masked": mask_url(url), "status": response.get("status"), "mime": mime},
            )
            if candidate:
                out.append(candidate)
        if request_id and should_scan_response_body(url, mime):
            try:
                body_result = client.call("Network.getResponseBody", {"requestId": request_id}, timeout=3)
                if not body_result.get("base64Encoded"):
                    body = str(body_result.get("body") or "")
                    out.extend(scan_network_response_body(body, url, response.get("status"), mime, elapsed))
            except Exception:
                pass

    elif method == "Network.loadingFinished":
        if response_meta is not None:
            request_id = str(params.get("requestId") or "")
            meta = response_meta.pop(request_id, None)
            if meta:
                url = str(meta.get("url") or request_urls.get(request_id) or "")
                mime = str(meta.get("mime") or "")
                try:
                    body_result = client.call("Network.getResponseBody", {"requestId": request_id}, timeout=5)
                    if not body_result.get("base64Encoded"):
                        body = str(body_result.get("body") or "")
                        out.extend(scan_network_response_body(body, url, meta.get("status"), mime, elapsed))
                except Exception:
                    pass

    elif method == "Network.webSocketFrameReceived":
        frame = (params.get("response") or {}).get("payloadData") or ""
        if has_signal_text(str(frame)[:5000]):
            candidate = candidate_from_text(
                "websocket_frame",
                "frame_payload",
                str(frame),
                elapsed,
                70,
                {"opcode": (params.get("response") or {}).get("opcode"), "length": len(str(frame))},
            )
            if candidate:
                out.append(candidate)

    elif method == "Network.webSocketCreated":
        url = str(params.get("url") or "")
        if url:
            out.append(
                {
                    "type": "network_observation",
                    "ts": now_iso(),
                    "elapsed": round(elapsed, 3),
                    "source": "websocket_created",
                    "url_masked": mask_url(url),
                }
            )
    return out


class ProbeRecorder:
    def __init__(self, probe_path: Path, timeline_path: Path) -> None:
        self.probe_path = probe_path
        self.timeline_path = timeline_path
        self.seen: set[str] = set()
        self.event_seq = 0
        self.signal_count = 0
        self.strong_signal_count = 0
        self.active_change_count = 0
        self.unresolved_count = 0
        self.max_confidence = 0
        self.source_counts: dict[str, int] = {}
        self.strong_source_counts: dict[str, int] = {}
        self.last_active_key = ""
        self.active_change_events: list[dict[str, Any]] = []
        self.manual_markers: list[dict[str, Any]] = []
        self.pending_candidates: dict[str, dict[str, Any]] = {}
        self.active_product_candidates: list[dict[str, Any]] = []
        self.rule_review_events: list[dict[str, Any]] = []
        self.pending_partial_active: dict[str, dict[str, Any]] = {}
        self.recent_dom_card_events: list[dict[str, Any]] = []
        self.product_cache_by_promotion: dict[str, dict[str, Any]] = {}
        self.product_cache_by_product: dict[str, dict[str, Any]] = {}
        self.active_product_candidate_count = 0
        self.status_2_candidate_count = 0
        self.active_product_confirmed_count = 0
        self.active_product_rule_review_count = 0
        self.confirmed_by_dom_count = 0
        self.confirmed_by_network_repeat_count = 0
        self.confirmed_by_runtime_count = 0
        self.confirmed_by_im_count = 0
        self.confirmed_by_ocr_count = 0
        self.detail_candidate_count = 0
        self.catalog_only_count = 0

    def _next_event_id(self, prefix: str = "evt") -> str:
        self.event_seq += 1
        return f"{prefix}_{self.event_seq:06d}"

    def write_probe(self, event: dict[str, Any]) -> dict[str, Any] | None:
        event.setdefault("event_id", self._next_event_id("probe"))
        event_type = str(event.get("type") or "")
        signal_types = {"candidate_signal", "active_product_candidate", "active_product_manual_or_detail_candidate"}
        if event_type not in signal_types:
            jsonl_write(self.probe_path, event)
            return event
        self.remember_product(event)
        identity = event_identity(event)
        if identity in self.seen:
            return None
        self.seen.add(identity)
        self.signal_count += 1
        source = str(event.get("source") or "unknown")
        self.source_counts[source] = self.source_counts.get(source, 0) + 1
        confidence = int(event.get("confidence") or 0)
        self.max_confidence = max(self.max_confidence, confidence)
        if event.get("is_strong"):
            self.strong_signal_count += 1
            self.strong_source_counts[source] = self.strong_source_counts.get(source, 0) + 1
        if event_type == "active_product_candidate":
            self.active_product_candidate_count += 1
            if event.get("reason") in {"network_status_2_candidate", "network_pop_v3_status_2_candidate"}:
                self.status_2_candidate_count += 1
            self.active_product_candidates.append(event)
        elif event_type == "active_product_manual_or_detail_candidate":
            self.detail_candidate_count += 1
        elif event.get("reason") == "product_object_weak":
            evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
            evidence_path = str(evidence.get("path") or "").lower()
            if any(token in evidence_path for token in ("catalog", "promotions", "products", "list", "items")):
                self.catalog_only_count += 1
        jsonl_write(self.probe_path, event)
        return event

    def remember_product(self, event: dict[str, Any]) -> None:
        product = event.get("product") if isinstance(event.get("product"), dict) else {}
        if not product_has_metadata(product):
            return
        promotion_id = str(product.get("promotion_id") or "").strip()
        product_id = str(product.get("product_id") or "").strip()
        if promotion_id:
            self.product_cache_by_promotion[promotion_id] = merge_product_metadata(
                self.product_cache_by_promotion.get(promotion_id),
                product,
            )
        if product_id:
            self.product_cache_by_product[product_id] = merge_product_metadata(
                self.product_cache_by_product.get(product_id),
                product,
            )

    def cached_product_for(self, product: dict[str, Any]) -> dict[str, Any]:
        cached: dict[str, Any] = {}
        promotion_id = str(product.get("promotion_id") or "").strip()
        product_id = str(product.get("product_id") or "").strip()
        if promotion_id:
            cached = merge_product_metadata(cached, self.product_cache_by_promotion.get(promotion_id))
        if product_id:
            cached = merge_product_metadata(cached, self.product_cache_by_product.get(product_id))
        return cached

    def write_active_change(
        self,
        event: dict[str, Any],
        product: dict[str, Any],
        evidence_event_ids: list[str | None] | None = None,
        source: str | None = None,
        reason: str | None = None,
        confidence: Any = None,
        evidence: dict[str, Any] | None = None,
        elapsed: Any = None,
    ) -> None:
        key = product_id_key(product) or product_key(product)
        if not key or key == self.last_active_key:
            return
        self.last_active_key = key
        self.active_change_count += 1
        event_ids = [item for item in (evidence_event_ids or [event.get("event_id")]) if item]
        change_event = {
            "type": "active_product_change",
            "ts": event.get("ts") or now_iso(),
            "elapsed": event.get("elapsed") if elapsed is None else elapsed,
            "source": source or event.get("source"),
            "reason": reason or event.get("reason"),
            "confidence": confidence if confidence is not None else event.get("confidence"),
            "product": product,
            "product_id": product.get("product_id") or "",
            "promotion_id": product.get("promotion_id") or "",
            "title": product.get("title") or "",
            "start_ms": int(float((event.get("elapsed") if elapsed is None else elapsed) or 0) * 1000),
            "evidence_event_ids": event_ids,
            "confirm_reason": reason or event.get("reason") or "strong_signal",
            "evidence": evidence if evidence is not None else (event.get("evidence") or {}),
        }
        self.active_change_events.append(change_event)
        jsonl_write(self.timeline_path, change_event)

    def maybe_complete_partial_active(self, event: dict[str, Any], elapsed: float) -> bool:
        event_product = event.get("product") if isinstance(event.get("product"), dict) else {}
        if not product_has_metadata(event_product):
            return False
        event_elapsed = float(event.get("elapsed") or elapsed)
        for key, pending in list(self.pending_partial_active.items()):
            pending_product = pending.get("product") if isinstance(pending.get("product"), dict) else {}
            if not self.same_product(pending_product, event_product):
                continue
            pending_elapsed = float(pending.get("elapsed") or event_elapsed)
            if event_elapsed - pending_elapsed > PARTIAL_ACTIVE_CONFIRM_WINDOW_SEC:
                continue
            merged = merge_product_metadata(pending_product, event_product)
            self.pending_partial_active.pop(key, None)
            self.write_active_change(
                pending,
                merged,
                evidence_event_ids=[pending.get("event_id"), event.get("event_id")],
                source=f"{pending.get('source')}+{event.get('source')}",
                reason=f"{pending.get('reason')}_with_product_metadata",
                confidence=pending.get("confidence"),
                evidence={
                    "current_signal": pending.get("evidence") or {},
                    "product_metadata": event.get("evidence") or {},
                },
                elapsed=pending.get("elapsed"),
            )
            return True
        return False

    def expire_partial_active(self, elapsed: float, force: bool = False) -> None:
        for key, pending in list(self.pending_partial_active.items()):
            pending_elapsed = float(pending.get("elapsed") or 0)
            if not force and elapsed - pending_elapsed <= PARTIAL_ACTIVE_CONFIRM_WINDOW_SEC:
                continue
            self.pending_partial_active.pop(key, None)
            product = pending.get("product") if isinstance(pending.get("product"), dict) else {}
            self.write_active_change(pending, product)

    def maybe_write_active_change(self, event: dict[str, Any]) -> None:
        if event.get("type") != "candidate_signal" or not event.get("is_strong"):
            return
        product = event.get("product") if isinstance(event.get("product"), dict) else {}
        key = product_key(product)
        if not key:
            return
        if not product_has_metadata(product):
            cached = self.cached_product_for(product)
            if product_has_metadata(cached):
                self.write_active_change(
                    event,
                    merge_product_metadata(product, cached),
                    reason=f"{event.get('reason')}_with_cached_product_metadata",
                    evidence={"current_signal": event.get("evidence") or {}, "cached_product": cached},
                )
                return
            self.pending_partial_active[key] = dict(event)
            return
        self.write_active_change(event, product)

    def process_event(self, event: dict[str, Any], elapsed: float) -> None:
        event_type = str(event.get("type") or "")
        source = str(event.get("source") or "")
        self.expire_candidates(elapsed)
        self.expire_partial_active(elapsed)
        if event_type == "candidate_signal" and source.startswith("dom_active_card"):
            self.remember_recent_dom_card(event, elapsed)
        if self.maybe_complete_partial_active(event, elapsed):
            return
        if event_type == "candidate_signal" and event.get("is_strong"):
            if not self.confirm_pending_candidates(event, elapsed):
                self.maybe_write_active_change(event)
            return
        if event_type == "active_product_candidate":
            before_changes = self.active_change_count
            consumed = self.confirm_pending_candidates(event, elapsed)
            if not consumed and self.active_change_count == before_changes:
                self.add_active_candidate(event, elapsed)
            return
        if event_type == "active_product_manual_or_detail_candidate":
            self.confirm_pending_candidates(event, elapsed)
            return
        if event_type == "candidate_signal":
            self.confirm_pending_candidates(event, elapsed)

    def add_active_candidate(self, event: dict[str, Any], elapsed: float) -> None:
        product = event.get("product") if isinstance(event.get("product"), dict) else {}
        key = product_id_key(product) or product_key(product)
        if not key:
            return
        candidate = dict(event)
        candidate["candidate_id"] = event.get("event_id") or self._next_event_id("candidate")
        candidate["candidate_elapsed"] = float(event.get("elapsed") or elapsed)
        confirm_window = float(event.get("confirm_window_sec") or ACTIVE_CANDIDATE_CONFIRM_WINDOW_SEC)
        review_window = float(event.get("review_window_sec") or confirm_window)
        candidate["confirm_deadline"] = candidate["candidate_elapsed"] + confirm_window
        candidate["review_deadline"] = candidate["candidate_elapsed"] + max(confirm_window, review_window)
        candidate["confirmed"] = False
        candidate["rule_review_written"] = False
        recent_dom_confirmation = self.confirm_from_recent_dom_card(candidate, elapsed)
        if recent_dom_confirmation:
            dom_event, confirmation = recent_dom_confirmation
            self.write_confirmed_active_change(candidate, dom_event, confirmation)
            candidate["confirmed"] = True
            return
        self.pending_candidates[key] = candidate
        jsonl_write(
            self.timeline_path,
            {
                "type": "active_product_candidate",
                "ts": event.get("ts") or now_iso(),
                "elapsed": event.get("elapsed"),
                "reason": event.get("reason"),
                "confidence": event.get("confidence"),
                "is_strong": False,
                "needs_confirm": True,
                "confirm_window_sec": event.get("confirm_window_sec") or ACTIVE_CANDIDATE_CONFIRM_WINDOW_SEC,
                "review_window_sec": event.get("review_window_sec") or event.get("confirm_window_sec") or ACTIVE_CANDIDATE_CONFIRM_WINDOW_SEC,
                "product": product,
                "evidence": event.get("evidence") or {},
                "event_id": event.get("event_id"),
            },
        )

    def confirm_pending_candidates(self, event: dict[str, Any], elapsed: float) -> bool:
        consumed = False
        for key, candidate in list(self.pending_candidates.items()):
            if candidate.get("confirmed") or candidate.get("rule_review_written"):
                continue
            candidate_elapsed = float(candidate.get("candidate_elapsed") or 0)
            confirm_window = float(candidate.get("confirm_window_sec") or ACTIVE_CANDIDATE_CONFIRM_WINDOW_SEC)
            review_window = float(candidate.get("review_window_sec") or confirm_window)
            window = max(confirm_window, review_window)
            event_elapsed = float(event.get("elapsed") or elapsed)
            if event_elapsed < candidate_elapsed:
                continue
            if event_elapsed - candidate_elapsed > window:
                continue
            confirmation = self.confirmation_from_event(candidate, event, event_elapsed - candidate_elapsed)
            if not confirmation:
                continue
            if confirmation.get("review_only"):
                self.write_rule_review(candidate, event_elapsed, confirmation)
                self.pending_candidates.pop(key, None)
                consumed = True
                continue
            self.write_confirmed_active_change(candidate, event, confirmation)
            candidate["confirmed"] = True
            self.pending_candidates.pop(key, None)
            consumed = True
        return consumed

    def confirmation_from_event(self, candidate: dict[str, Any], event: dict[str, Any], elapsed_delta: float = 0.0) -> dict[str, Any] | None:
        if event.get("event_id") == candidate.get("event_id"):
            return None
        candidate_product = candidate.get("product") if isinstance(candidate.get("product"), dict) else {}
        event_product = event.get("product") if isinstance(event.get("product"), dict) else {}
        source = str(event.get("source") or "")
        event_type = str(event.get("type") or "")
        candidate_reason = str(candidate.get("reason") or "")
        confirm_window = float(candidate.get("confirm_window_sec") or ACTIVE_CANDIDATE_CONFIRM_WINDOW_SEC)
        evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
        evidence_text = " ".join(
            str(evidence.get(key) or "")
            for key in ("text", "snippet", "url_masked", "path", "message_type", "raw_type", "dom_title", "dom_text")
        )
        same_id = self.same_product(candidate_product, event_product)
        dom_like_source = source.startswith("dom_mutation") or source.startswith("dom_active_card")
        visible_dom_card = str(evidence.get("current_source") or "") == "visible_dom_product_card"
        if dom_like_source:
            text_confirms = any(term in evidence_text for term in ACTIVE_TEXT_TERMS) or title_matches_text(candidate_product.get("title") or "", evidence_text)
            if same_id and visible_dom_card:
                confidence = 86 if candidate_reason == "network_pop_v3_status_2_candidate" else 85
                return {"confirm_reason": "network_status_2_plus_visible_dom_product_card", "confidence": confidence, "bucket": "dom"}
            if text_confirms:
                return {"confirm_reason": "network_status_2_plus_dom_product_card", "confidence": 85, "bucket": "dom"}
        if source.startswith("network") or event_type == "active_product_manual_or_detail_candidate":
            if same_id:
                if candidate_reason == "network_pop_v3_status_2_candidate":
                    if elapsed_delta <= confirm_window:
                        return {"confirm_reason": "pop_v3_status_2_network_repeat_short_window", "confidence": 85, "bucket": "network_repeat"}
                    return {
                        "confirm_reason": "pop_v3_status_2_network_repeat_long_window_review",
                        "confidence": 75,
                        "bucket": "network_repeat",
                        "review_only": True,
                        "reason": "pop_v3_repeat_without_secondary_confirmation",
                    }
                return {"confirm_reason": "network_status_2_plus_network_repeat", "confidence": 80, "bucket": "network_repeat"}
        if source.startswith("runtime") and same_id:
            return {"confirm_reason": "network_status_2_plus_runtime_global", "confidence": 90, "bucket": "runtime"}
        if source.startswith("im_onMessage") and (same_id or title_matches_text(candidate_product.get("title") or "", evidence_text)):
            return {"confirm_reason": "network_status_2_plus_im", "confidence": 90, "bucket": "im"}
        if source.startswith("websocket") and (same_id or title_matches_text(candidate_product.get("title") or "", evidence_text)):
            return {"confirm_reason": "network_status_2_plus_websocket", "confidence": 90, "bucket": "im"}
        if source.startswith("ocr") and title_matches_text(candidate_product.get("title") or "", evidence_text):
            return {"confirm_reason": "network_status_2_plus_ocr", "confidence": 78, "bucket": "ocr"}
        return None

    def remember_recent_dom_card(self, event: dict[str, Any], elapsed: float) -> None:
        self.recent_dom_card_events.append(dict(event))
        cutoff = float(elapsed) - ACTIVE_POP_SHORT_CONFIRM_WINDOW_SEC
        self.recent_dom_card_events = [
            item
            for item in self.recent_dom_card_events[-50:]
            if float(item.get("elapsed") or 0) >= cutoff
        ]

    def confirm_from_recent_dom_card(self, candidate: dict[str, Any], elapsed: float) -> tuple[dict[str, Any], dict[str, Any]] | None:
        candidate_elapsed = float(candidate.get("candidate_elapsed") or candidate.get("elapsed") or elapsed)
        confirm_window = float(candidate.get("confirm_window_sec") or ACTIVE_CANDIDATE_CONFIRM_WINDOW_SEC)
        for dom_event in reversed(self.recent_dom_card_events):
            dom_elapsed = float(dom_event.get("elapsed") or elapsed)
            delta = abs(dom_elapsed - candidate_elapsed)
            if delta > confirm_window:
                continue
            confirmation = self.confirmation_from_event(candidate, dom_event, delta)
            if confirmation and not confirmation.get("review_only"):
                return dom_event, confirmation
        return None

    @staticmethod
    def same_product(left: dict[str, Any], right: dict[str, Any]) -> bool:
        for field in ("promotion_id", "product_id"):
            left_value = str(left.get(field) or "").strip()
            right_value = str(right.get(field) or "").strip()
            if left_value and right_value and left_value == right_value:
                return True
        return False

    def write_confirmed_active_change(self, candidate: dict[str, Any], evidence_event: dict[str, Any], confirmation: dict[str, Any]) -> None:
        product = candidate.get("product") if isinstance(candidate.get("product"), dict) else {}
        key = product_id_key(product) or product_key(product)
        if not key or key == self.last_active_key:
            return
        self.last_active_key = key
        self.active_change_count += 1
        self.active_product_confirmed_count += 1
        bucket = confirmation.get("bucket")
        if bucket == "dom":
            self.confirmed_by_dom_count += 1
        elif bucket == "network_repeat":
            self.confirmed_by_network_repeat_count += 1
        elif bucket == "runtime":
            self.confirmed_by_runtime_count += 1
        elif bucket == "im":
            self.confirmed_by_im_count += 1
        elif bucket == "ocr":
            self.confirmed_by_ocr_count += 1
        change_event = {
            "type": "active_product_change",
            "ts": evidence_event.get("ts") or now_iso(),
            "elapsed": candidate.get("elapsed"),
            "source": f"{candidate.get('source')}+{evidence_event.get('source')}",
            "reason": candidate.get("reason"),
            "confidence": confirmation.get("confidence"),
            "product": product,
            "product_id": product.get("product_id") or "",
            "promotion_id": product.get("promotion_id") or "",
            "title": product.get("title") or "",
            "start_ms": int(float(candidate.get("elapsed") or 0) * 1000),
            "evidence_event_ids": [candidate.get("event_id"), evidence_event.get("event_id")],
            "confirm_reason": confirmation.get("confirm_reason"),
            "evidence": {
                "candidate": candidate.get("evidence") or {},
                "confirmation": evidence_event.get("evidence") or {},
            },
        }
        self.active_change_events.append(change_event)
        jsonl_write(self.timeline_path, change_event)

    def expire_candidates(self, elapsed: float, force: bool = False) -> None:
        for key, candidate in list(self.pending_candidates.items()):
            if candidate.get("confirmed") or candidate.get("rule_review_written"):
                self.pending_candidates.pop(key, None)
                continue
            deadline = float(candidate.get("review_deadline") or candidate.get("confirm_deadline") or 0)
            if not force and elapsed <= deadline:
                continue
            self.write_rule_review(candidate, elapsed)
            self.pending_candidates.pop(key, None)

    def write_rule_review(self, candidate: dict[str, Any], elapsed: float, confirmation: dict[str, Any] | None = None) -> None:
        if candidate.get("rule_review_written"):
            return
        confirmation = confirmation or {}
        candidate["rule_review_written"] = True
        product = candidate.get("product") if isinstance(candidate.get("product"), dict) else {}
        review_event = {
            "type": "active_product_rule_review",
            "ts": now_iso(),
            "elapsed": round(elapsed, 3),
            "reason": confirmation.get("reason") or "status_2_without_secondary_confirmation",
            "review_required": True,
            "product_id": product.get("product_id") or "",
            "promotion_id": product.get("promotion_id") or "",
            "title": product.get("title") or "",
            "confidence": confirmation.get("confidence") or candidate.get("confidence") or ACTIVE_CANDIDATE_CONFIDENCE,
            "confirm_reason": confirmation.get("confirm_reason") or "",
            "product": product,
            "candidate_event_id": candidate.get("event_id"),
            "candidate_elapsed": candidate.get("elapsed"),
            "evidence": candidate.get("evidence") or {},
        }
        self.active_product_rule_review_count += 1
        self.rule_review_events.append(review_event)
        jsonl_write(self.probe_path, review_event)
        jsonl_write(self.timeline_path, review_event)

    def write_manual_marker(self, elapsed: float, note: str) -> None:
        wall_time = now_iso()
        marker = {
            "type": "manual_observed_product_switch",
            "event": "manual_observed_product_switch",
            "ts": wall_time,
            "wall_time": wall_time,
            "ts_ms": int(elapsed * 1000),
            "elapsed": round(elapsed, 3),
            "note": note.strip() or "manual observed product switch",
        }
        self.manual_markers.append(marker)
        jsonl_write(self.probe_path, marker)
        jsonl_write(
            self.timeline_path,
            marker,
        )

    def write_unresolved(self, elapsed: float, sources_checked: list[str], note: str = "") -> None:
        self.unresolved_count += 1
        jsonl_write(
            self.timeline_path,
            {
                "type": "active_product_unresolved",
                "ts": now_iso(),
                "elapsed": round(elapsed, 3),
                "sources_checked": sources_checked,
                "candidate_signal_count": self.signal_count,
                "strong_signal_count": self.strong_signal_count,
                "note": note,
            },
        )


def drain_runtime_events(client: CDPClient, elapsed: float) -> list[dict[str, Any]]:
    try:
        drained = client.evaluate(DRAIN_JS, timeout=8)
    except Exception as exc:
        return [
            {
                "type": "probe_error",
                "ts": now_iso(),
                "elapsed": round(elapsed, 3),
                "source": "runtime_drain",
                "error": str(exc),
            }
        ]
    if not isinstance(drained, list):
        return []
    events: list[dict[str, Any]] = []
    for item in drained:
        if isinstance(item, dict):
            normalized = normalize_probe_event(item, elapsed)
            if normalized:
                events.append(normalized)
    return events


def poll_runtime(client: CDPClient, elapsed: float) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        result = client.evaluate(POLL_JS, timeout=12)
        events.append(
            {
                "type": "probe_poll",
                "ts": now_iso(),
                "elapsed": round(elapsed, 3),
                "source": "runtime_poll",
                "result": result if isinstance(result, dict) else {},
            }
        )
    except Exception as exc:
        events.append(
            {
                "type": "probe_error",
                "ts": now_iso(),
                "elapsed": round(elapsed, 3),
                "source": "runtime_poll",
                "error": str(exc),
            }
        )
    events.extend(drain_runtime_events(client, elapsed))
    return events


def poll_dom_active_card(client: CDPClient, elapsed: float) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        result = client.evaluate(ACTIVE_CARD_JS, timeout=8)
    except Exception as exc:
        return [
            {
                "type": "probe_error",
                "ts": now_iso(),
                "elapsed": round(elapsed, 3),
                "source": "dom_active_card",
                "error": str(exc),
            }
        ]
    cards = result.get("cards") if isinstance(result, dict) else []
    if not isinstance(cards, list) or not cards:
        return events
    try:
        catalog = snapshot_catalog(client)
    except Exception:
        catalog = {"products": []}
    products = catalog.get("products") if isinstance(catalog, dict) else []
    products = [item for item in products if isinstance(item, dict)]
    resolved: list[tuple[bool, int, dict[str, Any], dict[str, Any]]] = []
    for card in cards[:20]:
        if not isinstance(card, dict):
            continue
        title = str(card.get("title") or "").strip()
        if not title:
            continue
        best_product: dict[str, Any] = {}
        best_score = 0
        for item in products:
            score = title_match_score(title, str(item.get("title") or ""))
            if score > best_score:
                best_score = score
                best_product = item
        product = normalize_product(best_product) if best_score >= 90 else {
            "product_id": "",
            "promotion_id": "",
            "shop_id": "",
            "title": title,
            "detail_url": "",
            "index": str(card.get("index") or ""),
            "status": "",
            "explain_type": "",
        }
        if product and not product.get("title"):
            product["title"] = title
        active_marker = bool(card.get("active_marker"))
        has_ids = product_has_ids(product) and active_marker
        resolved.append(
            (
                active_marker,
                bool(has_ids),
                best_score,
                card,
                {
                    "type": "candidate_signal",
                    "ts": now_iso(),
                    "elapsed": round(elapsed, 3),
                    "source": "dom_active_card_catalog_match" if has_ids else "dom_active_card",
                    "reason": "dom_active_card_catalog_title_match" if has_ids else "dom_active_card_title_only",
                    "confidence": 92 if has_ids else 62,
                    "is_strong": bool(has_ids),
                    "needs_confirm": not has_ids,
                    "product": product,
                    "evidence": {
                        "dom_title": title,
                        "dom_index": str(card.get("index") or ""),
                        "dom_price": str(card.get("price") or ""),
                        "dom_text": str(card.get("text") or "")[:500],
                        "dom_rect": card.get("rect") or {},
                        "dom_active_marker": active_marker,
                        "catalog_match_score": best_score,
                        "catalog_match_count": len(products),
                        "catalog_as_current_source": False,
                        "current_source": "visible_dom_product_card",
                    },
                },
            )
        )
    resolved.sort(key=lambda item: (not item[0], not item[1], -item[2], len(str(item[3].get("text") or ""))))
    for _, _, _, _, event in resolved[:2]:
        events.append(event)
    return events


class ManualMarkerReader:
    """Poll stdin without a blocking daemon thread.

    The Web UI sends marker notes through stdin. A background input() thread can
    crash Python during interpreter shutdown on Windows, so marker input is
    drained opportunistically inside the main probe loop instead.
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.closed = False
        self._byte_buffer = bytearray()
        self._console_buffer: list[str] = []
        try:
            self._fd = sys.stdin.fileno()
        except Exception:
            self._fd = -1
            self.enabled = False
        self._is_tty = bool(self.enabled and sys.stdin.isatty())

    def drain_notes(self) -> list[str]:
        if not self.enabled or self.closed or self._fd < 0:
            return []
        if os.name == "nt":
            if self._is_tty:
                return self._drain_windows_console()
            return self._drain_windows_pipe()
        return self._drain_posix()

    def _drain_windows_console(self) -> list[str]:
        notes: list[str] = []
        try:
            import msvcrt
        except Exception:
            self.enabled = False
            return notes
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                if msvcrt.kbhit():
                    msvcrt.getwch()
                continue
            if ch in ("\r", "\n"):
                notes.append("".join(self._console_buffer).strip())
                self._console_buffer.clear()
            elif ch in ("\b", "\x7f"):
                if self._console_buffer:
                    self._console_buffer.pop()
            elif ch == "\x03":
                raise KeyboardInterrupt
            else:
                self._console_buffer.append(ch)
        return notes

    def _drain_windows_pipe(self) -> list[str]:
        notes: list[str] = []
        try:
            import ctypes
            import msvcrt

            available = ctypes.c_ulong(0)
            ok = ctypes.windll.kernel32.PeekNamedPipe(
                msvcrt.get_osfhandle(self._fd),
                None,
                0,
                None,
                ctypes.byref(available),
                None,
            )
            if not ok:
                self.closed = True
                return notes
            if available.value <= 0:
                return notes
            chunk = os.read(self._fd, min(int(available.value), 4096))
        except OSError:
            self.closed = True
            return notes
        except Exception:
            self.enabled = False
            return notes
        if not chunk:
            self.closed = True
            return notes
        self._byte_buffer.extend(chunk)
        notes.extend(self._pop_buffered_lines())
        return notes

    def _drain_posix(self) -> list[str]:
        notes: list[str] = []
        try:
            import select

            while True:
                ready, _, _ = select.select([sys.stdin], [], [], 0)
                if not ready:
                    break
                line = sys.stdin.readline()
                if line == "":
                    self.closed = True
                    break
                notes.append(line.rstrip("\r\n"))
        except Exception:
            self.enabled = False
        return notes

    def _pop_buffered_lines(self) -> list[str]:
        notes: list[str] = []
        while True:
            newline = self._byte_buffer.find(b"\n")
            if newline < 0:
                break
            raw = bytes(self._byte_buffer[:newline])
            del self._byte_buffer[: newline + 1]
            notes.append(raw.rstrip(b"\r").decode("utf-8", errors="replace").strip())
        return notes


def drain_manual_markers(reader: ManualMarkerReader, recorder: ProbeRecorder, started_at: float) -> bool:
    stop_requested = False
    for note in reader.drain_notes():
        if is_control_stop_note(note):
            stop_requested = True
            continue
        recorder.write_manual_marker(time.time() - started_at, note)
    return stop_requested


def signal_source_buckets(source_counts: dict[str, int]) -> dict[str, int]:
    buckets = {name: 0 for name in SIGNAL_SOURCE_BUCKETS}
    for source, count in source_counts.items():
        for bucket_name, prefixes in SIGNAL_SOURCE_BUCKETS.items():
            if any(source == prefix or source.startswith(prefix) for prefix in prefixes):
                buckets[bucket_name] += count
                break
    return buckets


def match_manual_markers(
    manual_markers: list[dict[str, Any]],
    active_changes: list[dict[str, Any]],
    window_sec: float,
) -> tuple[int, list[dict[str, Any]]]:
    matches: list[dict[str, Any]] = []
    for marker in manual_markers:
        marker_elapsed = float(marker.get("elapsed") or 0)
        best: dict[str, Any] | None = None
        best_delta = window_sec + 1
        for change in active_changes:
            change_elapsed = float(change.get("elapsed") or 0)
            delta = abs(change_elapsed - marker_elapsed)
            if delta <= window_sec and delta < best_delta:
                best = change
                best_delta = delta
        if best:
            matches.append(
                {
                    "manual_elapsed": marker_elapsed,
                    "active_change_elapsed": best.get("elapsed"),
                    "delta_sec": round(best_delta, 3),
                    "manual_note": marker.get("note") or "",
                    "active_source": best.get("source"),
                    "active_reason": best.get("reason"),
                    "product": best.get("product") or {},
                }
            )
    return len(matches), matches


def write_probe_report(
    report_path: Path,
    summary: dict[str, Any],
    probe_path: Path,
    timeline_path: Path,
    review_segments_path: Path,
    catalog_path: Path,
    catalog_enabled: bool,
) -> None:
    outcome = "active_product_change captured" if summary.get("active_product_changed") else "active_product_unresolved"
    lines = [
        "# Douyin Active Product Probe Report",
        "",
        "## Run",
        "",
        f"- Created at: {summary.get('created_at')}",
        f"- Original room URL: {summary.get('original_room_url') or summary.get('room_url')}",
        f"- Room URL: {summary.get('room_url')}",
        f"- Duration sec: {summary.get('duration_sec')}",
        f"- Monitor elapsed sec: {summary.get('monitor_elapsed_sec')}",
        f"- Stream found: {summary.get('stream_found')}",
        f"- Stream quality: {summary.get('stream_quality') or 'N/A'}",
        f"- Stream candidates: {summary.get('stream_candidate_count') or 0}",
        f"- Stream probe enabled: {summary.get('stream_probe_enabled')}",
        f"- Stream probe count: {summary.get('stream_probe_count') or 0}",
        f"- Selected stream probe ok: {summary.get('stream_quality_probe_ok')}",
        f"- Minimum stream quality: {summary.get('min_stream_quality') or 'auto'}",
        f"- Quality accepted: {summary.get('quality_accepted')}",
        f"- Recording output: {summary.get('recording_output') or 'N/A'}",
        f"- Recording blocked reason: {summary.get('recording_blocked_reason') or 'N/A'}",
        "",
        "## Stream Candidates",
        "",
    ]
    stream_candidates = summary.get("stream_candidates") or []
    if stream_candidates:
        for item in stream_candidates[:10]:
            resolution = ""
            if item.get("width") and item.get("height"):
                resolution = f" / {item.get('width')}x{item.get('height')}"
            named = ""
            if item.get("named_quality") and item.get("named_quality") != item.get("quality"):
                named = f" / name:{item.get('named_quality')}"
            probe = ""
            if "probe_ok" in item:
                probe = f" / probe:{'ok' if item.get('probe_ok') else 'failed'}"
            lines.append(
                f"- {item.get('quality') or 'unknown'} / {item.get('format') or 'unknown'}"
                f"{resolution}{named}{probe} / {item.get('masked_url') or ''}"
            )
    else:
        lines.append("- N/A")
    lines.extend(
        [
            "",
        "## Product Rule",
        "",
        "- Catalog is metadata only and was not used as active-product evidence.",
        "- Only strong active signals can write active_product_change.",
        "- If no strong signal is captured, fallback stays pending_user_confirm without product binding.",
        "",
        "## Signal Summary",
        "",
        f"- Outcome: {outcome}",
        f"- Candidate signals: {summary.get('candidate_signal_count')}",
        f"- Weak signals: {summary.get('weak_signal_count')}",
        f"- Strong signals: {summary.get('strong_signal_count')}",
        f"- Active product changes: {summary.get('active_product_change_count')}",
        f"- Active product candidates: {summary.get('active_product_candidate_count')}",
        f"- Status=2 candidates: {summary.get('status_2_candidate_count')}",
        f"- Confirmed candidates: {summary.get('active_product_confirmed_count')}",
        f"- Rule review candidates: {summary.get('active_product_rule_review_count')}",
        f"- Detail candidates: {summary.get('detail_candidate_count')}",
        f"- Catalog-only weak candidates: {summary.get('catalog_only_count')}",
        f"- Max confidence: {summary.get('max_confidence')}",
        f"- Unresolved reason: {summary.get('unresolved_reason') or 'N/A'}",
        "",
        "## Candidate Confirmation",
        "",
        f"- Confirmed by DOM: {summary.get('confirmed_by_dom_count')}",
        f"- Confirmed by network repeat: {summary.get('confirmed_by_network_repeat_count')}",
        f"- Confirmed by runtime/global: {summary.get('confirmed_by_runtime_count')}",
        f"- Confirmed by IM/WebSocket: {summary.get('confirmed_by_im_count')}",
        f"- Confirmed by OCR: {summary.get('confirmed_by_ocr_count')}",
        "",
        "### Captured Candidates",
        "",
        ]
    )
    candidates = summary.get("active_product_candidates") or []
    if candidates:
        for candidate in candidates[:20]:
            product = candidate.get("product") or {}
            evidence = candidate.get("evidence") or {}
            lines.append(
                f"- {candidate.get('elapsed')}s {candidate.get('reason')} "
                f"confidence={candidate.get('confidence')} confirmed_window={candidate.get('confirm_window_sec')} "
                f"product_id={product.get('product_id') or ''} promotion_id={product.get('promotion_id') or ''} "
                f"title={product.get('title') or ''} path={evidence.get('path') or ''}"
            )
    else:
        lines.append("- N/A")
    lines.extend(
        [
            "",
            "### Confirmed Active Changes",
            "",
        ]
    )
    changes = summary.get("active_product_changes") or []
    if changes:
        for change in changes[:20]:
            product = change.get("product") or {}
            ids = ", ".join(str(item) for item in (change.get("evidence_event_ids") or []) if item)
            lines.append(
                f"- {change.get('elapsed')}s confidence={change.get('confidence')} "
                f"confirm_reason={change.get('confirm_reason') or change.get('reason') or ''} "
                f"product_id={product.get('product_id') or change.get('product_id') or ''} "
                f"promotion_id={product.get('promotion_id') or change.get('promotion_id') or ''} "
                f"title={product.get('title') or change.get('title') or ''} evidence_event_ids={ids}"
            )
    else:
        lines.append("- N/A")
    lines.extend(
        [
            "",
            "### Rule Review",
            "",
        ]
    )
    reviews = summary.get("active_product_rule_reviews") or []
    if reviews:
        for review in reviews[:20]:
            lines.append(
                f"- {review.get('candidate_elapsed')}s {review.get('reason')} "
                f"review_required={review.get('review_required')} product_id={review.get('product_id') or ''} "
                f"promotion_id={review.get('promotion_id') or ''} title={review.get('title') or ''}"
            )
    else:
        lines.append("- N/A")
    lines.extend(
        [
            "",
        "## Signal Sources",
        "",
        ]
    )
    for source, count in (summary.get("signal_sources") or {}).items():
        lines.append(f"- {source}: {count}")
    lines.extend(
        [
            "",
            "## Manual Markers",
            "",
            f"- Manual switch markers: {summary.get('manual_switch_marker_count')}",
            f"- Matched manual switches: {summary.get('matched_manual_switch_count')}",
            f"- Match window sec: {summary.get('manual_match_window_sec')}",
            "",
        ]
    )
    manual_matches = summary.get("manual_matches") or []
    if manual_matches:
        lines.append("### Matches")
        lines.append("")
        for match in manual_matches[:20]:
            product = match.get("product") or {}
            product_key_text = product.get("promotion_id") or product.get("product_id") or product.get("detail_url") or ""
            lines.append(
                f"- manual {match.get('manual_elapsed')}s -> active {match.get('active_change_elapsed')}s "
                f"(delta {match.get('delta_sec')}s, {match.get('active_source')}/{match.get('active_reason')}, {product_key_text})"
            )
        lines.append("")
    lines.extend(
        [
            "## Output Files",
            "",
            f"- Probe JSONL: {probe_path}",
            f"- Timeline JSONL: {timeline_path}",
            f"- Summary JSON: {summary.get('summary_path')}",
            f"- Review segments: {review_segments_path}",
            f"- Catalog JSONL: {catalog_path if catalog_enabled else 'N/A'}",
            "",
            "## Review Guidance",
            "",
        ]
    )
    if summary.get("active_product_changed"):
        lines.append("Review active_product_change rows in the timeline and compare them with manual markers/video.")
    else:
        lines.append("Review manual markers and video moments. If markers exist but no strong signals exist, the Web page may not expose active product in the monitored sources for those events.")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run() -> int:
    args = parse_args()
    original_url = args.url
    try:
        resolved_url = resolve_douyin_short_url(args.url)
        if resolved_url and resolved_url != args.url:
            log(f"Resolved Douyin short link: {args.url} -> {resolved_url}")
            args.url = resolved_url
    except Exception as exc:  # noqa: BLE001 - best-effort short-link expansion
        log(f"Douyin short link resolve skipped: {exc}")
    out_dir = Path(args.output_dir).expanduser() if args.output_dir else Path.home() / "Videos" / "LiveClipperActiveProbe"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    room_name = safe_stem(args.room_name or room_id_from_url(args.url) or "douyin_live")
    base = out_dir / f"{room_name}_{stamp}"
    timeline_path = base.with_suffix(".timeline.jsonl")
    probe_path = base.with_suffix(".active_product_probe.jsonl")
    summary_path = base.with_suffix(".probe_summary.json")
    review_segments_path = base.with_suffix(".review_segments.json")
    catalog_path = base.with_suffix(".catalog.jsonl")
    report_path = base.with_suffix(".active_product_probe_report.md")

    version = prepare_chrome(args.port, args.url)
    target, opened_tab_for_probe = ensure_live_tab(args.port, args.url)
    log(f"Connected Chrome: {version.get('Browser', 'Chrome')}")
    log(f"Using tab: {target.get('title') or target.get('url')}")

    client = CDPClient(str(target["webSocketDebuggerUrl"]))
    ffmpeg_proc: subprocess.Popen[Any] | None = None
    output_path: Path | None = None
    stderr_path: Path | None = None
    request_urls: dict[str, str] = {}
    response_meta: dict[str, dict[str, Any]] = {}
    recorder = ProbeRecorder(probe_path, timeline_path)
    started_at = time.time()
    run_started_at = started_at
    requested_manual_markers = (not args.no_manual_markers) and (args.manual_markers or sys.stdin.isatty())
    manual_reader = ManualMarkerReader(requested_manual_markers)
    manual_markers_enabled = manual_reader.enabled
    if manual_markers_enabled:
        log("Manual marker enabled: type a note and press Enter when you see a product switch.")
    elif requested_manual_markers:
        log("Manual marker disabled: stdin is not readable.")
    stream_url = ""
    stream_candidates: list[str] = []
    stream_probe_info: dict[str, dict[str, Any]] = {}
    recording_blocked_reason = ""
    min_stream_quality = normalize_min_stream_quality(args.min_stream_quality)
    wait_for_hd_stream = QUALITY_RANKS.get(min_stream_quality, 0) >= QUALITY_RANKS["1080p"]
    last_unresolved_at = 0.0
    last_catalog_at = 0.0

    summary: dict[str, Any] = {
        "created_at": now_iso(),
        "original_room_url": original_url,
        "room_url": args.url,
        "target_url": target.get("url"),
        "target_title": target.get("title"),
        "target_id": target.get("id"),
        "chrome_tab_opened_by_probe": opened_tab_for_probe,
        "chrome": version.get("Browser"),
        "probe_path": str(probe_path),
        "timeline_path": str(timeline_path),
        "summary_path": str(summary_path),
        "report_path": str(report_path),
        "review_segments_path": str(review_segments_path),
        "catalog_path": str(catalog_path) if args.capture_catalog else "",
        "recording_output": "",
        "recording_log": "",
        "stream_found": False,
        "stream_masked": "",
        "stream_quality": "",
        "stream_candidate_count": 0,
        "stream_candidates": [],
        "stream_probe_enabled": False,
        "stream_probe_count": 0,
        "min_stream_quality": min_stream_quality,
        "quality_accepted": True,
        "recording_blocked_reason": "",
        "setup_elapsed_sec": 0.0,
        "duration_sec": 0.0,
        "requested_duration_sec": args.seconds,
        "monitor_elapsed_sec": 0.0,
        "duration_seconds": 0.0,
        "candidate_signal_count": 0,
        "weak_signal_count": 0,
        "strong_signal_count": 0,
        "active_product_change_count": 0,
        "active_product_changed": False,
        "active_product_candidate_count": 0,
        "status_2_candidate_count": 0,
        "active_product_confirmed_count": 0,
        "active_product_rule_review_count": 0,
        "confirmed_by_dom_count": 0,
        "confirmed_by_network_repeat_count": 0,
        "confirmed_by_runtime_count": 0,
        "confirmed_by_im_count": 0,
        "confirmed_by_ocr_count": 0,
        "detail_candidate_count": 0,
        "catalog_only_count": 0,
        "active_product_candidates": [],
        "active_product_rule_reviews": [],
        "active_product_changes": [],
        "unresolved_count": 0,
        "unresolved_reason": "not_finished",
        "max_confidence": 0,
        "signal_sources": {name: 0 for name in SIGNAL_SOURCE_BUCKETS},
        "source_counts": {},
        "strong_source_counts": {},
        "manual_markers_enabled": manual_markers_enabled,
        "manual_switch_marker_count": 0,
        "matched_manual_switch_count": 0,
        "manual_match_window_sec": args.manual_match_window,
        "manual_matches": [],
        "fallback_review_segment": None,
    }

    try:
        client.call("Runtime.enable", timeout=5)
        client.call("Page.enable", timeout=5)
        client.call("Network.enable", timeout=5)
        install_result = client.evaluate(PROBE_INSTALL_JS, timeout=20)
        jsonl_write(
            probe_path,
            {
                "type": "probe_install",
                "ts": now_iso(),
                "elapsed": 0,
                "result": install_result if isinstance(install_result, dict) else {},
            },
        )

        if args.open_catalog:
            # Catalog is optional metadata only. This script never turns it into active_product.
            try:
                client.evaluate(
                    "(() => { const n = document.querySelector('[data-e2e=\"yellowCart-container\"]'); if (n) n.dispatchEvent(new MouseEvent('click', {bubbles:true,cancelable:true,view:window})); return !!n; })()",
                    timeout=5,
                )
                time.sleep(2.0)
            except Exception:
                pass

        page_state = get_page_state(client)
        validate_target_room(target, args.url, page_state)
        if page_state.get("liveEnded"):
            log("Page state says the live has ended or is not live.")
        else:
            def record_setup_network_event(event: dict[str, Any]) -> None:
                # Stream discovery happens before ffmpeg starts; product signals
                # found here describe the active item at the beginning of the
                # recording, so anchor them to video time zero.
                elapsed = 0.0
                for probe_event in process_network_event(client, event, elapsed, request_urls, response_meta):
                    stored_event = recorder.write_probe(probe_event)
                    if stored_event:
                        recorder.process_event(stored_event, elapsed)

            log("Looking for live stream URL...")
            stream_candidates = collect_stream_candidates(
                client,
                args.stream_wait_seconds,
                wait_for_hd=wait_for_hd_stream,
                event_callback=record_setup_network_event,
            )
            stream_url = stream_candidates[0] if stream_candidates else ""
            page_state = get_page_state(client)
            validate_target_room(target, args.url, page_state)
            if not stream_url and page_has_playing_video(page_state):
                log("Page is already playing through a blob URL; reloading once to recapture stream request...")
                try:
                    client.call("Page.reload", {"ignoreCache": False}, timeout=5)
                    time.sleep(4.0)
                    client.evaluate(PROBE_INSTALL_JS, timeout=20)
                except Exception as exc:
                    log(f"Reload skipped: {exc}")
                page_state = get_page_state(client)
                validate_target_room(target, args.url, page_state)
                stream_candidates = collect_stream_candidates(
                    client,
                    args.stream_wait_seconds,
                    wait_for_hd=wait_for_hd_stream,
                    event_callback=record_setup_network_event,
                )
                stream_url = stream_candidates[0] if stream_candidates else ""
                page_state = get_page_state(client)
                validate_target_room(target, args.url, page_state)

        should_probe_streams = bool(stream_candidates) and (
            wait_for_hd_stream
            or any(stream_quality_label(url) == "未知清晰度" for url in stream_candidates[:8])
        )
        if should_probe_streams:
            log("Probing stream candidate resolutions with ffprobe...")
            stream_candidates, stream_probe_info = rank_stream_candidates_with_probe(stream_candidates, max_probe=8)
            stream_url = stream_candidates[0] if stream_candidates else ""

        started_at = time.time()
        summary["setup_elapsed_sec"] = round(started_at - run_started_at, 3)
        summary["stream_found"] = bool(stream_url)
        summary["stream_masked"] = mask_url(stream_url) if stream_url else ""
        stream_candidate_report = describe_stream_candidates(stream_candidates, stream_probe_info)
        selected_probe_info = stream_probe_info.get(stream_url) if stream_url else None
        selected_quality = effective_stream_quality_label(stream_url, selected_probe_info) if stream_url else ""
        quality_accepted = True if not stream_url else stream_quality_meets_min(
            selected_quality,
            min_stream_quality,
            stream_url,
            selected_probe_info,
        )
        summary["stream_quality"] = selected_quality
        summary["stream_candidate_count"] = len({url for url in stream_candidates if url})
        summary["stream_candidates"] = stream_candidate_report
        summary["stream_probe_enabled"] = bool(stream_probe_info)
        summary["stream_probe_count"] = len(stream_probe_info)
        summary["stream_quality_probe_ok"] = bool(selected_probe_info and selected_probe_info.get("ok"))
        summary["quality_accepted"] = quality_accepted
        jsonl_write(
            timeline_path,
            {
                "type": "probe_start",
                "ts": now_iso(),
                "room_url": args.url,
                "target_url": target.get("url"),
                "stream_found": bool(stream_url),
                "stream_masked": mask_url(stream_url) if stream_url else "",
                "stream_quality": selected_quality,
                "stream_candidate_count": len({url for url in stream_candidates if url}),
                "stream_candidates": stream_candidate_report,
                "stream_probe_enabled": bool(stream_probe_info),
                "stream_probe_count": len(stream_probe_info),
                "stream_quality_probe_ok": bool(selected_probe_info and selected_probe_info.get("ok")),
                "min_stream_quality": min_stream_quality,
                "quality_accepted": quality_accepted,
                "catalog_as_active_source": False,
                "active_signal_required": True,
            },
        )

        if stream_candidate_report:
            labels = ", ".join(
                (
                    f"{item.get('quality')}/{item.get('format')}"
                    + (f"/{item.get('width')}x{item.get('height')}" if item.get("width") and item.get("height") else "")
                )
                for item in stream_candidate_report[:6]
            )
            log(f"Stream candidates captured: {len({url for url in stream_candidates if url})} ({labels})")
        if stream_url:
            log(f"Selected stream quality: {selected_quality}")
        if not stream_url:
            recorder.write_unresolved(0, ["runtime", "dom_mutation", "network", "websocket", "im"], "no_stream_url")
            log("No stream URL was found; live is not recording.")
        elif not quality_accepted:
            recording_blocked_reason = "stream_quality_below_minimum"
            summary["recording_blocked_reason"] = recording_blocked_reason
            recorder.write_unresolved(
                0,
                ["network", "runtime", "dom_mutation", "websocket", "im"],
                recording_blocked_reason,
            )
            if (
                QUALITY_RANKS.get(min_stream_quality, 0) >= QUALITY_RANKS["1080p"]
                and not (selected_probe_info and selected_probe_info.get("ok"))
            ):
                log(f"直播流未实测到 1080p 分辨率：当前 {selected_quality or '未知清晰度'}，最低要求 {min_stream_quality}，未启动录制。")
            else:
                log(f"直播流清晰度低于要求：当前 {selected_quality}，最低要求 {min_stream_quality}，未启动录制。")
            jsonl_write(
                timeline_path,
                {
                    "type": "recording_skipped",
                    "ts": now_iso(),
                    "elapsed": 0,
                    "reason": recording_blocked_reason,
                    "stream_quality": selected_quality,
                    "stream_quality_probe_ok": bool(selected_probe_info and selected_probe_info.get("ok")),
                    "min_stream_quality": min_stream_quality,
                    "stream_masked": mask_url(stream_url),
                },
            )
        elif not args.no_record:
            suffix = output_suffix_for_stream(stream_url)
            output_path = base.with_suffix(suffix)
            stderr_path = base.with_suffix(".ffmpeg.log")
            log(f"Starting ffmpeg recording: {output_path}")
            ffmpeg_proc = start_ffmpeg(find_ffmpeg(), stream_url, output_path, args.seconds, stderr_path)
            summary["recording_output"] = str(output_path)
            summary["recording_log"] = str(stderr_path)
            jsonl_write(
                timeline_path,
                {
                    "type": "recording_start",
                    "ts": now_iso(),
                    "elapsed": 0,
                    "output": str(output_path),
                    "ffmpeg_log": str(stderr_path),
                    "stream_masked": mask_url(stream_url),
                    "seconds": args.seconds,
                },
            )

        next_runtime_poll = 0.0
        next_active_card = 0.0
        next_unresolved = 0.0
        next_catalog = 0.0
        deadline = time.time() if (not stream_url or recording_blocked_reason) else (time.time() + args.seconds if args.seconds > 0 else None)
        stop_requested = False
        while True:
            elapsed = time.time() - started_at
            recorder.expire_partial_active(elapsed)
            if drain_manual_markers(manual_reader, recorder, started_at):
                stop_requested = True
                break
            if ffmpeg_proc and ffmpeg_proc.poll() is not None:
                break
            if deadline and time.time() >= deadline:
                break

            event = client.recv_event(timeout=0.25)
            if event:
                for probe_event in process_network_event(client, event, elapsed, request_urls, response_meta):
                    stored_event = recorder.write_probe(probe_event)
                    if stored_event:
                        recorder.process_event(stored_event, elapsed)

            if time.time() >= next_runtime_poll:
                for probe_event in poll_runtime(client, elapsed):
                    stored_event = recorder.write_probe(probe_event)
                    if stored_event:
                        recorder.process_event(stored_event, elapsed)
                next_runtime_poll = time.time() + max(1.0, args.probe_interval)

            if time.time() >= next_active_card:
                for probe_event in poll_dom_active_card(client, elapsed):
                    stored_event = recorder.write_probe(probe_event)
                    if stored_event:
                        recorder.process_event(stored_event, elapsed)
                next_active_card = time.time() + max(2.0, args.probe_interval)

            if args.capture_catalog and time.time() >= next_catalog:
                try:
                    catalog = snapshot_catalog(client)
                    jsonl_write(catalog_path, {"type": "catalog_snapshot", "ts": now_iso(), "elapsed": round(elapsed, 3), **catalog})
                except Exception as exc:
                    jsonl_write(catalog_path, {"type": "catalog_error", "ts": now_iso(), "elapsed": round(elapsed, 3), "error": str(exc)})
                next_catalog = time.time() + max(2.0, args.probe_interval)

            if time.time() >= next_unresolved:
                if recorder.strong_signal_count == 0:
                    recorder.write_unresolved(
                        elapsed,
                        ["runtime_global", "runtime_react", "dom_mutation", "im_onMessage", "network", "websocket"],
                    )
                next_unresolved = time.time() + max(2.0, args.probe_interval)

        stop_requested = drain_manual_markers(manual_reader, recorder, started_at) or stop_requested
        recorder.expire_candidates(time.time() - started_at, force=True)
        recorder.expire_partial_active(time.time() - started_at, force=True)

        ffmpeg_returncode: int | None = None
        if ffmpeg_proc and ffmpeg_proc.poll() is None:
            ffmpeg_proc.terminate()
            try:
                ffmpeg_proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                ffmpeg_proc.kill()
                try:
                    ffmpeg_proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        if ffmpeg_proc:
            ffmpeg_returncode = ffmpeg_proc.poll()
        close_ffmpeg_log(ffmpeg_proc)

        elapsed_total = time.time() - started_at
        matched_manual_count, manual_matches = match_manual_markers(
            recorder.manual_markers,
            recorder.active_change_events,
            max(0.0, args.manual_match_window),
        )
        signal_sources = signal_source_buckets(recorder.source_counts)
        unresolved_reason = None if recorder.active_change_count > 0 else "no_strong_active_product_signal_captured"
        if not stream_url:
            unresolved_reason = "no_stream_url"
        elif recording_blocked_reason:
            unresolved_reason = recording_blocked_reason
        effective_duration = elapsed_total
        summary["duration_sec"] = round(effective_duration, 3)
        summary["monitor_elapsed_sec"] = round(elapsed_total, 3)
        summary["duration_seconds"] = round(elapsed_total, 3)
        summary["recording_returncode"] = ffmpeg_returncode
        summary["recording_completed"] = ffmpeg_returncode in (None, 0)
        summary["candidate_signal_count"] = recorder.signal_count
        summary["weak_signal_count"] = max(0, recorder.signal_count - recorder.strong_signal_count)
        summary["strong_signal_count"] = recorder.strong_signal_count
        summary["active_product_change_count"] = recorder.active_change_count
        summary["active_product_changed"] = recorder.active_change_count > 0
        summary["active_product_candidate_count"] = recorder.active_product_candidate_count
        summary["status_2_candidate_count"] = recorder.status_2_candidate_count
        summary["active_product_confirmed_count"] = recorder.active_product_confirmed_count
        summary["active_product_rule_review_count"] = recorder.active_product_rule_review_count
        summary["confirmed_by_dom_count"] = recorder.confirmed_by_dom_count
        summary["confirmed_by_network_repeat_count"] = recorder.confirmed_by_network_repeat_count
        summary["confirmed_by_runtime_count"] = recorder.confirmed_by_runtime_count
        summary["confirmed_by_im_count"] = recorder.confirmed_by_im_count
        summary["confirmed_by_ocr_count"] = recorder.confirmed_by_ocr_count
        summary["detail_candidate_count"] = recorder.detail_candidate_count
        summary["catalog_only_count"] = recorder.catalog_only_count
        summary["active_product_candidates"] = recorder.active_product_candidates[:30]
        summary["active_product_rule_reviews"] = recorder.rule_review_events[:30]
        summary["active_product_changes"] = recorder.active_change_events[:30]
        summary["unresolved_count"] = recorder.unresolved_count
        summary["unresolved_reason"] = unresolved_reason
        summary["max_confidence"] = recorder.max_confidence
        summary["signal_sources"] = signal_sources
        summary["source_counts"] = recorder.source_counts
        summary["strong_source_counts"] = recorder.strong_source_counts
        summary["manual_switch_marker_count"] = len(recorder.manual_markers)
        summary["matched_manual_switch_count"] = matched_manual_count
        summary["manual_matches"] = manual_matches
        summary["stop_requested"] = bool(stop_requested)

        if output_path and output_path.exists():
            summary["recording_size"] = output_path.stat().st_size
        if recorder.active_change_count == 0:
            segment_end = round(effective_duration, 3)
            review_segment = {
                "type": "pending_user_confirm",
                "start": 0.0,
                "end": segment_end,
                "product_id": "",
                "promotion_id": "",
                "detail_url": "",
                "reason": "active_product_unresolved",
                "note": "No strong active-product signal was captured; catalog was not used as active-product evidence.",
            }
            review_segments_path.write_text(json.dumps({"segments": [review_segment]}, ensure_ascii=False, indent=2), encoding="utf-8")
            summary["fallback_review_segment"] = review_segment
            jsonl_write(timeline_path, {"type": "pending_user_confirm_segment", "ts": now_iso(), "elapsed": round(elapsed_total, 3), "segment": review_segment})
        else:
            review_segments_path.write_text(json.dumps({"segments": []}, ensure_ascii=False, indent=2), encoding="utf-8")

        jsonl_write(
            timeline_path,
            {
                "type": "probe_stop",
                "ts": now_iso(),
                "elapsed": round(elapsed_total, 3),
                "candidate_signal_count": recorder.signal_count,
                "strong_signal_count": recorder.strong_signal_count,
                "active_product_change_count": recorder.active_change_count,
                "active_product_candidate_count": recorder.active_product_candidate_count,
                "status_2_candidate_count": recorder.status_2_candidate_count,
                "active_product_rule_review_count": recorder.active_product_rule_review_count,
                "manual_switch_marker_count": len(recorder.manual_markers),
                "stop_requested": bool(stop_requested),
            },
        )
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        write_probe_report(
            report_path,
            summary,
            probe_path,
            timeline_path,
            review_segments_path,
            catalog_path,
            args.capture_catalog,
        )
        log(f"Probe summary: {summary_path}")
        log(f"Probe report: {report_path}")
        log(f"Probe events: {probe_path}")
        log(f"Timeline: {timeline_path}")
        return 0
    except KeyboardInterrupt:
        log("Stopping...")
        if ffmpeg_proc and ffmpeg_proc.poll() is None:
            ffmpeg_proc.terminate()
            try:
                ffmpeg_proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                ffmpeg_proc.kill()
        close_ffmpeg_log(ffmpeg_proc)
        return 130
    except Exception as exc:
        log(f"Fatal error: {exc}")
        if ffmpeg_proc and ffmpeg_proc.poll() is None:
            ffmpeg_proc.terminate()
            try:
                ffmpeg_proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                ffmpeg_proc.kill()
        close_ffmpeg_log(ffmpeg_proc)
        return 1
    finally:
        try:
            client.close()
        except Exception:
            pass
        if opened_tab_for_probe and target.get("id"):
            try:
                if close_target(args.port, str(target.get("id") or "")):
                    log("Closed probe Chrome tab.")
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(run())
