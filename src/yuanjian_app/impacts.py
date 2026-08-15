"""Local-only mapping from public judgments to private interests."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from .forecasts import ALLOWED_PROBABILITIES


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
    if score < 0.80:
        return "L3"
    return "L4"


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
            "recommended_action": "继续收集执行证据；人工确认后才进入正式预测账本。",
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
            importance = max(1, min(int(interest["importance"]), 5)) / 5
            components = {
                "evidence": evidence,
                "confidence": confidence,
                "importance": importance,
                "exposure": exposure,
                "urgency": urgency,
            }
            score = round(
                evidence * 0.25
                + confidence * 0.20
                + importance * 0.25
                + exposure * 0.20
                + urgency * 0.10,
                6,
            )
            alert = _alert_level(score)
            if cluster["evidence_level"] == "E1" and alert == "L4":
                alert = "L3"
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
                SELECT p.impact_id, p.candidate_json, p.updated_at, i.category
                FROM personal_impacts p
                JOIN interest_objects i ON i.object_id = p.interest_id
                WHERE p.candidate_json NOT LIKE '%confirmed_forecast_id%'
                    AND (p.muted_until IS NULL OR p.muted_until < ?)
                ORDER BY p.impact_score DESC, p.updated_at DESC
                LIMIT ?
                """,
                (_iso(self.now()), max(1, min(int(limit), 100))),
            ).fetchall()
        output = []
        for row in rows:
            candidate = json.loads(row["candidate_json"] or "{}")
            output.append(
                {
                    "id": row["impact_id"],
                    "statement": candidate.get("title", ""),
                    "summary": candidate.get("title", ""),
                    "category": row["category"] or "general",
                    "window_end": candidate.get("window_end", ""),
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
        forecast_id = "F-CAND-" + impact_id.removeprefix("P-")[:20].upper()
        result = self.forecast_service.create_forecast(
            {
                "forecast_id": forecast_id,
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
