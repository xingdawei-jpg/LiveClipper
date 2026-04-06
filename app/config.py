# ============================================================
# 配置文件 v8.0：抖音女装带货爆款逻辑（3必选+2可选灵活链路 + 爆款规律增强）
# ============================================================

# 一、片段类型 → 关键词分级（7种类型）
CLIP_KEYWORDS = {
    "hook": {
        "keywords": [
            # 爆料型(最强停留)
            "假的", "行业秘密", "全都是假的", "不敢信",
            # 痛点型(精准锁客)
            "显腿长", "藏肉", "遮胯宽", "微胖友好", "小个子",
            "显瘦", "显白", "显高", "不挑人", "腿粗",
            # 信任型(强信任)
            "盲拍", "不搞虚", "精挑细选", "闭眼入",
            # 夸奖型(情绪感染)
            "绝了", "天花板", "被扣爆", "卖疯了", "巨好看", "抢疯了", "秒空",
            # 背书型(品味暗示)
            "秀场款", "大牌同款", "平替",
        ],
        "weight": 35,
        "description": "开头抓人(爆料/痛点/信任/夸奖/背书)",
    },
    "product": {
        "keywords": [
            # 痛点解决(最强转化)
            "绝对可以穿", "也能穿", "穿得进去", "不用担心", "放心",
            # 对比突出(差异化种草)
            "比市面上", "市面版本", "市面上的", "外面卖", "实体店", "专柜",
            # 版型/面料/细节
            "面料", "垂感", "防透", "内衬", "不起球", "不掉色",
            "高腰线", "开叉", "弹力", "修身", "松紧腰", "包容性",
            "材质", "手感", "亲肤", "透气", "抗皱", "软糯",
            "遮胯", "藏肚子", "做工", "走线", "高级感", "质感",
            "上身", "版型", "刺绣", "蕾丝",
            "真皮", "皮质", "皮", "水洗", "定制", "工艺",
            "收腰", "显白", "显高", "弹", "薄", "厚", "轻", "重",
            # 穿搭展示
            "搭牛仔", "搭裙子", "百搭", "实穿", "内搭", "外穿",
            "通勤", "约会", "度假", "日常",
            # 价格锚点
            "块钱", "元", "99", "199", "229", "159", "379",
            "性价比", "划算", "超值",
        ],
        "weight": 25,
        "description": "产品种草(痛点解决/对比/版型/面料/穿搭/价格锚点)",
    },
    "close": {
        "keywords": [
            # 尺码推荐
            "尺码", "卡码", "往小拍", "往大拍", "推荐尺码",
            # 信任强化
            "一定是真的", "精挑细选", "不搞虚",
            # 风格定位
            "洋气", "高级", "韩剧", "欧尼", "气质",
            # 促单指令
            "321", "上车", "冲", "抢", "入手", "下单",
            "直接拍", "链接", "购物车", "小黄车", "左下角",
            # 优惠
            "到手价", "到手", "领券", "立减", "满减", "破价",
            "福利价", "秒杀价",
        ],
        "weight": 35,
        "description": "促单收尾(尺码/信任/风格/促单/优惠)",
    },
    "bridge": {
        "keywords": [
            # 科普过渡
            "教你", "辨别", "怎么看", "区别", "真假",
            "为什么", "原因是", "是因为",
        ],
        "weight": 15,
        "description": "过渡衔接(科普/提问，可选)",
    },
    "trend": {
        "keywords": [
            "流行色", "当季", "新款", "复古风", "韩系", "法式",
            "设计款", "原创", "不撞款", "独家",
            "今年流行", "今年最火", "秀场",
        ],
        "weight": 10,
        "description": "流行趋势(可选)",
    },
}

# 黄金链路（新结构：3必选 + 2可选）
CLIP_ORDER = [
    "hook", "bridge", "product", "close", "trend",
]

# 兜底用简单链路
SIMPLE_CHAIN = ["hook", "product", "close"]


# 黄金链路（7环节，严格顺序）
CLIP_ORDER = [
    "hook", "bridge", "product", "close", "trend",
]

# ============================================================
# 二、前置数据清洗（OpenClaw 执行，绝不喂给模型）
# ============================================================

# 禁止进入候选池的内容（仅绝对垃圾，不过杀）
BAN_PATTERNS = [
    r"再开新款", r"过下一个", r"过款", r"接下来", r"好吧",
    r"就这样", r"我先占", r"你们等一下", r"等一下",
    r"感谢关注", r"喝水", r"点关注",
    r"\d+[件条套个]",  # 纯数量描述如 "3件"、"5条"、"2套"
    r"福利", r"直播间", r"号链接", r"链接",
    r"关注一下", r"点关注",
]

