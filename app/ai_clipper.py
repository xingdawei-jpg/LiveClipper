# -*- coding: utf-8 -*-
"""
AI 智能选片模块 v5.0 - 抖音女装带货爆款逻辑
前置清洗(三级兜底降级)+ 强制数量约束 + temperature=0.1
"""

import json
import os
import sys
import ssl
import urllib.request
import urllib.error
import re


# 多版本全量选片模式：设为True时跳过偏好限定
_skip_focus = False

def _friendly_http(code, err=""):
    """翻译 HTTP 错误码为用户友好提示"""
    code = int(code) if isinstance(code, (int, str)) and str(code).isdigit() else 0
    if code == 401:
        return "API Key 无效或已过期，请检查设置"
    elif code == 402:
        return "API 余额不足，请充值后重试"
    elif code == 429:
        return "请求太频繁，请稍后再试"
    elif code == 404:
        return "接口地址错误，请检查 Base URL 设置"
    elif code == 500 or code == 502 or code == 503:
        return "AI 服务器暂时不可用，请稍后再试"
    elif code == 413:
        return "发送内容过长，请缩短视频后重试"
    else:
        return f"请检查网络和API设置（错误码:{code}）"

def _friendly_msg(err_str):
    """翻译常见错误信息"""
    s = err_str.lower()
    if "timeout" in s or "timed out" in s:
        return "网络连接超时，请检查网络后重试"
    if "connection" in s or "connect" in s or "winerror" in s:
        return "网络连接失败，请检查网络设置"
    if "api" in s and "key" in s:
        return "API Key 无效，请检查设置"
    return err_str[:80]




