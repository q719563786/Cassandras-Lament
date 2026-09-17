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
                       f.confirmed_by, v.version, v.probability, v.content
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
                "version": row["version"],
                "probability": row["probability"],
                "title": fields.get("title", row["forecast_id"]),
                "statement": fields.get("title", row["forecast_id"]),
                "created_at": fields.get("created_at", ""),
                "confidence": fields.get("confidence", "unknown"),
                "alert_level": fields.get("alert_level", "L1"),
                "resolution_criteria": fields.get("resolution_criteria", ""),
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
        样本 = 同类别**所有已结算且结果为二元**的预测。

        ⚠ 曾经这里写过 ，是错的：该字段 DEFAULT
        就是 'unknown'（v1.1 加它时本意是标历史行来源不可考），于是**任何不经
        confirm_candidate 落库的预测都会被排除**，样本永远为 0，基准率永远拿不到 ——
        机制自己把自己锁死。同一个模式在本文件出现过三次（confirmed_by、校准口径、
        以及 observable_signals 被白名单吞掉）。
        真正的质量闸是 ：它已保证该条有过**二元**结果，
        模糊到无法判定的命题（outcome=indeterminate）本来就被排除在外。
        基准率是**经验频率**，来源可不可考不改变频率本身。
        """
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS n,
                       SUM(CASE WHEN r.outcome='occurred' THEN 1 ELSE 0 END) AS hits
                FROM resolutions r
                JOIN forecasts f ON f.forecast_id = r.forecast_id
                WHERE f.category = ?
                  AND r.brier_score IS NOT NULL
                  AND r.outcome IN ('occurred','not_occurred')
                """,
                (category or "general",),
            ).fetchone()
        sample = row["n"] if row else 0
        if sample < 5:
            return (None, sample)
        return (round(row["hits"] / sample, 4), sample)

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
                " confirmed_by, base_rate, base_rate_sample)"
                " VALUES (?, 'open', ?, ?, ?, ?, ?)",
                (
                    card["forecast_id"],
                    card["window_end"],
                    card["category"],
                    card.get("confirmed_by") or "unknown",
                    card.get("base_rate"),
                    card.get("base_rate_sample", 0),
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

    def resolve(self, forecast_id, outcome, resolved_at, note):
        """Resolve a forecast once and score binary outcomes."""
        outcomes = {"occurred": 1.0, "not_occurred": 0.0}
        allowed = {*outcomes, "partial", "indeterminate"}
        if outcome not in allowed:
            raise ValueError("不支持的结算结果")
        resolved_at = str(resolved_at or "").strip()
        if resolved_at and not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?", resolved_at
        ):
            raise ValueError("结算日期格式无效，应为年月日")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT probability FROM forecast_versions WHERE forecast_id = ? ORDER BY version DESC LIMIT 1",
                (forecast_id,),
            ).fetchone()
            if row is None:
                raise KeyError(forecast_id)
            probability = row[0]
            brier = None if outcome not in outcomes else round((probability - outcomes[outcome]) ** 2, 10)
            connection.execute(
                "INSERT INTO resolutions(forecast_id, outcome, resolved_at, probability, brier_score) VALUES (?, ?, ?, ?, ?)",
                (forecast_id, outcome, resolved_at, probability, brier),
            )
            connection.execute(
                "UPDATE forecasts SET status = 'resolved' WHERE forecast_id = ?",
                (forecast_id,),
            )
            connection.execute(
                "INSERT INTO audit_log(occurred_at, action, object_type, object_id, details_json) VALUES (?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    "forecast.resolve",
                    "forecast",
                    forecast_id,
                    json.dumps({"outcome": outcome, "note": note}, ensure_ascii=False),
                ),
            )
        return {"forecast_id": forecast_id, "outcome": outcome, "brier_score": brier}

    def score_summary(self):
        """Return aggregate binary calibration statistics.

        判据与 `calibration_summary` 一致：**命题可结算**才进 Brier 平均。
        不用 `confirmed_by != 'unknown'` —— 那个字段默认就是 'unknown'，
        会把新建的正常预测也一并误排除。
        """
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT r.brier_score,
                       f.base_rate,
                       (SELECT v.content FROM forecast_versions v
                         WHERE v.forecast_id = f.forecast_id
                         ORDER BY v.version DESC LIMIT 1) AS latest_content
                FROM resolutions r
                JOIN forecasts f ON f.forecast_id = r.forecast_id
                WHERE r.brier_score IS NOT NULL
                """
            ).fetchall()
        usable = [r for r in rows if self._calibratable(r)]
        row = (
            len(usable),
            (sum(float(r["brier_score"]) for r in usable) / len(usable)) if usable else None,
        )
        return {
            "resolved_binary": row[0],
            "brier_score": None if row[1] is None else round(row[1], 10),
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

        口径（v1.2 起）：`resolved_total` 是**已结算总数**（不过滤），
        Brier 只用**命题可结算**的那些；被排除的数量单独给出，
        面板上分开显示，不含混。

        Philosophy: never fabricate conclusions — denominators of zero
        produce null instead of 0.
        """
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT f.forecast_id, f.category, r.outcome, r.resolved_at,
                       r.probability, r.brier_score, f.base_rate,
                       (SELECT v.content FROM forecast_versions v
                         WHERE v.forecast_id = f.forecast_id
                         ORDER BY v.version DESC LIMIT 1) AS latest_content
                FROM forecasts f
                JOIN resolutions r ON r.forecast_id = f.forecast_id
                """
            ).fetchall()
        resolved_total = len(rows)
        scored_rows = [row for row in rows if self._calibratable(row)]
        excluded_total = resolved_total - len(scored_rows)
        binary = [
            dict(row)
            for row in scored_rows
            if row["outcome"] in {"occurred", "not_occurred"}
        ]
        confident = [row for row in binary if float(row["probability"]) >= 0.5]
        hits = [row for row in confident if row["outcome"] == "occurred"]
        miss = [row for row in confident if row["outcome"] == "not_occurred"]
        scored = [row for row in scored_rows if row["brier_score"] is not None]
        overall_brier = (
            round(sum(float(row["brier_score"]) for row in scored) / len(scored), 10)
            if scored
            else None
        )
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
        by_category = {}
        categories = {row["category"] or "general" for row in confident}
        for category in categories:
            subset = [
                row for row in confident if (row["category"] or "general") == category
            ]
            if subset:
                by_category[category] = round(
                    sum(1 for row in subset if row["outcome"] == "occurred")
                    / len(subset),
                    10,
                )
        return {
            "resolved_total": len(rows),
            # 已结算但命题不可结算、因此不进 Brier 的条数。
            # 面板要把它和 resolved_total 分开显示 —— 用户看到的应该是
            # "已结算 12 条，其中 9 条可用于校准，3 条是旧口径已排除"，
            # 而不是一个被静默缩小的总数。
            "excluded_total": excluded_total,
            "resolved_binary": len(binary),
            "open_total": self._open_total(),
            "hit_rate": round(len(hits) / len(confident), 10) if confident else None,
            "false_positive_rate": (
                round(len(miss) / len(confident), 10) if confident else None
            ),
            "brier": overall_brier,
            "brier_series": brier_series,
            "unknown_week_count": unknown_weeks,
            "by_category": by_category,
        }

    def _open_total(self):
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM forecasts WHERE status = 'open'"
            ).fetchone()[0]

    def progress_summary(self, now=None):
        """Lightweight counts for the Action Home 'prediction progress' card.

        Returns only the four integers the home page needs:
          - resolved_total: predictions whose outcome is recorded
          - hit_total: predictions the user gave >=50% that came true
          - miss_total: predictions the user gave >=50% that didn't come true
          - due_this_week: open predictions whose window ends within 7 days
        """
        now = now or datetime.now(timezone.utc)
        today_str = now.date().isoformat()
        week_later_str = (now + timedelta(days=7)).date().isoformat()
        with self.database.connect() as connection:
            binary_rows = connection.execute(
                """
                SELECT f.forecast_id, r.outcome, r.probability
                FROM forecasts f
                JOIN resolutions r ON r.forecast_id = f.forecast_id
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
        confident = [r for r in binary_rows if float(r["probability"]) >= 0.5]
        hits = [r for r in confident if r["outcome"] == "occurred"]
        miss = [r for r in confident if r["outcome"] == "not_occurred"]
        return {
            "resolved_total": len(binary_rows),
            "hit_total": len(hits),
            "miss_total": len(miss),
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
