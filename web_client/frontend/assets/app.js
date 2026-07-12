const state = {
  page: "smart-cut",
  settingsTab: "ai",
  liveRecTab: "rooms",
  smartPreview: null,
  mixPreview: null,
  diagnosticsVisible: false,
  videoInfoByTarget: {},
  videoInfoRequestSeq: {},
  pipPoolByPrefix: {},
  pipPoolRequestSeq: {},
  keywordConfig: {},
  progressByScope: {},
  legacyBatchProgress: {},
  mixGroups: [],
  activeMixGroupIndex: null,
  previewDrafts: {},
  previewDraftSaveTimers: {},
  previewDetailSelection: { smart: null, mix: null },
  previewInlineVideos: {},
  previewSplitRatios: {},
  previewPrepAutoCollapsed: { smart: false, mix: false },
  runningScopes: new Set(),
  latestTasks: [],
  outputHistory: [],
  outputHistoryFetchedAt: 0,
  lastIssuesByScope: {},
  lastPreflightLog: null,
  liveRooms: [],
  liveRoomFilter: "all",
  liveRoomSearch: "",
  liveRoomPlatform: "all",
  update: {
    checked: false,
    checking: false,
    installing: false,
    available: false,
    info: null,
    message: "\u672a\u68c0\u67e5",
    error: "",
  },
  runtime: null,
  liveRoomActivity: {},
  featurePreferencesLoading: false,
  featurePreferencesSaveTimer: null,
  logs: {
    "smart-cut": 0,
    settings: 0,
    mix: 0,
    "ai-scan": 0,
    "product-scan": 0,
    "video-split": 0,
    dedup: 0,
    "live-rec": 0,
  },
  diagnosticLogs: {
    "smart-cut": 0,
    settings: 0,
    mix: 0,
    "ai-scan": 0,
    "product-scan": 0,
    "video-split": 0,
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
  style_profile_strength: "s-style-profile-strength",
};

const keywordFields = {
  clip_keywords: "kw-clip-keywords",
  forbidden_phrases: "kw-forbidden-phrases",
  filler_words: "kw-filler-words",
  preference_keywords: "kw-preference-keywords",
};

const customAiPresetsKey = "lc:custom-ai-presets";
const liveRoomsStorageKey = "lc:live-rooms";
const themeStorageKey = "lc:ui-theme";
const uiFontSizeStorageKey = "lc:ui-font-size";
const previewDraftStoragePrefix = "lc:preview-draft:";
const validThemes = new Set(["system", "light", "dark"]);
const compactVideoListTargetIds = new Set(["video-paths", "mix-video-paths"]);

const progressScopes = ["smart-cut", "mix", "ai-scan", "product-scan", "video-split", "dedup", "live-rec", "settings"];

const progressStageRules = [
  { label: "准备素材", percent: 12, tokens: ["任务已启动", "启动", "目标时长", "读取", "上传"] },
  { label: "标准化素材", percent: 22, tokens: ["TS", "标准化", "normalized", "remux", "转码", "CFR"] },
  { label: "识别字幕", percent: 36, tokens: ["语音识别中", "启动本地语音识别", "云端语音识别", "ASR 识别", "ASR成功", "ASR 成功"] },
  { label: "AI 分析", percent: 56, tokens: ["AI 智能选片", "AI 选片", "候选", "评分", "片单", "最终片单", "预览列表"] },
  { label: "分析画面", percent: 64, tokens: ["检测到源视频", "输出分辨率", "SmartCrop: 检测", "SmartCrop: 应用", "SmartCrop: cover"] },
  { label: "裁剪片段", percent: 72, tokens: ["开始切割", "切割片段中", "裁剪", "剪辑", "Cut ["] },
  { label: "动态画面", percent: 84, tokens: ["KenBurns", "Ken Burns", "动态", "缩放"] },
  { label: "拼接合并", percent: 86, tokens: ["拼接", "Concatenating", "Concat done", "Concat copy"] },
  { label: "去重处理", percent: 90, tokens: ["整体去重", "去重步骤", "去重效果", "去重完成", "dedup"] },
  { label: "字幕处理", percent: 94, tokens: ["字幕处理中", "字幕时间轴", "字幕烧录", "drawtext", "DeepSeek修复", "画中画"] },
  { label: "收尾校验", percent: 97, tokens: ["成品真实时长", "切割报告", "生成成功", "输出路径", "大小:", "片段:"] },
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
      "sc-dedup-crop",
      "sc-dedup-crop-value",
      "sc-dedup-speed",
      "sc-dedup-speed-value",
      "sc-dedup-frame-structure",
      "sc-dedup-frame-level",
      "sc-dedup-blur",
      "sc-dedup-blur-value",
      "sc-dedup-sharpen",
      "sc-dedup-sharpen-value",
      "sc-dedup-color",
      "sc-dedup-mask",
      "sc-dedup-bg-fill",
      "sc-dedup-audio-pitch",
      "sc-dedup-audio-reverb",
      "sc-dedup-noise-fusion",
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
      "mix-dedup-crop",
      "mix-dedup-crop-value",
      "mix-dedup-speed",
      "mix-dedup-speed-value",
      "mix-dedup-frame-structure",
      "mix-dedup-frame-level",
      "mix-dedup-blur",
      "mix-dedup-blur-value",
      "mix-dedup-sharpen",
      "mix-dedup-sharpen-value",
      "mix-dedup-color",
      "mix-dedup-mask",
      "mix-dedup-bg-fill",
      "mix-dedup-audio-pitch",
      "mix-dedup-audio-reverb",
      "mix-dedup-noise-fusion",
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
  video_split: {
    prefixes: [],
    ids: [
      "vs-output-dir",
      "vs-split-mode",
      "vs-segment-count",
      "vs-segment-seconds",
    ],
  },
  live_rec: {
    prefixes: [],
    ids: [
      "live-save-dir",
      "live-segment",
      "live-check-interval",
      "live-min-stream-quality",
      "live-platform",
      "live-product-split-enabled",
      "live-product-auto-cut",
      "live-product-default-minutes",
      "live-product-min-minutes",
      "live-product-max-minutes",
      "live-product-switch-confirm",
      "live-product-head-seconds",
      "live-product-tail-seconds",
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
  food_fresh: {
    label: "食品/生鲜",
    category: "食品/生鲜",
    goal: "食欲种草",
    focus: "口感食欲",
    hook: "试吃反应开头",
    ending: "发货保鲜",
    strictness: "标准",
    selling: ["口感食欲", "新鲜品质", "发货保鲜", "场景吃法"],
    avoid: ["价格", "闲聊", "保健功效", "重复卖点"],
  },
};

document.addEventListener("DOMContentLoaded", () => {
  bindNavigation();
  bindSettingsTabs();
  bindLiveRecTabs();
  bindLiveRoomFilters();
  bindActions();
  syncVideoSplitMode();
  bindAiPresetControls();
  bindPreviewControls();
  bindFeaturePreferenceAutoSave();
  bindDedupCustomControls();
  setupCollapsiblePanels();
  setupAdvancedParamToggles();
  setupLogProgressBars();
  bindPreviewModalShortcuts();
  loadLiveRooms();
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
  syncFlowActionState();
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
    const error = new Error(sanitizeApiText(detail, "\u8bf7\u6c42\u5931\u8d25\uff0c\u8bf7\u68c0\u67e5\u9875\u9762\u53c2\u6570\u540e\u91cd\u8bd5\u3002") || `HTTP ${response.status}`);
    error.status = response.status;
    error.body = body;
    throw error;
  }
  return body;
}

function isApiNotFound(error) {
  const message = String(error?.message || error || "").toLowerCase();
  return Number(error?.status) === 404 || /(^|\b)(404|not found)(\b|$)|找不到|未找到/.test(message);
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
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

function setLiveRecTab(tab) {
  const targetTab = tab || "rooms";
  state.liveRecTab = targetTab;
  document.querySelectorAll(".live-rec-tab").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.liveRecTab === targetTab);
  });
  document.querySelectorAll(".live-rec-page").forEach((page) => {
    page.classList.toggle("is-active", page.id === `live-rec-${targetTab}`);
  });
}

function bindLiveRecTabs() {
  document.querySelectorAll(".live-rec-tab").forEach((button) => {
    button.addEventListener("click", () => setLiveRecTab(button.dataset.liveRecTab));
  });
}

function bindLiveRoomFilters() {
  $("live-room-search")?.addEventListener("input", (event) => {
    state.liveRoomSearch = event.target.value || "";
    renderLiveRooms();
  });
  $("live-platform-filter")?.addEventListener("change", (event) => {
    state.liveRoomPlatform = event.target.value || "all";
    renderLiveRooms();
  });
}

function bindActions() {
  document.body.addEventListener("click", async (event) => {
    const target = event.target.closest("[data-action]");
    if (!target) return;
    if (target.disabled || target.getAttribute("aria-disabled") === "true") {
      event.preventDefault();
      return;
    }
    const action = target.dataset.action;

    try {
      if (action === "add-path") addPath(target.dataset.input, target.dataset.target);
      if (action === "pick-videos") await pickVideos(target.dataset.target, target);
      if (action === "pick-file") await pickFile(target.dataset.target, target.dataset.kind || "file");
      if (action === "pick-directory") await pickDirectory(target.dataset.target);
      if (action === "open-path") await openPath(target.dataset.target);
      if (action === "open-task-output") await openPathValue(target.dataset.path);
      if (action === "stop-scope") await stopScope(target.dataset.scope || state.page);
      if (action === "clear-video-list") clearVideoList(target.dataset.target);
      if (action === "remove-video") removeVideoPath(target.dataset.target, Number(target.dataset.index));
      if (action === "move-video") moveVideoPath(target.dataset.target, Number(target.dataset.index), Number(target.dataset.direction));
      if (action === "mix-save-group") saveCurrentMixGroup();
      if (action === "mix-new-group") newMixGroup();
      if (action === "mix-delete-group") deleteActiveMixGroup();
      if (action === "mix-select-group") selectMixGroup(Number(target.dataset.index));
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
      if (action === "refresh-ai-feedback") await loadAiFeedbackSamples(true);
      if (action === "export-ai-feedback") exportAiFeedback();
      if (action === "import-ai-feedback") await importAiFeedback();
      if (action === "delete-ai-feedback-sample") await deleteAiFeedbackSample(target);
      if (action === "clear-ai-feedback") await clearAiFeedback();
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
      if (action === "preview-video-split") await previewVideoSplit();
      if (action === "feature-submit") await submitFeature(target.dataset.feature);
      if (action === "reset-dedup") resetDedupDefaults();
      if (action === "toggle-dedup-detail") toggleDedupDetail(target.dataset.prefix);
      if (action === "add-live-room") addLiveRoom();
      if (action === "live-switch-tab") setLiveRecTab(target.dataset.tab || "rooms");
      if (action === "live-status-filter") setLiveRoomFilter(target.dataset.status || "all");
      if (action === "refresh-live-rooms") {
        renderLiveRooms();
        await refreshTasks();
      }
      if (action === "clear-live-rooms") clearLiveRooms();
      if (action === "record-live-room") await startLiveRoom(Number(target.dataset.index));
      if (action === "live-open-room-dir") await openLiveRoomDirectory(Number(target.dataset.index));
      if (action === "live-open-product-dir") await openLiveProductDirectory(Number(target.dataset.index));
      if (action === "live-stop-room") await stopLiveRoom(Number(target.dataset.index));
      if (action === "live-preview-room") await previewLiveRoom(Number(target.dataset.index));
      if (action === "live-detail-room") showLiveRoomDetail(Number(target.dataset.index));
      if (action === "close-live-detail") closeLiveDetailModal();
      if (action === "remove-live-room") removeLiveRoom(Number(target.dataset.index));
      if (action === "fill-live-room") fillLiveRoom(Number(target.dataset.index));
      if (action === "toggle-secret") toggleSecret(target);
      if (action === "activate-license") await activateLicense();
      if (action === "unbind-device") await unbindDevice();
      if (action === "check-update") await checkUpdate();
      if (action === "apply-update") await applyUpdate();
      if (action === "toggle-update-card") toggleUpdateCard();
      if (action === "close-update-card") closeUpdateCard();
      if (action === "feedback") feedback();
      if (action === "apply-user-data-dir") await applyUserDataDir();
    } catch (error) {
      toast(error.message || String(error), "error");
    }
  });

  document.body.addEventListener("change", (event) => {
    const target = event.target.closest("[data-live-naming-mode]");
    if (target) updateLiveRoomNamingMode(Number(target.dataset.index), target.value);
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
  $("vs-split-mode")?.addEventListener("change", syncVideoSplitMode);

  bindVideoDropzones();
  bindFileDropTargets();
  injectDiagnosticButtons();
  ["video-paths", "mix-video-paths", "scan-video-paths", "ps-video-paths", "vs-video-paths", "dedup-video-paths"].forEach(renderVideoList);
  renderMixGroups();
  document.querySelectorAll(".path-box").forEach((box) => {
    box.addEventListener("input", () => renderVideoList(box.id));
  });
}

function injectDiagnosticButtons() {
  document.querySelectorAll(".log-panel .panel-header").forEach((header) => {
    const panel = header.closest(".log-panel");
    const logView = panel?.querySelector(".log-view");
    const scope = logView?.id?.replace(/^log-/, "") || "";
    const title = header.querySelector("h2");
    if (title && title.textContent.trim() === "运行日志") title.textContent = "运行摘要";
    if (title && !header.querySelector(".run-summary-heading")) {
      const heading = document.createElement("div");
      heading.className = "run-summary-heading";
      title.insertAdjacentElement("beforebegin", heading);
      heading.appendChild(title);

      const batchSummary = document.createElement("span");
      batchSummary.className = "run-summary-header-batch";
      batchSummary.dataset.runSummaryBatch = scope;
      batchSummary.hidden = true;
      batchSummary.innerHTML = `
        <span>批量剪辑</span>
        <strong data-run-summary-batch-ratio>已完成 0/0</strong>
      `;
      heading.appendChild(batchSummary);
    }
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

function logPanelForScope(scope) {
  return $(`log-${scope}`)?.closest(".log-panel") || null;
}

function bindAiPresetControls() {
  refreshAiPresetOptions();
  document.querySelectorAll("[data-ai-preset]").forEach((select) => {
    select.addEventListener("change", () => {
      applyAiPreset(select.dataset.aiPreset, select.value);
    });
  });

  ["sc", "mix"].forEach((prefix) => {
    [`${prefix}-category`, `${prefix}-focus`, `${prefix}-goal`, `${prefix}-hook-style`, `${prefix}-ending-style`, `${prefix}-strictness`].forEach((id) => {
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
  setSelectIfPresent(`${prefix}-category`, preset.category);
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
    category: $(`${prefix}-category`)?.value || "自动检测",
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

function refreshPipPool(prefix) {
  const folder = $(`${prefix}-pip-folder`)?.value.trim() || "";
  if (!folder) {
    delete state.pipPoolByPrefix[prefix];
    return;
  }
  inspectPipPool(prefix);
}

function refreshFeaturePreferenceUi() {
  document.querySelectorAll("[data-collapsible-panel]").forEach((panel) => updatePanelSummary(panel));
  ["sc", "mix", "dedup"].forEach((prefix) => refreshPipPool(prefix));
  ["sc", "mix"].forEach((prefix) => refreshDedupCustomVisibility(prefix));
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
      if (!input || input.disabled) return;
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
    closeLiveDetailModal();
    closeUpdateCard();
  });
}

function collapsiblePanelStorageKey(panel) {
  const id = panel?.dataset?.collapsiblePanel;
  if (!id) return "";
  if (panel.dataset.collapseGroup) return `lc:panel:${id}:compact-v1`;
  return `lc:panel:${id}`;
}

function setCollapsiblePanelCollapsed(panel, collapsed, persist = true) {
  if (!panel) return;
  const key = collapsiblePanelStorageKey(panel);
  panel.classList.toggle("is-collapsed", collapsed);
  if (persist && key) localStorage.setItem(key, collapsed ? "closed" : "open");
  updatePanelSummary(panel);
}

function collapseSiblingPanels(panel) {
  const group = panel?.dataset?.collapseGroup;
  if (!group) return;
  document.querySelectorAll(`[data-collapse-group="${group}"]`).forEach((item) => {
    if (item !== panel) setCollapsiblePanelCollapsed(item, true);
  });
}

function setupCollapsiblePanels() {
  document.querySelectorAll("[data-collapsible-panel]").forEach((panel) => {
    const id = panel.dataset.collapsiblePanel;
    const header = panel.querySelector(".panel-header");
    if (!id || !header || header.querySelector(".collapse-toggle")) return;
    const saved = localStorage.getItem(collapsiblePanelStorageKey(panel));
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
      const shouldOpen = panel.classList.contains("is-collapsed");
      if (shouldOpen) collapseSiblingPanels(panel);
      setCollapsiblePanelCollapsed(panel, !shouldOpen);
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
    progress.className = "run-summary log-progress is-idle";
    progress.dataset.logProgress = scope;
    progress.innerHTML = `
      <div class="run-summary-grid">
        <div class="run-summary-progress-block">
          <div class="run-summary-ring" data-run-summary-ring>
            <span data-run-summary-ratio>0%</span>
            <small>总进度</small>
          </div>
          <div class="run-summary-main">
            <div class="log-progress-meta">
              <span class="log-progress-title">当前步骤</span>
              <strong class="log-progress-label">等待任务</strong>
              <span class="log-progress-percent">0%</span>
            </div>
            <div class="log-progress-track"><span class="log-progress-fill"></span></div>
          </div>
        </div>
        <div class="run-summary-section">
          <div class="run-summary-section-title">最近成片</div>
          <div class="run-summary-list" data-run-summary-recent>
            <span class="run-summary-muted">暂无成片记录</span>
          </div>
        </div>
        <div class="run-summary-section run-summary-issue-section">
          <div class="run-summary-section-title">异常原因</div>
          <div class="run-summary-issue is-empty" data-run-summary-issue>
            <span>暂无异常</span>
          </div>
        </div>
      </div>
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
    if (latest && (!current?.taskId || current.status === "idle")) {
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

function batchNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function formatBatchProgress({ total, done = 0, succeeded = null, current = 0, failed = 0, label = "", status = "" } = {}) {
  const safeTotal = Math.max(0, Math.floor(batchNumber(total)));
  if (safeTotal <= 1) return null;
  const safeFailed = Math.max(0, Math.floor(batchNumber(failed)));
  const safeDone = Math.max(0, Math.min(safeTotal, Math.floor(batchNumber(done))));
  const parsedSucceeded = succeeded == null ? Number.NaN : Number(succeeded);
  const fallbackSucceeded = status === "completed" ? safeDone : Math.max(0, safeDone - safeFailed);
  const safeSucceeded = Number.isFinite(parsedSucceeded)
    ? Math.max(0, Math.min(safeTotal, Math.floor(parsedSucceeded)))
    : fallbackSucceeded;
  const safeCurrent = Math.max(0, Math.min(safeTotal, Math.floor(batchNumber(current))));
  const cleanLabel = String(label || "").trim();
  const finishedWithFailures = status === "completed" && safeFailed > 0;
  const mainText = finishedWithFailures ? `成功 ${safeSucceeded}/${safeTotal}` : `已完成 ${safeDone}/${safeTotal}`;
  const parts = [mainText];
  if (!["completed", "failed", "cancelled"].includes(status) && safeCurrent > 0 && safeDone < safeTotal) {
    parts.push(`正在第 ${safeCurrent} 个`);
  }
  if (safeFailed > 0) parts.push(`失败 ${safeFailed}`);
  const titleParts = [`共 ${safeTotal} 个`, `已完成 ${safeDone} 个`];
  if (safeCurrent > 0 && safeDone < safeTotal) titleParts.push(`当前第 ${safeCurrent} 个`);
  if (safeFailed > 0) titleParts.push(`失败 ${safeFailed} 个`);
  if (cleanLabel) titleParts.push(cleanLabel);
  return {
    total: safeTotal,
    done: safeDone,
    succeeded: safeSucceeded,
    current: safeCurrent,
    failed: safeFailed,
    status,
    label: cleanLabel,
    text: parts.join(" · "),
    shortText: finishedWithFailures ? `成功 ${safeSucceeded}/${safeTotal}` : `已完成 ${safeDone}/${safeTotal}`,
    title: titleParts.join(" · "),
  };
}

function hasBatchProgress(batch) {
  return Number(batch?.total || 0) > 1;
}

function batchHeaderText(batch) {
  const total = Math.max(1, Math.floor(batchNumber(batch?.total, 1)));
  const done = Math.max(0, Math.min(total, Math.floor(batchNumber(batch?.done))));
  const succeeded = Math.max(0, Math.min(total, Math.floor(batchNumber(batch?.succeeded, done))));
  const current = Math.max(0, Math.min(total, Math.floor(batchNumber(batch?.current))));
  const failed = Math.max(0, Math.floor(batchNumber(batch?.failed)));
  const status = String(batch?.status || "");
  if (!["completed", "failed", "cancelled"].includes(status) && current > 0 && done < total) {
    return `第 ${current}/${total} 个`;
  }
  if (status === "completed" && failed > 0) return `成功 ${succeeded}/${total}`;
  return `已完成 ${done}/${total}`;
}

function setLegacyBatchProgress(scope, values = {}) {
  const batch = formatBatchProgress(values);
  if (batch) state.legacyBatchProgress[scope] = batch;
  else delete state.legacyBatchProgress[scope];
  const previous = state.progressByScope[scope] || {};
  updateLogProgressBar(scope, {
    ...previous,
    label: values.labelText || previous.label || "批量处理中",
    percent: Number.isFinite(Number(values.percent)) ? Number(values.percent) : (previous.percent || 0),
    status: values.status || previous.status || "running",
    batch,
  });
  return batch;
}

function batchProgressFromTask(task) {
  if (!task) return null;
  const structured = formatBatchProgress({
    total: task.batch_total,
    done: task.batch_done,
    succeeded: task.batch_succeeded,
    current: task.batch_current,
    failed: task.batch_failed,
    label: task.batch_label,
    status: task.status || "",
  });
  const inferred = batchProgressFromText([task.title, task.message, task.error].filter(Boolean).join(" "), task.status || "");
  if (structured && inferred && structured.total === inferred.total) {
    return formatBatchProgress({
      total: structured.total,
      done: Math.max(batchNumber(structured.done), batchNumber(inferred.done)),
      succeeded: Math.max(batchNumber(structured.succeeded), batchNumber(inferred.succeeded)),
      current: batchNumber(inferred.current) || batchNumber(structured.current),
      failed: Math.max(batchNumber(structured.failed), batchNumber(inferred.failed)),
      label: inferred.label || structured.label,
      status: task.status || "",
    }) || structured;
  }
  if (structured) return structured;
  return inferred;
}

function batchProgressFromOutputHistory(scope) {
  const candidates = (state.outputHistory || [])
    .filter((item) => {
      if (String(item.scope || "") !== String(scope || "")) return false;
      return Math.max(batchNumber(item.batch_total), batchNumber(item.total)) > 1;
    })
    .sort((a, b) => Number(b.created_at || 0) - Number(a.created_at || 0));
  const latest = candidates[0];
  if (!latest) return null;

  const total = Math.max(1, Math.floor(batchNumber(latest.batch_total, latest.total)));
  const taskId = String(latest.task_id || "");
  const createdAt = Number(latest.created_at || 0);
  const related = candidates.filter((item) => {
    if (taskId) return String(item.task_id || "") === taskId;
    return Number(item.created_at || 0) === createdAt && Number(item.total || 0) === Number(latest.total || 0);
  });
  const recordedDone = Math.max(...related.map((item) => batchNumber(item.batch_done)), 0);
  const indexedDone = new Set(related.map((item) => Number(item.index || 0)).filter((value) => value > 0)).size;
  const done = Math.max(recordedDone, indexedDone);
  const failed = Math.max(...related.map((item) => batchNumber(item.batch_failed)), 0);
  const succeeded = Math.max(...related.map((item) => batchNumber(item.batch_succeeded)), Math.max(0, done - failed));
  return formatBatchProgress({ total, done, succeeded, failed, status: "completed" });
}

function progressFromTask(task) {
  const scope = task.scope || "settings";
  const status = task.status || "queued";
  const text = [task.title, task.message, task.error].filter(Boolean).join(" ");
  const inferred = inferProgressStage(text);
  const batch = batchProgressFromTask(task);
  const explicitProgress = Number(task.progress);
  const hasExplicitProgress = Number.isFinite(explicitProgress);
  const statusLabels = {
    queued: "排队中",
    running: task.message || inferred.label || "运行中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已停止",
  };

  if (status === "completed") return { taskId: task.id, label: statusLabels.completed, percent: 100, status, source: "task", batch };
  if (status === "failed") return { taskId: task.id, label: task.error || statusLabels.failed, percent: 100, status, source: "task", batch };
  if (status === "cancelled") return { taskId: task.id, label: statusLabels.cancelled, percent: 100, status, source: "task", batch };
  if (status === "queued") {
    return { taskId: task.id, label: task.message || statusLabels.queued, percent: hasExplicitProgress ? explicitProgress : 6, status, source: "task", batch };
  }

  const previous = state.progressByScope[scope];
  const previousPercent = previous?.taskId === task.id ? previous.percent || 0 : 0;
  return {
    taskId: task.id,
    label: statusLabels.running,
    percent: Math.max(previousPercent, hasExplicitProgress ? explicitProgress : inferred.percent || 10),
    status,
    source: "task",
    batch,
  };
}

function batchProgressFromText(text, status = "") {
  const value = String(text || "").trim();
  const success = value.match(/^(?:智能成片(?:批量)?完成[：:]\s*)?成功\s*(\d+)\s*\/\s*(\d+)(?:\s*个)?[。.]?$/);
  if (success) {
    const succeeded = Number(success[1]);
    const total = Number(success[2]);
    return formatBatchProgress({
      done: status === "completed" ? total : succeeded,
      succeeded,
      failed: Math.max(0, total - succeeded),
      total,
      status: status || "completed",
    });
  }
  const match = value.match(/^(跳过失败|完成扫描|完成导出|快速分割|处理|完成|扫描|导出)\s*(\d+)\s*\/\s*(\d+)(?:\s*[:：]\s*([^。；\n]+))?$/)
    || value.match(/^\[(\d+)\s*\/\s*(\d+)\]\s*(?:开始处理|当前素材|智能成片|批量)/);
  if (!match) return null;
  const hasAction = match.length > 3;
  const action = hasAction ? match[1] : "处理";
  const current = Number(hasAction ? match[2] : match[1]);
  const total = Number(hasAction ? match[3] : match[2]);
  if (!Number.isFinite(current) || !Number.isFinite(total) || total <= 1) return null;
  const safeCurrent = Math.max(1, Math.min(total, current));
  const isCurrentStep = ["处理", "扫描", "导出", "快速分割"].includes(action);
  return formatBatchProgress({
    total,
    done: isCurrentStep ? safeCurrent - 1 : safeCurrent,
    current: isCurrentStep ? safeCurrent : 0,
    failed: action === "跳过失败" ? 1 : 0,
    label: hasAction ? match[4] || "" : "",
    status,
  });
}

function taskStatusLabel(status) {
  return {
    queued: "排队中",
    running: "运行中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已停止",
    idle: "待处理",
  }[status] || status || "待处理";
}

function taskPercent(task) {
  const value = Number(task?.progress);
  return Math.max(0, Math.min(100, Number.isFinite(value) ? Math.round(value) : 0));
}

function clampProgressPercent(value, fallback = 0) {
  const number = Number(value);
  const safeFallback = Number(fallback);
  const next = Number.isFinite(number) ? number : (Number.isFinite(safeFallback) ? safeFallback : 0);
  return Math.max(0, Math.min(100, Math.round(next)));
}

function taskSummaryText(task) {
  return [task?.title, task?.message, task?.error].filter(Boolean).join(" ");
}

function taskHasOutput(task) {
  return Boolean(
    task?.output ||
    task?.output_dir ||
    (Array.isArray(task?.outputs) && task.outputs.some(Boolean))
  );
}

function isAiSelectionPreviewTask(task) {
  const text = taskSummaryText(task);
  return /AI\s*选片预览|AI选片预览|混剪\s*AI\s*选片预览|混剪AI选片预览/.test(text);
}

function isPreviewOutputTask(task) {
  return /预览成片|预览混剪/.test(taskSummaryText(task));
}

function totalProgressFromSummary(progress, task, batch) {
  const progressStatus = progress?.status || "idle";
  const status = progressStatus !== "idle" ? progressStatus : (task?.status || batch?.status || "idle");
  const stepPercent = clampProgressPercent(progress?.percent, taskPercent(task));
  if (batch?.total > 1) {
    const total = Math.max(1, Math.floor(batchNumber(batch.total, 1)));
    const done = Math.max(0, Math.min(total, Math.floor(batchNumber(batch.done))));
    const completedPercent = Math.round((done / total) * 100);
    const percent = ["failed", "cancelled"].includes(status)
      ? completedPercent
      : (status === "completed" ? 100 : Math.max(completedPercent, stepPercent));
    return {
      percent,
      text: `${percent}%`,
      status,
    };
  }
  if (!task) {
    return { percent: stepPercent, text: `${stepPercent}%`, status };
  }
  if (status === "failed") {
    return { percent: Math.min(99, stepPercent), text: "失败", status };
  }
  if (status === "cancelled") {
    return { percent: Math.min(99, stepPercent), text: "停止", status };
  }
  if (isPreviewOutputTask(task)) {
    const percent = status === "completed" ? 100 : Math.min(99, 50 + Math.round(stepPercent * 0.5));
    return { percent, text: `${percent}%`, status };
  }
  if (isAiSelectionPreviewTask(task)) {
    const percent = status === "completed" ? 50 : Math.min(50, Math.round(stepPercent * 0.5));
    return { percent, text: `${percent}%`, status };
  }
  if (status === "completed" || taskHasOutput(task)) {
    const percent = status === "completed" ? 100 : stepPercent;
    return { percent, text: `${percent}%`, status };
  }
  return { percent: stepPercent, text: `${stepPercent}%`, status };
}

function newestScopedTasks(tasks, scope) {
  return scopedProgressTasks(tasks, scope).sort((a, b) => {
    const aActive = ["queued", "running"].includes(a.status) ? 1 : 0;
    const bActive = ["queued", "running"].includes(b.status) ? 1 : 0;
    if (aActive !== bActive) return bActive - aActive;
    return taskTime(b) - taskTime(a);
  });
}

function issueSuggestion(message) {
  const text = String(message || "");
  if (/素材|视频|至少|添加|不存在|路径/.test(text)) return "请补充素材或检查文件路径。";
  if (/402|余额不足|quota|balance|充值/i.test(text)) return "请到模型平台充值，或在设置中更换可用 API Key。";
  if (/API|Key|模型|DeepSeek|OpenAI|连接|网络|代理/.test(text)) return "请到设置里检查 AI 配置和网络。";
  if (/权限|目录|写入|保存|输出/.test(text)) return "请换一个可写的输出目录。";
  if (/ffmpeg|编码|转码|裁剪|合成|导出/i.test(text)) return "请检查源视频是否可播放，必要时换稳定转码。";
  return "打开高级诊断查看详细原因。";
}

function issueForScope(scope, scopedTasks) {
  const failed = newestTask(scopedTasks.filter((task) => task.status === "failed"));
  if (failed) {
    const message = failed.error || failed.message || "任务处理失败。";
    return {
      title: failed.title || "任务失败",
      message,
      suggestion: issueSuggestion(message),
      tone: "error",
    };
  }

  const active = newestTask(scopedTasks.filter((task) => ["queued", "running"].includes(task.status)));
  const issue = state.lastIssuesByScope[scope];
  if (active && issue) {
    return {
      title: issue.level === "warning" ? "提示" : "异常",
      message: issue.message,
      suggestion: issueSuggestion(issue.message),
      tone: issue.level === "warning" ? "warning" : "error",
    };
  }
  return null;
}

function fileNameFromPath(path) {
  const text = String(path || "").trim().replace(/[\\/]+$/, "");
  if (!text) return "";
  return text.split(/[\\/]/).filter(Boolean).pop() || text;
}

function outputPathKey(path) {
  return String(path || "").trim().replace(/[\\/]+/g, "\\").toLowerCase();
}

function completedOutputsFromTasks(tasks) {
  const rows = [];
  for (const task of tasks || []) {
    if (task.status !== "completed") continue;
    const outputs = Array.isArray(task.outputs) ? task.outputs.filter(Boolean) : [];
    if (!outputs.length && task.output) outputs.push(task.output);
    if (!outputs.length && task.output_dir) outputs.push(task.output_dir);
    outputs.slice(0, 4).forEach((path, index) => {
      const name = fileNameFromPath(path) || task.title || "输出文件";
      const total = outputs.length || Number(task.result_count || 0) || 1;
      rows.push({
        path,
        name,
        title: task.title || "成片",
        meta: total > 1 ? `${index + 1}/${total}` : "已完成",
        time: taskTime(task),
      });
    });
  }
  return rows.sort((a, b) => b.time - a.time);
}

function outputRowsForScope(scope, tasks) {
  const rows = [];
  for (const item of state.outputHistory || []) {
    const path = String(item.path || "").trim();
    if (!path) continue;
    const itemScope = String(item.scope || "");
    if (itemScope && scope && itemScope !== scope) continue;
    const total = Number(item.total || item.result_count || 0);
    const index = Number(item.index || 0);
    rows.push({
      path,
      name: item.name || fileNameFromPath(path) || "输出文件",
      title: item.title || "成片",
      meta: total > 1 && index > 0 ? `${index}/${total}` : "已完成",
      time: Number(item.created_at || item.finished_at || 0),
    });
  }
  rows.push(...completedOutputsFromTasks(tasks));
  const seen = new Set();
  return rows
    .filter((item) => {
      const key = outputPathKey(item.path);
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .sort((a, b) => Number(b.time || 0) - Number(a.time || 0));
}

function renderRecentOutputRows(scope, tasks) {
  const outputs = outputRowsForScope(scope, tasks).slice(0, 2);
  if (!outputs.length) return '<span class="run-summary-muted">暂无成片记录</span>';
  return outputs.map((item) => `
    <div class="run-output-row">
      <span class="run-task-dot"></span>
      <div class="run-output-main">
        <strong title="${escapeHtml(item.path)}">${escapeHtml(item.name)}</strong>
        <span>${escapeHtml(item.title)} · ${escapeHtml(item.meta)}</span>
      </div>
      <button type="button" class="run-output-button" data-action="open-task-output" data-path="${escapeHtml(item.path)}" title="${escapeHtml(item.path)}">打开文件夹</button>
    </div>
  `).join("");
}

function renderRunSummary(scope) {
  const el = document.querySelector(`[data-log-progress="${scope}"]`);
  if (!el) return;
  const progress = state.progressByScope[scope] || { label: "等待任务", percent: 0, status: "idle" };
  const scoped = newestScopedTasks(state.latestTasks || [], scope);
  const focusTask = newestTask(scoped.filter((task) => ["queued", "running"].includes(task.status))) || newestTask(scoped);
  const taskBatch = batchProgressFromTask(focusTask);
  const historyBatch = !focusTask ? batchProgressFromOutputHistory(scope) : null;
  const batch = progress.batch || taskBatch || historyBatch;
  const totalProgress = totalProgressFromSummary(progress, focusTask, batch);

  el.style.setProperty("--run-summary-percent", `${totalProgress.percent}%`);
  const ring = el.querySelector("[data-run-summary-ring]");
  const ratioEl = el.querySelector("[data-run-summary-ratio]");
  const batchHeader = document.querySelector(`[data-run-summary-batch="${scope}"]`);
  const recentEl = el.querySelector("[data-run-summary-recent]");
  const issueEl = el.querySelector("[data-run-summary-issue]");

  if (historyBatch && !progress.batch) {
    const labelEl = el.querySelector(".log-progress-label");
    const percentEl = el.querySelector(".log-progress-percent");
    const fill = el.querySelector(".log-progress-fill");
    el.className = "log-progress is-completed";
    if (labelEl) labelEl.textContent = "已完成";
    if (percentEl) percentEl.textContent = "100%";
    if (fill) fill.style.width = "100%";
  }
  if (ring) ring.className = `run-summary-ring is-${totalProgress.status || "idle"}`;
  if (ratioEl) ratioEl.textContent = totalProgress.text;
  if (batchHeader) {
    const showBatch = hasBatchProgress(batch);
    batchHeader.hidden = !showBatch;
    const batchRatio = batchHeader.querySelector("[data-run-summary-batch-ratio]");
    if (batchRatio && showBatch) {
      batchRatio.textContent = batchHeaderText(batch);
      batchHeader.title = batch.title || batch.text || batchHeaderText(batch);
    } else {
      batchHeader.title = "";
    }
  }
  if (recentEl) recentEl.innerHTML = renderRecentOutputRows(scope, scoped);

  const issue = issueForScope(scope, scoped);
  if (issueEl) {
    issueEl.className = `run-summary-issue ${issue ? `is-${issue.tone}` : "is-empty"}`;
    issueEl.innerHTML = issue
      ? `
        <strong>${escapeHtml(issue.title)}</strong>
        <span>${escapeHtml(issue.message)}</span>
        <em>${escapeHtml(issue.suggestion)}</em>
      `
      : "<span>暂无异常</span>";
  }
}

function renderRunSummaries(tasks = state.latestTasks || []) {
  state.latestTasks = tasks || [];
  progressScopes.forEach((scope) => renderRunSummary(scope));
}

function updateProgressFromLog(scope, item = {}) {
  const level = String(item.level || "info").toLowerCase();
  const text = [item.message, item.raw].filter(Boolean).join(" ");
  if (!scope || !text) return;
  const activeTask = newestTask(scopedProgressTasks(state.latestTasks || [], scope)
    .filter((task) => ["queued", "running"].includes(task.status)));

  if (level === "error") {
    if (activeTask) {
      updateLogProgressBar(scope, {
        ...progressFromTask(activeTask),
        label: "当前素材失败，批量继续",
        status: "running",
      });
      return;
    }
    updateLogProgressBar(scope, { label: "处理失败", percent: 100, status: "failed", batch: batchProgressFromText(text, "failed") });
    return;
  }
  if (level === "success" || /任务完成|成片完成|混剪完成|预览完成|处理完成|成功|已生成/.test(text)) {
    if (activeTask) {
      updateLogProgressBar(scope, progressFromTask(activeTask));
      return;
    }
    updateLogProgressBar(scope, { label: "已完成", percent: 100, status: "completed", batch: batchProgressFromText(text, "completed") });
    return;
  }

  const inferred = inferProgressStage(text);
  const batch = batchProgressFromText(text, "running");
  if (!inferred.label && !batch) return;
  const previous = state.progressByScope[scope] || {};
  if (previous.source === "task" && previous.taskId && previous.status === "running") return;
  updateLogProgressBar(scope, {
    label: inferred.label || previous.label || "运行中",
    percent: Math.max(previous.percent || 0, inferred.percent),
    status: "running",
    taskId: previous.taskId,
    batch,
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
  const previous = state.progressByScope[scope] || {};
  const legacyBatch = state.legacyBatchProgress[scope];
  const incomingBatch = hasBatchProgress(progress?.batch) ? progress.batch : null;
  const preservedBatch = hasBatchProgress(previous?.batch) ? previous.batch : null;
  const fallbackBatch = hasBatchProgress(legacyBatch) ? legacyBatch : null;
  const sameTask = Boolean(progress?.taskId && previous?.taskId && progress.taskId === previous.taskId);
  const effectiveProgress = {
    ...progress,
    batch: incomingBatch || fallbackBatch || (sameTask ? preservedBatch : null),
  };
  const percent = Math.max(0, Math.min(100, Math.round(Number(effectiveProgress.percent) || 0)));
  const status = effectiveProgress.status || "idle";
  const label = effectiveProgress.label || "等待任务";
  state.progressByScope[scope] = { ...effectiveProgress, percent, label, status };

  el.className = `log-progress is-${status}`;
  const labelEl = el.querySelector(".log-progress-label");
  const percentEl = el.querySelector(".log-progress-percent");
  const fill = el.querySelector(".log-progress-fill");
  if (labelEl) labelEl.textContent = label;
  if (percentEl) percentEl.textContent = `${percent}%`;
  if (fill) fill.style.width = `${percent}%`;
  renderRunSummary(scope);
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
    state.runtime = data;
    $("app-version").textContent = `v${data.version}`;
    $("runtime-user-data").value = data.user_data_dir || "";
    if ($("user-data-dir")) $("user-data-dir").value = data.user_data_dir || "";
    $("runtime-repo-root").value = data.repo_root || "";
    renderUpdateState();
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
      element.checked = normalizeBooleanSetting(value, key === "style_profile_enabled");
    } else {
      element.value = value ?? "";
    }
  });
  applyUiFontSize(data.ui_font_size || 14);
  syncSubtitleFontSize();
  applyTheme(data.ui_theme || "system");
  applyPreferenceWeights(data.preference_weights || {});
  applyAiRules(data.ai_rules || {});
  await loadAiFeedbackSamples(false);
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
  data.style_profile_strength = normalizeStyleProfileStrength(data.style_profile_strength);
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
  await loadSettings(false);
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

function formatFileSize(bytes) {
  const value = Number(bytes || 0);
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)}MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)}KB`;
  return `${value}B`;
}

async function loadAiFeedbackStats(showToast = false) {
  const result = await api("/api/ai-feedback/stats");
  renderAiFeedbackStats(result);
  if (showToast) toast("剪辑风格画像统计已刷新", "success");
}

function renderAiFeedbackStats(result = {}) {
  const count = $("ai-feedback-count");
  const size = $("ai-feedback-size");
  const path = $("ai-feedback-path");
  if (count) count.textContent = String(result.record_count || 0);
  if (size) size.textContent = formatFileSize(result.size || 0);
  if (path) path.textContent = `保存位置：${result.path || "%APPDATA%\\LiveClipper\\ai_feedback"}`;
}

function renderPreferenceSignalList(items = [], kind = "positive") {
  if (!items.length) return '<p class="panel-note">暂无稳定方向。</p>';
  return items.map((item) => {
    const count = kind === "positive" ? item.positive_count : item.negative_count;
    const opposite = kind === "positive" ? item.negative_count : item.positive_count;
    const examples = kind === "positive" ? item.positive_examples : item.negative_examples;
    const exampleText = Array.isArray(examples) && examples.length
      ? `<span>${escapeHtml(examples[0])}</span>`
      : "";
    return `
      <div class="preference-signal ${kind}">
        <strong>${escapeHtml(item.label || "")}</strong>
        <em>${Number(count || 0)} 次 · ${escapeHtml(item.confidence || "观察中")}${Number(opposite || 0) ? ` · 反向 ${Number(opposite || 0)} 次` : ""}</em>
        ${exampleText}
      </div>
    `;
  }).join("");
}

function styleProfileStatusFromSamples(sampleCount = 0) {
  const count = Number(sampleCount || 0);
  if (count <= 0) return { status: "未开始", impact: "只读" };
  if (count <= 2) return { status: "观察中", impact: "只读" };
  if (count <= 9) return { status: "初步成型", impact: "轻度" };
  return { status: "稳定画像", impact: "标准" };
}

function normalizeBooleanSetting(value, fallback = false) {
  if (value === undefined || value === null || value === "") return Boolean(fallback);
  if (typeof value === "string") {
    const text = value.trim().toLowerCase();
    if (["0", "false", "off", "no", "disabled", "关闭", "关", "否"].includes(text)) return false;
    if (["1", "true", "on", "yes", "enabled", "开启", "开", "是"].includes(text)) return true;
  }
  return Boolean(value);
}

function normalizeStyleProfileStrength(value = "auto") {
  const text = String(value || "auto").trim().toLowerCase();
  if (["off", "关闭", "关", "false"].includes(text)) return "off";
  if (["light", "轻度"].includes(text)) return "light";
  if (["standard", "标准"].includes(text)) return "standard";
  if (["strong", "强", "强力"].includes(text)) return "strong";
  return "auto";
}

function buildStyleProfileFromSummary(summary = {}) {
  const positive = Array.isArray(summary.positive) ? summary.positive : [];
  const negative = Array.isArray(summary.negative) ? summary.negative : [];
  const sampleCount = Number(summary.sample_count || 0);
  const state = styleProfileStatusFromSamples(sampleCount);
  const selling = positive.slice(0, 5).map((item) => ({
    label: item.label || "",
    count: Number(item.positive_count || 0),
    confidence: item.confidence || "观察中",
    examples: item.positive_examples || [],
  }));
  const avoid = negative.slice(0, 5).map((item) => ({
    label: item.label || "",
    count: Number(item.negative_count || 0),
    confidence: item.confidence || "观察中",
    examples: item.negative_examples || [],
  }));
  return {
    name: "剪辑风格画像",
    status: state.status,
    impact: state.impact,
    configured_strength: "auto",
    learned_records: 0,
    sample_count: sampleCount,
    selling_preferences: selling,
    avoid_preferences: avoid,
    hook_preferences: selling.length ? selling.slice(0, 3) : [{ label: "直接利益点开头", count: 0, confidence: "观察中" }],
    ending_preferences: [{ label: "自然总结", count: 0, confidence: "观察中" }],
    metrics: {
      selling_density: selling.length ? "中" : "观察中",
      rhythm: "观察中",
      context_length: "观察中",
      emotion_strength: "观察中",
      cta_strength: "观察中",
    },
    summary: Array.isArray(summary.brief) ? summary.brief : [],
    ai_hint: "",
    selection_enabled: true,
  };
}

function renderStyleProfilePills(items = [], emptyText = "继续积累样本") {
  const values = Array.isArray(items) ? items.filter((item) => item && item.label) : [];
  if (!values.length) return `<span class="style-profile-empty">${escapeHtml(emptyText)}</span>`;
  return values.slice(0, 5).map((item) => `
    <span class="style-profile-pill" title="${escapeHtml((item.examples || [])[0] || item.label || "")}">
      ${escapeHtml(item.label || "")}
      ${Number(item.count || 0) ? `<em>${Number(item.count || 0)}</em>` : ""}
    </span>
  `).join("");
}

function renderStyleProfile(profile = {}) {
  const metrics = profile.metrics || {};
  const summary = Array.isArray(profile.summary) ? profile.summary : [];
  const latest = Number(profile.latest_at || 0);
  const latestText = latest ? new Date(latest * 1000).toLocaleDateString() : "暂无";
  return `
    <div class="style-profile-card">
      <div class="style-profile-head">
        <div>
          <strong>${escapeHtml(profile.name || "剪辑风格画像")}</strong>
          <span>学习状态：${escapeHtml(profile.status || "观察中")} · 当前影响：${escapeHtml(profile.impact || "只读")} · 设置：${escapeHtml(profile.configured_strength || "auto")}</span>
        </div>
        <div class="style-profile-stats">
          <span>成片调整 ${Number(profile.learned_records || 0)}</span>
          <span>样本 ${Number(profile.sample_count || 0)}</span>
          <span>更新 ${escapeHtml(latestText)}</span>
        </div>
      </div>
      ${summary.length ? `<div class="style-profile-summary">${summary.slice(0, 4).map((line) => `<span>${escapeHtml(line)}</span>`).join("")}</div>` : ""}
      <div class="style-profile-grid">
        <section>
          <h3>常保留卖点</h3>
          <div>${renderStyleProfilePills(profile.selling_preferences, "暂无稳定卖点")}</div>
        </section>
        <section>
          <h3>常删除内容</h3>
          <div>${renderStyleProfilePills(profile.avoid_preferences, "暂无稳定删除方向")}</div>
        </section>
        <section>
          <h3>开头偏好</h3>
          <div>${renderStyleProfilePills(profile.hook_preferences, "继续观察开头选择")}</div>
        </section>
        <section>
          <h3>结尾偏好</h3>
          <div>${renderStyleProfilePills(profile.ending_preferences, "继续观察结尾选择")}</div>
        </section>
      </div>
      <div class="style-profile-metrics">
        <span>卖点密度 <strong>${escapeHtml(metrics.selling_density || "观察中")}</strong></span>
        <span>剪辑节奏 <strong>${escapeHtml(metrics.rhythm || "观察中")}</strong></span>
        <span>上下文 <strong>${escapeHtml(metrics.context_length || "观察中")}</strong></span>
        <span>情绪强度 <strong>${escapeHtml(metrics.emotion_strength || "观察中")}</strong></span>
        <span>CTA <strong>${escapeHtml(metrics.cta_strength || "观察中")}</strong></span>
      </div>
    </div>
  `;
}

const FEEDBACK_POSITIVE_ROLES = new Set(["hook_positive", "close_positive", "move_to_front", "move_to_end", "sentence_positive"]);
const FEEDBACK_NEGATIVE_ROLES = new Set(["hook_negative", "close_negative", "sentence_negative"]);
const FEEDBACK_STRUCTURAL_AVOID = new Set(["host_chatter", "environment_noise", "inventory_pressure", "filler_or_fragment"]);
const FEEDBACK_SIGNAL_RULES = [
  { key: "color_benefit", label: "颜色/显白卖点", words: ["显白", "显肤", "肤亮", "颜色", "黑色", "白色", "绿色", "亮色", "米白", "饱和度", "冷白"] },
  { key: "fit_texture", label: "版型/质感卖点", words: ["显瘦", "质感", "面料", "版型", "细节", "袖子", "好穿", "舒服", "垂感", "高级"] },
  { key: "scene_styling", label: "场景/搭配表达", words: ["日常", "生活", "运动", "骑行", "拍照", "场景", "搭配", "出片", "穿搭", "黑白灰"] },
  { key: "objection_answer", label: "购买顾虑解释", words: ["不安心", "从来没有", "不敢", "不知道", "怕", "适合", "稳妥", "尝试", "口味", "惊喜"] },
  { key: "emotional_hook", label: "情绪/记忆点", words: ["相信我", "惊喜", "记忆点", "风格", "气质", "性格", "值得", "好看", "宝宝"] },
  { key: "host_chatter", label: "主播闲聊/自嗨", words: ["老粉", "拉黑", "划走", "催债", "催交", "不好意思", "听我讲话", "吹牛", "下次"] },
  { key: "environment_noise", label: "环境/直播间干扰", words: ["直播间", "手机屏幕", "肉眼", "窗户", "光很亮", "帘子", "走远", "颜色比较对"] },
  { key: "inventory_pressure", label: "库存/预售催促", words: ["首批", "拼手速", "没了", "预售", "库存", "加完", "备货", "一点都没有"] },
  { key: "filler_or_fragment", label: "口头禅/断句", words: ["来好了", "对然后", "能理解吗", "为什么", "呀对不对", "白开水", "因为我知道", "然后整个", "你看啊"] },
];

function compactFeedbackText(text = "") {
  return String(text || "").replace(/[^\u4e00-\u9fffA-Za-z0-9]+/g, "");
}

function feedbackConfidence(count = 0) {
  if (count >= 8) return "较强";
  if (count >= 5) return "明显";
  if (count >= 3) return "轻微";
  if (count >= 1) return "观察中";
  return "无";
}

function feedbackSignalKeys(text = "") {
  const value = String(text || "");
  const compact = compactFeedbackText(value);
  const keys = [];
  FEEDBACK_SIGNAL_RULES.forEach((rule) => {
    if (rule.words.some((word) => value.includes(word) || compact.includes(compactFeedbackText(word)))) {
      keys.push(rule.key);
    }
  });
  if (compact.length <= 5 && ["对然后", "为什么", "来好了", "白开水", "能理解吗", "呀对不对"].includes(compact)) {
    if (!keys.includes("filler_or_fragment")) keys.push("filler_or_fragment");
  }
  if (/(因为|然后|包括|或者|这个|整个|有一点|你看)$/.test(value.trim())) {
    if (!keys.includes("filler_or_fragment")) keys.push("filler_or_fragment");
  }
  return keys;
}

function buildPreferenceSummaryFromSamples(samples = []) {
  const signalMap = new Map(FEEDBACK_SIGNAL_RULES.map((rule) => [rule.key, {
    key: rule.key,
    label: rule.label,
    positive_count: 0,
    negative_count: 0,
    positive_examples: [],
    negative_examples: [],
  }]));
  const textRoles = new Map();
  let total = 0;
  samples.forEach((sample) => {
    const role = sample.role || "";
    const polarity = FEEDBACK_POSITIVE_ROLES.has(role) ? "positive" : FEEDBACK_NEGATIVE_ROLES.has(role) ? "negative" : "";
    if (!polarity) return;
    const text = String(sample.text || "").trim();
    const count = Math.max(1, Number(sample.count || 1));
    if (!text) return;
    total += count;
    const compact = compactFeedbackText(text);
    const textEntry = textRoles.get(compact) || { text, positive: 0, negative: 0 };
    textEntry[polarity] += count;
    textRoles.set(compact, textEntry);
    feedbackSignalKeys(text).forEach((key) => {
      const signal = signalMap.get(key);
      if (!signal) return;
      const countKey = polarity === "positive" ? "positive_count" : "negative_count";
      const exampleKey = polarity === "positive" ? "positive_examples" : "negative_examples";
      signal[countKey] += count;
      if (!signal[exampleKey].includes(text) && signal[exampleKey].length < 3) signal[exampleKey].push(text);
    });
  });
  const positive = [];
  const negative = [];
  signalMap.forEach((signal) => {
    const pos = Number(signal.positive_count || 0);
    const neg = Number(signal.negative_count || 0);
    if (!pos && !neg) return;
    const net = pos - neg;
    const item = { ...signal, net, confidence: feedbackConfidence(Math.abs(net)) };
    if (FEEDBACK_STRUCTURAL_AVOID.has(signal.key)) {
      if (neg > 0) negative.push({ ...item, net: -neg, confidence: feedbackConfidence(neg) });
    } else if (net > 0) {
      positive.push(item);
    } else if (net < 0) {
      negative.push(item);
    }
  });
  positive.sort((a, b) => (b.net - a.net) || (b.positive_count - a.positive_count));
  negative.sort((a, b) => (Math.abs(b.net) - Math.abs(a.net)) || (b.negative_count - a.negative_count));
  const conflicts = Array.from(textRoles.values())
    .filter((item) => item.positive > 0 && item.negative > 0)
    .sort((a, b) => (b.positive + b.negative) - (a.positive + a.negative))
    .slice(0, 8);
  const brief = [];
  if (positive.length) brief.push(`优先倾向：${positive.slice(0, 4).map((item) => item.label).join("、")}。`);
  if (negative.length) brief.push(`谨慎避开：${negative.slice(0, 4).map((item) => item.label).join("、")}。`);
  if (conflicts.length) brief.push("存在正反都出现过的句子，不能按原文硬匹配，需要结合上下文。");
  const summary = {
    read_only: true,
    confidence: feedbackConfidence(total),
    sample_count: total,
    positive: positive.slice(0, 6),
    negative: negative.slice(0, 6),
    conflicts,
    brief,
    notes: [
      "这是前端只读摘要；画像会按所选影响强度进入 AI 软参考，选择关闭时只学习不参与选片。",
      "1-2 次样本只作为观察，建议累计到 3 次以上再作为稳定偏好。",
    ],
  };
  summary.style_profile = buildStyleProfileFromSummary(summary);
  return summary;
}

function renderAiFeedbackSummary(summary = {}) {
  const box = $("ai-feedback-summary");
  if (!box) return;
  const profile = summary.style_profile || buildStyleProfileFromSummary(summary);
  const positive = Array.isArray(summary.positive) ? summary.positive : [];
  const negative = Array.isArray(summary.negative) ? summary.negative : [];
  const conflicts = Array.isArray(summary.conflicts) ? summary.conflicts : [];
  const brief = Array.isArray(summary.brief) ? summary.brief : [];
  const notes = Array.isArray(summary.notes) ? summary.notes : [];
  if (!positive.length && !negative.length && !conflicts.length) {
    box.innerHTML = `
      ${renderStyleProfile(profile)}
      <p class="panel-note">完成一次 AI 预览人工调整并成片后，剪辑风格画像会开始学习。</p>
    `;
    return;
  }
  const conflictRows = conflicts.slice(0, 4).map((item) => `
    <span title="${escapeHtml(item.text || "")}">${escapeHtml(item.text || "")} · 保留 ${Number(item.positive || 0)} / 删除 ${Number(item.negative || 0)}</span>
  `).join("");
  box.innerHTML = `
    ${renderStyleProfile(profile)}
    <div class="feedback-summary-head">
      <strong>取舍依据</strong>
      <span>摘要 · ${escapeHtml(summary.confidence || "观察中")} · ${Number(summary.sample_count || 0)} 条样本</span>
    </div>
    ${brief.length ? `<div class="preference-brief">${brief.map((line) => `<span>${escapeHtml(line)}</span>`).join("")}</div>` : ""}
    <div class="preference-summary-grid">
      <section>
        <h3>倾向保留</h3>
        ${renderPreferenceSignalList(positive, "positive")}
      </section>
      <section>
        <h3>倾向删除</h3>
        ${renderPreferenceSignalList(negative, "negative")}
      </section>
    </div>
    ${conflictRows ? `<div class="preference-conflicts"><strong>歧义样本</strong>${conflictRows}</div>` : ""}
    ${notes.length ? `<p class="panel-note">${notes.map(escapeHtml).join(" ")}</p>` : ""}
  `;
}

function renderAiFeedbackSamples(result = {}) {
  renderAiFeedbackStats(result);
  const box = $("ai-feedback-samples");
  if (!box) return;
  const samples = Array.isArray(result.samples) ? result.samples : [];
  const roles = Array.isArray(result.roles) ? result.roles : [];
  renderAiFeedbackSummary(result.preference_summary || buildPreferenceSummaryFromSamples(samples));
  if (!samples.length) {
    box.innerHTML = '<p class="panel-note">暂无学习样本。完成一次 AI 预览人工调整并成片后，这里会显示常用开头、常删句子和常用结尾。</p>';
    return;
  }
  const byRole = new Map();
  samples.forEach((sample) => {
    const role = sample.role || "sentence_positive";
    if (!byRole.has(role)) byRole.set(role, []);
    byRole.get(role).push(sample);
  });
  const roleLabels = new Map(roles.map((item) => [item.role, item.label]));
  box.innerHTML = roles
    .filter((role) => byRole.has(role.role))
    .map((role) => {
      const roleSamples = byRole.get(role.role);
      const rows = roleSamples.map((sample) => `
        <div class="feedback-sample-row">
          <div class="feedback-sample-main">
            <strong>${escapeHtml(sample.text || "")}</strong>
            <span>${Number(sample.count || 0)} 次${sample.scopes?.length ? ` · ${escapeHtml(sample.scopes.join("/"))}` : ""}</span>
          </div>
          <button class="button button-muted button-small" data-action="delete-ai-feedback-sample" data-role="${escapeHtml(sample.role || role.role)}" data-text="${escapeHtml(sample.text || "")}">删除</button>
        </div>
      `).join("");
      const preview = roleSamples.slice(0, 3).map((sample) => `
        <span title="${escapeHtml(sample.text || "")}">${escapeHtml(sample.text || "")}</span>
      `).join("");
      return `
        <details class="feedback-role-group">
          <summary>
            <span>${escapeHtml(roleLabels.get(role.role) || role.label || role.role)}</span>
            <strong>${roleSamples.length} 条</strong>
          </summary>
          ${preview ? `<div class="feedback-role-preview">${preview}</div>` : ""}
          <div class="feedback-role-rows">${rows}</div>
        </details>
      `;
    })
    .join("");
}

async function loadAiFeedbackSamples(showToast = false) {
  let result;
  try {
    result = await api("/api/ai-feedback/samples");
  } catch (error) {
    const box = $("ai-feedback-samples");
    const message = String(error?.message || "");
    if (box && /not found|404/i.test(message)) {
      let runtime = {};
      try {
        runtime = await api("/api/runtime");
      } catch (_) {
        runtime = {};
      }
      const runtimePath = runtime.repo_root || runtime.web_dir || "";
      box.innerHTML = `
        <p class="panel-note">
          当前启动的完整包后端还没有剪辑风格画像接口，请关闭旧包，改用新版完整包后再刷新。
          ${runtimePath ? `<br>当前运行位置：${escapeHtml(runtimePath)}` : ""}
        </p>`;
      if (showToast) toast("当前启动的是旧后端，请使用新版完整包。", "warning");
      return;
    }
    throw error;
  }
  renderAiFeedbackSamples(result);
  if (showToast) toast("剪辑风格画像已刷新", "success");
}

function exportAiFeedback() {
  window.location.href = `/api/ai-feedback/export?t=${Date.now()}`;
  setTimeout(() => loadAiFeedbackSamples(false).catch(() => {}), 800);
  toast("剪辑风格画像数据导出已开始", "success");
}

async function importAiFeedback() {
  const picked = await api("/api/dialog/file", {
    method: "POST",
    body: JSON.stringify({ kind: "file" }),
  });
  if (!picked.path) return;
  const result = await api("/api/ai-feedback/import", {
    method: "POST",
    body: JSON.stringify({ path: picked.path }),
  });
  const count = $("ai-feedback-count");
  const size = $("ai-feedback-size");
  if (count) count.textContent = String(result.record_count || 0);
  if (size) size.textContent = formatFileSize(result.size || 0);
  await loadAiFeedbackSamples(false);
  toast(`导入完成：新增 ${result.added_count || 0} 条，跳过重复 ${result.skipped_count || 0} 条`, "success");
}

async function deleteAiFeedbackSample(button) {
  const role = button?.dataset.role || "";
  const text = button?.dataset.text || "";
  if (!role || !text) return;
  if (!confirm(`删除这条学习样本吗？\n\n${text}`)) return;
  const result = await api("/api/ai-feedback/sample/delete", {
    method: "POST",
    body: JSON.stringify({ role, text }),
  });
  renderAiFeedbackSamples(result);
  toast(`已删除 ${result.removed_count || 0} 条匹配样本`, "success");
}

async function clearAiFeedback() {
  if (!confirm("确定清空剪辑风格画像学习数据吗？清空前会自动备份当前文件。")) return;
  const result = await api("/api/ai-feedback/clear", { method: "POST", body: "{}" });
  renderAiFeedbackSamples(result);
  toast("剪辑风格画像学习数据已清空，原文件已自动备份", "success");
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
  if (!window.confirm("将清理成片/预览临时缓存，不会删除已导出的成片和原始素材；之后需要重新生成 AI 预览。确认继续？")) return;
  const result = await api("/api/cache/clear", { method: "POST", body: "{}" });
  toast(result.message || "缓存清理完成", "success");
}

async function applyUserDataDir() {
  const value = $("user-data-dir")?.value.trim() || "";
  if (!value) {
    toast("请先选择用户数据目录", "warning");
    return;
  }
  const current = state.runtime?.user_data_dir || "";
  const same = current && value.replace(/[\\/]+$/g, "").toLowerCase() === current.replace(/[\\/]+$/g, "").toLowerCase();
  if (same) {
    toast("当前已经使用这个用户数据目录", "warning");
    return;
  }
  if (!window.confirm("将把设置、词库和剪辑风格画像迁移到新目录。不会删除原目录，迁移期间请不要运行成片任务。确认继续？")) return;
  const result = await api("/api/user-data-dir", {
    method: "POST",
    body: JSON.stringify({ path: value, migrate: true }),
  });
  toast(result.message || "用户数据目录已切换", "success");
  await loadRuntime();
  await loadSettings(false);
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

async function getRuntimeInfo() {
  if (state.runtime) return state.runtime;
  try {
    state.runtime = await api("/api/runtime");
  } catch (_) {
    state.runtime = {};
  }
  return state.runtime;
}

function needsFullPackageUpdate(runtime = state.runtime, info = state.update?.info) {
  if (info?.requires_full_package) return true;
  if (info?.supports_web_incremental_updates === false) return true;
  if (!runtime) return false;
  return !runtime.app_dir || !runtime.web_dir || runtime.supports_web_incremental_updates === false;
}

function fullPackageUpdateMessage(runtime = state.runtime) {
  const path = runtime?.repo_root || runtime?.web_dir || "";
  return `当前启动的是旧完整包，在线增量更新可能只更新界面，不能更新后端。请关闭旧包，改用新版完整包。${path ? `\n当前运行位置：${path}` : ""}`;
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
  const fullPackageRequired = hasUpdate && needsFullPackageUpdate(state.runtime, info);
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
    const suffix = fileCount && !fullPackageRequired ? `\n${fileCount} \u4e2a\u6587\u4ef6\u5c06\u66f4\u65b0` : "";
    const fullPackageNote = fullPackageRequired ? `\n\n${info.requires_full_package_note || fullPackageUpdateMessage(state.runtime)}` : "";
    notes.textContent = (releaseNotes || (hasUpdate ? "\u53d1\u73b0\u53ef\u5b89\u88c5\u66f4\u65b0\u3002" : "\u6ca1\u6709\u53ef\u7528\u66f4\u65b0\u3002")) + suffix + fullPackageNote;
  }
  const applyButton = $("update-card-apply");
  if (applyButton) applyButton.disabled = !hasUpdate || busy || fullPackageRequired;
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
    const runtime = await getRuntimeInfo();
    if (needsFullPackageUpdate(runtime, update)) {
      update.requires_full_package = true;
      update.requires_full_package_note = update.requires_full_package_note || fullPackageUpdateMessage(runtime);
    }
    const version = update.version || "";
    const fullPackageRequired = Boolean(update.requires_full_package);
    setUpdateState({
      checking: false,
      available: true,
      info: update,
      message: fullPackageRequired
        ? (version ? `发现新版本 v${version}，需要完整包` : "发现新版本，需要完整包")
        : (version ? `\u53d1\u73b0\u65b0\u7248\u672c v${version}` : "\u53d1\u73b0\u65b0\u7248\u672c"),
    });
    if (!quiet) {
      toast(
        fullPackageRequired
          ? "这次更新需要下载新版完整包，当前客户端已禁止在线增量安装。"
          : (version ? `\u53d1\u73b0\u65b0\u7248\u672c v${version}` : "\u53d1\u73b0\u65b0\u7248\u672c"),
        fullPackageRequired ? "warning" : "success",
      );
    }
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
  const runtime = await getRuntimeInfo();
  if (needsFullPackageUpdate(runtime, state.update.info)) {
    const message = fullPackageUpdateMessage(runtime);
    setUpdateState({
      installing: false,
      error: message,
      message: "需要新版完整包",
    });
    alert(message);
    toast("当前客户端已禁止在线增量安装，请使用新版完整包。", "warning");
    return { ok: false, full_package_required: true, msg: message };
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

async function openPathValue(path) {
  const targetPath = String(path || "").trim();
  if (!targetPath) {
    toast("没有可打开的路径", "warning");
    return;
  }
  const result = await api("/api/path/open", {
    method: "POST",
    body: JSON.stringify({ path: targetPath }),
  });
  toast(result.message || "已打开目录", "success");
}

function defaultOutputPath(targetId) {
  const defaults = {
    "output-dir": ["video-paths", "output"],
    "mix-output-dir": ["mix-video-paths", "mix_output"],
    "scan-output-dir": ["scan-video-paths", "scan_output"],
    "vs-output-dir": ["vs-video-paths", "split_output"],
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

function mixGroupName(paths = [], fallbackIndex = 0) {
  const first = String(paths[0] || "").split(/[\\/]/).filter(Boolean).pop() || "";
  const stem = first.replace(/\.[^.]+$/, "").trim();
  return stem ? stem.slice(0, 28) : `第${fallbackIndex + 1}组`;
}

function normalizeMixGroup(group, index = 0) {
  const paths = Array.isArray(group?.video_paths)
    ? group.video_paths.map((item) => String(item || "").trim()).filter(Boolean)
    : [];
  const name = String(group?.name || "").trim() || mixGroupName(paths, index);
  return { name, video_paths: paths };
}

function syncActiveMixGroupFromEditor() {
  const index = state.activeMixGroupIndex;
  if (!Number.isInteger(index) || index < 0 || index >= state.mixGroups.length) return;
  const paths = getLines("mix-video-paths");
  state.mixGroups[index] = normalizeMixGroup({ ...state.mixGroups[index], video_paths: paths }, index);
}

function saveCurrentMixGroup() {
  const paths = getLines("mix-video-paths");
  if (!paths.length) {
    toast("当前组没有视频素材", "warning");
    return;
  }
  const currentIndex = state.activeMixGroupIndex;
  if (Number.isInteger(currentIndex) && currentIndex >= 0 && currentIndex < state.mixGroups.length) {
    state.mixGroups[currentIndex] = normalizeMixGroup({ ...state.mixGroups[currentIndex], video_paths: paths }, currentIndex);
    renderMixGroups();
    toast(`已更新第 ${currentIndex + 1} 组`, "success");
    return;
  }
  const nextIndex = state.mixGroups.length;
  state.mixGroups.push(normalizeMixGroup({ video_paths: paths }, nextIndex));
  state.activeMixGroupIndex = nextIndex;
  renderMixGroups();
  toast(`已保存第 ${nextIndex + 1} 组`, "success");
}

function newMixGroup() {
  const paths = getLines("mix-video-paths");
  if (paths.length) {
    if (Number.isInteger(state.activeMixGroupIndex) && state.activeMixGroupIndex >= 0 && state.activeMixGroupIndex < state.mixGroups.length) {
      syncActiveMixGroupFromEditor();
    } else {
      state.mixGroups.push(normalizeMixGroup({ video_paths: paths }, state.mixGroups.length));
    }
  }
  state.activeMixGroupIndex = null;
  setLines("mix-video-paths", []);
  renderMixGroups();
  toast(`准备第 ${state.mixGroups.length + 1} 组`, "success");
}

function deleteActiveMixGroup() {
  const index = state.activeMixGroupIndex;
  if (!Number.isInteger(index) || index < 0 || index >= state.mixGroups.length) {
    toast("请先选择要删除的组", "warning");
    return;
  }
  state.mixGroups.splice(index, 1);
  if (state.mixGroups.length) {
    state.activeMixGroupIndex = Math.min(index, state.mixGroups.length - 1);
    setLines("mix-video-paths", state.mixGroups[state.activeMixGroupIndex].video_paths);
  } else {
    state.activeMixGroupIndex = null;
    setLines("mix-video-paths", []);
  }
  renderMixGroups();
  toast("已删除当前组", "success");
}

function selectMixGroup(index) {
  if (!Number.isInteger(index) || index < 0 || index >= state.mixGroups.length) return;
  syncActiveMixGroupFromEditor();
  state.activeMixGroupIndex = index;
  setLines("mix-video-paths", state.mixGroups[index].video_paths);
  renderMixGroups();
}

function collectMixBatchGroups() {
  syncActiveMixGroupFromEditor();
  const groups = state.mixGroups.map((group, index) => normalizeMixGroup(group, index));
  const current = getLines("mix-video-paths");
  if (current.length && !Number.isInteger(state.activeMixGroupIndex)) {
    groups.push(normalizeMixGroup({ video_paths: current }, groups.length));
  }
  return groups.filter((group) => group.video_paths.length);
}

function renderMixGroups() {
  const box = $("mix-group-list");
  if (!box) return;
  const groups = state.mixGroups.map((group, index) => normalizeMixGroup(group, index));
  if (!groups.length) {
    box.innerHTML = "";
    box.classList.add("is-empty");
    return;
  }
  box.classList.remove("is-empty");
  box.innerHTML = groups.map((group, index) => {
    const active = index === state.activeMixGroupIndex;
    const title = `${group.name} · ${group.video_paths.length} 个视频`;
    return `
      <button class="mix-group-chip ${active ? "is-active" : ""}" type="button" data-action="mix-select-group" data-index="${index}" title="${escapeHtml(title)}">
        <strong>${index + 1}</strong>
        <span>${escapeHtml(group.name)}</span>
        <em>${group.video_paths.length}个</em>
      </button>`;
  }).join("");
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

function compactVideoMetaText(info) {
  if (!info) return "检测中";
  if (!info.exists || !info.valid) return videoMetaText(info);
  const parts = [];
  if (Number(info.duration) > 0) parts.push(formatSeconds(info.duration));
  if (info.resolution) parts.push(info.resolution);
  return parts.join(" · ") || "可用";
}

function renderVideoList(targetId) {
  const box = document.querySelector(`[data-list-for="${targetId}"]`);
  if (!box) return;
  const lines = getLines(targetId);
  const isCompact = compactVideoListTargetIds.has(targetId);
  updateVideoCountBadge(targetId, lines.length);
  box.closest(".video-picker-card")?.classList.toggle("has-videos", lines.length > 0);
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
    const rowClass = ["video-row", isCompact ? "video-row-compact" : "", isInvalid ? "is-invalid" : "", isDuplicate ? "is-duplicate" : ""].filter(Boolean).join(" ");
    const badges = [
      isInvalid ? `<span class="video-badge is-invalid">无效</span>` : "",
      isDuplicate ? `<span class="video-badge is-duplicate">重复</span>` : "",
    ].join("");
    if (isCompact) {
      return `
        <div class="${rowClass}" draggable="true" data-video-row="${targetId}" data-index="${index}">
          <div class="video-drag" title="拖拽排序">≡</div>
          <div class="video-main">
            <div class="video-title"><strong title="${escapeHtml(path)}">${escapeHtml(name)}</strong>${badges}</div>
            <span class="video-meta">${escapeHtml(compactVideoMetaText(info))}</span>
          </div>
          <button class="video-remove" type="button" title="删除" data-action="remove-video" data-target="${targetId}" data-index="${index}">×</button>
        </div>`;
    }
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
  syncFlowActionState();
}

function updateVideoCountBadge(targetId, count) {
  document.querySelectorAll(`[data-video-count-for="${targetId}"]`).forEach((badge) => {
    badge.textContent = `${Number(count) || 0}个`;
    badge.title = `已添加 ${Number(count) || 0} 个视频素材`;
  });
}

function setButtonsEnabled(selector, enabled, reason = "") {
  document.querySelectorAll(selector).forEach((button) => {
    if (button.dataset.enabledTitle === undefined) {
      button.dataset.enabledTitle = button.getAttribute("title") || "";
    }
    button.disabled = !enabled;
    button.setAttribute("aria-disabled", enabled ? "false" : "true");
    button.classList.toggle("is-disabled", !enabled);
    button.title = enabled ? button.dataset.enabledTitle : reason || button.dataset.enabledTitle || "";
  });
}

function previewReady(preview) {
  return preview?.id && preview.status === "ready" && (preview.clips || []).some((clip) => clip?.selected !== false);
}

function syncFlowActionState() {
  const smartHasVideos = getVideoPaths().length > 0;
  const mixHasVideos = getLines("mix-video-paths").length > 0;
  const runningScopes = state.runningScopes instanceof Set ? state.runningScopes : new Set();

  setButtonsEnabled('[data-action="start-smart-preview"]', smartHasVideos, "先添加视频素材");
  setButtonsEnabled('[data-action="start-smart-cut"]', smartHasVideos, "先添加视频素材");
  setButtonsEnabled('[data-action="start-smart-from-preview"]', previewReady(state.smartPreview), "先生成并保留 AI 选片预览");
  setButtonsEnabled('[data-action="start-mix-preview"]', mixHasVideos, "先添加混剪视频素材");
  setButtonsEnabled('[data-action="feature-submit"][data-feature="mix"]', mixHasVideos, "先添加混剪视频素材");
  setButtonsEnabled('[data-action="start-mix-from-preview"]', previewReady(state.mixPreview), "先生成并保留混剪 AI 选片预览");
  setButtonsEnabled('[data-action="stop-scope"][data-scope="smart-cut"]', runningScopes.has("smart-cut"), "当前没有智能成片任务");
  setButtonsEnabled('[data-action="stop-scope"][data-scope="mix"]', runningScopes.has("mix"), "当前没有混剪任务");
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
  const useAssetPip = mode === "asset";
  return {
    pip_enabled: useAutoPip || (useAssetPip && Boolean(pipPath || pipFolder)),
    pip_path: useAutoPip ? "auto" : (useAssetPip ? pipPath : ""),
    pip_folder: useAssetPip ? pipFolder : "",
    pip_size: Number($(`${prefix}-pip-size`)?.value || 0.15),
    pip_opacity: Number($(`${prefix}-pip-opacity`)?.value || 0.03),
    pip_pos: $(`${prefix}-pip-pos`)?.value || "右下",
  };
}

function normalizeDedupPresetValue(value) {
  const preset = String(value || "medium").trim().toLowerCase();
  return preset === "off" ? "none" : (preset || "medium");
}

function collectDedupCustomPayload(prefix) {
  const checked = (id, fallback = false) => $(id)?.checked ?? fallback;
  const number = (id, fallback = 0) => Number($(id)?.value || fallback);
  return {
    video: {
      mirror: checked(`${prefix}-mirror`, true),
      crop: checked(`${prefix}-dedup-crop`, false),
      crop_value: number(`${prefix}-dedup-crop-value`, 0),
      speed: checked(`${prefix}-dedup-speed`, false),
      speed_value: number(`${prefix}-dedup-speed-value`, 100),
      frame_structure: checked(`${prefix}-dedup-frame-structure`, false),
      frame_structure_level: $(`${prefix}-dedup-frame-level`)?.value || "medium",
      blur: checked(`${prefix}-dedup-blur`, false),
      blur_value: number(`${prefix}-dedup-blur-value`, 0),
      sharpen: checked(`${prefix}-dedup-sharpen`, false),
      sharpen_value: number(`${prefix}-dedup-sharpen-value`, 0),
      gamma_shift: checked(`${prefix}-dedup-color`, false),
      corner_mask: checked(`${prefix}-dedup-mask`, false),
      bg_fill: checked(`${prefix}-dedup-bg-fill`, false),
    },
    audio: {
      pitch: checked(`${prefix}-dedup-audio-pitch`, false),
      reverb: checked(`${prefix}-dedup-audio-reverb`, false),
      noise_fusion: checked(`${prefix}-dedup-noise-fusion`, false),
    },
  };
}

function collectTransitionPayload(prefix) {
  const mode = $(`${prefix}-transition`)?.value || "off";
  return {
    transition: {
      mode: mode === "fade" ? "fade" : "off",
      duration: mode === "fade" ? 0.12 : 0,
    },
  };
}

function refreshDedupCustomVisibility(prefix) {
  const panel = document.querySelector(`[data-dedup-custom-panel="${prefix}"]`);
  const entry = document.querySelector(`[data-dedup-custom-entry="${prefix}"]`);
  const button = entry?.querySelector("[data-action='toggle-dedup-detail']");
  if (!panel) return;
  const isCustom = normalizeDedupPresetValue($(`${prefix}-dedup`)?.value) === "custom";
  panel.classList.toggle("is-hidden", !isCustom);
  entry?.classList.toggle("is-hidden", !isCustom);
  if (!isCustom) panel.classList.remove("is-open");
  if (button) button.textContent = panel.classList.contains("is-open") ? "收起设置" : "详细设置";
}

function toggleDedupDetail(prefix) {
  const panel = document.querySelector(`[data-dedup-custom-panel="${prefix}"]`);
  if (!panel) return;
  const isCustom = normalizeDedupPresetValue($(`${prefix}-dedup`)?.value) === "custom";
  if (!isCustom) return;
  panel.classList.toggle("is-open");
  refreshDedupCustomVisibility(prefix);
}

function bindDedupCustomControls() {
  ["sc", "mix"].forEach((prefix) => {
    $(`${prefix}-dedup`)?.addEventListener("change", () => refreshDedupCustomVisibility(prefix));
    refreshDedupCustomVisibility(prefix);
  });
}

function collectSmartPayload(options = {}) {
  const requireVideos = options.requireVideos === true;
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
    dedup_preset: normalizeDedupPresetValue($("sc-dedup").value),
    mirror_enabled: $("sc-mirror").checked,
    subtitle_overlay: $("sc-subtitle").checked,
    smart_crop_enabled: $("sc-crop").checked,
    crop_level: $("sc-crop-level").value,
    ken_burns_enabled: $("sc-kenburns").checked,
    ken_burns_intensity: $("sc-kb-intensity").value,
    ...collectDedupCustomPayload("sc"),
    ...collectTransitionPayload("sc"),
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
    order: selection.order,
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
    order: selection.order,
    selected_segments: selection.selectedSegments,
  };
  await runPreflight("mix-from-preview", payload, "mix");
  const result = await api("/api/mix/from-preview/start", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  toast(result.message || "预览混剪任务已启动", "success");
  refreshTasks();
}

function previewPageId(scope = "smart") {
  return scope === "mix" ? "page-mix" : "page-smart-cut";
}

function previewPanelPrefix(scope = "smart") {
  return scope === "mix" ? "mix" : "sc";
}

function autoCollapsePreviewPrepPanels(scope = "smart") {
  const prefix = previewPanelPrefix(scope);
  document.querySelectorAll(`[data-summary-prefix="${prefix}"]`).forEach((panel) => {
    setCollapsiblePanelCollapsed(panel, true);
  });
}

function setPreviewLayoutState(scope = "smart", preview = null) {
  const clips = preview?.clips || [];
  const status = preview?.status || "";
  const isRunning = status === "running" || status === "queued";
  const hasResult = Boolean(preview?.id && status === "ready" && clips.length);
  const page = $(previewPageId(scope));
  if (isRunning) return;
  page?.classList.toggle("has-preview-result", hasResult);
  if (!hasResult) {
    state.previewPrepAutoCollapsed[scope] = false;
    return;
  }
  if (!state.previewPrepAutoCollapsed[scope]) {
    autoCollapsePreviewPrepPanels(scope);
    state.previewPrepAutoCollapsed[scope] = true;
  }
}

function getPreviewState(scope = "smart") {
  return scope === "mix" ? state.mixPreview : state.smartPreview;
}

function renderPreviewState(scope = "smart") {
  if (scope === "mix") renderMixPreview(state.mixPreview);
  else renderSmartPreview(state.smartPreview);
}

function previewBox(scope = "smart") {
  return scope === "mix" ? $("mix-preview") : $("smart-preview");
}

function previewStoryScrollTop(scope = "smart") {
  return previewBox(scope)?.querySelector(".clip-story-list")?.scrollTop || 0;
}

function renderPreviewStateKeepStoryScroll(scope = "smart") {
  const scrollTop = previewStoryScrollTop(scope);
  renderPreviewState(scope);
  const list = previewBox(scope)?.querySelector(".clip-story-list");
  if (list) list.scrollTop = scrollTop;
}

function previewSplitStorageKey(scope = "smart") {
  return `liveclipper:preview-split:${scope}`;
}

function clampPreviewSplitRatio(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return 70;
  return Math.max(38, Math.min(74, number));
}

function previewSplitRatio(scope = "smart") {
  if (state.previewSplitRatios[scope] !== undefined) {
    return clampPreviewSplitRatio(state.previewSplitRatios[scope]);
  }
  let stored = null;
  try {
    stored = localStorage.getItem(previewSplitStorageKey(scope));
  } catch (error) {
    stored = null;
  }
  const ratio = clampPreviewSplitRatio(stored || 70);
  state.previewSplitRatios[scope] = ratio;
  return ratio;
}

function applyPreviewSplitRatio(scope = "smart", ratio = null) {
  const value = clampPreviewSplitRatio(ratio ?? previewSplitRatio(scope));
  state.previewSplitRatios[scope] = value;
  const workbench = previewBox(scope)?.querySelector(`[data-preview-workbench="${scope}"]`);
  if (!workbench) return;
  workbench.style.setProperty("--preview-left", `${value}%`);
  const resizer = workbench.querySelector(`[data-preview-resizer="${scope}"]`);
  if (resizer) resizer.setAttribute("aria-valuenow", String(Math.round(value)));
}

function savePreviewSplitRatio(scope = "smart", ratio = 70) {
  const value = clampPreviewSplitRatio(ratio);
  state.previewSplitRatios[scope] = value;
  try {
    localStorage.setItem(previewSplitStorageKey(scope), String(value));
  } catch (error) {
    // Split position is a comfort setting; ignore storage failures.
  }
  applyPreviewSplitRatio(scope, value);
}

function previewSplitRatioFromPointer(workbench, clientX) {
  const rect = workbench.getBoundingClientRect();
  const width = Math.max(1, rect.width);
  const handle = workbench.querySelector(".clip-preview-resizer");
  const handleWidth = handle?.getBoundingClientRect().width || 10;
  const minLeft = Math.min(Math.max(340, width * 0.38), width - 260);
  const minRight = Math.min(300, width * 0.42);
  const maxLeft = Math.max(minLeft, width - minRight - handleWidth);
  const rawLeft = clientX - rect.left;
  const left = Math.max(minLeft, Math.min(maxLeft, rawLeft));
  return Math.round((left / width) * 1000) / 10;
}

function bindPreviewSplitResizer(box, scope = "smart") {
  const workbench = box.querySelector(`[data-preview-workbench="${scope}"]`);
  const resizer = workbench?.querySelector(`[data-preview-resizer="${scope}"]`);
  if (!workbench || !resizer) return;
  applyPreviewSplitRatio(scope);
  resizer.addEventListener("pointerdown", (event) => {
    if (event.button !== undefined && event.button !== 0) return;
    event.preventDefault();
    workbench.classList.add("is-resizing");
    document.body.classList.add("is-preview-resizing");
    resizer.setPointerCapture?.(event.pointerId);
    const onMove = (moveEvent) => {
      const ratio = previewSplitRatioFromPointer(workbench, moveEvent.clientX);
      applyPreviewSplitRatio(scope, ratio);
    };
    const onUp = (upEvent) => {
      const ratio = previewSplitRatioFromPointer(workbench, upEvent.clientX);
      savePreviewSplitRatio(scope, ratio);
      workbench.classList.remove("is-resizing");
      document.body.classList.remove("is-preview-resizing");
      resizer.releasePointerCapture?.(upEvent.pointerId);
      resizer.removeEventListener("pointermove", onMove);
      resizer.removeEventListener("pointerup", onUp);
      resizer.removeEventListener("pointercancel", onUp);
    };
    resizer.addEventListener("pointermove", onMove);
    resizer.addEventListener("pointerup", onUp);
    resizer.addEventListener("pointercancel", onUp);
  });
  resizer.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const current = previewSplitRatio(scope);
    let next = current;
    if (event.key === "ArrowLeft") next = current - 2;
    if (event.key === "ArrowRight") next = current + 2;
    if (event.key === "Home") next = 50;
    if (event.key === "End") next = 70;
    savePreviewSplitRatio(scope, next);
  });
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
        segment.selected = segment.selection_locked === true
          ? false
          : segmentSet.has(Number(segment.index));
      });
    } else if (segments.length) {
      segments.forEach((segment) => {
        if (segment.selection_locked === true) segment.selected = false;
        else if (segment.selected === undefined) segment.selected = true;
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
  renderPreviewStateKeepStoryScroll(scope);
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
      if (segment.selection_locked === true) segment.selected = false;
      else if (segmentChecked.has(key)) segment.selected = segmentChecked.get(key);
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
        segment.selected = segment.selection_locked === true ? false : selected;
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
  if (!segment || segment.selection_locked === true) return;
  segment.selected = selected;
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
  renderPreviewStateKeepStoryScroll(scope);
}

function collectPreviewSelection(scope = "smart") {
  syncPreviewClipSelections(scope);
  const draft = commitPreviewDraft(scope, { remote: true });
  return {
    selectedIndices: draft.selected_indices || [],
    order: draft.order || [],
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
  renderPreviewStateKeepStoryScroll(scope);
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

function previewInlineVideoKey(scope, preview, clip, draft = null) {
  if (!preview?.id || !clip) return "";
  const clipIndex = Number(clip.index);
  const selection = draft || buildPreviewDraftFromState(scope);
  const selectedSegments = selection?.selected_segments && Array.isArray(selection.selected_segments[String(clipIndex)])
    ? selection.selected_segments[String(clipIndex)].join(",")
    : "";
  const selected = Array.isArray(selection?.selected_indices) && selection.selected_indices.includes(clipIndex) ? "1" : "0";
  return `${scope}:${preview.id}:${clipIndex}:${selected}:${selectedSegments}`;
}

function previewInlineVideoPanel(scope, index, key) {
  return Array.from(document.querySelectorAll(`[data-preview-inline-video="${scope}"]`))
    .find((node) => Number(node.dataset.previewIndex) === Number(index) && node.dataset.videoKey === key) || null;
}

function setInlinePreviewStatus(panel, message, type = "info") {
  const status = panel?.querySelector("[data-preview-inline-status]");
  if (!status) return;
  status.textContent = message || "";
  status.classList.toggle("is-hidden", !message);
  status.classList.toggle("is-error", type === "error");
}

function applyInlinePreviewVideoState(scope, index, key) {
  const panel = previewInlineVideoPanel(scope, index, key);
  if (!panel) return;
  const video = panel.querySelector("[data-preview-inline-player]");
  const entry = state.previewInlineVideos[key] || {};
  if (entry.url) {
    if (video && video.getAttribute("src") !== entry.url) {
      video.src = entry.url;
      video.load();
    }
    video?.classList.remove("is-hidden");
    setInlinePreviewStatus(panel, "", "info");
    return;
  }
  video?.classList.add("is-hidden");
  if (entry.status === "loading") {
    setInlinePreviewStatus(panel, "正在生成所选片段小视频...", "info");
  } else if (entry.error) {
    setInlinePreviewStatus(panel, `小视频生成失败：${entry.error}`, "error");
  } else {
    setInlinePreviewStatus(panel, "选择片段后显示小视频。", "info");
  }
}

async function ensureInlinePreviewVideo(scope = "smart", index = null) {
  const preview = getPreviewState(scope);
  if (!preview?.id || preview.status !== "ready") return;
  const clipIndex = Number(index);
  const clip = preview.clips?.find((item) => Number(item.index) === clipIndex);
  if (!clip) return;
  syncPreviewClipSelections(scope);
  const segments = previewSegments(clip);
  if (clip.selected === false || (segments.length && !selectedPreviewSegments(clip).length)) return;
  const draft = commitPreviewDraft(scope, { remote: true });
  const key = previewInlineVideoKey(scope, preview, clip, draft);
  if (!key) return;
  const existing = state.previewInlineVideos[key];
  if (existing?.url || existing?.status === "loading" || existing?.error) {
    applyInlinePreviewVideoState(scope, clipIndex, key);
    return;
  }
  state.previewInlineVideos[key] = { status: "loading" };
  applyInlinePreviewVideoState(scope, clipIndex, key);
  const endpoint = scope === "mix" ? "/api/mix/preview/clip-video" : "/api/smart-cut/preview/clip-video";
  try {
    const result = await api(endpoint, {
      method: "POST",
      body: JSON.stringify({
        preview_id: preview.id,
        clip_index: clipIndex,
        scope,
        selected_indices: draft.selected_indices || [],
        order: draft.order || [],
        selected_segments: draft.selected_segments || {},
        updated_at: draft.updated_at || Date.now(),
      }),
    });
    state.previewInlineVideos[key] = { status: "ready", url: result.url };
  } catch (error) {
    state.previewInlineVideos[key] = { status: "failed", error: error.message || String(error || "未知错误") };
  }
  applyInlinePreviewVideoState(scope, clipIndex, key);
}

async function previewClipVideo(index, scope = "smart") {
  const preview = scope === "mix" ? state.mixPreview : state.smartPreview;
  if (!preview?.id || preview.status !== "ready") {
    toast("请先生成 AI 选片预览", "warning");
    return;
  }
  syncPreviewClipSelections(scope);
  const clip = preview.clips?.find((item) => Number(item.index) === index);
  if (!clip) {
    toast("片段不存在，请重新生成预览", "warning");
    return;
  }
  const segments = previewSegments(clip);
  if (clip.selected === false || (segments.length && !selectedPreviewSegments(clip).length)) {
    toast("这个片段没有选中的句子，勾选后再预览", "warning");
    return;
  }
  const draft = commitPreviewDraft(scope, { remote: true });
  const bounds = effectiveClipBounds(clip);
  const modal = ensurePreviewModal();
  const video = modal.querySelector("#preview-modal-video");
  const title = modal.querySelector("#preview-modal-title");
  const status = modal.querySelector("#preview-modal-status");
  if (!video) return;
  if (title) title.textContent = `片段预览 ${formatSeconds(bounds.start)}-${formatSeconds(bounds.end)} · ${bounds.duration.toFixed(1)}s`;
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
      body: JSON.stringify({
        preview_id: preview.id,
        clip_index: index,
        scope,
        selected_indices: draft.selected_indices || [],
        order: draft.order || [],
        selected_segments: draft.selected_segments || {},
        updated_at: draft.updated_at || Date.now(),
      }),
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
  if (feature === "live-rec-monitor") {
    await submitLiveRecord();
    return;
  }
  await saveFeaturePreferences();
  const payload = collectFeaturePayload(feature);
  let preflightFeature = feature;
  let endpoint = `/api/${feature}/start`;
  if (feature === "mix") {
    const groups = collectMixBatchGroups();
    if (groups.length > 1) {
      payload.groups = groups;
      payload.video_paths = groups[0].video_paths;
      await submitMixBatch(payload, groups);
      return;
    } else if (groups.length === 1) {
      payload.video_paths = groups[0].video_paths;
    }
  }
  await runPreflight(preflightFeature, payload, scopeForFeature(feature));
  const result = await api(endpoint, {
    method: "POST",
    body: JSON.stringify(payload),
  });
  toast(result.message || "任务已提交", result.ok ? "success" : "warning");
  refreshTasks();
}

function mixSingleGroupPayload(basePayload, group) {
  const payload = {
    ...basePayload,
    video_paths: Array.isArray(group?.video_paths) ? group.video_paths : [],
  };
  delete payload.groups;
  return payload;
}

async function submitMixBatch(payload, groups) {
  delete state.legacyBatchProgress.mix;
  let batchPreflightOk = false;
  try {
    await runPreflight("mix-batch", payload, "mix");
    batchPreflightOk = true;
  } catch (error) {
    if (!isApiNotFound(error)) throw error;
    batchPreflightOk = false;
  }

  try {
    const result = await api("/api/mix/batch/start", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    toast(result.message || "批量混剪任务已提交", result.ok ? "success" : "warning");
    refreshTasks();
    return;
  } catch (error) {
    if (!isApiNotFound(error)) throw error;
    appendLog("mix", {
      time: new Date().toLocaleTimeString(),
      level: "warning",
      message: batchPreflightOk
        ? "当前运行的后端还没有批量混剪接口，已切换为兼容模式逐组提交。建议重启客户端或使用新版完整包。"
        : "当前运行的后端还没有批量混剪接口/预检接口，已切换为兼容模式逐组提交。建议重启客户端或使用新版完整包。",
    });
  }

  await submitMixBatchLegacyQueue(payload, groups);
}

async function submitMixBatchLegacyQueue(basePayload, groups) {
  const cleanGroups = groups.map((group, index) => normalizeMixGroup(group, index)).filter((group) => group.video_paths.length);
  if (!cleanGroups.length) throw new Error("请至少保存 1 个混剪素材组。");
  for (const group of cleanGroups) {
    await runPreflight("mix", mixSingleGroupPayload(basePayload, group), "mix");
  }

  toast(`兼容模式：将按顺序提交 ${cleanGroups.length} 组混剪`, "warning");
  appendLog("mix", {
    time: new Date().toLocaleTimeString(),
    level: "warning",
    message: `兼容模式启动：共 ${cleanGroups.length} 组。请保持此页面打开，当前组完成后会自动提交下一组。`,
  });

  const completed = [];
  const totalGroups = cleanGroups.length;
  setLegacyBatchProgress("mix", {
    total: totalGroups,
    done: 0,
    current: 1,
    status: "running",
    labelText: "批量混剪",
    percent: 0,
  });
  for (let index = 0; index < cleanGroups.length; index += 1) {
    const group = cleanGroups[index];
    const singlePayload = mixSingleGroupPayload(basePayload, group);
    setLegacyBatchProgress("mix", {
      total: totalGroups,
      done: index,
      current: index + 1,
      status: "running",
      label: group.name,
      labelText: `批量混剪 ${index + 1}/${totalGroups}`,
      percent: Math.round((index / totalGroups) * 100),
    });
    appendLog("mix", {
      time: new Date().toLocaleTimeString(),
      level: "info",
      message: `兼容模式提交第 ${index + 1}/${cleanGroups.length} 组：${group.name}`,
    });
    try {
      const result = await api("/api/mix/start", {
        method: "POST",
        body: JSON.stringify(singlePayload),
      });
      refreshTasks();
      const task = await waitForTaskComplete(result.task_id, "mix");
      if (task.status !== "completed") {
        const reason = task.error || task.message || "任务未完成";
        throw new Error(`第 ${index + 1} 组混剪失败：${reason}`);
      }
      completed.push(task.output || task.outputs?.[0] || group.name);
      setLegacyBatchProgress("mix", {
        total: totalGroups,
        done: index + 1,
        current: index + 1 < totalGroups ? index + 2 : 0,
        status: index + 1 === totalGroups ? "completed" : "running",
        labelText: index + 1 === totalGroups ? "批量混剪完成" : "批量混剪",
        percent: Math.round(((index + 1) / totalGroups) * 100),
      });
    } catch (error) {
      setLegacyBatchProgress("mix", {
        total: totalGroups,
        done: completed.length,
        current: 0,
        failed: 1,
        status: "failed",
        labelText: "批量混剪失败",
        percent: Math.round((completed.length / totalGroups) * 100),
      });
      throw error;
    }
  }

  toast(`兼容模式混剪完成：成功 ${completed.length}/${cleanGroups.length} 组`, "success");
  appendLog("mix", {
    time: new Date().toLocaleTimeString(),
    level: "success",
    message: `兼容模式混剪完成：成功 ${completed.length}/${cleanGroups.length} 组。`,
  });
  refreshTasks();
}

async function waitForTaskComplete(taskId, scope = "mix") {
  const startedAt = Date.now();
  while (Date.now() - startedAt < 6 * 60 * 60 * 1000) {
    await delay(2500);
    let data = {};
    try {
      data = await api("/api/tasks");
    } catch (error) {
      continue;
    }
    const task = (data.tasks || []).find((item) => item.id === taskId);
    if (!task) continue;
    if (["completed", "failed", "cancelled"].includes(task.status)) return task;
    if (task.scope === scope) updateLogProgressBar(scope, progressFromTask(task));
  }
  return { id: taskId, status: "failed", error: "等待混剪任务完成超时。" };
}

function shouldAppendPreflightLog(scope, level, message) {
  const key = `${scope || "settings"}:${level}:${message}`;
  const now = Date.now();
  const last = state.lastPreflightLog || {};
  if (last.key === key && now - Number(last.time || 0) < 3000) return false;
  state.lastPreflightLog = { key, time: now };
  return true;
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
    const logMessage = `${message}。解决办法：请先修正红色错误后再启动任务。`;
    if (shouldAppendPreflightLog(scope, "error", logMessage)) {
      appendLog(scope || "settings", {
        time: new Date().toLocaleTimeString(),
        level: "error",
        message: logMessage,
      });
    }
    throw new Error(message);
  }
  if (warnings.length) {
    const logMessage = `启动检查提示：${warnings.join("；")}`;
    if (shouldAppendPreflightLog(scope, "warning", logMessage)) {
      appendLog(scope || "settings", {
        time: new Date().toLocaleTimeString(),
        level: "warning",
        message: logMessage,
      });
    }
    toast(`启动检查提示：${warnings[0]}${warnings.length > 1 ? `（另有 ${warnings.length - 1} 项）` : ""}`, "warning");
  }
  return result;
}

function syncVideoSplitMode() {
  const mode = $("vs-split-mode")?.value || "count";
  document.querySelectorAll("[data-vs-mode-field]").forEach((row) => {
    row.hidden = row.dataset.vsModeField !== mode;
  });
}

function collectVideoSplitOverrides() {
  const overrides = {};
  document.querySelectorAll("[data-vs-override]").forEach((input) => {
    const key = input.dataset.vsOverride || "";
    const value = Math.round(Number(input.value || 0));
    if (key && Number.isFinite(value) && value > 0) {
      overrides[key] = value;
    }
  });
  return overrides;
}

async function previewVideoSplit() {
  await saveFeaturePreferences();
  const payload = collectFeaturePayload("video-split");
  await runPreflight("video-split", payload, "video-split");
  const preview = await api("/api/video-split/preview", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  renderVideoSplitPreview(preview);
  toast(`已生成 ${preview.total_segments || 0} 段分割预览`, "success");
}

function renderVideoSplitPreview(preview) {
  const box = $("vs-preview");
  if (!box) return;
  const videos = Array.isArray(preview?.videos) ? preview.videos : [];
  if (!videos.length) {
    box.classList.add("empty");
    box.innerHTML = "<p>添加视频后点击“预览分割”，这里会显示每段的起止时间和输出文件名。</p>";
    return;
  }

  const totalSegments = Number(preview.total_segments || videos.reduce((sum, item) => sum + (item.segments?.length || 0), 0));
  const totalDuration = Number(preview.total_duration || videos.reduce((sum, item) => sum + Number(item.duration || 0), 0));
  const mode = preview.mode || $("vs-split-mode")?.value || "count";
  const modeText = mode === "duration" ? "按时长分段" : "按数量分段";
  const summary = `
    <div class="split-preview-summary">
      <span>${escapeHtml(modeText)}</span>
      <strong>${videos.length} 个视频</strong>
      <strong>${totalSegments} 段</strong>
      <strong>${formatSeconds(totalDuration)}</strong>
    </div>
  `;

  const groups = videos.map((video, videoIndex) => {
    const segments = Array.isArray(video.segments) ? video.segments : [];
    const segmentCount = Number(video.segment_count || segments.length || 1);
    const path = String(video.path || "");
    const countControl = mode === "count" ? `
      <label class="split-count-control">
        <span>此文件段数</span>
        <input class="split-count-input" type="number" min="1" max="500" step="1" value="${segmentCount}" data-vs-override="${escapeHtml(path)}">
      </label>
    ` : "";
    const rows = segments.map((segment) => `
      <div class="split-preview-row">
        <span>#${Number(segment.index || 0)}</span>
        <span>${formatSeconds(segment.start)}</span>
        <span>${formatSeconds(segment.end)}</span>
        <span>${formatSeconds(segment.duration)}</span>
        <span title="${escapeHtml(segment.output_name || "")}">${escapeHtml(segment.output_name || "")}</span>
      </div>
    `).join("");
    return `
      <div class="split-preview-video">
        <div class="split-preview-video-head">
          <div>
            <strong>${videoIndex + 1}. ${escapeHtml(video.name || path || "视频")}</strong>
            <span>${escapeHtml(video.resolution || "未知分辨率")} · ${formatSeconds(video.duration)}</span>
          </div>
          ${countControl}
        </div>
        <div class="split-preview-table">
          <div class="split-preview-row split-preview-head">
            <span>序号</span>
            <span>起始</span>
            <span>结束</span>
            <span>时长</span>
            <span>输出文件</span>
          </div>
          ${rows}
        </div>
      </div>
    `;
  }).join("");

  box.classList.remove("empty");
  box.innerHTML = summary + groups;
}

function scopeForFeature(feature) {
  if (feature === "mix") return "mix";
  if (feature?.startsWith("ai-scan")) return "ai-scan";
  if (feature?.startsWith("product-scan")) return "product-scan";
  if (feature === "video-split") return "video-split";
  if (feature === "dedup") return "dedup";
  if (feature?.startsWith("live-rec")) return "live-rec";
  return "settings";
}

async function refreshTasks() {
  try {
    const data = await api("/api/tasks");
    const tasks = data.tasks || [];
    state.latestTasks = tasks;
    await refreshOutputHistory();
    const latest = tasks.slice(-8).reverse();
    state.runningScopes = new Set(tasks.filter((task) => ["queued", "running"].includes(task.status)).map((task) => task.scope));
    syncLiveRoomActivityFromTasks(tasks);
    renderTaskBadges(latest);
    updateLogProgressFromTasks(tasks);
    renderRunSummaries(tasks);
    syncFlowActionState();
  } catch (error) {
    // The log websocket already reports connection state; keep this quiet.
  }
}

async function refreshOutputHistory(force = false) {
  const now = Date.now();
  if (!force && state.outputHistoryFetchedAt && now - state.outputHistoryFetchedAt < 8000) return;
  try {
    const data = await api("/api/output-history?limit=80");
    state.outputHistory = Array.isArray(data.items) ? data.items : [];
    state.outputHistoryFetchedAt = now;
  } catch (error) {
    state.outputHistoryFetchedAt = now;
  }
}

function syncLiveRoomActivityFromTasks(tasks) {
  const liveTasks = (tasks || []).filter((task) => task.scope === "live-rec");
  if (!state.liveRooms.length) return;
  const taskById = new Map(liveTasks.map((task) => [task.id, task]));
  let changed = false;
  for (const room of state.liveRooms) {
    const key = liveRoomKey(room);
    const current = state.liveRoomActivity[key] || {};
    const roomUrl = normalizeLiveRoomUrl(room.url);
    const roomTasks = liveTasks.filter((item) => {
      const taskUrl = normalizeLiveRoomUrl(item.live_room_url);
      if (taskUrl && roomUrl && taskUrl === roomUrl) return true;
      const output = String(item.outputs?.[0] || item.output || "");
      return output && room.name && output.includes(room.name);
    });
    const currentTask = taskById.get(current.taskId);
    const task = (currentTask && ["queued", "running"].includes(currentTask.status))
      ? currentTask
      : (roomTasks.slice().reverse().find((item) => ["queued", "running"].includes(item.status)) || roomTasks[roomTasks.length - 1]);
    if (!task) {
      const staleStatus = `${current.liveStatus || ""} ${current.recordStatus || ""}`;
      if (current.taskId && /(启动中|检测中|录制中|排队中)/.test(staleStatus)) {
        state.liveRoomActivity[key] = {
          ...current,
          liveStatus: "未检测",
          recordStatus: "未监控",
          productStatus: isDouyinLiveRoom(room) ? "待监控" : "未启用",
          duration: "0:00",
          taskId: "",
        };
        changed = true;
      }
      continue;
    }
    const output = String(task.outputs?.[0] || task.output || "");
    const updates = { taskId: task.id };
    Object.assign(updates, liveProductResultFromTask(task));
    if (task.status === "queued") {
      updates.liveStatus = "排队中";
      updates.recordStatus = "待启动";
      updates.productStatus = isDouyinLiveRoom(room) ? "待监控" : "未启用";
      updates.duration = "0:00";
    } else if (task.status === "running") {
      const isRecording = String(task.message || "").includes("录制中") || Number(task.progress || 0) >= 35;
      updates.liveStatus = isRecording ? "直播中" : "检测中";
      updates.recordStatus = isRecording ? "录制中" : "检测中";
      updates.productStatus = isRecording && isDouyinLiveRoom(room) ? "监控中" : (isDouyinLiveRoom(room) ? "待监控" : "未启用");
      if (isRecording && (task.recording_started_at || task.started_at)) {
        updates.duration = formatSeconds(Math.max(0, Date.now() / 1000 - (task.recording_started_at || task.started_at)));
      } else {
        updates.duration = "0:00";
      }
      if (task.stream_quality) updates.streamQuality = normalizeLiveStreamQuality(task.stream_quality);
    } else if (task.status === "completed") {
      updates.liveStatus = "已完成";
      updates.recordStatus = "录制完成";
      const boundSegments = Number(task.product_bound_segments || 0);
      const pendingSegments = Number(task.product_pending_segments || 0);
      const candidateSignals = Number(task.active_product_candidate_count || task.status_2_candidate_count || 0);
      if (boundSegments > 0 || task.active_product_changed) {
        updates.productStatus = "已识别";
      } else if (candidateSignals > 0) {
        updates.productStatus = "候选待确认";
      } else if (pendingSegments > 0 || task.product_segments) {
        updates.productStatus = "待确认";
      } else {
        updates.productStatus = task.probe_summary || task.product_split_queue ? "未捕获" : "未启用";
      }
      updates.taskId = "";
      if (output) updates.file = output;
      if (task.stream_quality) updates.streamQuality = normalizeLiveStreamQuality(task.stream_quality);
    } else if (task.status === "failed") {
      const text = `${task.error || ""} ${task.message || ""}`;
      const noLive = /未直播|未开播|停播|直播已结束|no_stream|No matching|Target room mismatch/i.test(text);
      const qualityIssue = /清晰度|画质|stream_quality_below_minimum|quality/i.test(text);
      updates.liveStatus = noLive ? "未直播" : (qualityIssue ? "直播中" : "检测失败");
      updates.recordStatus = noLive ? "未录制" : (qualityIssue ? "清晰度不足" : "录制失败");
      updates.productStatus = noLive || qualityIssue ? "未监控" : (Number(task.active_product_candidate_count || task.status_2_candidate_count || 0) > 0 ? "候选待确认" : "待确认");
      updates.duration = "0:00";
      updates.taskId = "";
      if (task.stream_quality) updates.streamQuality = normalizeLiveStreamQuality(task.stream_quality);
    } else if (task.status === "cancelled") {
      updates.liveStatus = "已停止";
      updates.recordStatus = "已停止";
      const boundSegments = Number(task.product_bound_segments || 0);
      const pendingSegments = Number(task.product_pending_segments || 0);
      const candidateSignals = Number(task.active_product_candidate_count || task.status_2_candidate_count || 0);
      if (boundSegments > 0 || task.active_product_changed) {
        updates.productStatus = "已识别";
      } else if (candidateSignals > 0) {
        updates.productStatus = "候选待确认";
      } else if (pendingSegments > 0 || task.product_segments) {
        updates.productStatus = "待确认";
      } else {
        updates.productStatus = isDouyinLiveRoom(room) ? "未捕获" : "未启用";
      }
      updates.duration = "0:00";
      updates.taskId = "";
      if (output) updates.file = output;
      if (task.stream_quality) updates.streamQuality = normalizeLiveStreamQuality(task.stream_quality);
    }
    if ((task.recording_started_at || task.started_at) && task.status === "completed") {
      const end = task.finished_at || Date.now() / 1000;
      updates.duration = formatSeconds(Math.max(0, end - (task.recording_started_at || task.started_at)));
    }
    state.liveRoomActivity[key] = { ...current, ...updates };
    changed = true;
  }
  if (changed) renderLiveRooms();
}

function liveProductResultFromTask(task = {}) {
  const outputs = Array.isArray(task.outputs) ? task.outputs.map((item) => String(item || "").trim()).filter(Boolean) : [];
  const productOutputs = outputs.filter((path) => /\.(mp4|mov|m4v|flv|ts)$/i.test(path));
  const resultCount = Number(task.result_count || productOutputs.length || 0);
  const boundSegments = Number(task.product_bound_segments || 0);
  const pendingSegments = Number(task.product_pending_segments || 0);
  const productSegments = Number(task.product_segments || 0);
  const candidateSignals = Number(task.candidate_signal_count || 0);
  const activeCandidateCount = Number(task.active_product_candidate_count || task.status_2_candidate_count || 0);
  const ruleReviewCount = Number(task.active_product_rule_review_count || 0);
  const strongSignalCount = Number(task.strong_signal_count || 0);
  return {
    productOutputs,
    productOutputCount: resultCount,
    productClipsDir: String(task.product_clips_dir || "").trim(),
    productSplitQueue: String(task.product_split_queue || "").trim(),
    productTimeline: String(task.product_timeline || "").trim(),
    productBoundSegments: boundSegments,
    productPendingSegments: pendingSegments,
    productSegments,
    productCandidateSignals: candidateSignals,
    productActiveCandidateCount: activeCandidateCount,
    productRuleReviewCount: ruleReviewCount,
    productStrongSignalCount: strongSignalCount,
    productUnresolvedReason: String(task.unresolved_reason || "").trim(),
    recordingReturncode: task.recording_returncode,
    activeProductChanged: Boolean(task.active_product_changed || boundSegments > 0),
    rollingCycleCount: Number(task.rolling_cycle_count || 0),
  };
}

function livePathName(path) {
  return String(path || "").split(/[\\/]/).filter(Boolean).pop() || String(path || "");
}

function normalizeLiveRoomUrl(url) {
  return String(url || "").trim().replace(/[?#].*$/, "").replace(/\/$/, "");
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
  renderPreviewStateKeepStoryScroll(scope);
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

function sourceBaseName(value) {
  return String(value || "").split(/[\\/]/).filter(Boolean).pop() || String(value || "");
}

function previewSourceAlias(scope, preview, clip) {
  if (scope !== "mix") return "";
  const sourceName = String(clip?.source || clip?.source_name || "").trim();
  const sources = Array.isArray(preview?.sources) ? preview.sources : [];
  const sourceIndex = sources.findIndex((item) => {
    const value = typeof item === "object" && item
      ? String(item.path || item.source || item.name || "").trim()
      : String(item || "").trim();
    if (!value || !sourceName) return false;
    return value === sourceName || sourceBaseName(value) === sourceBaseName(sourceName);
  });
  if (sourceIndex >= 0) return `V${sourceIndex + 1}`;
  const marker = selectedPreviewText(clip).match(/\bV(\d+)\b/i);
  if (marker) return `V${marker[1]}`;
  return sourceName ? "V?" : "";
}

function previewClipReasonParts(clip, analysis, { includeRisk = true } = {}) {
  const clipIndex = Number(clip?.index);
  const risk = analysis?.riskByIndex?.get(clipIndex);
  if (clip?.selected === false) {
    return [risk?.detail || "未选"];
  }
  const parts = [];
  const focus = String(clip?.focus || clip?.focus_block || "").trim();
  if (focus && focus !== "其他") parts.push(`重点：${focus}`);
  const tags = classifyClipScoreTags(clip, analysis)
    .filter((tag) => tag.label && tag.label !== "普通")
    .map((tag) => tag.label);
  if (tags.length) parts.push(`标签：${Array.from(new Set(tags)).slice(0, 4).join("/")}`);
  if (includeRisk && risk?.label && risk.label !== "正常") parts.push(`检查：${risk.label}`);
  return parts.length ? parts : [`${clipTypeLabel(clip?.clip_type)}片段，建议结合字幕和画面确认`];
}

function renderClipStoryMeta(clip, position, scope, preview, analysis) {
  const typeLabel = clipTypeLabel(clip.clip_type);
  const bounds = effectiveClipBounds(clip);
  const time = `${formatSeconds(bounds.start)}-${formatSeconds(bounds.end)}`;
  const alias = previewSourceAlias(scope, preview, clip);
  const parts = [
    `#${position + 1}`,
    typeLabel,
    time,
    ...previewClipReasonParts(clip, analysis, { includeRisk: true }),
  ];
  if (alias) parts.push(alias);
  const text = parts.filter(Boolean).join(" · ");
  return `<span class="clip-story-meta" title="${escapeHtml(text)}">${escapeHtml(text)}</span>`;
}

function renderClipStoryCard(clip, position, scope, preview, analysis, activeIndex) {
  const checked = clip.selected === false ? "" : "checked";
  const repeatTags = renderManualRepeatTags(clip);
  return `
    <article class="clip-preview-row clip-story-card ${clip.selected === false ? "is-unselected" : ""} ${Number(clip.index) === activeIndex ? "is-active" : ""}" draggable="true" data-preview-row data-preview-scope="${scope}" data-preview-index="${clip.index}">
      <div class="clip-story-select">
        <input type="checkbox" data-preview-clip="${clip.index}" data-preview-scope="${scope}" ${checked} title="保留这个片段">
        <div class="clip-drag-handle" title="拖拽排序" aria-label="拖拽排序">&#9776;</div>
      </div>
      <div class="clip-story-main">
        <div class="clip-story-topline">
          ${renderClipStoryMeta(clip, position, scope, preview, analysis)}
        </div>
        <div class="clip-content">
          ${renderClipStoryText(clip, repeatTags)}
        </div>
      </div>
    </article>
  `;
}

function renderPreviewInlineVideo(scope, preview, clip) {
  const segments = previewSegments(clip);
  const hasSelectedText = clip.selected !== false && (!segments.length || selectedPreviewSegments(clip).length);
  const draft = buildPreviewDraftFromState(scope);
  const key = previewInlineVideoKey(scope, preview, clip, draft);
  const entry = state.previewInlineVideos[key] || {};
  const videoClass = entry.url ? "" : "is-hidden";
  const statusClass = entry.url ? "is-hidden" : "";
  const statusText = !hasSelectedText
    ? "勾选左侧片段或句子后显示小视频。"
    : entry.error
      ? `小视频生成失败：${entry.error}`
      : entry.status === "loading"
        ? "正在生成所选片段小视频..."
        : "正在准备所选片段小视频...";
  return `
    <div class="clip-detail-video" data-preview-inline-video="${scope}" data-preview-index="${Number(clip.index)}" data-video-key="${escapeHtml(key)}">
      <div class="clip-detail-video-stage">
        <video class="${videoClass}" data-preview-inline-player controls playsinline preload="metadata" ${entry.url ? `src="${escapeHtml(entry.url)}"` : ""}></video>
        <div class="clip-detail-video-status ${statusClass} ${entry.error ? "is-error" : ""}" data-preview-inline-status>${escapeHtml(statusText)}</div>
      </div>
    </div>
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
    const locked = segment.selection_locked === true;
    const start = Number(segment.start || 0);
    const end = Number(segment.end || start);
    const segmentDuration = Math.max(0, Number(segment.duration || end - start));
    const rawTitle = segment.blocked_reason || segment.auto_unselected_reason || segment.text || "";
    const segmentTitle = escapeHtml(rawTitle);
    return `
      <label class="clip-segment clip-detail-segment ${segment.selected === false ? "is-unselected" : ""} ${locked ? "is-locked" : ""}" title="${segmentTitle}" data-preview-segment-row data-preview-scope="${scope}" data-preview-segment-parent="${Number(clip.index)}" data-preview-segment-index="${Number(segment.index)}" draggable="false">
        <input type="checkbox" data-preview-segment data-preview-scope="${scope}" data-preview-segment-parent="${Number(clip.index)}" data-preview-segment-index="${Number(segment.index)}" ${checked} ${locked ? "disabled" : ""}>
        <span class="clip-segment-time">${escapeHtml(formatSeconds(start))}-${escapeHtml(formatSeconds(end))}<em>${segmentDuration.toFixed(1)}s</em></span>
        <span class="clip-segment-text">${segmentTitle}</span>
      </label>
    `;
  }).join("") : `<div class="clip-detail-empty">这个片段没有句子拆分，将按整段参与成片。</div>`;
  return `
    <aside class="clip-detail-panel" data-preview-detail="${scope}">
      <div class="clip-detail-head">
        <div class="clip-detail-title">
          <strong>#${position + 1} ${escapeHtml(typeLabel)}</strong>
          <span>${escapeHtml(time)}</span>
        </div>
        <button class="button button-secondary button-small" data-action="preview-clip-video" data-preview-scope="${scope}" data-preview-index="${clip.index}">打开大预览</button>
      </div>
      <div class="clip-detail-stats">
        <span>${escapeHtml(duration)}</span>
        <span>${escapeHtml(segmentCountText)}</span>
        <span class="clip-risk is-${riskClass}" title="${escapeHtml(risk?.detail || riskLabel)}">${escapeHtml(riskLabel)}</span>
      </div>
      ${renderPreviewInlineVideo(scope, preview, clip)}
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
  const rows = clips.map((clip, position) => renderClipStoryCard(clip, position, scope, preview, analysis, activeIndex));
  return `
    <div data-preview-summary="${scope}">${renderPreviewSummary(analysis)}</div>
    <div class="clip-preview-workbench" data-preview-workbench="${scope}" style="--preview-left: ${previewSplitRatio(scope)}%;">
      <div class="clip-story-list" role="list">
        ${rows.join("")}
      </div>
      <div class="clip-preview-resizer" data-preview-resizer="${scope}" role="separator" aria-orientation="vertical" aria-label="调整片段列表和详情宽度" aria-valuemin="38" aria-valuemax="74" aria-valuenow="${Math.round(previewSplitRatio(scope))}" tabindex="0" title="拖动调整左右分栏"></div>
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
  setPreviewLayoutState("smart", preview);
  syncFlowActionState();
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
  bindPreviewSplitResizer(box, "smart");
  ensureInlinePreviewVideo("smart", previewDetailIndex("smart", clips));
}

function renderMixPreview(preview) {
  const box = $("mix-preview");
  const count = $("mix-preview-count");
  if (!box) return;
  const clips = preview?.clips || [];
  if (count) count.textContent = String(clips.length || 0);
  box.classList.toggle("empty", !clips.length);
  setPreviewLayoutState("mix", preview);
  syncFlowActionState();
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
  bindPreviewSplitResizer(box, "mix");
  ensureInlinePreviewVideo("mix", previewDetailIndex("mix", clips));
}

function renderClipMeta(clip, position, riskLabel) {
  const pieces = [
    `#${position + 1}`,
    `原序${Number(clip?.index || 0) + 1}`,
  ];
  if (clip?.focus_block && clip.focus_block !== clip.focus) pieces.push(`块:${clip.focus_block}`);
  if (clip?.source_name) pieces.push(`源:${clip.source_name}`);
  if (clip?.selected === false) pieces.push("已取消");
  if (riskLabel && riskLabel !== "正常") pieces.push(`风险:${riskLabel}`);
  return `<div class="clip-meta">${pieces.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>`;
}

function clipTextForScore(clip) {
  const selected = typeof selectedPreviewText === "function" ? selectedPreviewText(clip) : "";
  return `${clip?.clip_type || ""} ${clip?.focus || ""} ${clip?.focus_block || ""} ${selected || clip?.text || ""}`.toLowerCase();
}

const previewPreferenceBlocks = [
  "版型显瘦", "面料质感", "穿着体验", "品质细节", "尺寸长度", "颜色氛围",
  "场景搭配", "工艺细节", "性价比", "对比优势", "情绪感染", "流行趋势",
  "紧迫稀缺", "口感食欲", "新鲜品质", "产地溯源", "规格分量", "发货保鲜", "场景吃法",
];

const previewPreferenceAliases = {
  身材痛点: "版型显瘦",
  版型: "版型显瘦",
  显瘦: "版型显瘦",
  面料: "面料质感",
  质感: "面料质感",
  颜色: "颜色氛围",
  色彩: "颜色氛围",
  场景: "场景搭配",
  搭配: "场景搭配",
  工艺: "工艺细节",
  品质: "品质细节",
  情绪: "情绪感染",
  流行: "流行趋势",
  趋势: "流行趋势",
  紧迫: "紧迫稀缺",
  稀缺: "紧迫稀缺",
  对比: "对比优势",
  口感: "口感食欲",
  食欲: "口感食欲",
  新鲜: "新鲜品质",
  产地: "产地溯源",
  溯源: "产地溯源",
  规格: "规格分量",
  分量: "规格分量",
  发货: "发货保鲜",
  保鲜: "发货保鲜",
  吃法: "场景吃法",
};

function normalizePreviewPreferenceLabel(value) {
  const text = String(value || "").trim();
  if (!text || ["-", "自动", "默认", "随机偏好", "兜底偏好", "全量选片", "通用卖点", "其他"].includes(text)) return "";
  if (previewPreferenceAliases[text]) return previewPreferenceAliases[text];
  const direct = previewPreferenceBlocks.find((block) => text === block || text.includes(block) || block.includes(text));
  if (direct) return direct;
  const alias = Object.entries(previewPreferenceAliases).find(([key]) => text.includes(key));
  return alias ? alias[1] : "";
}

function previewPreferenceLabelFromSummary(summary = {}) {
  const candidates = [summary.used_label, summary.label, summary.matched_label, summary.detail];
  for (const value of candidates) {
    const label = normalizePreviewPreferenceLabel(value);
    if (label) return label;
  }
  return "";
}

const finalClipTopicRules = [
  ["版型显瘦", ["显瘦", "遮肉", "藏肉", "收腰", "显高", "显腿长", "比例", "版型", "廓形", "剪裁", "修身", "宽松", "遮胯", "遮肚", "肩宽", "胯宽", "小个子", "梨形", "拜拜肉", "盖臀", "盖胯"]],
  ["面料质感", ["面料", "材质", "莱赛尔", "天丝", "氨纶", "弹力", "聚酯纤维", "纯棉", "棉麻", "针织", "冰丝", "真丝", "垂感", "垂坠", "高织", "薄纱", "克重"]],
  ["穿着体验", ["舒服", "舒适", "亲肤", "柔软", "冰凉", "凉感", "裸肤", "裸感", "透气", "不闷", "不热", "不勒", "不卡", "不紧绷", "轻盈", "自在", "不透", "活动方便"]],
  ["品质细节", ["品质", "质感", "做工", "走线", "高级感", "精致", "质检", "不起球", "不褪色", "不变形", "色牢度"]],
  ["颜色氛围", ["颜色", "色系", "显白", "提亮", "气色", "肤色", "黄皮", "黑皮", "绿色", "白色", "黑色", "藏青", "藏蓝", "亮色", "彩色", "米白", "冷白", "复古色", "氛围感"]],
  ["场景搭配", ["通勤", "上班", "约会", "日常", "出门", "旅游", "度假", "放假", "聚会", "职场", "搭配", "内搭", "外穿", "单穿", "叠穿", "百搭", "拍照", "出片"]],
  ["尺寸长度", ["衣长", "袖长", "长度", "短款", "中长款", "盖住", "遮住", "到脚踝", "九分", "七分"]],
  ["工艺细节", ["工艺", "拼接", "包边", "锁边", "加固", "扣子", "纽扣", "亨利扣", "领口", "U领", "圆领", "V领", "口袋", "里衬", "定染", "固色"]],
  ["对比优势", ["买不到", "外面没有", "不一样", "独特", "独家", "全网无同款", "比外面", "比市面", "同品质", "没有第二家", "原创"]],
  ["口感食欲", ["好吃", "鲜甜", "脆甜", "爆汁", "多汁", "口感", "鲜嫩", "软糯", "酥脆", "Q弹", "试吃"]],
  ["新鲜品质", ["新鲜", "鲜活", "现摘", "现采", "现捕", "当天发", "鲜度", "饱满", "坏果包赔"]],
  ["产地溯源", ["产地", "原产地", "源头", "基地", "果园", "农场", "直采", "溯源", "产区"]],
  ["规格分量", ["规格", "净含量", "净重", "重量", "斤装", "箱装", "袋装", "盒装", "果径", "分量"]],
  ["发货保鲜", ["发货", "现发", "冷链", "冰袋", "保温箱", "保鲜", "锁鲜", "冷冻", "冷藏"]],
  ["场景吃法", ["早餐", "夜宵", "下午茶", "办公室", "全家", "聚餐", "煲汤", "下饭", "即食", "囤货", "送礼"]],
];

function classifyFinalClipTopic(clip) {
  const text = String(selectedPreviewText(clip) || "").replace(/\s+/g, "").toLowerCase();
  let bestTopic = "其他";
  let bestScore = 0;
  finalClipTopicRules.forEach(([topic, words]) => {
    let score = 0;
    words.forEach((word) => {
      let offset = 0;
      while (word && (offset = text.indexOf(String(word).toLowerCase(), offset)) >= 0) {
        score += String(word).length >= 3 ? 1.4 : 1;
        offset += String(word).length;
      }
    });
    if (score > bestScore) {
      bestTopic = topic;
      bestScore = score;
    }
  });
  return bestTopic;
}

function buildFinalTopicCoverageFromClips(clips = []) {
  const products = clips.filter((clip) => clipEligibleForPreference(clip));
  const topicCounts = {};
  const topicDurations = {};
  products.forEach((clip) => {
    const topic = classifyFinalClipTopic(clip);
    topicCounts[topic] = Number(topicCounts[topic] || 0) + 1;
    topicDurations[topic] = Number(topicDurations[topic] || 0) + effectiveClipDuration(clip);
  });
  const distinctTopics = Object.keys(topicCounts).filter((topic) => topic !== "其他" && Number(topicCounts[topic]) > 0);
  const minDistinct = products.length >= 5 ? 3 : products.length >= 3 ? 2 : products.length ? 1 : 0;
  return {
    source: "final_clips_client",
    product_count: products.length,
    topic_counts: topicCounts,
    topic_durations: Object.fromEntries(Object.entries(topicDurations).map(([key, value]) => [key, Number(value.toFixed(3))])),
    distinct_topics: distinctTopics,
    distinct_count: distinctTopics.length,
    min_distinct: minDistinct,
    undercovered: distinctTopics.length < minDistinct,
  };
}

function applyFinalPreferenceToCoverage(topicCoverage = {}, preferenceLabel = "") {
  const counts = topicCoverage.topic_counts || {};
  const durations = topicCoverage.topic_durations || {};
  const productCount = Number(topicCoverage.product_count || 0);
  const preferenceCount = Number(counts[preferenceLabel] || 0);
  const totalDuration = Object.values(durations).reduce((sum, value) => sum + Number(value || 0), 0);
  const preferenceDuration = Number(durations[preferenceLabel] || 0);
  return {
    ...topicCoverage,
    preferred_topic: preferenceLabel,
    preference_count: preferenceCount,
    preference_ratio: productCount ? preferenceCount / productCount : 0,
    preference_duration_ratio: totalDuration ? preferenceDuration / totalDuration : 0,
    overconcentrated: Boolean(productCount && preferenceCount / productCount > 0.55),
  };
}

function buildFinalPreferenceSummary(summary = {}, topicCoverage = {}) {
  const counts = topicCoverage.topic_counts || {};
  const durations = topicCoverage.topic_durations || {};
  const entries = Object.entries(counts)
    .filter(([name, count]) => name && name !== "其他" && Number(count) > 0)
    .sort((left, right) => {
      const countDiff = Number(right[1]) - Number(left[1]);
      if (countDiff) return countDiff;
      const durationDiff = Number(durations[right[0]] || 0) - Number(durations[left[0]] || 0);
      if (durationDiff) return durationDiff;
      return String(left[0]).localeCompare(String(right[0]), "zh-CN");
    });
  if (!entries.length) return summary || {};
  const actualLabel = entries[0][0];
  const previousLabel = summary.used_label || summary.label || "";
  return {
    ...(summary || {}),
    status: "final",
    mode: "最终片单统计",
    label: actualLabel,
    used_label: actualLabel,
    ai_selected_label: previousLabel && previousLabel !== actualLabel ? previousLabel : undefined,
    source: "final_clips",
    detail: `按最终保留片段统计，主线为${actualLabel}。`,
  };
}

function previewPreferenceLabel(analysis) {
  return previewPreferenceLabelFromSummary(analysis?.preferenceSummary || {});
}

function clipMatchesPreference(clip, preferenceLabel) {
  const preferred = normalizePreviewPreferenceLabel(preferenceLabel);
  if (!preferred) return false;
  const evidencePattern = previewPreferenceEvidencePatterns[preferred];
  if (evidencePattern) {
    return evidencePattern.test(selectedPreviewText(clip).toLowerCase());
  }
  const focus = normalizePreviewPreferenceLabel(clip?.focus_block || clip?.focus || "");
  if (focus === preferred) return true;
  return classifyClipScoreTags(clip)
    .some((tag) => normalizePreviewPreferenceLabel(tag.label) === preferred);
}

function clipEligibleForPreference(clip) {
  const type = String(clip?.clip_type || "").toLowerCase();
  return clip?.selected !== false && !type.includes("hook") && !type.includes("close") && type !== "call_to_action";
}

const previewSalesRoleLabels = {
  hook: "Hook开头",
  hook_followup: "承接Hook",
  direct_effect: "直接效果",
  proof_detail: "证明细节",
  scene_crowd: "场景人群",
  objection_resolver: "顾虑解除",
  natural_close: "自然收尾",
  weak_fragment: "弱断句",
  other: "补充卖点",
};

const previewPreferenceEvidencePatterns = {
  版型显瘦: /显瘦|遮肉|藏肉|收腰|显高|显腿长|比例|小个子|梨形|苹果型|胯宽|腿粗|大骨架|盖臀|修身|宽松|版型/,
  面料质感: /面料|材质|手感|触感|亲肤|柔软|垂感|垂坠|透气|冰丝|真丝|纯棉|棉麻|针织|不闷|不透|厚实|薄款/,
  颜色氛围: /颜色|色系|显白|提气色|抬气色|黄皮|黑色|白色|咖色|复古|高级色|温柔色|氛围感|上镜|亮色/,
  场景搭配: /通勤|上班|约会|日常|逛街|旅游|度假|聚会|职场|见家长|搭配|套穿|叠穿|内搭|外穿|成套|百搭|出门|出片|拍照/,
  穿着体验: /舒服|舒适|不勒|不紧绷|自在|轻盈|无感|不卡|不掉|不卷边|活动方便|不束缚|不扎人|凉爽|温暖/,
  品质细节: /品质|质感|做工|走线|细节|高级感|精致|缝合|刺绣|蕾丝|重工|大牌|专柜/,
};

function previewSalesRole(clip) {
  const explicit = String(clip?.sales_role || "").trim();
  if (explicit && !previewSegments(clip).length) return explicit;
  const type = String(clip?.clip_type || "").toLowerCase();
  if (type.includes("hook")) return "hook";
  if (type.includes("close") || type === "call_to_action") return "natural_close";
  const focus = normalizePreviewPreferenceLabel(clip?.focus_block || clip?.focus || "");
  const text = selectedPreviewText(clip).toLowerCase();
  if (/^(嗯+|啊+|好+|好的|是的|对|然后|而且|但是|不过|其实|就是)/.test(text) || /(然后|而且|但是|不过|所以|因为|就是|对不对|能理解吗|呢|吧|啊|呀)$/.test(text)) {
    if (text.length < 24) return "weak_fragment";
  }
  if (["版型显瘦", "穿着体验", "口感食欲"].includes(focus) || /显瘦|遮肉|显高|显腿长|上身|穿上|效果|显白|好吃|爆汁|口感|试吃/.test(text)) return "direct_effect";
  if (["面料质感", "品质细节", "工艺细节", "新鲜品质", "产地溯源", "规格分量", "发货保鲜"].includes(focus) || /面料|材质|质感|手感|做工|工艺|细节|品质|新鲜|产地|源头|规格|分量|冷链|包赔/.test(text)) return "proof_detail";
  if (["场景搭配", "场景吃法", "流行趋势"].includes(focus) || /通勤|上班|约会|日常|出门|旅游|搭配|出片|小个子|微胖|梨形|苹果型|全家|早餐|办公室|送礼/.test(text)) return "scene_crowd";
  if (["尺寸长度", "对比优势"].includes(focus) || /不挑|不用担心|不会|不显|不胖|不勒|不卡|不闷|不透|不起球|遮肚子|胯宽|腿粗|尺码|码数|身高|体重|放心|安心|不踩雷/.test(text)) return "objection_resolver";
  if (/推荐|建议|适合|放心|安心|闭眼|值得|自留|复购|老客/.test(text)) return "natural_close";
  return "other";
}

function previewSalesRoleLabel(clip) {
  return clip?.sales_role_label || previewSalesRoleLabels[previewSalesRole(clip)] || "补充卖点";
}

function buildSalesChainSummary(clips = []) {
  const roles = clips.map((clip) => previewSalesRole(clip));
  const has = (names) => names.some((name) => roles.includes(name));
  const slots = [
    { key: "hook", label: "Hook", ok: has(["hook"]) },
    { key: "effect", label: "承接/效果", ok: has(["hook_followup", "direct_effect"]) },
    { key: "proof", label: "证明", ok: has(["proof_detail"]) },
    { key: "resolve", label: "顾虑/场景", ok: has(["objection_resolver", "scene_crowd"]) },
    { key: "close", label: "收尾", ok: has(["natural_close"]) },
  ];
  const hit = slots.filter((slot) => slot.ok).length;
  const missing = slots.filter((slot) => !slot.ok).map((slot) => slot.label);
  return {
    hit,
    total: slots.length,
    label: `${hit}/${slots.length}`,
    ok: hit >= 4,
    title: missing.length ? `缺少：${missing.join("、")}` : "Hook、承接、证明、顾虑/场景、收尾均有覆盖",
  };
}

function classifyClipScoreTags(clip, analysis = null) {
  const text = clipTextForScore(clip);
  const tags = [];
  const seen = new Set();
  const add = (label, tone = "info", detail = "") => {
    if (!label || seen.has(label)) return;
    seen.add(label);
    tags.push({ label, tone, detail: detail || label });
  };
  const type = String(clip?.clip_type || "").toLowerCase();
  const focus = String(clip?.focus_block || clip?.focus || "").trim();

  if (type === "hook" || /hook|爆点|痛点|开头|第一眼|有没有发现|姐妹/.test(text)) {
    add("开头候选", "strong", "可能承担开头吸引作用");
  } else if (type === "close" || type === "call_to_action" || /收尾|尺码引导|放心拍|建议大家|喜欢的/.test(text)) {
    add("收尾候选", "neutral", "可能承担结尾承接或转化作用");
  }

  const salesRole = previewSalesRole(clip);
  const salesLabel = previewSalesRoleLabel(clip);
  if (salesLabel && salesRole !== "other") {
    const tone = salesRole === "weak_fragment" ? "warn" : (salesRole === "hook_followup" ? "strong" : "info");
    add(salesLabel, tone, `成交链路角色：${salesLabel}`);
  }

  if (focus && focus !== "其他") {
    add(focus, "good", `AI 标注卖点：${focus}`);
  }

  const focusRules = [
    ["版型显瘦", /显瘦|遮肉|藏肉|收腰|显高|显腿长|比例|小个子|梨形|苹果型|胯宽|腿粗|大骨架|盖臀|修身|宽松|版型/, "修饰身材或版型效果"],
    ["面料质感", /面料|材质|手感|触感|亲肤|柔软|垂感|垂坠|透气|冰丝|真丝|纯棉|棉麻|针织|不闷|不透|厚实|薄款/, "面料、触感或穿着质感"],
    ["颜色氛围", /颜色|色系|显白|提气色|抬气色|黄皮|黑色|白色|咖色|复古|高级色|温柔色|氛围感|上镜/, "颜色、肤色或视觉氛围"],
    ["场景搭配", /通勤|上班|约会|日常|逛街|旅游|度假|聚会|职场|见家长|搭配|套穿|叠穿|内搭|外穿|成套|百搭/, "穿着场景或搭配建议"],
    ["穿着体验", /舒服|舒适|不勒|不紧绷|自在|轻盈|无感|不卡|不掉|不卷边|活动方便|不束缚|不扎人|凉爽|温暖/, "穿着感受或活动体验"],
    ["品质细节", /品质|质感|做工|走线|细节|高级感|精致|缝合|刺绣|蕾丝|重工|大牌|专柜/, "品质背书或细节描述"],
    ["尺寸长度", /裙长|衣长|袖长|长度|九分|七分|短款|中长款|过膝|不过膝|露脚踝|遮小腿|盖住|刚好/, "长度、比例或遮盖位置"],
    ["工艺细节", /工艺|成本|拼接|剪裁|立体|定型|压褶|包边|锁边|加固|五金|拉链|扣子|里衬|固色/, "工艺结构或制作细节"],
    ["对比优势", /买不到|外面没有|不一样|区别|独特|独家|同价位|同品质|比外面|比商场|没有第二家|源头/, "对比、稀缺或差异化"],
    ["情绪感染", /绝了|太漂亮|太好看|美爆|太爱|神仙|封神|超级|天呐|妈呀|信我|相信我|真心|自留|美哭|疯了/, "主播情绪或强推荐语气"],
    ["流行趋势", /流行|当季|新款|原创|不撞款|爆款|热门|趋势|法式|新中式|设计师|小众|时髦|松弛感|多巴胺|复古|国风/, "流行趋势或风格标签"],
    ["紧迫稀缺", /限量|限时|手慢无|秒空|断码|断货|补不到|不补货|最后|错过|下架|余量|稀缺|卖完/, "紧迫或稀缺表达"],
    ["口感食欲", /好吃|鲜甜|脆甜|爆汁|多汁|汁水|入口|口感|鲜嫩|软糯|酥脆|q弹|弹牙|拉丝|试吃|咬一口/, "试吃、口感或食欲画面"],
    ["新鲜品质", /新鲜|鲜活|现摘|现采|现捕|现捞|当天发|鲜度|品质|果形|果径|个头|饱满|坏果包赔|基地|果园/, "新鲜度、品质或售后信任"],
    ["产地溯源", /产地|原产地|源头|基地|果园|农场|牧场|渔港|海捕|直采|直发|溯源|农户|合作社|当季|应季/, "产地、源头或供应链背书"],
    ["规格分量", /规格|净含量|净重|克重|重量|斤装|箱装|袋装|盒装|整箱|大果|中果|果径|个头|份量|分量/, "规格、重量或分量展示"],
    ["发货保鲜", /发货|现发|冷链|冰袋|保温箱|泡沫箱|顺丰|次日达|保鲜|锁鲜|冷冻|速冻|冷藏|破损包赔/, "发货、物流或保鲜保障"],
    ["场景吃法", /早餐|夜宵|下午茶|办公室|孩子|老人|全家|聚餐|火锅|烧烤|煲汤|下饭|拌饭|空气炸锅|即食|囤货|送礼/, "食用场景或吃法建议"],
  ];
  focusRules.forEach(([label, pattern, detail]) => {
    if (label !== focus && pattern.test(text)) add(label, "good", detail);
  });

  if (/尺码|码数|s码|m码|l码|xl|xxl|身高|体重|小码|中码|大码|正码|拍大|拍小|卡码/.test(text)) {
    add("尺码信息", "neutral", "包含尺码、身高或体重信息");
  }
  if (/(\d+(\.\d+)?\s*(元|块|¥|￥))|价格|福利价|到手价|原价|现价|优惠|券|领券|满减|半价|折扣|\d+\s*折/.test(text)) {
    add("疑似价格", "warn", "可能包含价格、折扣或优惠表达");
  }
  if (/3\s*2\s*1|三\s*二\s*一|拍下|下单|小黄车|购物车|链接|连结|連結|号链接|上车|挂车|库存|补货|刷新拍/.test(text)) {
    add("疑似下单", "warn", "可能包含下单、链接或库存信息");
  }
  if (/感谢|反馈|评论区|公屏|扣[0-9一二三四五六七八九十]|后台|稍等|等一下|看一下后台|有没有码|欢迎|关注/.test(text)) {
    add("互动废话", "warn", "可能是直播互动、后台或评论区内容");
  }
  if (/^(嗯+|啊+|好+|对+|是的|然后|这个的话|就是说|那个的话)|然后呢|然后的话|对吧|哈哈|闲聊/.test(text)) {
    add("口头废话", "warn", "可能是口头禅或承接废话");
  }

  const preferred = previewPreferenceLabel(analysis);
  if (preferred && clipEligibleForPreference(clip)) {
    const hit = seen.has(preferred) || normalizePreviewPreferenceLabel(focus) === preferred;
    tags.unshift(hit
      ? { label: `偏好命中:${preferred}`, tone: "strong", detail: `该片段命中本次 AI 偏好：${preferred}` }
      : { label: `偏好未命中:${preferred}`, tone: "warn", detail: `该片段未识别到本次 AI 偏好：${preferred}` });
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
  const clientTopicCoverage = buildFinalTopicCoverageFromClips(clips);
  let topicCoverage = clientTopicCoverage.product_count
    ? clientTopicCoverage
    : (dedupSummary.topic_coverage_summary || {});
  const preferenceSummary = buildFinalPreferenceSummary(dedupSummary.preference_summary || {}, topicCoverage);
  const preferenceLabel = previewPreferenceLabelFromSummary(preferenceSummary);
  topicCoverage = applyFinalPreferenceToCoverage(topicCoverage, preferenceLabel);
  const target = Number(preview?.target_duration || $(targetId)?.value || 60);
  const durationSpeedFactor = Math.max(0.1, Number(dedupSummary.duration_speed_factor || preview?.duration_speed_factor || 1) || 1);
  const rawTotal = clips.reduce((sum, clip) => sum + effectiveClipDuration(clip), 0);
  const total = rawTotal / durationSpeedFactor;
  const riskByIndex = new Map();
  const warnings = [];
  const preferenceEligibleClips = preferenceLabel ? clips.filter((clip) => clipEligibleForPreference(clip)) : [];
  const preferenceHitCount = preferenceEligibleClips.filter((clip) => clipMatchesPreference(clip, preferenceLabel)).length;
  const salesChain = buildSalesChainSummary(clips);
  if (topicCoverage.overconcentrated) {
    warnings.push(`偏好主题占比过高：${Number(topicCoverage.preference_count || 0)}/${Number(topicCoverage.product_count || 0)}，需要补充其他卖点。`);
  }
  if (topicCoverage.undercovered) {
    warnings.push(`商品主题覆盖不足：当前${Number(topicCoverage.distinct_count || 0)}类，建议至少${Number(topicCoverage.min_distinct || 0)}类。`);
  }

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
    rawTotal,
    durationSpeedFactor,
    diff,
    status,
    statusText,
    riskByIndex,
    riskCount: Array.from(riskByIndex.values()).filter((item) => item.level !== "ok").length,
    warnings: Array.from(new Set(warnings)).slice(0, 3),
    autoRemovedCount: Number(dedupSummary.auto_removed_count || 0),
    manualCheckCount: Number(dedupSummary.manual_check_count || 0),
    categorySummary: dedupSummary.category_summary || {},
    preferenceSummary,
    preferenceLabel,
    preferenceEligibleCount: preferenceEligibleClips.length,
    preferenceHitCount,
    topicCoverage,
    salesChain,
  };
}

function renderPreviewSummary(analysis) {
  const diffText = analysis.diff >= 0 ? `+${analysis.diff.toFixed(1)}s` : `${analysis.diff.toFixed(1)}s`;
  const category = analysis.categorySummary || {};
  const preference = analysis.preferenceSummary || {};
  const topicCoverage = analysis.topicCoverage || {};
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
  const preferenceText = preference.used_label
    || preference.label
    || (preference.status === "missing" ? "未生成" : "-");
  const topicCounts = topicCoverage.topic_counts || {};
  const preferenceProductCount = Number(topicCoverage.product_count || analysis.preferenceEligibleCount || 0);
  const preferenceCount = Number(
    analysis.preferenceLabel
      ? (topicCounts[analysis.preferenceLabel] ?? topicCoverage.preference_count ?? analysis.preferenceHitCount ?? 0)
      : 0
  );
  const preferenceRatio = preferenceProductCount > 0 ? preferenceCount / preferenceProductCount : 0;
  const preferenceHitText = analysis.preferenceLabel
    ? `${preferenceCount}/${preferenceProductCount} · ${(preferenceRatio * 100).toFixed(0)}%`
    : (preference.status === "missing" ? "未计算" : "-");
  const preferenceHitWarn = Boolean(
    analysis.preferenceLabel
    && preferenceProductCount > 0
    && (preferenceCount === 0 || topicCoverage.overconcentrated || topicCoverage.underpreferred)
  );
  const preferenceHitTitle = analysis.preferenceLabel
    ? `AI偏好：${analysis.preferenceLabel}；最终Product命中：${preferenceCount}/${preferenceProductCount}`
    : "未识别到AI偏好";
  const topicEntries = Object.entries(topicCounts).filter(([name, count]) => name && name !== "其他" && Number(count) > 0);
  const topicCoverageText = topicEntries.length ? `${topicEntries.length}类` : "-";
  const topicCoverageTitle = topicEntries.length
    ? topicEntries.map(([name, count]) => `${name}${count}段`).join("、")
    : "未计算商品主题覆盖";
  const topicCoverageWarn = Boolean(topicCoverage.undercovered || topicCoverage.overconcentrated);
  const salesChain = analysis.salesChain || { label: "-", ok: false, title: "未计算成交结构" };
  const preferenceTitleParts = [];
  if (preference.mode) preferenceTitleParts.push(preference.mode);
  if (preference.matched_label && preference.matched_label !== preferenceText) {
    preferenceTitleParts.push(`命中：${preference.matched_label}`);
  }
  if (preference.score !== undefined && preference.score !== null && preference.score !== "") {
    preferenceTitleParts.push(`命中强度：${preference.score}`);
  }
  if (preference.detail) preferenceTitleParts.push(preference.detail);
  if (preference.error) preferenceTitleParts.push(`异常：${preference.error}`);
  const preferenceTitle = preferenceTitleParts.join("；") || preferenceText;
  const warningText = analysis.warnings.length
    ? `<div class="preview-warnings">${analysis.warnings.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>`
    : `<div class="preview-warnings is-ok"><span>未发现明显时间戳风险。</span></div>`;
  const totalTitle = Math.abs(Number(analysis.durationSpeedFactor || 1) - 1) > 0.01
    ? `原片合计 ${Number(analysis.rawTotal || 0).toFixed(1)}s，按预计变速 ${Number(analysis.durationSpeedFactor || 1).toFixed(2)}x 折算`
    : "按选中片段时长统计";
  return `
    <div class="clip-preview-summary">
      <div><span>已选片段</span><strong>${analysis.clips.length}</strong></div>
      <div><span>预计成片</span><strong title="${escapeHtml(totalTitle)}">${analysis.total.toFixed(1)}s</strong></div>
      <div><span>目标差值</span><strong class="is-${analysis.status}">${diffText}</strong></div>
      <div><span>已自动处理</span><strong class="is-${analysis.autoRemovedCount ? "warn" : "ok"}">${analysis.autoRemovedCount ? `${analysis.autoRemovedCount} 段` : "无"}</strong></div>
      <div><span>人工检查</span><strong class="is-${analysis.manualCheckCount ? "warn" : "ok"}">${analysis.manualCheckCount ? `${analysis.manualCheckCount} 组` : "无"}</strong></div>
      <div><span>品类</span><strong title="${escapeHtml(categoryTitle)}">${escapeHtml(categoryText)}</strong></div>
      <div><span>AI偏好</span><strong title="${escapeHtml(preferenceTitle)}">${escapeHtml(preferenceText)}</strong></div>
      <div><span>偏好占比</span><strong class="is-${preferenceHitWarn ? "warn" : "ok"}" title="${escapeHtml(preferenceHitTitle)}">${escapeHtml(preferenceHitText)}</strong></div>
      <div><span>主题覆盖</span><strong class="is-${topicCoverageWarn ? "warn" : "ok"}" title="${escapeHtml(topicCoverageTitle)}">${escapeHtml(topicCoverageText)}</strong></div>
      <div><span>成交结构</span><strong class="is-${salesChain.ok ? "ok" : "warn"}" title="${escapeHtml(salesChain.title)}">${escapeHtml(salesChain.label)}</strong></div>
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
  const batch = batchProgressFromTask(task);
  const batchChip = batch
    ? `<span class="task-batch-chip" title="${escapeHtml(batch.title)}">${escapeHtml(batch.shortText || batch.text)}</span>`
    : "";
  const outputs = Array.isArray(task.outputs) ? task.outputs.filter(Boolean) : [];
  const output = outputs[0] || task.output || "";
  const outputLabel = outputs.length > 1 ? `打开结果(${outputs.length})` : "打开结果";
  const outputAction = task.status === "completed" && output
    ? `<button type="button" class="task-output-button" data-action="open-task-output" data-path="${escapeHtml(output)}" title="${escapeHtml(output)}">${outputLabel}</button>`
    : "";
  const title = [task.error, batch?.title, output].filter(Boolean).join("\n");
  return `<span class="${cls}" title="${escapeHtml(title)}"><span class="task-pill-text">${text}</span>${batchChip}${outputAction}</span>`;
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
      dedup_preset: normalizeDedupPresetValue($("mix-dedup").value),
      mirror_enabled: $("mix-mirror").checked,
      subtitle_overlay: $("mix-subtitle").checked,
      smart_crop_enabled: $("mix-crop").checked,
      crop_level: $("mix-crop-level").value,
      ken_burns_enabled: $("mix-kenburns").checked,
      ken_burns_intensity: $("mix-kb-intensity").value,
      ...collectDedupCustomPayload("mix"),
      ...collectTransitionPayload("mix"),
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

  if (feature === "video-split") {
    syncVideoSplitMode();
    return {
      video_paths: getLines("vs-video-paths"),
      output_dir: $("vs-output-dir").value.trim(),
      mode: $("vs-split-mode")?.value || "count",
      segment_count: Number($("vs-segment-count")?.value || 2),
      segment_seconds: Number($("vs-segment-seconds")?.value || 60),
      overrides: collectVideoSplitOverrides(),
    };
  }

  if (feature === "dedup") {
    const videoPaths = getLines("dedup-video-paths");
    return {
      video_path: videoPaths[0] || "",
      video_paths: videoPaths,
      output_dir: $("dedup-output-dir").value.trim(),
      dedup_preset: normalizeDedupPresetValue($("dedup-preset").value),
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
      min_stream_quality: $("live-min-stream-quality")?.value || "",
      room_name: $("live-room-name").value.trim(),
      room_url: $("live-room-url").value.trim(),
      platform: $("live-platform").value,
      product_split_enabled: $("live-product-split-enabled")?.checked || false,
      product_auto_cut: $("live-product-auto-cut")?.checked || false,
      product_naming_mode: "product_id",
      product_default_minutes: Math.min(15, Number($("live-product-default-minutes")?.value || 10)),
      product_min_minutes: Math.min(15, Number($("live-product-min-minutes")?.value || 3)),
      product_max_minutes: Math.min(15, Number($("live-product-max-minutes")?.value || 15)),
      product_switch_confirm_seconds: Number($("live-product-switch-confirm")?.value || 8),
      product_head_seconds: Number($("live-product-head-seconds")?.value || 10),
      product_tail_seconds: Number($("live-product-tail-seconds")?.value || 20),
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

function normalizeLiveRoom(room = {}) {
  const name = String(room.name || "").trim();
  const url = String(room.url || "").trim();
  const platform = String(room.platform || "抖音").trim() || "抖音";
  const product_naming_mode = liveNormalizeNamingMode(room.product_naming_mode || room.productNamingMode || room.naming_mode);
  if (!name || !url) return null;
  return { name, url, platform, product_naming_mode };
}

function normalizeLiveRooms(rooms = []) {
  const normalized = [];
  const seen = new Set();
  for (const raw of Array.isArray(rooms) ? rooms : []) {
    const room = normalizeLiveRoom(raw);
    if (!room) continue;
    const key = `${room.platform.toLowerCase()}|${room.url.toLowerCase()}`;
    if (seen.has(key)) continue;
    seen.add(key);
    normalized.push(room);
  }
  return normalized;
}

function liveNormalizeNamingMode(value) {
  return String(value || "").trim() === "product_name" ? "product_name" : "product_id";
}

function liveNamingModeLabel(value) {
  return liveNormalizeNamingMode(value) === "product_name" ? "商品名称命名" : "商品ID命名";
}

function loadLiveRooms() {
  let localRooms = [];
  try {
    const raw = localStorage.getItem(liveRoomsStorageKey);
    const rooms = JSON.parse(raw || "[]");
    localRooms = normalizeLiveRooms(rooms);
  } catch {
    localRooms = [];
  }
  state.liveRooms = localRooms;
  renderLiveRooms();
  loadLiveRoomsFromServer(Boolean(localRooms.length));
}

async function loadLiveRoomsFromServer(hasLocalRooms = false) {
  try {
    const result = await api("/api/live-rec/rooms");
    const remoteRooms = normalizeLiveRooms(result.rooms || []);
    if (!hasLocalRooms && remoteRooms.length) {
      state.liveRooms = remoteRooms;
      persistLiveRoomsLocal();
      renderLiveRooms();
      return;
    }
    if (hasLocalRooms) {
      saveLiveRoomsToServer();
    }
  } catch (error) {
    console.warn("Failed to load live room cache", error);
  }
}

function saveLiveRooms() {
  persistLiveRoomsLocal();
  saveLiveRoomsToServer();
}

function persistLiveRoomsLocal() {
  try {
    localStorage.setItem(liveRoomsStorageKey, JSON.stringify(state.liveRooms || []));
  } catch {
    // Local storage can be unavailable in restricted browser contexts.
  }
}

async function saveLiveRoomsToServer() {
  try {
    await api("/api/live-rec/rooms", {
      method: "POST",
      body: JSON.stringify({ rooms: normalizeLiveRooms(state.liveRooms || []) }),
    });
  } catch (error) {
    console.warn("Failed to save live room cache", error);
  }
}

function liveRoomsFromTable() {
  const cards = Array.from(document.querySelectorAll("#page-live-rec .live-room-card"));
  return cards.map((card) => {
    return normalizeLiveRoom({
      name: card.dataset.name || "",
      platform: card.dataset.platform || "",
      url: card.dataset.url || "",
      product_naming_mode: card.querySelector("[data-live-naming-mode]")?.value || card.dataset.productNamingMode || "product_id",
    });
  }).filter(Boolean);
}

function liveRoomKey(room) {
  return `${room?.platform || ""}|${room?.url || ""}`;
}

function liveStatusBadge(label, tone = "") {
  return `<span class="live-status ${tone ? `is-${tone}` : ""}">${escapeHtml(label)}</span>`;
}

function renderLiveProductResult(index, activity = {}) {
  const outputs = Array.isArray(activity.productOutputs) ? activity.productOutputs : [];
  const outputCount = Number(activity.productOutputCount || outputs.length || 0);
  const segmentCount = Number(activity.productSegments || 0);
  const boundCount = Number(activity.productBoundSegments || 0);
  const pendingCount = Number(activity.productPendingSegments || 0);
  const activeCandidateCount = Number(activity.productActiveCandidateCount || 0);
  const candidateSignals = Number(activity.productCandidateSignals || 0);
  const ruleReviewCount = Number(activity.productRuleReviewCount || 0);
  const strongSignalCount = Number(activity.productStrongSignalCount || 0);
  const recordingReturncode = activity.recordingReturncode;
  const clipsDir = String(activity.productClipsDir || "").trim();
  if (!outputCount && !segmentCount && !clipsDir && !activity.productSplitQueue && !candidateSignals && !activeCandidateCount) return "";
  const title = outputCount > 0 ? `已提取 ${outputCount} 个单品` : `已生成 ${segmentCount || pendingCount || 0} 个候选段`;
  const fileRows = outputs.slice(0, 3).map((path) => `
    <span class="live-product-file" title="${escapeHtml(path)}">${escapeHtml(livePathName(path))}</span>
  `).join("");
  const moreText = outputs.length > 3 ? `<span class="live-product-more">还有 ${outputs.length - 3} 个</span>` : "";
  const reviewText = pendingCount > 0 ? `<span class="live-product-chip is-warn">待确认 ${pendingCount}</span>` : "";
  const boundText = boundCount > 0 ? `<span class="live-product-chip is-ok">已绑定 ${boundCount}</span>` : "";
  const candidateText = activeCandidateCount > 0 ? `<span class="live-product-chip is-warn">商品候选 ${activeCandidateCount}</span>` : "";
  const signalText = candidateSignals > 0 ? `<span class="live-product-chip">候选信号 ${candidateSignals}</span>` : "";
  const strongText = strongSignalCount > 0 ? `<span class="live-product-chip is-ok">强信号 ${strongSignalCount}</span>` : "";
  const reviewRuleText = ruleReviewCount > 0 ? `<span class="live-product-chip is-warn">需复核 ${ruleReviewCount}</span>` : "";
  const recordingIssueText = recordingReturncode !== undefined && recordingReturncode !== null && Number(recordingReturncode) !== 0
    ? `<span class="live-product-chip is-warn">录制异常 ${escapeHtml(recordingReturncode)}</span>`
    : "";
  return `
    <div class="live-product-result">
      <div class="live-product-result-head">
        <strong>${escapeHtml(title)}</strong>
        <button class="button button-muted button-small" data-action="live-open-product-dir" data-index="${index}" ${clipsDir ? "" : "disabled"}>
          <span class="button-icon" aria-hidden="true">■</span><span>单品目录</span>
        </button>
      </div>
      <div class="live-product-chips">
        ${boundText}
        ${reviewText}
        ${candidateText}
        ${signalText}
        ${strongText}
        ${reviewRuleText}
        ${recordingIssueText}
        ${segmentCount ? `<span class="live-product-chip">时间线 ${segmentCount}</span>` : ""}
        ${activity.rollingCycleCount ? `<span class="live-product-chip">录制段 ${Number(activity.rollingCycleCount)}</span>` : ""}
      </div>
      ${fileRows ? `<div class="live-product-files">${fileRows}${moreText}</div>` : ""}
      ${clipsDir ? `<div class="live-product-dir" title="${escapeHtml(clipsDir)}">${escapeHtml(clipsDir)}</div>` : ""}
    </div>
  `;
}

function liveRoomActivity(room) {
  return state.liveRoomActivity[liveRoomKey(room)] || {};
}

function isDouyinLiveRoom(room) {
  return String(room?.platform || "").includes("抖音") || String(room?.url || "").toLowerCase().includes("douyin.com");
}

function liveMinStreamQualityLabel() {
  const select = $("live-min-stream-quality");
  return select?.selectedOptions?.[0]?.textContent?.trim() || "自动最高";
}

function normalizeLiveStreamQuality(value, fallback = "") {
  let text = repairMojibakeText(String(value || "").trim());
  if (!text) return fallback;
  const compact = text.replace(/\s+/g, "").toLowerCase();
  if (/(2160|1440|4k|uhd|原画|原畫|origin|source)/.test(compact)) return "原画";
  if (/(1080|full[_-]?hd|fhd|蓝光|藍光|超清)/.test(compact)) return "1080p";
  if (/(720|hd|高清)/.test(compact)) return "720p";
  if (/(540|480|sd|标清|標清)/.test(compact)) return "480p";
  if (/(360|ld|low|低清)/.test(compact)) return "低清";
  if (/(未知|unknown)/.test(compact)) return "未知清晰度";
  if (/(自动|auto)/.test(compact)) return "自动最高";
  if (looksGarbledText(text) || /\?{2,}/.test(text)) return fallback || "未知清晰度";
  return text;
}

function setLiveRoomActivity(room, updates = {}) {
  const key = liveRoomKey(room);
  state.liveRoomActivity[key] = { ...(state.liveRoomActivity[key] || {}), ...updates };
  renderLiveRooms();
}

function setLiveRoomFilter(status) {
  state.liveRoomFilter = status || "all";
  document.querySelectorAll(".live-filter-chip").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.status === state.liveRoomFilter);
  });
  renderLiveRooms();
}

function liveRoomDirectory(room) {
  const saveDir = $("live-save-dir")?.value.trim();
  if (!saveDir || !room?.name) return saveDir || "";
  return `${saveDir.replace(/[\\\/]+$/, "")}\\${room.name}`;
}

function liveRoomMatchesFilter(room, activity) {
  const search = String(state.liveRoomSearch || "").trim().toLowerCase();
  if (search) {
    const haystack = `${room.name} ${room.platform} ${room.url}`.toLowerCase();
    if (!haystack.includes(search)) return false;
  }
  const platform = state.liveRoomPlatform || "all";
  if (platform !== "all" && room.platform !== platform) return false;

  const filter = state.liveRoomFilter || "all";
  const liveStatus = activity.liveStatus || "未检测";
  const recordStatus = activity.recordStatus || "待录制";
  const productStatus = activity.productStatus || "";
  if (filter === "recording") return recordStatus.includes("录制中");
  if (filter === "live") return liveStatus.includes("直播中");
  if (filter === "not_started") return liveStatus.includes("未") || liveStatus.includes("待");
  if (filter === "error") return recordStatus.includes("失败") || productStatus.includes("错误");
  if (filter === "unmonitored") return recordStatus.includes("待录制") || productStatus.includes("未启用");
  return true;
}

function liveRoomStatusClass(activity) {
  const recordStatus = activity.recordStatus || "待录制";
  if (recordStatus.includes("录制中")) return "is-recording";
  if (recordStatus.includes("失败")) return "is-error";
  return "";
}

function renderLiveRooms() {
  const grid = document.querySelector("#live-room-grid");
  if (!grid) return;
  grid.innerHTML = "";
  const rooms = state.liveRooms || [];
  const visibleRooms = rooms
    .map((room, index) => ({ room, index, activity: liveRoomActivity(room) }))
    .filter((item) => liveRoomMatchesFilter(item.room, item.activity));
  if (!rooms.length || !visibleRooms.length) {
    const empty = document.createElement("div");
    empty.className = "live-room-empty";
    empty.textContent = rooms.length ? "没有符合筛选条件的直播间" : "暂无直播间";
    grid.appendChild(empty);
    return;
  }
  visibleRooms.forEach(({ room, index, activity }) => {
    const productEnabled = $("live-product-split-enabled")?.checked !== false && isDouyinLiveRoom(room);
    const liveStatus = activity.liveStatus || "未检测";
    const recordStatus = activity.recordStatus || "待录制";
    const productStatus = activity.productStatus || (productEnabled ? "待监控" : "未启用");
    const qualityLabel = normalizeLiveStreamQuality(activity.streamQuality, liveMinStreamQualityLabel());
    const liveTone = liveStatus.includes("中") || liveStatus.includes("已") ? "ok" : "";
    const recordTone = recordStatus.includes("录制中") ? "run" : recordStatus.includes("失败") ? "danger" : recordStatus.includes("完成") ? "ok" : "";
    const productTone = productStatus.includes("监控") ? "run" : productStatus.includes("已识别") || productStatus.includes("捕获") ? "ok" : productStatus.includes("候选") || productStatus.includes("待确认") || productStatus.includes("未捕获") ? "warn" : "";
    const card = document.createElement("article");
    card.className = `live-room-card ${liveRoomStatusClass(activity)}`;
    card.dataset.name = room.name;
    card.dataset.platform = room.platform;
    card.dataset.url = room.url;
    card.dataset.productNamingMode = liveNormalizeNamingMode(room.product_naming_mode);
    const namingMode = liveNormalizeNamingMode(room.product_naming_mode);
    card.innerHTML = `
      <div class="live-card-top">
        <div class="live-card-title">
          <span class="live-platform-mark">${escapeHtml(room.platform.slice(0, 2) || "直")}</span>
          <strong>${escapeHtml(room.name)}</strong>
        </div>
        ${liveStatusBadge(recordStatus, recordTone)}
      </div>
      <div class="live-card-meta">
        <div>
          <span>直播状态</span>
          <strong>${liveStatusBadge(liveStatus, liveTone)}</strong>
        </div>
        <div>
          <span>商品识别</span>
          <strong>${liveStatusBadge(productStatus, productTone)}</strong>
        </div>
        <div>
          <span>录制画质</span>
          <strong>${escapeHtml(qualityLabel)}</strong>
        </div>
        <div>
          <span>录制时长</span>
          <strong>${escapeHtml(activity.duration || "0:00:00")}</strong>
        </div>
      </div>
      <div class="live-card-url" title="${escapeHtml(room.url)}">${escapeHtml(room.url)}</div>
      ${renderLiveProductResult(index, activity)}
      <div class="live-card-control-row">
        <div class="live-card-config">
          <label>
            <span>命名方式</span>
            <select data-live-naming-mode data-index="${index}">
              <option value="product_id" ${namingMode === "product_id" ? "selected" : ""}>商品ID命名</option>
              <option value="product_name" ${namingMode === "product_name" ? "selected" : ""}>商品名称命名</option>
            </select>
          </label>
        </div>
        <div class="live-card-actions">
          <button class="button button-secondary button-small" data-action="record-live-room" data-index="${index}"><span class="button-icon" aria-hidden="true">◎</span><span>监控</span></button>
          <button class="button button-muted button-small" data-action="live-open-room-dir" data-index="${index}"><span class="button-icon" aria-hidden="true">■</span><span>目录</span></button>
          <button class="button button-danger button-small" data-action="live-stop-room" data-index="${index}"><span class="button-icon" aria-hidden="true">■</span><span>停止</span></button>
          <button class="button button-muted button-small" data-action="live-detail-room" data-index="${index}"><span class="button-icon" aria-hidden="true">i</span><span>详情</span></button>
          <button class="button button-muted button-small" data-action="remove-live-room" data-index="${index}"><span class="button-icon" aria-hidden="true">⌫</span><span>删除</span></button>
        </div>
      </div>`;
    grid.appendChild(card);
  });
}

function addLiveRoom() {
  const name = $("live-room-name").value.trim();
  const url = $("live-room-url").value.trim();
  const platform = $("live-platform").value;
  if (!name || !url) {
    toast("请先填写直播间名称和地址", "warning");
    return;
  }
  const room = normalizeLiveRoom({ name, url, platform, product_naming_mode: "product_id" });
  const exists = state.liveRooms.some((item) => item.url === room.url);
  if (exists) {
    toast("这个直播间已经在列表里", "warning");
    return;
  }
  state.liveRooms.push(room);
  saveLiveRooms();
  renderLiveRooms();
  setLiveRecTab("rooms");
  appendLog("live-rec", { time: new Date().toLocaleTimeString(), level: "info", message: `已添加直播间: ${name}。点击开始录制会按列表逐个启动。` });
}

function removeLiveRoom(index) {
  if (!Number.isInteger(index) || index < 0 || index >= state.liveRooms.length) return;
  const [room] = state.liveRooms.splice(index, 1);
  if (room) delete state.liveRoomActivity[liveRoomKey(room)];
  saveLiveRooms();
  renderLiveRooms();
  if (room) appendLog("live-rec", { time: new Date().toLocaleTimeString(), level: "info", message: `已移除直播间: ${room.name}` });
}

function clearLiveRooms() {
  if (!state.liveRooms.length) {
    toast("当前没有直播间", "warning");
    return;
  }
  if (!window.confirm("确定删除当前列表里的所有直播间？正在运行的录制请先停止。")) return;
  state.liveRooms = [];
  state.liveRoomActivity = {};
  saveLiveRooms();
  renderLiveRooms();
  appendLog("live-rec", { time: new Date().toLocaleTimeString(), level: "warning", message: "已清空直播间列表。" });
}

function updateLiveRoomNamingMode(index, value) {
  if (!Number.isInteger(index) || index < 0 || index >= state.liveRooms.length) return;
  state.liveRooms[index] = normalizeLiveRoom({
    ...state.liveRooms[index],
    product_naming_mode: liveNormalizeNamingMode(value),
  }) || state.liveRooms[index];
  saveLiveRooms();
  renderLiveRooms();
}

function fillLiveRoom(index) {
  const room = state.liveRooms[index];
  if (!room) return;
  $("live-room-name").value = room.name;
  $("live-room-url").value = room.url;
  $("live-platform").value = room.platform;
  setLiveRecTab("add");
}

async function openLiveRoomDirectory(index) {
  const room = state.liveRooms[index];
  if (!room) return;
  const activity = liveRoomActivity(room);
  await openPathValue(activity.file || liveRoomDirectory(room));
}

async function openLiveProductDirectory(index) {
  const room = state.liveRooms[index];
  if (!room) return;
  const activity = liveRoomActivity(room);
  await openPathValue(activity.productClipsDir || liveRoomDirectory(room));
}

async function stopLiveRoom(index) {
  const room = state.liveRooms[index];
  if (!room) return;
  const activity = liveRoomActivity(room);
  const taskId = String(activity.taskId || "").trim();
  if (!taskId) {
    toast("当前直播间没有正在运行的录制任务", "warning");
    return;
  }
  const result = await api("/api/tasks/stop", {
    method: "POST",
    body: JSON.stringify({ task_id: taskId }),
  });
  setLiveRoomActivity(room, {
    liveStatus: "已停止",
    recordStatus: "已停止",
    productStatus: isDouyinLiveRoom(room) ? "待监控" : "未启用",
    duration: activity.duration || "0:00",
    taskId: "",
  });
  toast(result.message || "已停止当前直播间", result.ok ? "warning" : "error");
  await refreshTasks();
}

async function previewLiveRoom(index) {
  const room = state.liveRooms[index];
  if (!room) return;
  const activity = liveRoomActivity(room);
  if (!activity.file) {
    toast("录制完成后可预览输出文件", "warning");
    await openLiveRoomDirectory(index);
    return;
  }
  await openPathValue(activity.file);
}

function showLiveRoomDetail(index) {
  const room = state.liveRooms[index];
  if (!room) return;
  const activity = liveRoomActivity(room);
  const modal = ensureLiveDetailModal();
  const body = modal.querySelector("#live-detail-body");
  const title = modal.querySelector("#live-detail-title");
  if (title) title.textContent = "详情";
  const savePath = activity.productClipsDir || activity.file || liveRoomDirectory(room) || "-";
  const segment = $("live-segment")?.value || "不限";
  const productSplitEnabled = $("live-product-split-enabled")?.checked !== false;
  const productAutoCut = $("live-product-auto-cut")?.checked !== false;
  const namingMode = liveNamingModeLabel(activity.productNamingMode || room.product_naming_mode);
  const defaultMinutes = $("live-product-default-minutes")?.value || "10";
  const checkInterval = $("live-check-interval")?.value || "30";
  const qualityRequirement = liveMinStreamQualityLabel();
  const actualQuality = normalizeLiveStreamQuality(activity.streamQuality);
  const productOutputs = Array.isArray(activity.productOutputs) ? activity.productOutputs : [];
  const sourceExt = String(activity.file || "").split(".").pop()?.toUpperCase() || "TS";
  const rows = [
    ["主播名称", escapeHtml(room.name)],
    ["平台名称", escapeHtml(room.platform)],
    ["直播链接", `<a href="${escapeHtml(room.url)}" target="_blank" rel="noreferrer">${escapeHtml(room.url)}</a>`],
    ["直播标题", escapeHtml(activity.title || room.name || "-")],
    ["录制格式", escapeHtml(sourceExt)],
    ["录制画质", escapeHtml(actualQuality || qualityRequirement)],
    ["最低画质要求", escapeHtml(qualityRequirement)],
    ["分段录制", segment === "不限" ? "未开启" : "已开启"],
    ["分段时长", escapeHtml(segment === "不限" ? "不分段" : segment)],
    ["商品时间线", productSplitEnabled ? "已开启" : "未开启"],
    ["录后自动切段", productAutoCut ? "已开启" : "未开启"],
    ["命名方式", escapeHtml(namingMode)],
    ["单品默认时长", `${escapeHtml(defaultMinutes)}分钟`],
    ["监控状态", escapeHtml(activity.productStatus || (isDouyinLiveRoom(room) ? "待监控" : "未启用"))],
    ["已提取单品", `${Number(activity.productOutputCount || productOutputs.length || 0)} 个`],
    ["已绑定商品", `${Number(activity.productBoundSegments || 0)} 段`],
    ["待确认单品", `${Number(activity.productPendingSegments || 0)} 段`],
    ["商品候选", `${Number(activity.productActiveCandidateCount || 0)} 个`],
    ["候选信号", `${Number(activity.productCandidateSignals || 0)} 条`],
    ["强信号", `${Number(activity.productStrongSignalCount || 0)} 条`],
    ["需复核候选", `${Number(activity.productRuleReviewCount || 0)} 个`],
    ["未确认原因", escapeHtml(activity.productUnresolvedReason || "-")],
    ["录制退出码", activity.recordingReturncode === undefined || activity.recordingReturncode === null ? "-" : escapeHtml(activity.recordingReturncode)],
    ["单品目录", escapeHtml(activity.productClipsDir || "-")],
    ["检测间隔", `${escapeHtml(checkInterval)}秒`],
    ["录制状态", escapeHtml(activity.recordStatus || "未监控")],
    ["直播状态", escapeHtml(activity.liveStatus || "未检测")],
    ["保存路径", escapeHtml(savePath)],
  ];
  if (body) {
    body.innerHTML = rows.map(([label, value]) => `
      <div class="live-detail-row">
        <strong>${escapeHtml(label)}：</strong>
        <span>${value || "-"}</span>
      </div>
    `).join("") + (productOutputs.length ? `
      <div class="live-detail-products">
        <strong>单品文件：</strong>
        <div>
          ${productOutputs.map((path) => `<span title="${escapeHtml(path)}">${escapeHtml(livePathName(path))}</span>`).join("")}
        </div>
      </div>
    ` : "");
  }
  modal.classList.remove("is-hidden");
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("live-detail-modal-open");
}

function closeLiveDetailModal() {
  const modal = $("live-detail-modal");
  if (!modal) return;
  modal.classList.add("is-hidden");
  modal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("live-detail-modal-open");
}

function ensureLiveDetailModal() {
  let modal = $("live-detail-modal");
  if (modal) return modal;
  modal = document.createElement("div");
  modal.id = "live-detail-modal";
  modal.className = "live-detail-modal is-hidden";
  modal.setAttribute("aria-hidden", "true");
  modal.innerHTML = `
    <div class="live-detail-backdrop" data-action="close-live-detail"></div>
    <div class="live-detail-dialog" role="dialog" aria-modal="true" aria-labelledby="live-detail-title">
      <div class="live-detail-head">
        <strong id="live-detail-title">详情</strong>
      </div>
      <div class="live-detail-body" id="live-detail-body"></div>
      <div class="live-detail-actions">
        <button class="button button-muted button-small" data-action="close-live-detail">关闭</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
  return modal;
}

function livePayloadForRoom(room) {
  const basePayload = collectFeaturePayload("live-rec-monitor");
  return {
    ...basePayload,
    room_name: room.name,
    room_url: room.url,
    platform: room.platform,
    product_naming_mode: liveNormalizeNamingMode(room.product_naming_mode || basePayload.product_naming_mode),
  };
}

function liveRoomHasActiveLocalTask(room) {
  const activity = state.liveRoomActivity[liveRoomKey(room)] || {};
  const text = `${activity.liveStatus || ""} ${activity.recordStatus || ""} ${activity.productStatus || ""}`;
  return /(启动中|检测中|录制中|排队中|监控中)/.test(text) && !/(已停止|失败|完成|未直播|未录制)/.test(text);
}

async function startLiveRoom(index) {
  if (!Number.isInteger(index) || index < 0 || index >= state.liveRooms.length) return;
  await saveFeaturePreferences();
  const room = state.liveRooms[index];
  if (liveRoomHasActiveLocalTask(room)) {
    toast(`${room.name} 已在录制或检测中`, "warning");
    appendLog("live-rec", {
      time: new Date().toLocaleTimeString(),
      level: "warning",
      message: `${room.name}: 已在录制或检测中，未重复启动。`,
    });
    return;
  }
  const payload = livePayloadForRoom(room);
  setLiveRoomActivity(room, {
    liveStatus: "检测中",
    recordStatus: "启动中",
    productStatus: isDouyinLiveRoom(room) ? "监控中" : "未启用",
  });
  try {
    await runPreflight("live-rec-monitor", payload, "live-rec");
    const result = await api("/api/live-rec-monitor/start", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setLiveRoomActivity(room, {
      liveStatus: "检测中",
      recordStatus: result.ok ? "检测中" : "待录制",
      productStatus: isDouyinLiveRoom(room) ? "待监控" : "未启用",
      duration: "0:00",
      taskId: result.task_id || "",
    });
    appendLog("live-rec", {
      time: new Date().toLocaleTimeString(),
      level: result.reused ? "warning" : (result.ok ? "success" : "warning"),
      message: `${room.name}: ${result.message || "任务已提交"}`,
    });
    toast(result.message || "直播录制任务已启动", result.reused ? "warning" : (result.ok ? "success" : "warning"));
  } catch (error) {
    setLiveRoomActivity(room, {
      recordStatus: "启动失败",
      productStatus: "待确认",
    });
    appendLog("live-rec", {
      time: new Date().toLocaleTimeString(),
      level: "error",
      message: `${room.name}: ${error.message || error}`,
    });
    throw error;
  } finally {
    refreshTasks();
  }
}

function liveRecordRoomsForSubmit() {
  const tableRooms = liveRoomsFromTable();
  if (tableRooms.length && !state.liveRooms.length) {
    state.liveRooms = tableRooms;
    saveLiveRooms();
  }
  if (state.liveRooms.length) return state.liveRooms;
  const payload = collectFeaturePayload("live-rec-monitor");
  const room = normalizeLiveRoom({ name: payload.room_name, url: payload.room_url, platform: payload.platform });
  return room ? [room] : [];
}

async function submitLiveRecord() {
  await saveFeaturePreferences();
  const rooms = liveRecordRoomsForSubmit();
  if (!rooms.length) {
    toast("请先填写或添加直播间", "warning");
    appendLog("live-rec", { time: new Date().toLocaleTimeString(), level: "warning", message: "请先填写或添加直播间。" });
    return;
  }
  let started = 0;
  let reused = 0;
  const failed = [];
  for (const room of rooms) {
    if (liveRoomHasActiveLocalTask(room)) {
      reused += 1;
      appendLog("live-rec", { time: new Date().toLocaleTimeString(), level: "warning", message: `${room.name}: 已在录制或检测中，跳过重复启动。` });
      continue;
    }
    const payload = livePayloadForRoom(room);
    try {
      setLiveRoomActivity(room, {
        liveStatus: "检测中",
        recordStatus: "启动中",
        productStatus: isDouyinLiveRoom(room) ? "监控中" : "未启用",
      });
      await runPreflight("live-rec-monitor", payload, "live-rec");
      const result = await api("/api/live-rec-monitor/start", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      if (result.reused) reused += 1;
      else started += result.ok ? 1 : 0;
      setLiveRoomActivity(room, {
        liveStatus: "检测中",
        recordStatus: result.ok ? "检测中" : "待录制",
        productStatus: isDouyinLiveRoom(room) ? "待监控" : "未启用",
        duration: "0:00",
        taskId: result.task_id || "",
      });
      appendLog("live-rec", {
        time: new Date().toLocaleTimeString(),
        level: result.reused ? "warning" : (result.ok ? "success" : "warning"),
        message: `${room.name}: ${result.message || "任务已提交"}`,
      });
    } catch (error) {
      failed.push(room.name);
      setLiveRoomActivity(room, {
        recordStatus: "启动失败",
        productStatus: "待确认",
      });
      appendLog("live-rec", {
        time: new Date().toLocaleTimeString(),
        level: "error",
        message: `${room.name}: ${error.message || error}`,
      });
    }
  }
  if (started) toast(`已启动 ${started} 个直播录制任务`, "success");
  if (!started && reused) toast("选中的直播间已经在录制中", "warning");
  if (!started && failed.length) toast("直播录制启动失败，请看运行日志", "error");
  else if (failed.length) toast(`${failed.length} 个直播间启动失败，请看运行日志`, "warning");
  refreshTasks();
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

  const destination = ensureDiagnosticLogView(targetScope);
  if (!destination) return;

  destination.querySelector(".diagnostic-empty")?.remove();

  const row = createLogRow(item);
  destination.appendChild(row);
  destination.scrollTop = destination.scrollHeight;
  updateProgressFromLog(targetScope, item);

  const level = String(item.level || "info").toLowerCase();
  if (level === "error" || (level === "warning" && isImportantWarning(item.message || ""))) {
    state.lastIssuesByScope[targetScope] = {
      level,
      message: item.message || item.raw || "任务遇到问题。",
      time: Date.now(),
    };
  }

  state.diagnosticLogs[targetScope] = (state.diagnosticLogs[targetScope] || 0) + 1;
  state.logs[targetScope] = state.diagnosticLogs[targetScope];
  document.querySelectorAll(`.diagnostic-toggle[data-scope="${targetScope}"]`).forEach(updateDiagnosticButton);
  renderRunSummary(targetScope);
  const counter = $(logCounterId(targetScope));
  if (counter) counter.textContent = String(state.logs[targetScope]);
}

function createLogRow(item) {
  const row = document.createElement("div");
  row.className = `log-entry is-${item.level || "info"}`;
  if ((item.level || "info") === "warning" && isImportantWarning(item.message || "")) {
    row.classList.add("is-important");
  }

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

function isImportantWarning(message) {
  return /AI API Key|云端语音识别|本地语音识别|ASR|Whisper|火山|阿里云|启动检查提示/.test(String(message || ""));
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
    "最终片单",
    "阶段耗时",
    "切割报告",
    "综合评分:",
    "总时长:",
    "输出路径:",
    "路径:",
    "大小:",
    "片段:",
    "Hook:",
    "品类:",
    "拼接 ",
    "拼接完成",
    "[STEP]",
    "━━",
    "▰",
    "▱",
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
  delete state.lastIssuesByScope[scope];
  const counter = $(logCounterId(scope));
  if (counter) counter.textContent = "0";
  resetLogProgress(scope);
  renderRunSummary(scope);
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
  const key = `${type}:${String(message || "")}`;
  const existing = Array.from(stack.querySelectorAll(".toast")).find((node) => node.dataset.toastKey === key);
  if (existing) existing.remove();
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.dataset.toastKey = key;
  item.textContent = message;
  stack.appendChild(item);
  setTimeout(() => item.remove(), 3600);
}