# ============================================================
# 设置管理
# ============================================================
def _get_base_path():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def load_settings():
    path = os.path.join(_get_base_path(), "ai_settings.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return _default_settings()

def save_settings(settings):
    path = os.path.join(_get_base_path(), "ai_settings.json")
    try:
        existing = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                existing = json.load(f)
        existing.update(settings)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        import traceback
        traceback.print_exc()
        return False

def _default_settings():
    return {
        "api_key": "", "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-1-5-pro-32k-250115", "enabled": False,
    }


# ============================================================
# 黄金链路(3必选+2可选)
# ============================================================
GOLDEN_CHAIN = [
    "hook", "bridge", "product", "close", "trend",
]
SIMPLE_CHAIN = ["hook", "product", "close"]

# ============================================================
# ASR 常见错误修正字典(持续补充)
# ============================================================
ASR_CORRECTIONS = {
    # 语音混淆(发音相近导致的误识别)
    "惊恐": "惊艳",
    "惊吓": "惊艳",
    "猩红": "心动",
    "惊呆": "惊艳",
    "恐怖": "好看",  # 上下文依赖，保守替换
    # 面料相关
    "沙洗棉": "砂洗棉",
    "纱洗": "砂洗",
    "可沙洗": "可砂洗",
    # 常见口语误识别
    "二十一": "21",
    "二一": "21",
    "上链": "上链接",
    "小黄": "小黄车",
    # 尺码相关
    "码子": "码",
    # 数字误识别
    "一百九十": "190",
    "三百七十九": "379",
    "三百七": "370",
    "裙长80": "裙长84",
    "裙长 80": "裙长84",
    "衣长一百一": "衣长110",
}

# 主播回弹幕的废话模式(短句 + 否定/确认 + 无产品信息)
HOST_CHAT_PATTERNS = [
    re.compile(r"^(没有的|没有的事|没有啊|不是的|不是啊|不是的啊)$"),
    re.compile(r"^(知道|知道了|好的|好的呀|对对对|是是是)$"),
    re.compile(r"^(没错|没毛病|没毛病吧)$"),
    re.compile(r"^(可以的|可以的呀|行|行的)$"),
    re.compile(r"^(哈哈|哈哈哈|嘿嘿)$"),
    re.compile(r"^(谢谢|谢谢宝宝|谢谢姐妹)$"),
    re.compile(r"^(等一下|稍等|等一等的)$"),
    re.compile(r"^(看一下|我看看|看一下啊)$"),
    re.compile(r"^(好了吗|好了没|可以了)$"),
    re.compile(r"^(是的来|好吧不说了|来吧)$"),
    re.compile(r"^(那就是没有|没有现货哦)$"),
    re.compile(r"^(可以吗|对吗|好吗|是吧|啊对|啊是|嗯对)$"),
    re.compile(r"^(来|这个|那个|这些|那些|什么)$"),
    re.compile(r"^(对|嗯|啊|哦|诶|嘿|好)$"),
    re.compile(r"^(然后呢|接下来呢|所以说|因为这个)$"),
    re.compile(r"^(真的吗|真的啊|真的假的)$"),
    re.compile(r"^(你说呢|你觉得呢|懂吧|懂了吧)$"),
    re.compile(r"^(差不多|差不多吧|基本上|基本上吧)$"),
    re.compile(r"^(先这样|那就这样|就这样吧)$"),
    re.compile(r"^(我现在|我刚才|我之前|我到时候)$"),
    re.compile(r"^(你知道的|你懂的|我你懂得)$"),
    re.compile(r"^(大刘|小刘|潘桂丽|文静姐)$"),
    re.compile(r"^(姐妹\d*单现货|姐妹\d*单)$"),
    re.compile(r"^(姐妹们|姐妹\d*人)$"),
    re.compile(r"^(可以的|好的呀|行吧|好嘞)$"),
    re.compile(r"^(来来来|冲冲冲|拍拍拍)$"),
    re.compile(r"^(对不对|是不是|好不好|行不行)$"),
    re.compile(r"^(就这么说|就这么定)$"),
    re.compile(r"^(看一下|看一下哈)$"),
    re.compile(r"^(你要的话|你要的话)$"),
    re.compile(r"^(不废话了|不说了)$"),
]


# ============================================================
# 前置数据清洗(三级兜底降级，不过杀)
# ============================================================
def _pre_clean_srt(srt_text, log_fn=None):
    """
    三级兜底降级清洗:
    - 先用标准规则过滤
    - 如果通过数 < 20 条，自动放宽(取消字数限制，放宽时长)
    - 如果还 < 10 条，仅过滤黑名单过渡废话
    """
    def _log(msg):
        if log_fn: log_fn(msg)

    from config import BAN_PATTERNS, NEGATIVE_SIGNALS, FILLER_WORDS

    def _parse_and_filter(lines, min_dur, max_dur, min_len, filter_level):
        """解析 SRT 并按给定阈值过滤"""
        entries = []
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if re.match(r'^\d+$', line):
                if i + 2 < len(lines):
                    time_line = lines[i + 1].strip()
                    text_line = lines[i + 2].strip() if i + 2 < len(lines) else ""
                    j = i + 3
                    while j < len(lines) and lines[j].strip() and not re.match(r'^\d+$', lines[j].strip()):
                        text_line += lines[j].strip()
                        j += 1
                    time_match = re.match(
                        r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})',
                        time_line)
                    if time_match:
                        start_s = int(time_match.group(1))*3600 + int(time_match.group(2))*60 + int(time_match.group(3)) + int(time_match.group(4))/1000.0
                        end_s = int(time_match.group(5))*3600 + int(time_match.group(6))*60 + int(time_match.group(7)) + int(time_match.group(8))/1000.0
                        duration = end_s - start_s

                        skip = False
                        # 时长过滤
                        if duration < min_dur or duration > max_dur:
                            skip = True
                        # 字数过滤
                        clean_text = text_line
                        for fw in FILLER_WORDS:
                            clean_text = clean_text.replace(fw, "")
                        clean_text = clean_text.strip()
                        if len(clean_text) < min_len:
                            skip = True
                        # 黑名单(仅过渡废话)
                        if not skip:
                            for ban in BAN_PATTERNS:
                                if re.search(ban, text_line):
                                    skip = True
                                    break
                        # 负面信号(仅标准/宽松模式)
                        if not skip and filter_level >= 1:
                            for sig in NEGATIVE_SIGNALS:
                                if sig in text_line:
                                    skip = True
                                    break
                        if not skip:
                            entries.append((text_line, start_s, end_s, duration))
                    i = j
                    continue
            i += 1
        return entries

    all_lines = srt_text.strip().split("\n")

    # 标准模式
    entries = _parse_and_filter(all_lines, 1.5, 12, 5, 2)
    _log(f"前置清洗(标准): {len(entries)} 条通过")

    # 一级降级:取消字数限制，放宽时长
    if len(entries) < 20:
        entries = _parse_and_filter(all_lines, 1.0, 15, 3, 1)
        _log(f"一级降级(放宽字数+时长): {len(entries)} 条通过")

    # 二级降级:仅过滤黑名单
    if len(entries) < 10:
        entries = _parse_and_filter(all_lines, 0.5, 20, 3, 0)
        _log(f"二级降级(仅黑名单): {len(entries)} 条通过")

    # 相邻片段重叠检测：Whisper medium 会在片段边界重复识别
    if entries:
        merged = []
        for entry in entries:
            if not merged:
                merged.append(entry)
                continue
            prev_text, prev_start, prev_end, prev_dur = merged[-1]
            curr_text, curr_start, curr_end, curr_dur = entry
            prev_clean = prev_text.replace(" ", "").replace("\u3000", "")
            curr_clean = curr_text.replace(" ", "").replace("\u3000", "")
            should_merge = False
            if prev_clean and curr_clean:
                # 时间重叠：前片段还没结束，后片段已开始
                time_overlap = max(0, prev_end - curr_start) / min(prev_dur, curr_dur) if min(prev_dur, curr_dur) > 0 else 0
                # 找最长公共子串
                overlap_chars = 0
                shorter = prev_clean if len(prev_clean) <= len(curr_clean) else curr_clean
                longer = curr_clean if len(prev_clean) <= len(curr_clean) else prev_clean
                for length in range(min(len(shorter), 10), 2, -1):
                    for si in range(len(shorter) - length + 1):
                        sub = shorter[si:si+length]
                        if sub in longer:
                            overlap_chars = length
                            break
                    if overlap_chars > 0:
                        break
                # Whisper边界重复特征：时间交叀 + 少量文本重叠
                should_merge = (time_overlap > 0.3 and overlap_chars >= 3)
            if should_merge:
                new_start = min(prev_start, curr_start)
                new_end = max(prev_end, curr_end)
                # Merge text: remove overlapping part from curr, then append
                if overlap_chars >= 3:
                    # Find the overlapping tail of prev and head of curr
                    overlap_str = ""
                    for length in range(min(len(prev_clean), overlap_chars + 2), max(overlap_chars - 1, 2), -1):
                        tail = prev_clean[-length:]
                        if tail in curr_clean[:length + 2]:
                            overlap_str = tail
                            break
                    if overlap_str:
                        idx = curr_clean.find(overlap_str)
                        new_text = prev_text + curr_text[idx + len(overlap_str):]
                    else:
                        new_text = prev_text
                else:
                    new_text = prev_text
                merged[-1] = (new_text, new_start, new_end, new_end - new_start)
            else:
                merged.append(entry)
        if len(merged) < len(entries):
            import builtins
            builtins._merge_count = len(entries) - len(merged)
        entries = merged

    # 重建 SRT
    output = []
    for text, start_s, end_s, dur in entries:
        h1, m1, s1, ms1 = int(start_s//3600), int((start_s%3600)//60), int(start_s%60), int((start_s%1)*1000)
        h2, m2, s2, ms2 = int(end_s//3600), int((end_s%3600)//60), int(end_s%60), int((end_s%1)*1000)
        output.append(f"{h1:02d}:{m1:02d}:{s1:02d},{ms1:03d} --> {h2:02d}:{m2:02d}:{s2:02d},{ms2:03d}")
        output.append(text)
        output.append("")

    return "\n".join(output)


# ============================================================
# 强制数量约束 Prompt
# ============================================================
SYSTEM_PROMPT = """你是抖音女装带货短视频专业编导，严格执行以下规则，禁止自由发挥.

[零,推理步骤(输出JSON前必须先完成)]
1. 品类统计：列出本场出现过的品类及其出现次数，确定主打单品
2. Hook扫描：遍历全片字幕，标注所有可能做Hook的句子及类型(圈人群/极端表态/痛点/爆料/夸奖信任)，选出冲击力最强的
3. 链路规划：确定 hook→product→close 的片段数量分配方案
4. 重复排查：列出候选片段中信息重叠的组，标注保留哪条及理由
5. 完成以上推理后再输出JSON

[一,品类一致性(最重要)]
1. 先通读所有字幕，判断本场直播有哪几个品类(如裤子,上衣,裙子等)
2. 选择出现次数最多的品类作为"主打单品"
3. 所有片段必须围绕同一个主打单品，禁止混入其他品类的内容
4. 即使其他品类的文案再好，也不能选

[二,数量与时长]
1. 必须输出[12-20段]片段，绝对不能少于10段
2. 总时长严格控制45-65秒，超出65秒必须删除冗余段落
3. 如果主打单品的同类型好片段不够，宁可重复不同角度的卖点，也不要混入其他品类

[三,黄金链路结构]
采用"必选+可选"灵活组合模式：

【必选环节】（必须覆盖，缺一不可）
1. Hook(开头抓人): ★★★ 最关键环节，决定用户是否划走 ★★★
   - 必须是独立完整的一句话，能在3秒内传达一个强烈信息
   - 五类高价值Hook(优先级从高到低):
     ① 圈人群型(最强Hook): "屁股大的看过来"、"腿粗的看过来"、"110斤显瘦80斤"、"肩宽背厚的"、"小个子也能穿"、"胯宽的姐妹" → 精准锁定目标人群，转化最高
     ② 极端表态型(强好奇): "吃土都要买"、"经典中的经典"、"必入"、"闭眼入"、"不买后悔" → 极端判断制造好奇
     ③ 痛点型(精准锁客): "显瘦"、"显白"、"腿粗也能穿"、"想穿XX" → 直击需求
     ④ 爆料型(强停留): "XX%是假的"、"行业秘密"、"全都是假的" → 制造好奇
     ⑤ 夸奖+信任型(情绪感染): "被扣爆了"、"卖疯了"、"盲拍"、"不搞虚" → 激发兴趣+拉信任
   - ★绝对禁止★:用产品/面料/款式做Hook开场(如"这个西装"、"这件风衣"、"面料很好")——人群圈定比产品介绍重要
   - ★绝对禁止★:话头接续句("就像我的话...","然后...","所以呢...","看一下...","来...","好...")，不完整半句，平淡开场
   - ★Hook最低标准★:如果这句话单独出现在抖音信息流，用户会不会停留?不会就换一条
   - ★必须搜遍全片选最强Hook★:不要偷懒选时间最早的第一句，要在整个字幕中找出冲击力最强的那句
   - ★Hook选片铁律★:
     a) 直播开头80%是暖场废话，不要优先选时间最早的第一句
     b) 允许合并相邻2-3句字幕提炼成一句更有冲击力的Hook
     c) 好Hook vs 坏Hook对比:
        好:"屁股大的看过来"(圈人群) 好:"110斤显瘦80斤"(圈人群+效果) 好:"吃土都要买一条"(极端表态)
        坏:"这个西装真的很好看"(产品开场) 坏:"然后这一整身"(接续句) 坏:"面料很舒服"(无钩子)

2. Product(产品种草): 选择3-5个片段，全链路消除用户购买顾虑
   核心手段:穿搭介绍+性价比对比+效果对比，三种方式交替使用效果最佳
   优先级从高到低(不强求每个类型都有):
   ① 价值锚点(最强种草，爆款必选): "原版16000→我们300来块"、"外面卖七八千"、"同款XX块钱" → 先锚定高价再给我们的价格
   ② 代工厂/产地背书(强信任): "一线品牌代工厂"、"给XX做婚纱的工厂"、"意大利工艺" → 信任感拉满
   ③ 痛点解决(强转化): "腿粗绝对可以穿进去"、"120斤也能穿" → 直接回答用户顾虑
   ④ 对比突出(差异化): "比市面上的加宽"、"市面版本拉不开" → 突出优势
   ⑤ 版型/面料/细节: 讲解设计、材质、做工 → 建立产品认知
   ⑥ 场景想象(画面感): "法国女生的浪漫感"、"办公室喝茶的雅" → 感觉比参数更打动人
   ⑦ 穿搭展示: 多种风格搭配、跨季节可穿 → 证明百搭实穿性

3. Close(促单收尾): 选择1-2个片段，核心是引导下单
   - ★尺码引导只能放在Close区域(最后1-2段)★
   - 促单(最有效): "闭眼冲"、"剩下不多"、"300件拍完就没有了" → 紧迫感引导下单
   - 闭眼入(强信心): "闭眼入"、"不买后悔" → 极致断言
   - 尺码推荐: "按推荐尺码买就行"、"卡码买小不要买大" → 消除尺码顾虑
   - 信任强化: "一定是真的"、"没人舍得退" → 最终信任确认
   - ★价格/购物车/链接内容绝对禁止选择★
   - 如果原视频有尺码引导，必须放在最后

【可选环节】（有就保留，没有不强求）
- Bridge(过渡衔接): 科普类、提问类 → 连接Hook和Product
- 信任话术: "盲拍"、"不搞虚"、"自留款" → 可穿插任意环节，不要独立成段
- trend(流行趋势): 当季流行、设计款 → 有则保留

[四,片段时长规范]
1. 总共选8-12个片段，不要选太多碎片
2. hook: 4-6秒(核心冲击句+必要上下文)，必须第一个输出
3. product: 5-8秒(1-2句完整的卖点句，允许包含1句必要的铺垫)
4. bridge: 3-5秒(简短过渡)
5. close: 4-7秒(促单核心句+必要收束)
6. ★每个片段必须是完整的语义单元★——一句话没说完不要切断，但也不要把3句以上无关内容打包
7. ★片段边界要对齐句子边界★——不要从句子中间开始，也不要在句子中间结束

[五,绝对禁止]
1. 禁止只选1-7段，最低8段
2. 禁止选过渡废话:再开新款,过款,接下来,好吧,"然后反正这身","好的然后","来给大家看一下"
3. 禁止混入其他品类的片段——反复检查每个片段是否属于主打单品
4. 禁止选不完整的半句话:每个片段首尾必须是完整句子边界
5. 禁止选主播回弹幕的废话:如"没有的啊","知道知道"等纯互动
6. 禁止选纯ASR错误片段:读起来完全不通则跳过
7. 禁止选语句不完整片段:缺主语/谓语/补语，最后一个片段禁止以"你会觉得""就感觉""呢""吧""啊"等未完成语气结束
8. 禁止保留具体品牌名:如"香奈儿""古驰"→替换为"大牌平替""秀场款"或删除
9. 禁止语义重复:同一卖点用不同说法说了多遍，只保留信息量最高的第一次
   - 例外:连续夸奖堆叠("太好看了""绝了""巨好看")是有效种草手法，不算重复
   - 真正重复:同一事实用不同话术复述(如"面料不厚"→"比较薄透"→"很轻盈")
10. 禁止选无实质内容语气词:单独的"嗯""对""啊""当然"
11. 禁止同主题内容被无关内容打断:讲面料的段落必须相邻，不能穿插其他话题
12. 禁止尺码/价格信息出现在前半段:尺码只能在最后2-3段，价格/购物车绝对禁止

[六,叙事连贯与节奏]
1. 每个片段必须是完整的一句话或完整意思
2. 片段间要有递进感:hook(抓人)→ product(种草，同主题相邻)→ close(促单)
3. 禁止相邻片段内容重复或高度相似
4. 每段文案应该是"能直接作为短视频配音"的流畅语句
5. ★只选有价值的完整句子★——每个片段的核心价值句必须完整，允许前后各含1句必要的上下文使语意通顺
6. ★start/end必须对齐句子真实起止★——不要往前延伸到上一句，也不要往后延伸到下一句
6. 结尾片段必须自然收束——最后一句没说完就去掉或往前找完整结束点
7. 同主题必须相邻:讲面料的放一起、讲版型的放一起、讲颜色的放一起
8. 信息密度要高:每3-5秒必须有新信息，禁止超过8秒连续讲同一个点
9. 节奏递进:开头紧凑抓人→中段展开卖点(信息量最大)→后段促单收尾
10. 负面信息后置:"没货了"之类放到结尾区域

[七,ASR纠错]
1. 根据上下文修正明显的ASR识别错误(如"惊恐"应为"惊艳")
2. 去掉不必要的语气词和重复
3. 保留口语化风格，确保读起来通顺自然
4. 不确定原文意思则宁可不修正
5. 重复检测:核心信息相同的片段只保留更完整的那条

[八,质量自检(输出前必须执行)]
1. Hook检查:第一段能否让陌生人停下来?太淡则重新搜索全片最强Hook
2. 重复检查:相同信息出现2次以上?只保留第一次
3. 时长检查:超过65秒?优先删除:重复段→过渡废话段→信息量最低的product段
4. 内容检查:每段有实质内容?纯语气词/纯过渡句删除
5. 位置检查:尺码/价格是否在最后2-3段?前半段出现则移到末尾
6. 连贯性检查:同主题内容相邻?被隔开则重新排列聚拢
7. 结尾检查:最后1-2段包含促单信号?没有则找合适促单内容作结尾
8. 完整性检查:每段是完整句子?半截话删除
9. 品牌名检查:文案有具体品牌名?替换为通用描述或删除

[九,输出格式]
只输出JSON数组，每个片段必须包含 focus 和 reason 字段:
[
  {"clip_type": "hook", "start": "秒数", "end": "秒数", "text": "修正后的文案，用【】标注重点词如【超级无敌冰】", "focus": "版型", "reason": "圈人群+效果对比，2秒内传达完整信息"},
  ...
]
text字段:用中文【】符号包裹该片段最核心的1-3个关键词，字幕会自动将【】内的词标黄放大
start/end:使用输入字幕中的真实时间戳，必须精确到每一句话的开始和结束，不要前后延伸多余内容
reason字段:一句话说明选择这段的理由(用于质量审核和迭代优化)

clip_type:
- "hook": 开头抓人片段
- "product": 产品种草片段
- "close": 促单收尾片段
- "bridge": 过渡衔接片段(可选)

focus(必填):
- "版型": 版型、剪裁、廓形、显瘦遮肉
- "面料": 材质、手感、触感、起球、克重
- "颜色": 颜色、花色、图案、条纹
- "显瘦": 显瘦、显高、遮胯、藏肉、修饰身材
- "场景": 通勤、约会、度假、日常
- "搭配": 搭配、组合、套穿、配什么
- "对比": 对比市面产品、反面案例、价格锚点
- "品质": 做工、走线、细节、质感
- "价格": 价格、到手价、性价比、优惠
- "痛点解决": 直接回答用户顾虑
- "信任": 盲拍、不搞虚、自留款、私服
- "其他": 不属于以上类别
涉及多个卖点时选最核心的一个"""

# : 去除主播重复讲述的段落
# ============================================================
def _dedup_srt_repeated_sections(cleaned_srt, log_fn):
    """检测 SRT 中重复出现的连续段落并删除（主播经常重复讲同一批卖点）"""
    def _log(msg):
        if log_fn: log_fn(msg)

    lines = cleaned_srt.strip().split("\n")
    if len(lines) < 12:
        return cleaned_srt

    # 解析 SRT 为段落列表
    segments = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if re.match(r'\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}', line):
            text = ""
            if i + 1 < len(lines):
                text = lines[i + 1].strip()
            j = i + 2
            while j < len(lines) and lines[j].strip() and '-->' not in lines[j]:
                next_line = lines[j].strip()
                # Stop if next line is a segment number (digits only)
                if re.match(r'^\d+$', next_line):
                    break
                text += next_line
                j += 1
            segments.append((line, text, i))
            i = j
        else:
            i += 1

    if len(segments) < 6:
        return cleaned_srt

    def norm(text):
        return re.sub(r'[\s\W]+', '', text).lower()

    fingerprints = [norm(seg[1]) for seg in segments]

    removed_segs = set()
    for win_size in range(min(8, len(segments) // 2), 2, -1):
        seen_windows = {}
        for i in range(len(segments) - win_size + 1):
            if any(j in removed_segs for j in range(i, i + win_size)):
                continue
            fp = tuple(fingerprints[j] for j in range(i, i + win_size))
            fp_key = tuple(f[:min(8, len(f))] for f in fp)

            if fp_key in seen_windows:
                first_start = seen_windows[fp_key]
                gap = i - first_start
                if gap >= win_size:
                    total_sim = 0
                    for k in range(win_size):
                        a = fingerprints[first_start + k]
                        b = fingerprints[i + k]
                        if not a or not b:
                            continue
                        sa, sb = set(a), set(b)
                        if sa and sb:
                            total_sim += len(sa & sb) / max(len(sa | sb), 1)
                    avg_sim = total_sim / win_size
                    if avg_sim > 0.35:  # 35% threshold for fuzzy repeat detection
                        for j in range(i, i + win_size):
                            removed_segs.add(j)
                        _log(f"SRT预去重: 删除第{i+1}-{i+win_size}段(与第{first_start+1}-{first_start+win_size}段重复, 相似度{avg_sim:.0%})")
            else:
                seen_windows[fp_key] = i

    # [增强] 整段重复检测：逐段两两比对
    if len(segments) >= 4:
        for i in range(len(segments)):
            if i in removed_segs:
                continue
            for j in range(i + 1, len(segments)):
                if j in removed_segs:
                    continue
                fi = fingerprints[i]
                fj = fingerprints[j]
                if not fi or not fj:
                    continue
                si_set, sj_set = set(fi), set(fj)
                if not si_set or not sj_set:
                    continue
                overlap = len(si_set & sj_set) / max(len(si_set | sj_set), 1)
                # 相邻段用低阈值(0.5)，非相邻用高阈值(0.65)
                threshold = 0.5 if (j - i) <= 1 else 0.65
                if overlap > threshold:
                    removed_segs.add(j)
                    _log(f"SRT预去重[整段]: 删除第{j+1}段(与第{i+1}段重复, 相似度{overlap:.0%})")

    if not removed_segs:
        return cleaned_srt

    _log(f"SRT预去重: 共删除 {len(removed_segs)} 个重复段落, {len(segments) - len(removed_segs)} 个保留")

    # [增强] 包含检测: 如果段A的文本完全被段B包含，删除较短的A
    contain_removed = 0
    for i in range(len(segments)):
        if i in removed_segs:
            continue
        fi = fingerprints[i]
        if not fi or len(fi) < 3:
            continue
        for j in range(len(segments)):
            if i == j or j in removed_segs:
                continue
            fj = fingerprints[j]
            if not fj or len(fj) < 3:
                continue
            # 检查包含: fi 是 fj 的子串或相反
            if fi in fj or fj in fi:
                # 删除较短的那个
                shorter = i if len(fi) < len(fj) else j
                if shorter not in removed_segs:
                    removed_segs.add(shorter)
                    contain_removed += 1
    if contain_removed:
        _log(f"SRT预去重[包含]: 删除 {contain_removed} 个被包含的短片段")

    # [v9.2] 口吃检测: 在前后3段范围内检测子串和完全相同
    # 规则: 子串匹配只删短段; 完全相同删后出现的; 不误杀长段
    removed_fps = set(fingerprints[idx] for idx in removed_segs)
    stutter_removed = 0
    for i in range(len(segments)):
        if i in removed_segs:
            continue
        fi = fingerprints[i]
        if not fi or len(fi) < 2:
            continue
        should_remove = False
        # 1. 与已删段完全相同
        if fi in removed_fps:
            should_remove = True
        # 2. 与前后3段内的段比对
        if not should_remove:
            for j in range(max(0, i - 3), min(len(segments), i + 4)):
                if j == i or j in removed_segs:
                    continue
                fj = fingerprints[j]
                if not fj or len(fj) < 2:
                    continue
                # 完全相同: 删后出现的(i > j)
                if fi == fj and i > j:
                    should_remove = True
                    break
                # 子串匹配: 只删较短的那个
                shorter, longer = (fi, fj) if len(fi) < len(fj) else (fj, fi)
                shorter_idx = i if len(fi) < len(fj) else j
                if len(fi) == len(fj):
                    continue  # 等长且不完全相同,跳过
                if len(shorter) >= 3 and (longer[:len(shorter)] == shorter or longer[-len(shorter):] == shorter):
                    if shorter_idx == i:
                        should_remove = True
                        break
                    # shorter_idx == j: j已未被删,不删j(保留长段)
        if should_remove:
            removed_segs.add(i)
            removed_fps.add(fi)
            stutter_removed += 1
            _log(f"SRT预去重[口吃]: 删除第{i+1}段")
    if stutter_removed:
        _log(f"SRT预去重[口吃]: 删除 {stutter_removed} 个口吃重复段")

    removed_line_indices = set()
    for seg_idx in removed_segs:
        time_line, text, start_idx = segments[seg_idx]
        removed_line_indices.add(start_idx)
        removed_line_indices.add(start_idx + 1)

    removed_line_indices = set()
    for seg_idx in removed_segs:
        time_line, text, start_idx = segments[seg_idx]
        removed_line_indices.add(start_idx)
        removed_line_indices.add(start_idx + 1)

    output_lines = []
    for i, line in enumerate(lines):
        if i not in removed_line_indices:
            output_lines.append(line)

    return "\n".join(output_lines)


# ============================================================
# 品类过滤:从 SRT 源头移除非主品类片段
# ============================================================
def _filter_srt_by_main_product(cleaned_srt, log_fn, force_category=None):
    """四维品类判定:成交铁证 > 下款预告排除 > 深度讲解 > 基础词频"""
    def _log(msg):
        if log_fn: log_fn(msg)

    # ============================================================
    # 1. 词库配置
    # ============================================================
    CORE_CATEGORIES = {
        "上衣": ["上衣","T恤","衬衫","针织衫","卫衣","打底衫","小衫","衬衣","网纱罩衫","罩衫",
                 "襯衣","毛衣","短袖","长袖","吊带","背心","抹胸","这件","这款","这条",
                 "针织","毛衣","卫衣","小衫","打底"],
        "裤子": ["裤子","牛仔裤","阔腿裤","打底裤","工装裤","休闲裤","长裤","短裤","九分裤",
                 "小脚裤","直筒裤","牛仔褲","褲子","褲","闊腿褲",
                 "裤"],  # 兜底:牛奶裤,烟管裤,哈伦裤等
        "裙子": ["裙子","连衣裙","半身裙","A字裙","包臀裙","长裙","短裙","百褶裙","魚尾裙","連衣裙",
                 "裙"],  # 兜底:碎花裙,吊带裙等
        "外套": ["外套","风衣","西装","羽绒服","大衣","夹克","棉服","皮衣","開衫","馬甲",
                 "風衣","夾克","羽絨服"],
        "套装": ["套装","四件套","三件套","两件套","三件","四件","成套","组合","套装组合",
                 "整套","全套"],
        "鞋子": ["鞋","鞋子","凉鞋","运动鞋","高跟鞋","平底鞋","单鞋","靴子","老爹鞋","帆布鞋"],
    }

    # 搭配触发词(搭配+品类 → 该品类不计分)
    MATCH_WORDS = ["搭","配","搭配","配着穿","搭什么","配什么","同款","一套","两件套"]

    # 下款预告词(预告+品类 → 该品类全程排除)
    NEXT_PREVIEW = ["下一个开","接下来开","过款","下一款","马上开","下个","接下来","下一个","过下","看下"]

    # 成交铁证词(+50分，绑定最近品类词)
    SELLING_PROOF = {
        "开价": ["到手价","价格","219","199","229","159","299","99","开价","多少钱","几块钱"],
        "行动": ["321","上车","上链接","刷新拍","拼手速","拍","抢","入手","下单","链接","小黄车","购同款"],
        "服务": ["报尺码","现货","发货","平铺晾","机洗","尺码","码数","不多了","没货","截单","断码","库存"],
    }
    SELLING_PROOF_ALL = []
    for v in SELLING_PROOF.values():
        SELLING_PROOF_ALL.extend(v)

    # ============================================================
    # 2. 解析 SRT 为段落
    # ============================================================
    lines = cleaned_srt.strip().split("\n")
    segments = []  # [(time_line, text, line_indices)]
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if re.match(r'\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}', line):
            text = ""
            if i + 1 < len(lines):
                text = lines[i + 1].strip()
            j = i + 2
            while j < len(lines) and lines[j].strip() and '-->' not in lines[j]:
                text += lines[j].strip()
                j += 1
            segments.append((line, text, list(range(i, j))))
            i = j
        else:
            i += 1

    if not segments:
        return cleaned_srt

    # ============================================================
    # 3. 逐段分析
    # ============================================================
    # 记录每个段的属性
    seg_info = []  # {text, cats_found, has_match, has_preview, has_proof, preview_cat}
    for time_line, text, line_indices in segments:
        info = {
            "text": text, "time_line": time_line, "line_indices": line_indices,
            "cats_found": [],      # 本段出现的品类
            "has_match": False,    # 是否有搭配词
            "has_preview": False,  # 是否有下款预告
            "has_proof": False,    # 是否有成交铁证
            "preview_cats": [],    # 被预告的品类
            "proof_cats": [],      # 成交铁证绑定的品类
        }
        for cat, keywords in CORE_CATEGORIES.items():
            for kw in keywords:
                if kw in text:
                    info["cats_found"].append(cat)
                    break
        # 搭配检测
        for mw in MATCH_WORDS:
            if mw in text:
                info["has_match"] = True
                break
        # 下款预告检测
        for nw in NEXT_PREVIEW:
            if nw in text:
                info["has_preview"] = True
                # 预告绑定的品类 = 文本中的品类(除搭配外)
                for cat in info["cats_found"]:
                    info["preview_cats"].append(cat)
                break
        # 成交铁证检测
        for sp in SELLING_PROOF_ALL:
            if sp in text:
                info["has_proof"] = True
                # 成交铁证绑定最近品类 = 本段中的品类(排除搭配和预告)
                for cat in info["cats_found"]:
                    if cat not in info.get("preview_cats", []):
                        info["proof_cats"].append(cat)
                # 如果本段无品类，向前/向后找最近的品类
                if not info["proof_cats"]:
                    idx = segments.index((time_line, text, line_indices)) if (time_line, text, line_indices) in segments else -1
                    # 向前找5段
                    for di in range(1, min(6, idx + 1)):
                        prev_idx = idx - di
                        if prev_idx >= 0 and prev_idx < len(seg_info):
                            prev = seg_info[prev_idx]
                            if prev["cats_found"] and not prev["has_preview"]:
                                info["proof_cats"] = [prev["cats_found"][0]]
                                break
                    # 向后找5段
                    if not info["proof_cats"]:
                        for di in range(1, min(6, len(segments) - idx)):
                            next_idx = idx + di
                            if next_idx < len(segments) and next_idx < len(seg_info):
                                nxt = seg_info[next_idx]
                                if nxt["cats_found"] and not nxt["has_preview"]:
                                    info["proof_cats"] = [nxt["cats_found"][0]]
                                    break
                break
        seg_info.append(info)

    # ============================================================
    # 4. 四维判定
    # ============================================================
    all_cats = list(CORE_CATEGORIES.keys())

    # 优先级1:成交铁证(权重降低，避免顺带提及碾压主推品)
    proof_scores = {cat: 0 for cat in all_cats}
    proof_details = {cat: 0 for cat in all_cats}
    for info in seg_info:
        for cat in info["proof_cats"]:
            proof_scores[cat] += 15
            proof_details[cat] += 1

    # 优先级2:下款预告排除
    excluded_cats = set()
    for info in seg_info:
        for cat in info["preview_cats"]:
            excluded_cats.add(cat)

    # 优先级3:深度讲解篇幅(连续≥3条同品类 = 深度讲解)
    continuous = {cat: 0 for cat in all_cats}
    max_continuous = {cat: 0 for cat in all_cats}
    for info in seg_info:
        active_cats = set(info["cats_found"]) - excluded_cats
        for cat in active_cats:
            continuous[cat] += 1
            max_continuous[cat] = max(max_continuous[cat], continuous[cat])
        for cat in all_cats:
            if cat not in active_cats:
                continuous[cat] = 0

    deep_bonus = {}
    for cat in all_cats:
        if cat in excluded_cats:
            deep_bonus[cat] = 0
        elif max_continuous[cat] >= 50:
            deep_bonus[cat] = 30
        elif max_continuous[cat] >= 20:
            deep_bonus[cat] = 15
        elif max_continuous[cat] >= 10:
            deep_bonus[cat] = 5
        else:
            deep_bonus[cat] = 0

    # 优先级4:基础核心词计分(排除搭配+预告)— 每段+15，段落数是最可靠指标
    base_scores = {cat: 0 for cat in all_cats}
    seg_counts = {cat: 0 for cat in all_cats}  # 提到该品类的段落数
    for info in seg_info:
        for cat in info["cats_found"]:
            if cat in excluded_cats:
                continue
            if info["has_match"] and len(info["cats_found"]) > 1:
                continue
            base_scores[cat] += 15
            seg_counts[cat] += 1

    # 计算总分(段落数×15 + 基础词×15 + 铁证×15 + 深度讲解加成)
    final_scores = {}
    for cat in all_cats:
        s = base_scores[cat] + proof_scores[cat] + deep_bonus[cat]
        if cat in excluded_cats:
            s = 0
        final_scores[cat] = s

    # 日志
    _log("品类过滤:")
    if excluded_cats:
        # 找排除原因
        for cat in excluded_cats:
            for info in seg_info:
                if cat in info["preview_cats"]:
                    for nw in NEXT_PREVIEW:
                        if nw in info["text"]:
                            _log(f"  下款预告排除品类:{cat}(命中词:{nw})")
                            break
                    break
    for cat in all_cats:
        detail = f"铁证:{proof_details[cat]}次(+{proof_scores[cat]}分)"
        detail += f" 深度讲解:{max_continuous[cat]}条(+{deep_bonus[cat]}分)"
        detail += f" 基础词:{seg_counts[cat]}段(+{base_scores[cat]}分)"
        _log(f"  {cat}: 总分={final_scores[cat]}分 | {detail}")

    # 判定主品类
    valid_cats = {cat: s for cat, s in final_scores.items() if s > 0 and cat not in excluded_cats}
    if not valid_cats:
        _log("  无法识别主品类，保留全部")
        return cleaned_srt

    # [v8.3] 套装加权: 套装+单品共现段落 +30分
    if "套装" in valid_cats and valid_cats["套装"] > 0:
        suit_bonus = 0
        for info in seg_info:
            cats_found = info["cats_found"]
            if "套装" in cats_found and len(cats_found) >= 2:
                suit_bonus += 30
        if suit_bonus > 0:
            final_scores["套装"] = final_scores.get("套装", 0) + suit_bonus
            _log(f"  套装加权: +{suit_bonus}分 (套装+单品共现段落)")
            valid_cats = {cat: s for cat, s in final_scores.items() if s > 0 and cat not in excluded_cats}


    # 用户手动指定主品类(最高优先级)
    if force_category and force_category != "自动检测":
        # 查找匹配的品类(支持模糊匹配，如"上衣"匹配"上衣"，"裤子"匹配"裤子")
        matched = None
        for cat in CORE_CATEGORIES:
            if force_category in cat or cat in force_category:
                matched = cat
                break
        if matched and matched in CORE_CATEGORIES:
            main_cat = matched
            _log(f"  用户指定主品类={main_cat}(覆盖自动检测结果)")
        else:
            _log(f"  未找到品类'{force_category}'，使用自动检测")
            main_cat = max(valid_cats, key=valid_cats.get)
    else:
        main_cat = max(valid_cats, key=valid_cats.get)

    # ============================================================
    # 5. SRT 过滤
    # ============================================================
    output_lines = []
    removed = 0
    kept = 0
    preview_removed = 0
    match_removed = 0

    for seg_idx, info in enumerate(seg_info):
        should_remove = False

        # 规则1:下款预告的品类片段 → 删除
        if info["has_preview"] and main_cat not in info["cats_found"]:
            should_remove = True
            preview_removed += 1

        # 规则2:仅搭配提及的跨品类片段 → 删除
        elif not should_remove and info["has_match"]:
            has_main = main_cat in info["cats_found"]
            has_other = any(c != main_cat for c in info["cats_found"])
            if has_other and not has_main:
                should_remove = True
                match_removed += 1

        # 规则3:纯次品类片段(无主品类,无搭配,无预告)→ 也删除
        elif not should_remove:
            has_main = main_cat in info["cats_found"]
            has_other = any(c != main_cat for c in info["cats_found"])
            if has_other and not has_main:
                should_remove = True
                match_removed += 1

        if should_remove:
            removed += 1
            continue

        # 保留
        output_lines.append(info["time_line"])
        output_lines.append(info["text"])
        output_lines.append("")
        kept += 1

    _log(f"品类过滤: 最终主品类={main_cat}({final_scores[main_cat]}分)，移除 {removed} 个片段(预告{preview_removed}+搭配/纯次品类{match_removed})，保留 {kept} 个")

    # ============================================================
    # 6. 跨品类合法性校验(第二道防线)
    # ============================================================
    # 唯一合法的次品类提及:同一句中必须同时有 主品类词 + 搭配词 + 次品类词
    # 否则删除
    main_keywords = set()
    for kw in CORE_CATEGORIES.get(main_cat, []):
        main_keywords.add(kw)
    # 扩展主品类词:包含"这件","这款","这个"等指代词(如果后面紧跟主品类相关描述)
    main_keywords.update(["这件", "这款", "这个", "这条", "那个", "那种"])

    match_trigger = {"搭", "配", "搭配", "配着穿", "搭什么", "配什么", "同款", "一套", "两件套"}

    other_keywords = set()
    for cat, keywords in CORE_CATEGORIES.items():
        if cat != main_cat:
            for kw in keywords:
                other_keywords.add(kw)

    # 重新解析 output_lines 做合法性校验
    legal_lines = []
    orphan_removed = 0
    legal_match_kept = 0
    ol = output_lines
    oi = 0
    while oi < len(ol):
        line = ol[oi].strip() if oi < len(ol) else ""
        # 检测时间戳行
        if re.match(r'\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}', line):
            text = ol[oi + 1].strip() if oi + 1 < len(ol) else ""
            text_len = len(text)

            has_main = any(kw in text for kw in main_keywords)
            has_match = any(kw in text for kw in match_trigger)
            has_other = any(kw in text for kw in other_keywords)

            if has_other and not has_match:
                # 有次品类但无搭配词 → 检查是否有主品类
                if has_main:
                    # 有主品类 + 次品类但无搭配 → 合法(主品类讲解中顺便提了下其他品)
                    legal_lines.append(line)
                    legal_lines.append(text)
                    legal_lines.append("")
                else:
                    # 孤立次品类 → 强制删除
                    orphan_removed += 1
                    oi += 3
                    continue
            elif has_other and has_match and not has_main:
                # 次品类+搭配 但无主品类 → 删除
                orphan_removed += 1
                oi += 3
                continue
            else:
                # 其他情况(有主品类,或无品类词)→ 保留
                legal_lines.append(line)
                legal_lines.append(text)
                legal_lines.append("")
                if has_match and has_other and has_main:
                    legal_match_kept += 1
            oi += 3
        else:
            legal_lines.append(line)
            oi += 1

    if orphan_removed > 0:
        _log(f"品类合法性校验: 移除 {orphan_removed} 个孤立跨品类片段，保留 {legal_match_kept} 个合法搭配片段")
    else:
        _log(f"品类合法性校验: 无突兀跨品类内容")

    return "\n".join(legal_lines)


# ============================================================
# 核心:调用 AI + 前置清洗 + 重试
# ============================================================
def ai_analyze_clips(srt_text, log_fn=None, force_category=None):
    def _log(msg):
        if log_fn: log_fn(msg)

    settings = load_settings()
    if not settings.get("api_key"):
        _log("AI: 未配置 API Key")
        return []

    api_key = settings["api_key"]
    base_url = settings["base_url"].rstrip("/")
    model = settings["model"]

    # [v9.3] 拆分长SRT条目，提高AI选片精度
    from srt_splitter import split_long_srt_entries
    srt_text = split_long_srt_entries(srt_text, max_duration=5.0, log_fn=_log)

    cleaned_srt = _pre_clean_srt(srt_text, log_fn)
    if not cleaned_srt.strip():
        _log("AI: 清洗后无有效字幕，尝试使用原始SRT...")
        cleaned_srt = srt_text
        if not cleaned_srt.strip():
            _log("AI: 原始SRT也为空")
            return []

    # SRT预去重: 去除主播重复讲述的段落
    cleaned_srt = _dedup_srt_repeated_sections(cleaned_srt, log_fn)

    # 品类过滤:识别主品类，从源SRT中移除其他品类(支持用户手动指定)
    cleaned_srt = _filter_srt_by_main_product(cleaned_srt, log_fn, force_category=force_category)

    # [PATCH] Compute SRT max time for safety clamping
    _srt_entries_times = []
    for _ln in cleaned_srt.strip().split("\n"):
        _tm = re.match(r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})', _ln.strip())
        if _tm:
            _es = int(_tm.group(5))*3600 + int(_tm.group(6))*60 + int(_tm.group(7)) + int(_tm.group(8))/1000.0
            _srt_entries_times.append(_es)
    srt_max_end = max(_srt_entries_times) + 0.5 if _srt_entries_times else None

    # AI 分析(最多重试 3 次)
    best_clips = []
    for attempt in range(3):
        _log(f"AI: 调用 {model}(第 {attempt + 1} 次)...")
        clips = _call_ai(api_key, base_url, model, cleaned_srt, log_fn)
        if not clips:
            continue
        original_clips = list(clips)
        removed_from_dedup = []
        clips = _dedup_clips(clips, log_fn)
        removed_from_dedup = [c for c in original_clips if c not in clips]
        # [v9.5] 多版本模式：去重后如果片段不足12个，回收被去除的片段
        if _skip_focus and len(clips) < 12 and removed_from_dedup:
            # 按评分排序回收，优先补回高分的
            removed_sorted = sorted(removed_from_dedup, key=lambda c: c[4], reverse=True)
            added = 0
            for rc in removed_sorted:
                # 避免时间重叠（和已有片段间隔>2s）
                overlap = False
                for ec in clips:
                    if abs(rc[2] - ec[2]) < 2 or abs(rc[3] - ec[3]) < 2:
                        overlap = True; break
                if not overlap:
                    clips.append(rc)
                    removed_from_dedup.remove(rc)
                    added += 1
                    if len(clips) >= 12:
                        break
            if added > 0:
                _log(f"多版本回收: 补回{added}个片段，当前{len(clips)}个")
        if not clips:
            continue
        # [增强] 内容去重
        # 记住最好的结果
        if len(clips) > len(best_clips):
            best_clips = clips[:]
        if _validate_clips(clips, log_fn):
            _log(f"AI: 校验通过，{len(clips)} 个片段")
            for ct, text, s, e, sc, d, *_ in clips:
                _log(f"  {ct:<16s} | {s:.1f}-{e:.1f}s ({d:.1f}s) | {text}")
            # 跨品类扫描(第二道防线)
            clips = _post_filter_cross_category(clips, cleaned_srt, log_fn)
            # 叙事连贯性检查
            clips = _check_narrative_coherence(clips, log_fn)
            # ASR纠错:修正文案中的常见识别错误
            clips = [(ct, _apply_asr_corrections(text, log_fn), s, e, sc, d)
                     for ct, text, s, e, sc, d in clips]
            # 主播互动废话过滤
            clips = _filter_host_interaction(clips, log_fn)
            # 语义重复过滤(代码层兜底)
            clips = _filter_semantic_repeat(clips, log_fn)
            # 明星名字过滤
            clips = _filter_celebrity(clips, log_fn)
            # CTA误判校验
            clips = _validate_cta(clips, log_fn)
            # 先去重(在边界修复前，避免边界扩展导致误判重叠)
            clips = _dedup_clip_text_overlap(clips, log_fn)
            if not clips:
                _log("AI: 去重后无剩余")
                continue
            # 片段边界修复:确保首尾对齐到完整句子
            # clips = _fix_clip_boundaries(clips, cleaned_srt, log_fn)  # [DISABLED] 延伸打乱节奏
            # [v9.2] 裁掉片段开头的语气词(对/嗯/呃等)对应的画面和音频
            clips = _trim_filler_start(clips, cleaned_srt, log_fn)
            # [v9.3] 用SRT时间戳收紧AI选片范围，去掉多选的废话
            from tighten import tighten_clip_boundaries
            clips = tighten_clip_boundaries(clips, srt_text, log_fn)
            # [DISABLED] trim_long_clips - 让Prompt控制时长，后处理不要切更碎
            # from trim_long import trim_long_clips
            # clips = trim_long_clips(clips, srt_text, max_dur=7.0, log_fn=log_fn)
            # [DISABLED] 延伸已禁用：会导致重叠、捞入垃圾内容
            # clips = _extend_clips(clips, log_fn, target_min=55, target_max=75, max_end=srt_max_end)
            # 兜底回收：延伸后如果仍不到50s，从去重被砍的片段中回收
            _total_dur = sum(c[5] for c in clips)
            if _total_dur < 50 and removed_from_dedup:
                _log(f"兜底回收: 当前 {_total_dur:.1f}s < 50s, 尝试回收被去重片段...")
                for rc in removed_from_dedup:
                    if sum(c[5] for c in clips) >= 50:
                        break
                    if rc[5] >= 8:
                        continue
                    # 检查重叠
                    _overlap = False
                    for ec in clips:
                        if rc[2] < ec[3] and rc[3] > ec[2]:
                            _overlap = True
                            break
                    if not _overlap:
                        # 插在最后一个close前面，不要加在close后面
                        last_close_idx = None
                        for ci, cc in enumerate(clips):
                            if 'close' in cc[0].lower():
                                last_close_idx = ci
                        if last_close_idx is not None:
                            clips.insert(last_close_idx, rc)
                        else:
                            clips.append(rc)
                        _log(f"  回收: {rc[2]:.1f}-{rc[3]:.1f}s ({rc[5]:.1f}s)")
            return clips
        _log(f"AI: 第 {attempt + 1} 次校验未通过，重试...")

    # 用最好的结果(不硬拒绝)
    if best_clips:
        _log(f"AI: 使用最佳结果({len(best_clips)} 片段)")
        clips = _dedup_clip_text_overlap(best_clips, log_fn)
        clips = _post_filter_cross_category(clips, cleaned_srt, log_fn)
        clips = _check_narrative_coherence(clips, log_fn)
        clips = [(ct, _apply_asr_corrections(text, log_fn), s, e, sc, d)
                 for ct, text, s, e, sc, d in clips]
        clips = _filter_host_interaction(clips, log_fn)
        # 价格/CTA硬过滤（AI Prompt拦不住的用代码拦）
        clips = _filter_price_and_cta(clips, log_fn)
        # 语义重复过滤(代码层兜底)
        clips = _filter_semantic_repeat(clips, log_fn)
        # 明星名字过滤
        clips = _filter_celebrity(clips, log_fn)
        # CTA误判校验
        clips = _validate_cta(clips, log_fn)
        # 先去重(在边界修复前)
        clips = _dedup_clip_text_overlap(clips, log_fn)
        if not clips:
            _log("AI: 去重后无剩余")
            return []
        # clips = _fix_clip_boundaries(clips, cleaned_srt, log_fn)  # [DISABLED] 延伸打乱节奏
        # [DISABLED] 延伸已禁用
        # clips = _extend_clips(clips, log_fn, target_min=55, target_max=75, max_end=srt_max_end)
        # 兜底回收：延伸后如果仍不到50s，从去重被砍的片段中回收
        _total_dur = sum(c[5] for c in clips)
        if _total_dur < 50 and removed_from_dedup:
            _log(f"兜底回收(best): 当前 {_total_dur:.1f}s < 50s, 尝试回收...")
            for rc in removed_from_dedup:
                if sum(c[5] for c in clips) >= 50:
                    break
                if rc[5] >= 8:
                    continue
                _overlap = False
                for ec in clips:
                    if rc[2] < ec[3] and rc[3] > ec[2]:
                        _overlap = True
                        break
                if not _overlap:
                    # 插在最后一个close前面，不要加在close后面
                    last_close_idx = None
                    for ci, cc in enumerate(clips):
                        if 'close' in cc[0].lower():
                            last_close_idx = ci
                    if last_close_idx is not None:
                        clips.insert(last_close_idx, rc)
                    else:
                        clips.append(rc)
                    _log(f"  回收(best): {rc[2]:.1f}-{rc[3]:.1f}s ({rc[5]:.1f}s)")
        # 如果还是不够8段，用关键词补充
        if len(clips) < 8:
            clips = _supplement_clips(clips, cleaned_srt, log_fn, min_total=4)
        return clips

    # 宽松修复
    relaxed = _relax_clips(clips if clips else [], log_fn)
    if relaxed and len(relaxed) < 8:
        relaxed = _supplement_clips(relaxed, cleaned_srt, log_fn, min_total=4)
    return relaxed if relaxed else []


def _call_ai(api_key, base_url, model, srt_text, log_fn):
    def _log(msg):
        if log_fn: log_fn(msg)

    transcript = srt_text.strip()
    if len(transcript) > 30000:
        transcript = transcript[-30000:]

    # 随机化:同一视频多次生成不同成品
    import random
    temperature = round(random.uniform(0.1, 0.4), 2)
    _log(f"AI: temperature={temperature}")

    # 随机偏好提示(每次侧重不同角度，增加差异化)
    if _skip_focus:
        focus = ""
        _log("AI: 全量选片模式（不限定偏好）")
    else:
        focus_hints = [
            "侧重价格冲击力，选价格对比最有冲击力的片段",
            "侧重面料卖点，优先选面料手感,质感相关的片段",
            "侧重身材痛点，优先选显瘦,遮肉,修饰身材的片段",
            "侧重穿着场景，优先选通勤,约会,出门等场景化片段",
            "侧重情绪感染力，优先选主播语气最激动,最真诚的片段",
            "侧重紧迫感，优先选限量,库存,限时相关的片段",
            "侧重性价比，优先选到手价,划算,超值的片段",
            "侧重流行趋势，优先选当季流行,设计感的片段",
        ]
        focus = random.choice(focus_hints)
        _log(f"AI: 本轮偏好 → {focus}")

    # [增强] 计算 SRT 时间范围，告知 AI
    _srt_times = []
    for _ln in srt_text.strip().split("\n"):
        _tm = re.match(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})", _ln.strip())
        if _tm:
            _es = int(_tm.group(5))*3600 + int(_tm.group(6))*60 + int(_tm.group(7)) + int(_tm.group(8))/1000.0
            _srt_times.append(_es)
    _srt_max = max(_srt_times) if _srt_times else 60
    _srt_min = min(_srt_times) if _srt_times else 0

    # 多版本模式：选更多片段，允许同卖点不同角度
    if _skip_focus:
        _clip_range = "15-25"
        _dedup_rule = "★同一卖点如果主播用了不同表达方式（如'面料好'和'这个面料摸着特别软'），可以分别选取，因为多版本需要差异化素材★"
        _total_rule = "总时长50-80秒（宁可多选，后续会按版本分配）"
    else:
        _clip_range = "10-15"
        _dedup_rule = '★绝对禁止重复同一卖点★ 字幕中主播会重复讲同一个卖点(如"面料好"说了3遍)，你必须只选每个卖点的最佳版本，严禁选两段内容相似的片段' 
        _total_rule = "总时长40-65秒（短一点没关系，内容丰富更重要）"

    user_msg = f"""以下是清洗后的直播字幕，你需要像专业短视频编导一样，从中精选片段并编排成一个完整的带货短视频脚本.

要求:
1. {_dedup_rule}
2. 像讲故事一样编排，每个片段自然衔接下一段，听起来是一段流畅的口播
2. 精选{_clip_range}个片段，宁可多选也不要少选，{_total_rule}
3. 每个片段包含完整的语句，不要切断正在说的话
4. [时间范围] 字幕时间范围 {_srt_min:.0f}s - {_srt_max:.0f}s，你的 start/end 必须严格在此范围内，禁止编造超出此范围的时间戳
5. [本轮选片偏好]{focus}

字幕内容:
{transcript}"""

    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg}
        ],
        "temperature": temperature,
        "top_p": 0.9,
        "max_tokens": 4096,
    }, ensure_ascii=False).encode("utf-8")

    url = f"{base_url}/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=180, context=ctx) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        msg = result.get("choices", [{}])[0].get("message", {})
        content = msg.get("content", "")
        # DeepSeek-R1: reasoning in reasoning_content, final answer in content
        # If content is empty but reasoning exists, try to extract JSON from reasoning tail
        if not content.strip():
            reasoning = msg.get("reasoning_content", "")
            if reasoning.strip():
                # R1 sometimes puts the JSON at the end of reasoning
                json_match = re.search(r'\[\s*\{[^]]*\}\s*\]', reasoning, re.DOTALL)
                if json_match:
                    content = json_match.group()
                    _log(f"AI: 从推理内容中提取JSON，长度={len(content)}字")
                else:
                    _log(f"AI: R1推理完成但content为空，reasoning长度={len(reasoning)}字")
        _log(f"AI: 响应成功，内容长度={len(content)}字")
        return _parse_ai_response(content, log_fn)
    except urllib.error.HTTPError as e:
        err = ""
        try: err = e.read().decode("utf-8", errors="replace")[:200]
        except Exception: pass
        _log(f"⚠️ AI 接口调用失败 (HTTP {e.code})：{_friendly_http(e.code, err)}")
        return []
    except Exception as e:
        _log(f"⚠️ AI 选片失败: {_friendly_msg(str(e))}")
        return []


