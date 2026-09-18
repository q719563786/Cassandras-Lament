"""v1.4 · S0/S1（R-03 / R-04）：让校准面板上的每个数字都**如实**。

两个病：

  R-03 · 面板把**机制问题说成了时间问题**。文案写的是"已结算的预测还太少，
        等远见多跑几轮再看校准"。实测：v1.3 之前的 8,607 条命题的
        `observable_signals` 全为空，而校准准入把它当硬门槛 —— 它们**即使补结算
        也永远进不了校准**。"再等等"和"永远不行"是两件事，面板必须分开说。

  R-04 · 面板把跨来源聚合的数字说成**你的成绩**。`hit_rate` / `false_positive_rate` /
        整体 `brier` / `by_category` 全都不过滤来源，而文案写的是
        "**你确认过的预测里**，真的发生了的比例"。CHANGELOG v1.1 早就写过为什么
        必须拆："账本里的行如果分不出是人选的还是机器填的，那么命中率、误报率和
        Brier 分数就混进了人类从未做过的预测" —— 被点名的三个分数，一个都没拆。
"""

import json
import pathlib
import re
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from yuanjian_app.database import Database
from yuanjian_app.forecasts import ForecastService

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "yuanjian_app" / "static"
CALIB_JS = STATIC / "js" / "views" / "calib.js"
NODE = "node"


def _forecast(
    forecast_id,
    category="finance",
    signals="河源市水务局挂出泵类采购公告",
    probability=0.65,
):
    return {
        "forecast_id": forecast_id,
        "title": "截至2026-09-10：河源市水务局是否挂出泵类采购公告",
        "resolution_criteria": (
            "以河源市水务局官网公告为判定依据；出现泵类采购条目即记为发生。"
        ),
        "observable_signals": signals,
        "window_start": "2026-08-01",
        "window_end": "2026-09-10",
        "probability": probability,
        "category": category,
        # v1.4（R-04）：主指标与基准率只采**人工确认**（confirmed_by='user'）的条目。
        # 夹具不标的话会落进 'unknown' 那一列，本文件测的"人工口径"就无从验证。
        "confirmed_by": "user",
    }


class CalibrationHonestyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "yuanjian.db")
        self.database.initialize()
        self.service = ForecastService(self.database)

    def tearDown(self):
        self.temporary.cleanup()

    def add(self, forecast_id, **kwargs):
        data = _forecast(forecast_id, **kwargs)
        self.service.create_forecast(data)
        return data

    def resolve(self, forecast_id, outcome="occurred", resolved_at="2026-09-11"):
        self.service.resolve(forecast_id, outcome, resolved_at, "test")

    def seed_base_rate(self, category, hits=3, total=5):
        """先给该类别攒够 5 条**人工**二元结算，让后续预测拿得到基准率。

        没有基准率的预测不会进校准（`_calibratable` 的硬门槛），而在建库时
        "同类别人工样本 ≥5"必须先存在 —— 这是自举顺序，不是夹具的取巧。
        种子自己用的是 <50% 的低概率，所以它们不会混进命中/失误的分子分母。
        """
        for index in range(total):
            fid = f"F-SEED-{category}-{index}"
            self.add(fid, category=category, probability=0.05)
            self.resolve(fid, "occurred" if index < hits else "not_occurred")
        return hits / total

    # ------------------------------------------------------------------ #
    # R-03 · 排除构成必须能加总，且分得清"可修 / 不可修"
    # ------------------------------------------------------------------ #
    def test_excluded_breakdown_is_exhaustive_and_mutually_exclusive(self):
        """三类构成之和必须等于 `excluded_total`（在构造数据上验证）。

        优先级：① 命题缺可观测事实（永远不可校准）→ ② 缺基准率（攒样本可修）
        → ③ 有信号但过不了结算性闸（改命题可修）。一条只归一类。
        """
        # ① 历史批次：有结算、但没有 observable_signals。
        legacy = self.add("F-LEGACY", signals="")
        self.resolve("F-LEGACY")
        # ② 可结算但类别里没有人工样本 → base_rate 为 None。
        missing = self.add("F-NOBASE", category="catNoSample")
        self.resolve("F-NOBASE")
        # ③ 有信号、有基准率，但含禁用虚词 → 过不了结算性闸。
        #    先造出基准率：同类别 5 条人工二元结算。
        for index in range(5):
            fid = f"F-SEED-{index}"
            self.add(fid, category="catBanned")
            self.resolve(fid, "occurred" if index < 3 else "not_occurred")
        banned = self.add("F-BANNED", category="catBanned", signals="视情况发布文件")
        self.resolve("F-BANNED")

        summary = self.service.calibration_summary()
        breakdown = summary["excluded_breakdown"]

        self.assertEqual(
            sum(breakdown.values()), summary["excluded_total"],
            f"三类之和必须等于 excluded_total：{breakdown} vs {summary['excluded_total']}",
        )
        self.assertEqual(breakdown["legacy_proposition"], 1)
        self.assertGreaterEqual(breakdown["missing_base_rate"], 1)
        self.assertEqual(breakdown["unsettleable"], 1)
        self.assertIsNotNone(legacy)
        self.assertIsNotNone(missing)
        self.assertIsNotNone(banned)

    def test_legacy_proposition_total_counts_whole_ledger(self):
        """「账本里有多少条命题**结构上**进不了校准」——不限已结算。

        这是面板上那句"补结算不会让它们进入校准"的数据来源。
        """
        for index in range(3):
            self.add(f"F-LEG-{index}", signals="")
        self.add("F-OK-1")

        self.assertEqual(self.service.legacy_proposition_total(), 3)
        self.assertIsNone(self.service.calibration_summary()["brier"])

    def test_open_ledger_has_zero_resolutions_so_panel_says_no_sample(self):
        self.add("F-OPEN-1")
        summary = self.service.calibration_summary()
        self.assertEqual(summary["resolved_total"], 0)
        self.assertEqual(summary["excluded_total"], 0)
        self.assertEqual(summary["excluded_breakdown"]["legacy_proposition"], 0)

    # ------------------------------------------------------------------ #
    # R-04 · 主指标只统计人工来源
    # ------------------------------------------------------------------ #
    def test_hit_rate_is_user_only_and_auto_is_reported_separately(self):
        """3 条人工命中 + 3 条人工未命中 + 10 条自动全命中：
        人工 `hit_rate` 必须是 0.5，且自动那 10 条**不进入**这个值；
        同时自动的分列值必须看得到（分列，不是合并、也不是隐藏）。
        """
        self.seed_base_rate("catMix")  # 先有基准率，下面的行才进得了校准
        for index in range(3):
            fid = f"F-UH-{index}"
            self.add(fid, category="catMix")
            self.resolve(fid, "occurred")
        for index in range(3):
            fid = f"F-UM-{index}"
            self.add(fid, category="catMix")
            self.resolve(fid, "not_occurred")
        for index in range(10):
            fid = f"F-AH-{index}"
            self.add(fid, category="catMix")
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE forecasts SET confirmed_by='auto' WHERE forecast_id=?", (fid,)
                )
            self.resolve(fid, "occurred")

        summary = self.service.calibration_summary()

        user = summary["by_source"]["user"]
        auto = summary["by_source"]["auto"]
        self.assertEqual(user["hit_total"], 3)
        self.assertEqual(user["miss_total"], 3)
        self.assertEqual(user["hit_rate"], 0.5)
        self.assertEqual(summary["hit_rate"], 0.5, "主指标必须就是人工口径")
        # 自动那 10 条确实被统计了 —— 只是分列，不混进上面那个 0.5。
        self.assertEqual(auto["hit_total"], 10)
        self.assertEqual(auto["hit_rate"], 1.0)
        self.assertNotEqual(summary["hit_rate"], 13 / 16)
        # by_category 同口径（只有人工的 3 命中 3 未命中）。
        self.assertEqual(summary["by_category"], {"catMix": 0.5})

    def test_base_rate_for_category_samples_only_user_confirmed_rows(self):
        """基准率是"**我**出题的命中率"。只采人工确认的二元结算。"""
        for index in range(6):
            fid = f"F-AUTO-{index}"
            self.add(fid, category="catAuto")
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE forecasts SET confirmed_by='auto' WHERE forecast_id=?", (fid,)
                )
            self.resolve(fid, "occurred")

        rate, sample = self.service.base_rate_for_category("catAuto")

        self.assertIsNone(rate, "只有自动结算时不得给出基准率")
        self.assertEqual(sample, 0)
        composition = self.service.base_rate_composition("catAuto")
        self.assertEqual(composition["user"], 0)
        self.assertEqual(composition["total"], 6, "样本构成要如实报总数")

    def test_base_rate_composition_reports_human_and_total(self):
        for index in range(2):
            fid = f"F-H-{index}"
            self.add(fid, category="catComp")
            self.resolve(fid, "occurred")
        for index in range(3):
            fid = f"F-M-{index}"
            self.add(fid, category="catComp")
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE forecasts SET confirmed_by='auto' WHERE forecast_id=?", (fid,)
                )
            self.resolve(fid, "occurred")

        composition = self.service.base_rate_composition("catComp")

        self.assertEqual(composition["user"], 2)
        self.assertEqual(composition["total"], 5)
        self.assertEqual(composition["auto"], 3)

    def test_progress_summary_hit_and_miss_are_user_only(self):
        for index in range(2):
            fid = f"F-PU-{index}"
            self.add(fid, category="catP")
            self.resolve(fid, "occurred")
        fid = "F-PA-0"
        self.add(fid, category="catP")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE forecasts SET confirmed_by='auto' WHERE forecast_id=?", (fid,)
            )
        self.resolve(fid, "occurred")

        progress = self.service.progress_summary(
            now=datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
        )

        self.assertEqual(progress["hit_total"], 2, "首页那两栏是人的成绩")
        self.assertEqual(progress["miss_total"], 0)
        self.assertEqual(progress["resolved_total"], 3)
        self.assertEqual(progress["by_source"]["auto"]["hit_total"], 1)

    def test_score_summary_is_user_only(self):
        self.seed_base_rate("catS")
        for index in range(2):
            fid = f"F-SU-{index}"
            self.add(fid, category="catS")
            self.resolve(fid, "occurred")
        fid = "F-SA-0"
        self.add(fid, category="catS")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE forecasts SET confirmed_by='auto' WHERE forecast_id=?", (fid,)
            )
        self.resolve(fid, "occurred")

        summary = self.service.score_summary()

        self.assertEqual(summary["resolved_binary"], 2)
        self.assertEqual(summary["by_source"]["auto"]["usable_total"], 1)