# 淘汰级关键词（仅短文本时负分）
NEGATIVE_KEYWORDS = [
    "好看", "不错", "舒服", "挺好", "网红款",
]

# 废话填充词（清理用，不作为过滤条件）
FILLER_WORDS = [
    "啊", "呃", "嗯", "那个", "就是", "然后", "那个啥",
    "对吧", "是吧", "所以说", "你知道吗", "我跟你说",
    "这样子的", "的话",

    "呕", "嗯", "对不对", "对吧", "然后",
]

# 负面信号
NEGATIVE_SIGNALS = [
    "是不是", "能不能", "好不好", "会不会", "对不对",
    "不知道", "不清楚", "抱歉", "不好意思", "稍等", "卡了",
    "听不见", "看不到", "断线",
    "倒计时", "321", "上链接", "原价", "成交价", "特惠",
    "截图抽奖", "下播前", "马上要下",
]

NEGATION_WORDS = ["不", "没", "别", "不是", "不会", "不能", "没有"]

# 情绪加分词
EMOTION_WORDS = {
    "exclaim": ["！", "!", "天呐", "哇", "绝了", "太", "超", "超级", "真"],
    "contrast": ["别家没有", "只有我家", "独款", "独家"],
    "action": ["抢", "冲", "闭眼入", "直接拍", "入手", "上车"],
}

# 违禁词替换表
PROHIBITED_WORDS = {
    "最": "爆款", "顶级": "人气款", "唯一": "专属款",
    "减肥": "修饰身形", "瘦腿": "修饰腿型", "塑形": "贴合身形",
    "100%纯羊绒": "羊绒质感", "纯羊毛": "羊毛混纺", "纯棉": "棉感面料",
    "不跑绒": "不易跑绒", "不掉色": "不易掉色", "不起球": "不易起球",
    "跳楼价": "限时特惠", "亏本甩卖": "福利价", "全网最低": "到手价",
    "必买": "热卖推荐", "必囤": "人气款",
}

# 候选池准入门槛（松紧平衡，不过杀）
CANDIDATE_MIN_LENGTH = 5    # 字数≥5（带货口语5字就够）
CANDIDATE_MIN_DURATION = 1.5  # 时长≥1.5秒
CANDIDATE_MAX_DURATION = 12   # 时长≤12秒


# 违禁词(GUI关键词管理用, 命中则移除片段)
FORBIDDEN_PHRASES = [
    "再开新款", "过下一个", "过款", "接下来",
    "好吧", "就这样", "我先占", "你们等一下", "等一下",
    "感谢关注", "喝水", "点关注",
]
# ============================================================
# 三、文案优化配置
# ============================================================
TEXT_OPTIMIZATION = {
    "max_length": 45,
    "min_length": 5,
    "end_punctuation": "！",
    "remove_patterns": [r"^那个", r"^就是", r"^然后", r"^嗯", r"^啊"],
}

# ============================================================
# 四、视频参数
# ============================================================
VIDEO_CONFIG = {
    "resolution": "1080:1920", "fps": 30,
    "bitrate_v": "5M", "bitrate_a": "192k",
    "codec_v": "libx264", "preset": "fast",
    "codec_a": "aac", "format": "mp4",
}

import os, sys, shutil
def _find_ffmpeg():
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    local = os.path.join(base, "ffmpeg", "ffmpeg.exe")
    if os.path.exists(local):
        return local
    # PyInstaller bundled: _internal/ffmpeg/ffmpeg.exe
    internal = os.path.join(base, "_internal", "ffmpeg", "ffmpeg.exe")
    if os.path.exists(internal):
        return internal
    found = shutil.which("ffmpeg")
    if found:
        return found
    for p in [r"C:\ffmpeg\bin\ffmpeg.exe", r"D:\ffmpeg\bin\ffmpeg.exe"]:
        if os.path.exists(p):
            return p
    return None

FFMPEG_PATH = _find_ffmpeg()

