"""Local-only mapping from public judgments to private interests."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from .forecasts import ALLOWED_PROBABILITIES

_logger = logging.getLogger(__name__)

_ALLOWED_PROB_LIST = sorted(ALLOWED_PROBABILITIES)


def _nearest_probability(value: float) -> float:
    """把任意概率映射到最近的固定档位（ALLOWED_PROBABILITIES）。"""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.50
    value = max(0.0, min(1.0, value))
    return min(_ALLOWED_PROB_LIST, key=lambda p: abs(p - value))


# 证伪性闸：命题要能进不可变账本，必须满足三件事——**有可观测条件** /
# **信号事件级特化** / **无禁用虚词**。含虚词的命题无法在截止日机械核验，写进账本
# 只会污染校准分（Brier），所以一律停在待补充、不进 forecasts。
#
# ⚠ v1.4 删除了两项**结构上不可能失败**的检查（R-06）：原先还查「有可核验日期」与
# 「有判定依据」，但那两项查的是 `_candidate` 自己刚写下的模板文本
# （title 由 `f"截至{end_date}：…"` 生成、criteria 里本来就写死着"核验以官方公告…"），
# 必然命中 —— 那一段在校验它自己。**不要让闸门看起来比实际严。**
_BANNED_PHRASES = (
    "产生实际影响", "实际影响", "尚未补充", "待补充",
    "后续官方公告", "执行进展通报", "视情况", "一定程度上",
    "可能影响", "需持续观察",
)

# 新闻原标题在命题标题里的占位符（R-07）。见 `_generated_text`。
_HEADLINE_PLACEHOLDER = "〔来源标题〕"

# 信号"在窗口开始前已成立"的**明确**标记（R-09）。只认这些词，是为了不误伤：
# 证据里出现"发布"太常见，但"已正式出台"这种表述几乎只出现在既成事实上。
_ALREADY_TRUE_MARKERS = (
    "已发布", "已出台", "已印发", "已下达", "已生效", "已实施", "已挂牌",
    "已签署", "已批准", "已批复", "已经发布", "正式发布", "正式出台",
    "正式印发", "正式生效", "正式实施", "此前已", "早已经", "已于", "已就",
)


# 可观测信号的长度与条数上限。
# 为什么要有：命题是拿去**机械核验**的（"到那天看什么新闻能判定真假"），
# 塞进去的程序内部记账文本既不可观测，也会把标题撑长到读不下去。
MAX_OBSERVABLE_SIGNAL_CHARS = 60
MAX_OBSERVABLE_SIGNALS = 4

# 本机规则引擎写在领先指标尾部的内部标注。它不是"可观测事实"，要剥掉。
_ENGINE_ANNOTATION = re.compile(r"[｜|]?\s*规则引擎命中[:：].*$", re.S)


def _clean_signal(value) -> str:
    """把一个候选信号洗成"人能用一条新闻去对照"的短句。"""
    text = str(value or "").strip()
    if not text or text in ("待补充", "尚未补充"):
        return ""
    text = _ENGINE_ANNOTATION.sub("", text).strip(" ；;｜|")
    if not text:
        return ""
    if len(text) > MAX_OBSERVABLE_SIGNAL_CHARS:
        text = text[:MAX_OBSERVABLE_SIGNAL_CHARS].rstrip(" ，,；;、") + "…"
    return text


def _extract_observable_signals(judgment):
    """从判断内容里抽取可观测信号，作为命题的“阈值或可观测事实”要素。
    空则命题不具备结算性。

    v1.3 起**优先取 `gyw.observable_signals`**：那是校验器唯一强制过
    "具体到能被一条未来新闻证伪"的字段（还带引用约束），可结算性最好。
    它缺失时才退回 `leading_indicators` + 上下调触发条件。
    旧版把顺序反了，于是最好的一路数据被排在最差的两路之后。
    """
    signals = []
    gyw = judgment.get("gyw") or {}
    structured = gyw.get("observable_signals") or []
    if isinstance(structured, str):
        structured = [structured]
    for item in structured:
        item = _clean_signal(item)
        if item:
            signals.append(item)
    leading = (gyw.get("leading_indicators") or "").strip()
    if leading:
        leading = re.sub(r"^领先指标[:：]\s*", "", leading).strip()
        leading = _clean_signal(leading)
        if leading:
            signals.append(leading)
    for trig in (judgment.get("up_triggers") or []) + (judgment.get("down_triggers") or []):
        trig = _clean_signal(trig)
        if trig:
            signals.append(trig)
    seen, out = set(), []
    for s in signals:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return "；".join(out[:MAX_OBSERVABLE_SIGNALS])


def _main_signal(signals) -> str:
    """命题的主信号 = `observable_signals` 的第一条（R-09）。

    结算判据只认主信号：旧版对最多 4 条信号取**或**，四条各 30% 独立、或运算后
    约 76% —— 命题天然偏"发生"。指定一条主信号，其余降为辅助观察。
    """
    text = str(signals or "").strip()
    if not text:
        return ""
    return text.split("；")[0].split(";")[0].strip()


def _generated_text(candidate) -> str:
    """命题里**由我们生成**的那部分文本（不含新闻原标题）—— R-07。

    为什么必须排除原标题：`title` 内嵌了新闻原标题（`「{cluster title}」`）。
    一条标题写成「机构：降息**可能影响**楼市」的新闻，会让整条候选被拒 ——
    闸门把"分析者的含糊"和"记者的措辞"当成了同一件事。禁用词表要拦的是前者：
    含糊的是**我们的命题**，不是被报道的那句话。
    """
    title = str(candidate.get("title", "") or "")
    headline = str(candidate.get("source_headline", "") or "").strip()
    if headline and headline in title:
        title = title.replace(headline, _HEADLINE_PLACEHOLDER)
    return "\n".join(
        [
            title,
            str(candidate.get("resolution_criteria", "") or ""),
            str(candidate.get("observable_signals", "") or ""),
        ]
    )


def _terms_from_text(haystack: str) -> list[str]:
    """从一段文本里抽出"能被一条未来新闻对照"的实体词：机构名 / 地名 / 数字。

    识别不到就返回空 —— 那样候选会被特化闸拦住，停在"待补充"。这正是我们要的：
    **宁可挡住，也不要放一条"分类平均命题"进账本。**

    "专有名词"这一档用机构名 + 数字兜底：这两类是证据里真实出现过、又能被一条
    未来新闻对照的东西，比"猜哪些词算专有名词"可靠。
    """
    from .knowledge_base import extract_orgs

    haystack = str(haystack or "")
    terms: list[str] = []
    for group in extract_orgs(haystack).values():
        for name in group:
            if len(str(name)) >= 2 and name not in terms:
                terms.append(name)
    # 两个抽取器都要用：`extract_orgs`（策展全称 + 地方政府文法）负责"证据里真实
    # 出现的机构"，`judgment_local` 的 `_extract_institutions` 负责本地研判
    # **实际拿来生成信号**的那一批。只认一个就会对不上 —— 本地模板信号里的机构名
    # 出自后者，若闸门只认前者，模板信号会被系统性误判为"未特化"。
    try:
        from .judgment_local import LocalHeuristicProvider

        for name in LocalHeuristicProvider._extract_institutions(haystack):
            if len(str(name)) >= 2 and name not in terms:
                terms.append(name)
    except Exception:  # 抽取器不可用不该让整条候选失去可观测性判断
        pass
    for match in _NUMBER_RE.finditer(haystack):
        value = match.group(0).strip()
        if value and value not in terms:
            terms.append(value)
    return terms


def _event_terms(cluster, judgment) -> list[str]:
    """从事件簇与研判里抽本事件特有的实体词（R-08 用）。"""
    texts = [
        str(cluster.get("title") or ""),
        str(cluster.get("summary") or ""),
        str(judgment.get("fact_summary") or ""),
        " ".join(str(item) for item in (judgment.get("actors") or [])),
    ]
    return _terms_from_text(" ".join(texts))


#: 候选卡标题里 `「…」` 段的分隔。第 0 段是本地利益名（我们自己填的），
#: 第 1 段起才是新闻原标题。
_HEADLINE_SEGMENT_RE = re.compile(r"「([^」]+)」")


def _candidate_event_text(candidate) -> str:
    """候选卡里**能代表本事件**的那段文本（R-08）。

    ⚠ 绝不能把 `observable_signals` 也算进来：那会让特化校验变成自证 ——
    信号里提到什么，什么就自动成了"本事件实体"，闸门等于没有。
    所以只用：`source_headline`（v1.4 起单独落库）或标题里的 `「…」` 原标题段，
    外加因果链。老候选卡（v1.3 之前）没有单独的 headline，标题里的 `「…」` 是唯一来源。
    """
    headline = str(candidate.get("source_headline") or "").strip()
    if not headline:
        title = str(candidate.get("title") or "")
        segments = _HEADLINE_SEGMENT_RE.findall(title)
        if segments:
            # 第 0 段是本地利益名（我们自己填的），第 1 段起才是新闻原标题。
            headline = " ".join(segments[1:])
        else:
            # 没有 `「…」` 段，说明标题不是 `_candidate` 生成的（手写命题 / 历史数据）。
            # 那就把生成模板那部分剥掉 —— 否则"受到可观测影响"这种模板用词会变成
            # 事件文本的一部分，一条同样套用模板的信号就能自证"已特化"。
            headline = re.sub(r"（即：.*$", "", title)
            if "是否因" in headline:
                headline = headline.split("是否因")[-1]
            headline = headline.replace("受到可观测影响", "")
    return " ".join([headline, str(candidate.get("causal_chain") or "")])


def _candidate_terms(candidate) -> list[str]:
    """候选卡能提供的本事件实体词 = 卡内文本抽出的 ∪ `_candidate` 落库的 event_terms。"""
    terms = _terms_from_text(_candidate_event_text(candidate))
    for item in candidate.get("event_terms") or []:
        text = str(item).strip()
        if text and text not in terms:
            terms.append(text)
    return terms


#: "专有名词"这一路的判据长度：信号与事件文本的共同**连续片段**至少这么长。
#: 取 4 字是为了避开"发布 / 公告 / 通知 / 相关"这类**任何事件都会出现**的通用词 ——
#: 中文不需要分词，而"泵类采购公告"这种能立刻说明"这条信号讲的就是这件事"的片段，
#: 长度一定不止两个字。
_SPECIALIZATION_SHARED_CHARS = 4


def _shares_event_fragment(signal, event_text) -> bool:
    """信号与事件文本有没有一段 ≥4 字的共同连续片段。"""
    window = _SPECIALIZATION_SHARED_CHARS
    signal = str(signal or "")
    event_text = str(event_text or "")
    if len(signal) < window or len(event_text) < window:
        return False
    return any(
        signal[index : index + window] in event_text
        for index in range(len(signal) - window + 1)
    )


def _signals_specialized(signals, terms, event_text) -> bool:
    """可观测信号里**至少一条**含本事件特有的实体（R-08）。

    两条路任一满足即可：命中机构名/地名/数字，或与事件标题共享一段 ≥4 字的
    连续片段（即"专有名词"那一档）。
    """
    text = str(signals or "")
    if any(term and str(term) in text for term in (terms or [])):
        return True
    return any(
        _shares_event_fragment(part, event_text)
        for part in re.split(r"[；;]", text)
        if part.strip()
    )


def _main_signal_preexisting(main_signal, evidence_text) -> bool:
    """主信号是不是"在窗口开始前就已成立"（R-09）。

    旧判据写的是"在该日期前发生"——**包含窗口开始前的既成事实**。政策类事件的
    模板信号里就有"…发布配套实施细则"，而这类文件往往在事件被采集时已经存在，
    命题从建立那天起就已经成立，**零证伪风险**。

    机械代理（保守、有明确边界）：证据里出现"已发布/已正式出台"这类**既成事实标记**，
    同时主信号的动作词也出现在证据里，就判为既成事实。判不出来就不判——但结算标准
    文本里已经写明了"必须在 window_start 之后首次被观测到"，人工判定时仍有依据。
    """
    main_signal = str(main_signal or "")
    evidence = str(evidence_text or "")
    if not main_signal or not evidence:
        return False
    if not any(marker in evidence for marker in _ALREADY_TRUE_MARKERS):
        return False
    return any(word in main_signal and word in evidence for word in _SIGNAL_ACTION_WORDS)


# 主信号里的动作词。用于判断"这条信号说的是不是一件已经做完了的事"。
_SIGNAL_ACTION_WORDS = (
    "发布", "公布", "出台", "印发", "下达", "生效", "实施", "启动", "开工",
    "签署", "挂牌", "通报", "批准", "批复", "立案", "处罚", "通过", "调整",
)


def _settlement_ready(candidate):
    """证伪性闸（机械检查）：不合格停在待补充、不进账本。返回 (ok, reason)。

    v1.4 只保留**实质可失败**的分支。此前这道闸查四件事，其中两件结构上不可能失败
    （R-06）：
      - 「有可核验日期」查的是 `title + criteria` 里有无 `\\d{4}-\\d{2}-\\d{2}`，
        而 title 由 `_candidate` 自己写成 `f"截至{end_date}：…"` → 必然命中；
      - 「有判定依据」查 criteria 里有无 `(核验|官方|公告|文件|记录|来源)`，而
        criteria 是同函数硬编码的文本，本来就含"…核验以官方公告…" → 必然命中。
    两项已删除（不改成校验外部输入）：本闸只该拦"**命题内容**"写得好不好，
    把 `published_at` 之类的来源元数据搬进来会让闸门依赖另一个模块的状态，
    它的失败原因也就不再指向"这条命题写得不行"。

    现在能失败的分支只剩四条，且都是实质的：
      1. 有可观测条件（`observable_signals` 非空、非占位）
      2. 主信号可确定（`observable_signals` 的第一条）—— R-09
      3. 可观测信号**事件级特化**（至少一条含本事件特有实体）—— R-08
      4. 主信号不是"窗口开始前就已成立"的既成事实 —— R-09
      5. 无禁用虚词，且禁用词只查**生成的部分**（不含新闻原标题）—— R-07

    这个顺序就是"可失败分支"的全部；不要在此之外加看起来更严的检查。
    """
    signals = (candidate.get("observable_signals", "") or "").strip()
    if not signals or signals in ("待补充", "尚未补充"):
        return False, "命题缺少可观测条件"
    main_signal = _main_signal(signals)
    if not main_signal or main_signal in ("待补充", "尚未补充"):
        return False, "命题未指定主信号，无法判定「发生/未发生」"
    if not _signals_specialized(signals, _candidate_terms(candidate), _candidate_event_text(candidate)):
        return False, "可观测信号未特化（不含本事件的机构名/地名/数字），需人工补充"
    if candidate.get("main_signal_preexisting"):
        return False, "主信号在窗口开始前已成立（零证伪风险），需换一条落在窗口内的信号"
    hit = next((p for p in _BANNED_PHRASES if p in _generated_text(candidate)), None)
    if hit:
        return False, f"命题含不可结算表述：{hit}"
    return True, ""


# ---- 第二波：概率中心来自 base_rate + 信号，区间宽度只由 E 级决定 ----
# 审计批的点：旧映射把「多少家转载」直接当概率中心，来源越多概率越高，且与
# 事件本身、与「条件是否埋下」完全无关。修复是**拆开**这两件事——中心走账本
# 自身的基准率 + 实质信号，宽度才看证据等级（证据越弱越宽，E1 最宽、E4 最窄）。
# 顺序不能反：先基准、后信号。
_INTERVAL_WIDTH = {"E1": 0.18, "E2": 0.14, "E3": 0.10, "E4": 0.07}


def _interval_width(evidence_level: str) -> float:
    """E 级只决定区间宽度，不参与中心。"""
    return _INTERVAL_WIDTH.get(evidence_level, _INTERVAL_WIDTH["E1"])


# 领先指标对概率中心的影响上限（与 knowledge_base.MAX_LEADING_BOOST 同一件事）。
# 上限之所以必需：8 条模式可以同时命中（"降息+专项债+试点"很常见），
# 不封顶就有 0.87 的推力，那就退化成"关键词越多概率越高"。
MAX_LEADING_BOOST = 0.20


def _signal_adjustment(judgment) -> float:
    """把「与来源多少无关」的真实信号折算成中心偏移。

    只用三样：
      1. 紧迫性（horizons）
      2. 是否有具体的可观测领先指标 / 触发条件
      3. **命中的领先指标规则及其 risk_boost**（v1.3 起真正参与运算）

    **不**用 confidence、也**不**用来源数量——那正是审计在批的失真来源。

    v1.3 修正：`risk_boost` 此前只被拼进一句展示文本（"风险上调 +15%"），
    从未进过任何公式 —— 用户看到"风险上调 15%"，而那个数字是装饰。
    现在它按 knowledge_base 给出的权重**真实折进中心**，合计封顶
    MAX_LEADING_BOOST，并作为独立一段写进候选卡（可核对）。
    """
    urgency = _urgency(judgment.get("horizons", ()))
    triggers = (judgment.get("up_triggers") or []) + (judgment.get("down_triggers") or [])
    concrete = 1.0 if any(
        t and str(t).strip() not in ("", "待补充", "尚未补充") for t in triggers
    ) else 0.0
    gyw = judgment.get("gyw") or {}
    leading = gyw.get("leading_indicators", "")
    has_leading = 1.0 if leading and str(leading).strip() not in ("", "待补充", "尚未补充") else 0.0
    raw = urgency * 0.5 + (concrete + has_leading) / 2 * 0.5  # 0..1
    delta = (raw - 0.5) * 0.4
    boost = _leading_boost(judgment)
    # 上限比单项宽：领先信号是"条件已埋下"的直接证据，配得上独立的一段推力。
    return max(-0.2, min(0.30, delta + boost))


def _leading_boost(judgment) -> float:
    """取本次研判的领先指标合计权重（已封顶）。来源优先级：
    gyw.leading_boost（本地研判写入）> gyw.leading_indicator_hits（重算）。"""
    gyw = judgment.get("gyw") or {}
    try:
        value = float(gyw.get("leading_boost") or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        hits = gyw.get("leading_indicator_hits") or []
        value = sum(
            float(item.get("risk_boost") or 0.0)
            for item in hits
            if isinstance(item, dict)
        )
    return max(0.0, min(MAX_LEADING_BOOST, value))


# 量级：从证据文本里抽可核验的数量级数字。
# 为什么必须有它（审计 2.11）：L1–L4 的分数是
#   证据×.25 + 置信×.20 + **我在乎**×.25 + 领域相关×.20 + 紧迫×.10
# ——里面有两项是"对我有多相关"，所以它算出来的是**关注度**，不是风险量级。
# 界面此前把它翻成"高/中/低风险"，等于把关注度当成损失规模。既不能改名糊过去
# （用户仍需要知道"这事大不大"），也不能编数字，所以**如实标注"未量化"**。
_NUMBER_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|亿元|万亿|万元|元|个百分点|bp|BP|万|亿|"
    r"吨|万吨|万人|万户|万立方米|立方米|公里|亩|平方米|亩产)"
)
_MAGNITUDE_SCOPE = {
    "central_direct": "全国（中央发文）",
    "vertical_agency": "全国（垂直系统）",
    "ministry_lead": "全国/行业",
    "local_lead": "地方/区域",
}


def _magnitude(judgment, cluster) -> dict:
    """给出「这件事的量级」的**可核验**表述，没有就明说没有。"""
    texts = [
        str(cluster.get("summary") or ""),
        str(judgment.get("fact_summary") or ""),
        " ".join(str(x) for x in (judgment.get("causal_chain") or [])),
        str((judgment.get("gyw") or {}).get("constraints") or ""),
    ]
    numbers: list[str] = []
    for text in texts:
        for match in _NUMBER_RE.finditer(text):
            value = match.group(0).strip()
            if value not in numbers:
                numbers.append(value)
    numbers = numbers[:5]

    power = (judgment.get("gyw") or {}).get("power_structure") or {}
    scope = _MAGNITUDE_SCOPE.get(str(power.get("rule") or ""), "未判定")

    if numbers:
        level = "有数字，但未折算成本/收益量级"
        basis = (
            "证据里出现了这些数量：" + "、".join(numbers)
            + "。它们说明事件有可核验的规模线索，但**尚未折算成对你本人的成本或收益**，"
            "所以不能当成量级结论。"
        )
    else:
        level = "未量化"
        basis = (
            "证据中没有任何金额/数量/规模数字，因此**无法给出量级**。"
            "这不是「小」，是「不知道」——不要用看起来严重的词替代没有的数字。"
        )
    return {"scope": scope, "level": level, "numbers": numbers, "basis": basis}


def magnitude_line(magnitude) -> str:
    """把量级对象压成一行可落库的文本。"""
    if not isinstance(magnitude, dict):
        return ""
    return f"范围 {magnitude.get('scope', '未判定')}；量级 {magnitude.get('level', '未量化')}"


def _probability_center(base_rate, judgment) -> float:
    """概率中心 = 基准率 + 信号调整。

    - base_rate 有值：中心随基准率走，再叠信号。
    - base_rate 为 None（样本不足）：用**中性先验 0.5**，而非编造一个基准率；
      信号调整照常。二者是两件事：前者如实报 None，后者仍给个有锚的猜测。
    """
    prior = base_rate if base_rate is not None else 0.5
    return max(0.05, min(0.95, prior + _signal_adjustment(judgment)))


# GYW framework fallback templates (mirror of LocalHeuristicProvider._GYW_TEMPLATES).
# Used by pending_candidates to backfill gyw for legacy judgments that
# pre-date the GYW schema, without rewriting historical judgment rows.
_GYW_BACKFILL = {
    "cashflow": {
        "stakeholders": "推动方：付款方、金融机构；阻力方：风控合规、审计",
        "constraints": "现金流约束：银行不良率、上下游账期、企业利润空间",
        "least_resistance_path": "最小阻力路径：分期拨付 / 展期重组 / 国资兜底",
        "counter_evidence": "反对证据：政策叫停、流动性收紧、反腐审计",
        "leading_indicators": "领先指标：实际拨付时间、配套政策落地",
    },
    "finance": {
        "stakeholders": "推动方：监管、机构投资者；阻力方：散户、合规",
        "constraints": "市场约束：流动性、估值、跨境资本",
        "least_resistance_path": "最小阻力路径：渐进调整 / 试点先行",
        "counter_evidence": "反对证据：监管反向、市场恐慌、外部冲击",
        "leading_indicators": "领先指标：监管口径、北向资金、信用利差",
    },
    "policy": {
        "stakeholders": "推动方：发文机关、上级政府；阻力方：执行部门、利益集团",
        "constraints": "资源约束：财政预算、编制、配套立法",
        "least_resistance_path": "最小阻力路径：试点 → 推广 → 全面执行",
        "counter_evidence": "反对证据：执行阻力、利益集团游说、政策转向",
        "leading_indicators": "领先指标：试点公告、配套细则、部门预算",
    },
    "work": {
        "stakeholders": "推动方：雇主、地方政府；阻力方：工会、员工",
        "constraints": "成本约束：企业利润空间、财政补贴",
        "least_resistance_path": "最小阻力路径：分阶段执行 / 试点先行",
        "counter_evidence": "反对证据：经济下行、财政紧张、企业抵制",
        "leading_indicators": "领先指标：地方实施细则、行业响应",
    },
    "opportunity": {
        "stakeholders": "推动方：投资人、地方政府、产业方；阻力方：竞争者、监管",
        "constraints": "市场约束：需求、资本、关键技术",
        "least_resistance_path": "最小阻力路径：先小规模试水 → 复制扩张",
        "counter_evidence": "反对证据：竞争者抢先、政策转向、技术失败",
        "leading_indicators": "领先指标：投资公告、试点规模、关键客户签约",
    },
    "family": {
        "stakeholders": "推动方：家庭成员；阻力方：其他家庭成员、时间",
        "constraints": "资源约束：时间、金钱、精力",
        "least_resistance_path": "最小阻力路径：分阶段执行 / 借力外部",
        "counter_evidence": "反对证据：家庭沟通阻力、突发情况",
        "leading_indicators": "领先指标：家庭讨论结果、资源到位",
    },
}
_GYW_BACKFILL_DEFAULT = {
    "stakeholders": "推动方：事件发起方；阻力方：执行部门、外部不确定",
    "constraints": "资源约束：财政、编制、执行能力、外部配合",
    "least_resistance_path": "最小阻力路径：分阶段执行 / 试点先行",
    "counter_evidence": "反对证据：执行阻力、政策转向、外部冲击",
    "leading_indicators": "领先指标：配套细则、试点公告、执行进度",
}


def _backfill_gyw(category: str) -> dict:
    key = str(category or "").casefold()
    return dict(_GYW_BACKFILL.get(key) or _GYW_BACKFILL_DEFAULT)


EVIDENCE_WEIGHTS = {"E1": 0.25, "E2": 0.50, "E3": 0.75, "E4": 1.0}
CATEGORY_EXPOSURE = {
    "health": {"health": 1.0, "family": 0.7, "cashflow": 0.6},
    "finance": {"cashflow": 1.0, "assets": 1.0, "work": 0.5},
    "employment": {"work": 1.0, "cashflow": 0.8, "opportunity": 0.6},
    "policy": {"policy": 1.0, "cashflow": 0.4, "work": 0.4, "opportunity": 0.5},
    "safety": {"health": 0.9, "family": 0.9, "assets": 0.5},
    "housing": {"assets": 0.9, "family": 0.7, "cashflow": 0.6},
    "technology": {"opportunity": 0.7, "work": 0.6, "assets": 0.4},
    "business": {"opportunity": 1.0, "work": 0.8, "cashflow": 0.7},
    "legal": {"policy": 0.8, "assets": 0.6, "family": 0.5},
    "education": {"family": 0.8, "opportunity": 0.7, "cashflow": 0.4},
    "transportation": {"work": 0.6, "cashflow": 0.5, "family": 0.4},
    "environment": {"health": 0.6, "family": 0.5, "assets": 0.4},
    "global": {"assets": 0.5, "opportunity": 0.5, "cashflow": 0.4},
    "general": {"opportunity": 0.35},
}


# ════════════════════════════════════════════════════════════
# v1.5：把方法论的结构化产物接进定级（改动 A + B）
# ════════════════════════════════════════════════════════════
# 用的都是**已存在**的结构化产物（`gyw.power_structure` 与 `gyw.risk_signal_hit`，
# 由 judgment_local 产出、_candidate 已落库），**不新增任何 AI 输出字段**。
#
# 改动 A —— L4 结构闸：L4 必须在结构上有抓手（存在执行摩擦，或已出现风险信号）。
L4_STRUCTURAL_DELAYS = ("高", "中")

# 改动 B —— 事件侧强度 s ∈ (0,1]，乘到 interest.importance 上（权重仍 1.00）。
#   语义：**只有拿到"这件事结构性弱"的正面证据才下调**（垂直机构办事=低摩擦、
#   部委牵头=中摩擦）；有摩擦（高）、有风险信号、以及**结构未知**都保持 1.0。
#
#   ⚠ "未知不降分"是刻意的，不是偷懒：真库 11.9 万条 personal_impacts 离线重算
#   （读 judgment.gyw.power_structure）显示，delay=未知 占 52.7%；若把未知按 0.85
#   惩罚，等于**给一半以上的库整体打折**——L3 从 16.1% 塌到 6.8%，并有 13,238 条
#   原本 L3/L4 的事件掉回 L1/L2（R-16 下这些事件**连候选都不再生成**）。
#   那是在"按信息缺失降分"，与"识别不到就如实说未知、不猜"的本机哲学相悖。
#   取"未知=1.0"后：L3 保持 15.7%，掉级仅 2,619 条，而 L4/天 仍从 12 压到 4。
STRUCTURAL_INTENSITY = {"高": 1.0, "中": 0.95, "低": 0.9}
STRUCTURAL_INTENSITY_UNKNOWN = 1.0
STRUCTURAL_INTENSITY_RISK = 1.0


def _structural_intensity(delay_risk, risk_signal_hit):
    """事件侧结构强度：(0,1]。有风险信号或执行摩擦（高）→1.0；部委牵头（中）→0.95；
    垂直机构（低，办事阻力最小）→0.9；**结构未知/缺失 →1.0（不因信息缺失降分）**。"""
    if risk_signal_hit:
        return STRUCTURAL_INTENSITY_RISK
    return STRUCTURAL_INTENSITY.get(delay_risk, STRUCTURAL_INTENSITY_UNKNOWN)


def _has_structural_signal(delay_risk, risk_signal_hit):
    """L4 结构闸：存在执行摩擦（delay ∈ {高,中}）或已出现风险信号才放行。"""
    return bool(risk_signal_hit) or delay_risk in L4_STRUCTURAL_DELAYS


def _text_list(value) -> list:
    """把历史字段收敛成字符串列表：None / 标量 / bool 都不得让定级崩掉。

    真库里存在 `risk_signal_hit: true`、`impact_categories: "health"` 这类**历史形状**
    （字段早期只记"有没有"，或记成了标量）。存量回填要通读**全部**旧行，一行脏数据
    不能让整批迁移失败 —— 所以统一在这里收敛，并且**保守**：认不出来就当一个空表，
    绝不猜出内容来。
    """
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, dict):
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _risk_hits(judgment) -> list:
    """本次研判命中的「慷慨激昂」词表（历史 bool 形状 → 空表）。"""
    return _text_list((judgment.get("gyw") or {}).get("risk_signal_hit"))


# 结构信号的来源标记 —— 让每一条 `delay_risk` 都能回溯"是谁给的"：
# 让每一条 `delay_risk` 都能回溯"是谁给的"：
#   · judgment       —— 研判自带的 `gyw.power_structure`（本地分支本来就产它）
#   · local_backfill —— 远程研判缺这个字段，由**本机同一个规则引擎**在读时补算
#   · absent         —— 研判里确实没有，且不该补（本地研判）→ 结构如实未知
# 必须可区分：补算出来的结论与本地自产的结论，日后要能分开统计、分开追责。
STRUCTURE_SOURCE_JUDGMENT = "judgment"
STRUCTURE_SOURCE_LOCAL_BACKFILL = "local_backfill"
STRUCTURE_SOURCE_ABSENT = "absent"


def _needs_structure_backfill(judgment, provider):
    """是否要为本条研判补算权力结构：**只对远程研判**，且它确实缺字段。

    本地研判不补 —— 本地分支每条都产 `power_structure`，这里若缺说明它真的缺，
    补它等于给本地分支换一套判据，超出"补齐远程缺口"的范围。
    """
    if str(provider or "local") == "local":
        return False
    return not ((judgment.get("gyw") or {}).get("power_structure"))


def _backfill_power_structure(cluster_title, cluster_summary, judgment, entity_names=None):
    """远程研判缺 `gyw.power_structure` 时，用**本机同一个规则引擎**补算。

    为什么必须补：L4 结构闸（改动 A）要求 L4 在结构上有抓手（执行摩擦或风险信号），
    而远程 provider 的输出契约里**根本没有** `power_structure` 字段 —— 不补的话，
    用户花额度换来的"远程升级版"会被**系统性降为 L3**，等于花钱买降级。

    判据不做第二套：直接用 `judgment_local` 产这条字段时的**同一个函数**
    `knowledge_base.analyze_power_structure`。`institutions` 优先取事件簇的
    `event_entities`；为空时该函数内部会对同一段文本自己抽机构名（`extract_orgs`），
    与本地分支的口径一致 —— 这里**不另写抽取器**。

    只算不写：`judgments.content_json` 是判读原文、不可变，补算结果只进本次计算路径
    —— 落进 `components_json` 并带 `structure_source` 标记，可回溯、可区分。
    """
    from .knowledge_base import analyze_power_structure

    institutions = [
        str(name).strip() for name in (entity_names or []) if str(name).strip()
    ]
    text = " ".join(
        [
            str(cluster_title or ""),
            str(cluster_summary or ""),
            str(judgment.get("fact_summary") or ""),
            " ".join(_text_list(judgment.get("causal_chain"))),
        ]
    )
    return analyze_power_structure(institutions, text)


def _resolve_power_structure(
    judgment, provider, *, cluster_title="", cluster_summary="", entity_names=None
):
    """取本次定级要用的权力结构，并标明它从哪来（`structure_source`）。"""
    power = (judgment.get("gyw") or {}).get("power_structure") or {}
    if power:
        return power, STRUCTURE_SOURCE_JUDGMENT
    if str(provider or "local") == "local":
        return {}, STRUCTURE_SOURCE_ABSENT
    return (
        _backfill_power_structure(
            cluster_title, cluster_summary, judgment, entity_names
        ),
        STRUCTURE_SOURCE_LOCAL_BACKFILL,
    )


def _evaluate_impact(
    *,
    evidence_level,
    categories,
    interest,
    judgment,
    penalties,
    power_structure,
    structure_source=STRUCTURE_SOURCE_ABSENT,
):
    """**唯一的定级入口**：暴露度 → 算分 → 分档 → 风险上调 → E1 封顶 → L4 结构闸。

    `map_judgment`（新事件）与 `recompute_personal_impacts`（存量回填）都走这里，
    两条路因此不可能漂移 —— 否则"回填出来的档位"与"新算的档位"会是两套口径，
    而用户拿它们并排看时只会看到自相矛盾。

    `power_structure` / `structure_source` 由**调用方解析一次**后传入：
    解析要查 `event_entities`（一次数据库往返），不该在每个利益对象上重算一遍。

    返回 None 表示该利益对本事件的暴露度 <= 0（本就不该有候选）。
    """
    evidence = EVIDENCE_WEIGHTS.get(evidence_level, 0.25)
    confidence = max(0.0, min(float(judgment.get("confidence", 0.0) or 0.0), 1.0))
    urgency = _urgency(_text_list(judgment.get("horizons")))
    categories = tuple(_text_list(categories))
    exposure = max(
        (
            CATEGORY_EXPOSURE.get(category, {}).get(interest["category"], 0.0)
            for category in categories
        ),
        default=0.0,
    )
    exposure = round(exposure * penalties.get(interest["category"], 1.0), 6)
    if exposure <= 0:
        return None
    power_structure = power_structure or {}
    delay_risk = power_structure.get("delay_risk")
    structural_rule = power_structure.get("rule")
    risk_hits = _risk_hits(judgment)
    structural_intensity = _structural_intensity(delay_risk, risk_hits)
    importance_base = max(1, min(int(interest["importance"]), 5)) / 5
    # v1.5：importance 不再恒定 —— 利益侧（用户设定）**×** 事件侧结构强度。
    # 此前同一"利益对象+类目+来源数"的事件分数恒等（importance 恒为 0.6）；
    # 现在它随这件事本身的结构信号（执行阻力 / 风险）变化。
    importance = importance_base * structural_intensity
    components = {
        "evidence": evidence,
        # confidence 仍然留档（可回溯、可重算旧分数），但**不参与** base_score。
        "confidence": confidence,
        "importance": importance,
        # v1.5 新增（可回溯）：利益侧基准 / 事件侧强度 / 结构信号原值 + 来源。
        "importance_base": importance_base,
        "structural_intensity": structural_intensity,
        "structural_rule": structural_rule,
        "delay_risk": delay_risk,
        "structure_source": structure_source,
        "exposure": exposure,
        "urgency": urgency,
    }
    # v1.4（R-05）：`confidence` **不再进 base_score**。
    #
    # 为什么：在本地路径下 confidence 就是
    # `{"E1":0.30,"E2":0.50,"E3":0.70,"E4":0.82}[E级]`（judgment_local），
    # 而 evidence 是 `EVIDENCE_WEIGHTS[E级]` —— **两者都是"独立域名数"的
    # 单调函数**，合计权重 0.45。同一个变量计两次，等于把"来源多"这件事
    # 放大近一倍来驱动首页排序、通知、以及"能否被自动写进账本"。
    # v1.2 已把**概率**与来源数解耦，但驱动关注度的分数没动，而这恰好是
    # 用户每天看到的东西 —— 这是本次补上的那一半。
    #
    # 移除后权重按剩余四项**等比**放大（原合计 0.80 → 1.00）：不改各项之间的
    # 相对关系，只去掉重复计分的那一项。`confidence` 留在 components 里只为
    # 可回溯 —— **留在留档里不等于参与运算**。
    base_score = (
        evidence * 0.3125
        + importance * 0.3125
        + exposure * 0.25
        + urgency * 0.125
    )
    # 结合AI对"用户本人相关性"的结论做升降级：判无关则压到行动板之下
    relevance = _personal_relevance(judgment)
    components["personal_relevance"] = relevance
    score = round(max(0.0, min(base_score * relevance, 1.0)), 6)
    alert = _alert_level(score)
    # 「慷慨激昂 = 内心已感知风险」→ **上调告警等级**。
    # v1.3 修正：旧代码在本地研判里做的是 `confidence += 0.08`，方向反了
    # （越慷慨激昂，系统越自信）。规则引擎的判断本该落在风险侧，这里落地。
    if risk_hits:
        alert = _elevate(alert)
        components["risk_signal_keywords"] = risk_hits
        components["alert_before_risk_signal"] = _alert_level(score)
    # E1 是单一来源线索，未经互证。README 与 PRIVACY.md 对外承诺
    # 「E1 无论多重要都不得超过 L3」，此处是该承诺的强制点：只有 E2 及以上
    # （同域转载不算互证）才允许进入 L4 立即行动。证据等级缺失或无法识别时
    # 与 EVIDENCE_WEIGHTS 的兜底权重一致，按 E1 处理，宁可保守。
    # ⚠ 顺序要求：**在上调之后**执行这道封顶，否则风险信号会把 E1 顶上 L4。
    if alert == "L4" and evidence <= EVIDENCE_WEIGHTS["E1"]:
        alert = "L3"
    # v1.5（改动 A）：L4 结构闸。放在 E1 封顶**之后** —— E1 封顶是对外承诺，
    # 先行强制；结构闸是"L4 还必须有结构性抓手"这条新纪律，只在事件仍为 L4
    # 时发挥作用。缺结构化产物（rule/delay 缺失且无风险信号）时**视为不满足，
    # 降为 L3**（宁可保守）。降级原因写进 components_json 以便回溯。
    if alert == "L4" and not _has_structural_signal(delay_risk, risk_hits):
        alert = "L3"
        components["l4_downgraded_by"] = "no_structural_signal"
        components["l4_gate_rule"] = structural_rule
        components["l4_gate_delay_risk"] = delay_risk
    return {
        "exposure": exposure,
        "score": score,
        "alert": alert,
        "components": components,
    }


# ── 存量回填（v1.5 口径）──────────────────────────────────────────────
ALERT_LEVELS = ("L1", "L2", "L3", "L4")


def _category_penalties_from_connection(connection):
    """Feedback-learning multipliers persisted by the learning consumer.

    读失败**不再静默清零**：`{}` 的含义是"没有惩罚"，与"读不到所以当没有"
    在行为上一样，但后者是降级 —— 抹平它会让"学习回路其实没生效"永远看不见。
    返回值保持 dict（调用方 `exposure * penalties.get(...)` 直接做算术，
    不能返回 None），可观测性由日志承担。
    """
    try:
        row = connection.execute(
            "SELECT value_json FROM runtime_state WHERE state_key=?",
            ("learning.category_penalties",),
        ).fetchone()
    except Exception:
        _logger.warning(
            "读取反馈学习惩罚系数失败，本轮按无惩罚（全 1.0）处理", exc_info=True
        )
        return {}
    try:
        penalties = json.loads(row["value_json"]) if row else {}
    except (TypeError, json.JSONDecodeError):
        _logger.warning(
            "反馈学习惩罚系数内容不是合法 JSON，按无惩罚处理：%r",
            (row["value_json"] if row else "")[:120],
        )
        penalties = {}
    if not isinstance(penalties, dict):
        _logger.warning(
            "反馈学习惩罚系数不是对象（%s），按无惩罚处理", type(penalties).__name__
        )
        return {}
    return {
        str(key): max(0.5, min(float(value), 1.0))
        for key, value in penalties.items()
        if isinstance(value, (int, float))
    }


def _alert_distribution(connection):
    """当前 L1–L4 的条数与占比（回填前后的真值都由它读出来）。"""
    counts = {level: 0 for level in ALERT_LEVELS}
    for row in connection.execute(
        "SELECT alert_level, COUNT(*) FROM personal_impacts GROUP BY alert_level"
    ):
        counts[str(row[0])] = int(row[1])
    total = sum(counts.values())
    return {
        "total": total,
        "counts": counts,
        "shares": {
            level: (round(count / total, 4) if total else 0.0)
            for level, count in counts.items()
        },
    }


def _load_impact_contexts(connection, *, entity_limit=50):
    """回填用上下文：`{judgment_id: {...}}`，只覆盖个人影响真正引用到的研判。

    ⚠ 性能是**实测**约束，不是臆测。真库（119,331 条 `personal_impacts`、79,088 条
    `judgments`、80,245 个事件簇、463,157 条 `event_entities`）上的对照：

        · 全表扫 `event_clusters` 取 18.5 万行/61,885 个簇 → 15.5s
        · 用临时表 JOIN 只取用到的簇                      →  0.7s
        · 全表扫 `event_entities`（46.3 万行）            → 27.1s
        · 只对"真的需要补算"的 732 个簇 JOIN              →  亚秒

    所以这里**只取用到的行**：临时表装 id，再 JOIN 回主表（走主键索引）。
    事件簇实体更是**只为远程研判**查 —— 本地研判自带结构判定，用不上它。

    库里的 `event_entities.category` 目前恒为 `shared_term`（cognition.py 写死），
    存的是"同簇共现词片段"而**不是机构名**。仍按约定优先取它：真要出现机构名时
    能直接用上；取不到就由 `analyze_power_structure` 对同一段文本自己抽。
    """
    needed = {
        row["judgment_id"]: row["cluster_id"]
        for row in connection.execute(
            "SELECT DISTINCT cluster_id, judgment_id FROM personal_impacts"
        )
    }
    if not needed:
        return {}
    cluster_ids = set(needed.values())
    connection.execute(
        "CREATE TEMP TABLE _yj_needed_cluster(cluster_id TEXT PRIMARY KEY)"
    )
    connection.executemany(
        "INSERT OR IGNORE INTO _yj_needed_cluster VALUES (?)",
        ((cluster_id,) for cluster_id in cluster_ids),
    )
    clusters = {
        row["cluster_id"]: row
        for row in connection.execute(
            "SELECT c.cluster_id, c.title, c.summary, c.evidence_level"
            " FROM event_clusters c"
            " JOIN _yj_needed_cluster x ON x.cluster_id = c.cluster_id"
        )
    }
    connection.execute(
        "CREATE TEMP TABLE _yj_needed_judgment(judgment_id TEXT PRIMARY KEY)"
    )
    connection.executemany(
        "INSERT OR IGNORE INTO _yj_needed_judgment VALUES (?)",
        ((judgment_id,) for judgment_id in needed),
    )
    loaded = {}
    entities_wanted = set()
    for row in connection.execute(
        "SELECT j.judgment_id, j.provider, j.content_json"
        " FROM judgments j"
        " JOIN _yj_needed_judgment x ON x.judgment_id = j.judgment_id"
    ):
        try:
            content = json.loads(row["content_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(content, dict):
            continue
        provider = str(row["provider"] or "local")
        cluster_id = needed[row["judgment_id"]]
        if _needs_structure_backfill(content, provider):
            entities_wanted.add(cluster_id)
        loaded[row["judgment_id"]] = (cluster_id, provider, content)
    entities: dict = {}
    if entities_wanted:
        connection.execute(
            "CREATE TEMP TABLE _yj_need_entity(cluster_id TEXT PRIMARY KEY)"
        )
        connection.executemany(
            "INSERT OR IGNORE INTO _yj_need_entity VALUES (?)",
            ((cluster_id,) for cluster_id in entities_wanted),
        )
        for row in connection.execute(
            "SELECT e.cluster_id, e.name FROM event_entities e"
            " JOIN _yj_need_entity x ON x.cluster_id = e.cluster_id"
            " ORDER BY e.confidence DESC"
        ):
            if not row["name"]:
                continue
            bucket = entities.setdefault(row["cluster_id"], [])
            if len(bucket) < entity_limit:
                bucket.append(str(row["name"]))
    contexts = {}
    for judgment_id, (cluster_id, provider, content) in loaded.items():
        cluster = clusters.get(cluster_id)
        power_structure, structure_source = _resolve_power_structure(
            content,
            provider,
            cluster_title=(cluster["title"] if cluster else "") or "",
            cluster_summary=(cluster["summary"] if cluster else "") or "",
            entity_names=entities.get(cluster_id),
        )
        contexts[judgment_id] = {
            "provider": provider,
            "judgment": content,
            "categories": tuple(_text_list(content.get("impact_categories"))),
            "evidence_level": cluster["evidence_level"] if cluster else None,
            "power_structure": power_structure,
            "structure_source": structure_source,
        }
    return contexts


def recompute_personal_impacts(database, *, batch_size=5000):
    """存量 `personal_impacts` 一次性回填到当前（v1.5）定级口径。

    **只写 `alert_level` 与 `components_json` 两列** —— 绝不删行，也绝不动
    `interest_id` / `cluster_id` / `judgment_id` / `created_at`。

    为什么必须回填：定级口径变了（事件侧结构强度 + L4 结构闸 + 风险上调 + E1 封顶），
    存量行却仍是**旧口径**算出来的。不回填的话，用户打开程序看到的还是旧 L4，
    会以为"修复根本没生效"。

    幂等 + 可中断：同样的输入必然得到同样的输出；逐批提交，中途被打断后重跑即可
    （迁移标记只在**整批成功后**写入，见 `Database._apply_legacy_alert_backfill`）。
    """
    started = time.monotonic()
    report = {
        "total": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped_no_context": 0,
        "level_changed": 0,
        "structure_backfilled": 0,
        "before": {},
        "after": {},
        "duration_seconds": 0.0,
    }
    with database.connect() as connection:
        report["before"] = _alert_distribution(connection)
        interests = {
            row["object_id"]: dict(row)
            for row in connection.execute(
                "SELECT object_id, name, category, importance, status"
                " FROM interest_objects"
            )
        }
        penalties = _category_penalties_from_connection(connection)
        contexts = _load_impact_contexts(connection)
        pending_since_commit = 0
        for row in connection.execute(
            "SELECT impact_id, judgment_id, interest_id, alert_level,"
            " components_json FROM personal_impacts"
        ):
            report["total"] += 1
            context = contexts.get(row["judgment_id"])
            interest = interests.get(row["interest_id"])
            if context is None or interest is None:
                # 研判正文读不出来、或利益对象已不存在 —— 宁可不碰，也不猜。
                report["skipped_no_context"] += 1
                continue
            evaluated = _evaluate_impact(
                evidence_level=context["evidence_level"],
                categories=context["categories"],
                interest=interest,
                judgment=context["judgment"],
                penalties=penalties,
                power_structure=context["power_structure"],
                structure_source=context["structure_source"],
            )
            if evaluated is None:
                report["skipped_no_context"] += 1
                continue
            level = evaluated["alert"]
            components = evaluated["components"]
            if context["structure_source"] == STRUCTURE_SOURCE_LOCAL_BACKFILL:
                report["structure_backfilled"] += 1
            try:
                previous = json.loads(row["components_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                previous = None
            if previous == components and level == row["alert_level"]:
                # 已经是当前口径 —— 不写。回填因此可以反复运行而结果恒定。
                report["unchanged"] += 1
                continue
            if level != row["alert_level"]:
                report["level_changed"] += 1
            connection.execute(
                "UPDATE personal_impacts SET alert_level=?, components_json=?"
                " WHERE impact_id=?",
                (
                    level,
                    json.dumps(components, ensure_ascii=False, sort_keys=True),
                    row["impact_id"],
                ),
            )
            report["updated"] += 1
            pending_since_commit += 1
            if pending_since_commit >= batch_size:
                connection.commit()
                pending_since_commit = 0
        connection.commit()
        report["after"] = _alert_distribution(connection)
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    return report


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _elevate(level):
    """告警等级上调一档（L1→L2→L3→L4，L4 封顶）。"""
    return {"L1": "L2", "L2": "L3", "L3": "L4", "L4": "L4"}.get(level, level)


def _alert_level(score):
    """关注度分档阈值（v1.4 重新校准，依据是真实库上的分数分布）。

    R-05 把 `confidence` 从分数里去掉之后，分数量纲变了（去掉一个 0.20 权重的项、
    其余按比例放大），旧阈值 0.35/0.55/0.67 不再落在分布的自然位置上。新阈值
    0.45/0.60/0.68 是**在真库 118,379 条 personal_impacts 的 components_json 上
    重算分数、再挑出来的**（见 build-artifacts/v14_probe_*.py），三个阈值都落在
    该分布的空档里，没有任何一个切开密集取值簇：

        新公式下分数分布（离散取值 58 个）：
            0.41563 → 累计 1.13%      0.45312 → 45.32%
            0.44062 → 39.83%（最大簇） 0.59375 → 82.26%
            0.60313 → 94.12%          0.66875 → 97.82%
            0.68125 → 99.64%

        阈值取 (0.45, 0.60, 0.68) → L1 39.8% / L2 42.4% / L3 15.6% / L4 2.2%
        对比旧口径（L2 占 91.7%、最高档 145 条里 128 条挤在同一个分值 0.675），
        这是第一次四档都站得住。**这四个比例是挑阈值的依据，不是要凑出来的目标**：
        真实分布变了就该重新看，而不是把阈值往回调。
    """
    if score < 0.45:
        return "L1"
    if score < 0.60:
        return "L2"
    if score < 0.68:
        return "L3"
    return "L4"


# AI 在 personal_action 中明确判定"与该用户无关"的强信号。命中即说明事件虽有
# 公共层面重要性，但对用户本人无传导路径，应自动降级、不占L3/L4行动板——这是
# "系统自己过滤，不把判断推给用户"的关键一环。
_IRRELEVANT_PATTERNS = (
    # 允许"与你"和结论之间隔着对用户情况的复述（如"与你月薪5000…现金流无直接关系"）
    r"与(你|您).{0,40}?无(直接)?(关系|关联|交集|联系|挂钩)",
    r"与(你|您).{0,40}?(没有|并无|不存在)(直接)?(关系|关联|交集|联系|挂钩)",
    r"与(你|您).{0,16}?无关",
    r"(基本|大致|总体|整体)?.{0,4}(无关|不相干)",
    r"不(涉及|触及|作用于|影响|直接作用|直接影响)(你|您)",
    r"(对|对于)(你|您).{0,20}?无(直接)?(影响|关系|关联|作用)",
    r"无直接(关系|关联|影响|作用|联系)",
    r"不直接(作用|影响|相关|关联)",
    r"不是影响(你|您)个人决策",
    r"无需(为此|操作|行动|调整|关注|处理|紧张)",
    r"不必(为此|操作|行动|关注|处理|紧张)",
    r"不需要(你|您)(操作|行动|处理|关注)",
)
# 注意：正向词必须用否定后视排除"无/没/不/非/未直接关系"这类否定表述，
# 否则"无直接关系"里的子串"直接关系"会被误判成正向而保留本应降级的事件。
_RELEVANT_PATTERN = (
    r"(?<![无没非未不])直接(相关|关系|影响|作用)|"
    r"有直接(关系|关联|影响|作用)|直接作用于|实质影响|密切相关|间接相关|"
    r"与(你|您)相关|关系到(你|您)"
)


def _match_irrelevant(text: str) -> bool:
    return any(re.search(pattern, text) for pattern in _IRRELEVANT_PATTERNS)


def _personal_relevance(judgment) -> float:
    """返回个人相关性系数：AI明确判无关→0.45（压到L2以下），否则1.0。

    AI被要求先给结论，因此以句首判定为准：开头就说无关/基本无关的，即便后文
    为措辞周全提到"相关"，也不翻案；开头明确直接/间接相关的优先保留。"""
    text = str(judgment.get("personal_action") or "")
    if not text:
        return 1.0
    head = text[:42]
    if _match_irrelevant(head) and not re.search(_RELEVANT_PATTERN, head):
        return 0.45
    if re.search(_RELEVANT_PATTERN, text):
        return 1.0
    if _match_irrelevant(text):
        return 0.45
    return 1.0


def _urgency(horizons):
    text = " ".join(map(str, horizons))
    if any(word in text for word in ("立即", "今日", "7天", "一周")):
        return 1.0
    if any(word in text for word in ("30天", "一个月", "本月")):
        return 0.7
    return 0.4


def _window_days(horizons):
    numbers = [int(value) for value in re.findall(r"(\d+)天", " ".join(horizons))]
    return max(7, min(numbers or [90]))


class ImpactService:
    def __init__(self, database, interest_service, forecast_service, now=None):
        self.database = database
        self.interest_service = interest_service
        self.forecast_service = forecast_service
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _load(self, cluster_id, judgment_id):
        """读出事件簇、研判内容，以及**研判是谁产的**（provider）。

        provider 必须一起返回：远程研判缺 `gyw.power_structure` 时要在本机补算，
        而"要不要补"只由 provider 决定（见 `_needs_structure_backfill`）。
        """
        with self.database.connect() as connection:
            cluster = connection.execute(
                "SELECT * FROM event_clusters WHERE cluster_id=?", (cluster_id,)
            ).fetchone()
            judgment = connection.execute(
                "SELECT * FROM judgments WHERE judgment_id=? AND cluster_id=?",
                (judgment_id, cluster_id),
            ).fetchone()
        if cluster is None or judgment is None:
            raise KeyError(judgment_id)
        return (
            dict(cluster),
            json.loads(judgment["content_json"]),
            str(judgment["provider"] or "local"),
        )

    def _cluster_entity_names(self, cluster_id):
        """事件簇抽取到的实体名，优先给权力结构补算用。

        取不到就返回空表 —— 补算函数会退回对同一段文本自己抽机构名（与本地分支
        同款口径），**不会**因为这里为空就编一个结构出来。
        """
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT name FROM event_entities WHERE cluster_id=?"
                " ORDER BY confidence DESC",
                (cluster_id,),
            ).fetchall()
        return [str(row["name"]) for row in rows if row["name"]]

    def _candidate(self, cluster, judgment, interest, impact_id, alert_level=None):
        now = self.now().astimezone(timezone.utc)
        start_date = now.date().isoformat()
        end = now + timedelta(days=_window_days(judgment.get("horizons", [])))
        end_date = end.date().isoformat()
        # 命题要素：对象 / 方向 / 阈值或可观测事实 / 判定依据。
        # 旧版只写“是否产生实际影响”（含虚词），不可结算；
        # 新版必须给出可观测信号 + 可核验依据，否则证伪性闸会拒掉。
        observable = _extract_observable_signals(judgment)
        main_signal = _main_signal(observable)
        # 本事件特有的实体（机构名/地名/数字）。R-08 的特化闸要用它，所以必须**随
        # 候选卡一起落库** —— 那两道闸（confirm_candidate / auto_confirm_all）运行时
        # 手上只有候选卡，没有事件簇。
        event_terms = _event_terms(cluster, judgment)
        # 既成事实检查（R-09）：证据里已经有"已正式出台"这类标记、且主信号的动作词
        # 也在证据里出现过 → 这条信号在窗口开始前就已成立，零证伪风险。
        evidence_text = " ".join(
            [
                str(cluster.get("title") or ""),
                str(cluster.get("summary") or ""),
                str(judgment.get("fact_summary") or ""),
                " ".join(str(item) for item in (judgment.get("causal_chain") or [])),
            ]
        )
        preexisting = _main_signal_preexisting(main_signal, evidence_text)
        # 基准率：同类别历史结算命中率（账本自身 resolutions，**只采人工确认的**）。
        # 样本不足时返回 (None, n)，_probability_center 退化为中性先验 0.5，但
        # base_rate 字段如实记 None（不编造）。
        base_rate, base_rate_sample = self.forecast_service.base_rate_for_category(
            interest["category"]
        )
        # 样本构成（R-04）：界面要写「本类别样本 N 条，其中人工 M 条」。只给一个
        # 「样本不足」的话，用户既不知道差在哪，也不知道要做什么才能补上。
        composition = self.forecast_service.base_rate_composition(interest["category"])
        center = _probability_center(base_rate, judgment)
        width = _interval_width(cluster.get("evidence_level", "E1"))
        object_name = interest["name"]
        # 新闻原标题内嵌在命题标题里（历史如此，界面已经习惯），**同时**单独存一份
        # `source_headline`：禁用词检查要能把"记者的措辞"从"我们的命题"里摘出去
        # （R-07）。不单独存的话，闸门只能整段查，一条标题里带"可能影响"的新闻
        # 会让整条候选被误拒。
        source_headline = str(cluster.get("title") or "")
        title = (
            f"截至{end_date}：「{object_name}」是否因「{source_headline}」"
            f"受到可观测影响（即：{main_signal or '待补充'}）"
        )
        # 结算判据（R-09）：旧版对最多 4 条信号取**或**，且"在该日期前发生"把
        # 窗口开始前的既成事实也算了进去 —— 命题从建立那天起就可能已经成立。
        # 现在：指定**一条主信号**，其余只作辅助；并且要求信号在窗口内**首次**被观测到。
        resolution_criteria = (
            f"判定依据：以主信号为准核验（其余信号只作辅助观察，不参与“发生/未发生”判定）——"
            f"主信号：{main_signal or '待补充'}。"
            f"“发生”的判断标准：该信号在 {start_date}（窗口开始）之后、{end_date}（窗口截止）"
            f"之前首次被观测到；若在窗口开始前就已成立，记为“窗口开始时已成立”，"
            f"不等同于命中。核验以官方公告、主管部门文件及本地可核验记录为准。"
        )
        magnitude = _magnitude(judgment, cluster)
        gyw = judgment.get("gyw") or {}
        return {
            "impact_id": impact_id,
            "title": title,
            "source_headline": source_headline,
            "resolution_criteria": resolution_criteria,
            "observable_signals": observable,
            # 主信号（`observable_signals` 的第一条）。结算只认它。
            "main_signal": main_signal,
            "main_signal_preexisting": preexisting,
            # 本事件特有实体，供证伪性闸判定"信号是否事件级特化"（R-08）。
            "event_terms": event_terms,
            # 真实告警等级（confirm_candidate 此前硬编码 L3，把等级信息丢了）
            "alert_level": alert_level,
            # 量级：没有数字就明说"未量化"（审计 2.11）
            "magnitude": magnitude,
            "magnitude_line": magnitude_line(magnitude),
            # 「慷慨激昂」命中词：界面要显示"因为哪个词"，否则无从判断
            "risk_signal_hit": _text_list(gyw.get("risk_signal_hit")),
            "power_structure_rule": (gyw.get("power_structure") or {}).get("rule"),
            "delay_risk": (gyw.get("power_structure") or {}).get("delay_risk"),
            "leading_boost": _leading_boost(judgment),
            "window_start": start_date,
            "window_end": end_date,
            # 概率：中心 = base_rate + 信号调整；宽度只由 E 级决定（证据越弱越宽）。
            # 不再用「多少家转载」抬中心——那是审计批的失真源。
            "probability_low": round(max(0.0, center - width), 2),
            "probability_high": round(min(1.0, center + width), 2),
            "base_rate": base_rate,
            "base_rate_sample": base_rate_sample,
            "base_rate_composition": composition,
            "causal_chain": "\n".join(judgment.get("causal_chain", [])),
            "supporting_evidence": "\n".join(judgment.get("supporting_source_ids", [])),
            "opposing_evidence": "\n".join(judgment.get("uncertainties", [])),
            "falsification": "\n".join(judgment.get("down_triggers", [])),
            "recommended_action": "请在校准面板确认概率后记录为正式预测，到期后结算复盘。",
        }

    def _category_penalties(self):
        """Feedback-learning multipliers persisted by the learning consumer.

        连接失败原先静默 `return {}`（= 静默清零惩罚），现在留日志（带堆栈）。
        返回值仍是 dict：`_score` 里直接 `penalties.get(...)` 做算术，
        改成 None 会当场炸在评分路径上。见 `_category_penalties_from_connection`。
        """
        try:
            with self.database.connect() as connection:
                return _category_penalties_from_connection(connection)
        except Exception:
            _logger.warning(
                "打开数据库读取反馈学习惩罚系数失败，本轮按无惩罚处理", exc_info=True
            )
            return {}

    def map_judgment(self, cluster_id: str, judgment_id: str) -> list[dict]:
        cluster, judgment, provider = self._load(cluster_id, judgment_id)
        # v1.2 第三波：**分析不成立的研判不得产生候选预测**。
        #
        # 候选预测会进不可变账本并参与 Brier 校准 —— 不能建立在"其实没分析"的基础上。
        # 兜底出来的研判此前和正常分析长得一模一样，这条路是它进账本的入口。
        #
        # 只拦**显式**的 degraded / placeholder：
        #   - 历史行没有这个字段（`.get()` 得到 None）→ 不拦。
        #     不是漏判 —— 那些"兜底出来的"历史行其实**已经被第一波的证伪性闸拦住了**：
        #     它们的 observable_signals 是占位值「后续官方公告 / 执行进展通报」，
        #     而这两个词正在 _BANNED_PHRASES 里。两道闸互补，不是重复。
        #   - 若这里把历史行也一并拦掉，等于把所有既有事件的候选生成一起停掉 ——
        #     那是过度收紧，会把功能打死。
        if judgment.get("analysis_status") in ("degraded", "placeholder"):
            return []
        categories = tuple(_text_list(judgment.get("impact_categories")))
        penalties = self._category_penalties()
        now = _iso(self.now())
        # v1.5：方法论的结构化产物随研判落库，这里取出来接进定级（不新增 AI 字段）。
        # ③：远程研判的契约里没有 `power_structure`，缺了就用**本机同一个规则引擎**
        # 在读时补算并标明来源 —— 不补的话，用户花额度换来的远程升级版会被 L4
        # 结构闸**系统性降为 L3**（等于花钱买降级）。
        power_structure, structure_source = _resolve_power_structure(
            judgment,
            provider,
            cluster_title=cluster.get("title", ""),
            cluster_summary=cluster.get("summary", ""),
            entity_names=(
                self._cluster_entity_names(cluster_id)
                if _needs_structure_backfill(judgment, provider)
                else None
            ),
        )
        # 只改**内存副本**：让候选卡 / 量级 / 摘要看到的结构与定级用的是同一份，
        # 免得出现"分级按补算结果、卡片却按缺失渲染"的自相矛盾。
        # **绝不回写** `judgments.content_json`（研判不可变，原文就是原文）。
        if not isinstance(judgment.get("gyw"), dict):
            judgment["gyw"] = {}
        judgment["gyw"]["power_structure"] = power_structure
        results = []
        for interest in self.interest_service.list_objects():
            if interest["status"] != "active":
                continue
            # 定级只有这一个入口（`_evaluate_impact`）：存量回填走的是同一条，
            # 两条路因此不可能漂移。
            evaluated = _evaluate_impact(
                evidence_level=cluster["evidence_level"],
                categories=categories,
                interest=interest,
                judgment=judgment,
                penalties=penalties,
                power_structure=power_structure,
                structure_source=structure_source,
            )
            if evaluated is None:
                continue
            exposure = evaluated["exposure"]
            # 低暴露度事件对该利益影响微弱，不生成候选预测
            if exposure < 0.3:
                continue
            score = evaluated["score"]
            alert = evaluated["alert"]
            components = evaluated["components"]
            # v1.4（R-16）：**不再为 L1/L2 生成候选预测**。
            #
            # 真库实测：个人影响 118,379 条里 L2 占 108,535（92%），而首页最多显示
            # 3 条 —— 信噪比约 39,460 : 1。L1/L2 的候选**本来就不显示**，却仍然生成、
            # 仍然占库、仍然参与通知节流。停在这里生成，比事后清理便宜得多，
            # 也顺带把 R-01 的结算吞吐压力降下来（生成量直接少一个数量级）。
            #
            # 判断用**最终等级**（已含风险信号上调）：一条基础 L2 的候选若因命中
            # 「慷慨激昂」被上调到 L3，它仍然该被生成 —— 上调本来就是"这件事值得看"
            # 的信号，在这里截断会让上调规则再次变成装饰品。
            if alert not in ("L3", "L4"):
                continue
            with self.database.connect() as connection:
                existing = connection.execute(
                    """
                    SELECT impact_id,candidate_json FROM personal_impacts
                    WHERE cluster_id=? AND judgment_id=? AND interest_id=?
                    """,
                    (cluster_id, judgment_id, interest["object_id"]),
                ).fetchone()
                impact_id = existing["impact_id"] if existing else "P-" + uuid.uuid4().hex
                candidate = self._candidate(
                    cluster, judgment, interest, impact_id, alert_level=alert
                )
                if existing:
                    old_candidate = json.loads(existing["candidate_json"] or "{}")
                    if old_candidate.get("confirmed_forecast_id"):
                        candidate = old_candidate
                reason = (
                    f"事件类别{','.join(categories) or 'general'}映射到本地利益；"
                    f"证据{cluster['evidence_level']}，重要度{interest['importance']}/5"
                )
                connection.execute(
                    """
                    INSERT INTO personal_impacts(
                        impact_id,cluster_id,judgment_id,interest_id,impact_score,
                        alert_level,components_json,reason,candidate_json,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(cluster_id,judgment_id,interest_id) DO UPDATE SET
                        impact_score=excluded.impact_score,
                        alert_level=excluded.alert_level,
                        components_json=excluded.components_json,
                        reason=excluded.reason,
                        candidate_json=excluded.candidate_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        impact_id,
                        cluster_id,
                        judgment_id,
                        interest["object_id"],
                        score,
                        alert,
                        json.dumps(components, ensure_ascii=False, sort_keys=True),
                        reason,
                        json.dumps(candidate, ensure_ascii=False, sort_keys=True),
                        now,
                        now,
                    ),
                )
            # v1.1 第一波：L4 一律不再自动确认（原 P1 的 L4 自动确认块已删除）。
            # 高影响事件必须由用户在界面手动选概率确认（confirmed_by='user'）；
            # 只有 E2+ 的 L3 才由 auto_confirm_all 按证据等级自动确认。
            results.append(
                {
                    "impact_id": impact_id,
                    "cluster_id": cluster_id,
                    "judgment_id": judgment_id,
                    "interest_id": interest["object_id"],
                    "interest_name": interest["name"],
                    "impact_score": score,
                    "alert_level": alert,
                    "components": components,
                    "reason": reason,
                    "candidate": candidate,
                }
            )
        return sorted(results, key=lambda item: (-item["impact_score"], item["interest_id"]))

    def purge_garbage_forecasts(self) -> int:
        """把 v1.0 自动确认产生的垃圾 F-CAND 预测**置为 void**，重置为待确认状态。

        旧版本在 map_judgment 中自动将候选预测写入正式账本，产生大量未经验证的
        F-CAND 记录。本方法把它们作废（`status='void'`）、并清除对应 impact 的
        确认标记，让用户重新确认。返回作废的预测数量。

        ⚠ v1.4 的行为变更（R-12 的直接后果）：**从"删除父行"改成"置 void"**。
        原先这里 `DELETE FROM forecasts`，而 `forecast_versions` 受不可变触发器保护
        删不掉 —— 父行一删，子行就成了永久孤儿（真库实测 10,663 条，占该表 55%，
        且被触发器保护、无法清理）。R-12 给 `forecasts` 补上 `no_delete` 触发器之后，
        这条删除路径**已经不可能存在**；与其让它每次启动都抛 IntegrityError 被
        `except Exception` 静默吞掉，不如按账本本来的语义作废：状态从 'open' 流转到
        'void'（数据库注释里早就写明了有这个状态），子行不再失去父行，
        也不再产生新的孤儿。

        审计说明：本方法会改写不可变账本，属于**受控例外**，因此每轮都写一条
        `audit_log`（`action='forecast.purge_garbage'`），如实记录作废了多少条、
        清了多少 impact 的确认标记。存量孤儿（R-13）是否清理是产品决策，本方法不碰。

        可观测性（2026-09-21 补）：清除确认标记的单条失败**不再 `pass`**，而是
        计入 `failed_impacts`（同时进审计 `details_json`）并
        `_logger.warning(..., exc_info=True)`。失败 = 那条影响仍留着指向已作废
        预测的 `confirmed_forecast_id`（孤儿引用），必须看得见。单条失败不打断
        整轮作废 —— 与原先"继续跑"的控制流一致，只是不再无声。
        """
        voided = 0
        cleared_impacts = 0
        #: 清除确认标记失败的条数。**不再静默**：失败意味着那条 impact 会留着
        #: 指向"已作废预测"的 `confirmed_forecast_id`，页面上就成了一个有效关联
        #: 指向一条 void 预测（即"孤儿引用"）。必须能看见，才能知道要补跑/排查。
        failed_impacts = 0
        with self.database.connect() as connection:
            garbage = connection.execute(
                "SELECT forecast_id FROM forecasts WHERE forecast_id LIKE 'F-CAND-%'"
                " AND status != 'void'"
            ).fetchall()
            for row in garbage:
                fid = row["forecast_id"]
                # 找出引用此forecast的impact，清除confirmed标记
                impact_rows = connection.execute(
                    "SELECT impact_id, candidate_json FROM personal_impacts "
                    "WHERE candidate_json LIKE ?",
                    (f'%"confirmed_forecast_id": "{fid}"%',),
                ).fetchall()
                for irow in impact_rows:
                    try:
                        cj = json.loads(irow["candidate_json"] or "{}")
                        cj.pop("confirmed_forecast_id", None)
                        cj.pop("confirmed_probability", None)
                        connection.execute(
                            "UPDATE personal_impacts SET candidate_json=? WHERE impact_id=?",
                            (json.dumps(cj, ensure_ascii=False, sort_keys=True), irow["impact_id"]),
                        )
                        cleared_impacts += 1
                    except Exception:
                        # 原先这里是裸 `pass` —— 一旦 JSON 损坏或写库失败，这条影响
                        # 就永久留着指向已 void 预测的确认标记，且**完全无痕**。
                        # 现在计入失败数并留日志（带堆栈）；流程照旧继续，不因单条
                        # 坏数据打断整轮作废。
                        failed_impacts += 1
                        _logger.warning(
                            "清除个人影响的预测确认标记失败，该影响仍指向已作废预测"
                            " impact_id=%s forecast_id=%s",
                            irow["impact_id"],
                            fid,
                            exc_info=True,
                        )
                connection.execute(
                    "UPDATE forecasts SET status='void' WHERE forecast_id=?", (fid,)
                )
                voided += 1
            if voided:
                connection.execute(
                    "INSERT INTO audit_log(occurred_at, action, object_type, object_id,"
                    " details_json) VALUES (?, ?, ?, ?, ?)",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        "forecast.purge_garbage",
                        "forecast",
                        None,
                        json.dumps(
                            {
                                "voided_forecasts": voided,
                                "cleared_impacts": cleared_impacts,
                                "failed_impacts": failed_impacts,
                                "note": (
                                    "v1.4 起不再删除父行（forecasts 已加 no_delete 触发器）。"
                                    "删除父行正是历史上 55% 孤儿子行的来源；改为置 void，"
                                    "账本行与子行都留着，状态如实变成'已作废'。"
                                ),
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
            connection.commit()
        return voided

    def candidate_forecast(self, impact_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT candidate_json FROM personal_impacts WHERE impact_id=?",
                (impact_id,),
            ).fetchone()
        if row is None:
            raise KeyError(impact_id)
        return json.loads(row["candidate_json"])

    def _impact_category(self, impact_id: str) -> str:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT i.category FROM personal_impacts p
                JOIN interest_objects i ON i.object_id = p.interest_id
                WHERE p.impact_id = ?
                """,
                (impact_id,),
            ).fetchone()
        return (row["category"] if row else "") or "general"

    def pending_candidates(self, limit: int = 20) -> list[dict]:
        """Unconfirmed impact candidates for the calibration panel."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.impact_id, p.candidate_json, p.updated_at,
                       p.cluster_id, p.judgment_id, i.category
                FROM personal_impacts p
                JOIN interest_objects i ON i.object_id = p.interest_id
                WHERE (p.muted_until IS NULL OR p.muted_until < ?)
                    AND p.candidate_json IS NOT NULL AND p.candidate_json != ''
                    AND p.candidate_json NOT LIKE '%"confirmed_forecast_id":%'
                    AND p.alert_level IN ('L3','L4')
                ORDER BY p.impact_score DESC, p.updated_at DESC
                LIMIT ?
                """,
                (_iso(self.now()), max(1, min(int(limit), 100))),
            ).fetchall()
            # Pre-fetch judgment content for all candidates in one query so
            # we can surface the GYW framework fields the provider generated.
            judgment_ids = [row["judgment_id"] for row in rows if row["judgment_id"]]
            judgments_by_id: dict[str, dict] = {}
            judgment_providers: dict[str, str] = {}
            if judgment_ids:
                placeholders = ",".join("?" for _ in judgment_ids)
                judgment_rows = connection.execute(
                    f"SELECT judgment_id, content_json, provider FROM judgments WHERE judgment_id IN ({placeholders})",
                    judgment_ids,
                ).fetchall()
                for jrow in judgment_rows:
                    try:
                        content = json.loads(jrow["content_json"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        content = {}
                    judgments_by_id[jrow["judgment_id"]] = content
                    judgment_providers[jrow["judgment_id"]] = str(jrow["provider"] or "local")
        output = []
        for row in rows:
            candidate = json.loads(row["candidate_json"] or "{}")
            # 这道闸在界面上也要能被看见（R-08）：一条"停在待补充"的候选如果只是
            # 点了确认才弹错，用户会以为是 bug。把闸的结论和原因一起透出，
            # 界面上直接标「可观测信号未特化，需人工补充」。
            settleable, settle_reason = _settlement_ready(candidate)
            judgment_content = judgments_by_id.get(row["judgment_id"], {})
            judgment_provider = judgment_providers.get(row["judgment_id"], "local")
            gyw = judgment_content.get("gyw") or {}
            # Backfill for legacy judgments that pre-date the GYW schema.
            # Do not write back to disk — keep history immutable; the home
            # page just needs the analysis rendered today.
            if not gyw or not all(
                gyw.get(field) for field in (
                    "stakeholders",
                    "constraints",
                    "least_resistance_path",
                    "counter_evidence",
                    "leading_indicators",
                )
            ):
                gyw = _backfill_gyw(row["category"])
                gyw_source = "legacy-backfill"
            else:
                gyw_source = "judgment"
            output.append(
                {
                    "id": row["impact_id"],
                    "statement": candidate.get("title", ""),
                    "summary": candidate.get("title", ""),
                    "category": row["category"] or "general",
                    "window_end": candidate.get("window_end", ""),
                    "cluster_id": row["cluster_id"],
                    "judgment_id": row["judgment_id"],
                    "gyw": gyw,
                    "gyw_source": gyw_source,
                    "judgment_provider": judgment_provider,
                    "fact_summary": judgment_content.get("fact_summary", ""),
                    "actors": judgment_content.get("actors", []),
                    "causal_chain": judgment_content.get("causal_chain", []),
                    "confirmed": bool(candidate.get("confirmed_forecast_id")),
                    "confirmed_probability": candidate.get("confirmed_probability"),
                    # 证伪性闸的结论（R-08）：不满足时候选停在"待补充"，
                    # 界面必须照实标注原因，而不是等用户点了确认才弹错。
                    "settleable": settleable,
                    "settle_block_reason": settle_reason,
                    "observable_signals": candidate.get("observable_signals", ""),
                    "main_signal": candidate.get("main_signal", ""),
                }
            )
        return output

    def confirm_candidate(self, impact_id: str, probability: float, by: str = "user") -> dict:
        """确认一条候选预测并写入不可变账本。

        `by` 记录这条预测是怎么进来的，落库到 `forecasts.confirmed_by`：
          - 'user'：本人在界面上选的概率（默认值，因为这是界面调用入口）
          - 'auto'：auto_confirm_all 按 E2+ 多源证据自动确认
        账本是不可变的，所以**必须能一眼分出哪些是人选的、哪些是机器填的** ——
        否则校准评分（Brier）里混着人类从未做过的预测，分数就没有意义。
        """
        if by not in ("user", "auto"):
            raise ValueError("确认来源只能是 user 或 auto")
        try:
            probability = round(float(probability), 2)
        except (TypeError, ValueError):
            raise ValueError("概率必须选择固定档位") from None
        if probability not in ALLOWED_PROBABILITIES:
            raise ValueError("概率必须选择固定档位")
        candidate = self.candidate_forecast(impact_id)
        if candidate.get("confirmed_forecast_id"):
            return self.forecast_service.get_forecast(candidate["confirmed_forecast_id"])
        # 证伪性闸：命题不具结算性（缺可观测条件 / 信号未事件级特化 / 主信号已是
        # 既成事实 / 含虚词）则不允许进不可变账本，停在待补充状态由用户补全。
        ready, reason = _settlement_ready(candidate)
        if not ready:
            raise ValueError(
                f"命题不具备结算性，无法记入账本：{reason}（请补齐可观测条件，"
                f"并让信号指向本事件的具体机构/地点/数字）"
            )
        # 使用正常的F-前缀格式，不传入forecast_id让create_forecast自动生成
        result = self.forecast_service.create_forecast(
            {
                "title": candidate["title"],
                "category": self._impact_category(impact_id),
                "resolution_criteria": candidate["resolution_criteria"],
                "window_start": candidate["window_start"],
                "window_end": candidate["window_end"],
                "probability": probability,
                "confidence": "medium",
                "alert_level": "L3",
                "model_version": "v0.5",
                "privacy_level": "P3",
                "causal_chain": candidate["causal_chain"],
                "supporting_evidence": candidate["supporting_evidence"],
                "opposing_evidence": candidate["opposing_evidence"],
                "falsification": candidate["falsification"],
                "recommended_action": candidate["recommended_action"],
                "confirmed_by": by,
                # ⚠ 以下五项此前**一个都没传**。后果是：证伪性闸在入账那一刻
                # 用 observable_signals 判过命题是否可结算，而**落进账本时它被丢了**
                # —— 到期核对时账本里没有"阈值或可观测事实"这一要素，命题实际不可核。
                # base_rate / base_rate_sample 同理：第二波加了字段，却从未落过库。
                # 这是同一个"字段静默丢失"病的第 4 次发作（前三次：confirmed_by、
                # observable_signals、base_rate）。**加字段时先查它有没有进这条链。**
                "observable_signals": candidate.get("observable_signals", ""),
                "base_rate": candidate.get("base_rate"),
                "base_rate_sample": candidate.get("base_rate_sample", 0),
                # v1.4：同类别**全部**二元样本数（人工 + 非人工）。界面要写
                # 「本类别样本 N 条，其中人工 M 条」，只有 M 的话说不清"差在哪"。
                "base_rate_sample_total": (
                    candidate.get("base_rate_composition") or {}
                ).get("total", 0),
                "magnitude": candidate.get("magnitude_line", ""),
                "risk_signal_keywords": "、".join(
                    _text_list(candidate.get("risk_signal_hit"))
                ),
                "alert_level": candidate.get("alert_level") or "L3",
            }
        )
        candidate["confirmed_forecast_id"] = result["forecast_id"]
        candidate["confirmed_probability"] = probability
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE personal_impacts SET candidate_json=?,updated_at=? WHERE impact_id=?",
                (json.dumps(candidate, ensure_ascii=False, sort_keys=True), _iso(self.now()), impact_id),
            )
        return result


    def auto_confirm_all(self) -> dict:
        """自动确认未确认的 L3 候选预测（L4 一律交由用户在界面手动确认）。

        **证据等级闸（v1.1 补）**：只对 **E2 及以上**（多来源互证）自动确认。
        E1 是单来源线索，无论多重要都**不得**自动进入不可变账本 —— 必须由本人
        选择概率后手动确认。
        **证伪性闸**：命题不具结算性的候选同样不自动进账本，停在待补充。

        为什么必须是这一层闸：v1.0 曾用"把 E1 的告警级别从 L4 降到 L3"来阻止它
        进账本，但本函数的筛选条件**只看 alert_level、不看证据等级**，于是降级后的
        E1 照样被自动确认 —— **改档位不等于加闸**，那次修复实际没有生效。

        证据等级用白名单（E2/E3/E4）而不是黑名单排除 E1：关联不到事件簇、
        或 evidence_level 为空的行，同样不该自动写入不可变账本。
        """
        confirmed = 0
        skipped = 0
        errors = []
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.impact_id, p.candidate_json, p.alert_level
                FROM personal_impacts p
                JOIN event_clusters c ON c.cluster_id = p.cluster_id
                WHERE p.candidate_json IS NOT NULL AND p.candidate_json != ''
                  AND p.alert_level IN ('L3')
                  AND p.user_label NOT IN ('false_positive','dismissed')
                  AND c.evidence_level IN ('E2','E3','E4')
                """
            ).fetchall()
        for row in rows:
            try:
                candidate = json.loads(row["candidate_json"] or "{}")
                if candidate.get("confirmed_forecast_id"):
                    skipped += 1
                    continue
                # 证伪性闸：命题不具结算性的候选不自动进账本。
                ready, reason = _settlement_ready(candidate)
                if not ready:
                    skipped += 1
                    errors.append(f"{row['impact_id']}: 命题未过结算性闸——{reason}")
                    continue
                low = float(candidate.get("probability_low", 0.3))
                high = float(candidate.get("probability_high", 0.7))
                probability = _nearest_probability((low + high) / 2)
                self.confirm_candidate(row["impact_id"], probability, by="auto")
                confirmed += 1
            except Exception as exc:
                errors.append(f"{row['impact_id']}: {exc}")
                skipped += 1
        return {"confirmed": confirmed, "skipped": skipped, "errors": errors[:10], "total": len(rows)}
