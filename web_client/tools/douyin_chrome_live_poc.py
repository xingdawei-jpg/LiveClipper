#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""POC: record a Douyin live room through a logged-in Chrome tab.

This is intentionally independent from the main web UI. It proves three things
before we wire the workflow into LiveClipper:
1. a real logged-in Chrome page can expose a playable live stream URL;
2. the live product list can be snapshotted into JSONL sidecars;
3. ffmpeg can record continuously while the sidecars are being written.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import os
import random
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


DEFAULT_PORT = 9222
DEFAULT_SNAPSHOT_INTERVAL = 5.0
DEFAULT_STREAM_WAIT_SECONDS = 75.0


PRODUCT_CATALOG_JS = r"""
(() => {
  const roots = Array.from(document.querySelectorAll('[data-e2e="live-promotion-list"]'));
  const fallbackNodes = Array.from(document.querySelectorAll('li,[data-e2e],[class*="promotion"],[class*="goods"],[class*="product"]')).slice(0, 800);
  const nodes = roots.length
    ? roots.flatMap((root) => [root, ...Array.from(root.querySelectorAll('*')).slice(0, 1000)])
    : fallbackNodes;
  const out = [];
  const seen = new Set();

  function clean(value) {
    if (value === undefined || value === null) return '';
    if (typeof value === 'number' || typeof value === 'boolean') return String(value);
    if (typeof value !== 'string') return '';
    return value.replace(/\s+/g, ' ').trim();
  }

  function first(obj, keys) {
    if (!obj || typeof obj !== 'object') return '';
    for (const key of keys) {
      if (Object.prototype.hasOwnProperty.call(obj, key) && obj[key] !== undefined && obj[key] !== null) {
        return obj[key];
      }
    }
    return '';
  }

  function addCandidate(obj, source, nodeText) {
    if (!obj || typeof obj !== 'object') return;
    const data = obj.promotion && typeof obj.promotion === 'object' ? obj.promotion : obj;
    const product_id = clean(first(data, ['product_id', 'productId', 'productID', 'commodity_id', 'item_id']));
    const promotion_id = clean(first(data, ['promotion_id', 'promotionId', 'promotionID', 'roomPromotionId', 'id']));
    const shop_id = clean(first(data, ['shop_id', 'shopId', 'shopID']));
    const title = clean(first(data, ['title', 'name', 'product_name', 'productName', 'short_title', 'shortTitle', 'desc']));
    let detail_url = clean(first(data, ['detail_url', 'detailUrl', 'jump_url', 'jumpUrl', 'url', 'schema']));
    detail_url = detail_url.replace(/\\u0026/g, '&');
    const index = clean(first(data, ['index', 'rank', 'rankIndex', 'sequence', 'sort']));
    const statusRaw = clean(first(data, ['status', 'status_text', 'statusText', 'explain_type', 'explainType', 'explain_status', 'explainStatus']));
    const min_price = clean(first(data, ['min_price', 'minPrice', 'price', 'regular_price', 'regularPrice']));
    const cover = clean(first(data, ['cover', 'image', 'image_url', 'imageUrl']));
    const text = clean(nodeText);
    const marker = `${statusRaw} ${text}`;
    const is_explaining = /讲解中|正在讲|正在讲解|讲解|explaining|explain/i.test(marker);

    if (!(product_id || promotion_id || detail_url)) return;
    const key = promotion_id || product_id || detail_url;
    if (!key || seen.has(key)) return;
    seen.add(key);
    out.push({
      product_id,
      promotion_id,
      shop_id,
      title,
      detail_url,
      index,
      status: statusRaw,
      min_price,
      cover,
      is_explaining,
      node_text: text.slice(0, 160),
      source
    });
  }

  function walk(obj, depth, source, nodeText, visited) {
    if (!obj || typeof obj !== 'object' || depth > 7) return;
    if (visited.has(obj)) return;
    visited.add(obj);
    addCandidate(obj, source, nodeText);

    const keys = Object.keys(obj).slice(0, 90);
    for (const key of keys) {
      if (/^(stateNode|ownerDocument|parentNode|parentElement|firstChild|lastChild|nextSibling|previousSibling|return|alternate|child|sibling)$/i.test(key)) {
        continue;
      }
      const value = obj[key];
      if (!value || typeof value !== 'object') continue;
      walk(value, depth + 1, `${source}.${key}`, nodeText, visited);
    }
  }

  for (const node of nodes) {
    const nodeText = clean(node.innerText || node.textContent || '');
    for (const key of Object.keys(node)) {
      if (!key.startsWith('__reactProps$') && !key.startsWith('__reactFiber$')) continue;
      const value = node[key];
      const visited = new WeakSet();
      walk(value, 0, key, nodeText, visited);
      if (value && typeof value === 'object') {
        walk(value.memoizedProps, 0, `${key}.memoizedProps`, nodeText, visited);
        walk(value.pendingProps, 0, `${key}.pendingProps`, nodeText, visited);
        walk(value.memoizedState, 0, `${key}.memoizedState`, nodeText, visited);
      }
    }
  }

  function walkFiber(obj, depth, source, visited, budget) {
    if (!obj || typeof obj !== 'object' || depth > 20 || budget.count > 70000) return;
    if (visited.has(obj)) return;
    visited.add(obj);
    budget.count += 1;
    addCandidate(obj, source, '');

    const keys = Object.keys(obj);
    const priority = [];
    const rest = [];
    for (const key of keys) {
      if (/^(stateNode|ownerDocument|parentNode|parentElement|firstChild|lastChild|nextSibling|previousSibling)$/.test(key)) {
        continue;
      }
      if (/^(memoizedProps|pendingProps|memoizedState|state|props|return|child|sibling|alternate|_owner|children|data|list|items|promotions|promotion|product|commodity|goods|ecom|cart)$/i.test(key)) {
        priority.push(key);
      } else if (/(product|promotion|commodity|goods|detail|schema|shop|room|cart|ecom|title|price)/i.test(key)) {
        priority.push(key);
      } else {
        rest.push(key);
      }
    }
    for (const key of priority.concat(rest.slice(0, 35))) {
      const value = obj[key];
      if (value && typeof value === 'object') {
        walkFiber(value, depth + 1, `${source}.${key}`, visited, budget);
      }
    }
  }

  const fiberRoots = roots.length
    ? roots
    : Array.from(document.querySelectorAll('[data-e2e="__e_commerce__"],[data-e2e="yellowCart-container"],[data-e2e="living-container"]'));
  for (const root of fiberRoots) {
    for (const key of Object.keys(root)) {
      if (!key.startsWith('__reactFiber$') && !key.startsWith('__reactProps$')) continue;
      walkFiber(root[key], 0, `${root.getAttribute('data-e2e') || root.tagName}.${key}`, new WeakSet(), { count: 0 });
    }
  }

  return {
    href: location.href,
    title: document.title,
    hasPromotionRoot: roots.length > 0,
    count: out.length,
    products: out
  };
})()
"""