# ============================================================
# 解析 AI 响应
# ============================================================
def _parse_ai_response(content, log_fn):
    def _log(msg):
        if log_fn: log_fn(msg)

    # 去掉 markdown 代码块包裹(```json ... ```)
    # R1 经常返回 ```json\n[...]\n``` 格式，需要多行匹配
    cleaned = re.sub(r'^```(?:json)?\s*\n?', '', content)
    cleaned = re.sub(r'\n?```\s*$', '', cleaned)
    # 兜底：如果有残留的 ``` 则逐个清除
    cleaned = cleaned.replace('```json', '').replace('```', '')
    cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r'\[[\s\S]*\]', cleaned)
        if m:
            try: data = json.loads(m.group())
            except json.JSONDecodeError:
                _log(f"AI: JSON 解析失败，原始前200字: {content[:200]}"); return []
        else:
            _log(f"AI: 未找到 JSON，原始前200字: {content[:200]}"); return []

    if not isinstance(data, list):
        data = data.get("clips", [])
    if not isinstance(data, list):
        _log("AI: 格式不正确"); return []

    type_map = {
        "hook": "hook", "钩子": "hook",
        "product": "product", "种草": "product", "卖点": "product", "highlight": "product", "亮点": "product",
        "scene": "product", "场景": "product",
        "close": "close", "促单": "close", "收尾": "close",
        "bridge": "bridge", "过渡": "bridge", "科普": "bridge",
        "trend": "trend", "趋势": "trend",
        "price": "product", "价格": "product",
        "urgency": "close", "紧迫": "close",
        "call_to_action": "close", "cta": "close", "逼单": "close",
    }

    clips = []
    skipped_no_text = 0
    skipped_bad_time = 0
    for idx, item in enumerate(data):
        # 诊断:打印第一个 item 的所有字段名
        if idx == 0:
            _log(f"AI: 第1项字段名={list(item.keys()) if isinstance(item, dict) else type(item).__name__}")
            _log(f"AI: 第1项原始值 start={item.get('start')} end={item.get('end')} text={str(item.get('text',''))[:40]}")
        ct = str(item.get("clip_type", item.get("type", "")))
        ct = type_map.get(ct, ct)
        if ct not in GOLDEN_CHAIN:
            ct = "highlight"
        text = str(item.get("text", "")).strip()
        start = _parse_time(item.get("start", 0))
        end = _parse_time(item.get("end", start + 5))
        if not text:
            skipped_no_text += 1; continue
        if end <= start:
            skipped_bad_time += 1; continue
        focus = str(item.get("focus", "")).strip()
        clips.append((ct, text, start, end, 50, end - start, focus))
    if not clips:
        _log(f"AI: {len(data)}项中有效0(无文本:{skipped_no_text}, 时间错误:{skipped_bad_time})")
    return clips


