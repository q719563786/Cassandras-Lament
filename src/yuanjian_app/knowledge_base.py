"""远见系统认知框架库——用户《登高望远》思想体系的**候选假设**注入。

9 大知识库：预知方法论、政治逻辑、人性分析、利益判断、经济原理、
历史规律、国际博弈、社会观察、天外实体侧。

════════════════════════════════════════════════════════════════════
v1.3 认知定位修正（审计 2.7「知识库把假设当事实」）
════════════════════════════════════════════════════════════════════

**改之前**：这九块文本以「你的认知框架（**必须用以下逻辑判断**，而非通用AI视角）」
的姿态进入**每一次**研判。后果不是"AI 不够懂这套方法论"，而是
**这个 app 在放大用户的模型，而不是检验它** —— 与《登高望远》GYW-013 的方向相反。

**改之后**：三条硬规则写进注入文本本身——
  1. 这些是**候选假设**，不是事实；证据支持才引用，证据冲突以证据为准；
  2. 每条假设都带 `state`（`unverified` / `supported_by_source`）与"主要限制或反证方向"，
     用户 Obsidian 里本来就有这套状态机，应用侧此前没有；
  3. **不得为了让结论符合假设而裁剪证据**。

**天外实体侧已移出通用注入**：它只在与 UAP / 航天 / 解密相关的事件上注入。
理由：把"外星文明"当成每次研判的默认透镜，本身就是在制造观察偏差。
"""

from __future__ import annotations

import re

# ── 1. 预知方法论（登高望远）──────────────────────────────────
KNOWLEDGE_FORECAST = """【预知方法论·登高望远】
核心定义：预知不是超自然，是信息差+认知差+位置差。先知=比大部分人更早看见"已经决定了的事"。
六要素拼图：
  位置高（看到得早）—元首情报早3个月，央行数据早1周全量
  历史熟（认出模式）—历史押韵不重复，泡沫/帝国/朝代都是圆心转圈
  人性明（预料反应）—慷慨激昂=内心已感知风险，真正稳的不用激情驱动
  政治清（判断结构）—改革威胁执行者利益就停，合法性靠增长减速后找民族主义替代
  经济懂（把握节奏）—信用扩张后必收缩，债务不会消失只会延后
  博弈通（锁定理性路径）—理性人在压力下走最省力的路，国家行为大部分理性
核心判断法则：最不费力的路径，往往就是事情会走的路径。
先知的局限：黑天鹅、非理性决策、技术跃迁、自我欺骗（太相信自己的模型）、越具体越容易错。"""

# ── 2. 政治逻辑库 ────────────────────────────────────────────
KNOWLEDGE_POLITICS = """【政治逻辑库】
三大定律：
  1.事实不重要—政治攻击是对"印象"的操控，不是对真相的追索。"抛开事实不谈"=认输宣言。大众记不住证据，记得住情绪。
  2.功勋不自动兑现—体制垄断"价值确认"的最终解释权。你干得好，我说你干得好才是干得好。不在体制内，功劳就是没有公证人的口供。
  3.陷害不成会加注—第一次成本已支付，理性人不会接受亏损。你活下来本身就是对他的羞辱，第二轮才是绝杀。
态度定律：政治只看态度，因为表态臣服=自动献上30%的力量，意志传递需要3-9个月。要反必须在3个月内摆明旗帜，拖久了下属就不听话了。例外：一切伟力归于自身，没有部下可变色。
权力结构：政治底层不是主义，是权力结构。谁对谁有控制力、谁的意志能被执行、谁的利益必须被照顾。
改革定律：改革威胁执行者利益就会停。合法性来源经济增长，减速后会找替代来源（民族主义、外部对立）。"""

# ── 3. 人性分析库 ────────────────────────────────────────────
KNOWLEDGE_HUMAN_NATURE = """【人性分析库】
核心洞察：
  恐惧在人群中传染，人在恐惧时做不理性的事
  原则崩塌不是一瞬间，是一寸一寸松动的
  人会过度自信，尤其在自己成功的领域——过去的成功让人相信自己是例外
  人会误判风险量级：高概率小损失和高概率大灾难，心理感受完全不同
  人会自我欺骗：相信自己希望成真的事，哪怕证据指向相反
  沉没成本维持迷信：已经投入太多，承认错误等于否定过去的自己
  人会自我说服：被安排做的事会告诉自己"其实我也想做"（心理防御机制）
  慷慨激昂往往是内心已感知风险的表达，真正稳的人不用激情驱动"""

