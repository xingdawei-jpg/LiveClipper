const state = {
  page: "smart-cut",
  settingsTab: "ai",
  smartPreview: null,
  mixPreview: null,
  diagnosticsVisible: false,
  videoInfoByTarget: {},
  videoInfoRequestSeq: {},
  pipPoolByPrefix: {},
  pipPoolRequestSeq: {},
  keywordConfig: {},
  progressByScope: {},
  previewDrafts: {},
  previewDraftSaveTimers: {},
  previewDetailSelection: { smart: null, mix: null },
  update: {
    checked: false,
    checking: false,
    installing: false,
    available: false,
    info: null,
    message: "\u672a\u68c0\u67e5",
    error: "",
  },
  featurePreferencesLoading: false,
  featurePreferencesSaveTimer: null,
  logs: {
    "smart-cut": 0,
    settings: 0,
    mix: 0,
    "ai-scan": 0,
    "product-scan": 0,
    dedup: 0,
    "live-rec": 0,
  },
  diagnosticLogs: {
    "smart-cut": 0,
    settings: 0,
    mix: 0,
    "ai-scan": 0,
    "product-scan": 0,
    dedup: 0,
    "live-rec": 0,
  },
};

const settingFields = {
  api_key: "s-api-key",
  base_url: "s-base-url",
  model: "s-model",
  enabled: "s-enabled",
  asr_enabled: "s-asr-enabled",
  asr_provider: "s-asr-provider",
  whisper_model: "s-whisper-model",
  volc_api_key: "s-volc-api-key",
  volc_tos_ak: "s-volc-tos-ak",
  volc_tos_sk: "s-volc-tos-sk",
  volc_bucket: "s-volc-bucket",
  volc_region: "s-volc-region",
  ui_theme: "s-ui-theme",
  hardware_encoder_enabled: "s-hardware-encoder",
  subtitle_font_size: "s-subtitle-font-size",
  ui_font_size: "s-ui-font-size",
};

const keywordFields = {
  clip_keywords: "kw-clip-keywords",
  forbidden_phrases: "kw-forbidden-phrases",
  filler_words: "kw-filler-words",
  preference_keywords: "kw-preference-keywords",
};

const customAiPresetsKey = "lc:custom-ai-presets";
const themeStorageKey = "lc:ui-theme";
const uiFontSizeStorageKey = "lc:ui-font-size";
const previewDraftStoragePrefix = "lc:preview-draft:";
const validThemes = new Set(["system", "light", "dark"]);

const progressScopes = ["smart-cut", "mix", "ai-scan", "product-scan", "dedup", "live-rec", "settings"];

const progressStageRules = [
  { label: "准备素材", percent: 12, tokens: ["任务已启动", "启动", "目标时长", "读取", "上传", "路径"] },
  { label: "标准化素材", percent: 20, tokens: ["TS", "标准化", "normalized", "remux", "转码", "CFR"] },
  { label: "识别字幕", percent: 34, tokens: ["SRT", "字幕", "ASR", "识别", "Whisper", "火山", "阿里云", "语音"] },
  { label: "AI 分析", percent: 52, tokens: ["AI", "候选", "评分", "选片", "片单", "预览"] },
  { label: "去重变速", percent: 68, tokens: ["去重", "变速", "dedup", "speed", "重复"] },
  { label: "剪辑合成", percent: 82, tokens: ["剪辑", "裁剪", "片段", "合成", "混剪", "Cut", "Concat"] },
  { label: "导出成品", percent: 92, tokens: ["导出", "输出", "成品", "保存", "路径"] },
  { label: "已完成", percent: 100, tokens: ["完成", "成功", "ready", "已生成"] },
];

const featurePreferenceGroups = {
  smart_cut: {
    prefixes: ["sc"],
    ids: [
      "output-dir",
      "sc-duration",
      "sc-versions",
      "sc-dedup",
      "sc-mirror",
      "sc-subtitle",
      "sc-crop",
      "sc-crop-level",
      "sc-kenburns",
      "sc-kb-intensity",
      "sc-pip-mode",
      "sc-pip-path",
      "sc-pip-folder",
      "sc-pip-size",
      "sc-pip-opacity",
      "sc-pip-pos",
      "sc-ai-preset",
      "sc-category",
      "sc-focus",
      "sc-goal",
      "sc-hook-style",
      "sc-selling-custom",
      "sc-ending-style",
      "sc-strictness",
    ],
  },
  mix: {
    prefixes: ["mix"],
    ids: [
      "mix-output-dir",
      "mix-duration",
      "mix-versions",
      "mix-dedup",
      "mix-mirror",
      "mix-subtitle",
      "mix-crop",
      "mix-crop-level",
      "mix-kenburns",
      "mix-kb-intensity",
      "mix-pip-mode",
      "mix-pip-path",
      "mix-pip-folder",
      "mix-pip-size",
      "mix-pip-opacity",
      "mix-pip-pos",
      "mix-ai-preset",
      "mix-category",
      "mix-focus",
      "mix-goal",
      "mix-hook-style",
      "mix-selling-custom",
      "mix-ending-style",
      "mix-strictness",
    ],
  },
  dedup: {
    prefixes: [],
    ids: [
      "dedup-output-dir",
      "dedup-preset",
      "dedup-mirror",
      "dedup-crop",
      "dedup-crop-value",
      "dedup-speed",
      "dedup-speed-value",
      "dedup-frame-structure",
      "dedup-frame-level",
      "dedup-blur",
      "dedup-blur-value",
      "dedup-sharpen",
      "dedup-sharpen-value",
      "dedup-color",
      "dedup-mask",
      "dedup-bg-fill",
      "dedup-bg-image",
      "dedup-pip-mode",
      "dedup-pip-path",
      "dedup-pip-folder",
      "dedup-pip-size",
      "dedup-pip-opacity",
      "dedup-pip-pos",
      "dedup-audio-pitch",
      "dedup-audio-reverb",
      "dedup-noise-fusion",
    ],
  },
  product_scan: {
    prefixes: [],
    ids: [
      "ps-output-dir",
      "ps-advance",
      "ps-video-start-offset",
      "ps-live-start-time",
    ],
  },
};
const featurePreferenceControlIds = new Set(
  Object.values(featurePreferenceGroups).flatMap((group) => group.ids)
);
const featurePreferenceAiControls = new Set(["sc-selling", "sc-avoid", "mix-selling", "mix-avoid"]);

function normalizeTheme(theme) {
  return validThemes.has(theme) ? theme : "system";
}

function applyTheme(theme) {
  const normalized = normalizeTheme(theme);
  document.documentElement.dataset.theme = normalized;
  try {
    localStorage.setItem(themeStorageKey, normalized);
  } catch {
    // localStorage can be unavailable in restricted browser contexts.
  }
  const select = $("s-ui-theme");
  if (select && select.value !== normalized) select.value = normalized;
}

function normalizeUiFontSize(value) {
  const size = Number(value || 14);
  return Math.max(12, Math.min(18, Number.isFinite(size) ? Math.round(size) : 14));
}

function applyUiFontSize(value) {
  const size = normalizeUiFontSize(value);
  document.documentElement.style.setProperty("--ui-font-size", `${size}px`);
  try {
    localStorage.setItem(uiFontSizeStorageKey, String(size));
  } catch {
    // localStorage can be unavailable in restricted browser contexts.
  }
  const input = $("s-ui-font-size");
  const label = $("s-ui-font-size-value");
  if (input && input.value !== String(size)) input.value = String(size);
  if (label) label.textContent = String(size);
}

applyTheme(localStorage.getItem(themeStorageKey) || document.documentElement.dataset.theme || "system");
applyUiFontSize(localStorage.getItem(uiFontSizeStorageKey) || 14);

const aiPresets = {
  viral: {
    label: "爆款种草",
    goal: "爆款种草",
    focus: "情绪感染",
    hook: "爆点金句开头",
    ending: "信任背书",
    strictness: "标准",
    selling: ["版型显瘦", "颜色氛围", "情绪感染"],
    avoid: ["价格", "闲聊", "搭配其他品"],
  },
  slim: {
    label: "显瘦转化",
    goal: "显瘦转化",
    focus: "版型显瘦",
    hook: "痛点开头",
    ending: "尺码引导",
    strictness: "严格",
    selling: ["版型显瘦", "尺寸长度"],
    avoid: ["价格", "闲聊", "搭配其他品", "重复卖点"],
  },
  quality: {
    label: "质感高级",
    goal: "质感高级",
    focus: "面料质感",
    hook: "上身效果开头",
    ending: "场景收尾",
    strictness: "标准",
    selling: ["面料质感", "品质细节", "颜色氛围"],
    avoid: ["价格", "库存", "闲聊"],
  },
  commute: {
    label: "通勤场景",
    goal: "专业讲解",
    focus: "场景搭配",
    hook: "上身效果开头",
    ending: "场景收尾",
    strictness: "标准",
    selling: ["场景搭配", "穿着体验", "面料质感"],
    avoid: ["价格", "闲聊"],
  },
  fast: {
    label: "快速促单",
    goal: "快速促单",
    focus: "紧迫稀缺",
    hook: "主播强推荐开头",
    ending: "尺码引导",
    strictness: "标准",
    selling: ["紧迫稀缺", "版型显瘦", "尺寸长度"],
    avoid: ["价格", "搭配其他品"],
  },
  gentle: {
    label: "温柔讲解",
    goal: "专业讲解",
    focus: "面料质感",
    hook: "上身效果开头",
    ending: "自然结束",
    strictness: "宽松",
    selling: ["面料质感", "穿着体验", "场景搭配"],
    avoid: ["价格", "库存", "闲聊"],
  },
};

document.addEventListener("DOMContentLoaded", () => {
  bindNavigation();
  bindSettingsTabs();
  bindActions();
  bindAiPresetControls();
  bindPreviewControls();
  bindFeaturePreferenceAutoSave();
  setupCollapsiblePanels();
  setupAdvancedParamToggles();
  setupLogProgressBars();
  bindPreviewModalShortcuts();
  loadRuntime();
  loadSettings();
  loadFeaturePreferences();
  loadKeywords();
  loadLicense();
  connectLogSocket();
  refreshTasks();
  loadScanResults();
  loadLatestSmartPreview();
  loadLatestMixPreview();
  renderUpdateState();
  setTimeout(() => {
    checkUpdate({ quiet: true }).catch((error) => {
      console.warn("Update check failed", error);
    });
  }, 1200);
  setInterval(refreshTasks, 2500);
  setInterval(loadScanResults, 4000);
  setInterval(loadLatestSmartPreview, 5000);
  setInterval(loadLatestMixPreview, 5000);
  window.addEventListener("resize", () => {
    updatePreviewStickyOffset("smart");
    updatePreviewStickyOffset("mix");
  });
});

function $(id) {
  return document.getElementById(id);
}

async function api(path, options = {}) {
  const headers = {
    "Content-Type": "application/json",
    ...(options.headers || {}),
  };
  let response;
  try {
    response = await fetch(path, { ...options, headers });
  } catch (error) {
    throw new Error("本地服务连接失败，请刷新页面；如果仍失败，请重新启动 Web 客户端。");
  }
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json") ? await response.json() : await response.text();
  sanitizeApiPayload(body);
  if (!response.ok) {
    const detail = typeof body === "object" ? body.detail || body.message : body;
    throw new Error(sanitizeApiText(detail, "\u8bf7\u6c42\u5931\u8d25\uff0c\u8bf7\u68c0\u67e5\u9875\u9762\u53c2\u6570\u540e\u91cd\u8bd5\u3002") || `HTTP ${response.status}`);
  }
  return body;
}

async function upload(path, formData) {
  let response;
  try {
    response = await fetch(path, { method: "POST", body: formData });
  } catch (error) {
    throw new Error("本地服务连接失败，文件没有上传成功。");
  }
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json") ? await response.json() : await response.text();
  sanitizeApiPayload(body);
  if (!response.ok) {
    const detail = typeof body === "object" ? body.detail || body.message : body;
    throw new Error(sanitizeApiText(detail, "\u4e0a\u4f20\u5931\u8d25\uff0c\u8bf7\u91cd\u8bd5\u3002") || `HTTP ${response.status}`);
  }
  return body;
}

function looksGarbledText(value) {
  const text = String(value || "");
  return /[\uE000-\uF8FF\uFFFD]|锛|銆|鐨|璇|绋|鏅|浜|妫|棰|[ÃÂÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖØÙÚÛÜÝÞßàáâãäåæçèéêëìíîï]/.test(text);
}

function repairMojibakeText(value) {
  if (typeof value !== "string" || !looksGarbledText(value)) return value;
  try {
    const bytes = Uint8Array.from(Array.from(value), (ch) => ch.charCodeAt(0) & 0xff);
    const repaired = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    const oldScore = (value.match(/[\uE000-\uF8FF\uFFFD]|[ÃÂÄÅåæçèé]/g) || []).length;
    const newScore = (repaired.match(/[\uE000-\uF8FF\uFFFD]|[ÃÂÄÅåæçèé]/g) || []).length;
    if (repaired && newScore < oldScore) return repaired;
  } catch (error) {
    // Keep the original text when it is not reversible mojibake.
  }
  return value;
}

function sanitizeApiText(value, fallback = "\u64cd\u4f5c\u5df2\u63d0\u4ea4\u3002") {
  if (typeof value !== "string") return value;
  const repaired = repairMojibakeText(value);
  return looksGarbledText(repaired) ? fallback : repaired;
}

function sanitizeApiPayload(value) {
  if (!value || typeof value !== "object") return value;
  const fallbackByKey = {
    message: "\u64cd\u4f5c\u5df2\u63d0\u4ea4\u3002",
    detail: "\u8bf7\u6c42\u672a\u901a\u8fc7\uff0c\u8bf7\u68c0\u67e5\u9875\u9762\u53c2\u6570\u540e\u91cd\u8bd5\u3002",
    error: "\u4efb\u52a1\u5931\u8d25\uff0c\u8bf7\u68c0\u67e5\u7d20\u6750\u548c\u8bbe\u7f6e\u3002",
    summary: "\u68c0\u6d4b\u5b8c\u6210\u3002",
  };
  Object.entries(value).forEach(([key, item]) => {
    if (item && typeof item === "object") sanitizeApiPayload(item);
    else if (key in fallbackByKey) value[key] = sanitizeApiText(item, fallbackByKey[key]);
    else if (typeof item === "string") value[key] = repairMojibakeText(item);
  });
  return value;
}

function bindNavigation() {
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => switchPage(button.dataset.page));
  });
}

function switchPage(page) {
  state.page = page;
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.classList.toggle("is-active", item.dataset.page === page);
  });
  document.querySelectorAll(".page").forEach((section) => {
    section.classList.toggle("is-active", section.id === `page-${page}`);
  });
  if (page === "settings") {
    loadSettings();
    loadKeywords();
  }
}

function bindSettingsTabs() {
  document.querySelectorAll(".settings-tab").forEach((button) => {
    button.addEventListener("click", () => {
      const tab = button.dataset.settingsTab;
      state.settingsTab = tab;
      document.querySelectorAll(".settings-tab").forEach((item) => {
        item.classList.toggle("is-active", item.dataset.settingsTab === tab);
      });
      document.querySelectorAll(".settings-page").forEach((page) => {
        page.classList.toggle("is-active", page.id === `settings-${tab}`);
      });
    });
  });
}