def _parse_time(t):
    try: return float(t)
    except (ValueError, TypeError): pass
    s = str(t)
    m = re.match(r'(\d+):(\d+):(\d+)[,.](\d+)', s)
    if m:
        return int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3)) + int(m.group(4))/1000.0
    m = re.match(r'(\d+):(\d+):(\d+)$', s)
    if m:
        return int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
    m = re.match(r'(\d+):(\d+)[,.](\d+)', s)
    if m:
        return int(m.group(1))*60 + int(m.group(2)) + int(m.group(3))/1000.0
    m = re.match(r'(\d+):(\d+)$', s)
    if m:
        return int(m.group(1))*60 + int(m.group(2))
    return 0.0


# ============================================================
# 去重
# ============================================================
def _dedup_clip_text_overlap(clips, log_fn):
    """Remove clips with overlapping source time ranges or highly similar text."""
    def _log(msg):
        if log_fn: log_fn(msg)
    if len(clips) < 2:
        return clips

    # Pass 1: Time range overlap detection
    # If clip A's time range is mostly inside clip B's range, remove A (the shorter one)
    removed = set()
    for i in range(len(clips)):
        if i in removed:
            continue
        ci_type, ci_text, ci_start, ci_end, ci_score, ci_dur = clips[i]
        for j in range(i + 1, len(clips)):
            if j in removed:
                continue
            cj_type, cj_text, cj_start, cj_end, cj_score, cj_dur = clips[j]

            # Calculate overlap
            overlap_start = max(ci_start, cj_start)
            overlap_end = min(ci_end, cj_end)
            overlap_dur = max(0, overlap_end - overlap_start)

            # If one clip is mostly inside the other
            if overlap_dur > 0:
                shorter_dur = min(ci_dur, cj_dur)
                if overlap_dur / shorter_dur > 0.5:  # >50% of shorter clip is overlapped
                    # Remove the shorter/less informative one
                    if ci_dur <= cj_dur:
                        removed.add(i)
                        _log(f"时间重叠: 移除片段{i+1}({ci_start:.1f}-{ci_end:.1f}s, 被片段{j+1}({cj_start:.1f}-{cj_end:.1f}s)包含)")
                        break  # i removed, stop comparing it
                    else:
                        removed.add(j)
                        _log(f"时间重叠: 移除片段{j+1}({cj_start:.1f}-{cj_end:.1f}s, 被片段{i+1}({ci_start:.1f}-{ci_end:.1f}s)包含)")

    # Pass 2: Text similarity (original logic)
    for i in range(len(clips)):
        if i in removed:
            continue
        ci_text = clips[i][1]
        ci_chars = set(re.sub(r"[\s\W]+", "", ci_text))
        if not ci_chars:
            continue
        for j in range(i + 1, len(clips)):
            if j in removed:
                continue
            cj_text = clips[j][1]
            cj_chars = set(re.sub(r"[\s\W]+", "", cj_text))
            if not cj_chars:
                continue
            overlap = len(ci_chars & cj_chars) / max(len(ci_chars | cj_chars), 1)
            if overlap > 0.55:
                if len(cj_text) <= len(ci_text):
                    removed.add(j)
                    _log(f"内容重复: 移除片段{j+1}(与片段{i+1}重复, 重叠{overlap:.0%})")
                else:
                    removed.add(i)
                    _log(f"内容重复: 移除片段{i+1}(与片段{j+1}重复, 重叠{overlap:.0%})")
                    break

    if not removed:
        return clips
    result = [c for idx, c in enumerate(clips) if idx not in removed]
    _log(f"去重: {len(clips)} -> {len(result)} 片段")
    return result