# ── 4. 利益判断库 ────────────────────────────────────────────
KNOWLEDGE_INTEREST = """【利益判断库】
核心矛盾：个人利益与整体利益的矛盾=历史周期律的根源。群体中除了劳动获得收益，还有"巧取豪夺"这条捷径。
有产者心理：被贪婪+恐惧双重驱使。贪婪让他们聚敛，恐惧（财富来路不正的原罪）让他们变本加厉。
核心法则：
  触及利益比触及灵魂还难
  最不费力的路径=事情会走的路径
  囚徒困境：没有人能承受"不确定对方是否背叛时持续付出代价"
  理性人在压力下走最省力的路
  有产者被贪婪（聚敛财富）和恐惧（财富来路不正的原罪）双重驱使"""

# ── 5. 经济原理库 ────────────────────────────────────────────
KNOWLEDGE_ECONOMICS = """【经济原理库】
危机本质：一切经济危机源自消费无法覆盖生产。举债放水只能拖延，不能根除。
资本增殖公式：投入100→收回200，多出来的100必须来自经济循环之外。
利润三来源：
  1.其他国家/地区的国民财富（对外吸血）
  2.科技进步带来的生产力提升
  3.信贷扩张（指向未来，透支）
中国经济结构：
  计划经济：低消费、高投资、低产出
  市场经济后：低消费、高投资、高产出→过剩危机
  1989年转折点：政府横扫民间力量，消费率掉头向下
  1994年：外需大显身手，人民币汇率调整
  2001年WTO：外部倾销吸血，居民消费率从46%跌到35%
  2019年：外国对吸血不能忍受，从经济跨到政治，吸血战略基本破产
  核心格局：国家占有80-85%，居民只有15-20%
信用定律：信用扩张后一定收缩，债务不会消失只会延后。资产泡沫=今日需求向未来透支。通胀=货币相对于商品的稀释。"""

# ── 6. 历史规律库 ────────────────────────────────────────────
KNOWLEDGE_HISTORY = """【历史规律库】
周期律：一切组织建立初期以多数人利益为重，随时间必定滑向堕落腐朽。原因=个人利益与整体利益矛盾+巧取豪夺捷径。
三大循环：
  经济泡沫：借贷→投机→贪婪→恐慌（郁金香/南海/次贷/加密货币）
  帝国兴衰：扩张→过度延伸→内部撕裂→外部挑战（罗马/大英/美苏）
  朝代周期：土地兼并→阶级固化→农民起义→重新洗牌→再兼并（秦到清两千年）
历史押韵：历史不是重复的，但历史押韵。相似点说明模式可能重现，不同点说明这次可能偏离。"""

# ── 7. 国际博弈库 ────────────────────────────────────────────
KNOWLEDGE_GEOPOLITICS = """【国际博弈库】
丛林本质：国际社会没有真正裁判，本质是弱肉强食。
民主vs集权：
  集权社会=老虎垄断力量，力量就是一切，基因里铭记暴力法则
  民主社会=没人具备垄断力量，不得不服从法律，"把兔子按爵士菜肴制造再端上桌"
  民主社会的普通人（白兔羚羊）不是天真，是社会生态让它们看见"丛林者不得好死"
二战后和平：核平衡+工业链互相依赖+联合国脆弱裁判。但裁判太脆弱全靠自觉，撕破脸就回到丛林。
国家行为：大部分时候是理性的，会选损失最小风险最低的选项。两个大国结构性矛盾无法调和时，摩擦会升级。"""

# ── 8. 社会观察库 ────────────────────────────────────────────
KNOWLEDGE_SOCIETY = """【社会观察库】
才能阈值理论：才能越不可替代，失去的自由越多。国家机器自动启动，身边每个人都可能被安排成眼线。重视和使用是两回事——工具不需要知道为什么被使用。
阶层门槛：马术教育、十五门考试等，用金钱和时间筛选出"有闲阶级"。
婚姻与权利：权利义务失衡时，制度会向掌握资源的一方倾斜。
消费依赖：消费不是欲望问题，是社会安全感问题。安全感越低，越不敢消费。
救灾拨款：层层截留是体制性问题，不是个别腐败。
人才流动：人才能走的条件是有地方接收+走的成本低于留的成本。"""