function bindActions() {
  document.body.addEventListener("click", async (event) => {
    const target = event.target.closest("[data-action]");
    if (!target) return;
    const action = target.dataset.action;

    try {
      if (action === "add-path") addPath(target.dataset.input, target.dataset.target);
      if (action === "pick-videos") await pickVideos(target.dataset.target, target);
      if (action === "pick-file") await pickFile(target.dataset.target, target.dataset.kind || "file");
      if (action === "pick-directory") await pickDirectory(target.dataset.target);
      if (action === "open-path") await openPath(target.dataset.target);
      if (action === "stop-scope") await stopScope(target.dataset.scope || state.page);
      if (action === "clear-video-list") clearVideoList(target.dataset.target);
      if (action === "remove-video") removeVideoPath(target.dataset.target, Number(target.dataset.index));
      if (action === "move-video") moveVideoPath(target.dataset.target, Number(target.dataset.index), Number(target.dataset.direction));
      if (action === "start-smart-preview") await startSmartPreview();
      if (action === "start-smart-from-preview") await startSmartFromPreview();
      if (action === "start-mix-preview") await startMixPreview();
      if (action === "start-mix-from-preview") await startMixFromPreview();
      if (action === "preview-clip-video") await previewClipVideo(Number(target.dataset.previewIndex), target.dataset.previewScope || "smart");
      if (action === "close-preview-video") closePreviewVideo(target.dataset.previewScope || "smart");
      if (action === "start-smart-cut") await startSmartCut();
      if (action === "clear-log") clearLog(target.dataset.log);
      if (action === "reload-settings") await loadSettings(true);
      if (action === "save-settings") await saveSettings();
      if (action === "test-ai") await testAI();
      if (action === "diagnose-volc") await diagnoseVolcengine();
      if (action === "load-keywords") await loadKeywords(true);
      if (action === "open-keyword-editor") await openKeywordEditor();
      if (action === "close-keyword-editor") closeKeywordEditor();
      if (action === "save-keywords") await saveKeywords();
      if (action === "reset-keywords") await resetKeywords();
      if (action === "clear-cache") await clearCache();
      if (action === "toggle-diagnostics") toggleDiagnostics();
      if (action === "save-ai-preset") saveCurrentAiPreset(target.dataset.prefix);
      if (action === "delete-ai-preset") deleteCurrentAiPreset(target.dataset.prefix);
      if (action === "feature-start") await startFeature(target.dataset.feature);
      if (action === "feature-submit") await submitFeature(target.dataset.feature);
      if (action === "reset-dedup") resetDedupDefaults();
      if (action === "add-live-room") addLiveRoom();
      if (action === "toggle-secret") toggleSecret(target);
      if (action === "activate-license") await activateLicense();
      if (action === "unbind-device") await unbindDevice();
      if (action === "check-update") await checkUpdate();
      if (action === "apply-update") await applyUpdate();
      if (action === "toggle-update-card") toggleUpdateCard();
      if (action === "close-update-card") closeUpdateCard();
      if (action === "feedback") feedback();
    } catch (error) {
      toast(error.message || String(error), "error");
    }
  });

  document.querySelectorAll(".path-entry input").forEach((input) => {
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      const targetId = input.dataset.pathTarget;
      if (!targetId) return;
      addVideoPaths(targetId, [input.value]);
      input.value = "";
    });
  });

  document.querySelectorAll("[data-pref-key]").forEach((input) => {
    input.addEventListener("input", () => syncPreferenceSlider(input));
  });
  $("s-ui-font-size")?.addEventListener("input", (event) => applyUiFontSize(event.target.value));
  $("s-subtitle-font-size")?.addEventListener("input", syncSubtitleFontSize);
  $("s-ui-theme")?.addEventListener("change", (event) => {
    applyTheme(event.target.value);
  });

  bindVideoDropzones();
  bindFileDropTargets();
  injectDiagnosticButtons();
  ["video-paths", "mix-video-paths", "scan-video-paths", "ps-video-paths", "dedup-video-paths"].forEach(renderVideoList);
  document.querySelectorAll(".path-box").forEach((box) => {
    box.addEventListener("input", () => renderVideoList(box.id));
  });
}

function injectDiagnosticButtons() {
  document.querySelectorAll(".log-panel .panel-header").forEach((header) => {
    const panel = header.closest(".log-panel");
    const logView = panel?.querySelector(".log-view");
    const scope = logView?.id?.replace(/^log-/, "") || "";
    let actions = header.querySelector(".log-header-actions");
    if (!actions) {
      actions = document.createElement("div");
      actions.className = "log-header-actions";
      Array.from(header.children).forEach((child) => {
        if (child.classList?.contains("log-count")) actions.appendChild(child);
      });
      header.appendChild(actions);
    }
    ensureDiagnosticLogView(scope);
    if (actions.querySelector("[data-action='toggle-diagnostics']")) return;
    const button = document.createElement("button");
    button.className = "button button-muted button-small diagnostic-toggle";
    button.type = "button";
    button.dataset.action = "toggle-diagnostics";
    button.dataset.scope = scope;
    button.textContent = "高级诊断";
    actions.appendChild(button);
  });
}

function ensureDiagnosticLogView(scope) {
  if (!scope || $(`log-${scope}-diagnostics`)) return $(`log-${scope}-diagnostics`);
  const logView = $(`log-${scope}`);
  if (!logView) return null;
  const box = document.createElement("div");
  box.id = `log-${scope}-diagnostics`;
  box.className = "diagnostic-log-view";
  box.innerHTML = '<div class="diagnostic-empty">高级诊断打开后，这里显示 AI、切割、拼接、字幕和 FFmpeg 细节。</div>';
  logView.insertAdjacentElement("afterend", box);
  return box;
}

function toggleDiagnostics() {
  state.diagnosticsVisible = !state.diagnosticsVisible;
  document.body.classList.toggle("show-diagnostics", state.diagnosticsVisible);
  document.querySelectorAll(".diagnostic-toggle").forEach((button) => {
    button.classList.toggle("is-active", state.diagnosticsVisible);
    updateDiagnosticButton(button);
  });
}

function updateDiagnosticButton(button) {
  const scope = button?.dataset?.scope || "";
  const count = state.diagnosticLogs[scope] || 0;
  const suffix = count ? ` ${count}` : "";
  button.textContent = state.diagnosticsVisible ? `收起诊断${suffix}` : `高级诊断${suffix}`;
}

function bindAiPresetControls() {
  refreshAiPresetOptions();
  document.querySelectorAll("[data-ai-preset]").forEach((select) => {
    select.addEventListener("change", () => {
      applyAiPreset(select.dataset.aiPreset, select.value);
    });
  });

  ["sc", "mix"].forEach((prefix) => {
    [`${prefix}-focus`, `${prefix}-goal`, `${prefix}-hook-style`, `${prefix}-ending-style`, `${prefix}-strictness`].forEach((id) => {
      $(id)?.addEventListener("change", () => markAiPresetCustom(prefix));
    });
    document.querySelectorAll(`[data-ai-control="${prefix}-selling"], [data-ai-control="${prefix}-avoid"]`).forEach((input) => {
      input.addEventListener("change", () => markAiPresetCustom(prefix));
    });
    $(`${prefix}-selling-custom`)?.addEventListener("input", () => markAiPresetCustom(prefix));
  });
}

function readCustomAiPresets() {
  try {
    const data = JSON.parse(localStorage.getItem(customAiPresetsKey) || "{}");
    return data && typeof data === "object" ? data : {};
  } catch (error) {
    return {};
  }
}

function writeCustomAiPresets(data) {
  localStorage.setItem(customAiPresetsKey, JSON.stringify(data || {}));
}

function allAiPresets() {
  return { ...aiPresets, ...readCustomAiPresets() };
}

function refreshAiPresetOptions() {
  const custom = readCustomAiPresets();
  document.querySelectorAll("[data-ai-preset]").forEach((select) => {
    const current = select.value;
    select.querySelectorAll("option[data-custom-preset]").forEach((option) => option.remove());
    Object.entries(custom).forEach(([key, preset]) => {
      const option = document.createElement("option");
      option.value = key;
      option.dataset.customPreset = "1";
      option.textContent = `我的：${preset.label || "未命名"}`;
      select.appendChild(option);
    });
    if (current && Array.from(select.options).some((option) => option.value === current)) {
      select.value = current;
    }
  });
}

function markAiPresetCustom(prefix) {
  const preset = $(`${prefix}-ai-preset`);
  if (preset && preset.value !== "custom") {
    preset.value = "custom";
  }
}

function applyAiPreset(prefix, presetKey) {
  if (!prefix || presetKey === "custom") return;
  const preset = allAiPresets()[presetKey];
  if (!preset) return;
  setSelectIfPresent(`${prefix}-focus`, preset.focus);
  setSelectIfPresent(`${prefix}-goal`, preset.goal);
  setSelectIfPresent(`${prefix}-hook-style`, preset.hook);
  setSelectIfPresent(`${prefix}-ending-style`, preset.ending);
  setSelectIfPresent(`${prefix}-strictness`, preset.strictness);
  setCheckedValues(`${prefix}-selling`, preset.selling);
  setCheckedValues(`${prefix}-avoid`, preset.avoid);
  const customInput = $(`${prefix}-selling-custom`);
  if (customInput) customInput.value = (preset.selling_custom || []).join("，");
  toast(`已套用「${preset.label}」选片预设`, "success");
}

function customSellingValues(prefix) {
  const customInput = $(`${prefix}-selling-custom`);
  if (!customInput) return [];
  return customInput.value
    .split(/[,，、\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function collectCurrentAiPreset(prefix, label) {
  return {
    label,
    goal: $(`${prefix}-goal`)?.value || "自动",
    focus: $(`${prefix}-focus`)?.value || "自动",
    hook: $(`${prefix}-hook-style`)?.value || "自动",
    ending: $(`${prefix}-ending-style`)?.value || "自动",
    strictness: $(`${prefix}-strictness`)?.value || "标准",
    selling: checkedControlValues(`${prefix}-selling`),
    selling_custom: customSellingValues(prefix),
    avoid: checkedControlValues(`${prefix}-avoid`),
  };
}

function saveCurrentAiPreset(prefix) {
  if (!prefix) return;
  const label = prompt("给这套 AI 参数起个名字：", "我的选片预设");
  if (!label || !label.trim()) return;
  const custom = readCustomAiPresets();
  const key = `custom-${Date.now()}`;
  custom[key] = collectCurrentAiPreset(prefix, label.trim().slice(0, 18));
  writeCustomAiPresets(custom);
  refreshAiPresetOptions();
  const select = $(`${prefix}-ai-preset`);
  if (select) select.value = key;
  toast(`已保存「${custom[key].label}」`, "success");
}

function deleteCurrentAiPreset(prefix) {
  const select = $(`${prefix}-ai-preset`);
  if (!select || !select.value) return;
  const key = select.value;
  if (!key.startsWith("custom-")) {
    toast("系统预设不能删除，可以保存成自己的预设后再调整。", "warning");
    return;
  }
  const custom = readCustomAiPresets();
  const label = custom[key]?.label || "自定义预设";
  if (!confirm(`删除「${label}」吗？`)) return;
  delete custom[key];
  writeCustomAiPresets(custom);
  refreshAiPresetOptions();
  select.value = "custom";
  toast("已删除自定义预设", "success");
}

function setSelectIfPresent(id, value) {
  const select = $(id);
  if (!select) return;
  const hasValue = Array.from(select.options).some((option) => option.value === value || option.textContent.trim() === value);
  if (hasValue) {
    select.value = value;
  }
}

function setCheckedValues(controlName, values = []) {
  const wanted = new Set(values);
  document.querySelectorAll(`[data-ai-control="${controlName}"]`).forEach((input) => {
    input.checked = wanted.has(input.value);
  });
}

function controlValue(id) {
  const element = $(id);
  if (!element) return undefined;
  return element.type === "checkbox" ? Boolean(element.checked) : element.value;
}

function setControlValue(id, value) {
  const element = $(id);
  if (!element || value === undefined || value === null) return;
  if (element.type === "checkbox") {
    element.checked = Boolean(value);
    return;
  }
  if (element.tagName === "SELECT") {
    const wanted = String(value);
    const exists = Array.from(element.options || []).some((option) => option.value === wanted || option.textContent === wanted);
    if (!exists) return;
  }
  element.value = String(value);
}

function collectControlGroup(group) {
  const values = {};
  group.ids.forEach((id) => {
    const value = controlValue(id);
    if (value !== undefined) values[id] = value;
  });
  const ai = {};
  (group.prefixes || []).forEach((prefix) => {
    ai[`${prefix}-selling`] = checkedControlValues(`${prefix}-selling`);
    ai[`${prefix}-avoid`] = checkedControlValues(`${prefix}-avoid`);
  });
  return { values, ai };
}

function collectFeaturePreferences() {
  const preferences = { version: 1 };
  Object.entries(featurePreferenceGroups).forEach(([key, group]) => {
    preferences[key] = collectControlGroup(group);
  });
  return preferences;
}

function applyControlGroup(group, saved) {
  if (!saved || typeof saved !== "object") return;
  const values = saved.values || {};
  group.ids.forEach((id) => setControlValue(id, values[id]));
  const ai = saved.ai || {};
  (group.prefixes || []).forEach((prefix) => {
    if (Array.isArray(ai[`${prefix}-selling`])) setCheckedValues(`${prefix}-selling`, ai[`${prefix}-selling`]);
    if (Array.isArray(ai[`${prefix}-avoid`])) setCheckedValues(`${prefix}-avoid`, ai[`${prefix}-avoid`]);
  });
}

function refreshFeaturePreferenceUi() {
  document.querySelectorAll("[data-collapsible-panel]").forEach((panel) => updatePanelSummary(panel));
  ["sc", "mix", "dedup"].forEach((prefix) => refreshPipPool(prefix));
}

async function loadFeaturePreferences() {
  state.featurePreferencesLoading = true;
  try {
    const result = await api("/api/preferences");
    const preferences = result.preferences || {};
    Object.entries(featurePreferenceGroups).forEach(([key, group]) => {
      applyControlGroup(group, preferences[key]);
    });
    refreshFeaturePreferenceUi();
  } catch (error) {
    console.warn("Failed to load feature preferences", error);
  } finally {
    state.featurePreferencesLoading = false;
  }
}

async function saveFeaturePreferences() {
  if (state.featurePreferencesLoading) return;
  try {
    await api("/api/preferences", {
      method: "POST",
      body: JSON.stringify(collectFeaturePreferences()),
    });
  } catch (error) {
    console.warn("Failed to save feature preferences", error);
  }
}

function scheduleFeaturePreferenceSave() {
  if (state.featurePreferencesLoading) return;
  clearTimeout(state.featurePreferencesSaveTimer);
  state.featurePreferencesSaveTimer = setTimeout(() => saveFeaturePreferences(), 400);
}

function isFeaturePreferenceControl(target) {
  if (!target) return false;
  if (target.id && featurePreferenceControlIds.has(target.id)) return true;
  const aiControl = target.dataset?.aiControl;
  return Boolean(aiControl && featurePreferenceAiControls.has(aiControl));
}

function bindFeaturePreferenceAutoSave() {
  ["input", "change"].forEach((eventName) => {
    document.body.addEventListener(eventName, (event) => {
      if (isFeaturePreferenceControl(event.target)) scheduleFeaturePreferenceSave();
    });
  });
}

function bindPreviewControls() {
  document.body.addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-preview-segment-toggle]");
    if (toggle) {
      event.preventDefault();
      togglePreviewSegments(
        Number(toggle.dataset.previewClip),
        toggle.dataset.previewScope || "smart"
      );
      return;
    }
    const row = event.target.closest("[data-preview-segment-row]");
    if (row && !event.target.closest("input, button")) {
      event.preventDefault();
      const input = row.querySelector("[data-preview-segment]");
      if (!input) return;
      input.checked = !input.checked;
      updatePreviewSegmentSelection(
        Number(row.dataset.previewSegmentParent),
        Number(row.dataset.previewSegmentIndex),
        input.checked,
        row.dataset.previewScope || "smart"
      );
      return;
    }
    const previewRow = event.target.closest("[data-preview-row]");
    if (!previewRow || event.target.closest("input, button, a, label, [data-action]")) return;
    event.preventDefault();
    setPreviewDetailSelection(
      previewRow.dataset.previewScope || "smart",
      Number(previewRow.dataset.previewIndex)
    );
  });

  document.body.addEventListener("change", (event) => {
    const segment = event.target.closest("[data-preview-segment]");
    if (segment) {
      updatePreviewSegmentSelection(
        Number(segment.dataset.previewSegmentParent),
        Number(segment.dataset.previewSegmentIndex),
        segment.checked,
        segment.dataset.previewScope || "smart"
      );
      return;
    }
    const target = event.target.closest("[data-preview-clip]");
    if (!target) return;
    updatePreviewClipSelection(Number(target.dataset.previewClip), target.checked, target.dataset.previewScope || "smart");
  });
}

function bindPreviewModalShortcuts() {
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    closeKeywordEditor();
    closePreviewVideo();
    closeUpdateCard();
  });
}

function setupCollapsiblePanels() {
  document.querySelectorAll("[data-collapsible-panel]").forEach((panel) => {
    const id = panel.dataset.collapsiblePanel;
    const header = panel.querySelector(".panel-header");
    if (!id || !header || header.querySelector(".collapse-toggle")) return;
    const saved = localStorage.getItem(`lc:panel:${id}`);
    if (!saved && panel.dataset.defaultOpen === "true") panel.classList.remove("is-collapsed");
    if (saved === "open") panel.classList.remove("is-collapsed");
    if (saved === "closed") panel.classList.add("is-collapsed");

    const summary = document.createElement("span");
    summary.className = "panel-summary";
    header.appendChild(summary);

    const button = document.createElement("button");
    button.type = "button";
    button.className = "button button-muted button-small collapse-toggle";
    button.addEventListener("click", () => {
      panel.classList.toggle("is-collapsed");
      localStorage.setItem(`lc:panel:${id}`, panel.classList.contains("is-collapsed") ? "closed" : "open");
      updatePanelSummary(panel);
    });
    header.appendChild(button);

    panel.addEventListener("change", () => updatePanelSummary(panel));
    panel.addEventListener("input", () => updatePanelSummary(panel));
    updatePanelSummary(panel);
  });
}