def _dedup_clips(clips, log_fn):

    def _log(msg):
        if log_fn: log_fn(msg)
    if not clips:
        return clips
    original = len(clips)
    _log(f"AI: 解析到 {original} 个片段，开始去重...")
    no_overlap = []
    for clip in clips:
        ct, text, start, end, score, dur = clip
        overlap = False
        for ex in no_overlap:
            ov_s, ov_e = max(start, ex[2]), min(end, ex[3])
            if ov_e > ov_s and (ov_e - ov_s) / dur > 0.5:
                overlap = True; break
        if not overlap:
            no_overlap.append(clip)
    clips = no_overlap
    # 增强去重: 全局已选内容比较
    STOP_CHARS = set("的了是得很都也就在有被把给到和不还而与人这那她又他它们会要能让得去上下来过对说没好几什么怎这么一个自己我们你们他们")
    def extract_keys(text):
        chars = set(c for c in text if c not in STOP_CHARS and c.strip())
        bigrams = set(text[i:i+2] for i in range(len(text)-1)
                     if text[i] not in STOP_CHARS and text[i+1] not in STOP_CHARS)
        return chars, bigrams

    no_similar = []
    seen_chars = set()
    seen_bigrams = set()
    seen_texts = []
    for clip in clips:
        ct, text, start, end, score, dur = clip
        chars, bigrams = extract_keys(text)
        is_dup = False
        # 短句高度重复检查
        if chars and seen_chars and len(chars) < 8:
            if len(chars & seen_chars) / len(chars) > 0.8:
                is_dup = True
        # 逐条语义比较
        if not is_dup:
            for pt in seen_texts:
                pc, pb = extract_keys(pt)
                if bigrams and pb:
                    bo = len(bigrams & pb)
                    bu = len(bigrams | pb)
                    if bu > 0 and bo / bu > 0.7:
                        is_dup = True; break
                if chars and pc:
                    co = len(chars & pc)
                    cu = len(chars | pc)
                    if cu > 0 and co / cu > 0.75:
                        is_dup = True; break
        if not is_dup:
            no_similar.append(clip)
            seen_chars |= chars
            seen_bigrams |= bigrams
            seen_texts.append(text)
        else:
            _log(f"  去重移除: {text[:30]}...")
    clips = no_similar
    # 不排序，保持 AI 原始输出顺序（AI prompt 已要求叙事编排）

    # 报尺码的片段移到末尾(扩展关键词覆盖Whisper各种转录形式)
    size_keywords = [
        # 尺码标识
        "S码", "M码", "L码", "XL", "XXL", "3XL", "4XL", "尺码", "码数",
        "均码", "大码", "小码", "加肥", "加大", "宽松版",
        # 体重段
        "80斤", "90斤", "100斤", "110斤", "120斤", "130斤", "140斤", "150斤", "160斤",
        "80到120", "90到130", "100到140", "110到150", "120到160", "80-120", "90-130",
        "80至120", "90至130", "100至140", "110至150", "120至160",
        "八十", "九十", "一百", "一百一", "一百二", "一百三", "一百四", "一百五",
        # 身高段
        "身高", "体重", "cm", "一米五", "一米六", "一米七", "155", "160", "165", "170",
        # 穿搭建议
        "穿什么码", "选什么码", "拍什么码", "入什么码", "报一下", "报尺码",
        "尺码表", "码型", "偏大", "偏小", "正常码", "码数",
        # Whisper可能的转录
        "码子", "码", "斤", "公斤",
    ]
    # 单字词太容易误匹配，只检查长度≥2的词
    size_keywords = [kw for kw in size_keywords if len(kw) >= 2]

    size_clips = [c for c in clips if any(kw in c[1] for kw in size_keywords)]
    other_clips = [c for c in clips if c not in size_clips]
    # [v9.2] 尺码去重：多个尺码片段只保留最后一个（尺码只报一次）
    if len(size_clips) > 1:
        _log(f"尺码去重: {len(size_clips)} 个尺码片段，只保留最后1个")
        size_clips = [size_clips[-1]]
    if size_clips:
        clips = other_clips + size_clips
        _log(f"尺码后置: {len(size_clips)} 个尺码片段移到末尾")

    # 额外保护:hook位置(前2个)禁止尺码内容，和后面非尺码片段交换
    for i in range(min(2, len(clips))):
        c = clips[i]
        if any(kw in c[1] for kw in size_keywords):
            for j in range(i + 1, len(clips)):
                if not any(kw in clips[j][1] for kw in size_keywords):
                    clips[i], clips[j] = clips[j], clips[i]
                    _log(f"尺码保护: 位置{i+1}的尺码片段与位置{j+1}交换")
                    break

    # [v9.2] 价格/购物车片段直接排除(用户要求成品不报价格)
    price_keywords = [
        "折后", "到手", "价格", "多少钱", "减", "优惠",
        "购物车", "链接", "上车", "拍", "下单", "抢",
        "原价", "现价", "划算", "性价比",
        "小黄车", "左下角", "去拍", "直播间", "小车",
        "挂车", "加购", "拼单", "福利", "赠品", "包邮",
        "满减", "专区", "特价", "限时", "截止",
    ]
    price_clips = [c for c in clips if any(kw in c[1] for kw in price_keywords)]
    if price_clips:
        clips = [c for c in clips if c not in price_clips]
        _log(f"价格排除: 删除 {len(price_clips)} 个价格片段, 剩余 {len(clips)} 片")

    # [v9.2] 纯语气词片段排除(“对”“呞”“呃”“啊”等无实质内容)
    filler_patterns = [
        "对", "呞", "呃", "啊", "噢", "哼", "嗯", "啦",
        "哈", "啼", "嘿", "哈哈", "啦啦",
        "是的", "的啊", "的呢",
    ]
    filler_clips = []
    for c in clips:
        text_clean = re.sub(r'[\s\W]+', '', c[1]).lower()
        if len(text_clean) <= 3 and any(text_clean == p or text_clean == p+p for p in filler_patterns):
            filler_clips.append(c)
        elif len(text_clean) <= 5:
            # 检查是否全部由语气词组成
            chars = set(text_clean)
            filler_chars = set('对呞呃啊噢哼嗯啦哈啼嘿是的啊呢呢啦')
            if chars <= filler_chars:
                filler_clips.append(c)
    if filler_clips:
        clips = [c for c in clips if c not in filler_clips]
        _log(f"语气词排除: 删除 {len(filler_clips)} 个纯语气词片段, 剩余 {len(clips)} 片")

    # [DISABLED] close位置交换打乱AI叙事顺序，由强制排序保证close在末尾
    # for i in range(min(3, len(clips))):
    #     if clips[i][0] == "close":
    #         for j in range(i + 1, len(clips)):
    #             if clips[j][0] != "close":
    #                 clips[i], clips[j] = clips[j], clips[i]
    #                 _log(f"close保护: 位置{i+1}的close片段与位置{j+1}交换")
    #                 break

    # Hook首位保护: 第一个片段必须是hook类型
    if clips and clips[0][0] != "hook":
        for j in range(1, len(clips)):
            if clips[j][0] == "hook":
                clips[0], clips[j] = clips[j], clips[0]
                _log(f"Hook首位保护: 位置1的非hook片段与位置{j+1}的hook交换")
                break
        else:
            _log("警告: 没有找到hook类型片段，无法执行首位保护")

    # [DISABLED] 品类后置打乱AI叙事顺序，由AI Prompt控制品类排列
    # clips = _enforce_product_coherence(clips, log_fn)

    # 终剪前最后防线:移除孤立跨品类片段
    clips = _remove_orphan_cross_category(clips, log_fn)

    # [v9.0] 面料不再强制后置,由AI Prompt控制同主题相邻排列
    # 原面料后置逻辑会打断叙事流(讲面料→突然插入尺码→又讲面料),已移除


    if len(clips) < original:
        _log(f"去重: {original} -> {len(clips)}")
    # 卖点聚焦排序
    focus_counts = {}
    for clip in clips:
        fp = _detect_focus_point(clip[1])
        focus_counts[fp] = focus_counts.get(fp, 0) + 1
    if focus_counts:
        sorted_foci = sorted(focus_counts.items(), key=lambda x: -x[1])
        primary = sorted_foci[0][0]
        secondary = sorted_foci[1][0] if len(sorted_foci) > 1 else None
        _log(f"卖点聚焦: 主={primary}({focus_counts[primary]}段) 次={secondary}")
        type_groups = {}
        for clip in clips:
            type_groups.setdefault(clip[0], []).append(clip)
        def _frank(c):
            fp = _detect_focus_point(c[1])
            if fp == primary: return 0
            if fp == secondary: return 1
            return 2
        # 不排序，保持 AI 叙事顺序

    return clips