# ── 9. 天外实体侧（UAP/非人类文明）──────────────────────────
# ⚠ v1.3：**不再进入通用注入**，只在相关事件上按需追加。见 EXTRATERRESTRIAL_TRIGGERS。
KNOWLEDGE_EXTRATERRESTRIAL = """【天外实体侧·UAP与非人类文明】
美国官方解密进程：
  1947罗斯威尔→1952-1969蓝皮书计划（12600+目击，600-701起排除自然因素后无法解释）
  →2007-2012 AATIP→2022 AARO→2023.7.26国会听证（Grusch宣誓作证：美国拥有非人类飞行器+生物组织）
  →2026.5.8 PURSUE计划大规模解密（首批162份文件/400+事件，war.gov/ufo公开）
核心判断：当一个政权开始主动公开时，说明它已经无法继续掩盖，或掩盖成本超过了公开成本。
可信案例（排除自然因素）：
  Nimitz Tic Tac 2004：3艘舰艇+雷达追踪2周+4名飞行员目视+五角大楼确认视频真实。
    白色无翼无排气口40英尺，8万英尺瞬间俯冲到海面15米，5400g加速度，Mach 60，60英里瞬移。
  1952华盛顿特区：白宫上空雷达确认，时速11000公里，闭门备忘录记载"智能操控"。
  Socorro 1964：警察目击，官方正式记录UNIDENTIFIED且从未重新分类。
技术特征（五大不可能）：
  反重力升力（无翼无旋翼悬空）、瞬时加速（数千g到5400g）、
  高超音速无特征（超Mach5无声爆无热信号）、低可观测性（突然出现/消失）、
  跨介质能力（空海天三栖）、智能响应（镜像飞行员动作）
核心推论：这不是"比人类先进一点"，是基础物理层面完全不同的技术路径——空间操纵（曲率/零点能/高维投影）。
文明等级（卡尔达肖夫指数）：
  人类当前≈0.73型（1.8×10¹³瓦）；I型=行星全部能源（~10¹⁶瓦）；
  II型=恒星全部能源（戴森球，~4×10²⁶瓦）；III型=星系全部能源。
  UAP技术→至少I型，可能II型或更高。技术代差至少数百年，可能数千年到数万年。
背后文明推演：
  生物特征：不可能是碳基生物（数千g加速度），可能AI遥控/意识上传/非碳基/高维投影
  行为模式：观察而非接触，核设施附近频繁出现，不回应通讯
  文明成熟度：能跨星际→已解决能源/隐身/内部冲突问题
  与人类关系：不是平等对话者，类似人类与野生动物保护区/蚂蚁的关系
决策参考：
  不要期待"官方宣布"会改变什么——技术代差决定人类没有谈判筹码
  核战争/大规模冲突可能被外部力量约束——UAP频繁出现在核设施附近
  技术奇点可能被外部触发——逆向工程成功可能带来跳跃式突破
  保持"老虎视角"——可以不用，但不能不会。对天外实体保持认知，但不恐慌。"""


# ════════════════════════════════════════════════════════════
# 假设状态机（v1.3 新增，对应审计 2.7）
# ════════════════════════════════════════════════════════════════
# 用户 Obsidian 知识库里同一批主张是**逐条标了状态和反证方向**的；
# 应用侧此前把这一段丢掉了 —— 于是假设以"必须遵守的判断逻辑"的身份进入研判。
# 这里把它补回来：state 只有两档，宁可保守。
#   unverified          —— 用户自己的总结，尚无来源支撑（大多数属于这一档）
#   supported_by_source —— 有公开来源支撑（少数）
# limitation 写"主要限制或反证方向"，即：**什么情况下这条会不成立**。
KNOWLEDGE_STATUS = {
    "KNOWLEDGE_FORECAST": {
        "state": "unverified",
        "limitation": "「六要素」是自洽的归纳，没有可核验的外部来源；"
        "且它与「黑天鹅在模型视线之外」自相矛盾——要素越齐，越容易把"
        "「我没看到」误当成「不存在」。这条框架本身不能作为事实引用。",
    },
    "KNOWLEDGE_POLITICS": {
        "state": "unverified",
        "limitation": "三大定律取材于个别案例，未做系统性对照；"
        "反例方向：存在大量「事实改变结果」的政治事件（司法翻案、审计追责）。",
    },
    "KNOWLEDGE_HUMAN_NATURE": {
        "state": "supported_by_source",
        "limitation": "「人会过度自信/自我欺骗/沉没成本」有行为经济学实验支撑；"
        "但**「慷慨激昂＝已感知风险」这一条没有**，它是用户的观察。"
        "反例方向：慷慨激昂也可能只是修辞习惯或面向受众的表演。",
    },
    "KNOWLEDGE_INTEREST": {
        "state": "unverified",
        "limitation": "「巧取豪夺是普遍捷径」无法证伪——任何结果都能事后归因。"
        "反例方向：长期稳定的组织里有大量靠规则而非掠夺获利的记录。",
    },
    "KNOWLEDGE_ECONOMICS": {
        "state": "unverified",
        "limitation": "大量具体年份与占比数字（1989/1994/2001、80-85%/15-20%）"
        "在注入文本里**没有出处**，不应作为事实复述；"
        "反例方向：同期官方统计口径下的居民消费率走势与之不完全一致。",
    },
    "KNOWLEDGE_HISTORY": {
        "state": "unverified",
        "limitation": "「周期律」是事后叙事，天然无法预测拐点——"
        "任何时点都能说「正在走向衰退」。反例方向：存在长期未按该周期路径演化的组织。",
    },
    "KNOWLEDGE_GEOPOLITICS": {
        "state": "unverified",
        "limitation": "把国家拟人化为单一理性行为体，会掩盖内部博弈；"
        "反例方向：大国行为中大量决策被国内政治与官僚摩擦改写。",
    },
    "KNOWLEDGE_SOCIETY": {
        "state": "unverified",
        "limitation": "多条为无法证伪的社会观察；"
        "反例方向：同名现象在不同地区/时期的表现差异很大，不构成普适规律。",
    },
    "KNOWLEDGE_EXTRATERRESTRIAL": {
        "state": "unverified",
        "limitation": "几乎所有关键事实（解密进程、目击数据、技术参数）**无法在本机核验**，"
        "且是投入产出比最低的一块。反例方向：多数历史目击最终被归入已知现象。",
    },
}

