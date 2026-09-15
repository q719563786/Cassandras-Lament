"""降级兜底路径（`remote_ai.py:1004-1017`）的行为契约（qa · 2026-09-15）。

这块代码只负责一件事：**远程彻底用不了时，本机顶上来**。它挂在
`except RemoteProviderError` 的 `else` 上，下面紧接着就是 `return summary` ——
**没有第二层 catch**。所以 `local_provider.analyze()` 一旦抛异常，就会冲出整个
`run_due`，整轮认知被记成 task error，而不是"这一个作业失败了"。

三段说明为什么这里值得单独一份测试：

**1 · 边界是 off-by-one 的高发区。** 分支条件是
`elif is_remote and attempts < MAX_REMOTE_RETRIES`（`:985`），而上面已经先算过
`attempts = int(job["attempts"]) + 1`（本次尝试也算进去了）。所以对**库里的那一行**：

| 库里 `attempts` | 本次算完 | 走向 |
|---|---|---|
| `<= cap - 2` | `< cap` | `status='retry'`，指数退避 |
| `>= cap - 1` | `>= cap` | 降级本地，`status='remote_error_fallback_local'` |

把那个 `<` 写成 `<=`，退避会静默多打一次远程 API ——**真金白银**、而且**没有任何
现存测试会红**。本文件的那对边界测试就是给这个报警器。

**2 · `MAX_REMOTE_RETRIES` 是 `run_due` 的局部变量**（`:840`，实测 = 4），不是模块常量，
`import` 不到。本文件用 `retry_cap()` 从**源码文本**读它，而不是硬编码 4：
硬编码会让"有人把重试次数从 4 改成 3"只表现为一条假红，而不是让边界自动跟着走。
源码形态一变，`retry_cap()` 会带着明确的提示语红掉。

**3 · 触发条件 ② 当前不可达，但仍然测。** 给定的触发条件是
① `is_remote=True` 且 `attempts >= cap`；② `is_remote=False`（本机 provider 自己出错）。
实测：`application.py:227/231` 把 `providers["local"]` 与本机 `local_provider` 接成
**同一个对象**，`judgment_local.py` 全文**没有任何 `raise`**，而远程 provider 的
`name` 只有 `openai_responses` / `deepseek_chat`（永不与 `"local"` 撞名）。
所以 ② 在当前接线下不可达 —— 这个兜底实际上只有 ① 一种真实触发。
仍然把它写成一条测试（`LocalProviderFailureTests`），但定位是**分支可达性**、
不是线上行为：它守的是"这个 `else` 也吃本机路径"这个事实，将来本机 provider
若真会抛，它立刻从"文档"变成"防线"。
"""