ACTIVE_PRODUCT_JS = r"""
(() => {
  const candidates = [];
  const seenCandidates = new Set();
  const roots = Array.from(document.querySelectorAll(
    '[data-e2e="living-container"],[data-e2e="yellowCart-container"],[data-e2e="__e_commerce__"],[data-e2e="live-promotion-list"]'
  ));

  function clean(value) {
    if (value === undefined || value === null) return '';
    if (typeof value === 'number' || typeof value === 'boolean') return String(value);
    if (typeof value !== 'string') return '';
    return value.replace(/\s+/g, ' ').trim();
  }

  function first(obj, keys) {
    if (!obj || typeof obj !== 'object') return '';
    for (const key of keys) {
      if (Object.prototype.hasOwnProperty.call(obj, key) && obj[key] !== undefined && obj[key] !== null) {
        return obj[key];
      }
    }
    return '';
  }

  function productFrom(obj) {
    if (!obj || typeof obj !== 'object') return null;
    const data = obj.promotion && typeof obj.promotion === 'object' ? obj.promotion : obj;
    let detail_url = clean(first(data, ['detail_url', 'detailUrl', 'jump_url', 'jumpUrl', 'url', 'schema']));
    detail_url = detail_url.replace(/\\u0026/g, '&');
    const product = {
      product_id: clean(first(data, ['product_id', 'productId', 'productID', 'commodity_id', 'commodityId', 'item_id'])),
      promotion_id: clean(first(data, ['promotion_id', 'promotionId', 'promotionID', 'roomPromotionId', 'id'])),
      shop_id: clean(first(data, ['shop_id', 'shopId', 'shopID'])),
      title: clean(first(data, ['title', 'name', 'product_name', 'productName', 'short_title', 'shortTitle', 'desc'])),
      detail_url,
      index: clean(first(data, ['index', 'real_index', 'rank', 'rankIndex', 'sequence', 'sort'])),
      status: clean(first(data, ['status', 'status_text', 'statusText'])),
      explain_type: clean(first(data, ['explain_type', 'explainType', 'explain_status', 'explainStatus'])),
      source_keys: Object.keys(data).slice(0, 50)
    };
    const isProductUrl = /ecom\.douyin\.com|ecommerce|trade\/detail/.test(product.detail_url);
    if (!(product.product_id || product.promotion_id || isProductUrl || product.title)) return null;
    return product;
  }

  function productKey(product) {
    if (!product) return '';
    return product.promotion_id || product.product_id || product.detail_url || product.title || '';
  }

  function addCandidate(source, confidence, product, reason, extra = {}) {
    if (!product) return;
    const key = `${source}:${productKey(product)}:${reason}`;
    if (!productKey(product) || seenCandidates.has(key)) return;
    seenCandidates.add(key);
    candidates.push({
      source,
      confidence,
      reason,
      product,
      extra
    });
  }

  function visibleTextMatches(regex) {
    const nodes = Array.from(document.querySelectorAll('div,span,button,li'))
      .filter((node) => {
        const rect = node.getBoundingClientRect();
        return rect && rect.width > 0 && rect.height > 0;
      });
    return nodes
      .map((node) => ({ node, text: clean(node.innerText || node.textContent || '') }))
      .filter((item) => regex.test(item.text));
  }

  for (const item of visibleTextMatches(/正在讲解|讲解中|当前讲解|正在介绍|本场讲解/)) {
    let node = item.node;
    for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
      const text = clean(node.innerText || node.textContent || '');
      const titleNode = node.querySelector && node.querySelector('[data-e2e="promotion-title"]');
      const title = titleNode ? clean(titleNode.innerText || titleNode.textContent || '') : '';
      if (title || text) {
        addCandidate('dom_explaining_badge', 45, { title: title || text.slice(0, 120) }, item.text);
        break;
      }
    }
  }

  const knownProducts = new Map();
  function rememberProduct(product) {
    if (!product) return;
    for (const key of [product.promotion_id, product.product_id, product.detail_url, product.title]) {
      if (key) knownProducts.set(String(key), product);
    }
  }

  const seen = new WeakSet();
  let visited = 0;
  function inspect(obj, source) {
    if (!obj || typeof obj !== 'object') return;
    const product = productFrom(obj);
    if (product) rememberProduct(product);

    const currentPromotionId = clean(first(obj, ['currentPromotionId', 'current_promotion_id', 'currentPromotionID']));
    if (currentPromotionId) {
      addCandidate(
        'react_current_promotion_id',
        95,
        knownProducts.get(currentPromotionId) || { promotion_id: currentPromotionId },
        'currentPromotionId',
        { currentPromotionId }
      );
    }

    for (const key of ['defaultPromotion', 'default_promotion', 'currentPromotion', 'current_promotion', 'openDetailPromotion', 'open_detail_promotion', 'explainingPromotion', 'explainPromotion']) {
      const value = obj[key];
      if (value && typeof value === 'object') {
        addCandidate(`react_${key}`, 90, productFrom(value), key);
      } else if (value) {
        const id = clean(value);
        addCandidate(`react_${key}`, 75, knownProducts.get(id) || { promotion_id: id }, key, { id });
      }
    }

    const explainType = clean(first(obj, ['explain_type', 'explainType', 'explain_status', 'explainStatus']));
    if (product && explainType && explainType !== '0') {
      addCandidate('react_explain_type', 80, product, `explain_type=${explainType}`);
    }
    if (product && (obj.is_explaining || obj.isExplaining || obj.is_current || obj.isCurrent)) {
      addCandidate('react_explaining_flag', 80, product, 'boolean_explaining_flag');
    }
  }

  function walk(obj, path, depth) {
    if (!obj || typeof obj !== 'object' || depth > 20 || visited > 70000 || candidates.length > 80) return;
    if (seen.has(obj)) return;
    seen.add(obj);
    visited += 1;
    inspect(obj, path);

    const keys = Object.keys(obj);
    const priority = [];
    const rest = [];
    for (const key of keys) {
      if (/^(stateNode|ownerDocument|parentNode|parentElement|firstChild|lastChild|nextSibling|previousSibling|socket|xhr|transport|player|video)$/.test(key)) {
        continue;
      }
      if (/^(memoizedProps|pendingProps|memoizedState|state|props|return|child|sibling|alternate|_owner|children|data|list|items|promotions|promotion|product|commodity|goods|ecom|cart|currentPromotion|defaultPromotion|openDetailPromotion|messageInstance)$/i.test(key)) {
        priority.push(key);
      } else if (/(promotion|product|commodity|goods|detail|schema|shop|room|cart|ecom|title|price|current|default|explain)/i.test(key)) {
        priority.push(key);
      } else {
        rest.push(key);
      }
    }
    for (const key of priority.concat(rest.slice(0, 35))) {
      const value = obj[key];
      if (value && typeof value === 'object') {
        walk(value, `${path}.${key}`, depth + 1);
      }
    }
  }

  for (const root of roots) {
    for (const key of Object.keys(root)) {
      if (!key.startsWith('__reactFiber$') && !key.startsWith('__reactProps$')) continue;
      walk(root[key], `${root.getAttribute('data-e2e') || root.tagName}.${key}`, 0);
    }
  }

  candidates.sort((a, b) => b.confidence - a.confidence);
  return {
    ts: Date.now(),
    visited,
    active: candidates[0] || null,
    candidates: candidates.slice(0, 10),
    candidate_count: candidates.length
  };
})()
"""


RESOURCE_URLS_JS = r"""
(() => Array.from(new Set(
  performance.getEntriesByType('resource')
    .map((entry) => entry && entry.name)
    .filter(Boolean)
)).slice(-2000))()
"""


TRY_OPEN_PRODUCTS_JS = r"""
(() => {
  if (document.querySelector('[data-e2e="live-promotion-list"]')) {
    return { clicked: false, open: true, reason: 'already-open' };
  }
  const direct = document.querySelector('[data-e2e="yellowCart-container"]');
  if (direct) {
    direct.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
    return { clicked: true, open: false, reason: 'yellow-cart', text: (direct.innerText || direct.textContent || '').trim().slice(0, 80) };
  }
  const elements = Array.from(document.querySelectorAll('button,a,div,span'))
    .filter((el) => {
      const rect = el.getBoundingClientRect();
      if (!rect || rect.width <= 0 || rect.height <= 0) return false;
      const text = `${el.innerText || ''} ${el.getAttribute('aria-label') || ''} ${el.title || ''}`.trim();
      return /全部商品|商品列表|购物车|小黄车|商品/.test(text) && text.length <= 80;
    });
  const target = elements[0];
  if (!target) return { clicked: false, open: false, reason: 'no-button' };
  target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
  return { clicked: true, open: false, reason: 'text-button', text: (target.innerText || target.getAttribute('aria-label') || target.title || '').trim().slice(0, 80) };
})()
"""