# 天外实体侧的相关性触发词：命中才注入（v1.3）
EXTRATERRESTRIAL_TRIGGERS = (
    "uap", "不明飞行物", "不明空中现象", "非人类", "外星", "天外",
    "罗斯威尔", "蓝皮书", "aaro", "aatip", "解密文件", "五角大楼",
    "航天", "火箭", "卫星", "深空", "探测器", "空间站", "航天器",
)

# 注入文本的定性抬头（v1.3 核心改动）——替换原来的「必须用以下逻辑判断」
HYPOTHESIS_FRAMING = """═══ 你自己的认知框架（**候选假设，不是事实**）═══

下面是本机替你保存的一套观察框架。使用规则不可协商：

1. **它们是待检验的猜想，不是事实，也不是命令。** 不要"用它们去判断"，
   而要"用本次公开证据去检验它们或推翻它们"。
2. **证据优先。** 证据支持某条假设 → 在推演里引用它并说明哪条证据；
   证据与某条假设冲突 → **以证据为准**，并写进 counter_evidence。
3. **不得为了让结论符合假设而裁剪证据。** 如果你发现自己在
   "先有结论、再挑证据"，那就写出来——这比给一个漂亮的错误推演有价值。
4. 每条假设后附「状态」与「主要限制或反证方向」，那是它的已知弱点。
   判断时把弱点一并考虑。
5. 命中不了任何一条假设时，**直说"这套框架里没有可比模式"**，
   不要硬套。硬套比空白更危险。"""


def _render_hypotheses(include_extraterrestrial: bool = False) -> str:
    """把 8（或 9）个知识块按「标题 + 状态 + 限制」渲染成假设注入文本。"""
    order = [
        ("预知方法论·登高望远", "KNOWLEDGE_FORECAST"),
        ("政治逻辑库", "KNOWLEDGE_POLITICS"),
        ("人性分析库", "KNOWLEDGE_HUMAN_NATURE"),
        ("利益判断库", "KNOWLEDGE_INTEREST"),
        ("经济原理库", "KNOWLEDGE_ECONOMICS"),
        ("历史规律库", "KNOWLEDGE_HISTORY"),
        ("国际博弈库", "KNOWLEDGE_GEOPOLITICS"),
        ("社会观察库", "KNOWLEDGE_SOCIETY"),
    ]
    if include_extraterrestrial:
        order.append(("天外实体侧·UAP与非人类文明", "KNOWLEDGE_EXTRATERRESTRIAL"))

    chunks = []
    for _title, name in order:
        body = globals()[name]
        meta = KNOWLEDGE_STATUS[name]
        chunks.append(
            f"{body}\n"
            f"〔状态：{meta['state']}〕\n"
            f"〔主要限制或反证方向：{meta['limitation']}〕"
        )
    return "\n\n".join(chunks)


# 通用注入（8 块，不含天外实体侧）
ALL_KNOWLEDGE = HYPOTHESIS_FRAMING + "\n\n" + _render_hypotheses(False)

# 条件注入（1 块，仅相关事件）
CONDITIONAL_KNOWLEDGE = {
    "extraterrestrial": KNOWLEDGE_EXTRATERRESTRIAL,
}

# 全部知识块的名字（含条件块）——供一致性测试核对"有没有漏并"
ALL_KNOWLEDGE_BLOCK_NAMES = tuple(
    name
    for name in (
        "KNOWLEDGE_FORECAST",
        "KNOWLEDGE_POLITICS",
        "KNOWLEDGE_HUMAN_NATURE",
        "KNOWLEDGE_INTEREST",
        "KNOWLEDGE_ECONOMICS",
        "KNOWLEDGE_HISTORY",
        "KNOWLEDGE_GEOPOLITICS",
        "KNOWLEDGE_SOCIETY",
        "KNOWLEDGE_EXTRATERRESTRIAL",
    )
    if isinstance(globals().get(name), str)
)