import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.judgments import LocalHeuristicProvider, build_public_bundle
from yuanjian_app.remote_ai import (
    DAILY_REMOTE_BUDGET,
    JudgmentQueue,
    OpenAIResponsesProvider,
    RemoteProviderError,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "yuanjian_app" / "remote_ai.py"
STAMP = "2026-08-11T00:00:00Z"
CLUSTERS = ("C-fallback", "C-auth-a", "C-auth-b", "C-local-job")


def retry_cap() -> int:
    """从源码读 `run_due` 的重试上限。

    它是 `run_due` 内的**局部变量**（`remote_ai.py:840`），导入不到，所以只能读源码。
    刻意不硬编码：上一行注释写着「最多重试3次（指数退避15/30/60分钟）」，
    3 + 1 次失败 = 4，与实测值一致；但"3 次还是 4 次"是产品决定，不是本文件的断言对象 ——
    本文件断的是**边界语义**（`< cap` 退避 / `>= cap` 降级），它要能跟着常量一起走。
    """
    text = SOURCE.read_text(encoding="utf-8")
    match = re.search(r"^\s*MAX_REMOTE_RETRIES\s*=\s*(\d+)", text, re.MULTILINE)
    if match is None:
        raise AssertionError(
            "在 %s 里找不到 `MAX_REMOTE_RETRIES = <数字>`：它可能被改名、提成了模块常量、"
            "或换了写法。本文件刻意从源码读它（局部变量导入不到），"
            "源码形态变了就请同步更新 retry_cap()。" % SOURCE
        )
    return int(match.group(1))


def bundle():
    return build_public_bundle(
        {
            "cluster_id": "C-fallback",
            "title": "兜底测试事件",
            "summary": "公开事件",
            "evidence_level": "E2",
            "categories": ["policy"],
        },
        [
            {
                "source_id": "S-1",
                "title": "来源标题",
                "summary": "来源摘要",
                "canonical_url": "https://news.example/fallback",
                "published_at": STAMP,
            }
        ],
    )


def failing_transport(kind="network", status=None):
    """真·provider 的替身 transport：必失败，抛 `RemoteProviderError`。

    用真 provider + 假 transport，而不是整个假 provider：这样从 transport 的异常
    到 `analyze` 的透传、再到 `run_due` 的分支，整条链都是真代码在跑。
    """

    def transport(url, headers, body, timeout):
        raise RemoteProviderError(kind, status)

    return transport


def succeeding_transport(payload):
    """按 OpenAI Responses 的结构回一份合法研判 JSON。"""

    def transport(url, headers, body, timeout):
        return {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(payload)}],
                }
            ]
        }

    return transport


class RecordingLocalProvider:
    """本机兜底的替身：记录被喂了什么，返回一份合法的本机研判。"""

    def __init__(self):
        self.seen = []

    def analyze(self, evidence):
        self.seen.append(evidence)
        return LocalHeuristicProvider().analyze(evidence)


class FailingLocalProvider:
    """`providers["local"]` 的替身：本机 provider 自己抛错（触发条件 ② 需要的形状）。"""

    model = "local"

    def __init__(self, kind="network"):
        self.kind = kind
        self.calls = 0

    def analyze(self, evidence):
        self.calls += 1
        raise RemoteProviderError(self.kind)


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 8, 11, 8, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