# ---------------------------------------------------------------------- #
# R-03 面板文案：node 真跑，不看源码字符串
# ---------------------------------------------------------------------- #
_HARNESS = r"""
const fs = require('fs');
const { pathToFileURL } = require('node:url');

// 注意 node 的 argv 布局：用 `-e` 时 `process.argv[1]` 就是**第一个**附加参数
// （没有脚本文件名占位），所以这里取 [1] / [2]，不是 [2] / [3]。
const viewUrl = process.argv[1];
const calibBody = process.argv[2];

class FakeElement {
  constructor(tag) {
    this.tag = tag || 'div';
    this.children = [];
    this.attrs = {};
    this.dataset = {};
    this.style = {};
    this.classList = new Set();
    this.innerHTML = '';
    this.textContent = '';
    this.hidden = false;
    this.value = '';
    this.disabled = false;
  }
  appendChild(c) { this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter(x => x !== c); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener() { return null; }
  removeEventListener() { return null; }
  querySelector() { return new FakeElement(); }
  querySelectorAll() { return []; }
  getElementsByTagName() { return []; }
  click() { return null; }
  remove() { return null; }
}
class FakeLocation { constructor() { this.href = 'http://localhost/'; this.search = ''; this.hash = '#/calib'; } }
class FakeDocument {
  constructor() { this.body = new FakeElement('body'); this._byId = {}; }
  createElement(tag) { return new FakeElement(tag); }
  getElementById(id) { if (!this._byId[id]) this._byId[id] = new FakeElement('div'); return this._byId[id]; }
  querySelector() { return new FakeElement(); }
  querySelectorAll() { return []; }
  addEventListener() { return null; }
}
class FakeMutationObserver { constructor() {} observe() {} disconnect() {} }
class FakeResponse { constructor(ok, body) { this.ok = ok; this._body = body; this.status = ok ? 200 : 500; } async text() { return this._body; } }

globalThis.window = { location: new FakeLocation() };
globalThis.location = globalThis.window.location;
globalThis.document = new FakeDocument();
globalThis.MutationObserver = FakeMutationObserver;
globalThis.HTMLElement = FakeElement;
globalThis.Node = class {};
globalThis.localStorage = { getItem: () => null, setItem: () => null, removeItem: () => null };
globalThis.requestAnimationFrame = (cb) => setTimeout(cb, 0);
globalThis.cancelAnimationFrame = (h) => clearTimeout(h);
globalThis.fetch = async (url) => {
  if (String(url).includes('/api/calibration')) return new FakeResponse(true, calibBody);
  return new FakeResponse(true, 'null');
};

(async () => {
  const mod = await import(viewUrl);
  const root = document.getElementById('view-root');
  await mod.render(root);
  process.stdout.write('HTML_START' + root.innerHTML + 'HTML_END');
})().catch(error => {
  process.stderr.write('RENDER_FAIL ' + (error && error.stack || String(error)));
  process.exit(1);
});
"""