function setupAdvancedParamToggles() {
  document.querySelectorAll("[data-advanced-panel]").forEach((panel) => {
    const id = panel.dataset.advancedPanel;
    const header = panel.querySelector(".panel-header");
    if (!id || !header || header.querySelector("[data-action='toggle-advanced-panel']")) return;
    const saved = localStorage.getItem(`lc:advanced:${id}`);
    panel.classList.toggle("show-advanced", saved === "open");

    const button = document.createElement("button");
    button.type = "button";
    button.className = "button button-muted button-small advanced-toggle";
    button.dataset.action = "toggle-advanced-panel";
    button.dataset.advancedPanel = id;
    button.textContent = panel.classList.contains("show-advanced") ? "收起高级" : "高级参数";
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      panel.classList.toggle("show-advanced");
      localStorage.setItem(`lc:advanced:${id}`, panel.classList.contains("show-advanced") ? "open" : "closed");
      button.textContent = panel.classList.contains("show-advanced") ? "收起高级" : "高级参数";
    });
    header.appendChild(button);
  });
}

function setupLogProgressBars() {
  document.querySelectorAll(".log-panel .log-view").forEach((logView) => {
    const scope = (logView.id || "").replace(/^log-/, "");
    if (!scope || logView.parentElement?.querySelector(`[data-log-progress="${scope}"]`)) return;

    const progress = document.createElement("div");
    progress.className = "log-progress is-idle";
    progress.dataset.logProgress = scope;
    progress.innerHTML = `
      <div class="log-progress-meta">
        <span class="log-progress-title">进度</span>
        <strong class="log-progress-label">等待任务</strong>
        <span class="log-progress-percent">0%</span>
      </div>
      <div class="log-progress-track"><span class="log-progress-fill"></span></div>
    `;
    logView.parentElement?.insertBefore(progress, logView);
    updateLogProgressBar(scope, { label: "等待任务", percent: 0, status: "idle" });
  });
}

function updateLogProgressFromTasks(tasks = []) {
  progressScopes.forEach((scope) => {
    const scoped = scopedProgressTasks(tasks, scope);
    const active = newestTask(scoped.filter((task) => ["queued", "running"].includes(task.status)));
    const latest = newestTask(scoped);
    const current = state.progressByScope[scope];

    if (active) {
      updateLogProgressBar(scope, progressFromTask(active));
      return;
    }
    if (latest && current?.taskId === latest.id) {
      updateLogProgressBar(scope, progressFromTask(latest));
      return;
    }
    if (!current) {
      updateLogProgressBar(scope, { label: "等待任务", percent: 0, status: "idle" });
    }
  });
}

function scopedProgressTasks(tasks, scope) {
  return (tasks || []).filter((task) => task.scope === scope || (scope === "settings" && task.scope === "settings"));
}

function newestTask(tasks) {
  return [...tasks].sort((a, b) => taskTime(b) - taskTime(a))[0] || null;
}

function taskTime(task) {
  const fromId = String(task?.id || "").match(/-(\d{10,})-/)?.[1];
  return Number(task?.finished_at || task?.started_at || fromId || 0);
}

function progressFromTask(task) {
  const scope = task.scope || "settings";
  const status = task.status || "queued";
  const text = [task.title, task.message, task.error].filter(Boolean).join(" ");
  const inferred = inferProgressStage(text);
  const explicitProgress = Number(task.progress);
  const hasExplicitProgress = Number.isFinite(explicitProgress);
  const statusLabels = {
    queued: "排队中",
    running: task.message || inferred.label || "运行中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已停止",
  };

  if (status === "completed") return { taskId: task.id, label: statusLabels.completed, percent: 100, status, source: "task" };
  if (status === "failed") return { taskId: task.id, label: task.error || statusLabels.failed, percent: 100, status, source: "task" };
  if (status === "cancelled") return { taskId: task.id, label: statusLabels.cancelled, percent: 100, status, source: "task" };
  if (status === "queued") {
    return { taskId: task.id, label: task.message || statusLabels.queued, percent: hasExplicitProgress ? explicitProgress : 6, status, source: "task" };
  }

  const previous = state.progressByScope[scope];
  const previousPercent = previous?.taskId === task.id ? previous.percent || 0 : 0;
  return {
    taskId: task.id,
    label: statusLabels.running,
    percent: Math.max(previousPercent, hasExplicitProgress ? explicitProgress : inferred.percent || 10),
    status,
    source: "task",
  };
}

function updateProgressFromLog(scope, item = {}) {
  const level = String(item.level || "info").toLowerCase();
  const text = [item.message, item.raw].filter(Boolean).join(" ");
  if (!scope || !text) return;

  if (level === "error") {
    updateLogProgressBar(scope, { label: "处理失败", percent: 100, status: "failed" });
    return;
  }
  if (level === "success" || /任务完成|成片完成|混剪完成|预览完成|处理完成|成功|已生成/.test(text)) {
    updateLogProgressBar(scope, { label: "已完成", percent: 100, status: "completed" });
    return;
  }

  const inferred = inferProgressStage(text);
  if (!inferred.label) return;
  const previous = state.progressByScope[scope] || {};
  if (previous.source === "task" && previous.taskId && previous.status === "running") return;
  updateLogProgressBar(scope, {
    label: inferred.label,
    percent: Math.max(previous.percent || 0, inferred.percent),
    status: "running",
    taskId: previous.taskId,
  });
}

function inferProgressStage(text) {
  const value = String(text || "");
  for (const stage of progressStageRules) {
    if (stage.tokens.some((token) => value.includes(token))) {
      return { label: stage.label, percent: stage.percent };
    }
  }
  return { label: "", percent: 0 };
}

function updateLogProgressBar(scope, progress) {
  const el = document.querySelector(`[data-log-progress="${scope}"]`);
  if (!el) return;
  const percent = Math.max(0, Math.min(100, Math.round(Number(progress.percent) || 0)));
  const status = progress.status || "idle";
  const label = progress.label || "等待任务";
  state.progressByScope[scope] = { ...progress, percent, label, status };

  el.className = `log-progress is-${status}`;
  const labelEl = el.querySelector(".log-progress-label");
  const percentEl = el.querySelector(".log-progress-percent");
  const fill = el.querySelector(".log-progress-fill");
  if (labelEl) labelEl.textContent = label;
  if (percentEl) percentEl.textContent = `${percent}%`;
  if (fill) fill.style.width = `${percent}%`;
}

function resetLogProgress(scope) {
  delete state.progressByScope[scope];
  updateLogProgressBar(scope, { label: "等待任务", percent: 0, status: "idle" });
}

function fieldText(id, fallback = "-") {
  const el = $(id);
  if (!el) return fallback;
  if (el.tagName === "SELECT") {
    return el.selectedOptions?.[0]?.textContent?.trim() || el.value || fallback;
  }
  return el.value?.trim() || fallback;
}

function checkedText(id, label) {
  return $(id)?.checked ? label : "";
}

function selectedAiValues(name) {
  return Array.from(document.querySelectorAll(`[data-ai-control="${name}"]:checked`))
    .map((input) => input.parentElement?.textContent?.trim() || input.value)
    .filter(Boolean);
}

function updatePanelSummary(panel) {
  const prefix = panel.dataset.summaryPrefix;
  const kind = panel.dataset.summaryKind;
  const summary = panel.querySelector(".panel-summary");
  const button = panel.querySelector(".collapse-toggle");
  if (!prefix || !kind || !summary || !button) return;
  let text = "";
  if (kind === "params") {
    const duration = fieldText(`${prefix}-duration`);
    const versions = fieldText(`${prefix}-versions`);
    const dedup = fieldText(`${prefix}-dedup`);
    const flags = [
      checkedText(`${prefix}-subtitle`, "字幕"),
      checkedText(`${prefix}-crop`, "裁切"),
      checkedText(`${prefix}-kenburns`, "缩放"),
      checkedText(`${prefix}-mirror`, "镜像"),
    ].filter(Boolean).join("、") || "基础模式";
    text = `${duration} · ${versions}版 · ${dedup}去重 · ${flags}`;
  } else if (kind === "pip") {
    const mode = fieldText(`${prefix}-pip-mode`, "关闭");
    if ($( `${prefix}-pip-mode`)?.value === "off") {
      text = "关闭";
    } else {
      const source = fieldText(`${prefix}-pip-folder`, "") || fieldText(`${prefix}-pip-path`, "") || mode;
      const pool = state.pipPoolByPrefix[prefix];
      const poolText = pool ? (pool.empty ? `素材为空 · 已用${pool.used}` : `剩余${pool.remaining} · 已用${pool.used}`) : "";
      text = `${mode} · ${fieldText(`${prefix}-pip-size`)} · ${fieldText(`${prefix}-pip-opacity`)} · ${fieldText(`${prefix}-pip-pos`)}${poolText ? ` · ${poolText}` : ""} · ${source}`;
      inspectPipPool(prefix);
    }
  } else if (kind === "ai") {
    const preset = fieldText(`${prefix}-ai-preset`, "自定义");
    const category = fieldText(`${prefix}-category`, "自动");
    const focus = fieldText(`${prefix}-focus`, "自动");
    const goal = fieldText(`${prefix}-goal`, "自动");
    const selling = selectedAiValues(`${prefix}-selling`);
    const avoid = selectedAiValues(`${prefix}-avoid`);
    const ruleText = [
      selling.length ? `优先${selling.slice(0, 3).join("、")}` : "",
      avoid.length ? `排除${avoid.slice(0, 2).join("、")}` : "",
    ].filter(Boolean).join(" · ");
    text = `${preset} · ${category} · ${focus} · ${goal}${ruleText ? ` · ${ruleText}` : ""}`;
  }
  summary.textContent = text;
  button.textContent = panel.classList.contains("is-collapsed") ? "展开" : "收起";
}

async function inspectPipPool(prefix) {
  const folder = $(`${prefix}-pip-folder`)?.value.trim() || "";
  if (!folder) {
    delete state.pipPoolByPrefix[prefix];
    return;
  }
  if (state.pipPoolByPrefix[prefix]?.folder === folder) return;
  const seq = (state.pipPoolRequestSeq[prefix] || 0) + 1;
  state.pipPoolRequestSeq[prefix] = seq;
  try {
    const result = await api("/api/pip/inspect", {
      method: "POST",
      body: JSON.stringify({ path: folder }),
    });
    if (state.pipPoolRequestSeq[prefix] !== seq) return;
    state.pipPoolByPrefix[prefix] = result;
    const panel = document.querySelector(`[data-summary-kind="pip"][data-summary-prefix="${prefix}"]`);
    if (panel) updatePanelSummary(panel);
    if (result.empty && $(`${prefix}-pip-mode`)?.value !== "off") {
      toast("画中画素材池为空，请补充素材。", "warning");
    }
  } catch (error) {
    // 素材池状态只是提示，不阻塞主流程。
  }
}

async function loadRuntime() {
  try {
    const data = await api("/api/runtime");
    $("app-version").textContent = `v${data.version}`;
    $("runtime-user-data").value = data.user_data_dir || "";
    $("runtime-repo-root").value = data.repo_root || "";
  } catch (error) {
    toast(`运行信息读取失败: ${error.message}`, "warning");
  }
}

function normalizeProvider(value) {
  if (value === "volcengine") return "火山引擎";
  if (value === "aliyun") return "火山引擎";
  return value || "火山引擎";
}

function providerToPreset(value) {
  if (value === "火山引擎") return "火山引擎";
  return "火山引擎";
}

function normalizeVolcRegion(value) {
  const text = String(value || "").trim();
  const compact = text.replace(/\s+/g, "").replace(/_/g, "-").toLowerCase();
  const aliases = {
    "": "cn-beijing",
    "beijing": "cn-beijing",
    "bj": "cn-beijing",
    "cn-beijing": "cn-beijing",
    "\u5317\u4eac": "cn-beijing",
    "\u4e2d\u56fd\u5317\u4eac": "cn-beijing",
    "shanghai": "cn-shanghai",
    "sh": "cn-shanghai",
    "cn-shanghai": "cn-shanghai",
    "\u4e0a\u6d77": "cn-shanghai",
    "\u4e2d\u56fd\u4e0a\u6d77": "cn-shanghai",
    "guangzhou": "cn-guangzhou",
    "gz": "cn-guangzhou",
    "cn-guangzhou": "cn-guangzhou",
    "\u5e7f\u5dde": "cn-guangzhou",
    "\u4e2d\u56fd\u5e7f\u5dde": "cn-guangzhou",
    "singapore": "ap-southeast-1",
    "ap-southeast-1": "ap-southeast-1",
    "\u65b0\u52a0\u5761": "ap-southeast-1",
  };
  return aliases[text] || aliases[compact] || compact || "cn-beijing";
}

async function loadSettings(showToast = false) {
  const data = await api("/api/settings");
  Object.entries(settingFields).forEach(([key, id]) => {
    const element = $(id);
    if (!element) return;
    let value = data[key];
    if (key === "asr_provider") value = normalizeProvider(value || data.asr_preset);
    if (key === "volc_region") value = normalizeVolcRegion(value);
    if (element.type === "checkbox") {
      element.checked = Boolean(value);
    } else {
      element.value = value ?? "";
    }
  });
  applyUiFontSize(data.ui_font_size || 14);
  syncSubtitleFontSize();
  applyTheme(data.ui_theme || "system");
  applyPreferenceWeights(data.preference_weights || {});
  applyAiRules(data.ai_rules || {});
  if (showToast) toast("设置已重新载入", "success");
}

function collectSettings() {
  const data = {};
  Object.entries(settingFields).forEach(([key, id]) => {
    const element = $(id);
    if (!element) return;
    data[key] = element.type === "checkbox" ? element.checked : element.value.trim();
  });
  data.asr_provider = "火山引擎";
  data.asr_preset = providerToPreset(data.asr_provider);
  data.volc_region = normalizeVolcRegion(data.volc_region);
  data.volc_app_id = "";
  data.volc_access_token = "";
  data.subtitle_font_size = Math.max(32, Math.min(96, Number(data.subtitle_font_size || 52)));
  data.ui_font_size = normalizeUiFontSize(data.ui_font_size);
  data.preference_weights = collectPreferenceWeights();
  data.ai_rules = collectAiRules();
  return data;
}

async function saveSettings() {
  const data = collectSettings();
  applyTheme(data.ui_theme || "system");
  const result = await api("/api/settings", {
    method: "POST",
    body: JSON.stringify(data),
  });
  toast(result.message || "设置已保存", "success");
}

async function testAI() {
  const result = await api("/api/settings/test-ai", {
    method: "POST",
    body: JSON.stringify(collectSettings()),
  });
  toast(result.message || "AI 连接测试完成", result.ok ? "success" : "warning");
}

async function diagnoseVolcengine() {
  toast("火山完整诊断已开始", "warning");
  const result = await api("/api/settings/diagnose-volcengine", {
    method: "POST",
    body: JSON.stringify(collectSettings()),
  });
  toast(result.message || "诊断完成", result.ok ? "success" : "error");
}

function uniqueTexts(items) {
  const result = [];
  const seen = new Set();
  (Array.isArray(items) ? items : []).forEach((item) => {
    const text = String(item || "").trim();
    if (text && !seen.has(text)) {
      result.push(text);
      seen.add(text);
    }
  });
  return result;
}

function formatKeywordMap(value) {
  if (!value || typeof value !== "object") return "";
  const lines = [];
  Object.entries(value).forEach(([group, items]) => {
    uniqueTexts(items).forEach((word) => lines.push(`${group}=${word}`));
    if (lines.length && lines[lines.length - 1] !== "") lines.push("");
  });
  while (lines[lines.length - 1] === "") lines.pop();
  return lines.join("\n");
}

function parseKeywordMap(text) {
  const result = {};
  String(text || "")
    .split(/\r?\n/)
    .forEach((line) => {
      const value = line.trim();
      if (!value || value.startsWith("#")) return;
      const sep = value.indexOf("=");
      if (sep <= 0) return;
      const group = value.slice(0, sep).trim();
      const word = value.slice(sep + 1).trim();
      if (!group || !word) return;
      if (!result[group]) result[group] = [];
      result[group].push(word);
    });
  Object.keys(result).forEach((group) => {
    result[group] = uniqueTexts(result[group]);
  });
  return result;
}

function formatKeywordList(value) {
  return uniqueTexts(value).join("\n");
}

function parseKeywordList(text) {
  return uniqueTexts(
    String(text || "")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith("#"))
  );
}

function applyKeywordConfig(config = {}) {
  state.keywordConfig = config && typeof config === "object" ? config : {};
  const clip = $(keywordFields.clip_keywords);
  const forbidden = $(keywordFields.forbidden_phrases);
  const filler = $(keywordFields.filler_words);
  const preference = $(keywordFields.preference_keywords);
  if (clip) clip.value = formatKeywordMap(state.keywordConfig.clip_keywords);
  if (forbidden) forbidden.value = formatKeywordList(state.keywordConfig.forbidden_phrases);
  if (filler) filler.value = formatKeywordList(state.keywordConfig.filler_words);
  if (preference) preference.value = formatKeywordMap(state.keywordConfig.preference_keywords);
}

