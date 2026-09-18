import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4


ALLOWED_PROBABILITIES = {0.05, 0.10, 0.20, 0.35, 0.50, 0.65, 0.80, 0.90, 0.95}


class ForecastConflictError(ValueError):
    """Raised when a new forecast would reuse an existing identity."""


def _iso_week_label(value):
    """Defensively resolve a stored resolved_at string to an ISO week label."""
    text = str(value or "").strip()
    if not text:
        return "", False
    try:
        day = datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            day = datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return "", False
    iso = day.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}", True


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _int_or_zero(value):
    """把卡片正文里读回来的字符串字段转成整数；转不了就是 0，不编造。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _is_settleable_content(content) -> bool:
    """命题本身有没有「可观测事实」这一要素。

    这是 R-03 的机械判据：v1.3 之前的 8,607 条历史命题的 `observable_signals`
    全为空 —— 它们**即使补结算也永远进不了校准**，而面板此前把这件事说成了
    「等远见多跑几轮就好了」。判据只用命题自己的内容，与结算状态无关。
    """
    fields = parse_frontmatter(content or "")
    return bool(str(fields.get("observable_signals") or "").strip())


def parse_frontmatter(text):
    """Parse the flat YAML subset used by forecast cards."""
    fields = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return fields
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip().strip('"')
    return fields


def parse_sections(text):
    """Parse the named reasoning sections from a rendered forecast card."""
    names = {
        "因果链": "causal_chain",
        "支持证据": "supporting_evidence",
        "反对证据": "opposing_evidence",
        "替代假设": "alternatives",
        "反证条件": "falsification",
        "建议行动": "recommended_action",
    }
    result = {}
    current = None
    lines = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current:
                result[current] = "\n".join(lines).strip()
            current = names.get(line.removeprefix("## ").strip())
            lines = []
        elif current:
            lines.append(line)
    if current:
        result[current] = "\n".join(lines).strip()
    return result


def _single_line(value):
    """Keep user-provided frontmatter values inside one local text line."""
    return " ".join(str(value).replace("\x00", "").split())


def _normalized_card(data, forecast_id=None, created_at=None):
    """Validate and normalize the public forecast-card fields."""
    title = _single_line(data.get("title", ""))
    criteria = _single_line(data.get("resolution_criteria", ""))
    if not title:
        raise ValueError("预测标题不能为空")
    if not criteria:
        raise ValueError("结算标准不能为空")
    try:
        probability = round(float(data.get("probability")), 2)
    except (TypeError, ValueError):
        raise ValueError("概率必须选择固定档位") from None
    if probability not in ALLOWED_PROBABILITIES:
        raise ValueError("概率必须选择固定档位")
    window_start = _single_line(data.get("window_start", ""))
    window_end = _single_line(data.get("window_end", ""))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", window_start) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", window_end
    ):
        raise ValueError("日期必须使用年月日")
    if window_start > window_end:
        raise ValueError("日期范围不能前后倒置")
    identity = _single_line(forecast_id or data.get("forecast_id", ""))
    if not identity:
        identity = f"F-{datetime.now(timezone.utc):%Y%m%d}-{uuid4().hex[:8].upper()}"
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,64}", identity):
        raise ValueError("预测编号格式无效")
    # 这条预测怎么进账本的：user（本人选的）/ auto（系统按 E2+ 自动确认）/
    # unknown（v1.1 之前的历史行，来源不可考）。
    # **必须在这里显式列入返回表** —— 本函数重建 dict，不在返回表里的键会被静默丢掉，
    # 调用方传进来的值就消失了（这个字段第一次加上时正是这样没生效的）。
    confirmed_by = _single_line(data.get("confirmed_by", "unknown")) or "unknown"
    if confirmed_by not in ("user", "auto", "unknown"):
        confirmed_by = "unknown"
    # 基准率：来自账本自身统计，这里只做归一化与边界守卫。样本不足时调用方传
    # None（绝不编造）。只接受 None 或 [0,1] 浮点，其它一律当 None。
    base_rate = data.get("base_rate", None)
    if base_rate is not None:
        try:
            base_rate = float(base_rate)
            if not (0.0 <= base_rate <= 1.0):
                base_rate = None
        except (TypeError, ValueError):
            base_rate = None
    try:
        base_rate_sample = int(data.get("base_rate_sample", 0) or 0)
    except (TypeError, ValueError):
        base_rate_sample = 0
    # 同类别**全部**二元结算样本数（人工 + 自动 + 历史不明），用于在界面上写出
    # 「本类别样本 N 条，其中人工 M 条」。base_rate_sample 是其中人工的那部分。
    try:
        base_rate_sample_total = int(data.get("base_rate_sample_total", 0) or 0)
    except (TypeError, ValueError):
        base_rate_sample_total = 0
    # 这条预测最后是怎么结算的：user（逐条人工判定）/ batch（批量或自动规则）/
    # unknown（v1.4 之前的历史行，来源不可考）。与 confirmed_by 同构，理由也一样：
    # 批量结算出来的 indeterminate 是「没来得及看」，逐条判定出来的 indeterminate
    # 是「看了但判不了」—— 两者含义完全不同，账本必须能一眼分开。
    # 同上：**不列进返回表就会被静默丢掉**（这个坑已发作 4 次）。
    resolved_by = _single_line(data.get("resolved_by", "unknown")) or "unknown"
    if resolved_by not in ("user", "batch", "unknown"):
        resolved_by = "unknown"
    return {
        "forecast_id": identity,
        "created_at": _single_line(
            created_at or data.get("created_at") or datetime.now(timezone.utc).isoformat()
        ),
        "status": "open",
        "title": title,
        "statement": title,
        "category": _single_line(data.get("category", "general")) or "general",
        "resolution_criteria": criteria,
        "window_start": window_start,
        "window_end": window_end,
        "probability": probability,
        "confidence": _single_line(data.get("confidence", "medium")) or "medium",
        "alert_level": _single_line(data.get("alert_level", "L2")) or "L2",
        "next_review_at": _single_line(data.get("next_review_at", window_start)),
        "model_version": _single_line(data.get("model_version", "v0.2")) or "v0.2",
        "privacy_level": _single_line(data.get("privacy_level", "P2")) or "P2",
        "causal_chain": str(data.get("causal_chain", "尚未补充。" )).strip(),
        "supporting_evidence": str(data.get("supporting_evidence", "尚未补充。" )).strip(),
        "opposing_evidence": str(data.get("opposing_evidence", "尚未补充。" )).strip(),
        "alternatives": str(data.get("alternatives", "尚未补充。" )).strip(),
        "falsification": str(data.get("falsification", criteria)).strip(),
        "recommended_action": str(data.get("recommended_action", "继续观察并在复核日更新。" )).strip(),
        # 命题四要素里的"阈值或可观测事实"。**必须列进这张返回表** ——
        # 本函数重建 dict，不在表里的键会被静默丢弃（v1.1 的 confirmed_by 就是这样
        # 第一版完全没生效的）。丢了它，命题在核对时就无法机械判定可结算性。
        "observable_signals": str(data.get("observable_signals", "")).strip(),
        "confirmed_by": confirmed_by,
        "base_rate": base_rate,
        "base_rate_sample": base_rate_sample,
        "base_rate_sample_total": base_rate_sample_total,
        # 结算来源：这条命题最后是被「逐条判定」还是「批量/自动规则」结算的。
        # 与 confirmed_by 一样必须走完整条链，否则到期核对时分不出
        # 「没来得及看」与「看了但判不了」。
        "resolved_by": resolved_by,
        # 量级（v1.3）：没有数字时为「未量化」的如实表述，**不是**一个数字。
        "magnitude": str(data.get("magnitude", "")).strip(),
        # 「慷慨激昂」命中的词（v1.3）：让"为什么这条等级更高"在账本里可追溯。
        "risk_signal_keywords": str(data.get("risk_signal_keywords", "")).strip(),
    }


def _render_card(card):
    """Render the canonical local forecast card used for immutable hashing."""
    return f"""---