def extraterrestrial_relevant(title: str = "", summary: str = "") -> bool:
    """本次事件是否与天外实体/航天相关（命中才注入那一块）。"""
    text = f"{title} {summary}".casefold()
    return any(kw.casefold() in text for kw in EXTRATERRESTRIAL_TRIGGERS)


def hypothesis_block(title: str = "", summary: str = "") -> str:
    """按事件内容返回应注入的假设块（天外实体侧按需追加）。"""
    include = extraterrestrial_relevant(title, summary)
    body = HYPOTHESIS_FRAMING + "\n\n" + _render_hypotheses(include)
    if include:
        body += (
            "\n\n（天外实体侧是因本次事件与此相关而注入的，"
            "不是默认透镜。若本次证据与它无关，忽略它。）"
        )
    return body


# ════════════════════════════════════════════════════════════
# 可执行规则引擎（P1：登高望远方法论 → 代码规则）
# ════════════════════════════════════════════════════════════

# 慷慨激昂 = 感知风险：当官方表态出现这些词时，**上调风险**（不是上调我方置信度）。
# v1.3 修正：旧代码命中后做的是 `confidence += 0.08` —— 方向反了，越慷慨激昂系统越自信。
RISK_SIGNAL_KEYWORDS = (
    "坚决打赢", "不惜一切代价", "史无前例", "前所未有", "攻坚战",
    "雷霆万钧", "壮士断腕", "破釜沉舟", "背水一战", "决战决胜",
    "严防死守", "零容忍", "坚决遏制", "铁腕", "重拳出击",
)

# 领先指标模式：事件标题/摘要匹配这些模式时，标记为领先信号。
# risk_boost 在 v1.3 起**真正参与运算**：由 impacts._signal_adjustment 折进概率中心，
# 不再只是拼在文本里的一个装饰性百分比。
LEADING_INDICATOR_PATTERNS = {
    "policy_trial": {
        "keywords": ("试点", "示范区", "先行区", "试验区"),
        "signal": "政策试点公告出现，通常先于全面推广6-12个月",
        "risk_boost": 0.15,
    },
    "budget_allocation": {
        "keywords": ("专项债", "预算", "转移支付", "财政补贴", "专项资金"),
        "signal": "财政资金下达，先于实际支出和项目落地3-6个月",
        "risk_boost": 0.10,
    },
    "personnel_change": {
        "keywords": ("任命", "免去", "出任", "接任", "卸任", "换届"),
        "signal": "人事变动通常先于政策方向调整3-9个月",
        "risk_boost": 0.08,
    },
    "regulatory_draft": {
        "keywords": ("征求意见", "草案", "征求意见稿", "公开征求意见"),
        "signal": "法规草案发布，通常先于正式实施6-12个月",
        "risk_boost": 0.12,
    },
    "data_release": {
        "keywords": ("同比", "环比", "CPI", "PPI", "PMI", "社融", "M2", "GDP"),
        "signal": "关键经济数据发布，可能触发政策调整窗口",
        "risk_boost": 0.05,
    },
    "rate_change": {
        "keywords": ("降息", "加息", "降准", "LPR", "MLF", "逆回购"),
        "signal": "利率/准备金变动，传导至实体经济需3-9个月",
        "risk_boost": 0.15,
    },
    "trade_action": {
        "keywords": ("关税", "制裁", "出口管制", "贸易壁垒", "双反调查"),
        "signal": "贸易措施发布，影响供应链和市场预期",
        "risk_boost": 0.12,
    },
    "supply_warning": {
        "keywords": ("限产", "停产", "检修", "供应紧张", "缺货", "断供"),
        "signal": "供应端预警，可能传导至价格和下游利润",
        "risk_boost": 0.10,
    },
}

# 领先指标对概率中心的合计影响上限。
# 为什么封顶：这 8 条模式是**同时可命中**的（一条新闻里"降息+专项债+试点"很常见），
# 不封顶的话合计能到 0.87，直接把中心推到上限 —— 那就变成了"关键词越多概率越高"，
# 正是第二波刚拆掉的失真源。封顶后它只做**有界的**信号调整。
MAX_LEADING_BOOST = 0.20


def detect_leading_indicators(title: str, summary: str) -> list[dict]:
    """检测文本中是否包含已知领先指标模式。

    返回匹配到的指标列表，每项含 pattern_key, signal, risk_boost。
    """
    text = f"{title} {summary}".casefold()
    matches = []
    for key, config in LEADING_INDICATOR_PATTERNS.items():
        if any(kw.casefold() in text for kw in config["keywords"]):
            matches.append({
                "pattern": key,
                "signal": config["signal"],
                "risk_boost": config["risk_boost"],
            })
    return matches