PAGE_STATE_JS = r"""
(() => {
  const bodyText = document.body ? (document.body.innerText || '').replace(/\s+/g, ' ').trim() : '';
  const videos = Array.from(document.querySelectorAll('video')).map((video) => ({
    src: video.src || '',
    currentSrc: video.currentSrc || '',
    readyState: video.readyState,
    paused: video.paused,
    muted: video.muted,
    autoplay: video.autoplay,
    width: video.videoWidth,
    height: video.videoHeight
  }));
  return {
    href: location.href,
    title: document.title,
    readyState: document.readyState,
    liveEnded: /直播已结束|直播结束|暂未开播|未开播/.test(bodyText),
    maybeLoginRequired: /登录|扫码|验证码|请先登录/.test(bodyText),
    bodyTextSample: bodyText.slice(0, 500),
    videos
  };
})()
"""


DOUYIN_WEB_ENTER_STREAMS_JS = r"""
(async () => {
  const out = {
    ok: false,
    status: 0,
    url: "",
    web_rid: "",
    room_id: "",
    candidates: [],
    errors: []
  };

  function clean(value) {
    if (value === undefined || value === null) return "";
    return String(value).replace(/\\u0026/g, "&").replace(/\\\//g, "/").trim();
  }

  function add(source, quality, value) {
    const url = clean(value);
    if (!/^https?:\/\//i.test(url)) return;
    if (!/(\.flv|\.m3u8|pull-flv|pull-hls|live-flv|live-hls)/i.test(url)) return;
    if (out.candidates.some((item) => item.url === url)) return;
    out.candidates.push({ source, quality, url });
  }

  function pathGet(obj, path) {
    if (!obj || !path) return undefined;
    const parts = String(path).replace(/\[(\d+)\]/g, ".$1").split(".");
    let cur = obj;
    for (const part of parts) {
      if (part === "") continue;
      if (cur === undefined || cur === null) return undefined;
      cur = cur[part];
    }
    return cur;
  }

  function tryJson(value) {
    if (typeof value !== "string") return null;
    const text = value.trim();
    if (!text || !/^[\[{]/.test(text)) return null;
    try {
      return JSON.parse(text);
    } catch (err) {
      return null;
    }
  }

  function collectFromRoot(root, prefix) {
    if (!root || typeof root !== "object") return;
    const directPaths = [
      ["1080p", "stream_url.flv_pull_url.FULL_HD1"],
      ["720p", "stream_url.flv_pull_url.HD1"],
      ["原画", "stream_url.flv_pull_url.ORIGION"],
      ["原画", "stream_url.flv_pull_url.ORIGIN"],
      ["480p", "stream_url.flv_pull_url.SD1"],
      ["低清", "stream_url.flv_pull_url.SD2"],
      ["1080p", "streamUrl.flvPullUrl.FULL_HD1"],
      ["720p", "streamUrl.flvPullUrl.HD1"],
      ["原画", "streamUrl.flvPullUrl.ORIGION"],
      ["原画", "streamUrl.flvPullUrl.ORIGIN"],
      ["480p", "streamUrl.flvPullUrl.SD1"],
      ["低清", "streamUrl.flvPullUrl.SD2"],
      ["1080p", "data.uhd.main.flv"],
      ["720p", "data.hd.main.flv"],
      ["原画", "data.origin.main.flv"],
      ["480p", "data.sd.main.flv"],
      ["低清", "data.ld.main.flv"],
      ["未知清晰度", "data.md.main.flv"],
      ["未知清晰度", "data.ao.main.flv"]
    ];
    for (const [quality, path] of directPaths) {
      add(`${prefix}:${path}`, quality, pathGet(root, path));
    }
    const streamData = tryJson(pathGet(root, "stream_url.live_core_sdk_data.pull_data.stream_data"))
      || tryJson(pathGet(root, "streamUrl.liveCoreSdkData.pullData.streamData"));
    if (streamData) {
      collectFromRoot(streamData, `${prefix}:stream_data`);
    }
  }

  const params = new URLSearchParams(location.search || "");
  const pathRid = (location.pathname.match(/\/(\d+)/) || [])[1] || "";
  const webRid = pathRid || params.get("web_rid") || params.get("webRid") || "";
  const roomId = params.get("room_id") || params.get("roomId") || params.get("enter_room_id") || "";
  out.web_rid = webRid;
  out.room_id = roomId;
  if (!/live\.douyin\.com$/i.test(location.hostname)) {
    out.errors.push(`not live.douyin.com: ${location.hostname}`);
    return out;
  }
  if (!webRid && !roomId) {
    out.errors.push("missing web_rid and room_id");
    return out;
  }

  const url = new URL("/webcast/room/web/enter/", location.origin);
  const defaults = {
    aid: "6383",
    app_name: "douyin_web",
    live_id: "1",
    device_platform: "web",
    language: "zh-CN",
    enter_from: "web_live",
    cookie_enabled: "true",
    screen_width: String(Math.max(1920, window.screen && window.screen.width || 0)),
    screen_height: String(Math.max(1080, window.screen && window.screen.height || 0)),
    browser_language: navigator.language || "zh-CN",
    browser_platform: navigator.platform || "Win32",
    browser_name: "Chrome",
    browser_version: (navigator.userAgent.match(/Chrome\/([\d.]+)/) || [])[1] || "120.0.0.0",
    browser_online: String(navigator.onLine),
  };
  for (const [key, value] of Object.entries(defaults)) {
    url.searchParams.set(key, value);
  }
  if (webRid) url.searchParams.set("web_rid", webRid);
  if (roomId) url.searchParams.set("room_id", roomId);
  const msToken = (document.cookie.match(/(?:^|;\s*)msToken=([^;]+)/) || [])[1] || "";
  if (msToken) url.searchParams.set("msToken", msToken);
  out.url = url.toString();

  try {
    const response = await fetch(out.url, {
      credentials: "include",
      cache: "no-store",
      headers: {
        "accept": "application/json, text/plain, */*"
      }
    });
    out.status = response.status;
    const text = await response.text();
    out.ok = response.ok;
    let data = null;
    try {
      data = JSON.parse(text);
    } catch (err) {
      out.errors.push(`json parse failed: ${err && err.message ? err.message : err}`);
      out.sample = text.slice(0, 500);
      return out;
    }
    const roots = [data, data && data.data, data && data.room, pathGet(data, "data.data[0]")];
    for (const root of roots) {
      collectFromRoot(root, "web_enter");
    }
  } catch (err) {
    out.errors.push(String(err && err.message ? err.message : err));
  }
  return out;
})()
"""


def log(message: str) -> None:
    now = dt.datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def safe_stem(value: str, fallback: str = "douyin_live") -> str:
    text = (value or "").strip() or fallback
    text = re.sub(r'[\\/:*?"<>|]+', "_", text)
    text = re.sub(r"\s+", "_", text)
    return text[:80].strip("._ ") or fallback


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def write_jsonl(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json_dumps(event) + "\n")


def http_json(url: str, timeout: float = 3.0, method: str = "GET") -> Any:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def chrome_base(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def find_chrome() -> str:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe"),
    ]
    found = shutil.which("chrome") or shutil.which("chrome.exe")
    if found:
        candidates.insert(0, found)
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise FileNotFoundError("Chrome was not found.")


