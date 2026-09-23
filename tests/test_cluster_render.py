"""事件详情页（cluster.js）的**真执行**测试。

为什么必须真执行：这个仓库此前对视图模块只做"源码文本包含某些字符串"的断言，
而那**恰好漏掉了 v1.2 引入的一个会让整页崩掉的缺陷** ——
`renderGywSection(gyw)` 内部引用了不在其作用域的变量 `j`
（用于显示分析状态），一调用就抛 `ReferenceError`，
被 `render()` 的 try/catch 吞掉后，用户看到的是「加载失败：j is not defined」。

本测试把 cluster.js 拿到 node 里**真的跑一遍**：
  1) 正常数据必须渲染出不抛异常，且关键产物（获利方/承担成本方/权力结构/风险信号）
     真的出现在输出里；
  2) 变异对照：把 v1.2 那一版 cluster.js（从 git 取）用**同一套夹具**跑，
     必须复现 ReferenceError —— 证明这个探针有牙，而不是"碰巧通过"；
  3) 机械护栏：cluster.js 用到的每一个 CSS 类都必须在 CSS 里有定义
     （此前有 53 个类根本没定义，整页是裸 HTML）。
"""

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "yuanjian_app" / "static"
CLUSTER_JS = STATIC / "js" / "views" / "cluster.js"

# 带缺陷的那一版 cluster.js。注意必须取 **dc22a18**（第三波提交）——
# 缺陷是第三波引入的；adcfd22 是第三波之前，那一版没有这个问题，
# 拿它做对照会得到"旧版也通过"，从而误判本探针无效（我第一次就踩了这个）。
BROKEN_REV = "dc22a18"

# 真跑 JS 用的 node：优先读环境变量 NODE（CI/本地统一入口），缺省回退到 PATH 上的 node。
NODE = os.environ.get("NODE", "node")

HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

const modulePath = process.argv[2];
const fixturePath = process.argv[3];

let src = fs.readFileSync(modulePath, 'utf8');
// 去掉 ES module 的 import，换成桩（其余视图函数只依赖这几个）
src = src.replace(/^import[^;]*;\s*$/m, '');
src = src.replace(/^export\s+/gm, '');

const stubs = `
const api = async () => ({});
const escapeHtml = (value) => String(value === null || value === undefined ? '' : value)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
const showLoading = () => {};
const showPageError = () => {};
`;

const exportsLine = `
return {renderGywSection, renderScenarios, renderImpacts, renderFactChain, alertBadge, renderSources};
`;

const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
const out = {ok: false};

try {
  const factory = new Function(stubs + src + exportsLine);
  const mod = factory();
  out.gyw = mod.renderGywSection(fixture.gyw, fixture.analysis_status);
  out.scenarios = mod.renderScenarios(fixture.gyw.scenario_paths);
  out.impacts = mod.renderImpacts(fixture.impacts, 'C-1');
  out.factChain = mod.renderFactChain(fixture.judgment, fixture.domains);
  out.sources = mod.renderSources(fixture.items, fixture.domains);
  out.badgeL4 = mod.alertBadge('L4');
  out.ok = true;
} catch (error) {
  out.error = String(error && error.message ? error.message : error);
  out.errorName = String(error && error.name ? error.name : '');
}

