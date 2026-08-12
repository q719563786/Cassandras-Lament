"""Persistent signal inbox and conservative danger triage."""

import json
from datetime import datetime, timezone
from uuid import uuid4

from .rules import create_candidate


DOMAIN_LABELS = {
    "health": "健康安全",
    "cashflow": "现金流",
    "work": "工作收入",
    "policy": "政策权益",
    "family": "家庭关系",
    "assets": "资产负债",
    "opportunity": "机会成长",
}


class SignalService:
    """Save observed facts separately from forecasts and generated advice."""

    def __init__(self, database, interests):
        self.database = database
        self.interests = interests

    def ingest(self, text, occurred_at, source_type="manual", source_ref=""):
        summary = " ".join(str(text).replace("\x00", "").split())
        if not summary:
            raise ValueError("信号内容不能为空")
        candidate = create_candidate(summary, str(occurred_at or ""))
        domains = candidate["domains"]
        alert_level = self._alert_level(summary, candidate)
        interest_ids = self._matching_interest_ids(domains)
        why = self._why(domains, interest_ids)
        action = self._action(alert_level, candidate)
        item = {
            "signal_id": f"S-{uuid4().hex[:12]}",
            "received_at": datetime.now(timezone.utc).isoformat(),
            "occurred_at": str(occurred_at or ""),
            "source_type": str(source_type or "manual"),
            "source_ref": str(source_ref or ""),
            "summary": summary,
            "domains": domains,
            "reliability": "unverified" if source_type == "manual" else "source-reported",
            "alert_level": alert_level,
            "status": "new",
            "interest_ids": interest_ids,
            "candidate": candidate,
            "why_it_matters": why,
            "recommended_action": action,
        }
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO signals(
                    signal_id, received_at, occurred_at, source_type, source_ref,
                    summary, domains_json, reliability, alert_level, status,
                    interest_ids_json, candidate_json, why_it_matters, recommended_action
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["signal_id"], item["received_at"], item["occurred_at"],
                    item["source_type"], item["source_ref"], item["summary"],
                    json.dumps(domains, ensure_ascii=False), item["reliability"],
                    alert_level, item["status"], json.dumps(interest_ids),
                    json.dumps(candidate, ensure_ascii=False), why, action,
                ),
            )
            connection.execute(
                "INSERT INTO audit_log(occurred_at, action, object_type, object_id, details_json) VALUES (?, 'signal.ingest', 'signal', ?, ?)",
                (datetime.now(timezone.utc).isoformat(), item["signal_id"], json.dumps({"alert_level": alert_level})),
            )
        return item

    def list_signals(self, status=None):
        sql = "SELECT * FROM signals"
        params = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY received_at DESC, signal_id DESC"
        with self.database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["domains"] = json.loads(item.pop("domains_json"))
            item["interest_ids"] = json.loads(item.pop("interest_ids_json"))
            item["candidate"] = json.loads(item.pop("candidate_json"))
            result.append(item)
        return result

    def high_alerts(self):
        return [item for item in self.list_signals("new") if item["alert_level"] in {"L3", "L4"}]

    @staticmethod
    def _alert_level(text, candidate):
        if candidate.get("direction") == "benefit":
            return "L1"
        urgent = any(word in text for word in ("立即", "马上", "紧急", "明天", "今天", "截止", "逾期", "停工"))
        high_domain = bool({"health", "cashflow"}.intersection(candidate["domains"]))
        large_amount = max(candidate["amounts"], default=0) >= 10000
        if urgent and (high_domain or large_amount):
            return "L4"
        if high_domain or large_amount:
            return "L3"
        if candidate["can_register_forecast"]:
            return "L2"
        return "L1"

    def _matching_interest_ids(self, domains):
        wanted = set(domains)
        return [
            item["object_id"]
            for item in self.interests.list_objects()
            if item["category"] in wanted and item["status"] == "active"
        ]

    @staticmethod
    def _why(domains, interest_ids):
        labels = [DOMAIN_LABELS[domain] for domain in domains if domain in DOMAIN_LABELS]
        if not labels:
            return "目前未发现与已登记利益类别的直接传导关系。"
        return f"该信号可能影响{'、'.join(labels)}，已关联{len(interest_ids)}个利益对象。"

    @staticmethod
    def _action(alert_level, candidate):
        if candidate.get("direction") == "benefit" and "cashflow" in candidate["domains"]:
            return "核对金额和到账记录，优先补足现金缓冲，不要立刻增加非必要支出。"
        if alert_level == "L4":
            return "立即核实事实、金额和最晚处理时间，并准备可撤回的应对方案。"
        if alert_level == "L3":
            return "今天内补充反对证据和最坏情景，确认是否需要登记正式预测。"
        if candidate["can_register_forecast"]:
            return "补充期限和结算标准后，再决定是否登记正式预测。"
        return "保留为观察信号；出现新证据时再升级。"