def find_ffmpeg() -> str:
    root = Path(__file__).resolve().parents[1]
    candidates = [
        root / "app" / "ffmpeg" / "ffmpeg.exe",
        root / "_internal" / "ffmpeg" / "ffmpeg.exe",
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
    ]
    found = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if found:
        candidates.insert(0, Path(found))
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return "ffmpeg"


def find_ffprobe() -> str:
    ffmpeg = find_ffmpeg()
    candidates: list[Path] = []
    try:
        ffmpeg_path = Path(ffmpeg)
        suffix = ".exe" if ffmpeg_path.suffix.lower() == ".exe" else ""
        candidates.append(ffmpeg_path.with_name(f"ffprobe{suffix}"))
    except Exception:
        pass
    root = Path(__file__).resolve().parents[1]
    candidates.extend(
        [
            root / "app" / "ffmpeg" / "ffprobe.exe",
            root / "_internal" / "ffmpeg" / "ffprobe.exe",
            Path(r"C:\ffmpeg\bin\ffprobe.exe"),
        ]
    )
    found = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if found:
        candidates.insert(0, Path(found))
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return "ffprobe"


def launch_chrome(port: int, url: str, profile_dir: Path) -> subprocess.Popen[Any]:
    chrome = find_chrome()
    profile_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--new-window",
        url,
    ]
    log(f"Launching Chrome DevTools on 127.0.0.1:{port}")
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def wait_for_devtools(port: int, timeout: float) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            return http_json(f"{chrome_base(port)}/json/version", timeout=2.0)
        except Exception as exc:  # noqa: BLE001 - diagnostic only
            last_error = str(exc)
            time.sleep(0.5)
    raise RuntimeError(f"Chrome DevTools is not available on 127.0.0.1:{port}: {last_error}")


def get_targets(port: int) -> list[dict[str, Any]]:
    data = http_json(f"{chrome_base(port)}/json/list", timeout=3.0)
    if isinstance(data, list):
        return data
    return []


def open_new_tab(port: int, url: str) -> dict[str, Any] | None:
    encoded = urllib.parse.quote(url, safe="")
    endpoint = f"{chrome_base(port)}/json/new?{encoded}"
    try:
        result = http_json(endpoint, timeout=3.0, method="PUT")
        return result if isinstance(result, dict) else None
    except Exception:
        try:
            result = http_json(endpoint, timeout=3.0)
            return result if isinstance(result, dict) else None
        except Exception:
            return None


def close_target(port: int, target_id: str) -> bool:
    target_id = str(target_id or "").strip()
    if not target_id:
        return False
    encoded = urllib.parse.quote(target_id, safe="")
    endpoint = f"{chrome_base(port)}/json/close/{encoded}"
    for method in ("PUT", "GET"):
        try:
            request = urllib.request.Request(endpoint, method=method)
            with urllib.request.urlopen(request, timeout=2.0) as response:
                response.read()
            return True
        except Exception:
            continue
    return False


def room_id_from_url(url: str) -> str:
    text = str(url or "")
    patterns = [
        r"live\.douyin\.com/(\d+)",
        r"webcast\.amemv\.com/douyin/webcast/reflow/(\d+)",
        r"[?&]room_id=(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def find_live_target(port: int, room_url: str, timeout: float = 20.0) -> dict[str, Any]:
    expected_room_id = room_id_from_url(room_url)
    deadline = time.time() + timeout
    best: dict[str, Any] | None = None
    while time.time() < deadline:
        for target in get_targets(port):
            if target.get("type") != "page" or not target.get("webSocketDebuggerUrl"):
                continue
            target_url = str(target.get("url") or "")
            if expected_room_id and expected_room_id in target_url:
                return target
            if not expected_room_id and "live.douyin.com" in target_url:
                best = target
        if best:
            return best
        time.sleep(0.5)
    if expected_room_id:
        raise RuntimeError(f"No matching Douyin live page was found for room {expected_room_id}.")
    raise RuntimeError("No Douyin live page was found in the DevTools-enabled Chrome.")


class MiniWebSocket:
    def __init__(self, ws_url: str) -> None:
        parsed = urllib.parse.urlparse(ws_url)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError(f"Unsupported websocket scheme: {parsed.scheme}")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        raw_sock = socket.create_connection((self.host, self.port), timeout=5)
        self.sock: socket.socket
        if parsed.scheme == "wss":
            self.sock = ssl.create_default_context().wrap_socket(raw_sock, server_hostname=self.host)
        else:
            self.sock = raw_sock
        self.sock.settimeout(5)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = self._read_http_header()
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"WebSocket handshake failed: {response[:120]!r}")

    def _read_http_header(self) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 65536:
                break
        return bytes(data)

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass

    def _read_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise EOFError("WebSocket closed.")
            data.extend(chunk)
        return bytes(data)

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self, timeout: float = 5.0) -> str:
        self.sock.settimeout(timeout)
        fragments: list[bytes] = []
        while True:
            first, second = self._read_exact(2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length) if length else b""
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                raise EOFError("WebSocket closed by peer.")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode in {0x1, 0x0}:
                fragments.append(payload)
                if fin:
                    return b"".join(fragments).decode("utf-8", errors="replace")


class CDPClient:
    def __init__(self, ws_url: str) -> None:
        self.ws = MiniWebSocket(ws_url)
        self.next_id = 1
        self.events: list[dict[str, Any]] = []

    def close(self) -> None:
        self.ws.close()

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 8.0) -> dict[str, Any]:
        message_id = self.next_id
        self.next_id += 1
        self.ws.send_text(json.dumps({"id": message_id, "method": method, "params": params or {}}, separators=(",", ":")))
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(0.2, deadline - time.time())
            try:
                raw = self.ws.recv_text(timeout=remaining)
            except socket.timeout:
                continue
            message = json.loads(raw)
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(f"CDP {method} failed: {message['error']}")
                return message.get("result") or {}
            self.events.append(message)
        raise TimeoutError(f"CDP {method} timed out.")

    def recv_event(self, timeout: float = 1.0) -> dict[str, Any] | None:
        if self.events:
            return self.events.pop(0)
        try:
            return json.loads(self.ws.recv_text(timeout=timeout))
        except socket.timeout:
            return None

    def evaluate(self, expression: str, timeout: float = 8.0, await_promise: bool = False) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "timeout": int(timeout * 1000),
            },
            timeout=timeout + 2,
        )
        if result.get("exceptionDetails"):
            raise RuntimeError(f"Runtime.evaluate failed: {result['exceptionDetails']}")
        remote = result.get("result") or {}
        return remote.get("value")


def looks_like_stream_url(url: str) -> bool:
    lower = str(url or "").lower()
    if not lower.startswith(("http://", "https://")):
        return False
    if any(ext in lower for ext in (".js", ".css", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".woff", ".ttf")):
        return False
    return any(token in lower for token in (".flv", ".m3u8", "pull-flv", "pull-hls", "live-flv", "live-hls"))


STREAM_URL_RE = re.compile(r"https?:(?://|\\/\\/)[^\"'<>\s]+?\.(?:flv|m3u8)(?:[^\"'<>\s]*)?", re.I)


def normalize_stream_url_candidate(value: str) -> str:
    text = html.unescape(str(value or "")).strip()
    text = text.replace("\\/", "/").replace("\\u0026", "&").replace("\\u003d", "=")
    text = text.replace("\\u002F", "/").replace("\\u0026", "&")
    text = text.strip(" \t\r\n\"'")
    text = re.sub(r"[\\,;)}\]]+$", "", text)
    return text


def extract_stream_urls_from_text(text: str) -> list[str]:
    normalized = html.unescape(str(text or ""))
    found: list[str] = []
    for match in STREAM_URL_RE.finditer(normalized):
        url = normalize_stream_url_candidate(match.group(0))
        if looks_like_stream_url(url):
            found.append(url)
    return found