process.stdout.write(JSON.stringify(out));
"""


def _run_cluster(cluster_source: str, fixture: dict) -> dict:
    """把给定的 cluster.js 源码跑一遍，返回渲染结果（或错误）。"""
    tmp = pathlib.Path(tempfile.mkdtemp())
    module_path = tmp / "cluster_under_test.js"
    module_path.write_text(cluster_source, encoding="utf-8")
    fixture_path = tmp / "fixture.json"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    harness_path = tmp / "harness.js"
    harness_path.write_text(HARNESS, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(harness_path), str(module_path), str(fixture_path)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=90,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise AssertionError(
            f"node 执行失败 rc={result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
    return json.loads(result.stdout)


def _fixture() -> dict:
    return {
        "analysis_status": "real",
        "gyw": {
            "stakeholders": "【推动方】市水务局；【阻力方】竞争者；【力量对比】甲方强；【群体心理预判】观望",
            "constraints": "预算与工期约束",
            "least_resistance_path": "先易后难",
            "counter_evidence": "可能流标",
            "leading_indicators": "主页挂出泵类采购公告",
            "leading_indicator_hits": [
                {"pattern": "budget_allocation", "signal": "财政资金下达", "risk_boost": 0.10}
            ],
            "leading_boost": 0.10,
            "beneficiaries": [
                {"subject": "某设备供应商", "gain": "获得订单", "evidence_refs": ["s1"]},
                {"subject": "[推断]本地施工方", "gain": "承接配套工程", "evidence_refs": []},
            ],
            "cost_bearers": [
                {"subject": "[推断]未中标竞争者", "cost": "前期投入沉没", "evidence_refs": []}
            ],
            "historical_parallel": "可比事件：某年某置换",
            "historical_parallel_detail": {
                "event": "2014年确定的172项节水供水重大水利工程集中开工期",
                "similarity": "同样以水利投资作为稳增长抓手",
                "difference": "当前地方配套资金到位率不同",
                "matched_keywords": ["水利", "引水"],
                "source": "本机模板类比（不是检索结果，需自行核验）",
            },
            "power_structure": {
                "rule": "local_lead",
                "execution_layer": "基层政府和具体执行机构",
                "veto_analysis": "基层执行层有较大裁量权",
                "delay_risk": "高",
                "matched_orgs": ["河源市水务局"],
                "basis": "证据中识别到机构：河源市水务局；执行阻力按执行层判定（高）。",
            },
            "risk_signal_hit": ["坚决打赢", "攻坚战"],
            "scenario_paths": [
                {
                    "path_type": "most_likely",
                    "label": "最可能路径",
                    "description": "市水务局发布招标 → 评标 → 定标",
                    "trigger": "挂出招标公告",
                    "probability": None,
                    "probability_note": "本机模板不赋概率",
                },
                {
                    "path_type": "secondary",
                    "label": "次可能路径",
                    "description": "投标人不足导致流标重招",
                    "trigger": "报名不足三家",
                    "probability": None,
                    "probability_note": "本机模板不赋概率",
                },
                {
                    "path_type": "black_swan",
                    "label": "黑天鹅路径",
                    "description": "预算被上级收回，项目暂缓",
                    "trigger": "超出模型范围",
                    "probability": None,
                    "probability_note": "无法赋概率",
                },
            ],
            "source_domains": ["water.gov.cn"],
        },
        "judgment": {
            "fact_summary": "某市水务局发布泵类采购公告",
            "actors": ["河源市水务局"],
            "causal_chain": ["公告发布", "投标", "定标"],
            "up_triggers": ["挂出正式招标公告"],
            "down_triggers": ["公告撤销"],
            "gyw": {"source_domains": ["water.gov.cn"]},
        },
        "scenario_paths": [],
        "domains": ["water.gov.cn"],
        "items": [
            {
                "title": "某市水务局发布泵类采购公告",
                "canonical_url": "https://water.gov.cn/a",
                "source_domain": "water.gov.cn",
                "published_at": "2026-09-17T00:00:00Z",
                "summary": "预算 1200 万元",
            }
        ],
        "impacts": [
            {
                "impact_id": "P-1",
                "alert_level": "L3",
                "interest_name": "水泵业务收入",
                "candidate": {
                    "title": "截至2026-12-31：是否挂出泵类采购公告",
                    "probability_low": 0.36,
                    "probability_high": 0.64,
                    "base_rate": 0.4,
                    "base_rate_sample": 12,
                    "magnitude_line": "范围 地方/区域；量级 未量化",
                    "magnitude": {"scope": "地方/区域", "level": "未量化", "numbers": []},
                    "risk_signal_hit": ["坚决打赢"],
                    "observable_signals": "主页挂出泵类采购公告",
                    "recommended_action": "关注招标进展",
                    "window_end": "2026-12-31",
                },
            }
        ],
    }


@unittest.skipUnless(sys.platform.startswith("win") or True, "需要 node")
class ClusterDetailRenderTests(unittest.TestCase):
    """真跑 cluster.js，而不是"源码里有没有某个字符串"。"""

    def test_render_functions_execute_without_throwing(self):
        outcome = _run_cluster(CLUSTER_JS.read_text(encoding="utf-8"), _fixture())

        self.assertTrue(
            outcome.get("ok"),
            f"渲染抛异常：{outcome.get('errorName')}: {outcome.get('error')}",
        )

    def test_beneficiaries_and_cost_bearers_reach_the_page(self):
        """核心方法论产物必须有出口（审计第四波 4.1）。"""
        outcome = _run_cluster(CLUSTER_JS.read_text(encoding="utf-8"), _fixture())
        gyw = outcome["gyw"]

        self.assertIn("某设备供应商", gyw)
        self.assertIn("获得订单", gyw)
        self.assertIn("本地施工方", gyw)
        self.assertIn("未中标竞争者", gyw)
        # [推断] 与"有出处"要能区分开
        self.assertIn("推断", gyw)
        self.assertIn("有出处", gyw)

    def test_power_structure_and_risk_signal_are_rendered(self):
        outcome = _run_cluster(CLUSTER_JS.read_text(encoding="utf-8"), _fixture())
        gyw = outcome["gyw"]

        self.assertIn("河源市水务局", gyw)
        self.assertIn("执行阻力", gyw)
        self.assertIn("高", gyw)
        self.assertIn("坚决打赢", gyw)

    def test_scenario_panel_carries_no_fabricated_probability(self):
        """路径概率恒为 null，界面必须说"不给概率"，不得印出百分比。"""
        outcome = _run_cluster(CLUSTER_JS.read_text(encoding="utf-8"), _fixture())
        scenarios = outcome["scenarios"]

        self.assertIn("最可能路径", scenarios)
        self.assertIn("黑天鹅路径", scenarios)
        self.assertIn("不给概率", scenarios)
        self.assertNotIn("%", scenarios)

    def test_actors_and_domains_are_not_conflated(self):
        """actors 是参与方、来源域名是来源域名 —— 不能把"某网站"显示成"当事方"。"""
        outcome = _run_cluster(CLUSTER_JS.read_text(encoding="utf-8"), _fixture())
        chain = outcome["factChain"]

        self.assertIn("相关方", chain)
        self.assertIn("河源市水务局", chain)
        self.assertIn("来源域名", chain)
        self.assertIn("water.gov.cn", chain)

    def test_impact_card_shows_base_rate_and_magnitude(self):
        outcome = _run_cluster(CLUSTER_JS.read_text(encoding="utf-8"), _fixture())
        impacts = outcome["impacts"]

        self.assertIn("基准率", impacts)
        self.assertIn("40%", impacts)
        self.assertIn("未量化", impacts)
        self.assertIn("风险上调依据", impacts)

    def test_attention_label_is_not_called_risk(self):
        """档位是"关注度"不是"风险"（审计 2.11）。"""
        outcome = _run_cluster(CLUSTER_JS.read_text(encoding="utf-8"), _fixture())

        self.assertIn("重点关注", outcome["badgeL4"])
        self.assertNotIn("高风险", outcome["badgeL4"])

    # ── 变异对照 ────────────────────────────────────────────
    def test_mutation_control_the_v12_bug_would_be_caught(self):
        """把 v1.2 那一版 cluster.js 用**同一套夹具**跑，必须复现崩溃。

        这是"探针有牙"的证明：如果旧版也通过，说明本测试根本测不到那个缺陷。
        """
        old_source = subprocess.run(
            ["git", "show", f"{BROKEN_REV}:src/yuanjian_app/static/js/views/cluster.js"],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        if old_source.returncode != 0:
            self.skipTest("取不到 v1.2 的 cluster.js，跳过变异对照")

        outcome = _run_cluster(old_source.stdout, _fixture())

        self.assertFalse(
            outcome.get("ok"),
            "旧版竟然也通过了 —— 本测试捕捉不到那个作用域缺陷，断言不可信",
        )
        self.assertEqual(outcome.get("errorName"), "ReferenceError")
        self.assertIn("j", outcome.get("error", ""))


class ClusterCssContractTests(unittest.TestCase):
    """详情页用到的每个类都必须有样式，否则"接出口"接出来的是裸 HTML。"""

    def test_every_class_used_by_cluster_js_is_defined_in_css(self):
        import re

        css = "".join(
            path.read_text(encoding="utf-8")
            for path in sorted((STATIC / "css").rglob("*.css"))
        )
        defined = set(re.findall(r"\.([A-Za-z][\w-]*)", css))

        source = CLUSTER_JS.read_text(encoding="utf-8")
        used = set()
        for match in re.finditer(r'class="([^"]*)"', source):
            body = re.sub(r"\$\{[^}]*\}", " ", match.group(1))
            for cls in body.split():
                if cls and not any(ch in cls for ch in ("<>`",)):
                    used.add(cls)
        # 由 JS 动态拼出来的类名
        used.update(
            [
                "gyw-list", "gyw-wide", "tag-low", "tag-high", "tag-mid",
                "scenario-most", "scenario-secondary", "scenario-swan",
                "badge-alert-L4", "badge-alert-L3", "badge-alert-L2", "badge-alert-L1",
            ]
        )

        missing = sorted(cls for cls in used if cls not in defined)
        self.assertEqual(missing, [], f"这些类没有样式定义：{missing}")


# ── N1 同款：bindDetailActions 往持久节点 #view-root 重复挂 click 监听（与 today.js 一类）──
# 旧实现每次 render / 进出详情页都往同一持久节点挂新的 click 监听 → 监听随进出次数线性叠加，
# 一次点击会重复触发（重复跳转校准面板）。修复：先按引用移除旧监听再绑定。
# 这里真跑整页 render（而非只跑子渲染函数），数清楚「进出 N 次后单次点击触发几次」。
HARNESS_LEAK = r"""
const fs = require('fs');
const modulePath = process.argv[2];
const fixturePath = process.argv[3];
let src = fs.readFileSync(modulePath, 'utf8');
src = src.replace(/^import[^;]*;\s*$/mg, '');
src = src.replace(/^export\s+/gm, '');

