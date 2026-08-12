import re


DOMAIN_KEYWORDS = {
    "health": ("医院", "住院", "手术", "医保", "医疗", "复课", "身体"),
    "cashflow": ("元", "工资", "收入", "支出", "费用", "账单", "贷款", "还款"),
    "work": ("工作", "工资", "客户", "工地", "全勤", "报销"),
    "policy": ("政策", "税", "平台规则", "监管"),
}

RISK_MARKERS = (
    "未到账", "没到账", "不到账", "延迟", "少发", "扣发", "冻结", "欠款",
    "逾期", "支出", "支付", "费用", "新增贷款", "还款压力", "停工", "失业",
    "手术", "住院", "受伤", "清创",
)

BENEFIT_MARKERS = (
    "到账", "收到", "收款", "退款", "退费", "报销到账", "收入增加", "加薪",
    "结清", "还清",
)


def _direction(text):
    if any(marker in text for marker in RISK_MARKERS):
        return "risk"
    if any(marker in text for marker in BENEFIT_MARKERS):
        return "benefit"
    return "neutral"


def create_candidate(text, occurred_at):
    """Create a conservative offline classification without pretending to be AI."""
    normalized = text.strip()
    domains = [
        domain
        for domain, keywords in DOMAIN_KEYWORDS.items()
        if any(keyword in normalized for keyword in keywords)
    ]
    amounts = [float(value) for value in re.findall(r"(\d+(?:\.\d+)?)\s*元", normalized)]
    future_markers = ("预计", "可能", "将", "计划", "之前", "以后", "下周", "下月")
    high_risk = bool({"health", "cashflow"}.intersection(domains))
    return {
        "original_text": normalized,
        "occurred_at": occurred_at,
        "domains": domains or ["general"],
        "amounts": amounts,
        "direction": _direction(normalized),
        "requires_human_confirmation": high_risk,
        "can_register_forecast": any(marker in normalized for marker in future_markers),
        "confidence": "low",
    }