def extract_stream_urls_from_value(value: Any, depth: int = 0) -> list[str]:
    if depth > 7 or value is None:
        return []
    found: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            found.extend(extract_stream_urls_from_value(item, depth + 1))
        return found
    if isinstance(value, list):
        for item in value:
            found.extend(extract_stream_urls_from_value(item, depth + 1))
        return found
    if isinstance(value, str):
        found.extend(extract_stream_urls_from_text(value))
        candidate = html.unescape(value).replace("\\/", "/").replace('\\"', '"').strip()
        if candidate and candidate[0] in "[{":
            try:
                found.extend(extract_stream_urls_from_value(json.loads(candidate), depth + 1))
            except Exception:
                pass
    return found


def should_inspect_stream_response(url: str, mime_type: str = "") -> bool:
    lower_url = str(url or "").lower()
    lower_mime = str(mime_type or "").lower()
    if not any(token in lower_url for token in ("douyin", "amemv", "webcast", "live", "room", "stream")):
        return False
    if any(token in lower_mime for token in ("json", "text", "javascript", "x-www-form-urlencoded")):
        return True
    return any(token in lower_url for token in ("webcast", "room", "stream", "live"))


def collect_streams_from_response_body(client: CDPClient, request_id: str) -> list[str]:
    if not request_id:
        return []
    try:
        result = client.call("Network.getResponseBody", {"requestId": request_id}, timeout=3)
    except Exception:
        return []
    body = str(result.get("body") or "")
    if result.get("base64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8", errors="replace")
        except Exception:
            return []
    found = extract_stream_urls_from_text(body)
    stripped = body.strip()
    if stripped and stripped[0] in "[{":
        try:
            found.extend(extract_stream_urls_from_value(json.loads(stripped)))
        except Exception:
            pass
    return found


def stream_quality_label(url: str) -> str:
    lower = urllib.parse.unquote(str(url or "")).lower()
    parsed = urllib.parse.urlparse(lower)
    query = urllib.parse.parse_qs(parsed.query)
    values = [parsed.path]
    for key in ("biz_quality", "quality", "definition", "ratio", "unique_id"):
        values.extend(query.get(key, []))
    text = " ".join(str(value) for value in values)
    compact = re.sub(r"[^a-z0-9]+", "_", text)
    douyin_suffix_patterns = [
        (r"(^|_)or\d*($|_)", "原画"),
        (r"(^|_)origin\d*($|_)", "原画"),
        (r"(^|_)uhd\d*($|_)", "超清"),
        (r"stage\d+t\d*hd\d*($|_)", "720p"),
        (r"(^|_)hd\d*($|_)", "720p"),
        (r"stage\d+t\d*sd\d*($|_)", "480p"),
        (r"(^|_)sd\d*($|_)", "480p"),
        (r"stage\d+t\d*ld\d*($|_)", "低清"),
        (r"(^|_)ld\d*($|_)", "低清"),
    ]
    for pattern, label in douyin_suffix_patterns:
        if re.search(pattern, compact):
            return label
    quality_order = [
        ("origin", "原画"),
        ("source", "原画"),
        ("uhd", "超清"),
        ("4k", "超清"),
        ("full_hd1", "1080p"),
        ("full_hd", "1080p"),
        ("1080p", "1080p"),
        ("1080", "1080p"),
        ("hd1", "720p"),
        ("hd", "720p"),
        ("720p", "720p"),
        ("720", "720p"),
        ("sd1", "480p"),
        ("sd", "480p"),
        ("480p", "480p"),
        ("480", "480p"),
        ("ld", "低清"),
        ("360p", "低清"),
        ("360", "低清"),
    ]
    for marker, label in quality_order:
        if re.search(rf"(^|[^a-z0-9]){re.escape(marker)}([^a-z0-9]|$)", text):
            return label
    return "未知清晰度"


def quality_label_from_resolution(width: Any, height: Any) -> str:
    try:
        w = int(width or 0)
        h = int(height or 0)
    except Exception:
        return "未知清晰度"
    if not w or not h:
        return "未知清晰度"
    effective_side = min(w, h)
    if effective_side >= 2160:
        return "超清"
    if effective_side >= 1080:
        return "1080p"
    if effective_side >= 720:
        return "720p"
    if effective_side >= 480:
        return "480p"
    if effective_side > 0:
        return "低清"
    return "未知清晰度"


def probe_stream_resolution(url: str, timeout: float = 8.0) -> dict[str, Any]:
    if not url:
        return {}
    ffprobe = find_ffprobe()
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-rw_timeout",
        str(int(max(timeout, 1.0) * 1_000_000)),
        "-analyzeduration",
        "3000000",
        "-probesize",
        "1000000",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,codec_name,avg_frame_rate,bit_rate",
        "-of",
        "json",
        url,
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout + 2.0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        return {"ok": False, "error": str(exc)}
    if result.returncode != 0:
        return {"ok": False, "error": (result.stderr or "").strip()[:500]}
    try:
        data = json.loads(result.stdout or "{}")
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        return {"ok": False, "error": f"invalid ffprobe json: {exc}"}
    streams = data.get("streams") if isinstance(data, dict) else None
    if not isinstance(streams, list) or not streams:
        return {"ok": False, "error": "no video stream"}
    stream = streams[0] if isinstance(streams[0], dict) else {}
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    return {
        "ok": bool(width and height),
        "width": width,
        "height": height,
        "codec_name": str(stream.get("codec_name") or ""),
        "avg_frame_rate": str(stream.get("avg_frame_rate") or ""),
        "bit_rate": str(stream.get("bit_rate") or ""),
        "quality": quality_label_from_resolution(width, height),
    }


def effective_stream_quality_label(url: str, probe_info: dict[str, Any] | None = None) -> str:
    if probe_info and probe_info.get("ok"):
        label = quality_label_from_resolution(probe_info.get("width"), probe_info.get("height"))
        if label != "未知清晰度":
            return label
    return stream_quality_label(url)


def stream_score_with_probe(url: str, probe_info: dict[str, Any] | None = None) -> tuple[int, int, int, int]:
    lower = url.lower()
    format_score = 0 if (".flv" in lower or "pull-flv" in lower or "live-flv" in lower) else 1
    quality_order = {
        "1080p": 0,
        "超清": 5,
        "原画": 10,
        "720p": 30,
        "480p": 55,
        "未知清晰度": 70,
        "低清": 80,
    }
    label = effective_stream_quality_label(url, probe_info)
    quality_score = quality_order.get(label, 70)
    height = 0
    if probe_info and probe_info.get("ok"):
        try:
            width = int(probe_info.get("width") or 0)
            height = int(probe_info.get("height") or 0)
            height = min(width, height) if width and height else height
        except Exception:
            height = 0
    return (quality_score, -height, format_score, len(url))


def stream_score(url: str) -> tuple[int, int, int]:
    lower = url.lower()
    format_score = 0 if (".flv" in lower or "pull-flv" in lower or "live-flv" in lower) else 1
    quality_order = {
        "1080p": 0,
        "超清": 5,
        "原画": 10,
        "720p": 30,
        "480p": 55,
        "低清": 80,
        "未知清晰度": 70,
    }
    quality_score = quality_order.get(stream_quality_label(url), 70)
    return (quality_score, format_score, len(url))


def pick_best_stream(urls: list[str]) -> str:
    candidates = sorted({url for url in urls if looks_like_stream_url(url)}, key=stream_score)
    return candidates[0] if candidates else ""