def _detect_focus_point(text):
    RULES = [
        ("版型", ["版型","廓形","剪裁","袖型","领型","宽松","修身","收腰","直筒","微喇","落肩","短款","长款","箱型",
                  "高腰","中腰","低腰","A字","包臀","开叉","大摆","灯笼袖","泡泡袖","垫肩","阔腿","小脚","九分",
                  "高领","V领","圆领","方领","一字肩","露肩","抓绳","杆腰",
                  "显瘦","显高","显腿长","比例","曼妙","修饰"]),
        ("面料", ["面料","材质","手感","触感","起球","克重","纱线","针织","棉麻","真丝","垂感","弹力","透气","柔软","蓬松","网纱",
                  "莱赛尔","天丝","冰丝","雪纺","纯棉","亚麻","锦纶","涤纶","缎面","丝绒","灯芯绒","牛仔",
                  "垂坠","亲肤","凉感","吸汗","不闷","不透","厚实","薄款","加厚","夹棉","抓绒",
                  "胸垫","垫肩","内衬","里布","提花","刺绣","铻绣","压钻","重工"]),
        ("颜色", ["颜色","色系","复古","条纹","碎花","纯色","拼色","渐变","军绿","咖色","黑色","白色","花色","撞色",
                  "显白","不挑人","不挑肤色","黄皮","提亮","显气色","高级色","莫兰迪","燕麦色","奶白色"]),
        ("场景", ["通勤","约会","度假","日常","职场","上学","出门","旅游","年会","聚会","居家","运动","健身","瑜伽",
                  "拍照","逛街","户外","婚礼","相亲","见家长","面试"]),
        ("搭配", ["搭配","套穿","叠穿","外套","西装","组合","成套","同款",
                  "配什么","搭什么","内搭","外穿","打底","单穿","腰带","配饰"]),
        ("品质", ["做工","走线","细节","质感","高级感","精致","工艺","品质","缝合",
                  "大牌","专柜","原单","高定","免烫","不起球","不褪色","不变形"]),
        ("价格", ["价格","到手价","优惠","划算","超值","折扣","领券","立减","性价比",
                  "秒杀","福利","特价","骨折价","白菜价","闭眼入","手慢无","抢瘆了",
                  "拍一发二","多拍","囤货"]),
    ]
    for focus, kws in RULES:
        for kw in kws:
            if kw in text:
                return focus
    return "其他"


# ============================================================
# 硬校验(最少7段)
# ============================================================
def _validate_clips(clips, log_fn):
    def _log(msg):
        if log_fn: log_fn(msg)

    if len(clips) < 3:
        _log(f"校验失败: 仅 {len(clips)} 片段")
        return False

    types = [c[0] for c in clips]
    if "hook" not in types:
        _log("警告: 缺少 hook(但不拒绝)")
    if "call_to_action" not in types:
        _log("警告: 缺少 close(但不拒绝)")
    # 允许重复类型(去除严格限制)
    # indices = [GOLDEN_CHAIN.index(t) if t in GOLDEN_CHAIN else 99 for t in types]
    # if indices != sorted(indices):
    #     _log("校验失败: 顺序错误"); return False

    total = sum(c[5] for c in clips)
    if total < 20 or total > 120:
        _log(f"校验失败: 总时长 {total:.1f}s 异常")
        return False

    if len(clips) < 3:
        _log(f"警告: 仅 {len(clips)} 段(建议≥7段)，但继续处理")

    return True


def _relax_clips(clips, log_fn):
    def _log(msg):
        if log_fn: log_fn(msg)
    if not clips or len(clips) < 2:
        return None
    type_best = {}
    for c in clips:
        if c[0] not in type_best or c[4] > type_best[c[0]][4]:
            type_best[c[0]] = c
    sorted_clips = [type_best[t] for t in GOLDEN_CHAIN if t in type_best]
    if len(sorted_clips) < 2:
        return None

    total = sum(c[5] for c in sorted_clips)
    if total > 65:
        for i, c in enumerate(sorted_clips):
            if c[5] > 10 and total > 60:
                cut = min(c[5] - 10, total - 60)
                s, e = c[2], c[3] - cut
                sorted_clips[i] = (c[0], c[1], s, e, c[4], e - s)
                total = sum(x[5] for x in sorted_clips)
    elif total < 50:
        for i, c in enumerate(sorted_clips):
            if c[0] == "highlight" and c[5] < 15:
                add = min(60 - total, 15 - c[5])
                if add > 0:
                    s, e = c[2], c[3] + add
                    sorted_clips[i] = (c[0], c[1], s, e, c[4], e - s)
                break
    total = sum(c[5] for c in sorted_clips)
    _log(f"宽松修复: {len(sorted_clips)} 片段, 总时长 {total:.1f}s")
    return sorted_clips


# ============================================================
# 片段延伸(自动前后扩展短片段，目标总时长 45-65 秒)
# ============================================================
# ============================================================
# ASR 纠错:修正 AI 输出文案中的常见 ASR 错误
# ============================================================
def _apply_asr_corrections(text, log_fn=None):
    """对片段文案进行 ASR 错误修正"""
    def _log(msg):
        if log_fn: log_fn(msg)

    original = text
    corrected = text

    for wrong, right in ASR_CORRECTIONS.items():
        if wrong in corrected:
            corrected = corrected.replace(wrong, right)
            if corrected != original:
                _log(f"  ASR纠错: '{wrong}' → '{right}'")

    return corrected


# ============================================================
# 片段边界修复:确保片段首尾对齐到 SRT 句子边界
# ============================================================
def _trim_filler_start(clips, cleaned_srt, log_fn=None):
    """裁掉片段开头的语气词SRT条目，把start推迟到第一个非语气词条目的起始时间"""
    def _log(msg):
        if log_fn: log_fn(msg)

    if not clips or not cleaned_srt:
        return clips

    FILLER_WORDS = {"对","嗯","呃","啊","噢","哼","啦","哈","嘿","哎","是的","当然","对吧","然后呢"}

    # 解析 SRT 为 entries
    entries = []
    lines = cleaned_srt.strip().split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = re.match(
            r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})',
            line)
        if m:
            start_s = (int(m.group(1))*3600 + int(m.group(2))*60 +
                       int(m.group(3)) + int(m.group(4))/1000.0)
            end_s = (int(m.group(5))*3600 + int(m.group(6))*60 +
                     int(m.group(7)) + int(m.group(8))/1000.0)
            text = ""
            j = i + 1
            while j < len(lines) and lines[j].strip() and '-->' not in lines[j]:
                text += lines[j].strip()
                j += 1
            norm = re.sub(r'[^\u4e00-\u9fff\w]', '', text.strip())
            entries.append((start_s, end_s, norm))
            i = j
        else:
            i += 1

    if not entries:
        return clips

    trimmed = []
    trim_count = 0
    for ct, text, start, end, score, dur in clips:
        new_start = start
        # 找片段开头连续的语气词SRT条目
        for s, e, norm in entries:
            if e <= start:
                continue  # 在片段之前
            if s >= end:
                break  # 超出片段范围
            # 条目跟片段重叠
            if s < start:
                continue  # 条目在片段中间开始，跳过
            # 条目从片段开头开始，检查是否是语气词
            if norm in FILLER_WORDS or (len(norm) <= 2 and norm in FILLER_WORDS):
                new_start = e  # 跳过这个语气词条目，start设为条目结尾
                trim_count += 1
            else:
                break  # 遇到非语气词，停止
        new_dur = end - new_start
        if new_dur < 2.0:
            # 裁掉语气词后太短，保留原样
            trimmed.append((ct, text, start, end, score, dur))
        else:
            trimmed.append((ct, text, new_start, end, score, new_dur))

    if trim_count:
        _log(f"语气词裁剪: 裁掉 {trim_count} 个片段开头的语气词")

    return trimmed


def _fix_clip_boundaries(clips, cleaned_srt, log_fn=None):
    """
    检查每个片段的 start/end 是否切割了完整的 SRT 句子.
    如果切割了，自动扩展边界到最近的 SRT 句子边界.
    """
    def _log(msg):
        if log_fn: log_fn(msg)

    if not clips or not cleaned_srt:
        return clips

    # 解析 SRT 为 entries: [(start_s, end_s, text), ...]
    entries = []
    lines = cleaned_srt.strip().split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = re.match(
            r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})',
            line)
        if m:
            start_s = (int(m.group(1))*3600 + int(m.group(2))*60 +
                       int(m.group(3)) + int(m.group(4))/1000.0)
            end_s = (int(m.group(5))*3600 + int(m.group(6))*60 +
                     int(m.group(7)) + int(m.group(8))/1000.0)
            text = ""
            j = i + 1
            while j < len(lines) and lines[j].strip() and '-->' not in lines[j]:
                text += lines[j].strip()
                j += 1
            entries.append((start_s, end_s, text.strip()))
            i = j
        else:
            i += 1

    if not entries:
        return clips

    # [PATCH] SRT max end time as safety ceiling
    srt_max_end = max(e for s, e, t in entries) + 0.5

    fixed_clips = []
    fix_count = 0

    for ct, text, start, end, score, dur in clips:
        new_start = start
        new_end = end

        # [PATCH] Safety: clamp clip time to SRT range
        if end > srt_max_end:
            new_end = min(end, srt_max_end)
            new_start = min(start, srt_max_end - 1.0)
            if new_start >= new_end:
                new_start = max(0, new_end - dur)
            if new_end - new_start < 2.0:
                fix_count += 1
                continue

        # 检查 start 是否在某个 SRT entry 的中间(而非起始点)
        # Hook不做前向延伸，保持AI选的精确起始时间
        is_hook = 'hook' in ct.lower()
        for s, e, t in entries:
            if abs(s - start) < 0.3:
                break
            if s < start < e:
                if not is_hook and start - s <= 1.5:
                    new_start = s
                    fix_count += 1
                break

        # 检查 end 是否在某个 SRT entry 的中间(而非结束点)
        # Close不做后向截断，确保结尾完整不丢字
        is_close = 'close' in ct.lower()
        for s, e, t in entries:
            if abs(e - end) < 0.3:
                break
            if s < end < e:
                if not is_close and e - end <= 1.5:
                    new_end = e
                    fix_count += 1
                # close片段：如果end在SRT条目中间，延伸到条目末尾确保不丢字
                if is_close:
                    new_end = e
                    fix_count += 1
                break

        # [PATCH] Secondary clamp after boundary fix
        if new_end > srt_max_end:
            new_end = srt_max_end
            if new_end - new_start < 2.0:
                continue

        new_dur = new_end - new_start
        fixed_clips.append((ct, text, new_start, new_end, score, new_dur))

    if fix_count:
        total_before = sum(c[5] for c in clips)
        total_after = sum(c[5] for c in fixed_clips)
        _log(f"边界修复: 修复 {fix_count} 处截断，时长 {total_before:.1f}s -> {total_after:.1f}s")

        # [修复] 检查相邻片段重叠，截断到中点
        # 注意：不排序！保持AI编排的叙事顺序（hook→product→close）
        if len(fixed_clips) >= 2:
            overlap_fixed = 0
            for i in range(len(fixed_clips) - 1):
                ct1, t1, s1, e1, sc1, d1 = fixed_clips[i]
                ct2, t2, s2, e2, sc2, d2 = fixed_clips[i + 1]
                if e1 > s2:  # 重叠
                    mid = (e1 + s2) / 2
                    fixed_clips[i] = (ct1, t1, s1, mid, sc1, mid - s1)
                    fixed_clips[i + 1] = (ct2, t2, mid, e2, sc2, e2 - mid)
                    overlap_fixed += 1
            if overlap_fixed:
                _log(f"边界重叠修复: {overlap_fixed} 处重叠已截断")

    # [强制排序] hook必须在第一，close必须在最后
    if len(fixed_clips) >= 3:
        hooks = [c for c in fixed_clips if 'hook' in c[0].lower()]
        closes = [c for c in fixed_clips if 'close' in c[0].lower()]
        others = [c for c in fixed_clips if 'hook' not in c[0].lower() and 'close' not in c[0].lower()]
        if hooks or closes:
            fixed_clips = (hooks[:1] if hooks else []) + others + (closes if closes else [])
            _log(f"排序修正: hook首位={bool(hooks)}, close末位={bool(closes)}")

    # [增强] 结尾完整性检查
    if fixed_clips:
        last = fixed_clips[-1]
        ct, text, start, end, score, dur = last
        t = text.rstrip()
        is_incomplete = False
        # 规则1: 以语气词/助词结尾且句子较短
        weak_endings = ["穿到", "的", "了", "呀", "呢", "吧", "咯", "啊", "哈", "啦", "嘛",
                        "觉得", "感觉", "然后", "就是", "其实", "不过", "而且", "但是"]
        for w in weak_endings:
            if t.endswith(w) and len(t) <= 30:
                is_incomplete = True
                break
        # 规则2: 悬空结尾（后面应有结论但没有）
        if not is_incomplete:
            dangling = ["你会觉得", "就感觉", "就发现", "你就会", "你会看到",
                        "一件", "一套", "一条", "一个", "这个"]
            for p in dangling:
                if t.endswith(p):
                    is_incomplete = True
                    break
        # 规则3: 用SRT实际内容验证片段末尾
        if not is_incomplete and entries:
            for s, e, txt in entries:
                if abs(e - end) < 0.5 and txt:
                    txt_c = txt.rstrip()
                    for w in ["然后", "就是", "其实", "而且", "但是", "不过", "所以"]:
                        if txt_c.endswith(w) and len(txt_c) <= 15:
                            is_incomplete = True
                            break
        if is_incomplete and len(fixed_clips) >= 2:
            _log(f"结尾片段不完整: [{start:.1f}-{end:.1f}] {t[-25:]}")
            fixed_clips.pop()
            total_after = sum(c[5] for c in fixed_clips)
            _log(f"结尾修复: 移除最后片段，剩余 {len(fixed_clips)} 段, 总时长 {total_after:.1f}s")

    return fixed_clips