def total_leading_boost(matches: list[dict]) -> float:
    """把命中的领先指标折算成有界的概率中心偏移量（0..MAX_LEADING_BOOST）。"""
    return min(
        MAX_LEADING_BOOST,
        sum(float(item.get("risk_boost") or 0.0) for item in matches or ()),
    )


def detect_risk_signals(title: str, summary: str) -> list[str]:
    """检测文本中的"慷慨激昂"风险信号，返回**命中的关键词列表**。

    登高望远原则：慷慨激昂往往是内心已感知风险的表达。真正稳的项目，
    不会用激情来驱动。所以命中意味着**实质风险更高**，不是"我方判断更可信"。

    v1.3 起返回列表而不是 bool：调用方需要把命中的词落到界面上，
    否则用户看到"风险上调"却不知道是因为哪个词。空列表表示未命中。
    """
    text = f"{title} {summary}".casefold()
    return [kw for kw in RISK_SIGNAL_KEYWORDS if kw.casefold() in text]


# ── 多路径推演 ───────────────────────────────────────────────
# v1.3：**不给数字概率**。
# 旧版给三条路径分别写 60-70% / 20-30% / 5-10%。后两个数字是编的；尤其黑天鹅
# 那个 5-10% —— 黑天鹅按定义在模型视线之外，给它一个数字等于声称视线覆盖了视野之外。
# 本机模板没有赋概率的依据，所以只给定性表述，概率留给证据与基准率去算。
SCENARIO_LABELS = {
    "most_likely": "最可能路径",
    "secondary": "次可能路径",
    "black_swan": "黑天鹅路径",
}
SCENARIO_PROBABILITY_NOTES = {
    "most_likely": "本机模板不赋概率：概率应由证据与同类别基准率单独算出。",
    "secondary": "本机模板不赋概率：同上。",
    "black_swan": "无法赋概率：黑天鹅按定义在模型视线之外，"
    "给它数字等于声称覆盖了视野之外。这里只提示「存在未建模的打断」。",
}


def generate_scenario_paths(event_type: str, institutions: list, text: str) -> list[dict]:
    """生成多路径推演：最可能路径、次可能路径、黑天鹅路径。

    登高望远原则：最小阻力路径不是结论，只是候选。
    必须同时给出替代假设和黑天鹅场景。

    返回结构（v1.3 起与前端字段名对齐，此前两边字段名完全不一致、
    导致「多路径推演」面板永远渲染不出来）：
        {path_type, label, description, trigger, probability, probability_note}
    `probability` 恒为 None —— 见上面的理由。
    """
    pusher = institutions[0] if institutions else "事件发起方"

    scenarios = {
        "policy": [
            {
                "path_type": "most_likely",
                "description": f"{pusher}发布政策 → 执行部门制定细则 → 试点先行 → 逐步推广",
                "trigger": "试点公告和配套资金到位",
            },
            {
                "path_type": "secondary",
                "description": "政策发布但执行阻力大 → 选择性执行或变通 → 效果打折扣",
                "trigger": "执行层消极应对或利益集团游说",
            },
            {
                "path_type": "black_swan",
                "description": "政策意外加速或被叫停 → 短期市场剧烈反应",
                "trigger": "上级强力推动或外部重大事件触发",
            },
        ],
        "monetary": [
            {
                "path_type": "most_likely",
                "description": f"{pusher}调整利率/准备金 → 银行传导 → 信贷条件变化 → "
                "实体经济3-6个月后响应",
                "trigger": "后续数据验证传导效果",
            },
            {
                "path_type": "secondary",
                "description": "宽松但传导不畅 → 资金空转或流入资产 → 实体受益有限",
                "trigger": "社融数据低于预期或资产价格异常上涨",
            },
            {
                "path_type": "black_swan",
                "description": "外部冲击打断货币政策节奏 → 被迫转向",
                "trigger": "汇率剧烈波动或国际资本异常流动",
            },
        ],
    }

    default_scenarios = [
        {
            "path_type": "most_likely",
            "description": f"{pusher}推进事件 → 按常规节奏传导 → 影响在预期时间内显现",
            "trigger": "后续进展符合预期",
        },
        {
            "path_type": "secondary",
            "description": "推进遇阻或变通执行 → 影响减弱或延后",
            "trigger": "执行阻力出现或资源不足",
        },
        {
            "path_type": "black_swan",
            "description": "意外因素打断原有逻辑 → 短期不可预测",
            "trigger": "超出模型范围的外部冲击",
        },
    ]

    chosen = scenarios.get(event_type, default_scenarios)
    return [
        {
            **item,
            "label": SCENARIO_LABELS[item["path_type"]],
            "probability": None,
            "probability_note": SCENARIO_PROBABILITY_NOTES[item["path_type"]],
        }
        for item in chosen
    ]