def rank_stream_candidates_with_probe(urls: list[str], max_probe: int = 8) -> tuple[list[str], dict[str, dict[str, Any]]]:
    candidates = sorted({url for url in urls if looks_like_stream_url(url)}, key=stream_score)
    probe_info_by_url: dict[str, dict[str, Any]] = {}
    for url in candidates[: max(0, max_probe)]:
        probe_info_by_url[url] = probe_stream_resolution(url)
    ranked = sorted(candidates, key=lambda item: stream_score_with_probe(item, probe_info_by_url.get(item)))
    return ranked, probe_info_by_url


def mask_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.rsplit("/", 1)[-1] or parsed.path
        return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "...", ""))
    except Exception:
        return "<stream-url>"


def collect_resource_streams(client: CDPClient) -> list[str]:
    try:
        urls = client.evaluate(RESOURCE_URLS_JS, timeout=6)
    except Exception:
        return []
    if not isinstance(urls, list):
        return []
    found: list[str] = []
    for url in urls:
        text = str(url)
        if looks_like_stream_url(text):
            found.append(text)
        found.extend(extract_stream_urls_from_text(text))
    return found


def collect_douyin_web_enter_streams(client: CDPClient) -> list[str]:
    try:
        result = client.evaluate(DOUYIN_WEB_ENTER_STREAMS_JS, timeout=15, await_promise=True)
    except Exception:
        return []
    if not isinstance(result, dict):
        return []
    candidates = result.get("candidates")
    if not isinstance(candidates, list):
        return []
    found: list[str] = []
    for item in candidates:
        if isinstance(item, dict):
            url = str(item.get("url") or "")
        else:
            url = str(item or "")
        if looks_like_stream_url(url):
            found.append(url)
    return found


def collect_stream_candidates(
    client: CDPClient,
    wait_seconds: float,
    wait_for_hd: bool = False,
    event_callback: Callable[[dict[str, Any]], None] | None = None,
) -> list[str]:
    found: list[str] = []
    found.extend(collect_douyin_web_enter_streams(client))
    found.extend(collect_resource_streams(client))
    best = pick_best_stream(found)
    if best and stream_score(best)[0] <= 10:
        return sorted({url for url in found if looks_like_stream_url(url)}, key=stream_score)

    deadline = time.time() + wait_seconds
    min_collect_until = time.time() + min(8.0, max(1.0, wait_seconds))
    next_poll = 0.0
    response_meta: dict[str, dict[str, str]] = {}
    while time.time() < deadline:
        event = client.recv_event(timeout=0.8)
        if event:
            if event_callback:
                try:
                    event_callback(event)
                except Exception:
                    pass
            method = event.get("method")
            params = event.get("params") or {}
            url = ""
            if method == "Network.requestWillBeSent":
                request = params.get("request") or {}
                url = str(request.get("url") or "")
            elif method == "Network.responseReceived":
                response = params.get("response") or {}
                url = str(response.get("url") or "")
                request_id = str(params.get("requestId") or "")
                mime_type = str(response.get("mimeType") or "")
                if request_id and should_inspect_stream_response(url, mime_type):
                    response_meta[request_id] = {"url": url, "mimeType": mime_type}
            elif method == "Network.loadingFinished":
                request_id = str(params.get("requestId") or "")
                meta = response_meta.pop(request_id, None)
                if meta:
                    found.extend(collect_streams_from_response_body(client, request_id))
            if looks_like_stream_url(url):
                found.append(url)
            elif url:
                found.extend(extract_stream_urls_from_text(url))
            if url or method == "Network.loadingFinished":
                best = pick_best_stream(found)
                if best and stream_score(best)[0] <= 10 and time.time() >= min_collect_until:
                    break
                if best and not wait_for_hd and time.time() >= min_collect_until:
                    break
        if time.time() >= next_poll:
            found.extend(collect_douyin_web_enter_streams(client))
            found.extend(collect_resource_streams(client))
            best = pick_best_stream(found)
            if best and stream_score(best)[0] <= 10 and time.time() >= min_collect_until:
                break
            if best and not wait_for_hd and time.time() >= min_collect_until:
                break
            next_poll = time.time() + 3.0
    return sorted({url for url in found if looks_like_stream_url(url)}, key=stream_score)