function collectKeywordConfig() {
  return {
    ...state.keywordConfig,
    clip_keywords: parseKeywordMap($(keywordFields.clip_keywords)?.value || ""),
    forbidden_phrases: parseKeywordList($(keywordFields.forbidden_phrases)?.value || ""),
    filler_words: parseKeywordList($(keywordFields.filler_words)?.value || ""),
    preference_keywords: parseKeywordMap($(keywordFields.preference_keywords)?.value || ""),
  };
}

function updateKeywordSummary(data) {
  const count = $("keyword-count");
  const source = $("keyword-source");
  if (count) count.textContent = String(data.count || 0);
  if (source) source.textContent = data.source || data.path || "本地";
}

async function openKeywordEditor() {
  if (!Object.keys(state.keywordConfig || {}).length) await loadKeywords(false);
  const modal = $("keyword-editor-modal");
  if (!modal) return;
  modal.classList.remove("is-hidden");
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("keyword-modal-open");
  setTimeout(() => $(keywordFields.clip_keywords)?.focus(), 0);
}

function closeKeywordEditor() {
  const modal = $("keyword-editor-modal");
  if (!modal) return;
  modal.classList.add("is-hidden");
  modal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("keyword-modal-open");
}

async function loadKeywords(showToast = false) {
  const data = await api("/api/keywords");
  updateKeywordSummary(data);
  applyKeywordConfig(data.keywords || {});
  if (showToast) toast("关键词信息已刷新", "success");
}

async function saveKeywords() {
  const result = await api("/api/keywords", {
    method: "POST",
    body: JSON.stringify(collectKeywordConfig()),
  });
  updateKeywordSummary(result);
  applyKeywordConfig(result.keywords || collectKeywordConfig());
  toast(result.message || "词库已保存", "success");
}

async function resetKeywords() {
  if (!window.confirm("恢复默认词库会删除当前用户自定义词库，确认继续？")) return;
  const result = await api("/api/keywords/reset", { method: "POST", body: "{}" });
  updateKeywordSummary(result);
  applyKeywordConfig(result.keywords || {});
  toast(result.message || "词库已恢复默认", "success");
}

async function clearCache() {
  const result = await api("/api/cache/clear", { method: "POST", body: "{}" });
  toast(result.message || "缓存清理完成", "success");
}

function toggleSecret(button) {
  const target = $(button.dataset.target || "");
  if (!target) return;
  const visible = target.type === "password";
  target.type = visible ? "text" : "password";
  button.classList.toggle("is-visible", visible);
  button.setAttribute("aria-pressed", visible ? "true" : "false");
}

function syncPreferenceSlider(input) {
  const key = input.dataset.prefKey;
  const value = input.value;
  const label = document.querySelector(`[data-pref-value="${key}"]`);
  if (label) label.textContent = value;
}

function syncSubtitleFontSize() {
  const input = $("s-subtitle-font-size");
  const label = $("s-subtitle-font-size-value");
  if (!input || !label) return;
  label.textContent = input.value;
}

function applyPreferenceWeights(weights) {
  document.querySelectorAll("[data-pref-key]").forEach((input) => {
    const key = input.dataset.prefKey;
    if (Object.prototype.hasOwnProperty.call(weights, key)) {
      input.value = String(weights[key]);
    }
    syncPreferenceSlider(input);
  });
}

function collectPreferenceWeights() {
  const weights = {};
  document.querySelectorAll("[data-pref-key]").forEach((input) => {
    weights[input.dataset.prefKey] = Number(input.value);
  });
  return weights;
}

function applyAiRules(rules) {
  $("s-rule-narrative").value = rules.narrative || "";
  $("s-rule-category-filter").checked = rules.category_filter !== false;
  $("s-rule-time-coherence").checked = rules.time_coherence !== false;
  $("s-rule-hook-cap").value = rules.hook_cap || "5秒";
  $("s-rule-custom-text").value = rules.custom_text || "";
}

function collectAiRules() {
  return {
    narrative: $("s-rule-narrative").value.trim(),
    category_filter: $("s-rule-category-filter").checked,
    time_coherence: $("s-rule-time-coherence").checked,
    hook_cap: $("s-rule-hook-cap").value,
    custom_text: $("s-rule-custom-text").value.trim(),
  };
}

async function loadLicense() {
  try {
    const data = await api("/api/license");
    $("license-code").value = data.code || "";
    $("license-days-left").value = data.activated
      ? `${data.days_left ?? 0} 天，到期 ${data.expires_date || ""}`
      : data.reason || "未激活";
  } catch (error) {
    $("license-days-left").value = "读取失败";
  }
}

async function activateLicense() {
  const code = $("license-code").value.trim();
  if (!code) {
    toast("请先输入激活码", "warning");
    return;
  }
  const result = await api("/api/license/activate", {
    method: "POST",
    body: JSON.stringify({ code }),
  });
  toast(result.message || "激活完成", result.ok ? "success" : "warning");
  if (result.ok && result.restart_required) {
    alert(result.message || "激活完成，请重启客户端后再使用。");
  }
  await loadLicense();
}

async function unbindDevice() {
  if (!confirm("确定解绑当前设备吗？解绑后需要重新激活。")) return;
  const result = await api("/api/license/unbind", { method: "POST", body: "{}" });
  toast(result.message || "解绑完成", result.ok ? "success" : "warning");
  await loadLicense();
}

async function checkUpdate() {
  const status = $("update-status");
  if (status) status.value = "正在检查...";
  const result = await api("/api/update/check");
  if (!result.update_available) {
    if (status) status.value = "当前已是最新版本";
    toast("当前已是最新版本", "success");
    return;
  }
  const update = result.update || {};
  if (status) status.value = `发现新版本 v${update.version || ""}`;
  toast(`发现新版本 v${update.version || ""}`, "success");
}

async function applyUpdate() {
  const status = $("update-status");
  if (!confirm("安装更新后需要重启客户端才能生效，继续吗？")) return;
  if (status) status.value = "正在安装更新...";
  const result = await api("/api/update/apply", { method: "POST", body: "{}" });
  if (result.ok) {
    const message = result.auto_restart
      ? "更新完成，客户端即将自动重启..."
      : result.restart_required
        ? "更新完成，请重启客户端"
        : "当前已是最新版本";
    if (status) status.value = message;
    toast(message, "success");
    return;
  } else {
    if (status) status.value = "更新失败";
    toast(result.msg || "更新失败", "error");
  }
}

function setUpdateState(patch = {}) {
  state.update = { ...state.update, ...patch };
  renderUpdateState();
}

function renderUpdateState() {
  const update = state.update || {};
  const info = update.info || {};
  const version = info.version || info.latest_version || "";
  const hasUpdate = Boolean(update.available);
  const busy = Boolean(update.checking || update.installing);
  const message = update.message || "\u672a\u68c0\u67e5";
  const status = $("update-status");
  if (status) status.value = message;

  const indicator = $("update-indicator");
  if (indicator) {
    indicator.classList.toggle("has-update", hasUpdate);
    indicator.classList.toggle("is-busy", busy);
    indicator.classList.toggle("is-error", Boolean(update.error));
    const title = hasUpdate && version
      ? `\u53d1\u73b0\u65b0\u7248\u672c v${version}`
      : message;
    indicator.title = title;
    indicator.setAttribute("aria-label", title);
  }

  const cardStatus = $("update-card-status");
  if (cardStatus) cardStatus.textContent = message;
  const cardVersion = $("update-card-version");
  if (cardVersion) {
    cardVersion.textContent = hasUpdate && version
      ? `v${version}`
      : "\u5f53\u524d\u7248\u672c";
  }
  const notes = $("update-card-notes");
  if (notes) {
    const releaseNotes = info.release_notes || info.update_message || "";
    const fileCount = Number(info.file_count || 0);
    const suffix = fileCount ? `\n${fileCount} \u4e2a\u6587\u4ef6\u5c06\u66f4\u65b0` : "";
    notes.textContent = (releaseNotes || (hasUpdate ? "\u53d1\u73b0\u53ef\u5b89\u88c5\u66f4\u65b0\u3002" : "\u6ca1\u6709\u53ef\u7528\u66f4\u65b0\u3002")) + suffix;
  }
  const applyButton = $("update-card-apply");
  if (applyButton) applyButton.disabled = !hasUpdate || busy;
}

function openUpdateCard() {
  const card = $("update-popover");
  if (!card) return;
  card.classList.remove("is-hidden");
  card.setAttribute("aria-hidden", "false");
  renderUpdateState();
  if (!state.update.checked && !state.update.checking) {
    checkUpdate({ quiet: true }).catch((error) => console.warn("Update check failed", error));
  }
}

function closeUpdateCard() {
  const card = $("update-popover");
  if (!card) return;
  card.classList.add("is-hidden");
  card.setAttribute("aria-hidden", "true");
}

function toggleUpdateCard() {
  const card = $("update-popover");
  if (!card || card.classList.contains("is-hidden")) openUpdateCard();
  else closeUpdateCard();
}

async function checkUpdate(options = {}) {
  const quiet = Boolean(options.quiet);
  setUpdateState({
    checked: true,
    checking: true,
    installing: false,
    error: "",
    message: "\u6b63\u5728\u68c0\u67e5\u66f4\u65b0...",
  });
  try {
    const result = await api("/api/update/check");
    if (!result.update_available) {
      setUpdateState({
        checking: false,
        available: false,
        info: null,
        message: "\u5f53\u524d\u5df2\u662f\u6700\u65b0\u7248\u672c",
      });
      if (!quiet) toast("\u5f53\u524d\u5df2\u662f\u6700\u65b0\u7248\u672c", "success");
      return result;
    }
    const update = result.update || {};
    const version = update.version || "";
    setUpdateState({
      checking: false,
      available: true,
      info: update,
      message: version ? `\u53d1\u73b0\u65b0\u7248\u672c v${version}` : "\u53d1\u73b0\u65b0\u7248\u672c",
    });
    if (!quiet) toast(version ? `\u53d1\u73b0\u65b0\u7248\u672c v${version}` : "\u53d1\u73b0\u65b0\u7248\u672c", "success");
    return result;
  } catch (error) {
    setUpdateState({
      checking: false,
      available: false,
      error: error.message || String(error),
      message: "\u68c0\u67e5\u66f4\u65b0\u5931\u8d25",
    });
    if (!quiet) throw error;
    return null;
  }
}

async function applyUpdate() {
  if (!state.update.available && !state.update.checking) {
    const result = await checkUpdate({ quiet: true });
    if (!result?.update_available) {
      toast("\u5f53\u524d\u5df2\u662f\u6700\u65b0\u7248\u672c", "success");
      return;
    }
  }
  if (!confirm("\u5b89\u88c5\u66f4\u65b0\u540e\u9700\u8981\u91cd\u542f\u5ba2\u6237\u7aef\u624d\u80fd\u751f\u6548\uff0c\u7ee7\u7eed\u5417\uff1f")) return;
  setUpdateState({
    installing: true,
    checking: false,
    error: "",
    message: "\u6b63\u5728\u5b89\u88c5\u66f4\u65b0...",
  });
  const result = await api("/api/update/apply", { method: "POST", body: "{}" });
  if (result.ok) {
    const message = result.auto_restart
      ? "\u66f4\u65b0\u5b8c\u6210\uff0c\u5ba2\u6237\u7aef\u5373\u5c06\u81ea\u52a8\u91cd\u542f..."
      : result.restart_required
        ? "\u66f4\u65b0\u5b8c\u6210\uff0c\u8bf7\u91cd\u542f\u5ba2\u6237\u7aef"
        : "\u5f53\u524d\u5df2\u662f\u6700\u65b0\u7248\u672c";
    setUpdateState({
      installing: false,
      available: false,
      message,
    });
    toast(message, "success");
    return result;
  }
  const message = result.msg || "\u66f4\u65b0\u5931\u8d25";
  setUpdateState({
    installing: false,
    error: message,
    message,
  });
  toast(message, "error");
  return result;
}

function feedback() {
  toast("反馈入口先保留，后续可接入表单或客服链接。", "warning");
}

function getVideoPaths() {
  return getLines("video-paths");
}

function addPath(inputId, targetId) {
  const input = $(inputId || "");
  if (!input || !targetId) return;
  const value = input.value.trim();
  if (!value) return;
  addVideoPaths(targetId, [value]);
  input.value = "";
}