const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));

const stubs = `
const api = async (url, opts) => fixture;
const escapeHtml = (v) => String(v === null || v === undefined ? '' : v)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
const showLoading = () => {};
const showPageError = () => {};
const alert = () => {};
const setTimeout = () => 0;
const document = { createElement: () => ({ set innerHTML(_v) {}, appendChild() {} }) };
const locationHashWrites = [];
const _location = { _hash: '#/cluster/C-1' };
Object.defineProperty(_location, 'hash', {
  get() { return this._hash; },
  set(v) { if (v === '#/calib') locationHashWrites.push(v); this._hash = v; },
});
const location = _location;
`;

const exportsLine = `
return { render, bindDetailActions, locationHashWrites };
`;

function makeRoot() {
  const listeners = [];
  return {
    _listeners: listeners,
    addEventListener(type, fn) { listeners.push({ type, fn }); },
    removeEventListener(type, fn) {
      const i = listeners.findIndex(l => l.type === type && l.fn === fn);
      if (i >= 0) listeners.splice(i, 1);
    },
    querySelector() { return null; },
    appendChild() {},
    classList: { add() {} },
    set innerHTML(_v) {},
    get innerHTML() { return ''; },
  };
}
function makeDetailBtn() {
  const btn = {
    dataset: { action: 'confirm-from-detail', impact: 'P-9', cluster: 'C-1' },
    closest(sel) {
      if (sel === 'button[data-action="confirm-from-detail"]') return btn;
      return null;
    },
  };
  return btn;
}