def describe_stream_candidates(
    urls: list[str],
    probes: dict[str, dict[str, Any]] | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    described = []
    probes = probes or {}
    ranked = sorted(
        {url for url in urls if looks_like_stream_url(url)},
        key=lambda item: stream_score_with_probe(item, probes.get(item)),
    )
    for url in ranked[:limit]:
        lower = url.lower()
        probe_info = probes.get(url) or {}
        item = {
            "quality": effective_stream_quality_label(url, probe_info),
            "named_quality": stream_quality_label(url),
            "format": "flv" if ".flv" in lower or "pull-flv" in lower else "m3u8" if ".m3u8" in lower or "pull-hls" in lower else "unknown",
            "masked_url": mask_url(url),
            "score": list(stream_score_with_probe(url, probe_info)),
        }
        if probe_info:
            item["probe_ok"] = bool(probe_info.get("ok"))
            if probe_info.get("width") or probe_info.get("height"):
                item["width"] = int(probe_info.get("width") or 0)
                item["height"] = int(probe_info.get("height") or 0)
            if probe_info.get("codec_name"):
                item["codec_name"] = probe_info.get("codec_name")
            if probe_info.get("bit_rate"):
                item["bit_rate"] = probe_info.get("bit_rate")
            if probe_info.get("error"):
                item["probe_error"] = str(probe_info.get("error"))[:300]
        described.append(
            item
        )
    return described


def collect_stream_url(client: CDPClient, wait_seconds: float) -> str:
    candidates = collect_stream_candidates(client, wait_seconds, wait_for_hd=False)
    return candidates[0] if candidates else ""


def get_page_state(client: CDPClient) -> dict[str, Any]:
    try:
        value = client.evaluate(PAGE_STATE_JS, timeout=6)
    except Exception as exc:  # noqa: BLE001 - diagnostic only
        return {"error": str(exc)}
    return value if isinstance(value, dict) else {}


def ensure_product_list(client: CDPClient, timeout: float = 18.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_result: dict[str, Any] = {}
    while time.time() < deadline:
        try:
            result = client.evaluate(TRY_OPEN_PRODUCTS_JS, timeout=5)
            if isinstance(result, dict):
                last_result = result
                if result.get("open") or result.get("reason") == "already-open":
                    return result
                if result.get("clicked"):
                    time.sleep(1.5)
                    opened = client.evaluate("!!document.querySelector('[data-e2e=\"live-promotion-list\"]')", timeout=3)
                    if opened:
                        result["open"] = True
                        return result
        except Exception as exc:  # noqa: BLE001 - diagnostic only
            last_result = {"error": str(exc)}
        time.sleep(1.0)
    return last_result


def page_has_playing_video(page_state: dict[str, Any]) -> bool:
    videos = page_state.get("videos") if isinstance(page_state, dict) else None
    if not isinstance(videos, list):
        return False
    for video in videos:
        if not isinstance(video, dict):
            continue
        if int(video.get("readyState") or 0) > 0 or video.get("currentSrc") or video.get("src"):
            return True
    return False


def normalize_products(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"href": "", "title": "", "hasPromotionRoot": False, "count": 0, "products": []}
    products = raw.get("products")
    if not isinstance(products, list):
        products = []
    normalized: list[dict[str, Any]] = []
    seen = set()
    for item in products:
        if not isinstance(item, dict):
            continue
        product = {
            "product_id": str(item.get("product_id") or "").strip(),
            "promotion_id": str(item.get("promotion_id") or "").strip(),
            "shop_id": str(item.get("shop_id") or "").strip(),
            "title": re.sub(r"\s+", " ", str(item.get("title") or "")).strip(),
            "detail_url": str(item.get("detail_url") or "").replace("\\u0026", "&").strip(),
            "index": str(item.get("index") or "").strip(),
            "status": str(item.get("status") or "").strip(),
            "min_price": str(item.get("min_price") or "").strip(),
            "cover": str(item.get("cover") or "").strip(),
            "is_explaining": bool(item.get("is_explaining")),
            "node_text": str(item.get("node_text") or "").strip(),
        }
        detail_lower = product["detail_url"].lower()
        is_product_url = (
            ("ecom.douyin.com" in detail_lower or "ecommerce" in detail_lower or "trade/detail" in detail_lower)
            and "creator.douyin.com" not in detail_lower
            and "streamingtool.douyin.com" not in detail_lower
            and "livedata.douyin.com" not in detail_lower
        )
        has_product_ids = bool(product["product_id"] and product["promotion_id"])
        has_product_body = bool(product["title"] or is_product_url)
        if not ((has_product_ids and has_product_body) or is_product_url):
            continue
        key = product["promotion_id"] or product["product_id"] or product["detail_url"]
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(product)
    raw["products"] = normalized
    raw["count"] = len(normalized)
    return raw


def snapshot_catalog(client: CDPClient) -> dict[str, Any]:
    raw = client.evaluate(PRODUCT_CATALOG_JS, timeout=10)
    return normalize_products(raw)


def snapshot_active_product(client: CDPClient) -> dict[str, Any]:
    try:
        raw = client.evaluate(ACTIVE_PRODUCT_JS, timeout=12)
    except Exception as exc:  # noqa: BLE001 - diagnostic sidecar
        return {"active": None, "candidates": [], "candidate_count": 0, "error": str(exc)}
    if not isinstance(raw, dict):
        return {"active": None, "candidates": [], "candidate_count": 0}
    active = raw.get("active")
    if active and isinstance(active, dict):
        product = active.get("product")
        if isinstance(product, dict):
            active["product"] = {
                "product_id": str(product.get("product_id") or "").strip(),
                "promotion_id": str(product.get("promotion_id") or "").strip(),
                "shop_id": str(product.get("shop_id") or "").strip(),
                "title": re.sub(r"\s+", " ", str(product.get("title") or "")).strip(),
                "detail_url": str(product.get("detail_url") or "").replace("\\u0026", "&").strip(),
                "index": str(product.get("index") or "").strip(),
                "status": str(product.get("status") or "").strip(),
                "explain_type": str(product.get("explain_type") or "").strip(),
            }
    return raw


def product_signature(products: list[dict[str, Any]]) -> str:
    keys = [item.get("promotion_id") or item.get("product_id") or item.get("detail_url") or "" for item in products]
    return "|".join(keys)


def active_product_key(products: list[dict[str, Any]]) -> str:
    for product in products:
        if product.get("is_explaining"):
            return str(product.get("promotion_id") or product.get("product_id") or product.get("detail_url") or "")
    return ""


def active_snapshot_key(snapshot: dict[str, Any]) -> str:
    active = snapshot.get("active") if isinstance(snapshot, dict) else None
    if not isinstance(active, dict):
        return ""
    product = active.get("product")
    if not isinstance(product, dict):
        return ""
    key = product.get("promotion_id") or product.get("product_id") or product.get("detail_url") or product.get("title") or ""
    source = active.get("source") or ""
    return f"{source}:{key}" if key else ""


def start_ffmpeg(ffmpeg: str, stream_url: str, output: Path, seconds: float, stderr_path: Path) -> subprocess.Popen[Any]:
    cmd = [ffmpeg, "-hide_banner", "-y", "-fflags", "+genpts+igndts+discardcorrupt"]
    if stream_url.lower().startswith(("http://", "https://")):
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "30"]
    cmd += ["-i", stream_url, "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy"]
    if seconds > 0:
        cmd += ["-t", f"{seconds:.3f}"]
    if output.suffix.lower() == ".ts":
        cmd += ["-avoid_negative_ts", "make_zero", "-f", "mpegts", "-mpegts_flags", "+resend_headers"]
    cmd.append(str(output))
    stderr_file = stderr_path.open("wb")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        setattr(proc, "_liveclipper_stderr_file", stderr_file)
        return proc
    except Exception:
        stderr_file.close()
        raise


def close_ffmpeg_log(proc: subprocess.Popen[Any] | None) -> None:
    if not proc:
        return
    handle = getattr(proc, "_liveclipper_stderr_file", None)
    if handle:
        try:
            handle.close()
        except Exception:
            pass


def output_suffix_for_stream(url: str) -> str:
    lower = url.lower()
    if ".flv" in lower or "pull-flv" in lower or "live-flv" in lower:
        return ".ts"
    return ".ts"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Douyin logged-in Chrome live recording POC.")
    parser.add_argument("--url", required=True, help="Douyin live room URL.")
    parser.add_argument("--seconds", type=float, default=300.0, help="Recording duration. Use 0 to record until Ctrl+C.")
    parser.add_argument("--output-dir", default="", help="Output directory. Default: ~/Videos/LiveClipperChromePoc")
    parser.add_argument("--room-name", default="", help="Optional room name for output filenames.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Chrome DevTools port.")
    parser.add_argument("--snapshot-interval", type=float, default=DEFAULT_SNAPSHOT_INTERVAL, help="Catalog snapshot interval in seconds.")
    parser.add_argument("--stream-wait-seconds", type=float, default=DEFAULT_STREAM_WAIT_SECONDS, help="How long to wait for a stream URL.")
    parser.add_argument("--no-record", action="store_true", help="Only collect stream/catalog evidence; do not start ffmpeg.")
    parser.add_argument("--no-open-products", action="store_true", help="Do not try to click a product-list entry when the list is not visible.")
    return parser.parse_args()


def prepare_chrome(args: argparse.Namespace) -> dict[str, Any]:
    try:
        return wait_for_devtools(args.port, timeout=1.5)
    except Exception:
        profile_root = Path(os.environ.get("APPDATA", Path.home())) / "LiveClipper" / "chrome-live-poc"
        launch_chrome(args.port, args.url, profile_root)
        log("If Douyin asks for login, finish login in the Chrome window. This script will keep polling.")
        return wait_for_devtools(args.port, timeout=25.0)


def ensure_live_tab(args: argparse.Namespace) -> dict[str, Any]:
    try:
        return find_live_target(args.port, args.url, timeout=3.0)
    except Exception:
        open_new_tab(args.port, args.url)
        return find_live_target(args.port, args.url, timeout=20.0)


def run() -> int:
    args = parse_args()
    out_dir = Path(args.output_dir).expanduser() if args.output_dir else Path.home() / "Videos" / "LiveClipperChromePoc"
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    room_name = safe_stem(args.room_name or room_id_from_url(args.url) or "douyin_live")
    base = out_dir / f"{room_name}_{stamp}"
    timeline_path = base.with_suffix(".timeline.jsonl")
    catalog_path = base.with_suffix(".catalog.jsonl")
    diagnostics_path = base.with_suffix(".diagnostics.json")

    log("Preparing Chrome connection...")
    version = prepare_chrome(args)
    target = ensure_live_tab(args)
    log(f"Connected Chrome: {version.get('Browser', 'Chrome')}")
    log(f"Using tab: {target.get('title') or target.get('url')}")

    client = CDPClient(str(target["webSocketDebuggerUrl"]))
    ffmpeg_proc: subprocess.Popen[Any] | None = None
    output_path: Path | None = None
    stderr_path: Path | None = None
    started_at = time.time()
    last_signature = ""
    last_active_key = ""

    try:
        client.call("Runtime.enable", timeout=5)
        client.call("Page.enable", timeout=5)
        client.call("Network.enable", timeout=5)

        if not args.no_open_products:
            opened = ensure_product_list(client, timeout=18.0)
            if opened.get("open"):
                log("Product list is open.")
            elif opened.get("clicked"):
                log(f"Tried to open product list: {opened.get('text') or opened.get('reason') or 'clicked'}")
            elif opened:
                log(f"Product list not opened yet: {opened.get('reason') or opened.get('error') or opened}")

        page_state = get_page_state(client)
        if page_state.get("liveEnded"):
            log("Page state says the live has ended or is not live.")
            stream_url = ""
        else:
            log("Looking for live stream URL from the real Chrome page...")
            stream_url = collect_stream_url(client, args.stream_wait_seconds)
            page_state = get_page_state(client)
            if not stream_url and page_has_playing_video(page_state):
                log("The page is already playing through a blob URL; reloading once to recapture the original stream request...")
                try:
                    client.call("Page.reload", {"ignoreCache": False}, timeout=5)
                except Exception as exc:  # noqa: BLE001 - diagnostic only
                    log(f"Reload request skipped: {exc}")
                time.sleep(4.0)
                if not args.no_open_products:
                    opened = ensure_product_list(client, timeout=18.0)
                    if opened.get("open"):
                        log("Product list is open after reload.")
                    elif opened.get("clicked"):
                        log(f"Tried to open product list after reload: {opened.get('text') or opened.get('reason') or 'clicked'}")
                    elif opened:
                        log(f"Product list not opened after reload: {opened.get('reason') or opened.get('error') or opened}")
                stream_url = collect_stream_url(client, args.stream_wait_seconds)
                page_state = get_page_state(client)
        catalog = snapshot_catalog(client)
        products = catalog.get("products") or []
        active_snapshot = snapshot_active_product(client)
        active_key = active_snapshot_key(active_snapshot)

        diagnostics = {
            "created_at": now_iso(),
            "room_url": args.url,
            "target_url": target.get("url"),
            "target_title": target.get("title"),
            "chrome": version,
            "stream_found": bool(stream_url),
            "stream_masked": mask_url(stream_url) if stream_url else "",
            "page_state": page_state,
            "catalog_count": len(products),
            "catalog_has_promotion_root": bool(catalog.get("hasPromotionRoot")),
            "active_product_found": bool(active_snapshot.get("active")),
            "active_product": active_snapshot.get("active"),
            "active_candidate_count": active_snapshot.get("candidate_count", 0),
            "timeline_path": str(timeline_path),
            "catalog_path": str(catalog_path),
        }
        diagnostics_path.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")

        write_jsonl(
            timeline_path,
            {
                "type": "poc_start",
                "ts": now_iso(),
                "room_url": args.url,
                "target_url": target.get("url"),
                "stream_found": bool(stream_url),
                "stream_masked": mask_url(stream_url) if stream_url else "",
                "page_state": page_state,
                "catalog_count": len(products),
                "active_product_found": bool(active_snapshot.get("active")),
            },
        )
        write_jsonl(catalog_path, {"type": "catalog_snapshot", "ts": now_iso(), "elapsed": 0, **catalog})
        write_jsonl(
            timeline_path,
            {
                "type": "active_product_snapshot" if active_snapshot.get("active") else "active_product_unresolved",
                "ts": now_iso(),
                "elapsed": 0,
                **active_snapshot,
            },
        )
        last_signature = product_signature(products)
        last_active_key = active_key

        log(f"Catalog snapshot: {len(products)} products, root={bool(catalog.get('hasPromotionRoot'))}")
        if active_snapshot.get("active"):
            active_product = (active_snapshot.get("active") or {}).get("product") or {}
            log(f"Active product signal: {active_product.get('title') or active_product.get('promotion_id') or active_product.get('product_id')}")
        else:
            log("Active product signal: unresolved")
        if not stream_url:
            if page_state.get("liveEnded"):
                log("Page state says the live has ended or is not live.")
            elif page_state.get("maybeLoginRequired"):
                log("Page state suggests login or verification may be required in the Chrome window.")
            else:
                videos = page_state.get("videos") or []
                if videos:
                    log(f"Video element state: {videos[0]}")
            log("No stream URL was found. Keep the Chrome tab playing, then rerun the POC.")
            return 2

        log(f"Stream URL found: {mask_url(stream_url)}")
        if not args.no_record:
            suffix = output_suffix_for_stream(stream_url)
            output_path = base.with_suffix(suffix)
            stderr_path = base.with_suffix(".ffmpeg.log")
            ffmpeg = find_ffmpeg()
            log(f"Starting ffmpeg recording: {output_path}")
            ffmpeg_proc = start_ffmpeg(ffmpeg, stream_url, output_path, args.seconds, stderr_path)
            write_jsonl(
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

        next_snapshot = time.time() + max(1.0, args.snapshot_interval)
        deadline = time.time() + args.seconds if args.no_record and args.seconds > 0 else None
        while True:
            if ffmpeg_proc and ffmpeg_proc.poll() is not None:
                break
            if deadline and time.time() >= deadline:
                break
            if time.time() >= next_snapshot:
                elapsed = round(time.time() - started_at, 3)
                catalog = snapshot_catalog(client)
                products = catalog.get("products") or []
                signature = product_signature(products)
                active_snapshot = snapshot_active_product(client)
                active_key = active_snapshot_key(active_snapshot)
                write_jsonl(catalog_path, {"type": "catalog_snapshot", "ts": now_iso(), "elapsed": elapsed, **catalog})
                write_jsonl(
                    timeline_path,
                    {
                        "type": "active_product_snapshot" if active_snapshot.get("active") else "active_product_unresolved",
                        "ts": now_iso(),
                        "elapsed": elapsed,
                        **active_snapshot,
                    },
                )
                if signature != last_signature:
                    write_jsonl(
                        timeline_path,
                        {
                            "type": "catalog_change",
                            "ts": now_iso(),
                            "elapsed": elapsed,
                            "count": len(products),
                            "products": products,
                        },
                    )
                    last_signature = signature
                    log(f"Catalog changed: {len(products)} products")
                if active_key and active_key != last_active_key:
                    active = active_snapshot.get("active") or {}
                    active_product = active.get("product") or {}
                    write_jsonl(
                        timeline_path,
                        {
                            "type": "active_product_change",
                            "ts": now_iso(),
                            "elapsed": elapsed,
                            "source": active.get("source"),
                            "confidence": active.get("confidence"),
                            "reason": active.get("reason"),
                            "product": active_product,
                        },
                    )
                    last_active_key = active_key
                    log(f"Active product signal changed: {active_product.get('title') or active_key}")
                next_snapshot = time.time() + max(1.0, args.snapshot_interval)
            time.sleep(0.35)

        rc = ffmpeg_proc.poll() if ffmpeg_proc else 0
        close_ffmpeg_log(ffmpeg_proc)
        size = output_path.stat().st_size if output_path and output_path.exists() else 0
        write_jsonl(
            timeline_path,
            {
                "type": "poc_stop",
                "ts": now_iso(),
                "elapsed": round(time.time() - started_at, 3),
                "returncode": rc,
                "output": str(output_path) if output_path else "",
                "size": size,
            },
        )
        if ffmpeg_proc and rc != 0:
            log(f"ffmpeg ended with rc={rc}. See: {stderr_path}")
            return int(rc or 1)
        if output_path:
            log(f"Recording complete: {output_path} ({size / 1024 / 1024:.1f} MB)")
        log(f"Timeline: {timeline_path}")
        log(f"Catalog: {catalog_path}")
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
        write_jsonl(timeline_path, {"type": "poc_interrupted", "ts": now_iso(), "elapsed": round(time.time() - started_at, 3)})
        return 130
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(run())