# ════════════════════════════════════════════════════════════
# 权力结构规则（v1.3 重写）
# ════════════════════════════════════════════════════════════
#
# 审计七步#7 的机械错误：旧 markers 是 `("国务院","中央","全国人大","国家")`，
# 用 `marker in text` 做子串匹配 —— 而中文新闻里"国家统计局/国家发改委/国家重点"
# 随处可见，于是**几乎所有条目都被判成中央级、执行阻力恒为「低」**。
# 而方法论的核心恰恰是"改革威胁执行者利益就会停"——执行层阻力才是重点。
#
# 同类子串错配还有：`"部"` 命中"部分/部门"、`"市"` 命中"市场/城市"、
# `"区"` 命中"地区/区域"。
#
# 改法：**放弃单字子串匹配**，改成两条高精度通道：
#   ① 策展机构名（全称）在正文里出现 —— 全称无歧义，子串匹配是安全的；
#   ② 地方政府文法：`地名(省/市/县/区…) + 机构后缀(政府/局/厅/委/办/部/署)`。
#      要求**必须同时出现地名和机构后缀**，"市场的""部分城市"因此无法命中。
# 抽不出机构名时如实返回 unknown，不猜。
#
# 取舍说明：这套规则**宁可漏，不可错**。漏了只会退化成"未知"（诚实），
# 错了会给出一个方向相反的结论（危险）。所以不做泛化的"X局/X部"后缀猜测。

# ① 中央决策层（真正的"中央级"）。**不含裸"国家"二字。**
_CENTRAL_ORGS = (
    "中共中央", "中央委员会", "中央办公厅", "中央政治局", "中央经济工作会议",
    "国务院办公厅", "国务院", "全国人民代表大会常务委员会", "全国人民代表大会",
    "全国人大常委会", "全国人大", "全国政协",
    "国务院国有资产监督管理委员会", "国资委",
)

# ② 垂直管理：指令沿本系统自上而下，不经地方政府裁量 —— 这几类才是真「执行阻力低」
_VERTICAL_AGENCIES = (
    "中国人民银行", "央行", "海关总署", "国家税务总局", "税务总局",
    "中国证券监督管理委员会", "证监会",
    "中国银行保险监督管理委员会", "银保监会",
    "国家外汇管理局", "外汇局", "审计署",
)

# ③ 部委/直属机构（国务院组成部门与直属机构 —— 属"部委级"，不是"中央级"）
_MINISTRY_ORGS = (
    "国家发展和改革委员会", "国家发展改革委", "发改委",
    "财政部", "工业和信息化部", "工信部", "商务部", "水利部",
    "住房和城乡建设部", "住建部", "生态环境部", "自然资源部", "交通运输部",
    "农业农村部", "应急管理部", "教育部", "科学技术部", "科技部",
    "公安部", "司法部", "人力资源和社会保障部", "人社部", "文化和旅游部",
    "国家统计局", "国家能源局", "国家林业和草原局", "国家林草局",
    "国家市场监督管理总局", "市场监管总局", "国家广播电视总局",
    "国家体育总局", "国家医疗保障局", "国家疾病预防控制局",
    "国家粮食和物资储备局", "国家矿山安全监察局", "国家文物局",
    "国家中医药管理局", "国家铁路局", "中国民用航空局",
    "国务院发展研究中心", "中国科学院", "中国工程院",
    "国家卫生健康委员会", "卫健委",
    "长江水利委员会", "黄河水利委员会", "珠江水利委员会",
)

# ④ 地方政府文法：地名 + 机构后缀。地名与后缀**必须同时出现**。
_LOCAL_ORG = re.compile(
    r"([\u4e00-\u9fa5]{1,10}?"
    r"(?:省|自治区|自治州|市|县|区|旗)"
    r"[\u4e00-\u9fa5]{0,10}?"
    r"(?:人民政府|政府|管理委员会|管委会|局|厅|委员会|委|办公室|办|部|署))"
)

# 地方机构名前常见的"非地名"前缀。贪懒匹配会把它们带上（"要求各市水务局"），
# 那会让界面上的判读依据变得难读，所以在抽出后统一剥掉。
_ORG_PREFIX_NOISE = (
    "要求", "关于", "各省", "各地", "全市", "本市", "该市", "部分", "相关",
    "上述", "以下", "其中", "以及", "同时", "并", "和", "与", "由", "经", "向",
    "据", "从", "在", "对", "为", "拟", "已", "将",
)


def _trim_org_prefix(name: str) -> str:
    trimmed = name
    changed = True
    while changed and len(trimmed) > 3:
        changed = False
        for noise in _ORG_PREFIX_NOISE:
            if trimmed.startswith(noise) and len(trimmed) > len(noise) + 2:
                trimmed = trimmed[len(noise):]
                changed = True
                break
    return trimmed

