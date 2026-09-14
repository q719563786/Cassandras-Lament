"""防冗余（F 组）的对抗性测试。

背景（契约 §0，实测结论）
------------------------
设计文档原以为「`judgment_jobs` 有去重约束，但 `judgments` 没有」，因此 F1 要
新增去重逻辑。实测推翻了该前提：`judgments` **本来就有**
`UNIQUE(cluster_id, provider, evidence_hash)`（`database.py:278`），写入端也一直用
`INSERT OR IGNORE` + 回查（`remote_ai.py:707`），三元组重复组数实测为 0。

所以 **F1 不实现新代码**——它由数据库约束承担。`F1ConstraintGuardTests` 是 F1 的
唯一防线：**钉死该约束存在且真的会拒绝重复**。将来若有人"清理死代码"时把这条
UNIQUE 删掉，F1 的防冗余能力会静默归零（不报错，只是慢慢重复堆积），
这类静默退化只能靠守卫测试拦住。

F2/F3 是写入端的两道闸门（`remote_ai.py` 的 `_persist_judgment`），见
`F2WriteSideTests` / `F3WriteSideTests`。
"""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.judgments import LocalHeuristicProvider, build_public_bundle
from yuanjian_app.remote_ai import JudgmentQueue
from yuanjian_app.retention import (
    DEFAULT_MAX_JUDGMENTS_PER_CLUSTER,
    read_retention_setting,
    write_retention_setting,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def _stamp(moment=NOW):
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def unique_indexes(connection, table):
    """列出表上的 UNIQUE 索引，返回 [(列名元组, 是否部分索引)]。

    表级 `UNIQUE(...)` 约束在 SQLite 里落成 `sqlite_autoindex_<表>_<n>`
    （`origin='u'`），因此必须用 PRAGMA 反查，不能只看建表语句。
    """
    found = []
    for row in connection.execute("PRAGMA index_list(%s)" % table).fetchall():
        if not row["unique"]:
            continue
        columns = tuple(
            info["name"]
            for info in connection.execute(
                "PRAGMA index_info(%s)" % row["name"]
            ).fetchall()
        )
        found.append((columns, bool(row["partial"])))
    return found


class StubProvider:
    """不联网、可复现的假 provider。"""

    model = "stub-model"

    def analyze(self, evidence):
        return LocalHeuristicProvider().analyze(evidence)


def bundle(cluster_id="C-1", title="医保政策调整", extra_items=()):
    items = [
        {
            "source_id": "S-1",
            "title": "政策通知",
            "summary": "公开内容",
            "canonical_url": "https://news.example/policy",
            "published_at": "2026-08-11T00:00:00Z",
        }
    ]
    for index, item in enumerate(extra_items):
        items.append(
            {
                "source_id": "S-%d" % (index + 2),
                "title": item,
                "summary": "补充内容",
                "canonical_url": "https://news.example/extra-%d" % index,
                "published_at": "2026-08-12T00:00:00Z",
            }
        )
    return build_public_bundle(
        {
            "cluster_id": cluster_id,
            "title": title,
            "summary": "公开事件",
            "evidence_level": "E2",
            "categories": ["policy"],
        },
        items,
    )


def analyze(target):
    return LocalHeuristicProvider().analyze(target)


def content_json_of(target):
    return json.dumps(analyze(target).to_dict(), ensure_ascii=False, sort_keys=True)


class WriteSideCase(unittest.TestCase):
    """F 组共用夹具：临时库 + 可控时钟 + 真实 `JudgmentQueue`。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.queue = JudgmentQueue(
            self.database,
            providers={"remote": StubProvider()},
            bundle_loader=lambda cluster_id: bundle(cluster_id),
            local_provider=LocalHeuristicProvider(),
            now=lambda: NOW,
        )

    def tearDown(self):
        self.temporary.cleanup()

    # -- 造数据 -----------------------------------------------------------

    def insert_cluster(self, cluster_id, needs_judgment=1):
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO event_clusters(cluster_id,title,summary,first_seen_at,"
                "last_seen_at,evidence_level,evidence_hash,categories_json,status,"
                "needs_judgment,independent_domains,primary_source_count,created_at,updated_at)"
                " VALUES (?,?,'',?,?,'E2','hash','[\"policy\"]','active',?,1,1,?,?)",
                (cluster_id, "标题", _stamp(), _stamp(), needs_judgment, _stamp(), _stamp()),
            )

    def insert_judgment(self, judgment_id, cluster_id, content_json, created_at,
                        provider="local", evidence_hash=None):
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO judgments(judgment_id,cluster_id,provider,evidence_hash,"
                "content_json,created_at) VALUES (?,?,?,?,?,?)",
                (
                    judgment_id,
                    cluster_id,
                    provider,
                    evidence_hash or ("hash-" + judgment_id),
                    content_json,
                    _stamp(created_at),
                ),
            )

    def insert_judgment_unchecked(self, connection, judgment_id, cluster_id, content_json,
                                  created_at, provider="local", evidence_hash=None):
        """事务内直插，供 `_persist_judgment` 与调用方共用同一连接。"""
        connection.execute(
            "INSERT INTO judgments(judgment_id,cluster_id,provider,evidence_hash,"
            "content_json,created_at) VALUES (?,?,?,?,?,?)",
            (
                judgment_id,
                cluster_id,
                provider,
                evidence_hash or ("hash-" + judgment_id),
                content_json,
                _stamp(created_at),
            ),
        )

    # -- 调用与断言 -------------------------------------------------------

    def persist(self, cluster_id, evidence_hash, result, provider="remote"):
        """在与 `_persist_judgment` 相同的连接/事务里调用它。"""
        with self.database.connect() as connection:
            job = {"cluster_id": cluster_id, "evidence_hash": evidence_hash}
            return self.queue._persist_judgment(connection, job, provider, result, NOW)

    def counts(self, table, where="1=1", params=()):
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM %s WHERE %s" % (table, where), params
            ).fetchone()[0]

    def cluster_state(self, cluster_id):
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT latest_judgment_id, needs_judgment FROM event_clusters"
                " WHERE cluster_id=?",
                (cluster_id,),
            ).fetchone()
            exists = connection.execute(
                "SELECT COUNT(*) FROM judgments WHERE judgment_id=?",
                (row["latest_judgment_id"],),
            ).fetchone()[0]
        return row["latest_judgment_id"], row["needs_judgment"], exists


class F1ConstraintGuardTests(WriteSideCase):
    """F1：`judgments` 的三元组唯一约束必须存在且强制生效。"""

    def insert_judgment_row(self, judgment_id, cluster_id, provider, evidence_hash):
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO judgments(judgment_id,cluster_id,provider,evidence_hash,"
                "content_json,created_at) VALUES (?,?,?,?,'{}',?)",
                (judgment_id, cluster_id, provider, evidence_hash, _stamp()),
            )

    def test_judgments_has_unique_cluster_provider_evidence_hash(self):
        """契约 §0 的核心断言：这条约束一旦消失，F1 就退化成空操作。"""
        with self.database.connect() as connection:
            indexes = unique_indexes(connection, "judgments")

        expected = ("cluster_id", "provider", "evidence_hash")
        self.assertIn(
            (expected, False),
            indexes,
            "judgments 上缺少 UNIQUE%s（实测现有唯一索引：%r）" % (expected, indexes),
        )

    def test_judgment_jobs_has_unique_cluster_evidence_hash_provider(self):
        """`judgment_jobs` 的同类约束是实测里"设计意图"的来源，一并钉死。"""
        with self.database.connect() as connection:
            indexes = unique_indexes(connection, "judgment_jobs")

        expected = ("cluster_id", "evidence_hash", "provider")
        self.assertIn((expected, False), indexes, "实测现有唯一索引：%r" % (indexes,))

    def test_duplicate_triple_is_rejected_by_integrity_error(self):
        """同一 (cluster_id, provider, evidence_hash) 插第二次必须被数据库拒绝。"""
        self.insert_judgment_row("J-1", "C-1", "remote", "hash-1")

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_judgment_row("J-2", "C-1", "remote", "hash-1")

        self.assertEqual(self.counts("judgments"), 1)

    def test_duplicate_triple_is_ignored_by_insert_or_ignore(self):
        """写入端用的是 INSERT OR IGNORE —— 重复必须静默丢弃而不是新增一行。"""
        with self.database.connect() as connection:
            for judgment_id in ("J-1", "J-2", "J-3"):
                connection.execute(
                    "INSERT OR IGNORE INTO judgments(judgment_id,cluster_id,provider,"
                    "evidence_hash,content_json,created_at) VALUES (?,?,?,?,'{}',?)",
                    (judgment_id, "C-1", "remote", "hash-1", _stamp()),
                )
            rows = connection.execute(
                "SELECT judgment_id FROM judgments WHERE cluster_id='C-1'"
            ).fetchall()

        self.assertEqual([row[0] for row in rows], ["J-1"])

    def test_evidence_hash_variance_does_not_false_positive(self):
        """约束只锁三元组：证据变化或多 provider 必须能正常新增。

        这同时是"研判会随证据更新"这条产品能力的回归保护 —— 如果将来有人把
        约束收紧成 UNIQUE(cluster_id)，研判将永远无法更新。
        """
        self.insert_judgment_row("J-1", "C-1", "remote", "hash-1")
        self.insert_judgment_row("J-2", "C-1", "remote", "hash-2")  # 证据更新
        self.insert_judgment_row("J-3", "C-1", "local", "hash-1")  # 换 provider
        self.insert_judgment_row("J-4", "C-2", "remote", "hash-1")  # 换簇

        self.assertEqual(self.counts("judgments"), 4)

    def test_persist_judgment_reuses_row_and_returns_existing_id(self):
        """真实写入路径的守卫：同一个三元组写两次，只能有一行，且 id 不变。

        调用方依赖 `_persist_judgment` 始终返回一个有效 id；契约 §6 要求它在
        F2 跳过路径上也必须返回**已存在的** id。这条断言在 F2 落地前后都必须成立。
        """
        self.insert_cluster("C-1")
        result = analyze(bundle("C-1"))

        first = self.persist("C-1", "hash-1", result)
        second = self.persist("C-1", "hash-1", result)

        self.assertTrue(first)
        self.assertEqual(second, first, "重复写入必须复用已存在的研判 id")
        self.assertEqual(self.counts("judgments", "cluster_id='C-1'"), 1)
        latest, needs_judgment, exists = self.cluster_state("C-1")
        self.assertEqual(latest, first)
        self.assertEqual(needs_judgment, 0)
        self.assertEqual(exists, 1, "latest_judgment_id 指向了一条不存在的研判")


class F2WriteSideTests(WriteSideCase):
    """F2（契约 §6）：内容与该簇最新一条研判完全相同 → 跳过插入。

    这是拿「反复写同一份 JSON」换磁盘，**不是**「同证据重判」（那个由 F1 的
    唯一约束 + INSERT OR IGNORE 覆盖）。
    """

    def seed_cluster_with_judgment(self, cluster_id="C-1", created_at=NOW):
        """先塞一条研判，内容等于 `bundle(cluster_id)` 的本地研判结果。"""
        target = bundle(cluster_id)
        content = content_json_of(target)
        self.insert_cluster(cluster_id)
        self.insert_judgment("J-seed", cluster_id, content, created_at)
        return target, content

    def test_content_equality_is_reachable_with_independent_judgments(self):
        """前置自检：同一 bundle 两次研判必须是同一份 JSON。

        若本地研判不可复现（比如带时间戳），F2 在生产里永远不会触发，
        后面的测试就会变成空断言。这条先把这个前提钉死。
        """
        target = bundle("C-1")

        self.assertEqual(content_json_of(target), content_json_of(bundle("C-1")))

    def test_skip_when_content_matches_latest(self):
        self.seed_cluster_with_judgment()
        # 证据变了（不同 evidence_hash，唯一约束拦不住），但模型给出一字不差的判读
        result = analyze(bundle("C-1"))

        returned = self.persist("C-1", "brand-new-hash", result)

        self.assertEqual(returned, "J-seed", "F2 跳过时必须返回已存在的最新 id")
        self.assertEqual(self.counts("judgments", "cluster_id='C-1'"), 1)
        latest, needs_judgment, exists = self.cluster_state("C-1")
        self.assertEqual(latest, "J-seed")
        self.assertEqual(
            needs_judgment, 0, "跳过路径必须清 needs_judgment，否则会被无限重排"
        )
        self.assertEqual(exists, 1)

    def test_no_skip_when_content_differs(self):
        self.seed_cluster_with_judgment()
        other = bundle("C-1", title="完全不同的标题", extra_items=["新增条目"])
        self.assertNotEqual(
            content_json_of(other),
            content_json_of(bundle("C-1")),
            "用例自身没造出不同的内容，断言会变成空的",
        )

        returned = self.persist("C-1", "hash-2", analyze(other))

        self.assertNotEqual(returned, "J-seed")
        self.assertEqual(self.counts("judgments", "cluster_id='C-1'"), 2)
        latest, needs_judgment, _ = self.cluster_state("C-1")
        self.assertEqual(latest, returned)
        self.assertEqual(needs_judgment, 0)

    def test_comparison_is_against_the_newest_not_the_oldest(self):
        """契约 §6 明确按 `created_at` 取**最新一条**比对，不是最早那条。

        构造：最早一条内容 = X，最新一条内容 = Y（两者不同）。
        - 提交 Y → 必须跳过。若实现误拿**最早一条**比对，会看到 X ≠ Y 而新增。
        - 提交 X → 必须新增（最新是 Y，内容不同）。
        """
        target = bundle("C-1")
        content_x = content_json_of(target)
        other = bundle("C-1", title="后来的内容", extra_items=["新条目"])
        content_y = content_json_of(other)
        self.assertNotEqual(content_x, content_y)

        self.insert_cluster("C-1")
        self.insert_judgment("J-old", "C-1", content_x, NOW - timedelta(days=5))
        self.insert_judgment("J-new", "C-1", content_y, NOW - timedelta(days=1))

        # 最新一条已是 Y → 必须跳过；误比最早一条则会产生第 3 条
        y_returned = self.persist("C-1", "hash-y", analyze(other))
        self.assertEqual(y_returned, "J-new", "比对的是最早一条而非最新一条")
        self.assertEqual(self.counts("judgments", "cluster_id='C-1'"), 2)

        # 提交 X：最新是 Y，内容不同 → 必须新增
        x_returned = self.persist("C-1", "hash-x", analyze(target))
        self.assertNotEqual(x_returned, "J-new")
        self.assertEqual(self.counts("judgments", "cluster_id='C-1'"), 3)

    def test_empty_cluster_is_not_treated_as_a_match(self):
        """没有历史研判时不存在"内容相同"，必须正常新增。"""
        self.insert_cluster("C-1")

        returned = self.persist("C-1", "hash-1", analyze(bundle("C-1")))

        self.assertTrue(returned)
        self.assertEqual(self.counts("judgments", "cluster_id='C-1'"), 1)
        _, needs_judgment, _ = self.cluster_state("C-1")
        self.assertEqual(needs_judgment, 0)

    def test_skip_path_does_not_raise_and_leaves_history_untouched(self):
        """跳过路径只能"静默跳过 + 返回"，不能抛异常，也不能动历史研判。"""
        self.seed_cluster_with_judgment()
        before = None
        with self.database.connect() as connection:
            before = [
                tuple(row)
                for row in connection.execute(
                    "SELECT judgment_id, cluster_id, provider, evidence_hash,"
                    " content_json, created_at FROM judgments ORDER BY judgment_id"
                )
            ]

        returned = self.persist("C-1", "another-hash", analyze(bundle("C-1")))

        with self.database.connect() as connection:
            after = [
                tuple(row)
                for row in connection.execute(
                    "SELECT judgment_id, cluster_id, provider, evidence_hash,"
                    " content_json, created_at FROM judgments ORDER BY judgment_id"
                )
            ]
        self.assertEqual(returned, "J-seed")
        self.assertEqual(after, before)


class F3WriteSideTests(WriteSideCase):
    """F3（契约 §6）：单簇研判条数上限。

    **产品代价（必须知情）**：簇一旦触顶，其研判"停止进化"——
    `latest_judgment_id` 不再跟随证据更新。这是拿判读新鲜度换磁盘。
    """

    def seed_cluster_at(self, existing, cluster_id="C-1"):
        """造一个已有 `existing` 条研判的簇，内容与新判读**不同**。

        每条都追加 `#<序号>` 后缀，避免 F2 的内容比对提前命中，
        从而让用例只测 F3 这一道闸门。
        """
        self.insert_cluster(cluster_id)
        stale = content_json_of(bundle(cluster_id, title="历史判读"))
        for index in range(existing):
            self.insert_judgment(
                "J-%d" % index,
                cluster_id,
                "%s#%d" % (stale, index),
                NOW - timedelta(days=existing - index),
            )
        return bundle(cluster_id)

    def test_cap_allows_filling_up_to_the_limit(self):
        """上限 3、已有 2 条 → 允许新增，插完正好 3 条。"""
        write_retention_setting(self.database, {"max_judgments_per_cluster": 3})
        self.seed_cluster_at(2)

        returned = self.persist("C-1", "hash-new", analyze(bundle("C-1")))

        self.assertEqual(self.counts("judgments", "cluster_id='C-1'"), 3)
        self.assertTrue(returned)

    def test_cap_blocks_at_the_limit(self):
        """上限 3、已有 3 条 → 不再新增，返回已存在的最新 id，`needs_judgment` 归 0。"""
        write_retention_setting(self.database, {"max_judgments_per_cluster": 3})
        self.seed_cluster_at(3)

        returned = self.persist("C-1", "hash-new", analyze(bundle("C-1")))

        self.assertEqual(self.counts("judgments", "cluster_id='C-1'"), 3, "超限后仍新增了")
        self.assertTrue(returned)
        latest, needs_judgment, exists = self.cluster_state("C-1")
        self.assertEqual(returned, latest)
        self.assertEqual(
            needs_judgment, 0, "封顶后必须清 needs_judgment，否则反复入队制造垃圾"
        )
        self.assertEqual(exists, 1, "latest_judgment_id 指向了一条不存在的研判")

    def test_cap_blocks_when_already_over_the_limit(self):
        """历史数据本就超限（比如用户把上限调低）时，同样不许再新增。"""
        write_retention_setting(self.database, {"max_judgments_per_cluster": 2})
        self.seed_cluster_at(5)

        self.persist("C-1", "hash-new", analyze(bundle("C-1")))

        self.assertEqual(self.counts("judgments", "cluster_id='C-1'"), 5)

    def test_cap_is_configurable_down_to_one(self):
        write_retention_setting(self.database, {"max_judgments_per_cluster": 1})
        self.seed_cluster_at(1)

        self.persist("C-1", "hash-new", analyze(bundle("C-1")))

        self.assertEqual(self.counts("judgments", "cluster_id='C-1'"), 1)

    def test_cap_does_not_apply_to_an_empty_cluster(self):
        self.insert_cluster("C-1")

        returned = self.persist("C-1", "hash-new", analyze(bundle("C-1")))

        self.assertTrue(returned)
        self.assertEqual(self.counts("judgments", "cluster_id='C-1'"), 1)

    def test_default_cap_matches_the_contract(self):
        self.assertEqual(
            read_retention_setting(self.database)["max_judgments_per_cluster"],
            DEFAULT_MAX_JUDGMENTS_PER_CLUSTER,
        )
        self.assertEqual(DEFAULT_MAX_JUDGMENTS_PER_CLUSTER, 8)

    def test_cap_of_eight_stops_the_ninth_judgment(self):
        """契约默认 N=8 的端到端边界：第 8 条能进，第 9 条进不来。"""
        self.insert_cluster("C-1")
        accepted = 0
        for index in range(12):
            # 每轮内容都不同，确保是 F3 而不是 F2 在起作用
            target = bundle("C-1", title="第 %d 版判读" % index)
            before = self.counts("judgments", "cluster_id='C-1'")
            self.persist("C-1", "hash-%d" % index, analyze(target))
            after = self.counts("judgments", "cluster_id='C-1'")
            if after > before:
                accepted += 1

        self.assertEqual(accepted, DEFAULT_MAX_JUDGMENTS_PER_CLUSTER)
        self.assertEqual(
            self.counts("judgments", "cluster_id='C-1'"), DEFAULT_MAX_JUDGMENTS_PER_CLUSTER
        )

    def test_cap_leaves_needs_judgment_zero_on_every_attempt(self):
        """反复提交超限内容，`needs_judgment` 必须始终为 0（否则无限重排）。"""
        self.insert_cluster("C-1")
        for index in range(DEFAULT_MAX_JUDGMENTS_PER_CLUSTER + 3):
            self.persist(
                "C-1", "hash-%d" % index, analyze(bundle("C-1", title="第 %d 版" % index))
            )
            with self.database.connect() as connection:
                needs = connection.execute(
                    "SELECT needs_judgment FROM event_clusters WHERE cluster_id='C-1'"
                ).fetchone()[0]
            self.assertEqual(needs, 0, "第 %d 次提交后 needs_judgment 没清" % index)

    def test_cap_does_not_touch_existing_rows(self):
        """封顶只拦"新增"，绝不能改动或删除已有研判（判读不可变）。"""
        write_retention_setting(self.database, {"max_judgments_per_cluster": 2})
        self.seed_cluster_at(2)
        with self.database.connect() as connection:
            before = [
                tuple(row)
                for row in connection.execute(
                    "SELECT judgment_id, content_json, created_at FROM judgments"
                    " ORDER BY judgment_id"
                )
            ]

        self.persist("C-1", "hash-new", analyze(bundle("C-1")))

        with self.database.connect() as connection:
            after = [
                tuple(row)
                for row in connection.execute(
                    "SELECT judgment_id, content_json, created_at FROM judgments"
                    " ORDER BY judgment_id"
                )
            ]
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