forecast_id: {card['forecast_id']}
created_at: {card['created_at']}
status: open
title: {card['title']}
resolution_criteria: {card['resolution_criteria']}
window_start: {card['window_start']}
window_end: {card['window_end']}
probability: {card['probability']:.2f}
confidence: {card['confidence']}
alert_level: {card['alert_level']}
next_review_at: {card['next_review_at']}
model_version: {card['model_version']}
privacy_level: {card['privacy_level']}
observable_signals: {card.get('observable_signals', '')}
base_rate: {('null' if card['base_rate'] is None else card['base_rate'])}
base_rate_sample: {card['base_rate_sample']}
base_rate_sample_total: {card.get('base_rate_sample_total', 0)}
resolved_by: {card.get('resolved_by', 'unknown')}
magnitude: {card.get('magnitude', '')}
risk_signal_keywords: {card.get('risk_signal_keywords', '')}
---
## 因果链
{card['causal_chain']}
## 支持证据
{card['supporting_evidence']}
## 反对证据
{card['opposing_evidence']}
## 替代假设
{card['alternatives']}
## 反证条件
{card['falsification']}
## 建议行动
{card['recommended_action']}
"""


class ForecastService:
    """Read immutable forecast versions and record explicit resolutions."""

    def __init__(self, database):
        self.database = database

    def list_forecasts(self, limit=None, offset=0):
        """Return one summary per forecast using its latest version."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT f.forecast_id, f.status, f.window_end, f.category,
                       f.confirmed_by, f.resolved_by, f.base_rate_sample,
                       f.created_at AS created_at_column,
                       v.version, v.probability, v.content
                FROM forecasts f
                JOIN forecast_versions v ON v.forecast_id = f.forecast_id
                JOIN (
                    SELECT forecast_id, MAX(version) AS latest_version
                    FROM forecast_versions GROUP BY forecast_id
                ) latest ON latest.forecast_id = v.forecast_id
                         AND latest.latest_version = v.version
                ORDER BY f.window_end, f.forecast_id
                """
            ).fetchall()
        result = []
        for row in rows:
            fields = parse_frontmatter(row["content"])
            summary = {
                "forecast_id": row["forecast_id"],
                "status": row["status"],
                "window_end": row["window_end"],
                "category": row["category"] or "general",
                # 这条预测的来源：user=本人选的 / auto=系统自动确认 / unknown=历史行。
                # 必须原样透出，界面才能把"机器填的"和"你选的"分开显示。
                "confirmed_by": row["confirmed_by"] or "unknown",
                # 结算来源：user=逐条人工判定 / batch=批量或自动规则 / unknown=历史行。
                # 与 confirmed_by 同理，界面必须能一眼分开"没来得及看"和"看了但判不了"。
                "resolved_by": row["resolved_by"] or "unknown",
                "version": row["version"],
                "probability": row["probability"],
                "title": fields.get("title", row["forecast_id"]),
                "statement": fields.get("title", row["forecast_id"]),
                # v1.4 起时间轴来自 forecasts.created_at 列；**历史行该列为 NULL
                # （不回填）**，此时退回解析卡片正文里的 created_at —— 这是老行的
                # 唯一来源，读不出来就如实给空串，不猜。
                "created_at": row["created_at_column"] or fields.get("created_at", ""),
                "confidence": fields.get("confidence", "unknown"),
                "alert_level": fields.get("alert_level", "L1"),
                "resolution_criteria": fields.get("resolution_criteria", ""),
                "observable_signals": fields.get("observable_signals", ""),
                "base_rate_sample": row["base_rate_sample"] or 0,
                "base_rate_sample_total": _int_or_zero(fields.get("base_rate_sample_total")),
            }
            result.append(summary)
        total = len(result)
        start = max(0, int(offset or 0))
        if limit is not None:
            result = result[start : start + max(1, int(limit))]
        elif start:
            result = result[start:]
        return result, total

    def get_forecast(self, forecast_id):
        """Return a forecast summary plus all immutable versions."""
        forecasts, _total = self.list_forecasts()
        items = [item for item in forecasts if item["forecast_id"] == forecast_id]
        if not items:
            raise KeyError(forecast_id)
        with self.database.connect() as connection:
            versions = [
                dict(row)
                for row in connection.execute(
                    "SELECT version, probability, content FROM forecast_versions WHERE forecast_id = ? ORDER BY version",
                    (forecast_id,),
                ).fetchall()
            ]
        latest_fields = parse_frontmatter(versions[-1]["content"])
        draft = {
            key: latest_fields.get(key, "")
            for key in (
                "title",
                "resolution_criteria",
                "window_start",
                "window_end",
                "probability",
                "confidence",
                "alert_level",
                "privacy_level",
            )
        }
        draft.update(parse_sections(versions[-1]["content"]))
        return {**items[0], "versions": versions, "draft": draft}

    def base_rate_for_category(self, category: str):
        """同类别历史结算命中率（来自账本自身 resolutions）。

        返回 (rate: float|None, sample: int)。样本 < 5 时 rate 为 None——
        **绝不编造默认值顶上**。命中率 = outcome='occurred' 的占比。
        样本 = 同类别**人工确认（confirmed_by='user'）且结果为二元**的已结算预测。

        ⚠ 本方法在 v1.4 被**反转过一次**。两次结论都对，理由不同：

        - v1.1 曾按 `confirmed_by != 'unknown'` 过滤，把机制锁死了：该字段当时
          DEFAULT 就是 'unknown'，于是任何不经 confirm_candidate 落库的预测都被
          排除，样本永远为 0。那次撤销是对的。
        - v1.4 起改为**只采 `confirmed_by='user'`**（R-04）。理由是基准率的口径：
          它是「**我**出题的命中率」。若把机器自动确认的条目算进去，base_rate 就变成
          「机器填的概率的自我回声」—— 自动条目的概率本就由 base_rate 生成，结算后
          又回流成 base_rate，形成正反馈闭环，系统会稳定地显示出「我越来越准」
          而世界没变。现在 `confirmed_by` 在新建预测上有真实取值（不再是 v1.1 那个人人
          都是 'unknown' 的默认值），所以这次过滤不会再把样本锁死。

        代价是诚实的：人工样本长期不足 5 条时该类别 base_rate 保持 NULL，新预测因此
        进不了校准。**这是可接受的（诚实优先）**，但界面必须把原因写出来——
        见 `base_rate_composition`，不要让人以为是 bug。

        取样**不依赖父行存活**（R-12）：`forecasts` 在 v1.4 之前没有不可变触发器，
        历史上被删掉父行的结算记录会从 `JOIN` 里整体消失（连带 excluded_total 也
        看不到它们）。改用 LEFT JOIN + `resolutions.category` 冗余列兜底，被删父行的
        结算仍然进得了统计。
        """
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS n,
                       SUM(CASE WHEN r.outcome='occurred' THEN 1 ELSE 0 END) AS hits
                FROM resolutions r
                LEFT JOIN forecasts f ON f.forecast_id = r.forecast_id
                WHERE COALESCE(NULLIF(r.category, ''), f.category, 'general') = ?
                  AND r.brier_score IS NOT NULL
                  AND r.outcome IN ('occurred','not_occurred')
                  AND COALESCE(NULLIF(r.confirmed_by, ''), f.confirmed_by, 'unknown') = 'user'
                """,
                (category or "general",),
            ).fetchone()
        sample = row["n"] if row else 0
        if sample < 5:
            return (None, sample)
        return (round(row["hits"] / sample, 4), sample)

    def base_rate_composition(self, category: str) -> dict:
        """同类别的二元结算样本构成：人工 user 条、非人工 auto 条、合计 total 条。

        界面要写「本类别样本 N 条，其中人工 M 条」—— 否则 base_rate_sample<5 时
        用户只看到「样本不足」，既不知道差在哪，也不知道怎么才能补上。
        """
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT
                  SUM(CASE WHEN COALESCE(NULLIF(r.confirmed_by,''), f.confirmed_by, 'unknown')='user'
                           THEN 1 ELSE 0 END) AS user_n,
                  COUNT(*) AS total_n
                FROM resolutions r
                LEFT JOIN forecasts f ON f.forecast_id = r.forecast_id
                WHERE COALESCE(NULLIF(r.category, ''), f.category, 'general') = ?
                  AND r.brier_score IS NOT NULL
                  AND r.outcome IN ('occurred','not_occurred')
                """,
                (category or "general",),
            ).fetchone()
        user_n = int(row["user_n"] or 0) if row else 0
        total_n = int(row["total_n"] or 0) if row else 0
        return {"user": user_n, "total": total_n, "auto": total_n - user_n}

    def create_forecast(self, card_data):
        """Create a validated forecast and its first immutable version."""
        card = _normalized_card(card_data)
        # 基准率以创建时刻的同类别历史为准（不反推、不编造）。即便调用方传入
        # base_rate，也以实时统计覆盖，保证账本里记的是可核验的来源。
        base_rate, base_rate_sample = self.base_rate_for_category(card["category"])
        card["base_rate"] = base_rate
        card["base_rate_sample"] = base_rate_sample
        content = _render_card(card)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self.database.connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM forecasts WHERE forecast_id = ?", (card["forecast_id"],)
            ).fetchone()
            if exists:
                raise ForecastConflictError("预测编号已经存在")
            connection.execute(
                "INSERT INTO forecasts(forecast_id, status, window_end, category,"
                " confirmed_by, base_rate, base_rate_sample, resolved_by, created_at)"
                " VALUES (?, 'open', ?, ?, ?, ?, ?, 'unknown', ?)",
                (
                    card["forecast_id"],
                    card["window_end"],
                    card["category"],
                    card.get("confirmed_by") or "unknown",
                    card.get("base_rate"),
                    card.get("base_rate_sample", 0),
                    card["created_at"],
                ),
            )
            connection.execute(
                "INSERT INTO forecast_versions(forecast_id, version, probability, content_sha256, content) VALUES (?, 1, ?, ?, ?)",
                (card["forecast_id"], card["probability"], digest, content),
            )
            connection.execute(
                "INSERT INTO audit_log(occurred_at, action, object_type, object_id, details_json) VALUES (?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    "forecast.create",
                    "forecast",
                    card["forecast_id"],
                    json.dumps({"version": 1}, ensure_ascii=False),
                ),
            )
        return {"forecast_id": card["forecast_id"], "version": 1, "duplicate": False}

    def add_version(self, forecast_id, card_data):
        """Append a changed immutable version or return the matching latest version."""
        with self.database.connect() as connection:
            latest = connection.execute(
                "SELECT version, content, content_sha256 FROM forecast_versions WHERE forecast_id = ? ORDER BY version DESC LIMIT 1",
                (forecast_id,),
            ).fetchone()
            if latest is None:
                raise KeyError(forecast_id)
            original = parse_frontmatter(latest["content"])
            merged = {**original, **parse_sections(latest["content"]), **card_data}
            card = _normalized_card(
                merged,
                forecast_id=forecast_id,
                created_at=original.get("created_at"),
            )
            content = _render_card(card)
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if digest == latest["content_sha256"]:
                return {
                    "forecast_id": forecast_id,
                    "version": latest["version"],
                    "duplicate": True,
                }
            version = latest["version"] + 1
            connection.execute(
                "INSERT INTO forecast_versions(forecast_id, version, probability, content_sha256, content) VALUES (?, ?, ?, ?, ?)",
                (forecast_id, version, card["probability"], digest, content),
            )
            connection.execute(
                "UPDATE forecasts SET window_end = ? WHERE forecast_id = ?",
                (card["window_end"], forecast_id),
            )
            connection.execute(
                "INSERT INTO audit_log(occurred_at, action, object_type, object_id, details_json) VALUES (?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    "forecast.version.add",
                    "forecast",
                    forecast_id,
                    json.dumps({"version": version}, ensure_ascii=False),
                ),
            )
        return {"forecast_id": forecast_id, "version": version, "duplicate": False}

    def resolve(self, forecast_id, outcome, resolved_at, note, resolved_by="user"):
        """Resolve a forecast once and score binary outcomes.

        `resolved_by` 与 `confirmed_by` 同构：'user' = 本人在界面上逐条判定；
        'batch' = 批量结算或自动归档规则。**必须能一眼分开**：批量写出来的
        `indeterminate` 是「没来得及看」，逐条判定写出来的 `indeterminate` 是
        「看了但判不了」，两者的含义完全不同。

        `resolutions.category` 在这里冗余写入（R-12）：`forecasts` 在 v1.4 之前
        没有不可变触发器，父行可能已被删除；不冗余的话，那些结算记录会从基准率
        （以及 excluded_total）里整体消失，而 v1.2 §四 承诺过「不静默缩小总数」。
        """
        outcomes = {"occurred": 1.0, "not_occurred": 0.0}
        allowed = {*outcomes, "partial", "indeterminate"}
        if outcome not in allowed:
            raise ValueError("不支持的结算结果")
        if resolved_by not in ("user", "batch"):
            raise ValueError("结算来源只能是 user 或 batch")
        resolved_at = str(resolved_at or "").strip()
        if resolved_at and not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?", resolved_at
        ):
            raise ValueError("结算日期格式无效，应为年月日")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT v.probability, f.category,"
                " COALESCE(f.confirmed_by,'unknown') AS confirmed_by"
                " FROM forecast_versions v"
                " LEFT JOIN forecasts f ON f.forecast_id = v.forecast_id"
                " WHERE v.forecast_id = ? ORDER BY v.version DESC LIMIT 1",
                (forecast_id,),
            ).fetchone()
            if row is None:
                raise KeyError(forecast_id)
            probability = row["probability"]
            category = row["category"] or "general"
            brier = None if outcome not in outcomes else round((probability - outcomes[outcome]) ** 2, 10)
            connection.execute(
                "INSERT INTO resolutions(forecast_id, outcome, resolved_at, probability,"
                " brier_score, category, confirmed_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    forecast_id, outcome, resolved_at, probability, brier, category,
                    row["confirmed_by"] or "unknown",
                ),
            )
            connection.execute(
                "UPDATE forecasts SET status = 'resolved', resolved_by = ? WHERE forecast_id = ?",
                (resolved_by, forecast_id),
            )
            connection.execute(
                "INSERT INTO audit_log(occurred_at, action, object_type, object_id, details_json) VALUES (?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    "forecast.resolve",
                    "forecast",
                    forecast_id,
                    json.dumps(
                        {"outcome": outcome, "note": note, "resolved_by": resolved_by},
                        ensure_ascii=False,
                    ),
                ),
            )
        return {"forecast_id": forecast_id, "outcome": outcome, "brier_score": brier}

    #: 批量结算的默认结果。
    #:
    #: **这是本任务书里最不可妥协的一条：绝不能用 `not_occurred`。**
    #: `resolutions.outcome` 的枚举是 occurred / not_occurred / partial /
    #: indeterminate，而 Brier 只在 occurred / not_occurred 上计算（见 `resolve()`：
    #: outcome 不在 outcomes 里时 brier=None）。若拿 not_occurred 当批量默认，等于
    #: **凭空给成百上千条命题盖上「没发生」的断言**，而这些断言不是观察、是默认值——
    #: 它们会直接进入 Brier 与命中率/误报率的分子分母，把校准彻底污染。这与前几轮
    #: 已修掉的「机器填的概率混进人类命中率」是同一个病，只是这次更严重。
    #:
    #: indeterminate 的效果：积压清了、status 正常流转、brier=None，且不进
    #: hit_rate / false_positive_rate / 整体 Brier 平均。**积压清了，校准没被污染。**
    BATCH_DEFAULT_OUTCOME = "indeterminate"

    def batch_targets(self, *, categories=None, due_before=None, status="open"):
        """批量结算的**只读**预览：将结算 N 条、到期区间是哪一段。

        `resolutions` 有不可变触发器（写进去就删不掉、改不了），所以批量结算
        不可撤销 —— 确认前必须让用户看到自己按下的是多大一批、覆盖哪段时间。
        """
        with self.database.connect() as connection:
            rows = self._batch_candidates(
                connection, categories=categories, due_before=due_before, status=status
            )
        return {
            "count": len(rows),
            "window_end_min": rows[0]["window_end"] if rows else None,
            "window_end_max": rows[-1]["window_end"] if rows else None,
            "categories": sorted({row["category"] or "general" for row in rows}),
            "default_outcome": self.BATCH_DEFAULT_OUTCOME,
        }

    @staticmethod
    def _batch_candidates(connection, *, categories=None, due_before=None,
                          status="open", limit=None):
        """按筛选条件取出可批量结算的候选（status 默认只看 'open'）。"""
        clauses = ["f.status = ?"]
        params = [str(status or "open")]
        wanted = [str(item) for item in (categories or []) if str(item).strip()]
        if wanted:
            placeholders = ",".join("?" for _ in wanted)
            clauses.append(f"f.category IN ({placeholders})")
            params.extend(wanted)
        due_before = str(due_before or "").strip()
        if due_before:
            clauses.append("f.window_end < ?")
            params.append(due_before)
        sql = (
            "SELECT f.forecast_id, f.category, f.window_end, f.base_rate,"
            " COALESCE(f.confirmed_by,'unknown') AS confirmed_by,"
            " (SELECT v.probability FROM forecast_versions v"
            "   WHERE v.forecast_id = f.forecast_id"
            "   ORDER BY v.version DESC LIMIT 1) AS probability"
            " FROM forecasts f WHERE " + " AND ".join(clauses) +
            " ORDER BY f.window_end, f.forecast_id"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        return connection.execute(sql, params).fetchall()

    def batch_resolve(self, *, outcome=None, resolved_at="", note="", categories=None,
                      due_before=None, status="open", limit=None, resolved_by="batch",
                      trigger="manual"):
        """按筛选条件**一次事务**结算一批预测；任一条失败则整批回滚。

        与 `resolve()` 共用同一套写入语义（写 resolutions + 置 status='resolved'），
        区别只在批量的原子性与留痕：整批写**一条** `audit_log`
        （`action='forecast.batch_resolve'`），`details_json` 记录筛选条件原文、
        目标条数、实际条数、结果枚举、发起时间。

        `outcome` 缺省即 `BATCH_DEFAULT_OUTCOME`（indeterminate）。理由见该类常量：
        用 not_occurred 当默认会把凭空断言的标签灌进 Brier，直接毁掉校准。
        """
        outcome = str(outcome or self.BATCH_DEFAULT_OUTCOME)
        if outcome not in ("occurred", "not_occurred", "partial", "indeterminate"):
            raise ValueError("不支持的结算结果")
        if resolved_by not in ("user", "batch"):
            raise ValueError("批量结算的来源只能是 user 或 batch")
        resolved_at = str(resolved_at or "").strip()
        if resolved_at and not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?", resolved_at
        ):
            raise ValueError("结算日期格式无效，应为年月日")
        filters = {
            "status": str(status or "open"),
            "categories": [str(item) for item in (categories or []) if str(item).strip()],
            "due_before": str(due_before or "").strip() or None,
            "limit": None if limit is None else max(1, int(limit)),
            "trigger": str(trigger or "manual"),
        }
        started_at = datetime.now(timezone.utc).isoformat()
        binary = {"occurred": 1.0, "not_occurred": 0.0}
        written = 0
        # 整个批次在**同一个** `connect()` 事务里完成：任何一条抛异常都会让
        # contextmanager 走 rollback，不会留下半批（不允许"结了 300 条、剩 700 条"）。
        with self.database.connect() as connection:
            rows = self._batch_candidates(
                connection,
                categories=filters["categories"],
                due_before=filters["due_before"],
                status=filters["status"],
                limit=filters["limit"],
            )
            target = len(rows)
            for row in rows:
                try:
                    probability = round(float(row["probability"]), 2)
                except (TypeError, ValueError):
                    probability = None
                if probability is None:
                    raise ValueError(
                        f"预测 {row['forecast_id']} 的最新版本读不出概率，整批回滚"
                    )
                brier = (
                    None
                    if outcome not in binary
                    else round((probability - binary[outcome]) ** 2, 10)
                )
                connection.execute(
                    "INSERT INTO resolutions(forecast_id, outcome, resolved_at,"
                    " probability, brier_score, category, confirmed_by)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["forecast_id"],
                        outcome,
                        resolved_at,
                        probability,
                        brier,
                        row["category"] or "general",
                        row["confirmed_by"] or "unknown",
                    ),
                )
                connection.execute(
                    "UPDATE forecasts SET status = 'resolved', resolved_by = ?"
                    " WHERE forecast_id = ?",
                    (resolved_by, row["forecast_id"]),
                )
                written += 1
            if written:
                connection.execute(
                    "INSERT INTO audit_log(occurred_at, action, object_type, object_id,"
                    " details_json) VALUES (?, ?, ?, ?, ?)",
                    (
                        started_at,
                        "forecast.batch_resolve",
                        "forecast",
                        None,
                        json.dumps(
                            {
                                "filters": filters,
                                "target_count": target,
                                "resolved_count": written,
                                "outcome": outcome,
                                "resolved_by": resolved_by,
                                "started_at": started_at,
                                "note": note,
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
        return {
            "target_count": target,
            "resolved_count": written,
            "outcome": outcome,
            "resolved_by": resolved_by,
        }

    def auto_archive_overdue(self, now=None):
        """到期满 N 天后自动记为 `indeterminate` 并归档（**用户可选，默认关闭**）。

        为什么默认关闭：账本不可变，批量结算写进去删不掉。「自动把没结算的算作
        无法判定」必须由用户明确选择，而不是系统替他做——否则用户会以为「没结算」
        是个可以反悔的状态，实际上已经被自动写死了。

        开关关闭时**不存在任何自动结算路径**（`resolutions` 一行都不会多），
        这一点有测试守着（把时钟推后 400 天，计数不变）。
        """
        from .system_settings import read_forecast_archive_setting  # 局部导入避免循环

        setting = read_forecast_archive_setting(self.database)
        if not setting.get("enabled"):
            return {"enabled": False, "archived": 0, "days": setting.get("days")}
        now = now or datetime.now(timezone.utc)
        due_before = (now.date() - timedelta(days=int(setting["days"]))).isoformat()
        result = self.batch_resolve(
            outcome=self.BATCH_DEFAULT_OUTCOME,
            resolved_at=now.date().isoformat(),
            note="自动归档规则：到期满 N 天未结算",
            due_before=due_before,
            resolved_by="batch",
            trigger="auto_archive",
        )
        return {
            "enabled": True,
            "archived": result["resolved_count"],
            "days": int(setting["days"]),
            "due_before": due_before,
        }

    def score_summary(self):
        """Return aggregate binary calibration statistics.

        判据与 `calibration_summary` 一致：**命题可结算**才进 Brier 平均。
        不用 `confirmed_by != 'unknown'` —— 那个字段是默认值，会把新建的正常预测
        也一并误排除。

        口径（v1.4，R-04）：主 `brier_score` **只算 `confirmed_by='user'`**，
        另给三个来源的分列值（`by_source`）。同一个数字在首页与校准页必须是同口径，
        半个人工半台机器的平均分没有意义。
        """
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT r.brier_score, f.base_rate,
                       COALESCE(NULLIF(r.confirmed_by, ''), f.confirmed_by, 'unknown')
                         AS confirmed_by,
                       (SELECT v.content FROM forecast_versions v
                         WHERE v.forecast_id = f.forecast_id
                         ORDER BY v.version DESC LIMIT 1) AS latest_content
                FROM resolutions r
                LEFT JOIN forecasts f ON f.forecast_id = r.forecast_id
                WHERE r.brier_score IS NOT NULL
                """
            ).fetchall()
        usable = [r for r in rows if self._calibratable(r)]
        by_source = {}
        for source in ("user", "auto", "unknown"):
            subset = [
                r for r in usable
                if r["confirmed_by"] == source and r["brier_score"] is not None
            ]
            by_source[source] = {
                "usable_total": len(
                    [r for r in usable if r["confirmed_by"] == source]
                ),
                "brier_score": (
                    round(sum(float(r["brier_score"]) for r in subset) / len(subset), 10)
                    if subset
                    else None
                ),
            }
        main = by_source["user"]
        return {
            "resolved_binary": main["usable_total"],
            "brier_score": main["brier_score"],
            "by_source": by_source,
        }

    def _calibratable(self, row) -> bool:
        """这条已结算预测能不能进 Brier 平均。两条硬门槛，任一不满足即排除：

        1) 有基准率（base_rate 非 NULL）。基准率缺失（样本 < 5）的预测，其概率
           没有「你过去在这个类别上的实际命中率」做锚，进 Brier 只会用无锚的概率
           污染校准分。这与第一波「不可校准」口径一致：校准只用**可结算 + 有基准率**
           的命题。基准率来自账本自身 resolutions（见 base_rate_for_category），
           **绝不由 probability 反推**。
        2) 命题可结算（四要素齐备，过 _settlement_ready 闸）。模糊命题自然不合格。

        base_rate 直接从 forecasts 表列读取（SELECT 已带上），不依赖版本内容解析——
        版本内容里即便渲染了 base_rate，也以表列为准，避免解析口径漂移。
        """
        # 门槛 1：无基准率 → 不可校准。
        # ⚠ row 是 sqlite3.Row —— **它没有 .get() 方法**，写成 row.get(...) 会抛
        # AttributeError 把整个校准面板打崩（本轮已实际发生过一次，被组 C 的断言抓住）。
        # 用 keys() 判断列是否存在；缺列时保守判为不可校准，宁可少算也不误算。
        if "base_rate" not in row.keys() or row["base_rate"] is None:
            return False
        fields = parse_frontmatter(row["latest_content"] or "")
        from .impacts import _settlement_ready  # 局部导入，避免模块级循环依赖

        ok, _reason = _settlement_ready(
            {
                "title": fields.get("title", ""),
                "resolution_criteria": fields.get("resolution_criteria", ""),
                "observable_signals": fields.get("observable_signals", ""),
            }
        )
        return ok

    def calibration_summary(self):
        """Flat calibration payload consumed by the calibration panel.

        口径（v1.4）：

        - `resolved_total` 是**已结算总数**（不过滤来源），Brier 只用**命题可结算**
          的那些；被排除的数量单独给出，并且**必须写出构成**（R-03：不能只给一个数，
          否则用户分不清"等一等就好了"和"永远进不了"）。
        - 主指标（`hit_rate` / `false_positive_rate` / `brier` / `by_category`）
          **只统计 `confirmed_by='user'`**（R-04）：面板把这三个数说成"你的成绩"，
          就必须只在你出过的题上算。机器自动确认的条目按来源**分列**给出，不合并。
        - 结算来源（`resolved_by`）同时分列：批量写出来的 `indeterminate` 是
          "没来得及看"，逐条判定写出来的才是"看了但判不了"。

        Philosophy: never fabricate conclusions — denominators of zero
        produce null instead of 0.
        """
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT f.forecast_id, f.category, f.base_rate,
                       COALESCE(NULLIF(r.confirmed_by, ''), f.confirmed_by, 'unknown') AS confirmed_by,
                       COALESCE(f.resolved_by, 'unknown') AS resolved_by,
                       COALESCE(NULLIF(r.category, ''), f.category, 'general') AS category_key,
                       r.outcome, r.resolved_at, r.probability, r.brier_score,
                       (SELECT v.content FROM forecast_versions v
                         WHERE v.forecast_id = f.forecast_id
                         ORDER BY v.version DESC LIMIT 1) AS latest_content
                FROM resolutions r
                LEFT JOIN forecasts f ON f.forecast_id = r.forecast_id
                """
            ).fetchall()
        resolved_total = len(rows)
        scored_rows = [row for row in rows if self._calibratable(row)]
        excluded_total = resolved_total - len(scored_rows)
        # 排除构成：**三类互斥**，按"最不可修 → 可修"的优先级归入第一类。
        # 顺序不能反着理解：一条命题若根本没有可观测事实，那么它缺不缺基准率
        # 都无所谓 —— 前者是"永远进不了"，后者只是"还没攒够样本"。
        excluded_breakdown = {
            "legacy_proposition": 0,     # 命题缺「可观测事实」要素（v1.3 之前的历史批次）
            "missing_base_rate": 0,      # 命题可结算，但同类别人工二元样本 < 5
            "unsettleable": 0,           # 有可观测信号，但过不了结算性闸（含禁用虚词）
        }
        for row in rows:
            if self._calibratable(row):
                continue
            fields = parse_frontmatter(row["latest_content"] or "")
            if not str(fields.get("observable_signals") or "").strip():
                excluded_breakdown["legacy_proposition"] += 1
            elif row["base_rate"] is None:
                excluded_breakdown["missing_base_rate"] += 1
            else:
                excluded_breakdown["unsettleable"] += 1
        by_source = {
            "user": self._calibration_metrics(
                [row for row in scored_rows if row["confirmed_by"] == "user"]
            ),
            "auto": self._calibration_metrics(
                [row for row in scored_rows if row["confirmed_by"] == "auto"]
            ),
            "unknown": self._calibration_metrics(
                [row for row in scored_rows if row["confirmed_by"] == "unknown"]
            ),
        }
        # 主指标 = 人工来源。**不要退回成跨来源聚合** —— 面板的文案把它们说成
        # "你判断得准不准"，一旦混进机器填的条目，这三个数就答非所问了。
        user_rows = [row for row in scored_rows if row["confirmed_by"] == "user"]
        main = by_source["user"]
        resolved_by_counts = {"user": 0, "batch": 0, "unknown": 0}
        for row in rows:
            key = row["resolved_by"] if row["resolved_by"] in resolved_by_counts else "unknown"
            resolved_by_counts[key] += 1
        scored = [row for row in user_rows if row["brier_score"] is not None]
        weekly = {}
        unknown_weeks = 0
        for row in scored:
            label, ok = _iso_week_label(row["resolved_at"])
            if not ok:
                unknown_weeks += 1
                continue
            weekly.setdefault(label, []).append(float(row["brier_score"]))
        brier_series = [
            {
                "week": label,
                "count": len(values),
                "brier": round(sum(values) / len(values), 10),
                "low_sample": len(values) < 2,
            }
            for label, values in sorted(weekly.items())
        ]
        by_category = self._hit_rate_by_category(
            [row for row in user_rows if row["outcome"] in {"occurred", "not_occurred"}
             and float(row["probability"]) >= 0.5]
        )
        return {
            "resolved_total": resolved_total,
            # 已结算但命题不可结算 / 缺基准率、因此不进 Brier 的条数。
            # 面板要把它和 resolved_total 分开显示 —— 用户看到的应该是
            # "已结算 12 条，其中 9 条可用于校准，3 条已排除（原因是…）"，
            # 而不是一个被静默缩小的总数。
            "excluded_total": excluded_total,
            "excluded_breakdown": excluded_breakdown,
            # 账本里**结构上**无法进入校准的命题总数（不限已结算的）。
            # 这是那个"等远见多跑几轮就好了"的说法必须被替换掉的依据。
            "legacy_proposition_total": self.legacy_proposition_total(),
            "resolved_binary": main["resolved_binary"],
            "open_total": self._open_total(),
            "hit_rate": main["hit_rate"],
            "false_positive_rate": main["false_positive_rate"],
            "brier": main["brier"],
            # 按来源分列：不要合并。auto 是"机器填的概率"，unknown 是"来源不可考的
            # 历史行"，把它们并进人的命中率里，那个数字就不再是人的成绩。
            "by_source": by_source,
            # 结算来源分列：batch 里绝大多数是"没来得及看"（indeterminate），
            # 与逐条判定的 indeterminate 性质不同。
            "resolved_by_counts": resolved_by_counts,
            "brier_series": brier_series,
            "unknown_week_count": unknown_weeks,
            # 按类别准确率也只算人工的（与主指标同口径）。
            "by_category": by_category,
        }

    @staticmethod
    def _hit_rate_by_category(confident_rows) -> dict:
        """按类别算"命中率"（>=50% 的预测里真的发生了的比例）。

        只接受已经筛过的（人工来源 + 二元结果 + 概率 >= 0.5）行，
        调用方负责口径，这里只做分组，避免两处各写一遍口径。
        """
        buckets = {}
        for row in confident_rows:
            buckets.setdefault(row["category_key"] or "general", []).append(row)
        return {
            category: round(
                sum(1 for row in subset if row["outcome"] == "occurred") / len(subset),
                10,
            )
            for category, subset in sorted(buckets.items())
            if subset
        }

    @staticmethod
    def _calibration_metrics(rows) -> dict:
        """一组已结算且可校准的行的命中率 / 误报率 / Brier。分母为 0 时给 None。"""
        binary = [row for row in rows if row["outcome"] in {"occurred", "not_occurred"}]
        confident = [row for row in binary if float(row["probability"]) >= 0.5]
        hits = [row for row in confident if row["outcome"] == "occurred"]
        miss = [row for row in confident if row["outcome"] == "not_occurred"]
        scored = [row for row in rows if row["brier_score"] is not None]
        return {
            "resolved_total": len(rows),
            "resolved_binary": len(binary),
            "confident_total": len(confident),
            "hit_total": len(hits),
            "miss_total": len(miss),
            "hit_rate": round(len(hits) / len(confident), 10) if confident else None,
            "false_positive_rate": (
                round(len(miss) / len(confident), 10) if confident else None
            ),
            "brier": (
                round(sum(float(row["brier_score"]) for row in scored) / len(scored), 10)
                if scored
                else None
            ),
        }

    def legacy_proposition_total(self) -> int:
        """账本里有多少条命题**结构上无法进入校准**（缺「可观测事实」这一要素）。

        v1.3 之前的 8,607 条全在这里：它们没有 `observable_signals`，而
        `_calibratable` 把它当成硬门槛 —— **补结算也不会让它们进入校准**。
        面板必须把这个数说出来并明说这一点，否则用户会以为"多跑几轮就好了"。
        """
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                SELECT v.content FROM forecasts f
                JOIN forecast_versions v ON v.forecast_id = f.forecast_id
                JOIN (SELECT forecast_id, MAX(version) AS latest_version
                      FROM forecast_versions GROUP BY forecast_id) latest
                  ON latest.forecast_id = v.forecast_id
                 AND latest.latest_version = v.version
                """
            )
            return sum(1 for row in cursor if not _is_settleable_content(row["content"]))

    def _open_total(self):
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM forecasts WHERE status = 'open'"
            ).fetchone()[0]

    def progress_summary(self, now=None):
        """Lightweight counts for the Action Home 'prediction progress' card.

        口径（v1.4，R-04）：`hit_total` / `miss_total` **只统计
        `confirmed_by='user'`** —— 首页把这两个数标成「我的预测命中 / 失误」，那是
        人的成绩；机器自动确认的条目不能算进去，否则那两栏显示的是"系统自己跟自己对账"。
        机器与来源不明的那部分按来源**分列**给出（`by_source`），不合并、也不隐藏。

        Returns:
          - resolved_total: 已记录结果的预测数（不过滤来源）
          - hit_total / miss_total: **你**给 >=50% 且命中 / 未命中的条数
          - by_source: 三个来源各自的 resolved_binary / hit_total / miss_total
          - due_this_week: 7 天内到期的开放预测
          - overdue_total: 已过期未结算的开放预测
        """
        now = now or datetime.now(timezone.utc)
        today_str = now.date().isoformat()
        week_later_str = (now + timedelta(days=7)).date().isoformat()
        with self.database.connect() as connection:
            binary_rows = connection.execute(
                """
                SELECT COALESCE(NULLIF(r.confirmed_by, ''), f.confirmed_by, 'unknown')
                         AS confirmed_by,
                       r.outcome, r.probability
                FROM resolutions r
                LEFT JOIN forecasts f ON f.forecast_id = r.forecast_id
                WHERE r.outcome IN ('occurred', 'not_occurred')
                """
            ).fetchall()
            due_rows = connection.execute(
                """
                SELECT forecast_id, window_end FROM forecasts
                WHERE status='open' AND window_end >= ? AND window_end <= ?
                """,
                (today_str, week_later_str),
            ).fetchall()
            overdue_rows = connection.execute(
                """
                SELECT forecast_id FROM forecasts
                WHERE status='open' AND window_end < ?
                """,
                (today_str,),
            ).fetchall()
        by_source = {}
        for source in ("user", "auto", "unknown"):
            subset = [r for r in binary_rows if r["confirmed_by"] == source]
            confident = [r for r in subset if float(r["probability"]) >= 0.5]
            by_source[source] = {
                "resolved_binary": len(subset),
                "hit_total": sum(1 for r in confident if r["outcome"] == "occurred"),
                "miss_total": sum(1 for r in confident if r["outcome"] == "not_occurred"),
            }
        # 主指标 = 人工来源。**不要在这里退回成跨来源聚合** —— 首页那两栏写的是
        # "历史命中 / 历史失误"，混进机器填的条目，它们就不再是人的成绩。
        main = by_source["user"]
        return {
            "resolved_total": len(binary_rows),
            "hit_total": main["hit_total"],
            "miss_total": main["miss_total"],
            "by_source": by_source,
            "due_this_week": len(due_rows),
            "overdue_total": len(overdue_rows),
        }

    def list_overdue(self, now=None):
        """P3: 返回已过期但未结算的预测（status='open' 且 window_end < 今天）。

        供前端提醒用户"有N条预测到期该结算了"，驱动预测闭环。
        """
        now = now or datetime.now(timezone.utc)
        today_str = now.date().isoformat()
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT f.forecast_id, f.status, f.window_end, f.category,
                       v.probability, v.content
                FROM forecasts f
                JOIN forecast_versions v ON v.forecast_id = f.forecast_id
                JOIN (
                    SELECT forecast_id, MAX(version) AS latest_version
                    FROM forecast_versions GROUP BY forecast_id
                ) latest ON latest.forecast_id = v.forecast_id
                         AND latest.latest_version = v.version
                WHERE f.status='open' AND f.window_end < ?
                ORDER BY f.window_end
                """,
                (today_str,),
            ).fetchall()
        result = []
        for row in rows:
            fields = parse_frontmatter(row["content"])
            result.append({
                "forecast_id": row["forecast_id"],
                "window_end": row["window_end"],
                "category": row["category"] or "general",
                "probability": row["probability"],
                "title": fields.get("title", row["forecast_id"]),
                "resolution_criteria": fields.get("resolution_criteria", ""),
            })
        return result