# ============================================================
# 主播互动废话过滤
# ============================================================


# ============================================================
# 明星名字过滤
# ============================================================
CELEBRITY_NAMES = [
    "巩俐", "杨帢", "赵丽颖", "范冰冰", "刘亦菲",
    "周迅", "李小龙", "谢霆锋", "曾毅嘉", "张学友",
    "戴小舩", "薛之谦", "马思纯", "关晓彤", "刘诗诗",
    "孙丽", "曹频幻", "威尔", "克莱尔", "泰勒",
    "小S", "成龙", "黄晓明", "邱毓娜", "秦岚",
    "舒淇", "以别蒋", "王丽坤", "钟楚红", "张宇",
    "刘德华", "邱淇", "刘芳", "邱淇",
]

def _filter_celebrity(clips, log_fn=None):
    """移除包含明星名字的片段"""
    def _log(msg):
        if log_fn: log_fn(msg)
    original = len(clips)
    filtered = []
    for clip in clips:
        text = clip[1]
        hit = any(name in text for name in CELEBRITY_NAMES)
        if hit:
            _log(f"明星过滤: 移除 [{clip[2]:.1f}-{clip[3]:.1f}]")
        else:
            filtered.append(clip)
    if len(filtered) < original:
        _log(f"明星过滤: {original} -> {len(filtered)}")
    return filtered


# ============================================================
# CTA 误判校验
# ============================================================
FAKE_CTA_KEYWORDS = [
    "帮我拿下包包", "看下后台", "库存哈",
    "稍微等我", "等一下哈", "我去看下",
    "有没有库存", "面料库存", "拿下包包",
    "我去看一下", "看看后台",
]

def _validate_cta(clips, log_fn=None):
    """CTA校验:移除误判为CTA的片段，真正的CTA必须包含行动号召关键词"""
    def _log(msg):
        if log_fn: log_fn(msg)

    # 真正CTA关键词：拍下/上车/上链接/321/抢/刷/刷新/点关注/去拍
    REAL_CTA_KW = ["拍下", "上车", "上链接", "321", "抢", "刷新",
                   "点好关注", "去拍", "直接拍", "拍", "下单",
                   "小黄车", "链接", "刷", "入手"]

    original = len(clips)
    filtered = []
    has_real_cta = False
    for clip in clips:
        if clip[0] == "call_to_action":
            text = clip[1]
            # 先检查是否包含真正CTA关键词
            is_real = any(kw in text for kw in REAL_CTA_KW)
            if is_real:
                has_real_cta = True
                filtered.append(clip)
                continue
            # 再检查是否匹配假CTA
            is_fake = any(kw in text for kw in FAKE_CTA_KEYWORDS)
            if is_fake:
                _log(f"CTA误判移除: [{clip[2]:.1f}-{clip[3]:.1f}] {text[:20]}")
                continue
            # 既不是真CTA也不匹配假CTA关键词 -> 尺码/无效信息
            _log(f"CTA无效移除(无行动号召): [{clip[2]:.1f}-{clip[3]:.1f}] {text[:20]}")
            continue
        filtered.append(clip)

    removed = original - len(filtered)
    if removed > 0:
        _log(f"CTA校验: {original} -> {len(filtered)}")

    if not has_real_cta and len(filtered) >= 1:
        _log("CTA警告: 没有真正的行动号召片段，结尾可能缺乏CTA力度")

    return filtered


# ============================================================
# 语义重复过滤: 同一卖点反复出现只保留第一次(代码层兜底)
# ============================================================
def _filter_semantic_repeat(clips, log_fn=None):
    """代码层兜底：检测片段间的语义重复，只保留信息更完整的那条。采用保守策略避免误删。"""
    def _log(msg):
        if log_fn: log_fn(msg)

    if len(clips) < 3:
        return clips

    _stop = set("的 了 在 是 我 有 和 就 都 也 不 人 这 那 他 到 说 要 会 着 过 把 得 能 可以 很 被 让 给 比 从 向 还 又 而 但 如果 因为 所以 虽然 但是 而且 或者 以及 一个 一些 什么 这个 那个 这些 那些 哪 几 多少 呢 呀 呵 呢 然后 所以说".split())
    _punct = set("，。！？、；：“”‘’（）《》【】…—·")

    def _kw(text):
        return set(c for c in text if c not in _stop and c not in _punct and c.strip())

    original = len(clips)
    keep = []
    kept_kws = []
    for clip in clips:
        ct, text, start, end, score, dur = clip
        kw = _kw(text)
        if len(kw) < 2:
            keep.append(clip); kept_kws.append(kw); continue
        dup = False
        for pi, pk in enumerate(kept_kws):
            if len(pk) < 2:
                continue
            ov = len(kw & pk)
            r = ov / min(len(kw), len(pk))
            if r > 0.6 and ov >= 3:
                dup = True
                _log(f"语义重复: ∼{keep[pi][1][:15]}∼ 与 ∼{text[:15]}∼ 重叠{ov}个(r={r:.0%})")
                break
        if not dup:
            keep.append(clip); kept_kws.append(kw)
    removed = original - len(keep)
    if removed > 0:
        _log(f"语义重复过滤: {original} -> {len(keep)} (去掉{removed}条)")
    return keep


def _filter_price_and_cta(clips, log_fn=None):
    """硬过滤：删除包含价格/报价/购物车/下单/链接的片段，AI Prompt拦不住就用代码拦"""
    def _log(msg):
        if log_fn: log_fn(msg)

    # 价格数字模式：2-4位数字+元/块，或纯数字价格（99/199/299等）
    price_patterns = [
        re.compile(r'\d{2,4}\s*[元块]'),           # 199元, 300块
        re.compile(r'[到拿]手[价]?\s*\d'),          # 到手价199, 拿到手99
        re.compile(r'\d{2,4}\s*[多几]?[块元]'),     # 300多块
        re.compile(r'(?:只要|才|仅)[一两三四五六七八九十百千万\d]+[块元]'),  # 只要199元
        re.compile(r'原价|秒杀价|福利价|破价|到手价'),
    ]
    # 绝对禁止词（出现在片段中就删除）
    forbidden_words = [
        '购物车', '小黄车', '左下角', '链接', '下单', '拍下', '去拍',
        '下单', '领券', '满减', '立减', '321上', '321冲',
        'free', 'Free', 'FREE', '免费', '抽奖', '赠品', '送你',
        '榜二', '置顶视频', '拼手速', '抢购', '限量',
    ]

    filtered = []
    removed = 0
    for ct, text, s, e, sc, d, *_ in clips:
        clean = re.sub(r'【|】', '', text)
        # 检查禁止词
        has_forbidden = any(w in clean for w in forbidden_words)
        # 检查价格模式
        has_price = any(p.search(clean) for p in price_patterns)
        if has_forbidden or has_price:
            removed += 1
            reason = []
            if has_forbidden:
                matched = [w for w in forbidden_words if w in clean]
                reason.append(f"禁止词:{','.join(matched)}")
            if has_price:
                reason.append("价格模式")
            _log(f'  价格过滤: 删除 [{ct}] "{clean[:30]}..." ({";".join(reason)})')
            continue
        filtered.append(tuple(c) if isinstance(c, (list,tuple)) and len(c)>6 else (ct, text, s, e, sc, d, ""))

    if removed:
        _log(f"价格硬过滤: 删除 {removed} 段含价格/CTA的片段，剩余 {len(filtered)} 段")
    return filtered

def _filter_host_interaction(clips, log_fn=None):
    """移除纯主播回弹幕的废话片段"""
    def _log(msg):
        if log_fn: log_fn(msg)

    if not clips:
        return clips

    cleaned = []
    removed = 0
    for ct, text, s, e, sc, d, *_ in clips:
        is_noise = False
        # 短片段(<3秒)更容易是废话
        if d < 4.0:
            clean = text.strip()
            for pattern in HOST_CHAT_PATTERNS:
                if pattern.match(clean):
                    is_noise = True
                    break
        if is_noise:
            removed += 1
            _log(f"废话过滤: 移除 '{text[:20]}'({d:.1f}s，主播回弹幕)")
        else:
            cleaned.append(tuple(c) if isinstance(c, (list,tuple)) and len(c)>6 else (ct, text, s, e, sc, d, ""))

    if removed:
        _log(f"废话过滤: 共移除 {removed} 个片段")
    return cleaned


# ============================================================
# 叙事连贯性检查
# ============================================================
def _check_narrative_coherence(clips, log_fn):
    """后处理:检查叙事连贯性，修补常见问题"""
    def _log(msg):
        if log_fn: log_fn(msg)

    if len(clips) < 3:
        return clips

    # 1. 相邻片段内容重复检测(简单文本相似度)
    def _text_similarity(t1, t2):
        """简单的字符级 Jaccard 相似度"""
        if not t1 or not t2:
            return 0
        s1, s2 = set(t1), set(t2)
        if not s1 or not s2:
            return 0
        return len(s1 & s2) / len(s1 | s2)

    i = 0
    removed_dup = 0
    while i < len(clips) - 1:
        _, t1, _, _, _, _ = clips[i]
        _, t2, _, _, _, _ = clips[i + 1]
        sim = _text_similarity(t1, t2)
        if sim > 0.6:
            # 保留时长更长的那个
            if clips[i][5] >= clips[i + 1][5]:
                clips.pop(i + 1)
            else:
                clips.pop(i)
                i = max(0, i - 1)
            removed_dup += 1
        else:
            i += 1
    if removed_dup:
        _log(f"叙事检查: 移除 {removed_dup} 个重复片段")

    # 2. 过短片段扩展(<2秒的向前或向后扩展到完整句)
    # 解析 SRT 找到前后时间戳来扩展
    # 这里我们只标记，实际扩展在 _extend_clips 里处理
    short_clips = [i for i, c in enumerate(clips) if c[5] < 2.0]
    if short_clips:
        _log(f"叙事检查: {len(short_clips)} 个片段 <2秒，将尝试扩展")

    # 3. 黄金链路跳跃检测
    chain_types = {c[0] for c in clips}
    chain_idx = {t: i for i, t in enumerate(GOLDEN_CHAIN)}
    used_indices = sorted([chain_idx[t] for t in chain_types if t in chain_idx])
    if len(used_indices) >= 2:
        gaps = []
        for j in range(1, len(used_indices)):
            if used_indices[j] - used_indices[j - 1] > 2:
                gaps.append(f"{GOLDEN_CHAIN[used_indices[j-1]]}→{GOLDEN_CHAIN[used_indices[j]]}")
        if gaps:
            _log(f"叙事检查: 链路跳跃 {', '.join(gaps)}")

    return clips


# ============================================================
# 选片后跨品类扫描(第二道防线)
# ============================================================
def _post_filter_cross_category(clips, cleaned_srt, log_fn):
    """扫描每个片段文本，踢出包含非主品类关键词的片段"""
    def _log(msg):
        if log_fn: log_fn(msg)

    if len(clips) < 3:
        return clips

    # 构建品类词库
    ALL_CATEGORIES = {
        "上衣": ["上衣","T恤","衬衫","针织衫","卫衣","打底衫","小衫","衬衣","网纱罩衫","罩衫",
                 "毛衣","短袖","长袖","吊带衫","背心","抹胸","针织"],
        "裤子": ["裤子","牛仔裤","阔腿裤","打底裤","工装裤","休闲裤","长裤","短裤","九分裤",
                 "小脚裤","直筒裤","牛奶裤","烟管裤","哈伦裤","裤"],
        "裙子": ["裙子","连衣裙","半身裙","A字裙","包臀裙","长裙","短裙","百褶裙","裙",
                 "吊带裙","碎花裙","鱼尾裙","蛋糕裙","一步裙","旗袍裙","吊带","背心裙"],
        "外套": ["外套","风衣","西装","羽绒服","大衣","夹克","棉服","皮衣","开衫","马甲"],
        "套装": ["套装","四件套","三件套","两件套","三件","四件","成套","整套","全套"],
        "鞋子": ["鞋","鞋子","凉鞋","运动鞋","高跟鞋","平底鞋","单鞋","靴子","老爹鞋"],
    }

    # 从 SRT 统计每个品类的出现频率，确定主品类
    cat_counts = {}
    for cat, keywords in ALL_CATEGORIES.items():
        count = sum(1 for kw in keywords if kw in cleaned_srt)
        if count > 0:
            cat_counts[cat] = count
    if not cat_counts:
        return clips
    main_cat = max(cat_counts, key=cat_counts.get)
    main_kws = set(ALL_CATEGORIES.get(main_cat, []))

    # 套装保护：如果 SRT 中出现套装相关词，跳过跨品类踢出（套装天然多品类）
    suit_keywords = ["套装", "整套", "全套", "成套", "两件套", "三件套", "四件套", "三件", "四件"]
    if any(kw in cleaned_srt for kw in suit_keywords):
        _log(f"检测到套装场景，跳过跨品类踢出（主品类={main_cat}）")
        return clips

    # 关联品类保护：套装/上衣+裤子/上衣+裙子 等常见搭配互不踢
    # 定义关联组
    related_groups = [
        {"上衣", "裙子"}, {"上衣", "裤子"}, {"裙子", "外套"}, {"裤子", "外套"}
    ]
    protected_cats = set()
    for group in related_groups:
        if main_cat in group:
            protected_cats.update(group)
    if protected_cats:
        _log(f"关联品类保护: {main_cat} 的关联品类 {protected_cats - {main_cat}} 不会被踢出")

    # 扫描每个片段
    kept = []
    removed = 0
    for ct, text, s, e, sc, d, *_ in clips:
        # 检查是否包含非主品类关键词
        has_other = False
        other_cat = None
        for cat, keywords in ALL_CATEGORIES.items():
            if cat == main_cat:
                continue
            if cat in protected_cats:
                continue
            for kw in keywords:
                if kw in text:
                    has_other = True
                    other_cat = cat
                    _log(f"跨品类踢出 [{ct}] {text[:30]}...(含'{kw}'，非主品类{main_cat})")
                    break
            if has_other:
                break
        # 同时检查是否有主品类关键词(双重确认)
        if has_other:
            has_main = any(kw in text for kw in main_kws)
            if has_main:
                # 同时有主品类和次品类 → 可能是搭配说明，保留
                kept.append(tuple(c) if isinstance(c, (list,tuple)) and len(c)>6 else (ct, text, s, e, sc, d, ""))
            else:
                removed += 1
        else:
            kept.append(tuple(c) if isinstance(c, (list,tuple)) and len(c)>6 else (ct, text, s, e, sc, d, ""))

    if removed:
        _log(f"跨品类扫描: 踢出 {removed} 个非{main_cat}片段，保留 {len(kept)} 个")
    return kept


