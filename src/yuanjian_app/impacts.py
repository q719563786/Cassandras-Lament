"""Local-only mapping from public judgments to private interests."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from .forecasts import ALLOWED_PROBABILITIES

_ALLOWED_PROB_LIST = sorted(ALLOWED_PROBABILITIES)


def _nearest_probability(value: float) -> float:
    """把任意概率映射到最近的固定档位（ALLOWED_PROBABILITIES）。"""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.50
    value = max(0.0, min(1.0, value))
    return min(_ALLOWED_PROB_LIST, key=lambda p: abs(p - value))

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
    "policy": {"policy": 1.0, "cashflow": 0.4, "work": 0.4},
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


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _alert_level(score):
    if score < 0.35:
        return "L1"
    if score < 0.55:
        return "L2"
    if score < 0.67:
        return "L3"
    return "L4"


# AI 在 personal_action 中明确判定"与该用户无关"的强信号。命中即说明事件虽有
# 公共层面重要性，但对用户本人无传导路径，应自动降级、不占L3/L4行动板——这是
# "系统自己过滤，不把判断推给用户"的关键一环。
_IRRELEVANT_PATTERNS = (
    r"与(你|您)(基本)?无(直接)?(关系|关联|交集)",
    r"与(你|您).{0,12}无关",
    r"(基本|大致|总体)无关",
    r"不(涉及|触及|作用于|影响)(你|您)",
    r"与(你|您)没有(直接)?(关系|关联|交集)",
    r"无需(为此|操作|行动|调整)",
)
_RELEVANT_PATTERN = r"(直接相关|与你直接相关|直接作用于|实质影响你)"


def _personal_relevance(judgment) -> float:
    """返回个人相关性系数：AI明确判无关→0.45（压到L2以下），否则1.0。"""
    text = str(judgment.get("personal_action") or "")
    if not text:
        return 1.0
    # 先看是否明确直接相关（优先，避免被"无关"字样误伤）
    if re.search(_RELEVANT_PATTERN, text):
        return 1.0
    for pattern in _IRRELEVANT_PATTERNS:
        if re.search(pattern, text):
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
        return dict(cluster), json.loads(judgment["content_json"])

    @staticmethod
    def _exposure(categories, interest_category):
        return max(
            (
                CATEGORY_EXPOSURE.get(category, {}).get(interest_category, 0.0)
                for category in categories
            ),
            default=0.0,
        )

    def _candidate(self, cluster, judgment, interest, impact_id):
        now = self.now().astimezone(timezone.utc)
        end = now + timedelta(days=_window_days(judgment.get("horizons", [])))
        return {
            "impact_id": impact_id,
            "title": f"{cluster['title']}将在观察期内影响{interest['name']}",
            "resolution_criteria": (
                f"截至{end.date().isoformat()}，依据公开执行信息或本地可核验记录，"
                f"判断该事件是否对{interest['name']}产生实际影响"
            ),
            "window_start": now.date().isoformat(),
            "window_end": end.date().isoformat(),
            "probability_low": float(judgment["probability_low"]),
            "probability_high": float(judgment["probability_high"]),
            "causal_chain": "\n".join(judgment.get("causal_chain", [])),
            "supporting_evidence": "\n".join(judgment.get("supporting_source_ids", [])),
            "opposing_evidence": "\n".join(judgment.get("uncertainties", [])),
            "falsification": "\n".join(judgment.get("down_triggers", [])),
            "recommended_action": "请在校准面板确认概率后记录为正式预测，到期后结算复盘。",
        }

    def _category_penalties(self):
        """Feedback-learning multipliers persisted by the learning consumer."""
        try:
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT value_json FROM runtime_state WHERE state_key=?",
                    ("learning.category_penalties",),
                ).fetchone()
            penalties = json.loads(row["value_json"]) if row else {}
        except Exception:
            penalties = {}
        return {
            str(key): max(0.5, min(float(value), 1.0))
            for key, value in penalties.items()
            if isinstance(value, (int, float))
        }

    def map_judgment(self, cluster_id: str, judgment_id: str) -> list[dict]:
        cluster, judgment = self._load(cluster_id, judgment_id)
        categories = tuple(judgment.get("impact_categories", ()))
        evidence = EVIDENCE_WEIGHTS.get(cluster["evidence_level"], 0.25)
        confidence = max(0.0, min(float(judgment.get("confidence", 0.0)), 1.0))
        urgency = _urgency(judgment.get("horizons", ()))
        penalties = self._category_penalties()
        now = _iso(self.now())
        results = []
        for interest in self.interest_service.list_objects():
            if interest["status"] != "active":
                continue
            exposure = self._exposure(categories, interest["category"])
            if exposure <= 0:
                continue
            exposure = round(exposure * penalties.get(interest["category"], 1.0), 6)
            # 低暴露度事件对该利益影响微弱，不生成候选预测
            if exposure < 0.3:
                continue
            importance = max(1, min(int(interest["importance"]), 5)) / 5
            components = {
                "evidence": evidence,
                "confidence": confidence,
                "importance": importance,
                "exposure": exposure,
                "urgency": urgency,
            }
            base_score = (
                evidence * 0.25
                + confidence * 0.20
                + importance * 0.25
                + exposure * 0.20
                + urgency * 0.10
            )
            # 结合AI对"用户本人相关性"的结论做升降级：判无关则压到行动板之下
            relevance = _personal_relevance(judgment)
            components["personal_relevance"] = relevance
            score = round(max(0.0, min(base_score * relevance, 1.0)), 6)
            alert = _alert_level(score)
            with self.database.connect() as connection:
                existing = connection.execute(
                    """
                    SELECT impact_id,candidate_json FROM personal_impacts
                    WHERE cluster_id=? AND judgment_id=? AND interest_id=?
                    """,
                    (cluster_id, judgment_id, interest["object_id"]),
                ).fetchone()
                impact_id = existing["impact_id"] if existing else "P-" + uuid.uuid4().hex
                candidate = self._candidate(cluster, judgment, interest, impact_id)
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
            # P1: L4高影响候选自动确认——用概率区间中值映射到最近固定档位；
            # L3及以下留待用户校准，避免中低影响事件被批量自动确认。
            if alert == "L4" and not candidate.get("confirmed_forecast_id"):
                try:
                    midpoint = (
                        float(candidate.get("probability_low", 0.5))
                        + float(candidate.get("probability_high", 0.7))
                    ) / 2
                    nearest = _nearest_probability(midpoint)
                    forecast = self.confirm_candidate(impact_id, nearest)
                    candidate["confirmed_forecast_id"] = forecast["forecast_id"]
                    candidate["confirmed_probability"] = nearest
                except Exception:
                    pass  # 自动确认失败不阻断映射流程，候选仍保留为待确认状态
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
        """清理v1.0自动确认产生的垃圾F-CAND预测，重置为待确认状态。
        旧版本在map_judgment中自动将候选预测写入正式账本，产生大量未经验证的F-CAND记录。
        此方法删除所有F-CAND-*预测，并清除对应impact的confirmed标记，让用户重新确认。
        返回清理的预测数量。
        """
        purged = 0
        with self.database.connect() as connection:
            garbage = connection.execute(
                "SELECT forecast_id FROM forecasts WHERE forecast_id LIKE 'F-CAND-%'"
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
                    except Exception:
                        pass
                connection.execute("DELETE FROM forecasts WHERE forecast_id=?", (fid,))
                purged += 1
            connection.commit()
        return purged

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
                }
            )
        return output

    def confirm_candidate(self, impact_id: str, probability: float) -> dict:
        try:
            probability = round(float(probability), 2)
        except (TypeError, ValueError):
            raise ValueError("概率必须选择固定档位") from None
        if probability not in ALLOWED_PROBABILITIES:
            raise ValueError("概率必须选择固定档位")
        candidate = self.candidate_forecast(impact_id)
        if candidate.get("confirmed_forecast_id"):
            return self.forecast_service.get_forecast(candidate["confirmed_forecast_id"])
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
        """全自动确认所有未确认的L3/L4候选预测，无需用户手动操作。"""
        confirmed = 0
        skipped = 0
        errors = []
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT impact_id, candidate_json, alert_level
                FROM personal_impacts
                WHERE candidate_json IS NOT NULL AND candidate_json != ''
                  AND alert_level IN ('L3','L4')
                  AND user_label NOT IN ('false_positive','dismissed')
                """
            ).fetchall()
        for row in rows:
            try:
                candidate = json.loads(row["candidate_json"] or "{}")
                if candidate.get("confirmed_forecast_id"):
                    skipped += 1
                    continue
                low = float(candidate.get("probability_low", 0.3))
                high = float(candidate.get("probability_high", 0.7))
                probability = _nearest_probability((low + high) / 2)
                self.confirm_candidate(row["impact_id"], probability)
                confirmed += 1
            except Exception as exc:
                errors.append(f"{row['impact_id']}: {exc}")
                skipped += 1
        return {"confirmed": confirmed, "skipped": skipped, "errors": errors[:10], "total": len(rows)}