const out = { ok: false };
(async () => {
  try {
    const factory = new Function('fixture', stubs + src + exportsLine);
    const mod = factory(fixture);
    const root = makeRoot();
    await mod.render(root);
    await mod.render(root);
    await mod.render(root);
    out.listenerCount = root._listeners.filter(l => l.type === 'click').length;
    const btn = makeDetailBtn();
    const ev = { target: btn, stopPropagation() {} };
    const proms = [];
    for (const l of root._listeners) {
      if (l.type === 'click') proms.push(l.fn(ev));
    }
    await Promise.allSettled(proms);
    out.confirmCalls = mod.locationHashWrites.length;
    out.ok = true;
  } catch (error) {
    out.error = String(error && error.message ? error.message : error);
    out.errorName = String(error && error.name ? error.name : '');
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def _run_cluster_render(cluster_source: str, fixture: dict) -> dict:
    """跑整页 render（含 bindDetailActions），返回监听数与点击触发数。"""
    tmp = pathlib.Path(tempfile.mkdtemp())
    module_path = tmp / "cluster_under_test.js"
    module_path.write_text(cluster_source, encoding="utf-8")
    fixture_path = tmp / "fixture.json"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    harness_path = tmp / "harness_leak.js"
    harness_path.write_text(HARNESS_LEAK, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(harness_path), str(module_path), str(fixture_path)],
        text=True, encoding="utf-8", capture_output=True, check=False, timeout=90,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise AssertionError(
            f"node 执行失败 rc={result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
    return json.loads(result.stdout)


class ClusterDetailActionListenerTests(unittest.TestCase):
    """N1 同款：cluster 详情页进出多次后，持久节点上不应叠加 click 监听。"""

    def test_render_three_times_then_one_click_fires_action_once(self):
        outcome = _run_cluster_render(CLUSTER_JS.read_text(encoding="utf-8"), _fixture())
        self.assertTrue(
            outcome.get("ok"),
            f"渲染抛异常：{outcome.get('errorName')}: {outcome.get('error')}",
        )
        self.assertEqual(outcome["listenerCount"], 1, "持久节点上不应叠加 click 监听")
        self.assertEqual(outcome["confirmCalls"], 1, "一次点击不应重复跳转校准面板")

    def test_mutation_control_removing_unbind_step_must_go_red(self):
        """去掉"先移除再绑定"那一步 → 监听叠加 → 一次点击重复触发 → 测试必须变红。"""
        source = CLUSTER_JS.read_text(encoding="utf-8")
        mutated = re.sub(
            r"  if \(root\._clusterDetailClick\) \{.*?root\.removeEventListener\('click', root\._clusterDetailClick\);.*?\}\n",
            "",
            source,
            flags=re.S,
        )
        self.assertNotEqual(mutated, source, "变异替换没生效，对照无效")

        outcome = _run_cluster_render(mutated, _fixture())
        self.assertTrue(outcome.get("ok"), f"变异版渲染抛异常：{outcome.get('error')}")
        self.assertGreater(
            outcome["listenerCount"], 1,
            "去掉先移除那步后，监听应当叠加（>1），否则本探针没牙",
        )
        self.assertGreater(
            outcome["confirmCalls"], 1,
            "去掉先移除那步后，一次点击应重复触发跳转（>1 次）",
        )


if __name__ == "__main__":
    unittest.main()
