"""Offline heuristic judgment provider; never sees personal interests."""

from __future__ import annotations

import re

from .judgment_models import EvidenceBundle, JudgmentResult
from .judgment_validation import validate_judgment
from .knowledge_base import (
    MAX_LEADING_BOOST,
    analyze_power_structure,
    detect_leading_indicators,
    detect_risk_signals,
    generate_scenario_paths,
    total_leading_boost,
)


class LocalHeuristicProvider:
    """A conservative offline fallback; it never sees personal interests.

    v2 升级（稿D）：从「按类别查表的固定模板」升级为「基于证据内容的动态推演骨架」。
    通过实体提取、事件分类、规则映射，让每条研判的 GYW 内容因证据不同而不同。
    远程 AI 可用时仍由远程覆盖；本地只在远程未启用/不达标/失败时兜底。
    所有本地推断的主体均带 [推断] 前缀且 evidence_refs 为空，诚实标注非 AI 推演。
    """

    name = "local"

    # ── 事件类型关键词表（用于从证据文本中分类事件） ──────────
    _EVENT_KEYWORDS = {
        "policy": ("政策", "新规", "条例", "通知", "意见", "办法", "规定", "印发", "部署", "细则", "方案", "纲要", "规划"),
        "data_release": ("数据", "统计", "公布", "同比", "环比", "增长", "下降", "CPI", "PPI", "GDP", "LPR", "利率", "社融", "M2", "PMI", "进出口", "外汇储备"),
        "personnel": ("任命", "免去", "辞职", "当选", "换届", "人事", "出任", "接任", "卸任"),
        "accident": ("事故", "灾难", "爆炸", "坍塌", "泄漏", "疫情", "伤亡", "地震", "洪水", "火灾", "坠机", "沉船"),
        "corporate": ("上市", "融资", "收购", "重组", "裁员", "财报", "营收", "利润", "破产", "退市", "IPO"),
        "international": ("外交", "制裁", "关税", "谈判", "峰会", "双边", "多边", "访华", "出访", "联合国", "WTO"),
        "market": ("股市", "楼市", "汇市", "债市", "黄金", "原油", "大宗商品", "A股", "港股", "美股", "纳斯达克", "上证指数"),
        "monetary": ("央行", "降息", "加息", "降准", "MLF", "逆回购", "流动性", "货币政策", "公开市场"),
        "fiscal": ("财政", "税收", "预算", "赤字", "国债", "地方债", "专项债", "转移支付"),
    }

    # ── 常见机构简称（精确匹配，弥补后缀正则无法覆盖的简称） ──
    _COMMON_INSTITUTIONS = (
        "央行", "美联储", "国务院", "发改委", "财政部", "证监会", "银保监会",
        "工信部", "商务部", "住建部", "农业部", "卫健委", "教育部", "科技部",
        "公安部", "司法部", "人社部", "自然资源部", "生态环境部", "交通运输部",
        "水利部", "文化和旅游部", "退役军人事务部", "应急管理部", "审计署",
        "国资委", "海关总署", "税务总局", "市场监管总局", "统计局", "林业局",
        "IMF", "世界银行", "WTO", "WHO", "联合国", "欧央行", "日本央行",
    )

    # ── 机构后缀（用于从文本中正则提取机构名） ────────────────
    _INSTITUTION_SUFFIXES = (
        "银行", "委员会", "管委会", "管理局", "监管局", "总局", "总署",
        "交易所", "公司", "集团", "控股", "协会", "学会", "基金会",
        "政府", "国务院", "人大", "政协", "法院", "检察院",
    )

    # ── 历史事件映射表（关键词 → (事件名, 相似点, 不同点)）──
    # v1.3 三处修正（审计七步#7）：
    #   a) **去重**：原表有三对完全重复的条目（裁员/失业、房地产、人民币汇率各两次），
    #      重复条目会因"首个命中即返回"而把另一条永远挡在后面。
    #   b) **改为打分匹配**：原先是 for-首个命中-即返回，而关键词大量交叠
    #      （"降息"同时出现在第 1、2、15 条），结果第 15 条（美联储）永远到不了。
    #      现在按 (命中条数, 关键词总长) 取最佳匹配。
    #   c) **补本地领域**：原表全部是宏观金融，与本人实际用途（地方水务/招投标/
    #      产业园区/督察）几乎不重叠，于是绝大多数条目拿到 None。
    # 另：所有条目都是**手写的模板类比，不是检索结果**。返回时会显式标注这一点，
    # 不允许它冒充"查到的历史"。
    _HISTORICAL_PARALLELS = [
        (("LPR", "中期借贷便利", "MLF", "逆回购", "公开市场操作", "利率走廊"),
         "2024年LPR下调与5年期以上利率一次性降25bp",
         "同样是货币政策宽松周期中的利率调整，市场关注对房贷和企业融资成本的传导",
         "当前经济周期位置、房地产市场温度和外部汇率约束与2024年不同"),
        (("降准", "存款准备金率"), "2023年两次降准共释放长期资金超万亿",
         "同样通过释放银行体系流动性支持实体经济，信号意义大于实际规模",
         "当前银行净息差压力和地方债务化解需求更为突出"),
        (("美联储", "联邦基金利率", "鲍威尔"), "2022年美联储激进加息周期",
         "同样是美联储货币政策转向对全球资本流动和新兴市场的冲击",
         "当前通胀位置、美国经济韧性和各国货币政策空间不同"),
        (("房地产", "楼市", "房价", "限购", "房贷"), "2014-2015年房地产去库存周期",
         "同样面临库存高企、销售低迷和政策转向宽松的组合，政策从紧缩转向刺激",
         "当前人口结构、城镇化率和居民杠杆率与2014年有本质差异"),
        (("地方债", "专项债", "化债"), "2015年地方政府债务置换",
         "同样是通过债务重组缓解地方财政压力，核心是期限置换和成本下降",
         "当前债务规模更大、涉及面更广，且叠加房地产土地出让收入下滑"),
        (("CPI", "通胀", "通缩", "物价"), "2012-2013年CPI低位徘徊期",
         "同样面临需求不足导致的物价低迷，政策关注点从防通胀转向稳增长",
         "当前外部环境、地产周期和人口结构与2012年不同"),
        (("PMI", "制造业景气", "工业增加值"), "2018-2019年制造业PMI持续低于荣枯线",
         "同样是外需走弱叠加内部转型压力，制造业景气度承压",
         "当前产业链位置和新能源等新动能占比与2018年不同"),
        (("关税", "贸易战", "贸易摩擦"), "2018-2019年中美贸易摩擦",
         "同样是大国博弈在贸易领域的具体化，关税手段反复升级",
         "当前全球供应链重构程度和双方依赖度已发生变化"),
        (("制裁", "出口管制", "实体清单"), "2022年半导体出口管制升级",
         "同样是通过技术管制遏制对手产业升级，影响全球供应链",
         "当前受影响领域和反制手段可能不同"),
        (("人民币", "汇率", "贬值", "升值", "外汇"),
         "2015年811汇改",
         "同样面临汇率波动与资本流动管理的平衡，市场预期管理是关键",
         "当前外汇储备充足度和资本项目开放程度与2015年不同"),
        (("社融", "信贷", "贷款"), "2022年社融增速持续下行",
         "同样是有效需求不足导致的信贷疲软，政策试图宽货币向宽信用传导",
         "当前房地产和地方政府融资约束与2022年不同"),
        (("裁员", "就业", "失业", "失业率"), "2022年互联网行业裁员潮",
         "同样是行业调整期的就业压力，传导至消费和社会预期",
         "当前涉及行业范围和政策托底力度可能不同"),
        (("新能源", "光伏", "电动车", "动力电池"), "2018年光伏531政策",
         "同样是新兴产业在快速扩张后面临政策调整和产能过剩压力",
         "当前产业成熟度、全球市场份额和技术迭代速度与2018年不同"),
        (("疫情", "公共卫生", "传染病"), "2020年初新冠疫情爆发",
         "同样是突发公共卫生事件对经济和社会运行的冲击",
         "当前病毒特性、防控经验和医疗资源准备与2020年不同"),
        (("IPO", "注册制", "上市辅导"), "2019年科创板设立与注册制试点",
         "同样是资本市场制度改革，影响企业融资渠道和市场估值体系",
         "当前市场环境、投资者结构和退市机制完善程度不同"),
        (("基建", "基础设施", "重大工程", "重点项目"), "2008年四万亿刺激计划",
         "同样是通过基建投资拉动总需求，短期见效快但长期影响债务结构",
         "当前地方债务负担、产能过剩程度和政策空间与2008年不同"),
        (("消费", "内需", "社会消费品零售", "以旧换新"),
         "2009年家电下乡与汽车购置税减免",
         "同样是通过财政补贴刺激消费，短期拉动效果明显但退出后可能回落",
         "当前居民收入预期、消费倾向和政策工具与2009年不同"),
        (("能源", "原油", "石油", "天然气", "电价"), "2022年欧洲能源危机",
         "同样是能源价格剧烈波动对通胀和产业链的冲击",
         "当前能源结构、战略储备和地缘政治格局不同"),
        (("粮食", "耕地", "种业", "农产品"), "2007-2008年全球粮食危机",
         "同样是粮食价格上涨对通胀和社会稳定的压力",
         "当前粮食储备、自给率和国际供应链环境不同"),
        (("老龄化", "生育", "出生人口", "人口结构"), "2016年全面二孩政策",
         "同样是人口政策调整试图逆转长期趋势，短期效果有限",
         "当前生育意愿、养育成本和社会观念与2016年不同"),
        (("芯片", "半导体", "人工智能", "算力"), "2018年中兴事件与半导体管制升级",
         "同样是技术管制推动国产替代和产业链重构",
         "当前技术成熟度、国内市场规模和反制能力与2018年不同"),
        (("碳达峰", "碳中和", "减排", "能耗双控"), "2021年运动式减碳与限电",
         "同样是环保政策执行中出现一刀切和运动式推进，引发短期冲击",
         "当前政策执行精细化程度和能源保供能力不同"),
        # ── 以下为本人的实际用途领域（v1.3 补，此前完全无覆盖）──
        (("水务", "供水", "污水处理", "自来水", "水厂", "管网"),
         "2015年《水污染防治行动计划》（水十条）后的污水处理提标改造周期",
         "同样是自上而下的水质目标推动地方集中上项目、改工艺、补管网",
         "当前地方财政状况、水价调整空间和支付能力与2015年不同"),
        (("水利", "引水", "水库", "防洪", "堤防", "灌区"),
         "2014年确定的172项节水供水重大水利工程集中开工期",
         "同样是水利投资作为稳增长抓手，项目从立项到开工有明确节点",
         "当前地方配套资金到位率、征地节奏和专项债额度与当年不同"),
        (("招标", "投标", "中标", "采购公告", "政府采购", "公开招标"),
         "2000年《招标投标法》施行后的工程建设招投标体系建立期",
         "同样是从「关系定标」转向「程序定标」，流程刚性和留痕要求显著上升",
         "当前电子招投标、评定分离改革和监管强度与当年不同"),
        (("产业园", "开发区", "工业园区", "园区"),
         "2000年代各地开发区、工业园区的集中建设期",
         "同样是地方以园区为载体重资产招商，先基建后招商、以地换项目",
         "当前土地指标、债务约束和招商竞争格局与当年不同"),
        (("乡村振兴", "帮扶", "脱贫", "农村人居环境"),
         "2015-2020年脱贫攻坚期的项目集中下达",
         "同样是自上而下压任务、限期完成、以考核推动地方落地",
         "当前考核力度、资金来源和基层执行能力与攻坚期不同"),
        (("督察", "巡视", "整改", "问责"), "2016年起中央环境保护督察常态化",
         "同样是督察组进驻、限期整改、地方集中关停与补手续",
         "当前督察频次、整改标准和地方承受能力与首轮不同"),
    ]

    # ── 事件类型 → 典型阻力方 ──────────────────────────────────
    _TYPICAL_RESISTANCE = {
        "policy": ("执行部门", "被监管对象", "利益受损方", "地方财政"),
        "data_release": ("市场预期差", "数据修正风险", "季节性扰动"),
        "personnel": ("交接磨合", "政策连续性", "内部博弈"),
        "accident": ("救援难度", "信息透明", "责任认定", "次生灾害"),
        "corporate": ("整合难度", "监管审查", "股东分歧", "债务负担"),
        "international": ("国内政治", "利益集团", "执行落差", "第三方反应"),
        "market": ("获利盘了结", "政策转向", "外部冲击", "流动性收紧"),
        "monetary": ("通胀反弹", "汇率压力", "银行净息差", "资产泡沫"),
        "fiscal": ("财政空间", "地方配套能力", "资金使用效率", "债务可持续性"),
    }

    # ── 事件类型 → 典型群体心理（对应方法论 4.2 人性弱点） ────
    _TYPICAL_PSYCHOLOGY = {
        "policy": "政策落地前观望情绪浓，落地后先试探再跟进；执行层怕担责而偏保守，受益方急于抢跑，受损方等待细则寻找缓冲空间",
        "data_release": "数据超预期时市场短期亢奋但持续性取决于后续验证，低于预期时恐慌容易过度反应；投资者倾向于用单月数据外推趋势，忽视季节性和基数效应",
        "personnel": "人事变动初期市场观望，新政方向明确前各方按兵不动；新任决策者倾向于先稳后调，避免初期激进引发反弹",
        "accident": "事故初期信息混乱引发恐慌，随信息透明情绪逐步修复；同类企业因恐惧被连带审查而主动自查，行业短期收缩",
        "corporate": "并购消息出来后标的方股东惜售，收购方股价因整合不确定性承压；裁员消息引发行业内就业焦虑，消费者对品牌信心下降",
        "international": "博弈升级期市场避险情绪上升，谈判取得进展时风险偏好快速修复；双方都倾向于在谈判前展示强硬姿态，实际让步留到最后时刻",
        "market": "上涨时赚钱效应吸引跟风资金，下跌时恐慌踩踏放大跌幅；散户在顶部最乐观、底部最悲观，机构在关键位置博弈政策预期",
        "monetary": "宽松预期升温时资产价格提前反应，政策落地后出现买预期卖事实；企业和居民在降息初期仍观望，信心修复滞后于利率下降",
        "fiscal": "财政发力初期市场期待高，执行进度低于预期时失望情绪放大；地方政府在债务约束下倾向于保守支出，中央项目落地快于地方",
    }

    @staticmethod
    def _extract_institutions(text: str) -> list[str]:
        """从文本中提取机构名：先精确匹配常见简称，再用后缀正则匹配。
        过滤明显的误匹配（句子片段、含动词的短语）。"""
        institutions: list[str] = []
        # 常见误匹配特征：以单字动词开头，或包含明显的双字动词
        _bad_prefixes = ("达", "有", "是", "在", "和", "与", "或", "但", "如", "因", "所", "被", "把", "让", "使", "向", "从", "到", "对", "为", "以", "按", "沿", "经", "凭", "沿", "替", "跟", "比", "除", "顺", "照")
        _bad_substrings = ("授权", "发布", "宣布", "表示", "称", "的", "了", "在", "是", "有", "和", "与", "或")

        def _is_valid(name: str) -> bool:
            if len(name) > 15:
                return False
            if any(name.startswith(p) for p in _bad_prefixes):
                return False
            if any(s in name for s in _bad_substrings):
                return False
            return True

        # 1. 常见机构简称精确匹配
        for name in LocalHeuristicProvider._COMMON_INSTITUTIONS:
            if name in text and name not in institutions:
                institutions.append(name)
        # 2. 后缀正则匹配（2-10 个前缀字符 + 机构后缀）
        for suffix in LocalHeuristicProvider._INSTITUTION_SUFFIXES:
            pattern = r'([\u4e00-\u9fa5A-Za-z0-9]{2,10}' + re.escape(suffix) + r')'
            for match in re.finditer(pattern, text):
                name = match.group(1)
                if name not in institutions and _is_valid(name):
                    institutions.append(name)
        return institutions[:5]

    @staticmethod
    def _extract_numbers(text: str) -> list[str]:
        """提取关键数字（百分比、金额、基点、同比环比变化）。"""
        patterns = [
            r'\d+(?:\.\d+)?%',
            r'\d+(?:\.\d+)?\s*(?:亿元|万亿|万元|元)',
            r'\d+(?:\.\d+)?\s*(?:个百分点|bp|BP)',
            r'(?:同比|环比)\s*(?:增长|下降|上涨|下跌)\s*\d+(?:\.\d+)?%',
        ]
        numbers: list[str] = []
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                value = match.group(0)
                if value not in numbers:
                    numbers.append(value)
        return numbers[:4]

    @staticmethod
    def _classify_event(text: str) -> str:
        """基于关键词对事件分类，返回得分最高的事件类型。"""
        scores: dict[str, int] = {}
        for event_type, keywords in LocalHeuristicProvider._EVENT_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in text)
            if score > 0:
                scores[event_type] = score
        if not scores:
            return "general"
        return max(scores, key=scores.get)

    def _build_stakeholders(self, event_type: str, institutions: list[str], text: str) -> str:
        """动态生成 stakeholders：推动方/阻力方/力量对比/群体心理预判。"""
        pusher = institutions[0] if institutions else "事件发起方"
        resistance = self._TYPICAL_RESISTANCE.get(event_type, ("执行部门", "资源约束", "外部不确定"))
        resistance_str = "、".join(resistance[:3])

        # 力量对比：基于推动方级别
        # v1.3：原 markers 含裸"国家"，且用单字"省"/"市"做子串匹配 ——
        # 而"国家统计局/国家重点"到处都是、"市场/城市"都含"市"。
        # 改成只认明确机构全称。
        central_markers = (
            "国务院", "中共中央", "中央办公厅", "中央政治局", "全国人大",
            "全国政协", "国资委",
        )
        if any(marker in pusher for marker in central_markers):
            balance = f"{pusher}处于强势主导地位，政策自上而下推进；阻力方分散且缺乏否决能力，但执行层的变通和拖延可能削弱实际效果"
        elif re.search(r"省|市|自治区|自治州|县|区|旗", pusher) and re.search(
            r"政府|管委会|局|厅|委|办公室|办|部|署", pusher
        ):
            balance = f"{pusher}在辖区内有执行力，但需上级政策配套和财政支持；跨区域协调能力有限"
        elif any(marker in pusher for marker in ("公司", "集团", "企业")):
            balance = f"{pusher}作为市场主体有商业决策自主权，但受监管、市场竞争和股东约束"
        else:
            balance = f"{pusher}有一定推动力，但需多方协调；最终走向取决于各方博弈结果"

        psych = self._TYPICAL_PSYCHOLOGY.get(event_type, "各方在信息不完整时倾向于观望，等待明确信号后再行动")
        return f"【推动方】{pusher}；【阻力方】{resistance_str}；【力量对比】{balance}；【群体心理预判】{psych}"

    def _build_beneficiaries(self, event_type: str, institutions: list[str]) -> list[dict]:
        """基于事件类型和提取的机构生成获利方（全部标[推断]，无引用）。"""
        inferred: list[dict] = []
        if institutions:
            inferred.append({
                "subject": f"[推断]{institutions[0]}",
                "gain": "作为事件发起方或直接关联方，可能在政策执行或市场变化中获得先发优势",
                "evidence_refs": [],
            })
        type_map = {
            "policy": ("[推断]政策直接受益行业", "获得政策支持的市场主体可能在准入、补贴、税收等方面获得优势"),
            "monetary": ("[推断]银行体系与高杠杆主体", "流动性宽松降低融资成本，对负债端敏感的主体有利"),
            "fiscal": ("[推断]财政资金投向领域", "获得财政支持的项目和行业现金流改善"),
            "market": ("[推断]提前布局的机构投资者", "在趋势形成前介入的资金享受估值修复"),
            "data_release": ("[推断]数据利好的相关板块", "数据超预期时受益行业短期获得资金青睐"),
            "corporate": ("[推断]并购方或行业整合者", "通过整合提升市场份额和议价能力"),
            "international": ("[推断]谈判中占据主动的一方", "在博弈中获得更有利条款的主体"),
        }
        if event_type in type_map:
            subj, gain = type_map[event_type]
            if not any(b["subject"] == subj for b in inferred):
                inferred.append({"subject": subj, "gain": gain, "evidence_refs": []})
        return inferred[:3]

    def _build_cost_bearers(self, event_type: str, institutions: list[str]) -> list[dict]:
        """基于事件类型生成成本承担方。"""
        type_map = {
            "policy": ("[推断]政策约束的行业与群体", "监管收紧或资源重新分配使部分主体承担合规成本或利益损失"),
            "monetary": ("[推断]存款人与固定收益投资者", "利率下行使存款收益和固定收益资产回报率下降"),
            "fiscal": ("[推断]未来纳税人与财政空间", "债务扩张将偿债压力转移至未来财政"),
            "market": ("[推断]追高入场的散户投资者", "在趋势末端介入的资金面临回调风险"),
            "accident": ("[推断]事故责任方与受影响民众", "直接承担人员伤亡、财产损失和环境修复成本"),
            "corporate": ("[推断]被收购方原有股东与员工", "整合过程中可能面临岗位调整和文化冲突"),
            "international": ("[推断]博弈中处于弱势的一方", "在谈判中被迫接受不利条款"),
            "data_release": ("[推断]数据不及预期的相关行业从业者", "数据走弱可能影响行业景气度和就业预期"),
            "personnel": ("[推断]政策连续性受影响的执行层", "人事变动可能导致原有工作重点和资源分配调整"),
        }
        result: list[dict] = []
        if event_type in type_map:
            subj, cost = type_map[event_type]
            result.append({"subject": subj, "cost": cost, "evidence_refs": []})
        if len(institutions) > 1:
            result.append({
                "subject": f"[推断]{institutions[1]}",
                "cost": "作为事件关联方，可能在政策变化或市场波动中承担间接成本",
                "evidence_refs": [],
            })
        return result[:3]

    def _build_causal_chain(self, event_type: str, institutions: list[str], has_numbers: bool) -> list[str]:
        """按事件类型生成 4-6 步因果链。"""
        pusher = institutions[0] if institutions else "事件发起方"
        chains = {
            "policy": [
                f"{pusher}发布政策文件",
                "执行部门制定配套细则并部署落实",
                "市场主体调整行为以适应新规则",
                "成本与收益在相关主体间重新分配",
                "政策效果在后续数据中逐步显现",
            ],
            "data_release": [
                "统计部门公布经济数据",
                "市场将数据与预期对比形成预期差",
                "资产价格根据预期差快速调整",
                "政策制定者根据数据评估后续政策方向",
                "后续月份数据验证或修正当前判断",
            ],
            "monetary": [
                f"{pusher}调整货币政策工具",
                "银行体系流动性和资金利率发生变化",
                "信贷条件和融资成本传导至实体经济",
                "企业和居民调整投资与消费行为",
                "总需求和物价水平逐步响应",
            ],
            "fiscal": [
                "财政政策调整支出规模或结构",
                "资金通过转移支付或项目审批下达",
                "相关领域获得资金支持并启动项目",
                "上下游产业需求被拉动",
                "财政乘数效应在后续季度体现",
            ],
            "personnel": [
                "人事变动正式公布",
                "新任决策者熟悉情况并组建团队",
                "政策方向在初期讲话和文件中逐步明确",
                "执行层根据新政方向调整工作重点",
                "政策连续性与变革力度在后续行动中验证",
            ],
            "accident": [
                "突发事件发生并造成初始影响",
                "救援与应急响应启动",
                "信息逐步公开，公众情绪从恐慌转向关注",
                "责任认定与整改措施启动",
                "同类行业和地区开展排查，监管可能收紧",
            ],
            "corporate": [
                "企业重大决策或事件公布",
                "市场重新评估企业价值和行业格局",
                "竞争对手和上下游调整策略",
                "监管部门关注是否涉及垄断或投资者保护",
                "整合效果或事件影响在后续财报中体现",
            ],
            "international": [
                "国际事件或博弈动作发生",
                "相关各方发表声明并评估应对方案",
                "反制措施或谈判启动",
                "市场避险情绪上升，资产价格波动",
                "最终走向取决于各方底线和妥协空间",
            ],
            "market": [
                "市场出现方向性变化或重要信号",
                "资金根据信号调整仓位和配置",
                "价格趋势形成并吸引跟风资金",
                "获利盘和政策预期影响趋势持续性",
                "趋势在基本面验证或政策干预下终结",
            ],
        }
        chain = chains.get(event_type, [
            f"{pusher}相关事件出现",
            "相关主体评估影响并调整行为",
            "影响逐步传导至相关领域",
            "后续发展取决于执行力度和外部条件",
        ])
        if has_numbers:
            chain.append("公开数字为后续核验提供量化锚点")
        return chain[:6]

    def _build_constraints(self, event_type: str, numbers: list[str]) -> str:
        """动态生成约束描述，注入提取到的关键数字。"""
        base = {
            "policy": "政策约束：执行能力、财政配套、地方积极性、利益集团阻力",
            "monetary": "货币政策约束：通胀反弹风险、汇率稳定压力、银行净息差收窄、资产泡沫担忧",
            "fiscal": "财政约束：赤字率上限、地方债务负担、资金使用效率、项目储备充足度",
            "data_release": "数据约束：统计口径变化、基数效应、季节性因素、数据修正可能性",
            "personnel": "人事约束：政策连续性、团队磨合、既得利益格局、外部环境变化",
            "accident": "应急约束：救援能力、信息透明、次生灾害风险、监管资源",
            "corporate": "商业约束：监管审查、债务负担、整合难度、市场竞争",
            "international": "国际约束：国内政治、利益集团、第三方反应、国际法框架",
            "market": "市场约束：流动性、估值水平、政策底线、外部冲击",
        }
        constraint = base.get(event_type, "资源约束：财政、执行能力、外部配合、不确定性")
        if numbers:
            constraint += f"；本次事件涉及关键数字：{'、'.join(numbers)}，这些数字的真实性和后续修正将直接影响判断"
        return constraint

    def _build_least_resistance_path(self, event_type: str, institutions: list[str]) -> str:
        """生成最小阻力路径（对应方法论 4.4/4.5：最省力路径即最可能路径）。"""
        pusher = institutions[0] if institutions else "决策方"
        paths = {
            "policy": f"最小阻力路径：{pusher}先发布框架性文件留出弹性空间，执行层在细则中缓冲冲击，先易后难逐步推进，遇到阻力时以试点方式探索",
            "monetary": f"最小阻力路径：{pusher}采用小幅渐进式调整，观察市场反应后决定后续力度，优先使用价格型工具避免数量型工具的信号冲击",
            "fiscal": f"最小阻力路径：优先使用已有预算内资金和专项债，避免新增赤字；项目选择偏向见效快、就业拉动强的领域",
            "data_release": "最小阻力路径：市场在数据公布后快速定价，随后等待后续数据和政策信号确认方向，不急于单边押注",
            "personnel": f"最小阻力路径：新任决策者先保持政策连续性稳定预期，在掌握情况后逐步调整，优先解决最紧迫的问题",
            "accident": "最小阻力路径：先全力救援控制事态，信息公开以稳定情绪，整改以重点领域先行，避免全面收紧导致次生影响",
            "corporate": "最小阻力路径：企业先与监管和主要股东沟通获得支持，交易方案设计留出监管审批弹性，整合以业务协同先行",
            "international": "最小阻力路径：双方先通过非正式渠道摸底底线，正式谈判中先易后难，在核心利益之外寻找交换空间",
            "market": "最小阻力路径：趋势形成后资金顺势而为，在关键阻力位和支撑位附近观望，政策信号出现时快速调整方向",
        }
        return paths.get(event_type, f"最小阻力路径：{pusher}采取渐进式策略，先试点再推广，在阻力最小的方向上逐步推进")

    def _build_counter_evidence(self, event_type: str) -> str:
        """生成反对证据与替代假设（对应方法论 6.1：先知可能错）。"""
        counters = {
            "policy": "反对证据：执行层公开抵制或变相拖延、上级政策转向、利益集团成功游说、配套资金不到位",
            "monetary": "反对证据：通胀数据超预期反弹、汇率大幅贬值压力、资产价格泡沫引发监管担忧、银行风险上升",
            "fiscal": "反对证据：赤字率突破约束、地方债务风险暴露、项目进度严重滞后、资金使用效率低下被审计指出",
            "data_release": "反对证据：后续月份数据大幅反向修正、统计口径调整说明、季节性因素被证实为主因、权威机构质疑数据质量",
            "personnel": "反对证据：新政与前任政策出现根本性冲突、团队内部公开分歧、上级否决关键决策、执行层集体消极应对",
            "accident": "反对证据：救援进展超预期、事故原因被认定为极小概率偶发事件、同类排查未发现系统性问题、监管未出台收紧措施",
            "corporate": "反对证据：监管否决交易、股东投票未通过、整合后业绩远低于预期、核心人才流失",
            "international": "反对证据：谈判破裂、一方退出协议、第三方干预改变格局、国内政治变化导致立场反转",
            "market": "反对证据：政策突然转向、外部黑天鹅事件、流动性急剧收紧、基本面数据证伪当前趋势",
        }
        return counters.get(event_type, "反对证据：执行阻力、政策转向、外部冲击、关键假设被证伪")

    def _build_leading_indicators(self, event_type: str) -> str:
        """生成领先指标总结。"""
        indicators = {
            "policy": "领先指标：配套细则发布时间、试点城市/行业名单、执行部门预算调整、地方响应速度、督查通报",
            "monetary": "领先指标：公开市场操作利率变化、MLF利率调整、银行间市场利率、信贷投放数据、汇率走势",
            "fiscal": "领先指标：专项债发行节奏、财政支出进度、项目开工率、基建投资增速、转移支付下达时间",
            "data_release": "领先指标：高频数据（发电耗煤、螺纹钢库存、商品房成交）、领先指标（PMI新订单、消费者信心）、政策信号",
            "personnel": "领先指标：新任领导首次公开讲话、首次主持会议主题、首批人事任命、政策文件措辞变化",
            "accident": "领先指标：救援进展通报、事故调查报告、同类企业自查结果、监管会议和文件、保险理赔数据",
            "corporate": "领先指标：监管审批进度、股东大会投票结果、整合后首次业绩指引、核心人员变动、客户流失率",
            "international": "领先指标：双方高层互动频率、非正式渠道消息、第三方态度变化、国内舆论导向、军事/经济动作",
            "market": "领先指标：成交量变化、北向资金流向、融资余额、期权隐含波动率、政策吹风会和官员表态",
        }
        return indicators.get(event_type, "领先指标：配套细则、执行进度、后续数据验证、权威来源表态")

    def _build_observable_signals(self, event_type: str, institutions: list[str]) -> list[str]:
        """生成 2-6 条可观测信号短语（具体到能被一条新闻证伪）。"""
        pusher = institutions[0] if institutions else "相关部门"
        signal_sets = {
            "policy": [
                f"{pusher}发布配套实施细则",
                "首批试点城市或行业名单公布",
                "执行部门预算或编制调整公告",
                "地方政府响应文件出台",
                "督查或执法检查通报发布",
            ],
            "monetary": [
                "公开市场操作利率调整公告",
                "MLF中标利率变化",
                "LPR报价调整",
                "银行间DR007持续偏离政策利率",
                "新增人民币贷款数据超预期",
            ],
            "fiscal": [
                "专项债新增发行额度下达",
                "重大项目集中开工公告",
                "财政支出进度月度数据",
                "基建投资累计增速变化",
                "地方政府新增债务限额公布",
            ],
            "data_release": [
                "下月同一指标数据公布",
                "高频经济数据周度更新",
                "统计局数据修正公告",
                "权威机构预测报告发布",
                "政策制定者对数据的公开表态",
            ],
            "personnel": [
                "新任领导首次公开讲话全文",
                "首次主持会议的议题和决议",
                "下属机构人事调整公告",
                "政策文件中措辞的明显变化",
                "外媒或内部人士透露的新政方向",
            ],
            "accident": [
                "事故最终调查报告发布",
                "伤亡人数最终确认通报",
                "同类行业全国排查结果公告",
                "监管处罚或整改通知下达",
                "保险理赔金额公开",
            ],
            "corporate": [
                "监管审批结果公告",
                "股东大会投票结果",
                "整合后首次业绩预告",
                "核心管理层变动公告",
                "主要客户续约或流失消息",
            ],
            "international": [
                "双方高层会晤公告",
                "联合声明或协议文本发布",
                "关税或制裁措施调整公告",
                "第三方国家表态或行动",
                "联合国或国际组织决议",
            ],
            "market": [
                "成交量突破或萎缩至关键阈值",
                "北向资金连续净流入或流出",
                "融资余额变化趋势",
                "央行或证监会官员公开表态",
                "重要指数突破关键技术位",
            ],
        }
        signals = signal_sets.get(event_type, [
            f"{pusher}后续官方公告",
            "相关执行部门行动通报",
            "后续数据或进展更新",
            "权威来源对事件的定性表态",
        ])
        return signals[:5]

    # 模板类比的显式标注。**不允许它冒充"查到的历史"。**
    PARALLEL_SOURCE_LABEL = "本机模板类比（不是检索结果，需自行核验）"

    @classmethod
    def _best_historical_parallel(cls, text: str):
        """按 (命中关键词条数, 关键词总长) 取最佳匹配。

        v1.3 改打分匹配的原因：原先是首个命中即返回，而关键词大量交叠
        （"降息"同时属于第 1、2、15 条），导致后面的条目永远到不了。
        打分后"美联储加息"能正确落到美联储那条，而不是落到"利率调整"那条。
        """
        best = None
        for keywords, name, similarity, difference in cls._HISTORICAL_PARALLELS:
            hits = [kw for kw in keywords if kw in text]
            if not hits:
                continue
            score = (len(hits), sum(len(kw) for kw in hits))
            if best is None or score > best[0]:
                best = (score, hits, name, similarity, difference)
        return best

    @classmethod
    def _find_historical_parallel(cls, text: str) -> str | None:
        """基于关键词匹配历史事件（对应方法论 4.1：历史的周期律）。"""
        found = cls._best_historical_parallel(text)
        if found is None:
            return None
        _score, hits, name, similarity, difference = found
        return (
            f"可比事件：{name}｜**{cls.PARALLEL_SOURCE_LABEL}**。"
            f"匹配关键词：{'、'.join(hits)}。"
            f"相似点：{similarity}。不同点：{difference}"
        )

    def analyze(self, bundle: EvidenceBundle) -> JudgmentResult:
        # 区间宽度：仅由证据等级决定（证据越弱越宽）。区间**中心**不再由 E 级决定——
        # 那是审计批的「来源越多概率越高」失真源；中心在 impacts._candidate 里由
        # base_rate + 信号调整给出。这里 low/high 只是围绕中性先验 0.5 的占位带，
        # 最终由 _candidate 用 base_rate 重算。confidence 仍随证据增强而提高。
        width_by_level = {"E1": 0.18, "E2": 0.14, "E3": 0.10, "E4": 0.07}
        width = width_by_level[bundle.evidence_level]
        low = round(max(0.0, 0.5 - width), 2)
        high = round(min(1.0, 0.5 + width), 2)
        confidence = {"E1": 0.30, "E2": 0.50, "E3": 0.70, "E4": 0.82}[
            bundle.evidence_level
        ]

        # 合并所有文本用于实体提取与事件分类
        text = " ".join(
            [bundle.title, bundle.summary]
            + [f"{item.title} {item.summary}" for item in bundle.items]
        )

        # 实体提取与事件分类
        institutions = self._extract_institutions(text)
        numbers = self._extract_numbers(text)
        event_type = self._classify_event(text)

        # 动态生成各字段
        stakeholders = self._build_stakeholders(event_type, institutions, text)
        beneficiaries = self._build_beneficiaries(event_type, institutions)
        cost_bearers = self._build_cost_bearers(event_type, institutions)
        constraints = self._build_constraints(event_type, numbers)
        least_resistance_path = self._build_least_resistance_path(event_type, institutions)
        counter_evidence = self._build_counter_evidence(event_type)
        leading_indicators = self._build_leading_indicators(event_type)
        observable_signals = self._build_observable_signals(event_type, institutions)
        historical_parallel = self._find_historical_parallel(text)
        causal_chain = self._build_causal_chain(event_type, institutions, bool(numbers))

        # P1 规则引擎：用登高望远方法论的结构化规则增强研判
        # 1) 领先指标检测：从证据文本中匹配已知的领先信号模式（试点/预算/人事/草案/数据/利率等）
        #    v1.3：risk_boost 不再只是拼在文本里的装饰 —— 它由
        #    impacts._signal_adjustment 折进概率中心，真正参与运算。
        detected_indicators = detect_leading_indicators(bundle.title, bundle.summary)
        leading_boost = total_leading_boost(detected_indicators)
        if detected_indicators:
            extra = "；".join(
                f"{m['signal']}（领先信号权重 +{m['risk_boost']:.0%}）"
                for m in detected_indicators
            )
            leading_indicators = (
                f"{leading_indicators}｜规则引擎命中：{extra}"
                f"｜合计权重 +{leading_boost:.0%}（已计入概率中心，合计封顶 "
                f"{MAX_LEADING_BOOST:.0%}）"
            )
        # 2) 风险信号检测：慷慨激昂 = 内心已感知风险。
        #    ⚠ v1.3 修正方向：旧代码在这里做 `confidence += 0.08` ——
        #    越慷慨激昂，系统越自信。而方法论说的是**风险更高**，不是"我们判断得更准"。
        #    现在：不动 confidence，改为把命中词记下来，由 impacts 上调**告警等级**。
        risk_signal_hit = detect_risk_signals(bundle.title, bundle.summary)
        # 3) 多路径推演：最可能 / 次可能 / 黑天鹅（方法论要求不能只给最小阻力路径）
        scenario_paths = generate_scenario_paths(event_type, institutions, text)
        # 4) 权力结构分析：谁有否决权、执行层会不会拖延
        power_structure = analyze_power_structure(institutions, text)

        # 紧急程度判断
        urgent = any(word in text for word in ("今日", "本月", "立即", "生效", "实施", "紧急", "突发"))
        horizons = ("未来7天", "未来30天") if urgent else ("未来30天", "未来90天")

        # v1.3 修正 actors 语义（审计 4.3）：字段名是 actors（参与方），
        # 旧代码填的却是**来源域名** —— 于是界面把"某网站"当成了"当事方"。
        # 现在 actors = 从证据里提取到的机构名；域名另存 source_domains 供来源区块用。
        source_domains = tuple(
            dict.fromkeys(item.domain for item in bundle.items if item.domain)
        )
        actors = tuple(institutions) if institutions else source_domains
        source_ids = tuple(dict.fromkeys(item.source_id for item in bundle.items))

        # fact_summary：用标题 + 提取到的机构/数字丰富
        fact_summary = bundle.title or "公开来源出现新的外部事件"
        if institutions and numbers:
            fact_summary += f"（涉及{institutions[0]}，关键数字：{'、'.join(numbers[:2])}）"
        elif institutions:
            fact_summary += f"（涉及{institutions[0]}）"

        raw = {
            "fact_summary": fact_summary,
            "actors": list(actors),
            "causal_chain": causal_chain,
            "uncertainties": [
                "公开信息可能不完整，执行细节和实际力度仍需后续来源确认",
                f"事件类型判定为「{event_type}」，若实际涉及多重属性，分析维度可能不完整",
            ],
            "horizons": list(horizons),
            "probability_low": low,
            "probability_high": high,
            "confidence": confidence,
            "supporting_source_ids": list(source_ids),
            "counter_source_ids": [],
            "up_triggers": ["出现正式文件或新增独立来源确认", "执行层采取实质性行动"],
            "down_triggers": ["权威来源否认、延期或关键数字被修正", "执行层公开抵制或政策转向"],
            "impact_categories": list(bundle.categories),
            "personal_action": "本地离线模板未掌握你的具体情况，按此类事件的通用保守策略处理，待远程AI结合你的画像复核。",
            "gyw": {
                "stakeholders": stakeholders,
                "constraints": constraints,
                "least_resistance_path": least_resistance_path,
                "counter_evidence": counter_evidence,
                "leading_indicators": leading_indicators,
                "beneficiaries": beneficiaries,
                "cost_bearers": cost_bearers,
                "historical_parallel": historical_parallel,
                "observable_signals": observable_signals,
            },
        }
        result = validate_judgment(raw, set(bundle.allowed_source_ids))
        # P1 规则引擎结果在 schema 校验通过后附加，不进入严格 gyw schema
        # （避免破坏远程 AI provider 的输出契约）。
        result.gyw["scenario_paths"] = scenario_paths
        result.gyw["power_structure"] = power_structure
        # risk_signal_hit：命中词列表（空列表=未命中）。前端要显示"因为哪个词"，
        # 否则用户看到"风险上调"却无从判断这句话凭什么。
        result.gyw["risk_signal_hit"] = risk_signal_hit
        result.gyw["leading_indicator_hits"] = detected_indicators
        result.gyw["leading_boost"] = leading_boost
        result.gyw["historical_parallel_source"] = self.PARALLEL_SOURCE_LABEL
        # 来源域名（与 actors 区分开：域名是"谁报道的"，actors 是"谁在事里"）
        result.gyw["source_domains"] = list(source_domains)
        found = self._best_historical_parallel(text)
        result.gyw["historical_parallel_detail"] = (
            None
            if found is None
            else {
                "event": found[2],
                "similarity": found[3],
                "difference": found[4],
                "matched_keywords": found[1],
                "source": self.PARALLEL_SOURCE_LABEL,
            }
        )
        return result