class _QueueHarness(unittest.TestCase):
    """夹具基类：**刻意不放任何 `test_` 方法**。

    父类的用例会被子类各继承跑一遍（用例数虚高、真实覆盖被掩盖），所以夹具和用例
    必须分家。这些子类都只继承本类。
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.clock = MutableClock()
        self.cap = retry_cap()
        with self.database.connect() as connection:
            for cluster_id in CLUSTERS:
                connection.execute(
                    "INSERT INTO event_clusters(cluster_id,title,summary,first_seen_at,"
                    "last_seen_at,evidence_level,evidence_hash,categories_json,status,"
                    "needs_judgment,independent_domains,primary_source_count,created_at,"
                    "updated_at) VALUES (?,?,?,?,?,'E2','h','[\"policy\"]','active',1,1,1,?,?)",
                    (cluster_id, "标题" + cluster_id, "", STAMP, STAMP, STAMP, STAMP),
                )

    def queue(self, *, remote=None, fallback=None, local_entry=None):
        providers = {}
        if remote is not None:
            providers["remote"] = remote
        if local_entry is not None:
            providers["local"] = local_entry
        return JudgmentQueue(
            self.database,
            providers=providers,
            bundle_loader=lambda cluster_id: bundle(),
            local_provider=fallback or LocalHeuristicProvider(),
            now=self.clock,
            daily_budget=DAILY_REMOTE_BUDGET,
        )

    def remote_provider(self, kind="network", status=None):
        return OpenAIResponsesProvider(
            model="fallback-test-model",
            token_loader=lambda: "unit-test-token",
            transport=failing_transport(kind, status),
        )

    def enqueue(self, queue, cluster_id, provider="remote", attempts=0):
        job_id = queue.enqueue(cluster_id, "hash-" + cluster_id, provider)
        if attempts:
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE judgment_jobs SET attempts=? WHERE job_id=?",
                    (attempts, job_id),
                )
        return job_id

    def job(self, job_id):
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM judgment_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return dict(row)

    def judgments(self, cluster_id):
        with self.database.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT judgment_id,provider,evidence_hash FROM judgments "
                    "WHERE cluster_id=? ORDER BY created_at,judgment_id",
                    (cluster_id,),
                )
            ]

    def cluster(self, cluster_id):
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT latest_judgment_id,needs_judgment FROM event_clusters "
                "WHERE cluster_id=?",
                (cluster_id,),
            ).fetchone()
        return dict(row)

    def expected_request_chars(self):
        """被测代码的口径：`len(json.dumps(bundle.to_public_dict(), ensure_ascii=False))`。

        `to_public_dict()` 里没有任何时间戳/随机字段（见 `judgment_models.py:123-137`），
        所以这个值是确定的，可以精确比对。
        """
        return len(json.dumps(bundle().to_public_dict(), ensure_ascii=False))


# ---------------------------------------------------------------------------
# 边界成对：attempts = cap-2 必须退避，attempts = cap-1 必须降级
# ---------------------------------------------------------------------------


class RetryCapBoundaryTests(_QueueHarness):
    def test_an_attempt_that_still_fits_under_the_cap_is_retried_not_degraded(self):
        """边界上半边：`attempts = cap-2` → 本次算完是 cap-1，仍 `< cap` → 退避。

        没有这条，"边界改成永远降级"也能让下面几条全绿 —— 那测的是"一定会降级"，
        不是"到点才降级"，等于把远程研判的机会白扔掉。
        """
        queue = self.queue(remote=self.remote_provider("rate_limit"))
        job_id = self.enqueue(queue, "C-fallback", attempts=self.cap - 2)

        summary = queue.run_due(limit=5, remote_limit=5)

        row = self.job(job_id)
        self.assertEqual(row["status"], "retry", "还没到上限就降级了 —— 白白丢掉远程研判")
        self.assertEqual(row["attempts"], self.cap - 1)
        self.assertEqual(row["last_error"], "rate_limit")
        self.assertIsNone(row["finished_at"], "重试中的作业不该有 finished_at")
        self.assertGreater(
            row["next_attempt_at"], row["created_at"], "退避时间没有推到未来"
        )
        self.assertEqual(self.judgments("C-fallback"), [], "重试路径不该落任何研判")
        self.assertEqual(summary, {"succeeded": 0, "deferred": 0, "failed": 1})

    def test_an_attempt_that_reaches_the_cap_degrades_to_local(self):
        """边界下半边：`attempts = cap-1` → 本次算完等于 cap，不再 `< cap` → 降级。

        把 `:985` 的 `<` 改成 `<=`，这条会红。**这就是那个 off-by-one 的报警器。**
        """
        queue = self.queue(remote=self.remote_provider("network"))
        job_id = self.enqueue(queue, "C-fallback", attempts=self.cap - 1)

        queue.run_due(limit=5, remote_limit=5)

        self.assertEqual(
            self.job(job_id)["status"],
            "remote_error_fallback_local",
            "用完了重试机会却没有降级本地 —— 远程彻底用不了时就没有兜底了",
        )


# ---------------------------------------------------------------------------
# 断言 1-3：落库、任务行字段、不外抛
# ---------------------------------------------------------------------------


class FallbackPersistenceTests(_QueueHarness):
    def test_the_degradation_writes_a_real_local_judgment_row(self):
        """断言 1：降级必须**真的写出一条本机研判**，不是只改状态。

        查的是 `judgments` 里的真实写入行（不看返回值），并要求簇的
        `latest_judgment_id` 指向它 —— 否则前端拿到的兜底研判是空的。
        """
        fallback = RecordingLocalProvider()
        queue = self.queue(remote=self.remote_provider("network"), fallback=fallback)
        self.enqueue(queue, "C-fallback", attempts=self.cap - 1)

        queue.run_due(limit=5, remote_limit=5)

        self.assertEqual(len(fallback.seen), 1, "本机 provider 没有被调用过")
        rows = self.judgments("C-fallback")
        self.assertEqual(len(rows), 1, "兜底没有写出研判行")
        self.assertEqual(rows[0]["provider"], "local", "兜底写出来的行不是本机研判")
        self.assertEqual(rows[0]["evidence_hash"], "hash-C-fallback")
        cluster = self.cluster("C-fallback")
        self.assertEqual(
            cluster["latest_judgment_id"],
            rows[0]["judgment_id"],
            "兜底研判没有成为簇的最新研判 —— 前端看不到它",
        )
        self.assertEqual(cluster["needs_judgment"], 0)

    def test_the_job_row_records_the_fallback_state_and_the_error_kind(self):
        """断言 2：任务行的五个字段都写对。少一个，面板就说不清刚才发生了什么。"""
        queue = self.queue(remote=self.remote_provider("timeout"))
        job_id = self.enqueue(queue, "C-fallback", attempts=self.cap - 1)

        queue.run_due(limit=5, remote_limit=5)

        row = self.job(job_id)
        self.assertEqual(row["status"], "remote_error_fallback_local")
        self.assertEqual(row["attempts"], self.cap, "attempts 没有把这次失败算进去")
        self.assertEqual(row["finished_at"], "2026-08-11T08:00:00Z", "finished_at 没写")
        self.assertEqual(row["last_error"], "timeout", "last_error 没带出失败类型")
        self.assertEqual(
            row["request_chars"],
            self.expected_request_chars(),
            "request_chars 没写或口径不对 —— 它是「这次到底发了多少字符」的唯一记录",
        )

    def test_run_due_returns_normally_and_counts_one_failure(self):
        """断言 3：兜底必须**自己兜住** —— 不许把异常抛出 `run_due`。

        这块代码下面是 `return summary`，没有第二层 catch：兜底里任何异常都会冲出
        整轮认知，被记成 task error。所以"不外抛"要和"计数 +1"一起钉。
        """
        queue = self.queue(remote=self.remote_provider("http_error", status=500))
        self.enqueue(queue, "C-fallback", attempts=self.cap - 1)

        summary = queue.run_due(limit=5, remote_limit=5)

        self.assertEqual(summary, {"succeeded": 0, "deferred": 0, "failed": 1})


# ---------------------------------------------------------------------------
# 断言 4：认证错误不走兜底
# ---------------------------------------------------------------------------


class AuthNeverDegradesTests(_QueueHarness):
    def test_an_auth_error_pauses_the_provider_instead_of_degrading(self):
        """断言 4：认证错误走 `paused_auth`（`:964-984`），**绝不**落进兜底。

        理由：auth 是"配置问题"，不是"这次调用不巧"。把它当普通失败降级，会把
        「用户的 token 过期了」静默成「今天用本机启发式凑合」，用户再也不知道远程
        其实一直没在工作；而且后面那些 429 / 网络抖动的重试预算会被它白白吃掉。

        顺带钉住连带暂停：同 provider 的其他排队作业也必须一起停下，否则它们会排着队
        一次次撞同一面 auth 墙。第二个作业故意用 `remote_limit=1` 挡在循环外 ——
        这样它只可能是被**连带**暂停的，不可能是自己跑出来的。
        """
        queue = self.queue(remote=self.remote_provider("auth"))
        first = self.enqueue(queue, "C-auth-a", attempts=self.cap - 1)
        self.clock.value += timedelta(minutes=1)
        second = self.enqueue(queue, "C-auth-b", attempts=0)

        queue.run_due(limit=5, remote_limit=1)

        first_row, second_row = self.job(first), self.job(second)
        self.assertEqual(first_row["status"], "paused_auth", "auth 没有走暂停分支")
        self.assertEqual(first_row["last_error"], "auth")
        self.assertEqual(
            second_row["status"],
            "paused_auth",
            "同 provider 的其他排队作业没有被连带暂停",
        )
        self.assertEqual(second_row["last_error"], "auth_paused")
        self.assertNotEqual(
            first_row["status"],
            "remote_error_fallback_local",
            "auth 被当成普通失败降级了 —— 用户会以为远程还在工作",
        )
        self.assertEqual(self.judgments("C-auth-a"), [], "auth 路径不该落兜底研判")
        self.assertEqual(self.judgments("C-auth-b"), [], "被连带暂停的作业不该落研判")


# ---------------------------------------------------------------------------
# 断言 5：反向 —— 远程成功时不许出现兜底
# ---------------------------------------------------------------------------


class RemoteSuccessControlTests(_QueueHarness):
    def test_a_successful_remote_call_writes_no_local_judgment(self):
        """断言 5（反向）：远程成功时不许出现兜底状态，也不许写本机研判。

        没有这条，"兜底被写成无条件执行"也能让上面全绿 —— 那测的是"兜底永远发生"，
        不是"只在远程用不了时发生"。这里故意把 `attempts` 也设到 cap-1，
        让「降级条件已满足」与「远程这次成功了」同时成立，看谁说了算。
        """
        payload = LocalHeuristicProvider().analyze(bundle()).to_dict()
        provider = OpenAIResponsesProvider(
            model="fallback-test-model",
            token_loader=lambda: "unit-test-token",
            transport=succeeding_transport(payload),
        )
        queue = self.queue(remote=provider)
        job_id = self.enqueue(queue, "C-fallback", attempts=self.cap - 1)

        summary = queue.run_due(limit=5, remote_limit=5)

        row = self.job(job_id)
        self.assertEqual(row["status"], "succeeded")
        self.assertNotEqual(row["status"], "remote_error_fallback_local")
        rows = self.judgments("C-fallback")
        self.assertEqual(len(rows), 1, "远程成功却没有落研判")
        self.assertEqual(rows[0]["provider"], "remote", "远程成功却写了本机研判")
        self.assertEqual(summary, {"succeeded": 1, "deferred": 0, "failed": 0})


# ---------------------------------------------------------------------------
# 触发条件 ②：本机路径也吃这个 else（分支可达性，不是线上行为）
# ---------------------------------------------------------------------------


class LocalProviderFailureTests(_QueueHarness):
    def test_a_local_job_that_fails_also_lands_in_the_fallback_status(self):
        """`is_remote=False` 时 `elif is_remote and ...` 恒为假，所以本机作业只要 provider
        抛 `RemoteProviderError`，就会落进同一个 `else`。

        **当前不可达**（见文件头第 3 段：`providers["local"]` 恒为 `LocalHeuristicProvider`，
        而它全文没有 `raise`）。之所以还是测：这条守的是"这个 `else` 也吃本机路径"这个
        事实。将来本机 provider 真会抛（或有人把远程 provider 注册到 `"local"` 这个名字下），
        它立刻从"文档"变成"防线"。

        另外注意：本机路径上状态名仍然是 `remote_error_fallback_local` —— 名字与语义对不上。
        这条测试会把这个不一致**变成可见的**（改名字会让它红），而不是让它悄悄留在库里。
        """
        local_entry = FailingLocalProvider("network")
        fallback = RecordingLocalProvider()
        queue = self.queue(local_entry=local_entry, fallback=fallback)
        job_id = self.enqueue(queue, "C-local-job", provider="local", attempts=0)

        summary = queue.run_due(limit=5, remote_limit=5)

        self.assertEqual(local_entry.calls, 1, "本机 provider 没有被调用")
        row = self.job(job_id)
        self.assertEqual(row["status"], "remote_error_fallback_local")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["last_error"], "network")
        self.assertEqual(len(fallback.seen), 1, "本机兜底 provider 没有被调用")
        self.assertEqual(
            [r["provider"] for r in self.judgments("C-local-job")], ["local"]
        )
        self.assertEqual(summary, {"succeeded": 0, "deferred": 0, "failed": 1})


if __name__ == "__main__":
    unittest.main()
