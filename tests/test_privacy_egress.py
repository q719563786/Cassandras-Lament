"""缺陷 1（P0）· 私人数据外发：远程研判的请求体里**一个个人字段都不许有**。

`PRIVACY.md` 三处明文承诺：

1. 「利益对象与关系、个人影响、反馈和预测历史」永不进入外部 AI；
2. 「外部 AI 的唯一输入」是 `build_public_bundle()` 生成的公开证据包；
3. 「唯一的外发请求」只有设置页的检查更新按钮。

此前 `application.py::_make_personal_context_loader` 把用户亲笔近况（manual
signals 的 summary / why_it_matters）、利益对象与关系、最近 5 条预测打包，
经 `remote_ai.py` 注入请求体 POST 给第三方 AI，**且完全不看 `privacy_level`**。

本文件的断言方式是**捕获真实外发的请求体**，而不是检查某段代码有没有被调用 ——
只要请求体里没有，怎么实现都对；只要有，怎么注释都不算。

断言分三层：
- 端到端（走 `Application.create()` 装配出来的真实队列 + 真实 provider）；
- 边界（把个人上下文加载器手工接回去，provider 仍必须拒绝外发）；
- 反向（公开证据包与 `personal_action` 必须仍在，防止"用删功能来实现隐私"）。
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from yuanjian_app.application import Application
from yuanjian_app.database import Database
from yuanjian_app.forecasts import ForecastService
from yuanjian_app.interests import InterestService
from yuanjian_app.judgments import LocalHeuristicProvider, build_public_bundle
from yuanjian_app.remote_ai import (
    DeepSeekChatProvider,
    JudgmentQueue,
    OpenAIResponsesProvider,
)
from yuanjian_app.signals import SignalService

STAMP = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc).isoformat().replace(
    "+00:00", "Z"
)

# 个人数据的"指纹"。用中文长串，避免与任何真实英文键名混淆。
MARKER_P1_INTEREST = "利益指纹甲-现金流水位"
MARKER_P3_INTEREST = "利益指纹乙-体检指标"
MARKER_LINK = "关系指纹丙-共同承担"
MARKER_FORECAST = "预测指纹丁-房贷重定价"
MARKER_SIGNAL = "近况指纹戊-月初收入下降"
MARKER_SIGNAL_WHY = "近况指纹己-关系到现金流安全垫"

# 公开证据包侧（**应该**外发）
CLUSTER_ID = "C-privacy"
MARKER_PUBLIC_TITLE = "公开事件指纹庚-利率公告"
MARKER_PUBLIC_SUMMARY = "公开事件指纹辛-公告摘要内容"
EVIDENCE_TITLE = "公开来源标题壬"
EVIDENCE_SUMMARY = "公开来源摘要癸"
EVIDENCE_DOMAIN = "news.example"
EVIDENCE_URL = "https://news.example/public-notice"

# 请求体里绝对不许出现的键名
FORBIDDEN_KEYS = ("personal_context", "interests", "recent_forecasts")
FORBIDDEN_MARKERS = (
    MARKER_P1_INTEREST,
    MARKER_P3_INTEREST,
    MARKER_LINK,
    MARKER_FORECAST,
    MARKER_SIGNAL,
    MARKER_SIGNAL_WHY,
)


def remote_payload():
    return {
        "fact_summary": "公开政策调整",
        "actors": ["主管部门"],
        "causal_chain": ["政策变化", "执行变化"],
        "uncertainties": ["细则待公布"],
        "horizons": ["未来30天"],
        "probability_low": 0.5,
        "probability_high": 0.7,
        "confidence": 0.65,
        "supporting_source_ids": ["S-1"],
        "counter_source_ids": [],
        "up_triggers": ["正式生效"],
        "down_triggers": ["延期"],
        "impact_categories": ["policy"],
        "personal_action": "偏向保守：先补充现金缓冲，暂缓非必要支出。",
        "gyw": {
            "stakeholders": "推动方：发文机关；阻力方：执行部门",
            "constraints": "资源约束：财政预算、配套立法",
            "least_resistance_path": "最小阻力路径：试点后推广",
            "counter_evidence": "反对证据：执行阻力、政策转向",
            "leading_indicators": "领先指标：试点公告、配套细则",
            "beneficiaries": [
                {"subject": "发文机关", "gain": "政绩落地", "evidence_refs": ["S-1"]}
            ],
            "cost_bearers": [
                {"subject": "[推断]执行部门", "cost": "配套资源压力", "evidence_refs": []}
            ],
            "historical_parallel": None,
            "observable_signals": ["配套细则挂网", "部门预算批复"],
        },
    }


class CapturingTransport:
    """记录真实发出的请求体，不联网。"""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": body})
        return self.response

    @property
    def body_text(self):
        return json.dumps(self.calls[-1]["body"], ensure_ascii=False)


class RecordingDesktop:
    def run(self, url, hidden=False):
        pass


class PrivacyEgressBase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.app = Application.create(
            self.root, desktop=RecordingDesktop(), legacy_path=None
        )
        self.database = Database(self.root / "data" / "yuanjian.db")
        self.seed_private_world()

    def tearDown(self):
        self.app.close()
        self.temporary.cleanup()

    # -- 造一个真实形状的个人世界 ------------------------------------------

    def seed_private_world(self):
        with self.database.connect() as connection:
            for object_id, name, category, privacy in (
                ("I-p1", MARKER_P1_INTEREST, "cashflow", "P1"),
                ("I-p3", MARKER_P3_INTEREST, "health", "P3"),
            ):
                connection.execute(
                    "INSERT INTO interest_objects(object_id,name,category,importance,"
                    "privacy_level,status) VALUES (?,?,?,5,?,'active')",
                    (object_id, name, category, privacy),
                )
            connection.execute(
                "INSERT INTO interest_links(link_id,source_id,target_id,relationship,"
                "impact_direction,strength) VALUES ('L-1','I-p1','I-p3',?,'negative',5)",
                (MARKER_LINK,),
            )
            connection.execute(
                "INSERT INTO event_clusters(cluster_id,title,summary,first_seen_at,"
                "last_seen_at,evidence_level,evidence_hash,categories_json,status,"
                "needs_judgment,independent_domains,primary_source_count,created_at,"
                "updated_at) VALUES (?,?,?,?,?,'E2','hash','[\"policy\"]','active',1,1,1,?,?)",
                (
                    CLUSTER_ID,
                    MARKER_PUBLIC_TITLE,
                    MARKER_PUBLIC_SUMMARY,
                    STAMP,
                    STAMP,
                    STAMP,
                    STAMP,
                ),
            )
            connection.execute(
                "INSERT INTO external_items(item_id,canonical_url,title,summary,"
                "published_at,fetched_at,source_id,source_name,content_hash,"
                "first_seen_at,last_seen_at) VALUES ('ITEM-1',?,?,?,?,?,'S-1','来源','h',?,?)",
                (
                    EVIDENCE_URL,
                    EVIDENCE_TITLE,
                    EVIDENCE_SUMMARY,
                    STAMP,
                    STAMP,
                    STAMP,
                    STAMP,
                ),
            )
            connection.execute(
                "INSERT INTO event_cluster_items(cluster_id,item_id,similarity,"
                "merge_reason,source_domain,is_primary,added_at)"
                " VALUES (?, 'ITEM-1', 1.0, 'new_cluster', ?, 1, ?)",
                (CLUSTER_ID, EVIDENCE_DOMAIN, STAMP),
            )

        ForecastService(self.database).create_forecast(
            {
                "title": MARKER_FORECAST,
                "category": "housing",
                "resolution_criteria": "以公开挂牌利率为准",
                "window_start": "2026-09-01",
                "window_end": "2026-12-31",
                "probability": 0.5,
                "privacy_level": "P2",
            }
        )
        signal = SignalService(
            self.database, InterestService(self.database)
        ).ingest(MARKER_SIGNAL, "2026-09-01", source_type="manual")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE signals SET why_it_matters=? WHERE signal_id=?",
                (MARKER_SIGNAL_WHY, signal["signal_id"]),
            )

    # -- 工具 -------------------------------------------------------------

    def assert_no_personal_data(self, body_text):
        for key in FORBIDDEN_KEYS:
            with self.subTest(kind="key", value=key):
                self.assertNotIn(
                    key, body_text, "外发请求体里出现了个人字段键名：%s" % key
                )
        for marker in FORBIDDEN_MARKERS:
            with self.subTest(kind="marker", value=marker):
                self.assertNotIn(
                    marker, body_text, "外发请求体里出现了私人数据：%s" % marker
                )

    def run_remote_once(self, provider):
        """往真实装配出来的队列里塞一个远程作业并跑掉。"""
        self.app.queue.providers["remote"] = provider
        self.app.queue.enqueue(CLUSTER_ID, "hash-privacy", "remote")
        return self.app.queue.run_due(limit=5, remote_limit=5)

    def stored_judgments(self):
        with self.database.connect() as connection:
            return [
                json.loads(row["content_json"])
                for row in connection.execute(
                    "SELECT content_json FROM judgments WHERE cluster_id=?",
                    (CLUSTER_ID,),
                )
            ]


class RemoteRequestBodyTests(PrivacyEgressBase):
    """核心：捕获真实外发的请求体，断言里面没有私人数据。"""

    def test_fixture_actually_carries_personal_data(self):
        """先证明夹具里真有这些个人数据 —— 否则下面"外发里没有"就是空断言。"""
        with self.database.connect() as connection:
            interest_names = {
                row["name"]
                for row in connection.execute("SELECT name FROM interest_objects")
            }
            link_relationships = {
                row["relationship"]
                for row in connection.execute("SELECT relationship FROM interest_links")
            }
            manual_signals = connection.execute(
                "SELECT summary,why_it_matters FROM signals WHERE source_type='manual'"
            ).fetchall()
        forecasts, _ = ForecastService(self.database).list_forecasts()

        self.assertIn(MARKER_P1_INTEREST, interest_names)
        self.assertIn(MARKER_P3_INTEREST, interest_names)
        self.assertIn(MARKER_LINK, link_relationships)
        self.assertIn(MARKER_FORECAST, {item["title"] for item in forecasts})
        self.assertEqual(
            [(row["summary"], row["why_it_matters"]) for row in manual_signals],
            [(MARKER_SIGNAL, MARKER_SIGNAL_WHY)],
        )

    def test_openai_request_body_carries_no_personal_data(self):
        transport = CapturingTransport(
            {"output_text": json.dumps(remote_payload(), ensure_ascii=False)}
        )
        provider = OpenAIResponsesProvider(
            model="test-model", token_loader=lambda: "unit-test-token", transport=transport
        )

        self.run_remote_once(provider)

        self.assertEqual(len(transport.calls), 1, "远程请求没有真的发出去")
        self.assert_no_personal_data(transport.body_text)

    def test_deepseek_request_body_carries_no_personal_data(self):
        transport = CapturingTransport(
            {"choices": [{"message": {"content": json.dumps(remote_payload(), ensure_ascii=False)}}]}
        )
        provider = DeepSeekChatProvider(
            model="test-model", token_loader=lambda: "unit-test-token", transport=transport
        )

        self.run_remote_once(provider)

        self.assertEqual(len(transport.calls), 1, "远程请求没有真的发出去")
        self.assert_no_personal_data(transport.body_text)

    def test_p1_interests_never_leave_even_if_a_context_loader_is_wired_back(self):
        """把"个人上下文加载器"手工接回去，provider 仍不许把它序列化出去。

        这条不依赖 `application.py` 里的那个 loader 函数是否还存在：加载器是我
        自己写的，形状照抄缺陷里的那个。它钉的是**队列/ provider 边界**这个更底层
        的承诺 —— 就算将来有人又把个人画像接回远程路径，也不该外发。

        同时覆盖 `privacy_level='P1'`（界面标"仅本机"）的对象。
        """
        self.app.queue.personal_context_loader = lambda cluster_id: {
            "用户个人近况（用户本人主动记录，判断相关性时优先参考）": [
                {
                    "recorded_at": "2026-09-01T00:00:00",
                    "situation": MARKER_SIGNAL,
                    "relevance": MARKER_SIGNAL_WHY,
                }
            ],
            "interests": {
                "objects": [
                    {"name": MARKER_P1_INTEREST, "category": "cashflow", "importance": 5},
                    {"name": MARKER_P3_INTEREST, "category": "health", "importance": 5},
                ],
                "links": [
                    {
                        "source": MARKER_P1_INTEREST,
                        "target": MARKER_P3_INTEREST,
                        "relationship": MARKER_LINK,
                        "impact": "negative",
                        "strength": 5,
                    }
                ],
            },
            "recent_forecasts": [{"title": MARKER_FORECAST, "probability": 0.5}],
        }
        transport = CapturingTransport(
            {"choices": [{"message": {"content": json.dumps(remote_payload(), ensure_ascii=False)}}]}
        )
        provider = DeepSeekChatProvider(
            model="test-model", token_loader=lambda: "unit-test-token", transport=transport
        )

        self.run_remote_once(provider)

        self.assertEqual(len(transport.calls), 1, "远程请求没有真的发出去")
        self.assert_no_personal_data(transport.body_text)

    def test_local_judgment_never_loads_personal_context(self):
        """本地启发式研判本来就不该读个人画像 —— 即使加载器被接上。

        本地路径是"零外发"的兜底：它不看利益地图，所以就算远程被关掉、任务降级
        本地，也不存在把画像读出内存的风险。判据是加载器一次都没被调用。
        """
        calls = []

        def spying_loader(cluster_id):
            calls.append(cluster_id)
            return None

        self.app.queue.personal_context_loader = spying_loader
        self.app.queue.enqueue(CLUSTER_ID, "hash-local", "local")

        self.app.queue.run_due(limit=5, remote_limit=5)

        self.assertEqual(calls, [], "本地研判路径调用了个人上下文加载器")


class PublicBundleStillWorksTests(PrivacyEgressBase):
    """反向断言：不许用"删掉功能"来实现隐私。"""

    def test_public_evidence_package_is_still_sent_intact(self):
        transport = CapturingTransport(
            {"output_text": json.dumps(remote_payload(), ensure_ascii=False)}
        )
        provider = OpenAIResponsesProvider(
            model="test-model", token_loader=lambda: "unit-test-token", transport=transport
        )

        self.run_remote_once(provider)

        text = transport.body_text
        for expected in (
            CLUSTER_ID,
            MARKER_PUBLIC_TITLE,
            MARKER_PUBLIC_SUMMARY,
            EVIDENCE_TITLE,
            EVIDENCE_SUMMARY,
            EVIDENCE_DOMAIN,
            EVIDENCE_URL,
            "E2",
            "policy",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text, "公开证据包的字段被误删了：%s" % expected)

    def test_remote_result_and_personal_action_are_still_persisted(self):
        """远程成功研判仍要落库，且 `personal_action` 不许被顺手阉掉。"""
        transport = CapturingTransport(
            {"output_text": json.dumps(remote_payload(), ensure_ascii=False)}
        )
        provider = OpenAIResponsesProvider(
            model="test-model", token_loader=lambda: "unit-test-token", transport=transport
        )

        summary = self.run_remote_once(provider)

        self.assertEqual(summary["succeeded"], 1)
        stored = self.stored_judgments()
        self.assertEqual(len(stored), 1)
        self.assertTrue(stored[0]["personal_action"].strip())
        self.assertEqual(stored[0]["fact_summary"], "公开政策调整")
        with self.database.connect() as connection:
            provider_name = connection.execute(
                "SELECT provider FROM judgments WHERE cluster_id=?", (CLUSTER_ID,)
            ).fetchone()["provider"]
        self.assertNotEqual(provider_name, "local", "远程研判被降级成本地了")

    def test_local_provider_still_produces_personal_action_without_personal_data(self):
        """离线兜底仍要给出 `personal_action`，而且它只能基于公开证据包。

        这是"隐私边界"与"产品功能"的交接点：本地 provider 拿到的 bundle 里
        既没有个人画像、也不许出现个人字段，但仍要输出面向用户的行动方向。
        """
        bundle = build_public_bundle(
            {
                "cluster_id": CLUSTER_ID,
                "title": MARKER_PUBLIC_TITLE,
                "summary": MARKER_PUBLIC_SUMMARY,
                "evidence_level": "E2",
                "categories": ["policy"],
            },
            [
                {
                    "source_id": "S-1",
                    "title": EVIDENCE_TITLE,
                    "summary": EVIDENCE_SUMMARY,
                    "canonical_url": EVIDENCE_URL,
                    "published_at": STAMP,
                }
            ],
        )

        result = LocalHeuristicProvider().analyze(bundle)

        self.assertTrue(result.personal_action.strip())
        self.assert_no_personal_data(json.dumps(bundle.to_public_dict(), ensure_ascii=False))

    def test_bundle_serialization_has_no_personal_context_by_default(self):
        """`build_public_bundle()` 的公开字典里本来就不该有 `personal_context` 键。"""
        bundle = build_public_bundle(
            {
                "cluster_id": CLUSTER_ID,
                "title": MARKER_PUBLIC_TITLE,
                "summary": MARKER_PUBLIC_SUMMARY,
                "evidence_level": "E2",
                "categories": ["policy"],
            },
            [
                {
                    "source_id": "S-1",
                    "title": EVIDENCE_TITLE,
                    "summary": EVIDENCE_SUMMARY,
                    "canonical_url": EVIDENCE_URL,
                    "published_at": STAMP,
                }
            ],
        )

        self.assertNotIn("personal_context", bundle.to_public_dict())

    def test_queue_still_works_without_any_personal_context_loader(self):
        """队列本身不依赖加载器：没有个人上下文时远程研判必须照常成功。"""
        queue = JudgmentQueue(
            self.database,
            providers={"remote": LocalHeuristicProvider()},
            bundle_loader=lambda cluster_id: build_public_bundle(
                {
                    "cluster_id": CLUSTER_ID,
                    "title": MARKER_PUBLIC_TITLE,
                    "summary": MARKER_PUBLIC_SUMMARY,
                    "evidence_level": "E2",
                    "categories": ["policy"],
                },
                [
                    {
                        "source_id": "S-1",
                        "title": EVIDENCE_TITLE,
                        "summary": EVIDENCE_SUMMARY,
                        "canonical_url": EVIDENCE_URL,
                        "published_at": STAMP,
                    }
                ],
            ),
            local_provider=LocalHeuristicProvider(),
            personal_context_loader=None,
        )

        queue.enqueue(CLUSTER_ID, "hash-no-loader", "remote")
        summary = queue.run_due(limit=5, remote_limit=5)

        self.assertEqual(summary["succeeded"], 1)


if __name__ == "__main__":
    unittest.main()