def _render_calib(calibration) -> str:
    result = subprocess.run(
        [
            NODE,
            "-e",
            _HARNESS,
            CALIB_JS.resolve().as_uri(),
            json.dumps(calibration, ensure_ascii=False),
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"calib.js 渲染失败：{result.stderr!r}")
    match = re.search(r"HTML_START(.*)HTML_END", result.stdout, re.S)
    if not match:
        raise AssertionError(f"harness 没有吐出 HTML：{result.stdout!r}")
    return match.group(1)


class CalibrationPanelCopyTests(unittest.TestCase):
    """真的把 calib.js 跑起来，看它渲染出来的**文案**。"""

    def test_empty_panel_does_not_promise_that_waiting_will_fix_it(self):
        html = _render_calib(
            {
                "resolved_total": 0,
                "excluded_total": 0,
                "excluded_breakdown": {},
                "legacy_proposition_total": 0,
                "hit_rate": None,
                "false_positive_rate": None,
                "brier": None,
                "brier_series": [],
                "by_category": {},
                "by_source": {},
                "candidates": [],
            }
        )

        for phrase in ("多跑几轮", "再跑几轮", "过一阵子", "等一等"):
            self.assertNotIn(phrase, html, f"空态不该暗示时间会解决：{phrase}")
        self.assertIn("还没有结算过任何预测", html)
        self.assertIn("再等也不会自动变好", html)

    def test_legacy_propositions_are_called_out_as_never_calibratable(self):
        html = _render_calib(
            {
                "resolved_total": 8607,
                "excluded_total": 8607,
                "excluded_breakdown": {
                    "legacy_proposition": 8607,
                    "missing_base_rate": 0,
                    "unsettleable": 0,
                },
                "legacy_proposition_total": 8607,
                "hit_rate": None,
                "false_positive_rate": None,
                "brier": None,
                "brier_series": [],
                "by_category": {},
                "by_source": {},
                "candidates": [],
            }
        )

        self.assertIn("补结算不会让它们进入校准", html)
        self.assertIn("8607", html)

    def test_source_split_is_visible_and_not_merged(self):
        html = _render_calib(
            {
                "resolved_total": 16,
                "excluded_total": 0,
                "excluded_breakdown": {
                    "legacy_proposition": 0,
                    "missing_base_rate": 0,
                    "unsettleable": 0,
                },
                "legacy_proposition_total": 0,
                "hit_rate": 0.5,
                "false_positive_rate": 0.5,
                "brier": 0.1,
                "brier_series": [],
                "by_category": {"finance": 0.5},
                "by_source": {
                    "user": {
                        "resolved_total": 6, "resolved_binary": 6,
                        "hit_total": 3, "miss_total": 3,
                        "hit_rate": 0.5, "false_positive_rate": 0.5, "brier": 0.1,
                    },
                    "auto": {
                        "resolved_total": 10, "resolved_binary": 10,
                        "hit_total": 10, "miss_total": 0,
                        "hit_rate": 1.0, "false_positive_rate": 0.0, "brier": 0.02,
                    },
                    "unknown": {},
                },
                "candidates": [],
            }
        )

        self.assertIn("按来源分列", html)
        self.assertIn("你选的", html)
        self.assertIn("系统自动", html)
        self.assertIn("只有「你选的」计入上面的命中率", html)

    def test_batch_resolve_default_result_is_indeterminate_in_the_ui(self):
        """界面上的结果下拉**不得**默认停在"未发生"。"""
        html = _render_calib(
            {
                "resolved_total": 0, "excluded_total": 0,
                "excluded_breakdown": {}, "legacy_proposition_total": 0,
                "hit_rate": None, "false_positive_rate": None, "brier": None,
                "brier_series": [], "by_category": {}, "by_source": {},
                "candidates": [],
            }
        )

        self.assertIn('value="indeterminate" selected', html)
        self.assertIn("先看将结算多少条", html)
        self.assertIn("批量结算不可撤销", html)
        self.assertNotIn('value="not_occurred" selected', html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
