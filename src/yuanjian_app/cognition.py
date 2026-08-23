"""Local event clusters and evidence grading for the external radar."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from .clustering import ClusterText, should_merge
from .text_cleaning import plain_text
from .external_sources import normalize_published_at


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _domain(url: str) -> str:
    return (urlsplit(url).hostname or "unknown").casefold()


def _json_object(value: str) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _categories(title: str, summary: str) -> list[str]:
    text = f"{title} {summary}".casefold()
    rules = (
        ("health", ("医保", "医疗", "医院", "药品", "手术", "健康")),
        ("finance", ("银行", "利率", "贷款", "信用", "黄金", "比特币", "保险")),
        ("employment", ("就业", "工资", "社保", "公积金", "招聘")),
        ("safety", ("安全", "事故", "灾害", "台风", "暴雨", "火灾")),
        ("policy", ("政策", "新规", "条例", "通知", "办法")),
        ("technology", ("人工智能", "ai", "软件", "网络", "数据")),
    )
    found = [category for category, words in rules if any(word in text for word in words)]
    return found or ["general"]


def _personal_advice(category: str, recommended_action: str) -> str:
    action = plain_text(recommended_action, 240)
    internal_terms = ("正式预测账本", "收集执行证据", "人工确认")
    if action and not any(term in action for term in internal_terms):
        return action
    category = str(category or "general").casefold()
    if category in {"cashflow", "asset", "liability", "income", "expense", "finance"}:
        return "先核对近期必须支付的金额和日期，暂缓非必要支出，保留现金。"
    if category in {"health", "protection"}:
        return "先确认费用、医保结算和保险材料并保留票据；身体异常及时就医。"
    if category in {"work", "employment"}:
        return "先确认收入、报销和工作安排是否变化，保留书面记录，暂不做不可逆决定。"
    if category == "family":
        return "先和家人确认最近 7 天的分工与必须事项，优先处理不能拖延的部分。"
    if category == "safety":
        return "先远离可能的危险并核对官方通知；紧急情况联系当地应急部门或报警。"
    return "先核实正式来源和适用期限，暂缓不可逆决定，并按时间窗口复查。"


class CognitionService:
    def __init__(self, database, now=lambda: datetime.now(timezone.utc)):
        self.database = database
        self.now = now

    def _load_item(self, connection, item_id):
        row = connection.execute(
            "SELECT * FROM external_items WHERE item_id=?", (item_id,)
        ).fetchone()
        if row is None:
            raise KeyError(item_id)
        return row

    def _source_facts(self, connection, cluster_id):
        rows = connection.execute(
            """
            SELECT ci.item_id, eis.source_id, eis.url, s.config_json,
                   e.canonical_url, e.source_id AS original_source_id
            FROM event_cluster_items ci
            JOIN external_items e ON e.item_id=ci.item_id
            LEFT JOIN external_item_sources eis ON eis.item_id=ci.item_id
            LEFT JOIN external_sources s ON s.source_id=eis.source_id
            WHERE ci.cluster_id=?
            ORDER BY ci.item_id, eis.source_id
            """,
            (cluster_id,),
        ).fetchall()
        facts = []
        for row in rows:
            source_id = row["source_id"] or row["original_source_id"]
            url = row["url"] or row["canonical_url"]
            config = _json_object(row["config_json"])
            facts.append(
                {
                    "item_id": row["item_id"],
                    "source_id": source_id,
                    "domain": _domain(url),
                    "primary": bool(config.get("primary_source", False)),
                }
            )
        return facts

    def _recalculate(self, connection, cluster_id, previous_hash):
        item_rows = connection.execute(
            """
            SELECT e.item_id, e.content_hash, e.published_at, e.first_seen_at
            FROM event_cluster_items ci
            JOIN external_items e ON e.item_id=ci.item_id
            WHERE ci.cluster_id=? ORDER BY e.item_id
            """,
            (cluster_id,),
        ).fetchall()
        facts = self._source_facts(connection, cluster_id)
        domains = {fact["domain"] for fact in facts}
        primary_sources = {
            fact["source_id"] for fact in facts if fact["primary"]
        }
        evidence_payload = {
            "items": [
                {
                    "item_id": row["item_id"],
                    "content_hash": row["content_hash"],
                    "observed_at": row["published_at"] or row["first_seen_at"],
                }
                for row in item_rows
            ],
            "sources": facts,
        }
        evidence_hash = hashlib.sha256(
            json.dumps(
                evidence_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        if primary_sources and len(domains) >= 3:
            level = "E4"
        elif primary_sources and len(domains) >= 2:
            level = "E3"
        elif len(domains) >= 2:
            level = "E2"
        else:
            level = "E1"
        changed = evidence_hash != previous_hash
        connection.execute(
            """
            UPDATE event_clusters SET evidence_level=?, evidence_hash=?,
                independent_domains=?, primary_source_count=?,
                needs_judgment=CASE WHEN ? THEN 1 ELSE needs_judgment END,
                updated_at=? WHERE cluster_id=?
            """,
            (
                level,
                evidence_hash,
                len(domains),
                len(primary_sources),
                1 if changed else 0,
                _iso(self.now()),
                cluster_id,
            ),
        )
        return level, evidence_hash, changed

    def process_item(self, item_id: str) -> dict:
        with self.database.connect() as connection:
            item = self._load_item(connection, item_id)
            existing = connection.execute(
                "SELECT cluster_id FROM event_cluster_items WHERE item_id=?",
                (item_id,),
            ).fetchone()
            observed_text = normalize_published_at(item["published_at"]) or item[
                "first_seen_at"
            ]
            observed_at = _parse_time(observed_text)
            decision = None
            if existing:
                cluster_id = existing["cluster_id"]
            else:
                best = None
                candidates = connection.execute(
                    "SELECT * FROM event_clusters WHERE status='active' ORDER BY last_seen_at DESC"
                ).fetchall()
                current = ClusterText(item["title"], item["summary"], observed_at)
                for candidate in candidates:
                    comparison = should_merge(
                        current,
                        ClusterText(
                            candidate["title"],
                            candidate["summary"],
                            _parse_time(candidate["last_seen_at"]),
                        ),
                    )
                    if comparison.merge and (best is None or comparison.score > best.score):
                        best = comparison
                        cluster_id = candidate["cluster_id"]
                decision = best
                if decision is None:
                    cluster_id = "C-" + uuid.uuid4().hex
                    timestamp = _iso(self.now())
                    connection.execute(
                        """
                        INSERT INTO event_clusters(
                            cluster_id,title,summary,first_seen_at,last_seen_at,
                            evidence_hash,categories_json,created_at,updated_at
                        ) VALUES (?,?,?,?,?,'',?,?,?)
                        """,
                        (
                            cluster_id,
                            item["title"],
                            item["summary"],
                            _iso(observed_at),
                            _iso(observed_at),
                            json.dumps(_categories(item["title"], item["summary"])),
                            timestamp,
                            timestamp,
                        ),
                    )
                    similarity_score = 1.0
                    merge_reason = "new_cluster"
                    shared_entities = ()
                else:
                    similarity_score = decision.score
                    merge_reason = decision.reason
                    shared_entities = decision.shared_entities
                    connection.execute(
                        """
                        UPDATE event_clusters SET
                            first_seen_at=MIN(first_seen_at, ?),
                            last_seen_at=MAX(last_seen_at, ?), updated_at=?
                        WHERE cluster_id=?
                        """,
                        (_iso(observed_at), _iso(observed_at), _iso(self.now()), cluster_id),
                    )

                source_rows = connection.execute(
                    """
                    SELECT eis.url, s.config_json FROM external_item_sources eis
                    LEFT JOIN external_sources s ON s.source_id=eis.source_id
                    WHERE eis.item_id=? ORDER BY eis.source_id
                    """,
                    (item_id,),
                ).fetchall()
                source_domain = _domain(
                    source_rows[0]["url"] if source_rows else item["canonical_url"]
                )
                is_primary = any(
                    bool(_json_object(row["config_json"]).get("primary_source"))
                    for row in source_rows
                )
                connection.execute(
                    """
                    INSERT INTO event_cluster_items(
                        cluster_id,item_id,similarity,merge_reason,source_domain,
                        is_primary,added_at
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        cluster_id,
                        item_id,
                        similarity_score,
                        merge_reason,
                        source_domain,
                        1 if is_primary else 0,
                        _iso(self.now()),
                    ),
                )
                for entity in shared_entities:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO event_entities(
                            entity_id,cluster_id,name,normalized_name,category,confidence
                        ) VALUES (?,?,?,?,?,?)
                        """,
                        (
                            "N-" + uuid.uuid4().hex,
                            cluster_id,
                            entity,
                            entity.casefold(),
                            "shared_term",
                            similarity_score,
                        ),
                    )

            cluster = connection.execute(
                "SELECT evidence_hash FROM event_clusters WHERE cluster_id=?",
                (cluster_id,),
            ).fetchone()
            level, evidence_hash, changed = self._recalculate(
                connection, cluster_id, cluster["evidence_hash"]
            )
            state = connection.execute(
                "SELECT needs_judgment FROM event_clusters WHERE cluster_id=?",
                (cluster_id,),
            ).fetchone()
        return {
            "cluster_id": cluster_id,
            "evidence_level": level,
            "evidence_hash": evidence_hash,
            "changed": changed,
            "needs_judgment": bool(state["needs_judgment"]),
        }

    def backfill_unclustered(self, limit: int = 1000) -> dict:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT e.item_id FROM external_items e
                LEFT JOIN event_cluster_items ci ON ci.item_id=e.item_id
                WHERE ci.item_id IS NULL
                ORDER BY COALESCE(e.published_at,e.first_seen_at), e.item_id
                LIMIT ?
                """,
                (max(1, min(int(limit), 10000)),),
            ).fetchall()
        for row in rows:
            self.process_item(row["item_id"])
        return {"processed": len(rows), "remaining": self._unclustered_count()}

    def _unclustered_count(self):
        with self.database.connect() as connection:
            return connection.execute(
                """
                SELECT COUNT(*) FROM external_items e
                LEFT JOIN event_cluster_items ci ON ci.item_id=e.item_id
                WHERE ci.item_id IS NULL
                """
            ).fetchone()[0]

    def list_clusters_page(
        self,
        limit: int = 10,
        offset: int = 0,
        query: str = "",
        category: str = "",
        evidence: str = "",
        needs_judgment=None,
    ) -> dict:
        limit = int(limit)
        offset = int(offset)
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("分页参数无效")
        evidence = str(evidence or "").strip().upper()
        if evidence not in {"", "E1", "E2", "E3", "E4"}:
            raise ValueError("证据等级无效")
        if needs_judgment not in {None, True, False}:
            raise ValueError("待研判筛选无效")
        clauses = []
        values = []
        query = plain_text(query, max_length=100)
        category = str(category or "").strip().casefold()
        if query:
            clauses.append("(c.title LIKE ? OR c.summary LIKE ?)")
            like = f"%{query}%"
            values.extend((like, like))
        if category:
            clauses.append("c.categories_json LIKE ?")
            values.append(f'%"{category}"%')
        if evidence:
            clauses.append("c.evidence_level=?")
            values.append(evidence)
        if needs_judgment is not None:
            clauses.append("c.needs_judgment=?")
            values.append(int(needs_judgment))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.database.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM event_clusters c{where}", values
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT c.*, COUNT(ci.item_id) AS item_count
                FROM event_clusters c
                LEFT JOIN event_cluster_items ci ON ci.cluster_id=c.cluster_id
                {where}
                GROUP BY c.cluster_id
                ORDER BY c.last_seen_at DESC LIMIT ? OFFSET ?
                """,
                (*values, limit, offset),
            ).fetchall()
        return {
            "items": [self._cluster_dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def list_clusters(self, limit: int = 100) -> list[dict]:
        safe_limit = max(1, min(int(limit), 100))
        return self.list_clusters_page(limit=safe_limit)["items"]

    def get_cluster(self, cluster_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM event_clusters WHERE cluster_id=?", (cluster_id,)
            ).fetchone()
            if row is None:
                raise KeyError(cluster_id)
            items = connection.execute(
                """
                SELECT ci.*, e.title, e.summary, e.canonical_url, e.published_at
                FROM event_cluster_items ci
                JOIN external_items e ON e.item_id=ci.item_id
                WHERE ci.cluster_id=? ORDER BY ci.added_at, ci.item_id
                """,
                (cluster_id,),
            ).fetchall()
            entities = connection.execute(
                "SELECT * FROM event_entities WHERE cluster_id=? ORDER BY confidence DESC,name",
                (cluster_id,),
            ).fetchall()
        result = self._cluster_dict(row)
        result["items"] = []
        for item in items:
            clean_item = dict(item)
            clean_item["title"] = plain_text(clean_item.get("title"), max_length=300)
            clean_item["summary"] = plain_text(clean_item.get("summary"), max_length=2000)
            result["items"].append(clean_item)
        result["entities"] = [dict(entity) for entity in entities]
        return result

    @staticmethod
    def _cluster_dict(row):
        result = dict(row)
        result["title"] = plain_text(result.get("title"), max_length=300)
        result["summary"] = plain_text(result.get("summary"), max_length=2000)
        result["categories"] = json.loads(result.pop("categories_json"))
        result["needs_judgment"] = bool(result["needs_judgment"])
        return result


class CognitionController:
    """Coordinates clustering, judgments, private impacts and notifications."""

    def __init__(
        self,
        database,
        cognition,
        trends,
        judgment_queue,
        impacts,
        notifications,
        ai_settings,
        now=lambda: datetime.now(timezone.utc),
    ):
        self.database = database
        self.cognition = cognition
        self.trends = trends
        self.judgment_queue = judgment_queue
        self.impacts = impacts
        self.notifications = notifications
        self.ai_settings = ai_settings
        self.now = now
        # Wall-clock cutoff: only notify for judgments created at or after
        # this timestamp. Prevents the first process_once() run from firing
        # one notification per historical judgment the database inherited
        # from a previous install (the regression that dumped 200+ noise
        # notifications on a v4->v5 upgrade). Mapped impacts still update
        # unconditionally so private-impact scoring stays current.
        self._notify_since = self.now()

    def _bundle(self, cluster_id):
        from .judgments import build_public_bundle

        cluster = self.cognition.get_cluster(cluster_id)
        return build_public_bundle(cluster, cluster["items"])

    def _provider_name(self):
        remote = self.ai_settings.create_remote_provider()
        if remote is not None:
            self.judgment_queue.providers[remote.name] = remote
            return remote.name
        return "local"

    def _should_use_remote(self, cluster) -> bool:
        """稿D 放宽远程闸：把有限的远程调用集中到有决策价值的事件上。
        - E2/E3/E4（中高等级证据）：必远程
        - E1（低等级证据）：本地模板兜底，省预算、UI 标「模板推断」
        原稿C v2 要求 E2 且独立域名>=3 才远程，过于严格导致大部分事件走模板；
        放宽后所有中高等级事件均走远程，远程质量由 SYSTEM_INSTRUCTION 约束。
        当远程未启用时 _provider_name 返回 local，本闸结果会被覆盖，天然兼容。"""
        level = str(cluster.get("evidence_level") or "E1")
        return level in ("E2", "E3", "E4")

    def _last_remote_finished_at(self):
        """查询最近一次成功的远程研判完成时间，用于频率控制。"""
        try:
            with self.database.connect() as connection:
                row = connection.execute(
                    """
                    SELECT finished_at FROM judgment_jobs
                    WHERE status='succeeded' AND provider!='local'
                    ORDER BY finished_at DESC LIMIT 1
                    """
                ).fetchone()
            if not row or not row["finished_at"]:
                return None
            return datetime.fromisoformat(str(row["finished_at"]).replace("Z", "+00:00"))
        except (ValueError, TypeError, OSError):
            return None

    def _remote_due(self) -> bool:
        """根据用户设置的频率判断是否应该进行远程调用。
        - low：每天本地21点后跑一次，当天跑过则不再跑
        - medium：距上次远程 >= 6 小时
        - high：距上次远程 >= 1 小时
        远程未启用时返回 False（由调用方额外判断）。
        """
        settings = self.ai_settings.get()
        if not settings.get("enabled"):
            return False
        frequency = settings.get("frequency", "medium")
        local_now = self.now().astimezone()
        last = self._last_remote_finished_at()
        if frequency == "low":
            # 每天21点后跑一次，当天已跑过则不再跑
            if local_now.hour < 21:
                return False
            if last is not None and last.astimezone().date() == local_now.date():
                return False
            return True
        if frequency == "high":
            interval_hours = 1
        else:  # medium（默认）
            interval_hours = 6
        if last is None:
            return True  # 从未跑过远程，立即到期
        elapsed = (self.now() - last).total_seconds()
        return elapsed >= interval_hours * 3600

    def _should_notify(self, row) -> bool:
        """False for judgments older than the bootstrap cutoff.

        The first process_once() run after install/upgrade sets
        _notify_since to "now", so historical judgments inherited from a
        previous install are silently mapped (to keep personal_impacts
        scoring current) but never notify. After the first pass, the
        cutoff advances and fresh judgments notify normally.
        """
        return str(row["j_created_at"]) >= _iso(self._notify_since)

    def process_once(self):
        backfill = self.cognition.backfill_unclustered(limit=1000)
        remote_provider = self._provider_name()
        # 频率控制：远程启用且到达频率间隔时才允许远程调用，否则全部走本地
        remote_enabled = remote_provider != "local" and self._remote_due()
        clusters = [
            item for item in self.cognition.list_clusters(limit=1000) if item["needs_judgment"]
        ]
        # provider 透传给 summary 供诊断展示；空库（无 cluster）时回退到 remote_provider
        # 的语义值，保持"本次循环实际会用的 provider"口径一致。
        provider = remote_provider if not remote_enabled else "local"
        for cluster in clusters:
            # 稿C v2 选择性调用：按事件等级决定走远程还是本地模板，
            # 把每日预算（30）花在高决策价值的事件上，低等级走本地并标「模板推断」。
            provider = remote_provider if (remote_enabled and self._should_use_remote(cluster)) else "local"
            self.judgment_queue.enqueue(
                cluster["cluster_id"], cluster["evidence_hash"], provider
            )
        judgments = self.judgment_queue.run_due(limit=30)
        mapped = 0
        notified = 0
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT j.judgment_id,j.cluster_id,j.created_at AS j_created_at,
                       c.evidence_hash
                FROM judgments j
                JOIN event_clusters c ON c.latest_judgment_id=j.judgment_id
                ORDER BY j.created_at
                """
            ).fetchall()
        for row in rows:
            results = self.impacts.map_judgment(row["cluster_id"], row["judgment_id"])
            mapped += len(results)
            # Silently upgrade pre-cutoff judgments on the bootstrap run
            # (mapping is unconditional) so the first cycle never dumps a
            # cascade of historical notifications into the inbox.
            if not self._should_notify(row):
                continue
            for impact in results:
                candidate = impact.get("candidate", {})
                try:
                    window_days = max(
                        0,
                        (
                            date.fromisoformat(candidate["window_end"])
                            - self.now().date()
                        ).days,
                    )
                except (KeyError, TypeError, ValueError):
                    window_days = 30
                notification = self.notifications.consider(
                    {
                        **impact,
                        "evidence_hash": row["evidence_hash"],
                        "action_window_hours": window_days * 24,
                    },
                    impact["reason"],
                )
                notified += int(notification["status"] == "created")
        # After the first pass, drop the cutoff so future cycles notify
        # normally — only the bootstrap run is muted.
        self._notify_since = self.now()
        return {
            "backfill": backfill,
            "queued": len(clusters),
            "judgments": judgments,
            "mapped_impacts": mapped,
            "notifications_created": notified,
            "provider": provider,
        }

    def capture_trends(self):
        return self.trends.capture(self.now())

    def run_once(self):
        result = self.process_once()
        result["trends"] = self.capture_trends()
        return result

    def status(self):
        with self.database.connect() as connection:
            counts = {
                "clusters": connection.execute(
                    "SELECT COUNT(*) FROM event_clusters"
                ).fetchone()[0],
                "needs_judgment": connection.execute(
                    "SELECT COUNT(*) FROM event_clusters WHERE needs_judgment=1"
                ).fetchone()[0],
                "queued_jobs": connection.execute(
                    "SELECT COUNT(*) FROM judgment_jobs WHERE status IN ('queued','retry','queued_budget')"
                ).fetchone()[0],
                "unread_notifications": connection.execute(
                    "SELECT COUNT(*) FROM notification_log WHERE status='unread'"
                ).fetchone()[0],
            }
            states = {
                row["state_key"]: json.loads(row["value_json"])
                for row in connection.execute(
                    "SELECT state_key,value_json FROM runtime_state WHERE state_key LIKE 'task.%'"
                )
            }
        return {**counts, "tasks": states, "ai": self.ai_settings.get()}

    def list_jobs(self, limit=100):
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT job_id,cluster_id,evidence_hash,provider,model,status,
                       attempts,request_chars,created_at,next_attempt_at,
                       finished_at,last_error
                FROM judgment_jobs ORDER BY created_at DESC LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def cluster_detail(self, cluster_id):
        result = self.cognition.get_cluster(cluster_id)
        judgment_id = result.get("latest_judgment_id")
        result["judgment"] = None
        result["impacts"] = []
        if not judgment_id:
            return result
        with self.database.connect() as connection:
            judgment = connection.execute(
                "SELECT content_json,provider,created_at FROM judgments WHERE judgment_id=?",
                (judgment_id,),
            ).fetchone()
            impacts = connection.execute(
                """
                SELECT p.*,i.name AS interest_name FROM personal_impacts p
                LEFT JOIN interest_objects i ON i.object_id=p.interest_id
                WHERE p.cluster_id=? AND p.judgment_id=?
                ORDER BY p.impact_score DESC
                """,
                (cluster_id, judgment_id),
            ).fetchall()
        if judgment:
            result["judgment"] = {
                **json.loads(judgment["content_json"]),
                "provider": judgment["provider"],
                "created_at": judgment["created_at"],
            }
        for row in impacts:
            item = dict(row)
            item["components"] = json.loads(item.pop("components_json"))
            item["candidate"] = json.loads(item.pop("candidate_json"))
            result["impacts"].append(item)
        return result

    @staticmethod
    def _risk_time_window(horizons):
        text = " ".join(map(str, horizons or ()))
        if any(word in text for word in ("立即", "今日", "今天")):
            return "今天"
        if any(word in text for word in ("7天", "一周")):
            return "7 天内"
        if any(word in text for word in ("30天", "一个月", "本月")):
            return "30 天内"
        return "更长期"

    def risk_dashboard(self, source_states=None, limit=3):
        """Project internal judgments into a small personal decision workload."""
        limit = max(1, min(int(limit), 3))
        now = self.now().astimezone(timezone.utc)
        now_text = _iso(now)
        with self.database.connect() as connection:
            verifying = connection.execute(
                "SELECT COUNT(*) FROM event_clusters WHERE status='active' AND needs_judgment=1"
            ).fetchone()[0]
            rows = connection.execute(
                """
                SELECT p.*,i.name AS interest_name,i.category AS interest_category,
                       j.content_json,c.last_seen_at
                FROM personal_impacts p
                JOIN event_clusters c ON c.cluster_id=p.cluster_id
                JOIN judgments j ON j.judgment_id=p.judgment_id
                LEFT JOIN interest_objects i ON i.object_id=p.interest_id
                WHERE c.status='active'
                  AND c.latest_judgment_id=p.judgment_id
                  AND p.alert_level IN ('L3','L4')
                  AND p.user_label NOT IN ('false_positive','dismissed')
                  AND (p.muted_until IS NULL OR p.muted_until<=?)
                ORDER BY p.updated_at DESC
                """,
                (now_text,),
            ).fetchall()

        items = []
        action_count = 0
        watch_count = 0
        for row in rows:
            judgment = json.loads(row["content_json"] or "{}")
            candidate = json.loads(row["candidate_json"] or "{}")
            time_window = self._risk_time_window(judgment.get("horizons"))
            mode = (
                "action"
                if row["alert_level"] == "L4" or time_window in {"今天", "7 天内"}
                else "watch"
            )
            if mode == "action":
                action_count += 1
            else:
                watch_count += 1
            confidence = float(judgment.get("confidence", 0.0) or 0.0)
            if confidence >= 0.75:
                confidence_label = "较高"
            elif confidence >= 0.45:
                confidence_label = "中等"
            else:
                confidence_label = "仍在核实"
            up = judgment.get("up_triggers") or []
            down = judgment.get("down_triggers") or []
            direction = (
                "风险上升" if up and not down else "风险缓解" if down and not up else "没有明显变化"
            )
            interest_name = plain_text(row["interest_name"] or "已登记利益", 80)
            interest_category = plain_text(row["interest_category"] or "general", 40)
            fact_summary = plain_text(
                judgment.get("fact_summary") or "外部变化可能产生影响", 220
            )
            action = _personal_advice(
                interest_category, candidate.get("recommended_action")
            )
            risk_label = (
                "高风险"
                if row["alert_level"] == "L4"
                else "中风险" if mode == "action" else "低风险"
            )
            items.append(
                {
                    "cluster_id": row["cluster_id"],
                    "impact_id": row["impact_id"],
                    "mode": mode,
                    "alert_level": row["alert_level"],
                    "risk_level": "需要行动" if mode == "action" else "继续观察",
                    "risk_label": risk_label,
                    "interest_name": interest_name,
                    "interest_category": interest_category,
                    "title": f"{interest_name}：{fact_summary}",
                    "time_window": time_window,
                    "confidence": confidence_label,
                    "action": action,
                    "advice": action,
                    "reason": fact_summary,
                    "direction": direction,
                    "decision_by": plain_text(candidate.get("window_end"), 32) or "按时间窗口复查",
                    "updated_at": row["updated_at"],
                    "impact_score": float(row["impact_score"]),
                    "candidate_confirmed": bool(candidate.get("confirmed_forecast_id")),
                    "candidate_prob_low": candidate.get("probability_low", 0.3),
                    "candidate_prob_high": candidate.get("probability_high", 0.7),
                    "candidate_title": candidate.get("title", ""),
                }
            )

        items.sort(
            key=lambda item: (
                0 if item["mode"] == "action" else 1,
                0 if item["alert_level"] == "L4" else 1,
                -item["impact_score"],
                item["updated_at"],
            )
        )
        sources = list(source_states or ())
        enabled = [source for source in sources if source.get("enabled", True)]
        healthy = [
            source
            for source in enabled
            if source.get("last_status") == "ok" and not source.get("stale", False)
        ]
        coverage_gap = bool(enabled) and not healthy
        # Prioritise showing actual risks over the coverage_gap empty state:
        # the user already has L3/L4 impacts in the database, and hiding them
        # behind "覆盖不足" (even when sources are temporarily stale) leaves
        # the Action Home looking empty — which was the regression that made
        # the user think the app produced "no 预知策略 at all".
        if action_count:
            state = "action"
            summary = f"今天有 {action_count} 件事需要处理，先保护最重要的个人利益。"
        elif watch_count:
            state = "watch"
            summary = f"有 {watch_count} 件事需要继续观察，目前不必仓促行动。"
        elif coverage_gap:
            state = "coverage_gap"
            summary = "公开信息监控覆盖不足，系统正在重试，暂不能判断目前平稳。"
        else:
            state = "stable"
            summary = "目前没有需要你处理的高等级风险，系统仍在后台监控。"
        if coverage_gap and (action_count or watch_count):
            summary = f"部分信息源正在重试。{summary}"
        return {
            "state": state,
            "summary": summary,
            "counts": {"action": action_count, "watch": watch_count, "verifying": verifying},
            "items": items[:limit],
            "coverage": {"enabled": len(enabled), "healthy": len(healthy)},
            "generated_at": now_text,
        }

    def feedback(self, cluster_id, action, payload=None):
        payload = payload or {}
        now = self.now().astimezone(timezone.utc)
        if action == "mute":
            hours = max(1, min(int(payload.get("hours", 24 * 7)), 24 * 365))
            field, value = "muted_until", _iso(now + timedelta(hours=hours))
        elif action == "lower_importance":
            importance = max(1, min(int(payload.get("importance", 1)), 5))
            field, value = "importance_override", importance
        elif action == "false_positive":
            field, value = "user_label", "false_positive"
        elif action == "dismiss":
            field, value = "user_label", "dismissed"
        else:
            raise ValueError("反馈动作无效")
        with self.database.connect() as connection:
            result = connection.execute(
                f"UPDATE personal_impacts SET {field}=?,updated_at=? WHERE cluster_id=?",
                (value, _iso(now), cluster_id),
            )
            if result.rowcount == 0:
                raise KeyError(cluster_id)
            category_row = connection.execute(
                """
                SELECT io.category
                FROM personal_impacts pi
                JOIN interest_objects io ON io.object_id = pi.interest_id
                WHERE pi.cluster_id=?
                LIMIT 1
                """,
                (cluster_id,),
            ).fetchone()
            interest_category = category_row["category"] if category_row else ""
            domain_rows = connection.execute(
                "SELECT DISTINCT source_domain FROM event_cluster_items WHERE cluster_id=?",
                (cluster_id,),
            ).fetchall()
            source_domains = sorted(
                {
                    row["source_domain"]
                    for row in domain_rows
                    if row["source_domain"]
                }
            )
            connection.execute(
                """
                INSERT INTO feedback_events(
                    occurred_at, cluster_id, action,
                    interest_category, source_domains_json, applied_json
                ) VALUES (?, ?, ?, ?, ?, '{}')
                """,
                (
                    _iso(now),
                    cluster_id,
                    action,
                    interest_category or "",
                    json.dumps(source_domains, ensure_ascii=False),
                ),
            )
        return {"cluster_id": cluster_id, "action": action, "updated": result.rowcount}

    def apply_feedback_learning(self, *, limit=50):
        """消费未处理的反馈事件：降低误报源权重、加重类别惩罚。

        由调度线程周期调用（约 6 小时一次），请求线程只写流水。
        """
        now_text = _iso(self.now().astimezone(timezone.utc))
        applied_events = []
        with self.database.connect() as connection:
            pending = connection.execute(
                """
                SELECT event_id, occurred_at, cluster_id, action,
                       interest_category, source_domains_json
                FROM feedback_events
                WHERE applied_json='{}'
                ORDER BY occurred_at
                LIMIT ?
                """,
                (max(1, min(int(limit), 200)),),
            ).fetchall()
            penalties_row = connection.execute(
                "SELECT value_json FROM runtime_state WHERE state_key='learning.category_penalties'"
            ).fetchone()
            penalties = {}
            if penalties_row:
                try:
                    stored = json.loads(penalties_row["value_json"])
                    if isinstance(stored, dict):
                        penalties = {
                            str(key): float(value)
                            for key, value in stored.items()
                        }
                except (ValueError, TypeError):
                    penalties = {}
            for event in pending:
                changes = {"source_weight": [], "category_penalty": []}
                domains = []
                try:
                    loaded = json.loads(event["source_domains_json"])
                    if isinstance(loaded, list):
                        domains = [str(item) for item in loaded if item]
                except (ValueError, TypeError):
                    domains = []
                if event["action"] == "false_positive":
                    for domain in domains:
                        updated = connection.execute(
                            """
                            UPDATE external_sources
                            SET reliability_weight=MAX(0.2, reliability_weight-0.05)
                            WHERE endpoint LIKE ?
                                AND user_managed=0
                            """,
                            (f"%{domain}%",),
                        )
                        if updated.rowcount:
                            changes["source_weight"].append(domain)
                    category = event["interest_category"]
                    if category:
                        current = penalties.get(category, 1.0)
                        penalties[category] = round(max(0.5, current - 0.05), 4)
                        changes["category_penalty"].append(category)
                connection.execute(
                    "UPDATE feedback_events SET applied_json=? WHERE event_id=?",
                    (json.dumps(changes, ensure_ascii=False), event["event_id"]),
                )
                applied_events.append(event["event_id"])
            if penalties:
                connection.execute(
                    """
                    INSERT INTO runtime_state(state_key, value_json, updated_at)
                    VALUES ('learning.category_penalties', ?, ?)
                    ON CONFLICT(state_key) DO UPDATE SET
                        value_json=excluded.value_json,
                        updated_at=excluded.updated_at
                    """,
                    (json.dumps(penalties, ensure_ascii=False), now_text),
                )
            if applied_events:
                connection.execute(
                    "INSERT INTO audit_log(occurred_at, action, object_type, object_id, details_json) VALUES (?, ?, ?, ?, ?)",
                    (
                        now_text,
                        "feedback_learning",
                        "feedback_events",
                        ",".join(str(event_id) for event_id in applied_events),
                        json.dumps(
                            {"applied": len(applied_events), "penalties": penalties},
                            ensure_ascii=False,
                        ),
                    ),
                )
        return {"applied": len(applied_events)}