def _extend_clips(clips, log_fn, target_min=45, target_max=65, max_end=None):
    def _log(msg):
        if log_fn: log_fn(msg)
    if not clips:
        return clips
    total = sum(c[5] for c in clips)
    if total >= target_min:
        return clips

    deficit = target_min - total
    _log(f"自动延伸片段: 当前 {total:.1f}s, 目标 {target_min}s, 差 {deficit:.1f}s")

    # 按 start 时间排序，用于计算每个片段的延伸上限
    sorted_clips = sorted(enumerate(clips), key=lambda x: x[1][2])
    clip_end_limits = {}  # idx -> max allowed end (next clip's start - 0.5s gap)
    for k in range(len(sorted_clips)):
        idx_k = sorted_clips[k][0]
        if k + 1 < len(sorted_clips):
            next_start = sorted_clips[k + 1][1][2]
            clip_end_limits[idx_k] = next_start - 0.5  # leave 0.5s gap
        else:
            clip_end_limits[idx_k] = max_end or 99999

    # 按时长从小到大排序，优先延伸最短的片段
    indexed = list(enumerate(clips))
    indexed.sort(key=lambda x: x[1][5])

    for idx, (ct, text, start, end, score, dur) in indexed:
        if total >= target_min:
            break
        max_dur = 15
        if dur >= max_dur:
            continue
        end_limit = clip_end_limits.get(idx, max_end or 99999)
        can_add = min(max_dur - dur, deficit, end_limit - end)
        if can_add <= 0:
            continue
        new_end = end + can_add
        new_dur = new_end - start
        clips[idx] = (ct, text, start, new_end, score, new_dur)
        total += can_add
        deficit -= can_add

    total = sum(c[5] for c in clips)
    _log(f"延伸完成: {len(clips)} 片段, 总时长 {total:.1f}s")
    return clips


# ============================================================
# 品类一致性检测:识别主品类，其他品类片段后置
# ============================================================
# 品类关键词(简体+繁体)
PRODUCT_CATEGORIES = {
    "上衣": ["上衣", "衬衫", "卫衣", "T恤", "针织", "毛衣", "打底衫",
             "针织衫", "短袖", "长袖", "吊带衫", "背心", "抹胸",
             "外套", "大衣", "风衣", "夹克", "西装", "棉服", "羽绒服",
             "皮衣", "开衫", "马甲",
             "襯衫", "毛衣", "外套", "風衣", "夾克", "羽絨服"],
    "裤子": ["裤子", "裤", "阔腿裤", "直筒裤", "牛仔裤", "小脚裤", "打底裤",
             "休闲裤", "运动裤", "西裤", "长裤", "短裤", "九分裤",
             "褲子", "褲", "牛仔褲", "闊腿褲", "直筒褲"],
    "裙子": ["裙子", "裙", "连衣裙", "半身裙", "长裙", "短裙", "A字裙",
             "百褶裙", "包臀裙", "鱼尾裙", "吊带裙", "碎花裙", "蛋糕裙",
             "一步裙", "背心裙", "吊带",
             "連衣裙", "半身裙", "百褶裙"],
    "套装": ["套装", "两件套", "三件套", "穿搭",
             "兩件套", "三件套"],
}

def _detect_product_category(text):
    """检测文本提到的品类，返回品类名或 None"""
    scores = {}
    for cat, keywords in PRODUCT_CATEGORIES.items():
        hits = sum(1 for kw in keywords if kw in text)
        if hits > 0:
            scores[cat] = hits
    if not scores:
        return None
    return max(scores, key=scores.get)


def _enforce_product_coherence(clips, log_fn):
    """检测主品类，将其他品类的片段移到末尾，前2个片段强制主品类"""
    def _log(msg):
        if log_fn: log_fn(msg)

    if len(clips) < 3:
        return clips

    # 统计每个品类出现次数
    cat_count = {}
    for ct, text, s, e, sc, d, *_ in clips:
        cat = _detect_product_category(text)
        if cat:
            cat_count[cat] = cat_count.get(cat, 0) + 1

    if not cat_count:
        return clips  # 无法识别品类

    main_cat = max(cat_count, key=cat_count.get)
    if cat_count[main_cat] < 2:
        return clips  # 没有明显主品类

    # 分类:主品类 vs 其他品类
    main_clips = []
    other_clips = []
    for c in clips:
        cat = _detect_product_category(c[1])
        if cat and cat != main_cat:
            other_clips.append(c)
        else:
            main_clips.append(c)

    if other_clips:
        _log(f"品类检测: 主品类={main_cat}，{len(other_clips)} 个跨品类片段后置")
        clips = main_clips + other_clips

    # 额外保护:前2个片段如果有无法识别品类的，且后面有主品类片段，交换
    if len(clips) >= 3:
        for i in range(min(2, len(clips))):
            c = clips[i]
            cat = _detect_product_category(c[1])
            if cat is None:
                # 找后面最近的主品类片段交换
                for j in range(i + 1, len(clips)):
                    cj_cat = _detect_product_category(clips[j][1])
                    if cj_cat == main_cat:
                        clips[i], clips[j] = clips[j], clips[i]
                        _log(f"品类保护: 位置{i+1}的片段与位置{j+1}交换(确保开头是主品类)")
                        break

    return clips


# ============================================================
# 终剪防线:移除孤立跨品类片段
# ============================================================
def _remove_orphan_cross_category(clips, log_fn):
    """AI 输出片段列表后最终扫描，移除无搭配绑定的跨品类片段"""
    def _log(msg):
        if log_fn: log_fn(msg)

    if len(clips) < 3:
        return clips

    # 找出出现最多的品类 = 主品类
    cat_count = {}
    for c in clips:
        cat = _detect_product_category(c[1])
        if cat:
            cat_count[cat] = cat_count.get(cat, 0) + 1

    if not cat_count:
        return clips

    main_cat = max(cat_count, key=cat_count.get)
    if cat_count[main_cat] < 2:
        return clips

    # 套装保护：套装天然包含多品类，跳过孤立踢出
    all_text = "".join(c[1] for c in clips)
    suit_kws = ["套装", "整套", "全套", "成套", "两件套", "三件套", "四件套"]
    if any(kw in all_text for kw in suit_kws):
        _log(f"检测到套装场景，跳过孤立跨品类踢出")
        return clips

    main_kws = set(PRODUCT_CATEGORIES.get(main_cat, []))
    match_kws = {"搭", "配", "搭配", "配着穿", "搭什么", "配什么", "同款", "两件套", "穿搭"}

    cleaned = []
    removed_texts = []
    for c in clips:
        ct, text, s, e, sc, d = c[0], c[1], c[2], c[3], c[4], c[5]
        other_cat = _detect_product_category(text)
        if other_cat and other_cat != main_cat:
            # 跨品类 → 必须有主品类词+搭配词才合法
            has_main = any(kw in text for kw in main_kws)
            has_match = any(kw in text for kw in match_kws)
            if not (has_main and has_match):
                removed_texts.append(text)
                continue
        cleaned.append(c)

    if removed_texts:
        for t in removed_texts[:3]:
            _log(f"已移除孤立跨品类片段:{t[:30]}...(无搭配绑定，突兀违规)")
        if len(removed_texts) > 3:
            _log(f"  ...共移除 {len(removed_texts)} 个孤立跨品类片段")

    return cleaned


# ============================================================
# 补充片段(AI 结果不足时用关键词自动补齐)
# ============================================================
def _supplement_clips(existing_clips, cleaned_srt, log_fn, min_total=4):
    """从清洗后的字幕中补充片段，直到达到 min_total 个"""
    def _log(msg):
        if log_fn: log_fn(msg)
    if len(existing_clips) >= min_total:
        return existing_clips

    from config import CLIP_KEYWORDS, NEGATIVE_KEYWORDS, FILLER_WORDS, NEGATIVE_SIGNALS, BAN_PATTERNS

    # 解析清洗后的 SRT
    entries = []
    for line in cleaned_srt.strip().split("\n"):
        m = re.match(r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})', line)
        if m:
            start_s = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3)) + int(m.group(4))/1000.0
            end_s = int(m.group(5))*3600 + int(m.group(6))*60 + int(m.group(7)) + int(m.group(8))/1000.0
            entries.append({"start": start_s, "end": end_s, "dur": end_s - start_s})

    # 收集已有片段的时间范围(避免重叠)
    used_ranges = [(c[2], c[3]) for c in existing_clips]

    # 关键词打分
    candidates = []
    for i, line in enumerate(cleaned_srt.strip().split("\n")):
        # 找时间戳行后的文本行
        if not re.match(r'\d{2}:\d{2}', line):
            continue
        text = ""
        lines_list = cleaned_srt.strip().split("\n")
        j = lines_list.index(line)
        if j + 1 < len(lines_list):
            text = lines_list[j + 1].strip()
        if not text or len(text) < 5:
            continue

        # 获取对应时间
        if i >= len(entries):
            continue
        entry = entries[i]
        start, end, dur = entry["start"], entry["end"], entry["dur"]
        if dur < 1.5 or dur > 15:
            continue

        # 检查是否与已有片段重叠
        overlap = False
        for us, ue in used_ranges:
            if max(start, us) < min(end, ue):
                overlap = True; break
        if overlap:
            continue

        # 过滤
        skip = False
        clean_t = text
        for fw in FILLER_WORDS:
            clean_t = clean_t.replace(fw, "")
        for ban in BAN_PATTERNS:
            if re.search(ban, text):
                skip = True; break
        if skip:
            continue
        for sig in NEGATIVE_SIGNALS:
            if sig in text:
                skip = True; break
        if skip:
            continue

        # 打分
        best_type, best_score = "highlight", 0
        for ct, cfg in CLIP_KEYWORDS.items():
            hits = sum(1 for kw in cfg.get("keywords", []) if kw in text)
            score = hits * cfg.get("weight", 20)
            if score > best_score:
                best_score = score
                best_type = ct
        for neg in NEGATIVE_KEYWORDS:
            if neg in text and len(text) < 20:
                best_score -= 15
        if best_score >= 15:
            candidates.append((best_type, text, start, end, best_score, dur))

    # 按分数排序，补充到 min_total
    candidates.sort(key=lambda c: (-c[4], c[2]))  # 分数高优先，时间靠前优先
    existing = list(existing_clips)
    for cand in candidates:
        if len(existing) >= min_total:
            break
        ct, text, s, e, sc, d = cand[0], cand[1], cand[2], cand[3], cand[4], cand[5]
        # 检查重叠
        overlap = False
        for ex in existing:
            if max(s, ex[2]) < min(e, ex[3]):
                overlap = True; break
        if not overlap:
            existing.append(cand)

    if len(existing) > len(existing_clips):
        _log(f"WARNING: 关键词补充: {len(existing_clips)} -> {len(existing)} 片段")
    return existing


# ============================================================
# 兜底逻辑
# ============================================================
def fallback_clips(srt_path, log_fn=None, force_category=None):
    def _log(msg):
        if log_fn: log_fn(msg)

    _log("WARNING: 关键词兖底选片(非AI, 质量可能不佳, 建议检查API后重试)")
    from srt_parser import open_srt
    from config import CLIP_KEYWORDS, NEGATIVE_KEYWORDS, FILLER_WORDS, NEGATIVE_SIGNALS, EMOTION_WORDS, BAN_PATTERNS

    try:
        subtitles, _ = open_srt(srt_path)
    except Exception as e:
        _log(f"兜底: SRT 解析失败 {e}"); return []

    scored = []
    for sub in subtitles:
        text = sub.text.strip()
        if not text: continue
        start = sub.start[0]*3600 + sub.start[1]*60 + sub.start[2] + sub.start[3]/1000.0
        end = sub.end[0]*3600 + sub.end[1]*60 + sub.end[2] + sub.end[3]/1000.0
        duration = end - start
        if duration < 1.5 or duration > 12:
            continue
        for fw in FILLER_WORDS:
            text = text.replace(fw, "")
        text = text.strip()
        if len(text) < 5:
            continue
        skip = False
        for ban in BAN_PATTERNS:
            if re.search(ban, text):
                skip = True; break
        if skip:
            continue
        for sig in NEGATIVE_SIGNALS:
            if sig in text:
                skip = True; break
        if skip:
            continue
        best_type, best_score = "highlight", 0
        for ct, cfg in CLIP_KEYWORDS.items():
            hits = sum(1 for kw in cfg.get("keywords", []) if kw in text)
            score = hits * cfg.get("weight", 20)
            if score > best_score:
                best_score = score
                best_type = ct
        for neg in NEGATIVE_KEYWORDS:
            if neg in text:
                best_score -= 20
        if best_score < 15:
            continue
        scored.append({
            "type": best_type, "text": text,
            "start": start, "end": end,
            "score": best_score, "duration": duration,
        })

    result = []
    for ct in SIMPLE_CHAIN:
        cands = [b for b in scored if b["type"] == ct]
        if not cands:
            continue
        best = max(cands, key=lambda b: b["score"])
        result.append((best["type"], best["text"], best["start"], best["end"],
                       best["score"], best["duration"]))
    if result:
        total = sum(c[5] for c in result)
        _log(f"兜底: {len(result)} 片段, 总时长 {total:.1f}s")
        for ct, text, s, e, sc, d in result:
            _log(f"  {ct:<16s} | {s:.1f}-{e:.1f}s ({d:.1f}s) | {text}")
    return result


def is_enabled():
    settings = load_settings()
    # 有 API Key 就启用 AI，不需要额外勾选
    # 之前要求 enabled=True，导致很多用户填了 Key 但没勾启用，走关键词兜底产出垃圾
    return bool(settings.get("api_key"))
