"""B 层清理的「保命」判据 + 阈值节流时钟 —— 两组都按契约写，不采信自述。

覆盖两个刚审计出的缺陷：

- **缺陷 4（P2）**：B 层把 `personal_impacts` / `notification_log` 整表当派生明细清，
  但这两张表里混着**用户亲手写下的状态**：`user_label`（`dismissed` /
  `false_positive`，还被学习回路当输入）、`muted_until`、`importance_override`、
  `notification_log.read_at`。删掉它们不只是丢展示，而是让系统"忘掉"用户教过的判断。
- **缺陷 5（P3）**：阈值节流原本读 `audit_log` 的 `MAX(occurred_at)`，而空跑不写审计
  → 时钟永不前进 → 每次阈值检查都真跑一遍 A–E 全表扫描。

纪律：本文件只断言**行为**，不绑定实现（不 import 节流键名、不假设时钟存在哪张表）。
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.retention import DEFAULT_CLUSTER_DAYS, RetentionService

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def stamp(moment):
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class RetentionGuardBase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.clock = Clock(NOW)
        self.service = RetentionService(self.database, now=self.clock)

    def tearDown(self):
        self.temporary.cleanup()

    # -- 造数据 -----------------------------------------------------------

    def add_interest(self, object_id="I-1", name="家庭"):
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO interest_objects(object_id,name,category,importance,"
                "privacy_level,status) VALUES (?,?, 'family',5,'P1','active')",
                (object_id, name),
            )

    def add_expired_cluster(self, cluster_id="C-old"):
        moment = stamp(NOW - timedelta(days=DEFAULT_CLUSTER_DAYS + 20))
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO event_clusters(cluster_id,title,summary,first_seen_at,"
                "last_seen_at,evidence_level,evidence_hash,categories_json,status,"
                "needs_judgment,independent_domains,primary_source_count,created_at,updated_at)"
                " VALUES (?,?,'',?,?,'E2','hash','[\"policy\"]','active',0,1,1,?,?)",
                (cluster_id, "标题" + cluster_id, moment, moment, moment, moment),
            )
            connection.execute(
                "INSERT INTO judgments(judgment_id,cluster_id,provider,evidence_hash,"
                "content_json,created_at) VALUES (?,?, 'local','h','{}',?)",
                ("J-" + cluster_id, cluster_id, moment),
            )
            connection.execute(
                "INSERT INTO event_entities(entity_id,cluster_id,name,normalized_name,"
                "category,confidence) VALUES (?,?, '甲','甲','actor',0.9)",
                ("E-" + cluster_id, cluster_id),
            )
            connection.execute(
                "INSERT INTO event_cluster_items(cluster_id,item_id,similarity,"
                "merge_reason,source_domain,is_primary,added_at)"
                " VALUES (?,?,1.0,'new_cluster','example.com',1,?)",
                (cluster_id, "ITEM-" + cluster_id, moment),
            )

    def add_impact(
        self,
        impact_id,
        cluster_id="C-old",
        *,
        user_label="",
        muted_until=None,
        importance_override=None,
        judgment_id=None,
        interest_id="I-1",
    ):
        """插一条个人影响。

        `personal_impacts` 上有 UNIQUE(cluster_id, judgment_id, interest_id)，
        所以同一簇里的多条影响必须各自挂到不同的 (judgment, interest) 上；
        默认用 impact_id 派生 judgment_id 来保证唯一。
        """
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO personal_impacts(impact_id,cluster_id,judgment_id,interest_id,"
                "impact_score,alert_level,components_json,reason,candidate_json,"
                "muted_until,importance_override,user_label,created_at,updated_at)"
                " VALUES (?,?,?,?,0.9,'L3','{}','原因','{}',?,?,?,?,?)",
                (
                    impact_id,
                    cluster_id,
                    judgment_id or "J-" + impact_id,
                    interest_id,
                    muted_until,
                    importance_override,
                    user_label,
                    stamp(NOW - timedelta(days=DEFAULT_CLUSTER_DAYS + 20)),
                    stamp(NOW - timedelta(days=DEFAULT_CLUSTER_DAYS + 20)),
                ),
            )

    def add_notification(
        self, notification_id, cluster_id="C-old", *, read_at=None, impact_id="P-plain"
    ):
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO notification_log(notification_id,cluster_id,impact_id,"
                "created_at,alert_level,reason,evidence_hash,status,delivery,read_at)"
                " VALUES (?,?,?,?,'L3','原因','h','sent','windows',?)",
                (
                    notification_id,
                    cluster_id,
                    impact_id,
                    stamp(NOW - timedelta(days=DEFAULT_CLUSTER_DAYS + 20)),
                    read_at,
                ),
            )

    # -- 只读工具 ---------------------------------------------------------

    def rows(self, table, column):
        with self.database.connect() as connection:
            return sorted(
                row[0] for row in connection.execute("SELECT %s FROM %s" % (column, table))
            )

    def count(self, table, where="1=1", params=()):
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM %s WHERE %s" % (table, where), params
            ).fetchone()[0]

    def audit_total(self):
        return self.count("audit_log")


class UserAnnotationSurvivalTests(RetentionGuardBase):
    """缺陷 4：B 层不得删掉带用户标注的行，但也不得因此整表豁免。"""

    def seed_annotated_and_plain(self):
        self.add_interest()
        self.add_expired_cluster()
        # 带用户标注 —— 必须活下来
        self.add_impact("P-label-false-positive", user_label="false_positive")
        self.add_impact("P-label-dismissed", user_label="dismissed")
        self.add_impact(
            "P-muted", muted_until=stamp(NOW + timedelta(days=30))
        )
        self.add_impact("P-override", importance_override=5)
        # 同簇、无任何标注 —— 必须被清
        self.add_impact("P-plain")
        self.add_notification("N-read", read_at=stamp(NOW - timedelta(days=1)))
        self.add_notification("N-plain")

    def test_annotated_rows_survive_while_plain_siblings_are_deleted(self):
        self.seed_annotated_and_plain()

        result = self.service.run("manual")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["expired_clusters"], 1)
        self.assertEqual(
            self.rows("personal_impacts", "impact_id"),
            [
                "P-label-dismissed",
                "P-label-false-positive",
                "P-muted",
                "P-override",
            ],
            "带用户标注的个人影响被清理掉了 —— 学习回路的输入被抹掉",
        )
        self.assertEqual(
            self.rows("notification_log", "notification_id"),
            ["N-read"],
            "已读标记跟着通知一起被删了",
        )
        # 反向：同簇里没被用户碰过的行必须仍被清掉，否则就是"整表排除"式的过度收紧
        self.assertEqual(self.count("personal_impacts", "impact_id='P-plain'"), 0)
        self.assertEqual(self.count("notification_log", "notification_id='N-plain'"), 0)

    def test_counts_report_real_deletions_not_candidates(self):
        """`deleted_detail` 必须是**真实删除行数**，不是"扫到多少行"。

        如果有人图省事把整张 `personal_impacts` 从 `CLUSTER_DETAIL_TABLES` 里摘掉，
        计数会变成 0 而数据其实一条没删 —— 上一条能抓住"该删的没删"，这条抓住
        "报了数却没删"。两个方向都要钉。
        """
        self.seed_annotated_and_plain()

        result = self.service.run("manual")

        self.assertEqual(result["deleted_detail"]["personal_impacts"], 1)
        self.assertEqual(result["deleted_detail"]["notification_log"], 1)
        self.assertEqual(
            self.count("personal_impacts") + result["deleted_detail"]["personal_impacts"],
            5,
            "清理前的个人影响总数与'保留数+删除数'对不上",
        )

    def test_pure_derived_tables_are_still_purged(self):
        """同一簇的纯派生明细（实体、簇成员）行为不变 —— 仍照常清。"""
        self.seed_annotated_and_plain()

        result = self.service.run("manual")

        self.assertEqual(self.count("event_entities", "cluster_id='C-old'"), 0)
        self.assertEqual(self.count("event_cluster_items", "cluster_id='C-old'"), 0)
        self.assertEqual(result["deleted_detail"]["event_entities"], 1)
        self.assertEqual(result["deleted_detail"]["event_cluster_items"], 1)

    def test_conclusions_and_interests_survive(self):
        """结论（簇 + 研判）与用户登记的利益对象一条都不能少。"""
        self.seed_annotated_and_plain()

        self.service.run("manual")

        self.assertEqual(self.count("event_clusters", "cluster_id='C-old'"), 1)
        self.assertEqual(self.count("judgments", "cluster_id='C-old'"), 1)
        self.assertEqual(self.count("interest_objects"), 1)

    def test_expired_mute_is_still_an_annotation(self):
        """`muted_until` 已过期的行同样要保命。

        判据只能是"用户碰过没有"（非 NULL），不能顺手优化成"还在静音期内" ——
        用户当初按下静音这个动作本身就是要保住的信息；静音期一过就删，
        用户回头会发现自己标注过的东西凭空消失了。
        """
        self.add_interest()
        self.add_expired_cluster()
        self.add_impact("P-muted-expired", muted_until=stamp(NOW - timedelta(days=1)))
        self.add_impact("P-plain")

        self.service.run("manual")

        self.assertEqual(self.rows("personal_impacts", "impact_id"), ["P-muted-expired"])

    def test_cluster_with_only_annotated_rows_purges_nothing(self):
        """整簇都带标注时，该簇的删除数必须全 0，且不留半删状态。"""
        self.add_interest()
        self.add_expired_cluster()
        self.add_impact("P-a", user_label="dismissed")
        self.add_notification("N-a", read_at=stamp(NOW))

        first = self.service.run("manual")
        second = self.service.run("manual")

        self.assertEqual(first["deleted_detail"]["personal_impacts"], 0)
        self.assertEqual(first["deleted_detail"]["notification_log"], 0)
        self.assertEqual(self.rows("personal_impacts", "impact_id"), ["P-a"])
        self.assertEqual(self.rows("notification_log", "notification_id"), ["N-a"])
        # 第二次跑仍然删不掉 —— 说明不是"第一次侥幸"
        self.assertEqual(second["deleted_detail"]["personal_impacts"], 0)
        self.assertEqual(self.rows("personal_impacts", "impact_id"), ["P-a"])


class ThresholdThrottleClockTests(RetentionGuardBase):
    """缺陷 5：阈值节流必须真的会前进，且空跑不许伪装成一条清理审计。"""

    def test_empty_database_throttles_the_second_threshold_check(self):
        """空库上连跑两次 `threshold`：第一次 ok，第二次必须 skipped。

        修复前：空跑不写审计 → 时钟不前进 → 第二次仍返回 ok，于是每个检查周期
        都把 A~E 全表扫描 + dbstat 重跑一遍（真库单次约 1.8 秒）。
        """
        first = self.service.run("threshold")
        second = self.service.run("threshold")

        self.assertEqual(first["status"], "ok")
        self.assertEqual(
            second["status"],
            "skipped",
            "空跑没有推进节流时钟：第二次阈值检查又完整跑了一遍 A~E",
        )
        self.assertEqual(second["deleted_items"], 0)
        self.assertEqual(second["deleted_detail"], {})
        self.assertEqual(second["downsampled_snapshots"], {})

    def test_empty_runs_leave_no_cleanup_audit_record(self):
        """空跑不许在 `audit_log` 里留下 `retention_cleanup` 记录。

        审计的语义是"真的删掉了东西"。为了推时钟而写假审计，会让诊断面板与
        事后追责都失真 —— 这跟"节流必须前进"是两件事，不能用审计去凑。
        """
        self.service.run("threshold")
        before = self.audit_total()
        self.service.run("threshold")

        self.assertEqual(
            self.count("audit_log", "action='retention_cleanup'"),
            0,
            "空跑写了一条 retention_cleanup 审计：审计语义被污染",
        )
        self.assertEqual(
            self.audit_total(), before, "被跳过的空跑往 audit_log 里追加了行"
        )

    def test_throttle_clock_advances_so_work_resumes_after_the_interval(self):
        """节流不是"一次跑过就锁死"：时钟走过 6 小时后必须放行并真的删东西。"""
        self.service.run("threshold")
        self.assertEqual(self.service.run("threshold")["status"], "skipped")

        self.clock.value = NOW + timedelta(hours=24)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO external_runs(run_id,source_id,started_at,finished_at,"
                "status,fetched_count,new_count,error_type,error_message)"
                " VALUES ('RUN-late','src',?,?,'ok',1,1,'','')",
                (
                    stamp(NOW - timedelta(days=200)),
                    stamp(NOW - timedelta(days=200)),
                ),
            )

        resumed = self.service.run("threshold")

        self.assertEqual(resumed["status"], "ok", "时钟前进后阈值检查仍被跳过")
        self.assertGreater(resumed["deleted_runs"], 0, "放行了却没真的删东西")
        self.assertEqual(self.count("audit_log", "action='retention_cleanup'"), 1)

    def test_disabled_and_manual_triggers_are_unaffected(self):
        """节流只约束 `threshold`：`disabled` 提前返回、`manual` 绕过间隔。

        这条是防过度收紧的对照 —— 改节流时钟很容易顺手把 manual/disabled 也
        挂到间隔判断上。
        """
        self.service.run("threshold")
        self.assertEqual(self.service.run("manual")["status"], "ok")
        self.assertEqual(self.service.run("manual")["status"], "ok")

        payload = {"enabled": False}
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO runtime_state(state_key,value_json,updated_at)"
                " VALUES ('settings.retention',?,?)"
                " ON CONFLICT(state_key) DO UPDATE SET value_json=excluded.value_json",
                (json.dumps(payload), stamp(NOW)),
            )
        self.assertEqual(self.service.run("threshold")["status"], "disabled")


if __name__ == "__main__":
    unittest.main()