function getLines(id) {
  return ($(id)?.value || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

function setLines(id, lines) {
  const textarea = $(id);
  if (!textarea) return;
  textarea.value = lines.join("\n");
  renderVideoList(id);
}

function addVideoPaths(targetId, paths) {
  const next = getLines(targetId);
  paths
    .map((path) => String(path || "").trim())
    .filter(Boolean)
    .forEach((path) => {
      if (!next.includes(path)) next.push(path);
    });
  setLines(targetId, next);
}

function setButtonBusy(button, busy, label = "正在打开...") {
  if (!button) return () => {};
  const previousText = button.textContent;
  const previousDisabled = button.disabled;
  if (busy) {
    button.dataset.busyText = previousText;
    button.textContent = label;
    button.disabled = true;
    button.classList.add("is-loading");
  }
  return () => {
    button.textContent = button.dataset.busyText || previousText;
    button.disabled = previousDisabled;
    button.classList.remove("is-loading");
    delete button.dataset.busyText;
  };
}

async function pickVideos(targetId = "video-paths", trigger = null) {
  const restoreButton = setButtonBusy(trigger, true, "正在打开...");
  let result;
  try {
    result = await api("/api/dialog/videos", { method: "POST", body: "{}" });
  } catch (error) {
    const detail = error.message || String(error);
    toast(`选择视频窗口打开失败。可以直接把视频拖到素材框，或复制完整路径后按 Enter。错误：${detail}`, "error");
    return;
  } finally {
    restoreButton();
  }
  const paths = result.paths || [];
  if (paths.length) {
    addVideoPaths(targetId, paths);
    toast(`已添加 ${paths.length} 个视频`, "success");
  }
}

async function pickFile(targetId, kind = "file") {
  if (!targetId) return;
  const result = await api("/api/dialog/file", {
    method: "POST",
    body: JSON.stringify({ kind }),
  });
  if (result.path) {
    setInputValue(targetId, result.path);
    toast("文件已选择", "success");
  }
}

async function pickDirectory(targetId) {
  if (!targetId) return;
  const result = await api("/api/dialog/directory", { method: "POST", body: "{}" });
  if (result.path) {
    setInputValue(targetId, result.path);
    toast("目录已选择", "success");
  }
}

async function openPath(targetId) {
  const target = $(targetId || "");
  const path = target?.value.trim() || defaultOutputPath(targetId);
  if (!path) {
    toast("请先选择或填写目录", "warning");
    return;
  }
  const result = await api("/api/path/open", {
    method: "POST",
    body: JSON.stringify({ path }),
  });
  toast(result.message || "已打开目录", "success");
}

function defaultOutputPath(targetId) {
  const defaults = {
    "output-dir": ["video-paths", "output"],
    "mix-output-dir": ["mix-video-paths", "mix_output"],
    "scan-output-dir": ["scan-video-paths", "scan_output"],
    "dedup-output-dir": ["dedup-video-paths", "dedup_output"],
  };
  const item = defaults[targetId];
  if (!item) return "";
  const [sourceId, folder] = item;
  const source = sourceId.endsWith("-paths") ? getLines(sourceId)[0] : $(sourceId)?.value.trim();
  if (!source) return "";
  const slash = source.lastIndexOf("\\") >= 0 ? "\\" : "/";
  const index = Math.max(source.lastIndexOf("\\"), source.lastIndexOf("/"));
  if (index <= 0) return "";
  return `${source.slice(0, index)}${slash}${folder}`;
}

async function stopScope(scope) {
  const result = await api("/api/tasks/stop-scope", {
    method: "POST",
    body: JSON.stringify({ scope }),
  });
  toast(result.message || "已发送停止请求", result.ok ? "warning" : "error");
  refreshTasks();
}

function setInputValue(targetId, value) {
  const target = $(targetId);
  if (!target) return;
  target.value = value || "";
  if (targetId?.endsWith("-pip-path") && value) {
    const prefix = targetId.replace("-pip-path", "");
    const mode = $(`${prefix}-pip-mode`);
    if (mode) mode.value = "asset";
  }
  if (targetId?.endsWith("-pip-folder") && value) {
    const prefix = targetId.replace("-pip-folder", "");
    const mode = $(`${prefix}-pip-mode`);
    if (mode) mode.value = "asset";
  }
  target.dispatchEvent(new Event("input", { bubbles: true }));
  target.dispatchEvent(new Event("change", { bubbles: true }));
}

function removeVideoPath(targetId, index) {
  const lines = getLines(targetId);
  if (Number.isInteger(index) && index >= 0 && index < lines.length) {
    lines.splice(index, 1);
    setLines(targetId, lines);
  }
}

function moveVideoPath(targetId, index, direction) {
  const lines = getLines(targetId);
  const to = index + direction;
  if (!Number.isInteger(index) || !direction || index < 0 || to < 0 || index >= lines.length || to >= lines.length) return;
  [lines[index], lines[to]] = [lines[to], lines[index]];
  setLines(targetId, lines);
}

function clearVideoList(targetId) {
  setLines(targetId, []);
}

function normalizeVideoPath(path) {
  return String(path || "").trim().replace(/\//g, "\\").toLowerCase();
}

function videoInfoMap(targetId) {
  return state.videoInfoByTarget[targetId] || {};
}

function videoListDuplicateMap(lines) {
  const pathCounts = new Map();
  const nameCounts = new Map();
  lines.forEach((path) => {
    const key = normalizeVideoPath(path);
    const name = path.split(/[\\/]/).filter(Boolean).pop()?.toLowerCase() || "";
    if (key) pathCounts.set(key, (pathCounts.get(key) || 0) + 1);
    if (name) nameCounts.set(name, (nameCounts.get(name) || 0) + 1);
  });
  return { pathCounts, nameCounts };
}

function videoMetaText(info) {
  if (!info) return "检测中";
  if (!info.exists) return info.message || "文件不存在";
  if (!info.valid) return info.message || "视频不可读";
  const parts = [];
  if (Number(info.duration) > 0) parts.push(formatSeconds(info.duration));
  if (info.resolution) parts.push(info.resolution);
  return parts.join(" · ") || "可用";
}

function renderVideoList(targetId) {
  const box = document.querySelector(`[data-list-for="${targetId}"]`);
  if (!box) return;
  const lines = getLines(targetId);
  const infoMap = videoInfoMap(targetId);
  const duplicateMap = videoListDuplicateMap(lines);
  box.innerHTML = lines.map((path, index) => {
    const name = path.split(/[\\/]/).filter(Boolean).pop() || path;
    const info = infoMap[path] || infoMap[normalizeVideoPath(path)];
    const isInvalid = info && (!info.exists || !info.valid);
    const nameKey = name.toLowerCase();
    const isDuplicate =
      (duplicateMap.pathCounts.get(normalizeVideoPath(path)) || 0) > 1 ||
      (duplicateMap.nameCounts.get(nameKey) || 0) > 1 ||
      Boolean(info?.duplicate);
    const rowClass = ["video-row", isInvalid ? "is-invalid" : "", isDuplicate ? "is-duplicate" : ""].filter(Boolean).join(" ");
    const badges = [
      isInvalid ? `<span class="video-badge is-invalid">无效</span>` : "",
      isDuplicate ? `<span class="video-badge is-duplicate">重复</span>` : "",
    ].join("");
    return `
      <div class="${rowClass}" draggable="true" data-video-row="${targetId}" data-index="${index}">
        <div class="video-drag" title="拖拽排序">≡</div>
        <div class="video-main">
          <div class="video-title"><strong>${escapeHtml(name)}</strong>${badges}</div>
          <span class="video-meta">${escapeHtml(videoMetaText(info))}</span>
          <span class="video-path" title="${escapeHtml(path)}">${escapeHtml(path)}</span>
        </div>
        <div class="video-actions">
          <button class="icon-button" type="button" title="上移" data-action="move-video" data-target="${targetId}" data-index="${index}" data-direction="-1" ${index === 0 ? "disabled" : ""}>上</button>
          <button class="icon-button" type="button" title="下移" data-action="move-video" data-target="${targetId}" data-index="${index}" data-direction="1" ${index === lines.length - 1 ? "disabled" : ""}>下</button>
          <button class="video-remove" type="button" title="删除" data-action="remove-video" data-target="${targetId}" data-index="${index}">×</button>
        </div>
      </div>`;
  }).join("");
  bindVideoRowDrag(box, targetId);
  inspectVideoList(targetId, lines);
}

function bindVideoRowDrag(box, targetId) {
  box.querySelectorAll("[data-video-row]").forEach((row) => {
    row.addEventListener("dragstart", (event) => {
      row.classList.add("is-dragging");
      event.dataTransfer?.setData("text/plain", JSON.stringify({ targetId, index: Number(row.dataset.index) }));
      if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
    });
    row.addEventListener("dragend", () => row.classList.remove("is-dragging"));
    row.addEventListener("dragover", (event) => {
      event.preventDefault();
      row.classList.add("is-drop-target");
    });
    row.addEventListener("dragleave", () => row.classList.remove("is-drop-target"));
    row.addEventListener("drop", (event) => {
      event.preventDefault();
      row.classList.remove("is-drop-target");
      let payload = null;
      try {
        payload = JSON.parse(event.dataTransfer?.getData("text/plain") || "{}");
      } catch (error) {
        return;
      }
      if (payload?.targetId !== targetId) return;
      reorderVideoPath(targetId, Number(payload.index), Number(row.dataset.index));
    });
  });
}

function reorderVideoPath(targetId, from, to) {
  const lines = getLines(targetId);
  if (!Number.isInteger(from) || !Number.isInteger(to) || from < 0 || to < 0 || from >= lines.length || to >= lines.length || from === to) return;
  const [item] = lines.splice(from, 1);
  lines.splice(to, 0, item);
  setLines(targetId, lines);
}

async function inspectVideoList(targetId, lines = getLines(targetId)) {
  if (!lines.length) {
    state.videoInfoByTarget[targetId] = {};
    return;
  }
  const currentMap = videoInfoMap(targetId);
  if (lines.every((path) => currentMap[path] || currentMap[normalizeVideoPath(path)])) return;
  const seq = (state.videoInfoRequestSeq[targetId] || 0) + 1;
  state.videoInfoRequestSeq[targetId] = seq;
  try {
    const result = await api("/api/videos/inspect", {
      method: "POST",
      body: JSON.stringify({ paths: lines }),
    });
    if (state.videoInfoRequestSeq[targetId] !== seq) return;
    const infoMap = {};
    (result.items || []).forEach((item) => {
      infoMap[item.path] = item;
      infoMap[normalizeVideoPath(item.path)] = item;
    });
    state.videoInfoByTarget[targetId] = infoMap;
    if (getLines(targetId).join("\n") === lines.join("\n")) renderVideoList(targetId);
  } catch (error) {
    // Metadata helps users but should never block adding videos.
  }
}

function bindVideoDropzones() {
  document.querySelectorAll("[data-drop-target]").forEach((zone) => {
    const targetId = zone.dataset.dropTarget;
    zone.addEventListener("click", (event) => {
      if (event.target.closest("[data-action]")) return;
      if (event.target.closest("input, textarea, select, button")) return;
      const picker = document.querySelector(`[data-action="pick-videos"][data-target="${targetId}"]`);
      pickVideos(targetId, picker);
    });
    zone.addEventListener("dragover", (event) => {
      event.preventDefault();
      zone.classList.add("is-dragging");
    });
    zone.addEventListener("dragleave", () => zone.classList.remove("is-dragging"));
    zone.addEventListener("drop", async (event) => {
      event.preventDefault();
      zone.classList.remove("is-dragging");
      const files = Array.from(event.dataTransfer?.files || []);
      const pathItems = files.map((file) => file.path || file.webkitRelativePath || "").filter(Boolean);
      if (pathItems.length === files.length && pathItems.length) {
        addVideoPaths(targetId, pathItems);
        toast(`已添加 ${pathItems.length} 个视频`, "success");
        return;
      }
      if (!files.length) return;
      toast("浏览器未提供本地文件路径。请点击“添加视频”选择文件，避免复制缓存和改名。", "warning");
    });
  });
}

function bindFileDropTargets() {
  document.querySelectorAll("[data-file-drop-target]").forEach((zone) => {
    const targetId = zone.dataset.fileDropTarget;
    const kind = zone.dataset.fileKind || "file";
    zone.addEventListener("dragover", (event) => {
      event.preventDefault();
      zone.classList.add("is-dragging");
    });
    zone.addEventListener("dragleave", () => zone.classList.remove("is-dragging"));
    zone.addEventListener("drop", async (event) => {
      event.preventDefault();
      zone.classList.remove("is-dragging");
      const files = Array.from(event.dataTransfer?.files || []);
      if (!files.length) return;
      const firstPath = files[0].path || files[0].webkitRelativePath || "";
      if (firstPath) {
        setInputValue(targetId, firstPath);
        toast("文件已添加", "success");
        return;
      }
      const form = new FormData();
      form.append("files", files[0], files[0].name);
      toast("正在缓存拖拽文件...", "warning");
      const result = await upload("/api/uploads/files", form);
      setInputValue(targetId, result.paths?.[0] || "");
      toast(kind === "excel" ? "Excel 已缓存并添加" : "文件已缓存并添加", "success");
    });
  });
}

function checkedControlValues(controlName) {
  return Array.from(document.querySelectorAll(`[data-ai-control="${controlName}"]:checked`))
    .map((node) => node.value)
    .filter(Boolean);
}

function collectAiControls(prefix) {
  var selling = checkedControlValues(`${prefix}-selling`);
  var customWords = customSellingValues(prefix);
  if (customWords.length) {
    selling = selling.concat(customWords);
  }
  return {
    goal: $(`${prefix}-goal`)?.value || "自动",
    selling_points: selling,
    avoid: checkedControlValues(`${prefix}-avoid`),
    hook_style: $(`${prefix}-hook-style`)?.value || "自动",
    ending_style: $(`${prefix}-ending-style`)?.value || "自动",
    strictness: $(`${prefix}-strictness`)?.value || "标准",
  };
}

function collectPipPayload(prefix) {
  const mode = $(`${prefix}-pip-mode`)?.value || "off";
  const pipPath = $(`${prefix}-pip-path`)?.value.trim() || "";
  const pipFolder = $(`${prefix}-pip-folder`)?.value.trim() || "";
  const useAutoPip = mode === "auto";
  return {
    pip_enabled: useAutoPip || Boolean(pipPath || pipFolder),
    pip_path: useAutoPip ? "auto" : pipPath,
    pip_folder: pipFolder,
    pip_size: Number($(`${prefix}-pip-size`)?.value || 0.15),
    pip_opacity: Number($(`${prefix}-pip-opacity`)?.value || 0.03),
    pip_pos: $(`${prefix}-pip-pos`)?.value || "右下",
  };
}

function collectSmartPayload(options = {}) {
  const requireVideos = options.requireVideos !== false;
  const videoPaths = getVideoPaths();
  if (requireVideos && !videoPaths.length) {
    throw new Error("请先填写视频路径");
  }
  return {
    video_paths: videoPaths,
    srt_path: $("srt-path").value.trim(),
    output_dir: $("output-dir").value.trim(),
    category: $("sc-category").value,
    focus_hint: $("sc-focus").value,
    ai_controls: collectAiControls("sc"),
    target_duration: Number($("sc-duration").value || 60),
    versions: Number($("sc-versions").value || 1),
    dedup_preset: $("sc-dedup").value,
    mirror_enabled: $("sc-mirror").checked,
    subtitle_overlay: $("sc-subtitle").checked,
    smart_crop_enabled: $("sc-crop").checked,
    crop_level: $("sc-crop-level").value,
    ken_burns_enabled: $("sc-kenburns").checked,
    ken_burns_intensity: $("sc-kb-intensity").value,
    ...collectPipPayload("sc"),
  };
}

async function startSmartPreview() {
  await saveFeaturePreferences();
  const payload = collectSmartPayload();
  await runPreflight("smart-preview", payload, "smart-cut");
  const result = await api("/api/smart-cut/preview/start", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  state.smartPreview = {
    id: result.preview_id,
    status: "running",
    message: "正在生成 AI 选片预览。",
    clips: [],
  };
  renderSmartPreview(state.smartPreview);
  toast(result.message || "AI选片预览已启动", "success");
  refreshTasks();
  pollSmartPreview(result.preview_id);
}

async function startMixPreview() {
  await saveFeaturePreferences();
  const payload = collectFeaturePayload("mix");
  await runPreflight("mix-preview", payload, "mix");
  const result = await api("/api/mix/preview/start", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  state.mixPreview = {
    id: result.preview_id,
    status: "running",
    message: "正在生成混剪 AI 选片预览。",
    clips: [],
  };
  renderMixPreview(state.mixPreview);
  toast(result.message || "混剪 AI 选片预览已启动", "success");
  refreshTasks();
  pollMixPreview(result.preview_id);
}

async function startSmartCut() {
  await saveFeaturePreferences();
  const payload = collectSmartPayload();
  await runPreflight("smart-cut", payload, "smart-cut");

  const result = await api("/api/smart-cut/start", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  toast(result.message || "任务已启动", "success");
  refreshTasks();
}

async function startSmartFromPreview() {
  await saveFeaturePreferences();
  if (!state.smartPreview?.id || state.smartPreview.status !== "ready") {
    toast("请先生成 AI 选片预览", "warning");
    return;
  }
  syncPreviewClipSelections();
  const selection = collectPreviewSelection("smart");
  const selected = selection.selectedIndices;
  if (!selected.length) {
    toast("请至少保留一个片段", "warning");
    return;
  }
  const payload = {
    ...collectSmartPayload({ requireVideos: false }),
    preview_id: state.smartPreview.id,
    selected_indices: selected,
    selected_segments: selection.selectedSegments,
  };
  await runPreflight("smart-from-preview", payload, "smart-cut");
  const result = await api("/api/smart-cut/from-preview/start", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  toast(result.message || "预览成片任务已启动", "success");
  refreshTasks();
}

async function startMixFromPreview() {
  await saveFeaturePreferences();
  if (!state.mixPreview?.id || state.mixPreview.status !== "ready") {
    toast("请先生成混剪 AI 选片预览", "warning");
    return;
  }
  syncPreviewClipSelections("mix");
  const selection = collectPreviewSelection("mix");
  const selected = selection.selectedIndices;
  if (!selected.length) {
    toast("请至少保留一个片段", "warning");
    return;
  }
  const payload = {
    ...collectFeaturePayload("mix"),
    preview_id: state.mixPreview.id,
    selected_indices: selected,
    selected_segments: selection.selectedSegments,
  };
  await runPreflight("mix", payload, "mix");
  const result = await api("/api/mix/from-preview/start", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  toast(result.message || "预览混剪任务已启动", "success");
  refreshTasks();
}

function getPreviewState(scope = "smart") {
  return scope === "mix" ? state.mixPreview : state.smartPreview;
}

function renderPreviewState(scope = "smart") {
  if (scope === "mix") renderMixPreview(state.mixPreview);
  else renderSmartPreview(state.smartPreview);
}

function previewDraftKey(scope, previewId) {
  return `${scope}:${previewId || ""}`;
}

function previewDraftStorageKey(scope, previewId) {
  return `${previewDraftStoragePrefix}${previewDraftKey(scope, previewId)}`;
}

function normalizedIntegerList(values) {
  const result = [];
  const seen = new Set();
  (Array.isArray(values) ? values : []).forEach((value) => {
    const number = Number(value);
    if (!Number.isInteger(number) || seen.has(number)) return;
    seen.add(number);
    result.push(number);
  });
  return result;
}

function buildPreviewDraftFromState(scope = "smart") {
  const preview = getPreviewState(scope);
  const draft = {
    preview_id: preview?.id || "",
    scope,
    order: [],
    selected_indices: [],
    selected_segments: {},
    updated_at: Date.now(),
  };
  (preview?.clips || []).forEach((clip) => {
    const clipIndex = Number(clip.index);
    if (!Number.isInteger(clipIndex)) return;
    draft.order.push(clipIndex);
    const segments = previewSegments(clip);
    const keptSegments = segments
      .filter((segment) => segment.selected !== false)
      .map((segment) => Number(segment.index))
      .filter((value) => Number.isInteger(value));
    const clipSelected = clip.selected !== false && (!segments.length || keptSegments.length > 0);
    if (!clipSelected) return;
    draft.selected_indices.push(clipIndex);
    if (segments.length) draft.selected_segments[String(clipIndex)] = keptSegments;
  });
  return draft;
}

function readStoredPreviewDraft(scope, preview) {
  if (!preview?.id) return null;
  const key = previewDraftStorageKey(scope, preview.id);
  let local = null;
  try {
    local = JSON.parse(localStorage.getItem(key) || "null");
  } catch (error) {
    local = null;
  }
  const server = preview.selection_draft && typeof preview.selection_draft === "object"
    ? preview.selection_draft
    : null;
  const candidates = [local, server]
    .filter((draft) => draft && draft.preview_id === preview.id)
    .sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0));
  return candidates[0] || null;
}

function applyPreviewDraftToState(scope = "smart", draft = null) {
  const preview = getPreviewState(scope);
  if (!preview?.clips?.length || !draft) return;
  const order = normalizedIntegerList(draft.order);
  if (order.length) {
    const positionByIndex = new Map(order.map((index, position) => [index, position]));
    preview.clips.sort((a, b) => {
      const aIndex = Number(a.index);
      const bIndex = Number(b.index);
      const aPosition = positionByIndex.has(aIndex) ? positionByIndex.get(aIndex) : Number.MAX_SAFE_INTEGER;
      const bPosition = positionByIndex.has(bIndex) ? positionByIndex.get(bIndex) : Number.MAX_SAFE_INTEGER;
      if (aPosition !== bPosition) return aPosition - bPosition;
      return aIndex - bIndex;
    });
  }
  const hasSelectedIndices = Array.isArray(draft.selected_indices);
  const selectedSet = new Set(normalizedIntegerList(draft.selected_indices));
  const segmentMap = draft.selected_segments && typeof draft.selected_segments === "object"
    ? draft.selected_segments
    : {};
  preview.clips.forEach((clip) => {
    const clipIndex = Number(clip.index);
    const segments = previewSegments(clip);
    const selectedByDraft = hasSelectedIndices ? selectedSet.has(clipIndex) : clip.selected !== false;
    if (!selectedByDraft) {
      clip.selected = false;
      segments.forEach((segment) => {
        segment.selected = false;
      });
      return;
    }
    const segmentValues = segmentMap[String(clipIndex)];
    if (segments.length && Array.isArray(segmentValues)) {
      const segmentSet = new Set(normalizedIntegerList(segmentValues));
      segments.forEach((segment) => {
        segment.selected = segmentSet.has(Number(segment.index));
      });
    } else if (segments.length) {
      segments.forEach((segment) => {
        if (segment.selected === undefined) segment.selected = true;
      });
    }
    clip.selected = !segments.length || segments.some((segment) => segment.selected !== false);
  });
}

function savePreviewDraft(scope = "smart", draft = null, { remote = true } = {}) {
  const preview = getPreviewState(scope);
  const nextDraft = draft || buildPreviewDraftFromState(scope);
  if (!preview?.id || !nextDraft.preview_id) return nextDraft;
  const key = previewDraftKey(scope, preview.id);
  state.previewDrafts[key] = nextDraft;
  try {
    localStorage.setItem(previewDraftStorageKey(scope, preview.id), JSON.stringify(nextDraft));
  } catch (error) {
    console.warn("Failed to persist preview draft", error);
  }
  if (remote) {
    clearTimeout(state.previewDraftSaveTimers[key]);
    state.previewDraftSaveTimers[key] = setTimeout(async () => {
      try {
        await api("/api/preview/selection/save", {
          method: "POST",
          body: JSON.stringify(nextDraft),
        });
      } catch (error) {
        console.warn("Failed to save preview draft", error);
      }
    }, 300);
  }
  return nextDraft;
}

function ensurePreviewDraft(scope = "smart") {
  const preview = getPreviewState(scope);
  if (!preview?.id || !preview?.clips?.length) return null;
  const key = previewDraftKey(scope, preview.id);
  let draft = state.previewDrafts[key] || readStoredPreviewDraft(scope, preview);
  if (draft) {
    applyPreviewDraftToState(scope, draft);
  } else {
    draft = buildPreviewDraftFromState(scope);
  }
  state.previewDrafts[key] = draft;
  savePreviewDraft(scope, draft, { remote: false });
  return draft;
}

function commitPreviewDraft(scope = "smart", options = {}) {
  const draft = buildPreviewDraftFromState(scope);
  return savePreviewDraft(scope, draft, options);
}

function updatePreviewStickyOffset(scope = "smart") {
  const box = scope === "mix" ? $("mix-preview") : $("smart-preview");
  const summary = box?.querySelector(`[data-preview-summary="${scope}"]`);
  if (!box || !summary) return;
  box.style.setProperty("--preview-summary-offset", `${Math.ceil(summary.getBoundingClientRect().height)}px`);
}

function refreshPreviewSelectionUi(scope = "smart") {
  renderPreviewState(scope);
}

function syncPreviewClipSelections(scope = "smart") {
  const preview = getPreviewState(scope);
  if (!preview?.clips) return;
  const checked = new Map(
    Array.from(document.querySelectorAll(`[data-preview-clip][data-preview-scope="${scope}"]`)).map((node) => [
      Number(node.dataset.previewClip),
      node.checked,
    ])
  );
  const segmentChecked = new Map(
    Array.from(document.querySelectorAll(`[data-preview-segment][data-preview-scope="${scope}"]`)).map((node) => [
      `${Number(node.dataset.previewSegmentParent)}:${Number(node.dataset.previewSegmentIndex)}`,
      node.checked,
    ])
  );
  preview.clips.forEach((clip) => {
    const clipIndex = Number(clip.index);
    const segments = Array.isArray(clip.segments) ? clip.segments : [];
    segments.forEach((segment) => {
      const key = `${clipIndex}:${Number(segment.index)}`;
      if (segmentChecked.has(key)) segment.selected = segmentChecked.get(key);
      else if (segment.selected === undefined) segment.selected = true;
    });
    const anySegmentSelected = segments.length ? segments.some((segment) => segment.selected !== false) : true;
    if (checked.has(clipIndex)) {
      clip.selected = checked.get(clipIndex) && anySegmentSelected;
    }
    else if (clip.selected === undefined) clip.selected = true;
    if (segments.length && !anySegmentSelected) clip.selected = false;
  });
}

function updatePreviewClipSelection(index, selected, scope = "smart") {
  const preview = getPreviewState(scope);
  const clip = preview?.clips?.find((item) => Number(item.index) === index);
  if (clip) {
    clip.selected = selected;
    if (Array.isArray(clip.segments)) {
      clip.segments.forEach((segment) => {
        segment.selected = selected;
      });
    }
    commitPreviewDraft(scope);
    refreshPreviewSelectionUi(scope);
  }
}

function updatePreviewSegmentSelection(index, segmentIndex, selected, scope = "smart") {
  const preview = getPreviewState(scope);
  const clip = preview?.clips?.find((item) => Number(item.index) === index);
  if (!clip || !Array.isArray(clip.segments)) return;
  const segment = clip.segments.find((item) => Number(item.index) === segmentIndex);
  if (segment) segment.selected = selected;
  clip.selected = clip.segments.some((item) => item.selected !== false);
  commitPreviewDraft(scope);
  refreshPreviewSelectionUi(scope);
}

function togglePreviewSegments(index, scope = "smart") {
  const preview = getPreviewState(scope);
  if (!preview?.clips) return;
  syncPreviewClipSelections(scope);
  const clip = preview.clips.find((item) => Number(item.index) === index);
  if (!clip) return;
  const nextExpanded = clip.segmentsExpanded !== true;
  preview.clips.forEach((item) => {
    item.segmentsExpanded = false;
  });
  clip.segmentsExpanded = nextExpanded;
  renderPreviewState(scope);
}

function collectPreviewSelection(scope = "smart") {
  syncPreviewClipSelections(scope);
  const draft = commitPreviewDraft(scope, { remote: true });
  return {
    selectedIndices: draft.selected_indices || [],
    selectedSegments: draft.selected_segments || {},
  };
}

function reorderPreviewClip(scope, fromIndex, toIndex) {
  const preview = getPreviewState(scope);
  if (!preview?.clips?.length) return;
  syncPreviewClipSelections(scope);
  const clips = preview.clips;
  const from = clips.findIndex((clip) => Number(clip.index) === fromIndex);
  const to = clips.findIndex((clip) => Number(clip.index) === toIndex);
  if (from < 0 || from === to) return;
  const [clip] = clips.splice(from, 1);
  clips.splice(to, 0, clip);
  commitPreviewDraft(scope);
  renderPreviewState(scope);
}

function bindPreviewRowDrag(box, scope = "smart") {
  box.querySelectorAll(`[data-preview-row][data-preview-scope="${scope}"]`).forEach((row) => {
    row.addEventListener("dragstart", (event) => {
      if (event.target?.closest?.("input, button, [data-preview-segment-row]")) {
        event.preventDefault();
        return;
      }
      row.classList.add("is-dragging");
      event.dataTransfer?.setData("text/plain", JSON.stringify({
        scope,
        index: Number(row.dataset.previewIndex),
      }));
      if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
    });
    row.addEventListener("dragend", () => row.classList.remove("is-dragging"));
    row.addEventListener("dragover", (event) => {
      event.preventDefault();
      row.classList.add("is-drop-target");
    });
    row.addEventListener("dragleave", () => row.classList.remove("is-drop-target"));
    row.addEventListener("drop", (event) => {
      event.preventDefault();
      row.classList.remove("is-drop-target");
      let payload = null;
      try {
        payload = JSON.parse(event.dataTransfer?.getData("text/plain") || "{}");
      } catch (error) {
        return;
      }
      if (payload?.scope !== scope) return;
      reorderPreviewClip(scope, Number(payload.index), Number(row.dataset.previewIndex));
    });
  });
}

async function previewClipVideo(index, scope = "smart") {
  const preview = scope === "mix" ? state.mixPreview : state.smartPreview;
  if (!preview?.id || preview.status !== "ready") {
    toast("请先生成 AI 选片预览", "warning");
    return;
  }
  syncPreviewClipSelections(scope);
  const clip = preview.clips?.find((item) => Number(item.index) === index);
  const modal = ensurePreviewModal();
  const video = modal.querySelector("#preview-modal-video");
  const title = modal.querySelector("#preview-modal-title");
  const status = modal.querySelector("#preview-modal-status");
  if (!video) return;
  if (title) title.textContent = `片段预览 ${clip ? formatSeconds(clip.start) + "-" + formatSeconds(clip.end) : ""}`;
  if (status) {
    status.textContent = "正在生成片段预览...";
    status.classList.remove("is-hidden", "is-error");
  }
  modal.classList.remove("is-hidden");
  modal.setAttribute("aria-hidden", "false");
  video.pause();
  video.removeAttribute("src");
  video.load();
  const endpoint = scope === "mix" ? "/api/mix/preview/clip-video" : "/api/smart-cut/preview/clip-video";
  try {
    const result = await api(endpoint, {
      method: "POST",
      body: JSON.stringify({ preview_id: preview.id, clip_index: index }),
    });
    video.src = result.url;
    video.load();
    if (status) status.classList.add("is-hidden");
    try {
      await video.play();
    } catch (error) {
      // Some browsers block autoplay; controls are visible for manual play.
    }
  } catch (error) {
    if (status) {
      status.textContent = `预览生成失败：${error.message || error}`;
      status.classList.add("is-error");
      status.classList.remove("is-hidden");
    }
    throw error;
  }
}

function closePreviewVideo(scope = "smart") {
  const modal = $("preview-modal");
  const modalVideo = $("preview-modal-video");
  if (modalVideo) {
    modalVideo.pause();
    modalVideo.removeAttribute("src");
    modalVideo.load();
  }
  if (modal) {
    modal.classList.add("is-hidden");
    modal.setAttribute("aria-hidden", "true");
  }
  ["smart", "mix"].forEach((item) => {
    const player = item === "mix" ? $("mix-preview-player") : $("smart-preview-player");
    const video = item === "mix" ? $("mix-preview-video") : $("smart-preview-video");
    if (video) {
      video.pause();
      video.removeAttribute("src");
      video.load();
    }
    if (player) player.classList.add("is-hidden");
  });
}

function ensurePreviewModal() {
  let modal = $("preview-modal");
  if (modal) return modal;
  modal = document.createElement("div");
  modal.id = "preview-modal";
  modal.className = "preview-modal is-hidden";
  modal.setAttribute("aria-hidden", "true");
  modal.innerHTML = `
    <div class="preview-modal-backdrop" data-action="close-preview-video"></div>
    <div class="preview-modal-dialog" role="dialog" aria-modal="true" aria-labelledby="preview-modal-title">
      <div class="preview-modal-head">
        <strong id="preview-modal-title">片段预览</strong>
        <button class="button button-muted button-small" data-action="close-preview-video">关闭</button>
      </div>
      <div id="preview-modal-status" class="preview-modal-status is-hidden"></div>
      <video id="preview-modal-video" controls playsinline></video>
    </div>
  `;
  document.body.appendChild(modal);
  return modal;
}

async function startFeature(feature) {
  if (!feature) return;
  const result = await api(`/api/${feature}/start`, { method: "POST", body: "{}" });
  toast(result.message || "任务已提交", result.ok ? "success" : "warning");
  refreshTasks();
}

async function submitFeature(feature) {
  if (!feature) return;
  await saveFeaturePreferences();
  const payload = collectFeaturePayload(feature);
  await runPreflight(feature, payload, scopeForFeature(feature));
  const result = await api(`/api/${feature}/start`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
  toast(result.message || "任务已提交", result.ok ? "success" : "warning");
  refreshTasks();
}

async function runPreflight(feature, payload, scope) {
  const result = await api("/api/preflight", {
    method: "POST",
    body: JSON.stringify({ feature, payload }),
  });
  const errors = result.errors || [];
  const warnings = result.warnings || [];
  if (errors.length) {
    const message = `启动检查未通过：${errors.join("；")}`;
    appendLog(scope || "settings", {
      time: new Date().toLocaleTimeString(),
      level: "error",
      message: `${message}。解决办法：请先修正红色错误后再启动任务。`,
    });
    throw new Error(message);
  }
  if (warnings.length) {
    appendLog(scope || "settings", {
      time: new Date().toLocaleTimeString(),
      level: "warning",
      message: `启动检查提示：${warnings.join("；")}`,
    });
    toast(`启动检查提示：${warnings[0]}${warnings.length > 1 ? `（另有 ${warnings.length - 1} 项）` : ""}`, "warning");
  }
  return result;
}

function scopeForFeature(feature) {
  if (feature === "mix") return "mix";
  if (feature?.startsWith("ai-scan")) return "ai-scan";
  if (feature?.startsWith("product-scan")) return "product-scan";
  if (feature === "dedup") return "dedup";
  if (feature?.startsWith("live-rec")) return "live-rec";
  return "settings";
}

async function refreshTasks() {
  try {
    const data = await api("/api/tasks");
    const tasks = data.tasks || [];
    const latest = tasks.slice(-8).reverse();
    renderTaskBadges(latest);
    updateLogProgressFromTasks(tasks);
  } catch (error) {
    // The log websocket already reports connection state; keep this quiet.
  }
}

async function loadLatestSmartPreview() {
  try {
    const preview = await api("/api/smart-cut/preview/latest");
    if (!preview?.id) return;
    const isNewPreview = !state.smartPreview || preview.id !== state.smartPreview.id;
    const isNewerPreview = preview.created_at > (state.smartPreview?.created_at || 0);
    if (isNewPreview || isNewerPreview) {
      state.smartPreview = preview;
      renderSmartPreview(preview);
    }
  } catch (error) {
    // Preview is optional.
  }
}

async function loadLatestMixPreview() {
  try {
    const preview = await api("/api/mix/preview/latest");
    if (!preview?.id) return;
    const isNewPreview = !state.mixPreview || preview.id !== state.mixPreview.id;
    const isNewerPreview = preview.created_at > (state.mixPreview?.created_at || 0);
    if (isNewPreview || isNewerPreview) {
      state.mixPreview = preview;
      renderMixPreview(preview);
    }
  } catch (error) {
    // Preview is optional.
  }
}

async function pollSmartPreview(previewId, attempt = 0) {
  if (!previewId || attempt > 180) return;
  try {
    const preview = await api(`/api/smart-cut/preview/${encodeURIComponent(previewId)}`);
    state.smartPreview = preview;
    renderSmartPreview(preview);
    if (preview.status === "ready" || preview.status === "failed") return;
  } catch (error) {
    if (attempt > 3) toast(error.message || "读取选片预览失败", "error");
  }
  setTimeout(() => pollSmartPreview(previewId, attempt + 1), 2000);
}

async function pollMixPreview(previewId, attempt = 0) {
  if (!previewId || attempt > 180) return;
  try {
    const preview = await api(`/api/mix/preview/${encodeURIComponent(previewId)}`);
    state.mixPreview = preview;
    renderMixPreview(preview);
    if (preview.status === "ready" || preview.status === "failed") return;
  } catch (error) {
    if (attempt > 3) toast(error.message || "读取混剪选片预览失败", "error");
  }
  setTimeout(() => pollMixPreview(previewId, attempt + 1), 2000);
}

function previewSegments(clip) {
  return Array.isArray(clip?.segments) ? clip.segments : [];
}

function selectedPreviewSegments(clip) {
  return previewSegments(clip).filter((segment) => segment.selected !== false);
}

function selectedPreviewText(clip) {
  const segments = previewSegments(clip);
  if (segments.length) {
    const selected = selectedPreviewSegments(clip);
    if (!selected.length) return "\u672a\u9009\u62e9\u53e5\u5b50";
    return selected.map((segment) => String(segment.text || "").trim()).filter(Boolean).join(" ");
  }
  return String(clip?.text || "").trim();
}

function selectedSegmentCountText(clip) {
  const segments = previewSegments(clip);
  if (!segments.length) return "";
  return `${selectedPreviewSegments(clip).length}/${segments.length}\u53e5`;
}

function effectiveClipDuration(clip) {
  if (clip?.selected === false) return 0;
  const segments = previewSegments(clip);
  if (segments.length) {
    return selectedPreviewSegments(clip).reduce((sum, segment) => {
      const start = Number(segment.start || 0);
      const end = Number(segment.end || start);
      return sum + Math.max(0, Number(segment.duration || end - start));
    }, 0);
  }
  const start = Number(clip?.start || 0);
  const end = Number(clip?.end || start);
  return Math.max(0, Number(clip?.duration || end - start));
}

function effectiveClipBounds(clip) {
  const allSegments = previewSegments(clip);
  const segments = selectedPreviewSegments(clip);
  if (allSegments.length && segments.length) {
    const start = Math.min(...segments.map((segment) => Number(segment.start || 0)));
    const end = Math.max(...segments.map((segment) => Number(segment.end || Number(segment.start || 0))));
    return { start, end, duration: effectiveClipDuration(clip) };
  }
  const start = Number(clip?.start || 0);
  const end = Number(clip?.end || start);
  if (allSegments.length || clip?.selected === false) return { start, end, duration: 0 };
  return { start, end, duration: Math.max(0, Number(clip?.duration || end - start)) };
}

function previewClipRisk(clip, analysis) {
  const risk = analysis.riskByIndex.get(Number(clip.index));
  const riskLabel = clip.selected === false ? "未选" : (risk?.label || "正常");
  const riskClass = clip.selected === false ? "muted" : (risk?.level || "ok");
  return { risk, riskLabel, riskClass };
}

function previewDetailIndex(scope, clips) {
  const stored = Number(state.previewDetailSelection?.[scope]);
  if (Number.isInteger(stored) && clips.some((clip) => Number(clip.index) === stored)) return stored;
  const first = clips.find((clip) => clip.selected !== false) || clips[0];
  const next = Number(first?.index);
  state.previewDetailSelection[scope] = Number.isInteger(next) ? next : null;
  return state.previewDetailSelection[scope];
}

function setPreviewDetailSelection(scope = "smart", index) {
  const preview = getPreviewState(scope);
  if (!preview?.clips?.some((clip) => Number(clip.index) === index)) return;
  syncPreviewClipSelections(scope);
  state.previewDetailSelection[scope] = index;
  renderPreviewState(scope);
}

function renderClipStoryText(clip, repeatTags = "") {
  const segments = previewSegments(clip);
  if (segments.length > 1) {
    const selectedText = selectedPreviewText(clip);
    const rows = segments.map((segment) => {
      const text = String(segment.text || "").trim();
      if (!text) return "";
      return `<span class="clip-story-sentence ${segment.selected === false ? "is-context" : "is-kept"}">${escapeHtml(text)}</span>`;
    }).filter(Boolean).join("");
    return `<div class="clip-text clip-story-text clip-story-context" data-preview-selected-text title="${escapeHtml(selectedText || "")}">${rows}${repeatTags}</div>`;
  }
  const selectedText = selectedPreviewText(clip);
  const text = selectedText || clip?.text || "";
  return `<strong class="clip-text clip-story-text clip-selected-summary" data-preview-selected-text title="${escapeHtml(text)}">${escapeHtml(text)}${repeatTags}</strong>`;
}

function renderClipStoryCard(clip, position, scope, analysis, activeIndex) {
  const typeLabel = clipTypeLabel(clip.clip_type);
  const bounds = effectiveClipBounds(clip);
  const time = `${formatSeconds(bounds.start)}-${formatSeconds(bounds.end)}`;
  const duration = `${bounds.duration.toFixed(1)}s`;
  const checked = clip.selected === false ? "" : "checked";
  const { risk, riskLabel, riskClass } = previewClipRisk(clip, analysis);
  const repeatTags = renderManualRepeatTags(clip);
  const sourceName = clip.source_name || clip.source || "";
  const source = scope === "mix" && sourceName
    ? `<span class="clip-story-source" title="${escapeHtml(sourceName)}"> · ${escapeHtml(sourceName.split(/[\\/]/).filter(Boolean).pop() || sourceName)}</span>`
    : "";
  const riskBadge = riskClass === "ok" && riskLabel === "正常"
    ? ""
    : `<span class="clip-risk is-${riskClass}" title="${escapeHtml(risk?.detail || riskLabel)}">${escapeHtml(riskLabel)}</span>`;
  return `
    <article class="clip-preview-row clip-story-card ${clip.selected === false ? "is-unselected" : ""} ${Number(clip.index) === activeIndex ? "is-active" : ""}" draggable="true" data-preview-row data-preview-scope="${scope}" data-preview-index="${clip.index}">
      <div class="clip-story-select">
        <input type="checkbox" data-preview-clip="${clip.index}" data-preview-scope="${scope}" ${checked} title="保留这个片段">
        <div class="clip-drag-handle" title="拖拽排序" aria-label="拖拽排序">&#9776;</div>
      </div>
      <div class="clip-story-main">
        <div class="clip-story-topline">
          <span class="clip-story-meta">#${position + 1} · ${escapeHtml(typeLabel)} · ${escapeHtml(time)} · ${escapeHtml(duration)}${source}</span>
          ${riskBadge}
        </div>
        <div class="clip-content">
          ${renderClipStoryText(clip, repeatTags)}
        </div>
      </div>
    </article>
  `;
}

function renderPreviewDetailPanel(scope, preview, analysis, activeIndex) {
  const clips = preview?.clips || [];
  const clip = clips.find((item) => Number(item.index) === activeIndex) || clips[0];
  if (!clip) {
    return `<aside class="clip-detail-panel"><p>请选择左侧片段查看句子。</p></aside>`;
  }
  const position = Math.max(0, clips.findIndex((item) => Number(item.index) === Number(clip.index)));
  const typeLabel = clipTypeLabel(clip.clip_type);
  const bounds = effectiveClipBounds(clip);
  const time = `${formatSeconds(bounds.start)}-${formatSeconds(bounds.end)}`;
  const duration = `${bounds.duration.toFixed(1)}s`;
  const { risk, riskLabel, riskClass } = previewClipRisk(clip, analysis);
  const segments = previewSegments(clip);
  const segmentCountText = selectedSegmentCountText(clip) || "整段";
  const segmentRows = segments.length ? segments.map((segment) => {
    const checked = segment.selected === false ? "" : "checked";
    const start = Number(segment.start || 0);
    const end = Number(segment.end || start);
    const segmentDuration = Math.max(0, Number(segment.duration || end - start));
    const segmentTitle = escapeHtml(segment.text || "");
    return `
      <label class="clip-segment clip-detail-segment ${segment.selected === false ? "is-unselected" : ""}" title="${segmentTitle}" data-preview-segment-row data-preview-scope="${scope}" data-preview-segment-parent="${Number(clip.index)}" data-preview-segment-index="${Number(segment.index)}" draggable="false">
        <input type="checkbox" data-preview-segment data-preview-scope="${scope}" data-preview-segment-parent="${Number(clip.index)}" data-preview-segment-index="${Number(segment.index)}" ${checked}>
        <span class="clip-segment-time">${escapeHtml(formatSeconds(start))}-${escapeHtml(formatSeconds(end))}<em>${segmentDuration.toFixed(1)}s</em></span>
        <span class="clip-segment-text">${segmentTitle}</span>
      </label>
    `;
  }).join("") : `<div class="clip-detail-empty">这个片段没有句子拆分，将按整段参与成片。</div>`;
  return `
    <aside class="clip-detail-panel" data-preview-detail="${scope}">
      <div class="clip-detail-head">
        <div>
          <span>句子选择</span>
          <strong>#${position + 1} ${escapeHtml(typeLabel)}</strong>
        </div>
        <button class="button button-secondary button-small" data-action="preview-clip-video" data-preview-scope="${scope}" data-preview-index="${clip.index}">预览</button>
      </div>
      <div class="clip-detail-stats">
        <span>${escapeHtml(time)}</span>
        <span>${escapeHtml(duration)}</span>
        <span>${escapeHtml(segmentCountText)}</span>
        <span class="clip-risk is-${riskClass}" title="${escapeHtml(risk?.detail || riskLabel)}">${escapeHtml(riskLabel)}</span>
      </div>
      <div class="clip-detail-segments">
        ${segmentRows}
      </div>
    </aside>
  `;
}

function renderPreviewWorkbench(scope, preview, targetId) {
  ensurePreviewDraft(scope);
  const clips = preview?.clips || [];
  const analysis = analyzeSmartPreview(preview, targetId);
  const activeIndex = previewDetailIndex(scope, clips);
  const rows = clips.map((clip, position) => renderClipStoryCard(clip, position, scope, analysis, activeIndex));
  return `
    <div data-preview-summary="${scope}">${renderPreviewSummary(analysis)}</div>
    <div class="clip-preview-workbench">
      <div class="clip-story-list" role="list">
        ${rows.join("")}
      </div>
      ${renderPreviewDetailPanel(scope, preview, analysis, activeIndex)}
    </div>
  `;
}

function renderSmartPreview(preview) {
  const box = $("smart-preview");
  const count = $("smart-preview-count");
  if (!box) return;
  const clips = preview?.clips || [];
  if (count) count.textContent = String(clips.length || 0);
  box.classList.toggle("empty", !clips.length);
  if (!preview?.id) {
    box.innerHTML = "<p>点击“AI选片预览”，先看 AI 会选哪些片段，再决定是否成片。</p>";
    closePreviewVideo();
    return;
  }
  if (preview.status === "running") {
    box.innerHTML = "<p>正在生成 AI 选片预览，请稍等...</p>";
    closePreviewVideo();
    return;
  }
  if (preview.status === "failed") {
    box.innerHTML = `<p>选片预览失败：${escapeHtml(preview.error || preview.message || "未知错误")}</p>`;
    closePreviewVideo();
    return;
  }
  if (!clips.length) {
    box.innerHTML = "<p>还没有可预览的片段。</p>";
    return;
  }
  box.innerHTML = renderPreviewWorkbench("smart", preview, "sc-duration");
  updatePreviewStickyOffset("smart");
  bindPreviewRowDrag(box, "smart");
}

function renderMixPreview(preview) {
  const box = $("mix-preview");
  const count = $("mix-preview-count");
  if (!box) return;
  const clips = preview?.clips || [];
  if (count) count.textContent = String(clips.length || 0);
  box.classList.toggle("empty", !clips.length);
  if (!preview?.id) {
    box.innerHTML = "<p>点击“AI选片预览”，先看混剪会从哪些素材里选哪些片段。</p>";
    closePreviewVideo("mix");
    return;
  }
  if (preview.status === "running") {
    box.innerHTML = "<p>正在生成混剪 AI 选片预览，请稍等...</p>";
    closePreviewVideo("mix");
    return;
  }
  if (preview.status === "failed") {
    box.innerHTML = `<p>混剪选片预览失败：${escapeHtml(preview.error || preview.message || "未知错误")}</p>`;
    closePreviewVideo("mix");
    return;
  }
  if (!clips.length) {
    box.innerHTML = "<p>还没有可预览的混剪片段。</p>";
    return;
  }
  box.innerHTML = renderPreviewWorkbench("mix", preview, "mix-duration");
  updatePreviewStickyOffset("mix");
  bindPreviewRowDrag(box, "mix");
}

function renderClipMeta(clip, position, riskLabel) {
  const pieces = [
    `#${position + 1}`,
    `原序${Number(clip?.index || 0) + 1}`,
  ];
  const score = Number(clip?.score || 0);
  if (score) pieces.push(`分数${score.toFixed(1)}`);
  if (clip?.focus_block && clip.focus_block !== clip.focus) pieces.push(`块:${clip.focus_block}`);
  if (clip?.source_name) pieces.push(`源:${clip.source_name}`);
  if (clip?.selected === false) pieces.push("已取消");
  if (riskLabel && riskLabel !== "正常") pieces.push(`风险:${riskLabel}`);
  return `<div class="clip-meta">${pieces.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>`;
}

function clipTextForScore(clip) {
  return `${clip?.clip_type || ""} ${clip?.focus || ""} ${clip?.text || ""}`.toLowerCase();
}

function classifyClipScoreTags(clip) {
  const text = clipTextForScore(clip);
  const tags = [];
  const add = (label, tone = "info", detail = "") => tags.push({ label, tone, detail: detail || label });
  if ((clip?.clip_type || "").toLowerCase() === "hook" || /hook|爆点|痛点|开头|第一眼|有没有发现|姐妹们/.test(text)) {
    add("Hook", "strong", "开头吸引片段");
  }
  if (/显瘦|遮肉|收腰|面料|质感|亲肤|显白|高级|好看|版型|垂感|透气|不皱|小个子|高腰|颜色|工艺|舒服|薄/.test(text)) {
    add("卖点", "good", "包含产品卖点");
  }
  if (/尺码|码|s码|m码|l码|xl|xxl|斤|身高|体重|小码|中码|大码|正码|拍大|拍小/.test(text)) {
    add("尺码", "neutral", "包含尺码/体重信息");
  }
  if (/通勤|上班|上学|约会|旅游|日常|逛街|聚会|见家长|职场|教师|体制|场景|夏天|春天|秋天|冬天/.test(text)) {
    add("场景", "neutral", "包含穿着场景");
  }
  if (/(\d+(\.\d+)?\s*(元|块|米|¥|￥))|价格|福利价|到手|拍下|下单|库存|链接|号链接|优惠|券/.test(text)) {
    add("疑似价格", "warn", "可能包含价格/下单/库存信息");
  }
  if (/嗯+|啊+|然后呢|然后的话|这个的话|就是说|对吧|是不是|家人们|宝贝们|直播间|稍等|看一下|废话|闲聊|哈哈|欢迎|关注/.test(text)) {
    add("疑似废话", "warn", "可能是口头禅、闲聊或直播间废话");
  }
  return tags.length ? tags : [{ label: "普通", tone: "muted", detail: "未识别到明显标签" }];
}

function renderClipScoreTags(clip) {
  return classifyClipScoreTags(clip)
    .slice(0, 4)
    .map((tag) => `<span class="clip-score-tag is-${tag.tone}" title="${escapeHtml(tag.detail)}">${escapeHtml(tag.label)}</span>`)
    .join("");
}

function renderManualRepeatTags(clip) {
  const checks = Array.isArray(clip?.manual_repeat_checks) ? clip.manual_repeat_checks : [];
  if (!checks.length) return "";
  const strongest = checks.some((item) => item.level === "high") ? "high" : "near";
  const title = checks
    .map((item) => `${item.reason || "疑似重复"} · 与 #${Number(item.with) + 1 || "?"} · ${(Number(item.score || 0) * 100).toFixed(0)}%`)
    .join("；");
  const label = strongest === "high" ? "高度相似" : "疑似重复";
  return `<span class="clip-repeat-tag is-${strongest}" title="${escapeHtml(title)}">${label}</span>`;
}

function analyzeSmartPreview(preview, targetId = "sc-duration") {
  const clips = (preview?.clips || []).filter((clip) => clip.selected !== false);
  const dedupSummary = preview?.dedup_summary || {};
  const target = Number(preview?.target_duration || $(targetId)?.value || 60);
  const total = clips.reduce((sum, clip) => sum + effectiveClipDuration(clip), 0);
  const riskByIndex = new Map();
  const warnings = [];

  clips.forEach((clip, index) => {
    const repeatChecks = Array.isArray(clip?.manual_repeat_checks) ? clip.manual_repeat_checks : [];
    if (repeatChecks.length) {
      const high = repeatChecks.some((item) => item.level === "high");
      const risk = {
        level: high ? "bad" : "warn",
        label: high ? "高度相似" : "疑似重复",
        detail: repeatChecks.map((item) => item.reason || "疑似重复，建议人工确认").join("；"),
      };
      riskByIndex.set(Number(clip.index), risk);
      warnings.push(risk.detail);
    }
    const bounds = effectiveClipBounds(clip);
    const start = bounds.start;
    const end = bounds.end;
    const duration = bounds.duration;
    if (duration < 1.2) {
      riskByIndex.set(Number(clip.index), {
        level: "warn",
        label: "过短",
        detail: "片段短于 1.2 秒，可能影响观感。",
      });
    }
    if (Math.abs(duration - Math.max(0, end - start)) > 0.25) {
      riskByIndex.set(Number(clip.index), {
        level: "warn",
        label: "时长异常",
        detail: "片段时长和起止时间不一致，建议预览确认。",
      });
    }
    if (index === 0) return;
    const prev = clips[index - 1];
    const prevBounds = effectiveClipBounds(prev);
    const gap = start - Number(prevBounds.end || 0);
    if (gap < -0.05) {
      const risk = {
        level: "bad",
        label: `重叠${Math.abs(gap).toFixed(2)}s`,
        detail: "后一段开始时间早于前一段结束时间，可能出现音画串段。",
      };
      riskByIndex.set(Number(clip.index), risk);
      warnings.push(risk.detail);
    } else if (gap >= -0.05 && gap < 0.12) {
      const risk = {
        level: "warn",
        label: `贴边${Math.max(0, gap).toFixed(2)}s`,
        detail: "两段之间几乎没有缓冲，建议预览确认前一段语音是否收干净。",
      };
      riskByIndex.set(Number(clip.index), risk);
      warnings.push(risk.detail);
    } else if (gap > 120) {
      const risk = {
        level: "warn",
        label: `跳变${formatSeconds(gap)}`,
        detail: "相邻片段在原视频中间隔较大，成片可能有跳跃感。",
      };
      riskByIndex.set(Number(clip.index), risk);
      warnings.push(risk.detail);
    }
  });

  const diff = total - target;
  const ratio = target > 0 ? total / target : 1;
  let status = "ok";
  let statusText = "接近目标";
  if (ratio < 0.85) {
    status = "warn";
    statusText = "偏短";
  } else if (ratio > 1.15) {
    status = "warn";
    statusText = "偏长";
  }
  return {
    clips,
    target,
    total,
    diff,
    status,
    statusText,
    riskByIndex,
    riskCount: Array.from(riskByIndex.values()).filter((item) => item.level !== "ok").length,
    warnings: Array.from(new Set(warnings)).slice(0, 3),
    autoRemovedCount: Number(dedupSummary.auto_removed_count || 0),
    manualCheckCount: Number(dedupSummary.manual_check_count || 0),
    categorySummary: dedupSummary.category_summary || {},
  };
}

function renderPreviewSummary(analysis) {
  const diffText = analysis.diff >= 0 ? `+${analysis.diff.toFixed(1)}s` : `${analysis.diff.toFixed(1)}s`;
  const category = analysis.categorySummary || {};
  const mainCategory = category.main_category || "-";
  const protectedCategories = Array.isArray(category.protected_categories)
    ? category.protected_categories.filter((item) => item && item !== mainCategory)
    : [];
  const categoryTitle = protectedCategories.length
    ? `保护：${protectedCategories.join("、")}；过滤：${Number(category.removed_segments || 0)} 段`
    : `过滤：${Number(category.removed_segments || 0)} 段`;
  const categoryText = protectedCategories.length
    ? `${mainCategory}+${protectedCategories.join("/")}`
    : mainCategory;
  const warningText = analysis.warnings.length
    ? `<div class="preview-warnings">${analysis.warnings.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>`
    : `<div class="preview-warnings is-ok"><span>未发现明显时间戳风险。</span></div>`;
  return `
    <div class="clip-preview-summary">
      <div><span>已选片段</span><strong>${analysis.clips.length}</strong></div>
      <div><span>总时长</span><strong>${analysis.total.toFixed(1)}s</strong></div>
      <div><span>目标差值</span><strong class="is-${analysis.status}">${diffText}</strong></div>
      <div><span>已自动处理</span><strong class="is-${analysis.autoRemovedCount ? "warn" : "ok"}">${analysis.autoRemovedCount ? `${analysis.autoRemovedCount} 段` : "无"}</strong></div>
      <div><span>人工检查</span><strong class="is-${analysis.manualCheckCount ? "warn" : "ok"}">${analysis.manualCheckCount ? `${analysis.manualCheckCount} 组` : "无"}</strong></div>
      <div><span>品类</span><strong title="${escapeHtml(categoryTitle)}">${escapeHtml(categoryText)}</strong></div>
    </div>
    ${warningText}
  `;
}

function clipTypeLabel(type) {
  const map = { hook: "开头", product: "卖点", close: "结尾", bridge: "承接", trend: "趋势" };
  return map[type] || type || "片段";
}

function renderTaskBadges(tasks) {
  document.querySelectorAll(".page-header").forEach((header) => {
    let badge = header.querySelector(".task-strip");
    if (!badge) {
      badge = document.createElement("div");
      badge.className = "task-strip";
      header.appendChild(badge);
    }
    const page = header.closest(".page")?.id?.replace("page-", "");
    const scoped = tasks.filter((task) => task.scope === page || (page === "settings" && task.scope === "settings")).slice(0, 3);
    badge.innerHTML = scoped.map(taskItem).join("");
  });
}

function taskItem(task) {
  const cls = `task-pill is-${task.status || "queued"}`;
  const statusLabels = {
    queued: "排队中",
    running: "运行中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已停止",
  };
  const stateText = task.message || statusLabels[task.status] || task.status || "";
  const text = `${escapeHtml(task.title || task.scope)} · ${escapeHtml(stateText)}`;
  return `<span class="${cls}" title="${escapeHtml(task.error || "")}">${text}</span>`;
}

async function loadScanResults() {
  try {
    const data = await api("/api/scan-results");
    renderScanResults(data.products || [], data.merged || []);
    renderProductPreview(data.schedule_groups || []);
  } catch (error) {
    // Results are optional; empty state stays visible.
  }
}

function renderScanResults(products, merged) {
  const box = $("scan-results");
  if (!box) return;
  const rows = [];
  products.slice(0, 40).forEach((item) => {
    rows.push(`<div class="result-row"><strong>${escapeHtml(item.name)}</strong><span>${formatSeconds(item.start)}-${formatSeconds(item.end)}</span><span>${escapeHtml(item.video || "")}</span></div>`);
  });
  if (merged.length) {
    rows.unshift(`<div class="result-row result-head"><strong>合并结果 ${merged.length}</strong><span>总时长</span><span>来源</span></div>`);
    merged.slice(0, 20).forEach((item) => {
      rows.push(`<div class="result-row"><strong>${escapeHtml(item.name)}</strong><span>${formatSeconds(item.total_duration)}</span><span>${item.source_count || 0} 个文件</span></div>`);
    });
  }
  box.classList.toggle("empty", rows.length === 0);
  box.innerHTML = rows.length ? rows.join("") : "<p>扫描后会在这里显示可导出的单品片段。</p>";
  const count = $("scan-selected-count");
  if (count) count.textContent = String(products.length + merged.length);
}

function renderProductPreview(groups) {
  const box = $("product-preview");
  if (!box) return;
  const rows = groups.slice(0, 60).map((item) => {
    return `<div class="result-row"><strong>${escapeHtml(item.name)}</strong><span>${item.segments || 0} 段</span><span>${formatSeconds(item.total_duration)}</span></div>`;
  });
  box.classList.toggle("empty", rows.length === 0);
  box.innerHTML = rows.length ? rows.join("") : "<p>读取 Excel 后显示单品时间段、标题和导出状态。</p>";
}

function formatSeconds(value) {
  const seconds = Math.max(0, Number(value || 0));
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function collectFeaturePayload(feature) {
  if (feature === "mix") {
    return {
      video_paths: getLines("mix-video-paths"),
      output_dir: $("mix-output-dir").value.trim(),
      category: $("mix-category").value,
      versions: Number($("mix-versions").value || 1),
      duration: Number($("mix-duration").value || 60),
      focus_hint: $("mix-focus").value,
      ai_controls: collectAiControls("mix"),
      dedup_preset: $("mix-dedup").value,
      mirror_enabled: $("mix-mirror").checked,
      subtitle_overlay: $("mix-subtitle").checked,
      smart_crop_enabled: $("mix-crop").checked,
      crop_level: $("mix-crop-level").value,
      ken_burns_enabled: $("mix-kenburns").checked,
      ken_burns_intensity: $("mix-kb-intensity").value,
      ...collectPipPayload("mix"),
    };
  }

  if (feature.startsWith("ai-scan")) {
    return {
      video_paths: getLines("scan-video-paths"),
      output_dir: $("scan-output-dir").value.trim(),
      auto_export: $("scan-auto-export").checked,
    };
  }

  if (feature.startsWith("product-scan")) {
    return {
      excel_path: $("ps-excel-path").value.trim(),
      video_paths: getLines("ps-video-paths"),
      output_dir: $("ps-output-dir").value.trim(),
      advance_seconds: Number($("ps-advance").value || 0),
      video_start_offset: $("ps-video-start-offset")?.value.trim() || "",
      live_start_time: $("ps-live-start-time")?.value.trim() || "",
    };
  }

  if (feature === "dedup") {
    const videoPaths = getLines("dedup-video-paths");
    return {
      video_path: videoPaths[0] || "",
      video_paths: videoPaths,
      output_dir: $("dedup-output-dir").value.trim(),
      dedup_preset: $("dedup-preset").value,
      ...collectPipPayload("dedup"),
      video: {
        mirror: $("dedup-mirror").checked,
        crop: $("dedup-crop").checked,
        crop_value: Number($("dedup-crop-value").value || 0),
        speed: $("dedup-speed").checked,
        speed_value: Number($("dedup-speed-value").value || 100),
        frame_structure: $("dedup-frame-structure").checked,
        frame_structure_level: $("dedup-frame-level").value,
        blur: $("dedup-blur").checked,
        blur_value: Number($("dedup-blur-value").value || 0),
        sharpen: $("dedup-sharpen").checked,
        sharpen_value: Number($("dedup-sharpen-value").value || 0),
        gamma_shift: $("dedup-color").checked,
        corner_mask: $("dedup-mask").checked,
        bg_fill: $("dedup-bg-fill").checked,
        bg_image: $("dedup-bg-image").value.trim(),
      },
      audio: {
        pitch: $("dedup-audio-pitch").checked,
        reverb: $("dedup-audio-reverb").checked,
        noise_fusion: $("dedup-noise-fusion").checked,
      },
    };
  }

  if (feature.startsWith("live-rec")) {
    return {
      save_dir: $("live-save-dir").value.trim(),
      segment: $("live-segment").value,
      check_interval: Number($("live-check-interval").value || 30),
      room_name: $("live-room-name").value.trim(),
      room_url: $("live-room-url").value.trim(),
      platform: $("live-platform").value,
    };
  }

  return {};
}

function resetDedupDefaults() {
  $("dedup-preset").value = "heavy";
  $("dedup-mirror").checked = true;
  $("dedup-crop").checked = true;
  $("dedup-crop-value").value = "5";
  $("dedup-speed").checked = true;
  $("dedup-speed-value").value = "115";
  $("dedup-frame-structure").checked = true;
  $("dedup-frame-level").value = "heavy";
  $("dedup-blur").checked = false;
  $("dedup-blur-value").value = "2";
  $("dedup-sharpen").checked = false;
  $("dedup-sharpen-value").value = "30";
  $("dedup-color").checked = true;
  $("dedup-mask").checked = true;
  $("dedup-bg-fill").checked = false;
  $("dedup-pip-mode").value = "off";
  $("dedup-pip-path").value = "";
  $("dedup-pip-folder").value = "";
  $("dedup-pip-size").value = "0.15";
  $("dedup-pip-opacity").value = "0.03";
  $("dedup-pip-pos").value = "右下";
  $("dedup-audio-pitch").checked = true;
  $("dedup-audio-reverb").checked = false;
  $("dedup-noise-fusion").checked = true;
  refreshFeaturePreferenceUi();
  scheduleFeaturePreferenceSave();
  toast("去重参数已恢复默认", "success");
}

function addLiveRoom() {
  const name = $("live-room-name").value.trim();
  const url = $("live-room-url").value.trim();
  const platform = $("live-platform").value;
  if (!name || !url) {
    toast("请先填写直播间名称和地址", "warning");
    return;
  }
  const table = document.querySelector("#page-live-rec .table-like");
  const empty = table?.querySelector(".empty-row");
  if (empty) empty.remove();
  const row = document.createElement("div");
  row.className = "table-row";
  row.innerHTML = `<span>${escapeHtml(name)}</span><span>${escapeHtml(platform)}</span><span>${escapeHtml(url)}</span><span>未开播</span><span>-</span><span>-</span><span>待接入</span>`;
  table?.appendChild(row);
  appendLog("live-rec", { time: new Date().toLocaleTimeString(), level: "info", message: `已添加直播间: ${name}` });
  $("live-room-name").value = "";
  $("live-room-url").value = "";
}

function connectLogSocket() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${location.host}/ws/log`);
  const dot = $("server-dot");
  const label = $("server-state");

  socket.addEventListener("open", () => {
    dot?.classList.add("is-online");
    if (label) label.textContent = "已连接";
  });

  socket.addEventListener("message", (event) => {
    try {
      const message = JSON.parse(event.data);
      if (message.type === "log") appendLog(message.scope || "settings", message);
    } catch (error) {
      console.warn("log parse failed", error);
    }
  });

  socket.addEventListener("close", () => {
    dot?.classList.remove("is-online");
    if (label) label.textContent = "重连中";
    setTimeout(connectLogSocket, 1500);
  });
}

function appendLog(scope, item) {
  const targetScope = $(`log-${scope}`) ? scope : "settings";
  const container = $(`log-${targetScope}`);
  if (!container) return;

  const diagnostic = isDiagnosticLog(item);
  const destination = diagnostic ? ensureDiagnosticLogView(targetScope) : container;
  if (!destination) return;

  if (diagnostic) {
    destination.querySelector(".diagnostic-empty")?.remove();
  }

  const row = createLogRow(item);
  destination.appendChild(row);
  destination.scrollTop = destination.scrollHeight;
  updateProgressFromLog(targetScope, item);

  if (diagnostic) {
    state.diagnosticLogs[targetScope] = (state.diagnosticLogs[targetScope] || 0) + 1;
    document.querySelectorAll(`.diagnostic-toggle[data-scope="${targetScope}"]`).forEach(updateDiagnosticButton);
    return;
  }

  state.logs[targetScope] = (state.logs[targetScope] || 0) + 1;
  const counter = $(logCounterId(targetScope));
  if (counter) counter.textContent = String(state.logs[targetScope]);
}

function createLogRow(item) {
  const row = document.createElement("div");
  row.className = `log-entry is-${item.level || "info"}`;

  const time = document.createElement("span");
  time.className = "log-time";
  time.textContent = item.time || "";

  const level = document.createElement("span");
  level.className = `log-level-${item.level || "info"}`;
  level.textContent = (item.level || "info").toUpperCase();

  const message = document.createElement("span");
  message.className = "log-message";
  fillLogMessage(message, item.message || "", item.level || "info");
  if (item.raw) {
    const raw = document.createElement("code");
    raw.className = "log-raw";
    raw.textContent = item.raw;
    message.appendChild(raw);
  }

  row.append(time, level, message);
  return row;
}

function isDiagnosticLog(item) {
  const level = String(item?.level || "info").toLowerCase();
  const message = String(item?.message || "");
  const raw = String(item?.raw || "");
  if (level === "error" || level === "success" || level === "warning") return false;
  if (raw) return true;
  const compact = message.replace(/\s+/g, " ").trim();
  if (!compact) return false;
  const keepMain = [
    "任务已启动",
    "预览完成",
    "成片完成",
    "混剪完成",
    "目标时长",
    "使用本地SRT",
    "AI选片完成",
    "最终片单",
    "智能成片任务完成",
    "混剪成片完成",
    "处理失败",
  ];
  if (keepMain.some((token) => compact.includes(token))) return false;
  const diagnosticTokens = [
    "AI:",
    "编排AI",
    "temperature=",
    "Hook候选",
    "历史避让",
    "差异化历史",
    "时间跳变",
    "Source map",
    "Mapped:",
    "Source:",
    "SmartCrop",
    "Cut [",
    "Cut ",
    "Concat",
    "KenBurns",
    "Ken Burns",
    "ffmpeg:",
    "drawtext",
    "滤镜",
    "volcengine_asr",
    "TOS",
    "DeepSeek",
    "字幕修复模型",
    "[PROGRESS]",
    "硬件诊断",
    "编码器:",
    "去重效果",
    "音频提取",
  ];
  if (diagnosticTokens.some((token) => compact.includes(token))) return true;
  if (/^\s*(hook|product|close|bridge|trend)\s*\|/i.test(compact)) return true;
  if (/^\s*\[[^\]]+\]\s+\d+(\.\d+)?-\d+(\.\d+)?s/.test(compact)) return true;
  return false;
}

function fillLogMessage(target, rawMessage, level) {
  const message = String(rawMessage || "");
  if (level === "error" && message.includes("解决办法：")) {
    const [problem, solution] = message.split("解决办法：");
    const problemLine = document.createElement("span");
    problemLine.className = "log-problem";
    problemLine.textContent = problem.replace(/。$/, "");
    const solutionLine = document.createElement("span");
    solutionLine.className = "log-solution";
    solutionLine.textContent = `解决办法：${solution}`;
    target.append(problemLine, solutionLine);
    return;
  }
  target.textContent = message;
}

function clearLog(scope) {
  if (!scope) return;
  const container = $(`log-${scope}`);
  if (!container) return;
  container.innerHTML = "";
  const diagnostic = ensureDiagnosticLogView(scope);
  if (diagnostic) {
    diagnostic.innerHTML = '<div class="diagnostic-empty">高级诊断打开后，这里显示 AI、切割、拼接、字幕和 FFmpeg 细节。</div>';
  }
  state.logs[scope] = 0;
  state.diagnosticLogs[scope] = 0;
  const counter = $(logCounterId(scope));
  if (counter) counter.textContent = "0";
  resetLogProgress(scope);
  document.querySelectorAll(`.diagnostic-toggle[data-scope="${scope}"]`).forEach(updateDiagnosticButton);
}

function logCounterId(scope) {
  if (scope === "smart-cut") return "smart-log-count";
  return `${scope}-log-count`;
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function toast(message, type = "success") {
  const stack = $("toast-stack");
  if (!stack) return;
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.textContent = message;
  stack.appendChild(item);
  setTimeout(() => item.remove(), 3600);
}