# ============================================================
# 五、视频去重策略
# ============================================================
DEDUP_CONFIG = {
    # === 增强版去重配置 v2.0 ===
    # 核心策略: 镜像 + 随机变速(保音调) + 随机微裁剪 + 音频微pitch
    "random_count": 1,  # 不再用随机选择，改用固定增强链路
    "strategy": "enhanced",  # enhanced = 增强版全链路

    # 镜像配置
    "mirror": {
        "enabled": True,
        "type": "horizontal",  # 水平镜像（保护女装展示，禁用垂直）
    },

    # 随机变速配置（保音调核心）
    "variable_speed": {
        "enabled": True,
        "min_rate": 1.10,
        "max_rate": 1.30,
        "decimal_precision": 2,
        # 加权随机：1.1-1.2x 占70%（人声自然），1.2-1.3x 占30%（强效去重）
        "weight_low": 0.7,   # 1.1~1.2x 概率
        "weight_high": 0.3,  # 1.2~1.3x 概率
        "audio_pitch_lock": False,  # 不保音调，用 atempo 纯变速（音质好）
        "fallback_speed": 1.15,    # 异常兜底速率
    },

    # 随机微裁剪（3%以内，不碰主体）
    "random_crop": {
        "enabled": True,
        "crop_min": 0.97,
        "crop_max": 0.99,
        "offset_min": 0.005,
        "offset_max": 0.015,
    },

    # 音频微pitch（增强音纹去重）
    "audio_pitch": {
        "enabled": True,
        "min_pitch": -1.5,  # 音高微降1.5%
        "max_pitch": 2.0,   # 音高微升2.0%
    },

    # === 新增去重方法 v2.1 ===
    # 等级1：轻量消重（0画质损失）
    "gamma_shift": {
        "enabled": True,    # 伽马微调
        "range": (-0.02, 0.02),  # ±0.02 肉眼看不出
    },

    # 等级2：中阶消重
    "corner_mask": {
        "enabled": True,    # 四角微遮罩
        "size_pct": 0.005,  # 0.5% 角标大小
        "color": "0x000000",  # 黑色（也可以用画面主色）
    },
    "audio_reverb": {
        "enabled": True,    # 极轻微混响改变音纹
        "probability": 0.5, # 50%概率启用
    },

    # 等级3：高阶消重
    "noise_fusion": {
        "enabled": True,    # 双音轨融合（原音+极轻白噪音）
        "noise_volume": 0.001,  # 噪音音量（极轻）
        "probability": 0.4, # 40%概率启用
    },
    "frame_interpolation": {
        "enabled": False,   # 单帧插值（minterpolate很慢，默认关闭）
        "mode": "blend",    # blend模式无卡顿
        "probability": 0.3,
    },

    # 原有方法保留但默认关闭（enhanced模式不使用）
    "methods": {
        "speed_change": {"enabled": False},
        "zoom_crop": {"enabled": False},
        "mirror": {"enabled": False},
        "frame_drop": {"enabled": False},
        "color_shift": {"enabled": False},
        "rotation": {"enabled": False},
        "noise": {"enabled": False},
        "pixel_shift": {"enabled": False},
        "edge_blur": {"enabled": False},
        "audio_pitch_old": {"enabled": False},
    },
}
DEDUP_PRESET = "medium"

# ============================================================
# 六、字幕叠加配置
# ============================================================
SUBTITLE_OVERLAY = {
    "enabled": True,
    "font_name": None,  # 运行时从 platform_config 获取
    "font_size": 52,
    "font_color": "&H00FFFFFF",
    "outline_color": "&H00000000",
    "outline_width": 0,
    "position": "bottom",
    "margin_v": 270,
    "keyword_font_size": 112,
    "keyword_font_color": "&H0000FFFF",
    "keyword_bold": True,
}

SUBTITLE_KEYWORDS = [
    "99", "199", "299", "229", "159", "399", "49", "69", "129", "189", "259",
    "元", "块", "到手", "价格", "领券", "优惠", "省",
    "绝了", "太好", "超好看", "惊艳", "漂亮", "高级", "质感",
    "爆款", "必入", "闭眼入", "冲", "绝美", "无敌", "天花板",
    "没货", "抢", "卖光", "后悔", "断码", "321", "库存",
    "面料", "垂感", "不起球", "高腰", "显瘦", "弹力",
]

# ============================================================
# 七、时长目标
# ============================================================
TARGET_DURATION = 60
TARGET_DURATION_TOLERANCE = 10
CLIP_DURATION_RANGE = (2, 10)
MIN_TOTAL_CLIPS = 7
REQUIRED_CLIP_TYPES = ["hook", "close"]
TIME_WINDOW_MINUTES = 8