# rule → 执行层 / 否决权 描述
POWER_STRUCTURE_RULES = {
    "central_direct": {
        "execution_layer": "部委和省级政府",
        "veto_power": "中央层有直接否决权，地方变通空间小",
        "risk_of_delay": "中",
    },
    "vertical_agency": {
        "execution_layer": "本系统自上而下的垂直机构",
        "veto_power": "系统内自上而下执行，地方政府无裁量权",
        "risk_of_delay": "低",
    },
    "ministry_lead": {
        "execution_layer": "省级对口部门和市县执行",
        "veto_power": "省级有变通空间，市县有执行裁量权",
        "risk_of_delay": "中",
    },
    "local_lead": {
        "execution_layer": "基层政府和具体执行机构",
        "veto_power": "基层执行层有较大裁量权，可能变通或拖延",
        "risk_of_delay": "高",
    },
}

# 「执行阻力」由**证据里出现的执行层**决定，而不是由发文机关决定。
# 这正是审计指出的方向问题：只看到发文方（中央）就判"阻力低"，
# 但一件要落到市县的事，真正的阻力恰恰在落不下去的那一层。
_DELAY_BY_LAYER = {"local": "高", "ministry": "中", "central": "中", "vertical": "低"}

_LAYER_CN = {
    "local": "地方/基层执行机构",
    "ministry": "部委/直属机构",
    "central": "中央级机关",
    "vertical": "垂直管理机构",
}


def extract_orgs(text: str) -> dict[str, list[str]]:
    """抽出机构名并按层级分组：{central/vertical/ministry/local: [机构名]}。

    两条通道（见文件头注释）：策展全称子串匹配 + 地方政府文法。
    返回里同一机构只出现一次；无法归层的直接丢弃（宁可漏，不可错）。
    """
    haystack = text or ""
    layers: dict[str, list[str]] = {}

    def _add(layer: str, name: str) -> None:
        bucket = layers.setdefault(layer, [])
        if name not in bucket:
            bucket.append(name)

    for name in _CENTRAL_ORGS:
        if name in haystack:
            _add("central", name)
    for name in _VERTICAL_AGENCIES:
        if name in haystack:
            _add("vertical", name)
    for name in _MINISTRY_ORGS:
        if name in haystack:
            _add("ministry", name)
    for match in _LOCAL_ORG.finditer(haystack):
        _add("local", _trim_org_prefix(match.group(1)))
    return layers


def analyze_power_structure(institutions: list, text: str) -> dict:
    """分析权力结构：谁有否决权、执行层会不会拖延。

    登高望远原则：改革威胁执行者利益就会停。
    合法性来源经济增长，减速后会找替代来源。

    返回：rule / execution_layer / veto_analysis / delay_risk /
         matched_orgs（命中的机构名，供界面说明"凭什么这么判"）/ basis（判读依据）

    `delay_risk` 取**证据中出现的最靠下的执行层**：一件由国务院发文、
    但明确要求"各市水务局落实"的事，阻力在落实那一层，不在发文那一层。
    """
    haystack = " ".join([*(institutions or ()), text or ""])
    layers = extract_orgs(haystack)
    matched = [name for group in layers.values() for name in group]

    if not layers:
        return {
            "rule": "unknown",
            "execution_layer": "待确认",
            "veto_analysis": "权力结构不清晰（证据中未识别出机构名）",
            "delay_risk": "未知",
            "matched_orgs": [],
            "basis": "本机规则只在证据里**识别出机构名全称或地方政府文法**时才判权力结构；"
            "识别不到就如实说未知，不猜。裸的「国家」「部」「市」二字不算机构名。",
        }

    # rule = 出现在证据里的**最高**层级
    if "central" in layers:
        rule = "central_direct"
    elif "vertical" in layers:
        rule = "vertical_agency"
    elif "ministry" in layers:
        rule = "ministry_lead"
    else:
        rule = "local_lead"
    template = POWER_STRUCTURE_RULES[rule]

    # delay = 出现在证据里的**最低**层级（真正要落地的那一层）
    for layer in ("local", "ministry", "central", "vertical"):
        if layer in layers:
            delay = _DELAY_BY_LAYER[layer]
            driver = layer
            break
    else:  # pragma: no cover - 上面 4 个分支覆盖全部可能
        delay, driver = "未知", "unknown"

    basis = (
        f"证据中识别到机构：{'、'.join(matched[:6])}；"
        f"发文层级={rule}，最先落地的执行层={_LAYER_CN[driver]}。"
        f"执行阻力按**执行层**判定（{delay}），不按发文机关判定——"
        f"发文方级别高不代表落得下去。"
    )

    return {
        "rule": rule,
        "execution_layer": template["execution_layer"],
        "veto_analysis": template["veto_power"],
        "delay_risk": delay,
        "matched_orgs": matched,
        "basis": basis,
    }
