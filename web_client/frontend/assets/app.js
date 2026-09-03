const state = {
  page: "smart-cut",
  previewWorkbenchModes: { smart: "role", mix: "role" },
  previewWorkbenchFilters: { smart: "all", mix: "all" },
  previewCandidateCategoryFilters: { smart: "all", mix: "all" },
  previewCandidateSourceFilters: { smart: "recommended", mix: "recommended" },
  previewDirectorCandidateViews: { smart: "recommended", mix: "recommended" },
  previewDirectorChapterFocus: { smart: "", mix: "" },
  previewDirectorAlternativesOpen: { smart: false, mix: false },
  previewWorkbenchStages: { smart: "triage", mix: "triage" },
  previewTriageSessions: {},
  previewAssemblyOrders: {},
  settingsTab: "ai",
  liveRecTab: "rooms",
  smartPreview: null,
  mixPreview: null,
  diagnosticsVisible: false,
  videoInfoByTarget: {},
  videoInfoRequestSeq: {},
  videoInfoRetryKeys: {},
  videoInfoRetryTimers: {},
  pipPoolByPrefix: {},
  pipPoolRequestSeq: {},
  keywordConfig: {},
  progressByScope: {},
  legacyBatchProgress: {},
  mixGroups: [],
  activeMixGroupIndex: null,
  mixGroupSelectionMode: false,
  selectedMixGroupIndices: new Set(),
  videoThumbnailByTarget: {},
  videoThumbnailRequestKey: {},
  previewDrafts: {},
  previewDraftSaveTimers: {},
  previewWordEditHistory: { smart: [], mix: [] },
  previewWordRangeAnchors: { smart: null, mix: null },
  previewWordRangeGesture: null,
  previewWordSkipClick: null,
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
  desktopVideoDropTarget: "",
  update: {
    checked: false,
    checking: false,
    installing: false,
    available: false,
    info: null,
    message: "\u672a\u68c0\u67e5",
    error: "",
    stage: "idle",
    progress: 0,
    downloaded: 0,
    total: 0,
  },
  runtime: null,
  backgroundRefreshStarted: false,
  commerceDirectorActiveResultId: "",
  commerceDirectorActiveStrategyId: "",
  commerceDirectorEvidenceFilter: "recommended",
  commerceDirectorFocusedEvidenceId: "",
  commerceDirectorFocusedDraftKey: "",
  commerceDirectorFocusedDraftIndex: -1,
  commerceDirectorLibraryCollapsed: false,
  commerceDirectorPlanDrafts: {},
  commerceDirectorStudioOpen: false,
  commerceDirectorStudioDismissedPreviewId: "",
  commerceDirectorAutoBatchRequestedPreviewId: "",
  commerceDirectorLastServerRenderKey: "",
  liveRoomActivity: {},
  featurePreferencesLoading: false,
  featurePreferencesSaveTimer: null,
  productScan: {
    status: "idle",
    validationKey: "",
    selectionKey: "",
    selectedRangeKeys: new Set(),
    groups: [],
    timeline: [],
    feedback: [],
  },
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

function startBackgroundRefreshLoops() {
  if (state.backgroundRefreshStarted) return;
  state.backgroundRefreshStarted = true;
  window.setInterval(refreshTasks, 2500);
  window.setInterval(loadScanResults, 4000);
  window.setInterval(loadLatestSmartPreview, 5000);
  window.setInterval(loadLatestMixPreview, 5000);
}

const previewInlineAudioStorageKey = "lc:preview:inline-audio";
const desktopVideoDropTargetIds = new Set([
  "video-paths",
  "mix-video-paths",
  "scan-video-paths",
  "ps-video-paths",
  "vs-video-paths",
  "dedup-video-paths",
]);

function isDesktopVideoDropTarget(targetId) {
  return desktopVideoDropTargetIds.has(String(targetId || ""));
}

function previewInlineAudioPreference() {
  try {
    const raw = JSON.parse(localStorage.getItem(previewInlineAudioStorageKey) || "null");
    return {
      muted: raw?.muted === true,
      volume: Number.isFinite(Number(raw?.volume)) ? Math.min(1, Math.max(0, Number(raw.volume))) : 1,
    };
  } catch (error) {
    return { muted: false, volume: 1 };
  }
}

function savePreviewInlineAudioPreference(video) {
  if (!video) return;
  try {
    localStorage.setItem(previewInlineAudioStorageKey, JSON.stringify({
      muted: Boolean(video.muted),
      volume: Number.isFinite(Number(video.volume)) ? Number(video.volume) : 1,
    }));
  } catch (error) {
    // localStorage can be unavailable in restricted browser contexts.
  }
}

function applyPreviewInlineAudioPreference(video) {
  if (!video) return;
  const preference = previewInlineAudioPreference();
  video.muted = preference.muted;
  if (Number.isFinite(preference.volume)) video.volume = preference.volume;
}

function previewInlineAudioMutedAttribute() {
  return previewInlineAudioPreference().muted ? "muted" : "";
}



// The top-level category is the only editable category state. This value is
// derived for the backend so it cannot drift away from the visible selection.
const primaryCategoryToBackendCategory = {
  "服饰内衣": "自动检测",
  "生鲜": "食品/生鲜",
  "食品饮料": "食品饮料",
  "美妆": "美妆护肤",
  "个护家清": "个护家清",
  "鞋靴箱包": "自动检测",
  "钟表配饰": "自动检测",
  "母婴宠物": "母婴宠物",
  "图书教育": "图书教育",
  "智能家居": "家居百货",
  "3C数码家电": "3C数码家电",
  "运动户外": "运动户外",
  "鲜花园艺": "鲜花园艺",
};

const categoryAiProfiles = {
  "服饰内衣": {
    preset_keys: ["viral", "slim", "quality", "commute", "fast", "gentle"],
    focus: ["自动", "版型显瘦", "上身效果", "面料质感", "尺寸长度", "穿着体验", "品质细节", "工艺细节", "颜色氛围", "风格定位", "场景搭配", "性价比", "对比优势", "情绪感染", "流行趋势", "紧迫稀缺"],
    secondary: ["自动识别", "女装", "男装", "内衣", "女鞋", "箱包", "钟表配饰"],
    goal: ["自动", "爆款种草", "场景种草", "专业讲解", "显瘦转化", "质感高级", "快速促单"],
    hook: ["自动", "痛点开头", "上身效果开头", "爆点金句开头", "主播强推荐开头", "细节近景开头", "不强制Hook"],
    ending: ["自动", "尺码引导", "信任背书", "场景收尾", "自然结束", "不要促单"],
    selling: ["版型显瘦", "上身效果", "面料质感", "品质细节", "颜色氛围", "风格定位", "场景搭配", "穿着体验", "性价比", "情绪感染", "流行趋势", "紧迫稀缺", "尺寸长度", "工艺细节", "对比优势"],
    avoid: ["价格", "尺码", "库存", "闲聊", "搭配其他品", "重复卖点"],
    default_focus: "版型显瘦",
    default_goal: "爆款种草",
    default_hook: "上身效果开头",
    default_ending: "场景收尾",
    default_selling: ["版型显瘦", "面料质感", "场景搭配"],
    default_avoid: ["价格", "闲聊"],
    custom_placeholder: "输入关键词，用逗号分隔，如：背心、垂感、通勤",
  },
  "食品/生鲜": {
    preset_keys: ["food_fresh"],
    focus: ["自动", "口感食欲", "新鲜品质", "产地溯源", "规格分量", "发货保鲜", "场景吃法", "性价比", "对比优势", "情绪感染", "紧迫稀缺"],
    secondary: ["自动识别", "新鲜水果", "精选肉类", "水产海鲜", "蔬菜蛋品", "冷饮冻食", "预制菜", "农产品"],
    goal: ["自动", "食欲种草", "新鲜转化", "囤货转化", "专业讲解", "快速促单"],
    hook: ["自动", "试吃反应开头", "切开爆汁开头", "开箱近景开头", "产地品质开头", "主播强推荐开头", "不强制Hook"],
    ending: ["自动", "发货保鲜", "坏果包赔", "囤货收尾", "复购背书", "自然结束", "不要促单"],
    selling: ["口感食欲", "新鲜品质", "产地溯源", "规格分量", "发货保鲜", "场景吃法", "性价比", "对比优势", "情绪感染", "紧迫稀缺"],
    avoid: ["保健功效", "医疗功效", "无限赔付承诺", "价格长段", "闲聊", "重复卖点", "跨商品混讲"],
    default_focus: "口感食欲",
    default_goal: "食欲种草",
    default_hook: "试吃反应开头",
    default_ending: "发货保鲜",
    default_selling: ["口感食欲", "新鲜品质", "发货保鲜", "场景吃法"],
    default_avoid: ["保健功效", "无限赔付承诺", "闲聊", "重复卖点"],
    custom_placeholder: "输入关键词，用逗号分隔，如：蟠桃、冷链、爆汁、坏果包赔",
  },
  "食品饮料": {
    preset_keys: ["food_packaged"],
    focus: ["自动", "口感食欲", "配料品质", "规格分量", "食用场景", "方便省心", "囤货理由", "发货保鲜", "性价比", "对比优势"],
    secondary: ["自动识别", "粮油速食", "休闲零食", "饮料冲调", "传统滋补", "方便速食", "酒水饮料"],
    goal: ["自动", "食欲种草", "囤货转化", "专业讲解", "快速促单"],
    hook: ["自动", "试吃反应开头", "开箱近景开头", "痛点开头", "主播强推荐开头", "不强制Hook"],
    ending: ["自动", "囤货收尾", "复购背书", "信任背书", "自然结束", "不要促单"],
    selling: ["口感食欲", "配料品质", "规格分量", "食用场景", "方便省心", "囤货理由", "发货保鲜", "性价比", "对比优势"],
    avoid: ["保健功效", "医疗功效", "无限赔付承诺", "价格长段", "闲聊", "重复卖点", "跨商品混讲"],
    default_focus: "口感食欲",
    default_goal: "食欲种草",
    default_hook: "试吃反应开头",
    default_ending: "囤货收尾",
    default_selling: ["口感食欲", "配料品质", "食用场景", "囤货理由"],
    default_avoid: ["保健功效", "无限赔付承诺", "闲聊", "重复卖点"],
    custom_placeholder: "输入关键词，用逗号分隔，如：低糖、独立包装、办公室",
  },
  "美妆护肤": {
    preset_keys: ["beauty"],
    focus: ["自动", "使用效果", "肤感质地", "颜色妆效", "成分特点", "适用人群", "使用方法", "持妆体验", "场景搭配", "对比优势"],
    secondary: ["自动识别", "彩妆香水", "美容护肤", "化妆工具", "美发护发"],
    goal: ["自动", "爆款种草", "专业讲解", "质感高级", "快速促单"],
    hook: ["自动", "上脸试色开头", "使用前后开头", "质地近景开头", "痛点开头", "主播强推荐开头", "不强制Hook"],
    ending: ["自动", "使用方法", "信任背书", "场景收尾", "自然结束", "不要促单"],
    selling: ["使用效果", "肤感质地", "颜色妆效", "成分特点", "适用人群", "使用方法", "持妆体验", "场景搭配", "对比优势"],
    avoid: ["医疗功效", "绝对承诺", "前后夸大", "价格长段", "闲聊", "跨商品混讲"],
    default_focus: "使用效果",
    default_goal: "爆款种草",
    default_hook: "上脸试色开头",
    default_ending: "使用方法",
    default_selling: ["使用效果", "肤感质地", "颜色妆效", "使用方法"],
    default_avoid: ["医疗功效", "绝对承诺", "闲聊"],
    custom_placeholder: "输入关键词，用逗号分隔，如：口红、哑光、不卡粉",
  },
  "个护家清": {
    preset_keys: ["personal_care"],
    focus: ["自动", "功能效果", "使用体验", "成分特点", "适用人群", "使用方法", "规格容量", "场景痛点", "对比优势"],
    secondary: ["自动识别", "个人护理", "家庭清洁", "纸品湿巾", "衣物清洁", "口腔护理", "身体护理"],
    goal: ["自动", "痛点转化", "专业讲解", "囤货转化", "快速促单"],
    hook: ["自动", "痛点开头", "使用演示开头", "细节近景开头", "主播强推荐开头", "不强制Hook"],
    ending: ["自动", "使用方法", "囤货收尾", "信任背书", "自然结束", "不要促单"],
    selling: ["功能效果", "使用体验", "成分特点", "适用人群", "使用方法", "规格容量", "场景痛点", "对比优势"],
    avoid: ["医疗功效", "绝对安全", "价格长段", "闲聊", "重复卖点", "跨商品混讲"],
    default_focus: "功能效果",
    default_goal: "痛点转化",
    default_hook: "痛点开头",
    default_ending: "使用方法",
    default_selling: ["功能效果", "使用体验", "场景痛点", "规格容量"],
    default_avoid: ["医疗功效", "绝对安全", "闲聊"],
    custom_placeholder: "输入关键词，用逗号分隔，如：去污、留香、家庭装",
  },
  "家居百货": {
    preset_keys: ["household"],
    focus: ["自动", "功能效果", "使用演示", "材质做工", "规格容量", "适用场景", "清洁维护", "耐用体验", "对比优势"],
    secondary: ["自动识别", "家纺好物", "家具家装", "家电好货", "家居优选", "餐厨优选", "日用百货"],
    goal: ["自动", "痛点转化", "专业讲解", "囤货转化", "快速促单"],
    hook: ["自动", "痛点开头", "使用演示开头", "细节近景开头", "主播强推荐开头", "不强制Hook"],
    ending: ["自动", "场景收尾", "使用方法", "信任背书", "自然结束", "不要促单"],
    selling: ["功能效果", "使用演示", "材质做工", "规格容量", "适用场景", "清洁维护", "耐用体验", "对比优势"],
    avoid: ["绝对承诺", "价格长段", "闲聊", "重复卖点", "跨商品混讲"],
    default_focus: "使用演示",
    default_goal: "痛点转化",
    default_hook: "使用演示开头",
    default_ending: "场景收尾",
    default_selling: ["功能效果", "使用演示", "适用场景", "材质做工"],
    default_avoid: ["绝对承诺", "闲聊", "重复卖点"],
    custom_placeholder: "输入关键词，用逗号分隔，如：收纳、省空间、不粘锅",
  },
  "3C数码家电": {
    preset_keys: ["electronics"],
    focus: ["自动", "功能参数", "使用演示", "性能体验", "适配场景", "外观质感", "安装使用", "售后保障", "对比优势"],
    secondary: ["自动识别", "3C数码配件", "影音智能", "手机", "大家电", "电脑办公", "厨房家电", "生活电器"],
    goal: ["自动", "专业讲解", "爆款种草", "快速促单", "质感高级"],
    hook: ["自动", "参数亮点开头", "使用演示开头", "痛点开头", "细节近景开头", "主播强推荐开头", "不强制Hook"],
    ending: ["自动", "售后保障", "使用方法", "信任背书", "自然结束", "不要促单"],
    selling: ["功能参数", "使用演示", "性能体验", "适配场景", "外观质感", "安装使用", "售后保障", "对比优势"],
    avoid: ["虚构参数", "绝对性能", "价格长段", "闲聊", "重复卖点", "跨商品混讲"],
    default_focus: "功能参数",
    default_goal: "专业讲解",
    default_hook: "参数亮点开头",
    default_ending: "售后保障",
    default_selling: ["功能参数", "使用演示", "适配场景", "售后保障"],
    default_avoid: ["虚构参数", "绝对性能", "闲聊"],
    custom_placeholder: "输入关键词，用逗号分隔，如：续航、降噪、安装",
  },
  "母婴宠物": {
    preset_keys: ["mother_baby_pet"],
    focus: ["自动", "适用对象", "安全材质", "使用场景", "功能效果", "规格容量", "喂养护理", "使用方法", "对比优势"],
    secondary: ["自动识别", "母婴用品", "宝宝食品", "儿童用品", "宠物食品", "宠物用品", "猫狗用品"],
    goal: ["自动", "专业讲解", "痛点转化", "囤货转化", "快速促单"],
    hook: ["自动", "痛点开头", "使用演示开头", "适用对象开头", "细节近景开头", "不强制Hook"],
    ending: ["自动", "使用方法", "囤货收尾", "信任背书", "自然结束", "不要促单"],
    selling: ["适用对象", "安全材质", "使用场景", "功能效果", "规格容量", "喂养护理", "使用方法", "对比优势"],
    avoid: ["医疗功效", "绝对安全", "夸大成长效果", "价格长段", "闲聊", "跨商品混讲"],
    default_focus: "适用对象",
    default_goal: "专业讲解",
    default_hook: "适用对象开头",
    default_ending: "使用方法",
    default_selling: ["适用对象", "安全材质", "使用场景", "规格容量"],
    default_avoid: ["医疗功效", "绝对安全", "闲聊"],
    custom_placeholder: "输入关键词，用逗号分隔，如：纸尿裤、适口性、猫砂",
  },
  "图书教育": {
    preset_keys: ["books_education"],
    focus: ["自动", "适用人群", "内容价值", "学习场景", "版本规格", "使用方法", "套装赠品", "复购理由"],
    secondary: ["自动识别", "图书", "童书绘本", "教辅练习", "课程音像", "学习用品", "文具书包"],
    goal: ["自动", "专业讲解", "种草转化", "快速促单"],
    hook: ["自动", "适用人群开头", "痛点开头", "内容亮点开头", "主播强推荐开头", "不强制Hook"],
    ending: ["自动", "使用方法", "套装赠品", "信任背书", "自然结束", "不要促单"],
    selling: ["适用人群", "内容价值", "学习场景", "版本规格", "使用方法", "套装赠品", "复购理由"],
    avoid: ["保证提分", "包过承诺", "焦虑恐吓", "价格长段", "闲聊", "跨商品混讲"],
    default_focus: "内容价值",
    default_goal: "专业讲解",
    default_hook: "内容亮点开头",
    default_ending: "使用方法",
    default_selling: ["适用人群", "内容价值", "学习场景", "版本规格"],
    default_avoid: ["保证提分", "包过承诺", "焦虑恐吓"],
    custom_placeholder: "输入关键词，用逗号分隔，如：绘本、同步练、错题",
  },
  "运动户外": {
    preset_keys: ["sports_outdoor"],
    focus: ["自动", "使用场景", "功能效果", "材质做工", "规格尺寸", "运动体验", "便携体验", "安全注意", "对比优势"],
    secondary: ["自动识别", "运动服饰", "健身训练", "户外旅行", "露营装备", "骑行用品", "球类用品"],
    goal: ["自动", "场景种草", "专业讲解", "快速促单"],
    hook: ["自动", "场景开头", "使用演示开头", "痛点开头", "细节近景开头", "不强制Hook"],
    ending: ["自动", "场景收尾", "使用方法", "信任背书", "自然结束", "不要促单"],
    selling: ["使用场景", "功能效果", "材质做工", "规格尺寸", "运动体验", "便携体验", "安全注意", "对比优势"],
    avoid: ["绝对安全", "医疗康复", "夸大保护", "价格长段", "闲聊", "跨商品混讲"],
    default_focus: "使用场景",
    default_goal: "场景种草",
    default_hook: "场景开头",
    default_ending: "场景收尾",
    default_selling: ["使用场景", "功能效果", "运动体验", "便携体验"],
    default_avoid: ["绝对安全", "医疗康复", "闲聊"],
    custom_placeholder: "输入关键词，用逗号分隔，如：露营、防水、透气",
  },
  "鲜花园艺": {
    preset_keys: ["flowers_garden"],
    focus: ["自动", "品相状态", "养护方法", "使用场景", "规格数量", "发货包装", "花期状态", "情绪氛围"],
    secondary: ["自动识别", "鲜花花束", "绿植盆栽", "多肉花卉", "种子种苗", "园艺资材", "园艺工具"],
    goal: ["自动", "场景种草", "专业讲解", "快速促单"],
    hook: ["自动", "品相近景开头", "场景开头", "养护方法开头", "主播强推荐开头", "不强制Hook"],
    ending: ["自动", "养护方法", "发货包装", "场景收尾", "自然结束", "不要促单"],
    selling: ["品相状态", "养护方法", "使用场景", "规格数量", "发货包装", "花期状态", "情绪氛围"],
    avoid: ["保证成活", "绝对花期", "价格长段", "闲聊", "重复卖点", "跨商品混讲"],
    default_focus: "品相状态",
    default_goal: "场景种草",
    default_hook: "品相近景开头",
    default_ending: "发货包装",
    default_selling: ["品相状态", "养护方法", "使用场景", "发货包装"],
    default_avoid: ["保证成活", "绝对花期", "闲聊"],
    custom_placeholder: "输入关键词，用逗号分隔，如：玫瑰、花期、醒花",
  },
};

Object.assign(categoryAiProfiles, {
  "鞋靴箱包": {
    preset_keys: [],
    focus: ["自动", "上脚效果", "舒适体验", "鞋型修饰", "材质做工", "尺码适配", "容量收纳", "搭配场景", "耐用体验", "对比优势", "性价比"],
    secondary: ["自动识别", "女鞋", "男鞋", "童鞋", "箱包"],
    goal: ["自动", "场景种草", "专业讲解", "舒适转化", "快速促单"],
    hook: ["自动", "上脚效果开头", "容量展示开头", "细节近景开头", "痛点开头", "主播强推荐开头", "不强制Hook"],
    ending: ["自动", "尺码引导", "场景收尾", "信任背书", "自然结束", "不要促单"],
    selling: ["上脚效果", "舒适体验", "鞋型修饰", "材质做工", "尺码适配", "容量收纳", "搭配场景", "耐用体验", "对比优势", "性价比"],
    avoid: ["尺码长段", "价格长段", "库存", "闲聊", "重复卖点", "跨商品混讲"],
    default_focus: "上脚效果",
    default_goal: "场景种草",
    default_hook: "上脚效果开头",
    default_ending: "场景收尾",
    default_selling: ["上脚效果", "舒适体验", "材质做工", "搭配场景"],
    default_avoid: ["尺码长段", "闲聊", "重复卖点"],
    custom_placeholder: "输入关键词，用逗号分隔，如：乐福鞋、软底、通勤、容量",
  },
  "钟表配饰": {
    preset_keys: [],
    focus: ["自动", "外观设计", "材质工艺", "佩戴效果", "功能参数", "尺寸规格", "品质细节", "适配场景", "送礼场景", "对比优势"],
    secondary: ["自动识别", "钟表", "珠宝文玩", "流行首饰", "眼镜"],
    goal: ["自动", "场景种草", "专业讲解", "质感高级", "快速促单"],
    hook: ["自动", "佩戴效果开头", "细节近景开头", "参数亮点开头", "场景开头", "主播强推荐开头", "不强制Hook"],
    ending: ["自动", "信任背书", "场景收尾", "使用方法", "自然结束", "不要促单"],
    selling: ["外观设计", "材质工艺", "佩戴效果", "功能参数", "尺寸规格", "品质细节", "适配场景", "送礼场景", "对比优势"],
    avoid: ["虚构材质", "绝对保值", "价格长段", "闲聊", "重复卖点", "跨商品混讲"],
    default_focus: "佩戴效果",
    default_goal: "质感高级",
    default_hook: "佩戴效果开头",
    default_ending: "信任背书",
    default_selling: ["外观设计", "材质工艺", "佩戴效果", "适配场景"],
    default_avoid: ["虚构材质", "绝对保值", "闲聊"],
    custom_placeholder: "输入关键词，用逗号分隔，如：机械表、珍珠、通勤、送礼",
  },
});

// The hierarchy follows the product-category structure supplied from the
// Douyin product selection UI. The leaf values are suggestions, never a hard
// whitelist, so a merchant can still type a new SKU or product name.
const primaryCategoryTaxonomy = {
  "服饰内衣": {
    profile_key: "服饰内衣",
    secondary: ["自动识别", "女装", "男装", "内衣"],
    leaf_categories: {
      "女装": ["套装", "西装", "大码女装", "女士T恤", "半身裙", "衬衫", "连衣裙", "裤子"],
      "男装": ["Polo衫", "男士T恤", "牛仔裤", "衬衫", "套装", "卫裤", "短裤", "休闲裤"],
      "内衣": ["文胸", "内裤", "家居服", "保暖内衣", "塑身衣", "睡衣", "袜子"],
    },
  },
  "生鲜": {
    profile_key: "食品/生鲜",
    secondary: ["自动识别", "新鲜水果", "精选肉类", "水产海鲜", "蔬菜蛋品", "冷饮冻食", "预制菜", "农产品"],
    leaf_categories: {
      "新鲜水果": ["橙子", "更多水果", "芒果", "猕猴桃", "百香果", "桔/橘", "草莓", "火龙果", "苹果", "榴莲", "蟠桃", "水蜜桃"],
      "精选肉类": ["牛肉类", "鸡肉类", "羊肉类", "更多肉类", "猪肉类", "鸭肉类"],
      "水产海鲜": ["虾类", "蟹类", "贝类", "鱼类", "海参", "海味干货"],
      "蔬菜蛋品": ["叶菜", "根茎", "菌菇", "玉米", "鸡蛋", "蔬菜组合"],
      "冷饮冻食": ["包点/面点", "冻半成品", "冰淇淋", "冷冻水产"],
      "预制菜": ["即烹菜", "加热即食", "调理肉", "汤羹"],
      "农产品": ["杂粮干货", "农家特产", "食用菌", "山货"],
    },
  },
  "食品饮料": {
    profile_key: "食品饮料",
    secondary: ["自动识别", "粮油速食", "传统滋补", "休闲零食", "饮料冲调", "方便速食", "酒水饮料"],
    leaf_categories: {
      "粮油速食": ["调味烘焙", "粮油米面", "方便速食", "干货其他"],
      "传统滋补": ["阿胶", "养生原料", "枸杞", "养生茶", "燕窝", "食疗滋补", "蜂蜜", "参茸贵细"],
      "休闲零食": ["海味零食", "肉类零食", "其他零食", "糕点点心", "坚果炒货", "糖巧果脯"],
      "饮料冲调": ["咖啡", "茶饮", "冲泡饮品", "果汁", "乳饮", "气泡水"],
      "方便速食": ["自热食品", "拌面米粉", "速食粥汤", "即食小吃"],
      "酒水饮料": ["白酒", "葡萄酒", "啤酒", "低度酒", "饮料"],
    },
  },
  "美妆": {
    profile_key: "美妆护肤",
    secondary: ["自动识别", "彩妆香水", "化妆工具", "美容护肤", "美发护发"],
    leaf_categories: {
      "彩妆香水": ["睫毛膏", "彩妆套装", "修容", "眉笔", "气垫", "口红", "粉底液", "眼影眼线", "香水", "隔离", "散粉", "腮红"],
      "化妆工具": ["假睫毛", "化妆棉", "面扑/粉扑", "化妆刷", "修眉刀", "美容工具"],
      "美容护肤": ["防晒", "洁面", "护肤套装", "精华", "面霜", "乳液", "面膜", "爽肤水"],
      "美发护发": ["洗发水", "护发素", "发膜", "染发", "造型工具"],
    },
  },
  "个护家清": {
    profile_key: "个护家清",
    secondary: ["自动识别", "个人护理", "家庭清洁"],
    leaf_categories: {
      "个人护理": ["卫生巾", "头发清洁", "口腔护理", "染发烫发", "足部护理", "身体护理", "身体清洁"],
      "家庭清洁": ["衣物清洁", "家庭清洁", "驱虫用品", "纸品/湿巾"],
    },
  },
  "鞋靴箱包": {
    profile_key: "鞋靴箱包",
    secondary: ["自动识别", "女鞋", "男鞋", "童鞋", "箱包"],
    leaf_categories: {
      "女鞋": ["低帮鞋", "女鞋配件", "靴子", "高跟鞋", "高帮鞋", "凉鞋", "帆布鞋", "拖鞋"],
      "男鞋": ["休闲鞋", "皮鞋", "运动鞋", "凉鞋", "靴子"],
      "童鞋": ["学步鞋", "运动鞋", "凉鞋", "棉鞋"],
      "箱包": ["功能箱包", "男士包袋", "女士包袋", "旅行箱", "双肩包"],
    },
  },
  "钟表配饰": {
    profile_key: "钟表配饰",
    secondary: ["自动识别", "钟表", "珠宝文玩", "流行首饰", "眼镜"],
    leaf_categories: {
      "钟表": ["机械表", "石英表", "智能手表", "表带"],
      "珠宝文玩": ["黄金", "玉石", "珍珠", "文玩手串"],
      "流行首饰": ["项链", "耳饰", "戒指", "手链"],
      "眼镜": ["太阳镜", "防蓝光眼镜", "镜框", "隐形眼镜"],
    },
  },
  "母婴宠物": {
    profile_key: "母婴宠物",
    secondary: ["自动识别", "母婴用品", "宝宝食品", "儿童用品", "宠物食品", "宠物用品"],
    leaf_categories: {
      "母婴用品": ["纸尿裤", "湿巾", "喂养用品", "孕产用品"],
      "宝宝食品": ["奶粉", "辅食", "零食", "营养品"],
      "儿童用品": ["童装", "玩具", "学习用品", "出行用品"],
      "宠物食品": ["猫粮", "狗粮", "零食", "罐头"],
      "宠物用品": ["猫砂", "清洁护理", "玩具", "牵引出行"],
    },
  },
  "图书教育": {
    profile_key: "图书教育",
    secondary: ["自动识别", "图书", "教育音像", "学习用品"],
    leaf_categories: {
      "图书": ["知识服务", "童书", "小说", "教辅", "生活", "励志", "传记", "社会科学", "文学"],
      "教育音像": ["教育音像", "课程资料"],
      "学习用品": ["画具/画材", "文具", "书包"],
    },
  },
  "智能家居": {
    profile_key: "家居百货",
    secondary: ["自动识别", "家纺好物", "家具家装", "家电好货", "家居优选", "餐厨优选"],
    leaf_categories: {
      "家纺好物": ["床上用品", "居家布艺"],
      "家具家装": ["电子电工", "五金工具", "基础建材", "品质家具", "家装主材", "灯饰照明"],
      "家电好货": ["个护健康", "厨房电器", "生活电器"],
      "家居优选": ["收纳清洁", "居家日用", "家庭饰品"],
      "餐厨优选": ["厨房工具", "餐具水具", "烹饪锅具"],
    },
  },
  "3C数码家电": {
    profile_key: "3C数码家电",
    secondary: ["自动识别", "影音智能", "3C数码配件", "手机", "大家电", "电脑办公"],
    leaf_categories: {
      "影音智能": ["影音设备", "智能设备"],
      "3C数码配件": ["数码配件", "充电配件", "支架"],
      "手机": ["手机", "手机壳", "贴膜"],
      "大家电": ["厨房大电", "空调", "热水器", "冰箱"],
      "电脑办公": ["鼠标键盘", "显示器", "打印设备", "电脑配件"],
    },
  },
  "运动户外": {
    profile_key: "运动户外",
    secondary: ["自动识别", "运动服饰", "健身训练", "户外旅行", "露营装备", "骑行用品", "球类用品"],
    leaf_categories: {
      "运动服饰": ["跑步服", "瑜伽服", "运动鞋", "防晒衣"],
      "健身训练": ["哑铃", "拉力器", "健身器械", "护具"],
      "户外旅行": ["户外照明", "垂钓装备", "饮水用具"],
      "露营装备": ["帐篷", "睡袋", "桌椅", "炉具"],
      "骑行用品": ["头盔", "骑行服", "车灯", "维修工具"],
      "球类用品": ["篮球", "羽毛球", "乒乓球", "足球"],
    },
  },
  "鲜花园艺": {
    profile_key: "鲜花园艺",
    secondary: ["自动识别", "鲜花花束", "绿植盆栽", "多肉花卉", "种子种苗", "园艺资材", "园艺工具"],
    leaf_categories: {
      "鲜花花束": ["玫瑰", "百合", "康乃馨", "节日花礼"],
      "绿植盆栽": ["观叶植物", "开花盆栽", "盆景"],
      "多肉花卉": ["多肉", "兰花", "球根花卉"],
      "种子种苗": ["蔬菜种子", "花卉种子", "果树苗"],
      "园艺资材": ["花盆", "营养土", "肥料", "营养液"],
      "园艺工具": ["剪刀", "喷壶", "浇水器", "园艺套装"],
    },
  },
};

const autoCategoryValues = new Set(["", "自动", "自动检测", "自动识别", "auto"]);

function isAutoCategoryValue(value) {
  return autoCategoryValues.has(String(value || "").trim());
}

function primaryCategoryValue(prefix) {
  return $(`${prefix}-primary-category`)?.value || "服饰内衣";
}

function backendCategoryForPrimary(primary) {
  return primaryCategoryToBackendCategory[String(primary || "").trim()] || "自动检测";
}

function categoryAiProfileKey(primary) {
  const taxonomy = primaryCategoryTaxonomy[String(primary || "").trim()];
  return taxonomy?.profile_key || "服饰内衣";
}

function currentCategoryAiProfile(prefix) {
  const primary = primaryCategoryValue(prefix);
  const taxonomy = primaryCategoryTaxonomy[primary] || primaryCategoryTaxonomy["服饰内衣"];
  const key = categoryAiProfileKey(primary);
  const baseProfile = categoryAiProfiles[key] || categoryAiProfiles["服饰内衣"];
  return {
    key,
    primary,
    taxonomy,
    profile: {
      ...baseProfile,
      secondary: taxonomy.secondary || baseProfile.secondary,
      leaf_categories: taxonomy.leaf_categories || {},
    },
  };
}

function replaceSelectOptions(id, values, preferredValue) {
  const select = $(id);
  if (!select || !Array.isArray(values) || !values.length) return;
  const wanted = preferredValue !== undefined ? String(preferredValue || "") : String(select.value || "");
  select.textContent = "";
  values.forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.appendChild(option);
  });
  const hasWanted = Array.from(select.options).some((option) => option.value === wanted || option.textContent.trim() === wanted);
  select.value = hasWanted ? wanted : values[0];
}

function renderAiChipGroup(prefix, kind, values, selectedValues) {
  const controlName = `${prefix}-${kind}`;
  const grid = document.querySelector(`[data-ai-chip-group="${controlName}"]`);
  if (!grid || !Array.isArray(values)) return;
  const selected = new Set(Array.isArray(selectedValues) ? selectedValues : checkedControlValues(controlName));
  grid.textContent = "";
  values.forEach((value) => {
    const label = document.createElement("label");
    label.className = "check-item";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.aiControl = controlName;
    input.value = value;
    input.checked = selected.has(value);
    label.appendChild(input);
    label.appendChild(document.createTextNode(` ${value}`));
    grid.appendChild(label);
  });
}

function refreshAiPresetVisibility(prefix, profile) {
  const select = $(`${prefix}-ai-preset`);
  if (!select || !profile) return;
  const allowed = new Set(["custom", ...(profile.preset_keys || [])]);
  Array.from(select.options).forEach((option) => {
    const isCustomPreset = option.dataset.customPreset === "1" || String(option.value || "").startsWith("custom-");
    const visible = isCustomPreset || allowed.has(option.value);
    option.hidden = !visible;
    option.disabled = !visible;
  });
  if (select.selectedOptions[0]?.disabled) select.value = "custom";
}

function refreshCategoryLeafSuggestions(prefix, profile, options = {}) {
  const input = $(`${prefix}-leaf-category`);
  const list = $(`${prefix}-leaf-category-list`);
  const secondary = $(`${prefix}-secondary-category`)?.value || "自动识别";
  const values = profile?.leaf_categories?.[secondary] || [];
  if (list) {
    list.textContent = "";
    values.forEach((value) => {
      const option = document.createElement("option");
      option.value = value;
      list.appendChild(option);
    });
  }
  if (!input) return;
  if (options.resetValue) input.value = "";
  if (options.preferredValue !== undefined) input.value = options.preferredValue || "";
  input.placeholder = values.length
    ? `自动识别，可填${values.slice(0, 3).join("/")}`
    : "自动识别，可填写具体商品";
}

function refreshCategoryAiControls(prefix, options = {}) {
  const { profile } = currentCategoryAiProfile(prefix);
  if (!profile) return;
  const preferDefaults = Boolean(options.preferDefaults);
  replaceSelectOptions(`${prefix}-secondary-category`, profile.secondary, options.preferredSecondary ?? (preferDefaults ? "自动识别" : undefined));
  replaceSelectOptions(`${prefix}-goal`, profile.goal, options.preferredGoal ?? (preferDefaults ? profile.default_goal : undefined));
  replaceSelectOptions(`${prefix}-hook-style`, profile.hook, options.preferredHook ?? (preferDefaults ? profile.default_hook : undefined));
  replaceSelectOptions(`${prefix}-ending-style`, profile.ending, options.preferredEnding ?? (preferDefaults ? profile.default_ending : undefined));
  renderAiChipGroup(prefix, "selling", profile.selling, options.selectedSelling ?? (preferDefaults ? profile.default_selling : undefined));
  renderAiChipGroup(prefix, "avoid", profile.avoid, options.selectedAvoid ?? (preferDefaults ? profile.default_avoid : undefined));
  const customInput = $(`${prefix}-selling-custom`);
  if (customInput && profile.custom_placeholder) {
    customInput.placeholder = profile.custom_placeholder;
    if (options.resetCustomSelling) customInput.value = "";
  }
  refreshCategoryLeafSuggestions(prefix, profile, {
    preferredValue: options.preferredLeaf,
    resetValue: Boolean(options.resetLeaf),
  });
  if (options.resetMainProduct) {
    const mainProduct = $(`${prefix}-main-product`);
    if (mainProduct) mainProduct.value = "";
  }
  refreshAiPresetVisibility(prefix, profile);
  const panel = document.querySelector(`[data-summary-kind="ai"][data-summary-prefix="${prefix}"]`);
  if (panel) updatePanelSummary(panel);
}

function syncPrimaryCategory(prefix, options = {}) {
  const primary = primaryCategoryValue(prefix);
  const select = $(`${prefix}-category`);
  if (select) {
    setSelectIfPresent(`${prefix}-category`, backendCategoryForPrimary(primary));
    select.dataset.autoCategory = "1";
  }
  const reset = Boolean(options.reset);
  refreshCategoryAiControls(prefix, {
    preferDefaults: reset,
    resetLeaf: reset,
    resetMainProduct: reset,
    resetCustomSelling: reset,
  });
}

function bindCategoryControls() {
  ["sc", "mix"].forEach((prefix) => {
    const primary = $(`${prefix}-primary-category`);
    if (primary) {
      primary.addEventListener("change", () => {
        syncPrimaryCategory(prefix, { reset: true });
      });
    }
    $(`${prefix}-secondary-category`)?.addEventListener("change", () => {
      const { profile } = currentCategoryAiProfile(prefix);
      refreshCategoryLeafSuggestions(prefix, profile, { resetValue: true });
      const mainProduct = $(`${prefix}-main-product`);
      if (mainProduct) mainProduct.value = "";
      const panel = document.querySelector(`[data-summary-kind="ai"][data-summary-prefix="${prefix}"]`);
      if (panel) updatePanelSummary(panel);
    });
    syncPrimaryCategory(prefix);
  });
}

const settingFields = {
  api_key: "s-api-key",
  base_url: "s-base-url",
  model: "s-model",
  enabled: "s-enabled",
  asr_enabled: "s-asr-enabled",
  local_asr_quality_retry_enabled: "s-local-asr-quality-retry-enabled",
  asr_provider: "s-asr-provider",
  volc_api_key: "s-volc-api-key",
  volc_tos_ak: "s-volc-tos-ak",
  volc_tos_sk: "s-volc-tos-sk",
  volc_bucket: "s-volc-bucket",
  volc_region: "s-volc-region",
  ui_theme: "s-ui-theme",
  hardware_encoder_enabled: "s-hardware-encoder",
  subtitle_font_size: "s-subtitle-font-size",
  subtitle_font_family: "s-subtitle-font-family",
  subtitle_font_color: "s-subtitle-font-color",
  subtitle_text_effect: "s-subtitle-text-effect",
  subtitle_opacity: "s-subtitle-opacity",
  subtitle_blur: "s-subtitle-blur",
  subtitle_position_percent: "s-subtitle-position-percent",
  ui_font_size: "s-ui-font-size",
  style_profile_strength: "s-style-profile-strength",
  content_review_mode: "s-content-review-mode",
  m2_planner_mode: "s-m2-planner-mode",
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
const compactVideoListTargetIds = new Set(["video-paths", "mix-video-paths", "ps-video-paths"]);

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
      "sc-output-naming",
      "sc-duration",
      "sc-duration-tolerance",
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
      "sc-primary-category",
      "sc-ai-preset",
      "sc-secondary-category",
      "sc-leaf-category",
      "sc-main-product",
      "sc-goal",
      "sc-hook-style",
      "sc-selling-custom",
      "sc-ending-style",
    ],
  },
  mix: {
    prefixes: ["mix"],
    ids: [
      "mix-output-dir",
      "mix-output-naming",
      "mix-duration",
      "mix-duration-tolerance",
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
      "mix-primary-category",
      "mix-ai-preset",
      "mix-secondary-category",
      "mix-leaf-category",
      "mix-main-product",
      "mix-goal",
      "mix-hook-style",
      "mix-selling-custom",
      "mix-ending-style",
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
      "ps-fast-cut",
      "ps-video-start-offset",
      "ps-live-start-time",
      "ps-time-basis-relative",
      "ps-time-basis-clock",
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
    goal: "场景种草",
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
    primary_category: "生鲜",
    category: "食品/生鲜",
    secondary_category: "新鲜水果",
    goal: "食欲种草",
    focus: "口感食欲",
    hook: "试吃反应开头",
    ending: "发货保鲜",
    strictness: "标准",
    selling: ["口感食欲", "新鲜品质", "发货保鲜", "场景吃法"],
    avoid: ["保健功效", "无限赔付承诺", "闲聊", "重复卖点"],
  },
  food_packaged: {
    label: "食品饮料",
    primary_category: "食品饮料",
    category: "食品饮料",
    secondary_category: "休闲零食",
    goal: "食欲种草",
    focus: "口感食欲",
    hook: "试吃反应开头",
    ending: "囤货收尾",
    strictness: "标准",
    selling: ["口感食欲", "配料品质", "食用场景", "囤货理由"],
    avoid: ["保健功效", "无限赔付承诺", "闲聊", "重复卖点"],
  },
  beauty: {
    label: "美妆护肤",
    primary_category: "美妆",
    category: "美妆护肤",
    secondary_category: "美容护肤",
    goal: "爆款种草",
    focus: "使用效果",
    hook: "上脸试色开头",
    ending: "使用方法",
    strictness: "标准",
    selling: ["使用效果", "肤感质地", "颜色妆效", "使用方法"],
    avoid: ["医疗功效", "绝对承诺", "闲聊"],
  },
  personal_care: {
    label: "个护家清",
    primary_category: "个护家清",
    category: "个护家清",
    secondary_category: "个人护理",
    goal: "痛点转化",
    focus: "功能效果",
    hook: "痛点开头",
    ending: "使用方法",
    strictness: "标准",
    selling: ["功能效果", "使用体验", "场景痛点", "规格容量"],
    avoid: ["医疗功效", "绝对安全", "闲聊"],
  },
  household: {
    label: "家居百货",
    primary_category: "智能家居",
    category: "家居百货",
    secondary_category: "家居优选",
    goal: "痛点转化",
    focus: "使用演示",
    hook: "使用演示开头",
    ending: "场景收尾",
    strictness: "标准",
    selling: ["功能效果", "使用演示", "适用场景", "材质做工"],
    avoid: ["绝对承诺", "闲聊", "重复卖点"],
  },
  electronics: {
    label: "3C数码家电",
    primary_category: "3C数码家电",
    category: "3C数码家电",
    secondary_category: "3C数码配件",
    goal: "专业讲解",
    focus: "功能参数",
    hook: "参数亮点开头",
    ending: "售后保障",
    strictness: "标准",
    selling: ["功能参数", "使用演示", "适配场景", "售后保障"],
    avoid: ["虚构参数", "绝对性能", "闲聊"],
  },
  mother_baby_pet: {
    label: "母婴宠物",
    primary_category: "母婴宠物",
    category: "母婴宠物",
    secondary_category: "自动识别",
    goal: "专业讲解",
    focus: "适用对象",
    hook: "适用对象开头",
    ending: "使用方法",
    strictness: "标准",
    selling: ["适用对象", "安全材质", "使用场景", "规格容量"],
    avoid: ["医疗功效", "绝对安全", "闲聊"],
  },
  books_education: {
    label: "图书教育",
    primary_category: "图书教育",
    category: "图书教育",
    secondary_category: "图书",
    goal: "专业讲解",
    focus: "内容价值",
    hook: "内容亮点开头",
    ending: "使用方法",
    strictness: "标准",
    selling: ["适用人群", "内容价值", "学习场景", "版本规格"],
    avoid: ["保证提分", "包过承诺", "焦虑恐吓"],
  },
  sports_outdoor: {
    label: "运动户外",
    primary_category: "运动户外",
    category: "运动户外",
    secondary_category: "户外旅行",
    goal: "场景种草",
    focus: "使用场景",
    hook: "场景开头",
    ending: "场景收尾",
    strictness: "标准",
    selling: ["使用场景", "功能效果", "运动体验", "便携体验"],
    avoid: ["绝对安全", "医疗康复", "闲聊"],
  },
  flowers_garden: {
    label: "鲜花园艺",
    primary_category: "鲜花园艺",
    category: "鲜花园艺",
    secondary_category: "鲜花花束",
    goal: "场景种草",
    focus: "品相状态",
    hook: "品相近景开头",
    ending: "发货包装",
    strictness: "标准",
    selling: ["品相状态", "养护方法", "使用场景", "发货包装"],
    avoid: ["保证成活", "绝对花期", "闲聊"],
  },
};

document.addEventListener("DOMContentLoaded", () => {
  startBackgroundRefreshLoops();
  bindNavigation();
  bindSettingsTabs();
  bindAiSelectionAutoSave();
  bindLiveRecTabs();
  bindLiveRoomFilters();
  bindActions();
  bindProductScanFlow();
  syncVideoSplitMode();
  setupCollapsiblePanels();
  setupAdvancedParamToggles();
  bindAiPresetControls();
  bindCategoryControls();
  bindPreviewControls();
  bindFeaturePreferenceAutoSave();
  bindDedupCustomControls();
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
  window.addEventListener("resize", () => {
    updatePreviewStickyOffset("smart");
    updatePreviewStickyOffset("mix");
  });
});

function $(id) {
  return document.getElementById(id);
}

function bindProductScanFlow() {
  document.querySelectorAll('input[name="ps-align-mode"]').forEach((input) => {
    input.addEventListener("change", syncProductScanFlow);
  });
  document.querySelectorAll('input[name="ps-time-basis"]').forEach((input) => {
    input.addEventListener("change", syncProductScanFlow);
  });
  document.querySelectorAll("#page-product-scan input, #page-product-scan textarea").forEach((input) => {
    input.addEventListener("input", syncProductScanFlow);
    input.addEventListener("change", syncProductScanFlow);
  });
  document.addEventListener("change", (event) => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement)) return;
    const rangeKey = input.dataset.productScanSelectRange;
    const groupIndex = input.dataset.productScanSelectGroup;
    if (!rangeKey && groupIndex === undefined) return;
    ensureProductScanRangeSelection(state.productScan.groups || []);
    if (rangeKey) {
      if (input.checked) state.productScan.selectedRangeKeys.add(rangeKey);
      else state.productScan.selectedRangeKeys.delete(rangeKey);
    } else {
      const keys = productScanSelectableRangeKeysForGroup(Number(groupIndex));
      keys.forEach((key) => {
        if (input.checked) state.productScan.selectedRangeKeys.add(key);
        else state.productScan.selectedRangeKeys.delete(key);
      });
    }
    renderProductPreview(
      state.productScan.groups,
      state.productScan.timeline,
      state.productScan.feedback,
    );
    syncProductScanFlow();
  });
  syncProductScanFlow();
}

function productScanAlignmentMode() {
  return document.querySelector('input[name="ps-align-mode"]:checked')?.value === "auto" ? "auto" : "manual";
}

function productScanTimeBasis() {
  return document.querySelector('input[name="ps-time-basis"]:checked')?.value === "clock" ? "clock" : "relative";
}

function productScanNeedsLiveStart() {
  return productScanAlignmentMode() === "auto" || productScanTimeBasis() === "clock";
}

function productScanLiveStartValue() {
  return $("ps-live-start-time")?.value.trim() || "";
}

function parseProductScanOffset(value) {
  const text = String(value || "").trim().replace(/：/g, ":");
  if (!text) return null;
  if (/^\d+(\.\d+)?$/.test(text)) return Number(text);
  const parts = text.split(":");
  if (![2, 3].includes(parts.length)) return null;
  const values = parts.map((part) => Number(part.trim()));
  if (values.some((part) => !Number.isInteger(part) || part < 0)) return null;
  const [hours, minutes, seconds] = parts.length === 3 ? values : [0, values[0], values[1]];
  if (minutes > 59 || seconds > 59) return null;
  return hours * 3600 + minutes * 60 + seconds;
}

function formatProductScanTime(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return "--";
  if (seconds < 0) return "片段开始前";
  const whole = Math.round(seconds);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const secs = whole % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`
    : `${minutes}:${String(secs).padStart(2, "0")}`;
}

function productScanFormKey() {
  const mode = productScanAlignmentMode();
  const timeBasis = productScanTimeBasis();
  return JSON.stringify({
    excel: $("ps-excel-path")?.value.trim() || "",
    videos: getLines("ps-video-paths"),
    mode,
    timeBasis,
    videoStart: mode === "manual" ? $("ps-video-start-offset")?.value.trim() || "" : "",
    liveStart: productScanNeedsLiveStart() ? productScanLiveStartValue() : "",
    advance: Number($("ps-advance")?.value || 0),
  });
}

function productScanHasSource() {
  return Boolean($("ps-excel-path")?.value.trim()) && getLines("ps-video-paths").length > 0;
}

function productScanCanValidate() {
  if (!productScanHasSource()) return false;
  if (productScanNeedsLiveStart() && !productScanLiveStartValue()) return false;
  return productScanAlignmentMode() === "auto"
    || parseProductScanOffset($("ps-video-start-offset")?.value) !== null;
}

function productScanIsReady() {
  return state.productScan.status === "ready"
    && Boolean(state.productScan.validationKey)
    && state.productScan.validationKey === productScanFormKey();
}

function productScanRangeKey(groupIndex, rangeIndex) {
  return `${Number(groupIndex)}:${Number(rangeIndex)}`;
}

function productScanRangeIsSelectable(range) {
  return Array.isArray(range?.parts) && range.parts.length > 0;
}

function productScanSelectableRangeKeys(groups = state.productScan.groups || []) {
  const keys = [];
  groups.forEach((group, groupIndex) => {
    (Array.isArray(group?.ranges) ? group.ranges : []).forEach((range, rangeIndex) => {
      if (productScanRangeIsSelectable(range)) keys.push(productScanRangeKey(groupIndex, rangeIndex));
    });
  });
  return keys;
}

function productScanSelectableRangeKeysForGroup(groupIndex) {
  const group = state.productScan.groups?.[groupIndex];
  return (Array.isArray(group?.ranges) ? group.ranges : [])
    .map((range, rangeIndex) => productScanRangeIsSelectable(range)
      ? productScanRangeKey(groupIndex, rangeIndex)
      : "")
    .filter(Boolean);
}

function ensureProductScanRangeSelection(groups = state.productScan.groups || []) {
  const availableKeys = productScanSelectableRangeKeys(groups);
  const available = new Set(availableKeys);
  if (state.productScan.selectionKey !== state.productScan.validationKey) {
    state.productScan.selectedRangeKeys = new Set(availableKeys);
    state.productScan.selectionKey = state.productScan.validationKey;
    return availableKeys;
  }
  const retained = [...(state.productScan.selectedRangeKeys || [])]
    .filter((key) => available.has(key));
  state.productScan.selectedRangeKeys = new Set(retained);
  return availableKeys;
}

function productScanSelectedRangeKeys(groups = state.productScan.groups || []) {
  ensureProductScanRangeSelection(groups);
  return [...state.productScan.selectedRangeKeys];
}

function productScanHasSelectedRanges() {
  return productScanSelectedRangeKeys().length > 0;
}

function productScanStatusMessage() {
  const mode = productScanAlignmentMode();
  const timeBasis = productScanTimeBasis();
  const offset = parseProductScanOffset($("ps-video-start-offset")?.value);
  if (state.productScan.status === "working") return { text: "正在读取时间表并校验可分割范围…", tone: "working" };
  if (productScanIsReady() && !productScanHasSelectedRanges()) return { text: "请在右侧勾选至少一个可切割的商品或时段。", tone: "invalid" };
  if (productScanIsReady()) return { text: `时间已校验，已选 ${productScanSelectedRangeKeys().length} 个时段，可以开始分割。`, tone: "ready" };
  if (!productScanHasSource()) return { text: "先选择排品表和至少一个直播视频。", tone: "invalid" };
  if (timeBasis === "clock" && !productScanLiveStartValue()) return { text: "表格已选“时钟时间”，请填写整场直播开播时间作为换算基准。", tone: "invalid" };
  if (mode === "manual" && offset === null) return { text: "填写本段视频从直播第几秒开始，再读取并校验。", tone: "invalid" };
  if (mode === "auto" && !productScanLiveStartValue()) return { text: "填写整场直播开播时间；视频文件名会自动识别时间戳。", tone: "invalid" };
  return { text: "素材已就绪，读取并校验后会显示文件内切分时间。", tone: "" };
}

function syncProductScanFlow() {
  const mode = productScanAlignmentMode();
  const timeBasis = productScanTimeBasis();
  document.querySelectorAll("[data-ps-align-choice]").forEach((choice) => {
    choice.classList.toggle("is-active", choice.dataset.psAlignChoice === mode);
  });
  document.querySelectorAll("[data-ps-align-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.psAlignPanel !== mode;
  });
  document.querySelectorAll("[data-ps-time-basis-choice]").forEach((choice) => {
    choice.classList.toggle("is-active", choice.dataset.psTimeBasisChoice === timeBasis);
  });
  document.querySelectorAll("[data-ps-live-start]").forEach((panel) => {
    panel.hidden = !productScanNeedsLiveStart();
  });
  const liveStartLabel = document.querySelector("[data-ps-live-start-label]");
  const liveStartHelp = document.querySelector("[data-ps-live-start-help]");
  if (liveStartLabel) liveStartLabel.textContent = "整场直播开播时间";
  if (liveStartHelp) {
    liveStartHelp.innerHTML = mode === "auto" && timeBasis === "clock"
      ? "系统会自动读取文件名时间戳，并用这里的开播时间把表格钟表时间换算为直播进度。"
      : mode === "auto"
        ? "这不是视频起点；系统会自动读取文件名时间戳。可填 <strong>202608051219</strong> 或 <strong>2026-08-05 12:19</strong>。"
        : "用来把表格中的钟表时间换算为直播进度；可填 <strong>202608051219</strong> 或 <strong>2026-08-05 12:19</strong>。";
  }

  if (state.productScan.status === "ready" && state.productScan.validationKey !== productScanFormKey()) {
    state.productScan.status = "idle";
    state.productScan.selectionKey = "";
    state.productScan.selectedRangeKeys = new Set();
    state.productScan.groups = [];
    state.productScan.timeline = [];
    state.productScan.feedback = [];
  }

  const status = productScanStatusMessage();
  const statusBox = $("ps-validation-state");
  if (statusBox) {
    statusBox.textContent = status.text;
    statusBox.classList.toggle("is-ready", status.tone === "ready");
    statusBox.classList.toggle("is-working", status.tone === "working");
    statusBox.classList.toggle("is-invalid", status.tone === "invalid");
  }

  const running = state.runningScopes instanceof Set && state.runningScopes.has("product-scan");
  setButtonsEnabled("#ps-read-schedule", productScanCanValidate() && !running && state.productScan.status !== "working", "请补充素材与时间对齐方式");
  setButtonsEnabled("#ps-start-scan", productScanIsReady() && productScanHasSelectedRanges() && !running, "请先读取并校验时间表并保留至少一个时段");
  const stopButton = $("ps-stop-scan");
  if (stopButton) stopButton.hidden = !running;
  renderProductScanInspector(state.productScan.groups || [], state.productScan.timeline || [], state.productScan.feedback || []);
}

function productScanCoverageLabel(status) {
  if (status === "covered") return "完整覆盖";
  if (status === "partial") return "部分覆盖";
  return "本批未覆盖";
}

function productScanSourceLabel(value) {
  const name = String(value || "");
  const match = name.match(/20\d{6}(\d{2})(\d{2})(?:\d{2})?/);
  if (match) return `${match[1]}:${match[2]}:${match[3]}`;
  return name.replace(/\.[^.]+$/, "").slice(-22) || "源视频";
}

function productScanScheduleRange(range) {
  const mode = productScanAlignmentMode();
  const offset = parseProductScanOffset($("ps-video-start-offset")?.value);
  const start = Number(range.start || 0);
  const end = Number(range.end || 0);
  if (mode === "manual" && offset !== null) {
    return `直播 ${formatProductScanTime(start + offset)}–${formatProductScanTime(end + offset)}`;
  }
  return `本批 ${formatProductScanTime(start)}–${formatProductScanTime(end)}`;
}

function renderProductScanInspector(groups, timeline = [], feedback = []) {
  const summary = $("ps-alignment-summary");
  if (!summary) return;
  const mode = productScanAlignmentMode();
  const timeBasis = productScanTimeBasis();
  const offset = parseProductScanOffset($("ps-video-start-offset")?.value);
  const ready = productScanIsReady();
  if (!ready || !groups.length) {
    summary.classList.add("is-empty");
    summary.innerHTML = mode === "manual" && offset !== null
      ? `<strong>将按片段起点 ${formatProductScanTime(offset)} 对齐</strong><p>读取时间表后，会把排品表中的直播时间换算为当前文件内的切分时间。</p>`
      : mode === "auto"
        ? "<strong>等待文件名自动对齐</strong><p>填写直播开播时间后，系统会从视频文件名识别片段开始时刻并进行换算。</p>"
        : "<strong>尚未校验时间表</strong><p>选择视频后，填写片段在整场直播中的开始位置；结果会显示在这里。</p>";
    return;
  }
  const records = groups.flatMap((group) => Array.isArray(group.ranges) ? group.ranges : []);
  const segmentCount = records.length;
  const covered = records.filter((record) => record.status === "covered").length;
  const partial = records.filter((record) => record.status === "partial").length;
  const missing = records.filter((record) => record.status === "missing").length;
  const exportGroups = groups.filter((group) => group.status !== "missing").length;
  const selectableRangeCount = productScanSelectableRangeKeys(groups).length;
  const selectedRangeCount = productScanSelectedRangeKeys(groups).length;
  const sourceSummary = timeline.length
    ? timeline.map((item) => `<span title="${escapeHtml(String(item.name || ""))}">${escapeHtml(productScanSourceLabel(item.name))} · ${escapeHtml(formatProductScanTime(item.start))}–${escapeHtml(formatProductScanTime(item.end))}</span>`).join("")
    : "<span>正在读取视频时长</span>";
  summary.classList.remove("is-empty");
  summary.innerHTML = `
    <strong>已确认文件范围</strong>
    <p>${timeBasis === "clock" ? "时钟时间已换算为直播进度" : "表格按开播后时长识别"} · ${mode === "manual" ? `本批从直播第 ${formatProductScanTime(offset || 0)} 开始` : "已按文件名时间戳自动对齐"} · 仅切割已勾选的时段。</p>
    <div class="alignment-summary-metrics"><span>${exportGroups} 个可导出商品</span><span class="is-selected">已选 ${selectedRangeCount}/${selectableRangeCount} 时段</span><span class="is-covered">完整 ${covered}</span><span class="is-partial">部分 ${partial}</span><span class="is-missing">未覆盖 ${missing}</span></div>
    <details class="product-scan-source-details"><summary>已导入 ${timeline.length} 段视频，查看文件范围</summary><div class="product-scan-source-summary" aria-label="已导入视频范围">${sourceSummary}</div></details>
  `;
}

function renderProductScanRanges(group, groupIndex) {
  const ranges = Array.isArray(group.ranges) ? group.ranges : [];
  if (!ranges.length) return "<div class=\"product-scan-range\"><span>暂未读取到可校验的时段</span></div>";
  return ranges.map((range, rangeIndex) => {
    const status = String(range.status || "missing");
    const parts = Array.isArray(range.parts) ? range.parts : [];
    const selectable = productScanRangeIsSelectable(range);
    const rangeKey = productScanRangeKey(groupIndex, rangeIndex);
    const selected = selectable && state.productScan.selectedRangeKeys.has(rangeKey);
    const target = productScanScheduleRange(range);
    const actual = parts.length
      ? parts.map((part) => `${productScanSourceLabel(part.video)} 文件 ${formatProductScanTime(part.file_start)}–${formatProductScanTime(part.file_end)}`).join(" · ")
      : "无已导入视频覆盖";
    const duration = status === "missing"
      ? `需 ${formatProductScanTime(range.expected_duration)}`
      : status === "partial"
        ? `可切 ${formatProductScanTime(range.covered_duration)} / 需 ${formatProductScanTime(range.expected_duration)}`
        : `预计 ${formatProductScanTime(range.expected_duration)}`;
    const route = `排表 ${target} → ${actual}`;
    const selection = selectable
      ? `<label class="product-scan-selection product-scan-range-selection" title="${selected ? "取消后此时段不会切割" : "勾选后切割此时段"}"><input type="checkbox" data-product-scan-select-range="${rangeKey}" ${selected ? "checked" : ""}><span>切割</span></label>`
      : `<span class="product-scan-selection is-disabled">不导出</span>`;
    return `<div class="product-scan-range is-${escapeHtml(status)}">${selection}<b>${escapeHtml(productScanCoverageLabel(status))}</b><span class="product-scan-range-route" title="${escapeHtml(route)}">${escapeHtml(route)}</span><span class="product-scan-range-duration">${escapeHtml(duration)}</span></div>`;
  }).join("");
}

async function submitProductScanRead() {
  const payload = collectFeaturePayload("product-scan-read");
  const validationKey = productScanFormKey();
  await saveFeaturePreferences();
  await runPreflight("product-scan-read", payload, "product-scan");
  state.productScan.status = "working";
  state.productScan.validationKey = validationKey;
  state.productScan.selectionKey = "";
  state.productScan.selectedRangeKeys = new Set();
  state.productScan.groups = [];
  state.productScan.timeline = [];
  state.productScan.feedback = [];
  syncProductScanFlow();
  try {
    const result = await api("/api/product-scan-read/start", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    toast(result.message || "正在读取并校验时间表", "success");
    await refreshTasks();
    const task = await waitForTaskComplete(result.task_id, "product-scan");
    if (task.status !== "completed") {
      throw new Error(task.error || task.message || "时间表校验未完成，请查看运行详情。");
    }
    if (validationKey !== productScanFormKey()) {
      state.productScan.status = "idle";
      state.productScan.selectionKey = "";
      state.productScan.selectedRangeKeys = new Set();
      state.productScan.groups = [];
      state.productScan.timeline = [];
      state.productScan.feedback = [];
      syncProductScanFlow();
      toast("素材或时间设置已经改变，请重新读取并校验。", "warning");
      return;
    }
    state.productScan.status = "ready";
    await loadScanResults();
    syncProductScanFlow();
    toast("时间已校验，可确认文件内时间后开始分割。", "success");
  } catch (error) {
    state.productScan.status = "idle";
    state.productScan.selectionKey = "";
    state.productScan.selectedRangeKeys = new Set();
    state.productScan.groups = [];
    state.productScan.timeline = [];
    state.productScan.feedback = [];
    syncProductScanFlow();
    throw error;
  }
}

async function submitProductScan() {
  if (!productScanIsReady()) throw new Error("请先读取并校验时间表，再开始分割。");
  if (!productScanHasSelectedRanges()) throw new Error("请在右侧勾选至少一个可切割的商品或时段。");
  await submitFeature("product-scan");
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
  rememberDesktopVideoDropTarget(activePageDesktopVideoDropTarget());
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
      if (action === "pick-video-folder") await pickVideoFolder(target.dataset.target, target);
      if (action === "pick-file") await pickFile(target.dataset.target, target.dataset.kind || "file");
      if (action === "pick-directory") await pickDirectory(target.dataset.target);
      if (action === "open-path") await openPath(target.dataset.target);
      if (action === "open-task-output") await openPathValue(target.dataset.path);
      if (action === "stop-scope") await stopScope(target.dataset.scope || state.page);
      if (action === "clear-video-list") clearVideoList(target.dataset.target);
      if (action === "remove-video") removeVideoPath(target.dataset.target, Number(target.dataset.index));
      if (action === "retry-video-inspection") retryVideoInspection(target.dataset.target, Number(target.dataset.index));
      if (action === "move-video") moveVideoPath(target.dataset.target, Number(target.dataset.index), Number(target.dataset.direction));
      if (action === "mix-new-group") newMixGroup();
      if (action === "mix-toggle-group-select") toggleMixGroupSelection(Number(target.dataset.index), Boolean(target.checked));
      if (action === "mix-toggle-all-group-select") toggleAllMixGroupSelection(Boolean(target.checked));
      if (action === "mix-delete-selected-groups") deleteSelectedMixGroups();
      if (action === "mix-select-group") selectMixGroup(Number(target.dataset.index));
      if (action === "preview-workbench-stage") setPreviewWorkbenchStage(target.dataset.previewScope || "smart", target.dataset.value);
      if (action === "preview-triage-role") setPreviewTriageRole(target.dataset.previewScope || "smart", target.dataset.value);
      if (action === "preview-triage-topic") setPreviewTriageTopic(target.dataset.previewScope || "smart", target.dataset.value);
      if (action === "preview-triage-filter") setPreviewTriageFilter(target.dataset.previewScope || "smart", target.dataset.value);
      if (action === "preview-triage-focus") setPreviewTriageActive(target.dataset.previewScope || "smart", Number(target.dataset.previewIndex));
      if (action === "preview-triage-prev") movePreviewTriage(target.dataset.previewScope || "smart", -1);
      if (action === "preview-triage-next") movePreviewTriage(target.dataset.previewScope || "smart", 1);
      if (action === "preview-triage-keep") keepPreviewTriageCandidate(target.dataset.previewScope || "smart");
      if (action === "preview-triage-skip") skipPreviewTriageCandidate(target.dataset.previewScope || "smart");
      if (action === "preview-triage-undo") undoPreviewTriageAction(target.dataset.previewScope || "smart");
      if (action === "preview-workbench-inspect-clip") inspectPreviewWorkbenchClip(Number(target.dataset.previewIndex), target.dataset.previewScope || "smart");
      if (action === "preview-assembly-move") movePreviewAssemblyClip(target.dataset.previewScope || "smart", Number(target.dataset.previewIndex), Number(target.dataset.direction));
      if (action === "preview-assembly-auto-arrange") autoArrangePreviewAssembly(target.dataset.previewScope || "smart");
      if (action === "preview-duration-fit") autoFitPreviewDuration(target.dataset.previewScope || "smart");
      if (action === "preview-overview-toggle") togglePreviewOverviewDetails(target.dataset.previewScope || "smart");
      if (action === "preview-overview-locate") locatePreviewOverviewIssue(target.dataset.previewScope || "smart", Number(target.dataset.previewIndex));
      if (action === "preview-director-chapter-focus") focusPreviewDirectorChapter(target.dataset.previewScope || "smart", target.dataset.chapterId || "");
      if (action === "preview-director-alternatives-toggle") togglePreviewDirectorAlternatives(target.dataset.previewScope || "smart");
      if (action === "preview-director-alternatives-close") togglePreviewDirectorAlternatives(target.dataset.previewScope || "smart", false);
      if (action === "preview-assembly-remove") removePreviewAssemblyCandidate(target.dataset.previewScope || "smart", Number(target.dataset.previewIndex));
      if (action === "start-smart-preview") await startSmartPreview();
      if (action === "start-commerce-director-preview") await startCommerceDirectorPreview();
      if (action === "select-commerce-director-story") await selectCommerceDirectorStory(target.dataset.storyId || "");
      if (action === "select-commerce-director-strategy") await selectCommerceDirectorStrategy(
        target.dataset.directorStrategyId || "",
        target.dataset.additionalAiCall === "true",
        target.dataset.previewScope || "smart",
      );
      if (action === "select-commerce-director-proposal") selectCommerceDirectorProposal(target.dataset.directorStrategyId || "");
      if (action === "generate-commerce-director-strategies") await generateCommerceDirectorStrategies();
      if (action === "play-commerce-director-review") await playCommerceDirectorReview(target.dataset.directorPreviewId || "");
      if (action === "select-commerce-director-result") selectCommerceDirectorResult(target.dataset.directorPreviewId || "");
      if (action === "close-commerce-director-studio") closeCommerceDirectorStudio();
      if (action === "restart-commerce-director-preview") await startCommerceDirectorPreview();
      if (action === "filter-commerce-director-evidence") setCommerceDirectorEvidenceFilter(target.dataset.evidenceFilter || "all");
      if (action === "toggle-commerce-director-library") toggleCommerceDirectorLibrary();
      if (action === "focus-commerce-director-evidence") focusCommerceDirectorEvidence(target.dataset.evidenceId || "");
      if (action === "director-plan-focus-row") focusCommerceDirectorDraftRow(target.dataset.draftKey || "", Number(target.dataset.draftIndex));
      if (action === "director-plan-replace") openCommerceDirectorReplacement(target.dataset.draftKey || "", Number(target.dataset.draftIndex));
      if (action === "director-plan-move") moveCommerceDirectorDraft(target.dataset.draftKey || "", Number(target.dataset.draftIndex), Number(target.dataset.direction));
      if (action === "director-plan-remove") removeCommerceDirectorDraftItem(target.dataset.draftKey || "", Number(target.dataset.draftIndex));
      if (action === "director-plan-add-evidence") addCommerceDirectorDraftEvidence(target.dataset.draftKey || "", target.dataset.evidenceId || "");
      if (action === "director-plan-place-evidence") placeCommerceDirectorDraftEvidence(target.dataset.draftKey || "", target.dataset.evidenceId || "", target.dataset.evidencePlacement || "end");
      if (action === "director-plan-save") saveCommerceDirectorDraft(target.dataset.draftKey || "");
      if (action === "director-plan-return-list") returnCommerceDirectorPlanList();
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
      if (action === "add-content-policy-rule") addContentPolicyRule();
      if (action === "remove-content-policy-rule") removeContentPolicyRule(target);
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
      if (action === "product-scan-read") await submitProductScanRead();
      if (action === "product-scan-start") await submitProductScan();
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

  document.body.addEventListener("input", (event) => {
    const target = event.target.closest("[data-preview-triage-search]");
    if (target) setPreviewTriageSearch(target.dataset.previewTriageSearch || "smart", target.value);
    const planText = event.target.closest("[data-director-plan-text]");
    if (planText) updateCommerceDirectorDraftText(planText.dataset.draftKey || "", Number(planText.dataset.draftIndex), planText.value);
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
  $("s-subtitle-opacity")?.addEventListener("input", syncSubtitleStyleValues);
  $("s-subtitle-blur")?.addEventListener("input", syncSubtitleStyleValues);
  $("s-subtitle-position-percent")?.addEventListener("input", syncSubtitleStyleValues);
  $("s-subtitle-font-family")?.addEventListener("change", syncSubtitleStyleValues);
  $("s-subtitle-font-color")?.addEventListener("change", syncSubtitleStyleValues);
  $("s-subtitle-text-effect")?.addEventListener("change", syncSubtitleStyleValues);
  $("s-ui-theme")?.addEventListener("change", (event) => {
    applyTheme(event.target.value);
  });
  $("vs-split-mode")?.addEventListener("change", syncVideoSplitMode);

  bindVideoDropzones();
  bindDesktopNativeVideoDropBridge();
  bindFileDropTargets();
  injectVideoFolderPickers();
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
  ["sc", "mix"].forEach((prefix) => refreshCategoryAiControls(prefix));
  document.querySelectorAll("[data-ai-preset]").forEach((select) => {
    select.addEventListener("change", () => {
      applyAiPreset(select.dataset.aiPreset, select.value);
    });
  });

  ["sc", "mix"].forEach((prefix) => {
    [`${prefix}-primary-category`, `${prefix}-goal`, `${prefix}-hook-style`, `${prefix}-ending-style`, `${prefix}-secondary-category`, `${prefix}-leaf-category`, `${prefix}-main-product`].forEach((id) => {
      $(id)?.addEventListener("change", () => markAiPresetCustom(prefix));
    });
    document.body.addEventListener("change", (event) => {
      const control = event.target?.dataset?.aiControl;
      if (control === `${prefix}-selling` || control === `${prefix}-avoid`) markAiPresetCustom(prefix);
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
  setSelectIfPresent(`${prefix}-primary-category`, preset.primary_category);
  syncPrimaryCategory(prefix);
  refreshCategoryAiControls(prefix, {
    preferredSecondary: preset.secondary_category,
    preferredLeaf: preset.leaf_category,
    preferredGoal: preset.goal,
    preferredHook: preset.hook,
    preferredEnding: preset.ending,
    selectedSelling: preset.selling,
    selectedAvoid: preset.avoid,
  });
  setSelectIfPresent(`${prefix}-secondary-category`, preset.secondary_category);
  const leafInput = $(`${prefix}-leaf-category`);
  if (leafInput && preset.leaf_category !== undefined) leafInput.value = preset.leaf_category || "";
  const mainProductInput = $(`${prefix}-main-product`);
  if (mainProductInput && preset.main_product !== undefined) mainProductInput.value = preset.main_product || "";
  setSelectIfPresent(`${prefix}-goal`, preset.goal);
  setSelectIfPresent(`${prefix}-hook-style`, preset.hook);
  setSelectIfPresent(`${prefix}-ending-style`, preset.ending);
  setCheckedValues(`${prefix}-selling`, sellingWithLegacyFocus(preset.selling, preset.focus));
  setCheckedValues(`${prefix}-avoid`, preset.avoid);
  const customInput = $(`${prefix}-selling-custom`);
  if (customInput) customInput.value = (preset.selling_custom || []).join("，");
  const panel = document.querySelector(`[data-summary-kind="ai"][data-summary-prefix="${prefix}"]`);
  if (panel) updatePanelSummary(panel);
  toast(`已套用「${preset.label}」导演预设`, "success");
}

function customSellingValues(prefix) {
  const customInput = $(`${prefix}-selling-custom`);
  if (!customInput) return [];
  return customInput.value
    .split(/[,，、\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function sellingWithLegacyFocus(values, focus) {
  const result = Array.isArray(values) ? [...values] : [];
  const legacy = String(focus || "").trim();
  if (legacy && !["自动", "默认", "auto"].includes(legacy) && !result.includes(legacy)) result.push(legacy);
  return result;
}

function collectCurrentAiPreset(prefix, label) {
  return {
    label,
    primary_category: $(`${prefix}-primary-category`)?.value || "服饰内衣",
    category: backendCategoryForPrimary(primaryCategoryValue(prefix)),
    secondary_category: $(`${prefix}-secondary-category`)?.value || "自动识别",
    leaf_category: $(`${prefix}-leaf-category`)?.value.trim() || "",
    main_product: $(`${prefix}-main-product`)?.value.trim() || "",
    goal: $(`${prefix}-goal`)?.value || "自动",
    hook: $(`${prefix}-hook-style`)?.value || "自动",
    ending: $(`${prefix}-ending-style`)?.value || "自动",
    selling: checkedControlValues(`${prefix}-selling`),
    selling_custom: customSellingValues(prefix),
    avoid: checkedControlValues(`${prefix}-avoid`),
  };
}

function saveCurrentAiPreset(prefix) {
  if (!prefix) return;
  const label = prompt("给这套 AI 导演参数起个名字：", "我的导演预设");
  if (!label || !label.trim()) return;
  const custom = readCustomAiPresets();
  const key = `custom-${Date.now()}`;
  custom[key] = collectCurrentAiPreset(prefix, label.trim().slice(0, 18));
  writeCustomAiPresets(custom);
  refreshAiPresetOptions();
  const select = $(`${prefix}-ai-preset`);
  if (select) select.value = key;
  refreshCategoryAiControls(prefix);
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
  refreshCategoryAiControls(prefix);
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
  (group.prefixes || []).forEach((prefix) => refreshCategoryAiControls(prefix));
  group.ids.forEach((id) => setControlValue(id, values[id]));
  const ai = saved.ai || {};
  (group.prefixes || []).forEach((prefix) => {
    if (Array.isArray(ai[`${prefix}-selling`]) || values[`${prefix}-focus`]) {
      setCheckedValues(`${prefix}-selling`, sellingWithLegacyFocus(ai[`${prefix}-selling`], values[`${prefix}-focus`]));
    }
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
  ["sc", "mix", "dedup"].forEach((prefix) => refreshPipPool(prefix));
  ["sc", "mix"].forEach((prefix) => {
    syncPrimaryCategory(prefix);
    refreshCategoryAiControls(prefix);
    refreshDedupCustomVisibility(prefix);
  });
  document.querySelectorAll("[data-collapsible-panel]").forEach((panel) => updatePanelSummary(panel));
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

  document.body.addEventListener("volumechange", (event) => {
    const video = event.target?.closest?.("[data-preview-inline-player]");
    if (video) savePreviewInlineAudioPreference(video);
  }, true);

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
              <span class="log-progress-title">当前任务</span>
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

function formatBatchProgress({ total, done = 0, succeeded = null, current = 0, failed = 0, insufficient = 0, label = "", status = "" } = {}) {
  const safeTotal = Math.max(0, Math.floor(batchNumber(total)));
  if (safeTotal <= 1) return null;
  const safeFailed = Math.max(0, Math.floor(batchNumber(failed)));
  const safeInsufficient = Math.max(0, Math.min(safeFailed, Math.floor(batchNumber(insufficient))));
  const safeOtherFailed = Math.max(0, safeFailed - safeInsufficient);
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
  if (safeInsufficient > 0) parts.push(`内容不足 ${safeInsufficient}`);
  if (safeOtherFailed > 0) parts.push(`其他失败 ${safeOtherFailed}`);
  const titleParts = [`共 ${safeTotal} 个`, `已完成 ${safeDone} 个`];
  if (safeCurrent > 0 && safeDone < safeTotal) titleParts.push(`当前第 ${safeCurrent} 个`);
  if (safeInsufficient > 0) titleParts.push(`内容不足 ${safeInsufficient} 个`);
  if (safeOtherFailed > 0) titleParts.push(`其他失败 ${safeOtherFailed} 个`);
  if (cleanLabel) titleParts.push(cleanLabel);
  return {
    total: safeTotal,
    done: safeDone,
    succeeded: safeSucceeded,
    current: safeCurrent,
    failed: safeFailed,
    insufficient: safeInsufficient,
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
    insufficient: task.batch_insufficient,
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
      insufficient: Math.max(batchNumber(structured.insufficient), batchNumber(inferred.insufficient)),
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
  const insufficient = Math.max(...related.map((item) => batchNumber(item.batch_insufficient)), 0);
  const succeeded = Math.max(...related.map((item) => batchNumber(item.batch_succeeded)), Math.max(0, done - failed));
  return formatBatchProgress({ total, done, succeeded, failed, insufficient, status: "completed" });
}

function progressFromTask(task) {
  const scope = task.scope || "settings";
  const status = task.status || "queued";
  const text = [task.title, task.message, task.error].filter(Boolean).join(" ");
  const inferred = inferProgressStage(text);
  const structuredLabel = String(task.phase_label || "").trim();
  const batch = batchProgressFromTask(task);
  const overallPercent = taskOverallPercent(task);
  const itemPercent = taskItemPercent(task, batch);
  const hasExplicitProgress = Number.isFinite(Number(task.progress));
  const itemCurrent = batch?.current || (Number(task.item_total || 0) > 1 ? Number(task.item_current || 0) : 0);
  const statusLabels = {
    queued: "排队中",
    running: structuredLabel || task.message || inferred.label || "运行中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已停止",
  };

  if (status === "completed") return { taskId: task.id, label: statusLabels.completed, percent: 100, overallPercent: 100, itemPercent: 100, itemCurrent, status, source: "task", batch };
  if (status === "failed") return { taskId: task.id, label: task.error || statusLabels.failed, percent: 100, overallPercent, itemPercent: 100, itemCurrent, status, source: "task", batch };
  if (status === "cancelled") return { taskId: task.id, label: statusLabels.cancelled, percent: 100, overallPercent, itemPercent: 100, itemCurrent, status, source: "task", batch };
  if (status === "queued") {
    const percent = hasExplicitProgress ? itemPercent : 0;
    return { taskId: task.id, label: task.message || statusLabels.queued, percent, overallPercent, itemPercent: percent, itemCurrent, status, source: "task", batch };
  }

  const previous = state.progressByScope[scope];
  const sameItem = previous?.taskId === task.id && Number(previous?.itemCurrent || 0) === Number(itemCurrent || 0);
  const previousPercent = sameItem ? previous.percent || 0 : 0;
  return {
    taskId: task.id,
    label: statusLabels.running,
    percent: Math.max(previousPercent, hasExplicitProgress ? itemPercent : inferred.percent || 10),
    overallPercent,
    itemPercent,
    itemCurrent,
    status,
    source: "task",
    batch,
  };
}

function batchProgressFromText(text, status = "") {
  const value = String(text || "").trim();
  const success = value.match(/(?:^|[：:]\s*)成功\s*(\d+)\s*\/\s*(\d+)(?:\s*[个组])?(?:\s|[，。·]|$)/);
  if (success) {
    const succeeded = Number(success[1]);
    const total = Number(success[2]);
    const insufficient = Number(value.match(/内容不足\s*(\d+)/)?.[1] || 0);
    return formatBatchProgress({
      done: status === "completed" ? total : succeeded,
      succeeded,
      failed: Math.max(0, total - succeeded),
      insufficient,
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

function taskOverallPercent(task) {
  const value = Number(task?.overall_percent);
  return Number.isFinite(value) ? clampProgressPercent(value) : taskPercent(task);
}

function taskItemPercent(task, batch = null) {
  const explicit = Number(task?.item_progress);
  if (Number.isFinite(explicit)) return clampProgressPercent(explicit);

  const phaseCurrent = Number(task?.phase_current);
  const phaseTotal = Number(task?.phase_total);
  if (Number.isFinite(phaseCurrent) && Number.isFinite(phaseTotal) && phaseTotal > 0) {
    return clampProgressPercent((phaseCurrent / phaseTotal) * 100);
  }

  if (!hasBatchProgress(batch)) return taskOverallPercent(task);
  const total = Math.max(1, Math.floor(batchNumber(batch.total, 1)));
  const done = Math.max(0, Math.min(total, Math.floor(batchNumber(batch.done))));
  const current = Math.max(0, Math.min(total, Math.floor(batchNumber(batch.current))));
  if (!current || done >= total) return done >= total ? 100 : 0;

  // Compatibility for task records created before item_progress existed.
  const estimated = ((taskOverallPercent(task) - 10) / 84) * total - done;
  return clampProgressPercent(estimated * 100);
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
  return /AI\s*选片预览|AI选片预览|AI\s*导演预览|AI导演预览|商业导演实验预览|混剪\s*AI\s*(?:选片|导演)预览|混剪AI(?:选片|导演)预览/.test(text);
}

function isPreviewOutputTask(task) {
  return /预览成片|预览混剪/.test(taskSummaryText(task));
}

function totalProgressFromSummary(progress, task, batch) {
  const progressStatus = progress?.status || "idle";
  const status = progressStatus !== "idle" ? progressStatus : (task?.status || batch?.status || "idle");
  const overallPercent = clampProgressPercent(progress?.overallPercent, taskOverallPercent(task));
  const itemPercent = clampProgressPercent(progress?.itemPercent ?? progress?.percent, taskItemPercent(task, batch));
  if (batch?.total > 1) {
    const total = Math.max(1, Math.floor(batchNumber(batch.total, 1)));
    const done = Math.max(0, Math.min(total, Math.floor(batchNumber(batch.done))));
    const completedPercent = Math.round((done / total) * 100);
    const current = Math.max(0, Math.min(total, Math.floor(batchNumber(batch.current))));
    const percent = ["failed", "cancelled"].includes(status)
      ? completedPercent
      : status === "completed"
        ? 100
        : current > 0
          ? Math.round(((done + (itemPercent / 100)) / total) * 100)
          : completedPercent;
    return {
      percent,
      text: `${percent}%`,
      status,
    };
  }
  if (!task) {
    return { percent: overallPercent, text: `${overallPercent}%`, status };
  }
  if (status === "failed") {
    return { percent: Math.min(99, overallPercent), text: "失败", status };
  }
  if (status === "cancelled") {
    return { percent: Math.min(99, overallPercent), text: "停止", status };
  }
  if (isPreviewOutputTask(task)) {
    const percent = status === "completed" ? 100 : Math.min(99, 50 + Math.round(overallPercent * 0.5));
    return { percent, text: `${percent}%`, status };
  }
  if (isAiSelectionPreviewTask(task)) {
    const percent = status === "completed" ? 50 : Math.min(50, Math.round(overallPercent * 0.5));
    return { percent, text: `${percent}%`, status };
  }
  if (status === "completed" || taskHasOutput(task)) {
    const percent = status === "completed" ? 100 : overallPercent;
    return { percent, text: `${percent}%`, status };
  }
  return { percent: overallPercent, text: `${overallPercent}%`, status };
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
  if (/AI未满足时长|时长未达标|超过目标上限/.test(text)) return "AI片单未落入目标时长范围，已跳过该素材并继续批量任务。";
  if (/有效内容不足|内容不足|目标至少|目标下限/.test(text)) return "该素材可用卖点较少，可缩短目标时长或补充素材。";
  if (/素材|视频|至少|添加|不存在|路径/.test(text)) return "请补充素材或检查文件路径。";
  if (/402|余额不足|quota|balance|充值/i.test(text)) return "请到模型平台充值，或在设置中更换可用 API Key。";
  if (/API|Key|模型|DeepSeek|OpenAI|连接|网络|代理/.test(text)) return "请到设置里检查 AI 配置和网络。";
  if (/权限|目录|写入|保存|输出/.test(text)) return "请换一个可写的输出目录。";
  if (/ffmpeg|编码|转码|裁剪|合成|导出/i.test(text)) return "请检查源视频是否可播放，必要时换稳定转码。";
  return "打开高级诊断查看详细原因。";
}

function failureDetailsForTask(task) {
  if (!task) return [];
  const details = Array.isArray(task.batch_failure_details) ? task.batch_failure_details : [];
  const normalized = details.map((item, index) => ({
    label: String(item?.label || `失败任务 ${index + 1}`).trim(),
    message: String(item?.message || "任务处理失败。").trim(),
    code: String(item?.code || "processing_failed").trim(),
  })).filter((item) => item.label || item.message);
  if (normalized.length || !task.error) return normalized;
  return [{
    label: String(task.title || "任务").trim(),
    message: String(task.error || task.message || "任务处理失败。").trim(),
    code: "processing_failed",
  }];
}

function historyFailureIssueForScope(scope) {
  const grouped = new Map();
  for (const item of state.outputHistory || []) {
    if (String(item?.scope || "") !== String(scope || "")) continue;
    const taskId = String(item?.task_id || "").trim();
    const key = taskId || `${item?.created_at || ""}:${item?.title || ""}`;
    const previous = grouped.get(key);
    if (!previous || Number(item?.created_at || 0) > Number(previous.created_at || 0)) {
      grouped.set(key, item);
    }
  }
  const latest = [...grouped.values()].sort((a, b) => Number(b?.created_at || 0) - Number(a?.created_at || 0))[0];
  const details = Array.isArray(latest?.batch_failure_details) ? latest.batch_failure_details : [];
  if (!latest || !details.length) return null;
  const first = details[0] || {};
  const message = String(first.message || "部分素材未生成成片。");
  return {
    title: "最近批量存在失败任务",
    message,
    suggestion: issueSuggestion(message),
    tone: "warning",
    details: details.map((item, index) => ({
      label: String(item?.label || `失败任务 ${index + 1}`),
      message: String(item?.message || "任务处理失败。"),
      code: String(item?.code || "processing_failed"),
    })),
  };
}

function issueForScope(scope, scopedTasks) {
  const failed = newestTask(scopedTasks.filter((task) => task.status === "failed"));
  if (failed) {
    const details = failureDetailsForTask(failed);
    const message = details[0]?.message || failed.error || failed.message || "任务处理失败。";
    return {
      title: failed.title || "任务失败",
      message,
      suggestion: issueSuggestion(message),
      tone: "error",
      details,
    };
  }

  const partial = newestTask(scopedTasks.filter((task) => Number(task.batch_failed || 0) > 0));
  if (partial) {
    const details = failureDetailsForTask(partial);
    const first = details[0] || {};
    const message = first.message || partial.message || "部分素材未生成成片。";
    const hasInsufficient = Number(partial.batch_insufficient || 0) > 0;
    const hasDurationMismatch = details.some((item) => item?.code === "duration_mismatch");
    let title = "部分素材处理失败";
    if (hasInsufficient && hasDurationMismatch) title = "部分素材内容或时长不符合";
    else if (hasInsufficient) title = "部分素材内容不足";
    else if (hasDurationMismatch) title = "部分素材时长未达标";
    return {
      title,
      message,
      suggestion: issueSuggestion(message),
      tone: "warning",
      details,
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
      details: [],
    };
  }
  if (!active) {
    const historicalIssue = historyFailureIssueForScope(scope);
    if (historicalIssue) return historicalIssue;
  }
  return null;
}

function renderIssueFailureRows(details = []) {
  const visible = details.slice(0, 3);
  if (!visible.length) return "";
  const rows = visible.map((item) => `
    <div class="run-summary-failure-item" title="${escapeHtml(item.message)}">
      <strong>${escapeHtml(item.label || "失败任务")}</strong>
      <span>${escapeHtml(item.message || "任务处理失败。")}</span>
    </div>
  `).join("");
  const more = details.length > visible.length
    ? `<span class="run-summary-failure-more">另有 ${details.length - visible.length} 个失败任务，请打开高级诊断查看。</span>`
    : "";
  return `<div class="run-summary-failure-list">${rows}${more}</div>`;
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
        ${renderIssueFailureRows(issue.details)}
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
  const overallPercent = Math.max(0, Math.min(100, Math.round(Number.isFinite(Number(effectiveProgress.overallPercent))
    ? Number(effectiveProgress.overallPercent)
    : (sameTask ? Number(previous.overallPercent || percent) : percent))));
  const status = effectiveProgress.status || "idle";
  const label = effectiveProgress.label || "等待任务";
  state.progressByScope[scope] = { ...effectiveProgress, percent, overallPercent, label, status };

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
    const outputNaming = fieldText(`${prefix}-output-naming`, "加时间戳");
    const duration = fieldText(`${prefix}-duration`);
    const tolerance = fieldText(`${prefix}-duration-tolerance`, "自动");
    const versions = fieldText(`${prefix}-versions`);
    const dedup = fieldText(`${prefix}-dedup`);
    const flags = [
      checkedText(`${prefix}-subtitle`, "字幕"),
      checkedText(`${prefix}-crop`, "裁切"),
      checkedText(`${prefix}-kenburns`, "缩放"),
      checkedText(`${prefix}-mirror`, "镜像"),
    ].filter(Boolean).join("、") || "基础模式";
    text = `${outputNaming} · ${versions}版 · ${duration} · 容差${tolerance} · ${dedup}去重 · ${flags}`;
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
    const primary = fieldText(`${prefix}-primary-category`, "");
    const secondary = fieldText(`${prefix}-secondary-category`, "自动识别");
    const leaf = fieldText(`${prefix}-leaf-category`, "");
    const goal = fieldText(`${prefix}-goal`, "自动");
    const selling = selectedAiValues(`${prefix}-selling`);
    const avoid = selectedAiValues(`${prefix}-avoid`);
    const ruleText = [
      selling.length ? `优先${selling.slice(0, 3).join("、")}` : "",
      avoid.length ? `排除${avoid.slice(0, 2).join("、")}` : "",
    ].filter(Boolean).join(" · ");
    const categoryPath = [primary, secondary, leaf].filter(Boolean).join(" / ");
    text = `${preset} · ${categoryPath || "自动识别"} · 导演方向${goal}${ruleText ? ` · ${ruleText}` : ""}`;
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
    if ($("runtime-settings-version")) $("runtime-settings-version").textContent = `v${data.version || ""}`;
    renderZeroCopyTestMarker(data);
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
  populateSubtitleFontOptions(
    data.subtitle_font_options,
    data.subtitle_font_family,
    data.subtitle_font_resolution,
  );
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
  syncSubtitleStyleValues();
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
  data.subtitle_opacity = Math.max(20, Math.min(100, Number(data.subtitle_opacity || 70)));
  data.subtitle_blur = Math.max(0, Math.min(100, Number(data.subtitle_blur || 10)));
  data.subtitle_position_percent = Math.max(8, Math.min(70, Number(data.subtitle_position_percent || 24)));
  data.ui_font_size = normalizeUiFontSize(data.ui_font_size);
  data.style_profile_strength = normalizeStyleProfileStrength(data.style_profile_strength);
  data.preference_weights = collectPreferenceWeights();
  data.ai_rules = collectAiRules();
  return data;
}

let aiSelectionSaveTimer = null;
let aiSelectionSaveInFlight = false;
let aiSelectionSaveQueued = false;
let aiSelectionSavedFingerprint = "";

function collectAiSelectionSettings() {
  return {
    preference_weights: collectPreferenceWeights(),
    style_profile_strength: normalizeStyleProfileStrength($("s-style-profile-strength")?.value),
    content_review_mode: $("s-content-review-mode")?.value || "off",
    m2_planner_mode: $("s-m2-planner-mode")?.value || "legacy",
    ai_rules: collectAiRules(),
  };
}

function aiSelectionSettingsFingerprint(payload = collectAiSelectionSettings()) {
  return JSON.stringify(payload);
}

function queueAiSelectionSave(delay = 0) {
  if (aiSelectionSaveTimer) window.clearTimeout(aiSelectionSaveTimer);
  aiSelectionSaveTimer = window.setTimeout(() => {
    aiSelectionSaveTimer = null;
    persistAiSelectionSettings();
  }, Math.max(0, delay));
}

async function persistAiSelectionSettings() {
  if (aiSelectionSaveInFlight) {
    aiSelectionSaveQueued = true;
    return;
  }
  const payload = collectAiSelectionSettings();
  const fingerprint = aiSelectionSettingsFingerprint(payload);
  if (fingerprint === aiSelectionSavedFingerprint) return;

  aiSelectionSaveInFlight = true;
  try {
    await api("/api/settings/ai-selection", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    aiSelectionSavedFingerprint = fingerprint;
  } catch (error) {
    toast("AI选片设置未保存，请检查服务后重试", "error");
  } finally {
    aiSelectionSaveInFlight = false;
    if (aiSelectionSaveQueued) {
      aiSelectionSaveQueued = false;
      queueAiSelectionSave(0);
    }
  }
}

function bindAiSelectionAutoSave() {
  const root = $("settings-selection");
  if (!root) return;
  const choiceSelector = [
    "#s-policy-price",
    "#s-policy-cta",
    "#s-policy-source-claim",
    "#s-policy-social-proof",
    "#s-policy-after-sale",
    "#s-policy-size-interaction",
    "#s-policy-live-interaction",
    "#s-content-review-mode",
    "#s-m2-planner-mode",
    "#s-style-profile-strength",
    "#s-rule-category-filter",
    "#s-rule-hook-cap",
    ".content-policy-rule-action",
  ].join(",");
  const textSelector = [
    ".content-policy-rule-text",
    "#s-rule-narrative",
    "#s-rule-custom-text",
  ].join(",");

  root.addEventListener("change", (event) => {
    const target = event.target;
    if (target?.matches(".content-policy-row select, .content-policy-rule-action")) {
      syncContentPolicyTone(target);
    }
    if (target?.matches(choiceSelector) || target?.matches("[data-pref-key]")) {
      queueAiSelectionSave(0);
    }
  });
  root.addEventListener("input", (event) => {
    const target = event.target;
    if (target?.matches("[data-pref-key]")) {
      queueAiSelectionSave(250);
    } else if (target?.matches(textSelector)) {
      queueAiSelectionSave(600);
    }
  });
  root.addEventListener("focusout", (event) => {
    if (event.target?.matches(textSelector)) queueAiSelectionSave(0);
  });
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
  return `
    <div class="style-profile-card">
      <div class="style-profile-head">
        <div>
          <strong>AI 已学到的剪辑风格</strong>
          <span>${escapeHtml(profile.status || "观察中")} · 分析 ${Number(profile.sample_count || 0)} 个片段选择</span>
        </div>
      </div>
      <div class="style-profile-grid">
        <section>
          <h3>你通常喜欢</h3>
          <div>${renderStyleProfilePills(profile.selling_preferences, "暂无稳定卖点")}</div>
        </section>
        <section>
          <h3>你通常避免</h3>
          <div>${renderStyleProfilePills(profile.avoid_preferences, "暂无稳定删除方向")}</div>
        </section>
      </div>
      <div class="style-profile-metrics">
        <span>卖点密度 <strong>${escapeHtml(metrics.selling_density || "观察中")}</strong></span>
        <span>节奏 <strong>${escapeHtml(metrics.rhythm || "观察中")}</strong></span>
        <span>上下文 <strong>${escapeHtml(metrics.context_length || "观察中")}</strong></span>
        <span>CTA <strong>${escapeHtml(metrics.cta_strength || "观察中")}</strong></span>
      </div>
      ${summary.length ? `<div class="style-profile-summary">${summary.slice(0, 2).map((line) => `<span>${escapeHtml(line)}</span>`).join("")}</div>` : ""}
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
    ${conflictRows ? `<div class="preference-conflicts"><strong>需要结合上下文判断</strong>${conflictRows}</div>` : ""}
    <p class="panel-note">历史偏好只做软参考；当前素材的语义完整性和商品证据始终优先。</p>
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
    clip_keywords: parseKeywordMap($(keywordFields.clip_keywords)?.value || ""),
    forbidden_phrases: parseKeywordList($(keywordFields.forbidden_phrases)?.value || ""),
    filler_words: parseKeywordList($(keywordFields.filler_words)?.value || ""),
    preference_keywords: parseKeywordMap($(keywordFields.preference_keywords)?.value || ""),
  };
}

function keywordValueChanged(current, baseline) {
  return JSON.stringify(current) !== JSON.stringify(baseline);
}

function collectKeywordChanges() {
  const current = collectKeywordConfig();
  const baseline = state.keywordConfig && typeof state.keywordConfig === "object" ? state.keywordConfig : {};
  return Object.fromEntries(
    Object.entries(current).filter(([key, value]) => keywordValueChanged(value, baseline[key]))
  );
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
  const changes = collectKeywordChanges();
  const result = await api("/api/keywords", {
    method: "POST",
    body: JSON.stringify({ changes }),
  });
  updateKeywordSummary(result);
  applyKeywordConfig(result.keywords || state.keywordConfig || {});
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
  if (!window.confirm("将清理成片/预览临时缓存和直播浏览器的可再生成缓存；不会删除登录信息、已导出的成片和原始素材。之后需要重新生成 AI 预览。确认继续？")) return;
  const result = await api("/api/cache/clear", { method: "POST", body: "{}" });
  toast(result.message || "缓存清理完成", result.failed?.length ? "warning" : "success");
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
  if (label) {
    label.textContent = preferenceWeightLabel(value);
    label.title = `当前偏好权重 ${value}`;
  }
}

function preferenceWeightLabel(value) {
  const weight = Number(value || 0);
  if (weight <= 0) return "弱化";
  if (weight <= 0.5) return "较少";
  if (weight <= 1) return "标准";
  if (weight <= 1.5) return "较优先";
  if (weight <= 2) return "优先";
  return "强优先";
}

function populateSubtitleFontOptions(fonts, currentValue, resolution = {}) {
  const select = $("s-subtitle-font-family");
  if (!select) return;
  const installed = Array.from(new Set((Array.isArray(fonts) ? fonts : [])
    .map((name) => String(name || "").trim())
    .filter(Boolean)));
  const current = String(currentValue || "").trim();
  const commonNames = [
    "Microsoft YaHei", "Microsoft YaHei Bold", "DengXian", "DengXian Bold",
    "SimHei", "SimSun", "KaiTi", "FangSong", "Noto Sans SC",
    "Noto Sans SC Medium", "Noto Sans SC Bold", "Source Han Serif SC Heavy",
  ];
  const byFold = new Map(installed.map((name) => [name.toLocaleLowerCase(), name]));
  const common = commonNames.map((name) => byFold.get(name.toLocaleLowerCase())).filter(Boolean);
  const commonSet = new Set(common.map((name) => name.toLocaleLowerCase()));
  const remaining = installed.filter((name) => !commonSet.has(name.toLocaleLowerCase()));
  select.innerHTML = "";

  function appendGroup(label, names) {
    if (!names.length) return;
    const group = document.createElement("optgroup");
    group.label = label;
    names.forEach((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      option.dataset.installed = "true";
      group.appendChild(option);
    });
    select.appendChild(group);
  }

  if (current && !byFold.has(current.toLocaleLowerCase())) {
    const group = document.createElement("optgroup");
    group.label = "当前设置";
    const option = document.createElement("option");
    option.value = current;
    option.textContent = `${current}（未检测到，将自动回退）`;
    option.dataset.installed = "false";
    group.appendChild(option);
    select.appendChild(group);
  }
  appendGroup("常用字幕字体", common);
  appendGroup("本机已安装字体", remaining);
  if (!select.options.length) {
    const option = document.createElement("option");
    option.value = current || "Microsoft YaHei Bold";
    option.textContent = current || "Microsoft YaHei Bold";
    select.appendChild(option);
  }
  select.value = byFold.get(current.toLocaleLowerCase()) || current || select.options[0].value;
  select.dataset.resolvedFont = String(resolution?.resolved || "");
  select.dataset.fontFallback = resolution?.fallback ? "true" : "false";
}

function syncSubtitleFontSize() {
  const input = $("s-subtitle-font-size");
  const label = $("s-subtitle-font-size-value");
  if (!input || !label) return;
  label.textContent = input.value;
  syncSubtitlePreview();
}

function syncSubtitleStyleValues() {
  const opacity = $("s-subtitle-opacity");
  const opacityLabel = $("s-subtitle-opacity-value");
  if (opacity && opacityLabel) opacityLabel.textContent = `${opacity.value}%`;
  const blur = $("s-subtitle-blur");
  const blurLabel = $("s-subtitle-blur-value");
  if (blur && blurLabel) blurLabel.textContent = blur.value;
  const position = $("s-subtitle-position-percent");
  const positionLabel = $("s-subtitle-position-value");
  if (position && positionLabel) positionLabel.textContent = position.value;
  syncSubtitlePreview();
}

function syncSubtitlePreview() {
  const preview = $("subtitle-preview-text");
  if (!preview) return;
  const rawFontSize = Number($("s-subtitle-font-size")?.value || 52);
  const fontSize = Math.max(11, Math.min(30, rawFontSize * 0.32));
  const opacity = Math.max(0.2, Math.min(1, Number($("s-subtitle-opacity")?.value || 70) / 100));
  const blur = Math.max(0, Math.min(4, Number($("s-subtitle-blur")?.value || 10) / 25));
  const position = Math.max(8, Math.min(70, Number($("s-subtitle-position-percent")?.value || 24)));
  const family = String($("s-subtitle-font-family")?.value || "Microsoft YaHei UI");
  const colorMap = { white: "#ffffff", yellow: "#ffe066", orange: "#ffad4d", red: "#ff6868", pink: "#ff8fbd", purple: "#c4a1ff", blue: "#78b7ff", green: "#7ad9a5", black: "#111827" };
  const color = colorMap[String($("s-subtitle-font-color")?.value || "white")] || "#ffffff";
  const effect = String($("s-subtitle-text-effect")?.value || "shadow");
  preview.style.fontFamily = `"${family}", "Microsoft YaHei UI", sans-serif`;
  preview.style.fontSize = `${fontSize}px`;
  preview.style.color = color;
  preview.style.opacity = String(opacity);
  preview.style.bottom = `${position}%`;
  preview.style.filter = blur ? `blur(${blur.toFixed(2)}px)` : "none";
  preview.style.textShadow = effect === "outline"
    ? "-2px -2px 0 #111827, 2px -2px 0 #111827, -2px 2px 0 #111827, 2px 2px 0 #111827"
    : "0 3px 8px rgba(0, 0, 0, .95), 0 1px 2px rgba(0, 0, 0, .95)";
  const stateLabel = $("subtitle-preview-state");
  if (stateLabel) stateLabel.textContent = `${rawFontSize}px · 高度${position}% · 不透明度${Math.round(opacity * 100)}%`;
  const fontStatus = $("subtitle-font-status");
  const selectedOption = $("s-subtitle-font-family")?.selectedOptions?.[0];
  if (fontStatus) {
    fontStatus.textContent = selectedOption?.dataset?.installed === "false"
      ? `未检测到“${family}”，正式成片将自动使用系统默认粗体。`
      : `当前使用本机字体“${family}”；保存后用于正式成片。`;
  }
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

const contentPolicyDefaults = Object.freeze({
  price: "block",
  cta: "block",
  inventory_pressure: "block",
  source_claim: "block",
  social_proof: "block",
  after_sale: "block",
  size_interaction: "block",
  live_interaction: "block",
  custom_rules: [],
});

const contentPolicyActions = new Set(["block", "body_only", "allow", "prefer"]);

function normalizeContentPolicy(policy = {}) {
  const source = policy && typeof policy === "object" ? policy : {};
  const normalized = {
    price: contentPolicyActions.has(source.price) ? source.price : contentPolicyDefaults.price,
    cta: contentPolicyActions.has(source.cta) ? source.cta : contentPolicyDefaults.cta,
    inventory_pressure: contentPolicyActions.has(source.inventory_pressure) ? source.inventory_pressure : contentPolicyDefaults.inventory_pressure,
    source_claim: contentPolicyActions.has(source.source_claim) ? source.source_claim : contentPolicyDefaults.source_claim,
    social_proof: contentPolicyActions.has(source.social_proof) ? source.social_proof : contentPolicyDefaults.social_proof,
    after_sale: contentPolicyActions.has(source.after_sale) ? source.after_sale : contentPolicyDefaults.after_sale,
    size_interaction: contentPolicyActions.has(source.size_interaction) ? source.size_interaction : contentPolicyDefaults.size_interaction,
    live_interaction: contentPolicyActions.has(source.live_interaction) ? source.live_interaction : contentPolicyDefaults.live_interaction,
    custom_rules: [],
  };
  const seen = new Set();
  for (const item of Array.isArray(source.custom_rules) ? source.custom_rules : []) {
    const text = String(item?.text || "").trim().slice(0, 80);
    const action = contentPolicyActions.has(item?.action) ? item.action : "block";
    const key = `${text.toLocaleLowerCase()}|${action}`;
    if (!text || seen.has(key)) continue;
    seen.add(key);
    normalized.custom_rules.push({ text, action });
    if (normalized.custom_rules.length >= 80) break;
  }
  return normalized;
}

function contentPolicyRuleRow(rule = {}) {
  const row = document.createElement("div");
  row.className = "content-policy-custom-row";
  row.dataset.contentPolicyRule = "true";

  const input = document.createElement("input");
  input.type = "text";
  input.className = "content-policy-rule-text";
  input.maxLength = 80;
  input.placeholder = "词语或短语";
  input.value = String(rule.text || "").slice(0, 80);

  const select = document.createElement("select");
  select.className = "content-policy-rule-action";
  [
    ["block", "禁止"],
    ["body_only", "仅正文"],
    ["allow", "可用"],
    ["prefer", "优先"],
  ].forEach(([value, label]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.append(option);
  });
  select.value = contentPolicyActions.has(rule.action) ? rule.action : "block";
  syncContentPolicyTone(select);

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "button button-muted button-small content-policy-rule-remove";
  remove.dataset.action = "remove-content-policy-rule";
  remove.title = "删除规则";
  remove.setAttribute("aria-label", "删除规则");
  remove.textContent = "×";

  row.append(input, select, remove);
  return row;
}

function renderContentPolicyRules(rules = []) {
  const root = $("content-policy-custom-rules");
  if (!root) return;
  root.replaceChildren(...rules.map((rule) => contentPolicyRuleRow(rule)));
}

function syncContentPolicyTone(control) {
  if (!control) return;
  ["block", "body_only", "allow", "prefer"].forEach((value) => control.classList.remove(`is-policy-${value}`));
  const value = contentPolicyActions.has(control.value) ? control.value : "block";
  control.classList.add(`is-policy-${value}`);
}

function addContentPolicyRule() {
  const root = $("content-policy-custom-rules");
  if (!root) return;
  const row = contentPolicyRuleRow();
  root.append(row);
  row.querySelector("input")?.focus();
}

function removeContentPolicyRule(button) {
  button.closest("[data-content-policy-rule]")?.remove();
  queueAiSelectionSave(0);
}

function applyContentPolicy(policy) {
  const normalized = normalizeContentPolicy(policy);
  const controls = {
    price: "s-policy-price",
    cta: "s-policy-cta",
    inventory_pressure: "s-policy-inventory-pressure",
    source_claim: "s-policy-source-claim",
    social_proof: "s-policy-social-proof",
    after_sale: "s-policy-after-sale",
    size_interaction: "s-policy-size-interaction",
    live_interaction: "s-policy-live-interaction",
  };
  Object.entries(controls).forEach(([key, id]) => {
    const control = $(id);
    if (control) {
      control.value = normalized[key];
      syncContentPolicyTone(control);
    }
  });
  renderContentPolicyRules(normalized.custom_rules);
}

function collectContentPolicy() {
  const policy = normalizeContentPolicy({
    price: $("s-policy-price")?.value,
    cta: $("s-policy-cta")?.value,
    inventory_pressure: $("s-policy-inventory-pressure")?.value,
    source_claim: $("s-policy-source-claim")?.value,
    social_proof: $("s-policy-social-proof")?.value,
    after_sale: $("s-policy-after-sale")?.value,
    size_interaction: $("s-policy-size-interaction")?.value,
    live_interaction: $("s-policy-live-interaction")?.value,
  });
  policy.custom_rules = Array.from(document.querySelectorAll("[data-content-policy-rule]")).flatMap((row) => {
    const text = row.querySelector(".content-policy-rule-text")?.value.trim().slice(0, 80) || "";
    const action = row.querySelector(".content-policy-rule-action")?.value || "block";
    return text ? [{ text, action: contentPolicyActions.has(action) ? action : "block" }] : [];
  });
  return normalizeContentPolicy(policy);
}

function applyAiRules(rules = {}) {
  $("s-rule-narrative").value = rules.narrative || "";
  $("s-rule-category-filter").checked = rules.category_filter !== false;
  $("s-rule-time-coherence").checked = rules.time_coherence !== false;
  $("s-rule-hook-cap").value = rules.hook_cap || "5秒";
  $("s-rule-custom-text").value = rules.custom_text || "";
  applyContentPolicy(rules.content_policy);
  aiSelectionSavedFingerprint = aiSelectionSettingsFingerprint();
}

function collectAiRules() {
  return {
    narrative: $("s-rule-narrative").value.trim(),
    category_filter: $("s-rule-category-filter").checked,
    time_coherence: $("s-rule-time-coherence").checked,
    hook_cap: $("s-rule-hook-cap").value,
    custom_text: $("s-rule-custom-text").value.trim(),
    content_policy: collectContentPolicy(),
  };
}

async function loadLicense() {
  try {
    const data = await api("/api/license");
    $("license-code").value = data.code || "";
    $("license-days-left").value = data.activated
      ? `${data.days_left ?? 0} 天，到期 ${data.expires_date || ""}`
      : data.reason || "未激活";
    const status = $("system-license-status");
    if (status) {
      status.classList.toggle("is-active", Boolean(data.activated));
      status.classList.toggle("is-inactive", !data.activated);
      status.lastChild.textContent = data.activated ? "已激活" : "未激活";
    }
  } catch (error) {
    $("license-days-left").value = "读取失败";
    const status = $("system-license-status");
    if (status) {
      status.classList.remove("is-active");
      status.classList.add("is-inactive");
      status.lastChild.textContent = "读取失败";
    }
  }
}

let licenseActivationInFlight = false;

async function activateLicense() {
  if (licenseActivationInFlight) {
    toast("正在验证激活码，请不要重复点击", "warning");
    return;
  }
  const code = $("license-code").value.trim();
  if (!code) {
    toast("请先输入激活码", "warning");
    return;
  }
  const button = document.querySelector("[data-action='activate-license']");
  const label = button?.textContent || "激活";
  licenseActivationInFlight = true;
  if (button) {
    button.disabled = true;
    button.textContent = "正在验证...";
  }
  try {
    const result = await api("/api/license/activate", {
      method: "POST",
      body: JSON.stringify({ code }),
    });
    toast(result.message || "激活完成", result.ok ? "success" : "warning");
    if (result.ok && result.restart_required) {
      alert(result.message || "激活完成，请重启客户端后再使用。");
    }
    await loadLicense();
  } finally {
    licenseActivationInFlight = false;
    if (button) {
      button.disabled = false;
      button.textContent = label;
    }
  }
}

async function unbindDevice() {
  if (!confirm("确定解绑当前设备吗？解绑后需要重新激活。")) return;
  const result = await api("/api/license/unbind", { method: "POST", body: "{}" });
  toast(result.message || "解绑完成", result.ok ? "success" : "warning");
  await loadLicense();
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

  const progressValue = Math.max(0, Math.min(100, Number(update.progress || 0)));
  const progressWrap = $("update-card-progress-wrap");
  if (progressWrap) progressWrap.hidden = !update.installing;
  const progressBar = $("update-card-progress");
  if (progressBar) progressBar.value = progressValue;
  const progressText = $("update-card-progress-text");
  if (progressText) {
    const downloaded = Number(update.downloaded || 0);
    const total = Number(update.total || 0);
    progressText.textContent = total > 0
      ? progressValue + "%  " + formatFileSize(downloaded) + " / " + formatFileSize(total)
      : (message || "正在准备下载");
  }
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
  if (applyButton) {
    applyButton.textContent = fullPackageRequired ? "获取完整包" : "安装";
    applyButton.disabled = !hasUpdate || busy || (fullPackageRequired && !info.has_package);
  }
  document.querySelectorAll('[data-action="apply-update"]').forEach((button) => {
    if (button === applyButton) return;
    button.textContent = fullPackageRequired ? "获取完整包" : "安装更新";
    button.disabled = !hasUpdate || busy || (fullPackageRequired && !info.has_package);
  });
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
      const noUpdateMessage = result.msg || "当前已是最新版本";
      setUpdateState({
        checking: false,
        available: false,
        info: null,
        message: noUpdateMessage,
      });
      if (!quiet) {
        const tone = ["channel_not_configured", "channel_hold", "channel_paused", "channel_disabled"].includes(result.reason)
          ? "warning"
          : "success";
        toast(noUpdateMessage, tone);
      }
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

async function refreshUpdateProgress() {
  const result = await api("/api/update/status");
  setUpdateState({
    installing: Boolean(result.running),
    stage: result.stage || state.update.stage || "idle",
    progress: Number(result.percent || 0),
    downloaded: Number(result.downloaded || 0),
    total: Number(result.total || 0),
    message: result.message || state.update.message,
    error: result.error || "",
  });
  return result;
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
    const result = await api("/api/update/open-package", { method: "POST", body: "{}" });
    const message = result.msg || fullPackageUpdateMessage(runtime);
    setUpdateState({
      installing: false,
      error: result.ok ? "" : message,
      message: result.ok ? "\u5df2\u6253\u5f00\u5b8c\u6574\u5305\u4e0b\u8f7d\u9875\u9762" : "\u9700\u8981\u65b0\u7248\u5b8c\u6574\u5305",
    });
    toast(message, result.ok ? "success" : "warning");
    return { ...result, full_package_required: true };
  }
  if (!confirm("\u5b89\u88c5\u66f4\u65b0\u540e\u9700\u8981\u91cd\u542f\u5ba2\u6237\u7aef\u624d\u80fd\u751f\u6548\uff0c\u7ee7\u7eed\u5417\uff1f")) return;
  setUpdateState({
    installing: true,
    checking: false,
    error: "",
    stage: "checking",
    progress: 0,
    downloaded: 0,
    total: Number(state.update.info?.patch_size || 0),
    message: "\u6b63\u5728\u51c6\u5907\u66f4\u65b0...",
  });

  let pollTimer = null;
  const poll = () => refreshUpdateProgress().catch(() => null);
  pollTimer = window.setInterval(poll, 500);
  poll();

  try {
    const result = await api("/api/update/apply", { method: "POST", body: "{}" });
    if (result.ok) {
      const message = result.auto_restart
        ? "\u66f4\u65b0\u5df2\u9a8c\u8bc1\uff0c\u5ba2\u6237\u7aef\u5373\u5c06\u81ea\u52a8\u91cd\u542f..."
        : result.restart_required
          ? "\u66f4\u65b0\u5df2\u9a8c\u8bc1\uff0c\u8bf7\u91cd\u542f\u5ba2\u6237\u7aef"
          : "\u5f53\u524d\u5df2\u662f\u6700\u65b0\u7248\u672c";
      setUpdateState({
        installing: false,
        available: false,
        stage: "complete",
        progress: 100,
        message,
      });
      toast(message, "success");
      return result;
    }
    const message = result.msg || "\u66f4\u65b0\u5931\u8d25";
    setUpdateState({
      installing: false,
      stage: result.full_package_required ? "full-package" : "error",
      error: message,
      message,
    });
    toast(message, result.full_package_required ? "warning" : "error");
    return result;
  } catch (error) {
    const message = error.message || String(error);
    setUpdateState({
      installing: false,
      stage: "error",
      error: message,
      message: "\u66f4\u65b0\u5931\u8d25\uff0c\u5df2\u4fdd\u7559\u4e0b\u8f7d\u65ad\u70b9",
    });
    toast(message, "error");
    return { ok: false, msg: message };
  } finally {
    if (pollTimer !== null) window.clearInterval(pollTimer);
  }
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
  const isMixMaterial = id === "mix-video-paths";
  const nextLines = Array.isArray(lines)
    ? lines.map((line) => String(line || "").trim()).filter(Boolean)
    : [];
  if (isMixMaterial && nextLines.length) ensureActiveMixGroup();
  textarea.value = nextLines.join("\n");
  if (isMixMaterial) syncActiveMixGroupFromEditor();
  renderVideoList(id);
  if (isMixMaterial) renderMixGroups();
}

function addVideoPaths(targetId, paths) {
  const next = getLines(targetId);
  const seen = new Set(next.map(normalizeVideoPath));
  paths
    .map((path) => String(path || "").trim())
    .filter(Boolean)
    .forEach((path) => {
      const key = normalizeVideoPath(path);
      if (!seen.has(key)) {
        seen.add(key);
        next.push(path);
      }
    });
  setLines(targetId, next);
}

function isDesktopWebViewHost() {
  return Boolean(window.pywebview?.platform === "edgechromium");
}

function zeroCopyDropScope(targetId) {
  return targetId === "mix-video-paths" ? "mix" : targetId === "video-paths" ? "smart-cut" : "settings";
}

function reportZeroCopyDropDiagnostic(stage, targetId = "", pathCount = 0, detail = "") {
  if (!isZeroCopyTestMode()) return;
  const scope = zeroCopyDropScope(targetId);
  const message = `零拷贝测试 | 前端 ${stage}：target=${targetId || "-"}，绝对路径=${Number(pathCount) || 0}${detail ? `，${detail}` : ""}。`;
  appendLog(scope, { time: new Date().toLocaleTimeString(), level: "info", message });
  api("/api/desktop-drop/diagnostic", {
    method: "POST",
    body: JSON.stringify({ stage, target: targetId, path_count: Math.max(0, Number(pathCount) || 0), detail }),
  }).catch((error) => {
    appendLog(scope, {
      time: new Date().toLocaleTimeString(),
      level: "warning",
      message: `零拷贝测试 | 前端诊断上报失败：${error.message || error}`,
    });
  });
}

function rememberDesktopVideoDropTarget(targetId) {
  if (isDesktopVideoDropTarget(targetId)) {
    state.desktopVideoDropTarget = targetId;
  }
}

function isZeroCopyTestMode() {
  return Boolean(state.runtime?.zero_copy_test_mode);
}

function renderZeroCopyTestMarker(runtime = state.runtime) {
  const enabled = Boolean(runtime?.zero_copy_test_mode);
  document.body.dataset.zeroCopyTest = enabled ? "true" : "false";
  const version = $("app-version");
  const markerId = "zero-copy-test-marker";
  let marker = $(markerId);
  if (!enabled) {
    document.title = "LiveClipper";
    marker?.remove();
    return;
  }
  document.title = "LiveClipper - 零拷贝测试";
  if (version) version.textContent = `v${runtime?.version || "dev"} · 零拷贝测试`;
  if (!marker) {
    marker = document.createElement("div");
    marker.id = markerId;
    marker.className = "brand-version";
    marker.style.color = "#b42318";
    marker.style.fontWeight = "700";
    $("app-version")?.insertAdjacentElement("afterend", marker);
  }
  marker.textContent = "开发环境：零拷贝测试";
  if (!window.__liveClipperZeroCopyFrontendReady) {
    window.__liveClipperZeroCopyFrontendReady = true;
    reportZeroCopyDropDiagnostic("frontend-bridge-ready", "", 0, `desktopHost=${isDesktopWebViewHost()}`);
  }
}

async function consumeDesktopVideoDrop(paths, targetId, targetSource = "") {
  if (!targetId) {
    toast("未识别到拖入区域，请将视频拖到当前功能页的视频列表。", "warning");
    return;
  }
  rememberDesktopVideoDropTarget(targetId);
  reportZeroCopyDropDiagnostic("custom-event-consumed", targetId, paths.length, targetSource);
  const result = await addResolvedVideoPaths(targetId, paths);
  reportZeroCopyDropDiagnostic("paths-resolved", targetId, result.paths?.length || 0);
}

function importSummaryText(summary = {}) {
  const parts = [];
  const labels = [
    ["duplicates", "重复"],
    ["missing", "不存在"],
    ["unreadable", "不可读"],
    ["unsupported", "不支持"],
    ["no_extension", "无后缀"],
    ["reparse_points", "链接/重解析点"],
  ];
  labels.forEach(([key, label]) => {
    const count = Number(summary[key] || 0);
    if (count > 0) parts.push(`${label} ${count}`);
  });
  return parts.length ? `，跳过 ${parts.join("、")}` : "";
}

async function resolveVideoInputPaths(paths) {
  const values = (paths || []).map((path) => String(path || "").trim()).filter(Boolean);
  if (!values.length) return { paths: [], summary: {} };
  const result = await api("/api/media/video-inputs", {
    method: "POST",
    body: JSON.stringify({ paths: values }),
  });
  return {
    paths: (result.paths || []).map((path) => String(path || "").trim()).filter(Boolean),
    summary: result.summary || {},
  };
}

async function addResolvedVideoPaths(targetId, rawPaths) {
  const result = await resolveVideoInputPaths(rawPaths);
  const before = new Set(getLines(targetId).map(normalizeVideoPath));
  addVideoPaths(targetId, result.paths);
  const added = result.paths.filter((path) => !before.has(normalizeVideoPath(path))).length;
  const detail = importSummaryText(result.summary);
  if (added) toast(`已添加 ${added} 个视频${detail}`, "success");
  else toast(`没有可添加的视频${detail || "，请检查文件类型和读取权限"}`, "warning");
  return result;
}

async function addDroppedVideoFiles(targetId, event) {
  if (isDesktopWebViewHost()) {
    // Explorer paths come only from the WinForms CF_HDROP bridge. A DOM drop
    // here must never fall through to the browser upload cache.
    reportZeroCopyDropDiagnostic("web-drop-ignored", targetId, 0, "等待 native CF_HDROP bridge");
    return;
  }

  if (isZeroCopyTestMode()) {
    reportZeroCopyDropDiagnostic("desktop-host-missing", targetId, 0, "window.pywebview.platform 不是 edgechromium");
    throw new Error("零拷贝测试窗口未识别为 pywebview EdgeWebView2；已阻止浏览器上传缓存。");
  }

  const droppedFiles = Array.from(event?.dataTransfer?.files || []).filter(Boolean);
  if (!droppedFiles.length) return;
  const form = new FormData();
  droppedFiles.forEach((file) => form.append("files", file, file.name || "video.mp4"));
  toast(`浏览器模式正在缓存 ${droppedFiles.length} 个拖入视频...`, "warning");
  const result = await upload("/api/uploads/videos", form);
  const uploadedPaths = (result.paths || []).map((path) => String(path || "").trim()).filter(Boolean);
  if (!uploadedPaths.length) throw new Error("视频缓存失败，没有可添加的文件。");
  await addResolvedVideoPaths(targetId, uploadedPaths);
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
    await addResolvedVideoPaths(targetId, paths);
  }
}

async function pickVideoFolder(targetId = "video-paths", trigger = null) {
  const restoreButton = setButtonBusy(trigger, true, "正在打开...");
  let result;
  try {
    result = await api("/api/dialog/directory", { method: "POST", body: "{}" });
  } finally {
    restoreButton();
  }
  if (result.path) await addResolvedVideoPaths(targetId, [result.path]);
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
  if (!Number.isInteger(index) || index < 0 || index >= lines.length || to < 0 || to >= lines.length) return;
  [lines[index], lines[to]] = [lines[to], lines[index]];
  setLines(targetId, lines);
}

function clearVideoList(targetId) {
  setLines(targetId, []);
}

function mixGroupName(paths = [], fallbackIndex = 0) {
  const first = String(paths[0] || "").split(/[\\/]/).filter(Boolean).pop() || "";
  const stem = first.replace(/\.[^.]+$/, "").trim();
  return stem ? stem.slice(0, 28) : `素材组 ${fallbackIndex + 1}`;
}

function normalizeMixGroup(group, index = 0) {
  const paths = Array.isArray(group?.video_paths)
    ? group.video_paths.map((item) => String(item || "").trim()).filter(Boolean)
    : [];
  const name = String(group?.name || "").trim() || mixGroupName(paths, index);
  return { name, video_paths: paths };
}

function isDefaultMixGroupName(name, index) {
  return String(name || "").trim() === `素材组 ${index + 1}`;
}

function ensureActiveMixGroup() {
  const index = state.activeMixGroupIndex;
  if (Number.isInteger(index) && index >= 0 && index < state.mixGroups.length) return index;
  const nextIndex = state.mixGroups.length;
  state.mixGroups.push(normalizeMixGroup({ name: `素材组 ${nextIndex + 1}`, video_paths: [] }, nextIndex));
  state.activeMixGroupIndex = nextIndex;
  return nextIndex;
}

function selectedMixGroupIndexes() {
  const existing = state.selectedMixGroupIndices instanceof Set ? state.selectedMixGroupIndices : new Set();
  const valid = new Set(
    [...existing].filter((index) => Number.isInteger(index) && index >= 0 && index < state.mixGroups.length),
  );
  state.selectedMixGroupIndices = valid;
  return valid;
}

function resetMixGroupSelection() {
  state.mixGroupSelectionMode = false;
  state.selectedMixGroupIndices = new Set();
}

function toggleMixGroupSelection(index, checked) {
  if (!Number.isInteger(index) || index < 0 || index >= state.mixGroups.length) return;
  const selected = selectedMixGroupIndexes();
  const shouldSelect = typeof checked === "boolean" ? checked : !selected.has(index);
  if (shouldSelect) selected.add(index);
  else selected.delete(index);
  state.mixGroupSelectionMode = selected.size > 0;
  state.selectedMixGroupIndices = selected;
  renderMixGroups();
}

function toggleAllMixGroupSelection(checked) {
  state.mixGroupSelectionMode = Boolean(checked);
  state.selectedMixGroupIndices = checked
    ? new Set(state.mixGroups.map((_, index) => index))
    : new Set();
  renderMixGroups();
}

function syncActiveMixGroupFromEditor() {
  const index = state.activeMixGroupIndex;
  if (!Number.isInteger(index) || index < 0 || index >= state.mixGroups.length) return;
  const paths = getLines("mix-video-paths");
  const current = state.mixGroups[index] || {};
  const next = { ...current, video_paths: paths };
  if (paths.length && isDefaultMixGroupName(current.name, index)) next.name = "";
  state.mixGroups[index] = normalizeMixGroup(next, index);
}

function newMixGroup() {
  syncActiveMixGroupFromEditor();
  const nextIndex = state.mixGroups.length;
  state.mixGroups.push(normalizeMixGroup({ name: `素材组 ${nextIndex + 1}`, video_paths: [] }, nextIndex));
  state.activeMixGroupIndex = nextIndex;
  setLines("mix-video-paths", []);
  renderMixGroups();
  toast(`已新建第 ${nextIndex + 1} 组`, "success");
}

function deleteSelectedMixGroups() {
  const selected = [...selectedMixGroupIndexes()].sort((left, right) => left - right);
  if (!selected.length) {
    toast("请先勾选要删除的素材组", "warning");
    return;
  }
  if (typeof window !== "undefined" && !window.confirm("确认删除已选的 " + selected.length + " 个素材组吗？")) return;

  syncActiveMixGroupFromEditor();
  const activeGroup = Number.isInteger(state.activeMixGroupIndex) ? state.mixGroups[state.activeMixGroupIndex] : null;
  const selectedSet = new Set(selected);
  state.mixGroups = state.mixGroups.filter((_, index) => !selectedSet.has(index));

  if (state.mixGroups.length) {
    const nextIndex = activeGroup ? state.mixGroups.indexOf(activeGroup) : -1;
    state.activeMixGroupIndex = nextIndex >= 0 ? nextIndex : Math.min(selected[0], state.mixGroups.length - 1);
    setLines("mix-video-paths", state.mixGroups[state.activeMixGroupIndex].video_paths);
  } else {
    state.activeMixGroupIndex = null;
    setLines("mix-video-paths", []);
  }

  resetMixGroupSelection();
  renderMixGroups();
  toast("已删除 " + selected.length + " 个素材组", "success");
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
  syncActiveMixGroupFromEditor();
  const groups = state.mixGroups.map((group, index) => normalizeMixGroup(group, index));
  const selected = selectedMixGroupIndexes();
  const activeIndex = state.activeMixGroupIndex;
  const activeGroup = Number.isInteger(activeIndex) && activeIndex >= 0 && activeIndex < groups.length
    ? groups[activeIndex]
    : null;

  const activeName = $("mix-active-group-name");
  const activeMeta = $("mix-active-group-meta");
  const bulkSummary = $("mix-group-bulk-summary");
  const deleteSelected = document.querySelector('[data-action="mix-delete-selected-groups"]');
  const selectAll = document.querySelector('[data-action="mix-toggle-all-group-select"]');
  const sidebar = box.closest(".mix-group-sidebar");

  if (activeName) activeName.textContent = activeGroup ? `第${activeIndex + 1}组 ${activeGroup.name}` : "当前素材组";
  if (activeMeta) activeMeta.textContent = activeGroup ? `${activeGroup.video_paths.length}个素材` : "未选择素材组";
  if (bulkSummary) bulkSummary.textContent = `${selected.size}/${groups.length}`;
  if (deleteSelected) deleteSelected.disabled = selected.size === 0;
  if (selectAll) {
    selectAll.checked = groups.length > 0 && selected.size === groups.length;
    selectAll.indeterminate = selected.size > 0 && selected.size < groups.length;
    selectAll.disabled = groups.length === 0;
  }
  if (sidebar) sidebar.classList.toggle("is-selecting", Boolean(state.mixGroupSelectionMode));

  if (!groups.length) {
    box.classList.add("is-empty");
    box.innerHTML = '<div class="mix-group-empty">暂无素材组</div>';
    return;
  }

  box.classList.remove("is-empty");
  box.innerHTML = groups.map((group, index) => {
    const active = index === state.activeMixGroupIndex;
    const selectedForDelete = selected.has(index);
    const title = group.name + " · " + group.video_paths.length + " 个素材";
    const classes = ["mix-group-row", active ? "is-active" : "", selectedForDelete ? "is-selected" : ""].filter(Boolean).join(" ");
    return [
      '<div class="' + classes + '">',
      '<button class="mix-group-row-main" type="button" data-action="mix-select-group" data-index="' + index + '" title="' + escapeHtml(title) + '">',
      '<strong>' + (index + 1) + '组</strong>',
      '<span>' + escapeHtml(group.name) + '</span>',
      '<em>' + group.video_paths.length + '</em>',
      '</button>',
      '<label class="mix-group-row-check" title="选择第' + (index + 1) + '组">',
      '<input type="checkbox" data-action="mix-toggle-group-select" data-index="' + index + '" aria-label="选择第' + (index + 1) + '组" ' + (selectedForDelete ? "checked" : "") + '>',
      '</label>',
      '</div>',
    ].join("");
  }).join("");
}
function normalizeVideoPath(path) {
  return String(path || "").trim().replace(/\//g, "\\").toLowerCase();
}

function videoInfoMap(targetId) {
  return state.videoInfoByTarget[targetId] || {};
}
function videoThumbnailMap(targetId) {
  return state.videoThumbnailByTarget[targetId] || {};
}

async function inspectVideoThumbnails(targetId, lines) {
  if (targetId !== "mix-video-paths") return;
  const key = lines.map(normalizeVideoPath).join("\n");
  if (!key) {
    state.videoThumbnailByTarget[targetId] = {};
    state.videoThumbnailRequestKey[targetId] = "";
    return;
  }
  if (state.videoThumbnailRequestKey[targetId] === key) return;
  state.videoThumbnailRequestKey[targetId] = key;
  try {
    const result = await api("/api/videos/thumbnails", {
      method: "POST",
      body: JSON.stringify({ paths: lines }),
    });
    const thumbnailMap = {};
    (result.items || []).forEach((item) => {
      if (!item?.path || !item?.url) return;
      thumbnailMap[item.path] = item.url;
      thumbnailMap[normalizeVideoPath(item.path)] = item.url;
    });
    state.videoThumbnailByTarget[targetId] = thumbnailMap;
    if (state.videoThumbnailRequestKey[targetId] === key && getLines(targetId).map(normalizeVideoPath).join("\n") === key) {
      renderVideoList(targetId);
    }
  } catch (error) {
    state.videoThumbnailByTarget[targetId] = {};
  }
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
  const thumbnailMap = videoThumbnailMap(targetId);
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
    const retryButton = isInvalid
      ? `<button class="icon-button video-retry" type="button" title="重新检测" aria-label="重新检测 ${escapeHtml(name)}" data-action="retry-video-inspection" data-target="${targetId}" data-index="${index}">&#8635;</button>`
      : "";
    if (targetId === "mix-video-paths") {
      const thumbnailUrl = thumbnailMap[path] || thumbnailMap[normalizeVideoPath(path)] || "";
      const thumbnail = thumbnailUrl
        ? `<img class="mix-video-thumb" src="${escapeHtml(thumbnailUrl)}" alt="" loading="lazy">`
        : '<span class="mix-video-thumb mix-video-thumb-loading" aria-label="正在生成缩略图"></span>';
      return `
        <div class="${rowClass} mix-video-row" draggable="true" data-video-row="${targetId}" data-index="${index}">
          <div class="video-drag" title="拖拽排序">&#8801;</div>
          <span class="mix-video-index">${index + 1}</span>
          <div class="mix-video-thumb-wrap">${thumbnail}</div>
          <div class="video-main">
            <div class="video-title"><strong title="${escapeHtml(path)}">${escapeHtml(name)}</strong>${badges}${retryButton}</div>
            <span class="video-meta">${escapeHtml(compactVideoMetaText(info))}</span>
          </div>
          <button class="video-remove" type="button" aria-label="删除 ${escapeHtml(name)}" data-action="remove-video" data-target="${targetId}" data-index="${index}">&times;</button>
        </div>`;
    }    if (isCompact) {
      return `
        <div class="${rowClass}" draggable="true" data-video-row="${targetId}" data-index="${index}">
          <div class="video-drag" title="拖拽排序">≡</div>
          <div class="video-main">
            <div class="video-title"><strong title="${escapeHtml(path)}">${escapeHtml(name)}</strong>${badges}${retryButton}</div>
            <span class="video-meta">${escapeHtml(compactVideoMetaText(info))}</span>
          </div>
          <button class="video-remove" type="button" title="删除" data-action="remove-video" data-target="${targetId}" data-index="${index}">×</button>
        </div>`;
    }
    return `
      <div class="${rowClass}" draggable="true" data-video-row="${targetId}" data-index="${index}">
        <div class="video-drag" title="拖拽排序">≡</div>
        <div class="video-main">
          <div class="video-title"><strong>${escapeHtml(name)}</strong>${badges}${retryButton}</div>
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
  inspectVideoThumbnails(targetId, lines);
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

function previewReady(preview, scope = "smart") {
  if (!preview?.id || preview.status !== "ready") return false;
  if (
    scope === "smart"
    && preview.commercial_director_experiment
    && !preview.commercial_director_sentence_preview
  ) return false;
  return previewWorkbenchSelectedClips(scope, preview).some((clip) => effectiveClipDuration(clip) > 0.05);
}

function syncFlowActionState() {
  const smartHasVideos = getVideoPaths().length > 0;
  const mixHasVideos = getLines("mix-video-paths").length > 0;
  const runningScopes = state.runningScopes instanceof Set ? state.runningScopes : new Set();
  const mediaPipelineBusy = runningScopes.has("smart-cut") || runningScopes.has("mix");
  const mediaPipelineReason = "智能成片与混剪不能同时运行";

  setButtonsEnabled('[data-action="start-smart-preview"]', smartHasVideos && !mediaPipelineBusy, mediaPipelineBusy ? mediaPipelineReason : "先添加视频素材");
  setButtonsEnabled('[data-action="start-smart-cut"]', smartHasVideos && !mediaPipelineBusy, mediaPipelineBusy ? mediaPipelineReason : "先添加视频素材");
  setButtonsEnabled('[data-action="start-smart-from-preview"]', previewReady(state.smartPreview, "smart") && !mediaPipelineBusy, mediaPipelineBusy ? mediaPipelineReason : "当前编排没有可用片段");
  setButtonsEnabled('[data-action="start-mix-preview"]', mixHasVideos && !mediaPipelineBusy, mediaPipelineBusy ? mediaPipelineReason : "先添加混剪视频素材");
  setButtonsEnabled('[data-action="feature-submit"][data-feature="mix"]', mixHasVideos && !mediaPipelineBusy, mediaPipelineBusy ? mediaPipelineReason : "先添加混剪视频素材");
  setButtonsEnabled('[data-action="start-mix-from-preview"]', previewReady(state.mixPreview, "mix") && !mediaPipelineBusy, mediaPipelineBusy ? mediaPipelineReason : "当前编排没有可用片段");
  setButtonsEnabled('[data-action="stop-scope"][data-scope="smart-cut"]', runningScopes.has("smart-cut"), "当前没有智能成片任务");
  setButtonsEnabled('[data-action="stop-scope"][data-scope="mix"]', runningScopes.has("mix"), "当前没有混剪任务");
  syncProductScanFlow();
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

function clearVideoInspectionRetry(targetId) {
  const timer = state.videoInfoRetryTimers[targetId];
  if (timer) clearTimeout(timer);
  delete state.videoInfoRetryTimers[targetId];
  delete state.videoInfoRetryKeys[targetId];
}

function scheduleVideoInspectionRetry(targetId, lines, items) {
  const retryable = (items || []).some((item) => item?.retryable && !item?.valid);
  if (!retryable) {
    clearVideoInspectionRetry(targetId);
    return;
  }
  const key = lines.map(normalizeVideoPath).join("\n");
  if (!key || state.videoInfoRetryKeys[targetId] === key) return;
  clearVideoInspectionRetry(targetId);
  state.videoInfoRetryKeys[targetId] = key;
  state.videoInfoRetryTimers[targetId] = setTimeout(() => {
    delete state.videoInfoRetryTimers[targetId];
    const currentLines = getLines(targetId);
    if (currentLines.map(normalizeVideoPath).join("\n") !== key) return;
    inspectVideoList(targetId, currentLines, true);
  }, 1200);
}

function retryVideoInspection(targetId, index) {
  const lines = getLines(targetId);
  const path = lines[index];
  if (!path) return;
  clearVideoInspectionRetry(targetId);
  const infoMap = { ...videoInfoMap(targetId) };
  delete infoMap[path];
  delete infoMap[normalizeVideoPath(path)];
  state.videoInfoByTarget[targetId] = infoMap;
  renderVideoList(targetId);
}

async function inspectVideoList(targetId, lines = getLines(targetId), force = false) {
  if (!lines.length) {
    state.videoInfoByTarget[targetId] = {};
    clearVideoInspectionRetry(targetId);
    return;
  }
  const currentMap = videoInfoMap(targetId);
  if (!force && lines.every((path) => currentMap[path] || currentMap[normalizeVideoPath(path)])) return;
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
    scheduleVideoInspectionRetry(targetId, lines, result.items || []);
  } catch (error) {
    // Metadata helps users but should never block adding videos.
  }
}

function bindVideoDropzones() {
  document.querySelectorAll("[data-drop-target]").forEach((zone) => {
    const targetId = zone.dataset.dropTarget;
    const activateTarget = () => rememberDesktopVideoDropTarget(targetId);
    zone.addEventListener("pointerdown", activateTarget);
    zone.addEventListener("focusin", activateTarget);
    if (zone.dataset.dropClickPicker !== "false") {
      zone.addEventListener("click", (event) => {
        activateTarget();
        if (event.target.closest("[data-action]")) return;
        if (event.target.closest("input, textarea, select, button")) return;
        const picker = document.querySelector(`[data-action="pick-videos"][data-target="${targetId}"]`);
        pickVideos(targetId, picker);
      });
    }
    zone.addEventListener("dragenter", (event) => {
      const desktopHost = isDesktopWebViewHost();
      event.preventDefault();
      zone.classList.add("is-dragging");
      if (desktopHost) reportZeroCopyDropDiagnostic("web-dragenter-observed", targetId, 0, "native bridge remains path source");
    });
    zone.addEventListener("dragover", (event) => {
      event.preventDefault();
      zone.classList.add("is-dragging");
    });
    zone.addEventListener("dragleave", (event) => {
      if (event.relatedTarget && zone.contains(event.relatedTarget)) return;
      zone.classList.remove("is-dragging");
    });
    zone.addEventListener("drop", async (event) => {
      event.preventDefault();
      zone.classList.remove("is-dragging");
      try {
        await addDroppedVideoFiles(targetId, event);
      } catch (error) {
        toast(error.message || String(error), "error");
      }
    });
  });
}

function nativeVideoDropTargetFromCoordinates(detail = {}) {
  const nativeX = Number(detail.x);
  const nativeY = Number(detail.y);
  if (!Number.isFinite(nativeX) || !Number.isFinite(nativeY)) return { targetId: "", detail: "missing-coordinates" };
  const dpiScale = Number(detail.dpi) > 0 ? Number(detail.dpi) / 96 : Number(window.devicePixelRatio || 1);
  const candidates = [];
  if (dpiScale && dpiScale !== 1) candidates.push({ x: nativeX / dpiScale, y: nativeY / dpiScale, label: `dpi=${dpiScale}` });
  candidates.push({ x: nativeX, y: nativeY, label: "direct" });
  for (const candidate of candidates) {
    const element = document.elementFromPoint(candidate.x, candidate.y);
    const targetId = element?.closest?.("[data-drop-target]")?.dataset?.dropTarget || "";
    if (isDesktopVideoDropTarget(targetId)) {
      return { targetId, detail: `coordinate=${Math.round(candidate.x)},${Math.round(candidate.y)} ${candidate.label}` };
    }
  }
  return { targetId: "", detail: `coordinate-miss=${Math.round(nativeX)},${Math.round(nativeY)}` };
}

function activePageDesktopVideoDropTarget() {
  const page = document.querySelector(".page.is-active");
  if (!page) return "";
  for (const zone of page.querySelectorAll("[data-drop-target]")) {
    const targetId = zone.dataset?.dropTarget || "";
    if (isDesktopVideoDropTarget(targetId)) return targetId;
  }
  return "";
}

function bindDesktopNativeVideoDropBridge() {
  if (window.__liveClipperDesktopVideoDropBound) return;
  window.__liveClipperDesktopVideoDropBound = true;
  window.addEventListener("liveclipper:native-video-drop", (event) => {
    const paths = Array.isArray(event.detail?.paths)
      ? event.detail.paths.map((path) => String(path || "").trim()).filter(Boolean)
      : [];
    if (!paths.length) {
      reportZeroCopyDropDiagnostic("native-event-empty", state.desktopVideoDropTarget, 0);
      return;
    }
    const resolved = nativeVideoDropTargetFromCoordinates(event.detail || {});
    const activePageTarget = activePageDesktopVideoDropTarget();
    const targetId = resolved.targetId || activePageTarget || state.desktopVideoDropTarget;
    const targetSource = resolved.targetId
      ? resolved.detail
      : activePageTarget
        ? `active-page=${state.page}; ${resolved.detail}`
        : state.desktopVideoDropTarget
          ? `last-active; ${resolved.detail}`
          : resolved.detail;
    if (!targetId) {
      reportZeroCopyDropDiagnostic("native-event-no-target", "", paths.length, targetSource);
      toast("未识别到拖入区域，请将视频拖到当前功能页的视频列表。", "warning");
      return;
    }
    reportZeroCopyDropDiagnostic("native-event-received", targetId, paths.length, targetSource);
    consumeDesktopVideoDrop(paths, targetId, targetSource).catch((error) => toast(error.message || String(error), "error"));
  });
}

function injectVideoFolderPickers() {
  Array.from(desktopVideoDropTargetIds).forEach((targetId) => {
    const picker = document.querySelector(`[data-action="pick-videos"][data-target="${targetId}"]`);
    if (!picker || picker.parentElement?.querySelector(`[data-action="pick-video-folder"][data-target="${targetId}"]`)) return;
    const folderPicker = document.createElement("button");
    folderPicker.type = "button";
    folderPicker.className = "button button-secondary button-small";
    folderPicker.dataset.action = "pick-video-folder";
    folderPicker.dataset.target = targetId;
    folderPicker.textContent = "选择文件夹";
    picker.insertAdjacentElement("afterend", folderPicker);
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
  const selling = checkedControlValues(`${prefix}-selling`);
  const customWords = customSellingValues(prefix);
  return {
    primary_category: primaryCategoryValue(prefix),
    secondary_category: $(`${prefix}-secondary-category`)?.value || "自动识别",
    leaf_category: $(`${prefix}-leaf-category`)?.value.trim() || "",
    main_product: $(`${prefix}-main-product`)?.value.trim() || "",
    goal: $(`${prefix}-goal`)?.value || "自动",
    selling_points: selling,
    priority_terms: customWords,
    preference_weights: collectPreferenceWeights(),
    avoid: checkedControlValues(`${prefix}-avoid`),
    hook_style: $(`${prefix}-hook-style`)?.value || "自动",
    ending_style: $(`${prefix}-ending-style`)?.value || "自动",
    // Carry the currently visible policy with every run. Saving remains useful
    // for future runs, but a preview must never silently use stale settings.
    content_policy: collectContentPolicy(),
    content_review_mode: $("s-content-review-mode")?.value || "off",
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

function selectedDurationTolerance(prefix) {
  const raw = $(`${prefix}-duration-tolerance`)?.value;
  if (!raw || raw === "auto") return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

function collectSmartPayload(options = {}) {
  const requireVideos = options.requireVideos === true;
  const videoPaths = getVideoPaths();
  if (requireVideos && !videoPaths.length) {
    throw new Error("请先填写视频路径");
  }
  const primaryCategory = primaryCategoryValue("sc");
  return {
    video_paths: videoPaths,
    srt_path: $("srt-path").value.trim(),
    output_dir: $("output-dir").value.trim(),
    output_naming_mode: $("sc-output-naming")?.value || "source_timestamp",
    primary_category: primaryCategory,
    category: backendCategoryForPrimary(primaryCategory),
    focus_hint: "自动",
    ai_controls: collectAiControls("sc"),
    target_duration: Number($("sc-duration").value || 60),
    duration_tolerance: selectedDurationTolerance("sc"),
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
    message: `本次目标 ${payload.target_duration} 秒：AI 正在先定故事，再从完整字幕选择真实短句。`,
    target_duration: payload.target_duration,
    duration_tolerance: payload.duration_tolerance,
    clips: [],
    commercial_director_experiment: true,
    commercial_director_preview: true,
  };
  renderSmartPreview(state.smartPreview);
  toast(result.message || "AI 导演预览已启动", "success");
  refreshTasks();
  pollSmartPreview(result.preview_id);
}

async function startCommerceDirectorPreview() {
  await saveFeaturePreferences();
  const payload = collectSmartPayload();
  await runPreflight("smart-preview", payload, "smart-cut");
  const result = await api("/api/smart-cut/commerce-director/preview/start", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  state.smartPreview = {
    id: result.preview_id,
    status: "running",
    message: `本次目标 ${payload.target_duration} 秒：AI 先确定故事章节，再从完整字幕一次性选择真实短句。`,
    target_duration: payload.target_duration,
    duration_tolerance: payload.duration_tolerance,
    clips: [],
    commercial_director_experiment: true,
  };
  state.commerceDirectorActiveResultId = "";
  state.commerceDirectorActiveStrategyId = "";
  state.commerceDirectorEvidenceFilter = "recommended";
  state.commerceDirectorFocusedEvidenceId = "";
  state.commerceDirectorFocusedDraftKey = "";
  state.commerceDirectorFocusedDraftIndex = -1;
  state.commerceDirectorAutoBatchRequestedPreviewId = "";
  state.commerceDirectorLastServerRenderKey = "";
  openCommerceDirectorStudio(state.smartPreview);
  renderSmartPreview(state.smartPreview);
    toast(result.message || "AI 商业导演预览已启动", "success");
  refreshTasks();
  pollSmartPreview(result.preview_id);
}

async function selectCommerceDirectorStory(storyId) {
  const preview = state.smartPreview;
  if (!preview?.id || !storyId) {
    toast("请先完成一次商业导演实验，并从故事库选择方向", "warning");
    return;
  }
  const result = await api("/api/smart-cut/commerce-director/story/select", {
    method: "POST",
    body: JSON.stringify({ preview_id: preview.id, story_id: storyId }),
  });
  state.smartPreview = {
    id: result.preview_id,
    status: "running",
    message: `正在按 ${storyId} 生成商业导演实验方案（复用 M1 故事）。`,
    clips: [],
    commercial_director_experiment: true,
  };
  renderSmartPreview(state.smartPreview);
  toast(result.message || "所选故事方案已启动", "success");
  refreshTasks();
  pollSmartPreview(result.preview_id);
}

async function selectCommerceDirectorStrategy(directorStrategyId, requiresAdditionalAiCall = false, scope = "smart") {
  const isMix = scope === "mix";
  const preview = isMix ? state.mixPreview : state.smartPreview;
  if (!preview?.id || !directorStrategyId) {
    toast("请先完成一次商业导演实验，并从 AI 导演方案中选择", "warning");
    return;
  }
  if (requiresAdditionalAiCall && !window.confirm("生成这个备选方向会重新调用 2 次 AI（故事章节、短句 Casting），并产生新的模型费用；原始视频和字幕会复用。继续吗？")) {
    return;
  }
  const result = await api(isMix ? "/api/mix/commerce-director/strategy/select" : "/api/smart-cut/commerce-director/strategy/select", {
    method: "POST",
    body: JSON.stringify({
      preview_id: preview.id,
      director_strategy_id: directorStrategyId,
      confirm_additional_ai_call: Boolean(requiresAdditionalAiCall),
    }),
  });
  const nextPreview = {
    id: result.preview_id,
    status: "running",
    message: requiresAdditionalAiCall ? "正在为所选备选方向确定故事章节并选择真实短句。" : "正在按所选 AI 导演方案生成真实口播预览。",
    clips: [],
    commercial_director_experiment: true,
  };
  if (isMix) {
    state.mixPreview = nextPreview;
    renderMixPreview(state.mixPreview);
  } else {
    state.smartPreview = nextPreview;
    openCommerceDirectorStudio(state.smartPreview);
    renderSmartPreview(state.smartPreview);
  }
  toast(result.message || "AI 导演方案已启动", "success");
  refreshTasks();
  if (isMix) pollMixPreview(result.preview_id);
  else pollSmartPreview(result.preview_id);
}

function selectCommerceDirectorProposal(directorStrategyId) {
  const id = String(directorStrategyId || "").trim();
  if (!id || !state.smartPreview?.commercial_director_experiment) return;
  state.commerceDirectorActiveStrategyId = id;
  state.commerceDirectorFocusedEvidenceId = "";
  renderCommerceDirectorStudio(state.smartPreview);
}

async function generateCommerceDirectorStrategies() {
  const preview = state.smartPreview;
  if (!preview?.id) {
    toast("请先完成一次 AI 导演方案发现", "warning");
    return;
  }
  const result = await api("/api/smart-cut/commerce-director/strategies/generate", {
    method: "POST",
    body: JSON.stringify({ preview_id: preview.id }),
  });
  state.smartPreview = {
    id: result.preview_id,
    status: "running",
    message: `正在复用 M1 故事地图，依次生成 ${Number(result.strategy_count || 0)} 条 M2→M3 审阅成片。`,
    clips: [],
    commercial_director_experiment: true,
  };
  state.commerceDirectorActiveResultId = "";
  state.commerceDirectorFocusedEvidenceId = "";
  openCommerceDirectorStudio(state.smartPreview);
  renderSmartPreview(state.smartPreview);
  toast(result.message || "多方案实验已启动", "success");
  refreshTasks();
  pollSmartPreview(result.preview_id);
}

function autoGenerateCommerceDirectorStrategies(preview) {
  const previewId = String(preview?.id || "").trim();
  if (!previewId || state.commerceDirectorAutoBatchRequestedPreviewId === previewId) return;
  state.commerceDirectorAutoBatchRequestedPreviewId = previewId;
  window.setTimeout(() => {
    // Compatibility path for runs created before one-click M1→M2→M3 batching.
    // The M1 map remains internal; the user never has to choose a generation step.
    generateCommerceDirectorStrategies().catch((error) => {
      state.commerceDirectorAutoBatchRequestedPreviewId = "";
      toast(error?.message || "无法自动生成导演方案", "error");
      renderCommerceDirectorStudio(state.smartPreview);
    });
  }, 0);
}

function selectCommerceDirectorResult(previewId) {
  const id = String(previewId || "").trim();
  if (!id || !state.smartPreview?.commercial_director_experiment) return;
  state.commerceDirectorActiveResultId = id;
  state.commerceDirectorEvidenceFilter = "recommended";
  state.commerceDirectorFocusedDraftKey = "";
  state.commerceDirectorFocusedDraftIndex = -1;
  renderSmartPreview(state.smartPreview);
}

function commerceDirectorStudioActiveResult(preview) {
  const review = preview?.director_review || {};
  const results = Array.isArray(review.batch_results) ? review.batch_results : [];
  if (results.length) {
    const activeId = results.some((item) => String(item?.preview_id || "") === state.commerceDirectorActiveResultId)
      ? state.commerceDirectorActiveResultId
      : String(results[0]?.preview_id || "");
    return {
      results,
      activeId,
      result: results.find((item) => String(item?.preview_id || "") === activeId) || results[0],
    };
  }
  const draft = review.m2_draft || {};
  const timeline = Array.isArray(review.m2_candidate_timeline) && review.m2_candidate_timeline.length
    ? review.m2_candidate_timeline.map((item, index) => ({ ...item, position: index + 1 }))
    : (preview?.clips || []).map((clip, index) => ({
      position: index + 1,
      chapter_id: clip?.clip_type || "",
      candidate_id: clip?.candidate_id || clip?.director_candidate_id || "",
      source_lineage: clip?.source_lineage || null,
      text: clip?.text || "",
      duration: Number(clip?.duration || 0),
      word_materialization_status: "not_verified",
    }));
  const outline = Array.isArray(review.m2_outline) && review.m2_outline.length
    ? review.m2_outline
    : (draft.chapters || []).map((chapter, index) => ({
      position: Number(chapter?.position || index + 1),
      chapter_id: chapter?.chapter_id || "",
      narrative_role: chapter?.narrative_role || "",
      goal: chapter?.goal || "",
      purchase_value: chapter?.purchase_value || chapter?.goal || "",
      seconds: Number(chapter?.seconds || 0),
    }));
  return {
    results: [], activeId: String(preview?.id || ""),
    result: {
      preview_id: preview?.id || "",
      name: review.headline || "商业导演审阅",
      icon: "",
      state: review.kind === "m3_materialized_review" ? "m3_materialized" : review.kind === "m2_draft_review_only" ? "m2_draft_review_only" : "pending",
      selected_seconds: timeline.reduce((sum, item) => sum + Number(item.duration || 0), 0),
      clip_count: timeline.length,
      opening_promise: review?.m1_story?.payoff || review?.m1_story?.thesis || "未标注",
      commercial_goal: review?.m1_story?.core_commercial_idea || "未标注",
      timeline,
      m2_outline: outline,
      review_video_available: review.kind === "m3_materialized_review" || review.kind === "m2_draft_review_only",
      error: preview?.error || "",
    },
  };
}

function commerceDirectorEvidenceLibrary(review) {
  const stories = Array.isArray(review?.m1_story_library?.stories) ? review.m1_story_library.stories : [];
  const seen = new Set();
  const rows = [];
  stories.forEach((story) => (story?.assets || []).forEach((asset) => (asset?.candidate_lineage || []).forEach((lineage) => {
    const id = String(lineage?.candidate_id || "").trim();
    if (!id || seen.has(id)) return;
    seen.add(id);
    const start = Number(lineage?.start || 0);
    const end = Number(lineage?.end || start);
    const seconds = Number(lineage?.duration_seconds || 0);
    const text = String(lineage?.text || asset?.claim || "").trim();
    rows.push({
      candidateId: id,
      text,
      seconds,
      tier: String(asset?.asset_tier || "supporting"),
      role: String(asset?.role || "evidence").trim(),
      story: String(story?.story_id || "素材"),
      storyId: String(story?.story_id || "").trim(),
      purchaseQuestionId: String(lineage?.purchase_question_id || asset?.purchase_question_id || "").trim(),
      purchaseQuestion: String(lineage?.purchase_question || asset?.purchase_question || "").trim(),
      answerRole: String(lineage?.answer_role || asset?.answer_role || asset?.role || "evidence").trim(),
      eligible: Boolean(id && text && seconds > 0 && end > start),
      sourceLineage: {
        candidate_id: id,
        start,
        end,
        duration_seconds: seconds,
        text,
        lineage_policy: "recorded_subtitle_to_frozen_candidate_only",
      },
      // M1 exposes a frozen safe candidate. A newly hand-added sentence is
      // still only a human-review draft until the existing M3 gate runs again.
      wordMaterializationStatus: "requires_m3_recheck",
    });
  })));
  return rows;
}

function commerceDirectorFocusedDraftContext(review, items, outline, draftKey) {
  const index = state.commerceDirectorFocusedDraftKey === String(draftKey || "")
    ? Number(state.commerceDirectorFocusedDraftIndex)
    : -1;
  const item = Number.isInteger(index) && index >= 0 && index < items.length ? items[index] : null;
  const chapter = item ? (commerceDirectorOutlineByChapter(outline).get(String(item?.chapter_id || "")) || {}) : {};
  const role = String(item?.answer_role || chapter?.narrative_role || item?.chapter_id || "").trim();
  const question = String(item?.purchase_question || chapter?.purchase_question || chapter?.purchase_value || chapter?.goal || "").trim();
  const questionId = String(item?.purchase_question_id || chapter?.purchase_question_id || "").trim();
  const library = commerceDirectorEvidenceLibrary(review);
  const selectedSource = item
    ? library.find((source) => String(source.candidateId) === String(item?.candidate_id || item?.candidateId || "")) || null
    : null;
  return {
    index,
    item,
    role,
    roleMeta: commerceDirectorRoleMeta(role),
    question,
    questionId,
    selectedSource,
    selectedIds: new Set(items.map((row) => String(row?.candidate_id || row?.candidateId || "")).filter(Boolean)),
  };
}

function commerceDirectorEvidenceRelevance(item, context) {
  const itemTone = commerceDirectorRoleMeta(item?.answerRole || item?.role).tone;
  const sameQuestion = Boolean(
    context.questionId && item.purchaseQuestionId && context.questionId === item.purchaseQuestionId,
  ) || Boolean(
    !context.questionId && context.question && item.purchaseQuestion
      && context.question === item.purchaseQuestion,
  );
  const sameStory = Boolean(context.selectedSource?.storyId && context.selectedSource.storyId === item.storyId);
  const sameRole = Boolean(context.item && context.roleMeta.tone === itemTone);
  const score = (sameQuestion ? 12 : 0) + (sameStory ? 7 : 0) + (sameRole ? 4 : 0) + (item.tier === "core" ? 2 : 0);
  const label = sameQuestion
    ? "同一购买问题"
    : sameStory && sameRole
      ? "同一故事 · 结构相近"
      : sameStory
        ? "同一故事的补充证据"
        : sameRole
          ? "同类结构证据"
          : item.tier === "core" ? "高相关冻结证据" : "可补充的新证据";
  return { sameQuestion, sameStory, sameRole, score, label };
}

function commerceDirectorStudioEvidencePanel(review, options = {}) {
  const rows = commerceDirectorEvidenceLibrary(review);
  const items = Array.isArray(options.items) ? options.items : [];
  const outline = Array.isArray(options.outline) ? options.outline : [];
  const context = commerceDirectorFocusedDraftContext(review, items, outline, options.draftKey);
  const allowedFilters = ["all", "current-question", "current-story", "recommended"];
  const filter = allowedFilters.includes(state.commerceDirectorEvidenceFilter)
    ? state.commerceDirectorEvidenceFilter
    : "recommended";
  const unselected = rows.filter((item) => !context.selectedIds.has(String(item.candidateId)));
  const ranked = unselected
    .map((item) => ({ ...item, relevance: commerceDirectorEvidenceRelevance(item, context) }))
    .sort((left, right) => right.relevance.score - left.relevance.score || Number(right.seconds) - Number(left.seconds));
  const questionRows = ranked.filter((item) => item.relevance.sameQuestion || item.relevance.sameRole);
  const storyRows = ranked.filter((item) => item.relevance.sameStory);
  const visible = (filter === "current-question"
    ? questionRows
    : filter === "current-story"
      ? storyRows
      : filter === "recommended"
        ? ranked.filter((item) => item.relevance.score > 0)
        : ranked
  ).slice(0, 8);
  const filters = [
    ["recommended", "推荐"], ["current-question", "当前购买问题"], ["current-story", "当前 Story"], ["all", "全部"],
  ];
  const filterButtons = filters.map(([value, label]) => `<button type="button" class="commerce-director-studio-filter ${filter === value ? "is-active" : ""}" data-action="filter-commerce-director-evidence" data-evidence-filter="${value}">${label}</button>`).join("");
  const recommendationCopy = !context.item
    ? "先点中一条口播，系统会按购买问题、故事和证据角色推荐替换句。"
    : filter === "current-question" && !ranked.some((item) => item.relevance.sameQuestion)
      ? "当前备用候选没有标注同一购买问题，已按相同结构角色推荐。"
      : `正在围绕第 ${context.index + 1} 句：${escapeHtml(context.question || "当前购买问题")} 推荐。`;
  const cards = visible.length ? visible.map((item) => {
    const canPlace = options.editable && options.draftKey && item.eligible;
    const replaceDisabled = !canPlace || !context.item ? "disabled" : "";
    const placementActions = canPlace
      ? `<div class="commerce-director-evidence-actions"><button type="button" data-action="director-plan-place-evidence" data-draft-key="${escapeHtml(options.draftKey)}" data-evidence-id="${escapeHtml(item.candidateId)}" data-evidence-placement="replace" ${replaceDisabled}>替换当前句</button><button type="button" data-action="director-plan-place-evidence" data-draft-key="${escapeHtml(options.draftKey)}" data-evidence-id="${escapeHtml(item.candidateId)}" data-evidence-placement="before">插入当前句前</button><button type="button" data-action="director-plan-place-evidence" data-draft-key="${escapeHtml(options.draftKey)}" data-evidence-id="${escapeHtml(item.candidateId)}" data-evidence-placement="after">插入当前句后</button><button type="button" data-action="director-plan-place-evidence" data-draft-key="${escapeHtml(options.draftKey)}" data-evidence-id="${escapeHtml(item.candidateId)}" data-evidence-placement="end">加入方案</button></div>`
      : '<small class="commerce-director-evidence-blocked">冻结候选信息不完整，不能加入草稿。</small>';
    const purchaseQuestion = item.purchaseQuestion || (item.relevance.sameQuestion ? context.question : "未标注（按相关度推荐）");
    const answerRole = commerceDirectorRoleMeta(item.answerRole || item.role).label;
    return `<article class="commerce-director-evidence-card"><header><span class="is-${escapeHtml(item.tier)}">${escapeHtml(item.relevance.label)}</span><small>${escapeHtml(item.story)} · ${escapeHtml(answerRole)}</small></header><p>${escapeHtml(item.text)}</p><dl><div><dt>购买问题</dt><dd>${escapeHtml(purchaseQuestion)}</dd></div><div><dt>回答角色</dt><dd>${escapeHtml(answerRole)}</dd></div></dl><footer><span>${Number(item.seconds || 0).toFixed(1)}s · 冻结候选 #${escapeHtml(item.candidateId)}</span>${placementActions}</footer><small class="commerce-director-evidence-status">加入后仅保存为人工草稿，必须重新通过 M3 词级物化。</small></article>`;
  }).join("") : '<p class="commerce-director-studio-empty">当前筛选下没有可加入的冻结候选。</p>';
  const collapsed = Boolean(state.commerceDirectorLibraryCollapsed);
  return `<aside class="commerce-director-studio-library ${collapsed ? "is-collapsed" : ""}"><div class="commerce-director-studio-library-head"><div><strong>备用句库</strong><span>${collapsed ? "展开候选" : `${rows.length} 条冻结候选`}</span></div><button type="button" class="commerce-director-library-toggle" data-action="toggle-commerce-director-library" aria-expanded="${collapsed ? "false" : "true"}">${collapsed ? "展开" : "收起"}</button></div>${collapsed ? "" : `<div class="commerce-director-studio-tabs">${filterButtons}</div><div class="commerce-director-studio-library-tools"><span>${recommendationCopy}</span></div><div class="commerce-director-evidence-list">${cards}</div>`}</aside>`;
}

function commerceDirectorFocusedEvidence(review) {
  const id = String(state.commerceDirectorFocusedEvidenceId || "").trim();
  if (!id) return null;
  return commerceDirectorEvidenceLibrary(review).find((item) => String(item.candidateId) === id) || null;
}

function commerceDirectorEvidenceInspector(review) {
  const item = commerceDirectorFocusedEvidence(review);
  if (!item) return "";
  return `<section class="commerce-director-evidence-inspector"><header><div><strong>当前查看素材</strong><span>仅查看冻结候选，不改变 M3 已物化片单</span></div><em>${Number(item.seconds || 0).toFixed(1)}s</em></header><p>${escapeHtml(item.text)}</p><dl><div><dt>故事关系</dt><dd>${escapeHtml(item.story)}</dd></div><div><dt>素材角色</dt><dd>${escapeHtml(item.role)}</dd></div><div><dt>候选编号</dt><dd>#${escapeHtml(item.candidateId)}</dd></div></dl></section>`;
}

function commerceDirectorStudioSolutionCards(results, activeId, activeResult) {
  const cards = results.length ? results : [activeResult];
  return `<div class="commerce-director-studio-solutions">${cards.map((item, index) => {
    const id = String(item?.preview_id || "");
    const active = id === activeId || (!id && index === 0);
    const state = String(item?.state || "pending");
    const stateLabel = state === "m3_materialized" ? "M3 已完成" : state === "m2_draft_review_only" ? "M2 草案" : state === "blocked" ? "本方案失败" : "生成中";
    const journey = commerceDirectorJourneySummary(item?.m2_outline || []);
    return `<button type="button" class="commerce-director-studio-solution ${active ? "is-active" : ""} is-${escapeHtml(state)}" data-action="select-commerce-director-result" data-director-preview-id="${escapeHtml(id)}" aria-pressed="${active ? "true" : "false"}"><header><strong>${escapeHtml(item?.icon || "")}${escapeHtml(item?.name || `方案 ${index + 1}`)}</strong><em>${active ? "当前方案" : stateLabel}</em></header><span class="commerce-director-solution-metrics">${Number(item?.selected_seconds || 0).toFixed(1)}s · ${Number(item?.clip_count || 0)} 段 · ${escapeHtml(stateLabel)}</span><p>${escapeHtml(item?.commercial_goal || item?.opening_promise || item?.error || "等待导演方案")}</p><small>购买路径：${escapeHtml(journey || "等待 M2 编排")}</small></button>`;
  }).join("")}</div>`;
}

function commerceDirectorJourneySummary(outline) {
  const labels = (Array.isArray(outline) ? outline : []).map((item) => commerceDirectorRoleMeta(item?.narrative_role || item?.chapter_id).label);
  return [...new Set(labels.filter(Boolean))].join(" → ");
}

function commerceDirectorRiskLabels(result, review) {
  const issues = Array.isArray(result?.issues) ? result.issues : [];
  const labels = [];
  issues.forEach((issue) => {
    const value = String(issue || "").toLowerCase();
    if (/opening/.test(value)) labels.push("开场吸引需要复核");
    else if (/asr|subtitle/.test(value)) labels.push("ASR 疑似异常");
    else if (/total_|duration|below/.test(value)) labels.push("时长不足");
    else if (/bridge|journey|purchase_path/.test(value)) labels.push("购买路径不完整");
    else if (/word|materializ|selector/.test(value)) labels.push("M3 词级物化未通过");
    else labels.push("方案存在待复核项");
  });
  if (String(result?.state || "") === "m2_draft_review_only") labels.push("仅 M2 草案，不能正式生成");
  if (String(result?.state || "") === "blocked") labels.push("当前方案被合同阻断");
  if (/selector_blocked|not_exact_bound/.test(String(result?.m3_status || review?.m3_status || ""))) labels.push("M3 词级物化未通过");
  return [...new Set(labels)].slice(0, 5);
}

function commerceDirectorRiskPanel(result, review) {
  const risks = commerceDirectorRiskLabels(result, review);
  if (!risks.length) return '<div class="commerce-director-risk-panel"><span>当前风险</span><em>仍为实验审阅，正式导出保持禁用。</em></div>';
  return `<div class="commerce-director-risk-panel"><span>当前风险</span><div>${risks.map((risk) => `<b>${escapeHtml(risk)}</b>`).join("")}</div></div>`;
}

function commerceDirectorDiscoveryProposalCards(proposals, activeId) {
  return `<div class="commerce-director-studio-solutions">${proposals.map((proposal, index) => {
    const id = String(proposal?.director_strategy_id || "");
    const active = id === activeId || (!activeId && index === 0);
    const available = Boolean(proposal?.available);
    const extraAi = Boolean(proposal?.requires_additional_ai_call);
    const actionLabel = extraAi ? "生成此方向（需 1 次 AI）" : "主方案已生成";
    return `<button type="button" class="commerce-director-studio-solution ${active ? "is-active" : ""}" ${extraAi ? 'data-action="select-commerce-director-strategy"' : "disabled"} data-director-strategy-id="${escapeHtml(id)}" data-additional-ai-call="${extraAi ? "true" : "false"}" aria-pressed="${active ? "true" : "false"}" ${available ? "" : "disabled"}><strong>${escapeHtml(proposal?.icon || "")}${escapeHtml(proposal?.name || `方案 ${index + 1}`)}</strong><span>${escapeHtml(proposal?.opening_promise || proposal?.headline || "当前素材支持的导演方向")}</span><em>${extraAi ? actionLabel : "主方案预览已生成"}</em></button>`;
  }).join("")}</div>`;
}

function commerceDirectorDiscoveryOutline(proposal, stories) {
  const storyById = new Map((stories || []).map((story) => [String(story?.story_id || ""), story]));
  return (proposal?.story_mix || []).map((mix, index) => {
    const story = storyById.get(String(mix?.story_id || "")) || {};
    return {
      position: index + 1,
      chapter_id: String(mix?.role || "story"),
      narrative_role: String(mix?.role || "story"),
      purchase_value: String(story?.title || story?.thesis || mix?.story_id || "待生成购买路径"),
      goal: String(story?.commercial_value || proposal?.commercial_goal || ""),
      seconds: Number(mix?.budget_seconds || story?.natural_duration || 0),
    };
  });
}

function renderCommerceDirectorStudio(preview) {
  const page = $("commerce-director-studio-page");
  const root = $("smart-preview");
  if (!page || !root) return;
  const review = preview?.director_review || {};
  const videoName = String(preview?.video_name || preview?.video || "当前商品素材").split(/[\\/]/).filter(Boolean).pop() || "当前商品素材";
  // The director workbench now lives in the existing preview panel.  Keep the
  // former full-screen container hidden so clicking preview never feels like a
  // page jump or a separate route.
  page.classList.add("is-hidden");
  page.setAttribute("aria-hidden", "true");
  if (!state.commerceDirectorStudioOpen || !preview?.commercial_director_experiment) {
    root.innerHTML = "";
    return;
  }
  if (preview.status === "running" || preview.status === "queued") {
  root.innerHTML = `<header class="commerce-director-studio-topbar"><div><strong>AI 商业导演</strong><span>预览</span></div><p>商品：${escapeHtml(videoName)}</p></header><main class="commerce-director-studio-loading"><strong>正在生成 ${Number(preview?.target_duration || 60)} 秒主方案</strong><span>第一遍只确定核心故事和章节；第二遍从完整字幕一次性选择 1–5 秒短句，并按章节连读检查。其他卖法只作为备选方向卡展示。</span></main>`;
    return;
  }
  if (preview.status === "failed" || !review?.m1_story) {
    root.innerHTML = `<header class="commerce-director-studio-topbar"><div><strong>AI 选片工作台</strong><span>导演实验</span></div><p>商品：${escapeHtml(videoName)}</p></header><main class="commerce-director-studio-loading is-error"><strong>本次导演实验没有生成可审阅方案</strong><span>${escapeHtml(preview.error || preview.message || "请检查模型调用后重新生成")}</span></main>`;
    return;
  }
  if (review.kind === "m1_story_map_discovery") {
    root.innerHTML = `<header class="commerce-director-studio-topbar"><div><strong>AI 选片工作台</strong><span>导演实验</span></div><p>商品：${escapeHtml(videoName)}</p></header><main class="commerce-director-studio-loading"><strong>已完成故事发现，正在自动生成可审阅成片</strong><span>M1 的不同卖法正在批量交给 M2 → M3；完成后直接显示方案卡、成片预览和逐句口播，不需要再选择或跳转。</span></main>`;
    autoGenerateCommerceDirectorStrategies(preview);
    return;
  }
  const active = commerceDirectorStudioActiveResult(preview);
  const result = active.result || {};
  const outline = result.m2_outline || [];
  const draft = commerceDirectorDraftItems(result);
  const timeline = draft.items;
  ensureCommerceDirectorFocusedDraft(draft.key, timeline);
  const story = review.m1_story || {};
  const stateCopy = { m3_materialized: "M3 词级物化完成", m2_draft_review_only: "仅 M2 草案审阅", blocked: "方案被合同阻断", pending: "仍在生成" };
  const stateText = stateCopy[String(result.state || "pending")] || stateCopy.pending;
  const video = result.review_video_available ? commerceDirectorPreviewPanel(result.preview_id, `${result.name || "商业导演"}审阅视频`) : '<div class="commerce-director-video-unavailable">此方案暂无可播放审阅视频</div>';
  const failureNote = result.state === "blocked" && result.error
    ? `<div class="commerce-director-result-error"><strong>本方案未生成可审阅片单</strong><span>${escapeHtml(result.error)}</span></div>` : "";
  root.innerHTML = `<header class="commerce-director-studio-topbar"><div><strong>AI 导演方案评审台</strong><span>实验审阅</span></div><p>商品：${escapeHtml(videoName)}</p><nav><button type="button" class="commerce-director-studio-quiet" data-action="restart-commerce-director-preview">重新生成方案</button><span class="commerce-director-studio-state">${escapeHtml(stateText)}</span></nav></header><main class="commerce-director-studio-main ${state.commerceDirectorLibraryCollapsed ? "is-library-collapsed" : ""}"><section class="commerce-director-studio-work">${commerceDirectorStudioSolutionCards(active.results, active.activeId, result)}${failureNote}<section class="commerce-director-studio-stage"><div class="commerce-director-studio-video-column"><header><strong>成片预览</strong><span>${escapeHtml(stateText)} · 仅供实验审阅</span></header><div class="commerce-director-studio-player">${video}</div></div><aside class="commerce-director-studio-core"><div class="commerce-director-studio-core-head"><span>导演意图</span><em>${escapeHtml(stateText)}</em></div><div class="commerce-director-intent-field"><span>核心承诺</span><strong>${escapeHtml(result.opening_promise || story.payoff || story.thesis || "未标注")}</strong></div><dl><div><dt>目标人群</dt><dd>${escapeHtml(story.audience_tension || "未标注")}</dd></div><div><dt>导演目标</dt><dd>${escapeHtml(result.commercial_goal || story.core_commercial_idea || "未标注")}</dd></div></dl>${commerceDirectorM2Outline(outline)}${commerceDirectorRiskPanel(result, review)}</aside></section><section class="commerce-director-studio-script"><header><div><strong>方案口播与编排</strong><span>点中一句即可在右侧获取替换建议；编辑只保存为人工审阅草稿。</span></div><div class="commerce-director-script-actions"><em>${timeline.length} 句 · ${timeline.reduce((sum, item) => sum + Number(item?.duration || 0), 0).toFixed(1)}s</em><button type="button" class="commerce-director-studio-quiet" data-action="director-plan-save" data-draft-key="${escapeHtml(draft.key)}">保存草稿</button></div></header>${commerceDirectorEditableTimelineRows(timeline, outline, draft.key)}</section></section>${commerceDirectorStudioEvidencePanel(review, { editable: true, draftKey: draft.key, items: timeline, outline })}</main><footer class="commerce-director-studio-footer"><span>人工修改只作用于本地实验草稿；原 ASR、冻结候选与既有 M3 审阅成片保持不变。</span><nav><button type="button" class="commerce-director-studio-quiet" data-action="director-plan-save" data-draft-key="${escapeHtml(draft.key)}">保存当前修改</button><button type="button" class="commerce-director-formal-blocked" disabled title="商业导演实验仅供人工审核，不能进入正式预览或导出">确认进入正式预览 / 生成成片</button><button type="button" class="commerce-director-studio-quiet" data-action="director-plan-return-list">返回方案列表</button></nav></footer>`;
  bindCommerceDirectorDraftRowDrag(root);
}

function openCommerceDirectorStudio(preview) {
  state.commerceDirectorStudioOpen = true;
  state.commerceDirectorStudioDismissedPreviewId = "";
  renderCommerceDirectorStudio(preview || state.smartPreview);
}

function closeCommerceDirectorStudio() {
  state.commerceDirectorStudioDismissedPreviewId = String(state.smartPreview?.id || "");
  state.commerceDirectorStudioOpen = false;
  renderCommerceDirectorStudio(null);
}

function setCommerceDirectorEvidenceFilter(filter) {
  state.commerceDirectorEvidenceFilter = ["all", "current-question", "current-story", "recommended"].includes(filter)
    ? filter
    : "recommended";
  refreshCommerceDirectorEvidenceDrawer();
}

function toggleCommerceDirectorLibrary() {
  state.commerceDirectorLibraryCollapsed = !state.commerceDirectorLibraryCollapsed;
  renderCommerceDirectorStudio(state.smartPreview);
}

function focusCommerceDirectorEvidence(candidateId) {
  state.commerceDirectorFocusedEvidenceId = String(candidateId || "").trim();
  refreshCommerceDirectorEvidenceDrawer();
}

function commerceDirectorDraftKey(result) {
  return `commerce-director:${String(result?.preview_id || "")}`;
}

function normalizeCommerceDirectorDraftItems(items) {
  return (Array.isArray(items) ? items : []).map((item, index) => ({
    ...item,
    position: index + 1,
    source_text: String(item?.source_text ?? item?.text ?? ""),
    manual_added: Boolean(item?.manual_added || item?.manual),
    manual_edited: Boolean(item?.manual_edited || item?.edited),
  }));
}

function commerceDirectorDraftItems(result) {
  const key = commerceDirectorDraftKey(result);
  if (!key || key.endsWith(":")) return { key, items: [] };
  if (!Array.isArray(state.commerceDirectorPlanDrafts[key])) {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(key) || "null"); } catch (_) { saved = null; }
    const savedItems = Array.isArray(saved) ? saved : Array.isArray(saved?.items) ? saved.items : null;
    state.commerceDirectorPlanDrafts[key] = normalizeCommerceDirectorDraftItems(
      savedItems || (result?.timeline || []).map((item) => ({ ...item, manual_added: false, manual_edited: false })),
    );
  }
  return { key, items: state.commerceDirectorPlanDrafts[key] };
}

function ensureCommerceDirectorFocusedDraft(key, items) {
  const valid = state.commerceDirectorFocusedDraftKey === String(key || "")
    && Number.isInteger(state.commerceDirectorFocusedDraftIndex)
    && state.commerceDirectorFocusedDraftIndex >= 0
    && state.commerceDirectorFocusedDraftIndex < items.length;
  if (valid) return;
  state.commerceDirectorFocusedDraftKey = String(key || "");
  state.commerceDirectorFocusedDraftIndex = items.length ? 0 : -1;
}

function refreshCommerceDirectorEvidenceDrawer() {
  const root = $("smart-preview");
  const preview = state.smartPreview;
  const drawer = root?.querySelector(".commerce-director-studio-library");
  if (!root || !drawer || !preview?.commercial_director_experiment) return;
  const active = commerceDirectorStudioActiveResult(preview);
  const result = active.result || {};
  const draft = commerceDirectorDraftItems(result);
  ensureCommerceDirectorFocusedDraft(draft.key, draft.items);
  drawer.outerHTML = commerceDirectorStudioEvidencePanel(preview.director_review || {}, {
    editable: true,
    draftKey: draft.key,
    items: draft.items,
    outline: result.m2_outline || [],
  });
}

function focusCommerceDirectorDraftRow(key, index) {
  const items = state.commerceDirectorPlanDrafts[String(key || "")];
  if (!Array.isArray(items) || !Number.isInteger(index) || index < 0 || index >= items.length) return;
  state.commerceDirectorFocusedDraftKey = String(key || "");
  state.commerceDirectorFocusedDraftIndex = index;
  const root = $("smart-preview");
  root?.querySelectorAll("[data-director-plan-row]").forEach((row) => {
    row.classList.toggle("is-selected", row.dataset.draftKey === String(key || "") && Number(row.dataset.draftIndex) === index);
  });
  refreshCommerceDirectorEvidenceDrawer();
}

function openCommerceDirectorReplacement(key, index) {
  focusCommerceDirectorDraftRow(key, index);
  state.commerceDirectorEvidenceFilter = "recommended";
  refreshCommerceDirectorEvidenceDrawer();
  $("smart-preview")?.querySelector(".commerce-director-studio-library")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function updateCommerceDirectorDraftText(key, index, text) {
  const items = state.commerceDirectorPlanDrafts[String(key || "")];
  if (!Array.isArray(items) || !Number.isInteger(index) || !items[index]) return;
  const item = items[index];
  item.source_text = String(item.source_text ?? item.text ?? "");
  item.text = String(text || "");
  item.manual_text_override = item.text;
  item.manual_edited = true;
  item.edited = true;
}

function moveCommerceDirectorDraft(key, index, direction) {
  const items = state.commerceDirectorPlanDrafts[String(key || "")];
  const next = index + direction;
  if (!Array.isArray(items) || index < 0 || next < 0 || index >= items.length || next >= items.length) return;
  [items[index], items[next]] = [items[next], items[index]];
  items.forEach((item, position) => { item.position = position + 1; item.edited = true; });
  if (state.commerceDirectorFocusedDraftKey === String(key || "")) {
    if (state.commerceDirectorFocusedDraftIndex === index) state.commerceDirectorFocusedDraftIndex = next;
    else if (state.commerceDirectorFocusedDraftIndex === next) state.commerceDirectorFocusedDraftIndex = index;
  }
  renderCommerceDirectorStudio(state.smartPreview);
}

function reorderCommerceDirectorDraft(key, fromIndex, targetIndex, placeAfter) {
  const items = state.commerceDirectorPlanDrafts[String(key || "")];
  if (!Array.isArray(items) || fromIndex < 0 || targetIndex < 0 || fromIndex >= items.length || targetIndex >= items.length || fromIndex === targetIndex) return;
  const [moved] = items.splice(fromIndex, 1);
  let insertAt = targetIndex + (placeAfter ? 1 : 0);
  if (fromIndex < targetIndex) insertAt -= 1;
  items.splice(Math.max(0, Math.min(insertAt, items.length)), 0, moved);
  items.forEach((item, position) => { item.position = position + 1; item.edited = true; });
  if (state.commerceDirectorFocusedDraftKey === String(key || "")) {
    const focusId = state.commerceDirectorFocusedDraftIndex === fromIndex ? moved : null;
    if (focusId) state.commerceDirectorFocusedDraftIndex = items.indexOf(focusId);
  }
  renderCommerceDirectorStudio(state.smartPreview);
}

function removeCommerceDirectorDraftItem(key, index) {
  const items = state.commerceDirectorPlanDrafts[String(key || "")];
  if (!Array.isArray(items) || index < 0 || index >= items.length) return;
  items.splice(index, 1);
  items.forEach((item, position) => { item.position = position + 1; item.edited = true; });
  if (state.commerceDirectorFocusedDraftKey === String(key || "")) {
    state.commerceDirectorFocusedDraftIndex = Math.min(index, items.length - 1);
  }
  renderCommerceDirectorStudio(state.smartPreview);
}

function addCommerceDirectorDraftEvidence(key, candidateId) {
  placeCommerceDirectorDraftEvidence(key, candidateId, "end");
}

function commerceDirectorEvidenceCanJoinDraft(source) {
  if (!source?.eligible || !source?.candidateId || !source?.text || Number(source?.seconds || 0) <= 0) {
    toast("这条备用句缺少冻结候选、原始口播或有效时长，不能加入草稿。", "warning");
    return false;
  }
  const lineage = source.sourceLineage || {};
  if (Number(lineage.end || 0) <= Number(lineage.start || 0)) {
    toast("这条备用句没有可验证的原始时间范围，不能加入草稿。", "warning");
    return false;
  }
  return true;
}

function placeCommerceDirectorDraftEvidence(key, candidateId, placement = "end") {
  const items = state.commerceDirectorPlanDrafts[String(key || "")];
  const source = commerceDirectorEvidenceLibrary(state.smartPreview?.director_review || {}).find((item) => String(item.candidateId) === String(candidateId));
  if (!Array.isArray(items) || !source || !commerceDirectorEvidenceCanJoinDraft(source)) return;
  const focusIndex = state.commerceDirectorFocusedDraftKey === String(key || "")
    ? Number(state.commerceDirectorFocusedDraftIndex)
    : -1;
  const current = Number.isInteger(focusIndex) && focusIndex >= 0 && focusIndex < items.length ? items[focusIndex] : null;
  if (placement === "replace" && !current) {
    toast("请先点中一条口播，再替换当前句。", "warning");
    return;
  }
  if (items.some((item) => String(item.candidate_id || item.candidateId || "") === String(source.candidateId))) {
    toast("该候选已经在当前草稿中", "warning");
    return;
  }
  const chapterId = current?.chapter_id || "manual_added";
  const item = {
    position: 0,
    candidate_id: source.candidateId,
    chapter_id: chapterId,
    text: source.text,
    source_text: source.text,
    duration: Number(source.seconds || 0),
    source_lineage: { ...source.sourceLineage },
    story_id: source.storyId,
    purchase_question_id: current?.purchase_question_id || "",
    purchase_question: current?.purchase_question || "",
    answer_role: source.answerRole || source.role,
    word_materialization_status: source.wordMaterializationStatus,
    manual_added: true,
    manual: true,
    manual_insert_placement: placement,
    edited: true,
  };
  let insertedIndex = items.length;
  if (placement === "replace") {
    item.replaces_candidate_id = current?.candidate_id || current?.candidateId || "";
    item.replaces_source_lineage = current?.source_lineage || current?.sourceLineage || null;
    items.splice(focusIndex, 1, item);
    insertedIndex = focusIndex;
  } else if (placement === "before" && current) {
    items.splice(focusIndex, 0, item);
    insertedIndex = focusIndex;
  } else if (placement === "after" && current) {
    items.splice(focusIndex + 1, 0, item);
    insertedIndex = focusIndex + 1;
  } else {
    items.push(item);
  }
  items.forEach((row, position) => { row.position = position + 1; row.edited = true; });
  state.commerceDirectorFocusedDraftKey = String(key || "");
  state.commerceDirectorFocusedDraftIndex = insertedIndex;
  toast("已加入人工审阅草稿；总时长已更新，仍需重新通过 M3 词级物化。", "success");
  renderCommerceDirectorStudio(state.smartPreview);
}

function saveCommerceDirectorDraft(key) {
  const items = state.commerceDirectorPlanDrafts[String(key || "")];
  if (!Array.isArray(items)) return;
  try {
    localStorage.setItem(String(key), JSON.stringify({ version: "commerce-director-draft-v2", items }));
    toast("已保存当前人工审阅草稿；未改动原 ASR、冻结候选或已生成的 M3 审阅成片", "success");
  } catch (_) {
    toast("当前浏览器无法保存草稿", "warning");
  }
}

function returnCommerceDirectorPlanList() {
  $("smart-preview")?.querySelector(".commerce-director-studio-solutions")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function playCommerceDirectorReview(targetPreviewId = "") {
  const preview = state.smartPreview;
  const previewId = targetPreviewId || preview?.id || "";
  const isDraft = preview?.director_review?.kind === "m2_draft_review_only";
  const isM3Review = preview?.director_review?.kind === "m3_materialized_review";
  if (!previewId || (!targetPreviewId && !isDraft && !isM3Review)) {
    toast("当前没有可播放的商业导演草案审阅视频", "warning");
    return;
  }
  const player = $("smart-preview-player");
  const video = $("smart-preview-video");
  const title = $("smart-preview-player-title");
  if (!player || !video) return;
  if (title) title.textContent = isDraft ? "M2 草案审阅视频（非 M3 成片）" : "M3 词级审阅视频（仅人工审核）";
  player.classList.remove("is-hidden");
  video.pause();
  video.removeAttribute("src");
  video.load();
  try {
    const result = await api("/api/smart-cut/commerce-director/review-video", {
      method: "POST",
      body: JSON.stringify({ preview_id: previewId }),
    });
    video.src = result.url;
    video.load();
    video.play().catch(() => {});
    toast(result.message || "草案审阅视频已生成", "info");
  } catch (error) {
    player.classList.add("is-hidden");
    toast(error.message || "草案审阅视频生成失败", "error");
  }
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
    message: `本次目标 ${payload.duration} 秒：AI 正在统一分析全部素材并规划混剪故事。`,
    target_duration: payload.duration,
    duration_tolerance: payload.duration_tolerance,
    clips: [],
    commercial_director_experiment: true,
    commercial_director_preview: true,
  };
  renderMixPreview(state.mixPreview);
  toast(result.message || "混剪 AI 导演预览已启动", "success");
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

function hydratePreviewCandidatePool(preview) {
  if (!preview || !Array.isArray(preview.candidate_clips) || !preview.candidate_clips.length) return preview;
  if (preview.clips !== preview.candidate_clips) preview.clips = preview.candidate_clips;
  return preview;
}

function getPreviewState(scope = "smart") {
  const preview = scope === "mix" ? state.mixPreview : state.smartPreview;
  return hydratePreviewCandidatePool(preview);
}
function renderPreviewState(scope = "smart") {
  if (scope === "mix") renderMixPreview(state.mixPreview);
  else renderSmartPreview(state.smartPreview);
}
function previewBox(scope = "smart") {
  return scope === "mix" ? $("mix-preview") : $("smart-preview");
}
function previewStoryScrollTop(scope = "smart") {
  return previewBox(scope)?.querySelector(".preview-sequence-scroll")?.scrollTop || 0;
}

function renderPreviewStateKeepStoryScroll(scope = "smart") {
  const box = previewBox(scope);
  const candidateScrollTop = box?.querySelector(".preview-candidate-list")?.scrollTop || 0;
  const storyScrollTop = previewStoryScrollTop(scope);
  renderPreviewState(scope);
  const refreshed = previewBox(scope);
  const candidateList = refreshed?.querySelector(".preview-candidate-list");
  const storyList = refreshed?.querySelector(".preview-sequence-scroll");
  if (candidateList) candidateList.scrollTop = candidateScrollTop;
  if (storyList) storyList.scrollTop = storyScrollTop;
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

function previewAssemblyOrderKey(scope = "smart", preview = null) {
  return previewDraftKey(scope, preview?.id || "");
}

function previewAssemblyOrder(scope = "smart", preview = getPreviewState(scope)) {
  if (!preview?.id || !Array.isArray(preview.clips)) return [];
  const key = previewAssemblyOrderKey(scope, preview);
  const selected = preview.clips.filter(isPreviewWorkbenchSelected);
  const selectedSet = new Set(selected.map((clip) => Number(clip.index)).filter(Number.isInteger));
  const stored = normalizedIntegerList(state.previewAssemblyOrders[key]);
  const ordered = stored.filter((index) => selectedSet.has(index));
  selected.forEach((clip) => {
    const index = Number(clip.index);
    if (Number.isInteger(index) && !ordered.includes(index)) ordered.push(index);
  });
  state.previewAssemblyOrders[key] = ordered;
  return ordered;
}

function setPreviewAssemblyMembership(scope = "smart", index, selected) {
  const preview = getPreviewState(scope);
  if (!preview?.id || !Number.isInteger(Number(index))) return;
  const key = previewAssemblyOrderKey(scope, preview);
  const clipIndex = Number(index);
  const current = previewAssemblyOrder(scope, preview);
  if (selected) {
    state.previewAssemblyOrders[key] = current.includes(clipIndex) ? current : [...current, clipIndex];
  } else {
    state.previewAssemblyOrders[key] = current.filter((item) => item !== clipIndex);
  }
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
  const clipsByIndex = new Map((preview?.clips || []).map((clip) => [Number(clip.index), clip]));
  const selectedOrder = previewAssemblyOrder(scope, preview);
  const remaining = (preview?.clips || [])
    .map((clip) => Number(clip.index))
    .filter((index) => Number.isInteger(index) && !selectedOrder.includes(index));
  draft.order = [...selectedOrder, ...remaining];
  selectedOrder.forEach((clipIndex) => {
    const clip = clipsByIndex.get(clipIndex);
    if (!clip) return;
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
  const selectedOrder = normalizedIntegerList(draft.selected_indices);
  const fallbackOrder = order.filter((index) => selectedSet.has(index));
  const preferred = (selectedOrder.length ? selectedOrder : fallbackOrder)
    .filter((index) => selectedSet.has(index));
  preview.clips.forEach((clip) => {
    const index = Number(clip.index);
    if (isPreviewWorkbenchSelected(clip) && Number.isInteger(index) && !preferred.includes(index)) preferred.push(index);
  });
  state.previewAssemblyOrders[previewAssemblyOrderKey(scope, preview)] = preferred;
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
  }
  if (normalizePreviewWordIndices(preview) || !draft) {
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
    setPreviewAssemblyMembership(scope, index, selected);
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
  setPreviewAssemblyMembership(scope, index, clip.selected);
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

function reorderPreviewClip(scope, fromIndex, toIndex, placeAfter = false) {
  const preview = getPreviewState(scope);
  if (!preview?.clips?.length) return;
  syncPreviewClipSelections(scope);
  const order = previewAssemblyOrder(scope, preview);
  const from = order.indexOf(Number(fromIndex));
  const target = Number(toIndex);
  if (from < 0 || !order.includes(target) || Number(fromIndex) === target) return;
  const [clip] = order.splice(from, 1);
  const to = order.indexOf(target);
  order.splice(placeAfter ? to + 1 : to, 0, clip);
  state.previewAssemblyOrders[previewAssemblyOrderKey(scope, preview)] = order;
  commitPreviewDraft(scope);
  renderPreviewStateKeepStoryScroll(scope);
}

function bindPreviewRowDrag(box, scope = "smart") {
  // The desktop shell reserves native HTML drag-and-drop for Explorer
  // CF_HDROP input. Pointer events keep in-page ordering independent from
  // that native file-drop bridge and work the same in Edge WebView2.
  const rowSelector = `[data-preview-row][data-preview-scope="${scope}"]`;
  let active = null;

  const clearDragState = () => {
    box.querySelectorAll(`${rowSelector}.is-dragging, ${rowSelector}.is-drop-target`).forEach((row) => {
      row.classList.remove("is-dragging", "is-drop-target", "is-drop-after");
    });
  };

  const targetAt = (clientX, clientY) => {
    const element = document.elementFromPoint(clientX, clientY);
    const row = element?.closest?.(rowSelector);
    return row && box.contains(row) ? row : null;
  };

  const updateTarget = (event) => {
    if (!active || event.pointerId !== active.pointerId) return;
    const moved = Math.hypot(event.clientX - active.startX, event.clientY - active.startY) >= 5;
    if (!active.started && !moved) return;
    active.started = true;
    active.sourceRow.classList.add("is-dragging");

    const targetRow = targetAt(event.clientX, event.clientY);
    if (active.targetRow && active.targetRow !== targetRow) {
      active.targetRow.classList.remove("is-drop-target", "is-drop-after");
    }
    active.targetRow = targetRow && targetRow !== active.sourceRow ? targetRow : null;
    if (!active.targetRow) return;

    const bounds = active.targetRow.getBoundingClientRect();
    active.placeAfter = event.clientY >= bounds.top + (bounds.height / 2);
    active.targetRow.classList.add("is-drop-target");
    active.targetRow.classList.toggle("is-drop-after", active.placeAfter);
  };

  const finish = (event, cancelled = false) => {
    if (!active || event.pointerId !== active.pointerId) return;
    if (!cancelled) updateTarget(event);
    const drag = active;
    active = null;
    try {
      if (drag.handle.hasPointerCapture?.(event.pointerId)) drag.handle.releasePointerCapture(event.pointerId);
    } catch (_error) {
      // Pointer capture can already be released by the WebView while closing.
    }
    clearDragState();
    if (cancelled || !drag.started || !drag.targetRow) return;
    reorderPreviewClip(
      scope,
      Number(drag.sourceRow.dataset.previewIndex),
      Number(drag.targetRow.dataset.previewIndex),
      drag.placeAfter,
    );
  };

  box.querySelectorAll(`[data-preview-drag-handle][data-preview-scope="${scope}"]`).forEach((handle) => {
    handle.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || active) return;
      const sourceRow = handle.closest(rowSelector);
      if (!sourceRow) return;
      event.preventDefault();
      active = {
        handle,
        pointerId: event.pointerId,
        sourceRow,
        targetRow: null,
        startX: event.clientX,
        startY: event.clientY,
        started: false,
        placeAfter: false,
      };
      try {
        handle.setPointerCapture(event.pointerId);
      } catch (_error) {
        // Pointer events still bubble on older WebView2 runtimes without capture.
      }
    });
    handle.addEventListener("pointermove", (event) => {
      updateTarget(event);
      if (active?.started) event.preventDefault();
    });
    handle.addEventListener("pointerup", (event) => finish(event));
    handle.addEventListener("pointercancel", (event) => finish(event, true));
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

function setInlinePreviewStatus(panel, message, type = "info", retry = null) {
  const status = panel?.querySelector("[data-preview-inline-status]");
  if (!status) return;
  status.replaceChildren();
  if (message) {
    const text = document.createElement("span");
    text.textContent = message;
    status.append(text);
  }
  if (retry && Number.isInteger(Number(retry.index))) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button button-muted button-small preview-inline-retry";
    button.textContent = "重新生成";
    button.dataset.action = "preview-inline-retry";
    button.dataset.previewScope = retry.scope || "smart";
    button.dataset.previewIndex = String(Number(retry.index));
    button.dataset.previewInspectOnly = retry.inspectOnly === true ? "true" : "false";
    status.append(button);
  }
  status.classList.toggle("is-hidden", !message);
  status.classList.toggle("is-error", type === "error");
}

function applyInlinePreviewVideoState(scope, index, key) {
  const panel = previewInlineVideoPanel(scope, index, key);
  if (!panel) return;
  const video = panel.querySelector("[data-preview-inline-player]");
  const entry = state.previewInlineVideos[key] || {};
  applyPreviewInlineAudioPreference(video);
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
    setInlinePreviewStatus(panel, `小视频生成失败：${entry.error}`, "error", {
      scope,
      index,
      inspectOnly: entry.inspectOnly === true,
    });
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
  const preview = getPreviewState(scope);
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
  const inspectOnly = !isPreviewWorkbenchSelected(clip);
  if (!inspectOnly && segments.length && !selectedPreviewSegments(clip).length) {
    toast("这个片段没有选中的句子，勾选后再预览", "warning");
    return;
  }
  const draft = inspectOnly ? null : commitPreviewDraft(scope, { remote: true });
  const bounds = effectiveClipBounds(clip);
  const modal = ensurePreviewModal();
  const video = modal.querySelector("#preview-modal-video");
  const title = modal.querySelector("#preview-modal-title");
  const status = modal.querySelector("#preview-modal-status");
  if (!video) return;
  if (title) title.textContent = `${inspectOnly ? "候选试看" : "片段预览"} ${formatSeconds(bounds.start)}-${formatSeconds(bounds.end)} · ${bounds.duration.toFixed(1)}s`;
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
      body: JSON.stringify(inspectOnly ? {
        preview_id: preview.id,
        clip_index: index,
        scope,
        inspect_only: true,
      } : {
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

  toast(`兼容模式：将按顺序提交 ${cleanGroups.length} 组混剪`, "warning");
  appendLog("mix", {
    time: new Date().toLocaleTimeString(),
    level: "warning",
    message: `兼容模式启动：共 ${cleanGroups.length} 组。请保持此页面打开，当前组完成后会自动提交下一组。`,
  });

  const completed = [];
  const failed = [];
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
      succeeded: completed.length,
      current: index + 1,
      failed: failed.length,
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
      await runPreflight("mix", singlePayload, "mix");
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
        succeeded: completed.length,
        current: index + 1 < totalGroups ? index + 2 : 0,
        failed: failed.length,
        status: index + 1 === totalGroups ? "completed" : "running",
        labelText: index + 1 === totalGroups
          ? (failed.length ? "批量混剪完成（有失败）" : "批量混剪完成")
          : "批量混剪",
        percent: Math.round(((index + 1) / totalGroups) * 100),
      });
    } catch (error) {
      const reason = error?.message || String(error || "未知错误");
      failed.push({ name: group.name, reason });
      appendLog("mix", {
        time: new Date().toLocaleTimeString(),
        level: "error",
        message: `兼容模式第 ${index + 1}/${totalGroups} 组失败并跳过：${group.name}，${reason}`,
      });
      setLegacyBatchProgress("mix", {
        total: totalGroups,
        done: index + 1,
        succeeded: completed.length,
        current: index + 1 < totalGroups ? index + 2 : 0,
        failed: failed.length,
        status: index + 1 === totalGroups ? "completed" : "running",
        labelText: index + 1 === totalGroups ? "批量混剪完成（有失败）" : "批量混剪",
        percent: Math.round(((index + 1) / totalGroups) * 100),
      });
    }
  }

  const summary = `兼容模式混剪完成：成功 ${completed.length}/${cleanGroups.length} 组，失败 ${failed.length} 组`;
  toast(summary, failed.length ? "warning" : "success");
  appendLog("mix", {
    time: new Date().toLocaleTimeString(),
    level: failed.length ? "warning" : "success",
    message: `${summary}。`,
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
    const statusChanged = preview.status !== state.smartPreview?.status;
    const workbenchMissing = previewWorkbenchNeedsRender("smart", preview);
    if (isNewPreview || isNewerPreview || statusChanged || workbenchMissing) {
      state.smartPreview = preview;
      renderSmartPreview(preview);
      if (preview.status === "running" || preview.status === "queued") pollSmartPreview(preview.id);
    }
  } catch (error) {
    console.warn("Failed to refresh smart preview", error);
  }
}

async function loadLatestMixPreview() {
  try {
    const preview = await api("/api/mix/preview/latest");
    if (!preview?.id) return;
    const isNewPreview = !state.mixPreview || preview.id !== state.mixPreview.id;
    const isNewerPreview = preview.created_at > (state.mixPreview?.created_at || 0);
    const statusChanged = preview.status !== state.mixPreview?.status;
    const workbenchMissing = previewWorkbenchNeedsRender("mix", preview);
    if (isNewPreview || isNewerPreview || statusChanged || workbenchMissing) {
      state.mixPreview = preview;
      renderMixPreview(preview);
      if (preview.status === "running" || preview.status === "queued") pollMixPreview(preview.id);
    }
  } catch (error) {
    console.warn("Failed to refresh mix preview", error);
  }
}

const previewPollMaxAttempts = 1800;

function previewWorkbenchNeedsRender(scope, preview) {
  // Commercial-director reviews use their own read-only workspace.  They do
  // not contain the legacy editable workbench marker, so treating that marker
  // as missing caused the background 5s poll to recreate videos repeatedly.
  if (preview?.commercial_director_experiment && !preview?.commercial_director_preview && !preview?.commercial_director_sentence_preview) {
    if (preview?.status !== "ready") return false;
    const box = previewBox(scope);
    return Boolean(box && !box.querySelector(".commerce-director-studio-main"));
  }
  if (preview?.status !== "ready" || !Array.isArray(preview.clips) || !preview.clips.length) return false;
  const box = previewBox(scope);
  return Boolean(box && !box.querySelector(`[data-preview-workbench="${scope}"]`));
}

function commerceDirectorServerRenderKey(preview) {
  if (!preview?.commercial_director_experiment) return "";
  const review = preview?.director_review || {};
  const batch = Array.isArray(review?.batch_results) ? review.batch_results.map((item) => ({
    id: item?.preview_id || "",
    state: item?.state || "",
    seconds: Number(item?.selected_seconds || 0),
    clips: Number(item?.clip_count || 0),
    error: item?.error || "",
  })) : [];
  return JSON.stringify({
    id: preview?.id || "",
    status: preview?.status || "",
    message: preview?.message || "",
    error: preview?.error || "",
    kind: review?.kind || "",
    batch,
  });
}

async function pollSmartPreview(previewId, attempt = 0) {
  if (!previewId || attempt > previewPollMaxAttempts) return;
  try {
    const preview = await api(`/api/smart-cut/preview/${encodeURIComponent(previewId)}`);
    const nextRenderKey = commerceDirectorServerRenderKey(preview);
    const shouldRender = !preview?.commercial_director_experiment
      || preview?.commercial_director_sentence_preview
      || nextRenderKey !== state.commerceDirectorLastServerRenderKey;
    state.smartPreview = preview;
    if (shouldRender) {
      state.commerceDirectorLastServerRenderKey = nextRenderKey;
      renderSmartPreview(preview);
    }
    if (preview.status === "ready" || preview.status === "failed") return;
  } catch (error) {
    if (attempt > 3) toast(error.message || "读取选片预览失败", "error");
  }
  setTimeout(() => pollSmartPreview(previewId, attempt + 1), 2000);
}

async function pollMixPreview(previewId, attempt = 0) {
  if (!previewId || attempt > previewPollMaxAttempts) return;
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
  const reviewed = clip?.content_semantics && typeof clip.content_semantics === "object"
    ? clip.content_semantics
    : null;
  const reviewedTopic = String(reviewed?.topic || "").trim();
  const reviewedSubtopic = String(reviewed?.subtopic || "").trim();
  const reviewedValue = String(reviewed?.buyer_value || "").trim();
  if (reviewedTopic) {
    parts.push(`卖点：${reviewedTopic}${reviewedSubtopic ? ` · ${reviewedSubtopic}` : ""}`);
    if (reviewedValue) parts.push(`价值：${reviewedValue}`);
  }
  const focus = String(clip?.focus || clip?.focus_block || "").trim();
  if (!reviewedTopic && focus && focus !== "其他") parts.push(`重点：${focus}`);
  const tags = classifyClipScoreTags(clip, analysis)
    .filter((tag) => tag.label && tag.label !== "普通" && tag.label !== reviewedTopic)
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
        <video class="${videoClass}" data-preview-inline-player controls playsinline ${previewInlineAudioMutedAttribute()} autoplay preload="metadata" ${entry.url ? `src="${escapeHtml(entry.url)}"` : ""}></video>
        <div class="clip-detail-video-status ${statusClass} ${entry.error ? "is-error" : ""}" data-preview-inline-status>${escapeHtml(statusText)}</div>
      </div>
    </div>
  `;
}

function renderPreviewDetailPanel(scope, preview, analysis, activeIndex, sequenceClips = null) {
  const clips = preview?.clips || [];
  const displayClips = Array.isArray(sequenceClips) && sequenceClips.length ? sequenceClips : clips;
  const clip = clips.find((item) => Number(item.index) === activeIndex) || displayClips[0] || clips[0];
  if (!clip) {
    return `<aside class="clip-detail-panel"><p>请选择成片片段，再逐句精修。</p></aside>`;
  }
  const position = Math.max(0, displayClips.findIndex((item) => Number(item.index) === Number(clip.index)));
  const typeLabel = clipTypeLabel(clip.clip_type);
  const bounds = effectiveClipBounds(clip);
  const time = `${formatSeconds(bounds.start)}-${formatSeconds(bounds.end)}`;
  const duration = `${bounds.duration.toFixed(1)}s`;
  const { risk, riskLabel, riskClass } = previewClipRisk(clip, analysis);
  const segments = previewSegments(clip);
  const segmentCountText = selectedSegmentCountText(clip) || "整段";
  const videoAction = isPreviewWorkbenchSelected(clip)
    ? `<button class="button button-secondary button-small" data-action="preview-clip-video" data-preview-scope="${scope}" data-preview-index="${clip.index}">打开大预览</button>`
    : `<button class="button button-muted button-small" data-action="preview-clip-video" data-preview-scope="${scope}" data-preview-index="${clip.index}">试看候选</button>`;
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
        ${videoAction}
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

function previewWorkbenchRoleKey(clip) {
  const role = previewSalesRole(clip);
  if (role === "hook" || role === "hook_followup") return "hook";
  if (role === "proof_detail") return "proof";
  if (role === "scene_crowd" || role === "objection_resolver") return "scene";
  if (role === "natural_close") return "close";
  if (role === "weak_fragment") return "weak";
  return "product";
}

function previewWorkbenchRoleLabel(clip) {
  const directorFunction = String(clip?.director_beat_function || "").trim().toLowerCase();
  if (directorFunction) {
    return ({
      result: "结果",
      mechanism: "原因机制",
      proof: "证明",
      experience: "穿着体验",
      risk_remove: "顾虑解除",
      styling: "搭配",
      scene: "使用场景",
      trust: "品质信任",
      transition: "自然承接",
      payoff: "兑现",
    })[directorFunction] || String(clip?.director_beat_function || "导演短句");
  }
  return ({ hook: "开头钩子", product: "核心卖点", proof: "证据解释", scene: "场景与顾虑", close: "收尾", weak: "补充内容" })[previewWorkbenchRoleKey(clip)] || "核心卖点";
}

function previewWorkbenchTopicLabel(clip) {
  const directorTitle = String(clip?.director_chapter_title || "").trim();
  if (directorTitle) return directorTitle;
  const explicit = String(clip?.focus_block || clip?.focus || "").trim();
  if (explicit && explicit !== "其他") return explicit;
  return String(classifyFinalClipTopic({ ...clip, segments: [] }) || explicit || "其他").trim();
}

function previewWorkbenchRoleRank(clip) {
  return ["hook", "product", "proof", "scene", "close", "weak"].indexOf(previewWorkbenchRoleKey(clip));
}

function isPreviewWorkbenchSelected(clip) {
  return clip?.selected !== false && (!previewSegments(clip).length || selectedPreviewSegments(clip).length > 0);
}

function previewWorkbenchCandidateDuration(clip) {
  const start = Number(clip?.start || 0);
  const end = Number(clip?.end || start);
  return Math.max(0, Number(clip?.duration || end - start));
}

function previewTriageSession(scope = "smart", preview = getPreviewState(scope)) {
  const key = previewDraftKey(scope, preview?.id || "");
  if (!state.previewTriageSessions[key]) {
    state.previewTriageSessions[key] = {
      activeIndex: null,
      role: "all",
      topic: "all",
      filter: "pending",
      search: "",
      skipped: {},
      reviewed: {},
      history: [],
    };
  }
  return state.previewTriageSessions[key];
}

function previewTriageCandidates(scope = "smart", preview = getPreviewState(scope)) {
  const session = previewTriageSession(scope, preview);
  const query = String(session.search || "").trim().toLocaleLowerCase();
  return (preview?.clips || []).filter((clip) => {
    const index = Number(clip.index);
    const skipped = session.skipped[String(index)] === true;
    const reviewed = session.reviewed[String(index)] === true;
    const selected = isPreviewWorkbenchSelected(clip);
    if (session.role !== "all" && previewWorkbenchRoleKey(clip) !== session.role) return false;
    if (session.topic !== "all" && previewWorkbenchTopicLabel(clip) !== session.topic) return false;
    if (query) {
      const searchable = [clip.text, previewWorkbenchTopicLabel(clip), previewWorkbenchRoleLabel(clip), clip.focus, clip.source]
        .filter(Boolean)
        .join(" ")
        .toLocaleLowerCase();
      if (!searchable.includes(query)) return false;
    }
    if (session.filter === "pending") return !reviewed && !skipped;
    if (session.filter === "kept") return selected;
    if (session.filter === "skipped") return skipped;
    return true;
  });
}

function previewTriageActive(scope = "smart", preview = getPreviewState(scope)) {
  const session = previewTriageSession(scope, preview);
  const candidates = previewTriageCandidates(scope, preview);
  let position = candidates.findIndex((clip) => Number(clip.index) === Number(session.activeIndex));
  if (position < 0) position = candidates.length ? 0 : -1;
  const clip = position >= 0 ? candidates[position] : null;
  session.activeIndex = clip ? Number(clip.index) : null;
  return { session, candidates, clip, position };
}

function setPreviewWorkbenchStage(scope = "smart", stage = "triage") {
  state.previewWorkbenchStages[scope] = stage === "assembly" ? "assembly" : "triage";
  const preview = getPreviewState(scope);
  if (state.previewWorkbenchStages[scope] === "assembly") {
    const selected = previewAssemblyOrder(scope, preview);
    if (selected.length) state.previewDetailSelection[scope] = selected[0];
  }
  renderPreviewState(scope);
}

function setPreviewTriageRole(scope = "smart", role = "all") {
  const session = previewTriageSession(scope);
  session.role = ["all", "hook", "product", "proof", "scene", "close", "weak"].includes(role) ? role : "all";
  session.activeIndex = null;
  renderPreviewState(scope);
}

function setPreviewTriageTopic(scope = "smart", topic = "all") {
  const session = previewTriageSession(scope);
  session.topic = String(topic || "all").slice(0, 80) || "all";
  session.activeIndex = null;
  renderPreviewState(scope);
}

function setPreviewTriageFilter(scope = "smart", filter = "pending") {
  const session = previewTriageSession(scope);
  session.filter = ["pending", "all", "kept", "skipped"].includes(filter) ? filter : "pending";
  session.activeIndex = null;
  renderPreviewState(scope);
}

function setPreviewTriageSearch(scope = "smart", value = "") {
  const session = previewTriageSession(scope);
  session.search = String(value || "").slice(0, 80);
  session.activeIndex = null;
  renderPreviewState(scope);
  window.requestAnimationFrame(() => {
    const input = previewBox(scope)?.querySelector(`[data-preview-triage-search="${scope}"]`);
    input?.focus();
    input?.setSelectionRange(input.value.length, input.value.length);
  });
}

function setPreviewTriageActive(scope = "smart", index) {
  const preview = getPreviewState(scope);
  if (!preview?.clips?.some((clip) => Number(clip.index) === Number(index))) return;
  previewTriageSession(scope, preview).activeIndex = Number(index);
  renderPreviewState(scope);
}

function movePreviewTriage(scope = "smart", direction = 1) {
  const preview = getPreviewState(scope);
  const { session, candidates, position } = previewTriageActive(scope, preview);
  if (!candidates.length) return;
  const next = (position + direction + candidates.length) % candidates.length;
  session.activeIndex = Number(candidates[next].index);
  renderPreviewState(scope);
}

function advancePreviewTriage(scope, currentIndex, previousCandidates) {
  const preview = getPreviewState(scope);
  const session = previewTriageSession(scope, preview);
  const candidates = previewTriageCandidates(scope, preview);
  if (!candidates.length) {
    session.activeIndex = null;
    return;
  }
  const valid = new Set(candidates.map((clip) => Number(clip.index)));
  const previous = Array.isArray(previousCandidates) && previousCandidates.length ? previousCandidates : candidates;
  const start = previous.findIndex((clip) => Number(clip.index) === Number(currentIndex));
  for (let offset = 1; offset <= previous.length; offset += 1) {
    const candidate = previous[(Math.max(0, start) + offset) % previous.length];
    if (candidate && valid.has(Number(candidate.index))) {
      session.activeIndex = Number(candidate.index);
      return;
    }
  }
  session.activeIndex = Number(candidates[0].index);
}

function rememberPreviewTriageAction(scope, clip) {
  const preview = getPreviewState(scope);
  const session = previewTriageSession(scope, preview);
  const index = Number(clip.index);
  session.history.push({
    activeIndex: session.activeIndex,
    index,
    selected: clip.selected !== false,
    segments: previewSegments(clip).map((segment) => segment.selected !== false),
    skipped: session.skipped[String(index)] === true,
    reviewed: session.reviewed[String(index)] === true,
    assemblyOrder: [...previewAssemblyOrder(scope, preview)],
  });
  session.history = session.history.slice(-12);
}

function keepPreviewTriageCandidate(scope = "smart") {
  const preview = getPreviewState(scope);
  const { clip, candidates, session } = previewTriageActive(scope, preview);
  if (!clip) return;
  rememberPreviewTriageAction(scope, clip);
  clip.selected = true;
  previewSegments(clip).forEach((segment) => { segment.selected = segment.selection_locked !== true; });
  setPreviewAssemblyMembership(scope, Number(clip.index), true);
  delete session.skipped[String(clip.index)];
  session.reviewed[String(clip.index)] = true;
  advancePreviewTriage(scope, Number(clip.index), candidates);
  commitPreviewDraft(scope);
  renderPreviewState(scope);
}

function skipPreviewTriageCandidate(scope = "smart") {
  const preview = getPreviewState(scope);
  const { clip, candidates, session } = previewTriageActive(scope, preview);
  if (!clip) return;
  rememberPreviewTriageAction(scope, clip);
  clip.selected = false;
  previewSegments(clip).forEach((segment) => { segment.selected = false; });
  setPreviewAssemblyMembership(scope, Number(clip.index), false);
  session.skipped[String(clip.index)] = true;
  session.reviewed[String(clip.index)] = true;
  advancePreviewTriage(scope, Number(clip.index), candidates);
  commitPreviewDraft(scope);
  renderPreviewState(scope);
}

function undoPreviewTriageAction(scope = "smart") {
  const preview = getPreviewState(scope);
  const session = previewTriageSession(scope, preview);
  const snapshot = session.history.pop();
  if (!snapshot) {
    toast("没有可撤销的选片操作", "info");
    return;
  }
  const clip = preview?.clips?.find((item) => Number(item.index) === Number(snapshot.index));
  if (!clip) return;
  clip.selected = snapshot.selected;
  previewSegments(clip).forEach((segment, position) => {
    segment.selected = segment.selection_locked === true ? false : Boolean(snapshot.segments[position]);
  });
  if (snapshot.skipped) session.skipped[String(snapshot.index)] = true;
  else delete session.skipped[String(snapshot.index)];
  if (snapshot.reviewed) session.reviewed[String(snapshot.index)] = true;
  else delete session.reviewed[String(snapshot.index)];
  state.previewAssemblyOrders[previewAssemblyOrderKey(scope, preview)] = [...snapshot.assemblyOrder];
  session.activeIndex = snapshot.activeIndex;
  commitPreviewDraft(scope);
  renderPreviewState(scope);
}

function inspectPreviewWorkbenchClip(index, scope = "smart") {
  state.previewDetailSelection[scope] = Number(index);
  if (state.previewWorkbenchStages[scope] !== "assembly") state.previewWorkbenchStages[scope] = "assembly";
  renderPreviewState(scope);
}

function movePreviewAssemblyClip(scope = "smart", index, direction) {
  const preview = getPreviewState(scope);
  const order = previewAssemblyOrder(scope, preview);
  const position = order.indexOf(Number(index));
  const next = position + Number(direction);
  if (position < 0 || next < 0 || next >= order.length) return;
  reorderPreviewClip(scope, Number(index), order[next]);
}

function removePreviewAssemblyCandidate(scope = "smart", index) {
  const preview = getPreviewState(scope);
  const clip = preview?.clips?.find((item) => Number(item.index) === Number(index));
  if (!clip) return;
  const order = previewAssemblyOrder(scope, preview);
  const position = order.indexOf(Number(index));
  const neighborIndex = position >= 0 ? (order[position + 1] ?? order[position - 1] ?? null) : null;
  clip.selected = false;
  previewSegments(clip).forEach((segment) => { segment.selected = false; });
  setPreviewAssemblyMembership(scope, Number(index), false);
  if (Number(state.previewDetailSelection?.[scope]) === Number(index)) {
    state.previewCandidateSelections[scope] = null;
    state.previewDetailSelection[scope] = neighborIndex;
  }
  commitPreviewDraft(scope);
  renderPreviewStateKeepStoryScroll(scope);
}

function autoArrangePreviewAssembly(scope = "smart") {
  const preview = getPreviewState(scope);
  const order = previewAssemblyOrder(scope, preview);
  const positions = new Map(order.map((index, position) => [index, position]));
  const byIndex = new Map((preview?.clips || []).map((clip) => [Number(clip.index), clip]));
  order.sort((left, right) => {
    const rank = previewWorkbenchRoleRank(byIndex.get(left)) - previewWorkbenchRoleRank(byIndex.get(right));
    return rank || positions.get(left) - positions.get(right);
  });
  state.previewAssemblyOrders[previewAssemblyOrderKey(scope, preview)] = order;
  commitPreviewDraft(scope);
  renderPreviewState(scope);
}

function previewDurationFitState(scope = "smart", preview = getPreviewState(scope), targetId = "") {
  const prefix = scope === "mix" ? "mix" : "sc";
  const resolvedTargetId = targetId || `${prefix}-duration`;
  const target = Math.max(1, Number(preview?.target_duration || $(resolvedTargetId)?.value || 60));
  const savedTolerance = preview?.duration_tolerance;
  const explicitTolerance = savedTolerance !== null && savedTolerance !== undefined && savedTolerance !== ""
    ? Number(savedTolerance)
    : selectedDurationTolerance(prefix);
  const tolerance = Number.isFinite(explicitTolerance)
    ? Math.max(0, explicitTolerance)
    : Math.max(5, target / 6);
  const speed = Math.max(0.1, Number(preview?.dedup_summary?.duration_speed_factor || 1) || 1);
  const selected = previewWorkbenchSelectedClips(scope, preview);
  const rawTotal = selected.reduce((sum, clip) => sum + effectiveClipDuration(clip), 0);
  const projected = rawTotal / speed;
  const low = Math.max(1, target - tolerance);
  const high = target + tolerance;
  return {
    target,
    tolerance,
    speed,
    selected,
    rawTotal,
    projected,
    low,
    high,
    sourceTarget: target * speed,
    sourceLow: low * speed,
    sourceHigh: high * speed,
    accepted: projected >= low - 0.001 && projected <= high + 0.001,
  };
}

function previewCandidateSelectableDuration(clip) {
  const segments = previewSegments(clip);
  if (!segments.length) return previewWorkbenchCandidateDuration(clip);
  return segments
    .filter((segment) => segment?.selection_locked !== true)
    .reduce((sum, segment) => {
      const start = Number(segment?.start || 0);
      const end = Number(segment?.end || start);
      return sum + Math.max(0, Number(segment?.duration || end - start));
    }, 0);
}

function autoFitPreviewDuration(scope = "smart") {
  const preview = getPreviewState(scope);
  if (!preview?.clips?.length) return;
  syncPreviewClipSelections(scope);
  const before = previewDurationFitState(scope, preview);
  const byIndex = new Map(preview.clips.map((clip) => [Number(clip.index), clip]));
  let order = [...previewAssemblyOrder(scope, preview)];
  let rawTotal = before.rawTotal;
  let added = 0;
  let removed = 0;

  const selectedSourceCounts = () => {
    const counts = new Map();
    order.forEach((index) => {
      const source = String(byIndex.get(index)?.source || byIndex.get(index)?.source_name || "");
      if (source) counts.set(source, (counts.get(source) || 0) + 1);
    });
    return counts;
  };
  const selectedTopics = () => new Set(
    order.map((index) => previewWorkbenchTopicLabel(byIndex.get(index))).filter(Boolean)
  );

  while (rawTotal < before.sourceLow - 0.01 && added < 8) {
    const sources = selectedSourceCounts();
    const topics = selectedTopics();
    const candidates = preview.clips
      .filter((clip) => !order.includes(Number(clip.index)))
      .filter((clip) => !["hook", "close"].includes(previewWorkbenchRoleKey(clip)))
      .map((clip, position) => {
        const duration = previewCandidateSelectableDuration(clip);
        const nextTotal = rawTotal + duration;
        const source = String(clip.source || clip.source_name || "");
        const minSourceCount = sources.size ? Math.min(...sources.values()) : 0;
        return {
          clip,
          position,
          duration,
          nextTotal,
          inside: nextTotal >= before.sourceLow - 0.01 && nextTotal <= before.sourceHigh + 0.01,
          sourceNeed: source && (sources.get(source) || 0) <= minSourceCount,
          newTopic: !topics.has(previewWorkbenchTopicLabel(clip)),
          extraCandidate: clip.recommended === false || (clip.candidate_origin && clip.candidate_origin !== "recommended"),
          distance: Math.abs(nextTotal - before.sourceTarget),
        };
      })
      .filter((item) => item.duration > 0.05 && item.nextTotal <= before.sourceHigh + 0.01)
      .sort((left, right) =>
        Number(right.inside) - Number(left.inside)
        || Number(right.sourceNeed) - Number(left.sourceNeed)
        || Number(right.newTopic) - Number(left.newTopic)
        || Number(right.extraCandidate) - Number(left.extraCandidate)
        || left.distance - right.distance
        || left.position - right.position
      );
    if (!candidates.length) break;
    const chosen = candidates[0];
    const clip = chosen.clip;
    clip.selected = true;
    previewSegments(clip).forEach((segment) => {
      segment.selected = segment.selection_locked !== true;
      resetPreviewSegmentWords(segment);
    });
    const closePosition = order.findIndex((index) => previewWorkbenchRoleKey(byIndex.get(index)) === "close");
    order.splice(closePosition >= 0 ? closePosition : order.length, 0, Number(clip.index));
    rawTotal += effectiveClipDuration(clip);
    added += 1;
  }

  while (rawTotal > before.sourceHigh + 0.01 && removed < 8) {
    const removable = order
      .map((index, position) => ({ clip: byIndex.get(index), index, position }))
      .filter((item) => item.position >= 2)
      .filter((item) => !["hook", "close"].includes(previewWorkbenchRoleKey(item.clip)))
      .map((item) => {
        const duration = effectiveClipDuration(item.clip);
        const nextTotal = rawTotal - duration;
        return {
          ...item,
          duration,
          nextTotal,
          inside: nextTotal >= before.sourceLow - 0.01 && nextTotal <= before.sourceHigh + 0.01,
          supplement: item.clip?.recommended === false || (item.clip?.candidate_origin && item.clip.candidate_origin !== "recommended"),
          weak: previewWorkbenchRoleKey(item.clip) === "weak",
          score: Number(item.clip?.score || 0),
          distance: Math.abs(nextTotal - before.sourceTarget),
        };
      })
      .filter((item) => item.duration > 0.05 && item.nextTotal >= before.sourceLow - 0.01)
      .sort((left, right) =>
        Number(right.inside) - Number(left.inside)
        || Number(right.supplement) - Number(left.supplement)
        || Number(right.weak) - Number(left.weak)
        || left.score - right.score
        || left.distance - right.distance
        || right.position - left.position
      );
    if (!removable.length) break;
    const chosen = removable[0];
    chosen.clip.selected = false;
    previewSegments(chosen.clip).forEach((segment) => { segment.selected = false; });
    order = order.filter((index) => index !== chosen.index);
    rawTotal = chosen.nextTotal;
    removed += 1;
  }

  state.previewAssemblyOrders[previewAssemblyOrderKey(scope, preview)] = order;
  commitPreviewDraft(scope);
  renderPreviewStateKeepStoryScroll(scope);
  const after = previewDurationFitState(scope, preview);
  const actionText = [added ? `补入${added}段` : "", removed ? `移出${removed}段` : ""].filter(Boolean).join("、");
  if (after.accepted) {
    toast(`时长已适配：${actionText || "无需改动"}，预计成片${after.projected.toFixed(1)}秒。`);
  } else {
    toast(`现有安全候选仍无法进入${after.low.toFixed(0)}-${after.high.toFixed(0)}秒区间。`, "warning");
  }
}

function renderPreviewTriageRoleFilters(scope, preview, session) {
  const roles = ["all", "hook", "product", "proof", "scene", "close", "weak"];
  return '<div class="preview-triage-role-strip" role="group" aria-label="按成片作用筛选">' + roles.map((role) => {
    const items = (preview?.clips || []).filter((clip) => role === "all" || previewWorkbenchRoleKey(clip) === role);
    const pending = items.filter((clip) => !session.reviewed[String(clip.index)] && !session.skipped[String(clip.index)]).length;
    const label = role === "all" ? "全部候选" : ({ hook: "开头", product: "卖点", proof: "证据", scene: "场景", close: "收尾", weak: "补充" })[role];
    return '<button class="' + (session.role === role ? "is-active" : "") + '" data-action="preview-triage-role" data-preview-scope="' + scope + '" data-value="' + role + '" aria-pressed="' + (session.role === role ? "true" : "false") + '"><span>' + label + '</span><em>' + pending + '/' + items.length + '</em></button>';
  }).join("") + '</div>';
}

function renderPreviewTriageTopicFilters(scope, preview, session) {
  const counts = new Map();
  (preview?.clips || []).forEach((clip) => {
    if (session.role !== "all" && previewWorkbenchRoleKey(clip) !== session.role) return;
    const topic = previewWorkbenchTopicLabel(clip);
    counts.set(topic, Number(counts.get(topic) || 0) + 1);
  });
  const topics = Array.from(counts.entries())
    .sort((left, right) => Number(right[1]) - Number(left[1]) || String(left[0]).localeCompare(String(right[0]), "zh-CN"))
    .slice(0, 8);
  if (!topics.length) return "";
  const buttons = [["all", "全部卖点", (preview?.clips || []).filter((clip) => session.role === "all" || previewWorkbenchRoleKey(clip) === session.role).length], ...topics]
    .map(([value, label, count]) => '<button class="' + (session.topic === value ? "is-active" : "") + '" data-action="preview-triage-topic" data-preview-scope="' + scope + '" data-value="' + escapeHtml(value) + '" aria-pressed="' + (session.topic === value ? "true" : "false") + '"><span>' + escapeHtml(label) + '</span><em>' + Number(count || 0) + '</em></button>')
    .join("");
  return '<div class="preview-triage-topic-strip" role="group" aria-label="按卖点模块筛选">' + buttons + '</div>';
}

function renderPreviewTriageQueueCard(clip, scope, analysis, isActive = false) {
  const selected = isPreviewWorkbenchSelected(clip);
  const topic = previewWorkbenchTopicLabel(clip);
  const role = previewWorkbenchRoleLabel(clip);
  const origin = clip?.recommended === false || (clip?.candidate_origin && clip.candidate_origin !== "recommended") ? "备用候选" : "AI 推荐";
  const reason = previewClipReasonParts({ ...clip, selected: true }, analysis, { includeRisk: false })[0] || "可用候选";
  return '<button class="preview-triage-queue-card ' + (isActive ? "is-active" : "") + (selected ? " is-kept" : "") + '" data-action="preview-triage-focus" data-preview-scope="' + scope + '" data-preview-index="' + Number(clip.index) + '"><span class="preview-triage-queue-meta">' + escapeHtml(origin) + ' · ' + escapeHtml(role) + ' · ' + previewWorkbenchCandidateDuration(clip).toFixed(1) + 's</span><strong>' + escapeHtml(topic) + '</strong><small>' + escapeHtml(String(clip.text || "").trim()) + '</small><em>' + escapeHtml(reason) + '</em></button>';
}

function renderPreviewTriage(scope, preview, analysis, targetId) {
  const { session, candidates, clip, position } = previewTriageActive(scope, preview);
  const selectedOrder = previewAssemblyOrder(scope, preview);
  const byIndex = new Map((preview?.clips || []).map((item) => [Number(item.index), item]));
  const selected = selectedOrder.map((index) => byIndex.get(index)).filter(Boolean);
  const totalDuration = selected.reduce((sum, item) => sum + effectiveClipDuration(item), 0);
  const targetDuration = Number(preview?.target_duration || $(targetId)?.value || 60);
  const filterButtons = [
    ["pending", "待审"],
    ["all", "全部"],
    ["kept", "片篮"],
    ["skipped", "已跳过"],
  ].map(([value, label]) => '<button class="' + (session.filter === value ? "is-active" : "") + '" data-action="preview-triage-filter" data-preview-scope="' + scope + '" data-value="' + value + '" aria-pressed="' + (session.filter === value ? "true" : "false") + '">' + label + '</button>').join("");
  const queue = clip ? candidates.slice(position + 1, position + 4).map((item) => renderPreviewTriageQueueCard(item, scope, analysis)).join("") : "";
  const origin = clip?.recommended === false || (clip?.candidate_origin && clip.candidate_origin !== "recommended") ? "备用候选" : "AI 推荐";
  const activeReason = clip ? (previewClipReasonParts({ ...clip, selected: true }, analysis, { includeRisk: false })[0] || "可用候选") : "";
  const activeSelected = clip && isPreviewWorkbenchSelected(clip);
  const activeCard = clip ? '<article class="preview-triage-active-card"><div class="preview-triage-card-top"><span>第 ' + (position + 1) + ' / ' + candidates.length + ' 条</span><span>' + escapeHtml(origin) + ' · ' + previewWorkbenchCandidateDuration(clip).toFixed(1) + 's</span></div><div class="preview-card-tags"><span>' + escapeHtml(previewWorkbenchRoleLabel(clip)) + '</span><span>' + escapeHtml(previewWorkbenchTopicLabel(clip)) + '</span><em>' + (activeSelected ? "已在片篮" : "待决定") + '</em></div><p>' + escapeHtml(String(clip.text || "").trim()) + '</p><small>' + escapeHtml(activeReason) + '</small><div class="preview-triage-actions"><button class="button button-muted button-small" data-action="preview-triage-prev" data-preview-scope="' + scope + '" title="上一条（左方向键）">← 上一条</button><button class="button button-muted button-small" data-action="preview-clip-video" data-preview-scope="' + scope + '" data-preview-index="' + Number(clip.index) + '" title="预览视频（P）">预览 P</button><button class="button button-muted button-small" data-action="preview-triage-skip" data-preview-scope="' + scope + '" title="跳过（X）">跳过 X</button><button class="button button-secondary button-small" data-action="preview-triage-keep" data-preview-scope="' + scope + '" title="保留（A）">' + (activeSelected ? "确认保留 A" : "保留 A") + '</button><button class="button button-muted button-small" data-action="preview-triage-next" data-preview-scope="' + scope + '" title="下一条（右方向键）">下一条 →</button></div></article>' : '<div class="preview-triage-empty"><strong>这个筛选下没有待选片段</strong><span>切换成“全部”或清空关键词，再继续挑选。</span></div>';
  const basket = selected.length ? selected.slice(0, 12).map((item, index) => '<button class="preview-basket-item" data-action="preview-workbench-inspect-clip" data-preview-scope="' + scope + '" data-preview-index="' + Number(item.index) + '"><span>' + (index + 1) + '</span><strong>' + escapeHtml(previewWorkbenchRoleLabel(item)) + '</strong><small>' + escapeHtml(selectedPreviewText(item) || item.text || "") + '</small></button>').join("") : '<div class="preview-sequence-empty"><strong>片篮还是空的</strong><span>保留候选后会自动放到这里。</span></div>';
  return '<div class="preview-triage-toolbar"><div class="preview-workbench-toggle">' + filterButtons + '</div><label class="preview-triage-search"><span>补片搜索</span><input type="search" value="' + escapeHtml(session.search) + '" placeholder="卖点、材质、场景…" data-preview-triage-search="' + scope + '"></label><button class="button button-muted button-small" data-action="preview-triage-undo" data-preview-scope="' + scope + '" ' + (session.history.length ? "" : "disabled") + '>撤销 Ctrl+Z</button></div>' + renderPreviewTriageRoleFilters(scope, preview, session) + renderPreviewTriageTopicFilters(scope, preview, session) + '<div class="preview-triage-layout"><section class="preview-triage-stage"><div class="preview-workbench-column-head"><div><strong>快速选片</strong><span>先预览再决定；A 保留、X 跳过、P 预览</span></div><small>当前筛选 ' + candidates.length + ' 条</small></div><div class="preview-triage-stage-scroll">' + activeCard + (queue ? '<div class="preview-triage-up-next"><strong>接下来</strong><div>' + queue + '</div></div>' : "") + '</div></section><aside class="preview-selection-basket"><div class="preview-workbench-column-head"><div><strong>已保留片篮</strong><span>' + selected.length + ' 段 · ' + totalDuration.toFixed(1) + ' / ' + targetDuration + 's</span></div><button class="button button-secondary button-small" data-action="preview-workbench-stage" data-preview-scope="' + scope + '" data-value="assembly">进入编排</button></div><div class="preview-basket-list">' + basket + '</div><div class="preview-basket-foot"><span>' + (totalDuration > targetDuration * 1.15 ? "已超出目标时长，编排时可逐句精修。" : "保留够了就进入编排，顺序与逐句删减都放在下一步。") + '</span></div></aside></div>';
}

function renderPreviewWorkbenchSequenceCard(clip, position, activeIndex, scope, total = 0) {
  const bounds = effectiveClipBounds(clip);
  const canMoveUp = position > 0;
  const canMoveDown = position < total - 1;
  return '<article class="preview-sequence-card ' + (Number(clip.index) === Number(activeIndex) ? "is-active" : "") + '" draggable="true" data-preview-row data-preview-scope="' + scope + '" data-preview-index="' + Number(clip.index) + '"><div class="clip-drag-handle" title="拖动调整成片顺序">&#9776;</div><button class="preview-sequence-main preview-sequence-select" data-action="preview-workbench-inspect-clip" data-preview-scope="' + scope + '" data-preview-index="' + Number(clip.index) + '"><span><strong>' + (position + 1) + '. ' + escapeHtml(previewWorkbenchRoleLabel(clip)) + '</strong><em>' + escapeHtml(previewWorkbenchTopicLabel(clip)) + ' · ' + bounds.duration.toFixed(1) + 's</em></span><small>' + escapeHtml(selectedPreviewText(clip) || clip.text || "") + '</small></button><div class="preview-sequence-actions"><button title="上移" aria-label="上移" data-action="preview-assembly-move" data-preview-scope="' + scope + '" data-preview-index="' + Number(clip.index) + '" data-direction="-1" ' + (canMoveUp ? "" : "disabled") + '>↑</button><button title="下移" aria-label="下移" data-action="preview-assembly-move" data-preview-scope="' + scope + '" data-preview-index="' + Number(clip.index) + '" data-direction="1" ' + (canMoveDown ? "" : "disabled") + '>↓</button><button class="preview-sequence-remove" title="移出成片" aria-label="移出成片" data-action="preview-assembly-remove" data-preview-scope="' + scope + '" data-preview-index="' + Number(clip.index) + '">×</button></div></article>';
}

function renderPreviewAssembly(scope, preview, analysis, targetId) {
  const selectedOrder = previewAssemblyOrder(scope, preview);
  const byIndex = new Map((preview?.clips || []).map((clip) => [Number(clip.index), clip]));
  const selected = selectedOrder.map((index) => byIndex.get(index)).filter(Boolean);
  const activeIndex = previewDetailIndex(scope, selected);
  const totalDuration = selected.reduce((sum, clip) => sum + effectiveClipDuration(clip), 0);
  const targetDuration = Number(preview?.target_duration || $(targetId)?.value || 60);
  const detail = selected.length ? renderPreviewDetailPanel(scope, preview, analysis, activeIndex, selected) : '<div class="preview-sequence-empty"><strong>还没有保留片段</strong><span>回到快速选片，先把可用内容放进片篮。</span><button class="button button-secondary button-small" data-action="preview-workbench-stage" data-preview-scope="' + scope + '" data-value="triage">去快速选片</button></div>';
  return '<div class="preview-assembly-layout"><section class="preview-assembly-sequence"><div class="preview-workbench-column-head"><div><strong>成片顺序</strong><span>' + selected.length + ' 段 · ' + totalDuration.toFixed(1) + ' / ' + targetDuration + 's</span></div><div class="preview-assembly-head-actions"><button class="button button-muted button-small" data-action="preview-assembly-auto-arrange" data-preview-scope="' + scope + '" ' + (selected.length > 1 ? "" : "disabled") + '>按作用整理</button><button class="button button-secondary button-small" data-action="preview-workbench-stage" data-preview-scope="' + scope + '" data-value="triage">补片</button></div></div><p class="preview-assembly-hint">拖动或用上下按钮调整结构；只展示已保留内容。</p><div class="preview-assembly-scroll">' + (selected.length ? selected.map((clip, position) => renderPreviewWorkbenchSequenceCard(clip, position, activeIndex, scope, selected.length)).join("") : "") + '</div></section><section class="preview-assembly-editor"><div class="preview-workbench-column-head"><div><strong>逐句精修</strong><span>点击成片片段，删除不需要的句子</span></div><small class="' + (totalDuration > targetDuration * 1.15 ? "is-warn" : "") + '">' + (totalDuration > targetDuration * 1.15 ? "超过目标时长" : "已保留内容") + '</small></div><div class="preview-assembly-detail">' + detail + '</div></section></div>';
}

function renderPreviewWorkbench(scope, preview, targetId) {
  preview = hydratePreviewCandidatePool(preview);
  ensurePreviewDraft(scope);
  const clips = preview?.clips || [];
  const analysis = analyzeSmartPreview(preview, targetId);
  const stage = state.previewWorkbenchStages[scope] === "assembly" ? "assembly" : "triage";
  const stageButtons = '<div class="preview-workbench-stage-toggle" role="tablist" aria-label="选片步骤"><button class="' + (stage === "triage" ? "is-active" : "") + '" data-action="preview-workbench-stage" data-preview-scope="' + scope + '" data-value="triage" aria-pressed="' + (stage === "triage" ? "true" : "false") + '"><span>1</span>快速选片</button><button class="' + (stage === "assembly" ? "is-active" : "") + '" data-action="preview-workbench-stage" data-preview-scope="' + scope + '" data-value="assembly" aria-pressed="' + (stage === "assembly" ? "true" : "false") + '"><span>2</span>编排精修</button></div>';
  return '<div data-preview-summary="' + scope + '">' + renderPreviewSummary(analysis) + '</div><div class="preview-workbench-toolbar preview-workbench-stage-bar">' + stageButtons + '<span class="preview-workbench-stage-note">' + (stage === "triage" ? "先看候选，再决定保留；不把逐句删减混在选片里。" : "只处理已保留内容：调顺序、看视频、逐句精修。") + '</span></div><div class="preview-selection-workbench preview-workbench-shell is-' + stage + '" data-preview-workbench="' + scope + '" data-preview-workbench-focus="' + scope + '" tabindex="0">' + (stage === "triage" ? renderPreviewTriage(scope, preview, analysis, targetId) : renderPreviewAssembly(scope, preview, analysis, targetId)) + '</div>';
}

function bindPreviewWorkbenchKeyboard(box, scope = "smart") {
  const workbench = box?.querySelector(`[data-preview-workbench-focus="${scope}"]`);
  if (!workbench) return;
  workbench.addEventListener("keydown", (event) => {
    if (state.previewWorkbenchStages[scope] !== "triage") return;
    if (event.target?.closest?.("input, textarea, select, video, [contenteditable=\"true\"]")) return;
    const key = String(event.key || "").toLocaleLowerCase();
    const active = previewTriageActive(scope).clip;
    if (!active) return;
    if ((event.ctrlKey || event.metaKey) && key === "z") {
      event.preventDefault();
      undoPreviewTriageAction(scope);
      return;
    }
    if (key === "a" || key === "enter") {
      event.preventDefault();
      keepPreviewTriageCandidate(scope);
      return;
    }
    if (key === "x") {
      event.preventDefault();
      skipPreviewTriageCandidate(scope);
      return;
    }
    if (key === "p") {
      event.preventDefault();
      previewClipVideo(Number(active.index), scope);
      return;
    }
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      movePreviewTriage(scope, -1);
      return;
    }
    if (event.key === "ArrowRight") {
      event.preventDefault();
      movePreviewTriage(scope, 1);
    }
  });
}
function renderCommerceDirectorStoryLibrary(review) {
  const library = review?.m1_story_library || {};
  const stories = Array.isArray(library.stories) ? library.stories : [];
  if (!stories.length) return "";
  const cards = stories.map((story) => {
    const assets = Array.isArray(story.assets) ? story.assets : [];
    const candidateCount = new Set(assets.flatMap((asset) => (asset.candidate_lineage || []).map((item) => item.candidate_id))).size;
    const hook = story.hook?.claim || "未发现独立 Hook 证据";
    return `<li><strong>${escapeHtml(story.story_id || "故事")}</strong> · ${escapeHtml(story.angle || "未命名方向")}`
      + ` · 自然 ${Number(story.natural_duration_seconds || 0).toFixed(1)}s<br>`
      + `<span>人群：${escapeHtml(story.target_audience || "未标注")}；购买理由：${escapeHtml(story.purchase_reason || "未标注")}</span><br>`
      + `<span>Hook 证据：${escapeHtml(hook)}；已关联 ${candidateCount} 个冻结候选</span></li>`;
  }).join("");
  return `<details class="preview-overview-details" open><summary>M1 商品故事库（仅发现，不自动组合）</summary><p class="preview-notice">每条故事保留各自的自然时长和候选血缘；选择导演方案后，只有卡片明确声明的 Story Mix 会交给 M2。M2 不会重新发现第四个故事。</p><ol>${cards}</ol></details>`;
}

function renderDirectorStrategyLibrary(review) {
  const library = review?.director_strategy_library || {};
  const proposals = Array.isArray(library.proposals) ? library.proposals : [];
  if (!proposals.length) return "";
  const cards = proposals.map((proposal) => {
    const available = Boolean(proposal.available);
    const mix = (proposal.story_mix || []).map((item) => `${item.story_id || "故事"} · ${item.role || ""}`).join(" + ");
    const structure = proposal.video_structure || {};
    const structureName = structure.name || proposal.narrative_archetype || "导演自定义结构";
    const extraAi = Boolean(proposal.requires_additional_ai_call);
    const action = available
      ? (extraAi
        ? `<button class="button button-primary button-small" data-action="select-commerce-director-strategy" data-director-strategy-id="${escapeHtml(proposal.director_strategy_id || "")}" data-additional-ai-call="true">生成此方向（需 1 次 AI）</button>`
        : `<span class="preview-notice">主方案已生成真实口播预览</span>`)
      : `<span class="preview-notice">${escapeHtml(proposal.unavailable_reason || "当前素材暂不支持")}</span>`;
    return `<article class="commerce-director-strategy-card ${available ? "is-available" : "is-unavailable"}"><div class="commerce-director-strategy-card-head"><div><strong>${escapeHtml(proposal.icon || "")}${escapeHtml(proposal.name || "AI 导演方案")}</strong><span>${escapeHtml(proposal.commercial_goal || proposal.goal || "")}</span></div><em>${Number(proposal.estimated_natural_duration || 0).toFixed(1)}s</em></div><p>${escapeHtml(proposal.headline || "")}</p><dl><div><dt>视频结构</dt><dd>${escapeHtml(structureName)}</dd></div><div><dt>开场承诺</dt><dd>${escapeHtml(proposal.opening_promise || "未形成可验证承诺")}</dd></div><div><dt>故事组合</dt><dd>${escapeHtml(mix || "当前无可验证故事")}</dd></div></dl><footer>${action}</footer></article>`;
  }).join("");
  return `<section class="commerce-director-strategies"><div class="commerce-director-section-head"><div><strong>AI 发现的 ${proposals.length} 种卖法</strong><span>本次只生成主方案；选择备选方向会明确重新调用 2 次 AI（展开故事章节 + 短句 Casting），再按真实原话生成预览。</span></div></div><div class="commerce-director-strategy-grid">${cards}</div></section>`;
}

function commerceDirectorReviewVideoUrl(previewId) {
  const id = String(previewId || "").trim();
  return id ? `/api/smart-cut/commerce-director/review-video/${encodeURIComponent(id)}` : "";
}

function commerceDirectorRoleMeta(role) {
  const value = String(role || "").trim().toLowerCase();
  if (/hook|opening|attention/.test(value)) return { label: "开场吸引", tone: "hook" };
  if (/result|payoff|outcome/.test(value)) return { label: "结果兑现", tone: "proof" };
  if (/proof|mechanism/.test(value)) return { label: "为什么有效", tone: "proof" };
  if (/risk|objection|comfort|fit|coverage|security/.test(value)) return { label: "顾虑解除", tone: "value" };
  if (/scene|styling|ending|close/.test(value)) return { label: "场景收尾", tone: "ending" };
  if (/new_value|value|benefit|trust/.test(value)) return { label: "新购买理由", tone: "value" };
  return { label: "购买证据", tone: "value" };
}

function commerceDirectorOutlineByChapter(items) {
  return new Map((Array.isArray(items) ? items : []).map((item) => [
    String(item?.chapter_id || "").trim(), item || {},
  ]));
}

function commerceDirectorTimelineRows(items, outline = []) {
  const rows = Array.isArray(items) ? items : [];
  if (!rows.length) return '<p class="commerce-director-empty-script">这版没有可审阅的逐句内容。</p>';
  const chapterMap = commerceDirectorOutlineByChapter(outline);
  return `<div class="commerce-director-script-table"><div class="commerce-director-script-table-head"><span>#</span><span>结构 / 完整口播文案</span><span>时长</span><span>购买问题</span></div><ol class="commerce-director-script">${rows.map((item, index) => {
    const text = String(item?.text || "").trim();
    const chapterId = String(item?.chapter_id || item?.clip_type || "").trim();
    const chapter = chapterMap.get(chapterId) || {};
    const role = String(chapter?.narrative_role || chapterId || "").trim();
    const roleMeta = commerceDirectorRoleMeta(role);
    const duration = Number(item?.duration || 0);
    const question = String(chapter?.purchase_value || chapter?.goal || "未标注购买问题").trim();
    return `<li><span class="commerce-director-script-index">${Number(item?.position || index + 1)}</span><div class="commerce-director-script-copy"><span class="commerce-director-role-pill is-${escapeHtml(roleMeta.tone)}">${escapeHtml(roleMeta.label)}</span><p>${escapeHtml(text)}</p><small>${escapeHtml(role || "审阅片段")}</small></div><em>${duration.toFixed(1)}s</em><small class="commerce-director-script-question">${escapeHtml(question)}</small></li>`;
  }).join("")}</ol></div>`;
}

function commerceDirectorEditableTimelineRows(items, outline = [], draftKey = "") {
  const rows = Array.isArray(items) ? items : [];
  if (!rows.length) return '<p class="commerce-director-empty-script">这版暂时没有已选片内容。</p>';
  const chapterMap = commerceDirectorOutlineByChapter(outline);
  return `<div class="commerce-director-script-table commerce-director-editable-script"><div class="commerce-director-script-table-head"><span></span><span>#</span><span>结构角色</span><span>购买问题</span><span>时长</span><span>完整原始口播（可编辑）</span><span>操作</span></div><ol class="commerce-director-script">${rows.map((item, index) => {
    const chapterId = String(item?.chapter_id || item?.clip_type || "").trim();
    const chapter = chapterMap.get(chapterId) || {};
    const role = String(item?.answer_role || chapter?.narrative_role || chapterId || "manual_support").trim();
    const roleMeta = commerceDirectorRoleMeta(role);
    const duration = Number(item?.duration || 0);
    const question = String(item?.purchase_question || chapter?.purchase_question || chapter?.purchase_value || chapter?.goal || (item?.manual_added || item?.manual ? "人工补充购买证据" : "未标注购买问题")).trim();
    const selected = state.commerceDirectorFocusedDraftKey === String(draftKey || "") && state.commerceDirectorFocusedDraftIndex === index;
    return `<li class="${selected ? "is-selected" : ""}" data-action="director-plan-focus-row" data-director-plan-row data-draft-key="${escapeHtml(draftKey)}" data-draft-index="${index}"><button type="button" class="commerce-director-drag-handle" data-director-plan-drag-handle data-draft-key="${escapeHtml(draftKey)}" title="按住拖拽调整顺序" aria-label="按住拖拽调整第 ${index + 1} 条口播顺序">⠿</button><span class="commerce-director-script-index">${index + 1}</span><div class="commerce-director-row-role"><span class="commerce-director-role-pill is-${escapeHtml(roleMeta.tone)}">${escapeHtml(roleMeta.label)}</span><small>${item?.manual_added ? "人工补句" : "M2 已选"}</small></div><small class="commerce-director-script-question">${escapeHtml(question)}</small><em>${duration.toFixed(1)}s</em><div class="commerce-director-script-copy"><textarea rows="2" data-director-plan-text data-draft-key="${escapeHtml(draftKey)}" data-draft-index="${index}" aria-label="第 ${index + 1} 条完整口播文案">${escapeHtml(String(item?.text || ""))}</textarea></div><nav class="commerce-director-row-actions"><button type="button" data-action="director-plan-move" data-draft-key="${escapeHtml(draftKey)}" data-draft-index="${index}" data-direction="-1" ${index === 0 ? "disabled" : ""}>上移</button><button type="button" data-action="director-plan-move" data-draft-key="${escapeHtml(draftKey)}" data-draft-index="${index}" data-direction="1" ${index === rows.length - 1 ? "disabled" : ""}>下移</button><button type="button" data-action="director-plan-replace" data-draft-key="${escapeHtml(draftKey)}" data-draft-index="${index}">替换</button><button type="button" data-action="director-plan-remove" data-draft-key="${escapeHtml(draftKey)}" data-draft-index="${index}">删除</button></nav></li>`;
  }).join("")}</ol></div>`;
}

function bindCommerceDirectorDraftRowDrag(root) {
  const rowSelector = "[data-director-plan-row]";
  let active = null;
  const clear = () => root.querySelectorAll(`${rowSelector}.is-dragging, ${rowSelector}.is-drop-target`).forEach((row) => {
    row.classList.remove("is-dragging", "is-drop-target", "is-drop-after");
  });
  const targetAt = (clientX, clientY) => {
    const row = document.elementFromPoint(clientX, clientY)?.closest?.(rowSelector);
    return row && root.contains(row) ? row : null;
  };
  const update = (event) => {
    if (!active || event.pointerId !== active.pointerId) return;
    if (!active.started && Math.hypot(event.clientX - active.startX, event.clientY - active.startY) < 5) return;
    active.started = true;
    active.source.classList.add("is-dragging");
    const target = targetAt(event.clientX, event.clientY);
    if (active.target && active.target !== target) active.target.classList.remove("is-drop-target", "is-drop-after");
    active.target = target && target !== active.source && target.dataset.draftKey === active.key ? target : null;
    if (!active.target) return;
    const bounds = active.target.getBoundingClientRect();
    active.after = event.clientY >= bounds.top + bounds.height / 2;
    active.target.classList.add("is-drop-target");
    active.target.classList.toggle("is-drop-after", active.after);
  };
  const finish = (event, cancelled = false) => {
    if (!active || event.pointerId !== active.pointerId) return;
    if (!cancelled) update(event);
    const drag = active;
    active = null;
    try { if (drag.handle.hasPointerCapture?.(event.pointerId)) drag.handle.releasePointerCapture(event.pointerId); } catch (_) { /* WebView may release capture first. */ }
    clear();
    if (cancelled || !drag.started || !drag.target) return;
    reorderCommerceDirectorDraft(drag.key, Number(drag.source.dataset.draftIndex), Number(drag.target.dataset.draftIndex), drag.after);
  };
  root.querySelectorAll("[data-director-plan-drag-handle]").forEach((handle) => {
    handle.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || active) return;
      const source = handle.closest(rowSelector);
      if (!source) return;
      event.preventDefault();
      active = { handle, source, target: null, key: String(source.dataset.draftKey || ""), pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, started: false, after: false };
      try { handle.setPointerCapture(event.pointerId); } catch (_) { /* Pointer events still bubble without capture. */ }
    });
    handle.addEventListener("pointermove", (event) => { update(event); if (active?.started) event.preventDefault(); });
    handle.addEventListener("pointerup", (event) => finish(event));
    handle.addEventListener("pointercancel", (event) => finish(event, true));
  });

}

function bindPreviewCandidateDrag(box, scope = "smart") {
  const handleSelector = `[data-preview-candidate-drag-handle][data-preview-scope="${scope}"]`;
  const candidateRowSelector = `[data-preview-candidate-row][data-preview-scope="${scope}"]`;
  const selectedRowSelector = `[data-preview-row][data-preview-scope="${scope}"]`;
  let active = null;

  const clearDragState = () => {
    box.querySelectorAll(`${candidateRowSelector}.is-dragging`).forEach((row) => row.classList.remove("is-dragging"));
    box.querySelectorAll(`${selectedRowSelector}.is-drop-target`).forEach((row) => row.classList.remove("is-drop-target", "is-drop-after"));
    box.querySelectorAll(".preview-selected-list.is-candidate-drop-target").forEach((list) => list.classList.remove("is-candidate-drop-target"));
  };

  const targetAt = (clientX, clientY) => {
    const element = document.elementFromPoint(clientX, clientY);
    const list = element?.closest?.(".preview-selected-list");
    if (!list || !box.contains(list)) return { list: null, row: null };
    const row = element?.closest?.(selectedRowSelector);
    return { list, row: row && list.contains(row) ? row : null };
  };

  const updateTarget = (event) => {
    if (!active || event.pointerId !== active.pointerId) return;
    const moved = Math.hypot(event.clientX - active.startX, event.clientY - active.startY) >= 5;
    if (!active.started && !moved) return;
    active.started = true;
    active.sourceRow.classList.add("is-dragging");

    const target = targetAt(event.clientX, event.clientY);
    if (active.targetRow && active.targetRow !== target.row) {
      active.targetRow.classList.remove("is-drop-target", "is-drop-after");
    }
    if (active.targetList && active.targetList !== target.list) {
      active.targetList.classList.remove("is-candidate-drop-target");
    }
    active.targetList = target.list;
    active.targetRow = target.row;
    active.placeAfter = false;
    if (!active.targetList) return;

    active.targetList.classList.add("is-candidate-drop-target");
    if (active.targetRow) {
      const bounds = active.targetRow.getBoundingClientRect();
      active.placeAfter = event.clientY >= bounds.top + (bounds.height / 2);
      active.targetRow.classList.add("is-drop-target");
      active.targetRow.classList.toggle("is-drop-after", active.placeAfter);
    }
  };

  const finish = (event, cancelled = false) => {
    if (!active || event.pointerId !== active.pointerId) return;
    if (!cancelled) updateTarget(event);
    const drag = active;
    active = null;
    try {
      if (drag.handle.hasPointerCapture?.(event.pointerId)) drag.handle.releasePointerCapture(event.pointerId);
    } catch (_error) {
      // WebView2 may release capture when the pointer crosses scroll regions.
    }
    clearDragState();
    if (cancelled || !drag.started || !drag.targetList) return;
    insertPreviewWorkbenchCandidate(
      Number(drag.sourceRow.dataset.previewIndex),
      scope,
      drag.targetRow ? Number(drag.targetRow.dataset.previewIndex) : null,
      drag.placeAfter,
    );
  };

  box.querySelectorAll(handleSelector).forEach((handle) => {
    handle.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || active) return;
      const sourceRow = handle.closest(candidateRowSelector);
      if (!sourceRow) return;
      event.preventDefault();
      active = {
        handle,
        pointerId: event.pointerId,
        sourceRow,
        targetList: null,
        targetRow: null,
        startX: event.clientX,
        startY: event.clientY,
        started: false,
        placeAfter: false,
      };
      try {
        handle.setPointerCapture(event.pointerId);
      } catch (_error) {
        // Pointer events still bubble on older WebView2 runtimes without capture.
      }
    });
    handle.addEventListener("pointermove", (event) => {
      updateTarget(event);
      if (active?.started) event.preventDefault();
    });
    handle.addEventListener("pointerup", (event) => finish(event));
    handle.addEventListener("pointercancel", (event) => finish(event, true));
  });
}

function commerceDirectorM2Outline(items) {
  const rows = Array.isArray(items) ? items : [];
  if (!rows.length) return "";
  return `<section class="commerce-director-outline"><div class="commerce-director-section-head"><div><strong>购买路径</strong><span>按普通用户能理解的顺序回答购买问题</span></div></div><ol>${rows.map((item, index) => { const meta = commerceDirectorRoleMeta(item?.narrative_role || item?.chapter_id); return `<li><span>${Number(item?.position || index + 1)}</span><div><b>${escapeHtml(item?.purchase_question || item?.purchase_value || item?.goal || "未标注购买问题")}</b><p><i class="commerce-director-role-pill is-${escapeHtml(meta.tone)}">${escapeHtml(meta.label)}</i></p></div><small>${Number(item?.seconds || 0).toFixed(1)}s</small></li>`; }).join("")}</ol></section>`;
}

function commerceDirectorPreviewPanel(previewId, label) {
  const url = commerceDirectorReviewVideoUrl(previewId);
  if (!url) return '<div class="commerce-director-video-unavailable">暂无可播放审阅视频</div>';
  return `<div class="commerce-director-video-wrap"><video controls muted playsinline preload="metadata" src="${escapeHtml(url)}" aria-label="${escapeHtml(label || "商业导演审阅视频")}"></video></div>`;
}

function commerceDirectorSourcePreviewPanel(previewId) {
  const id = String(previewId || "").trim();
  if (!id) return '<div class="commerce-director-video-unavailable">当前没有可播放的源素材</div>';
  return `<div class="commerce-director-video-wrap"><video controls muted playsinline preload="metadata" src="/api/smart-cut/commerce-director/source-video/${encodeURIComponent(id)}" aria-label="当前商品源视频"></video></div>`;
}

function renderCommerceDirectorBatch(review) {
  const results = Array.isArray(review?.batch_results) ? review.batch_results : [];
  if (!results.length) return "";
  const stateCopy = {
    m3_materialized: "M3 词级物化完成",
    m2_sentence_preview: "M2 逐句可编辑预览",
    m2_draft_review_only: "仅 M2 草案审阅",
    blocked: "本方案被合同阻断",
    pending: "仍在生成",
  };
  const activeId = results.some((item) => String(item?.preview_id || "") === state.commerceDirectorActiveResultId)
    ? state.commerceDirectorActiveResultId
    : String(results[0]?.preview_id || "");
  const activeResult = results.find((item) => String(item?.preview_id || "") === activeId) || results[0];
  const renderResult = (result) => {
    const state = String(result.state || "pending");
    const playable = state === "m3_materialized" || state === "m2_draft_review_only" || state === "m2_sentence_preview";
    const outcome = state === "blocked" ? (result.error || result.message || "没有得到可审阅结果") : "";
    const outline = result.m2_outline || [];
    const timeline = commerceDirectorTimelineRows(result.timeline || [], outline);
    const video = playable && result.review_video_available
      ? commerceDirectorPreviewPanel(result.preview_id, `${result.name || "AI 导演方案"}审阅视频`)
      : '<div class="commerce-director-video-unavailable">本方案没有可播放审阅视频</div>';
    return `<article class="commerce-director-result-card" id="commerce-director-result-${escapeHtml(result.director_strategy_id || result.preview_id || "plan")}"><header><div><strong>${escapeHtml(result.icon || "")}${escapeHtml(result.name || "AI 导演方案")}</strong><span>${escapeHtml(stateCopy[state] || stateCopy.pending)}</span></div><em>${Number(result.selected_seconds || 0).toFixed(1)}s · ${Number(result.clip_count || 0)} 段</em></header><div class="commerce-director-plan-summary"><div><span>开场承诺</span><strong>${escapeHtml(result.opening_promise || "未标注")}</strong></div><div><span>商业目标</span><strong>${escapeHtml(result.commercial_goal || "未标注")}</strong></div></div><div class="commerce-director-studio-stage"><div class="commerce-director-studio-video">${video}</div><aside class="commerce-director-studio-path">${commerceDirectorM2Outline(outline)}</aside></div><div class="commerce-director-script-head"><strong>方案口播编排</strong><span>完整文本已展开，无需再查看日志</span></div>${timeline}${outcome ? `<p class="preview-notice">${escapeHtml(outcome)}</p>` : ""}<p class="commerce-director-experiment-note">实验审阅结果，不进入正式预览、导出或发布。</p></article>`;
  };
  const jumpCards = results.map((result) => {
    const selected = String(result?.preview_id || "") === activeId;
    return `<button type="button" class="commerce-director-result-jump ${selected ? "is-active" : ""}" data-action="select-commerce-director-result" data-director-preview-id="${escapeHtml(result.preview_id || "")}" aria-pressed="${selected ? "true" : "false"}"><strong>${escapeHtml(result.icon || "")}${escapeHtml(result.name || "AI 导演方案")}</strong><span>${escapeHtml(result.opening_promise || "未标注开场")}</span><em>${Number(result.selected_seconds || 0).toFixed(1)}s</em></button>`;
  }).join("");
  return `<section class="commerce-director-batch"><div class="commerce-director-section-head"><div><strong>直接比较 ${results.length} 条导演方案</strong><span>先选择一种卖法；视频、购买路径和完整口播在下方同屏审阅。</span></div></div><nav class="commerce-director-result-jumps" aria-label="审阅方案">${jumpCards}</nav><div class="commerce-director-result-grid">${renderResult(activeResult)}</div></section>`;
}

function renderCommerceDirectorReview(preview) {
  const review = preview?.director_review;
  if (!preview?.commercial_director_experiment || !review?.m1_story) return "";
  const story = review.m1_story || {};
  const draft = review.m2_draft || {};
  const storyRows = [
    ["M1 主线", story.thesis],
    ["用户顾虑", story.audience_tension],
    ["购买主张", story.core_commercial_idea],
    ["承诺结果", story.payoff],
  ].filter(([, value]) => String(value || "").trim());
  const chapters = (draft.chapters || []).map((chapter) =>
    `<li><strong>${escapeHtml(chapter.chapter_id || "章节")}</strong> · ${escapeHtml(chapter.narrative_role || "")}`
      + ` · ${Number(chapter.seconds || 0).toFixed(1)}s<br><span>${escapeHtml(chapter.goal || "")}</span></li>`
  ).join("");
  const issues = (draft.issues || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const isDraft = review.kind === "m2_draft_review_only";
  const isDiscovery = review.kind === "m1_story_map_discovery";
  const isBatch = review.kind === "m1_m2_m3_batch_review";
  const headline = review.headline || (isDraft ? "草案审阅" : "M1 主线审阅");
  const stateLabel = isDiscovery ? "M1 已完成，等待选择方案" : isBatch ? "多方案审阅完成" : isDraft ? "M2 草案，M3 未执行" : "M3 词级物化已通过";
  const isPlayableReview = !isDiscovery && !isBatch && (isDraft || review.kind === "m3_materialized_review");
  const timeline = (preview?.clips || []).map((clip, index) => ({
    position: index + 1,
    chapter_id: clip?.clip_type || "",
    text: clip?.text || "",
    duration: clip?.duration || 0,
  }));
  // Older saved draft reviews predate m2_outline.  Reconstruct the same
  // readable purchase-path rows from their persisted chapter summary so the
  // studio never shows an empty planning panel for historical runs.
  const directOutline = Array.isArray(review.m2_outline) && review.m2_outline.length
    ? review.m2_outline
    : (draft.chapters || []).map((chapter, index) => ({
      position: Number(chapter?.position || index + 1),
      chapter_id: chapter?.chapter_id || "",
      narrative_role: chapter?.narrative_role || "",
      goal: chapter?.goal || "",
      purchase_value: chapter?.purchase_value || chapter?.goal || "",
      seconds: Number(chapter?.seconds || 0),
    }));
  const directResult = isPlayableReview
    ? `<section class="commerce-director-single-result"><div class="commerce-director-plan-summary"><div><span>开场承诺</span><strong>${escapeHtml(story.payoff || story.thesis || "未标注")}</strong></div><div><span>当前状态</span><strong>${escapeHtml(stateLabel)}</strong></div></div><div class="commerce-director-studio-stage"><div class="commerce-director-single-video">${commerceDirectorPreviewPanel(preview.id, isDraft ? "M2 草案审阅视频" : "M3 词级审阅视频")}</div><aside class="commerce-director-single-script">${commerceDirectorM2Outline(directOutline)}</aside></div><div class="commerce-director-script-head"><strong>${isDraft ? "M2 草案口播编排" : "M3 词级口播编排"}</strong><span>${timeline.length} 段 · ${timeline.reduce((sum, item) => sum + Number(item.duration || 0), 0).toFixed(1)}s</span></div>${commerceDirectorTimelineRows(timeline, directOutline)}</section>`
    : "";
  const draftSummary = draft.candidate_count
    ? `<p><strong>M2 草案：</strong>${Number(draft.selected_seconds || 0).toFixed(1)}s / 目标 ${Number(draft.target_seconds || 0).toFixed(0)}s，${Number(draft.chapter_count || 0)} 章、${Number(draft.candidate_count || 0)} 段。</p>`
    : "";
  const warning = isDiscovery
    ? '<p class="preview-notice">这里先完成 M1 商品故事发现与 AI 导演方案生成。尚未调用 M2/M3；请选择一个版本后才会进行词级成片实验。</p>'
    : isBatch
    ? '<p class="preview-notice">以下是同一份 M1 商品故事地图下的多条 M2→M3 审阅结果。即使某条完成词级物化，也仅供人工比较，不能正式导出或发布。</p>'
    : isDraft
    ? '<p class="preview-notice">这是一条用于判断叙事方向的 M2 草案审阅视频：候选顺序和词级时间均保持原样，但 M3 已拒绝物化。它不能进入正式预览、导出或发布。</p>'
    : '<p class="preview-notice">以下是 M1 主线及 M3 词级片段，仅供人工审核；不能进入正式成片或发布。</p>';
  // The primary plan now materializes immediately, so alternative direction
  // cards must remain visible on the resulting preview as well as on the
  // legacy discovery screen.
  const strategySection = renderDirectorStrategyLibrary(review)
    + (isDiscovery ? renderCommerceDirectorStoryLibrary(review) : "");
  return `<section class="preview-overview-card commerce-director-review"><div class="commerce-director-workspace-head"><div><span>AI 导演实验工作台</span><strong>${escapeHtml(headline)}</strong></div><em>${escapeHtml(stateLabel)}</em></div><p class="commerce-director-experiment-note">${warning.replace(/^<p class="preview-notice">|<\/p>$/g, "")}</p><div class="preview-overview-grid">${storyRows.map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("")}</div>${directResult}${isBatch ? renderCommerceDirectorBatch(review) : strategySection}${draftSummary}${chapters ? `<div class="commerce-director-chapter-summary"><strong>M2 章节摘要</strong><ol>${chapters}</ol></div>` : ""}${issues ? `<div class="commerce-director-issues"><strong>当前问题</strong><ul>${issues}</ul></div>` : ""}</section>`;
}

function renderSmartPreview(preview) {
  preview = hydratePreviewCandidatePool(preview);
  const box = $("smart-preview");
  const count = $("smart-preview-count");
  if (!box) return;
  const clips = preview?.clips || [];
  if (count) count.textContent = String(clips.length || 0);
  box.classList.toggle("empty", !clips.length);
  box.classList.remove("commerce-director-inline-host");
  setPreviewLayoutState("smart", preview);
  syncFlowActionState();
  if (preview?.commercial_director_experiment && !preview?.commercial_director_preview && !preview?.commercial_director_sentence_preview) {
    state.commerceDirectorStudioOpen = true;
    box.classList.add("commerce-director-inline-host");
    renderCommerceDirectorStudio(preview);
    closePreviewVideo();
    return;
  }
  if (!preview?.id) {
    box.innerHTML = "<p>点击“AI导演预览”，AI 会先确定故事和章节，再从完整字幕选择真实短句；结果可逐句试听、删改和调序。</p>";
    closePreviewVideo();
    return;
  }
  if (preview.status === "running") {
    box.innerHTML = `<p>${escapeHtml(preview.message || "AI 正在规划故事并选择真实短句，请稍等...")}</p>`;
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
  bindPreviewCandidateDrag(box, "smart");
  bindPreviewWorkbenchKeyboard(box, "smart");
  if (state.previewWorkbenchStages.smart === "assembly") ensureInlinePreviewVideo("smart", state.previewDetailSelection.smart);
}

function renderMixPreview(preview) {
  preview = hydratePreviewCandidatePool(preview);
  const box = $("mix-preview");
  const count = $("mix-preview-count");
  if (!box) return;
  const clips = preview?.clips || [];
  if (count) count.textContent = String(clips.length || 0);
  box.classList.toggle("empty", !clips.length);
  setPreviewLayoutState("mix", preview);
  syncFlowActionState();
  if (!preview?.id) {
    box.innerHTML = "<p>点击“AI导演预览”，AI 会先统一理解全部素材，再编排一条可逐句修改的混剪故事。</p>";
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
  bindPreviewCandidateDrag(box, "mix");
  bindPreviewWorkbenchKeyboard(box, "mix");
  if (state.previewWorkbenchStages.mix === "assembly") ensureInlinePreviewVideo("mix", state.previewDetailSelection.mix);
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
  "版型显瘦", "上身效果", "面料质感", "穿着体验", "品质细节", "尺寸长度", "颜色氛围",
  "风格定位", "场景搭配", "工艺细节", "性价比", "对比优势", "情绪感染", "流行趋势",
  "紧迫稀缺", "口感食欲", "新鲜品质", "产地溯源", "规格分量", "发货保鲜", "场景吃法",
];

const previewPreferenceAliases = {
  身材痛点: "版型显瘦",
  版型: "版型显瘦",
  显瘦: "版型显瘦",
  上身: "上身效果",
  试穿: "上身效果",
  上身反差: "上身效果",
  面料: "面料质感",
  质感: "面料质感",
  颜色: "颜色氛围",
  色彩: "颜色氛围",
  风格: "风格定位",
  学院: "风格定位",
  美式: "风格定位",
  气质: "风格定位",
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
  ["上身效果", ["挂着", "挂起来", "上身", "上身后", "穿上", "一穿", "试穿", "试一下", "穿起来", "整体效果", "镜子里", "实物上身"]],
  ["面料质感", ["面料", "材质", "莱赛尔", "天丝", "氨纶", "弹力", "聚酯纤维", "纯棉", "棉麻", "针织", "冰丝", "真丝", "垂感", "垂坠", "高织", "薄纱", "克重"]],
  ["穿着体验", ["舒服", "舒适", "亲肤", "柔软", "冰凉", "凉感", "裸肤", "裸感", "透气", "不闷", "不热", "不勒", "不卡", "不紧绷", "轻盈", "自在", "不透", "活动方便"]],
  ["品质细节", ["品质", "质感", "做工", "走线", "高级感", "精致", "质检", "不起球", "不褪色", "不变形", "色牢度"]],
  ["颜色氛围", ["颜色", "色系", "显白", "提亮", "气色", "肤色", "黄皮", "黑皮", "绿色", "白色", "黑色", "藏青", "藏蓝", "亮色", "彩色", "米白", "冷白", "复古色", "氛围感"]],
  ["风格定位", ["学院", "美式", "韩系", "法式", "日系", "辣妹", "甜酷", "小香风", "老钱风", "千金风", "轻奢", "街头", "松弛感", "俏皮", "减龄", "优雅", "得体", "气质", "清纯", "帅", "风格", "小众", "不烂大街", "复古风"]],
  ["流行趋势", ["流行", "当季", "今年", "本季", "热门", "爆款", "趋势", "秀场", "时装周", "流行元素", "流行款"]],
  ["场景搭配", ["通勤", "上班", "约会", "日常", "出门", "旅游", "度假", "放假", "聚会", "职场", "搭配", "内搭", "外穿", "单穿", "叠穿", "百搭", "拍照", "出片", "夏天", "夏季", "早秋", "初秋", "秋天", "换季", "天气凉"]],
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
  const reviewedTopic = normalizePreviewPreferenceLabel(clip?.content_semantics?.topic);
  if (reviewedTopic) return reviewedTopic;
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
  const displayLabel = previousLabel || actualLabel;
  return {
    ...(summary || {}),
    status: "final",
    mode: "最终片单统计",
    label: displayLabel,
    used_label: displayLabel,
    ai_selected_label: previousLabel || undefined,
    actual_mainline_label: actualLabel,
    source: "final_clips",
    detail: displayLabel !== actualLabel
      ? `AI偏好为${displayLabel}；按最终保留片段统计，正文主题主线为${actualLabel}。`
      : `按最终保留片段统计，主线为${actualLabel}。`,
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
  上身效果: /挂着|挂起来|上身|上身后|穿上|一穿|试穿|试一下|穿起来|整体效果|镜子里|实物上身/,
  面料质感: /面料|材质|手感|触感|亲肤|柔软|垂感|垂坠|透气|冰丝|真丝|纯棉|棉麻|针织|不闷|不透|厚实|薄款/,
  颜色氛围: /颜色|色系|显白|提气色|抬气色|黄皮|黑色|白色|咖色|复古|高级色|温柔色|氛围感|上镜|亮色/,
  风格定位: /学院|美式|韩系|法式|日系|辣妹|甜酷|小香风|老钱风|千金风|轻奢|街头|松弛感|俏皮|减龄|优雅|得体|气质|清纯|帅|风格|小众|不烂大街|复古风/,
  场景搭配: /通勤|上班|约会|日常|逛街|旅游|度假|聚会|职场|见家长|搭配|套穿|叠穿|内搭|外穿|成套|百搭|出门|出片|拍照|夏天|夏季|早秋|初秋|秋天|换季|天气凉/,
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
  if (["版型显瘦", "上身效果", "穿着体验", "口感食欲"].includes(focus) || /显瘦|遮肉|显高|显腿长|上身|穿上|效果|显白|好吃|爆汁|口感|试吃/.test(text)) return "direct_effect";
  if (["面料质感", "品质细节", "工艺细节", "新鲜品质", "产地溯源", "规格分量", "发货保鲜"].includes(focus) || /面料|材质|质感|手感|做工|工艺|细节|品质|新鲜|产地|源头|规格|分量|冷链|包赔/.test(text)) return "proof_detail";
  if (["风格定位", "场景搭配", "场景吃法", "流行趋势"].includes(focus) || /通勤|上班|约会|日常|出门|旅游|搭配|出片|小个子|微胖|梨形|苹果型|全家|早餐|办公室|送礼|学院|美式|韩系|法式|俏皮|减龄|优雅|气质/.test(text)) return "scene_crowd";
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
  const reviewed = clip?.content_semantics && typeof clip.content_semantics === "object"
    ? clip.content_semantics
    : null;
  const reviewedTopic = String(reviewed?.topic || "").trim();
  const reviewedSubtopic = String(reviewed?.subtopic || "").trim();

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

  if (reviewedTopic) {
    add(reviewedTopic, "good", `AI 内容审稿卖点：${reviewedTopic}${reviewedSubtopic ? ` · ${reviewedSubtopic}` : ""}`);
  } else if (focus && focus !== "其他") {
    add(focus, "good", `AI 标注卖点：${focus}`);
  }

  const focusRules = [
    ["版型显瘦", /显瘦|遮肉|藏肉|收腰|显高|显腿长|比例|小个子|梨形|苹果型|胯宽|腿粗|大骨架|盖臀|修身|宽松|版型/, "修饰身材或版型效果"],
    ["上身效果", /挂着|挂起来|上身|上身后|穿上|一穿|试穿|试一下|穿起来|整体效果|镜子里|实物上身/, "挂着、试穿或上身后的视觉变化"],
    ["面料质感", /面料|材质|手感|触感|亲肤|柔软|垂感|垂坠|透气|冰丝|真丝|纯棉|棉麻|针织|不闷|不透|厚实|薄款/, "面料、触感或穿着质感"],
    ["颜色氛围", /颜色|色系|显白|提气色|抬气色|黄皮|黑色|白色|咖色|复古|高级色|温柔色|氛围感|上镜/, "颜色、肤色或视觉氛围"],
    ["风格定位", /学院|美式|韩系|法式|日系|辣妹|甜酷|小香风|老钱风|千金风|轻奢|街头|松弛感|俏皮|减龄|优雅|得体|气质|清纯|帅|风格|小众|不烂大街|复古风/, "款式风格、气质或身份定位"],
    ["场景搭配", /通勤|上班|约会|日常|逛街|旅游|度假|聚会|职场|见家长|搭配|套穿|叠穿|内搭|外穿|成套|百搭|夏天|夏季|早秋|初秋|秋天|换季|天气凉/, "穿着场景、季节或搭配建议"],
    ["穿着体验", /舒服|舒适|不勒|不紧绷|自在|轻盈|无感|不卡|不掉|不卷边|活动方便|不束缚|不扎人|凉爽|温暖/, "穿着感受或活动体验"],
    ["品质细节", /品质|质感|做工|走线|细节|高级感|精致|缝合|刺绣|蕾丝|重工|大牌|专柜/, "品质背书或细节描述"],
    ["尺寸长度", /裙长|衣长|袖长|长度|九分|七分|短款|中长款|过膝|不过膝|露脚踝|遮小腿|盖住|刚好/, "长度、比例或遮盖位置"],
    ["工艺细节", /工艺|成本|拼接|剪裁|立体|定型|压褶|包边|锁边|加固|五金|拉链|扣子|里衬|固色/, "工艺结构或制作细节"],
    ["对比优势", /买不到|外面没有|不一样|区别|独特|独家|同价位|同品质|比外面|比商场|没有第二家|源头/, "对比、稀缺或差异化"],
    ["情绪感染", /绝了|太漂亮|太好看|美爆|太爱|神仙|封神|超级|天呐|妈呀|信我|相信我|真心|自留|美哭|疯了/, "主播情绪或强推荐语气"],
    ["流行趋势", /流行|当季|今年|本季|热门|爆款|趋势|秀场|时装周|流行元素|流行款/, "明确的当季、热门或趋势表达"],
    ["紧迫稀缺", /限量|限时|手慢无|秒空|断码|断货|补不到|不补货|最后|错过|下架|余量|稀缺|卖完/, "紧迫或稀缺表达"],
    ["口感食欲", /好吃|鲜甜|脆甜|爆汁|多汁|汁水|入口|口感|鲜嫩|软糯|酥脆|q弹|弹牙|拉丝|试吃|咬一口/, "试吃、口感或食欲画面"],
    ["新鲜品质", /新鲜|鲜活|现摘|现采|现捕|现捞|当天发|鲜度|品质|果形|果径|个头|饱满|坏果包赔|基地|果园/, "新鲜度、品质或售后信任"],
    ["产地溯源", /产地|原产地|源头|基地|果园|农场|牧场|渔港|海捕|直采|直发|溯源|农户|合作社|当季|应季/, "产地、源头或供应链背书"],
    ["规格分量", /规格|净含量|净重|克重|重量|斤装|箱装|袋装|盒装|整箱|大果|中果|果径|个头|份量|分量/, "规格、重量或分量展示"],
    ["发货保鲜", /发货|现发|冷链|冰袋|保温箱|泡沫箱|顺丰|次日达|保鲜|锁鲜|冷冻|速冻|冷藏|破损包赔/, "发货、物流或保鲜保障"],
    ["场景吃法", /早餐|夜宵|下午茶|办公室|孩子|老人|全家|聚餐|火锅|烧烤|煲汤|下饭|拌饭|空气炸锅|即食|囤货|送礼/, "食用场景或吃法建议"],
  ];
  if (!reviewedTopic) {
    focusRules.forEach(([label, pattern, detail]) => {
      if (label !== focus && pattern.test(text)) add(label, "good", detail);
    });
  }

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
  const selectionResult = dedupSummary.selection_result || {};
  const planQualityReport = dedupSummary.plan_quality_report || {};
  const contentReviewSummary = dedupSummary.content_review_summary || {};
  if (planQualityReport.status === "warning") {
    const qualityIssues = Array.isArray(planQualityReport.soft_quality_issues)
      ? planQualityReport.soft_quality_issues
      : (Array.isArray(planQualityReport.warnings) ? planQualityReport.warnings : []);
    const firstIssue = qualityIssues.map((item) => String(item || "").trim()).find(Boolean);
    if (firstIssue) warnings.push(`AI片单提示：${firstIssue}`);
  }
  if (selectionResult.status === "partial_insufficient") {
    const projected = Number(selectionResult?.details?.projected_final_duration || total);
    warnings.push(`安全内容不足，本次仅生成${projected.toFixed(1)}s人工预览；确认片段后才能成片。`);
  }
  const durationRelaxation = dedupSummary.duration_relaxation || {};
  if (durationRelaxation.applied) {
    const grace = Number(durationRelaxation.grace_seconds || 5);
    const projected = Number(durationRelaxation.projected_final_duration || total);
    warnings.push(`有效内容不足，已按${grace.toFixed(0)}秒弹性时长保留；预计成片${projected.toFixed(1)}s。`);
  }
  const preferenceEligibleClips = preferenceLabel ? clips.filter((clip) => clipEligibleForPreference(clip)) : [];
  const preferenceHitCount = preferenceEligibleClips.filter((clip) => clipMatchesPreference(clip, preferenceLabel)).length;
  if (preferenceLabel && !Object.prototype.hasOwnProperty.call(topicCoverage.topic_counts || {}, preferenceLabel)) {
    const preferenceHitDuration = preferenceEligibleClips
      .filter((clip) => clipMatchesPreference(clip, preferenceLabel))
      .reduce((sum, clip) => sum + effectiveClipDuration(clip), 0);
    const preferenceProductDuration = preferenceEligibleClips
      .reduce((sum, clip) => sum + effectiveClipDuration(clip), 0);
    topicCoverage = {
      ...topicCoverage,
      preference_count: preferenceHitCount,
      preference_ratio: preferenceEligibleClips.length ? preferenceHitCount / preferenceEligibleClips.length : 0,
      preference_duration_ratio: preferenceProductDuration ? preferenceHitDuration / preferenceProductDuration : 0,
    };
  }
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
    planQualityReport,
    contentReviewSummary,
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
      ? (Object.prototype.hasOwnProperty.call(topicCounts, analysis.preferenceLabel)
          ? topicCounts[analysis.preferenceLabel]
          : (analysis.preferenceHitCount ?? topicCoverage.preference_count ?? 0))
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
  const contentReview = analysis.contentReviewSummary || {};
  const contentReviewMode = String(contentReview.mode || "off").toLowerCase();
  const contentReviewLabel = {
    off: "关闭",
    shadow: "影子审稿",
    on: "审稿优先",
  }[contentReviewMode] || "未识别";
  const contentReviewSource = {
    environment: "环境变量",
    task: "本次参数",
    settings: "已保存设置",
  }[String(contentReview.mode_source || "")] || "未知来源";
  const contentPolicy = contentReview.content_policy || {};
  const contentPolicyTitle = [
    `实际模式：${contentReviewLabel}`,
    `来源：${contentReviewSource}`,
    `安全候选：${Number(contentReview.hard_safe_count || 0)} 段 / ${Number(contentReview.hard_safe_duration || 0).toFixed(1)}s`,
    `价格：${contentPolicy.price || "block"}，CTA：${contentPolicy.cta || "block"}，尺码互动：${contentPolicy.size_interaction || "block"}，直播互动：${contentPolicy.live_interaction || "block"}`,
    `自定义规则：${Number(contentPolicy.custom_rule_count || 0)} 条`,
    contentReview.fallback_reason ? `降级原因：${contentReview.fallback_reason}` : "",
  ].filter(Boolean).join("\n");
  const contentReviewTone = contentReviewMode === "on" ? "ok" : (contentReviewMode === "shadow" ? "warn" : "muted");
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
      <div><span>内容审稿</span><strong class="is-${contentReviewTone}" title="${escapeHtml(contentPolicyTitle)}">${escapeHtml(contentReviewLabel)}</strong></div>
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
    renderProductPreview(data.schedule_groups || [], data.schedule_timeline || [], data.schedule_feedback || []);
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

function productScanFeedbackKey(value) {
  return String(value || "").trim().replace(/\s+/g, " ").toLocaleLowerCase();
}

function renderProductScanCutFeedback(item, feedback) {
  const status = String(feedback?.status || (item.status === "missing" ? "skipped" : "ready"));
  const label = String(feedback?.label || (status === "skipped" ? "未切割" : "待切割"));
  const message = String(feedback?.message || (status === "skipped"
    ? "本批素材未覆盖，未生成文件"
    : `已校验，预计切割 ${formatProductScanTime(item.covered_duration)}`));
  return `<span class="product-scan-cut-feedback is-${escapeHtml(status)}" title="切割反馈：${escapeHtml(message)}"><b>切割</b><span>${escapeHtml(label)} · ${escapeHtml(message)}</span></span>`;
}

function renderProductPreview(groups, timeline = [], feedback = []) {
  const box = $("product-preview");
  if (!box) return;
  if (!["working", "ready"].includes(state.productScan.status)) {
    state.productScan.selectionKey = "";
    state.productScan.selectedRangeKeys = new Set();
    state.productScan.groups = [];
    state.productScan.timeline = [];
    state.productScan.feedback = [];
    box.classList.add("empty");
    box.innerHTML = "<p>读取并校验后，这里会列出可导出的商品与对应的文件内时间。</p>";
    renderProductScanInspector([], [], []);
    return;
  }
  state.productScan.groups = Array.isArray(groups) ? groups : [];
  state.productScan.timeline = Array.isArray(timeline) ? timeline : [];
  state.productScan.feedback = Array.isArray(feedback) ? feedback : [];
  ensureProductScanRangeSelection(state.productScan.groups);
  const feedbackByName = new Map(state.productScan.feedback.map((item) => [productScanFeedbackKey(item?.name), item]));
  const rows = state.productScan.groups.map((item, groupIndex) => {
    const status = String(item.status || "missing");
    const coverText = status === "missing"
      ? "不导出"
      : `可导出 ${formatProductScanTime(item.covered_duration)}`;
    const itemFeedback = feedbackByName.get(productScanFeedbackKey(item.name));
    const selectableKeys = productScanSelectableRangeKeysForGroup(groupIndex);
    const selectedCount = selectableKeys.filter((key) => state.productScan.selectedRangeKeys.has(key)).length;
    const allSelected = selectableKeys.length > 0 && selectedCount === selectableKeys.length;
    const selection = selectableKeys.length
      ? `<label class="product-scan-selection product-scan-product-selection" title="勾选或取消整个商品"><input type="checkbox" data-product-scan-select-group="${groupIndex}" ${allSelected ? "checked" : ""} ${!allSelected && selectedCount ? 'data-product-scan-indeterminate="true"' : ""}><span>导出 ${selectedCount}/${selectableKeys.length}</span></label>`
      : `<span class="product-scan-selection is-disabled">不导出</span>`;
    return `<article class="result-row product-scan-result-row is-${escapeHtml(status)}"><div class="product-scan-card-heading"><div class="product-scan-card-title"><strong title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</strong><span class="product-scan-status is-${escapeHtml(status)}">${escapeHtml(productScanCoverageLabel(status))}</span></div>${selection}</div><div class="product-scan-card-meta"><span>${Number(item.segments || 0)} 个排表时段</span><span>${escapeHtml(coverText)}</span>${renderProductScanCutFeedback(item, itemFeedback)}</div><div class="product-scan-ranges">${renderProductScanRanges(item, groupIndex)}</div></article>`;
  });
  box.classList.toggle("empty", rows.length === 0);
  box.innerHTML = rows.length ? rows.join("") : "<p>时间表中没有可用的商品时段，请检查 Excel 格式。</p>";
  box.querySelectorAll('input[data-product-scan-indeterminate="true"]').forEach((input) => {
    input.indeterminate = true;
  });
  renderProductScanInspector(state.productScan.groups, state.productScan.timeline, state.productScan.feedback);
}

function formatSeconds(value) {
  const seconds = Math.max(0, Number(value || 0));
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function collectFeaturePayload(feature) {
  if (feature === "mix") {
    const primaryCategory = primaryCategoryValue("mix");
    return {
      video_paths: getLines("mix-video-paths"),
      output_dir: $("mix-output-dir").value.trim(),
      output_naming_mode: $("mix-output-naming")?.value || "source_timestamp",
      primary_category: primaryCategory,
      category: backendCategoryForPrimary(primaryCategory),
      versions: Number($("mix-versions").value || 1),
      duration: Number($("mix-duration").value || 60),
      duration_tolerance: selectedDurationTolerance("mix"),
      focus_hint: "自动",
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
    const alignmentMode = productScanAlignmentMode();
    return {
      excel_path: $("ps-excel-path").value.trim(),
      video_paths: getLines("ps-video-paths"),
      output_dir: $("ps-output-dir").value.trim(),
      advance_seconds: Number($("ps-advance").value || 0),
      fast_cut: $("ps-fast-cut")?.checked !== false,
      selected_ranges: feature === "product-scan" ? productScanSelectedRangeKeys() : [],
      video_start_offset: alignmentMode === "manual" ? $("ps-video-start-offset")?.value.trim() || "" : "",
      live_start_time: productScanNeedsLiveStart() ? productScanLiveStartValue() : "",
      schedule_time_basis: productScanTimeBasis(),
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

// [AI_WORKBENCH_INSERTION_POINT]
// Direct AI selection workbench: word-level state and export draft.
if (!state.previewCandidateSelections) state.previewCandidateSelections = { smart: null, mix: null };
// Keep the one-screen workbench in its compatible assembly state so its
// currently focused clip loads an inline video automatically.
state.previewWorkbenchStages.smart = "assembly";
state.previewWorkbenchStages.mix = "assembly";

function previewSegments(clip) {
  return Array.isArray(clip?.segments) ? clip.segments : [];
}

function previewSegmentWords(segment) {
  return Array.isArray(segment?.words) ? segment.words : [];
}

function normalizePreviewWordIndices(preview) {
  let normalized = false;
  const clips = Array.isArray(preview?.clips) ? preview.clips : [];
  clips.forEach(function (clip) {
    previewSegments(clip).forEach(function (segment) {
      const words = previewSegmentWords(segment);
      const seen = new Set();
      const needsNormalization = words.some(function (word) {
        const index = Number(word?.index);
        if (!Number.isInteger(index) || seen.has(index)) return true;
        seen.add(index);
        return false;
      });
      if (!needsNormalization) return;
      words.forEach(function (word, index) {
        if (word && typeof word === "object") word.index = index;
      });
      normalized = true;
    });
  });
  return normalized;
}

function isPreviewWordLocked(word) {
  return word?.selection_locked === true;
}

function isPreviewWordSelected(word) {
  return !isPreviewWordLocked(word) && word?.selected !== false;
}

function selectedPreviewWords(segment) {
  return previewSegmentWords(segment).filter(isPreviewWordSelected);
}

function selectedPreviewSegmentText(segment) {
  const words = previewSegmentWords(segment);
  return words.length ? selectedPreviewWords(segment).map((word) => String(word?.text || "")).join("").trim() : String(segment?.text || "").trim();
}

function isPreviewSegmentSelected(segment) {
  if (!segment || segment.selection_locked === true || segment.selected === false) return false;
  const words = previewSegmentWords(segment);
  return !words.length || selectedPreviewWords(segment).length > 0;
}

function selectedPreviewSegments(clip) {
  return previewSegments(clip).filter(isPreviewSegmentSelected);
}

function selectedPreviewText(clip) {
  const segments = previewSegments(clip);
  if (!segments.length) return String(clip?.text || "").trim();
  const selected = selectedPreviewSegments(clip);
  return selected.length ? selected.map(selectedPreviewSegmentText).filter(Boolean).join(" ") : "\u672a\u9009\u62e9\u53e5\u5b50";
}

function selectedSegmentCountText(clip) {
  const segments = previewSegments(clip);
  return segments.length ? `${selectedPreviewSegments(clip).length}/${segments.length}\u53e5` : "";
}

function effectivePreviewSegmentBounds(segment) {
  const start = Number(segment?.start || 0);
  const end = Number(segment?.end || start);
  const words = previewSegmentWords(segment);
  const selected = selectedPreviewWords(segment);
  if (segment?.wordSelectionExplicit === true && words.length && selected.length) {
    return { start: Math.min(...selected.map((word) => Number(word.start || start))), end: Math.max(...selected.map((word) => Number(word.end || Number(word.start || start)))) };
  }
  return { start, end };
}

function effectivePreviewSegmentDuration(segment) {
  const words = previewSegmentWords(segment);
  if (segment?.wordSelectionExplicit === true && words.length) return selectedPreviewWords(segment).reduce((sum, word) => {
    const start = Number(word.start || 0);
    const end = Number(word.end || start);
    return sum + Math.max(0, end - start);
  }, 0);
  const bounds = effectivePreviewSegmentBounds(segment);
  return Math.max(0, Number(segment?.duration || bounds.end - bounds.start));
}

function effectiveClipDuration(clip) {
  if (clip?.selected === false) return 0;
  const segments = previewSegments(clip);
  if (segments.length) return selectedPreviewSegments(clip).reduce((sum, segment) => sum + effectivePreviewSegmentDuration(segment), 0);
  const start = Number(clip?.start || 0);
  const end = Number(clip?.end || start);
  return Math.max(0, Number(clip?.duration || end - start));
}

function effectiveClipBounds(clip) {
  const allSegments = previewSegments(clip);
  const segments = selectedPreviewSegments(clip);
  if (allSegments.length && segments.length) {
    const bounds = segments.map(effectivePreviewSegmentBounds);
    return { start: Math.min(...bounds.map((item) => item.start)), end: Math.max(...bounds.map((item) => item.end)), duration: effectiveClipDuration(clip) };
  }
  const start = Number(clip?.start || 0);
  const end = Number(clip?.end || start);
  if (allSegments.length || clip?.selected === false) return { start, end, duration: 0 };
  return { start, end, duration: Math.max(0, Number(clip?.duration || end - start)) };
}

function previewCandidateKey(clip) {
  return String(clip?.candidate_key || "").trim();
}

function buildPreviewDraftFromState(scope = "smart") {
  const preview = getPreviewState(scope);
  const draft = {
    preview_id: preview?.id || "", scope, order: [], order_keys: [], selected_indices: [], selected_keys: [],
    selected_segments: {}, selected_words: {}, selected_segments_by_key: {}, selected_words_by_key: {}, updated_at: Date.now(),
  };
  const clipsByIndex = new Map((preview?.clips || []).map((clip) => [Number(clip.index), clip]));
  const selectedOrder = previewAssemblyOrder(scope, preview);
  const remaining = (preview?.clips || []).map((clip) => Number(clip.index)).filter((index) => Number.isInteger(index) && !selectedOrder.includes(index));
  draft.order = [...selectedOrder, ...remaining];
  draft.order_keys = draft.order.map((index) => previewCandidateKey(clipsByIndex.get(index))).filter(Boolean);
  selectedOrder.forEach((clipIndex) => {
    const clip = clipsByIndex.get(clipIndex);
    if (!clip) return;
    const segments = previewSegments(clip);
    const keptSegments = segments.filter(isPreviewSegmentSelected).map((segment) => Number(segment.index)).filter(Number.isInteger);
    if (clip.selected === false || (segments.length && !keptSegments.length)) return;
    draft.selected_indices.push(clipIndex);
    const candidateKey = previewCandidateKey(clip);
    if (candidateKey) draft.selected_keys.push(candidateKey);
    if (segments.length) {
      draft.selected_segments[String(clipIndex)] = keptSegments;
      if (candidateKey) draft.selected_segments_by_key[candidateKey] = keptSegments;
    }
    const explicitWords = {};
    segments.forEach((segment) => {
      if (!segment.wordSelectionExplicit || !previewSegmentWords(segment).length) return;
      const segmentIndex = Number(segment.index);
      if (Number.isInteger(segmentIndex)) explicitWords[String(segmentIndex)] = selectedPreviewWords(segment).map((word) => Number(word.index)).filter(Number.isInteger);
    });
    if (Object.keys(explicitWords).length) {
      draft.selected_words[String(clipIndex)] = explicitWords;
      if (candidateKey) draft.selected_words_by_key[candidateKey] = explicitWords;
    }
  });
  return draft;
}

// [AI_WORKBENCH_WORD_MODEL_END]

function applyPreviewDraftToState(scope = "smart", draft = null) {
  const preview = getPreviewState(scope);
  if (!preview?.clips?.length || !draft) return;
  const keyToIndex = new Map(preview.clips.map((clip) => [previewCandidateKey(clip), Number(clip.index)]).filter(([key, index]) => key && Number.isInteger(index)));
  const selectedKeys = Array.isArray(draft.selected_keys) ? draft.selected_keys.map(String).filter(Boolean) : [];
  const orderKeys = Array.isArray(draft.order_keys) ? draft.order_keys.map(String).filter(Boolean) : [];
  const hasSelectedKeys = selectedKeys.length > 0;
  const hasSelectedIndices = hasSelectedKeys || Array.isArray(draft.selected_indices);
  const selectedByKey = selectedKeys.map((key) => keyToIndex.get(key)).filter(Number.isInteger);
  const selectedSet = new Set(hasSelectedKeys ? selectedByKey : normalizedIntegerList(draft.selected_indices));
  const order = orderKeys.length ? orderKeys.map((key) => keyToIndex.get(key)).filter(Number.isInteger) : normalizedIntegerList(draft.order);
  const segmentMap = draft.selected_segments && typeof draft.selected_segments === "object" ? draft.selected_segments : {};
  const wordMap = draft.selected_words && typeof draft.selected_words === "object" ? draft.selected_words : {};
  const segmentKeyMap = draft.selected_segments_by_key && typeof draft.selected_segments_by_key === "object" ? draft.selected_segments_by_key : {};
  const wordKeyMap = draft.selected_words_by_key && typeof draft.selected_words_by_key === "object" ? draft.selected_words_by_key : {};
  preview.clips.forEach((clip) => {
    const clipIndex = Number(clip.index);
    const candidateKey = previewCandidateKey(clip);
    const segments = previewSegments(clip);
    const selectedByDraft = hasSelectedIndices ? selectedSet.has(clipIndex) : clip.selected !== false;
    if (!selectedByDraft) {
      clip.selected = false;
      segments.forEach((segment) => { segment.selected = false; previewSegmentWords(segment).forEach((word) => { word.selected = false; }); });
      return;
    }
    const segmentValues = Array.isArray(segmentKeyMap[candidateKey]) ? segmentKeyMap[candidateKey] : segmentMap[String(clipIndex)];
    if (segments.length && Array.isArray(segmentValues)) {
      const segmentSet = new Set(normalizedIntegerList(segmentValues));
      segments.forEach((segment) => { segment.selected = segment.selection_locked === true ? false : segmentSet.has(Number(segment.index)); });
    } else if (segments.length) {
      segments.forEach((segment) => { if (segment.selection_locked === true) segment.selected = false; else if (segment.selected === undefined) segment.selected = true; });
    }
    const keyedWordMap = wordKeyMap[candidateKey];
    const clipWordMap = keyedWordMap && typeof keyedWordMap === "object" ? keyedWordMap : (wordMap[String(clipIndex)] && typeof wordMap[String(clipIndex)] === "object" ? wordMap[String(clipIndex)] : {});
    segments.forEach((segment) => {
      const words = previewSegmentWords(segment);
      if (!words.length) return;
      const values = clipWordMap[String(segment.index)];
      if (Array.isArray(values)) {
        const selectedWords = new Set(normalizedIntegerList(values));
        segment.wordSelectionExplicit = true;
        words.forEach((word) => { word.selected = isPreviewWordLocked(word) ? false : selectedWords.has(Number(word.index)); });
        segment.selected = segment.selection_locked !== true && selectedPreviewWords(segment).length > 0;
      } else {
        segment.wordSelectionExplicit = false;
        words.forEach((word) => { if (isPreviewWordLocked(word)) word.selected = false; else if (word.selected === undefined) word.selected = true; });
        if (segment.selected !== false && !selectedPreviewWords(segment).length) segment.selected = false;
      }
    });
    clip.selected = !segments.length || segments.some(isPreviewSegmentSelected);
  });
  const selectedOrder = hasSelectedKeys ? selectedByKey : normalizedIntegerList(draft.selected_indices);
  const fallbackOrder = order.filter((index) => selectedSet.has(index));
  const preferred = (selectedOrder.length ? selectedOrder : fallbackOrder).filter((index) => selectedSet.has(index));
  preview.clips.forEach((clip) => {
    const index = Number(clip.index);
    if (isPreviewWorkbenchSelected(clip) && Number.isInteger(index) && !preferred.includes(index)) preferred.push(index);
  });
  state.previewAssemblyOrders[previewAssemblyOrderKey(scope, preview)] = preferred;
}

function resetPreviewSegmentWords(segment) {
  previewSegmentWords(segment).forEach((word) => { word.selected = !isPreviewWordLocked(word); });
  segment.wordSelectionExplicit = false;
}

function syncPreviewClipSelections(scope = "smart") {
  const preview = getPreviewState(scope);
  if (!preview?.clips) return;
  const checked = new Map(Array.from(document.querySelectorAll(`[data-preview-clip][data-preview-scope="${scope}"]`)).map((node) => [Number(node.dataset.previewClip), node.checked]));
  const segmentChecked = new Map(Array.from(document.querySelectorAll(`[data-preview-segment][data-preview-scope="${scope}"]`)).map((node) => [`${Number(node.dataset.previewSegmentParent)}:${Number(node.dataset.previewSegmentIndex)}`, node.checked]));
  preview.clips.forEach((clip) => {
    const clipIndex = Number(clip.index);
    const segments = previewSegments(clip);
    segments.forEach((segment) => {
      const key = `${clipIndex}:${Number(segment.index)}`;
      if (segment.selection_locked === true) segment.selected = false;
      else if (segmentChecked.has(key)) segment.selected = segmentChecked.get(key);
      else if (segment.selected === undefined) segment.selected = true;
      previewSegmentWords(segment).forEach((word) => { if (isPreviewWordLocked(word)) word.selected = false; else if (word.selected === undefined) word.selected = true; });
      if (segment.selected !== false && previewSegmentWords(segment).length && !selectedPreviewWords(segment).length) segment.selected = false;
    });
    const anySegmentSelected = segments.length ? segments.some(isPreviewSegmentSelected) : true;
    if (checked.has(clipIndex)) clip.selected = checked.get(clipIndex) && anySegmentSelected;
    else if (clip.selected === undefined) clip.selected = true;
    if (segments.length && !anySegmentSelected) clip.selected = false;
  });
}

function updatePreviewClipSelection(index, selected, scope = "smart") {
  const preview = getPreviewState(scope);
  const clip = preview?.clips?.find((item) => Number(item.index) === Number(index));
  if (!clip) return;
  clip.selected = selected;
  previewSegments(clip).forEach((segment) => {
    segment.selected = segment.selection_locked === true ? false : selected;
    if (selected) resetPreviewSegmentWords(segment); else previewSegmentWords(segment).forEach((word) => { word.selected = false; });
  });
  setPreviewAssemblyMembership(scope, Number(index), selected);
  commitPreviewDraft(scope);
  refreshPreviewSelectionUi(scope);
}

function updatePreviewSegmentSelection(index, segmentIndex, selected, scope = "smart") {
  const preview = getPreviewState(scope);
  const clip = preview?.clips?.find((item) => Number(item.index) === Number(index));
  const segment = previewSegments(clip).find((item) => Number(item.index) === Number(segmentIndex));
  if (!clip || !segment || segment.selection_locked === true) return;
  segment.selected = selected;
  if (selected) resetPreviewSegmentWords(segment);
  else { previewSegmentWords(segment).forEach((word) => { word.selected = false; }); segment.wordSelectionExplicit = false; }
  clip.selected = previewSegments(clip).some(isPreviewSegmentSelected);
  setPreviewAssemblyMembership(scope, Number(index), clip.selected);
  commitPreviewDraft(scope);
  refreshPreviewSelectionUi(scope);
}

function togglePreviewWordSelection(clipIndex, segmentIndex, wordIndex, scope = "smart") {
  const preview = getPreviewState(scope);
  const clip = preview?.clips?.find((item) => Number(item.index) === Number(clipIndex));
  const segment = previewSegments(clip).find((item) => Number(item.index) === Number(segmentIndex));
  const word = previewSegmentWords(segment).find((item) => Number(item.index) === Number(wordIndex));
  if (!clip || !segment || !word || isPreviewWordLocked(word) || segment.selection_locked === true) return;
  word.selected = word.selected === false;
  const selectable = previewSegmentWords(segment).filter((item) => !isPreviewWordLocked(item));
  segment.wordSelectionExplicit = selectedPreviewWords(segment).length !== selectable.length;
  segment.selected = selectedPreviewWords(segment).length > 0;
  clip.selected = previewSegments(clip).some(isPreviewSegmentSelected);
  setPreviewAssemblyMembership(scope, Number(clipIndex), clip.selected);
  commitPreviewDraft(scope);
  renderPreviewStateKeepStoryScroll(scope);
}

function collectPreviewSelection(scope = "smart") {
  syncPreviewClipSelections(scope);
  const draft = commitPreviewDraft(scope, { remote: true });
  return {
    selectedIndices: draft.selected_indices || [], selectedKeys: draft.selected_keys || [],
    order: draft.order || [], orderKeys: draft.order_keys || [], selectedSegments: draft.selected_segments || {},
    selectedWords: draft.selected_words || {}, selectedSegmentsByKey: draft.selected_segments_by_key || {},
    selectedWordsByKey: draft.selected_words_by_key || {},
  };
}

// [AI_WORKBENCH_SELECTION_END]

function previewInlineVideoKey(scope, preview, clip, draft = null, { inspectOnly = false } = {}) {
  if (!preview?.id || !clip) return "";
  const clipIndex = Number(clip.index);
  const selection = draft || buildPreviewDraftFromState(scope);
  const selectedSegments = selection?.selected_segments && Array.isArray(selection.selected_segments[String(clipIndex)]) ? selection.selected_segments[String(clipIndex)].join(",") : "";
  const wordsBySegment = selection?.selected_words?.[String(clipIndex)] || {};
  const selectedWords = Object.keys(wordsBySegment).sort((left, right) => Number(left) - Number(right)).map((segmentIndex) => `${segmentIndex}:${normalizedIntegerList(wordsBySegment[segmentIndex]).join(",")}`).join("|");
  const selected = Array.isArray(selection?.selected_indices) && selection.selected_indices.includes(clipIndex) ? "1" : "0";
  return `${scope}:${preview.id}:${clipIndex}:${inspectOnly ? "inspect" : selected}:${selectedSegments}:${selectedWords}`;
}

function renderPreviewInlineVideo(scope, preview, clip, { inspectOnly = false, idleText = "\u70b9\u51fb\u9884\u89c8\u89c6\u9891\u540e\u663e\u793a\u8fd9\u91cc\u3002" } = {}) {
  const segments = previewSegments(clip);
  const hasSelectedText = inspectOnly || (clip.selected !== false && (!segments.length || selectedPreviewSegments(clip).length));
  const draft = inspectOnly ? null : buildPreviewDraftFromState(scope);
  const key = previewInlineVideoKey(scope, preview, clip, draft, { inspectOnly });
  const entry = state.previewInlineVideos[key] || {};
  const videoClass = entry.url ? "" : "is-hidden";
  const statusClass = entry.url ? "is-hidden" : "";
  const statusText = !hasSelectedText ? "\u5148\u4fdd\u7559\u81f3\u5c11\u4e00\u53e5\u5185\u5bb9\u540e\u518d\u9884\u89c8\u3002" : entry.error ? `\u5c0f\u89c6\u9891\u751f\u6210\u5931\u8d25\uff1a${entry.error}` : entry.status === "loading" ? "\u6b63\u5728\u751f\u6210\u7247\u6bb5\u5c0f\u89c6\u9891..." : idleText;
  return `<div class="clip-detail-video" data-preview-inline-video="${scope}" data-preview-index="${Number(clip.index)}" data-video-key="${escapeHtml(key)}"><div class="clip-detail-video-stage"><video class="${videoClass}" data-preview-inline-player controls playsinline ${previewInlineAudioMutedAttribute()} autoplay preload="metadata" ${entry.url ? `src="${escapeHtml(entry.url)}"` : ""}></video><div class="clip-detail-video-status ${statusClass} ${entry.error ? "is-error" : ""}" data-preview-inline-status>${escapeHtml(statusText)}</div></div></div>`;
}

function isRetryableInlinePreviewError(error) {
  const status = Number(error?.status || 0);
  return !status || status === 408 || status === 409 || status >= 500;
}

async function ensureInlinePreviewVideo(scope = "smart", index = null, { inspectOnly = false, force = false, retryAttempt = 0 } = {}) {
  const preview = getPreviewState(scope);
  if (!preview?.id || preview.status !== "ready") return;
  const clipIndex = Number(index);
  const clip = preview.clips?.find((item) => Number(item.index) === clipIndex);
  if (!clip) return;
  syncPreviewClipSelections(scope);
  const segments = previewSegments(clip);
  if (!inspectOnly && (clip.selected === false || (segments.length && !selectedPreviewSegments(clip).length))) return;
  const draft = inspectOnly ? null : commitPreviewDraft(scope, { remote: true });
  const key = previewInlineVideoKey(scope, preview, clip, draft, { inspectOnly });
  if (!key) return;
  const existing = state.previewInlineVideos[key];
  if (existing?.url || (existing?.status === "loading" && !force) || (existing?.error && !force)) {
    applyInlinePreviewVideoState(scope, clipIndex, key);
    return;
  }
  const attempt = Math.max(1, Number(retryAttempt || 0) + 1);
  state.previewInlineVideos[key] = { status: "loading", attempt, inspectOnly };
  applyInlinePreviewVideoState(scope, clipIndex, key);
  const endpoint = scope === "mix" ? "/api/mix/preview/clip-video" : "/api/smart-cut/preview/clip-video";
  try {
    const result = await api(endpoint, {
      method: "POST",
      body: JSON.stringify(inspectOnly ? { preview_id: preview.id, clip_index: clipIndex, scope, inspect_only: true } : {
        preview_id: preview.id,
        clip_index: clipIndex,
        scope,
        selected_indices: draft.selected_indices || [],
        selected_keys: draft.selected_keys || [],
        order: draft.order || [],
        order_keys: draft.order_keys || [],
        selected_segments: draft.selected_segments || {},
        selected_words: draft.selected_words || {},
        selected_segments_by_key: draft.selected_segments_by_key || {},
        selected_words_by_key: draft.selected_words_by_key || {},
        updated_at: draft.updated_at || Date.now(),
      }),
    });
    state.previewInlineVideos[key] = { status: "ready", url: result.url, attempt, inspectOnly };
  } catch (error) {
    const failed = {
      status: "failed",
      error: error.message || String(error || "\u672a\u77e5\u9519\u8bef"),
      attempt,
      inspectOnly,
      retryable: isRetryableInlinePreviewError(error),
    };
    state.previewInlineVideos[key] = failed;
    applyInlinePreviewVideoState(scope, clipIndex, key);
    if (failed.retryable && retryAttempt < 1) {
      window.setTimeout(() => {
        const latest = state.previewInlineVideos[key];
        const latestPreview = getPreviewState(scope);
        const latestClip = latestPreview?.clips?.find((item) => Number(item.index) === clipIndex);
        const latestDraft = inspectOnly ? null : buildPreviewDraftFromState(scope);
        const latestKey = latestClip ? previewInlineVideoKey(scope, latestPreview, latestClip, latestDraft, { inspectOnly }) : "";
        if (latest?.status !== "failed" || Number(latest.attempt) !== attempt || latestKey !== key) return;
        ensureInlinePreviewVideo(scope, clipIndex, { inspectOnly, force: true, retryAttempt: retryAttempt + 1 });
      }, 450);
    }
    return;
  }
  applyInlinePreviewVideoState(scope, clipIndex, key);
}

// [AI_WORKBENCH_VIDEO_END]

const directPreviewWorkbenchCandidateCategories = [
  ["hook", "\u5f00\u573a\u5438\u5f15"], ["pref_fabric", "\u9762\u6599\u8d28\u611f"], ["pref_quality", "\u54c1\u8d28\u7ec6\u8282"], ["pref_fit", "\u7248\u578b\u663e\u7626"],
  ["pref_color", "\u989c\u8272\u6c1b\u56f4"], ["pref_scene", "\u573a\u666f\u642d\u914d"], ["pref_emotion", "\u60c5\u7eea\u611f\u67d3"],
  ["pref_value", "\u6027\u4ef7\u6bd4"], ["pref_urgency", "\u7d27\u8feb\u7a00\u7f3a"], ["pref_trend", "\u6d41\u884c\u8d8b\u52bf"],
  ["food_taste", "\u53e3\u611f\u98df\u6b32"], ["food_quality", "\u65b0\u9c9c\u54c1\u8d28"], ["food_origin", "\u4ea7\u5730\u6eaf\u6e90"],
  ["food_spec", "\u89c4\u683c\u5206\u91cf"], ["food_fresh", "\u53d1\u8d27\u4fdd\u9c9c"], ["food_scene", "\u573a\u666f\u5403\u6cd5"],
  ["live", "\u76f4\u64ad\u4e92\u52a8"], ["unclear", "\u5f85\u786e\u8ba4"], ["close", "\u6536\u5c3e"],
];

function previewWorkbenchCandidateCategory(clip) {
  const role = previewWorkbenchRoleKey(clip);
  const text = `${clip?.focus || ""} ${clip?.focus_block || ""} ${clip?.text || ""}`.toLowerCase();
  if (role === "hook" || /\u5f00\u573a|\u7b2c\u4e00\u53e5|\u5148\u770b|\u59d0\u59b9\u4eec|\u5b9d\u5b9d\u4eec|\u6ce8\u610f/.test(text)) return "hook";
  if (role === "close" || /\u6536\u5c3e|\u6700\u540e|\u4e0d\u8981\u9519\u8fc7/.test(text)) return "close";
  if (/\u5c3a\u7801|\u7801\u6570|\u8eab\u9ad8|\u4f53\u91cd|\u65a4|s\u7801|m\u7801|l\u7801|xl/.test(text)) return "size";
  if (/\u642d\u914d|\u7a7f\u642d|\u5185\u642d|\u5916\u642d|\u53e0\u7a7f|\u914d\u4e0a|\u914d\u8fd9\u4e2a/.test(text)) return "styling";
  if (role === "scene" || /\u4e0a\u8eab|\u8bd5\u7a7f|\u65e5\u5e38|\u901a\u52e4|\u7ea6\u4f1a|\u51fa\u95e8|\u573a\u666f|\u62cd\u7167|\u8fd0\u52a8/.test(text)) return "scene";
  if (/\u9762\u6599|\u6750\u8d28|\u624b\u611f|\u900f\u6c14|\u4eb2\u80a4|\u67d4\u8f6f|\u5782\u611f|\u51b0\u4e1d|\u9488\u7ec7|\u68c9|\u9ebb/.test(text)) return "fabric";
  if (/\u7248\u578b|\u663e\u7626|\u906e\u8089|\u663e\u9ad8|\u6536\u8170|\u8170\u7ebf|\u817f\u957f|\u80a9|\u80ef|\u8eab\u6750|\u5bbd\u677e|\u4fee\u8eab/.test(text)) return "fit";
  if (role === "proof" || /\u7ec6\u8282|\u505a\u5de5|\u5de5\u827a|\u54c1\u8d28|\u8d70\u7ebf|\u5bf9\u6bd4|\u6b63\u54c1|\u53cd\u9988|\u9500\u91cf|\u4fdd\u8bc1/.test(text)) return "proof";
  return "core";
}

function previewWorkbenchCategoryLabel(clip, scope = "smart") {
  if (String(clip?.director_beat_function || "").trim()) {
    return previewDirectorBeatRoleMeta(clip).label;
  }
  const key = previewWorkbenchCandidateCategory(clip, scope);
  return directPreviewWorkbenchCandidateCategories.find(([item]) => item === key)?.[1] || "\u5546\u54c1\u4eae\u70b9";
}

function previewDirectorBeatRoleMeta(clip) {
  const key = String(clip?.director_beat_function || clip?.director_chapter_kind || "").trim().toLowerCase();
  return ({
    hook: { label: "开场吸引", tone: "hook" },
    result: { label: "结果兑现", tone: "result" },
    mechanism: { label: "为什么有效", tone: "mechanism" },
    proof: { label: "证据补强", tone: "proof" },
    comfort: { label: "穿着体验", tone: "comfort" },
    fit: { label: "身材适配", tone: "fit" },
    risk: { label: "顾虑解除", tone: "risk" },
    risk_remove: { label: "顾虑解除", tone: "risk" },
    styling: { label: "日常搭配", tone: "scene" },
    scene: { label: "使用场景", tone: "scene" },
    trust: { label: "品质信任", tone: "trust" },
    close: { label: "自然收尾", tone: "close" },
  })[key] || { label: "购买推进", tone: "value" };
}

function previewDirectorOutline(preview) {
  return Array.isArray(preview?.director_review?.m2_outline) ? preview.director_review.m2_outline : [];
}

function previewDirectorActiveChapterId(scope = "smart", preview = getPreviewState(scope)) {
  if (!preview?.commercial_director_experiment) return "";
  const outline = previewDirectorOutline(preview);
  const validIds = new Set(outline.map(function (item) { return String(item?.chapter_id || "").trim(); }).filter(Boolean));
  (preview?.clips || []).forEach(function (clip) {
    const id = String(clip?.director_chapter_id || "").trim();
    if (id) validIds.add(id);
  });
  let id = String(state.previewDirectorChapterFocus?.[scope] || "").trim();
  if (id && validIds.has(id)) return id;
  const selected = previewWorkbenchSelectedClips(scope, preview);
  const currentIndex = Number(state.previewDetailSelection?.[scope]);
  const current = selected.find(function (clip) { return Number(clip?.index) === currentIndex; }) || selected[0];
  id = String(current?.director_chapter_id || outline[0]?.chapter_id || "").trim();
  if (!state.previewDirectorChapterFocus) state.previewDirectorChapterFocus = { smart: "", mix: "" };
  state.previewDirectorChapterFocus[scope] = id;
  return id;
}

function previewDirectorCandidateView(scope = "smart", preview = getPreviewState(scope)) {
  if (!preview?.commercial_director_experiment) return "";
  const value = String(state.previewDirectorCandidateViews?.[scope] || "recommended");
  return ["recommended", "chapter", "all"].includes(value) ? value : "recommended";
}

function previewWorkbenchSelectedClips(scope, preview) {
  const byIndex = new Map((preview?.clips || []).map((clip) => [Number(clip.index), clip]));
  return previewAssemblyOrder(scope, preview).map((index) => byIndex.get(index)).filter(Boolean);
}

function previewWorkbenchCurrentClip(scope, preview, selected = previewWorkbenchSelectedClips(scope, preview)) {
  const rawIndex = state.previewCandidateSelections?.[scope];
  const candidateIndex = rawIndex === null || rawIndex === undefined ? NaN : Number(rawIndex);
  const candidate = (preview?.clips || []).find((clip) => Number(clip.index) === candidateIndex);
  if (candidate) return { clip: candidate, inspectOnly: !isPreviewWorkbenchSelected(candidate) };
  const activeIndex = previewDetailIndex(scope, selected);
  const active = selected.find((clip) => Number(clip.index) === Number(activeIndex));
  if (active) return { clip: active, inspectOnly: false };
  const fallback = preview?.clips?.[0] || null;
  return { clip: fallback, inspectOnly: Boolean(fallback && !isPreviewWorkbenchSelected(fallback)) };
}

function setPreviewDetailSelection(scope = "smart", index) {
  const preview = getPreviewState(scope);
  const clip = preview?.clips?.find((item) => Number(item.index) === Number(index));
  if (!clip || !isPreviewWorkbenchSelected(clip)) return;
  syncPreviewClipSelections(scope);
  state.previewCandidateSelections[scope] = null;
  state.previewDetailSelection[scope] = Number(index);
  renderPreviewStateKeepStoryScroll(scope);
  ensureInlinePreviewVideo(scope, Number(index));
}

function inspectPreviewWorkbenchClip(index, scope = "smart") {
  setPreviewDetailSelection(scope, Number(index));
}

function selectPreviewWorkbenchCandidate(index, scope = "smart") {
  const preview = getPreviewState(scope);
  const clip = preview?.clips?.find((item) => Number(item.index) === Number(index));
  if (!clip) return;
  if (isPreviewWorkbenchSelected(clip)) return setPreviewDetailSelection(scope, Number(index));
  state.previewCandidateSelections[scope] = Number(index);
  renderPreviewStateKeepStoryScroll(scope);
  ensureInlinePreviewVideo(scope, Number(index), { inspectOnly: true });
}

function insertPreviewWorkbenchCandidate(index, scope = "smart", targetIndex = null, placeAfter = false) {
  const preview = getPreviewState(scope);
  const clip = preview?.clips?.find((item) => Number(item.index) === Number(index));
  if (!clip) return;
  if (isPreviewWorkbenchSelected(clip)) return setPreviewDetailSelection(scope, Number(index));
  syncPreviewClipSelections(scope);
  const clipIndex = Number(index);
  const order = previewAssemblyOrder(scope, preview).filter((item) => Number(item) !== clipIndex);
  let insertAt = order.length;
  if (targetIndex !== null && targetIndex !== undefined) {
    const targetAt = order.indexOf(Number(targetIndex));
    if (targetAt >= 0) insertAt = targetAt + (placeAfter ? 1 : 0);
  }
  clip.selected = true;
  previewSegments(clip).forEach((segment) => { segment.selected = segment.selection_locked !== true; resetPreviewSegmentWords(segment); });
  order.splice(insertAt, 0, clipIndex);
  state.previewAssemblyOrders[previewAssemblyOrderKey(scope, preview)] = order;
  state.previewCandidateSelections[scope] = null;
  state.previewDetailSelection[scope] = clipIndex;
  commitPreviewDraft(scope);
  renderPreviewStateKeepStoryScroll(scope);
  ensureInlinePreviewVideo(scope, clipIndex);
}

function addPreviewWorkbenchCandidate(index, scope = "smart") {
  insertPreviewWorkbenchCandidate(index, scope);
}

function previewCurrentWorkbenchClip(scope = "smart") {
  const preview = getPreviewState(scope);
  const current = previewWorkbenchCurrentClip(scope, preview);
  if (current.clip) ensureInlinePreviewVideo(scope, Number(current.clip.index), { inspectOnly: current.inspectOnly });
}

function renderPreviewCandidateGroups(scope, preview) {
  const activeCandidate = state.previewCandidateSelections?.[scope];
  const hasActiveCandidate = activeCandidate !== null && activeCandidate !== undefined;
  return directPreviewWorkbenchCandidateCategories.map(([key, label]) => {
    const clips = (preview?.clips || []).filter((clip) => previewWorkbenchCandidateCategory(clip, scope) === key);
    if (!clips.length) return "";
    const rows = clips.map((clip) => {
      const selected = isPreviewWorkbenchSelected(clip);
      const active = Number(activeCandidate) === Number(clip.index) || (!hasActiveCandidate && Number(state.previewDetailSelection?.[scope]) === Number(clip.index));
      const text = String(clip.text || selectedPreviewText(clip) || "\u672a\u8bc6\u522b\u53e3\u64ad").trim();
      return `<article class="preview-candidate-row ${active ? "is-active" : ""} ${selected ? "is-selected" : ""}"><button class="preview-candidate-main" data-action="preview-workbench-select-candidate" data-preview-scope="${scope}" data-preview-index="${Number(clip.index)}" title="${escapeHtml(text)}"><span>${escapeHtml(text)}</span></button>${selected ? `<button class="preview-candidate-add is-added" data-action="preview-workbench-inspect-clip" data-preview-scope="${scope}" data-preview-index="${Number(clip.index)}">\u5df2\u9009</button>` : `<button class="preview-candidate-add" data-action="preview-workbench-add-candidate" data-preview-scope="${scope}" data-preview-index="${Number(clip.index)}">\u52a0\u5165</button>`}</article>`;
    }).join("");
    return `<section class="preview-candidate-group" data-preview-candidate-group="${key}"><div class="preview-candidate-group-head"><strong>${label}</strong><span>${clips.length}</span></div>${rows}</section>`;
  }).join("") || `<div class="preview-sequence-empty"><strong>\u6ca1\u6709\u53ef\u7528\u5019\u9009</strong><span>\u91cd\u65b0\u751f\u6210 AI \u9009\u7247\u9884\u89c8\u540e\u518d\u8bd5\u3002</span></div>`;
}

function previewDirectorChapterStageLabel(clip, groupIndex = 0) {
  if (groupIndex === 0) return "开场";
  const kind = String(clip?.director_chapter_kind || clip?.director_beat_function || "").trim().toLowerCase();
  return ({
    result: "结果兑现",
    mechanism: "为什么有效",
    proof: "证据补强",
    fit: "身材适配",
    comfort: "穿着体验",
    risk: "顾虑解除",
    risk_remove: "顾虑解除",
    styling: "日常搭配",
    scene: "使用场景",
    trust: "品质信任",
    close: "自然收尾",
  })[kind] || "购买推进";
}

function renderPreviewSelectedRow(scope, clip, position, activeIndex, options = {}) {
  const text = selectedPreviewText(clip) || String(clip.text || "");
  const role = previewDirectorBeatRoleMeta(clip);
  const label = options.director ? role.label : previewWorkbenchCategoryLabel(clip, scope);
  return '<article class="preview-selected-row ' + (options.director ? 'is-director ' : '') + (Number(clip.index) === activeIndex ? 'is-active' : '') + '" data-preview-row data-preview-scope="' + scope + '" data-preview-index="' + Number(clip.index) + '"><div class="clip-drag-handle" data-preview-drag-handle data-preview-scope="' + scope + '" title="按住拖拽调整顺序" aria-label="按住拖拽调整顺序">拖</div><button class="preview-selected-main" data-action="preview-workbench-inspect-clip" data-preview-scope="' + scope + '" data-preview-index="' + Number(clip.index) + '"><span><em>' + (position + 1) + '</em><strong class="' + (options.director ? 'is-' + escapeHtml(role.tone) : '') + '">' + escapeHtml(label) + '</strong><i>' + effectiveClipDuration(clip).toFixed(1) + 's</i></span><small>' + escapeHtml(text) + '</small></button><button type="button" class="preview-selected-remove" title="移出已选" aria-label="移出已选" data-action="preview-assembly-remove" data-preview-scope="' + scope + '" data-preview-index="' + Number(clip.index) + '">移除</button></article>';
}

function renderPreviewSelectedRows(scope, selected) {
  if (!selected.length) return `<div class="preview-sequence-empty"><strong>\u62d6\u5019\u9009\u53e5\u5230\u8fd9\u91cc</strong><span>\u4e5f\u53ef\u5728\u5de6\u4fa7\u70b9\u51fb\u201c\u52a0\u5165\u201d\uff0c\u5168\u7a0b\u4e0d\u8c03\u7528 AI\u3002</span></div>`;
  const activeIndex = Number(state.previewDetailSelection?.[scope]);
  const preview = getPreviewState(scope);
  if (!preview?.commercial_director_experiment) {
    return selected.map(function (clip, position) { return renderPreviewSelectedRow(scope, clip, position, activeIndex); }).join("");
  }
  const groups = [];
  let elapsed = 0;
  selected.forEach(function (clip, position) {
    const chapterId = String(clip?.director_chapter_id || `chapter-${position + 1}`).trim();
    let group = groups[groups.length - 1];
    if (!group || group.chapterId !== chapterId) {
      group = { chapterId, start: elapsed, end: elapsed, first: clip, items: [] };
      groups.push(group);
    }
    group.items.push({ clip, position });
    elapsed += effectiveClipDuration(clip);
    group.end = elapsed;
  });
  return groups.map(function (group, groupIndex) {
    const title = String(group.first?.director_chapter_title || "当前购买章节").trim();
    const stage = previewDirectorChapterStageLabel(group.first, groupIndex);
    const rows = group.items.map(function (item) { return renderPreviewSelectedRow(scope, item.clip, item.position, activeIndex, { director: true }); }).join("");
    return '<section class="preview-story-chapter" data-preview-selected-chapter="' + escapeHtml(group.chapterId) + '"><button type="button" class="preview-story-chapter-head" data-action="preview-director-chapter-focus" data-preview-scope="' + scope + '" data-chapter-id="' + escapeHtml(group.chapterId) + '"><strong>' + escapeHtml(stage) + '：' + escapeHtml(title) + '</strong><span>' + group.start.toFixed(0) + '–' + group.end.toFixed(0) + 's</span></button>' + rows + '</section>';
  }).join("");
}

// [AI_WORKBENCH_LIBRARY_END]
function renderPreviewWorkbenchVideoStage(scope, preview, current) {
  const clip = current?.clip;
  if (!clip) {
    return '<section class="preview-workbench-video"><div class="preview-sequence-empty"><strong>\u70b9\u51fb\u5de6\u4fa7\u5019\u9009\u7247\u6bb5\u5f00\u59cb\u9009\u7247</strong><span>\u89c6\u9891\u4f1a\u5728\u8fd9\u91cc\u5c55\u793a\u3002</span></div></section>';
  }
  const selected = isPreviewWorkbenchSelected(clip);
  const addButton = selected ? '' : '<button class="button button-secondary button-small" data-action="preview-workbench-add-candidate" data-preview-scope="' + scope + '" data-preview-index="' + Number(clip.index) + '">\u52a0\u5165\u5df2\u9009</button>';
  return '<section class="preview-workbench-video"><div class="preview-workbench-column-head"><div><strong>' + (selected ? '\u5f53\u524d\u5df2\u9009\u7247\u6bb5' : '\u5f53\u524d\u5019\u9009\u7247\u6bb5') + '</strong><span>' + escapeHtml(previewWorkbenchCategoryLabel(clip, scope)) + '</span></div><div class="preview-video-actions"><button class="button button-muted button-small" data-action="preview-workbench-preview-current" data-preview-scope="' + scope + '">\u9884\u89c8\u89c6\u9891</button>' + addButton + '</div></div>' + renderPreviewInlineVideo(scope, preview, clip, { inspectOnly: current.inspectOnly, idleText: current.inspectOnly ? '\u70b9\u51fb\u201c\u9884\u89c8\u89c6\u9891\u201d\u67e5\u770b\u5019\u9009\u753b\u9762\u3002' : '\u4fee\u6539\u540e\u70b9\u51fb\u201c\u9884\u89c8\u89c6\u9891\u201d\u540c\u6b65\u56de\u770b\u3002' }) + '</section>';
}

function renderPreviewEditorSentence(scope, clip, segment, position) {
  const locked = segment?.selection_locked === true;
  const selected = isPreviewSegmentSelected(segment);
  const words = previewSegmentWords(segment);
  const reason = String(segment?.blocked_reason || segment?.auto_unselected_reason || '').trim();
  const wordRows = words.length ? words.map((word) => {
    const text = escapeHtml(String(word?.text || ''));
    if (isPreviewWordLocked(word)) {
      return '<span class="preview-word is-locked" title="' + escapeHtml(String(word?.blocked_reason || '\u8fdd\u7981\u8bcd\u4e0d\u53ef\u6062\u590d')) + '">' + text + '</span>';
    }
    const deleted = word?.selected === false;
    return '<button type="button" class="preview-word ' + (deleted ? 'is-deleted' : '') + '" data-action="preview-word-toggle" data-preview-scope="' + scope + '" data-preview-clip="' + Number(clip.index) + '" data-preview-segment="' + Number(segment.index) + '" data-preview-word="' + Number(word.index) + '" title="' + (deleted ? '\u70b9\u51fb\u6062\u590d\u8fd9\u4e2a\u8bcd' : '\u70b9\u51fb\u5220\u9664\u8fd9\u4e2a\u8bcd') + '">' + text + '</button>';
  }).join('') : '<span class="preview-word is-static">' + escapeHtml(String(segment?.text || '\u672a\u8bc6\u522b\u53e5\u5b50')) + '</span>';
  return '<article class="preview-editor-sentence ' + (!selected ? 'is-deleted' : '') + ' ' + (locked ? 'is-locked' : '') + '"><div class="preview-editor-sentence-head"><label><input type="checkbox" data-preview-segment data-preview-scope="' + scope + '" data-preview-segment-parent="' + Number(clip.index) + '" data-preview-segment-index="' + Number(segment.index) + '" ' + (selected ? 'checked' : '') + ' ' + (locked ? 'disabled' : '') + '><strong>\u7b2c ' + (position + 1) + ' \u53e5</strong></label><span>' + (locked ? '\u98ce\u9669\u53e5\u4e0d\u53ef\u9009' : (selected ? '\u5df2\u4fdd\u7559' : '\u5df2\u5220\u9664')) + '</span></div><div class="preview-editor-words">' + wordRows + '</div>' + (reason ? '<small class="preview-editor-lock-reason">' + escapeHtml(reason) + '</small>' : '') + '</article>';
}

function renderPreviewSentenceEditor(scope, current) {
  const clip = current?.clip;
  if (!clip) {
    return '<section class="preview-sentence-editor"><div class="preview-workbench-column-head"><div><strong>\u9009\u53e5 / \u5220\u8bcd</strong><span>\u70b9\u51fb\u5df2\u9009\u7247\u6bb5\u540e\u7f16\u8f91</span></div></div><div class="preview-sequence-empty"><strong>\u8fd8\u6ca1\u6709\u5f53\u524d\u7247\u6bb5</strong></div></section>';
  }
  if (!isPreviewWorkbenchSelected(clip)) {
    return '<section class="preview-sentence-editor"><div class="preview-workbench-column-head"><div><strong>\u9009\u53e5 / \u5220\u8bcd</strong><span>\u52a0\u5165\u5df2\u9009\u540e\u624d\u53ef\u7cbe\u4fee</span></div></div><div class="preview-sequence-empty"><strong>\u5148\u786e\u8ba4\u8fd9\u6bb5\u89c6\u9891\u518d\u70b9\u201c\u52a0\u5165\u5df2\u9009\u201d</strong><span>\u5df2\u9009\u7247\u6bb5\u4f1a\u5728\u8fd9\u91cc\u663e\u793a\u5168\u90e8\u53e5\u5b50\u548c\u53ef\u5220\u8bcd\u3002</span></div></section>';
  }
  const segments = previewSegments(clip);
  const body = segments.length ? segments.map((segment, position) => renderPreviewEditorSentence(scope, clip, segment, position)).join('') : '<article class="preview-editor-sentence"><div class="preview-editor-words"><span class="preview-word is-static">' + escapeHtml(String(clip.text || '\u672a\u8bc6\u522b\u53e3\u64ad')) + '</span></div></article>';
  return '<section class="preview-sentence-editor"><div class="preview-workbench-column-head"><div><strong>\u9009\u53e5 / \u5220\u8bcd</strong><span>\u52fe\u9009\u4fdd\u7559\u53e5\u5b50\uff0c\u70b9\u8bcd\u5373\u53ef\u5220\u9664\uff1b\u9501\u5b9a\u8bcd\u65e0\u6cd5\u6062\u590d</span></div><small>' + escapeHtml(previewWorkbenchCategoryLabel(clip)) + '</small></div><div class="preview-editor-sentence-list">' + body + '</div></section>';
}

function renderPreviewWorkbench(scope, preview, targetId) {
  preview = hydratePreviewCandidatePool(preview);
  ensurePreviewDraft(scope);
  const analysis = analyzeSmartPreview(preview, targetId);
  const selected = previewWorkbenchSelectedClips(scope, preview);
  const current = previewWorkbenchCurrentClip(scope, preview, selected);
  const totalDuration = selected.reduce((sum, clip) => sum + effectiveClipDuration(clip), 0);
  return '<div data-preview-summary="' + scope + '">' + renderPreviewSummary(analysis) + '</div><div class="preview-workbench-toolbar"><span>\u5de6\u4fa7\u770b\u5019\u9009\u5e76\u52a0\u5165\uff0c\u53f3\u4fa7\u62d6\u62fd\u8c03\u6574\u6700\u7ec8\u987a\u5e8f\uff0c\u4e2d\u95f4\u76f4\u63a5\u9009\u53e5\u3001\u5220\u8bcd\u3002</span></div><div class="preview-selection-workbench preview-workbench-unified" data-preview-workbench="' + scope + '" data-preview-workbench-focus="' + scope + '" tabindex="0"><aside class="preview-candidate-sidebar"><div class="preview-workbench-column-head"><div><strong>\u5019\u9009\u7247\u6bb5</strong><span>\u6309\u5185\u5bb9\u5f52\u7c7b\uff0c\u4e0d\u6253\u4e71\u539f\u5019\u9009\u987a\u5e8f</span></div><small>' + (preview?.clips?.length || 0) + ' \u6bb5</small></div><div class="preview-candidate-list">' + renderPreviewCandidateGroups(scope, preview) + '</div></aside><main class="preview-workbench-main">' + renderPreviewWorkbenchVideoStage(scope, preview, current) + renderPreviewSentenceEditor(scope, current) + '</main><aside class="preview-selected-sidebar"><div class="preview-workbench-column-head"><div><strong>\u5df2\u9009\u7247\u6bb5</strong><span>\u62d6\u62fd\u6392\u5e8f\uff0c\u70b9\u51fb\u5373\u7f16\u8f91\u5168\u90e8\u5185\u5bb9</span></div><small>' + selected.length + ' \u6bb5 \u00b7 ' + totalDuration.toFixed(1) + 's</small></div><div class="preview-selected-list">' + renderPreviewSelectedRows(scope, selected) + '</div></aside></div>';
}

function bindPreviewWorkbenchKeyboard() {
  // The direct workbench keeps every operation visible; it has no hidden stage shortcut.
}

function previewStoryScrollTop(scope = "smart") {
  return previewBox(scope)?.querySelector('.preview-selected-list, .preview-sequence-scroll')?.scrollTop || 0;
}

function renderPreviewStateKeepStoryScroll(scope = "smart") {
  const box = previewBox(scope);
  const candidateScrollTop = box?.querySelector('.preview-candidate-list')?.scrollTop || 0;
  const storyScrollTop = previewStoryScrollTop(scope);
  renderPreviewState(scope);
  const refreshed = previewBox(scope);
  const candidateList = refreshed?.querySelector('.preview-candidate-list');
  const storyList = refreshed?.querySelector('.preview-selected-list, .preview-sequence-scroll');
  if (candidateList) candidateList.scrollTop = candidateScrollTop;
  if (storyList) storyList.scrollTop = storyScrollTop;
}

function bindDirectPreviewWorkbenchActions() {
  document.body.addEventListener('click', (event) => {
    const target = event.target?.closest?.('[data-action]');
    if (!target || target.disabled) return;
    const scope = target.dataset.previewScope || 'smart';
    const action = target.dataset.action;
    if (action === 'preview-workbench-select-candidate') {
      event.preventDefault();
      selectPreviewWorkbenchCandidate(Number(target.dataset.previewIndex), scope);
    } else if (action === 'preview-candidate-source-filter') {
      event.preventDefault();
      setPreviewCandidateSourceFilter(scope, target.dataset.value || 'recommended');
    } else if (action === 'preview-candidate-category-filter') {
      event.preventDefault();
      setPreviewCandidateCategoryFilter(scope, target.dataset.value || 'all');
    } else if (action === 'preview-workbench-add-candidate') {
      event.preventDefault();
      addPreviewWorkbenchCandidate(Number(target.dataset.previewIndex), scope);
    } else if (action === 'preview-workbench-preview-current') {
      event.preventDefault();
      previewCurrentWorkbenchClip(scope);
    } else if (action === 'preview-inline-retry') {
      event.preventDefault();
      ensureInlinePreviewVideo(scope, Number(target.dataset.previewIndex), {
        inspectOnly: target.dataset.previewInspectOnly === 'true',
        force: true,
      });
    } else if (action === 'preview-word-audition') {
      event.preventDefault();
      auditionPreviewWordEdit(scope).catch(function () {});
    } else if (action === 'preview-word-undo') {
      event.preventDefault();
      undoPreviewWordEdit(scope);
    } else if (action === 'preview-word-toggle') {
      event.preventDefault();
      togglePreviewWordSelection(Number(target.dataset.previewClip), Number(target.dataset.previewSegment), Number(target.dataset.previewWord), scope);
    } else if (action === 'preview-word-group-toggle') {
      event.preventDefault();
      const info = previewWordButtonInfo(target);
      if (shouldSkipPreviewWordClick(info)) return;
      if (event.shiftKey && applyPreviewWordRangeFromAnchor(info)) return;
      rememberPreviewWordRangeAnchor(info);
      togglePreviewWordGroupSelection(Number(target.dataset.previewClip), Number(target.dataset.previewSegment), target.dataset.previewWordGroup || '', scope);
    }
  });

  document.body.addEventListener('pointerdown', (event) => {
    const info = previewWordButtonInfo(event.target);
    if (info) beginPreviewWordRangeGesture(info, event);
  });

  document.body.addEventListener('pointermove', (event) => {
    if (!state.previewWordRangeGesture) return;
    updatePreviewWordRangeGesture(event);
    if (state.previewWordRangeGesture?.dragging) event.preventDefault();
  });

  document.body.addEventListener('pointerup', (event) => {
    if (finishPreviewWordRangeGesture(event)) event.preventDefault();
  });

  document.body.addEventListener('pointercancel', () => clearPreviewWordRangeGesture());

  document.body.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      const openScope = ['smart', 'mix'].find(function (scope) { return state.previewDirectorAlternativesOpen?.[scope]; });
      if (openScope) {
        event.preventDefault();
        togglePreviewDirectorAlternatives(openScope, false);
        return;
      }
    }
    const insideWorkbench = event.target?.closest?.('[data-preview-workbench]');
    const isUndo = (event.ctrlKey || event.metaKey) && String(event.key || '').toLowerCase() === 'z';
    if (!insideWorkbench || !isUndo || event.target?.closest?.('input, textarea, select, [contenteditable="true"]')) return;
    const scope = insideWorkbench.dataset.previewWorkbench || 'smart';
    if (!previewWordEditHistory(scope).length) return;
    event.preventDefault();
    undoPreviewWordEdit(scope);
  });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bindDirectPreviewWorkbenchActions, { once: true });
else bindDirectPreviewWorkbenchActions();

async function startSmartFromPreview() {
  await saveFeaturePreferences();
  if (!state.smartPreview?.id || state.smartPreview.status !== 'ready') {
    toast('\u8bf7\u5148\u751f\u6210 AI \u9009\u7247\u9884\u89c8', 'warning');
    return;
  }
  syncPreviewClipSelections();
  const selection = collectPreviewSelection('smart');
  if (!selection.selectedIndices.length) {
    toast('\u8bf7\u81f3\u5c11\u4fdd\u7559\u4e00\u4e2a\u7247\u6bb5', 'warning');
    return;
  }
  const payload = {
    ...collectSmartPayload({ requireVideos: false }),
    preview_id: state.smartPreview.id,
    selected_indices: selection.selectedIndices,
    selected_keys: selection.selectedKeys,
    order: selection.order,
    order_keys: selection.orderKeys,
    selected_segments: selection.selectedSegments,
    selected_words: selection.selectedWords,
    selected_segments_by_key: selection.selectedSegmentsByKey,
    selected_words_by_key: selection.selectedWordsByKey,
  };
  await runPreflight('smart-from-preview', payload, 'smart-cut');
  const result = await api('/api/smart-cut/from-preview/start', { method: 'POST', body: JSON.stringify(payload) });
  toast(result.message || '\u9884\u89c8\u6210\u7247\u4efb\u52a1\u5df2\u542f\u52a8', 'success');
  refreshTasks();
}

async function startMixFromPreview() {
  await saveFeaturePreferences();
  if (!state.mixPreview?.id || state.mixPreview.status !== 'ready') {
    toast('\u8bf7\u5148\u751f\u6210\u6df7\u526a AI \u9009\u7247\u9884\u89c8', 'warning');
    return;
  }
  syncPreviewClipSelections('mix');
  const selection = collectPreviewSelection('mix');
  if (!selection.selectedIndices.length) {
    toast('\u8bf7\u81f3\u5c11\u4fdd\u7559\u4e00\u4e2a\u7247\u6bb5', 'warning');
    return;
  }
  const payload = {
    ...collectFeaturePayload('mix'),
    preview_id: state.mixPreview.id,
    selected_indices: selection.selectedIndices,
    selected_keys: selection.selectedKeys,
    order: selection.order,
    order_keys: selection.orderKeys,
    selected_segments: selection.selectedSegments,
    selected_words: selection.selectedWords,
    selected_segments_by_key: selection.selectedSegmentsByKey,
    selected_words_by_key: selection.selectedWordsByKey,
  };
  await runPreflight('mix-from-preview', payload, 'mix');
  const result = await api('/api/mix/from-preview/start', { method: 'POST', body: JSON.stringify(payload) });
  toast(result.message || '\u9884\u89c8\u6df7\u526a\u4efb\u52a1\u5df2\u542f\u52a8', 'success');
  refreshTasks();
}

async function previewClipVideo(index, scope = 'smart') {
  const preview = getPreviewState(scope);
  if (!preview?.id || preview.status !== 'ready') {
    toast('\u8bf7\u5148\u751f\u6210 AI \u9009\u7247\u9884\u89c8', 'warning');
    return;
  }
  syncPreviewClipSelections(scope);
  const clip = preview.clips?.find((item) => Number(item.index) === Number(index));
  if (!clip) {
    toast('\u7247\u6bb5\u4e0d\u5b58\u5728\uff0c\u8bf7\u91cd\u65b0\u751f\u6210\u9884\u89c8', 'warning');
    return;
  }
  const inspectOnly = !isPreviewWorkbenchSelected(clip);
  const segments = previewSegments(clip);
  if (!inspectOnly && segments.length && !selectedPreviewSegments(clip).length) {
    toast('\u8bf7\u4fdd\u7559\u81f3\u5c11\u4e00\u53e5\u5185\u5bb9\u540e\u518d\u9884\u89c8', 'warning');
    return;
  }
  const draft = inspectOnly ? null : commitPreviewDraft(scope, { remote: true });
  const bounds = effectiveClipBounds(clip);
  const modal = ensurePreviewModal();
  const video = modal.querySelector('#preview-modal-video');
  const title = modal.querySelector('#preview-modal-title');
  const status = modal.querySelector('#preview-modal-status');
  if (!video) return;
  if (title) title.textContent = (inspectOnly ? '\u5019\u9009\u8bd5\u770b' : '\u7247\u6bb5\u9884\u89c8') + ' ' + formatSeconds(bounds.start) + '-' + formatSeconds(bounds.end);
  if (status) {
    status.textContent = '\u6b63\u5728\u751f\u6210\u7247\u6bb5\u9884\u89c8...';
    status.classList.remove('is-hidden', 'is-error');
  }
  modal.classList.remove('is-hidden');
  modal.setAttribute('aria-hidden', 'false');
  video.pause();
  video.removeAttribute('src');
  video.load();
  const endpoint = scope === 'mix' ? '/api/mix/preview/clip-video' : '/api/smart-cut/preview/clip-video';
  try {
    const result = await api(endpoint, {
      method: 'POST',
      body: JSON.stringify(inspectOnly ? {
        preview_id: preview.id,
        clip_index: Number(index),
        scope,
        inspect_only: true,
      } : {
        preview_id: preview.id,
        clip_index: Number(index),
        scope,
        selected_indices: draft.selected_indices || [],
        selected_keys: draft.selected_keys || [],
        order: draft.order || [],
        order_keys: draft.order_keys || [],
        selected_segments: draft.selected_segments || {},
        selected_words: draft.selected_words || {},
        selected_segments_by_key: draft.selected_segments_by_key || {},
        selected_words_by_key: draft.selected_words_by_key || {},
        updated_at: draft.updated_at || Date.now(),
      }),
    });
    video.src = result.url;
    video.load();
    if (status) status.classList.add('is-hidden');
  } catch (error) {
    if (status) {
      status.textContent = error.message || String(error || '\u9884\u89c8\u751f\u6210\u5931\u8d25');
      status.classList.remove('is-hidden');
      status.classList.add('is-error');
    }
  }
}
function renderPreviewEditorSentence(scope, clip, segment, position) {
  const locked = segment?.selection_locked === true;
  const selected = isPreviewSegmentSelected(segment);
  const words = previewSegmentWords(segment);
  const wordTimed = segment?.word_timed === true && words.length > 0;
  const reason = String(segment?.blocked_reason || segment?.auto_unselected_reason || '').trim();
  let wordHint = '';
  let wordRows = '';
  if (!wordTimed) {
    const sentenceText = String(segment?.text || words.map((word) => String(word?.text || '')).join('') || '\u672a\u8bc6\u522b\u53e5\u5b50');
    wordRows = '<span class="preview-word is-static">' + escapeHtml(sentenceText) + '</span>';
    if (!locked) wordHint = '<small class="preview-editor-word-hint">\u6682\u65e0\u9010\u8bcd\u65f6\u95f4\uff0c\u53ef\u4ee5\u6574\u53e5\u5220\u9664</small>';
  } else {
    wordRows = words.map((word) => {
      const text = escapeHtml(String(word?.text || ''));
      const wordLocked = locked || isPreviewWordLocked(word);
      if (wordLocked) {
        const blockedReason = locked ? (reason || '\u98ce\u9669\u53e5\u4e0d\u53ef\u9009') : String(word?.blocked_reason || '\u8fdd\u7981\u8bcd\u4e0d\u53ef\u6062\u590d');
        return '<span class="preview-word is-locked" title="' + escapeHtml(blockedReason) + '">' + text + '</span>';
      }
      const deleted = word?.selected === false;
      return '<button type="button" class="preview-word ' + (deleted ? 'is-deleted' : '') + '" data-action="preview-word-toggle" data-preview-scope="' + scope + '" data-preview-clip="' + Number(clip.index) + '" data-preview-segment="' + Number(segment.index) + '" data-preview-word="' + Number(word.index) + '" title="' + (deleted ? '\u70b9\u51fb\u6062\u590d\u8fd9\u4e2a\u8bcd' : '\u70b9\u51fb\u5220\u9664\u8fd9\u4e2a\u8bcd') + '">' + text + '</button>';
    }).join('');
  }
  return '<article class="preview-editor-sentence ' + (!selected ? 'is-deleted' : '') + ' ' + (locked ? 'is-locked' : '') + '"><div class="preview-editor-sentence-head"><label><input type="checkbox" data-preview-segment data-preview-scope="' + scope + '" data-preview-segment-parent="' + Number(clip.index) + '" data-preview-segment-index="' + Number(segment.index) + '" ' + (selected ? 'checked' : '') + ' ' + (locked ? 'disabled' : '') + '><strong>\u7b2c ' + (position + 1) + ' \u53e5</strong></label><span>' + (locked ? '\u98ce\u9669\u53e5\u4e0d\u53ef\u9009' : (selected ? '\u5df2\u4fdd\u7559' : '\u5df2\u5220\u9664')) + '</span></div><div class="preview-editor-words">' + wordRows + '</div>' + wordHint + (reason ? '<small class="preview-editor-lock-reason">' + escapeHtml(reason) + '</small>' : '') + '</article>';
}

// [AI_WORKBENCH_DIRECT_REFINEMENT]
// Keep candidate categories focused on the decision a user needs to make.  In
// particular, live-chat and low-confidence fragments must not flood the main
// product-selling category.
function previewWorkbenchCategoryDomain(scope = "smart", clip = null) {
  const prefix = scope === "mix" ? "mix" : "sc";
  const primary = primaryCategoryValue(prefix);
  const evidence = `${primary} ${clip?.focus || ""} ${clip?.focus_block || ""} ${clip?.text || ""}`;
  if (/\u751f\u9c9c|\u98df\u54c1|\u996e\u6599/.test(primary)) return "food";
  if (/\u670d\u9970|\u5185\u8863|\u978b|\u7bb1\u5305|\u914d\u9970|\u73e0\u5b9d/.test(primary)) return "apparel";
  if (/\u9762\u6599|\u7248\u578b|\u663e\u7626|\u906e\u8089|\u6536\u8170|\u8863\u670d|\u88d9\u5b50|\u886c\u886b|\u88e4\u5b50|\u62c9\u94fe|\u9886\u53e3|\u8896\u53e3/.test(evidence)) return "apparel";
  if (/\u73b0\u6458|\u73b0\u635e|\u51b7\u94fe|\u679c\u5f84|\u51c0\u542b\u91cf|\u53e3\u611f|\u597d\u5403|\u9c9c\u6d3b|\u4ea7\u5730/.test(evidence)) return "food";
  return "general";
}

function previewWorkbenchPreferenceCategory(text, domain = "general") {
  const allowApparel = domain !== "food";
  const allowFood = domain !== "apparel";
  const hasQualityDetail = /\u54c1\u8d28\u7ec6\u8282|\u54c1\u8d28|\u8d28\u91cf|\u505a\u5de5|\u5de5\u827a|\u7ec6\u8282|\u8d70\u7ebf|\u62c9\u94fe|\u6263\u5b50|\u7ebd\u6263|\u9886\u53e3|\u53e3\u888b|\u91cc\u886c|\u4e94\u91d1|\u8d28\u68c0|\u7cbe\u81f4/.test(text);
  if (allowApparel && /\u9762\u6599|\u6750\u8d28|\u8d28\u611f|\u624b\u611f|\u900f\u6c14|\u4eb2\u80a4|\u67d4\u8f6f|\u5782\u611f|\u51b0\u4e1d|\u9488\u7ec7|\u68c9|\u9ebb/.test(text)) return "pref_fabric";
  if (domain !== "food" && hasQualityDetail) return "pref_quality";
  if (/\u7248\u578b|\u663e\u7626|\u906e\u8089|\u663e\u9ad8|\u6536\u8170|\u8170\u7ebf|\u817f\u957f|\u80a9|\u80ef|\u8eab\u6750|\u5bbd\u677e|\u4fee\u8eab|\u7a7f\u7740\u4f53\u9a8c|\u5305\u5bb9/.test(text)) return "pref_fit";
  if (/\u989c\u8272|\u6c1b\u56f4|\u663e\u767d|\u663e\u6c14\u8272|\u629ac\u8272|\u56fe\u6848|\u5370\u82b1|\u590d\u53e4|\u98ce\u683c|\u5143\u7d20/.test(text)) return "pref_color";
  if (/\u573a\u666f|\u642d\u914d|\u7a7f\u642d|\u901a\u52e4|\u7ea6\u4f1a|\u65e5\u5e38|\u51fa\u95e8|\u4e0a\u73ed|\u62cd\u7167|\u53e0\u7a7f|\u5185\u642d|\u5916\u642d/.test(text)) return "pref_scene";
  if (/\u60c5\u7eea|\u611f\u67d3|\u6c1b\u56f4\u611f|\u9ad8\u7ea7|\u6e29\u67d4|\u8f7b\u719f|\u6c14\u8d28|\u597d\u770b|\u559c\u6b22|\u5c0f\u4f17/.test(text)) return "pref_emotion";
  if (/\u6027\u4ef7\u6bd4|\u5212\u7b97|\u503c|\u4ef7\u683c|\u798f\u5229|\u4f18\u60e0|\u4fbf\u5b9c|\u7701\u94b1/.test(text)) return "pref_value";
  if (/\u7d27\u8feb|\u7a00\u7f3a|\u9650\u91cf|\u5e93\u5b58|\u79d2\u6740|\u62a2|\u4e0a\u8f66|\u4e0b\u5355|\u6700\u540e|\u9519\u8fc7/.test(text)) return "pref_urgency";
  if (/\u6d41\u884c|\u8d8b\u52bf|\u7206\u6b3e|\u4eca\u5e74|\u5f53\u4e0b|\u65b0\u6b3e|\u4e0a\u65b0/.test(text)) return "pref_trend";
  if (allowFood && /\u53e3\u611f|\u98df\u6b32|\u597d\u5403|\u9999\u6c14|\u9165\u8106|\u8f6f\u7cef|\u9c9c\u751c|\u7206\u6c41|\u5165\u5473|\u89e3\u998b/.test(text)) return "food_taste";
  if (allowFood && /\u65b0\u9c9c|\u9c9c\u6d3b|\u73b0\u6458|\u73b0\u91c7|\u73b0\u6355|\u73b0\u635e|\u5f53\u5929\u53d1|\u9c9c\u5ea6|\u679c\u5f62|\u679c\u5f84|\u9971\u6ee1|\u574f\u679c/.test(text)) return "food_quality";
  if (allowFood && /\u4ea7\u5730|\u6eaf\u6e90|\u539f\u4ea7|\u519c\u573a|\u679c\u56ed|\u6d77\u57df|\u6e90\u5934/.test(text)) return "food_origin";
  if (allowFood && /\u89c4\u683c|\u5206\u91cf|\u51c0\u542b\u91cf|\u4e00\u7bb1|\u4e00\u888b|\u4e00\u65a4|\u5927\u679c|\u5c0f\u679c|\u4efd\u91cf/.test(text)) return "food_spec";
  if (allowFood && /\u53d1\u8d27|\u4fdd\u9c9c|\u51b7\u94fe|\u5305\u88c5|\u987a\u4e30|\u5230\u8d27|\u73b0\u53d1/.test(text)) return "food_fresh";
  if (allowFood && /\u5403\u6cd5|\u505a\u6cd5|\u706b\u9505|\u714e|\u716e|\u70e4|\u62cc|\u65e9\u9910|\u591c\u5bb5|\u9001\u793c|\u5bb6\u5ead/.test(text)) return "food_scene";
  if (hasQualityDetail) return "pref_quality";
  return "";
}

function previewWorkbenchCandidateCategory(clip, scope = "smart") {
  const directorChapterId = String(clip?.director_chapter_id || "").trim();
  if (directorChapterId) return `director:${directorChapterId}`;
  const role = previewWorkbenchRoleKey(clip);
  const text = (String(clip?.focus || "") + " " + String(clip?.focus_block || "") + " " + String(clip?.text || "")).toLowerCase();
  const spokenText = String(clip?.text || "").toLowerCase();
  const focus = String(clip?.focus_block || clip?.focus || "").toLowerCase();
  const explicitRole = String(clip?.sales_role || "").toLowerCase();
  const denseText = text.replace(/\s+/g, "");
  if (explicitRole === "weak_fragment") return "unclear";
  if (explicitRole === "hook" || explicitRole === "hook_followup") return "hook";
  if (explicitRole === "natural_close") return "close";
  const domain = previewWorkbenchCategoryDomain(scope, clip);
  const incompatibleFocus = (
    domain === "apparel"
    && /\u53e3\u611f\u98df\u6b32|\u65b0\u9c9c\u54c1\u8d28|\u4ea7\u5730\u6eaf\u6e90|\u89c4\u683c\u5206\u91cf|\u53d1\u8d27\u4fdd\u9c9c|\u573a\u666f\u5403\u6cd5/.test(focus)
  ) || (
    domain === "food"
    && /\u7248\u578b\u663e\u7626|\u9762\u6599\u8d28\u611f|\u7a7f\u7740\u4f53\u9a8c|\u54c1\u8d28\u7ec6\u8282|\u5c3a\u5bf8\u957f\u5ea6|\u989c\u8272\u6c1b\u56f4|\u573a\u666f\u642d\u914d/.test(focus)
  );
  const focusCategory = incompatibleFocus ? "" : previewWorkbenchPreferenceCategory(focus, domain);
  if (focusCategory) return focusCategory;
  if (role === "hook" || /\u5f00\u573a|\u7b2c\u4e00\u53e5|\u5148\u770b|\u59d0\u59b9\u4eec|\u5b9d\u5b9d\u4eec|\u6ce8\u610f/.test(text)) return "hook";
  if (role === "close" || /\u6536\u5c3e|\u6700\u540e|\u4e0d\u8981\u9519\u8fc7/.test(text)) return "close";
  if (/\u4e0b\u4e00\u4f4d|\u7c89\u4e1d|\u4e3e\u62a5|\u6295\u7968|\u76f4\u64ad\u95f4|\u4e0a\u8f66|\u4e0b\u5355|\u5ba2\u670d|\u94fe\u63a5|\u53d1\u8d27|\u552e\u540e|\u5e93\u5b58|\u79d2\u6740|\u8ba2\u5355|\u62a2|\u5f00\u6389|\u542c\u6b4c|\u97f3\u4e50|\u7a0d\u7b49|\d+\s*\u5355/.test(text)) return "live";
  const textCategory = previewWorkbenchPreferenceCategory(spokenText, domain);
  if (textCategory) return textCategory;
  if (role === "product" || /\u8863\u670d|\u88d9\u5b50|\u8fd9\u4e2a\u6b3e|\u8fd9\u4ef6|\u6b3e\u5f0f|\u8bbe\u8ba1|\u590f\u6b3e|\u4e0a\u65b0|\u5355\u54c1|\u7cbe\u81f4/.test(text)) return "pref_fabric";
  if ((role === "core" || role === "product") && denseText.length >= 18) return "pref_fabric";
  return "unclear";
}

function previewWorkbenchCandidateText(clip) {
  const selected = String(selectedPreviewText(clip) || "").trim();
  if (selected && selected !== "未选择句子") return selected;
  return String(clip?.text || "").trim();
}

function isPreviewWorkbenchHardWasteCandidate(clip) {
  const text = previewWorkbenchCandidateText(clip);
  const dense = text.replace(/[\s，。！？,.!?、：:；;"'“”‘’（）()【】\[\]-]/g, "");
  if (!dense) return true;
  if (/^(嗯+|啊+|哦+|噢+|呃+|额+|哈+|呀+|呢+|嘛+|啦+|好+|对+|是的|没错|可以|行+)$/.test(dense)) return true;
  if (/^(然后|而且|但是|所以|就是|这个|那个|好了|好吧|来吧|来看|接下来|下一位|稍等|等一下|听歌|音乐|为什么|能理解吗|知道吧|对不对|是不是)$/.test(dense)) return true;
  if (/\u76f4\u64ad\u95f4|\u5ba2\u670d|\u4e0a\u8f66|\u4e0b\u5355|\u8ba2\u5355|\u94fe\u63a5|\u5e93\u5b58|\u53d1\u8d27|\u552e\u540e|\u4e3e\u62a5|\u6295\u7968|\u542c\u6b4c|\u97f3\u4e50|\u4e0b\u4e00\u4f4d/.test(text)) return true;
  return false;
}

function previewWorkbenchCandidateQualityScore(clip, scope = "smart") {
  if (isPreviewWorkbenchHardWasteCandidate(clip)) return -100;
  const explicitRole = String(clip?.sales_role || "").toLowerCase();
  const text = previewWorkbenchCandidateText(clip);
  const dense = text.replace(/[\s，。！？,.!?、：:；;"'“”‘’（）()【】\[\]-]/g, "");
  const category = previewWorkbenchCandidateCategory(clip, scope);
  let score = 0;
  if (previewWorkbenchCandidateOrigin(clip) === "recommended") score += 8;
  if (clip?.selected !== false) score += 5;
  if (["hook", "close"].includes(category)) score += 5;
  if (category !== "live" && category !== "unclear") score += 4;
  if (explicitRole === "weak_fragment") score -= 8;
  if (category === "unclear") score -= 5;
  if (dense.length >= 18) score += 4;
  else if (dense.length >= 10) score += 2;
  else score -= 4;
  return score;
}

function isPreviewWorkbenchUsefulCandidate(clip, scope = "smart") {
  if (isPreviewWorkbenchHardWasteCandidate(clip)) return false;
  const explicitRole = String(clip?.sales_role || "").toLowerCase();
  if (explicitRole === "weak_fragment") return false;
  const text = previewWorkbenchCandidateText(clip);
  const dense = text.replace(/[\s，。！？,.!?、：:；;"'“”‘’（）()【】\[\]-]/g, "");
  const category = previewWorkbenchCandidateCategory(clip, scope);
  if (category === "live" || category === "unclear") return false;
  if (dense.length < 10 && !["hook", "close"].includes(category)) return false;
  return true;
}

function previewWorkbenchCandidateOrigin(clip) {
  if ((clip?.candidate_origin && clip.candidate_origin !== "recommended") || clip?.recommended === false) return "extra";
  return "recommended";
}

function previewWorkbenchCandidateSourceFilter(scope) {
  return state.previewCandidateSourceFilters?.[scope] === "extra" ? "extra" : "recommended";
}

function previewWorkbenchCategoryFilter(scope) {
  return state.previewCandidateCategoryFilters?.[scope] || "all";
}

function previewWorkbenchSourceCandidatesFor(scope, preview, source) {
  const pool = (preview?.clips || []).filter(function (clip) {
    return previewWorkbenchCandidateOrigin(clip) === source && !isPreviewWorkbenchHardWasteCandidate(clip);
  });
  const useful = pool.filter(function (clip) { return isPreviewWorkbenchUsefulCandidate(clip, scope); });
  const selectedCount = previewWorkbenchSelectedClips(scope, preview).length;
  const targetCount = Math.min(pool.length, Math.max(8, selectedCount + 4, Math.ceil(selectedCount * 1.5)));
  if (useful.length >= targetCount) return useful;
  const usefulSet = new Set(useful.map(function (clip) { return Number(clip.index); }));
  const supplements = pool
    .filter(function (clip) { return !usefulSet.has(Number(clip.index)); })
    .sort(function (a, b) {
      const scoreDiff = previewWorkbenchCandidateQualityScore(b, scope) - previewWorkbenchCandidateQualityScore(a, scope);
      if (scoreDiff !== 0) return scoreDiff;
      return Number(a.index) - Number(b.index);
    })
    .slice(0, targetCount - useful.length);
  return [...useful, ...supplements].sort(function (a, b) { return Number(a.index) - Number(b.index); });
}

function previewWorkbenchSourceCandidates(scope, preview) {
  if (preview?.commercial_director_experiment) {
    const view = previewDirectorCandidateView(scope, preview);
    const activeChapterId = previewDirectorActiveChapterId(scope, preview);
    const all = (preview?.clips || []).filter(function (clip) { return !isPreviewWorkbenchHardWasteCandidate(clip); });
    if (view === "recommended") {
      return all.filter(function (clip) {
        return previewWorkbenchCandidateOrigin(clip) === "extra" && !isPreviewWorkbenchSelected(clip);
      });
    }
    if (view === "chapter") {
      return all.filter(function (clip) { return String(clip?.director_chapter_id || "") === activeChapterId; });
    }
    return all;
  }
  return previewWorkbenchSourceCandidatesFor(scope, preview, previewWorkbenchCandidateSourceFilter(scope));
}

function previewWorkbenchFilteredCandidates(scope, preview) {
  const category = previewWorkbenchCategoryFilter(scope);
  return previewWorkbenchSourceCandidates(scope, preview).filter(function (clip) {
    return category === "all" || previewWorkbenchCandidateCategory(clip, scope) === category;
  });
}

function previewWorkbenchCandidateFilterStats(scope, preview) {
  if (preview?.commercial_director_experiment) {
    const all = (preview?.clips || []).filter(function (clip) { return !isPreviewWorkbenchHardWasteCandidate(clip); });
    const activeChapterId = previewDirectorActiveChapterId(scope, preview);
    const recommended = all.filter(function (clip) {
      return previewWorkbenchCandidateOrigin(clip) === "extra" && !isPreviewWorkbenchSelected(clip);
    });
    const chapter = all.filter(function (clip) { return String(clip?.director_chapter_id || "") === activeChapterId; });
    return {
      recommended: recommended.length,
      extra: recommended.length,
      chapter: chapter.length,
      all: all.length,
      source: previewWorkbenchSourceCandidates(scope, preview).length,
      filtered: previewWorkbenchFilteredCandidates(scope, preview).length,
      duration: all.reduce(function (sum, clip) { return sum + effectiveClipDuration(clip); }, 0),
    };
  }
  const recommended = previewWorkbenchSourceCandidatesFor(scope, preview, "recommended").length;
  const extra = previewWorkbenchSourceCandidatesFor(scope, preview, "extra").length;
  const sourceCandidates = previewWorkbenchSourceCandidates(scope, preview);
  return {
    recommended,
    extra,
    source: sourceCandidates.length,
    filtered: previewWorkbenchFilteredCandidates(scope, preview).length,
  };
}

function setPreviewCandidateSourceFilter(scope = "smart", value = "recommended") {
  const preview = getPreviewState(scope);
  if (preview?.commercial_director_experiment) {
    if (!state.previewDirectorCandidateViews) state.previewDirectorCandidateViews = { smart: "recommended", mix: "recommended" };
    state.previewDirectorCandidateViews[scope] = ["recommended", "chapter", "all"].includes(value) ? value : "recommended";
    state.previewCandidateCategoryFilters[scope] = "all";
    state.previewCandidateSelections[scope] = null;
    renderPreviewStateKeepStoryScroll(scope);
    return;
  }
  if (!state.previewCandidateSourceFilters) state.previewCandidateSourceFilters = { smart: "recommended", mix: "recommended" };
  state.previewCandidateSourceFilters[scope] = value === "extra" ? "extra" : "recommended";
  state.previewCandidateSelections[scope] = null;
  renderPreviewStateKeepStoryScroll(scope);
}

function setPreviewCandidateCategoryFilter(scope = "smart", value = "all") {
  if (!state.previewCandidateCategoryFilters) state.previewCandidateCategoryFilters = { smart: "all", mix: "all" };
  state.previewCandidateCategoryFilters[scope] = value || "all";
  state.previewCandidateSelections[scope] = null;
  renderPreviewStateKeepStoryScroll(scope);
}

function renderPreviewCandidateFilterBar(scope, preview) {
  const labels = new Map(directPreviewWorkbenchCandidateCategories);
  if (preview?.commercial_director_experiment) {
    const view = previewDirectorCandidateView(scope, preview);
    const stats = previewWorkbenchCandidateFilterStats(scope, preview);
    const chapterId = previewDirectorActiveChapterId(scope, preview);
    const chapter = previewDirectorOutline(preview).find(function (item) { return String(item?.chapter_id || "") === chapterId; }) || {};
    const chapterLabel = String(chapter?.goal || chapter?.purchase_value || "当前章节").trim();
    const buttons = [
      ["recommended", "AI 备用", stats.recommended],
      ["chapter", "当前章节", stats.chapter],
      ["all", "完整句库", stats.all],
    ].map(function ([value, label, count]) {
      return '<button type="button" class="' + (view === value ? 'is-active' : '') + '" data-action="preview-candidate-source-filter" data-preview-scope="' + scope + '" data-value="' + value + '" aria-pressed="' + (view === value ? 'true' : 'false') + '"><span>' + label + '</span><em>' + count + '</em></button>';
    }).join("");
    const hint = view === "recommended"
      ? `AI 同次返回 ${stats.recommended} 条备用；更多原句请切换“完整句库”`
      : (view === "chapter" ? `正在查看：${chapterLabel}` : `当前公开候选约 ${stats.duration.toFixed(1)} 秒`);
    return '<div class="preview-candidate-filterbar is-director"><div class="preview-candidate-source-filter" role="tablist" aria-label="候选片段范围">' + buttons + '</div><small>' + escapeHtml(hint) + '</small></div>';
  }
  const source = previewWorkbenchCandidateSourceFilter(scope);
  const category = previewWorkbenchCategoryFilter(scope);
  const stats = previewWorkbenchCandidateFilterStats(scope, preview);
  const sourceButtons = [
    ["recommended", "\u0041\u0049\u63a8\u8350", stats.recommended],
    ["extra", "\u5907\u7528\u5019\u9009", stats.extra],
  ].map(function ([value, label, count]) {
    return '<button type="button" class="' + (source === value ? 'is-active' : '') + '" data-action="preview-candidate-source-filter" data-preview-scope="' + scope + '" data-value="' + value + '" ' + (count ? '' : 'disabled') + '><span>' + label + '</span><em>' + count + '</em></button>';
  }).join("");
  const present = new Map();
  previewWorkbenchSourceCandidates(scope, preview).forEach(function (clip) {
    const key = previewWorkbenchCandidateCategory(clip, scope);
    present.set(key, (present.get(key) || 0) + 1);
    if (key.startsWith("director:") && !labels.has(key)) {
      labels.set(key, previewWorkbenchTopicLabel(clip));
    }
  });
  const directorKeys = Array.from(present.keys()).filter(function (key) { return key.startsWith("director:"); });
  const categoryRows = directorKeys.length
    ? directorKeys.map(function (key) { return [key, labels.get(key) || "导演章节"]; })
    : directPreviewWorkbenchCandidateCategories;
  const allLabel = directorKeys.length ? "全部章节" : "全部品类";
  const categoryButtons = ['<button type="button" class="' + (category === 'all' ? 'is-active' : '') + '" data-action="preview-candidate-category-filter" data-preview-scope="' + scope + '" data-value="all"><span>' + allLabel + '</span><em>' + stats.source + '</em></button>'];
  categoryRows.forEach(function ([key, label]) {
    const count = present.get(key) || 0;
    if (!count) return;
    categoryButtons.push('<button type="button" class="' + (category === key ? 'is-active' : '') + '" data-action="preview-candidate-category-filter" data-preview-scope="' + scope + '" data-value="' + key + '"><span>' + escapeHtml(label) + '</span><em>' + count + '</em></button>');
  });
  return '<div class="preview-candidate-filterbar"><div class="preview-candidate-source-filter">' + sourceButtons + '</div><div class="preview-candidate-category-filter">' + categoryButtons.join("") + '</div><small>\u5f53\u524d\u663e\u793a ' + stats.filtered + ' / ' + stats.source + ' \u6bb5</small></div>';
}

function previewDirectorCandidateSuggestion(scope, preview, clip) {
  const replaces = String(clip?.director_replaces_beat_id || "").trim();
  if (replaces) {
    const selected = previewWorkbenchSelectedClips(scope, preview);
    const position = selected.findIndex(function (item) { return String(item?.director_beat_id || "") === replaces; });
    if (position >= 0) return `可替换第 ${position + 1} 句`;
  }
  const chapter = String(clip?.director_chapter_title || "").trim();
  return chapter ? `可补强：${chapter}` : "可作为当前故事的补充短句";
}

function renderPreviewCandidateGroups(scope, preview) {
  const activeCandidate = state.previewCandidateSelections?.[scope];
  const hasActiveCandidate = activeCandidate !== null && activeCandidate !== undefined;
  const labels = new Map(directPreviewWorkbenchCandidateCategories);
  const candidates = previewWorkbenchFilteredCandidates(scope, preview);
  if (preview?.commercial_director_experiment) {
    const rows = candidates.map(function (clip) {
      const selected = isPreviewWorkbenchSelected(clip);
      const active = Number(activeCandidate) === Number(clip.index) || (!hasActiveCandidate && Number(state.previewDetailSelection?.[scope]) === Number(clip.index));
      const clipText = String(previewWorkbenchCandidateText(clip) || "未识别口播").trim();
      const bounds = effectiveClipBounds(clip);
      const seconds = effectiveClipDuration(clip) || previewWorkbenchCandidateDuration(clip);
      const role = previewDirectorBeatRoleMeta(clip);
      const inspectButton = '<button class="preview-candidate-add is-added" data-action="preview-workbench-inspect-clip" data-preview-scope="' + scope + '" data-preview-index="' + Number(clip.index) + '">已选</button>';
      const addButton = '<button class="preview-candidate-add" data-action="preview-workbench-add-candidate" data-preview-scope="' + scope + '" data-preview-index="' + Number(clip.index) + '">加入</button>';
      const dragHandle = selected
        ? '<span class="preview-candidate-drag-handle is-disabled" title="已在右侧">已</span>'
        : '<span class="preview-candidate-drag-handle" data-preview-candidate-drag-handle data-preview-scope="' + scope + '" title="拖到右侧已选片段" aria-label="拖到右侧已选片段">拖</span>';
      return '<article class="preview-candidate-row is-director ' + (active ? 'is-active ' : '') + (selected ? 'is-selected' : '') + '" data-preview-candidate-row data-preview-scope="' + scope + '" data-preview-index="' + Number(clip.index) + '">' + dragHandle
        + '<button class="preview-candidate-main" data-action="preview-workbench-select-candidate" data-preview-scope="' + scope + '" data-preview-index="' + Number(clip.index) + '" title="' + escapeHtml(clipText) + '"><span class="preview-candidate-time">' + escapeHtml(formatSeconds(bounds.start)) + '–' + escapeHtml(formatSeconds(bounds.end)) + '<em>' + seconds.toFixed(1) + 's</em></span><strong>' + escapeHtml(clipText) + '</strong><small class="is-' + escapeHtml(role.tone) + '">' + escapeHtml(previewDirectorCandidateSuggestion(scope, preview, clip)) + '</small></button>'
        + (selected ? inspectButton : addButton) + '</article>';
    }).join("");
    return rows || '<div class="preview-sequence-empty"><strong>当前范围没有补充短句</strong><span>可以切换“当前章节”或“完整句库”继续查看，不会重新调用 AI。</span></div>';
  }
  const groups = [];
  candidates.forEach(function (clip) {
    const key = previewWorkbenchCandidateCategory(clip, scope);
    const last = groups[groups.length - 1];
    const directorLabel = String(clip?.director_chapter_title || "").trim();
    if (!last || last.key !== key) groups.push({ key: key, label: directorLabel || labels.get(key) || "\u5f85\u786e\u8ba4", clips: [] });
    groups[groups.length - 1].clips.push(clip);
  });
  return groups.map(function (group) {
    const key = group.key;
    const label = group.label;
    const clips = group.clips;
    const rows = clips.map(function (clip) {
      const selected = isPreviewWorkbenchSelected(clip);
      const active = Number(activeCandidate) === Number(clip.index) || (!hasActiveCandidate && Number(state.previewDetailSelection?.[scope]) === Number(clip.index));
      const clipText = String(clip.text || selectedPreviewText(clip) || "\u672a\u8bc6\u522b\u53e3\u64ad").trim();
      const inspectButton = '<button class="preview-candidate-add is-added" data-action="preview-workbench-inspect-clip" data-preview-scope="' + scope + '" data-preview-index="' + Number(clip.index) + '">\u5df2\u9009</button>';
      const addButton = '<button class="preview-candidate-add" data-action="preview-workbench-add-candidate" data-preview-scope="' + scope + '" data-preview-index="' + Number(clip.index) + '">\u52a0\u5165</button>';
      const dragHandle = selected
        ? '<span class="preview-candidate-drag-handle is-disabled" title="\u5df2\u5728\u53f3\u4fa7">\u5df2</span>'
        : '<span class="preview-candidate-drag-handle" data-preview-candidate-drag-handle data-preview-scope="' + scope + '" title="\u62d6\u5230\u53f3\u4fa7\u5df2\u9009\u7247\u6bb5" aria-label="\u62d6\u5230\u53f3\u4fa7\u5df2\u9009\u7247\u6bb5">\u62d6</span>';
      return '<article class="preview-candidate-row ' + (active ? 'is-active ' : '') + (selected ? 'is-selected' : '') + '" data-preview-candidate-row data-preview-scope="' + scope + '" data-preview-index="' + Number(clip.index) + '">' + dragHandle + '<button class="preview-candidate-main" data-action="preview-workbench-select-candidate" data-preview-scope="' + scope + '" data-preview-index="' + Number(clip.index) + '" title="' + escapeHtml(clipText) + '"><span>' + escapeHtml(clipText) + '</span></button>' + (selected ? inspectButton : addButton) + '</article>';
    }).join("");
    const secondary = key === "live" || key === "unclear";
    return '<details class="preview-candidate-group ' + (secondary ? 'is-secondary' : '') + '" data-preview-candidate-group="' + key + '"' + (secondary ? '' : ' open') + '><summary class="preview-candidate-group-head" title="\u70b9\u51fb\u5c55\u5f00\u6216\u6536\u8d77"><strong>' + label + '</strong><span>' + clips.length + '</span></summary><div class="preview-candidate-group-rows">' + rows + '</div></details>';
  }).join("") || '<div class="preview-sequence-empty"><strong>\u8fd9\u4e2a\u7b5b\u9009\u4e0b\u6ca1\u6709\u6709\u6548\u5019\u9009</strong><span>\u5df2\u8fc7\u6ee4\u5f31\u65ad\u53e5\u3001\u4e92\u52a8\u5e9f\u8bdd\u548c\u65e0\u5356\u70b9\u7247\u6bb5\uff1b\u53ef\u5207\u6362\u54c1\u7c7b\u6216\u5907\u7528\u5019\u9009\u3002</span></div>';
}

// SenseVoice supplies CTC-aligned Chinese characters, not trustworthy lexical
// word boundaries. These phrases improve the editor's display and click target
// only; cuts still use the original character indices and timestamps.
const previewEditorWordLexicon = Object.freeze([
  "\u5bf9\u649e\u886b", "\u70c2\u5927\u8857", "\u6027\u4ef7\u6bd4", "\u9ad8\u7ea7\u611f", "\u677e\u5f1b\u611f", "\u6c1b\u56f4\u611f", "\u8d28\u611f", "\u663e\u7626", "\u663e\u9ad8", "\u663e\u767d", "\u906e\u8089", "\u906e\u80ef", "\u906e\u809a\u5b50", "\u906e\u62dc\u62dc\u8089", "\u4e0d\u663e\u80d6", "\u4e0d\u81c3\u80bf", "\u4e0d\u900f\u8089", "\u4e0d\u95f7\u70ed", "\u4e0d\u7c98\u8eab", "\u4e0d\u6613\u76b1", "\u4e0d\u892a\u8272", "\u4e0d\u53d8\u5f62", "\u4e0d\u624e\u4eba", "\u4e0d\u6311\u4eba", "\u4e0d\u6311\u8eab\u6750", "\u4e0d\u6311\u80a4\u8272", "\u4e0d\u6311\u5e74\u9f84", "\u68a8\u5f62\u8eab\u6750", "\u82f9\u679c\u578b\u8eab\u6750", "\u5c0f\u4e2a\u5b50", "\u5fae\u80d6", "\u57fa\u7840\u6b3e", "\u8fde\u8863\u88d9", "\u534a\u8eab\u88d9", "\u9632\u6652\u8863", "\u9632\u6652\u886b", "\u9614\u817f\u88e4", "\u725b\u4ed4\u88e4", "\u884c\u653f\u88e4", "\u9488\u7ec7\u886b", "\u8857\u5934", "\u8857\u5934", "\u886c\u886b", "\u5957\u88c5", "\u5916\u5957", "\u5185\u642d", "\u9886\u53e3", "\u8896\u53e3", "\u80a9\u7ebf", "\u8170\u7ebf", "\u7248\u578b", "\u9762\u6599", "\u5782\u611f", "\u900f\u6c14", "\u4eb2\u80a4", "\u67d4\u8f6f", "\u5f39\u6027", "\u901a\u52e4", "\u65e5\u5e38", "\u5c0f\u4f17", "\u590d\u53e4", "\u7b80\u7ea6", "\u65b0\u4e2d\u5f0f", "\u6cd5\u5f0f", "\u7f8e\u5f0f", "\u97e9\u7cfb", "\u7a7f\u642d", "\u642d\u914d", "\u4e0a\u8eab", "\u4e00\u4ef6", "\u8fd9\u4ef6", "\u8fd9\u4e2a", "\u90a3\u4ef6", "\u90a3\u4e2a", "\u5b9d\u5b50\u4eec", "\u59d0\u59b9\u4eec", "\u5bb6\u4eba\u4eec"
].sort(function (left, right) { return right.length - left.length; }));

function previewEditorNativeWordRanges(text) {
  const source = String(text || "");
  if (!source) return [];
  if (typeof Intl !== "undefined" && typeof Intl.Segmenter === "function") {
    try {
      const ranges = [];
      const segmenter = new Intl.Segmenter("zh-CN", { granularity: "word" });
      for (const item of segmenter.segment(source)) {
        const piece = String(item?.segment || "");
        const start = Number(item?.index);
        if (piece && Number.isFinite(start)) ranges.push({ start, end: start + piece.length });
      }
      if (ranges.length) return ranges;
    } catch (_error) {
      // Old WebView2 runtimes use the deterministic character fallback below.
    }
  }
  const ranges = [];
  for (let start = 0; start < source.length;) {
    const piece = String.fromCodePoint(source.codePointAt(start));
    ranges.push({ start, end: start + piece.length });
    start += piece.length;
  }
  return ranges;
}

function previewEditorLexicalRanges(text) {
  const source = String(text || "");
  const nativeRanges = previewEditorNativeWordRanges(source);
  const nativeByStart = new Map(nativeRanges.map(function (range) { return [range.start, range]; }));
  const ranges = [];
  for (let start = 0; start < source.length;) {
    const lexiconMatch = previewEditorWordLexicon.find(function (term) { return source.startsWith(term, start); });
    if (lexiconMatch) {
      ranges.push({ start, end: start + lexiconMatch.length });
      start += lexiconMatch.length;
      continue;
    }
    const nativeRange = nativeByStart.get(start);
    if (nativeRange && nativeRange.end > start) {
      ranges.push(nativeRange);
      start = nativeRange.end;
      continue;
    }
    const piece = String.fromCodePoint(source.codePointAt(start));
    ranges.push({ start, end: start + piece.length });
    start += piece.length;
  }
  return ranges;
}

function previewEditorWordGroupsForRun(run) {
  const units = [];
  let offset = 0;
  run.words.forEach(function (word) {
    const text = String(word?.text || "");
    if (!text) return;
    units.push({ word, start: offset, end: offset + text.length });
    offset += text.length;
  });
  const groups = [];
  let unitIndex = 0;
  previewEditorLexicalRanges(units.map(function (unit) { return String(unit.word?.text || ""); }).join(""))
    .forEach(function (range) {
      const words = [];
      while (unitIndex < units.length && units[unitIndex].start < range.end) {
        words.push(units[unitIndex].word);
        unitIndex += 1;
      }
      if (words.length) groups.push({
        text: words.map(function (word) { return String(word?.text || ""); }).join(""),
        words,
        locked: run.locked,
        selected: run.selected,
      });
    });
  while (unitIndex < units.length) {
    const word = units[unitIndex].word;
    groups.push({ text: String(word?.text || ""), words: [word], locked: run.locked, selected: run.selected });
    unitIndex += 1;
  }
  return groups;
}

function previewEditorWordGroups(segment) {
  const runs = [];
  let current = null;
  previewSegmentWords(segment).forEach(function (word) {
    const text = String(word?.text || "");
    const locked = segment?.selection_locked === true || isPreviewWordLocked(word);
    const selected = word?.selected !== false;
    const previous = current?.words?.[current.words.length - 1];
    const previousEnd = Number(previous?.end);
    const wordStart = Number(word?.start);
    const hasPause = Number.isFinite(previousEnd) && Number.isFinite(wordStart) && wordStart - previousEnd > 0.32;
    const previousEndsPhrase = /[\u3002\uff01\uff1f!?\uff1b;\u2026]$/.test(String(previous?.text || ""));
    if (!current || current.locked !== locked || current.selected !== selected || hasPause || previousEndsPhrase) {
      current = { words: [], locked, selected };
      runs.push(current);
    }
    if (text) current.words.push(word);
  });
  return runs.flatMap(previewEditorWordGroupsForRun);
}

function togglePreviewWordGroupSelection(clipIndex, segmentIndex, rawIndices, scope = "smart") {
  const preview = getPreviewState(scope);
  const clip = preview?.clips?.find(function (item) { return Number(item.index) === Number(clipIndex); });
  const segment = previewSegments(clip).find(function (item) { return Number(item.index) === Number(segmentIndex); });
  if (!clip || !segment || segment.selection_locked === true) return;
  const indices = new Set(String(rawIndices || "").split(",").map(Number).filter(Number.isInteger));
  const words = previewSegmentWords(segment).filter(function (word) { return indices.has(Number(word.index)) && !isPreviewWordLocked(word); });
  if (!words.length) return;
  const restoreWords = words.some(function (word) { return word.selected === false; });
  applyPreviewWordSelection(clipIndex, segmentIndex, words.map(function (word) { return Number(word.index); }), restoreWords, scope);
}

function previewWordGroupIndices(rawIndices) {
  return String(rawIndices || "").split(",").map(Number).filter(Number.isInteger);
}

function previewWordButtonInfo(node) {
  const button = node?.closest?.('[data-action="preview-word-group-toggle"]');
  if (!button) return null;
  const clipIndex = Number(button.dataset.previewClip);
  const segmentIndex = Number(button.dataset.previewSegment);
  const indices = previewWordGroupIndices(button.dataset.previewWordGroup);
  if (!Number.isInteger(clipIndex) || !Number.isInteger(segmentIndex) || !indices.length) return null;
  return { button, scope: button.dataset.previewScope || "smart", clipIndex, segmentIndex, indices };
}

function previewWordRangeIndices(scope, clipIndex, segmentIndex, leftIndices, rightIndices) {
  const preview = getPreviewState(scope);
  const clip = preview?.clips?.find(function (item) { return Number(item.index) === Number(clipIndex); });
  const segment = previewSegments(clip).find(function (item) { return Number(item.index) === Number(segmentIndex); });
  if (!segment) return [];
  const words = previewSegmentWords(segment).filter(function (word) { return !isPreviewWordLocked(word); });
  const leftSet = new Set(leftIndices || []);
  const rightSet = new Set(rightIndices || []);
  const left = words.findIndex(function (word) { return leftSet.has(Number(word.index)); });
  const right = words.findIndex(function (word) { return rightSet.has(Number(word.index)); });
  if (left < 0 || right < 0) return [];
  return words.slice(Math.min(left, right), Math.max(left, right) + 1).map(function (word) { return Number(word.index); });
}

function previewWordEditHistory(scope = "smart") {
  if (!Array.isArray(state.previewWordEditHistory?.[scope])) state.previewWordEditHistory[scope] = [];
  return state.previewWordEditHistory[scope];
}

function recordPreviewWordEdit(scope, clip, segment) {
  const history = previewWordEditHistory(scope);
  history.push({
    clipIndex: Number(clip.index),
    segmentIndex: Number(segment.index),
    selected: previewSegmentWords(segment).map(function (word) { return [Number(word.index), word.selected !== false]; }),
    segmentSelected: segment.selected !== false,
    wordSelectionExplicit: segment.wordSelectionExplicit === true,
    clipSelected: clip.selected !== false,
  });
  if (history.length > 50) history.shift();
}

function refreshPreviewWordSelection(clip, segment, scope = "smart") {
  const selectable = previewSegmentWords(segment).filter(function (word) { return !isPreviewWordLocked(word); });
  segment.wordSelectionExplicit = selectedPreviewWords(segment).length !== selectable.length;
  segment.selected = selectedPreviewWords(segment).length > 0;
  clip.selected = previewSegments(clip).some(isPreviewSegmentSelected);
  setPreviewAssemblyMembership(scope, Number(clip.index), clip.selected);
}

function applyPreviewWordSelection(clipIndex, segmentIndex, indices, selected, scope = "smart") {
  const preview = getPreviewState(scope);
  const clip = preview?.clips?.find(function (item) { return Number(item.index) === Number(clipIndex); });
  const segment = previewSegments(clip).find(function (item) { return Number(item.index) === Number(segmentIndex); });
  const wanted = new Set((indices || []).map(Number).filter(Number.isInteger));
  const words = previewSegmentWords(segment).filter(function (word) { return wanted.has(Number(word.index)) && !isPreviewWordLocked(word); });
  if (!clip || !segment || !words.length || segment.selection_locked === true) return false;
  const changed = words.some(function (word) { return (word.selected !== false) !== selected; });
  if (!changed) return false;
  recordPreviewWordEdit(scope, clip, segment);
  words.forEach(function (word) { word.selected = selected; });
  refreshPreviewWordSelection(clip, segment, scope);
  commitPreviewDraft(scope);
  renderPreviewStateKeepStoryScroll(scope);
  return true;
}

function rememberPreviewWordRangeAnchor(info) {
  if (!info) return;
  state.previewWordRangeAnchors[info.scope] = {
    clipIndex: info.clipIndex,
    segmentIndex: info.segmentIndex,
    indices: [...info.indices],
  };
}

function applyPreviewWordRangeFromAnchor(info) {
  const anchor = state.previewWordRangeAnchors?.[info?.scope];
  if (!info || !anchor || anchor.clipIndex !== info.clipIndex || anchor.segmentIndex !== info.segmentIndex) return false;
  const indices = previewWordRangeIndices(info.scope, info.clipIndex, info.segmentIndex, anchor.indices, info.indices);
  const preview = getPreviewState(info.scope);
  const clip = preview?.clips?.find(function (item) { return Number(item.index) === info.clipIndex; });
  const segment = previewSegments(clip).find(function (item) { return Number(item.index) === info.segmentIndex; });
  const words = previewSegmentWords(segment).filter(function (word) { return indices.includes(Number(word.index)); });
  if (!words.length) return false;
  const restoreWords = words.some(function (word) { return word.selected === false; });
  const changed = applyPreviewWordSelection(info.clipIndex, info.segmentIndex, indices, restoreWords, info.scope);
  rememberPreviewWordRangeAnchor(info);
  return changed;
}

function updatePreviewWordRangeGestureUi(gesture) {
  const selector = '[data-action="preview-word-group-toggle"][data-preview-scope="' + gesture.scope + '"][data-preview-clip="' + gesture.clipIndex + '"][data-preview-segment="' + gesture.segmentIndex + '"]';
  const active = new Set(gesture.indices || []);
  const anchor = new Set(gesture.anchorIndices || []);
  document.querySelectorAll(selector).forEach(function (button) {
    const indices = previewWordGroupIndices(button.dataset.previewWordGroup);
    button.classList.toggle("is-range-preview", indices.some(function (index) { return active.has(index); }));
    button.classList.toggle("is-range-anchor", indices.some(function (index) { return anchor.has(index); }));
  });
}

function clearPreviewWordRangeGesture() {
  const gesture = state.previewWordRangeGesture;
  if (gesture) {
    const selector = '[data-action="preview-word-group-toggle"][data-preview-scope="' + gesture.scope + '"][data-preview-clip="' + gesture.clipIndex + '"][data-preview-segment="' + gesture.segmentIndex + '"]';
    document.querySelectorAll(selector).forEach(function (button) { button.classList.remove("is-range-preview", "is-range-anchor"); });
  }
  state.previewWordRangeGesture = null;
}

function beginPreviewWordRangeGesture(info, event) {
  if (!info || event.button !== 0) return;
  state.previewWordRangeGesture = {
    scope: info.scope,
    clipIndex: info.clipIndex,
    segmentIndex: info.segmentIndex,
    pointerId: event.pointerId,
    anchorIndices: [...info.indices],
    activeIndices: [...info.indices],
    indices: [...info.indices],
    dragging: false,
  };
}

function updatePreviewWordRangeGesture(event) {
  const gesture = state.previewWordRangeGesture;
  if (!gesture || (Number.isInteger(gesture.pointerId) && gesture.pointerId !== event.pointerId)) return;
  const info = previewWordButtonInfo(document.elementFromPoint(event.clientX, event.clientY));
  if (!info || info.scope !== gesture.scope || info.clipIndex !== gesture.clipIndex || info.segmentIndex !== gesture.segmentIndex) return;
  const indices = previewWordRangeIndices(gesture.scope, gesture.clipIndex, gesture.segmentIndex, gesture.anchorIndices, info.indices);
  if (!indices.length) return;
  const changedTarget = String(indices) !== String(gesture.indices);
  gesture.activeIndices = [...info.indices];
  gesture.indices = indices;
  gesture.dragging = gesture.dragging || changedTarget;
  if (gesture.dragging) updatePreviewWordRangeGestureUi(gesture);
}

function finishPreviewWordRangeGesture(event) {
  const gesture = state.previewWordRangeGesture;
  if (!gesture || (Number.isInteger(gesture.pointerId) && gesture.pointerId !== event.pointerId)) return false;
  clearPreviewWordRangeGesture();
  if (!gesture.dragging) return false;
  const preview = getPreviewState(gesture.scope);
  const clip = preview?.clips?.find(function (item) { return Number(item.index) === gesture.clipIndex; });
  const segment = previewSegments(clip).find(function (item) { return Number(item.index) === gesture.segmentIndex; });
  const words = previewSegmentWords(segment).filter(function (word) { return gesture.indices.includes(Number(word.index)); });
  const restoreWords = words.some(function (word) { return word.selected === false; });
  state.previewWordSkipClick = { scope: gesture.scope, clipIndex: gesture.clipIndex, segmentIndex: gesture.segmentIndex, indices: [...gesture.activeIndices], expiresAt: Date.now() + 800 };
  rememberPreviewWordRangeAnchor({ scope: gesture.scope, clipIndex: gesture.clipIndex, segmentIndex: gesture.segmentIndex, indices: gesture.activeIndices });
  return applyPreviewWordSelection(gesture.clipIndex, gesture.segmentIndex, gesture.indices, restoreWords, gesture.scope);
}

function shouldSkipPreviewWordClick(info) {
  const skip = state.previewWordSkipClick;
  if (!skip || Date.now() > Number(skip.expiresAt || 0)) {
    state.previewWordSkipClick = null;
    return false;
  }
  const matches = info && skip.scope === info.scope && skip.clipIndex === info.clipIndex && skip.segmentIndex === info.segmentIndex
    && info.indices.some(function (index) { return (skip.indices || []).includes(index); });
  state.previewWordSkipClick = null;
  return matches;
}

function undoPreviewWordEdit(scope = "smart") {
  const history = previewWordEditHistory(scope);
  const previous = history.pop();
  if (!previous) return;
  const preview = getPreviewState(scope);
  const clip = preview?.clips?.find(function (item) { return Number(item.index) === Number(previous.clipIndex); });
  const segment = previewSegments(clip).find(function (item) { return Number(item.index) === Number(previous.segmentIndex); });
  if (!clip || !segment) return;
  const selectedByIndex = new Map(previous.selected || []);
  previewSegmentWords(segment).forEach(function (word) {
    if (!isPreviewWordLocked(word) && selectedByIndex.has(Number(word.index))) word.selected = selectedByIndex.get(Number(word.index));
  });
  segment.selected = previous.segmentSelected;
  segment.wordSelectionExplicit = previous.wordSelectionExplicit;
  clip.selected = previous.clipSelected;
  setPreviewAssemblyMembership(scope, Number(clip.index), clip.selected);
  commitPreviewDraft(scope);
  renderPreviewStateKeepStoryScroll(scope);
}

function previewWordEditStats(scope, clip) {
  const editableSegments = previewSegments(clip).filter(function (segment) { return segment?.selection_locked !== true; });
  const before = editableSegments.reduce(function (sum, segment) {
    const start = Number(segment?.start || 0);
    const end = Number(segment?.end || start);
    return sum + Math.max(0, Number(segment?.duration || end - start));
  }, 0);
  const current = effectiveClipDuration(clip);
  const selected = previewWorkbenchSelectedClips(scope, getPreviewState(scope));
  const total = selected.reduce(function (sum, item) { return sum + effectiveClipDuration(item); }, 0);
  return { removed: Math.max(0, before - current), current, total };
}

async function auditionPreviewWordEdit(scope = "smart") {
  const preview = getPreviewState(scope);
  const current = previewWorkbenchCurrentClip(scope, preview);
  if (!current?.clip || current.inspectOnly) return;
  await ensureInlinePreviewVideo(scope, Number(current.clip.index));
  const video = previewBox(scope)?.querySelector('[data-preview-inline-video="' + scope + '"][data-preview-index="' + Number(current.clip.index) + '"] [data-preview-inline-player]');
  if (!video || !video.getAttribute("src")) {
    toast("正在准备剪口试听，请稍后再试", "info");
    return;
  }
  video.currentTime = 0;
  try { await video.play(); } catch (_) {}
}

function renderPreviewWordEditToolbar(scope, clip) {
  const stats = previewWordEditStats(scope, clip);
  const undoEnabled = previewWordEditHistory(scope).length > 0;
  const impact = stats.removed > 0.01
    ? '已裁 ' + stats.removed.toFixed(1) + 's · 当前片段 ' + stats.current.toFixed(1) + 's · 成片预计 ' + stats.total.toFixed(1) + 's'
    : '当前片段 ' + stats.current.toFixed(1) + 's · 成片预计 ' + stats.total.toFixed(1) + 's';
  return '<div class="preview-editor-word-toolbar"><span class="preview-editor-impact" role="status" aria-live="polite">' + impact + '</span><div><button type="button" class="button button-muted button-small" data-action="preview-word-audition" data-preview-scope="' + scope + '">试听当前片段</button><button type="button" class="button button-muted button-small" data-action="preview-word-undo" data-preview-scope="' + scope + '" ' + (undoEnabled ? '' : 'disabled') + '>撤销</button></div></div>';
}

function renderPreviewEditorSentence(scope, clip, segment, position) {
  const locked = segment?.selection_locked === true;
  const selected = isPreviewSegmentSelected(segment);
  const words = previewSegmentWords(segment);
  const wordTimed = segment?.word_timed === true && words.length > 0;
  const reason = String(segment?.blocked_reason || segment?.auto_unselected_reason || "").trim();
  let wordHint = "";
  let wordRows = "";
  if (!wordTimed) {
    const sentenceText = String(segment?.text || words.map(function (word) { return String(word?.text || ""); }).join("") || "\u672a\u8bc6\u522b\u53e5\u5b50");
    wordRows = '<span class="preview-word is-static">' + escapeHtml(sentenceText) + '</span>';
    if (!locked) wordHint = '<small class="preview-editor-word-hint">\u6682\u65e0\u9010\u8bcd\u65f6\u95f4\uff0c\u53ef\u4ee5\u6574\u53e5\u5220\u9664</small>';
  } else {
    wordRows = previewEditorWordGroups(segment).map(function (group) {
      const groupText = escapeHtml(group.text || "");
      if (group.locked) {
        const blockedReason = locked ? (reason || "\u98ce\u9669\u53e5\u4e0d\u53ef\u9009") : String(group.words.find(function (word) { return isPreviewWordLocked(word); })?.blocked_reason || "\u8fdd\u7981\u8bcd\u4e0d\u53ef\u6062\u590d");
        return '<span class="preview-word is-locked" title="' + escapeHtml(blockedReason) + '">' + groupText + '</span>';
      }
      const deleted = group.words.length > 0 && group.words.every(function (word) { return word?.selected === false; });
      const indices = group.words.map(function (word) { return Number(word.index); }).filter(Number.isInteger).join(",");
        return '<button type="button" class="preview-word ' + (deleted ? 'is-deleted' : '') + '" data-action="preview-word-group-toggle" data-preview-scope="' + scope + '" data-preview-clip="' + Number(clip.index) + '" data-preview-segment="' + Number(segment.index) + '" data-preview-word-group="' + indices + '" aria-pressed="' + (!deleted ? 'true' : 'false') + '" title="' + (deleted ? '\u70b9\u51fb\u6062\u590d\u8fd9\u4e2a\u8bcd\u5757' : '\u70b9\u51fb\u5220\u9664\u8fd9\u4e2a\u8bcd\u5757') + '">' + groupText + '</button>';
    }).join("");
    wordHint = '<small class="preview-editor-word-hint">\u70b9\u51fb\u7cbe\u4fee\uff1b\u62d6\u9009\u8fde\u7eed\u5220\u8bcd\uff1bShift + \u70b9\u51fb\u6269\u5c55\u9009\u533a\uff1bCtrl + Z \u64a4\u9500</small>';
  }
  return '<article class="preview-editor-sentence ' + (!selected ? 'is-deleted' : '') + ' ' + (locked ? 'is-locked' : '') + '"><div class="preview-editor-sentence-head"><label><input type="checkbox" data-preview-segment data-preview-scope="' + scope + '" data-preview-segment-parent="' + Number(clip.index) + '" data-preview-segment-index="' + Number(segment.index) + '" ' + (selected ? 'checked' : '') + ' ' + (locked ? 'disabled' : '') + '><strong>\u7b2c ' + (position + 1) + ' \u53e5</strong></label><span>' + (locked ? '\u98ce\u9669\u53e5\u4e0d\u53ef\u9009' : (selected ? '\u5df2\u4fdd\u7559' : '\u5df2\u5220\u9664')) + '</span></div><div class="preview-editor-words">' + wordRows + '</div>' + wordHint + (reason ? '<small class="preview-editor-lock-reason">' + escapeHtml(reason) + '</small>' : '') + '</article>';
}

function renderPreviewSentenceEditor(scope, current) {
  const clip = current?.clip;
  if (!clip) return '<section class="preview-sentence-editor"><div class="preview-workbench-column-head"><div><strong>\u9009\u53e5 / \u5220\u8bcd</strong><span>\u70b9\u51fb\u5de6\u4fa7\u5019\u9009\u7247\u6bb5\u540e\u7f16\u8f91</span></div></div><div class="preview-sequence-empty"><strong>\u8fd8\u6ca1\u6709\u5f53\u524d\u7247\u6bb5</strong></div></section>';
  if (!isPreviewWorkbenchSelected(clip)) return '<section class="preview-sentence-editor"><div class="preview-workbench-column-head"><div><strong>\u9009\u53e5 / \u5220\u8bcd</strong><span>\u5148\u770b\u89c6\u9891\uff0c\u52a0\u5165\u5df2\u9009\u540e\u518d\u7cbe\u4fee</span></div></div><div class="preview-sequence-empty"><strong>\u786e\u8ba4\u8fd9\u6bb5\u89c6\u9891\u540e\u70b9\u201c\u52a0\u5165\u5df2\u9009\u201d</strong><span>\u5df2\u9009\u7247\u6bb5\u4f1a\u5728\u8fd9\u91cc\u663e\u793a\u5168\u90e8\u53e5\u5b50\u548c\u53ef\u5220\u8bcd\u5757\u3002</span></div></section>';
  const segments = previewSegments(clip);
  const body = segments.length ? segments.map(function (segment, position) { return renderPreviewEditorSentence(scope, clip, segment, position); }).join("") : '<article class="preview-editor-sentence"><div class="preview-editor-words"><span class="preview-word is-static">' + escapeHtml(String(clip.text || "\u672a\u8bc6\u522b\u53e3\u64ad")) + '</span></div></article>';
  return '<section class="preview-sentence-editor"><div class="preview-workbench-column-head preview-editor-column-head"><div><strong>\u9009\u53e5 / \u5220\u8bcd</strong><span>\u62d6\u9009\u8fde\u7eed\u8bcd\u7ec4\uff0c\u677e\u624b\u540e\u4e00\u6b21\u63d0\u4ea4</span></div><div class="preview-editor-head-actions"><small>' + escapeHtml(previewWorkbenchCategoryLabel(clip, scope)) + '</small>' + renderPreviewWordEditToolbar(scope, clip) + '</div></div><div class="preview-editor-sentence-list">' + body + '</div></section>';
}

function previewOverviewSourceValue(clip) {
  return String(clip?.source || clip?.source_name || clip?.source_marker || "").trim();
}

function previewOverviewSelectedSourceStats(scope, preview, selected, durationSpeed) {
  const stats = new Map();
  selected.forEach(function (clip) {
    const raw = previewOverviewSourceValue(clip);
    const key = raw ? raw.toLowerCase() : `missing:${Number(clip?.index)}`;
    const existing = stats.get(key) || {
      key,
      label: raw ? sourceBaseName(raw) : "来源待确认",
      alias: previewSourceAlias(scope, preview, clip),
      count: 0,
      rawDuration: 0,
    };
    existing.count += 1;
    existing.rawDuration += effectiveClipDuration(clip);
    stats.set(key, existing);
  });
  const speed = Math.max(0.1, Number(durationSpeed || 1) || 1);
  const total = Array.from(stats.values()).reduce(function (sum, item) { return sum + item.rawDuration; }, 0);
  return Array.from(stats.values()).map(function (item) {
    return {
      ...item,
      duration: item.rawDuration / speed,
      ratio: total > 0 ? item.rawDuration / total : 0,
    };
  });
}

function previewOverviewExpectedSourceCount(preview) {
  const declared = Array.isArray(preview?.sources) ? preview.sources : [];
  const values = declared.map(function (item) {
    if (item && typeof item === "object") return String(item.path || item.source || item.name || "").trim();
    return String(item || "").trim();
  }).filter(Boolean);
  if (values.length) return new Set(values.map(function (item) { return item.toLowerCase(); })).size;
  return new Set((preview?.clips || []).map(previewOverviewSourceValue).filter(Boolean).map(function (item) { return item.toLowerCase(); })).size;
}

function buildPreviewFilmOverview(scope, preview, targetId, selected, duration) {
  const prefix = scope === "mix" ? "mix" : "sc";
  const dedup = preview?.dedup_summary || {};
  const plan = dedup.plan_quality_report || {};
  const category = dedup.category_summary || {};
  const preference = dedup.preference_summary || {};
  const roleCounts = { hook: 0, product: 0, proof: 0, scene: 0, close: 0, weak: 0 };
  const topics = new Set();
  const issues = [];
  const duplicateTexts = new Map();

  selected.forEach(function (clip, position) {
    const role = previewWorkbenchRoleKey(clip);
    roleCounts[role] = (roleCounts[role] || 0) + 1;
    const topic = previewWorkbenchTopicLabel(clip);
    if (topic && topic !== "\u5176\u4ed6") topics.add(topic);

    const clipIndex = Number(clip?.index);
    const clipDuration = effectiveClipDuration(clip);
    if (clipDuration > 0 && clipDuration < 1.2) {
      issues.push({ kind: "short", label: `\u7b2c${position + 1}\u6bb5\u8fc7\u77ed`, detail: `\u5f53\u524d\u4ec5${clipDuration.toFixed(1)}s\uff0c\u53ef\u80fd\u5f71\u54cd\u89c2\u611f`, clipIndex });
    }

    const normalizedText = selectedPreviewText(clip).replace(/[^0-9a-z\u4e00-\u9fff]+/gi, "").toLowerCase();
    if (normalizedText.length >= 4 && duplicateTexts.has(normalizedText)) {
      issues.push({ kind: "duplicate", label: `\u7b2c${position + 1}\u6bb5\u6587\u6848\u91cd\u590d`, detail: `\u4e0e\u7b2c${duplicateTexts.get(normalizedText) + 1}\u6bb5\u6587\u6848\u76f8\u540c`, clipIndex });
    } else if (normalizedText.length >= 4) {
      duplicateTexts.set(normalizedText, position);
    }

    if (position === 0) return;
    const previous = selected[position - 1];
    const previousSource = previewOverviewSourceValue(previous).toLowerCase();
    const currentSource = previewOverviewSourceValue(clip).toLowerCase();
    const sameSource = scope !== "mix" || (previousSource && currentSource && previousSource === currentSource);
    if (!sameSource) return;
    const previousBounds = effectiveClipBounds(previous);
    const currentBounds = effectiveClipBounds(clip);
    if (currentBounds.start + 0.05 < previousBounds.start) {
      issues.push({ kind: "reverse", label: `\u7b2c${position + 1}\u6bb5\u65f6\u95f4\u5012\u5e8f`, detail: "\u540c\u4e00\u7d20\u6750\u4e2d\u540e\u4e00\u6bb5\u6392\u5230\u4e86\u66f4\u65e9\u7684\u65f6\u95f4", clipIndex });
    } else if (currentBounds.start < previousBounds.end - 0.05) {
      issues.push({ kind: "overlap", label: `\u7b2c${position + 1}\u6bb5\u65f6\u95f4\u91cd\u53e0`, detail: `\u4e0e\u524d\u4e00\u6bb5\u91cd\u53e0${(previousBounds.end - currentBounds.start).toFixed(2)}s`, clipIndex });
    }
  });

  if (!selected.length || duration.rawTotal <= 0.05) {
    issues.unshift({ kind: "empty", label: "\u5f53\u524d\u6ca1\u6709\u53ef\u7528\u7247\u6bb5", detail: "\u8bf7\u5148\u52a0\u5165\u7247\u6bb5\u5e76\u4fdd\u7559\u81f3\u5c11\u4e00\u53e5\u5185\u5bb9", blocking: true });
  } else if (!duration.accepted) {
    issues.unshift({ kind: "duration", label: duration.projected < duration.low ? "\u9884\u8ba1\u6210\u7247\u504f\u77ed" : "\u9884\u8ba1\u6210\u7247\u504f\u957f", detail: `\u5f53\u524d${duration.projected.toFixed(1)}s\uff0c\u5141\u8bb8${duration.low.toFixed(0)}-${duration.high.toFixed(0)}s`, action: "fit" });
  }
  if (selected.length && roleCounts.hook === 0) issues.push({ kind: "structure", label: "\u7f3a\u5c11\u5f00\u573a\u5438\u5f15", detail: "\u5f53\u524d\u7f16\u6392\u6ca1\u6709\u660e\u786e\u7684\u5f00\u573a\u7247\u6bb5" });
  if (selected.length && roleCounts.close === 0) issues.push({ kind: "structure", label: "\u7f3a\u5c11\u5b8c\u6574\u6536\u5c3e", detail: "\u5f53\u524d\u7f16\u6392\u6ca1\u6709\u660e\u786e\u7684\u6536\u5c3e\u7247\u6bb5" });

  const sourceStats = previewOverviewSelectedSourceStats(scope, preview, selected, duration.speed);
  const expectedSourceCount = scope === "mix" ? previewOverviewExpectedSourceCount(preview) : sourceStats.length;
  if (scope === "mix" && selected.length) {
    const unresolved = selected.find(function (clip) { return !previewOverviewSourceValue(clip); });
    if (unresolved) {
      issues.push({ kind: "source", label: "\u5b58\u5728\u7d20\u6750\u6765\u6e90\u5f85\u786e\u8ba4", detail: "\u8fd9\u4f1a\u5f71\u54cd\u6df7\u526a\u7d20\u6750\u5b9a\u4f4d", clipIndex: Number(unresolved.index) });
    }
    if (expectedSourceCount > sourceStats.length) {
      issues.push({ kind: "source", label: `\u7d20\u6750\u4f7f\u7528${sourceStats.length}/${expectedSourceCount}`, detail: "\u5f53\u524d\u6ca1\u6709\u8986\u76d6\u5168\u90e8\u6df7\u526a\u7d20\u6750" });
    }
    const largest = sourceStats.reduce(function (best, item) { return !best || item.ratio > best.ratio ? item : best; }, null);
    if (sourceStats.length >= 2 && largest && largest.ratio > 0.65) {
      issues.push({ kind: "balance", label: "\u6df7\u526a\u7d20\u6750\u5360\u6bd4\u4e0d\u5747", detail: `${largest.alias || largest.label}\u5360\u9884\u8ba1\u6210\u7247${(largest.ratio * 100).toFixed(0)}%` });
    }
  }

  const blocking = issues.some(function (item) { return item.blocking === true; });
  const status = blocking ? "block" : (issues.length ? "warn" : "ok");
  const statusLabel = blocking ? "\u6682\u4e0d\u80fd\u6210\u7247" : (issues.length ? `\u5efa\u8bae\u68c0\u67e5 ${issues.length} \u9879` : "\u53ef\u4ee5\u6210\u7247");
  const mainProduct = String($(`${prefix}-main-product`)?.value || plan?.product?.locked || plan?.product?.selected || "").trim();
  const mainCategory = String(category.main_category || $(`${prefix}-primary-category`)?.selectedOptions?.[0]?.textContent || "").trim();
  const preferenceLabel = String(preference.used_label || preference.label || "").trim();
  const salesChain = buildSalesChainSummary(selected);
  const sellingCount = roleCounts.product + roleCounts.proof + roleCounts.scene + roleCounts.weak;
  const naming = String($(`${prefix}-output-naming`)?.selectedOptions?.[0]?.textContent || "").trim();
  const versions = Number($(`${prefix}-versions`)?.value || 1) || 1;
  const subtitles = Boolean($(`${prefix}-subtitle`)?.checked);
  return {
    status,
    statusLabel,
    issues,
    roleCounts,
    sellingCount,
    topics: Array.from(topics),
    sourceStats,
    expectedSourceCount,
    mainProduct,
    mainCategory,
    preferenceLabel,
    salesChain,
    naming,
    versions,
    subtitles,
    initialPlanStatus: String(plan.status || ""),
  };
}

function renderPreviewFilmOverview(scope, preview, targetId, selected, duration) {
  const overview = buildPreviewFilmOverview(scope, preview, targetId, selected, duration);
  const maxDuration = Math.max(duration.high, duration.projected, 1);
  const currentPercent = Math.min(100, Math.max(0, duration.projected / maxDuration * 100));
  const lowPercent = Math.min(100, Math.max(0, duration.low / maxDuration * 100));
  const highPercent = Math.min(100, Math.max(lowPercent, duration.high / maxDuration * 100));
  const targetPercent = Math.min(100, Math.max(0, duration.target / maxDuration * 100));
  const directorChapterCount = Array.isArray(preview?.director_review?.m2_outline)
    ? preview.director_review.m2_outline.length
    : 0;
  const structureText = preview?.commercial_director_experiment && directorChapterCount
    ? `${directorChapterCount}\u7ae0 \u00b7 ${selected.length}\u53e5`
    : `\u5f00\u573a${overview.roleCounts.hook} \u00b7 \u5356\u70b9${overview.sellingCount} \u00b7 \u6536\u5c3e${overview.roleCounts.close}`;
  const sourceText = scope === "mix"
    ? `\u7d20\u6750 ${overview.sourceStats.length}/${overview.expectedSourceCount || overview.sourceStats.length}`
    : (overview.sourceStats[0]?.label || "\u7d20\u6750\u5f85\u786e\u8ba4");
  const issueRows = overview.issues.length ? overview.issues.map(function (issue) {
    let action = "";
    if (issue.action === "fit") {
      action = `<button type="button" data-action="preview-duration-fit" data-preview-scope="${scope}">\u9002\u914d\u65f6\u957f</button>`;
    } else if (Number.isInteger(issue.clipIndex)) {
      action = `<button type="button" data-action="preview-overview-locate" data-preview-scope="${scope}" data-preview-index="${issue.clipIndex}">\u5b9a\u4f4d\u7247\u6bb5</button>`;
    }
    return `<li><span><strong>${escapeHtml(issue.label)}</strong><small>${escapeHtml(issue.detail)}</small></span>${action}</li>`;
  }).join("") : '<li class="is-ok"><span><strong>\u672a\u53d1\u73b0\u660e\u663e\u95ee\u9898</strong><small>\u5f53\u524d\u65f6\u957f\u3001\u7ed3\u6784\u548c\u65f6\u95f4\u987a\u5e8f\u53ef\u7528</small></span></li>';
  const sourceRows = overview.sourceStats.length ? overview.sourceStats.map(function (item) {
    const prefix = item.alias ? `${item.alias} ` : "";
    return `<span title="${escapeHtml(item.label)}"><strong>${escapeHtml(prefix + item.label)}</strong><small>${item.count}\u6bb5 \u00b7 ${item.duration.toFixed(1)}s \u00b7 ${(item.ratio * 100).toFixed(0)}%</small></span>`;
  }).join("") : '<span><strong>\u6682\u65e0\u7d20\u6750\u5206\u5e03</strong></span>';
  const contentParts = [overview.mainProduct && `\u4e3b\u5546\u54c1\uff1a${overview.mainProduct}`, overview.mainCategory && `\u54c1\u7c7b\uff1a${overview.mainCategory}`, overview.preferenceLabel && `AI\u504f\u597d\uff1a${overview.preferenceLabel}`, overview.topics.length && `\u4e3b\u9898\uff1a${overview.topics.join("/")}`].filter(Boolean);
  const outputParts = [overview.naming, `${overview.versions}\u4e2a\u7248\u672c`, overview.subtitles ? "\u5b57\u5e55\u5f00\u542f" : "\u5b57\u5e55\u5173\u95ed", `\u9884\u8ba1\u53d8\u901f${duration.speed.toFixed(2)}x`].filter(Boolean);
  const initialPlan = overview.initialPlanStatus ? (overview.initialPlanStatus === "pass" ? "AI\u521d\u59cb\u7247\u5355\u901a\u8fc7" : "AI\u521d\u59cb\u7247\u5355\u6709\u63d0\u9192") : "AI\u521d\u59cb\u7247\u5355\u672a\u8bb0\u5f55";
  return `<section class="preview-film-overview is-${overview.status}" data-preview-film-overview="${scope}">
    <div class="preview-film-overview-head">
      <div class="preview-film-status"><i></i><span><strong>\u5f53\u524d\u6210\u7247</strong><small>${escapeHtml(overview.statusLabel)}</small></span></div>
      <div class="preview-film-metrics">
        <span><small>\u9884\u8ba1\u65f6\u957f</small><strong>${duration.projected.toFixed(1)}s <em>/ ${duration.target.toFixed(0)}s\u00b1${duration.tolerance.toFixed(0)}s</em></strong></span>
        <span><small>\u5df2\u9009\u7247\u6bb5</small><strong>${selected.length}\u6bb5</strong></span>
        <span><small>\u5185\u5bb9\u7ed3\u6784</small><strong>${escapeHtml(structureText)}</strong></span>
        <span><small>${scope === "mix" ? "\u6df7\u526a\u6765\u6e90" : "\u539f\u89c6\u9891"}</small><strong title="${escapeHtml(sourceText)}">${escapeHtml(sourceText)}</strong></span>
      </div>
      <div class="preview-film-actions"><button class="button button-muted button-small" data-action="preview-duration-fit" data-preview-scope="${scope}">\u9002\u914d\u65f6\u957f</button><button class="button button-secondary button-small" data-action="preview-overview-toggle" data-preview-scope="${scope}">${overview.issues.length ? "\u67e5\u770b\u95ee\u9898" : "\u67e5\u770b\u8be6\u60c5"}</button></div>
    </div>
    <div class="preview-film-duration" title="\u53ef\u63a5\u53d7 ${duration.low.toFixed(1)}-${duration.high.toFixed(1)}s\uff0c\u5f53\u524d\u9884\u8ba1 ${duration.projected.toFixed(1)}s"><span class="preview-film-duration-range" style="left:${lowPercent.toFixed(2)}%;width:${Math.max(0, highPercent - lowPercent).toFixed(2)}%"></span><span class="preview-film-duration-value" style="width:${currentPercent.toFixed(2)}%"></span><i style="left:${targetPercent.toFixed(2)}%"></i></div>
    <details class="preview-film-details" data-preview-film-details="${scope}"><summary><span>\u6210\u7247\u8be6\u60c5</span><small>${overview.issues.length ? `${overview.issues.length}\u9879\u5f85\u68c0\u67e5` : "\u5f53\u524d\u7f16\u6392\u6b63\u5e38"}</small></summary>
      <div class="preview-film-detail-rows">
        <div><strong>\u5185\u5bb9</strong><span>${escapeHtml(contentParts.join(" \u00b7 ") || "\u6682\u65e0\u4e3b\u5546\u54c1\u6216\u4e3b\u9898\u4fe1\u606f")}</span></div>
        <div><strong>\u7ed3\u6784</strong><span>${escapeHtml(`\u6210\u4ea4\u7ed3\u6784 ${overview.salesChain.label}\uff1b${overview.salesChain.title}`)}</span></div>
        <div><strong>\u7d20\u6750</strong><span class="preview-film-source-list">${sourceRows}</span></div>
        <div><strong>\u8f93\u51fa</strong><span>${escapeHtml(outputParts.join(" \u00b7 "))}</span></div>
        <div><strong>AI\u521d\u59cb\u7ed3\u679c</strong><span>${escapeHtml(initialPlan)}\uff1b\u4e0b\u65b9\u68c0\u67e5\u5df2\u6309\u5f53\u524d\u624b\u5de5\u7f16\u6392\u91cd\u65b0\u8ba1\u7b97</span></div>
      </div>
      <ul class="preview-film-issue-list">${issueRows}</ul>
    </details>
  </section>`;
}

function previewDirectorHasManualEdits(scope, preview) {
  const selected = previewWorkbenchSelectedClips(scope, preview);
  const initial = (preview?.clips || []).filter(function (clip) {
    return previewWorkbenchCandidateOrigin(clip) === "recommended" && !isPreviewWorkbenchHardWasteCandidate(clip);
  });
  const initialOrder = initial.map(function (clip) { return Number(clip.index); });
  const currentOrder = selected.map(function (clip) { return Number(clip.index); });
  const membershipChanged = selected.some(function (clip) { return previewWorkbenchCandidateOrigin(clip) === "extra"; })
    || initial.some(function (clip) { return !isPreviewWorkbenchSelected(clip); });
  const wordsChanged = selected.some(function (clip) {
    return previewSegments(clip).some(function (segment) { return segment?.wordSelectionExplicit === true; });
  });
  return membershipChanged || wordsChanged || String(initialOrder) !== String(currentOrder);
}

function previewDirectorCurrentStatus(scope, preview, targetId, selected, duration) {
  const overview = buildPreviewFilmOverview(scope, preview, targetId, selected, duration);
  const review = preview?.director_review || {};
  const fidelity = review.export_fidelity?.status === "warning"
    ? review.export_fidelity : (review.preview_fidelity || preview?.dedup_summary?.director_preview_fidelity || {});
  const boundaryWarning = fidelity.status === "warning";
  const currentStatus = overview.status === "block" ? "block" : (boundaryWarning ? "warn" : overview.status);
  const duplicates = overview.issues.filter(function (item) { return item.kind === "duplicate"; }).length;
  const firstFunction = String(selected[0]?.director_beat_function || selected[0]?.director_chapter_kind || "").toLowerCase();
  const lastFunction = String(selected[selected.length - 1]?.director_beat_function || selected[selected.length - 1]?.director_chapter_kind || "").toLowerCase();
  const openingReady = Boolean(selected.length && ["hook", "result", "scene", "comfort"].includes(firstFunction));
  const endingReady = Boolean(selected.length && ["close", "scene", "styling", "risk", "risk_remove", "comfort", "trust"].includes(lastFunction));
  return {
    ...overview,
    status: currentStatus,
    boundaryMessage: boundaryWarning ? String(fidelity.message || "内容边界影响了部分短句，请连读复核") : "",
    duplicates,
    openingLabel: fidelity.opening_affected ? "开场受内容边界影响" : (openingReady ? "开场已设置" : "开场待复核"),
    progressionLabel: duplicates ? `重复提示 ${duplicates}` : "未见完全重复",
    endingLabel: endingReady ? "结尾可连读" : "结尾待复核",
    overallLabel: currentStatus === "block" ? "暂不可成片" : (boundaryWarning ? "边界删减待复核" : (currentStatus === "warn" ? "建议调整" : "可继续审核")),
  };
}

function focusPreviewDirectorChapter(scope = "smart", chapterId = "") {
  const preview = getPreviewState(scope);
  if (!preview?.commercial_director_experiment || !chapterId) return;
  if (!state.previewDirectorChapterFocus) state.previewDirectorChapterFocus = { smart: "", mix: "" };
  if (!state.previewDirectorCandidateViews) state.previewDirectorCandidateViews = { smart: "recommended", mix: "recommended" };
  state.previewDirectorChapterFocus[scope] = String(chapterId);
  state.previewDirectorCandidateViews[scope] = "chapter";
  state.previewCandidateCategoryFilters[scope] = "all";
  state.previewCandidateSelections[scope] = null;
  const selected = previewWorkbenchSelectedClips(scope, preview);
  const first = selected.find(function (clip) { return String(clip?.director_chapter_id || "") === String(chapterId); });
  if (first) state.previewDetailSelection[scope] = Number(first.index);
  renderPreviewStateKeepStoryScroll(scope);
  setTimeout(function () {
    const box = previewBox(scope);
    const chapter = Array.from(box?.querySelectorAll("[data-preview-selected-chapter]") || []).find(function (item) { return item.dataset.previewSelectedChapter === String(chapterId); });
    chapter?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    if (first) ensureInlinePreviewVideo(scope, Number(first.index));
  }, 0);
}

function togglePreviewDirectorAlternatives(scope = "smart", force = null) {
  if (!state.previewDirectorAlternativesOpen) state.previewDirectorAlternativesOpen = { smart: false, mix: false };
  state.previewDirectorAlternativesOpen[scope] = typeof force === "boolean" ? force : !state.previewDirectorAlternativesOpen[scope];
  renderPreviewStateKeepStoryScroll(scope);
  if (state.previewDirectorAlternativesOpen[scope]) {
    setTimeout(function () { previewBox(scope)?.querySelector(".commerce-director-alternative-popover button")?.focus(); }, 0);
  }
}

function renderCommerceDirectorRecommendationCard(preview, duration = {}, scope = "smart") {
  if (!preview?.commercial_director_experiment && !preview?.commercial_director_preview) return "";
  const review = preview?.director_review || {};
  const proposals = Array.isArray(review?.director_strategy_library?.proposals)
    ? review.director_strategy_library.proposals
    : [];
  const stories = Array.isArray(review?.m1_story_library?.stories)
    ? review.m1_story_library.stories
    : [];
  const primary = proposals.find((item) => String(item?.director_plan_role || "") === "primary")
    || proposals.find((item) => !item?.requires_additional_ai_call)
    || proposals[0]
    || null;
  const story = review?.m1_story || {};
  if (!primary && !String(story?.thesis || story?.core_commercial_idea || "").trim()) return "";

  const primaryStory = stories.find((item) => String(item?.story_id || "") === String(primary?.primary_story_id || story?.strategy_id || "")) || {};
  const title = String(primary?.name || primaryStory?.angle || review?.headline || "AI \u63a8\u8350\u65b9\u6848").trim();
  const promise = String(primary?.why_this_plan || primaryStory?.purchase_reason || story?.core_commercial_idea || story?.thesis || "").trim();
  const openingPromise = String(primary?.opening_promise || primaryStory?.payoff || story?.payoff || "").trim();
  const structure = primary?.video_structure || {};
  const structureName = String(structure?.name || primary?.narrative_archetype || "AI 导演结构").trim();
  const outline = previewDirectorOutline(preview);
  const selected = previewWorkbenchSelectedClips(scope, preview);
  const status = previewDirectorCurrentStatus(scope, preview, `${scope}-target-duration`, selected, duration);
  const activeChapterId = previewDirectorActiveChapterId(scope, preview);
  const manual = previewDirectorHasManualEdits(scope, preview);
  const candidateStats = previewWorkbenchCandidateFilterStats(scope, preview);
  const targetSeconds = Number(preview?.target_duration || duration?.target || 0);
  const initialSeconds = Number(primary?.estimated_natural_duration || primaryStory?.natural_duration_seconds || duration?.rawTotal || duration?.projected || 0);
  const currentSeconds = Number(duration?.projected || 0);
  const durationCopy = currentSeconds > 0 ? `当前成片 ${currentSeconds.toFixed(1)}s` : "按真实原话计算";
  const targetCopy = targetSeconds > 0 ? `目标 ${targetSeconds.toFixed(0)}s` : "自然成片";
  const initialCopy = initialSeconds > 0 ? `AI 初稿 ${initialSeconds.toFixed(1)}s` : "";

  const chapterRows = outline.slice(0, 6).map((chapter, index) => {
    const id = String(chapter?.chapter_id || `chapter-${index + 1}`).trim();
    const goal = String(chapter?.goal || chapter?.purchase_value || `第 ${index + 1} 章`).trim();
    const detail = String(chapter?.purchase_value || chapter?.purchase_question || goal).trim();
    const active = id === activeChapterId;
    return `<li><button type="button" class="${active ? "is-active" : ""}" data-action="preview-director-chapter-focus" data-preview-scope="${scope}" data-chapter-id="${escapeHtml(id)}" aria-current="${active ? "step" : "false"}" title="${escapeHtml(detail)}"><span>${index + 1}</span><strong>${escapeHtml(goal)}</strong></button></li>`;
  }).join("");
  const moreChapters = outline.length > 6 ? `<span class="commerce-director-path-more">更多 ${outline.length - 6}</span>` : "";

  const alternatives = proposals.filter((item) => item !== primary && String(item?.director_plan_role || "") !== "primary");
  const alternativeRows = alternatives.map((item, index) => {
    const alternativeStory = stories.find((entry) => String(entry?.story_id || "") === String(item?.primary_story_id || "")) || {};
    // Alternative cards are direction-only by contract.  They intentionally
    // have no selected chapters or source assets until the user confirms a
    // fresh Story + Casting run, so an empty M1 asset view is not a failure.
    const unavailable = item?.available === false;
    const name = String(item?.name || alternativeStory?.angle || "\u5176\u4ed6\u5356\u6cd5").trim();
    const desire = String(item?.core_desire || item?.commercial_goal || alternativeStory?.purchase_reason || "").trim();
    const alternativeOpening = String(item?.opening_promise || alternativeStory?.payoff || "").trim();
    const alternativeStructure = String(item?.video_structure?.name || item?.narrative_archetype || "导演自定义结构").trim();
    const unavailableReason = String(item?.unavailable_reason || "当前素材无法支撑这个方向").trim();
    const action = unavailable
      ? `<span class="commerce-director-alt-state" title="${escapeHtml(unavailableReason)}">此方向不可用</span>`
      : `<button type="button" class="commerce-director-alt-action" data-action="select-commerce-director-strategy" data-preview-scope="${scope}" data-director-strategy-id="${escapeHtml(item?.director_strategy_id || "")}" data-additional-ai-call="true">生成此方向 <small>需 2 次 AI</small></button>`;
    return `<article class="commerce-director-alt-card ${unavailable ? "is-unavailable" : ""}">
      <header><span>备选 ${index + 1}</span><em>${escapeHtml(alternativeStructure)}</em></header>
      <strong>${escapeHtml(name)}</strong>
      ${desire ? `<p><b>核心购买理由</b>${escapeHtml(desire)}</p>` : ""}
      ${alternativeOpening ? `<p><b>开场方向</b>${escapeHtml(alternativeOpening)}</p>` : ""}
      <footer>${action}</footer>
    </article>`;
  }).join("");
  const alternativesOpen = Boolean(state.previewDirectorAlternativesOpen?.[scope]);
  const statusSource = manual ? "当前编排检查" : "AI 初稿评价";
  const statusTone = status.status === "block" ? "block" : (status.status === "warn" ? "warn" : "ok");

  return `<section class="commerce-director-recommendation is-${statusTone}" aria-label="AI 推荐方案">
    <div class="commerce-director-thesis-row">
      <div class="commerce-director-thesis-copy"><span class="commerce-director-recommendation-badge">AI 导演推荐</span><h3>${escapeHtml(title)}</h3><span class="commerce-director-structure-pill">${escapeHtml(structureName)}</span><p>${escapeHtml(promise || title)}</p></div>
      <div class="commerce-director-thesis-actions"><span><strong>${escapeHtml(durationCopy)}</strong><small>${escapeHtml([targetCopy, initialCopy].filter(Boolean).join(" · "))}</small></span>${alternativeRows ? `<button type="button" class="commerce-director-alternatives-toggle" data-action="preview-director-alternatives-toggle" data-preview-scope="${scope}" aria-expanded="${alternativesOpen ? "true" : "false"}">其他导演方向 ${alternatives.length}</button>` : ""}</div>
    </div>
    ${chapterRows ? `<nav class="commerce-director-recommendation-path" aria-label="说服路径"><span>说服路径</span><ol>${chapterRows}</ol>${moreChapters}</nav>` : ""}
    <div class="commerce-director-status-row" role="status"><span class="commerce-director-opening"><b>开场承诺</b>${escapeHtml(openingPromise || "尚未形成明确开场承诺")}</span><div class="commerce-director-status-metrics"><span>可编辑候选约 ${Number(candidateStats.duration || 0).toFixed(1)}s</span><span>已选 ${selected.length} 段</span><span>${escapeHtml(status.progressionLabel)}</span><span>${escapeHtml(status.openingLabel)}</span><span>${escapeHtml(status.endingLabel)}</span></div><span class="commerce-director-status-badge is-${statusTone}"><small>${escapeHtml(statusSource)}</small>${escapeHtml(status.overallLabel)}</span></div>
    ${status.boundaryMessage ? `<p class="commerce-director-status-row" role="alert">${escapeHtml(status.boundaryMessage)}</p>` : ""}
    <span class="visually-hidden" aria-live="polite">当前查看 ${escapeHtml(outline.find(function (item) { return String(item?.chapter_id || "") === activeChapterId; })?.goal || "导演方案")}</span>
    ${alternativesOpen && alternativeRows ? `<aside class="commerce-director-alternative-popover" role="dialog" aria-label="其他导演方向"><header><div><strong>其他导演方向</strong><small>仅展示方向摘要；选择后才会重新调用 AI</small></div><button type="button" data-action="preview-director-alternatives-close" data-preview-scope="${scope}" aria-label="关闭其他导演方向">关闭</button></header><div>${alternativeRows}</div></aside>` : ""}
  </section>`;
}

function togglePreviewOverviewDetails(scope = "smart") {
  const box = $(scope === "mix" ? "mix-preview" : "smart-preview");
  const details = box?.querySelector(`[data-preview-film-details="${scope}"]`);
  if (!details) return;
  details.open = true;
  details.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function locatePreviewOverviewIssue(scope = "smart", index) {
  if (!Number.isInteger(index)) return;
  setPreviewDetailSelection(scope, index);
  const box = $(scope === "mix" ? "mix-preview" : "smart-preview");
  const row = box?.querySelector(`[data-preview-row][data-preview-index="${index}"]`);
  row?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  ensureInlinePreviewVideo(scope, index);
}

function renderPreviewWorkbench(scope, preview, targetId) {
  preview = hydratePreviewCandidatePool(preview);
  ensurePreviewDraft(scope);
  const selected = previewWorkbenchSelectedClips(scope, preview);
  const current = previewWorkbenchCurrentClip(scope, preview, selected);
  const duration = previewDurationFitState(scope, preview, targetId);
  const candidateStats = previewWorkbenchCandidateFilterStats(scope, preview);
  const isDirectorPreview = Boolean(preview?.commercial_director_experiment);
  const candidateGuide = isDirectorPreview ? "完整显示原句；可点加入，或按住左侧拖到右边" : "\u5b8c\u6574\u663e\u793a\u539f\u53e5\uff1b\u53ef\u70b9\u52a0\u5165\uff0c\u6216\u6309\u4f4f\u5de6\u4fa7\u62d6\u5230\u53f3\u8fb9";
  const selectedGuide = isDirectorPreview ? "按导演章节审阅，可拖拽调整顺序" : "\u6309\u6545\u4e8b\u987a\u5e8f\u8fde\u7eed\u9605\u8bfb";
  const candidateHead = '<div class="preview-workbench-column-head"><div><strong>\u5019\u9009\u7247\u6bb5</strong><span>' + candidateGuide + '</span></div><small>' + (isDirectorPreview ? candidateStats.all : candidateStats.filtered + ' / ' + candidateStats.source) + ' \u6bb5</small></div>';
  const durationClass = duration.accepted ? "is-ok" : "is-warn";
  const selectedHead = '<div class="preview-workbench-column-head"><div><strong>' + (isDirectorPreview ? '已选片段（故事脚本）' : '\u5df2\u9009\u7247\u6bb5') + '</strong><span>' + selectedGuide + '</span></div><div class="preview-duration-control"><small class="' + durationClass + '" title="\u539f\u7247\u5408\u8ba1 ' + duration.rawTotal.toFixed(1) + 's\uff0c\u6309 ' + duration.speed.toFixed(2) + 'x \u9884\u8ba1\u53d8\u901f\u6298\u7b97">' + selected.length + ' \u6bb5 \u00b7 \u9884\u8ba1 ' + duration.projected.toFixed(1) + 's<br>\u5141\u8bb8 ' + duration.low.toFixed(0) + '-' + duration.high.toFixed(0) + 's</small><button class="button button-muted button-small" data-action="preview-duration-fit" data-preview-scope="' + scope + '">\u9002\u914d\u65f6\u957f</button></div></div>';
  const overview = isDirectorPreview
    ? renderCommerceDirectorRecommendationCard(preview, duration, scope)
    : renderPreviewFilmOverview(scope, preview, targetId, selected, duration);
  return overview + '<div class="preview-selection-workbench preview-workbench-unified" data-preview-workbench="' + scope + '" data-preview-workbench-focus="' + scope + '" tabindex="0"><aside class="preview-candidate-sidebar">' + candidateHead + renderPreviewCandidateFilterBar(scope, preview) + '<div class="preview-candidate-list">' + renderPreviewCandidateGroups(scope, preview) + '</div></aside><main class="preview-workbench-main">' + renderPreviewWorkbenchVideoStage(scope, preview, current) + renderPreviewSentenceEditor(scope, current) + '</main><aside class="preview-selected-sidebar">' + selectedHead + '<div class="preview-selected-list" data-preview-candidate-drop-zone data-preview-scope="' + scope + '">' + renderPreviewSelectedRows(scope, selected) + '</div></aside></div>';
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
